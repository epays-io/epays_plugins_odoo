# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.
""" Configure a test shop for the local ePays API. Run through `odoo shell`, by `shops.py`.

The credentials come from the environment and are never printed. The admin, who pays in the
tests, gets an address in `E2E_CUSTOMER_COUNTRY`: a gateway can be limited to some countries.
"""

import os

odoo_env = globals()['env']

icp = odoo_env['ir.config_parameter'].sudo()
icp.set_param('web.base.url', os.environ['E2E_BASE_URL'])
icp.set_param('web.base.url.freeze', 'True')

# The demo company sells in USD and ePays charges BHD: the addon converts with Odoo's rates.
company = odoo_env.company
bhd = odoo_env.ref('base.BHD')
bhd.active = True
if company.currency_id != bhd and not odoo_env['res.currency.rate'].search(
    [('currency_id', '=', bhd.id), ('company_id', '=', company.id)]
):
    odoo_env['res.currency.rate'].create({
        'currency_id': bhd.id, 'company_id': company.id, 'rate': 0.376, 'name': '2026-01-01',
    })

# Unpublished: only internal users, like the admin, see the provider (Localhost mode cannot be
# published). The production pair is required whatever the mode, and outside production the addon
# uses the sandbox pair once both are filled in: the test merchant goes in the production pair, and
# in the sandbox pair too in Sandbox mode; Localhost mode empties the sandbox pair.
provider = odoo_env['payment.provider'].search(
    [('code', '=', 'epays'), ('company_id', '=', company.id)]
)
provider.ensure_one()
mode = os.environ['E2E_EPAYS_MODE']
api_id, master_key = os.environ['E2E_EPAYS_API_ID'], os.environ['E2E_EPAYS_MASTER_KEY']
sandbox = mode == 'sandbox'
provider.write({
    'state': 'test',
    'is_published': False,
    'epays_mode': mode,
    'epays_merchant_domain': os.environ['E2E_EPAYS_DOMAIN'],
    'epays_link_expiry': 60,
    'epays_api_id': api_id,
    'epays_master_key': master_key,
    'epays_sandbox_api_id': api_id if sandbox else False,
    'epays_sandbox_master_key': master_key if sandbox else False,
})
epays_method = odoo_env['payment.method'].search([('code', '=', 'epays')], limit=1)
epays_method.active = True
provider.payment_method_ids = [(4, epays_method.id)]

country = odoo_env['res.country'].search(
    [('code', '=', os.environ['E2E_CUSTOMER_COUNTRY'])], limit=1
)
odoo_env.ref('base.partner_admin').write({
    'country_id': country.id, 'state_id': False, 'city': 'Manama', 'zip': '317',
    'street': 'Road 1, Block 317',
})
odoo_env.cr.commit()
print(
    f"configured: provider {provider.id} ({provider.state}, {provider.epays_mode}), "
    f"admin in {country.code}"
)
