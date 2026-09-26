# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

from . import controllers
from . import models

from odoo.addons.payment import setup_provider, reset_payment_provider


def post_init_hook(env):
    setup_provider(env, 'epays')


def uninstall_hook(env):
    reset_payment_provider(env, 'epays')
    env['ir.config_parameter'].sudo().search([('key', '=', 'payment_epays.server_ip')]).unlink()
