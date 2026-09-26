# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models

from odoo.addons.payment_epays import const


class PaymentEpaysGateway(models.Model):
    """ A gateway the merchant subscribed to on ePays, as returned by `GET /api/v2/gateways`.

    The rows are created by the "Sync from ePays" button. Each row links an ePays gateway to the
    Odoo payment method that opens it directly; the admin can correct that link.
    """
    _name = 'payment.epays.gateway'
    _description = "ePays Gateway"
    _order = 'provider_id, environment, gateway_type_id, id'
    _rec_name = 'name'

    provider_id = fields.Many2one(
        string="Provider", comodel_name='payment.provider', required=True, ondelete='cascade',
        index=True,
    )
    company_id = fields.Many2one(related='provider_id.company_id', store=True, index=True)
    environment = fields.Selection(selection=const.ENVIRONMENTS, required=True)
    gateway_ref = fields.Char(
        string="ePays Gateway ID",
        help="The id of the merchant's subscription to the gateway on ePays (merchantGateway). It "
             "is sent to ePays to open this gateway directly.",
        required=True,
    )
    gateway_type_id = fields.Integer(string="Gateway Type ID")
    gateway_class = fields.Char()
    name = fields.Char(string="Gateway")
    status = fields.Char()
    is_active = fields.Boolean(
        string="Active on ePays", compute='_compute_is_active', store=True,
    )
    payment_method_id = fields.Many2one(
        string="Payment Method",
        help="The Odoo payment method that opens this gateway directly.",
        comodel_name='payment.method',
        domain=[('code', 'in', const.INDIVIDUAL_PAYMENT_METHOD_CODES)],
        context={'active_test': False},
        ondelete='set null',
    )

    _sql_constraints = [(
        'gateway_uniq',
        'unique(provider_id, environment, gateway_ref)',
        "An ePays gateway can only be listed once per provider and environment.",
    )]

    @api.depends('status')
    def _compute_is_active(self):
        for gateway in self:
            gateway.is_active = (
                (gateway.status or '').strip().lower() == const.ACTIVE_GATEWAY_STATUS
            )
