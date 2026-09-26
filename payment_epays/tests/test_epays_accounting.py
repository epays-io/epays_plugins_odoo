# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

from datetime import timedelta
from unittest import SkipTest
from unittest.mock import patch
from urllib.parse import urlsplit

from odoo import Command, fields
from odoo.modules.registry import Registry
from odoo.tests import tagged
from odoo.tests.common import get_db_name
from odoo.tools import mute_logger

from odoo.addons.payment.tests.http_common import PaymentHttpCommon
from odoo.addons.payment_epays.controllers.main import EpaysController
from odoo.addons.payment_epays.tests.common import REQUESTS_PATH, EpaysCommon, FakeResponse

try:
    from odoo.addons.account_payment.tests.common import AccountPaymentCommon
except ImportError:  # account_payment is not on the addons path.
    AccountPaymentCommon = None

TX_LOGGER = 'odoo.addons.payment_epays.models.payment_transaction'
PROVIDER_LOGGER = 'odoo.addons.payment_epays.models.payment_provider'
CONTROLLER_LOGGER = 'odoo.addons.payment_epays.controllers.main'
PAYMENT_TX_LOGGER = 'odoo.addons.payment.models.payment_transaction'


def _account_payment_is_installed():
    registry = Registry(get_db_name())
    return 'payment_id' in registry['payment.transaction']._fields


if AccountPaymentCommon is not None:

    @tagged('post_install', '-at_install')
    class EpaysAccountingTest(EpaysCommon, AccountPaymentCommon, PaymentHttpCommon):
        """ The accounting side of ePays payments, with Odoo's real post-processing. """

        @classmethod
        def setUpClass(cls):
            if not _account_payment_is_installed():
                raise SkipTest("account_payment is not installed")
            super().setUpClass()
            cls.currency = cls.env.company.currency_id
            cls.bank_journal = cls.company_data['default_journal_bank']
            cls.provider.journal_id = cls.bank_journal
            cls.provider.journal_id.inbound_payment_method_line_ids.filtered(
                lambda line: line.payment_provider_id == cls.provider
            ).payment_account_id = cls.inbound_payment_method_line.payment_account_id

        #=== Helpers ===#

        def _fake_api(self, tx, calls, status='Completed'):
            """ Return a fake `requests.request` answering the payment details of the tx. """
            # Built here, in the test thread: reading the test records from the HTTP server thread
            # would cache their current values in the test environment.
            body = {'status': 'SUCCESS', 'data': self._payment_data(tx, status=status), 'meta': {}}

            def fake_request(method, url, *, json=None, headers=None, timeout=None,
                             allow_redirects=True):
                calls.append({'method': method, 'url': url, 'headers': dict(headers)})
                assert allow_redirects is False, "a redirect would resend the master key"
                return FakeResponse(200, body)
            return fake_request

        def _run_cron(self):
            with patch.object(self.env.cr, 'commit'), patch.object(self.env.cr, 'rollback'), \
                    mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER):
                self.env['payment.transaction']._cron_epays_poll_pending()

        def _set_create_date(self, tx, create_date):
            self.env.cr.execute(
                "UPDATE payment_transaction SET create_date = %s WHERE id = %s",
                (create_date, tx.id),
            )
            tx.invalidate_recordset(['create_date'])

        def _assert_payment_posted(self, tx):
            self.assertTrue(tx.payment_id, "A payment is created for the transaction.")
            self.assertNotEqual(tx.payment_id.state, 'draft')
            self.assertEqual(tx.payment_id.move_id.state, 'posted')
            self.assertEqual(tx.payment_id.journal_id, self.bank_journal)
            self.assertEqual(tx.payment_id.amount, tx.amount)

        #=== Tests ===#

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_done_transaction_posts_the_payment_and_confirms_the_order(self):
            self.ensure_installed('sale')
            order = self._create_sale_order(confirm=False, currency_id=self.currency.id)
            tx = self._create_epays_transaction(
                amount=order.amount_total, sale_order_ids=[Command.set(order.ids)],
            )
            with self._patch_payment_details(self._payment_data(tx)):
                tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
            self.assertEqual(tx.state, 'done')
            tx._post_process()
            self._assert_payment_posted(tx)
            self.assertEqual(order.state, 'sale')

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_portal_invoice_payment(self):
            invoice = self.init_invoice(
                'out_invoice', partner=self.partner, amounts=[137.0], post=True,
                currency=self.currency,
            )
            tx = self._create_epays_transaction(
                amount=invoice.amount_total, invoice_ids=[Command.set(invoice.ids)],
            )
            udf = tx._epays_get_custom_fields()
            self.assertEqual(udf['udf3'], invoice.name)
            if 'sale_order_ids' in tx._fields:
                self.assertEqual(udf['udf2'], '')

            data = self._payment_data(tx)
            with self._patch_payment_details(data):
                tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
                tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
            self.assertEqual(tx.state, 'done')
            notes = invoice.message_ids.filtered(
                lambda m: 'ePays payment details' in (m.body or '')
            )
            self.assertEqual(len(notes), 1, "One details note, however many notifications.")
            self.assertIn('Card Brand: MASTERCARD', notes.body)

            tx._post_process()
            self._assert_payment_posted(tx)
            self.assertIn(invoice.payment_state, ('paid', 'in_payment'))

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_payment_charged_in_bhd_settles_the_invoice_in_its_own_currency(self):
            """ ePays charges BHD; Odoo records the payment in the invoice's currency, which is
            the same money at the rate used for the conversion. """
            bhd = self._prepare_currency('BHD')
            if self.currency == bhd:
                self.skipTest("the company already works in BHD")
            invoice = self.init_invoice(
                'out_invoice', partner=self.partner, amounts=[100.0], post=True,
                currency=self.currency,
            )
            tx = self._create_epays_transaction(
                amount=invoice.amount_total, currency_id=self.currency.id,
                invoice_ids=[Command.set(invoice.ids)],
                epays_charged_amount=round(invoice.amount_total * 0.376, 3),
                epays_charged_currency_id=bhd.id,
                epays_exchange_rate=0.376,
            )
            data = self._payment_data(tx)
            self.assertEqual(data['currency'], 'BHD')
            with self._patch_payment_details(data):
                tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
            self.assertEqual(tx.state, 'done')

            tx._post_process()
            self._assert_payment_posted(tx)
            self.assertEqual(tx.payment_id.currency_id, self.currency)
            self.assertIn(invoice.payment_state, ('paid', 'in_payment'))

        def test_disabled_mid_payment_completes_through_the_return_route(self):
            tx = self._create_epays_transaction()
            self.provider.state = 'disabled'
            calls = []
            url = self._build_url(EpaysController._return_url)
            with patch(REQUESTS_PATH, side_effect=self._fake_api(tx, calls)), \
                    mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER, CONTROLLER_LOGGER):
                response = self.opener.get(
                    url, params={'paymentId': tx.provider_reference}, allow_redirects=False
                )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(tx.state, 'done')
            self._assert_sandbox_calls(calls)

            # Odoo's post-processing cron is off without an active provider; the ePays one is not.
            self._run_cron()
            self._assert_payment_posted(tx)

        def test_disabled_mid_payment_completes_through_the_cron(self):
            tx = self._create_epays_transaction(state='pending')
            self._set_create_date(tx, fields.Datetime.now() - timedelta(minutes=20))
            # Switched to production meanwhile: the transaction keeps the sandbox.
            self.provider.write({'state': 'enabled', 'epays_mode': 'production'})
            self.provider.state = 'disabled'
            calls = []
            with patch(REQUESTS_PATH, side_effect=self._fake_api(tx, calls)):
                self._run_cron()
            self.assertEqual(tx.state, 'done')
            self._assert_sandbox_calls(calls)
            self._assert_payment_posted(tx)

        def _assert_sandbox_calls(self, calls):
            self.assertTrue(calls)
            for call in calls:
                self.assertEqual(urlsplit(call['url']).hostname, 'testapi.epays.io')
                self.assertEqual(call['headers']['X-Api-Id'], 'sandbox-api-id')
                self.assertEqual(call['headers']['X-Api-Master-Key'], 'sandbox-master-key')
                self.assertEqual(call['headers']['X-Merchant-Domain'], 'shop.example.com')
