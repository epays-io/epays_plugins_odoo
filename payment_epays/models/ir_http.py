# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

from odoo import models


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    @classmethod
    def _get_translation_frontend_modules_name(cls):
        """ Override of `web` to translate the addon's strings on the website, e.g. the heading of
        the failure message on the payment step: the website loads the translations of the listed
        modules only. """
        mods = super()._get_translation_frontend_modules_name()
        return [*mods, 'payment_epays']
