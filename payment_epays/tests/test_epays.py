# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

import json
from datetime import timedelta
from unittest.mock import patch

import requests
from freezegun import freeze_time
from lxml import etree, objectify

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.modules import neutralize
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.payment import utils as payment_utils
from odoo.addons.payment.const import REPORT_REASONS_MAPPING
from odoo.addons.payment.controllers.post_processing import PaymentPostProcessing
from odoo.addons.payment.tests.http_common import PaymentHttpCommon
from odoo.addons.payment_epays import const
from odoo.addons.payment_epays import utils as epays_utils
from odoo.addons.payment_epays.controllers.main import EpaysController, EpaysPostProcessing
from odoo.addons.payment_epays.tests.common import REQUESTS_PATH, EpaysCommon, FakeResponse
from odoo.addons.payment_epays.utils import EpaysApiError

TX_LOGGER = 'odoo.addons.payment_epays.models.payment_transaction'
PROVIDER_LOGGER = 'odoo.addons.payment_epays.models.payment_provider'
CONTROLLER_LOGGER = 'odoo.addons.payment_epays.controllers.main'
PAYMENT_TX_LOGGER = 'odoo.addons.payment.models.payment_transaction'


@tagged('post_install', '-at_install')
class EpaysTest(EpaysCommon, PaymentHttpCommon):

    #=== Helpers ===#

    def _initiate(self, tx, response=None):
        """ Run the rendering of the transaction and return the captured request and the values. """
        calls = []
        response = response or {'paymentId': self.payment_id, 'redirect': self.redirect_url}

        def handler(provider, method, path, **kwargs):
            calls.append(dict(kwargs, method=method, path=path))
            return response

        with self._patch_make_request(handler):
            processing_values = tx._get_processing_values()
        return calls, processing_values

    def _sale_installed(self):
        return 'sale_order_ids' in self.env['payment.transaction']._fields

    def _create_sale_order(self, name=None):
        product = self.env['product.product'].create({'name': "Rug", 'list_price': 137.0})
        # In BHD, the only currency ePays accepts; the company's default currency is USD.
        bhd_pricelist = self.env['product.pricelist'].create({
            'name': "BHD", 'currency_id': self.currency.id,
        })
        values = {
            'partner_id': self.partner.id,
            'pricelist_id': bhd_pricelist.id,
            'order_line': [Command.create({'product_id': product.id, 'price_unit': 137.0})],
        }
        if name:
            values['name'] = name
        return self.env['sale.order'].create(values)

    def _count_detail_notes(self, record):
        return len(record.message_ids.filtered(
            lambda m: 'ePays payment details' in (m.body or '')
        ))

    #=== Payment request ===#

    @freeze_time('2026-01-01 10:00:00')
    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_payment_request_payload(self):
        tx = self._create_transaction('redirect', reference='S00012-1')
        calls, _values = self._initiate(tx)

        self.assertEqual(len(calls), 1)
        call = calls[0]
        payload = call['payload']
        self.assertEqual(call['method'], 'POST')
        self.assertEqual(call['path'], '/api/v2/Payments')
        self.assertEqual(call['environment'], 'sandbox')
        self.assertEqual(call['domain'], 'shop.example.com')
        self.assertEqual(
            call['idempotency_key'],
            payment_utils.generate_idempotency_key(tx, scope='payment_request_epays'),
        )
        self.assertEqual(payload['amount'], '137.000', "The amount is a string at 3 decimals.")
        self.assertEqual(payload['currency'], 'BHD')
        self.assertEqual(payload['description'], 'S00012-1')
        self.assertTrue(payload['notifyUrl'].endswith(f'{EpaysController._return_url}/sandbox'))
        self.assertLessEqual(len(payload['notifyUrl']), 250)
        self.assertEqual(payload['merchantDomain'], 'shop.example.com')
        self.assertIs(payload['testMode'], True)
        self.assertIn(payload['lang'], ('en', 'ar'))
        # 10:00 UTC + 60 minutes, expressed in Bahrain time (UTC+3).
        self.assertEqual(payload['expiryDate'], '2026-01-01T14:00:00')
        self.assertNotIn('gatewayId', payload, "\"ePays\": the customer chooses on ePays.")
        self.assertNotIn('trackId', payload)
        self.assertNotIn('order', payload)
        self.assertEqual(payload['customer']['fullName'], self.partner.name)
        self.assertEqual(payload['customer']['country'], 'BE')
        self.assertEqual(tx.epays_expires_at, fields.Datetime.from_string('2026-01-01 11:00:00'))

    def _initiated_gateway_id(self, payment_method):
        tx = self._create_transaction(
            'redirect', reference=self._new_reference(), payment_method_id=payment_method.id
        )
        calls, _values = self._initiate(tx)
        return calls[0]['payload'].get('gatewayId')

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_payment_request_gateway_id_per_method(self):
        rows = [
            (self.epays_method, None),  # The customer chooses on the ePays page.
            (self.apple_pay_method, self.apple_pay_gateway_ref),
            (self.benefit_pay_method, self.benefit_pay_gateway_ref),
            (self.card_method, self.card_gateway_ref),
            (self.card_visa_method, self.card_gateway_ref),  # A brand resolves to its primary.
            (self.benefit_method, None),  # Its only gateway is not active on ePays.
            (self.google_pay_method, None),  # No gateway synced for it.
        ]
        for payment_method, expected_gateway_id in rows:
            with self.subTest(method=payment_method.code):
                self.assertEqual(self._initiated_gateway_id(payment_method), expected_gateway_id)

    @mute_logger(PAYMENT_TX_LOGGER)
    def test_payment_request_without_gateway_logs_a_warning(self):
        with self.assertLogs(TX_LOGGER, level='WARNING') as logs:
            self.assertIsNone(self._initiated_gateway_id(self.google_pay_method))
        self.assertTrue(any('Google Pay' in message for message in logs.output))

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_payment_request_uses_the_gateways_of_the_environment(self):
        self.provider.epays_mode = 'production'
        tx = self._create_transaction(
            'redirect', reference='PROD-APPLE-1', payment_method_id=self.apple_pay_method.id
        )
        calls, _values = self._initiate(tx, response={
            'paymentId': self.payment_id, 'redirect': 'https://api.epays.io/pay/PAY-0001',
        })
        self.assertEqual(calls[0]['payload']['gatewayId'], self.production_apple_pay_gateway_ref)

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_payment_request_without_customer_details(self):
        self.provider.epays_send_customer_details = False
        tx = self._create_transaction('redirect', reference='NO-CUSTOMER-1')
        calls, _values = self._initiate(tx)
        self.assertNotIn('customer', calls[0]['payload'])

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_payment_request_production_environment(self):
        self.provider.write({'state': 'enabled', 'epays_mode': 'production'})
        tx = self._create_transaction('redirect', reference='PROD-1')
        response = {
            'paymentId': self.payment_id, 'redirect': 'https://api.epays.io/pay/PAY-0001',
        }
        calls, _values = self._initiate(tx, response=response)
        self.assertEqual(calls[0]['environment'], 'production')
        self.assertIs(calls[0]['payload']['testMode'], False)
        self.assertEqual(tx.epays_environment, 'production')

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_rendering_values_parse_the_redirect_url(self):
        tx = self._create_transaction('redirect', reference='RENDER-1')
        _calls, processing_values = self._initiate(tx)

        form_info = self._extract_values_from_html_form(processing_values['redirect_form_html'])
        self.assertEqual(form_info['action'], self.redirect_url)
        self.assertEqual(form_info['method'], 'get')
        self.assertDictEqual(form_info['inputs'], {'lang': 'en', 'token': 'abc'})
        self.assertEqual(tx.provider_reference, self.payment_id)
        self.assertEqual(tx.epays_environment, 'sandbox')
        self.assertEqual(tx.epays_merchant_domain, 'shop.example.com')
        self.assertEqual(tx.epays_udf2, '')
        self.assertEqual(tx.epays_udf3, '')

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_redirect_to_another_host_or_http_is_refused(self):
        for redirect in (
            'https://evil.example/pay/PAY-0001',
            'http://testapi.epays.io/pay/PAY-0001',
            'https://api.epays.io/pay/PAY-0001',  # The production host, for a sandbox payment.
            '',
        ):
            with self.subTest(redirect=redirect):
                tx = self._create_transaction('redirect', reference=self._new_reference())
                with self.assertRaises(ValidationError) as error:
                    self._initiate(tx, response={'paymentId': 'X', 'redirect': redirect})
                self.assertEqual(
                    error.exception.args[0],
                    "ePays: Could not establish the connection to the API.",
                )

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_disabled_provider_refuses_new_payments(self):
        tx = self._create_transaction('redirect', reference='DISABLED-1')
        self.provider.state = 'disabled'
        calls = []
        with self.assertRaises(ValidationError) as error:
            calls, _values = self._initiate(tx)
        self.assertEqual(
            error.exception.args[0], "ePays: This payment method is no longer available."
        )
        self.assertFalse(calls)

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
    def test_api_failure_at_checkout_shows_the_generic_message_only(self):
        tx = self._create_transaction('redirect', reference='FAIL-1')
        response = FakeResponse(401, {
            'status': 'ERROR',
            'error': {
                'code': 'UNAUTHORIZED',
                'message': "Invalid license IP",
                'details': {'reason': 'X004_INVALID_LICENSE_IP', 'observedIp': '203.0.113.7'},
            },
        })
        with patch(REQUESTS_PATH, return_value=response), \
                self.assertRaises(ValidationError) as error:
            tx._get_processing_values()
        message = error.exception.args[0]
        self.assertEqual(message, "ePays: Could not establish the connection to the API.")
        self.assertNotIn('X004', message)
        self.assertNotIn('203.0.113.7', message)

    @mute_logger(PROVIDER_LOGGER)
    def test_make_request_sends_the_auth_headers_and_parses_the_envelope(self):
        response = FakeResponse(200, {'status': 'SUCCESS', 'data': {'paymentId': 'P1'}, 'meta': {}})
        with patch(REQUESTS_PATH, return_value=response) as request_mock:
            data = self.provider._epays_make_request(
                'POST', '/api/v2/Payments', environment='sandbox', domain='shop.example.com',
                payload={'amount': '1.000'}, idempotency_key='KEY',
            )
        self.assertEqual(data, {'paymentId': 'P1'})
        args, kwargs = request_mock.call_args
        self.assertEqual(args, ('POST', 'https://testapi.epays.io/api/v2/Payments'))
        headers = kwargs['headers']
        self.assertEqual(headers['X-Api-Id'], 'sandbox-api-id')
        self.assertEqual(headers['X-Api-Master-Key'], 'sandbox-master-key')
        self.assertEqual(headers['X-Merchant-Domain'], 'shop.example.com')
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertEqual(headers['Idempotency-Key'], 'KEY')
        self.assertEqual(kwargs['timeout'], 10)

        # The production pair is used when the sandbox pair is incomplete.
        self.provider.epays_sandbox_master_key = False
        with patch(REQUESTS_PATH, return_value=response) as request_mock:
            self.provider._epays_make_request(
                'GET', '/api/v2/Payments/P1', environment='sandbox', domain='shop.example.com'
            )
        headers = request_mock.call_args.kwargs['headers']
        self.assertEqual(headers['X-Api-Id'], 'prod-api-id')
        self.assertNotIn('Content-Type', headers)
        self.assertNotIn('Idempotency-Key', headers)

    #=== Custom fields (udf) ===#

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_udf_layout_without_documents(self):
        """ Only udf2 (sales orders) and udf3 (invoices) are sent, always, even when empty. """
        self.partner.ref = 'C0042'
        tx = self._create_transaction('redirect', reference='S00012-1')
        self.assertEqual(tx._epays_get_custom_fields(), {'udf2': '', 'udf3': ''})
        calls, _values = self._initiate(tx)
        self.assertEqual(calls[0]['payload']['customFields'], {'udf2': '', 'udf3': ''})

    def test_udf6_to_udf10_are_never_sent(self):
        """ ePays reads udf6 to udf10 as gateway overrides (udf6 = MPGS merchant ID and Benefit
        alias, udf7/udf8 = MPGS display name and logo, udf9 = integration type / Benefit mode,
        udf10 = MPGS JSON overrides). A source string in udf6 made MPGS reject the payment and
        Benefit answer "Alias Name does not exist" on a live ePays, so the addon must never send
        them. """
        tx = self._create_transaction('redirect', reference='S00012-2')
        self.assertEqual(set(tx._epays_get_custom_fields()), {'udf2', 'udf3'})
        calls, _values = self._initiate(tx)
        self.assertEqual(set(calls[0]['payload']['customFields']), {'udf2', 'udf3'})

    def test_user_agent_carries_the_source_and_versions(self):
        user_agent = self.provider._epays_get_user_agent('shop.example.com')
        self.assertRegex(
            user_agent,
            r'^Odoo/\S+ payment_epays/18\.0\.\d+\.\d+\.\d+ \(db \S+; site shop\.example\.com\)$',
        )
        self.assertEqual(user_agent, user_agent.encode('latin-1').decode('latin-1'),
                         "HTTP headers must be Latin-1.")

        response = FakeResponse(200, {'status': 'SUCCESS', 'data': {}})
        with patch(REQUESTS_PATH, return_value=response) as request_mock:
            self.provider._epays_make_request(
                'GET', '/api/v2/Payments/P1', environment='sandbox', domain='shop.example.com'
            )
        self.assertEqual(request_mock.call_args.kwargs['headers']['User-Agent'], user_agent)

    def test_long_reference_lists_are_cut_at_a_whole_reference(self):
        names = [f'S{index:010d}' for index in range(40)]  # 11 characters each.
        joined = epays_utils.join_references(names)
        self.assertLessEqual(len(joined), 255)
        self.assertTrue(joined.endswith(', …'))
        kept = joined[:-len(', …')].split(', ')
        self.assertEqual(kept, names[:len(kept)], "Only whole references are kept.")
        self.assertEqual(epays_utils.join_references(['S1', 'S2']), 'S1, S2')

    def test_udf_layout_for_sale_orders(self):
        if not self._sale_installed():
            self.skipTest("sale is not installed")
        order = self._create_sale_order()
        tx = self._create_transaction(
            'redirect', reference=False, sale_order_ids=[Command.set(order.ids)]
        )
        self.assertEqual(
            tx._epays_get_custom_fields(), {'udf2': order.name, 'udf3': ''},
            "Shop orders are invoiced after the payment.",
        )

        other_order = self._create_sale_order()
        tx_multi = self._create_transaction(
            'redirect', reference='MULTI-1',
            sale_order_ids=[Command.set((order + other_order).ids)],
        )
        self.assertEqual(
            tx_multi._epays_get_custom_fields()['udf2'],
            ', '.join((order + other_order).mapped('name')),
        )

    #=== Notification processing ===#

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_status_mapping(self):
        rows = [
            ({'status': 'Completed'}, 'done'),
            ({'status': None, 'result': 'CAPTURED'}, 'done'),
            ({'status': None, 'result': ' approved '}, 'done'),
            ({'status': 'Refunded'}, 'error'),  # The money went back: never done.
            ({'status': 'Pending'}, 'pending'),
            ({'status': None, 'result': '3DS Authentication Initiated'}, 'pending'),
            ({'status': 'Failed'}, 'error'),
            ({'status': None, 'result': 'DECLINED'}, 'error'),
            ({'status': 'Error'}, 'error'),
            ({'status': 'Cancelled'}, 'cancel'),
            ({'status': None, 'result': 'CANCELED'}, 'cancel'),
            ({'status': 'Expired'}, 'cancel'),
            ({'status': 'Voided'}, 'cancel'),
            ({'status': None, 'result': 'SUCCESS'}, 'draft'),
            ({'status': 'Unknown', 'result': 'CAPTURED'}, 'draft'),
            ({'status': 'Failed', 'result': 'CAPTURED'}, 'error'),  # `status` takes precedence.
        ]
        for overrides, expected_state in rows:
            with self.subTest(**{k: str(v) for k, v in overrides.items()}):
                tx = self._create_epays_transaction()
                data = self._payment_data(tx, **overrides)
                if 'status' in overrides and overrides['status'] is None:
                    del data['status']  # An ePays server without the canonical status.
                with self._patch_payment_details(data):
                    tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
                self.assertEqual(tx.state, expected_state)

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_payment_details_mismatch_sets_error(self):
        rows = [
            {'amount': '136.999'},
            {'amount': 'not a number'},
            {'currency': 'USD'},
            {'customFields': {'udf2': 'S99999', 'udf3': ''}},  # Another order.
            {'customFields': {'udf2': '', 'udf3': 'INV/2026/99999'}},  # Another invoice.
        ]
        for overrides in rows:
            with self.subTest(**{k: str(v) for k, v in overrides.items()}):
                tx = self._create_epays_transaction()
                with self._patch_payment_details(self._payment_data(tx, **overrides)):
                    tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
                self.assertEqual(tx.state, 'error')
                # Money may have been taken: the customer is told to get in touch, not to retry.
                self.assertEqual(tx.state_message, (
                    "We could not confirm your payment. Please contact us before trying again, "
                    f"quoting the reference {tx.reference}."
                ))

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_amount_as_number_and_missing_custom_fields_complete(self):
        tx = self._create_epays_transaction()
        data = self._payment_data(tx, amount=137)
        del data['customFields']  # An ePays server without customFields: udf2/udf3 unchecked.
        with self._patch_payment_details(data):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(tx.state, 'done')

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_status_is_fetched_with_the_stored_environment_and_domain(self):
        tx = self._create_epays_transaction(epays_merchant_domain='old.example.com')
        self.provider.write({
            'state': 'enabled',
            'epays_mode': 'production',
            'epays_merchant_domain': 'new.example.com',
        })
        calls = []

        def handler(provider, method, path, **kwargs):
            calls.append(dict(kwargs, method=method, path=path))
            return self._payment_data(tx)

        with self._patch_make_request(handler):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(calls[0]['path'], f'/api/v2/Payments/{tx.provider_reference}')
        self.assertEqual(calls[0]['environment'], 'sandbox')
        self.assertEqual(calls[0]['domain'], 'old.example.com')
        self.assertEqual(tx.state, 'done')

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_payment_method_follows_the_epays_gateway(self):
        tx = self._create_epays_transaction()  # Paid with "ePays": chosen on the ePays page.
        data = self._payment_data(tx, gatewayId=int(self.apple_pay_gateway_ref))
        with self._patch_payment_details(data):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(tx.payment_method_id, self.apple_pay_method)

        # An inactive gateway still names the method that took the payment.
        tx_benefit = self._create_epays_transaction()
        data = self._payment_data(tx_benefit, gatewayId=self.benefit_gateway_ref)
        with self._patch_payment_details(data):
            tx_benefit._handle_notification_data(
                'epays', {'paymentId': tx_benefit.provider_reference}
            )
        self.assertEqual(tx_benefit.payment_method_id, self.benefit_method)

        tx_unknown_gateway = self._create_epays_transaction()
        with self._patch_payment_details(self._payment_data(tx_unknown_gateway, gatewayId='99')):
            tx_unknown_gateway._handle_notification_data(
                'epays', {'paymentId': tx_unknown_gateway.provider_reference}
            )
        self.assertEqual(tx_unknown_gateway.payment_method_id, self.epays_method)

        # The gateways of the other environment are not used.
        tx_production_gateway = self._create_epays_transaction()
        data = self._payment_data(
            tx_production_gateway, gatewayId=self.production_apple_pay_gateway_ref
        )
        with self._patch_payment_details(data):
            tx_production_gateway._handle_notification_data(
                'epays', {'paymentId': tx_production_gateway.provider_reference}
            )
        self.assertEqual(tx_production_gateway.payment_method_id, self.epays_method)

    def test_unknown_payment_id_raises(self):
        with self.assertRaises(ValidationError):
            self.env['payment.transaction']._get_tx_from_notification_data(
                'epays', {'paymentId': 'UNKNOWN'}
            )
        with self.assertRaises(ValidationError):
            self.env['payment.transaction']._get_tx_from_notification_data('epays', {})

    @mute_logger(CONTROLLER_LOGGER, TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_return_route_redirects_to_the_status_page_for_an_unknown_payment(self):
        url = self._build_url(EpaysController._return_url)
        with self._patch_payment_details({}):
            response = self.opener.get(url, params={'paymentId': 'UNKNOWN'}, allow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers['Location'].endswith('/payment/status'))

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_return_route_logs_an_unknown_payment_as_one_warning_without_traceback(self):
        """ A notification for a payment this database does not hold is expected (e.g. a payment
        created outside Odoo on the same merchant) and handled, so it must not log a traceback. """
        url = self._build_url(EpaysController._return_url)
        with self._patch_payment_details({}), \
                self.assertLogs(CONTROLLER_LOGGER, level='WARNING') as logs:
            response = self.opener.get(url, params={'paymentId': 'UNKNOWN'}, allow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        self.assertEqual(record.levelname, 'WARNING')
        self.assertIsNone(record.exc_info, "a handled notification must not log a traceback")
        self.assertIn('UNKNOWN', record.getMessage())

    @mute_logger(CONTROLLER_LOGGER, TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_return_route_confirms_the_transaction(self):
        tx = self._create_epays_transaction()
        url = self._build_url(EpaysController._return_url)
        with self._patch_payment_details(self._payment_data(tx)):
            response = self.opener.get(
                url, params={'paymentId': tx.provider_reference}, allow_redirects=False
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(tx.state, 'done')

    @mute_logger(CONTROLLER_LOGGER, TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_return_route_accepts_post(self):
        tx = self._create_epays_transaction()
        url = self._build_url(EpaysController._return_url)
        with self._patch_payment_details(self._payment_data(tx)):
            response = self.opener.post(
                url, data={'paymentId': tx.provider_reference}, allow_redirects=False
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(tx.state, 'done')

    #=== After a failed payment ===#

    def test_status_poll_sends_a_failed_shop_payment_back_to_the_payment_step(self):
        retry_route = '/shop/payment?epays_failed=1'
        rows = [
            # (provider code, state, landing route, expected landing route)
            ('epays', 'error', '/shop/payment/validate', retry_route),
            ('epays', 'cancel', '/shop/payment/validate', retry_route),
            ('epays', 'done', '/shop/payment/validate', '/shop/payment/validate'),
            ('epays', 'error', '/my/invoices/7?access_token=t', '/my/invoices/7?access_token=t'),
            ('stripe', 'error', '/shop/payment/validate', '/shop/payment/validate'),
        ]
        for provider_code, state, landing_route, expected_route in rows:
            polled_values = {
                'provider_code': provider_code, 'state': state, 'landing_route': landing_route,
            }
            with self.subTest(provider=provider_code, state=state, landing=landing_route), \
                    patch.object(PaymentPostProcessing, 'poll_status', return_value=polled_values):
                values = EpaysPostProcessing().poll_status()
                self.assertEqual(values['landing_route'], expected_route)

    def test_failure_route_returns_the_reason_of_the_monitored_transaction_only(self):
        declined = self._create_epays_transaction(state='error', state_message="Declined.")
        done = self._create_epays_transaction(state='done')
        other = self._create_transaction(
            'redirect', reference='OTHER-ERR', provider_id=self.dummy_provider.id, state='error',
        )
        for monitored, expected in [
            (declined, {'state': 'error', 'message': "Declined."}),
            (done, {}),
            (other, {}),
            (self.env['payment.transaction'], {}),  # Nothing monitored in this session.
        ]:
            with self.subTest(tx=monitored.reference), patch.object(
                EpaysPostProcessing, '_get_monitored_transaction', return_value=monitored,
            ):
                self.assertEqual(EpaysPostProcessing().epays_failure(), expected)

    #=== Payment details written to the transaction and the order ===#

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_payment_details_are_stored_on_the_transaction(self):
        tx = self._create_epays_transaction()
        with self._patch_payment_details(self._payment_data(tx)):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(tx.epays_transaction_id, 'TXN-0001')
        self.assertEqual(tx.epays_gateway_reference, 'GW-REF-1')
        self.assertEqual(tx.epays_response_code, '00')
        self.assertEqual(tx.epays_response_desc, 'Approved')
        self.assertEqual(tx.epays_gateway_id, self.card_gateway_ref)
        self.assertEqual(json.loads(tx.epays_gateway_response), self.gateway_response)
        display = tx.epays_gateway_response_display
        self.assertIn('Card Brand: MASTERCARD', display)
        self.assertIn('RRN: 123456789012', display)
        self.assertNotIn('Issuer Bank', display, "Empty values are skipped.")
        self.assertNotIn('Wallet Provider', display, "Empty values are skipped.")

    def test_details_note_is_written_once_on_the_sale_order(self):
        if not self._sale_installed():
            self.skipTest("sale is not installed")
        order = self._create_sale_order()
        tx = self._create_epays_transaction(sale_order_ids=[Command.set(order.ids)])
        data = self._payment_data(tx)
        with self._patch_payment_details(data), \
                patch.object(self.env.cr, 'commit'), patch.object(self.env.cr, 'rollback'), \
                patch('odoo.addons.payment.models.payment_transaction.PaymentTransaction'
                      '._cron_post_process'), \
                mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})  # Ping.
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})  # Return.
            self.env['payment.transaction']._cron_epays_poll_pending()  # Cron.
        self.assertEqual(tx.state, 'done')
        self.assertEqual(self._count_detail_notes(order), 1)
        note = order.message_ids.filtered(lambda m: 'ePays payment details' in (m.body or ''))
        self.assertIn('Card Brand: MASTERCARD', note.body)
        self.assertIn('GW-REF-1', note.body)
        self.assertNotIn('Issuer Bank', note.body)

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_response_without_gateway_response_completes_without_gateway_block(self):
        order = self._create_sale_order() if self._sale_installed() else None
        values = {'sale_order_ids': [Command.set(order.ids)]} if order else {}
        tx = self._create_epays_transaction(**values)
        data = self._payment_data(tx)
        for key in ('gatewayResponse', 'responseDescription', 'gatewayReference', 'updatedAt',
                    'customFields'):
            del data[key]  # An ePays server that predates these fields.
        with self._patch_payment_details(data):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(tx.state, 'done')
        self.assertFalse(tx.epays_gateway_response)
        self.assertFalse(tx.epays_gateway_response_display)
        if order:
            note = order.message_ids.filtered(lambda m: 'ePays payment details' in (m.body or ''))
            self.assertEqual(len(note), 1)
            self.assertNotIn('<hr', note.body)
            self.assertNotIn('Card Brand', note.body)

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_decline_shows_the_description_in_the_customer_language(self):
        declined = {
            'status': 'Failed', 'result': 'DECLINED', 'responseCode': '05',
            'responseDescription': {'en': "Do not honour", 'ar': "مرفوض"},
        }
        tx_en = self._create_epays_transaction()
        with self._patch_payment_details(self._payment_data(tx_en, **declined)):
            tx_en._handle_notification_data('epays', {'paymentId': tx_en.provider_reference})
        self.assertEqual(tx_en.state, 'error')
        self.assertEqual(
            tx_en.state_message,
            "Your payment was declined: Do not honour. "
            "You can try again or choose another payment method.",
        )

        # The whole message follows the customer, whatever the language of the request.
        self.env['res.lang']._activate_lang('ar_001')
        self.env['ir.module.module']._load_module_terms(['payment_epays'], ['ar_001'])
        self.partner.lang = 'ar_001'
        tx_ar = self._create_epays_transaction()
        with self._patch_payment_details(self._payment_data(tx_ar, **declined)):
            tx_ar.with_context(lang='en_US')._handle_notification_data(
                'epays', {'paymentId': tx_ar.provider_reference}
            )
        self.assertEqual(
            tx_ar.state_message,
            "تم رفض الدفع: مرفوض. يمكنك المحاولة مرة أخرى أو اختيار طريقة دفع أخرى.",
        )

        # A raw status (here `Failed`) means nothing to a customer and is not shown.
        self.partner.lang = 'en_US'
        tx_old_server = self._create_epays_transaction()
        data = self._payment_data(tx_old_server, **declined)
        del data['responseDescription']
        with self._patch_payment_details(data):
            tx_old_server._handle_notification_data(
                'epays', {'paymentId': tx_old_server.provider_reference}
            )
        self.assertEqual(
            tx_old_server.state_message,
            "Your payment was declined. You can try again or choose another payment method.",
        )

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_cancel_tells_the_customer_they_can_try_again(self):
        tx = self._create_epays_transaction()
        with self._patch_payment_details(self._payment_data(tx, status='Cancelled')):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(tx.state, 'cancel')
        self.assertEqual(
            tx.state_message,
            "Your payment was cancelled. You can try again or choose another payment method.",
        )

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_retry_after_decline_completes(self):
        tx = self._create_epays_transaction()
        with self._patch_payment_details(self._payment_data(tx, status='Failed')):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(tx.state, 'error')
        with self._patch_payment_details(self._payment_data(tx)):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(tx.state, 'done')

    #=== Cron ===#

    def _run_cron(self, handler):
        with self._patch_make_request(handler), \
                patch.object(self.env.cr, 'commit'), patch.object(self.env.cr, 'rollback'), \
                patch('odoo.addons.payment.models.payment_transaction.PaymentTransaction'
                      '._cron_post_process', autospec=True) as post_process_mock, \
                mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            self.env['payment.transaction']._cron_epays_poll_pending()
        return post_process_mock

    def _set_create_date(self, tx, create_date):
        self.env.cr.execute(
            "UPDATE payment_transaction SET create_date = %s WHERE id = %s", (create_date, tx.id)
        )
        tx.invalidate_recordset(['create_date'])

    def test_cron_picks_only_the_intended_transactions(self):
        now = fields.Datetime.now()
        expected, ignored = self.env['payment.transaction'], self.env['payment.transaction']
        for state, age, expires_in, has_reference, expect in [
            ('draft', timedelta(minutes=10), timedelta(hours=1), True, True),
            ('pending', timedelta(hours=2), timedelta(hours=1), True, True),
            ('error', timedelta(minutes=10), timedelta(minutes=20), True, True),
            ('error', timedelta(hours=2), -timedelta(minutes=5), True, False),  # Link expired.
            ('draft', timedelta(minutes=2), timedelta(hours=1), True, False),  # Too recent.
            ('pending', timedelta(days=4), timedelta(hours=1), True, False),  # Too old.
            ('done', timedelta(minutes=10), timedelta(hours=1), True, False),
            ('cancel', timedelta(minutes=10), timedelta(hours=1), True, False),
            ('draft', timedelta(minutes=10), timedelta(hours=1), False, False),  # Not initiated.
        ]:
            tx = self._create_epays_transaction(
                state=state,
                epays_expires_at=now + expires_in,
                provider_reference=None if not has_reference else self._new_reference('PAY'),
            )
            self._set_create_date(tx, now - age)
            if expect:
                expected |= tx
            else:
                ignored |= tx
        other_provider_tx = self._create_transaction(
            'redirect', reference='OTHER-1', provider_id=self.dummy_provider.id,
            provider_reference='PAY-OTHER',
        )
        self._set_create_date(other_provider_tx, now - timedelta(minutes=10))

        polled = []

        def handler(provider, method, path, **kwargs):
            polled.append(path.rsplit('/', 1)[1])
            return {'status': 'Pending'}

        self._run_cron(handler)
        self.assertCountEqual(polled, expected.mapped('provider_reference'))

    def test_cron_voids_an_expired_pending_payment(self):
        tx = self._create_epays_transaction(
            state='pending', epays_expires_at=fields.Datetime.now() - timedelta(minutes=31)
        )
        self._set_create_date(tx, fields.Datetime.now() - timedelta(hours=2))
        calls = []

        def handler(provider, method, path, **kwargs):
            calls.append(method)
            if method == 'DELETE':
                return {'paymentId': tx.provider_reference, 'status': 'Voided',
                        'voidedAt': '2026-01-01T12:00:00'}
            return self._payment_data(tx, status='Pending', result='')

        self._run_cron(handler)
        self.assertEqual(calls, ['GET', 'DELETE'])
        self.assertEqual(tx.state, 'cancel')

    def test_cron_does_not_void_within_the_grace_period(self):
        tx = self._create_epays_transaction(
            state='pending', epays_expires_at=fields.Datetime.now() - timedelta(minutes=10)
        )
        self._set_create_date(tx, fields.Datetime.now() - timedelta(hours=2))
        calls = []

        def handler(provider, method, path, **kwargs):
            calls.append(method)
            return self._payment_data(tx, status='Pending', result='')

        self._run_cron(handler)
        self.assertEqual(calls, ['GET'])
        self.assertEqual(tx.state, 'pending')

    def test_cron_leaves_an_open_payment_link_untouched(self):
        tx = self._create_epays_transaction()  # Draft: the customer is on the ePays page.
        self._set_create_date(tx, fields.Datetime.now() - timedelta(minutes=20))
        self._run_cron(lambda *args, **kwargs: self._payment_data(tx, status='Pending'))
        self.assertEqual(tx.state, 'draft', "An abandoned checkout must not become pending.")

    def test_cron_reads_the_status_again_when_the_void_is_refused(self):
        tx = self._create_epays_transaction(
            state='pending', epays_expires_at=fields.Datetime.now() - timedelta(hours=1)
        )
        self._set_create_date(tx, fields.Datetime.now() - timedelta(hours=2))
        calls = []

        def handler(provider, method, path, **kwargs):
            calls.append(method)
            if method == 'DELETE':
                raise EpaysApiError("ePays: refused", http_status=400)
            if len(calls) == 1:
                return self._payment_data(tx, status='Pending', result='')
            return self._payment_data(tx)  # Paid just before the void.

        self._run_cron(handler)
        self.assertEqual(calls, ['GET', 'DELETE', 'GET'])
        self.assertEqual(tx.state, 'done')

    def test_cron_post_processes_finished_transactions(self):
        tx_done = self._create_epays_transaction(state='done', is_post_processed=False)
        tx_processed = self._create_epays_transaction(state='done', is_post_processed=True)
        self._create_transaction(
            'redirect', reference='OTHER-DONE', provider_id=self.dummy_provider.id, state='done',
        )
        post_process_mock = self._run_cron(lambda *args, **kwargs: {})
        self.assertEqual(post_process_mock.call_count, 1)
        post_processed_txs = post_process_mock.call_args.args[0]
        self.assertIn(tx_done, post_processed_txs)
        self.assertNotIn(tx_processed, post_processed_txs)
        self.assertEqual(set(post_processed_txs.mapped('provider_code')), {'epays'})

    def test_cron_continues_after_a_failing_transaction(self):
        tx_failing = self._create_epays_transaction(state='pending')
        tx_ok = self._create_epays_transaction(state='pending')
        for tx in tx_failing + tx_ok:
            self._set_create_date(tx, fields.Datetime.now() - timedelta(hours=1))

        def handler(provider, method, path, **kwargs):
            if path.endswith(tx_failing.provider_reference):
                raise RuntimeError("unexpected failure")
            return self._payment_data(tx_ok)

        self._run_cron(handler)
        self.assertEqual(tx_failing.state, 'pending')
        self.assertEqual(tx_ok.state, 'done')

    #=== Availability ===#

    def _compatible_providers(self, user, currency=None):
        return self.env['payment.provider'].with_user(user).sudo()._get_compatible_providers(
            self.company_id, self.partner.id, self.amount,
            currency_id=(currency or self.currency).id,
        )

    def test_test_mode_is_visible_to_internal_users_only(self):
        self.provider.write({'state': 'test', 'is_published': False})
        self.assertNotIn(self.provider, self._compatible_providers(self.public_user))
        self.assertNotIn(self.provider, self._compatible_providers(self.portal_user))
        self.assertIn(self.provider, self._compatible_providers(self.internal_user))

        self.provider.write({'state': 'enabled', 'epays_mode': 'production', 'is_published': True})
        self.assertIn(self.provider, self._compatible_providers(self.public_user))
        self.assertIn(self.provider, self._compatible_providers(self.portal_user))

        self.provider.write({'state': 'disabled'})
        self.assertNotIn(self.provider, self._compatible_providers(self.internal_user))

    #=== Currency conversion ===#

    def _set_rate(self, currency, rate):
        """ Set today's rate of the currency (units per unit of the company's currency). """
        Rate = self.env['res.currency.rate']
        today = fields.Date.context_today(Rate)
        existing = Rate.search([
            ('currency_id', '=', currency.id), ('name', '=', today),
            ('company_id', 'in', (False, self.env.company.id)),
        ])
        if existing:
            existing.rate = rate
        else:
            Rate.create({
                'currency_id': currency.id, 'rate': rate, 'name': today,
                'company_id': self.env.company.id,
            })

    def _usd_at(self, bhd_per_usd):
        """ Make 1 USD worth the given BHD, whatever the company's currency. """
        usd = self._prepare_currency('USD')
        self._set_rate(usd, 1.0)
        self._set_rate(self.currency, bhd_per_usd)
        return usd

    def _without_rates(self, code):
        currency = self._prepare_currency(code)
        if currency == self.env.company.currency_id:
            self.skipTest(f"{code} is the company's currency")
        self.env['res.currency.rate'].search([('currency_id', '=', currency.id)]).unlink()
        return currency

    def test_epays_is_offered_in_every_currency_odoo_can_convert(self):
        self.provider.write({'state': 'enabled', 'epays_mode': 'production', 'is_published': True})
        self.assertFalse(self.provider.available_currency_ids, "No currency is excluded.")
        usd = self._usd_at(0.376)
        self.assertIn(self.provider, self._compatible_providers(self.internal_user))  # BHD.
        self.assertIn(self.provider, self._compatible_providers(self.internal_user, usd))

        # Odoo converts with a rate of 1 when there is none: 100 JPY would be charged 100 BHD.
        jpy = self._without_rates('JPY')
        report = {}
        providers = self.env['payment.provider'].with_user(self.internal_user).sudo()\
            ._get_compatible_providers(
                self.company_id, self.partner.id, self.amount, currency_id=jpy.id, report=report,
            )
        self.assertNotIn(self.provider, providers)
        self.assertEqual(  # The reasons are lazy translations.
            str(report['providers'][self.provider]['reason']),
            str(REPORT_REASONS_MAPPING['incompatible_currency']),
        )

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_a_payment_in_another_currency_is_charged_in_bhd(self):
        usd = self._usd_at(0.376)
        tx = self._create_transaction(
            'redirect', reference='USD-1', amount=100.0, currency_id=usd.id
        )
        calls, _values = self._initiate(tx)

        payload = calls[0]['payload']
        self.assertEqual(payload['currency'], 'BHD')
        self.assertEqual(payload['amount'], '37.600', "100 USD at 0.376, at 3 decimals.")
        self.assertEqual((tx.amount, tx.currency_id), (100.0, usd), "The order's currency stays.")
        self.assertEqual(tx.epays_charged_currency_id, self.currency)
        self.assertAlmostEqual(tx.epays_charged_amount, 37.6)
        self.assertAlmostEqual(tx.epays_exchange_rate, 0.376)

        # The payment is checked against what was charged, even after the rates moved.
        self._set_rate(self.currency, 0.380)
        with self._patch_payment_details(self._payment_data(tx)):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(tx.state, 'done')

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_a_converted_payment_must_come_back_in_bhd(self):
        usd = self._usd_at(0.376)
        for overrides in ({'amount': '100.000', 'currency': 'USD'}, {'amount': '37.500'}):
            with self.subTest(**overrides):
                tx = self._create_transaction(
                    'redirect', reference=self._new_reference(), amount=100.0, currency_id=usd.id,
                )
                self._initiate(tx)
                with self._patch_payment_details(self._payment_data(tx, **overrides)):
                    tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
                self.assertEqual(tx.state, 'error')

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_a_currency_without_rate_never_reaches_epays(self):
        jpy = self._without_rates('JPY')
        tx = self._create_transaction('redirect', reference='JPY-1', currency_id=jpy.id)
        with self._patch_make_request(lambda *args, **kwargs: {}) as make_request_mock, \
                self.assertRaises(ValidationError) as error:
            tx._get_processing_values()
        make_request_mock.assert_not_called()
        self.assertEqual(
            error.exception.args[0],
            "ePays: Payments in JPY cannot be taken at the moment. Please contact us.",
        )

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_the_note_tells_the_order_amount_and_the_rate(self):
        self._skip_without_sale()
        usd = self._usd_at(0.376)
        order = self._create_sale_order()
        order.pricelist_id = self.env['product.pricelist'].create({
            'name': "USD", 'currency_id': usd.id,
        })
        tx = self._create_transaction(
            'redirect', reference='USD-NOTE', **self._order_values(order),
        )
        self._initiate(tx)
        with self._patch_payment_details(self._payment_data(tx)):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        note = order.message_ids.filtered(lambda m: 'ePays payment details' in (m.body or ''))
        self.assertIn(f'Order Amount: {tx.amount:.2f} USD', note.body)
        self.assertIn('Exchange Rate: 1 USD = 0.376 BHD', note.body)

    def test_pay_again_follows_the_rate_of_the_day(self):
        """ Same rate: the pending payment is reused. New rate: the BHD amount changed, so the old
        payment is voided and a new one opens at the new rate. """
        self._skip_without_sale()
        usd = self._usd_at(0.376)
        order = self._create_sale_order()
        order.pricelist_id = self.env['product.pricelist'].create({
            'name': "USD", 'currency_id': usd.id,
        })
        first = self._earlier_attempt(
            order, state='pending', currency_id=usd.id, amount=order.amount_total,
            epays_charged_amount=usd._convert(
                order.amount_total, self.currency, self.env.company,
                fields.Date.context_today(order),
            ),
            epays_charged_currency_id=self.currency.id, epays_exchange_rate=0.376,
        )
        pending = self._payment_data(first, status='Pending', result='')

        tx, calls, _values = self._pay_again([pending], **self._order_values(order))
        self.assertEqual([method for method, _path in calls], ['GET'])
        self.assertEqual(tx.provider_reference, first.provider_reference)

        self._set_rate(self.currency, 0.380)
        tx.state = 'pending'  # The reused transaction is now the earlier attempt.
        voided = {'paymentId': tx.provider_reference, 'status': 'Voided'}
        new_tx, calls, _values = self._pay_again(
            [self._payment_data(tx, status='Pending', result='')], delete_response=voided,
            **self._order_values(order),
        )
        self.assertEqual([method for method, _path in calls], ['GET', 'DELETE', 'POST'])
        self.assertAlmostEqual(new_tx.epays_exchange_rate, 0.380)

    def test_default_payment_methods(self):
        self.assertEqual(self.provider._get_default_payment_method_codes(), {
            'epays', 'epays_brand_visa', 'epays_brand_mastercard', 'epays_brand_benefit',
            'epays_brand_benefit_pay', 'epays_brand_apple_pay', 'epays_brand_google_pay',
            'epays_brand_samsung_pay', 'epays_brand_tabby',
        })

    def test_every_company_gets_its_own_epays_provider(self):
        """ Odoo lists only the providers of the current company; without its own provider, a
        company would not find ePays under Payment Providers. """
        company_b = self.env['res.company'].create({'name': "Company B"})
        provider_b = self.env['payment.provider'].search(
            [('code', '=', 'epays'), ('company_id', '=', company_b.id)]
        )
        self.assertEqual(len(provider_b), 1, "A new company gets one at creation.")
        self.assertEqual(provider_b.name, 'ePays')
        self.assertEqual(provider_b.state, 'disabled')
        self.assertFalse(provider_b.is_published)
        self.assertFalse(provider_b.epays_api_id or provider_b.epays_master_key)
        self.assertEqual(
            provider_b.with_context(active_test=False).payment_method_ids,
            self.provider.with_context(active_test=False).payment_method_ids,
            "It offers the same payment methods.",
        )
        self.assertIn(provider_b, self.env['payment.provider'].with_context(
            allowed_company_ids=company_b.ids
        ).search([]), "Listed when working in that company.")

        # A company without one (e.g. created before the addon) gets it on the next update.
        provider_b.unlink()
        self.env['payment.provider']._epays_ensure_provider_per_company()
        self.assertEqual(self.env['payment.provider'].search_count(
            [('code', '=', 'epays'), ('company_id', '=', company_b.id)]
        ), 1)
        self.env['payment.provider']._epays_ensure_provider_per_company()
        self.assertEqual(self.env['payment.provider'].search_count(
            [('code', '=', 'epays'), ('company_id', '=', company_b.id)]
        ), 1, "Never a second one.")

    def test_second_company(self):
        company_b = self.env['res.company'].create({'name': "Company B"})
        duplicate = self.provider.copy({'company_id': company_b.id})
        self.assertEqual(duplicate.state, 'disabled', "The copy starts disabled.")
        self.assertFalse(duplicate.epays_api_id, "Credentials are not copied.")
        self.assertFalse(duplicate.epays_master_key, "Credentials are not copied.")

        provider_b = self._prepare_provider('epays', company=company_b, update_values={
            'epays_api_id': 'b-api-id', 'epays_master_key': 'b-master-key',
        })
        self.assertEqual(provider_b.company_id, company_b)
        self.assertEqual(provider_b.code, 'epays')
        self.assertNotEqual(provider_b, self.provider)

    def test_credentials_are_required_when_enabled(self):
        with self.assertRaises(ValidationError):
            self.provider.write({'epays_master_key': False})

    def test_neutralize_clears_the_keys(self):
        self.provider.epays_mode = 'production'
        self.provider.flush_recordset()
        for query in neutralize.get_neutralization_queries(['payment_epays']):
            self.env.cr.execute(query)
        self.provider.invalidate_recordset()
        self.assertFalse(self.provider.epays_master_key)
        self.assertFalse(self.provider.epays_sandbox_master_key)
        self.assertEqual(self.provider.epays_mode, 'sandbox', "A copy never reaches production.")

    #=== This server's IP address ===#

    def _find_server_ip(self, epays_response, echo_responses=()):
        """ Click "Find this server's IP address" with fake answers from ePays and the echo
        services; return the action and the URLs called. """
        echo_responses = list(echo_responses)
        urls = []

        def fake_request(method, url, **kwargs):
            urls.append(url)
            if url.startswith(tuple(const.IP_ECHO_URLS)):
                self.assertNotIn('X-Api-Master-Key', kwargs.get('headers') or {})
                self.assertIs(kwargs.get('allow_redirects'), False)
                answer = echo_responses.pop(0) if echo_responses else FakeResponse(503)
            else:
                answer = epays_response
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch(REQUESTS_PATH, side_effect=fake_request), mute_logger(PROVIDER_LOGGER):
            action = self.provider.action_epays_find_server_ip()
        return action, urls

    def _x004(self, observed_ip):
        return FakeResponse(401, {'status': 'ERROR', 'error': {
            'code': 'UNAUTHORIZED', 'message': "Unauthorized",
            'details': {'reason': const.AUTH_REASON_INVALID_IP, 'observedIp': observed_ip},
        }})

    def test_find_server_ip_uses_the_address_epays_refused(self):
        """ The address ePays saw is the one to register: no echo service is asked. """
        action, urls = self._find_server_ip(self._x004('203.0.113.7'))
        self.assertEqual(self.provider.epays_server_ip, '203.0.113.7')
        self.assertEqual(action['params']['type'], 'success')
        self.assertIn('ePays sees this server as 203.0.113.7', action['params']['message'])
        self.assertFalse([url for url in urls if url.startswith(tuple(const.IP_ECHO_URLS))])

    def test_find_server_ip_when_the_licence_already_allows_it(self):
        ok = FakeResponse(200, {'status': 'SUCCESS', 'data': {'items': []}})
        echo = FakeResponse(200, '198.51.100.20\n')
        echo.text = '198.51.100.20\n'
        action, _urls = self._find_server_ip(ok, [echo])
        self.assertEqual(self.provider.epays_server_ip, '198.51.100.20')
        self.assertIn('already allows it', action['params']['message'])

    def test_find_server_ip_without_credentials_asks_an_echo_service(self):
        self.provider.write({
            'epays_mode': 'production', 'epays_api_id': False, 'epays_master_key': False,
            'state': 'disabled',
        })
        garbage = FakeResponse(200, None)
        garbage.text = '<html>not an ip</html>'
        second = FakeResponse(200, None)
        second.text = '2001:db8::1'
        action, urls = self._find_server_ip(None, [garbage, second])
        self.assertEqual(urls, list(const.IP_ECHO_URLS), "ePays is not called without keys.")
        self.assertEqual(self.provider.epays_server_ip, '2001:db8::1')
        self.assertIn('Ask ePays to add it', action['params']['message'])

    def test_find_server_ip_when_nothing_answers(self):
        self.provider._epays_set_server_ip('192.0.2.1')
        action, _urls = self._find_server_ip(
            FakeResponse(502, None),
            [requests.exceptions.ConnectionError("offline"), FakeResponse(503, None)],
        )
        self.assertEqual(action['params']['type'], 'warning')
        self.assertEqual(self.provider.epays_server_ip, '192.0.2.1', "The last known IP stays.")

    def test_test_connection_records_the_ip_epays_refused(self):
        with patch(REQUESTS_PATH, return_value=self._x004('203.0.113.9')), \
                mute_logger(PROVIDER_LOGGER):
            self.provider.action_epays_test_connection()
        self.assertEqual(self.provider.epays_server_ip, '203.0.113.9')

    def test_an_invalid_observed_ip_is_never_stored(self):
        with patch(REQUESTS_PATH, return_value=self._x004('<script>')), \
                mute_logger(PROVIDER_LOGGER):
            self.provider.action_epays_test_connection()
        self.assertFalse(self.provider.epays_server_ip)

    def test_only_admins_can_look_up_the_server_ip(self):
        with self.assertRaises(AccessError):
            self.provider.with_user(self.internal_user).action_epays_find_server_ip()

    def test_the_server_ip_is_one_value_for_every_provider(self):
        company_b = self.env['res.company'].create({'name': "Company B"})
        provider_b = self.env['payment.provider'].search(
            [('code', '=', 'epays'), ('company_id', '=', company_b.id)]
        )
        self.provider._epays_set_server_ip('203.0.113.50')
        self.assertEqual(provider_b.epays_server_ip, '203.0.113.50', "It is the server's address.")

    def test_the_addon_adds_no_new_column_to_payment_provider(self):
        """ A new stored field on payment.provider breaks Apps > Upgrade on every database that
        does not have its column yet: marking the module to upgrade makes Odoo recompute the
        providers' colour, which reads every column, before the upgrade can add it. Keep new
        provider settings out of payment.provider (a system parameter, or a model of the addon),
        or accept that such an update must be run from the command line (`-u payment_epays`). """
        stored_epays_fields = sorted(
            name for name, field in self.env['payment.provider']._fields.items()
            if name.startswith('epays_') and field.store and field.column_type
        )
        self.assertEqual(stored_epays_fields, [
            'epays_api_id', 'epays_link_expiry', 'epays_master_key', 'epays_merchant_domain',
            'epays_mode', 'epays_payment_lang', 'epays_sandbox_api_id',
            'epays_sandbox_master_key', 'epays_send_customer_details',
        ])

    #=== Secrets ===#

    def test_the_master_keys_never_reach_the_browser_or_rpc(self):
        """ The web client and RPC get a mask; the addon itself still reads the real key. """
        admin_provider = self.provider.with_user(self.env.ref('base.user_admin'))
        web_values = admin_provider.web_read({
            'epays_master_key': {}, 'epays_sandbox_master_key': {}, 'epays_api_id': {},
        })[0]
        self.assertEqual(web_values['epays_master_key'], '********')
        self.assertEqual(web_values['epays_sandbox_master_key'], '********')
        self.assertEqual(web_values['epays_api_id'], 'prod-api-id', "The API ID is not a secret.")
        self.assertEqual(
            admin_provider.read(['epays_master_key'])[0]['epays_master_key'], '********'
        )
        self.assertEqual(
            admin_provider.search_read([('id', '=', self.provider.id)], ['epays_master_key'])[0]
            ['epays_master_key'], '********',
        )
        self.assertEqual(self.provider.epays_master_key, 'prod-master-key')

        self.provider.epays_sandbox_master_key = False
        self.assertFalse(admin_provider.read(['epays_sandbox_master_key'])[0][
            'epays_sandbox_master_key'
        ], "An empty key reads as empty, so the form shows it must be set.")

    def test_saving_the_form_back_keeps_the_key_and_a_new_key_replaces_it(self):
        self.provider.write({'epays_master_key': '********'})  # A client saving what it read.
        self.assertEqual(self.provider.epays_master_key, 'prod-master-key')
        self.provider.write({'epays_master_key': '  new-master-key\n'})  # Pasted.
        self.assertEqual(self.provider.epays_master_key, 'new-master-key')

    def test_a_malformed_key_is_refused_without_repeating_it(self):
        with self.assertRaises(ValidationError) as error:
            self.provider.write({'epays_master_key': 'half-of-the\nsecret-key'})
        self.assertIn('API Master Key', error.exception.args[0])
        self.assertNotIn('secret-key', error.exception.args[0])

    def test_the_master_keys_cannot_be_exported_searched_or_grouped(self):
        fields_info = self.env['payment.provider'].fields_get(
            ['epays_master_key', 'epays_sandbox_master_key'], ['exportable']
        )
        self.assertFalse(fields_info['epays_master_key']['exportable'])
        self.assertFalse(fields_info['epays_sandbox_master_key']['exportable'])

        Provider = self.env['payment.provider']
        for attempt in (
            lambda: Provider.search([('epays_master_key', '=like', 'prod%')]),
            lambda: Provider.search([], order='epays_master_key'),
            lambda: Provider._read_group([], ['epays_master_key']),
            lambda: Provider._read_group([], [], ['epays_sandbox_master_key:array_agg']),
        ):
            with self.subTest(attempt=attempt), self.assertRaises(AccessError):
                attempt()
        self.assertIn(self.provider, Provider.search([('code', '=', 'epays')], order='name'))

    def test_non_admins_cannot_read_the_credentials(self):
        for field_name in ('epays_api_id', 'epays_master_key', 'epays_sandbox_master_key'):
            with self.subTest(field=field_name), self.assertRaises(AccessError):
                self.provider.with_user(self.internal_user).read([field_name])

    def test_a_key_never_appears_in_the_logs_or_the_error(self):
        """ `requests` repeats an invalid header value in its error. A key saved by an older
        version with a line break must not reach the log, the admin notification or a traceback
        chained to the error. """
        secret = 'LEAKY-SECRET-KEY'
        self.env.cr.execute(  # As an older version could have stored it, bypassing the check.
            "UPDATE payment_provider SET epays_sandbox_master_key = %s WHERE id = %s",
            (f'{secret}\nX', self.provider.id),
        )
        self.provider.invalidate_recordset()
        send_path = 'requests.adapters.HTTPAdapter.send'
        with patch(send_path, side_effect=AssertionError("nothing is sent")), \
                self.assertLogs(PROVIDER_LOGGER, level='DEBUG') as logs, \
                self.assertRaises(EpaysApiError) as error:
            self.provider._epays_make_request(
                'GET', '/api/v2/Payments/P1', environment='sandbox', domain='shop.example.com'
            )
        exception = error.exception
        for text in (*logs.output, exception.args[0], exception.connection_error):
            self.assertNotIn(secret, text)
        self.assertIsNone(exception.__cause__)
        self.assertTrue(exception.__suppress_context__, "No chained traceback to log.")

        with patch(send_path, side_effect=AssertionError("nothing is sent")), \
                mute_logger(PROVIDER_LOGGER):
            action = self.provider.action_epays_test_connection()
        self.assertNotIn(secret, action['params']['message'])

    def test_a_successful_request_logs_no_credential(self):
        response = FakeResponse(200, {'status': 'SUCCESS', 'data': {'paymentId': 'P1'}})
        with patch(REQUESTS_PATH, return_value=response), \
                self.assertLogs(PROVIDER_LOGGER, level='DEBUG') as logs:
            self.provider._epays_make_request(
                'POST', '/api/v2/Payments', environment='sandbox', domain='shop.example.com',
                payload={'amount': '1.000'},
            )
        for text in logs.output:
            self.assertNotIn('sandbox-master-key', text)
            self.assertNotIn('prod-master-key', text)

    #=== Security ===#

    @mute_logger(PROVIDER_LOGGER)
    def test_redirects_are_refused_so_the_master_key_never_leaves_epays(self):
        """ `requests` resends custom headers such as X-Api-Master-Key to the host of a redirect;
        only the standard Authorization header is stripped. """
        redirect = FakeResponse(302, None, headers={'Location': 'https://attacker.example/steal'})
        with patch(REQUESTS_PATH, return_value=redirect) as request_mock, \
                self.assertRaises(EpaysApiError) as error:
            self.provider._epays_make_request(
                'GET', '/api/v2/Payments/P1', environment='sandbox', domain='shop.example.com'
            )
        self.assertIs(request_mock.call_args.kwargs['allow_redirects'], False)
        self.assertEqual(request_mock.call_count, 1, "The redirect is not followed.")
        self.assertEqual(error.exception.http_status, 302)
        self.assertEqual(
            error.exception.args[0], "ePays: Could not establish the connection to the API."
        )

    def test_customer_data_is_logged_at_debug_level_only(self):
        tx = self._create_transaction('redirect', reference='PII-1')
        response = FakeResponse(200, {'status': 'SUCCESS', 'data': {
            'paymentId': self.payment_id, 'redirect': self.redirect_url,
        }})
        with patch(REQUESTS_PATH, return_value=response), \
                self.assertLogs(PROVIDER_LOGGER, level='DEBUG') as logs, \
                mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            tx._get_processing_values()
        records_with_pii = [r for r in logs.records if self.partner.email in r.getMessage()]
        self.assertTrue(records_with_pii, "The payload is still available when debugging.")
        self.assertEqual({r.levelname for r in records_with_pii}, {'DEBUG'})

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_a_refund_never_completes_an_order(self):
        tx = self._create_epays_transaction()
        with self._patch_payment_details(self._payment_data(tx, status='Refunded')):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(tx.state, 'error')
        self.assertEqual(tx.state_message, (
            "Your payment was refunded, so your order was not confirmed. Please contact us, "
            f"quoting the reference {tx.reference}."
        ))

        # A later refund of an order Odoo completed is left to the accounting.
        tx_done = self._create_epays_transaction(state='done')
        tx_done._epays_apply_payment_details(self._payment_data(tx_done, status='Refunded'))
        self.assertEqual(tx_done.state, 'done')

    def test_a_finished_transaction_does_not_call_epays_again(self):
        for state in ('done', 'cancel'):
            with self.subTest(state=state):
                tx = self._create_epays_transaction(state=state)
                with self._patch_make_request(lambda *args, **kwargs: {}) as make_request_mock, \
                        mute_logger(TX_LOGGER):
                    tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
                make_request_mock.assert_not_called()
                self.assertEqual(tx.state, state)

    def test_the_epays_buttons_require_write_access_on_the_provider(self):
        """ Button methods can be called over RPC by anyone who can read the provider, e.g. POS
        managers with `pos_online_payment`. """
        reader_group = self.env['res.groups'].create({'name': "ePays provider readers"})
        self.env['ir.model.access'].create({
            'name': "payment.provider read-only",
            'model_id': self.env['ir.model']._get_id('payment.provider'),
            'group_id': reader_group.id,
            'perm_read': True,
        })
        reader = self.env['res.users'].create({
            'name': "Provider Reader", 'login': 'provider_reader',
            'groups_id': [Command.set([self.env.ref('base.group_user').id, reader_group.id])],
        })
        provider_as_reader = self.provider.with_user(reader)
        provider_as_reader.check_access('read')  # The reader can read the provider...
        for action in ('action_epays_test_connection', 'action_epays_sync_gateways'):
            with self.subTest(action=action), \
                    self._patch_make_request(lambda *a, **k: {}) as call, \
                    self.assertRaises(AccessError):
                getattr(provider_as_reader, action)()
            call.assert_not_called()  # ...but the master key is never used on their behalf.

    def test_gateways_are_for_admins_and_follow_the_provider_company(self):
        gateways = self.provider.epays_gateway_ids
        self.assertTrue(gateways)
        self.assertEqual(gateways.company_id, self.provider.company_id)
        with self.assertRaises(AccessError):
            gateways.with_user(self.internal_user).read(['gateway_ref'])

        company_b = self.env['res.company'].create({'name': "Company B"})
        admin_b = self.env['res.users'].create({
            'name': "Admin B", 'login': 'admin_b',
            'company_id': company_b.id, 'company_ids': [Command.set(company_b.ids)],
            'groups_id': [Command.set([self.env.ref('base.group_system').id])],
        })
        self.assertFalse(
            self.env['payment.epays.gateway'].with_user(admin_b).search([]),
            "Another company's admin does not see this company's gateways.",
        )

    def test_localhost_mode_cannot_be_published(self):
        self.provider.write({'state': 'test', 'epays_mode': 'localhost', 'is_published': False})
        with self.assertRaises(ValidationError):
            self.provider.is_published = True
        self.provider.write({'epays_mode': 'sandbox', 'is_published': True})
        with self.assertRaises(ValidationError):
            self.provider.epays_mode = 'localhost'

    def test_pay_again_locks_the_earlier_transaction_before_taking_over_its_payment(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, state='pending')
        pending = self._payment_data(first, status='Pending', result='')
        lock_path = (
            'odoo.addons.payment_epays.models.payment_transaction.PaymentTransaction'
            '._epays_lock_for_update'
        )
        with patch(lock_path, autospec=True) as lock_mock:
            tx, _calls, _values = self._pay_again([pending], **self._order_values(order))
        lock_mock.assert_called_once_with(first)
        self.assertEqual(tx.provider_reference, first.provider_reference)

    def test_the_lock_is_a_real_row_lock(self):
        tx = self._create_epays_transaction()
        with patch.object(self.env.cr, 'execute', wraps=self.env.cr.execute) as execute_mock:
            tx._epays_lock_for_update()
        self.assertIn('FOR UPDATE', str(execute_mock.call_args.args[0]))

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_the_return_url_names_the_environment_of_the_payment(self):
        """ Sandbox and production number their payments independently, so one payment id can
        exist in both. """
        tx = self._create_transaction('redirect', reference='ENV-1')
        calls, _values = self._initiate(tx)
        self.assertTrue(
            calls[0]['payload']['notifyUrl'].endswith(f'{EpaysController._return_url}/sandbox')
        )

        tx_sandbox = self._create_epays_transaction(provider_reference='PAY-SHARED')
        tx_production = self._create_epays_transaction(
            provider_reference='PAY-SHARED', epays_environment='production'
        )
        lookup = self.env['payment.transaction']._get_tx_from_notification_data
        key = const.NOTIFICATION_ENVIRONMENT_KEY
        self.assertEqual(lookup('epays', {'paymentId': 'PAY-SHARED', key: 'sandbox'}), tx_sandbox)
        self.assertEqual(
            lookup('epays', {'paymentId': 'PAY-SHARED', key: 'production'}), tx_production
        )
        # A payment started before the environment was in the URL: the newest one, as before.
        self.assertEqual(lookup('epays', {'paymentId': 'PAY-SHARED'}), tx_production)

    def test_the_return_route_with_the_environment_completes_the_transaction(self):
        tx = self._create_epays_transaction()
        url = self._build_url(f'{EpaysController._return_url}/sandbox')
        with self._patch_payment_details(self._payment_data(tx)), \
                mute_logger(CONTROLLER_LOGGER, TX_LOGGER, PAYMENT_TX_LOGGER):
            response = self.opener.get(
                url, params={'paymentId': tx.provider_reference}, allow_redirects=False
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(tx.state, 'done')

    #=== Mode ===#

    def test_each_mode_has_its_predefined_url(self):
        for mode, url in [
            ('production', 'https://api.epays.io'),
            ('sandbox', 'https://testapi.epays.io'),
            ('localhost', 'https://localhost:7124'),
        ]:
            with self.subTest(mode=mode):
                self.provider.epays_mode = mode
                self.assertEqual(self.provider.epays_api_url, url)
                self.assertEqual(self.provider._epays_get_environment(), mode)
                self.assertEqual(self.provider._epays_get_api_url(mode), url)

    @mute_logger(PROVIDER_LOGGER)
    def test_localhost_mode_uses_the_sandbox_credentials(self):
        response = FakeResponse(200, {'status': 'SUCCESS', 'data': {}, 'meta': {}})
        with patch(REQUESTS_PATH, return_value=response) as request_mock:
            self.provider._epays_make_request(
                'GET', '/api/v2/Payments/P1', environment='localhost', domain='shop.example.com'
            )
        args, kwargs = request_mock.call_args
        self.assertEqual(args, ('GET', 'https://localhost:7124/api/v2/Payments/P1'))
        self.assertEqual(kwargs['headers']['X-Api-Id'], 'sandbox-api-id')

        # Production never falls back on the sandbox pair.
        with patch(REQUESTS_PATH, return_value=response) as request_mock:
            self.provider._epays_make_request(
                'GET', '/api/v2/Payments/P1', environment='production', domain='shop.example.com'
            )
        self.assertEqual(request_mock.call_args.kwargs['headers']['X-Api-Id'], 'prod-api-id')

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_localhost_mode_sends_test_payments(self):
        self.provider.epays_mode = 'localhost'
        tx = self._create_transaction('redirect', reference='LOCAL-1')
        calls, _values = self._initiate(tx, response={
            'paymentId': self.payment_id, 'redirect': 'https://localhost:7124/Pay/1/PAY-0001',
        })
        self.assertEqual(calls[0]['environment'], 'localhost')
        self.assertIs(calls[0]['payload']['testMode'], True)
        self.assertEqual(tx.epays_environment, 'localhost')
        self.assertEqual(tx.provider_reference, self.payment_id)

    def test_enabled_provider_requires_production_mode(self):
        for mode in ('sandbox', 'localhost'):
            with self.subTest(mode=mode), self.assertRaises(ValidationError):
                self.provider.write({'state': 'enabled', 'epays_mode': mode})
        self.provider.write({'state': 'enabled', 'epays_mode': 'production'})
        with self.assertRaises(ValidationError):
            self.provider.epays_mode = 'sandbox'
        # Test state may use any mode, production included.
        self.provider.write({'state': 'test', 'epays_mode': 'production'})

    def test_state_change_moves_the_mode(self):
        for state, mode, expected_mode in [
            ('enabled', 'sandbox', 'production'),
            ('enabled', 'localhost', 'production'),
            ('test', 'production', 'sandbox'),
            ('test', 'localhost', 'localhost'),
            ('disabled', 'localhost', 'localhost'),
        ]:
            with self.subTest(state=state, mode=mode):
                provider = self.env['payment.provider'].new(
                    {'code': 'epays', 'state': state, 'epays_mode': mode}
                )
                provider._onchange_state_epays_mode()
                self.assertEqual(provider.epays_mode, expected_mode)

    #=== Test connection ===#

    @mute_logger(PROVIDER_LOGGER)
    def test_test_connection_reports_success(self):
        ok = FakeResponse(200, {'status': 'SUCCESS', 'data': {'items': []}})
        with patch(REQUESTS_PATH, return_value=ok) as request_mock:
            action = self.provider.action_epays_test_connection()
        self.assertEqual(action['params']['type'], 'success')
        urls = [call.args[1] for call in request_mock.call_args_list]
        self.assertEqual(urls, ['https://testapi.epays.io/api/v2/Payments?limit=1'],
                         "Only the server of the provider's mode is tested.")
        self.assertIn('Sandbox (https://testapi.epays.io)', action['params']['message'])

    @mute_logger(PROVIDER_LOGGER)
    def test_test_connection_diagnoses_authentication_failures(self):
        def unauthorized(details=None):
            error = {'code': 'UNAUTHORIZED', 'message': "Unauthorized"}
            if details:
                error['details'] = details
            return FakeResponse(401, {'status': 'ERROR', 'error': error, 'meta': {}})

        rows = [
            (unauthorized({'reason': const.AUTH_REASON_INVALID_IP, 'observedIp': '203.0.113.7'}),
             '203.0.113.7'),
            (unauthorized({'reason': const.AUTH_REASON_INVALID_DOMAIN}), 'shop.example.com'),
            (unauthorized({'reason': const.AUTH_REASON_MISSING_LICENSE}), 'no licence'),
            (unauthorized({'reason': const.AUTH_REASON_TOO_MANY_ATTEMPTS}), 'too many'),
            (unauthorized(), 'API master key is wrong'),
            (FakeResponse(502, None), 'HTTP 502'),
        ]
        for response, expected_text in rows:
            with self.subTest(expected=expected_text), \
                    patch(REQUESTS_PATH, return_value=response):
                action = self.provider.action_epays_test_connection()
                self.assertEqual(action['params']['type'], 'warning')
                self.assertIn(expected_text, action['params']['message'])

    @mute_logger(PROVIDER_LOGGER)
    def test_test_connection_reports_an_unreachable_api(self):
        with patch(REQUESTS_PATH, side_effect=requests.exceptions.ConnectionError("refused")):
            action = self.provider.action_epays_test_connection()
        self.assertEqual(action['params']['type'], 'warning')
        self.assertIn('could not be reached', action['params']['message'])

    #=== Payment methods ===#

    def test_payment_method_records(self):
        rows = [
            # (method, primary, countries, currencies: none, every payment is charged in BHD)
            (self.epays_method, None, [], []),
            (self.card_method, None, [], []),
            (self.benefit_method, None, ['BH'], []),
            (self.benefit_pay_method, None, ['BH'], []),
            (self.apple_pay_method, None, [], []),
            (self.google_pay_method, None, [], []),
            (self.samsung_pay_method, None, [], []),
            (self.tabby_method, None, ['AE', 'BH', 'KW', 'SA'], []),
            (self.card_visa_method, self.card_method, [], []),
        ]
        for method, primary, countries, currencies in rows:
            with self.subTest(code=method.code):
                self.assertEqual(
                    method.primary_payment_method_id, primary or self.env['payment.method']
                )
                self.assertEqual(sorted(method.supported_country_ids.mapped('code')), countries)
                self.assertEqual(sorted(method.supported_currency_ids.mapped('name')), currencies)
                self.assertTrue(method.image)
                self.assertTrue(method.image_payment_form)
                self.assertFalse(method.support_tokenization)
        brands = self.epays_method.with_context(active_test=False).brand_ids
        self.assertEqual(set(brands.mapped('code')), const.PRIMARY_PAYMENT_METHOD_BRAND_CODES)
        self.assertTrue(all(brand.image for brand in brands))
        self.assertEqual(
            set(self.card_method.with_context(active_test=False).brand_ids.mapped('code')),
            {'epays_card_visa', 'epays_card_mastercard'},
        )
        linked_methods = self.provider.with_context(active_test=False).payment_method_ids
        self.assertEqual(
            set(linked_methods.mapped('code')),
            {'epays', *const.INDIVIDUAL_PAYMENT_METHOD_CODES},
            "Only the addon's own primary methods are linked; Odoo's core methods are untouched.",
        )

    def _addon_methods(self):
        return self.env['payment.method'].with_context(active_test=False).search(
            [('code', '=like', 'epays%')]
        )

    def test_enabling_the_provider_activates_only_the_epays_option(self):
        self.provider.state = 'disabled'
        self.assertFalse(self._addon_methods().filtered('active'))

        self.provider.write({'state': 'enabled', 'epays_mode': 'production'})
        self.assertEqual(
            set(self._addon_methods().filtered('active').mapped('code')),
            {'epays', *const.PRIMARY_PAYMENT_METHOD_BRAND_CODES},
        )

        # The admin activates an individual method: its brands follow.
        self.card_method.active = True
        self.assertTrue(self.card_visa_method.active)
        self.assertTrue(self.env.ref('payment_epays.payment_method_epays_card_mastercard').active)

    def test_setup_links_the_methods_to_existing_providers(self):
        core_card = self.env.ref('payment.payment_method_card')
        self.provider.payment_method_ids = [Command.set(core_card.ids)]
        self.env['payment.provider']._epays_setup_payment_methods()
        linked_methods = self.provider.with_context(active_test=False).payment_method_ids
        self.assertNotIn(core_card, linked_methods)
        self.assertIn(self.epays_method, linked_methods)
        self.assertTrue(self.epays_method.active)

    def _compatible_methods(self, currency, country):
        self.partner.country_id = country
        return self.env['payment.method']._get_compatible_payment_methods(
            [self.provider.id], self.partner.id, currency_id=currency.id
        )

    def test_benefit_and_tabby_follow_their_countries(self):
        restricted = self.benefit_method + self.benefit_pay_method + self.tabby_method
        (restricted + self.apple_pay_method).write({'active': True})
        bahrain = self.env.ref('base.bh')

        methods = self._compatible_methods(self.currency, bahrain)  # BHD.
        self.assertLessEqual(restricted + self.epays_method + self.apple_pay_method, methods)

        # Any currency: ePays charges them in BHD.
        methods = self._compatible_methods(self.currency_usd, bahrain)
        self.assertLessEqual(restricted + self.epays_method + self.apple_pay_method, methods)

        methods = self._compatible_methods(self.currency, self.env.ref('base.be'))
        self.assertFalse(restricted & methods)

    def test_epays_methods_follow_the_billing_address_of_the_order(self):
        """ Logged in, Odoo checks the countries of a method against the user's own contact; the
        ePays methods follow the billing address selected at checkout instead. """
        self._skip_without_sale()
        bahrain_only = self.benefit_method + self.benefit_pay_method
        bahrain_only.write({'active': True})
        # Another provider's Bahrain-only method keeps Odoo's rule.
        other_method = self.env['payment.method'].create({
            'name': "Other Bahrain Method", 'code': 'other_bh_only',
            'provider_ids': [Command.link(self.dummy_provider.id)],
            'supported_country_ids': [Command.set(self.env.ref('base.bh').ids)],
        })
        user_contact = self.env['res.partner'].create({
            'name': "Shop Admin", 'country_id': self.env.ref('base.us').id,
        })
        billing_address = self.env['res.partner'].create({
            'name': "Billing in Manama", 'country_id': self.env.ref('base.bh').id,
        })
        order = self._create_sale_order()
        order.partner_invoice_id = billing_address
        provider_ids = [self.provider.id, self.dummy_provider.id]

        def compatible(partner, **kwargs):
            report = {}
            methods = self.env['payment.method']._get_compatible_payment_methods(
                provider_ids, partner.id, currency_id=self.currency.id, report=report, **kwargs
            )
            return methods, report

        methods, report = compatible(user_contact, sale_order_id=order.id)
        self.assertLessEqual(bahrain_only, methods, "The billing address is in Bahrain.")
        self.assertIn(self.epays_method, methods)
        self.assertNotIn(other_method, methods, "Other providers keep Odoo's rule.")
        self.assertTrue(report['payment_methods'][self.benefit_method]['available'])
        self.assertFalse(report['payment_methods'][other_method]['available'])
        self.assertEqual(list(methods), list(methods.sorted(lambda pm: (pm.sequence, pm.name))))

        # A billing address outside Bahrain hides them, whatever the user's contact.
        order.partner_invoice_id = self.env['res.partner'].create({
            'name': "Billing in Brussels", 'country_id': self.env.ref('base.be').id,
        })
        user_contact.country_id = self.env.ref('base.bh')
        methods, report = compatible(user_contact, sale_order_id=order.id)
        self.assertFalse(bahrain_only & methods)
        self.assertFalse(report['payment_methods'][self.benefit_method]['available'])
        self.assertEqual(  # The reasons are lazy translations.
            str(report['payment_methods'][self.benefit_method]['reason']),
            str(REPORT_REASONS_MAPPING['incompatible_country']),
        )

        # Without an order (e.g. an invoice or a payment link), Odoo's rule applies as before.
        methods, _report = compatible(self.env['res.partner'].create({
            'name': "No order", 'country_id': self.env.ref('base.us').id,
        }))
        self.assertFalse(bahrain_only & methods)

    def test_checkout_shows_every_logo_of_the_epays_option(self):
        self.provider.write({'state': 'enabled', 'epays_mode': 'production', 'is_published': True})
        response = self._portal_pay(**self._prepare_pay_values())
        self.assertEqual(response.status_code, 200)
        html_tree = objectify.fromstring(response.text, parser=etree.HTMLParser())
        radios = html_tree.xpath(
            '//input[@name="o_payment_radio"][@data-payment-method-code="epays"]'
        )
        self.assertEqual(len(radios), 1)
        option = radios[0].xpath('ancestor::li[@name="o_payment_option"]')[0]
        self.assertEqual(
            len(option.xpath('.//img')), len(const.PRIMARY_PAYMENT_METHOD_BRAND_CODES),
            "All the brand logos are shown, not only the first four.",
        )

    def test_apple_pay_filter_is_in_the_frontend_assets(self):
        paths = self.env['ir.asset']._get_asset_paths('web.assets_frontend', {})
        self.assertTrue(any(
            'payment_epays/static/src/js/payment_form.js' in str(item) for item in paths
        ))

    #=== Gateway sync ===#

    def _sync(self, gateways=None, error=None):
        calls = []

        def handler(provider, method, path, **kwargs):
            calls.append(dict(kwargs, method=method, path=path))
            if error:
                raise error
            return {'gateways': gateways or []}

        with self._patch_make_request(handler):
            action = self.provider.action_epays_sync_gateways()
        return calls, action

    def _gateways(self, environment):
        return self.env['payment.epays.gateway'].search([
            ('provider_id', '=', self.provider.id), ('environment', '=', environment),
        ])

    def test_sync_gateways_upserts_maps_and_drops(self):
        card_row = self._gateways('sandbox').filtered(
            lambda g: g.gateway_ref == self.card_gateway_ref
        )
        card_row.payment_method_id = self.benefit_method  # Corrected by the admin.
        production_rows = self._gateways('production')

        calls, action = self._sync([
            {'merchantGateway': self.card_gateway_ref, 'apiId': 'A', 'gatewayId': '2',
             'gatewayName': "Credit Card", 'gatewayClass': 'MPGS', 'status': 'Active',
             'parentGatewayId': '67940689'},
            {'merchantGateway': 11112222, 'gatewayId': 6, 'gatewayName': "BenefitPay",
             'gatewayClass': 'BenefitPay', 'status': 'Active'},
            {'merchantGateway': '33334444', 'gatewayId': '5', 'gatewayName': "Tabby",
             'status': 'Inactive'},
            {'merchantGateway': '55556666', 'gatewayId': '42', 'gatewayName': "Something new",
             'status': 'Active'},
            {'merchantGateway': '77778888', 'gatewayId': '7', 'gatewayName': "Zoho"},
            {'merchantGateway': '88889999', 'gatewayId': 8, 'gatewayName': "Odoo"},
            {'merchantGateway': '', 'gatewayId': '2'},
            'not a gateway',
        ])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['method'], 'GET')
        self.assertEqual(calls[0]['path'], '/api/v2/gateways')
        self.assertEqual(calls[0]['environment'], 'sandbox')
        self.assertEqual(calls[0]['domain'], 'shop.example.com')

        rows = {row.gateway_ref: row for row in self._gateways('sandbox')}
        self.assertEqual(
            set(rows), {self.card_gateway_ref, '11112222', '33334444', '55556666'},
            "Integrations are skipped and gateways no longer returned are dropped.",
        )
        self.assertEqual(rows[self.card_gateway_ref], card_row, "An existing row is updated.")
        self.assertEqual(card_row.name, "Credit Card")
        self.assertEqual(
            card_row.payment_method_id, self.benefit_method, "The admin's mapping is kept."
        )
        self.assertEqual(rows['11112222'].payment_method_id, self.benefit_pay_method)
        self.assertEqual(rows['11112222'].gateway_type_id, 6)
        self.assertEqual(rows['33334444'].payment_method_id, self.tabby_method)
        self.assertFalse(rows['33334444'].is_active)
        self.assertFalse(rows['55556666'].payment_method_id, "Unknown types have no method.")
        self.assertEqual(
            self._gateways('production'), production_rows, "The other environment is untouched."
        )
        self.assertEqual(action['params']['type'], 'success')
        self.assertIn('4 gateways found', action['params']['message'])
        self.assertIn('3 linked', action['params']['message'])

        # The Tabby gateway is inactive: a Tabby payment lets the customer choose on ePays.
        with mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            self.assertIsNone(self._initiated_gateway_id(self.tabby_method))

    def test_sync_gateways_uses_the_environment_of_the_provider_mode(self):
        self.provider.epays_mode = 'production'
        sandbox_rows = self._gateways('sandbox')
        calls, _action = self._sync([
            {'merchantGateway': '90000009', 'gatewayId': '9', 'gatewayName': "Samsung Pay",
             'status': 'Active'},
        ])
        self.assertEqual(calls[0]['environment'], 'production')
        production_rows = self._gateways('production')
        self.assertEqual(production_rows.mapped('gateway_ref'), ['90000009'])
        self.assertEqual(production_rows.payment_method_id, self.samsung_pay_method)
        self.assertEqual(self._gateways('sandbox'), sandbox_rows)

    @mute_logger(PROVIDER_LOGGER)
    def test_sync_gateways_reports_the_diagnosis_and_keeps_the_rows(self):
        rows = self._gateways('sandbox')
        error = EpaysApiError(
            "ePays: Could not establish the connection to the API.", http_status=401,
            error_code='UNAUTHORIZED',
            details={'reason': const.AUTH_REASON_INVALID_IP, 'observedIp': '203.0.113.7'},
        )
        _calls, action = self._sync(error=error)
        self.assertEqual(action['params']['type'], 'warning')
        self.assertIn('203.0.113.7', action['params']['message'])
        self.assertEqual(self._gateways('sandbox'), rows)

    #=== A payment reused by ePays ===#

    def test_second_attempt_on_the_same_order_takes_over_the_reused_payment(self):
        """ ePays returns the payment of an earlier attempt when udf1 to udf5 match, the payment is
        under ten minutes old, has no result and its page was never opened. With only the order in
        udf2, a second attempt on the same order gets the first attempt's payment id back. """
        if not self._sale_installed():
            self.skipTest("sale is not installed")
        order = self._create_sale_order()
        attempt_values = {
            'reference': False,
            'sale_order_ids': [Command.set(order.ids)],
            'amount': order.amount_total,
            'currency_id': order.currency_id.id,
        }
        tx_first = self._create_transaction('redirect', **attempt_values)
        tx_second = self._create_transaction('redirect', **attempt_values)
        with mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            calls_first, _values = self._initiate(tx_first)
            # An older ePays server, and a first attempt that cannot be reused by the addon itself
            # (e.g. created before the payment page was stored): ePays hands its payment back.
            tx_first.epays_payment_url = False
            calls_second, _values = self._initiate(tx_second)  # Same paymentId returned.

        expected_custom_fields = {'udf2': order.name, 'udf3': ''}
        self.assertEqual(calls_first[0]['payload']['customFields'], expected_custom_fields)
        self.assertEqual(calls_second[0]['payload']['customFields'], expected_custom_fields)
        self.assertEqual(tx_first.state, 'cancel', "The unopened first attempt is replaced.")
        self.assertEqual(
            tx_first.state_message, f"ePays: Replaced by transaction {tx_second.reference}."
        )
        self.assertEqual(tx_second.state, 'draft')
        self.assertEqual(tx_second.provider_reference, self.payment_id)
        self.assertEqual(
            self.env['payment.transaction']._get_tx_from_notification_data(
                'epays', {'paymentId': self.payment_id}
            ),
            tx_second,
        )

        url = self._build_url(EpaysController._return_url)
        with self._patch_payment_details(self._payment_data(tx_second)), \
                mute_logger(CONTROLLER_LOGGER, TX_LOGGER, PAYMENT_TX_LOGGER):
            response = self.opener.get(
                url, params={'paymentId': self.payment_id}, allow_redirects=False
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(tx_second.state, 'done')
        self.assertEqual(tx_first.state, 'cancel')
        with mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            tx_second._post_process()
        self.assertEqual(order.state, 'sale')

    def test_a_started_transaction_sharing_the_payment_is_left_alone(self):
        tx_started = self._create_epays_transaction(
            state='pending', provider_reference=self.payment_id
        )
        tx_new = self._create_transaction('redirect', reference='NEW-1')
        with self.assertLogs(TX_LOGGER, level='WARNING') as logs, \
                mute_logger(PAYMENT_TX_LOGGER):
            self._initiate(tx_new)
        self.assertTrue(any(tx_started.reference in message for message in logs.output))
        self.assertEqual(tx_started.state, 'pending')
        self.assertEqual(tx_new.provider_reference, self.payment_id)

    def test_notification_lookup_prefers_the_newest_open_transaction(self):
        tx_open = self._create_epays_transaction(provider_reference=self.payment_id)
        tx_cancelled_newest = self._create_epays_transaction(
            provider_reference=self.payment_id, state='cancel'
        )
        self.assertGreater(tx_cancelled_newest.id, tx_open.id)
        lookup = self.env['payment.transaction']._get_tx_from_notification_data
        self.assertEqual(lookup('epays', {'paymentId': self.payment_id}), tx_open)

        tx_open.state = 'cancel'
        self.assertEqual(
            lookup('epays', {'paymentId': self.payment_id}), tx_cancelled_newest,
            "Without an open transaction, the newest one is returned.",
        )

    #=== Paying the same order again ===#

    def _order_values(self, order, **values):
        return {
            'sale_order_ids': [Command.set(order.ids)],
            'amount': order.amount_total,
            'currency_id': order.currency_id.id,
            **values,
        }

    def _earlier_attempt(self, order=None, **values):
        """ An earlier ePays transaction, as left by a first click on Pay. """
        order_values = self._order_values(order) if order else {}
        reference = self._new_reference('FIRST')
        return self._create_epays_transaction(**{
            'reference': reference,
            'provider_reference': f'PAY-{reference}',
            'epays_payment_url': f'https://testapi.epays.io/pay/PAY-{reference}?token=t1',
            **order_values,
            **values,
        })

    def _pay_again(self, get_responses=(), delete_response=None, **tx_values):
        """ Click on Pay again: create a new transaction and render its redirect form.

        :param list get_responses: The successive answers to GET, as dicts or exceptions.
        :param delete_response: The answer to DELETE, as a dict or an exception.
        :return: The new transaction, the calls made to ePays and the processing values.
        """
        tx = self._create_transaction('redirect', reference=False, **tx_values)
        calls = []
        get_responses = list(get_responses)

        def handler(provider, method, path, **kwargs):
            calls.append((method, path))
            if method == 'POST':
                return {'paymentId': 'PAY-NEW', 'redirect': self.redirect_url}
            response = get_responses.pop(0) if method == 'GET' else delete_response
            if isinstance(response, Exception):
                raise response
            return response

        with self._patch_make_request(handler), mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            processing_values = tx._get_processing_values()
        return tx, calls, processing_values

    def _skip_without_sale(self):
        if not self._sale_installed():
            self.skipTest("sale is not installed")

    def test_pay_again_reuses_the_pending_payment_and_completes_the_new_transaction(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, state='pending')
        pending = self._payment_data(first, status='Pending', result='')

        tx, calls, processing_values = self._pay_again([pending], **self._order_values(order))

        self.assertEqual(calls, [('GET', f'/api/v2/Payments/{first.provider_reference}')],
                         "No new ePays payment is created.")
        self.assertEqual(tx.provider_reference, first.provider_reference)
        self.assertEqual(tx.epays_payment_url, first.epays_payment_url)
        self.assertEqual(tx.epays_expires_at, first.epays_expires_at)
        self.assertEqual(tx.epays_udf2, order.name)
        self.assertEqual(first.state, 'cancel')
        self.assertEqual(first.state_message, f"ePays: Replaced by transaction {tx.reference}.")
        form_info = self._extract_values_from_html_form(processing_values['redirect_form_html'])
        self.assertEqual(form_info['action'], first.epays_payment_url)
        self.assertEqual(form_info['inputs'], {'token': 't1'})

        # The customer pays; the return route and then the cron complete the new transaction.
        url = self._build_url(EpaysController._return_url)
        with self._patch_payment_details(self._payment_data(tx)), \
                mute_logger(CONTROLLER_LOGGER, TX_LOGGER, PAYMENT_TX_LOGGER):
            response = self.opener.get(
                url, params={'paymentId': tx.provider_reference}, allow_redirects=False
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(tx.state, 'done')
        self.assertEqual(first.state, 'cancel')
        with self._patch_payment_details(self._payment_data(tx)), \
                patch.object(self.env.cr, 'commit'), patch.object(self.env.cr, 'rollback'), \
                mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            self.env['payment.transaction']._cron_epays_poll_pending()
        self.assertEqual(order.state, 'sale')

    def test_pay_again_after_the_cart_changed_voids_the_old_payment(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, amount=order.amount_total - 10)
        pending = self._payment_data(first, status='Pending', result='')
        voided = {'paymentId': first.provider_reference, 'status': 'Voided'}

        tx, calls, _values = self._pay_again(
            [pending], delete_response=voided, **self._order_values(order)
        )

        self.assertEqual([method for method, _path in calls], ['GET', 'DELETE', 'POST'])
        self.assertEqual(calls[1][1], f'/api/v2/Payments/{first.provider_reference}')
        self.assertEqual(first.state, 'cancel')
        self.assertEqual(
            first.state_message, f"ePays: Replaced by transaction {tx.reference} (amount changed)."
        )
        self.assertEqual(tx.provider_reference, 'PAY-NEW')
        self.assertEqual(tx.epays_payment_url, self.redirect_url)

    def test_pay_again_with_another_payment_method_opens_its_gateway(self):
        """ A page opened for one gateway shows that gateway only: after going back and choosing
        another payment method, the customer must get a page for that method. """
        self._skip_without_sale()
        self.apple_pay_method.active = True
        order = self._create_sale_order()
        rows = [
            # (method of the first attempt's gateway, method chosen now, expected new gateway)
            (self.card_gateway_ref, self.apple_pay_method, self.apple_pay_gateway_ref),
            (self.card_gateway_ref, self.epays_method, False),  # ePays: choose on the page.
            (False, self.apple_pay_method, self.apple_pay_gateway_ref),
        ]
        for first_gateway, method, expected_gateway in rows:
            with self.subTest(first=first_gateway, now=method.code):
                first = self._earlier_attempt(
                    order, state='pending', epays_gateway_requested=first_gateway
                )
                pending = self._payment_data(first, status='Pending', result='')
                voided = {'paymentId': first.provider_reference, 'status': 'Voided'}

                tx, calls, _values = self._pay_again(
                    [pending], delete_response=voided,
                    **self._order_values(order, payment_method_id=method.id),
                )

                self.assertEqual([method for method, _path in calls], ['GET', 'DELETE', 'POST'])
                self.assertEqual(first.state, 'cancel')
                self.assertEqual(first.state_message, (
                    f"ePays: Replaced by transaction {tx.reference} (payment method changed)."
                ))
                self.assertEqual(tx.provider_reference, 'PAY-NEW')
                self.assertEqual(tx.epays_gateway_requested, expected_gateway)
                tx.state = 'cancel'  # Keep it out of the next rows.

    def test_pay_again_with_the_same_payment_method_reuses_its_page(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(
            order, state='pending', epays_gateway_requested=self.card_gateway_ref,
            payment_method_id=self.card_method.id,
        )
        pending = self._payment_data(first, status='Pending', result='')

        tx, calls, _values = self._pay_again(
            [pending], **self._order_values(order, payment_method_id=self.card_method.id)
        )

        self.assertEqual([method for method, _path in calls], ['GET'])
        self.assertEqual(tx.provider_reference, first.provider_reference)
        self.assertEqual(tx.epays_gateway_requested, self.card_gateway_ref)

    def test_pay_again_when_the_void_is_refused_and_the_order_was_paid(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, amount=order.amount_total - 10)
        pending = self._payment_data(first, status='Pending', result='')
        refused = EpaysApiError("ePays: refused", http_status=400)

        with self.assertRaises(ValidationError) as error:
            self._pay_again(
                [pending, self._payment_data(first)], delete_response=refused,
                **self._order_values(order),
            )
        self.assertEqual(error.exception.args[0], "ePays: This order has already been paid.")
        self._assert_completed_by_the_cron(first)

    def test_pay_again_when_the_void_is_refused_and_still_pending(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, amount=order.amount_total - 10)
        pending = self._payment_data(first, status='Pending', result='')

        with self.assertRaises(ValidationError) as error:
            self._pay_again(
                [pending, pending],
                delete_response=EpaysApiError("ePays: refused", http_status=400),
                **self._order_values(order),
            )
        self.assertEqual(
            error.exception.args[0], "ePays: Could not establish the connection to the API."
        )
        self.assertEqual(first.state, 'draft')

    def test_pay_again_when_the_earlier_payment_was_paid(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order)
        with self.assertRaises(ValidationError) as error:
            self._pay_again([self._payment_data(first)], **self._order_values(order))
        self.assertEqual(error.exception.args[0], "ePays: This order has already been paid.")
        self._assert_completed_by_the_cron(first)

    def _assert_completed_by_the_cron(self, tx):
        """ The failed request is rolled back, so the paid earlier attempt is completed by the
        scheduled check (or by its return route and the ePays notification). """
        self.assertEqual(tx.state, 'draft')
        self._set_create_date(tx, fields.Datetime.now() - timedelta(minutes=10))
        self._run_cron(lambda *args, **kwargs: self._payment_data(tx))
        self.assertEqual(tx.state, 'done')

    def test_pay_again_after_an_unknown_status_voids_the_old_payment(self):
        """ An unrecognised status may be a raw gateway word for a completed capture: never
        create a second payment unless ePays voided the first one. """
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order)
        unknown = self._payment_data(first, status='Unknown', result='SUCCESS')
        voided = {'paymentId': first.provider_reference, 'status': 'Voided'}

        tx, calls, _values = self._pay_again(
            [unknown], delete_response=voided, **self._order_values(order)
        )

        self.assertEqual([method for method, _path in calls], ['GET', 'DELETE', 'POST'])
        self.assertEqual(first.state, 'cancel')
        self.assertEqual(first.state_message, f"ePays: Replaced by transaction {tx.reference}.")
        self.assertEqual(tx.provider_reference, 'PAY-NEW')

    def test_pay_again_after_an_unknown_status_when_the_void_is_refused(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order)
        unknown = self._payment_data(first, status='Unknown', result='SUCCESS')
        refused = EpaysApiError("ePays: refused", http_status=400)

        with self.assertRaises(ValidationError) as error:
            self._pay_again(
                [unknown, unknown], delete_response=refused, **self._order_values(order)
            )
        self.assertEqual(
            error.exception.args[0], "ePays: Could not establish the connection to the API."
        )
        self.assertEqual(first.state, 'draft')

        with self.assertRaises(ValidationError) as error:
            self._pay_again(
                [unknown, self._payment_data(first)], delete_response=refused,
                **self._order_values(order),
            )
        self.assertEqual(error.exception.args[0], "ePays: This order has already been paid.")
        self._assert_completed_by_the_cron(first)

    def test_pay_again_after_an_unknown_status_never_posts_before_the_void(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order)
        unknown = self._payment_data(first, status=None, result='SUCCESS')
        del unknown['status']  # An older ePays server: only the raw result.
        methods = []

        def handler(provider, method, path, **kwargs):
            methods.append(method)
            if method == 'DELETE':
                raise EpaysApiError("ePays: refused", http_status=400)
            return unknown

        tx = self._create_transaction('redirect', reference=False, **self._order_values(order))
        with self._patch_make_request(handler), mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER), \
                self.assertRaises(ValidationError):
            tx._get_processing_values()
        self.assertEqual(methods, ['GET', 'DELETE', 'GET'], "No POST.")

    def test_pay_again_after_a_failed_payment_creates_a_new_one(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, state='pending')
        failed = self._payment_data(first, status='Failed', result='DECLINED')

        tx, calls, _values = self._pay_again([failed], **self._order_values(order))

        self.assertEqual([method for method, _path in calls], ['GET', 'POST'])
        self.assertEqual(first.state, 'error')
        self.assertEqual(tx.provider_reference, 'PAY-NEW')

    def test_pay_again_does_not_create_a_payment_when_epays_cannot_be_reached(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order)
        unreachable = EpaysApiError(
            "ePays: Could not establish the connection to the API.", connection_error="timeout"
        )
        with self.assertRaises(ValidationError) as error:
            self._pay_again([unreachable], **self._order_values(order))
        self.assertEqual(
            error.exception.args[0], "ePays: Could not establish the connection to the API."
        )
        self.assertEqual(first.state, 'draft')

    def test_pay_again_ignores_attempts_that_cannot_be_reused(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        other_order = self._create_sale_order()
        now = fields.Datetime.now()
        rows = [
            ("link expiring within 5 minutes", order,
             {'epays_expires_at': now + timedelta(minutes=3)}),
            ("created before the payment page was stored", order, {'epays_payment_url': False}),
            ("other environment", order, {'epays_environment': 'production'}),
            ("other order", other_order, {}),
            ("already finished", order, {'state': 'error'}),
        ]
        for label, earlier_order, values in rows:
            with self.subTest(label):
                first = self._earlier_attempt(earlier_order, **values)
                first_state = first.state
                tx, calls, _values = self._pay_again(**self._order_values(order))
                self.assertEqual([method for method, _path in calls], ['POST'])
                self.assertEqual(tx.provider_reference, 'PAY-NEW')
                self.assertEqual(first.state, first_state)
                first.state = 'cancel'  # Keep it out of the next rows.
                tx.state = 'cancel'

    def test_pay_again_never_reuses_a_payment_without_documents(self):
        first = self._earlier_attempt()  # A bare /payment/pay link.
        self.assertFalse(first.epays_udf2 or first.epays_udf3)
        tx, calls, _values = self._pay_again()
        self.assertEqual([method for method, _path in calls], ['POST'])
        self.assertEqual(tx.provider_reference, 'PAY-NEW')
        self.assertEqual(first.state, 'draft')

    def test_pay_again_reuses_the_payment_of_the_same_invoices(self):
        """ Without sales order, the invoices (udf3) identify the documents. """
        invoice_fields = {'udf2': '', 'udf3': 'INV/2026/00012'}
        first = self._earlier_attempt(epays_udf2='', epays_udf3='INV/2026/00012')
        pending = self._payment_data(first, status='Pending', result='')
        with patch(
            'odoo.addons.payment_epays.models.payment_transaction.PaymentTransaction'
            '._epays_get_custom_fields', return_value=invoice_fields,
        ):
            tx, calls, _values = self._pay_again([pending])
        self.assertEqual([method for method, _path in calls], ['GET'])
        self.assertEqual(tx.provider_reference, first.provider_reference)
        self.assertEqual(first.state, 'cancel')
