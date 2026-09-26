# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

from odoo import api, models

from odoo.addons.payment_epays import const


class PaymentMethod(models.Model):
    _inherit = 'payment.method'

    def _get_compatible_payment_methods(  # noqa: PLR0917 - Odoo's signature.
        self, provider_ids, partner_id, currency_id=None, force_tokenization=False,
        is_express_checkout=False, report=None, **kwargs
    ):
        """ Override of `payment` to offer the ePays methods according to the billing address of
        the order being paid.

        Odoo checks the countries of a payment method (e.g. Benefit: Bahrain only) against the
        logged-in user's own contact, whatever address was selected at checkout. For an order,
        the ePays methods follow its billing address instead; other providers keep Odoo's rule.

        :param int sale_order_id: The sales order being paid, if any, in `kwargs`.
        """
        compatible_methods = super()._get_compatible_payment_methods(
            provider_ids, partner_id, currency_id=currency_id,
            force_tokenization=force_tokenization, is_express_checkout=is_express_checkout,
            report=report, **kwargs
        )
        billing_partner = self._epays_get_billing_partner(kwargs.get('sale_order_id'))
        partner = self.env['res.partner'].browse(partner_id)
        if not billing_partner or billing_partner.country_id == partner.country_id:
            return compatible_methods

        # The same check for the billing address, reported apart so that only the ePays methods'
        # lines of the availability report change.
        billing_report = None if report is None else {'providers': report.get('providers', {})}
        billing_methods = super()._get_compatible_payment_methods(
            provider_ids, billing_partner.id, currency_id=currency_id,
            force_tokenization=force_tokenization, is_express_checkout=is_express_checkout,
            report=billing_report, **kwargs
        )
        epays_methods = (compatible_methods | billing_methods).filtered(
            lambda pm: pm.code in const.EPAYS_METHOD_CODES
        )
        if report is not None:
            for method, line in billing_report.get('payment_methods', {}).items():
                if method.code in const.EPAYS_METHOD_CODES:
                    report.setdefault('payment_methods', {})[method] = line
        kept_methods = (compatible_methods - epays_methods) | (billing_methods & epays_methods)
        return self.search([('id', 'in', kept_methods.ids)])  # In the usual display order.

    @api.model
    def _epays_get_billing_partner(self, sale_order_id):
        """ Return the billing address of the given sales order, if `sale` is installed.

        :param int sale_order_id: The sales order being paid, if any.
        :return: The billing partner, or an empty recordset.
        :rtype: res.partner
        """
        if not sale_order_id or 'sale.order' not in self.env:
            return self.env['res.partner']
        order = self.env['sale.order'].sudo().browse(sale_order_id).exists()
        return order.partner_invoice_id

    def write(self, values):
        """ Override of `payment` to activate the brands of an ePays method with the method.

        Odoo activates the brands of the default payment methods only (`_activate_default_pms`).
        When the admin activates an individual ePays method, such as Card, its brand logos are
        activated as well so that the checkout shows them.
        """
        res = super().write(values)
        if values.get('active'):
            epays_methods = self.filtered(
                lambda pm: pm.is_primary and pm.code in const.EPAYS_METHOD_CODES
            )
            inactive_brands = epays_methods.with_context(active_test=False).brand_ids.filtered(
                lambda brand: not brand.active
            )
            if inactive_brands:
                inactive_brands.write({'active': True})
        return res
