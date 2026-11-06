# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from freezegun import freeze_time
from lxml import etree, objectify
from psycopg2 import pool
from psycopg2.errors import SerializationFailure

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.modules import neutralize
from odoo.tests import tagged
from odoo.tools import config, is_html_empty, mute_logger

from odoo.addons.payment import utils as payment_utils
from odoo.addons.payment.const import REPORT_REASONS_MAPPING
from odoo.addons.payment.controllers.post_processing import PaymentPostProcessing
from odoo.addons.payment.tests.http_common import PaymentHttpCommon
from odoo.addons.payment_epays import const
from odoo.addons.payment_epays import utils as epays_utils
from odoo.addons.payment_epays.controllers.main import EpaysController, EpaysPostProcessing
from odoo.addons.payment_epays.tests.common import REQUESTS_PATH, EpaysCommon, FakeResponse
from odoo.addons.payment_epays.utils import EpaysApiError, is_at_bank

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
            payment_utils.generate_idempotency_key(tx, scope=f'payment_request_epays_{tx.id}'),
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
        self.assertNotIn('subscriptionId', payload)
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
        payload = calls[0]['payload']
        self.assertEqual(
            payload.get('subscriptionId'), payload.get('gatewayId'),
            "The current name and, for a server not updated yet, the former one.",
        )
        return payload.get('subscriptionId')

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_creating_a_payment_waits_longer_than_reading_one(self):
        tx = self._create_transaction('redirect', reference=self._new_reference())
        calls, _values = self._initiate(tx)
        self.assertEqual(calls[0]['timeout'], const.PAYMENT_REQUEST_TIMEOUT)
        self.assertGreater(const.PAYMENT_REQUEST_TIMEOUT, const.REQUEST_TIMEOUT)

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_a_retry_after_a_timeout_sends_a_new_idempotency_key(self):
        """ ePays answered too late: the request failed and was rolled back, though ePays may have
        created the payment. The next click gets the same reference back and must not resend the
        key with a new payload, or ePays refuses every click (409 IDEMPOTENCY_CONFLICT). """
        keys = []

        def handler(provider, method, path, **kwargs):
            keys.append(kwargs['idempotency_key'])
            if len(keys) == 1:
                raise EpaysApiError(
                    "ePays: Could not establish the connection to the API.",
                    connection_error="Read timed out.",
                )
            return {'paymentId': self.payment_id, 'redirect': self.redirect_url}

        with self._patch_make_request(handler):
            with self.assertRaises(ValidationError), self.env.cr.savepoint():
                self._create_transaction('redirect', reference='S00074-1')._get_processing_values()
            self.env.invalidate_all()
            tx = self._create_transaction('redirect', reference='S00074-1')
            tx._get_processing_values()

        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        self.assertEqual(tx.provider_reference, self.payment_id)

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
        self.assertEqual(
            calls[0]['payload']['subscriptionId'], self.production_apple_pay_gateway_ref
        )

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
                    tx._handle_notification_data('epays', self._notification_data(tx))
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
                    tx._handle_notification_data('epays', self._notification_data(tx))
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
            tx._handle_notification_data('epays', self._notification_data(tx))
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
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertEqual(calls[0]['path'], f'/api/v2/Payments/{tx.provider_reference}')
        self.assertEqual(calls[0]['environment'], 'sandbox')
        self.assertEqual(calls[0]['domain'], 'old.example.com')
        self.assertEqual(tx.state, 'done')

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_the_details_are_read_under_their_v2_names_of_2026_10_18(self):
        """ ePays renamed `gatewayId` to `subscriptionId` and `paidAt` to `completedAt`, and sends
        statuses in snake case. The current name wins when a server sends both. """
        tx = self._create_epays_transaction()
        data = self._payment_data(
            tx, subscriptionId=self.apple_pay_gateway_ref, gatewayId=self.card_gateway_ref,
            status='completed',
        )
        with self._patch_payment_details(data):
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.payment_method_id, self.apple_pay_method)
        self.assertEqual(tx.epays_gateway_id, self.apple_pay_gateway_ref)

        tx_new_server = self._create_epays_transaction()
        data = self._payment_data(
            tx_new_server, subscriptionId=self.card_gateway_ref, completedAt='2026-10-18T11:00:00',
            status='completed',
        )
        del data['gatewayId'], data['paidAt']
        with self._patch_payment_details(data):
            tx_new_server._handle_notification_data(
                'epays', self._notification_data(tx_new_server)
            )
        self.assertEqual(tx_new_server.payment_method_id, self.card_method)

    def test_the_note_reads_the_v2_names_of_2026_10_18(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        tx = self._create_epays_transaction(**self._order_values(order))
        data = self._payment_data(tx, status='completed', completedAt='2026-10-18T11:00:00')
        del data['paidAt']
        with self._patch_payment_details(data), mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            tx._handle_notification_data('epays', self._notification_data(tx))
        note = order.message_ids.filtered(lambda m: 'ePays payment details' in (m.body or ''))
        self.assertIn('Result: Completed', note.body, "A snake-case status reads as before.")
        self.assertNotIn('(Completed)', note.body, "The same word is not repeated as the result.")
        self.assertIn('Paid At: 2026-10-18T11:00:00', note.body)

    def test_payment_method_follows_the_epays_gateway(self):
        tx = self._create_epays_transaction()  # Paid with "ePays": chosen on the ePays page.
        data = self._payment_data(tx, gatewayId=int(self.apple_pay_gateway_ref))
        with self._patch_payment_details(data):
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertEqual(tx.payment_method_id, self.apple_pay_method)

        # An inactive gateway still names the method that took the payment.
        tx_benefit = self._create_epays_transaction()
        data = self._payment_data(tx_benefit, gatewayId=self.benefit_gateway_ref)
        with self._patch_payment_details(data):
            tx_benefit._handle_notification_data(
                'epays', self._notification_data(tx_benefit)
            )
        self.assertEqual(tx_benefit.payment_method_id, self.benefit_method)

        tx_unknown_gateway = self._create_epays_transaction()
        with self._patch_payment_details(self._payment_data(tx_unknown_gateway, gatewayId='99')):
            tx_unknown_gateway._handle_notification_data(
                'epays', self._notification_data(tx_unknown_gateway)
            )
        self.assertEqual(tx_unknown_gateway.payment_method_id, self.epays_method)

        # The gateways of the other environment are not used.
        tx_production_gateway = self._create_epays_transaction()
        data = self._payment_data(
            tx_production_gateway, gatewayId=self.production_apple_pay_gateway_ref
        )
        with self._patch_payment_details(data):
            tx_production_gateway._handle_notification_data(
                'epays', self._notification_data(tx_production_gateway)
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
        url = self._build_url(f'{EpaysController._return_url}/sandbox')
        with self._patch_payment_details(self._payment_data(tx)):
            response = self.opener.get(
                url, params={'paymentId': tx.provider_reference}, allow_redirects=False
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(tx.state, 'done')

    @mute_logger(CONTROLLER_LOGGER, TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_return_route_accepts_post(self):
        tx = self._create_epays_transaction()
        url = self._build_url(f'{EpaysController._return_url}/sandbox')
        with self._patch_payment_details(self._payment_data(tx)):
            response = self.opener.post(
                url, data=self._notification_data(tx), allow_redirects=False
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
                    patch.object(
                        EpaysPostProcessing, '_get_monitored_transaction',
                        return_value=self.env['payment.transaction'],
                    ), \
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
            tx._handle_notification_data('epays', self._notification_data(tx))
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
            tx._handle_notification_data('epays', self._notification_data(tx))  # Ping.
            tx._handle_notification_data('epays', self._notification_data(tx))  # Return.
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
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertEqual(tx.state, 'done')
        self.assertFalse(tx.epays_gateway_response)
        self.assertFalse(tx.epays_gateway_response_display)
        if order:
            note = order.message_ids.filtered(lambda m: 'ePays payment details' in (m.body or ''))
            self.assertEqual(len(note), 1)
            self.assertNotIn('<hr', note.body)
            self.assertNotIn('Card Brand', note.body)

    def _activate_arabic(self):
        self.env['res.lang']._activate_lang('ar_001')
        self.env['ir.module.module']._load_module_terms(['payment_epays'], ['ar_001'])

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_a_customer_paying_from_the_arabic_website_is_answered_in_arabic(self):
        """ The contact is in English, the website in Arabic: the page opens in Arabic, and so does
        everything the customer is told afterwards. The return from ePays carries no website
        language: ePays reports the page's (`paymentLang`). """
        self._activate_arabic()
        self.partner.lang = 'en_US'
        tx = self._create_transaction('redirect', reference=self._new_reference())

        calls, _values = self._initiate(tx.with_context(lang='ar_001'))

        self.assertEqual(calls[0]['payload']['lang'], 'ar')
        declined = self._payment_data(
            tx, status='Failed', result='DECLINED', responseCode='05', paymentLang='ar',
            responseDescription={'en': "Do not honour", 'ar': "مرفوض"},
        )
        with self._patch_payment_details(declined):
            tx.with_context(lang='en_US')._handle_notification_data(
                'epays', self._notification_data(tx)
            )
        self.assertEqual(
            tx.state_message,
            "تم رفض الدفع: مرفوض. يمكنك المحاولة مرة أخرى أو اختيار طريقة دفع أخرى.",
        )

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_every_message_follows_the_page_language_reported_by_epays(self):
        """ `paymentLang` decides, both ways and for every outcome; without it (an older ePays
        server) the customer's own language is used. """
        self._activate_arabic()
        description = {'responseDescription': {'en': "Do not honour", 'ar': "مرفوض"}}
        rows = [
            # (contact language, paymentLang, outcome, expected start of the message)
            ('en_US', 'ar', {'status': 'Failed', **description}, "تم رفض الدفع: مرفوض."),
            ('ar_001', 'en', {'status': 'Failed', **description}, "Your payment was declined: Do"),
            ('en_US', 'ar', {'status': 'Cancelled'}, "تم إلغاء الدفع."),
            ('ar_001', None, {'status': 'Failed', **description}, "تم رفض الدفع: مرفوض."),
            ('en_US', None, {'status': 'Failed', **description}, "Your payment was declined: Do"),
        ]
        for contact_lang, payment_lang, outcome, expected in rows:
            with self.subTest(
                contact=contact_lang, paymentLang=payment_lang, status=outcome['status']
            ):
                self.partner.lang = contact_lang
                tx = self._create_epays_transaction()
                data = self._payment_data(tx, result='', **outcome)
                if payment_lang:
                    data['paymentLang'] = payment_lang
                with self._patch_payment_details(data):
                    tx.with_context(lang='en_US')._handle_notification_data(
                        'epays', self._notification_data(tx)
                    )
                self.assertTrue(tx.state_message.startswith(expected), tx.state_message)

    def test_the_website_loads_the_addon_translations(self):
        """ The heading of the failure message on the payment step is translated in JavaScript,
        from the modules the website lists. """
        ir_http = self.env['ir.http']
        if 'website' not in self.env:
            modules = ir_http._get_translation_frontend_modules_name()
        else:
            # The website's override lists its own modules from the request's registry. Imported
            # here: the website is an optional dependency.
            from odoo.addons.website.tools import MockRequest  # noqa: PLC0415
            with MockRequest(self.env):
                modules = ir_http._get_translation_frontend_modules_name()
        self.assertIn('payment_epays', modules)

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_decline_shows_the_description_in_the_customer_language(self):
        declined = {
            'status': 'Failed', 'result': 'DECLINED', 'responseCode': '05',
            'responseDescription': {'en': "Do not honour", 'ar': "مرفوض"},
        }
        tx_en = self._create_epays_transaction()
        with self._patch_payment_details(self._payment_data(tx_en, **declined)):
            tx_en._handle_notification_data('epays', self._notification_data(tx_en))
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
                'epays', self._notification_data(tx_ar)
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
                'epays', self._notification_data(tx_old_server)
            )
        self.assertEqual(
            tx_old_server.state_message,
            "Your payment was declined. You can try again or choose another payment method.",
        )

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_cancel_tells_the_customer_they_can_try_again(self):
        tx = self._create_epays_transaction()
        with self._patch_payment_details(self._payment_data(tx, status='Cancelled')):
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertEqual(tx.state, 'cancel')
        self.assertEqual(
            tx.state_message,
            "Your payment was cancelled. You can try again or choose another payment method.",
        )

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_retry_after_decline_completes(self):
        tx = self._create_epays_transaction()
        with self._patch_payment_details(self._payment_data(tx, status='Failed')):
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertEqual(tx.state, 'error')
        with self._patch_payment_details(self._payment_data(tx)):
            tx._handle_notification_data('epays', self._notification_data(tx))
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
            ('error', timedelta(hours=2), -timedelta(minutes=5), True, True),  # Grace period.
            ('error', timedelta(hours=2), -timedelta(minutes=31), True, False),  # Link expired.
            ('draft', timedelta(minutes=2), timedelta(hours=1), True, False),  # Too recent.
            ('pending', timedelta(days=4), timedelta(hours=1), True, False),  # Too old.
            ('done', timedelta(minutes=10), timedelta(hours=1), True, False),
            ('cancel', timedelta(minutes=10), timedelta(hours=1), True, True),  # May be charged.
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

    def _skip_if_post_processing_is_stubbed(self):
        if self.account_payment_installed and self.enable_post_process_patcher:
            self.skipTest(
                "Odoo's payment tests stub the post-processing of account_payment, and with it the "
                "ePays one; EpaysAccountingTest runs it for real"
            )

    def test_post_processing_a_done_payment_acknowledges_it_on_epays(self):
        self._skip_if_post_processing_is_stubbed()
        tx = self._create_epays_transaction(state='done', epays_merchant_domain='a.example.com')
        calls = []

        def handler(provider, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {'paymentId': tx.provider_reference, 'firstAcknowledgement': True}

        with self._patch_make_request(handler):
            tx._post_process()
            self._run_commit_hooks()
        self.assertEqual(calls, [(
            'POST', f'/api/v2/Payments/{tx.provider_reference}/acknowledgement',
            {'environment': 'sandbox', 'domain': 'a.example.com',
             'timeout': const.POST_PROCESSING_TIMEOUT},
        )])
        self.assertTrue(tx.is_post_processed)

    def test_finished_epays_payments_are_acknowledged(self):
        self._skip_if_post_processing_is_stubbed()
        acknowledged = self.env['payment.transaction']
        for state, response_code in [('done', '00'), ('error', '51'), ('cancel', False)]:
            acknowledged |= self._create_epays_transaction(
                state=state, epays_response_code=response_code
            )
        left_alone = self.env['payment.transaction']
        for state in ('draft', 'pending'):
            left_alone |= self._create_epays_transaction(state=state)
        # Money ePays took that Odoo refused: left for the merchant to reconcile on ePays.
        left_alone |= self._create_epays_transaction(state='error', epays_response_code='00')
        left_alone |= self._create_epays_transaction(state='done', provider_reference=False)
        left_alone |= self._create_transaction(
            'redirect', reference='OTHER-DONE', provider_id=self.dummy_provider.id, state='done',
        )
        paths = []

        def handler(provider, method, path, **kwargs):
            if method == 'GET':
                return {'status': 'Failed'}
            paths.append(path)
            return {}

        with self._patch_make_request(handler):
            (acknowledged | left_alone)._post_process()
            self._run_commit_hooks()
        self.assertCountEqual(paths, [
            f'/api/v2/Payments/{tx.provider_reference}/acknowledgement' for tx in acknowledged
        ])

    def test_a_cancelled_payment_is_acknowledged_only_if_epays_kept_no_money(self):
        """ Odoo's state can lag behind: an acknowledgement would remove a charged payment from the
        ones ePays lists for the merchant. """
        self._skip_if_post_processing_is_stubbed()
        for name, answer, is_acknowledged in [
            ('voided', {'status': 'Voided'}, True),
            ('voided after the gateway approved it',
             {'status': 'Voided', 'responseCode': '00'}, False),
            ('declined after the gateway approved it',
             {'status': 'Failed', 'responseCode': '00'}, False),
            ('declined, paid again on its link', {'status': 'Failed', 'inFlight': True}, False),
            ('declined', {'status': 'Failed'}, True),
            ('charged', {'status': 'Completed'}, False),
            ('refunded', {'status': 'Refunded'}, False),
            ('open, taken over by a newer attempt', {'status': 'Pending'}, False),
            ('an unknown status', {'status': 'CAPTURING'}, False),
            ('unreadable', EpaysApiError("unreachable", http_status=500), False),
        ]:
            with self.subTest(name):
                tx = self._create_epays_transaction(state='cancel')
                with self._patch_make_request(
                    lambda *args, **kwargs: None
                ) as make_request_mock, mute_logger(TX_LOGGER):
                    make_request_mock.side_effect = [answer, {}]
                    tx._post_process()
                    self._run_commit_hooks()
                self.assertEqual(
                    [call.args[1] for call in make_request_mock.call_args_list],
                    ['GET', 'POST'] if is_acknowledged else ['GET'],
                )

    def test_a_payment_charged_after_the_cancellation_completes_the_transaction(self):
        tx = self._create_epays_transaction(state='cancel')
        with self._patch_payment_details(self._payment_data(tx)), \
                mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertEqual(tx.state, 'done', "ePays's status wins over the cancellation.")
        self.assertEqual(tx.epays_response_code, '00')

    def test_the_cron_finds_a_payment_charged_after_the_cancellation(self):
        tx = self._create_epays_transaction(state='cancel')
        self._set_create_date(tx, fields.Datetime.now() - timedelta(minutes=10))
        self._run_cron(lambda *args, **kwargs: self._payment_data(tx))
        self.assertEqual(tx.state, 'done')

    def test_a_cancelled_payment_that_ePays_did_not_charge_stays_cancelled(self):
        for status in ('Voided', 'Cancelled', 'Expired', 'Pending', 'Failed', 'Refunded'):
            with self.subTest(status=status):
                tx = self._create_epays_transaction(state='cancel', state_message="Replaced.")
                with self._patch_payment_details(self._payment_data(tx, status=status)), \
                        mute_logger(TX_LOGGER):
                    tx._handle_notification_data('epays', self._notification_data(tx))
                self.assertEqual((tx.state, tx.state_message), ('cancel', "Replaced."))

    def test_a_notification_days_later_completes_a_cancelled_payment(self):
        """ ePays corrected the payment and sent its notification again: past the time the cron
        looks back, as the cron voided the payment while ePays still reported it pending. """
        tx = self._create_epays_transaction(state='cancel')
        tx.last_state_change = fields.Datetime.now() - timedelta(days=10)
        tx.epays_watch_until = fields.Datetime.now() - timedelta(days=8)
        self._set_create_date(tx, fields.Datetime.now() - timedelta(days=10))
        polled = []
        self._run_cron(lambda provider, method, path, **kwargs: polled.append(path) or {})
        self.assertEqual(polled, [], "The cron has stopped reading it.")
        self.assertEqual(tx.state, 'cancel')

        with self._patch_payment_details(self._payment_data(tx)), \
                mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertEqual(tx.state, 'done')

    def test_a_notification_days_later_changes_nothing_while_epays_was_not_corrected(self):
        tx = self._create_epays_transaction(state='cancel', state_message="Voided.")
        tx.last_state_change = fields.Datetime.now() - timedelta(days=10)
        with self._patch_payment_details(self._payment_data(tx, status='Voided')), \
                mute_logger(TX_LOGGER):
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertEqual((tx.state, tx.state_message), ('cancel', "Voided."))

    def test_a_late_charge_that_does_not_match_is_refused(self):
        tx = self._create_epays_transaction(state='cancel')
        with self._patch_payment_details(self._payment_data(tx, amount='1.000')), \
                mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertEqual(tx.state, 'error')
        self.assertEqual(tx.epays_response_code, '00', "So it is left unacknowledged.")

    def test_a_cancelled_payment_is_read_only_for_a_while_and_only_by_its_owner(self):
        now = fields.Datetime.now()
        tx_old = self._create_epays_transaction(state='cancel')
        tx_old.epays_watch_until = now - timedelta(minutes=1)
        tx_taken_over = self._create_epays_transaction(state='cancel')
        self._create_epays_transaction(
            state='draft', provider_reference=tx_taken_over.provider_reference
        )
        tx_recent = self._create_epays_transaction(state='cancel')
        self.assertFalse(tx_old._epays_may_be_charged())
        self.assertFalse(tx_taken_over._epays_may_be_charged())
        self.assertTrue(tx_recent._epays_may_be_charged())

        self._set_create_date(tx_taken_over, now - timedelta(minutes=10))
        polled = []
        self._run_cron(lambda provider, method, path, **kwargs: polled.append(path) or {})
        self.assertNotIn(
            f'/api/v2/Payments/{tx_taken_over.provider_reference}', polled,
            "The transaction that took the payment over reads it.",
        )

    def test_s9_a_cancelled_payment_is_watched_as_long_as_its_customer_can_pay_it(self):
        """ S9: the same limits as `OrderStore::watch_payment` in the WooCommerce plugin. """
        now = fields.Datetime.now()
        with patch.object(fields.Datetime, 'now', return_value=now):
            for expires_in, at_bank, watched_for in (
                (timedelta(hours=1), False, const.LATE_CHARGE_WINDOW),
                (timedelta(hours=5), False, timedelta(hours=5) + const.CRON_VOID_GRACE_PERIOD),
                (timedelta(hours=1), True, timedelta(hours=1) + const.CRON_MAX_AGE),
                (-timedelta(hours=1), True, const.CRON_MAX_AGE),
            ):
                with self.subTest(expires_in=expires_in, at_bank=at_bank):
                    tx = self._create_epays_transaction(
                        state='draft', epays_expires_at=now + expires_in, epays_at_bank=at_bank,
                    )
                    tx._set_canceled("Replaced.")
                    self.assertEqual(tx.epays_watch_until, now + watched_for)

    def test_s9_a_payment_found_at_the_bank_is_watched_for_longer(self):
        """ S9: the cancellation did not know it, as with `OrderStore::set_at_bank`. """
        tx = self._create_epays_transaction(state='cancel')
        tx._epays_record_at_bank(
            self._payment_data(tx, status='Pending', result='', transactionId='TXN-1')
        )
        self.assertGreaterEqual(
            tx.epays_watch_until, fields.Datetime.now() + const.CRON_MAX_AGE - timedelta(seconds=5)
        )

    def test_s17_the_public_routes_read_a_payment_a_limited_number_of_times(self):
        """ S17: a transaction stays readable on a public route, whatever its state. """
        for state in ('cancel', 'draft', 'error'):
            with self.subTest(state=state):
                tx = self._create_epays_transaction(state=state)
                data = self._payment_data(tx, status='Pending', result='')
                with self._patch_payment_details(data) as make_request_mock, \
                        mute_logger(TX_LOGGER):
                    for _i in range(const.FETCH_LIMIT + 2):
                        tx._handle_notification_data('epays', self._notification_data(tx))
                    self.assertEqual(make_request_mock.call_count, const.FETCH_LIMIT)
                    with patch.object(const, 'FETCH_LIMIT_WINDOW', 0):
                        tx._handle_notification_data('epays', self._notification_data(tx))
                    self.assertEqual(
                        make_request_mock.call_count, const.FETCH_LIMIT + 1,
                        "Read again after the window.",
                    )

    def test_s10_a_read_that_cannot_run_goes_back_to_the_scheduled_check(self):
        """ S10: ePays's notification may be the only one, even long after the cancellation. """
        now = fields.Datetime.now()
        tx = self._create_epays_transaction(state='cancel')
        tx.epays_watch_until = now - timedelta(days=9)
        with self._patch_make_request(lambda *args, **kwargs: {}) as make_request_mock, \
                patch.object(type(tx), '_epays_may_read_payment', return_value=False), \
                mute_logger(TX_LOGGER):
            tx._handle_notification_data('epays', self._notification_data(tx))
        make_request_mock.assert_not_called()
        self.assertGreaterEqual(tx.epays_watch_until, now + const.LATE_CHARGE_WINDOW)

    def test_a_read_that_cannot_run_on_an_old_payment_goes_back_to_the_scheduled_check(self):
        """ ePays counts its notification as delivered whatever Odoo did with it. """
        now = fields.Datetime.now()
        for state in ('pending', 'error'):
            with self.subTest(state=state):
                tx = self._create_epays_transaction(
                    state=state, epays_expires_at=now - timedelta(days=8)
                )
                self._set_create_date(tx, now - timedelta(days=9))
                data = self._payment_data(tx)
                self.assertFalse(tx._epays_is_read_by_cron())

                def unreachable(provider, method, path, **kwargs):
                    raise EpaysApiError("unreachable", connection_error="timed out", timed_out=True)

                with self._patch_make_request(unreachable), mute_logger(TX_LOGGER):
                    tx._handle_notification_data('epays', self._notification_data(tx))
                self.assertEqual(tx.state, state)
                self.assertGreaterEqual(tx.epays_watch_until, now + const.LATE_CHARGE_WINDOW)

                self._run_cron(lambda provider, method, path, data=data, **kwargs: data)
                self.assertEqual(tx.state, 'done', "The scheduled check read it after all.")

    def test_a_limited_read_on_an_old_payment_goes_back_to_the_scheduled_check(self):
        now = fields.Datetime.now()
        tx = self._create_epays_transaction(state='error', epays_expires_at=now - timedelta(days=8))
        self._set_create_date(tx, now - timedelta(days=9))
        with self._patch_make_request(lambda *args, **kwargs: {}) as make_request_mock, \
                patch.object(type(tx), '_epays_may_read_payment', return_value=False), \
                mute_logger(TX_LOGGER):
            tx._handle_notification_data('epays', self._notification_data(tx))
        make_request_mock.assert_not_called()
        self.assertGreaterEqual(tx.epays_watch_until, now + const.LATE_CHARGE_WINDOW)

    def test_a_read_that_cannot_run_on_a_recent_payment_stays_with_the_scheduled_check(self):
        tx = self._create_epays_transaction(state='pending')
        with patch.object(type(tx), '_epays_may_read_payment', return_value=False), \
                mute_logger(TX_LOGGER):
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertFalse(tx.epays_watch_until, "The scheduled check reads it anyway.")

    def test_a_notification_days_later_completes_an_old_declined_payment(self):
        """ The customer's money was taken after all, and ePays corrected the payment. """
        now = fields.Datetime.now()
        tx = self._create_epays_transaction(state='error', epays_expires_at=now - timedelta(days=8))
        self._set_create_date(tx, now - timedelta(days=9))
        with self._patch_payment_details(self._payment_data(tx)), \
                mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertEqual(tx.state, 'done')

    def test_s17_a_failed_read_counts_and_leaves_the_next_one_free(self):
        """ ePays notifies once: a timeout must leave the retry free to record the charge. """
        tx = self._create_epays_transaction(state='cancel')
        tx.epays_watch_until = fields.Datetime.now() - timedelta(days=9)
        data = self._payment_data(tx)

        def handler(provider, method, path, **kwargs):
            raise EpaysApiError("unreachable", connection_error="read timed out", timed_out=True)

        with self._patch_make_request(handler), mute_logger(TX_LOGGER):
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertTrue(tx._epays_may_be_charged(), "The scheduled check reads it instead.")
        self.assertEqual(len(tx._epays_get_recent_reads()), 1)
        with self._patch_payment_details(data), mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertEqual(tx.state, 'done')

    def test_s17_failing_reads_stop_at_the_limit(self):
        """ Failed calls are what locks a licence on ePays: a script gets no more of them. """
        tx = self._create_epays_transaction(state='cancel')
        calls = []

        def handler(provider, method, path, **kwargs):
            calls.append(method)
            raise EpaysApiError("refused", http_status=401)

        with self._patch_make_request(handler), mute_logger(TX_LOGGER):
            for _i in range(const.FETCH_LIMIT + 3):
                tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertEqual(len(calls), const.FETCH_LIMIT)
        self.assertTrue(tx._epays_may_be_charged(), "The scheduled check reads it instead.")

    def test_s17_a_missing_or_unknown_environment_is_refused_without_a_lookup(self):
        """ The environment comes from a fixed list, and the URL of the route is public. """
        tx = self._create_epays_transaction(state='pending')
        Transaction = self.env['payment.transaction']
        for environment in ('unknown', '', None):
            data = dict(self._notification_data(tx))
            data[const.NOTIFICATION_ENVIRONMENT_KEY] = environment
            with self.subTest(environment=environment), patch.object(
                type(Transaction), 'search', wraps=Transaction.search,
            ) as search_mock:
                with self.assertRaises(ValidationError):
                    Transaction._get_tx_from_notification_data('epays', data)
                search_mock.assert_not_called()

    def test_s17_a_malformed_payment_id_is_refused_without_a_lookup(self):
        """ S17: the same rule as `ReturnController::is_valid_payment_id`. """
        Transaction = self.env['payment.transaction']
        for payment_id in ('', ' ', 'a' * 65, "PAY'1", 'PAY 1', '../PAY', ['PAY']):
            with self.subTest(payment_id=payment_id), patch.object(
                type(Transaction), 'search', wraps=Transaction.search,
            ) as search_mock:
                with self.assertRaises(ValidationError):
                    Transaction._get_tx_from_notification_data('epays', {'paymentId': payment_id})
                search_mock.assert_not_called()

    def test_a_read_retried_after_a_concurrency_error_still_records_the_charge(self):
        """ Odoo runs the request again, after the ePays read, in the same process. """
        tx = self._create_epays_transaction(state='cancel')
        data = self._payment_data(tx)
        with self._patch_payment_details(data) as make_request_mock, \
                mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            with patch.object(
                type(tx), '_epays_lock_for_update', side_effect=SerializationFailure
            ), self.assertRaises(SerializationFailure):
                tx._handle_notification_data('epays', self._notification_data(tx))
            tx._handle_notification_data('epays', self._notification_data(tx))
        self.assertEqual(make_request_mock.call_count, 2)
        self.assertEqual(tx.state, 'done')

    def test_the_cron_reads_a_cancelled_payment_whatever_its_age(self):
        """ A long payment link can be cancelled after the age limit of the open ones. """
        tx = self._create_epays_transaction(state='cancel')
        self._set_create_date(tx, fields.Datetime.now() - const.CRON_MAX_AGE - timedelta(days=1))
        data = self._payment_data(tx)
        self._run_cron(lambda provider, method, path, **kwargs: data)
        self.assertEqual(tx.state, 'done')

    def test_a_cursor_failure_after_the_commit_does_not_reach_the_request(self):
        tx = self._create_epays_transaction(state='done')
        with patch.object(type(self.env.registry), 'cursor', side_effect=pool.PoolError), \
                mute_logger(TX_LOGGER):
            tx._epays_close_on_epays()  # Does not raise.

    def test_a_failed_acknowledgement_is_only_logged(self):
        for error in (
            EpaysApiError("unreachable", http_status=404),  # A server before the endpoint.
            EpaysApiError("unreachable", http_status=404, error_code='PAYMENT_NOT_FOUND'),
            EpaysApiError("unreachable", http_status=500, error_code='SERVER_ERROR'),
            EpaysApiError("unreachable", connection_error="read timed out", timed_out=True),
        ):
            with self.subTest(http_status=error.http_status, error_code=error.error_code):
                tx = self._create_epays_transaction(state='done')

                def handler(provider, method, path, error=error, **kwargs):
                    raise error

                with self._patch_make_request(handler), mute_logger(TX_LOGGER):
                    tx._epays_acknowledge()  # Does not raise.
                self.assertEqual(tx.state, 'done')

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
            tx._handle_notification_data('epays', self._notification_data(tx))
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
                    tx._handle_notification_data('epays', self._notification_data(tx))
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
            tx._handle_notification_data('epays', self._notification_data(tx))
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

    def test_s21_the_database_only_holds_encrypted_master_keys(self):
        """ S21: a dump or a backup of the column reveals no key. """
        self.provider.write({'epays_master_key': 'a-new-master-key'})
        self.provider.flush_recordset()
        self.env.cr.execute(
            "SELECT epays_master_key FROM payment_provider WHERE id = %s", (self.provider.id,)
        )
        stored = self.env.cr.fetchone()[0]
        self.assertTrue(stored.startswith(const.SECRET_PREFIX))
        self.assertNotIn('a-new-master-key', stored)
        self.assertEqual(
            self.provider._epays_get_credentials('production'), ('prod-api-id', 'a-new-master-key')
        )

    def test_a_master_key_stored_before_encryption_still_works(self):
        """ A failed migration must not stop the payments. """
        self.env.cr.execute(
            "UPDATE payment_provider SET epays_master_key = 'plain-key' WHERE id = %s",
            (self.provider.id,),
        )
        self.provider.invalidate_recordset()
        self.assertEqual(
            self.provider._epays_get_credentials('production'), ('prod-api-id', 'plain-key')
        )

    def test_s21_a_master_key_that_cannot_be_read_is_never_replaced_by_another(self):
        """ S21: a sandbox request does not send the production key. """
        self.provider.flush_recordset()
        with patch.object(
            type(self.provider), '_epays_get_key_material', return_value='another-secret',
        ), mute_logger(PROVIDER_LOGGER):
            self.provider.invalidate_recordset()
            self.assertIsNone(self.provider._epays_read_secret('epays_sandbox_master_key'))
            self.assertEqual(
                self.provider._epays_get_credentials('sandbox'), ('sandbox-api-id', '')
            )
            self.provider.state = 'test'
            self.assertIn("can no longer be read", self.provider._epays_get_warnings()[0])

    def test_a_configured_encryption_key_comes_first(self):
        Provider = self.env['payment.provider']
        with patch.dict(os.environ, {const.ENCRYPTION_KEY_VARIABLE: 'from-the-environment'}):
            self.assertEqual(Provider._epays_get_key_material(), 'from-the-environment')
            with patch.dict(config.options, {const.ENCRYPTION_KEY_OPTION: 'from-odoo-conf'}):
                self.assertEqual(Provider._epays_get_key_material(), 'from-odoo-conf')

    def test_s21_a_master_key_is_refused_over_plain_http(self):
        """ S21: it just crossed the network in clear. """
        fake_request = MagicMock()
        fake_request.httprequest.scheme = 'http'
        fake_request.httprequest.host = 'shop.example.com'
        fake_request.httprequest.headers = {}
        provider_module = 'odoo.addons.payment_epays.models.payment_provider.request'
        with patch(provider_module, fake_request):
            with self.assertRaises(ValidationError):
                self.provider.write({'epays_master_key': 'a-new-master-key'})
            fake_request.httprequest.headers = {'X-Forwarded-Proto': 'https'}
            self.provider.write({'epays_master_key': 'a-new-master-key'})
            fake_request.httprequest.headers = {}
            fake_request.httprequest.host = 'localhost:8069'
            self.provider.write({'epays_master_key': 'another-master-key'})

    def test_s22_the_provider_warns_about_what_needs_attention(self):
        """ S22: the warnings of the WooCommerce plugin's `Notices`. """
        self.assertEqual(
            self.provider._epays_get_warnings(), [] if self.provider.state != 'test' else
            self.provider._epays_get_warnings(),
        )
        self.provider.write({'state': 'test', 'is_published': True, 'epays_mode': 'sandbox'})
        with patch.object(type(self.provider), '_epays_can_convert', return_value=True):
            self.assertIn("Sandbox mode is shown", self.provider._epays_get_warnings()[0])
        with patch.object(type(self.provider), '_epays_can_convert', return_value=False):
            self.assertIn("no exchange rate", self.provider._epays_get_warnings()[-1])
        with patch.object(type(self.provider), '_epays_can_convert', return_value=True), \
                patch.object(type(self.provider), '_epays_has_stale_rate', return_value=True):
            self.assertIn("more than a week old", self.provider._epays_get_warnings()[-1])

    def test_a_rate_older_than_a_week_is_stale(self):
        company = self.env.company
        charge_currency = self.provider._epays_get_charge_currency()
        if charge_currency == company.currency_id:
            self.skipTest("The company charges in the currency of ePays.")
        Rate = self.env['res.currency.rate']
        Rate.search([('currency_id', '=', charge_currency.id)]).unlink()
        rate = Rate.create({
            'currency_id': charge_currency.id,
            'rate': 0.376,
            'name': fields.Date.today() - timedelta(days=8),
            'company_id': False,
        })
        self.assertTrue(self.provider._epays_has_stale_rate(company))
        rate.name = fields.Date.today() - timedelta(days=6)
        self.assertFalse(self.provider._epays_has_stale_rate(company))

    def test_s20_the_order_lines_are_sent_only_when_they_add_up_to_the_fils(self):
        """ S20: Tabby recomputes what it charges from them. """
        item = {'name': "Coffee", 'quantity': 2, 'sku': 'C1', 'total': 5000}
        lines = epays_utils.get_order_lines([item], 500, 5500, 3)
        self.assertEqual(lines, [
            {'name': "Coffee", 'quantity': 2, 'unitPrice': '2.500', 'sku': 'C1'},
        ])
        self.assertIsNone(
            epays_utils.get_order_lines([item], 500, 5501, 3), "One fils short: left out."
        )
        self.assertIsNone(epays_utils.get_order_lines([], 0, 0, 3))
        self.assertIsNone(epays_utils.get_order_lines(
            [item, {'name': "Discount", 'quantity': 1, 'sku': '', 'total': -100}], 0, 4900, 3
        ), "A negative line would lower Tabby's own sum.")
        self.assertEqual(
            epays_utils.get_order_lines([dict(item, quantity=3)], 0, 5000, 3),
            [{'name': "Coffee × 3", 'quantity': 1, 'unitPrice': '5.000', 'sku': 'C1'}],
            "A unit price would be rounded: one line with its total.",
        )
        self.assertEqual(
            epays_utils.get_order_lines(
                [item, {'name': "Tea", 'quantity': 3, 'sku': '', 'total': 1000}], 0, 6000, 3,
            ),
            [
                {'name': "Coffee", 'quantity': 2, 'unitPrice': '2.500', 'sku': 'C1'},
                {'name': "Tea × 3", 'quantity': 1, 'unitPrice': '1.000'},
            ],
            "Only the line that does not divide loses its quantity.",
        )

    def test_each_payment_field_is_read_under_its_new_name_then_its_old_one(self):
        for label, data in (
            ('new', {'subscriptionId': 'G1', 'completedAt': 'T1'}),
            ('old', {'gatewayId': 'G1', 'paidAt': 'T1'}),
            ('both', {
                'subscriptionId': 'G1', 'gatewayId': 'X', 'completedAt': 'T1', 'paidAt': 'X',
            }),
        ):
            with self.subTest(label):
                details = epays_utils.read_payment(data)
                self.assertEqual(details['subscription_id'], 'G1')
                self.assertEqual(details['completed_at'], 'T1')
        for label, data in (
            ('new', {'subscriptionId': ' G1 ', 'gatewayTypeId': 2}),
            ('old', {'merchantGateway': 'G1', 'gatewayId': 2}),
        ):
            with self.subTest(label):
                details = epays_utils.read_gateway(data)
                self.assertEqual((details['ref'], details['type_id']), ('G1', 2))

    def test_a_malformed_payload_reads_as_empty(self):
        details = epays_utils.read_payment({'customFields': 'text', 'paymentLang': 'fr'})
        self.assertIsNone(details['udf2'])
        self.assertIsNone(details['page_lang'], "Only what the addon can answer in.")
        self.assertEqual(epays_utils.read_payment_list({'payments': 'text'}), [])
        self.assertEqual(epays_utils.read_payment_list({'payments': ['text', {}]}), [{}])
        self.assertEqual(
            epays_utils.read_gateway_page({'gateways': 'text', 'nextCursor': 5}), ([], None)
        )
        self.assertEqual(epays_utils.read_api_data({'data': ['list']}), {})

    def test_the_amount_is_compared_at_the_precision_of_the_charged_currency(self):
        data = {'amount': '37.600', 'currency': 'bhd '}
        self.assertEqual(epays_utils.get_amount_mismatches(data, 37.6, 'BHD', 3), [])
        self.assertEqual(epays_utils.get_amount_mismatches(data, 37.599, 'BHD', 3), ['amount'])
        self.assertEqual(epays_utils.get_amount_mismatches(data, 37.6, 'USD', 3), ['currency'])
        for amount in (None, 'not a number', 'nan', 'inf'):
            with self.subTest(amount=amount):
                self.assertEqual(
                    epays_utils.get_amount_mismatches(dict(data, amount=amount), 37.6, 'BHD', 3),
                    ['amount'],
                )

    def test_the_references_are_compared_only_when_epays_returns_them(self):
        def has_mismatch(custom_fields):
            return epays_utils.has_reference_mismatch(
                {'customFields': custom_fields}, 'S00001', False
            )

        self.assertFalse(has_mismatch(None))
        self.assertFalse(has_mismatch({'udf2': ' S00001 ', 'udf3': ''}))
        self.assertTrue(has_mismatch({'udf2': 'S00002', 'udf3': ''}))
        self.assertTrue(has_mismatch({'udf2': 'S00001', 'udf3': 'INV/1'}))

    def test_a_failed_response_is_read_as_one_kind_of_failure(self):
        error = {'code': 'E1'}
        failure = {'error_code': 'E1', 'error_message': None, 'details': None}
        nothing = dict.fromkeys(failure)
        self.assertIsNone(epays_utils.get_api_failure(True, {'status': 'SUCCESS'}))
        self.assertEqual(
            epays_utils.get_api_failure(True, {'status': 'error', 'error': error}), failure,
            "The envelope can fail on HTTP 200.",
        )
        self.assertEqual(epays_utils.get_api_failure(False, {'error': error}), failure)
        for body in (None, ['list'], {'error': 'text'}):
            with self.subTest(body=body):
                self.assertEqual(epays_utils.get_api_failure(False, body), nothing)
        self.assertEqual(epays_utils.get_api_failure(True, None), nothing, "Not JSON.")

    def test_an_error_response_is_logged_at_debug_level_only(self):
        """ An error can repeat what the request held, and the code is enough to diagnose it. """
        response = FakeResponse(422, {'status': 'ERROR', 'error': {
            'code': 'INVALID_EMAIL', 'message': f"Invalid email {self.partner.email}",
        }})
        with patch(REQUESTS_PATH, return_value=response), \
                self.assertLogs(PROVIDER_LOGGER, level='DEBUG') as logs, \
                self.assertRaises(EpaysApiError) as error:
            self.provider._epays_make_request(
                'GET', '/api/v2/Payments', environment='sandbox', domain='shop.example.com',
            )
        self.assertEqual(error.exception.error_code, 'INVALID_EMAIL')
        leaks = [r for r in logs.records if self.partner.email in r.getMessage()]
        self.assertTrue(leaks, "The response is still available when debugging.")
        self.assertEqual({r.levelname for r in leaks}, {'DEBUG'})
        self.assertTrue([r for r in logs.records if 'INVALID_EMAIL' in r.getMessage()])

    def test_the_payment_url_stays_out_of_the_debug_log(self):
        """ The payment page URL is a bearer link: see `get_loggable_data`. """
        url = 'https://testapi.epays.io/Pay/P1?token=secret-token'
        response = FakeResponse(200, {'status': 'SUCCESS', 'data': {
            'paymentId': 'P1', 'redirect': url,
        }})
        with patch(REQUESTS_PATH, return_value=response), \
                self.assertLogs(PROVIDER_LOGGER, level='DEBUG') as logs:
            data = self.provider._epays_make_request(
                'POST', '/api/v2/Payments', environment='sandbox', domain='shop.example.com',
            )
        self.assertEqual(data['redirect'], url, "The caller still gets it.")
        self.assertTrue([r for r in logs.records if 'P1' in r.getMessage()])
        self.assertNotIn('secret-token', '\n'.join(logs.output))

    def test_a_request_for_the_same_documents_waits_before_reading_epays(self):
        """ Two first clicks would each create a payable link: see `_epays_lock_documents`. """
        self._skip_without_sale()
        order = self._create_sale_order()
        tx = self._create_transaction('redirect', reference=False, **self._order_values(order))
        events = []

        def handler(provider, method, path, **kwargs):
            events.append(method)
            return {'paymentId': self.payment_id, 'redirect': self.redirect_url}

        lock_path = (
            'odoo.addons.payment_epays.models.payment_transaction.PaymentTransaction'
            '._epays_lock_documents'
        )
        with patch(lock_path, autospec=True, side_effect=lambda tx: events.append('lock')), \
                self._patch_make_request(handler), mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            tx._get_processing_values()
        self.assertEqual(events, ['lock', 'POST'])

    def test_the_document_lock_updates_the_row(self):
        """ A row lock alone leaves the second request on a snapshot without the first payment. """
        self._skip_without_sale()
        order = self._create_sale_order()
        tx = self._create_transaction('redirect', reference=False, **self._order_values(order))
        write_date = order.write_date
        self.env.flush_all()
        with patch.object(self.env.cr, 'execute', wraps=self.env.cr.execute) as execute_mock:
            tx._epays_lock_documents()
        self.assertIn('UPDATE "sale_order"', str(execute_mock.call_args.args[0]))
        order.invalidate_recordset(['write_date'])
        self.assertEqual(order.write_date, write_date, "Nobody edited the order.")

    def test_the_language_and_the_rate_source_are_stored_with_the_payment(self):
        self.provider.epays_payment_lang = 'ar'
        tx = self._create_transaction('redirect', reference=self._new_reference())
        self._initiate(tx)
        self.assertEqual(tx.epays_lang, 'ar')
        self.assertFalse(tx.epays_rate_source, "Nothing was converted.")

        self.provider.epays_payment_lang = 'en'
        usd = self._usd_at(0.376)
        converted = self._create_transaction(
            'redirect', reference=self._new_reference(), amount=100.0, currency_id=usd.id,
        )
        self._initiate(converted)
        self.assertEqual(converted.epays_lang, 'en')
        self.assertEqual(converted.epays_rate_source, const.RATE_SOURCE)

    def test_a_server_that_does_not_report_the_language_leaves_the_stored_one(self):
        tx = self._create_epays_transaction(epays_lang='ar')
        self.assertEqual(tx._epays_get_payment_lang({}), 'ar')
        self.assertEqual(tx._epays_get_payment_lang({'paymentLang': 'en'}), 'en')

    def test_a_retry_in_another_language_opens_a_new_page_on_any_server(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, state='pending', epays_lang='ar')
        pending = self._payment_data(first, status='Pending', result='')
        pending.pop('paymentLang', None)
        voided = {'paymentId': first.provider_reference, 'status': 'Voided'}
        tx, calls, _values = self._pay_again(
            [pending], delete_response=voided, lang='en_US', **self._order_values(order)
        )
        self.assertEqual([method for method, _path in calls], ['GET', 'DELETE', 'POST'])
        self.assertEqual(tx.epays_lang, 'en')

    def test_s1_an_unchanged_payment_at_the_bank_keeps_its_link(self):
        """ Nothing is voided, and the customer returns to the page of the same payment. """
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, state='pending')
        at_bank = self._payment_data(first, status='Pending', result='', transactionId='TXN-1')
        tx, calls, _values = self._pay_again([at_bank], **self._order_values(order))
        self.assertEqual([method for method, _path in calls], ['GET'], "No void, no new payment.")
        self.assertEqual(tx.provider_reference, first.provider_reference)
        self.assertTrue(tx.epays_at_bank, "The state read comes along with the payment.")

    def test_s2_a_replaced_payment_at_the_bank_outlives_its_link(self):
        """ Its link expired long ago, and the bank can still answer. """
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(
            order, state='cancel', epays_at_bank=True,
            epays_expires_at=fields.Datetime.now() - timedelta(days=1),
        )
        at_bank = self._payment_data(first, status='Pending', result='', transactionId='TXN-1')
        with self.assertRaises(ValidationError) as error:
            self._pay_again([at_bank], **self._order_values(order))
        self.assertIn("still being confirmed", error.exception.args[0])

        voided = self._payment_data(first, status='Voided', result='', transactionId=None)
        tx, _calls, _values = self._pay_again([voided], **self._order_values(order))
        self.assertEqual(tx.provider_reference, 'PAY-NEW', "The bank answered: no longer held up.")
        self.assertFalse(first.epays_at_bank)

    def test_s20_the_payment_request_describes_the_order(self):
        """ S20: as the WooCommerce plugin does. """
        if not self._sale_installed():
            self.skipTest("sale is not installed")
        order = self._create_sale_order()
        tx = self._create_transaction(
            'redirect', reference=False, sale_order_ids=[Command.set(order.ids)],
            amount=order.amount_total, currency_id=order.currency_id.id,
        )
        calls, _values = self._initiate(tx)
        payload = calls[0]['payload']
        self.assertEqual(payload['description'], f'Order {order.name}')
        lines = payload['order']['lineItems']
        self.assertTrue(lines)
        self.assertEqual(
            sum(float(line['unitPrice']) * line['quantity'] for line in lines)
            + float(payload['order'].get('shippingAmount', 0)),
            float(payload['amount']),
        )

    def test_a_partial_payment_sends_no_order(self):
        if not self._sale_installed():
            self.skipTest("sale is not installed")
        order = self._create_sale_order()
        tx = self._create_transaction(
            'redirect', reference=False, sale_order_ids=[Command.set(order.ids)],
            amount=order.amount_total / 2, currency_id=order.currency_id.id,
        )
        self.assertEqual(tx._epays_get_order_summary(), {})

    def test_the_references_are_compared_after_trimming(self):
        tx = self._create_epays_transaction(epays_udf2='S1', epays_udf3='')
        data = self._payment_data(tx)
        data['customFields']['udf2'] = ' S1 '
        self.assertEqual(tx._epays_get_mismatches(data), [])

    def test_s23_the_sync_follows_the_pages_of_gateways(self):
        """ S23: a row the sync did not see would be deleted. """
        pages = [
            {'gateways': [{'subscriptionId': 'G1', 'gatewayTypeId': 2, 'status': 'active'}],
             'nextCursor': 'next-page'},
            {'gateways': [{'subscriptionId': 'G2', 'gatewayTypeId': 4, 'status': 'active'}],
             'nextCursor': None},
        ]
        paths = []

        def handler(provider, method, path, **kwargs):
            paths.append(path)
            return pages.pop(0)

        with self._patch_make_request(handler):
            gateways = self.provider._epays_fetch_gateways('sandbox', 'shop.example.com')
        self.assertEqual(paths, [
            '/api/v2/gateways?limit=100', '/api/v2/gateways?limit=100&cursor=next-page',
        ])
        self.assertEqual([gateway['subscriptionId'] for gateway in gateways], ['G1', 'G2'])

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
        self.assertEqual(self.provider._epays_read_secret('epays_master_key'), 'prod-master-key')

        self.provider.epays_sandbox_master_key = False
        self.assertFalse(admin_provider.read(['epays_sandbox_master_key'])[0][
            'epays_sandbox_master_key'
        ], "An empty key reads as empty, so the form shows it must be set.")

    def test_saving_the_form_back_keeps_the_key_and_a_new_key_replaces_it(self):
        self.provider.write({'epays_master_key': '********'})  # A client saving what it read.
        self.assertEqual(self.provider._epays_read_secret('epays_master_key'), 'prod-master-key')
        self.provider.write({'epays_master_key': '  new-master-key\n'})  # Pasted.
        self.assertEqual(self.provider._epays_read_secret('epays_master_key'), 'new-master-key')

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
            lambda: self.provider.export_data(['epays_master_key']),
            lambda: self._create_epays_transaction().export_data(
                ['provider_id/epays_sandbox_master_key']
            ),
        ):
            with self.subTest(attempt=attempt), self.assertRaises(AccessError):
                attempt()
        self.assertIn(self.provider, Provider.search([('code', '=', 'epays')], order='name'))
        self.assertEqual(self.provider.export_data(['name'])['datas'], [[self.provider.name]])

    def test_s21_an_unreadable_key_is_not_offered_to_customers(self):
        """ A key that cannot be read is no key: a customer must not reach a failing payment. """
        self.provider.write({'state': 'test', 'is_published': True})
        self.provider.flush_recordset()
        Provider = self.env['payment.provider'].sudo()

        def get_compatible_providers(report=None):
            return Provider._get_compatible_providers(
                self.company_id, self.partner.id, self.amount, currency_id=self.currency.id,
                report=report,
            )

        self.assertIn(self.provider, get_compatible_providers())
        report = {}
        with patch.object(
            type(self.provider), '_epays_get_key_material', return_value='another-secret',
        ), mute_logger(PROVIDER_LOGGER):
            self.provider.invalidate_recordset()
            self.assertNotIn(self.provider, get_compatible_providers(report))
        self.assertFalse(report['providers'][self.provider]['available'])

    def test_the_payment_link_stays_out_of_the_logs(self):
        """ Whoever holds the link can open the payment page, and the response has customer data.
        """
        tx = self._create_transaction('redirect', reference='LOG-1')
        response = {'redirect': 'https://testapi.epays.io/pay/P1?token=the-token'}
        with self._patch_make_request(lambda *args, **kwargs: response), \
                self.assertLogs(TX_LOGGER, level='WARNING') as logs, \
                mute_logger(PAYMENT_TX_LOGGER), self.assertRaises(ValidationError):
            tx._get_processing_values()
        self.assertFalse([r for r in logs.records if 'the-token' in r.getMessage()])

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
            tx._handle_notification_data('epays', self._notification_data(tx))
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
        done_tx = self._create_epays_transaction(state='done')
        taken_over_tx = self._create_epays_transaction(state='cancel')
        self._create_epays_transaction(
            state='draft', provider_reference=taken_over_tx.provider_reference
        )
        for tx, state in ((done_tx, 'done'), (taken_over_tx, 'cancel')):
            with self.subTest(state=state):
                with self._patch_make_request(lambda *args, **kwargs: {}) as make_request_mock, \
                        mute_logger(TX_LOGGER):
                    tx._process_notification_data(self._notification_data(tx))
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
        with self.assertRaises(ValidationError, msg="Nothing says which of the two."):
            lookup('epays', {'paymentId': 'PAY-SHARED'})

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
        self.assertEqual(calls[0]['path'], '/api/v2/gateways?limit=100')
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

    def test_sync_gateways_reads_the_v2_names_of_2026_10_18(self):
        """ `merchantGateway` became `subscriptionId` and the type `gatewayId` became
        `gatewayTypeId`; statuses are in snake case. """
        _calls, action = self._sync([
            {'subscriptionId': self.card_gateway_ref, 'merchantApiId': 'A', 'gatewayTypeId': 2,
             'gatewayName': "Credit Card", 'gatewayClass': 'MPGS', 'status': 'active'},
            {'subscriptionId': '11112222', 'gatewayTypeId': 6, 'gatewayName': "BenefitPay",
             'status': 'inactive'},
            {'subscriptionId': '77778888', 'gatewayTypeId': 7, 'gatewayName': "Zoho"},
        ])
        rows = {row.gateway_ref: row for row in self._gateways('sandbox')}
        self.assertEqual(set(rows), {self.card_gateway_ref, '11112222'})
        self.assertEqual(rows[self.card_gateway_ref].gateway_type_id, 2)
        self.assertTrue(rows[self.card_gateway_ref].is_active)
        self.assertEqual(rows['11112222'].payment_method_id, self.benefit_pay_method)
        self.assertFalse(rows['11112222'].is_active)
        self.assertEqual(action['params']['type'], 'success')

    @mute_logger('odoo.addons.payment_epays.models.payment_provider')
    def test_sync_gateways_keeps_the_rows_when_it_cannot_read_the_list(self):
        """ A list in names this version does not know would otherwise drop every row, and with
        them the payment methods offered at checkout. """
        rows = self._gateways('sandbox')
        _calls, action = self._sync([
            {'gatewaySubscription': '12345678', 'gatewayKind': 2, 'gatewayName': "Credit Card"},
        ])
        self.assertEqual(self._gateways('sandbox'), rows)
        self.assertEqual(action['params']['type'], 'warning')
        self.assertIn('cannot read', action['params']['message'])

        # An empty list is a merchant without gateways: the rows go.
        self._sync([])
        self.assertFalse(self._gateways('sandbox'))

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

    #=== A payment request ePays answered too late ===#

    def _unanswered(self, udf2, age=timedelta(minutes=10)):
        """ An unanswered request recorded `age` ago. """
        request = self.env['payment.epays.unanswered.request'].create({
            'provider_id': self.provider.id, 'environment': 'sandbox',
            'merchant_domain': 'shop.example.com', 'udf2': udf2, 'reference': udf2,
        })
        self.env.cr.execute(
            "UPDATE payment_epays_unanswered_request SET create_date = %s WHERE id = %s",
            [fields.Datetime.now() - age, request.id],
        )
        request.invalidate_recordset(['create_date'])
        return request

    def _run_orphan_cron(self, listing=None, list_error=None, void_error=None):
        calls = []

        def handler(provider, method, path, **kwargs):
            calls.append((method, path))
            if method == 'GET' and list_error:
                raise list_error
            if method == 'DELETE' and void_error:
                raise void_error
            return listing if method == 'GET' else {'status': 'voided'}

        with self._patch_make_request(handler), \
                patch.object(self.env.cr, 'commit'), patch.object(self.env.cr, 'rollback'), \
                mute_logger('odoo.addons.payment_epays.models.payment_epays_unanswered_request'):
            self.env['payment.epays.unanswered.request']._cron_void_orphan_payments()
        return calls

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER,
                 'odoo.addons.payment_epays.models.payment_epays_unanswered_request')
    def test_a_payment_request_answered_too_late_is_recorded_for_its_orders(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        Request = self.env['payment.epays.unanswered.request']

        def pay_and_fail(error):
            """ Not `assertRaises`: Odoo's rolls back the database changes of its block, and the
            request is recorded precisely to survive the rollback of a failed payment. """
            def handler(provider, method, path, **kwargs):
                raise error

            tx = self._create_transaction('redirect', reference=False, **self._order_values(order))
            with self._patch_make_request(handler):
                try:
                    tx._get_processing_values()
                except ValidationError:
                    return tx
            return self.fail("The payment request did not fail.")

        pay_and_fail(EpaysApiError("ePays: unreachable", connection_error="refused"))
        self.assertFalse(
            Request.search([('udf2', '=', order.name)]), "A request never sent created nothing."
        )

        tx = pay_and_fail(EpaysApiError(
            "ePays: unreachable", connection_error="Read timed out.", timed_out=True
        ))
        request = Request.search([('udf2', '=', order.name)])
        self.assertEqual(len(request), 1)
        self.assertEqual(request.environment, 'sandbox')
        self.assertEqual(request.merchant_domain, 'shop.example.com')
        self.assertEqual(request.reference, tx.reference)

    def test_the_cron_voids_the_payments_left_by_an_unanswered_request(self):
        """ Only the pending payments of the order, old enough, that no transaction holds. """
        self._skip_without_sale()
        order = self._create_sale_order()
        held = self._earlier_attempt(order, state='pending')
        request = self._unanswered(order.name)
        now = datetime.now(timezone.utc)
        old, young = now - timedelta(minutes=5), now - timedelta(seconds=30)

        def item(payment_id, created_at, status='pending', udf2=order.name):
            return {'paymentId': payment_id, 'udf2': udf2, 'status': status,
                    'createdAt': created_at.isoformat(timespec='seconds')}

        # An older ePays server: PascalCase status, a timestamp without offset (Bahrain time).
        old_server_item = {
            'paymentId': 'ORPHAN-OLD-SERVER', 'udf2': order.name, 'status': 'Pending',
            'createdAt': (old + const.EPAYS_TIMEZONE_OFFSET).replace(tzinfo=None).isoformat(
                timespec='seconds'
            ),
        }
        calls = self._run_orphan_cron({'payments': [
            item('ORPHAN', old),
            old_server_item,
            item(held.provider_reference, old),  # A transaction holds it.
            item('YOUNG', young),  # A click may still be waiting for it.
            item('OTHER-ORDER', old, udf2='S99999'),
            item('PAID', old, status='completed'),
            item('NO-DATE', old) | {'createdAt': ''},
        ]})

        self.assertEqual(calls[0][0], 'GET')
        self.assertIn('status=pending', calls[0][1])
        self.assertIn(f'udf2={order.name}', calls[0][1])
        self.assertEqual(
            [path for method, path in calls if method == 'DELETE'],
            ['/api/v2/Payments/ORPHAN', '/api/v2/Payments/ORPHAN-OLD-SERVER'],
        )
        self.assertEqual(
            [path for method, path in calls if method == 'POST'], [
                '/api/v2/Payments/ORPHAN/acknowledgement',
                '/api/v2/Payments/ORPHAN-OLD-SERVER/acknowledgement',
            ], "S16: a voided payment took no money.",
        )
        self.assertFalse(request.exists(), "A handled request is forgotten.")

    def test_the_cron_voids_nothing_on_a_server_that_ignores_the_list_filters(self):
        """ The sandbox of 2026-10-20 ignores `status` and `udf2` and lists other payments, without
        those fields: a payment is voided only when its own fields say it is a pending one of the
        order, so such a list voids nothing. """
        request = self._unanswered('S00111')
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec='seconds')
        calls = self._run_orphan_cron({'payments': [
            {'paymentId': '832582023', 'createdAt': old},
            {'paymentId': '853484944', 'createdAt': old},
        ]})
        self.assertEqual([method for method, _path in calls], ['GET'])
        self.assertFalse(request.exists())

    def test_s24_an_orphan_in_flight_is_kept_until_epays_allows_the_void(self):
        """ S24: a refused orphan stays payable, so forgetting the request would leave it open. """
        request = self._unanswered('S00112')
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec='seconds')
        listing = {'payments': [
            {'paymentId': 'ORPHAN', 'udf2': 'S00112', 'status': 'pending', 'createdAt': old},
        ]}
        calls = self._run_orphan_cron(
            listing, void_error=EpaysApiError("ePays: refused", http_status=409)
        )
        self.assertEqual([method for method, _path in calls], ['GET', 'DELETE'])
        self.assertTrue(request.exists())

        calls = self._run_orphan_cron(listing)
        self.assertEqual([method for method, _path in calls], ['GET', 'DELETE', 'POST'])
        self.assertFalse(request.exists())

    def test_the_cron_waits_retries_and_gives_up_on_unanswered_requests(self):
        young = self._unanswered('S-YOUNG', age=timedelta(seconds=30))
        unreachable = self._unanswered('S-UNREACHABLE')
        too_old = self._unanswered('S-TOO-OLD', age=const.UNANSWERED_REQUEST_MAX_AGE + timedelta(1))

        calls = self._run_orphan_cron(list_error=EpaysApiError("ePays: unreachable"))

        self.assertEqual(calls, [('GET', calls[0][1])], "Only the request old enough is looked up.")
        self.assertIn('udf2=S-UNREACHABLE', calls[0][1])
        self.assertTrue(young.exists(), "A click may still be waiting for its answer.")
        self.assertTrue(unreachable.exists(), "ePays could not be reached: retried next run.")
        self.assertFalse(too_old.exists(), "Given up after the maximum age.")

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
            # An older ePays server, and a first attempt the addon no longer retires, as its link
            # is over: ePays hands its payment back.
            tx_first.epays_expires_at = fields.Datetime.now() - timedelta(hours=1)
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
                'epays', self._notification_data(tx_second)
            ),
            tx_second,
        )

        url = self._build_url(f'{EpaysController._return_url}/sandbox')
        with self._patch_payment_details(self._payment_data(tx_second)), \
                mute_logger(CONTROLLER_LOGGER, TX_LOGGER, PAYMENT_TX_LOGGER):
            response = self.opener.get(
                url, params={'paymentId': self.payment_id}, allow_redirects=False
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(tx_second.state, 'done')
        self.assertEqual(tx_first.state, 'cancel')
        with self._patch_make_request(lambda *args, **kwargs: {}), \
                mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
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
        self.assertEqual(lookup('epays', self._notification_data(tx_open)), tx_open)

        tx_open.state = 'cancel'
        self.assertEqual(
            lookup('epays', self._notification_data(tx_open)), tx_cancelled_newest,
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

    def _pay_again(self, get_responses=(), delete_response=None, lang=None, **tx_values):
        """ Click on Pay again: create a new transaction and render its redirect form.

        :param list get_responses: The successive answers to GET, as dicts or exceptions.
        :param delete_response: The answer to DELETE, as a dict or an exception.
        :param str lang: The language of the website the customer clicks on, if not the default.
        :return: The new transaction, the calls made to ePays and the processing values.
        """
        tx = self._create_transaction('redirect', reference=False, **tx_values)
        if lang:
            tx = tx.with_context(lang=lang)
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

    def _recording_handler(self, calls, data):
        """ Return a handler recording the HTTP methods, answering GET with the given data. """
        def handler(provider, method, path, **kwargs):
            calls.append(method)
            return data if method == 'GET' else {'status': 'Voided'}
        return handler

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
        url = self._build_url(f'{EpaysController._return_url}/sandbox')
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
        rows = [
            # (method of the first attempt's gateway, method chosen now, expected new gateway)
            (self.card_gateway_ref, self.apple_pay_method, self.apple_pay_gateway_ref),
            (self.card_gateway_ref, self.epays_method, False),  # ePays: choose on the page.
            (False, self.apple_pay_method, self.apple_pay_gateway_ref),
        ]
        for first_gateway, method, expected_gateway in rows:
            with self.subTest(first=first_gateway, now=method.code):
                order = self._create_sale_order()
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

    def test_pay_again_in_another_language_opens_a_new_page(self):
        """ The customer switched the website's language: the page in the old language is voided
        and a new one opens in the new language, so the page and the messages match the site. """
        self._skip_without_sale()
        self._activate_arabic()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, state='pending')
        pending = self._payment_data(first, status='Pending', result='', paymentLang='en')
        voided = {'paymentId': first.provider_reference, 'status': 'Voided'}

        tx, calls, _values = self._pay_again(
            [pending], delete_response=voided, lang='ar_001', **self._order_values(order)
        )

        self.assertEqual([method for method, _path in calls], ['GET', 'DELETE', 'POST'])
        self.assertEqual(first.state, 'cancel')
        # Written in the language of the click, here the Arabic website's.
        self.assertEqual(
            first.state_message, f"ePays: استُبدلت بالمعاملة {tx.reference} (تغيّرت اللغة).",
        )
        self.assertEqual(tx.provider_reference, 'PAY-NEW')

    def test_pay_again_in_the_same_language_reuses_its_page(self):
        self._skip_without_sale()
        self._activate_arabic()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, state='pending')
        pending = self._payment_data(first, status='Pending', result='', paymentLang='ar')

        tx, calls, _values = self._pay_again([pending], lang='ar_001', **self._order_values(order))

        self.assertEqual([method for method, _path in calls], ['GET'])
        self.assertEqual(tx.provider_reference, first.provider_reference)

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

    def test_s6_a_refused_void_on_an_order_that_was_paid(self):
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

    def test_s24_a_refused_void_that_is_still_pending_is_no_connection_failure(self):
        """ ePays answered: the customer is asked to try again, and no second payment is made. """
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
        self.assertIn("could not be closed yet", error.exception.args[0])
        self.assertNotIn("connection", error.exception.args[0])
        self.assertEqual(first.state, 'draft')

    def test_s24_a_payment_in_flight_counts_as_with_the_gateway(self):
        for label, overrides, expected in (
            ("in flight", {'inFlight': True}, True),
            ("gateway transaction", {'transactionId': 'TXN-1'}, True),
            ("not in flight", {'inFlight': False}, False),
            ("an older server", {}, False),
            ("not a boolean", {'inFlight': 'true'}, False),
            ("finished", {'inFlight': True, 'status': 'completed', 'result': 'CAPTURED'}, False),
            ("declined, paid again", {'inFlight': True, 'status': 'failed'}, True),
            ("declined", {'status': 'failed', 'transactionId': 'TXN-1'}, False),
        ):
            with self.subTest(label):
                data = {'status': 'pending', 'result': '', **overrides}
                self.assertEqual(is_at_bank(data), expected)

    def test_s24_a_retry_waits_for_a_payment_in_flight(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, amount=order.amount_total - 10)
        in_flight = self._payment_data(
            first, status='Pending', result='', transactionId=None, inFlight=True
        )
        with self.assertRaises(ValidationError) as error:
            self._pay_again(
                [in_flight],
                delete_response=AssertionError("No void: ePays would refuse it."),
                **self._order_values(order),
            )
        self.assertIn("still being confirmed", error.exception.args[0])
        self.assertEqual(first.state, 'draft')

    def test_s24_a_void_refused_while_the_customer_pays_is_no_connection_failure(self):
        """ S24: the customer started paying between the read and the void. """
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, amount=order.amount_total - 10)
        pending = self._payment_data(first, status='Pending', result='', transactionId=None)

        with self.assertRaises(ValidationError) as error:
            self._pay_again(
                [pending, dict(pending, inFlight=True)],
                delete_response=EpaysApiError("ePays: refused", http_status=409),
                **self._order_values(order),
            )
        self.assertIn("still being confirmed", error.exception.args[0])
        self.assertEqual(first.state, 'draft')

    def test_s24_a_declined_payment_paid_again_is_no_connection_failure(self):
        """ S24: ePays keeps a declined link payable, and refuses its void while it is paid. """
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, state='error')
        failed = self._payment_data(first, status='Failed', result='DECLINED', transactionId=None)

        with self.assertRaises(ValidationError) as error:
            self._pay_again(
                [failed, dict(failed, inFlight=True)],
                delete_response=EpaysApiError("ePays: refused", http_status=409),
                **self._order_values(order),
            )
        self.assertIn("still being confirmed", error.exception.args[0])

    def test_s3_a_retry_when_the_earlier_payment_was_paid(self):
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
        self.assertIn("could not be closed yet", error.exception.args[0])
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

    def test_s4_a_retry_after_a_failed_payment_voids_it_first(self):
        """ S4: ePays keeps a declined link payable. """
        self._skip_without_sale()
        for state in ('pending', 'error'):
            with self.subTest(state=state):
                order = self._create_sale_order()
                first = self._earlier_attempt(order, state=state)
                failed = self._payment_data(first, status='Failed', result='DECLINED')
                voided = {'paymentId': first.provider_reference, 'status': 'Voided'}

                tx, calls, _values = self._pay_again(
                    [failed], delete_response=voided, **self._order_values(order)
                )

                self.assertEqual([method for method, _path in calls], ['GET', 'DELETE', 'POST'])
                self.assertEqual(first.state, 'cancel')
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

    def test_s5_a_retry_voids_the_live_attempts_it_cannot_reuse(self):
        """ S5: a link about to expire can still be paid. """
        self._skip_without_sale()
        now = fields.Datetime.now()
        for label, values in (
            ("link expiring within 5 minutes", {'epays_expires_at': now + timedelta(minutes=3)}),
            ("link in its grace period", {'epays_expires_at': now - timedelta(minutes=10)}),
            ("created before the payment page was stored", {'epays_payment_url': False}),
        ):
            with self.subTest(label):
                order = self._create_sale_order()
                first = self._earlier_attempt(order, **values)
                pending = self._payment_data(first, status='Pending', result='')
                voided = {'paymentId': first.provider_reference, 'status': 'Voided'}
                tx, calls, _values = self._pay_again(
                    [pending], delete_response=voided, **self._order_values(order)
                )
                self.assertEqual([method for method, _path in calls], ['GET', 'DELETE', 'POST'])
                self.assertEqual(first.state, 'cancel')
                self.assertEqual(tx.provider_reference, 'PAY-NEW')

    def test_pay_again_leaves_the_attempts_that_are_not_live(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        other_order = self._create_sale_order()
        now = fields.Datetime.now()
        rows = [
            ("link over", order, {'epays_expires_at': now - timedelta(minutes=31)}),
            ("other environment", order, {'epays_environment': 'production'}),
            ("other order", other_order, {}),
            ("refused", order, {'state': 'error', 'epays_response_code': '00'}),
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
                first.epays_expires_at = now - timedelta(days=1)
                tx.state = 'cancel'
                tx.epays_expires_at = now - timedelta(days=1)

    def test_pay_again_voids_every_live_attempt_and_reuses_the_newest(self):
        self._skip_without_sale()
        order = self._create_sale_order()
        oldest = self._earlier_attempt(order, state='error')
        newest = self._earlier_attempt(order, state='pending')
        responses = {
            oldest.provider_reference: self._payment_data(oldest, status='Failed'),
            newest.provider_reference: self._payment_data(newest, status='Pending', result=''),
        }
        calls = []

        def handler(provider, method, path, **kwargs):
            calls.append((method, path.rsplit('/', 1)[1]))
            if method == 'DELETE':
                return {'status': 'Voided'}
            return responses[path.rsplit('/', 1)[1]]

        tx = self._create_transaction('redirect', reference=False, **self._order_values(order))
        with self._patch_make_request(handler), mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            tx._get_processing_values()

        self.assertEqual(calls, [
            ('GET', newest.provider_reference),
            ('GET', oldest.provider_reference),
            ('DELETE', oldest.provider_reference),
        ])
        self.assertEqual(oldest.state, 'cancel')
        self.assertEqual(newest.state, 'cancel')
        self.assertEqual(tx.provider_reference, newest.provider_reference)

    def test_s1_a_changed_payment_at_the_bank_stops_the_retry(self):
        """ S1: a void would not stop the bank. Reusing its page is harmless: nothing is voided. """
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, state='pending', amount=order.amount_total - 10)
        at_bank = self._payment_data(first, status='Pending', result='', transactionId='TXN-1')
        methods = []

        def handler(provider, method, path, **kwargs):
            methods.append(method)
            return at_bank

        tx = self._create_transaction('redirect', reference=False, **self._order_values(order))
        with self._patch_make_request(handler), mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER), \
                self.assertRaises(ValidationError) as error:
            tx._get_processing_values()
        self.assertIn("still being confirmed", error.exception.args[0])
        self.assertEqual(methods, ['GET'], "Neither voided nor replaced.")
        self.assertEqual(first.state, 'pending')

    def test_s2_a_retry_reads_the_replaced_payments_before_voiding_anything(self):
        """ S2, S3: raising does not undo a void, so nothing is voided before the check. """
        self._skip_without_sale()
        order = self._create_sale_order()
        for label, overrides, message in (
            ("at the bank", {'status': 'Pending', 'result': '', 'transactionId': 'TXN-1'},
             "still being confirmed"),
            ("completed", {}, "already been paid"),
        ):
            with self.subTest(label):
                replaced = self._earlier_attempt(order, state='cancel')
                current = self._earlier_attempt(order, amount=order.amount_total - 10)
                data = self._payment_data(replaced, **overrides)
                methods = []
                tx = self._create_transaction(
                    'redirect', reference=False, **self._order_values(order)
                )
                with self._patch_make_request(self._recording_handler(methods, data)), \
                        mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER), \
                        self.assertRaises(ValidationError) as error:
                    tx._get_processing_values()
                self.assertIn(message, error.exception.args[0])
                self.assertEqual(methods, ['GET'])
                self.assertEqual(current.state, 'draft')
                replaced.epays_expires_at = fields.Datetime.now() - timedelta(days=1)
                current.state = 'cancel'
                current.epays_expires_at = fields.Datetime.now() - timedelta(days=1)

    def test_no_new_payment_while_a_refused_charge_waits_for_the_merchant(self):
        """ S12, S14: and released by the merchant, the order can be paid again. """
        self._skip_without_sale()
        order = self._create_sale_order()
        message = "We could not confirm your payment."
        held = self._earlier_attempt(
            order, state='error', state_message=message, epays_response_code='00',
            epays_review_needed=True,
        )
        tx = self._create_transaction('redirect', reference=False, **self._order_values(order))
        with self._patch_make_request(lambda *args, **kwargs: {}) as make_request_mock, \
                mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER), \
                self.assertRaises(ValidationError) as error:
            tx._get_processing_values()
        self.assertEqual(error.exception.args[0], message)
        make_request_mock.assert_not_called()

        held.action_epays_release_review()
        tx, calls, _values = self._pay_again(**self._order_values(order))
        self.assertEqual([method for method, _path in calls], ['POST'])

    def test_s8_the_cron_voids_the_link_of_a_cancelled_order(self):
        """ S8: with this addon's dependencies, the scheduled check finds them. """
        self._skip_without_sale()
        for state, status in (('draft', 'Pending'), ('error', 'Failed')):
            for transaction_id in ('', 'TXN-1'):
                with self.subTest(state=state, transaction_id=transaction_id):
                    order = self._create_sale_order()
                    tx = self._earlier_attempt(order, state=state)
                    self._set_create_date(tx, fields.Datetime.now() - timedelta(minutes=10))
                    order._action_cancel()
                    data = self._payment_data(
                        tx, status=status, result='', transactionId=transaction_id or None,
                    )
                    calls = []
                    self._run_cron(self._recording_handler(calls, data))
                    self.assertEqual(calls, ['GET', 'DELETE'])
                    self.assertEqual(tx.state, 'cancel')
                    self.assertTrue(tx.epays_watch_until, "It can still be charged.")
                    tx.epays_watch_until = False  # Not read by the next row's check.

    def _refused_void_handler(self, data):
        """ Return a handler answering GET with the payment and refusing the void. """
        def handler(provider, method, path, **kwargs):
            if method == 'DELETE':
                raise EpaysApiError("ePays: refused", http_status=409)
            return data
        return handler

    def test_s24_the_expiry_void_is_tried_again_once_epays_allows_it(self):
        tx = self._create_epays_transaction(
            state='pending', epays_expires_at=fields.Datetime.now() - timedelta(minutes=31)
        )
        self._set_create_date(tx, fields.Datetime.now() - timedelta(hours=1))
        data = self._payment_data(
            tx, status='Pending', result='', transactionId=None, inFlight=True
        )
        self._run_cron(self._refused_void_handler(data))
        self.assertEqual(tx.state, 'pending')
        self.assertTrue(tx.epays_at_bank)

        calls = []
        self._run_cron(self._recording_handler(calls, dict(data, inFlight=False)))
        self.assertEqual(calls, ['GET', 'DELETE'])
        self.assertEqual(tx.state, 'cancel')

    def test_s24_a_cancelled_order_in_flight_is_voided_once_epays_allows_it(self):
        """ S8, S24: and watched as at the bank, as the money may still move. """
        self._skip_without_sale()
        order = self._create_sale_order()
        tx = self._earlier_attempt(order, state='draft')
        self._set_create_date(tx, fields.Datetime.now() - timedelta(minutes=10))
        order._action_cancel()
        data = self._payment_data(
            tx, status='Pending', result='', transactionId=None, inFlight=True
        )
        self._run_cron(self._refused_void_handler(data))
        self.assertEqual(tx.state, 'draft')
        self.assertTrue(tx.epays_at_bank)

        self._run_cron(self._recording_handler([], data))
        self.assertEqual(tx.state, 'cancel')
        self.assertGreater(tx.epays_watch_until, fields.Datetime.now() + timedelta(days=2))

    def test_the_cron_leaves_the_link_of_an_order_that_is_not_all_cancelled(self):
        self._skip_without_sale()
        order, other_order = self._create_sale_order(), self._create_sale_order()
        tx = self._earlier_attempt(
            state='draft', sale_order_ids=[Command.set((order | other_order).ids)]
        )
        self._set_create_date(tx, fields.Datetime.now() - timedelta(minutes=10))
        order._action_cancel()
        calls = []

        def handler(provider, method, path, **kwargs):
            calls.append(method)
            return self._payment_data(tx, status='Pending', result='')

        self._run_cron(handler)
        self.assertEqual(calls, ['GET'])
        self.assertEqual(tx.state, 'draft')

    def test_s18_the_status_page_reads_epays_for_a_customer_waiting(self):
        """ S18: within the limit of the public routes. """
        tx = self._create_epays_transaction(state='pending')
        data = self._payment_data(tx)
        with self._patch_payment_details(data) as make_request_mock, \
                mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER):
            tx._epays_refresh_for_status_page()
        self.assertEqual(tx.state, 'done')
        self.assertEqual(make_request_mock.call_count, 1)

        tx = self._create_epays_transaction(state='pending')
        with patch.object(type(tx), '_epays_may_read_payment', return_value=False), \
                self._patch_payment_details(data) as make_request_mock:
            tx._epays_refresh_for_status_page()
        make_request_mock.assert_not_called()

    def test_the_status_page_survives_a_failed_read(self):
        tx = self._create_epays_transaction(state='pending')

        def handler(provider, method, path, **kwargs):
            raise EpaysApiError("unreachable", connection_error="timeout")

        with self._patch_make_request(handler), mute_logger(TX_LOGGER):
            tx._epays_refresh_for_status_page()
        self.assertEqual(tx.state, 'pending')
        self.assertEqual(len(tx._epays_get_recent_reads()), 1, "It counts: the next poll retries.")

    def test_s18_the_status_page_keeps_a_customer_waiting_on_the_bank(self):
        """ S18: Odoo's page leaves on `cancel`, and the bank can still take the money. """
        tx = self._create_epays_transaction(state='cancel', epays_at_bank=True)
        at_bank = self._payment_data(tx, status='Pending', result='', transactionId='TXN-1')
        polled_values = {
            'provider_code': 'epays', 'state': 'cancel', 'landing_route': '/my/orders',
        }
        controller = EpaysPostProcessing()
        for data, expected_state in ((at_bank, 'pending'), (self._payment_data(
            tx, status='Voided', result='', transactionId=None,
        ), 'cancel')):
            with patch.object(
                EpaysPostProcessing, '_get_monitored_transaction', return_value=tx,
            ), patch.object(
                PaymentPostProcessing, 'poll_status', return_value=dict(polled_values),
            ), self._patch_payment_details(data), mute_logger(TX_LOGGER):
                values = controller.poll_status()
            self.assertEqual(values['state'], expected_state)

    def test_the_status_page_does_not_say_cancelled_while_the_bank_decides(self):
        """ S18: the page shows the state of the transaction, not what the poll answers. """
        tx = self._create_epays_transaction(
            state='cancel', epays_at_bank=True, state_message="ePays: Replaced by transaction X.",
        )

        def render():
            return str(self.env['ir.qweb']._render('payment.state_header', {
                'tx': tx, 'is_processing': True, 'is_html_empty': is_html_empty,
            }))

        html = render()
        self.assertIn("still confirming", html)
        self.assertNotIn("Replaced by", html)
        self.assertNotIn("alert-danger", html)
        tx.epays_at_bank = False
        html = render()
        self.assertIn("Replaced by", html)
        self.assertIn("alert-danger", html)

    def _old_tx(self, state, **values):
        """ Return a transaction that the scheduled check no longer reads. """
        now = fields.Datetime.now()
        tx = self._create_epays_transaction(
            state=state, epays_expires_at=now - timedelta(days=8), **values
        )
        self._set_create_date(tx, now - timedelta(days=9))
        return tx

    def test_a_status_page_read_that_cannot_run_goes_back_to_the_scheduled_check(self):
        """ S10: the customer waits on a payment that the scheduled check no longer reads. """
        now = fields.Datetime.now()

        def unreachable(provider, method, path, **kwargs):
            raise EpaysApiError("unreachable", connection_error="timed out", timed_out=True)

        for is_limited in (False, True):
            with self.subTest(is_limited=is_limited):
                tx = self._old_tx('pending')
                with self._patch_make_request(unreachable), mute_logger(TX_LOGGER), patch.object(
                    type(tx), '_epays_may_read_payment', return_value=not is_limited,
                ):
                    tx._epays_refresh_for_status_page()
                self.assertGreaterEqual(tx.epays_watch_until, now + const.LATE_CHARGE_WINDOW)

    def test_a_completed_payment_that_cannot_be_applied_goes_back_to_the_scheduled_check(self):
        """ S10: another module can refuse the completion, and ePays may not call again. """
        now = fields.Datetime.now()
        tx = self._old_tx('cancel', epays_watch_until=now - timedelta(days=1))
        with self._patch_payment_details(self._payment_data(tx)), patch.object(
            type(tx), '_set_done', side_effect=ValidationError("refused"),
        ), mute_logger(TX_LOGGER):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(tx.state, 'cancel')
        self.assertGreaterEqual(tx.epays_watch_until, now + const.LATE_CHARGE_WINDOW)

    def test_a_completed_payment_the_cron_cannot_apply_is_read_again(self):
        """ S9: the last read of its watch must not be the end of it. """
        now = fields.Datetime.now()
        tx = self._old_tx('cancel', epays_watch_until=now + timedelta(minutes=1))
        data = self._payment_data(tx)
        with patch.object(type(tx), '_set_done', side_effect=ValidationError("refused")):
            self._run_cron(lambda *args, **kwargs: data)
        self.assertEqual(tx.state, 'cancel')
        self.assertGreaterEqual(tx.epays_watch_until, now + const.LATE_CHARGE_WINDOW)

    def test_a_payment_still_at_the_bank_past_its_watch_is_watched_again(self):
        """ S9: read by ePays's notification or the merchant, as the scheduled check stopped. """
        now = fields.Datetime.now()
        tx = self._old_tx(
            'cancel', epays_at_bank=True, epays_watch_until=now - timedelta(hours=1)
        )
        data = self._payment_data(
            tx, status='Pending', result='', transactionId=None, inFlight=True
        )
        with self._patch_payment_details(data), mute_logger(TX_LOGGER):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertGreaterEqual(tx.epays_watch_until, now + const.CRON_MAX_AGE)

    def test_s26_a_declined_payment_paid_again_waits_for_the_bank(self):
        """ S24: ePays lets the customer pay it again on its link, so only the bank can decide. """
        tx = self._create_epays_transaction(state='pending')
        data = self._payment_data(
            tx, status='Failed', result='', transactionId=None, inFlight=True
        )
        with self._patch_payment_details(data), mute_logger(TX_LOGGER):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(tx.state, 'pending', "The customer is not told it was declined.")
        self.assertFalse(tx.state_message)
        self.assertTrue(tx.epays_at_bank)

    def test_a_declined_payment_at_the_bank_is_read_past_its_link(self):
        """ S9: an old declined payment paid again on its link. """
        now = fields.Datetime.now()
        tx = self._old_tx('error')
        data = self._payment_data(
            tx, status='Failed', result='', transactionId=None, inFlight=True
        )
        with self._patch_payment_details(data), mute_logger(TX_LOGGER):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(tx.state, 'error')
        self.assertGreaterEqual(tx.epays_watch_until, now + const.CRON_MAX_AGE)

    def test_the_status_page_keeps_a_customer_waiting_on_a_declined_payment_paid_again(self):
        """ S18: the page would send a customer whose money may be moving back to pay again. """
        tx = self._create_epays_transaction(state='error', epays_at_bank=True)
        polled_values = {'provider_code': 'epays', 'state': 'error', 'landing_route': '/my/orders'}
        controller = EpaysPostProcessing()
        for data, expected_state in (
            (self._payment_data(
                tx, status='Failed', result='', transactionId=None, inFlight=True,
            ), 'pending'),
            (self._payment_data(
                tx, status='Failed', result='', transactionId=None, inFlight=False,
            ), 'error'),
        ):
            with patch.object(
                EpaysPostProcessing, '_get_monitored_transaction', return_value=tx,
            ), patch.object(
                PaymentPostProcessing, 'poll_status', return_value=dict(polled_values),
            ), self._patch_payment_details(data), mute_logger(TX_LOGGER):
                values = controller.poll_status()
            self.assertEqual(values['state'], expected_state)

    def test_the_status_page_does_not_say_declined_while_the_bank_decides(self):
        tx = self._create_epays_transaction(
            state='error', epays_at_bank=True, state_message="ePays: Your payment was declined.",
        )
        html = str(self.env['ir.qweb']._render('payment.state_header', {
            'tx': tx, 'is_processing': True, 'is_html_empty': is_html_empty,
        }))
        self.assertIn("still confirming", html)
        self.assertNotIn("declined", html)

    def test_a_retry_keeps_a_declined_payment_paid_again(self):
        """ S1, S24: it is not replaced, as the bank's answer can still take the money. """
        self._skip_without_sale()
        order = self._create_sale_order()
        first = self._earlier_attempt(order, state='error', amount=order.amount_total - 10)
        in_flight = self._payment_data(
            first, status='Failed', result='', transactionId=None, inFlight=True
        )
        with self.assertRaises(ValidationError) as error:
            self._pay_again(
                [in_flight],
                delete_response=AssertionError("No void: ePays would refuse it."),
                **self._order_values(order),
            )
        self.assertIn("still being confirmed", error.exception.args[0])
        self.assertEqual(first.state, 'error')

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_s28_the_merchant_is_told_in_the_language_of_the_company(self):
        """ S12: not in the customer's, nor in the one of whoever triggered the read. """
        self._activate_arabic()
        self.env.company.partner_id.lang = 'ar_001'
        self.env.company.email = 'shop@example.com'
        tx = self._create_epays_transaction(state='pending')
        with self._patch_payment_details(self._payment_data(tx, amount='1.000')):
            tx.with_context(lang='en_US')._handle_notification_data(
                'epays', {'paymentId': tx.provider_reference}
            )
        self.assertTrue(tx.epays_review_needed)
        mail = self.env['mail.mail'].sudo().search([('subject', 'like', tx.reference)])
        self.assertEqual(len(mail), 1)
        self.assertNotIn("review transaction", mail.subject)
        self.assertNotIn("charged the customer", mail.body_html)

    def test_a_plain_user_error_on_a_completed_payment_goes_back_to_the_scheduled_check(self):
        """ S10: `UserError` is what other modules normally raise, and it escapes neither route. """
        now = fields.Datetime.now()
        tx = self._old_tx('cancel', epays_watch_until=now - timedelta(days=1))
        with self._patch_payment_details(self._payment_data(tx)), patch.object(
            type(tx), '_set_done', side_effect=UserError("refused"),
        ), mute_logger(TX_LOGGER):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(tx.state, 'cancel')
        self.assertGreaterEqual(tx.epays_watch_until, now + const.LATE_CHARGE_WINDOW)

    def test_a_read_failing_on_the_last_run_of_the_check_goes_back_to_it(self):
        """ S10: the check would not read it again, though it still reads it now. """
        now = fields.Datetime.now()
        for state, values, create_date in (
            ('pending', {}, now - const.CRON_MAX_AGE + timedelta(minutes=5)),
            ('error', {
                'epays_expires_at': now - const.CRON_VOID_GRACE_PERIOD + timedelta(minutes=5),
            }, now - timedelta(days=1)),
        ):
            with self.subTest(state=state):
                tx = self._create_epays_transaction(state=state, **values)
                self._set_create_date(tx, create_date)
                self.assertFalse(tx._epays_is_read_by_cron())
                with patch.object(type(tx), '_epays_may_read_payment', return_value=False), \
                        mute_logger(TX_LOGGER):
                    tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
                self.assertGreaterEqual(tx.epays_watch_until, now + const.LATE_CHARGE_WINDOW)

    def test_an_old_pending_payment_at_the_bank_is_watched(self):
        """ S9: a declined payment paid again stays pending, past the age the check reads. """
        now = fields.Datetime.now()
        tx = self._old_tx('pending')
        data = self._payment_data(
            tx, status='Failed', result='', transactionId=None, inFlight=True
        )
        with self._patch_payment_details(data), mute_logger(TX_LOGGER):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertEqual(tx.state, 'pending')
        self.assertGreaterEqual(tx.epays_watch_until, now + const.CRON_MAX_AGE)

    def test_a_recent_pending_payment_at_the_bank_stays_with_the_scheduled_check(self):
        tx = self._create_epays_transaction(state='pending')
        data = self._payment_data(
            tx, status='Failed', result='', transactionId=None, inFlight=True
        )
        with self._patch_payment_details(data), mute_logger(TX_LOGGER):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertFalse(tx.epays_watch_until, "The scheduled check reads it anyway.")

    @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER)
    def test_the_payment_note_is_in_the_language_of_the_company(self):
        """ S12: it is written for the merchant, whoever triggered the read. """
        self._skip_without_sale()
        self._activate_arabic()
        self.env.company.partner_id.lang = 'ar_001'
        order = self._create_sale_order()
        tx = self._create_epays_transaction(state='pending', **self._order_values(order))
        with self._patch_payment_details(self._payment_data(tx)):
            tx.with_context(lang='en_US')._handle_notification_data(
                'epays', {'paymentId': tx.provider_reference}
            )
        self.assertEqual(self._count_detail_notes(order), 0, "The labels are not in English.")

    def test_a_company_without_a_language_is_told_in_english(self):
        """ Not in the customer's language, which is what the context holds on a public route. """
        self._activate_arabic()
        self.env.company.partner_id.lang = False
        tx = self._create_epays_transaction(state='pending')
        self.assertFalse(tx.with_context(lang='ar_001')._epays_for_merchant().env.lang)

    def test_the_failure_route_says_nothing_while_the_bank_decides(self):
        """ S18: the message tells the customer to try again, which the status page stopped. """
        tx = self._create_epays_transaction(
            state='error', epays_at_bank=True, state_message="ePays: Your payment was declined.",
        )
        with patch.object(EpaysPostProcessing, '_get_monitored_transaction', return_value=tx):
            self.assertEqual(EpaysPostProcessing().epays_failure(), {})
            tx.epays_at_bank = False
            self.assertEqual(EpaysPostProcessing().epays_failure()['state'], 'error')

    def test_the_status_page_does_not_wait_on_a_payment_taken_over(self):
        """ S18: a newer transaction holds the payment, so this one waits for nothing. """
        tx = self._create_epays_transaction(
            state='cancel', epays_at_bank=True, state_message="ePays: Replaced by transaction X.",
        )
        self._create_epays_transaction(
            state='pending', provider_reference=tx.provider_reference,
            epays_environment=tx.epays_environment,
        )
        html = str(self.env['ir.qweb']._render('payment.state_header', {
            'tx': tx, 'is_processing': True, 'is_html_empty': is_html_empty,
        }))
        self.assertNotIn("still confirming", html)
        self.assertIn("Replaced by", html)

    def _stuck_mails(self, tx):
        return self.env['mail.mail'].sudo().search([('subject', 'like', tx.reference)])

    def test_s29_a_completed_payment_that_cannot_be_applied_is_reported_once(self):
        """ S29: it is read again, and nobody would notice the money otherwise. """
        self.env.company.email = 'shop@example.com'
        now = fields.Datetime.now()
        tx = self._old_tx('cancel', epays_watch_until=now + timedelta(minutes=1))
        data = self._payment_data(tx)
        for _attempt in range(2):
            with patch.object(type(tx), '_set_done', side_effect=UserError("refused")):
                self._run_cron(lambda *args, **kwargs: data)
            self.assertEqual(tx.state, 'cancel')
            self.assertEqual(len(self._stuck_mails(tx)), 1)
        self.assertIn("could not record", self._stuck_mails(tx).body_html)

        self._run_cron(lambda *args, **kwargs: data)
        self.assertEqual(tx.state, 'done')
        self.assertEqual(len(self._stuck_mails(tx)), 1, "A later success is not one more alert.")

    def test_a_declined_payment_that_cannot_be_applied_is_not_reported(self):
        """ S29: no money was taken, so the merchant has nothing to act on. """
        self.env.company.email = 'shop@example.com'
        tx = self._create_epays_transaction(state='pending')
        data = self._payment_data(tx, status='Failed', result='', responseCode='05')
        with self._patch_payment_details(data), patch.object(
            type(tx), '_set_error', side_effect=UserError("refused"),
        ), mute_logger(TX_LOGGER):
            tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
        self.assertFalse(self._stuck_mails(tx))

    def _run_cron_while_epays_is_down(self):
        def unreachable(provider, method, path, **kwargs):
            raise EpaysApiError("unreachable", connection_error="timed out", timed_out=True)

        self._run_cron(unreachable)

    def test_s30_a_failed_scheduled_read_keeps_a_watched_payment_watched(self):
        """ An outage must not outlast the watch, or a late charge goes unread. """
        now = fields.Datetime.now()
        tx = self._old_tx('cancel', epays_watch_until=now + timedelta(minutes=1))
        self._run_cron_while_epays_is_down()
        self.assertGreaterEqual(tx.epays_watch_until, now + const.LATE_CHARGE_WINDOW)
        self.assertGreaterEqual(tx.epays_read_failed_at, now)

    def test_s30_a_failed_scheduled_read_does_not_shorten_the_watch(self):
        now = fields.Datetime.now()
        limit = now + timedelta(days=2)
        tx = self._old_tx('cancel', epays_watch_until=limit)
        self._run_cron_while_epays_is_down()
        self.assertEqual(tx.epays_watch_until, limit)

    def test_s30_the_watch_ends_three_days_after_the_first_failure(self):
        """ What stops a payment that can never be read being read for ever. """
        now = fields.Datetime.now()
        first_failure = now - const.CRON_MAX_AGE + timedelta(hours=1)
        tx = self._old_tx(
            'cancel', epays_watch_until=now + timedelta(minutes=1),
            epays_read_failed_at=first_failure,
        )
        self._run_cron_while_epays_is_down()
        self.assertEqual(tx.epays_watch_until, first_failure + const.CRON_MAX_AGE)
        self.assertEqual(tx.epays_read_failed_at, first_failure, "Only the first one counts.")

        tx.epays_read_failed_at = now - const.CRON_MAX_AGE - timedelta(hours=1)
        tx.epays_watch_until = limit = now + timedelta(minutes=1)
        self._run_cron_while_epays_is_down()
        self.assertEqual(tx.epays_watch_until, limit, "Past the limit, the watch runs out.")

    def test_s30_a_good_read_resets_the_failures(self):
        now = fields.Datetime.now()
        tx = self._old_tx(
            'cancel', epays_watch_until=now + timedelta(minutes=1),
            epays_read_failed_at=now - timedelta(days=2),
        )
        data = self._payment_data(tx, status='Voided', result='', transactionId=None)
        self._run_cron(lambda *args, **kwargs: data)
        self.assertFalse(tx.epays_read_failed_at)

    def test_s30_a_payment_at_the_end_of_the_cron_window_joins_the_watched_ones(self):
        now = fields.Datetime.now()
        tx = self._create_epays_transaction(state='pending')
        self._set_create_date(tx, now - const.CRON_MAX_AGE + timedelta(minutes=5))
        self._run_cron_while_epays_is_down()
        self.assertGreaterEqual(tx.epays_watch_until, now + const.LATE_CHARGE_WINDOW)

    def test_s30_a_payment_the_cron_reads_anyway_is_not_watched(self):
        tx = self._create_epays_transaction(state='pending')
        self._set_create_date(tx, fields.Datetime.now() - timedelta(hours=1))
        self._run_cron_while_epays_is_down()
        self.assertFalse(tx.epays_watch_until)
        self.assertTrue(tx.epays_read_failed_at, "The limit counts from this failure.")

    def test_s30_the_other_payments_are_still_read(self):
        now = fields.Datetime.now()
        failing = self._old_tx('cancel', epays_watch_until=now + timedelta(minutes=1))
        readable = self._old_tx('cancel', epays_watch_until=now + timedelta(minutes=1))
        data = self._payment_data(readable)

        def handler(provider, method, path, **kwargs):
            if path.endswith(failing.provider_reference):
                raise EpaysApiError("unreachable", connection_error="timed out", timed_out=True)
            return data

        self._run_cron(handler)
        self.assertEqual(readable.state, 'done')
        self.assertGreaterEqual(failing.epays_watch_until, now + const.LATE_CHARGE_WINDOW)

    def test_the_website_language_wins_over_the_contact_language(self):
        """ `/payment/transaction` and the portal's route are not website routes: their context is
        the session's language, the contact's. The cookie says which website the customer is on. """
        self._activate_arabic()
        self.partner.lang = 'ar_001'
        for cookies, session_lang, expected in (
            ({'frontend_lang': 'en_US'}, 'ar_001', 'en'),
            ({'frontend_lang': 'ar_001'}, 'en_US', 'ar'),
            ({}, 'ar_001', 'ar'),
        ):
            with self.subTest(cookies=cookies, session_lang=session_lang):
                tx = self._create_transaction('redirect', reference=self._new_reference())
                with patch(f'{TX_LOGGER}.request', SimpleNamespace(cookies=cookies), create=True):
                    calls, _values = self._initiate(tx.with_context(lang=session_lang))
                self.assertEqual(calls[-1]['payload']['lang'], expected)

    def test_the_description_follows_the_website_language_too(self):
        """ S20: "Order N" in the language of the page. """
        self._activate_arabic()
        tx = self._create_transaction('redirect', reference=self._new_reference())
        website_request = SimpleNamespace(cookies={'frontend_lang': 'en_US'})
        with patch(f'{TX_LOGGER}.request', website_request, create=True):
            self.assertEqual(tx.with_context(lang='ar_001')._epays_get_request_lang(), 'en_US')

    def test_s19_the_merchant_can_check_a_payment_at_any_age(self):
        """ S19: the replaced payments are read too. """
        self._skip_without_sale()
        order = self._create_sale_order()
        replaced = self._earlier_attempt(order, state='cancel')
        replaced.epays_watch_until = fields.Datetime.now() - timedelta(days=9)
        current = self._earlier_attempt(order, state='draft')
        paid = self._payment_data(replaced)
        pending = self._payment_data(current, status='Pending', result='')

        def handler(provider, method, path, **kwargs):
            return paid if path.endswith(replaced.provider_reference) else pending

        with self._patch_make_request(handler), mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER), \
                patch.object(type(current), '_post_process'):
            action = current.action_epays_check_status()
        self.assertEqual(replaced.state, 'done')
        self.assertEqual(action['tag'], 'display_notification')

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
