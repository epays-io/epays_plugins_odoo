# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

from odoo import SUPERUSER_ID, api

from odoo.addons.payment_epays import const
from odoo.addons.payment_epays import utils as epays_utils


def migrate(cr, version):
    """ 18.0.1.0.7: the master keys are encrypted at rest. Those stored before are encrypted now,
    in place: no column changes on `payment.provider`, and a key already encrypted is left alone.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    providers = env['payment.provider'].with_context(active_test=False).search(
        [('code', '=', 'epays')]
    )
    key_material = env['payment.provider']._epays_get_key_material()
    for provider in providers:
        for field_name in const.SECRET_FIELDS:
            value = provider[field_name]
            if value and not epays_utils.is_encrypted(value):
                cr.execute(
                    f'UPDATE payment_provider SET "{field_name}" = %s WHERE id = %s',  # noqa: S608
                    (epays_utils.encrypt_secret(value, key_material), provider.id),
                )
    providers.invalidate_recordset(const.SECRET_FIELDS)
