# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

import logging
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.payment_epays import const
from odoo.addons.payment_epays import utils as epays_utils
from odoo.addons.payment_epays.utils import EpaysApiError

_logger = logging.getLogger(__name__)


class PaymentEpaysUnansweredRequest(models.Model):
    """ A payment request that ePays answered too late, for the sales orders in its `udf2`.

    Odoo gives up after `const.PAYMENT_REQUEST_TIMEOUT` and rolls the transaction back, but ePays
    may still create the payment: a pending payment that nobody has the link to, and that Odoo never
    learns about. The request is recorded in its own database transaction, so that the record
    survives that rollback, and the scheduled ePays check then voids the pending payments of those
    orders that no transaction holds.

    Only orders can be cleaned up: ePays lists payments by `udf2`, not by `udf3` (invoices).
    """
    _name = 'payment.epays.unanswered.request'
    _description = "ePays Unanswered Payment Request"
    _order = 'id'

    provider_id = fields.Many2one(
        string="Provider", comodel_name='payment.provider', required=True, ondelete='cascade',
    )
    environment = fields.Selection(selection=const.ENVIRONMENTS, required=True)
    merchant_domain = fields.Char()
    udf2 = fields.Char(string="Order References", required=True)
    reference = fields.Char(string="Transaction Reference")

    @api.model
    def _epays_record(self, tx, environment, domain, udf2):
        """ Record a payment request ePays answered too late, in its own database transaction.

        :param recordset tx: The transaction of the request, as a `payment.transaction` record.
        :param str environment: The environment of the request.
        :param str domain: The merchant domain of the request.
        :param str udf2: The order references sent in `udf2`.
        :return: None
        """
        if not udf2:
            return
        with self.pool.cursor() as cr:
            self.with_env(self.env(cr=cr, su=True)).create({
                'provider_id': tx.provider_id.id,
                'environment': environment,
                'merchant_domain': domain,
                'udf2': udf2,
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
                    request._epays_void_orphan_payments()
                    request.unlink()
                self.env.cr.commit()
            except ValidationError as error:  # ePays unreachable: retried by the next run.
                _logger.warning(
                    "could not look for the payment ePays may have created for transaction %s: %s",
                    reference, error,
                )
                self.env.cr.rollback()

    def _epays_void_orphan_payments(self):
        """ Void the pending ePays payments of the request's orders that no transaction holds.

        A payment younger than `const.UNANSWERED_REQUEST_MIN_AGE` is left alone: a click may still
        be waiting for it.

        Note: self.ensure_one()

        :return: None
        :raise EpaysApiError: If ePays cannot list the payments.
        """
        self.ensure_one()
        provider = self.provider_id
        query = urlencode({'status': 'pending', 'udf2': self.udf2, 'limit': 100})
        data = provider._epays_make_request(
            'GET', f'{const.PAYMENTS_PATH}?{query}',
            environment=self.environment, domain=self.merchant_domain,
        )
        payments = data.get('payments')
        now = datetime.now(timezone.utc)
        for payment in payments if isinstance(payments, list) else []:
            if not isinstance(payment, dict):
                continue
            payment_id = str(payment.get('paymentId') or '').strip()
            created_at = epays_utils.parse_datetime(payment.get('createdAt'))
            is_orphan_candidate = (
                payment_id
                and str(payment.get('udf2') or '').strip() == self.udf2
                and epays_utils.get_payment_state(payment)[0] == 'pending'
                and created_at
                and now - created_at >= const.UNANSWERED_REQUEST_MIN_AGE
            )
            if not is_orphan_candidate:
                continue
            if self.env['payment.transaction'].search_count([
                ('provider_code', '=', 'epays'),
                ('provider_reference', '=', payment_id),
                ('epays_environment', '=', self.environment),
            ], limit=1):
                continue  # A transaction holds it: not an orphan.
            try:
                provider._epays_make_request(
                    'DELETE', f'{const.PAYMENTS_PATH}/{quote(payment_id, safe="")}',
                    environment=self.environment, domain=self.merchant_domain,
                )
            except EpaysApiError:
                # Paid or closed meanwhile: ePays voids only a pending payment.
                _logger.info("ePays refused to void the orphan payment %s", payment_id)
                continue
            _logger.warning(
                "voided the ePays payment %s of %s: no transaction holds it (created for "
                "transaction %s, whose request ePays answered too late)",
                payment_id, self.udf2, self.reference,
            )
