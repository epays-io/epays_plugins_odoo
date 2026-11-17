# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.payment_epays import const
from odoo.addons.payment_epays import utils as epays_utils
from odoo.addons.payment_epays.utils import EpaysApiError

_logger = logging.getLogger(__name__)


class PaymentEpaysUnansweredRequest(models.Model):
    """ A payment request that ePays answered too late, for the documents in its `udf2` (sales
    orders) or, without sales order, its `udf3` (invoices).

    Odoo gives up after `const.PAYMENT_REQUEST_TIMEOUT` and rolls the transaction back, but ePays
    may still create the payment: a pending payment that nobody has the link to, and that Odoo never
    learns about. The request is recorded in its own database transaction, so that the record
    survives that rollback, and the scheduled ePays check then voids the pending payments of those
    documents that no transaction holds.

    ePays filters and lists payments by exact `udf2`, and by exact `udf3` since 2026-11-16. An older
    server ignores the `udf3` filter, finds the invoice only through its free-text search, which
    also matches other invoices, and does not list `udf3`: there an invoice's payment is confirmed
    by its own details before it is voided (`_epays_is_for_documents`).
    """
    _name = 'payment.epays.unanswered.request'
    _description = "ePays Unanswered Payment Request"
    _order = 'id'

    provider_id = fields.Many2one(
        string="Provider", comodel_name='payment.provider', required=True, ondelete='cascade',
    )
    environment = fields.Selection(selection=const.ENVIRONMENTS, required=True)
    merchant_domain = fields.Char()
    udf2 = fields.Char(string="Order References")
    udf3 = fields.Char(string="Invoice References")
    reference = fields.Char(string="Transaction Reference")

    # Without either, the payment cannot be listed: `udf2` was required before invoices were.
    _sql_constraints = [(
        'documents_required',
        "CHECK(COALESCE(udf2, '') != '' OR COALESCE(udf3, '') != '')",
        "An unanswered ePays payment request needs a sales order or invoice reference.",
    )]

    @api.model
    def _epays_record(self, tx, environment, domain, custom_fields):
        """ Record a payment request ePays answered too late, in its own database transaction.

        :param recordset tx: The transaction of the request, as a `payment.transaction` record.
        :param str environment: The environment of the request.
        :param str domain: The merchant domain of the request.
        :param dict custom_fields: The `customFields` of the request.
        :return: None
        """
        udf2, udf3 = custom_fields['udf2'], custom_fields['udf3']
        if not (udf2 or udf3):  # A bare payment link: nothing to find its payment by.
            return
        with self.pool.cursor() as cr:
            self.with_env(self.env(cr=cr, su=True)).create({
                'provider_id': tx.provider_id.id,
                'environment': environment,
                'merchant_domain': domain,
                'udf2': udf2,
                'udf3': udf3,
                'reference': tx.reference,
            })
        _logger.warning(
            "ePays answered the payment request of transaction %s too late; its payment, if ePays "
            "created it, will be voided by the scheduled check", tx.reference,
        )

    @api.model
    def _cron_void_orphan_payments(self):
        """ Void the payments left by the requests ePays answered too late.

        A request is handled once no click can still be waiting for its answer, and given up after
        `const.UNANSWERED_REQUEST_MAX_AGE`. Each one is committed separately.

        :return: None
        """
        now = fields.Datetime.now()
        for request in self.search([
            ('create_date', '<=', now - const.UNANSWERED_REQUEST_MIN_AGE),
        ]):
            reference = request.reference
            if request.create_date < now - const.UNANSWERED_REQUEST_MAX_AGE:
                _logger.warning(
                    "giving up on the payment ePays may have created for transaction %s",
                    reference,
                )
                request.unlink()
                self.env.cr.commit()
                continue
            try:
                with self.env.cr.savepoint():
                    if request._epays_void_orphan_payments():
                        request.unlink()
                self.env.cr.commit()
            except ValidationError as error:  # ePays unreachable: retried by the next run.
                _logger.warning(
                    "could not look for the payment ePays may have created for transaction %s: %s",
                    reference, error,
                )
                self.env.cr.rollback()

    def _epays_void_orphan_payments(self):
        """ Void the pending ePays payments of the request's documents that no transaction holds.

        Note: self.ensure_one()

        :return: Whether the request is finished: a refused void leaves it for the next run.
        :rtype: bool
        :raise EpaysApiError: If ePays cannot list or read the payments.
        """
        self.ensure_one()
        provider = self.provider_id
        is_finished = True
        # All are found before any is voided: a failed read leaves the request to the next run.
        for payment_id in self._epays_find_orphans():
            try:
                provider._epays_make_request(
                    'DELETE', f'{const.PAYMENTS_PATH}/{quote(payment_id, safe="")}',
                    environment=self.environment, domain=self.merchant_domain,
                )
            except EpaysApiError:
                # Paid or closed meanwhile, and no longer listed by the next run; or the customer is
                # paying it, and it stays payable until ePays allows the void.
                _logger.info("ePays refused to void the orphan payment %s", payment_id)
                is_finished = False
                continue
            self._epays_acknowledge_orphan(payment_id)
            _logger.warning(
                "voided the ePays payment %s of %s: no transaction holds it (created for "
                "transaction %s, whose request ePays answered too late)",
                payment_id, self.udf2 or self.udf3, self.reference,
            )
        return is_finished

    def _epays_find_orphans(self):
        """ Return the pending ePays payments of the request's documents that no transaction holds.

        A payment younger than `const.UNANSWERED_REQUEST_MIN_AGE` is left alone: a click may still
        be waiting for it.

        Note: self.ensure_one()

        :return: The ePays payment ids.
        :rtype: list
        :raise EpaysApiError: If ePays cannot list or read the payments.
        """
        self.ensure_one()
        now = datetime.now(timezone.utc)
        orphans = []
        for payment in epays_utils.read_payment_list(self._epays_list_pending_payments()):
            details = epays_utils.read_payment(payment)
            payment_id = str(details['payment_id'] or '').strip()
            created_at = epays_utils.parse_datetime(details['created_at'])
            is_orphan = (
                payment_id
                and epays_utils.get_payment_state(payment)[0] == 'pending'
                and created_at
                and now - created_at >= const.UNANSWERED_REQUEST_MIN_AGE
                and not self._epays_is_held(payment_id)
                and self._epays_is_for_documents(payment_id, details, created_at)
            )
            if is_orphan:
                orphans.append(payment_id)
        return orphans

    def _epays_is_held(self, payment_id):
        """ Return whether a transaction holds the ePays payment, which makes it no orphan.

        Note: self.ensure_one()

        :param str payment_id: The ePays payment id.
        :return: Whether a transaction holds it.
        :rtype: bool
        """
        self.ensure_one()
        return bool(self.env['payment.transaction'].search_count([
            ('provider_code', '=', 'epays'),
            ('provider_reference', '=', payment_id),
            ('epays_environment', '=', self.environment),
        ], limit=1))

    def _epays_list_pending_payments(self):
        """ List the pending ePays payments that may be the request's.

        Orders are listed by their exact `udf2`. Invoices by their exact `udf3` and, for a server
        that ignores that filter, by the free-text search `q`: on a server that knows `udf3` both
        apply, and `q` matches what `udf3` does. ePays runs `q` only inside a date window, and on
        at most `const.PAYMENT_SEARCH_MAX_LENGTH` characters: a prefix of `udf3` still matches it.
        The window takes a day on either side, as ePays reads its dates in its own time zone.

        Note: self.ensure_one()

        :return: The `data` of `GET /api/v2/Payments`.
        :rtype: dict
        :raise EpaysApiError: If ePays cannot list the payments.
        """
        self.ensure_one()
        query = {'status': 'pending', 'limit': 100}
        if self.udf2:
            query['udf2'] = self.udf2
        else:
            requested_on, day = self.create_date.date(), timedelta(days=1)
            query.update({
                'udf3': self.udf3,
                'q': self.udf3[:const.PAYMENT_SEARCH_MAX_LENGTH],
                'fromDate': (requested_on - day).isoformat(),
                'toDate': (requested_on + day).isoformat(),
            })
        return self.provider_id._epays_make_request(
            'GET', f'{const.PAYMENTS_PATH}?{urlencode(query)}',
            environment=self.environment, domain=self.merchant_domain,
        )

    def _epays_is_for_documents(self, payment_id, details, created_at):
        """ Return whether a listed payment is one of the request's documents.

        The listed `udf2`, and `udf3` on a server that lists it (since 2026-11-16), are exact. An
        older server lists no `udf3`: an invoice's payment is then read, and only if it was created
        within `const.UNANSWERED_REQUEST_CREATION_MARGIN` of the request, which keeps the reads few
        on a server whose search matched other invoices, or that ignores the search too.

        Note: self.ensure_one()

        :param str payment_id: The ePays payment id.
        :param dict details: The listed payment, as read by `epays_utils.read_payment`.
        :param datetime created_at: When ePays created it, timezone-aware.
        :return: Whether it is.
        :rtype: bool
        :raise EpaysApiError: If ePays cannot read the payment.
        """
        self.ensure_one()
        listed_udf2 = str(details['listed_udf2'] or '').strip()
        if self.udf2:
            return listed_udf2 == self.udf2
        if listed_udf2:  # An order's payment.
            return False
        if details['listed_udf3'] is not None:
            return str(details['listed_udf3']).strip() == self.udf3
        requested_at = self.create_date.replace(tzinfo=timezone.utc)
        return (
            abs(created_at - requested_at) <= const.UNANSWERED_REQUEST_CREATION_MARGIN
            and self._epays_is_invoice_orphan(payment_id)
        )

    def _epays_is_invoice_orphan(self, payment_id):
        """ Return whether ePays's own details show the payment as a pending one of the request's
        invoices, with no sales order: for a server that does not list `udf3`.

        Note: self.ensure_one()

        :param str payment_id: The ePays payment id.
        :return: Whether it is.
        :rtype: bool
        :raise EpaysApiError: If ePays cannot read the payment.
        """
        self.ensure_one()
        payment_data = self.provider_id._epays_make_request(
            'GET', f'{const.PAYMENTS_PATH}/{quote(payment_id, safe="")}',
            environment=self.environment, domain=self.merchant_domain,
        )
        details = epays_utils.read_payment(payment_data)
        # `read_payment` gives `None` for a server without `customFields`: never a match.
        return (
            epays_utils.get_payment_state(payment_data)[0] == 'pending'
            and not str(details['udf2'] or '').strip()
            and str(details['udf3'] or '').strip() == self.udf3
        )

    def _epays_acknowledge_orphan(self, payment_id):
        """ Acknowledge a voided orphan: it took no money, and would bury in ePays's unacknowledged
        list the charges the merchant has to act on. Informational: a failure is only logged.

        Note: self.ensure_one()

        :param str payment_id: The ePays payment id.
        :return: None
        """
        self.ensure_one()
        try:
            self.provider_id._epays_make_request(
                'POST',
                f'{const.PAYMENTS_PATH}/{quote(payment_id, safe="")}'
                f'{const.ACKNOWLEDGEMENT_ENDPOINT}',
                environment=self.environment, domain=self.merchant_domain,
                timeout=const.POST_PROCESSING_TIMEOUT,
            )
        except EpaysApiError:
            _logger.info("ePays did not acknowledge the orphan payment %s", payment_id)
