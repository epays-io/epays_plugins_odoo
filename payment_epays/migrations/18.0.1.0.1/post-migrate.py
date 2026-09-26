# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

from odoo import SUPERUSER_ID, Command, api

from odoo.addons.payment_epays import const


def migrate(cr, version):
    """ 18.0.1.0.1: a payment in another currency is converted to BHD, so the ePays providers and
    payment methods are no longer limited to BHD.

    Earlier versions set these limits themselves; they are removed only where they are still
    exactly BHD, so that a limit an administrator chose is kept.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    only_bhd = ['BHD']

    for provider in env['payment.provider'].search([('code', '=', 'epays')]):
        if provider.available_currency_ids.mapped('name') == only_bhd:
            provider.available_currency_ids = [Command.clear()]

    methods = env['payment.method'].with_context(active_test=False).search(
        [('code', 'in', list(const.EPAYS_METHOD_CODES))]
    )
    for method in methods:
        if method.supported_currency_ids.mapped('name') == only_bhd:
            method.supported_currency_ids = [Command.clear()]
