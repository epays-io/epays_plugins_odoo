# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

import json
import logging
import pprint
import re
import time
from datetime import timedelta
from urllib.parse import parse_qsl, quote, urlsplit

import psycopg2
from markupsafe import Markup
from werkzeug import urls

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import SQL
from odoo.tools.lru import LRU

from odoo.addons.payment import utils as payment_utils
from odoo.addons.payment_epays import const
from odoo.addons.payment_epays import utils as epays_utils
from odoo.addons.payment_epays.controllers.main import EpaysController
from odoo.addons.payment_epays.utils import EpaysApiError

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
    'epays_at_bank',
)

# When each transaction's payment was last read for the public routes, per process. Not worth a
# column: several workers only read a little more often, and an evicted entry reads once more.
PAYMENT_READS = LRU(4096)
PAYMENT_ID_RE = re.compile(const.PAYMENT_ID_PATTERN)


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

    # What the last read of the payment showed, and how long a cancelled one keeps being read.
    epays_at_bank = fields.Boolean(
        string="With the Gateway",
        help="The customer submitted the payment and the gateway has not answered yet, so it can "
             "still take the money, even if the payment is voided.",
        readonly=True,
    )
    epays_watch_until = fields.Datetime(
        string="Checked Until",
        help="The scheduled check reads this payment until then, as ePays can still charge it.",
        readonly=True,
    )
    epays_review_needed = fields.Boolean(
        string="Merchant Review Needed",
        help="ePays took money that does not match the documents. No new payment can be made on "
             "them until the merchant has reviewed it.",
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

    def _epays_get_document_transactions(self):
        """ Return the other transactions, of any provider, on the transaction's documents.

        Note: self.ensure_one()

        :return: The transactions.
        :rtype: recordset of `payment.transaction`
        """
        self.ensure_one()
        orders, invoices = self._epays_get_documents()
        txs = self.browse()
        for document in [*orders, *invoices]:
            txs |= document.sudo().transaction_ids
        return txs - self

    def _epays_get_amount_due(self):
        """ Return what is left to pay on the transaction's documents, without this transaction.

        The accounting decides, not the sum of the transactions: a document can be settled without
        any (a bank transfer, a credit note), an order through the link of its invoice, and a refund
        or an unreconciled payment opens a paid invoice again. A cancelled document is owed nothing.

        Note: self.ensure_one()

        :return: The amount due, in the transaction's currency.
        :rtype: float
        """
        self.ensure_one()
        orders, invoices = self._epays_get_documents()
        if orders:
            return sum(
                self._epays_get_order_due(order)
                for order in orders.sudo() if order.state != 'cancel'
            )
        if invoices:
            return self._epays_get_invoices_due(
                invoices.sudo().filtered(lambda invoice: invoice.state != 'cancel')
            )
        return 0.0

    def _epays_get_order_due(self, order):
        """ Return what is left to pay on a sales order, paid through its transactions or through
        its invoices, without this transaction. A transaction can be on both, hence the highest.

        :param recordset order: The sales order, as a sudoed `sale.order` record.
        :return: The amount due.
        :rtype: float
        """
        paid_txs = order.transaction_ids.filtered(
            lambda t: t != self and t.state in const.PAID_TRANSACTION_STATES
        )
        invoices = order.invoice_ids.filtered(
            lambda invoice: invoice.state == 'posted' and invoice.move_type == 'out_invoice'
            and invoice.payment_state != 'reversed'
        )
        paid_on_invoices = (
            sum(invoices.mapped('amount_total')) - self._epays_get_invoices_due(invoices)
        )
        return order.amount_total - max(sum(paid_txs.mapped('amount')), paid_on_invoices)

    def _epays_get_invoices_due(self, invoices):
        """ Return what is left to pay on invoices, without this transaction.

        A done transaction has no accounting payment until it is post-processed, so it does not
        show in the invoice's residual yet.

        :param recordset invoices: The invoices, as sudoed `account.move` records.
        :return: The amount due.
        :rtype: float
        """
        unrecorded_txs = invoices.transaction_ids.filtered(
            lambda t: t != self and t.state in const.PAID_TRANSACTION_STATES and not t.payment_id
        )
        return sum(invoices.mapped('amount_residual')) - sum(unrecorded_txs.mapped('amount'))

    def _epays_is_needed(self):
        """ Return whether the transaction's documents still expect a payment.

        Paying a part of a document leaves the rest due, so a second transaction can be legitimate.
        A transaction that never had documents is always needed; one whose documents were deleted
        is not, and its custom fields still name them.

        Note: self.ensure_one()

        :return: Whether the documents have something left to pay.
        :rtype: bool
        """
        self.ensure_one()
        orders, invoices = self._epays_get_documents()
        if not orders and not invoices:
            return not (self.epays_udf2 or self.epays_udf3)
        return self.currency_id.compare_amounts(self._epays_get_amount_due(), 0) > 0

    def _epays_get_paying_transactions(self):
        """ Return the done transactions on the documents, if they leave nothing for this one.

        Note: self.ensure_one()

        :return: The done transactions, or an empty recordset if this transaction is still needed.
        :rtype: recordset of `payment.transaction`
        """
        self.ensure_one()
        if self._epays_is_needed():
            return self.browse()
        return self._epays_get_document_transactions().filtered(lambda t: t.state == 'done')

    def _epays_get_payment_holders(self):
        """ Return the done transactions holding the same ePays payment as this one.

        An older ePays server can return one payment to two attempts on the same documents: one
        charge, whichever of them recorded it.

        Note: self.ensure_one()

        :return: The transactions.
        :rtype: recordset of `payment.transaction`
        """
        self.ensure_one()
        return self.search([
            ('provider_reference', '=', self.provider_reference),
            ('provider_code', '=', 'epays'),
            ('epays_environment', '=', self.epays_environment),
            ('state', '=', 'done'),
            ('id', '!=', self.id),
        ])

    def _epays_is_refused(self):
        """ Return whether ePays took this money and Odoo refused to record it. """
        return self.state == 'error' and self.epays_response_code == const.APPROVED_RESPONSE_CODE

    def _get_last(self):
        """ Override of `payment` to show the transaction the customer needs to read about.

        A customer who paid must not read that the payment was cancelled on the confirmation and
        portal pages, so a newer attempt made pointless by a payment gives way to that payment. A
        charge Odoo refused comes first, even though it can be the older transaction: otherwise a
        customer who paid twice only reads that the order is confirmed.

        :return: The last transaction of the recordset, sorted by id.
        :rtype: recordset of `payment.transaction`
        """
        last_tx = super()._get_last()
        refused_txs = self.filtered(lambda t: t.provider_code == 'epays' and t._epays_is_refused())
        if refused_txs:
            refused_tx = refused_txs.sorted('last_state_change')[-1]
            if refused_tx.last_state_change >= last_tx.last_state_change:
                return refused_tx
        if last_tx.provider_code == 'epays' and last_tx.state in ('error', 'cancel'):
            return last_tx._epays_get_paying_transactions()[:1] or last_tx
        return last_tx

    def _epays_request_payment(self, method, endpoint='', timeout=None):
        """ Call the ePays payment of the transaction (e.g. `GET /api/v2/Payments/{id}`), with the
        environment and merchant domain it was created with.

        :param str method: The HTTP method.
        :param str endpoint: The path after the payment, e.g. `/acknowledgement`, if any.
        :param int timeout: The seconds to wait for the answer; `const.REQUEST_TIMEOUT` by default.
        :return: The `data` of the response.
        :rtype: dict
        :raise EpaysApiError: If the request fails.
        """
        self.ensure_one()
        provider = self.provider_id
        return provider._epays_make_request(
            method,
            f'{const.PAYMENTS_PATH}/{quote(self.provider_reference or "", safe="")}{endpoint}',
            environment=self._epays_get_environment(),
            domain=self.epays_merchant_domain or provider._epays_get_merchant_domain(),
            timeout=timeout,
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
        lang = self._epays_get_lang(self.env.context.get('lang'))

        self._epays_check_replaced_payments()
        # One live ePays payment per order: going back and paying again reuses the pending payment.
        reused_payment_url = self._epays_reuse_or_retire_earlier_payment(
            environment, gateway_id, lang=lang
        )
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
        try:
            payment_data = provider._epays_make_request(  # Logs the payload.
                'POST',
                const.PAYMENTS_PATH,
                environment=environment,
                domain=domain,
                payload=payload,
                idempotency_key=self._epays_get_idempotency_key(),
                timeout=const.PAYMENT_REQUEST_TIMEOUT,
            )
        except EpaysApiError as error:
            if error.timed_out:  # ePays may still create the payment: void it later.
                self.env['payment.epays.unanswered.request']._epays_record(
                    self, environment, domain, payload['customFields']['udf2']
                )
            raise

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

    def _epays_get_idempotency_key(self):
        """ Return the idempotency key of the payment request of this attempt.

        Odoo's key is made from the transaction reference, and a reference comes back after a
        rollback: when ePays answers too late, the request fails, the transaction is rolled back
        and the customer's next click creates one with the same reference. If ePays created the
        payment anyway, that next request carries the same key with a new expiry, and ePays
        refuses it (409 IDEMPOTENCY_CONFLICT) on every click until the key expires. The record id
        comes from a sequence that a rollback does not rewind, so each attempt has its own key,
        while one attempt still sends one key.

        Note: self.ensure_one()

        :return: The idempotency key.
        :rtype: str
        """
        self.ensure_one()
        return payment_utils.generate_idempotency_key(
            self, scope=f'payment_request_epays_{self.id}'
        )

    @api.model
    def _epays_get_redirect_values(self, redirect_url):
        """ Return the rendering values of the redirect form to the given ePays payment page.

        The query parameters of the payment URL are passed separately: a GET form drops the query
        string of its action.
        """
        url_params = dict(parse_qsl(urlsplit(redirect_url).query, keep_blank_values=True))
        return {'api_url': redirect_url, 'url_params': url_params}

    def _epays_check_replaced_payments(self):
        """ Refuse a new payment on documents that a replaced payment or a merchant review holds up.

        It runs before anything is voided: raising rolls the request back, not a void on ePays. A
        payment Odoo replaced whose link would still be open can have been paid or be with the
        gateway, whose answer can still take the money, so the customer waits for it instead of
        paying again. Money that does not match the documents waits for the merchant.

        Note: self.ensure_one()

        :return: None
        :raise ValidationError: If no new payment can be made, or if ePays cannot be reached.
        """
        self.ensure_one()
        epays_txs = self._epays_get_document_transactions().filtered(
            lambda t: t.provider_code == 'epays'
        )
        held_tx = epays_txs.filtered('epays_review_needed')[:1]
        if held_tx:
            _logger.info(
                "transaction %s waits for the merchant's review; not taking a new payment for "
                "transaction %s", held_tx.reference, self.reference,
            )
            raise ValidationError(held_tx.state_message)
        replaced_txs = epays_txs.filtered(
            lambda t: t.state == 'cancel' and t._epays_is_link_open() and t._epays_owns_payment()
        )
        for tx in replaced_txs:
            payment_data = tx._epays_fetch_payment_details()  # Raises the generic error.
            if epays_utils.is_at_bank(payment_data):
                self._epays_raise_still_confirming(tx)
            if epays_utils.get_payment_state(payment_data)[0] == 'done':
                self._epays_raise_already_paid(tx)

    def _epays_find_live_attempts(self, environment):
        """ Return the earlier transactions whose ePays payment can still be paid, newest first.

        They must be for the same documents: the same sales orders (udf2) or, without sales order,
        the same invoices (udf3). A declined payment is one of them: ePays keeps its link payable.
        Money that Odoo refused is not: nothing more can happen to it.

        Note: self.ensure_one()

        :param str environment: The environment this transaction would use.
        :return: The earlier transactions, if any.
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
            ('state', 'in', ('draft', 'pending', 'error')),
            ('provider_reference', '!=', False),
            ('epays_expires_at', '>=', fields.Datetime.now() - const.CRON_VOID_GRACE_PERIOD),
            *document_domain,
        ], order='id desc').filtered(lambda t: not t._epays_is_refused())

    def _epays_reuse_or_retire_earlier_payment(self, environment, gateway_id=None, lang=None):
        """ Reuse the pending ePays payment of an earlier attempt on the same documents, and retire
        the others.

        Odoo creates a new transaction each time the customer clicks Pay, e.g. after going back to
        the cart. Without this, each click would create one more payable ePays payment for the
        same order. Every live attempt is read from ePays before anything is voided:

        - the newest, pending with the same amount, currency, gateway and language, on a link valid
          long enough: this transaction takes over its payment page;
        - with the gateway and not reusable: no new payment, as a void would not stop the bank, and
          the customer is asked to try again later;
        - paid: no new payment is created and the customer is told the order is already paid;
        - pending, declined (ePays keeps a declined link payable) or an unknown status (possibly a
          raw gateway word for a completed capture): voided; if ePays refuses, the payment is read
          again and nothing new is created;
        - cancelled, voided, expired or refunded: it is updated in Odoo.

        If ePays cannot be reached while checking, the generic connection error is raised rather
        than creating a new payment: a failed check must never leave two payable links for one
        order.

        Note: self.ensure_one()

        :param str environment: The environment this transaction would use.
        :param str gateway_id: The ePays gateway this transaction would open, if any.
        :param str lang: The language this transaction would open the page in (`en` or `ar`).
        :return: The payment page to redirect to if a payment was reused, `None` otherwise.
        :rtype: str|None
        :raise ValidationError: If the order is already paid, if its payment is with the gateway, or
                                if ePays cannot be reached.
        """
        self.ensure_one()
        attempts = self._epays_find_live_attempts(environment)
        if not attempts:
            return None
        # Two clicks on Pay at once must not both take over the same payment: the second waits
        # here, then fails on the first one's update and Odoo retries it with fresh data.
        attempts._epays_lock_for_update()

        payments = [(tx, tx._epays_fetch_payment_details()) for tx in attempts]  # Raises the error.
        newest, newest_data = payments[0]
        changes = self._epays_get_payment_changes(newest, newest_data, gateway_id, lang)
        reused_tx = newest if self._epays_can_reuse(newest, newest_data, changes) else self.browse()

        for tx, payment_data in payments:
            if tx == reused_tx:
                continue
            if epays_utils.is_at_bank(payment_data):
                self._epays_raise_still_confirming(tx)
            if epays_utils.get_payment_state(payment_data)[0] == 'done':
                self._epays_raise_already_paid(tx)

        for tx, payment_data in payments:
            if tx != reused_tx:
                self._epays_retire_attempt(tx, payment_data, changes if tx == newest else {})
        if reused_tx:
            self._epays_take_over_payment(reused_tx)
            return self.epays_payment_url
        return None

    def _epays_get_payment_changes(self, candidate, payment_data, gateway_id, lang):
        """ Return what changed since the earlier attempt created its pending payment.

        :param recordset candidate: The earlier transaction, as a `payment.transaction` record.
        :param dict payment_data: The `data` of its v2 payment details.
        :param str gateway_id: The ePays gateway this transaction would open, if any.
        :param str lang: The language this transaction would open the page in (`en` or `ar`).
        :return: The changes, as keyword arguments of `_epays_get_replaced_message`.
        :rtype: dict
        """
        if epays_utils.get_payment_state(payment_data)[0] != 'pending':
            return {}
        # The page and every message about the payment are in the language it was opened in. A
        # server that does not report it (no `paymentLang`) never retires a payment for it.
        page_lang = payment_data.get('paymentLang')
        return {
            'amount_changed': bool(self._epays_get_amount_mismatches(payment_data)),
            # A page opened for one gateway (e.g. Card) shows that gateway only.
            'method_changed': (candidate.epays_gateway_requested or None) != (gateway_id or None),
            'lang_changed': bool(lang) and page_lang in ('en', 'ar') and page_lang != lang,
        }

    def _epays_can_reuse(self, candidate, payment_data, changes):
        """ Return whether this transaction can take over the payment page of an earlier attempt.

        :param recordset candidate: The earlier transaction, as a `payment.transaction` record.
        :param dict payment_data: The `data` of its v2 payment details.
        :param dict changes: What changed since, see `_epays_get_payment_changes`.
        :return: Whether the page can be reused.
        :rtype: bool
        """
        min_expiry = fields.Datetime.now() + const.REUSE_MIN_REMAINING_VALIDITY
        return (
            candidate.state in ('draft', 'pending')
            and epays_utils.get_payment_state(payment_data)[0] == 'pending'
            and not any(changes.values())
            and candidate.epays_expires_at > min_expiry
            and candidate._epays_is_valid_redirect_url(
                candidate.epays_payment_url, candidate.epays_environment
            )
        )

    def _epays_retire_attempt(self, attempt, payment_data, changes):
        """ Close the ePays payment of an earlier attempt that this transaction replaces.

        :param recordset attempt: The earlier transaction, as a `payment.transaction` record.
        :param dict payment_data: The `data` of its v2 payment details.
        :param dict changes: What changed since, see `_epays_get_payment_changes`.
        :return: None
        :raise ValidationError: If it turned out paid or with the gateway, or if ePays refuses to
                                void it.
        """
        target_state = epays_utils.get_payment_state(payment_data)[0]
        if target_state not in ('pending', 'error', None):
            attempt._epays_apply_payment_details(payment_data)
            return
        if attempt._epays_void() is not None:
            attempt._set_canceled(
                self._epays_get_replaced_message(**changes), extra_allowed_states=('error',)
            )
            return
        payment_data = attempt._epays_fetch_payment_details()
        target_state, status = epays_utils.get_payment_state(payment_data)
        if target_state == 'done':
            self._epays_raise_already_paid(attempt)
        if epays_utils.is_at_bank(payment_data):  # The customer started paying since the read.
            self._epays_raise_still_confirming(attempt)
        _logger.warning(
            "ePays refused to void the payment %s of transaction %s (status %s); not creating a "
            "second payment for transaction %s",
            attempt.provider_reference, attempt.reference, status, self.reference,
        )
        raise ValidationError(self.provider_id._epays_get_connection_error_message())

    def _epays_raise_already_paid(self, attempt):
        """ Refuse a new payment because an earlier attempt was paid.

        Raising rolls back the whole request, so the earlier transaction cannot be completed here:
        its return route, the ePays notification or the scheduled check completes it. Completing it
        in a second database transaction was ruled out: it would wait on the locks of this request
        (e.g. on the sales order) while this request waits on it.

        :param recordset attempt: The earlier transaction, as a `payment.transaction` record.
        :raise ValidationError: Always.
        """
        _logger.warning(
            "the ePays payment %s of transaction %s is already paid; not creating a new payment "
            "for transaction %s", attempt.provider_reference, attempt.reference, self.reference,
        )
        raise ValidationError("ePays: " + _("This order has already been paid."))

    def _epays_raise_still_confirming(self, attempt):
        """ Refuse a new payment because an earlier attempt is with the gateway.

        :param recordset attempt: The earlier transaction, as a `payment.transaction` record.
        :raise ValidationError: Always.
        """
        _logger.info(
            "the ePays payment %s of transaction %s is with the gateway; not taking a new payment "
            "for transaction %s", attempt.provider_reference, attempt.reference, self.reference,
        )
        raise ValidationError("ePays: " + _(
            "Your previous payment for this order is still being confirmed. If you completed it, "
            "your order will update by itself, so please do not pay again. If you did not, you can "
            "try again in a few minutes."
        ))

    def _epays_lock_for_update(self):
        """ Lock the transactions' rows until the end of the database transaction. """
        self.env.cr.execute(
            SQL(
                "SELECT id FROM payment_transaction WHERE id IN %s ORDER BY id FOR UPDATE",
                tuple(self.ids),
            )
        )

    def _epays_get_replaced_message(
        self, amount_changed=False, method_changed=False, lang_changed=False
    ):
        """ Return the state message of an earlier transaction replaced by this one. """
        if amount_changed:
            message = _("Replaced by transaction %s (amount changed).", self.reference)
        elif method_changed:
            message = _("Replaced by transaction %s (payment method changed).", self.reference)
        elif lang_changed:
            message = _("Replaced by transaction %s (language changed).", self.reference)
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
        lang = self._epays_get_lang(self.env.context.get('lang'))

        payload = {
            'amount': f'{charged_amount:.{charged_currency.decimal_places}f}',
            'currency': charged_currency.name,
            'description': self._epays_get_description(lang),
            'notifyUrl': urls.url_join(base_url, f'{EpaysController._return_url}/{environment}'),
            'merchantUrl': base_url,
            'merchantDomain': domain,
            'testMode': environment != 'production',
            'lang': lang,
            'expiryDate': expires_at_epays.strftime(const.EPAYS_DATETIME_FORMAT),
            'customFields': self._epays_get_custom_fields(),
        }
        if gateway_id:
            # `subscriptionId` since ePays's v2 rename of 2026-10-18, `gatewayId` for a server not
            # updated yet. Each server ignores the name it does not know.
            payload['subscriptionId'] = gateway_id
            payload['gatewayId'] = gateway_id
        if provider.epays_send_customer_details:
            customer = self._epays_get_customer_values()
            if customer:
                payload['customer'] = customer
        order_summary = self._epays_get_order_summary()
        if order_summary:
            payload['order'] = order_summary
        return payload

    def _epays_get_description(self, lang):
        """ Return the description shown on the ePays page, in its language.

        :param str lang: The language of the page (`en` or `ar`).
        :return: The description.
        :rtype: str
        """
        self.ensure_one()
        tx = self.with_context(lang=self._epays_get_customer_lang_code(lang))
        return tx._epays_compose_description()[:const.TEXT_MAX_LENGTH]

    def _epays_compose_description(self):
        """ Compose the description in the language of the context. """
        custom_fields = self._epays_get_custom_fields()
        if custom_fields['udf2']:
            return _("Order %s", custom_fields['udf2'])
        if custom_fields['udf3']:
            return _("Invoice %s", custom_fields['udf3'])
        return self.reference

    def _epays_get_order_summary(self):
        """ Return the order details sent to ePays, in the charged currency.

        Only for a transaction paying a whole sales order: a partial payment has no lines that add
        up to it. See `epays_utils.get_order_lines` for when the lines themselves are sent.

        Note: self.ensure_one()

        :return: The `order` of the payment request, or an empty dict.
        :rtype: dict
        """
        self.ensure_one()
        orders, _invoices = self._epays_get_documents()
        if len(orders) != 1:
            return {}
        order = orders.sudo()
        if self.currency_id.compare_amounts(order.amount_total, self.amount) != 0:
            return {}
        charged_amount, charged_currency, rate = self._epays_get_charge()
        decimals = charged_currency.decimal_places

        def convert(amount):
            return epays_utils.to_minor_units(amount * rate, decimals)

        def is_shipping(line):
            return 'is_delivery' in line._fields and line.is_delivery

        lines = order.order_line.filtered(lambda line: not line.display_type)
        shipping = convert(sum(lines.filtered(is_shipping).mapped('price_total')))
        items = [{
            'name': (line.name or '').split('\n')[0],
            # ePays reads a whole number; a fractional quantity is one line with its total.
            'quantity': int(line.product_uom_qty) if line.product_uom_qty.is_integer() else 1,
            'sku': line.product_id.default_code or '',
            'total': convert(line.price_total),
        } for line in lines if not is_shipping(line) and line.product_uom_qty > 0]
        discount = convert(sum(
            line.price_unit * line.product_uom_qty * line.discount / 100 for line in lines
        ))
        summary = {
            key: epays_utils.format_minor_units(value, decimals)
            for key, value in (
                ('taxAmount', convert(order.amount_tax)),
                ('shippingAmount', shipping),
                ('discountAmount', discount),
            ) if value
        }
        order_lines = epays_utils.get_order_lines(
            items, shipping, epays_utils.to_minor_units(charged_amount, decimals), decimals
        )
        if order_lines:
            summary['lineItems'] = order_lines
        return summary

    def _epays_get_customer_message(self, outcome, payment_data=None):
        """ Return the message shown to the customer when the payment does not complete, in the
        language of the ePays payment page.

        The message is the transaction's `state_message`, which Odoo shows to the customer on the
        payment status, checkout and portal pages. It is written for the customer: the technical
        details (status, response code, gateway response) are stored on the transaction and logged
        on its documents instead.

        :param str outcome: `declined`, `cancelled`, `mismatch`, `paid_twice` or `refunded`.
        :param dict payment_data: The `data` of the v2 payment details.
        :return: The message.
        :rtype: str
        """
        self.ensure_one()
        payment_data = payment_data or {}
        lang = self._epays_get_payment_lang(payment_data)
        tx = self.with_context(lang=self._epays_get_customer_lang_code(lang))
        return tx._epays_compose_customer_message(outcome, payment_data, lang)

    def _epays_get_payment_lang(self, payment_data):
        """ Return the language the customer paid in: the one ePays reports the page was opened in
        (`paymentLang`), else the one the addon would choose for the customer.

        A customer paying from the Arabic website is answered in Arabic even if their contact is in
        English. The result arrives on requests that carry no website language (the return from
        ePays and its server notification), so the language cannot be read from the request.

        :param dict payment_data: The `data` of the v2 payment details.
        :return: `en` or `ar`.
        :rtype: str
        """
        payment_lang = payment_data.get('paymentLang')
        if payment_lang in ('en', 'ar'):
            return payment_lang
        return self._epays_get_lang()

    def _epays_compose_customer_message(self, outcome, payment_data, lang):
        """ Compose the message of `_epays_get_customer_message` in the language of the context,
        with ePays's decline reason in `lang` (`en` or `ar`). """
        if outcome == 'mismatch':
            # Money may have been taken: the customer must not simply pay again.
            return _(
                "We could not confirm your payment. Please contact us before trying again, "
                "quoting the reference %s.", self.reference,
            )
        if outcome == 'paid_twice':
            return _(
                "This payment was not needed: your order was already paid or cancelled. Please "
                "contact us to have it returned, quoting the reference %s.", self.reference,
            )
        if outcome == 'refunded':
            return _(
                "Your payment was refunded, so your order was not confirmed. Please contact us, "
                "quoting the reference %s.", self.reference,
            )
        if outcome == 'cancelled':
            what_happened = _("Your payment was cancelled.")
        else:
            reason = self._epays_get_response_description(payment_data, lang=lang)
            if reason:
                what_happened = _("Your payment was declined: %s.", reason.rstrip('. '))
            else:
                what_happened = _("Your payment was declined.")
        return ' '.join((
            what_happened, _("You can try again or choose another payment method."),
        ))

    def _epays_get_customer_lang_code(self, epays_lang):
        """ Return the installed Odoo language matching an ePays language (`en` or `ar`),
        preferring the customer's own variant, e.g. `ar_001`. """
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
        if not isinstance(payment_id, str) or not PAYMENT_ID_RE.match(payment_id.strip()):
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
        # A done transaction cannot change any more, and a cancelled one whose payment a newer one
        # took over is that one's to complete.
        if self.state == 'done' or (self.state == 'cancel' and not self._epays_owns_payment()):
            _logger.info(
                "transaction %s is already %s; not checking its ePays payment again",
                self.reference, self.state,
            )
            return
        # ePays may not call again, and counts this call as delivered whatever happens here. The
        # scheduled check may have stopped reading an old payment: a read that cannot run goes back
        # to the check, or a charge is lost.
        if not self._epays_may_read_payment():
            _logger.info("not checking transaction %s with ePays again yet", self.reference)
            if not self._epays_is_read_by_cron():
                self._epays_watch_again()
            return
        try:
            payment_data = self._epays_fetch_payment_details()
        except ValidationError:
            if self._epays_is_read_by_cron():
                raise
            self._epays_watch_again()
            return
        self._epays_apply_payment_details(payment_data)
        self._epays_count_read()

    def action_epays_check_status(self):
        """ Read the transaction's payment from ePays, and the attempts Odoo cancelled on its
        documents, at any age: the merchant asked, e.g. after ePays corrected a payment.

        Note: self.ensure_one()

        :return: The client action displaying the result.
        :rtype: dict
        """
        self.ensure_one()
        self.provider_id._epays_check_admin_access()
        replaced_txs = self._epays_get_document_transactions().filtered(
            lambda t: t.provider_code == 'epays' and t.state == 'cancel'
            and t._epays_owns_payment()
        )
        txs = (self | replaced_txs).filtered(lambda t: t.state != 'done' and t.provider_reference)
        for tx in txs:
            tx._epays_apply_payment_details(tx._epays_fetch_payment_details())
        txs.filtered(
            lambda t: t.state in ('done', 'error', 'cancel') and not t.is_post_processed
        )._post_process()
        states = dict(self._fields['state']._description_selection(self.env))
        return self.provider_id._epays_notification(
            _("ePays payment status"),
            _(
                "Transaction %(reference)s: %(state)s.",
                reference=self.reference, state=states[self.state],
            ),
            'info',
        )

    def action_epays_release_review(self):
        """ Allow new payments on the documents again: the merchant reviewed a refused charge.

        Note: self.ensure_one()

        :return: None
        """
        self.ensure_one()
        self.provider_id._epays_check_admin_access()
        self.epays_review_needed = False

    def _epays_owns_payment(self):
        """ Return whether this transaction is the one to complete its ePays payment.

        A payment that a newer transaction took over belongs to that transaction.

        Note: self.ensure_one()

        :return: Whether no newer transaction holds the payment.
        :rtype: bool
        """
        self.ensure_one()
        return bool(self.provider_reference) and not self.search_count([
            ('provider_reference', '=', self.provider_reference),
            ('provider_code', '=', 'epays'),
            ('epays_environment', '=', self.epays_environment),
            ('id', '>', self.id),
        ], limit=1)

    def _epays_may_be_charged(self):
        """ Return whether the scheduled check still reads this cancelled transaction's payment.

        Odoo's cancellation does not stop a customer who is already past the ePays page, e.g. on
        3-D Secure, see `const.LATE_CHARGE_WINDOW`.

        Note: self.ensure_one()

        :return: Whether to read the payment on ePays.
        :rtype: bool
        """
        self.ensure_one()
        return bool(
            self.epays_watch_until and self.epays_watch_until >= fields.Datetime.now()
        ) and self._epays_owns_payment()

    def _epays_get_watch_limit(self):
        """ Return until when the scheduled check reads the payment of this cancelled transaction.

        Note: self.ensure_one()

        :return: The limit, in UTC.
        :rtype: datetime
        """
        self.ensure_one()
        now = fields.Datetime.now()
        expires_at = self.epays_expires_at or now
        if self.epays_at_bank:
            return max(expires_at, now) + const.CRON_MAX_AGE
        return max(expires_at + const.CRON_VOID_GRACE_PERIOD, now + const.LATE_CHARGE_WINDOW)

    def _epays_watch_again(self):
        """ Make the scheduled check read this transaction's payment, at least for as long as a
        newly cancelled one.

        Note: self.ensure_one()

        :return: None
        """
        self.ensure_one()
        limit = fields.Datetime.now() + const.LATE_CHARGE_WINDOW
        if not self.epays_watch_until or self.epays_watch_until < limit:
            self.epays_watch_until = limit

    def _epays_is_read_by_cron(self):
        """ Return whether the scheduled check reads this transaction's payment by itself.

        Note: self.ensure_one()

        :return: Whether a read that cannot run now can be left to it.
        :rtype: bool
        """
        self.ensure_one()
        now = fields.Datetime.now()
        if self.state in ('draft', 'pending'):
            return self.create_date >= now - const.CRON_MAX_AGE
        if self.state == 'error':
            return bool(self.epays_expires_at) and (
                self.epays_expires_at >= now - const.CRON_VOID_GRACE_PERIOD
            )
        return False

    def _set_canceled(self, state_message=None, extra_allowed_states=()):
        """ Override of `payment` to keep reading the ePays payment of a cancelled transaction. """
        canceled_txs = super()._set_canceled(
            state_message=state_message, extra_allowed_states=extra_allowed_states
        )
        for tx in canceled_txs.filtered(
            lambda t: t.provider_code == 'epays' and t.provider_reference
        ):
            tx.epays_watch_until = tx._epays_get_watch_limit()
        return canceled_txs

    def _epays_get_recent_reads(self):
        """ Return when this transaction's payment was read for the public routes, in the last
        `const.FETCH_LIMIT_WINDOW` seconds.

        Note: self.ensure_one()

        :return: The monotonic times of the reads.
        :rtype: list[float]
        """
        self.ensure_one()
        window_start = time.monotonic() - const.FETCH_LIMIT_WINDOW
        reads = PAYMENT_READS.get((self.env.cr.dbname, self.id), ())
        return [read for read in reads if read > window_start]

    def _epays_may_read_payment(self):
        """ Return whether the public routes may read this transaction's payment now.

        Their id could otherwise keep Odoo's workers and ePays's rate limit busy.

        Note: self.ensure_one()

        :return: Whether to read the payment on ePays.
        :rtype: bool
        """
        self.ensure_one()
        return len(self._epays_get_recent_reads()) < const.FETCH_LIMIT

    def _epays_count_read(self):
        """ Count a read of this transaction's payment that was applied.

        Only such a read counts: ePays's notification may be the only one, so a read that failed, or
        that Odoo retries after a concurrency error, must leave the next one free.

        Note: self.ensure_one()

        :return: None
        """
        self.ensure_one()
        reads = (*self._epays_get_recent_reads(), time.monotonic())
        PAYMENT_READS[(self.env.cr.dbname, self.id)] = reads

    def _epays_refresh_for_status_page(self):
        """ Read the payment for a customer waiting on the status page, within the limit.

        A failure only means that the next poll tries again.

        Note: self.ensure_one()

        :return: None
        """
        self.ensure_one()
        if not self._epays_may_read_payment():
            return
        try:
            with self.env.cr.savepoint():
                self._epays_apply_payment_details(self._epays_fetch_payment_details())
        except ValidationError:
            return
        self._epays_count_read()

    def _epays_fetch_payment_details(self):
        """ Fetch the v2 payment details of the transaction from ePays.

        Note: self.ensure_one()

        :return: The `data` of the v2 payment details.
        :rtype: dict
        """
        return self._epays_request_payment('GET')

    def _epays_apply_payment_details(self, payment_data):
        """ Verify the payment details and update the transaction state accordingly.

        ePays's status wins over a cancellation in Odoo when, and only when, the money was taken.

        Note: self.ensure_one()

        :param dict payment_data: The `data` of the v2 payment details.
        :return: The transaction if its state changed, an empty recordset otherwise.
        :rtype: recordset of `payment.transaction`
        """
        self.ensure_one()
        target_state, status = epays_utils.get_payment_state(payment_data)
        if self.state == 'cancel' and target_state != 'done':
            self._epays_record_at_bank(payment_data)
            return self.env['payment.transaction']

        self._epays_update_payment_method(
            epays_utils.get_first(payment_data, 'subscriptionId', 'gatewayId')
        )

        updated_txs = self._epays_apply_target_state(target_state, status, payment_data)
        self._epays_record_at_bank(payment_data)  # After the cancellation, which reads it.
        if updated_txs:
            self._epays_store_payment_details(payment_data)
            if target_state == 'done' and self.state == 'error':
                # What says that the money was taken and refused, here and on the next reads. ePays
                # completes a payment only with this code, but a server may not report it.
                self.epays_response_code = const.APPROVED_RESPONSE_CODE
            if self.state in ('done', 'error', 'cancel'):
                self._epays_log_payment_details(payment_data)
        return updated_txs

    def _epays_record_at_bank(self, payment_data):
        """ Record whether the payment is with the gateway.

        :param dict payment_data: The `data` of the v2 payment details.
        :return: None
        """
        at_bank = epays_utils.is_at_bank(payment_data)
        if self.epays_at_bank != at_bank:
            self.epays_at_bank = at_bank
            # Read for as long as if it had been with the gateway when it was cancelled.
            if at_bank and self.state == 'cancel':
                self.epays_watch_until = max(
                    self.epays_watch_until or fields.Datetime.now(), self._epays_get_watch_limit()
                )

    def _epays_apply_target_state(self, target_state, status, payment_data):
        """ Move the transaction to the state that ePays reports.

        :param str target_state: The state from `epays_utils.get_payment_state`.
        :param str status: The ePays status it was mapped from.
        :param dict payment_data: The `data` of the v2 payment details.
        :return: The transaction if its state changed, an empty recordset otherwise.
        :rtype: recordset of `payment.transaction`
        """
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
                updated_txs = self._set_error(
                    self._epays_get_customer_message('refunded', payment_data)
                )
        elif target_state == 'pending':
            if self.state == 'draft':
                updated_txs = self._set_pending()
        elif target_state == 'error':
            updated_txs = self._set_error(
                self._epays_get_customer_message('declined', payment_data)
            )
        elif target_state == 'cancel':
            updated_txs = self._set_canceled(
                self._epays_get_customer_message('cancelled', payment_data)
            )
        else:
            _logger.warning(
                "received data with unrecognised payment status (%s) for transaction with "
                "reference %s; leaving it unchanged", status, self.reference,
            )
        return updated_txs

    def _epays_complete(self, payment_data):
        """ Set the transaction done if the completed ePays payment matches it and what its
        documents still expect, in error otherwise.

        The transaction can be cancelled: ePays charged a payment after Odoo gave up on it. A charge
        that Odoo cannot use is not recorded: it stays for the merchant to refund on ePays.

        :param dict payment_data: The `data` of the v2 payment details.
        :return: The transaction if its state changed, an empty recordset otherwise.
        :rtype: recordset of `payment.transaction`
        """
        is_late = self.state == 'cancel'
        # Two attempts completing at once must not both find the documents unpaid: the second waits
        # here, then fails on the first one's update and Odoo retries it with fresh data.
        (self | self._epays_get_document_transactions())._epays_lock_for_update()
        holders = self._epays_get_payment_holders()
        if holders:
            # The same charge, already recorded: there is nothing to refuse or to log again.
            self._set_canceled(
                holders[:1]._epays_get_replaced_message(), extra_allowed_states=('error',)
            )
            return self.browse()
        mismatches = self._epays_get_mismatches(payment_data)
        if mismatches:
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
            return self._epays_hold_payment(payment_data)
        if not self._epays_is_needed():
            return self._epays_refuse_unneeded_payment(payment_data)
        if not self._epays_fits_amount_due(is_late):
            _logger.warning(
                "the ePays payment %s of transaction %s does not fit what its documents expect",
                self.provider_reference, self.reference,
            )
            return self._epays_hold_payment(payment_data)
        return self._set_done(extra_allowed_states=('cancel',))

    def _epays_fits_amount_due(self, is_late):
        """ Return whether this charge fits what the documents expect now.

        A charge above what is due cannot be recorded. A late charge on the documents' own payment
        charged what they expected, if they did not change since. A replaced one, which they stopped
        expecting, has to be what they would charge now, as a new payment would: rates included.
        A payment on time can be below it: paying a part of a document is legitimate.

        Note: self.ensure_one()

        :param bool is_late: Whether the transaction was cancelled when ePays charged it.
        :return: Whether the transaction can be recorded.
        :rtype: bool
        """
        self.ensure_one()
        orders, invoices = self._epays_get_documents()
        if not orders and not invoices:
            return True
        amount_due = self._epays_get_amount_due()
        if self.currency_id.compare_amounts(self.amount, amount_due) > 0:
            return False
        if not is_late:
            return True
        if self.currency_id.compare_amounts(self.amount, amount_due) == 0 \
                and self._epays_is_latest_attempt():
            return True
        charged_amount, charged_currency, _rate = self._epays_get_charge()
        if charged_currency == self.currency_id:  # Nothing was converted: no rate is needed.
            return charged_currency.compare_amounts(charged_amount, amount_due) == 0
        try:
            charge_due = self._epays_convert_to_charge(amount_due)[0]
        except ValidationError:
            return False
        return charged_currency.compare_amounts(charged_amount, charge_due) == 0

    def _epays_is_latest_attempt(self):
        """ Return whether no newer ePays payment replaced this one on the documents.

        Note: self.ensure_one()

        :return: Whether this is the documents' own payment.
        :rtype: bool
        """
        self.ensure_one()
        return not self._epays_get_document_transactions().filtered(
            lambda t: t.provider_code == 'epays' and t.provider_reference and t.id > self.id
        )

    def _epays_hold_payment(self, payment_data):
        """ Refuse a charge that does not match the documents, and hold them for the merchant.

        Odoo has no state for this: the refused transaction stops the other attempts, and no new
        payment can be made until the merchant has reviewed it.

        :param dict payment_data: The `data` of the v2 payment details.
        :return: The transaction if its state changed, an empty recordset otherwise.
        :rtype: recordset of `payment.transaction`
        """
        updated_txs = self._epays_refuse_payment(
            self._epays_get_customer_message('mismatch', payment_data),
            _(
                "ePays charged the customer for payment %(payment)s (transaction %(reference)s), "
                "which does not match these documents. Odoo did not record it, and takes no new "
                "payment on them until the transaction is released: review them, and refund it on "
                "ePays if it is not needed.",
                payment=self.provider_reference, reference=self.reference,
            ),
        )
        if updated_txs:
            self.epays_review_needed = True
        return updated_txs

    def _epays_refuse_unneeded_payment(self, payment_data):
        """ Refuse a charge on documents that were paid or cancelled meanwhile.

        :param dict payment_data: The `data` of the v2 payment details.
        :return: The transaction if its state changed, an empty recordset otherwise.
        :rtype: recordset of `payment.transaction`
        """
        _logger.warning(
            "the ePays payment %s of transaction %s completed, but its documents were already paid "
            "or cancelled", self.provider_reference, self.reference,
        )
        return self._epays_refuse_payment(
            self._epays_get_customer_message('paid_twice', payment_data),
            _(
                "ePays charged the customer for payment %(payment)s (transaction %(reference)s) "
                "although these documents were already paid or cancelled. Odoo did not record it: "
                "refund it on ePays.",
                payment=self.provider_reference, reference=self.reference,
            ),
        )

    def _epays_refuse_payment(self, message, merchant_message):
        """ Set the transaction in error for money that ePays took and that Odoo does not record,
        and tell the merchant.

        It may be cancelled, or in error after a decline that the customer retried on the same link.

        :param str message: The message for the customer.
        :param str merchant_message: What the merchant has to know.
        :return: The transaction if its state changed, an empty recordset otherwise.
        :rtype: recordset of `payment.transaction`
        """
        if self._epays_is_refused():
            return self.browse()  # Already refused; the cron reads it until its link expires.
        updated_txs = self._set_error(message, extra_allowed_states=('cancel', 'error'))
        if updated_txs:
            self._log_message_on_linked_documents(merchant_message)
            self._epays_alert_merchant(merchant_message)
        return updated_txs

    def _epays_alert_merchant(self, message):
        """ Email the company about money that needs its attention, as a note can go unread.

        The email is queued, so that a request that fails afterwards does not send it.

        Note: self.ensure_one()

        :param str message: What the merchant has to know.
        :return: None
        """
        self.ensure_one()
        company = self.company_id
        if not company.email:
            _logger.warning(
                "company %s has no email address to tell about transaction %s",
                company.name, self.reference,
            )
            return
        self.env['mail.mail'].sudo().create({
            'subject': _("ePays: review transaction %s", self.reference),
            'body_html': Markup('<p>%s</p>') % message,
            'email_from': company.email_formatted,
            'email_to': company.email,
        })

    def _epays_update_payment_method(self, gateway_id):
        """ Set the payment method from the ePays gateway that took the payment, when it is a synced
        gateway linked to a payment method.

        :param str gateway_id: The `subscriptionId` (formerly `gatewayId`) of the payment details:
                               the merchantGateway id.
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
                if str(custom_fields.get(key) or '').strip() != (sent_value or '').strip():
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
        return self._epays_convert_to_charge(self.amount)

    def _epays_convert_to_charge(self, amount):
        """ Return what ePays would charge now for an amount in the transaction's currency.

        Note: self.ensure_one()

        :param float amount: The amount.
        :return: The amount, the currency and the exchange rate (1 without conversion).
        :rtype: tuple[float, res.currency, float]
        :raise ValidationError: If the transaction's currency cannot be converted.
        """
        self.ensure_one()
        provider = self.provider_id
        charged_currency = provider._epays_get_charge_currency()
        if self.currency_id == charged_currency:
            return amount, self.currency_id, 1.0
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
        charged_amount = self.currency_id._convert(amount, charged_currency, self.company_id, date)
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
            'epays_gateway_id': epays_utils.get_first(payment_data, 'subscriptionId', 'gatewayId'),
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
        status = None if epays_utils.is_empty(status) else epays_utils.humanize_status(status)
        result = payment_data.get('result')
        if status and not epays_utils.is_empty(result) \
                and status.lower() != str(result).strip().lower():
            result_text = f'{status} ({result})'
        else:
            result_text = status or result
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
            (_("Paid At"), epays_utils.get_first(payment_data, 'completedAt', 'paidAt')),
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

        ePays queues one notification for a finished payment and gives up after three failed
        deliveries; this cron catches the payments whose notification was missed.
        That includes recently cancelled transactions: ePays can still charge their payment.
        Each transaction is committed separately so that one failure does not block the others.

        :return: None
        """
        now = fields.Datetime.now()
        txs_to_poll = self.search([
            ('provider_id.code', '=', 'epays'),
            ('provider_reference', '!=', False),
            ('create_date', '<=', now - const.CRON_MIN_AGE),
            '|',
            '&', ('state', 'in', ('draft', 'pending')),
            ('create_date', '>=', now - const.CRON_MAX_AGE),
            '|',
            '&', ('state', '=', 'error'),
            ('epays_expires_at', '>=', now - const.CRON_VOID_GRACE_PERIOD),
            '&', ('state', 'in', ('draft', 'pending', 'error', 'cancel')),
            ('epays_watch_until', '>=', now),
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

        self.env['payment.epays.unanswered.request']._cron_void_orphan_payments()

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
        """ Poll the payment status of the transaction and apply it, voiding an expired link, the
        link of documents that were cancelled, or one left open on settled documents.

        Note: self.ensure_one()

        :return: None
        """
        self.ensure_one()
        if self.state == 'cancel' and not self._epays_may_be_charged():
            return
        payment_data = self._epays_fetch_payment_details()
        target_state, _status = epays_utils.get_payment_state(payment_data)
        if self.state != 'cancel' and target_state in ('pending', 'error') and (
            self._epays_are_documents_cancelled() or self._epays_is_left_open(payment_data)
        ):
            self._epays_record_at_bank(payment_data)
            if self._epays_void() is not None:
                self._set_canceled(
                    self._epays_get_customer_message('cancelled', payment_data),
                    extra_allowed_states=('error',),
                )
                return
            # Refused: the payment may have moved on meanwhile.
            payment_data = self._epays_fetch_payment_details()
            target_state, _status = epays_utils.get_payment_state(payment_data)
        if target_state == 'pending' and self.state != 'cancel':
            # The payment link is still open. The transaction is left as it is: moving an abandoned
            # checkout to `pending` would make Odoo's post-processing email the customer.
            self._epays_record_at_bank(payment_data)
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
                    self._epays_record_at_bank(payment_data)
                    return
        self._epays_apply_payment_details(payment_data)

    def _epays_is_left_open(self, payment_data):
        """ Return whether another transaction should have voided this payment. It voids the other
        attempts once, after a commit: its read or void can fail, and a payment with the gateway is
        left alone. The scheduled check finishes the job, up to `const.CRON_MAX_AGE`.

        Note: self.ensure_one()

        :param dict payment_data: The `data` of the v2 payment details.
        :return: Whether the documents were settled or held, and the payment can be voided.
        :rtype: bool
        """
        self.ensure_one()
        if epays_utils.is_at_bank(payment_data) or self._epays_is_refused():
            return False
        others = self._epays_get_document_transactions() - self
        return not self._epays_is_needed() or any(others.mapped('epays_review_needed'))

    def _epays_are_documents_cancelled(self):
        """ Return whether every document of the transaction was cancelled or deleted, so that its
        payment link must not be paid any more.

        This addon depends on `payment` alone and cannot extend the cancellation of a sales order
        or an invoice: the scheduled check finds them.

        Note: self.ensure_one()

        :return: Whether the transaction has documents and none is left to pay.
        :rtype: bool
        """
        self.ensure_one()
        orders, invoices = self._epays_get_documents()
        documents = [*orders, *invoices]
        if not documents:
            return bool(self.epays_udf2 or self.epays_udf3)
        return all(document.sudo().state == 'cancel' for document in documents)

    def _epays_is_link_open(self):
        """ Return whether the payment link can still be paid, as far as Odoo knows. """
        self.ensure_one()
        return bool(self.epays_expires_at) and not self._epays_is_link_expired()

    def _epays_is_link_expired(self):
        """ Return whether the payment link expired more than the grace period ago. """
        self.ensure_one()
        return bool(
            self.epays_expires_at
            and fields.Datetime.now() > self.epays_expires_at + const.CRON_VOID_GRACE_PERIOD
        )

    def _epays_void(self, timeout=None):
        """ Void the pending ePays payment of the transaction.

        ePays only voids a pending or a declined payment, so a paid one can never be cancelled.

        :param int timeout: The seconds to wait for the answer; `const.REQUEST_TIMEOUT` by default.
        :return: The `data` of the response if the void succeeded, `None` if ePays refused it.
        :rtype: dict|None
        """
        try:
            return self._epays_request_payment('DELETE', timeout=timeout)
        except ValidationError:
            _logger.info(
                "ePays refused to void the payment of transaction %s; reading its status again",
                self.reference,
            )
            return None

    #=== BUSINESS METHODS - POST-PROCESSING ===#

    def _post_process(self):
        """ Override of `payment` to tell ePays about the finished payments, once they are recorded.

        This addon depends on `payment` alone, so this override runs before `account_payment`
        creates the accounting payment. Hence the commit hook: ePays must not hear that Odoo
        recorded a result that a failure rolls back, nor have the other payments voided for it.

        :return: None
        """
        super()._post_process()
        txs = self.filtered(
            lambda t: t.provider_code == 'epays' and t.provider_reference
            and t.state in ('done', 'error', 'cancel')
        )
        if txs:
            self.env.cr.postcommit.add(txs._epays_close_on_epays)

    def _epays_close_on_epays(self):
        """ Acknowledge the transactions' payments, and close the other payments on the documents
        a payment settled.

        Runs after the commit, so in its own cursor: writes on the committed one would be lost. The
        customer's status page can still be waiting. A failure must not reach the committed request:
        the cron reads what was left open, and the acknowledgement is informational.

        :return: None
        """
        try:
            with self.pool.cursor() as cr:
                for tx in self.with_env(self.env(cr=cr, su=True)).exists():
                    reference = tx.reference
                    try:
                        if tx.state == 'done' or tx.epays_review_needed:
                            tx._epays_void_other_attempts()
                        if tx.state == 'done' or tx._epays_took_no_money():
                            tx._epays_acknowledge()
                        cr.commit()
                    except Exception:  # noqa: BLE001 - one must not stop the others.
                        _logger.exception(
                            "encountered an error while closing the ePays payment of transaction "
                            "%s", reference,
                        )
                        cr.rollback()
        except Exception:  # noqa: BLE001 - the request was already committed.
            _logger.exception(
                "encountered an error while closing the ePays payments of transactions %s", self.ids
            )

    def _epays_void_other_attempts(self):
        """ Void the ePays payments still payable on the documents this transaction settled, or
        holds for the merchant.

        Typically the customer started another attempt, then the first one completed. A declined
        payment is included: ePays lets the customer retry it on the same link. A payment with the
        gateway is left alone, as a void would not stop the bank. The cron reads it, and voids what
        is still open afterwards (`_epays_is_left_open`), like an attempt that could not be voided
        here.

        Note: self.ensure_one()

        :return: None
        """
        self.ensure_one()
        attempts = self._epays_get_document_transactions().filtered(
            lambda t: t.provider_code == 'epays' and t.provider_reference
            and t.provider_reference != self.provider_reference
            and (t.state in ('draft', 'pending') or (
                t.state == 'error' and not t._epays_is_refused()
            ))
            and (self.epays_review_needed or not t._epays_is_needed())
        )
        for tx in attempts:
            try:
                payment_data = tx._epays_request_payment(
                    'GET', timeout=const.POST_PROCESSING_TIMEOUT
                )
            except ValidationError:
                _logger.info("could not read the ePays payment of transaction %s", tx.reference)
                continue
            tx._epays_record_at_bank(payment_data)
            if tx.epays_at_bank:
                continue
            if tx._epays_void(timeout=const.POST_PROCESSING_TIMEOUT) is not None:
                tx._set_canceled(
                    self._epays_get_replaced_message(), extra_allowed_states=('error',)
                )

    def _epays_took_no_money(self):
        """ Return whether ePays confirms that this declined or cancelled payment was not charged.

        Odoo's state can lag behind: ePays may have charged the payment since, and acknowledging it
        would remove it from the payments ePays lists for the merchant. Money that ePays took and
        Odoo refused (an amount or reference mismatch, a refund, a second payment) stays there too.
        A payment charged after this check is safe: ePays clears the mark when a payment completes.

        Note: self.ensure_one()

        :return: Whether the payment can be acknowledged.
        :rtype: bool
        """
        self.ensure_one()
        if self.epays_response_code == const.APPROVED_RESPONSE_CODE:
            return False
        try:
            payment_data = self._epays_request_payment(
                'GET', timeout=const.POST_PROCESSING_TIMEOUT
            )
        except ValidationError:
            _logger.info(
                "could not read the ePays payment of transaction %s; not acknowledging it",
                self.reference,
            )
            return False
        return epays_utils.get_payment_state(payment_data)[0] in ('error', 'cancel')

    def _epays_acknowledge(self):
        """ Mark the ePays payment of the transaction as processed by the merchant.

        ePays then stops notifying it and leaves it out of its unacknowledged payments. A declined
        payment completed later on the same link is notified again: ePays clears the mark when a
        payment completes. Repeats are harmless, and a failure is only logged: Odoo's transaction
        state, not this flag, keeps a payment from being processed twice.

        Note: self.ensure_one()

        :return: None
        """
        self.ensure_one()
        try:
            self._epays_request_payment(
                'POST', '/acknowledgement', timeout=const.POST_PROCESSING_TIMEOUT
            )
        except ValidationError as error:
            # A server before the endpoint answers a bare 404; an unknown payment is a 404 with
            # an ePays error code.
            if getattr(error, 'http_status', None) == 404 and not getattr(error, 'error_code', ''):
                _logger.info(
                    "ePays did not acknowledge the payment of transaction %s: this ePays server "
                    "does not support acknowledgements yet", self.reference,
                )
            else:
                _logger.warning(
                    "ePays did not acknowledge the payment of transaction %s; Odoo recorded it "
                    "anyway", self.reference,
                )
