# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

import json
import logging
import pprint
from datetime import timedelta
from urllib.parse import parse_qsl, quote, urlsplit

import psycopg2
from markupsafe import Markup
from werkzeug import urls

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import SQL

from odoo.addons.payment import utils as payment_utils
from odoo.addons.payment_epays import const
from odoo.addons.payment_epays import utils as epays_utils
from odoo.addons.payment_epays.controllers.main import EpaysController

_logger = logging.getLogger(__name__)

# The fields that tie a transaction to its ePays payment. They move together when a transaction
# takes over the payment of an earlier attempt.
EPAYS_PAYMENT_LINK_FIELDS = (
    'provider_reference',
    'epays_environment',
    'epays_merchant_domain',
    'epays_expires_at',
    'epays_payment_url',
    'epays_gateway_requested',
    'epays_charged_amount',
    'epays_charged_currency_id',
    'epays_exchange_rate',
    'epays_udf2',
    'epays_udf3',
)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    # Set at initiation and reused by every later call, so that a transaction is always checked
    # against the environment, credentials and domain it was created with.
    epays_environment = fields.Selection(
        string="ePays Environment",
        selection=const.ENVIRONMENTS,
        readonly=True,
    )
    epays_merchant_domain = fields.Char(string="ePays Merchant Domain", readonly=True)
    epays_expires_at = fields.Datetime(string="ePays Link Expiry", readonly=True)
    epays_payment_url = fields.Char(
        string="ePays Payment Page",
        # The URL carries the page's access token: whoever holds it can open the payment page.
        groups='base.group_system',
        help="The ePays payment page of this transaction, reused if the customer pays the same "
             "order again while the payment is still pending.",
        readonly=True,
    )
    epays_gateway_requested = fields.Char(
        string="ePays Gateway Requested",
        help="The ePays gateway the payment page was opened for (merchantGateway), for a payment "
             "method that opens one gateway directly. Empty when the customer chose on the ePays "
             "page.",
        readonly=True,
    )
    # What ePays charges: the transaction's amount, converted when it is in another currency.
    epays_charged_currency_id = fields.Many2one(
        string="Currency Charged", comodel_name='res.currency', readonly=True,
    )
    epays_charged_amount = fields.Monetary(
        string="Amount Charged",
        help="The amount sent to ePays: the transaction's amount, converted with Odoo's exchange "
             "rate when the transaction is in another currency.",
        currency_field='epays_charged_currency_id',
        readonly=True,
    )
    epays_exchange_rate = fields.Float(
        string="Exchange Rate",
        help="The units of the charged currency per unit of the transaction's currency.",
        digits=(12, 6),
        readonly=True,
    )
    epays_udf2 = fields.Char(
        string="Sales Orders Sent (udf2)",
        help="The sales order numbers sent to ePays in udf2, checked again when the payment "
             "completes.",
        readonly=True,
    )
    epays_udf3 = fields.Char(
        string="Invoices Sent (udf3)",
        help="The invoice numbers sent to ePays in udf3, checked again when the payment completes.",
        readonly=True,
    )

    # Payment details received from ePays.
    epays_transaction_id = fields.Char(string="ePays Transaction ID", readonly=True)
    epays_gateway_reference = fields.Char(string="Gateway Reference", readonly=True)
    epays_response_code = fields.Char(string="Response Code", readonly=True)
    epays_response_desc = fields.Char(string="Response Description", readonly=True)
    epays_gateway_id = fields.Char(string="ePays Gateway ID", readonly=True)
    epays_gateway_response = fields.Text(
        string="Gateway Response (raw)",
        help="The gateway response returned by ePays, as JSON. Card data is masked by ePays.",
        readonly=True,
    )
    epays_gateway_response_display = fields.Text(
        string="Gateway Response",
        compute='_compute_epays_gateway_response_display',
    )

    #=== COMPUTE METHODS ===#

    @api.depends('epays_gateway_response')
    def _compute_epays_gateway_response_display(self):
        for tx in self:
            lines = epays_utils.get_gateway_response_lines(
                epays_utils.parse_json(tx.epays_gateway_response)
            )
            tx.epays_gateway_response_display = '\n'.join(
                f'{label}: {value}' for label, value in lines
            ) or False

    #=== BUSINESS METHODS - HELPERS ===#

    def _epays_get_environment(self):
        """ Return the environment of the transaction's ePays payment: the one it was created in,
        or the provider's current mode before it has a payment. """
        self.ensure_one()
        return self.epays_environment or self.provider_id._epays_get_environment()

    def _epays_get_documents(self):
        """ Return the sales orders and the invoices paid by the transaction. Either list is empty
        when its module (`sale`, `account_payment`) is not installed.

        :return: The sales orders and the invoices.
        :rtype: tuple[recordset, recordset]
        """
        self.ensure_one()
        orders = self.sale_order_ids if 'sale_order_ids' in self._fields else []
        invoices = self.invoice_ids if 'invoice_ids' in self._fields else []
        return orders, invoices

    def _epays_request_payment(self, method):
        """ Call the ePays payment of the transaction (`GET` or `DELETE /api/v2/Payments/{id}`),
        with the environment and merchant domain it was created with.

        :param str method: The HTTP method.
        :return: The `data` of the response.
        :rtype: dict
        :raise EpaysApiError: If the request fails.
        """
        self.ensure_one()
        provider = self.provider_id
        return provider._epays_make_request(
            method,
            f'{const.PAYMENTS_PATH}/{quote(self.provider_reference or "", safe="")}',
            environment=self._epays_get_environment(),
            domain=self.epays_merchant_domain or provider._epays_get_merchant_domain(),
        )

    #=== BUSINESS METHODS - PAYMENT FLOW ===#

    def _get_specific_rendering_values(self, processing_values):
        """ Override of `payment` to return ePays-specific rendering values.

        Note: self.ensure_one() from `_get_processing_values`

        :param dict processing_values: The generic and specific processing values of the transaction
        :return: The dict of provider-specific rendering values
        :rtype: dict
        """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'epays':
            return res

        provider = self.provider_id
        # An already open checkout page can still create a transaction after the provider was
        # disabled.
        if provider.state not in ('test', 'enabled'):
            raise ValidationError("ePays: " + _("This payment method is no longer available."))

        environment = provider._epays_get_environment()
        gateway_id = self._epays_get_gateway_id(environment)

        # One live ePays payment per order: going back and paying again reuses the pending payment.
        reused_payment_url = self._epays_reuse_or_retire_earlier_payment(environment, gateway_id)
        if reused_payment_url:
            return self._epays_get_redirect_values(reused_payment_url)

        domain = provider._epays_get_merchant_domain()
        expires_at = fields.Datetime.now().replace(microsecond=0) + timedelta(
            minutes=max(provider.epays_link_expiry, 1)
        )
        charged_amount, charged_currency, exchange_rate = self._epays_get_charge()
        payload = self._epays_prepare_payment_request_payload(
            environment, domain, expires_at, gateway_id=gateway_id
        )
        payment_data = provider._epays_make_request(  # Logs the payload.
            'POST',
            const.PAYMENTS_PATH,
            environment=environment,
            domain=domain,
            payload=payload,
            idempotency_key=payment_utils.generate_idempotency_key(
                self, scope='payment_request_epays'
            ),
        )

        payment_id = payment_data.get('paymentId')
        redirect_url = payment_data.get('redirect')
        if not payment_id or not self._epays_is_valid_redirect_url(redirect_url, environment):
            _logger.warning(
                "refusing the payment link received from ePays for transaction %s:\n%s",
                self.reference, pprint.pformat(payment_data),
            )
            raise ValidationError(provider._epays_get_connection_error_message())

        # The provider reference is set now to allow fetching the payment status after redirection.
        self.write({
            'provider_reference': str(payment_id),
            'epays_environment': environment,
            'epays_merchant_domain': domain,
            'epays_expires_at': expires_at,
            'epays_payment_url': redirect_url,
            'epays_gateway_requested': gateway_id or False,
            'epays_charged_amount': charged_amount,
            'epays_charged_currency_id': charged_currency.id,
            'epays_exchange_rate': exchange_rate,
            'epays_udf2': payload['customFields']['udf2'],
            'epays_udf3': payload['customFields']['udf3'],
        })
        self._epays_cancel_replaced_transactions()
        return self._epays_get_redirect_values(redirect_url)

    @api.model
    def _epays_get_redirect_values(self, redirect_url):
        """ Return the rendering values of the redirect form to the given ePays payment page.

        The query parameters of the payment URL are passed separately: a GET form drops the query
        string of its action.
        """
        url_params = dict(parse_qsl(urlsplit(redirect_url).query, keep_blank_values=True))
        return {'api_url': redirect_url, 'url_params': url_params}

    def _epays_find_reusable_transaction(self, environment):
        """ Return the newest earlier transaction whose ePays payment may serve this one.

        It must be for the same documents: the same sales orders (udf2) or, without sales order,
        the same invoices (udf3). A payment without any document is never reused.

        Note: self.ensure_one()

        :param str environment: The environment this transaction would use.
        :return: The candidate transaction, if any.
        :rtype: recordset of `payment.transaction`
        """
        self.ensure_one()
        custom_fields = self._epays_get_custom_fields()
        if custom_fields['udf2']:
            document_domain = [('epays_udf2', '=', custom_fields['udf2'])]
        elif custom_fields['udf3']:
            document_domain = [('epays_udf3', '=', custom_fields['udf3'])]
        else:
            return self.browse()
        return self.search([
            ('id', '!=', self.id),
            ('provider_id', '=', self.provider_id.id),
            ('epays_environment', '=', environment),
            ('state', 'in', ('draft', 'pending')),
            ('provider_reference', '!=', False),
            ('epays_payment_url', '!=', False),
            ('epays_expires_at', '>', fields.Datetime.now() + const.REUSE_MIN_REMAINING_VALIDITY),
            *document_domain,
        ], order='id desc', limit=1)

    def _epays_reuse_or_retire_earlier_payment(self, environment, gateway_id=None):
        """ Reuse the pending ePays payment of an earlier attempt on the same documents, or retire
        it.

        Odoo creates a new transaction each time the customer clicks Pay, e.g. after going back to
        the cart. Without this, each click would create one more payable ePays payment for the
        same order. The newest earlier attempt is checked with ePays:

        - pending with the same amount, currency and gateway: this transaction takes over its
          payment page;
        - pending with another amount (the cart changed) or opened for another gateway (the
          customer chose another payment method): its payment is voided before a new one is
          created; if ePays refuses the void, the payment is read again and nothing new is created;
        - unknown status (possibly a raw gateway word for a completed capture): handled like a
          pending payment that cannot be reused; ePays voids only a pending payment, so a paid
          one cannot be voided and no second payment is created for it;
        - paid: no new payment is created and the customer is told the order is already paid;
        - failed or cancelled: it is updated in Odoo and a new payment is created.

        If ePays cannot be reached while checking, the generic connection error is raised rather
        than creating a new payment: a failed check must never leave two payable links for one
        order.

        Note: self.ensure_one()

        :param str environment: The environment this transaction would use.
        :param str gateway_id: The ePays gateway this transaction would open, if any.
        :return: The payment page to redirect to if a payment was reused, `None` otherwise.
        :rtype: str|None
        :raise ValidationError: If the order is already paid, or if ePays cannot be reached.
        """
        self.ensure_one()
        candidate = self._epays_find_reusable_transaction(environment)
        if not candidate:
            return None
        # Two clicks on Pay at once must not both take over the same payment: the second waits
        # here, then fails on the first one's update and Odoo retries it with fresh data.
        candidate._epays_lock_for_update()

        payment_data = candidate._epays_fetch_payment_details()  # Raises the generic error.
        target_state, status = epays_utils.get_payment_state(payment_data)
        amount_changed = bool(self._epays_get_amount_mismatches(payment_data))
        # A page opened for one gateway (e.g. Card) shows that gateway only.
        method_changed = (candidate.epays_gateway_requested or None) != (gateway_id or None)
        is_reusable = (
            target_state == 'pending'
            and not amount_changed
            and not method_changed
            and candidate._epays_is_valid_redirect_url(
                candidate.epays_payment_url, candidate.epays_environment
            )
        )
        if is_reusable:
            self._epays_take_over_payment(candidate)
            return self.epays_payment_url

        if target_state in ('pending', None):
            # Not reusable (e.g. the cart changed) or an unknown status: void the old payment before
            # creating a new one. ePays voids only a pending payment, never a paid one.
            if candidate._epays_void() is not None:
                candidate._set_canceled(self._epays_get_replaced_message(
                    amount_changed=target_state == 'pending' and amount_changed,
                    method_changed=target_state == 'pending' and method_changed,
                ))
                return None
            payment_data = candidate._epays_fetch_payment_details()
            target_state, status = epays_utils.get_payment_state(payment_data)
            if target_state != 'done':
                _logger.warning(
                    "ePays refused to void the payment %s of transaction %s (status %s); not "
                    "creating a second payment for transaction %s",
                    candidate.provider_reference, candidate.reference, status, self.reference,
                )
                raise ValidationError(self.provider_id._epays_get_connection_error_message())

        if target_state == 'done':
            # Raising rolls back the whole request, so the earlier transaction cannot be completed
            # here: its return route, the ePays notification or the scheduled check completes it.
            # Completing it in a second database transaction was ruled out: it would wait on the
            # locks of this request (e.g. on the sales order) while this request waits on it.
            _logger.warning(
                "the ePays payment %s of transaction %s is already paid; not creating a new "
                "payment for transaction %s", candidate.provider_reference, candidate.reference,
                self.reference,
            )
            raise ValidationError("ePays: " + _("This order has already been paid."))

        candidate._epays_apply_payment_details(payment_data)
        return None

    def _epays_lock_for_update(self):
        """ Lock the transactions' rows until the end of the database transaction. """
        self.env.cr.execute(
            SQL("SELECT id FROM payment_transaction WHERE id IN %s FOR UPDATE", tuple(self.ids))
        )

    def _epays_get_replaced_message(self, amount_changed=False, method_changed=False):
        """ Return the state message of an earlier transaction replaced by this one. """
        if amount_changed:
            message = _("Replaced by transaction %s (amount changed).", self.reference)
        elif method_changed:
            message = _("Replaced by transaction %s (payment method changed).", self.reference)
        else:
            message = _("Replaced by transaction %s.", self.reference)
        return "ePays: " + message

    def _epays_take_over_payment(self, candidate):
        """ Make this transaction the owner of the pending ePays payment of an earlier attempt,
        and cancel that attempt.

        :param recordset candidate: The earlier transaction, as a `payment.transaction` record.
        :return: None
        """
        self.ensure_one()
        self.write({field: candidate[field] for field in EPAYS_PAYMENT_LINK_FIELDS})
        candidate._set_canceled(self._epays_get_replaced_message())
        _logger.info(
            "reusing the pending ePays payment %s of transaction %s for transaction %s",
            self.provider_reference, candidate.reference, self.reference,
        )

    def _epays_cancel_replaced_transactions(self):
        """ Cancel the earlier draft transactions to which ePays returned the same payment.

        Older ePays servers reuse a payment when a new request carries the same custom fields (udf1
        to udf5) as a payment that is less than ten minutes old, has no result and whose page was
        never opened; ePays skips that check for requests with an idempotency key, which the addon
        always sends. As only the order and invoice numbers are sent, a second attempt on the same
        documents could get the payment of the first attempt back. That first attempt never reached
        the payment page, so nothing was paid on it: it is cancelled, and the payment belongs to
        this transaction.

        Note: self.ensure_one()

        :return: None
        """
        self.ensure_one()
        other_txs = self.search([
            ('provider_reference', '=', self.provider_reference),
            ('provider_code', '=', 'epays'),
            ('id', '!=', self.id),
        ])
        replaced_txs = other_txs.filtered(lambda tx: tx.state == 'draft')
        for tx in other_txs - replaced_txs:
            _logger.warning(
                "the ePays payment %s of transaction %s is shared with transaction %s (state %s); "
                "leaving the latter unchanged",
                self.provider_reference, self.reference, tx.reference, tx.state,
            )
        if replaced_txs:
            _logger.info(
                "ePays returned the payment %s of the unopened transaction(s) %s for transaction "
                "%s; cancelling them", self.provider_reference,
                ', '.join(replaced_txs.mapped('reference')), self.reference,
            )
            replaced_txs._set_canceled(self._epays_get_replaced_message())

    def _epays_is_valid_redirect_url(self, redirect_url, environment):
        """ Check that the payment URL is https on the host of the configured ePays API.

        :param str redirect_url: The payment URL returned by ePays.
        :param str environment: The environment of the transaction.
        :return: Whether the customer can be redirected to the URL.
        :rtype: bool
        """
        if not redirect_url or not isinstance(redirect_url, str):
            return False
        parsed_url = urlsplit(redirect_url.strip())
        api_host = urlsplit(self.provider_id._epays_get_api_url(environment)).hostname
        return bool(
            parsed_url.scheme == 'https'
            and parsed_url.hostname
            and api_host
            and parsed_url.hostname.lower() == api_host.lower()
        )

    def _epays_prepare_payment_request_payload(
        self, environment, domain, expires_at, gateway_id=None
    ):
        """ Create the payload of the payment request based on the transaction values.

        :param str environment: `production`, `sandbox` or `localhost`.
        :param str domain: The merchant domain.
        :param datetime expires_at: The expiry of the payment link, in UTC.
        :param str gateway_id: The ePays gateway to open directly, if any.
        :return: The request payload.
        :rtype: dict
        """
        provider = self.provider_id
        base_url = provider.get_base_url()
        charged_amount, charged_currency, _rate = self._epays_get_charge()
        expires_at_epays = expires_at + const.EPAYS_TIMEZONE_OFFSET

        payload = {
            'amount': f'{charged_amount:.{charged_currency.decimal_places}f}',
            'currency': charged_currency.name,
            'description': self.reference,
            'notifyUrl': urls.url_join(base_url, f'{EpaysController._return_url}/{environment}'),
            'merchantUrl': base_url,
            'merchantDomain': domain,
            'testMode': environment != 'production',
            'lang': self._epays_get_lang(self.env.context.get('lang')),
            'expiryDate': expires_at_epays.strftime(const.EPAYS_DATETIME_FORMAT),
            'customFields': self._epays_get_custom_fields(),
        }
        if gateway_id:
            payload['gatewayId'] = gateway_id
        if provider.epays_send_customer_details:
            customer = self._epays_get_customer_values()
            if customer:
                payload['customer'] = customer
        return payload

    def _epays_get_customer_message(self, outcome, payment_data=None):
        """ Return the message shown to the customer when the payment does not complete, in the
        language of the ePays payment page.

        The message is the transaction's `state_message`, which Odoo shows to the customer on the
        payment status, checkout and portal pages. It is written for the customer: the technical
        details (status, response code, gateway response) are stored on the transaction and logged
        on its documents instead.

        :param str outcome: `declined`, `cancelled`, `mismatch` or `refunded`.
        :param dict payment_data: The `data` of the v2 payment details, for a decline.
        :return: The message.
        :rtype: str
        """
        self.ensure_one()
        tx = self.with_context(lang=self._epays_get_customer_lang_code())
        return tx._epays_compose_customer_message(outcome, payment_data or {})

    def _epays_compose_customer_message(self, outcome, payment_data):
        """ Compose the message of `_epays_get_customer_message` in the language of the context. """
        if outcome == 'mismatch':
            # Money may have been taken: the customer must not simply pay again.
            return _(
                "We could not confirm your payment. Please contact us before trying again, "
                "quoting the reference %s.", self.reference,
            )
        if outcome == 'refunded':
            return _(
                "Your payment was refunded, so your order was not confirmed. Please contact us, "
                "quoting the reference %s.", self.reference,
            )
        if outcome == 'cancelled':
            what_happened = _("Your payment was cancelled.")
        else:
            reason = self._epays_get_response_description(
                payment_data, lang=self._epays_get_lang()
            )
            if reason:
                what_happened = _("Your payment was declined: %s.", reason.rstrip('. '))
            else:
                what_happened = _("Your payment was declined.")
        return ' '.join((
            what_happened, _("You can try again or choose another payment method."),
        ))

    def _epays_get_customer_lang_code(self):
        """ Return the installed Odoo language matching the ePays page language (`en` or `ar`),
        preferring the customer's own variant, e.g. `ar_001`. """
        epays_lang = self._epays_get_lang()
        installed_codes = [code for code, _name in self.env['res.lang'].get_installed()]
        matching_codes = [
            code for code in (self.partner_lang, self.env.lang, *installed_codes)
            if code in installed_codes and code.startswith(epays_lang)
        ]
        return matching_codes[0] if matching_codes else self.env.lang

    def _epays_get_lang(self, preferred_lang=None):
        """ Return the language of the ePays payment page and messages: `en` or `ar`.

        :param str preferred_lang: The language to use first when the provider follows the customer,
                                   e.g. the language of the website.
        :return: `en` or `ar`.
        :rtype: str
        """
        setting = self.provider_id.epays_payment_lang
        if setting in ('en', 'ar'):
            return setting
        lang = preferred_lang or self.partner_lang or ''
        return 'ar' if lang.startswith('ar') else 'en'

    def _epays_get_gateway_id(self, environment):
        """ Return the ePays gateway to open directly for the payment method chosen in Odoo.

        The single "ePays" method lets the customer choose on the ePays page. Any other method opens
        the active gateway synced for it in the transaction's environment; without one, the
        customer chooses on the ePays page as well.

        :param str environment: `production`, `sandbox` or `localhost`.
        :return: The ePays gateway id (merchantGateway), or `None`.
        :rtype: str|None
        """
        payment_method = self.payment_method_id.primary_payment_method_id or self.payment_method_id
        if payment_method.code == const.PRIMARY_PAYMENT_METHOD_CODE:
            return None
        gateway = self.env['payment.epays.gateway'].sudo().search([
            ('provider_id', '=', self.provider_id.id),
            ('environment', '=', environment),
            ('payment_method_id', '=', payment_method.id),
            ('is_active', '=', True),
        ], limit=1)
        if not gateway:
            _logger.warning(
                "no active ePays gateway is linked to the payment method %s (%s) of provider %s; "
                "the customer will choose the payment method on the ePays page. Sync the gateways "
                "from ePays on the provider form.",
                payment_method.name, environment, self.provider_id.name,
            )
            return None
        return gateway.gateway_ref

    def _epays_get_custom_fields(self):
        """ Return the order references sent to ePays in the custom fields, for reconciliation.

        Only udf2 (sales order numbers) and udf3 (invoice numbers) are sent, both always, empty when
        there is no such document. udf1, udf4 and udf5 are left unused.

        Because the references are not unique per attempt, ePays may return the payment of an
        earlier attempt on the same documents; see `_epays_cancel_replaced_transactions`.

        udf6 to udf10 are never sent. ePays reads them as gateway overrides - udf6 is the
        MPGS merchant ID and the Benefit alias, udf7/udf8 the MPGS display name and logo, udf9 the
        MPGS integration type and Benefit mode, udf10 MPGS JSON overrides - so any value there
        reconfigures the gateway: a source string in udf6 made MPGS reject the payment ("Invalid
        character '='") and Benefit answer "Alias Name does not exist". The source and versions
        travel in the User-Agent instead (see `payment.provider._epays_get_user_agent`), which ePays
        stores as the payment's creation agent.

        :return: The custom fields.
        :rtype: dict
        """
        orders, invoices = self._epays_get_documents()
        # A draft invoice is named "/" until it is posted.
        invoice_names = [invoice.name for invoice in invoices if invoice.name not in (False, '/')]
        return {
            'udf2': epays_utils.join_references([order.name for order in orders]),
            'udf3': epays_utils.join_references(invoice_names),
        }

    def _epays_get_customer_values(self):
        """ Return the customer details sent to ePays, without the empty ones. """
        partner = self.partner_id
        mobile = partner.mobile if 'mobile' in partner._fields else False
        values = {
            'fullName': self.partner_name,
            'email': self.partner_email,
            'mobile': mobile or self.partner_phone,
            'phone': self.partner_phone,
            'country': self.partner_country_id.code,
            'city': self.partner_city,
            'state': self.partner_state_id.name,
            'zipCode': self.partner_zip,
            'addressLine1': partner.street or self.partner_address,
            'addressLine2': partner.street2,
        }
        return {key: value for key, value in values.items() if value}

    def _get_tx_from_notification_data(self, provider_code, notification_data):
        """ Override of `payment` to find the transaction based on ePays data.

        The transaction is found by the ePays payment id stored at initiation, never by a reference
        taken from the notification.

        :param str provider_code: The code of the provider that handled the transaction.
        :param dict notification_data: The notification data sent by the provider.
        :return: The transaction if found.
        :rtype: recordset of `payment.transaction`
        :raise ValidationError: If the data match no transaction.
        """
        tx = super()._get_tx_from_notification_data(provider_code, notification_data)
        if provider_code != 'epays' or len(tx) == 1:
            return tx

        payment_id = notification_data.get('paymentId')
        if not payment_id or not isinstance(payment_id, str):
            raise ValidationError("ePays: " + _("Received data with missing payment id."))
        domain = [('provider_reference', '=', payment_id.strip()), ('provider_code', '=', 'epays')]
        # Sandbox and production number their payments independently: the return URL of a payment
        # names its environment. Payments started before it did are looked up by id alone.
        environment = notification_data.get(const.NOTIFICATION_ENVIRONMENT_KEY)
        if environment in dict(const.ENVIRONMENTS):
            domain.append(('epays_environment', '=', environment))
        txs = self.search(domain, order='id desc')
        # ePays can return the payment of an earlier, never opened attempt on the same documents;
        # that attempt was then cancelled and replaced by the newest transaction.
        tx = txs.filtered(lambda t: t.state != 'cancel')[:1] or txs[:1]
        if not tx:
            raise ValidationError("ePays: " + _(
                "No transaction found matching payment id %s.", payment_id
            ))
        return tx

    def _process_notification_data(self, notification_data):
        """ Override of `payment` to process the transaction based on ePays data.

        The notification only carries the payment id: the status is always pulled from ePays.

        Note: self.ensure_one()

        :param dict notification_data: The notification data sent by the provider.
        :return: None
        """
        super()._process_notification_data(notification_data)
        if self.provider_code != 'epays':
            return
        # A done or cancelled transaction cannot change any more: the public return route must not
        # be usable to make Odoo call ePays again and again for it.
        if self.state in ('done', 'cancel'):
            _logger.info(
                "transaction %s is already %s; not checking its ePays payment again",
                self.reference, self.state,
            )
            return

        payment_data = self._epays_fetch_payment_details()
        self._epays_apply_payment_details(payment_data)

    def _epays_fetch_payment_details(self):
        """ Fetch the v2 payment details of the transaction from ePays.

        Note: self.ensure_one()

        :return: The `data` of the v2 payment details.
        :rtype: dict
        """
        return self._epays_request_payment('GET')

    def _epays_apply_payment_details(self, payment_data):
        """ Verify the payment details and update the transaction state accordingly.

        Note: self.ensure_one()

        :param dict payment_data: The `data` of the v2 payment details.
        :return: The transaction if its state changed, an empty recordset otherwise.
        :rtype: recordset of `payment.transaction`
        """
        self.ensure_one()
        self._epays_update_payment_method(payment_data.get('gatewayId'))

        target_state, status = epays_utils.get_payment_state(payment_data)
        updated_txs = self.env['payment.transaction']
        if target_state == 'done':
            updated_txs = self._epays_complete(payment_data)
        elif target_state == 'refunded':
            # The money went back to the customer, so the payment can never complete an order. A
            # refund of an order Odoo already completed is left to the accounting.
            if self.state != 'done':
                _logger.warning(
                    "the ePays payment of transaction %s was refunded before Odoo recorded it; "
                    "not completing the order", self.reference,
                )
                updated_txs = self._set_error(self._epays_get_customer_message('refunded'))
        elif target_state == 'pending':
            if self.state == 'draft':
                updated_txs = self._set_pending()
        elif target_state == 'error':
            updated_txs = self._set_error(
                self._epays_get_customer_message('declined', payment_data)
            )
        elif target_state == 'cancel':
            updated_txs = self._set_canceled(self._epays_get_customer_message('cancelled'))
        else:
            _logger.warning(
                "received data with unrecognised payment status (%s) for transaction with "
                "reference %s; leaving it unchanged", status, self.reference,
            )

        if updated_txs:
            self._epays_store_payment_details(payment_data)
            if self.state in ('done', 'error', 'cancel'):
                self._epays_log_payment_details(payment_data)
        return updated_txs

    def _epays_complete(self, payment_data):
        """ Set the transaction done if the completed ePays payment matches it, in error otherwise.

        :param dict payment_data: The `data` of the v2 payment details.
        :return: The transaction if its state changed, an empty recordset otherwise.
        :rtype: recordset of `payment.transaction`
        """
        mismatches = self._epays_get_mismatches(payment_data)
        if not mismatches:
            return self._set_done()
        # Only the compared values are logged: the details hold the customer's data.
        custom_fields = payment_data.get('customFields')
        custom_fields = custom_fields if isinstance(custom_fields, dict) else {}
        _logger.warning(
            "the payment details received from ePays for transaction %s do not match it (%s): "
            "ePays has %s %s, udf2 %r, udf3 %r; the transaction has %s %s, udf2 %r, udf3 %r",
            self.reference, ', '.join(mismatches),
            payment_data.get('amount'), payment_data.get('currency'),
            custom_fields.get('udf2'), custom_fields.get('udf3'),
            self.amount, self.currency_id.name, self.epays_udf2, self.epays_udf3,
        )
        return self._set_error(self._epays_get_customer_message('mismatch'))

    def _epays_update_payment_method(self, gateway_id):
        """ Set the payment method from the ePays gateway that took the payment, when it is a synced
        gateway linked to a payment method.

        :param str gateway_id: The `gatewayId` of the payment details (the merchantGateway id).
        :return: None
        """
        if epays_utils.is_empty(gateway_id):
            return
        gateway = self.env['payment.epays.gateway'].sudo().search([
            ('provider_id', '=', self.provider_id.id),
            ('environment', '=', self._epays_get_environment()),
            ('gateway_ref', '=', str(gateway_id).strip()),
            ('payment_method_id', '!=', False),
        ], limit=1)
        current_method = self.payment_method_id.primary_payment_method_id or self.payment_method_id
        if gateway and gateway.payment_method_id != current_method:
            self.payment_method_id = gateway.payment_method_id

    def _epays_get_mismatches(self, payment_data):
        """ Return what, among the amount, the currency and the reference, does not match.

        The reference check compares the sales orders (udf2) and invoices (udf3) that this
        transaction sent. It is skipped when ePays does not return `customFields`.

        :param dict payment_data: The `data` of the v2 payment details.
        :return: The names of the mismatching values.
        :rtype: list[str]
        """
        mismatches = self._epays_get_amount_mismatches(payment_data)
        custom_fields = payment_data.get('customFields')
        if isinstance(custom_fields, dict):
            for key, sent_value in (('udf2', self.epays_udf2), ('udf3', self.epays_udf3)):
                if str(custom_fields.get(key) or '') != (sent_value or ''):
                    mismatches.append('reference')
                    break
        return mismatches

    def _epays_get_amount_mismatches(self, payment_data):
        """ Return which of the amount and the currency of the ePays payment differ from what
        this transaction charges (see `_epays_get_charge`).

        :param dict payment_data: The `data` of the v2 payment details.
        :return: `amount` and/or `currency`, or an empty list if both match.
        :rtype: list[str]
        """
        self.ensure_one()
        charged_amount, charged_currency, _rate = self._epays_get_charge()
        mismatches = []
        try:
            amount = float(payment_data.get('amount'))
        except (TypeError, ValueError):
            amount = None
        if amount is None or charged_currency.compare_amounts(amount, charged_amount) != 0:
            mismatches.append('amount')
        if str(payment_data.get('currency') or '').strip().upper() != charged_currency.name:
            mismatches.append('currency')
        return mismatches

    def _epays_get_charge(self):
        """ Return what ePays charges for this transaction: its amount in the currency ePays
        charges in, converted with Odoo's exchange rate of the day when it is in another currency.

        Once the ePays payment exists, the charge stored with it is returned, so that the payment
        is always checked against what was sent, whatever the rates did since.

        Note: self.ensure_one()

        :return: The amount, the currency and the exchange rate (1 without conversion).
        :rtype: tuple[float, res.currency, float]
        :raise ValidationError: If the transaction's currency cannot be converted.
        """
        self.ensure_one()
        if self.epays_charged_currency_id:
            return (
                self.epays_charged_amount, self.epays_charged_currency_id,
                self.epays_exchange_rate or 1.0,
            )
        provider = self.provider_id
        charged_currency = provider._epays_get_charge_currency()
        if self.currency_id == charged_currency:
            return self.amount, self.currency_id, 1.0
        if not provider._epays_can_convert(self.currency_id, self.company_id):
            _logger.warning(
                "no exchange rate between %s and %s for company %s; refusing transaction %s",
                self.currency_id.name, const.CHARGE_CURRENCY, self.company_id.name, self.reference,
            )
            raise ValidationError("ePays: " + _(
                "Payments in %(currency)s cannot be taken at the moment. Please contact us.",
                currency=self.currency_id.name,
            ))
        date = fields.Date.context_today(self)
        exchange_rate = self.env['res.currency']._get_conversion_rate(
            self.currency_id, charged_currency, self.company_id, date
        )
        charged_amount = self.currency_id._convert(
            self.amount, charged_currency, self.company_id, date
        )
        return charged_amount, charged_currency, exchange_rate

    @api.model
    def _epays_get_response_description(self, payment_data, lang='en'):
        """ Return the response description in the given language, falling back to the other one.

        :param dict payment_data: The `data` of the v2 payment details.
        :param str lang: `en` or `ar`.
        :return: The description, or an empty string.
        :rtype: str
        """
        description = payment_data.get('responseDescription')
        if isinstance(description, dict):
            other_lang = 'ar' if lang == 'en' else 'en'
            text = description.get(lang) or description.get(other_lang)
        else:
            text = description
        return str(text).strip() if text else ''

    def _epays_store_payment_details(self, payment_data):
        """ Store the payment details received from ePays on the transaction. """
        gateway_response = payment_data.get('gatewayResponse')
        values = {
            'epays_transaction_id': payment_data.get('transactionId'),
            'epays_gateway_reference': payment_data.get('gatewayReference'),
            'epays_response_code': payment_data.get('responseCode'),
            'epays_response_desc': self._epays_get_response_description(payment_data),
            'epays_gateway_id': payment_data.get('gatewayId'),
            'epays_gateway_response': json.dumps(
                gateway_response, ensure_ascii=False, indent=2
            ) if isinstance(gateway_response, dict) else None,
        }
        self.write({
            field_name: str(value)
            for field_name, value in values.items()
            if not epays_utils.is_empty(value)
        })

    def _epays_log_payment_details(self, payment_data):
        """ Log the payment details as an internal note on the linked sales orders and invoices.

        :param dict payment_data: The `data` of the v2 payment details.
        :return: None
        """
        status = payment_data.get('status')
        result = payment_data.get('result')
        if not epays_utils.is_empty(status) and not epays_utils.is_empty(result) \
                and str(status).strip().lower() != str(result).strip().lower():
            result_text = f'{status} ({result})'
        else:
            result_text = status if not epays_utils.is_empty(status) else result
        amount = payment_data.get('amount')
        rows = [
            (_("Result"), result_text),
            (_("ePays Payment ID"), payment_data.get('paymentId') or self.provider_reference),
            (_("Amount"), f"{amount} {payment_data.get('currency') or ''}".strip()
                if not epays_utils.is_empty(amount) else None),
            *self._epays_get_conversion_note_rows(),
            (_("Gateway Reference"), payment_data.get('gatewayReference')),
            (_("Transaction ID"), payment_data.get('transactionId')),
            (_("Response Code"), payment_data.get('responseCode')),
            (_("Response Description"), self._epays_get_response_description(payment_data)),
            (_("Created At"), payment_data.get('createdAt')),
            (_("Paid At"), payment_data.get('paidAt')),
        ]
        items = Markup('').join(
            Markup('<li>%s: %s</li>') % (label, epays_utils.format_value(value))
            for label, value in rows if not epays_utils.is_empty(value)
        )
        body = Markup('<p><strong>%s</strong></p><ul>%s</ul>') % (
            _("ePays payment details for transaction %s", self.reference), items
        )
        gateway_lines = epays_utils.get_gateway_response_lines(payment_data.get('gatewayResponse'))
        if gateway_lines:
            body += Markup('<hr/><ul>%s</ul>') % Markup('').join(
                Markup('<li>%s: %s</li>') % (label, value) for label, value in gateway_lines
            )

        orders, invoices = self._epays_get_documents()
        for document in [*orders, *invoices]:
            document._message_log(body=body)

    def _epays_get_conversion_note_rows(self):
        """ Return the note rows telling the order amount and the rate of a converted payment. """
        currency = self.epays_charged_currency_id
        if not currency or currency == self.currency_id:
            return []
        return [
            (_("Order Amount"), f"{self.amount:.{self.currency_id.decimal_places}f} "
                                f"{self.currency_id.name}"),
            (_("Exchange Rate"), _(
                "1 %(from_currency)s = %(rate)s %(to_currency)s",
                from_currency=self.currency_id.name,
                rate=f'{self.epays_exchange_rate:.6f}'.rstrip('0').rstrip('.'),
                to_currency=currency.name,
            )),
        ]

    #=== BUSINESS METHODS - CRON ===#

    @api.model
    def _cron_epays_poll_pending(self):
        """ Poll ePays for the transactions whose result is still unknown to Odoo, void the expired
        payment links, and post-process the finished ePays transactions.

        ePays notifies Odoo only once; this cron catches the payments whose notification was missed.
        Each transaction is committed separately so that one failure does not block the others.

        :return: None
        """
        now = fields.Datetime.now()
        txs_to_poll = self.search([
            ('provider_id.code', '=', 'epays'),
            ('provider_reference', '!=', False),
            ('create_date', '<=', now - const.CRON_MIN_AGE),
            ('create_date', '>=', now - const.CRON_MAX_AGE),
            '|',
            ('state', 'in', ('draft', 'pending')),
            '&', ('state', '=', 'error'), ('epays_expires_at', '>', now),
        ])
        for tx in txs_to_poll:
            reference = tx.reference
            try:
                with self.env.cr.savepoint():
                    tx._epays_poll()
                self.env.cr.commit()
            except psycopg2.OperationalError:
                self.env.cr.rollback()  # Rollback and try later.
            except ValidationError as error:
                _logger.warning(
                    "could not check the ePays payment of transaction %s: %s", reference, error
                )
                self.env.cr.rollback()
            except Exception:  # noqa: BLE001 - one failing transaction must not stop the others.
                _logger.exception(
                    "encountered an error while checking the ePays payment of transaction %s",
                    reference,
                )
                self.env.cr.rollback()

        # Odoo's own post-processing cron is inactive when no provider is enabled, e.g. when ePays
        # was disabled while payments were in progress.
        txs_to_post_process = self.search([
            ('provider_id.code', '=', 'epays'),
            ('state', 'in', ('done', 'cancel', 'error')),
            ('is_post_processed', '=', False),
            ('last_state_change', '>=', now - const.CRON_POST_PROCESS_LIMIT),
        ])
        if txs_to_post_process:  # An empty recordset would post-process every transaction.
            txs_to_post_process._cron_post_process()

    def _epays_poll(self):
        """ Poll the payment status of the transaction and apply it, voiding an expired link.

        Note: self.ensure_one()

        :return: None
        """
        self.ensure_one()
        payment_data = self._epays_fetch_payment_details()
        target_state, _status = epays_utils.get_payment_state(payment_data)
        if target_state == 'pending':
            # The payment link is still open. The transaction is left as it is: moving an abandoned
            # checkout to `pending` would make Odoo's post-processing email the customer.
            if not self._epays_is_link_expired():
                return
            void_data = self._epays_void()
            if void_data is not None:
                payment_data = dict(payment_data, **{
                    key: value for key, value in void_data.items()
                    if not epays_utils.is_empty(value)
                })
                payment_data['status'] = 'Voided'
            else:  # The void was refused: the payment may have moved on meanwhile.
                payment_data = self._epays_fetch_payment_details()
                target_state, _status = epays_utils.get_payment_state(payment_data)
                if target_state == 'pending':
                    return
        self._epays_apply_payment_details(payment_data)

    def _epays_is_link_expired(self):
        """ Return whether the payment link expired more than the grace period ago. """
        self.ensure_one()
        return bool(
            self.epays_expires_at
            and fields.Datetime.now() > self.epays_expires_at + const.CRON_VOID_GRACE_PERIOD
        )

    def _epays_void(self):
        """ Void the pending ePays payment of the transaction.

        ePays only voids a payment that is still pending, so a paid payment can never be cancelled.

        :return: The `data` of the response if the void succeeded, `None` if ePays refused it.
        :rtype: dict|None
        """
        try:
            return self._epays_request_payment('DELETE')
        except ValidationError:
            _logger.info(
                "ePays refused to void the payment of transaction %s; reading its status again",
                self.reference,
            )
            return None
