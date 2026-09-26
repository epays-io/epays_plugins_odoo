# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

from datetime import timedelta
from unittest.mock import patch

from odoo import fields

from odoo.addons.payment.tests.common import PaymentCommon

MAKE_REQUEST_PATH = (
    'odoo.addons.payment_epays.models.payment_provider.PaymentProvider._epays_make_request'
)
REQUESTS_PATH = 'odoo.addons.payment_epays.models.payment_provider.requests.request'


class FakeResponse:
    """ A minimal stand-in for `requests.Response`. """

    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self.ok = status_code < 400
        self._body = body
        self.text = '' if body is None else str(body)
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("No JSON body")
        return self._body


class EpaysCommon(PaymentCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.epays = cls._prepare_provider('epays', update_values={
            'epays_api_id': 'prod-api-id',
            'epays_master_key': 'prod-master-key',
            'epays_sandbox_api_id': 'sandbox-api-id',
            'epays_sandbox_master_key': 'sandbox-master-key',
            'epays_merchant_domain': 'shop.example.com',
            'epays_mode': 'sandbox',
        })
        cls.provider = cls.epays
        cls.currency = cls._prepare_currency('BHD')
        cls.amount = 137.0

        # The addon's own payment methods.
        cls.epays_method = cls.env.ref('payment_epays.payment_method_epays')
        cls.card_method = cls.env.ref('payment_epays.payment_method_epays_card')
        cls.card_visa_method = cls.env.ref('payment_epays.payment_method_epays_card_visa')
        cls.benefit_method = cls.env.ref('payment_epays.payment_method_epays_benefit')
        cls.benefit_pay_method = cls.env.ref('payment_epays.payment_method_epays_benefit_pay')
        cls.apple_pay_method = cls.env.ref('payment_epays.payment_method_epays_apple_pay')
        cls.google_pay_method = cls.env.ref('payment_epays.payment_method_epays_google_pay')
        cls.samsung_pay_method = cls.env.ref('payment_epays.payment_method_epays_samsung_pay')
        cls.tabby_method = cls.env.ref('payment_epays.payment_method_epays_tabby')
        cls.payment_method = cls.epays_method
        cls.payment_method_id = cls.epays_method.id
        cls.payment_method_code = cls.epays_method.code

        # The gateways synced from ePays (merchantGateway ids).
        cls.card_gateway_ref = '32267213'
        cls.apple_pay_gateway_ref = '22785469'
        cls.benefit_pay_gateway_ref = '40000006'
        cls.benefit_gateway_ref = '40000001'  # Inactive on ePays.
        cls.production_apple_pay_gateway_ref = '90000004'
        Gateway = cls.env['payment.epays.gateway']
        for environment, gateway_ref, type_id, method, status in [
            ('sandbox', cls.card_gateway_ref, 2, cls.card_method, 'Active'),
            ('sandbox', cls.apple_pay_gateway_ref, 4, cls.apple_pay_method, 'Active'),
            ('sandbox', cls.benefit_pay_gateway_ref, 6, cls.benefit_pay_method, 'Active'),
            ('sandbox', cls.benefit_gateway_ref, 1, cls.benefit_method, 'Inactive'),
            ('production', cls.production_apple_pay_gateway_ref, 4, cls.apple_pay_method,
             'Active'),
        ]:
            Gateway.create({
                'provider_id': cls.provider.id,
                'environment': environment,
                'gateway_ref': gateway_ref,
                'gateway_type_id': type_id,
                'name': method.name,
                'gateway_class': 'MPGS',
                'status': status,
                'payment_method_id': method.id,
            })

        cls.payment_id = 'PAY-0001'
        cls.redirect_url = 'https://testapi.epays.io/pay/PAY-0001?lang=en&token=abc'
        cls.gateway_response = {
            'fundingMethod': 'CREDIT',
            'maskedCard': '512345xxxxxx0008',
            'cardBrand': 'MASTERCARD',
            'walletProvider': None,
            'issuerBank': '',
            'rrn': '123456789012',
            'authCode': 'A1B2C3',
            'authenticationStatus': 'AUTHENTICATION_SUCCESSFUL',
            'authenticationVersion': '2.2.0',
        }
        cls._reference_counter = 0

    #=== Helpers ===#

    def _new_reference(self, prefix='EPAYS-TX'):
        type(self)._reference_counter += 1
        return f'{prefix}-{self._reference_counter}'

    def _create_epays_transaction(self, **values):
        """ Create a redirect transaction as it is after a successful ePays initiation. """
        reference = values.pop('reference', None) or self._new_reference()
        default_values = {
            'reference': reference,
            'provider_reference': f'PAY-{reference}',
            'epays_environment': 'sandbox',
            'epays_merchant_domain': 'shop.example.com',
            'epays_expires_at': fields.Datetime.now() + timedelta(hours=1),
        }
        tx = self._create_transaction('redirect', **dict(default_values, **values))
        # The charge recorded at initiation: here, the transaction's own amount and currency.
        if 'epays_charged_currency_id' not in values:
            tx.write({
                'epays_charged_amount': tx.amount,
                'epays_charged_currency_id': tx.currency_id.id,
                'epays_exchange_rate': 1.0,
            })
        # The custom fields sent at initiation.
        custom_fields = tx._epays_get_custom_fields()
        tx.write({
            'epays_udf2': values.get('epays_udf2', custom_fields['udf2']),
            'epays_udf3': values.get('epays_udf3', custom_fields['udf3']),
        })
        return tx

    def _payment_data(self, tx, **overrides):
        """ Return the v2 payment details of a completed payment matching the transaction. """
        # ePays reports what it charged: the converted amount for a transaction in another currency.
        currency = tx.epays_charged_currency_id or tx.currency_id
        amount = tx.epays_charged_amount if tx.epays_charged_currency_id else tx.amount
        data = {
            'paymentId': tx.provider_reference,
            'transactionId': 'TXN-0001',
            'result': 'CAPTURED',
            'status': 'Completed',
            'responseCode': '00',
            'amount': f'{amount:.{currency.decimal_places}f}',
            'currency': currency.name,
            'description': tx.reference,
            'gatewayId': self.card_gateway_ref,
            'reference': '',
            'customer': {'fullName': tx.partner_name},
            'createdAt': '2026-01-01T10:00:00',
            'paidAt': '2026-01-01T10:05:00',
            'updatedAt': '2026-01-01T10:05:00',
            'testMode': True,
            # ePays returns every custom field; the unused ones are null.
            'customFields': {
                'udf1': None, 'udf2': tx.epays_udf2 or '', 'udf3': tx.epays_udf3 or '',
                'udf4': None, 'udf5': None,
            },
            'responseDescription': {'en': "Approved", 'ar': "تمت الموافقة"},
            'gatewayReference': 'GW-REF-1',
            'gatewayResponse': dict(self.gateway_response),
        }
        data.update(overrides)
        return data

    def _patch_make_request(self, handler):
        """ Patch `_epays_make_request` with a handler `(provider, method, path, **kwargs)`. """
        return patch(MAKE_REQUEST_PATH, autospec=True, side_effect=handler)

    def _patch_payment_details(self, data):
        """ Patch `_epays_make_request` to return the given payment details for any GET. """
        def handler(provider, method, path, **kwargs):
            assert method == 'GET', f"unexpected {method} {path}"
            return data
        return self._patch_make_request(handler)
