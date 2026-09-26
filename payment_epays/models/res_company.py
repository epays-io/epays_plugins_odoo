# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

from odoo import api, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    @api.model_create_multi
    def create(self, vals_list):
        """ Override of `base` to give each new company its own ePays provider, like the ones that
        existed when the addon was installed. """
        companies = super().create(vals_list)
        self.env['payment.provider']._epays_ensure_provider_per_company(companies)
        return companies
