# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.


def migrate(cr, version):
    """ 18.0.1.0.8: a refused charge is recorded as such, and is never applied afterwards. Before,
    only a failed transaction with the response code `00` showed it. That can also be a payment
    ePays declined with this code: it stays refused too, as nothing here tells them apart, and an
    unrecorded charge is what the merchant already reviews. """
    cr.execute(
        """
        UPDATE payment_transaction
           SET epays_charge_refused = TRUE
         WHERE state = 'error'
           AND epays_response_code = '00'
           AND provider_id IN (SELECT id FROM payment_provider WHERE code = 'epays')
        """
    )
