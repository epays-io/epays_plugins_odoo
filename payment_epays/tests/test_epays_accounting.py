# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

from datetime import timedelta
from unittest import SkipTest
from unittest.mock import patch
from urllib.parse import urlsplit

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.modules.registry import Registry
from odoo.tests import tagged
from odoo.tests.common import get_db_name
from odoo.tools import mute_logger

from odoo.addons.payment.controllers.post_processing import PaymentPostProcessing
from odoo.addons.payment.tests.http_common import PaymentHttpCommon
from odoo.addons.payment_epays import const
from odoo.addons.payment_epays.controllers.main import EpaysController, EpaysPostProcessing
from odoo.addons.payment_epays.tests.common import REQUESTS_PATH, EpaysCommon, FakeResponse
from odoo.addons.payment_epays.utils import EpaysApiError

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
                self._run_commit_hooks()

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
            calls = []
            with self._patch_make_request(lambda provider, method, path, **kwargs: calls.append(
                (method, path)
            ) or {}):
                tx._post_process()
                self.assertEqual(calls, [], "ePays hears of nothing a failure can still roll back.")
                self._assert_payment_posted(tx)
                self.assertEqual(order.state, 'sale')
                self._run_commit_hooks()
            self.assertEqual(
                calls, [('POST', f'/api/v2/Payments/{tx.provider_reference}/acknowledgement')],
                "Odoo acknowledges the payment once it recorded it.",
            )

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s16_a_declined_payment_is_acknowledged_but_not_a_refused_one(self):
            declined = self._create_epays_transaction()
            refused = self._create_epays_transaction()  # Paid, but not the amount of the order.
            for tx, overrides in [
                (declined, {'status': 'Failed', 'result': 'DECLINED', 'responseCode': '51'}),
                (refused, {'amount': '1.000'}),
            ]:
                with self._patch_payment_details(self._payment_data(tx, **overrides)):
                    tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})
            self.assertEqual((declined.state, refused.state), ('error', 'error'))

            calls = []

            def handler(provider, method, path, **kwargs):
                calls.append((method, path))
                return {'status': 'Failed'}

            with self._patch_make_request(handler):
                (declined | refused)._post_process()
                self._run_commit_hooks()
            path = f'/api/v2/Payments/{declined.provider_reference}'
            self.assertEqual(
                calls, [('GET', path), ('POST', f'{path}/acknowledgement')],
                "The refused payment stays among the ones ePays lists for the merchant to check.",
            )
            self.assertFalse((declined | refused).payment_id, "No accounting payment for either.")

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_failed_acknowledgement_does_not_stop_the_order(self):
            self.ensure_installed('sale')
            order = self._create_sale_order(confirm=False, currency_id=self.currency.id)
            tx = self._create_epays_transaction(
                amount=order.amount_total, sale_order_ids=[Command.set(order.ids)],
            )
            with self._patch_payment_details(self._payment_data(tx)):
                tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})

            def handler(provider, method, path, **kwargs):
                raise EpaysApiError("unreachable", http_status=404)  # An older ePays server.

            with self._patch_make_request(handler):
                tx._post_process()
                self._run_commit_hooks()
            self.assertTrue(tx.is_post_processed)
            self._assert_payment_posted(tx)
            self.assertEqual(order.state, 'sale')

        def _order_and_attempts(self, *states):
            """ Return a draft order and one ePays transaction per state on it. """
            order = self._create_sale_order(confirm=False, currency_id=self.currency.id)
            return order, [self._create_epays_transaction(
                state=state, amount=order.amount_total, sale_order_ids=[Command.set(order.ids)],
            ) for state in states]

        def _notify(self, tx, **overrides):
            with self._patch_payment_details(self._payment_data(tx, **overrides)):
                tx._handle_notification_data('epays', {'paymentId': tx.provider_reference})

        def _notes(self, document, text):
            return document.message_ids.filtered(lambda m: text in (m.body or ''))

        def _post_process_recording_calls(self, txs):
            """ Post-process the transactions, with ePays accepting every call. """
            calls = []

            def handler(provider, method, path, **kwargs):
                calls.append((method, path))
                return {'status': 'Voided', 'paymentId': path.rsplit('/', 1)[1]}

            with self._patch_make_request(handler):
                txs._post_process()
                self._run_commit_hooks()
            return calls

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s11_a_voided_payment_charged_late_pays_the_order_and_closes_the_replacement(self):
            """ Voided while the customer was on 3-D Secure, charged by ePays anyway. """
            self.ensure_installed('sale')
            order, (voided, replacement) = self._order_and_attempts('cancel', 'draft')
            self._notify(voided)
            self.assertEqual(voided.state, 'done')

            calls = self._post_process_recording_calls(voided)
            self._assert_payment_posted(voided)
            self.assertEqual(order.state, 'sale')
            self.assertEqual(replacement.state, 'cancel', "The replacement must not be paid too.")
            self.assertEqual(
                order.get_portal_last_transaction(), voided,
                "The customer who paid does not read that the payment was cancelled.",
            )
            self.assertEqual(calls, [
                ('GET', f'/api/v2/Payments/{replacement.provider_reference}'),
                ('DELETE', f'/api/v2/Payments/{replacement.provider_reference}'),
                ('POST', f'/api/v2/Payments/{voided.provider_reference}/acknowledgement'),
            ])

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_notification_days_later_pays_the_order_through_the_cron(self):
            """ A server notification has no customer session: the cron finishes the work. """
            self.ensure_installed('sale')
            order, (cancelled,) = self._order_and_attempts('cancel')
            cancelled.last_state_change = fields.Datetime.now() - timedelta(days=10)
            self._notify(cancelled)
            self.assertEqual(cancelled.state, 'done')
            self.assertEqual(order.state, 'draft', "Not post-processed yet.")

            calls = []
            with patch(REQUESTS_PATH, side_effect=self._fake_api(cancelled, calls)):
                self._run_cron()
            self._assert_payment_posted(cancelled)
            self.assertEqual(order.state, 'sale')

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s7_a_declined_link_is_closed_once_the_order_is_paid(self):
            """ ePays lets the customer retry a declined payment on the same link. """
            self.ensure_installed('sale')
            order, (paid, declined) = self._order_and_attempts('draft', 'error')
            declined.epays_response_code = '51'
            self._notify(paid)
            self._post_process_recording_calls(paid)
            self.assertEqual(paid.state, 'done')
            self.assertEqual(declined.state, 'cancel')

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s13_a_second_charge_is_kept_out_of_the_books_for_the_merchant_to_refund(self):
            self.ensure_installed('sale')
            for name, late_state in (('voided', 'cancel'), ('declined, then retried', 'error')):
                with self.subTest(name):
                    order, (late, paid) = self._order_and_attempts(late_state, 'draft')
                    late.epays_response_code = '51' if late_state == 'error' else False
                    self._notify(paid)
                    self._post_process_recording_calls(paid | late)
                    late.state = late_state  # Closing the other attempts may have moved it.
                    self.assertEqual(order.state, 'sale')

                    self._notify(late)
                    self.assertEqual(late.state, 'error')
                    self.assertEqual(late.epays_response_code, '00')
                    self.assertIn('already paid', late.state_message)
                    self.assertTrue(self._notes(order, 'refund it on ePays'))
                    self.assertEqual(
                        order.get_portal_last_transaction(), late,
                        "The customer reads about it, although the other transaction is newer.",
                    )

                    calls = self._post_process_recording_calls(late)
                    self.assertEqual(calls, [], "Left unacknowledged, for the merchant to find.")
                    self.assertFalse(late.payment_id)
                    self.assertEqual(paid.payment_id.amount, order.amount_total)

                    notes = len(order.message_ids)
                    self._notify(late)  # The cron reads it until its link expires.
                    self.assertEqual(len(order.message_ids), notes, "The merchant is told once.")

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_paying_the_rest_of_an_invoice_is_not_a_second_charge(self):
            invoice = self.init_invoice(
                'out_invoice', partner=self.partner, amounts=[100.0], post=True,
                currency=self.currency,
            )
            first, second = (self._create_epays_transaction(
                amount=amount, invoice_ids=[Command.set(invoice.ids)],
            ) for amount in (invoice.amount_total / 2, invoice.amount_total / 2))
            self._notify(first)
            self._post_process_recording_calls(first)
            self.assertEqual(second.state, 'draft', "The rest of the invoice is still due.")
            self._notify(second)
            self.assertEqual(second.state, 'done')

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_the_status_page_follows_the_payment_that_paid_the_order(self):
            self.ensure_installed('sale')
            _order, (charged, watched) = self._order_and_attempts('cancel', 'draft')

            def poll(controller, monitored):
                polled_values = {
                    'provider_code': 'epays', 'state': 'done',
                    'landing_route': '/shop/payment/validate',
                }
                with patch.object(
                    EpaysPostProcessing, '_get_monitored_transaction', return_value=monitored,
                ), patch.object(
                    PaymentPostProcessing, 'poll_status', return_value=polled_values,
                ), patch.object(EpaysPostProcessing, 'monitor_transaction') as monitor_mock:
                    controller.poll_status()
                return monitor_mock

            poll(EpaysPostProcessing(), watched).assert_not_called()
            self._notify(charged)
            self.assertEqual(charged.state, 'done')
            poll(EpaysPostProcessing(), watched).assert_called_once_with(charged)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_the_status_page_keeps_a_refused_charge_for_the_customer_to_read(self):
            self.ensure_installed('sale')
            order, (paid, refused) = self._order_and_attempts('draft', 'cancel')
            self._notify(paid)
            self._post_process_recording_calls(paid)
            refused.state = 'cancel'
            self._notify(refused)
            self.assertTrue(refused._epays_is_refused())
            landing_route = '/shop/payment/validate'

            with patch.object(
                EpaysPostProcessing, '_get_monitored_transaction', return_value=refused,
            ), patch.object(PaymentPostProcessing, 'poll_status', return_value={
                'provider_code': 'epays', 'state': 'error', 'landing_route': landing_route,
            }), patch.object(EpaysPostProcessing, 'monitor_transaction') as monitor_mock:
                values = EpaysPostProcessing().poll_status()
            monitor_mock.assert_not_called()
            self.assertEqual(
                values['landing_route'], landing_route,
                "The payment step of a paid order has nothing to show: the confirmation page does.",
            )

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_refused_charge_gives_way_to_a_later_payment(self):
            self.ensure_installed('sale')
            order, (refused, paid) = self._order_and_attempts('draft', 'draft')
            self._notify(refused, amount='1.000')
            self.assertTrue(refused._epays_is_refused())
            self._notify(paid)
            paid.last_state_change = refused.last_state_change + timedelta(minutes=5)
            self.assertEqual(order.get_portal_last_transaction(), paid)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_charge_on_an_invoice_settled_without_a_transaction_is_refused(self):
            invoice = self.init_invoice(
                'out_invoice', partner=self.partner, amounts=[100.0], post=True,
                currency=self.currency,
            )
            tx = self._create_epays_transaction(
                state='cancel', amount=invoice.amount_total, invoice_ids=[Command.set(invoice.ids)],
            )
            self.env['account.payment.register'].with_context(
                active_model='account.move', active_ids=invoice.ids,
            ).create({})._create_payments()
            self.assertEqual(invoice.amount_residual, 0)

            self._notify(tx)
            self.assertTrue(tx._epays_is_refused())
            self.assertTrue(self._notes(invoice, 'refund it on ePays'))
            self._post_process_recording_calls(tx)
            self.assertFalse(tx.payment_id)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_charge_on_an_order_paid_through_its_invoice_is_refused(self):
            self.ensure_installed('sale')
            order, (voided,) = self._order_and_attempts('cancel')
            order.action_confirm()
            for line in order.order_line:
                line.qty_delivered = line.product_uom_qty
            invoice = order._create_invoices()
            invoice.action_post()
            on_invoice = self._create_epays_transaction(
                amount=invoice.amount_total, invoice_ids=[Command.set(invoice.ids)],
            )
            self.assertNotIn(on_invoice, order.transaction_ids)
            self._notify(on_invoice)
            self._post_process_recording_calls(on_invoice)
            self.assertEqual(on_invoice.state, 'done')

            self._notify(voided)
            self.assertTrue(voided._epays_is_refused())

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_paying_a_reopened_invoice_is_not_a_second_charge(self):
            invoice = self.init_invoice(
                'out_invoice', partner=self.partner, amounts=[100.0], post=True,
                currency=self.currency,
            )
            first, second = (self._create_epays_transaction(
                amount=invoice.amount_total, invoice_ids=[Command.set(invoice.ids)],
            ) for _i in range(2))
            self._notify(first)
            self.assertFalse(second._epays_is_needed(), "Not recorded yet, but paid all the same.")
            self._post_process_recording_calls(first)
            self.assertEqual(second.state, 'cancel', "Closed along with the invoice.")

            invoice.line_ids.remove_move_reconcile()  # E.g. after a refund on ePays.
            self.assertTrue(second._epays_is_needed())
            second.state = 'draft'
            self._notify(second)
            self.assertEqual(second.state, 'done')

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_charge_on_a_cancelled_document_is_refused(self):
            self.ensure_installed('sale')
            order, (on_order,) = self._order_and_attempts('cancel')
            order._action_cancel()
            invoice = self.init_invoice(
                'out_invoice', partner=self.partner, amounts=[100.0], post=True,
                currency=self.currency,
            )
            on_invoice = self._create_epays_transaction(
                state='cancel', amount=invoice.amount_total, invoice_ids=[Command.set(invoice.ids)],
            )
            invoice.button_draft()
            invoice.button_cancel()
            for tx, document in ((on_order, order), (on_invoice, invoice)):
                self._notify(tx)
                self.assertTrue(tx._epays_is_refused())
                self.assertTrue(self._notes(document, 'refund it on ePays'))
                self.assertEqual(self._post_process_recording_calls(tx), [])
                self.assertFalse(tx.payment_id)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_charge_on_a_deleted_document_is_refused(self):
            self.ensure_installed('sale')
            order, (tx,) = self._order_and_attempts('cancel')
            tx.epays_udf2 = order.name
            order._action_cancel()
            order.unlink()
            self.env.invalidate_all()
            self._notify(tx)
            self.assertTrue(tx._epays_is_refused())
            self.assertEqual(self._post_process_recording_calls(tx), [])
            self.assertFalse(tx.payment_id)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_one_payment_held_by_two_attempts_is_not_a_second_charge(self):
            """ An older ePays server returns one payment to two attempts. """
            self.ensure_installed('sale')
            order, (older, newer) = self._order_and_attempts('pending', 'draft')
            newer.write({field: older[field] for field in ('provider_reference', 'epays_udf2')})
            newer._epays_apply_payment_details(self._payment_data(newer))
            self._post_process_recording_calls(newer)
            self.assertEqual(newer.state, 'done')

            older._epays_apply_payment_details(self._payment_data(older))
            self.assertEqual(older.state, 'cancel')
            self.assertFalse(older._epays_is_refused())
            self.assertFalse(self._notes(order, 'refund it on ePays'))
            self.assertEqual(newer.payment_id.amount, order.amount_total)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_attempts_completing_at_once_wait_for_each_other(self):
            self.ensure_installed('sale')
            _order, (first, second) = self._order_and_attempts('draft', 'draft')
            with patch.object(
                type(first), '_epays_lock_for_update', autospec=True,
            ) as lock_mock:
                self._notify(first)
            self.assertEqual(lock_mock.call_args.args[0], first | second)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s12_a_late_charge_that_no_longer_fits_the_order_is_held_for_the_merchant(self):
            """ S12: the first attempt was for a smaller cart, and was charged after all. """
            self.ensure_installed('sale')
            order = self._create_sale_order(confirm=False, currency_id=self.currency.id)
            total = order.amount_total
            smaller, current = (self._create_epays_transaction(
                state=state, amount=amount, sale_order_ids=[Command.set(order.ids)],
            ) for state, amount in (('cancel', total / 2), ('draft', total)))
            self.env.company.email = 'shop@example.com'
            self._notify(smaller)
            self.assertTrue(smaller._epays_is_refused())
            self.assertTrue(smaller.epays_review_needed)
            self.assertIn("contact us", smaller.state_message)
            self.assertTrue(self._notes(order, 'does not match these documents'))
            self.assertTrue(self.env['mail.mail'].search(
                [('email_to', '=', 'shop@example.com'), ('subject', 'like', smaller.reference)]
            ), "A note can go unread.")

            calls = self._post_process_recording_calls(smaller)
            self.assertEqual(order.state, 'draft')
            self.assertFalse(smaller.payment_id)
            self.assertEqual(current.state, 'cancel', "The other attempt is stopped.")
            self.assertEqual(calls, [
                ('GET', f'/api/v2/Payments/{current.provider_reference}'),
                ('DELETE', f'/api/v2/Payments/{current.provider_reference}'),
            ], "A refused charge stays unacknowledged.")

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_held_order_can_be_paid_once_the_merchant_released_it(self):
            self.ensure_installed('sale')
            order, (held,) = self._order_and_attempts('cancel')
            self._notify(held, amount='1.000')
            self.assertTrue(held.epays_review_needed)
            held.action_epays_release_review()
            self.assertFalse(held.epays_review_needed)
            self.assertTrue(held._epays_is_refused(), "The charge is still refused.")

        def _held_late_charge(self):
            """ Return an order and the charge held on it: below what is due, and no longer late
            in Odoo's eyes, as a refused transaction is in error. """
            order = self._create_sale_order(confirm=False, currency_id=self.currency.id)
            total = order.amount_total
            smaller, _current = (self._create_epays_transaction(
                state=state, amount=amount, sale_order_ids=[Command.set(order.ids)],
            ) for state, amount in (('cancel', total / 2), ('draft', total)))
            self._notify(smaller)
            self.assertTrue(smaller.epays_review_needed)
            self.assertEqual(smaller.state, 'error')
            return order, smaller

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s12_a_held_charge_stays_refused_when_it_is_read_again(self):
            """ ePays resends its notification, and the merchant is not told twice. """
            order, held = self._held_late_charge()
            notes = self._notes(order, 'does not match these documents')
            self._notify(held)
            self.assertEqual(held.state, 'error')
            self.assertTrue(held.epays_review_needed)
            self.assertEqual(self._notes(order, 'does not match these documents'), notes)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s12_the_cron_leaves_a_held_charge_refused(self):
            """ The cron reads a transaction in error until its link expires. """
            _order, held = self._held_late_charge()
            self._set_create_date(held, fields.Datetime.now() - timedelta(hours=1))
            calls = []
            with patch(REQUESTS_PATH, side_effect=self._fake_api(held, calls)):
                self._run_cron()
            self.assertIn(held.provider_reference, calls[0]['url'], "The cron read it.")
            self.assertEqual(held.state, 'error')
            self.assertFalse(held.payment_id)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s12_a_released_charge_is_never_applied(self):
            """ The release allows new payments: it does not record the charge. """
            _order, held = self._held_late_charge()
            held.action_epays_release_review()
            self._notify(held)
            self.assertEqual(held.state, 'error')
            self.assertFalse(held.epays_review_needed, "It does not block new payments again.")

        def _create_staff(self, *groups):
            return self.env['res.users'].create({
                'name': "Staff", 'login': 'epays_staff', 'email': 'staff@example.com',
                'groups_id': [Command.set([self.env.ref(group).id for group in groups])],
            })

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s12_staff_who_manage_the_order_can_release_it(self):
            self.ensure_installed('sale')
            _order, held = self._held_late_charge()
            staff = self._create_staff(
                'sales_team.group_sale_salesman_all_leads', 'account.group_account_invoice',
            )
            held.with_user(staff).action_epays_release_review()
            self.assertFalse(held.epays_review_needed)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s12_staff_who_cannot_edit_the_order_cannot_release_it(self):
            self.ensure_installed('sale')
            _order, held = self._held_late_charge()
            staff = self._create_staff('account.group_account_readonly')
            with self.assertRaises(AccessError):
                held.with_user(staff).action_epays_release_review()
            self.assertTrue(held.epays_review_needed)

        def _assert_merchant_told(self, order, tx, note, logs):
            """ The note on the document, the warning in the log, and the email to the shop. """
            self.assertTrue(self._notes(order, note))
            self.assertTrue([r for r in logs.records if tx.reference in r.getMessage()])
            self.assertTrue(self.env['mail.mail'].search(
                [('email_to', '=', 'shop@example.com'), ('subject', 'like', tx.reference)]
            ))

        @mute_logger(PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s13_the_merchant_is_told_of_a_second_charge(self):
            self.ensure_installed('sale')
            self.env.company.email = 'shop@example.com'
            order, (late, paid) = self._order_and_attempts('cancel', 'draft')
            self._notify(paid)
            with self.assertLogs(TX_LOGGER, level='WARNING') as logs:
                self._notify(late)
            self.assertTrue(late._epays_is_refused())
            self.assertIn("contact us", late.state_message, "The customer is told too.")
            self._assert_merchant_told(order, late, 'refund it on ePays', logs)

        @mute_logger(PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s14_a_mismatch_on_the_current_payment_holds_the_order_and_tells_everyone(self):
            self.ensure_installed('sale')
            self.env.company.email = 'shop@example.com'
            order, (tx,) = self._order_and_attempts('draft')
            with self.assertLogs(TX_LOGGER, level='WARNING') as logs:
                self._notify(tx, amount='1.000')
            self.assertEqual(tx.state, 'error')
            self.assertEqual(order.state, 'draft', "Not paid.")
            self.assertTrue(tx.epays_charge_refused)
            self.assertIn("contact us", tx.state_message)
            self.assertIn(tx.reference, tx.state_message)
            self._assert_merchant_told(order, tx, 'does not match these documents', logs)

            retry = self._create_transaction(
                'redirect', reference=False, amount=order.amount_total,
                sale_order_ids=[Command.set(order.ids)],
            )
            with self.assertRaises(ValidationError) as error, \
                    self._patch_make_request(lambda *args, **kwargs: {}) as make_request_mock:
                retry._get_processing_values()
            self.assertIn(tx.reference, error.exception.args[0], "New payments are blocked.")
            make_request_mock.assert_not_called()

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s16_a_cancelled_payment_with_an_approved_code_is_not_acknowledged(self):
            """ ePays reports it now, and Odoo stored no response code: the money was taken. """
            tx = self._create_epays_transaction(state='cancel')
            calls = []

            def handler(provider, method, path, **kwargs):
                calls.append((method, path))
                return {'status': 'cancelled', 'responseCode': '00'}

            with self._patch_make_request(handler):
                tx._post_process()
                self._run_commit_hooks()
            self.assertEqual(calls, [('GET', f'/api/v2/Payments/{tx.provider_reference}')])

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s7_a_held_order_keeps_a_payment_at_the_bank(self):
            """ S7: a void would not stop the bank. """
            self.ensure_installed('sale')
            _order, (held, at_bank) = self._order_and_attempts('cancel', 'draft')
            self._notify(held, amount='1.000')
            calls = []
            data = self._payment_data(at_bank, status='Pending', result='', transactionId='TXN-1')

            def handler(provider, method, path, **kwargs):
                calls.append(method)
                return data

            with self._patch_make_request(handler):
                held._post_process()
                self._run_commit_hooks()
            self.assertEqual(calls, ['GET'])
            self.assertEqual(at_bank.state, 'draft')
            self.assertTrue(at_bank.epays_at_bank)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s24_the_cron_voids_what_a_settled_order_left_open(self):
            """ S7, S24: the void after a payment runs once. """
            self.ensure_installed('sale')
            refused = EpaysApiError("ePays: refused", http_status=409)
            for label, hook_data, void_error in (
                ("void refused", {'inFlight': False}, refused),
                ("read failed", EpaysApiError("ePays: unreachable"), None),
                ("with the gateway", {'inFlight': True}, None),
            ):
                with self.subTest(label):
                    _order, (paid, left_open) = self._order_and_attempts('draft', 'draft')
                    self._notify(paid)
                    self._set_create_date(left_open, fields.Datetime.now() - timedelta(minutes=10))
                    data = self._payment_data(
                        left_open, status='Pending', result='', transactionId=None,
                    )
                    self._run_hook_with_answers(paid, data, hook_data, void_error)
                    self.assertEqual(left_open.state, 'draft')

                    calls = []
                    with self._patch_make_request(self._recording_handler(calls, data)):
                        self._run_cron()
                    self.assertIn('DELETE', calls)
                    self.assertEqual(left_open.state, 'cancel')

        def _recording_handler(self, calls, data):
            """ Return a handler recording the HTTP methods, answering GET with the payment. """
            def handler(provider, method, path, **kwargs):
                calls.append(method)
                return data
            return handler

        def _run_hook_with_answers(self, tx, data, hook_data, void_error):
            """ Post-process the transaction, with ePays answering as given. """
            def handler(provider, method, path, **kwargs):
                if method == 'GET' and isinstance(hook_data, Exception):
                    raise hook_data
                if method == 'DELETE' and void_error:
                    raise void_error
                return dict(data, **hook_data) if isinstance(hook_data, dict) else data

            with self._patch_make_request(handler):
                tx._post_process()
                self._run_commit_hooks()

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s24_the_cron_leaves_a_payment_in_flight_on_a_settled_order(self):
            self.ensure_installed('sale')
            _order, (paid, at_bank) = self._order_and_attempts('draft', 'draft')
            self._notify(paid)
            self._set_create_date(at_bank, fields.Datetime.now() - timedelta(minutes=10))
            data = self._payment_data(
                at_bank, status='Pending', result='', transactionId=None, inFlight=True
            )
            calls = []
            with self._patch_make_request(self._recording_handler(calls, data)):
                self._run_cron()
            self.assertNotIn('DELETE', calls)
            self.assertEqual(at_bank.state, 'draft')
            self.assertTrue(at_bank.epays_at_bank)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_the_cron_voids_the_link_left_open_on_an_invoice_paid_another_way(self):
            """ S7: nobody else reads it, and a customer could still pay it. """
            invoice = self.init_invoice(
                'out_invoice', partner=self.partner, amounts=[100.0], post=True,
                currency=self.currency,
            )
            tx = self._create_epays_transaction(
                state='draft', amount=invoice.amount_total, invoice_ids=[Command.set(invoice.ids)],
            )
            self.env['account.payment.register'].with_context(
                active_model='account.move', active_ids=invoice.ids,
            ).create({})._create_payments()
            self._set_create_date(tx, fields.Datetime.now() - timedelta(minutes=10))
            data = self._payment_data(tx, status='Pending', result='', transactionId=None)
            calls = []
            with self._patch_make_request(self._recording_handler(calls, data)):
                self._run_cron()
            self.assertIn('DELETE', calls)
            self.assertEqual(tx.state, 'cancel')

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_the_cron_voids_the_link_left_open_on_a_held_order(self):
            """ S12: the void after the refusal can fail, and it runs once. """
            self.ensure_installed('sale')
            _order, (held, left_open) = self._order_and_attempts('cancel', 'draft')
            self._notify(held, amount='1.000')
            self.assertTrue(held.epays_review_needed)
            self._set_create_date(left_open, fields.Datetime.now() - timedelta(minutes=10))
            data = self._payment_data(left_open, status='Pending', result='', transactionId=None)
            calls = []
            with self._patch_make_request(self._recording_handler(calls, data)):
                self._run_cron()
            self.assertIn('DELETE', calls)
            self.assertEqual(left_open.state, 'cancel')

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_later_refusal_holds_a_released_order_again(self):
            """ A release answers one refusal. """
            self.ensure_installed('sale')
            _order, (first, second) = self._order_and_attempts('cancel', 'cancel')
            self._notify(first, amount='1.000')
            first.action_epays_release_review()
            self._notify(second, amount='1.000')
            self.assertTrue(second.epays_review_needed)
            new_tx = self._create_epays_transaction(
                state='draft', amount=second.amount,
                sale_order_ids=[Command.set(second.sale_order_ids.ids)],
            )
            with self.assertRaises(ValidationError):
                new_tx._epays_check_replaced_payments()

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_released_order_reads_its_other_payments_before_a_new_one(self):
            """ S2: only the money already refused was reviewed. """
            self.ensure_installed('sale')
            order, (held, replaced) = self._order_and_attempts('cancel', 'cancel')
            self._notify(held, amount='1.000')
            held.action_epays_release_review()
            new_tx = self._create_epays_transaction(
                state='draft', amount=replaced.amount, sale_order_ids=[Command.set(order.ids)],
            )
            data = self._payment_data(
                replaced, status='Pending', result='', transactionId=None, inFlight=True
            )
            with self._patch_payment_details(data), self.assertRaises(ValidationError) as error:
                new_tx._epays_check_replaced_payments()
            self.assertIn("still being confirmed", error.exception.args[0])

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_cancelled_order_offers_no_release(self):
            """ The merchant has decided: there is nothing left to allow a payment for. """
            self.ensure_installed('sale')
            order, (held,) = self._order_and_attempts('cancel')
            self._notify(held, amount='1.000')
            self.assertTrue(held.epays_review_releasable)
            order._action_cancel()
            held.invalidate_recordset(['epays_review_releasable'])
            self.assertFalse(held.epays_review_releasable)
            held.action_epays_release_review()
            self.assertTrue(held.epays_review_needed)
            order.action_draft()
            held.invalidate_recordset(['epays_review_releasable'])
            self.assertTrue(held.epays_review_releasable, "The order was reopened.")

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_completed_payment_on_a_refunded_invoice_is_reported(self):
            """ S13: the customer got the money back, and this charge is not what was refunded. """
            invoice = self.init_invoice(
                'out_invoice', partner=self.partner, amounts=[100.0], post=True,
                currency=self.currency,
            )
            tx = self._create_epays_transaction(
                state='cancel', amount=invoice.amount_total, invoice_ids=[Command.set(invoice.ids)],
            )
            self.env['account.payment.register'].with_context(
                active_model='account.move', active_ids=invoice.ids,
            ).create({})._create_payments()
            invoice._reverse_moves().action_post()
            self._notify(tx)
            self.assertTrue(tx._epays_is_refused())
            self.assertTrue(self._notes(invoice, 'refund it on ePays'))

        def test_a_total_changed_by_less_than_a_fils_is_the_same_charge(self):
            """ S15: ePays charges in fils, and a cent of a weak currency is less than one. """
            self.ensure_installed('sale')
            _order, (tx,) = self._order_and_attempts('draft')
            bhd = self.env.ref('base.BHD')
            tx.write({
                'amount': 1000.0,
                'epays_charged_currency_id': bhd.id,
                'epays_charged_amount': 4.0,
                'epays_exchange_rate': 0.004,
            })
            for is_late in (False, True):
                with self.subTest(is_late=is_late), patch.object(
                    type(tx), '_epays_get_amount_due', return_value=999.99,
                ):
                    self.assertTrue(tx._epays_fits_amount_due(is_late))
            with patch.object(type(tx), '_epays_get_amount_due', return_value=999.0):
                self.assertFalse(tx._epays_fits_amount_due(False), "A whole fils is a change.")

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_s15_a_charge_above_what_is_due_is_refused_even_on_time(self):
            """ S15: the order was changed after the payment was created. """
            self.ensure_installed('sale')
            order = self._create_sale_order(confirm=False, currency_id=self.currency.id)
            tx = self._create_epays_transaction(
                amount=order.amount_total + 10, sale_order_ids=[Command.set(order.ids)],
            )
            self._notify(tx)
            self.assertTrue(tx._epays_is_refused())
            self.assertTrue(tx.epays_review_needed)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_charge_below_what_is_due_stays_a_partial_payment(self):
            """ S15: only Odoo has partial payments, and only a late one is told apart. """
            self.ensure_installed('sale')
            order = self._create_sale_order(confirm=False, currency_id=self.currency.id)
            tx = self._create_epays_transaction(
                amount=order.amount_total / 2, sale_order_ids=[Command.set(order.ids)],
            )
            self._notify(tx)
            self.assertEqual(tx.state, 'done')
            self.assertFalse(tx.epays_review_needed)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_late_charge_on_the_orders_own_payment_ignores_a_moved_rate(self):
            """ S11: it charged what was stored, as in WooCommerce. A customer whose link expired
            while the bank answered must not wait for a merchant because the rate moved. """
            self.ensure_installed('sale')
            order, (tx,) = self._order_and_attempts('cancel')
            bhd = self.env.ref('base.BHD')
            tx.write({
                'epays_charged_currency_id': bhd.id,
                'epays_charged_amount': tx.amount * 0.5,
                'epays_exchange_rate': 0.5,
            })
            with patch.object(
                type(tx), '_epays_convert_to_charge',
                return_value=(tx.amount * 0.4, bhd, 0.4),
            ):
                self.assertTrue(tx._epays_fits_amount_due(True))
                order.order_line[:1].product_uom_qty += 1
                self.assertFalse(tx._epays_fits_amount_due(True), "The order changed since.")

        def test_a_replaced_late_charge_in_another_currency_follows_the_rate_of_the_day(self):
            """ S12: what a new payment would charge now, as `rates->charge_for()` does. """
            self.ensure_installed('sale')
            order, (tx, _newer) = self._order_and_attempts('cancel', 'cancel')
            bhd = self.env.ref('base.BHD')
            tx.write({
                'epays_charged_currency_id': bhd.id,
                'epays_charged_amount': tx.amount * 0.5,
                'epays_exchange_rate': 0.5,
            })
            with patch.object(
                type(tx), '_epays_convert_to_charge',
                return_value=(tx.amount * 0.4, bhd, 0.4),
            ):
                self.assertFalse(tx._epays_fits_amount_due(True))
            with patch.object(
                type(tx), '_epays_convert_to_charge',
                return_value=(tx.amount * 0.5, bhd, 0.5),
            ):
                self.assertTrue(tx._epays_fits_amount_due(True))
            with patch.object(
                type(tx), '_epays_convert_to_charge', side_effect=ValidationError("No rate."),
            ):
                self.assertFalse(tx._epays_fits_amount_due(True), "No rate, no match.")

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_refused_charge_stays_refused_without_a_response_code(self):
            self.ensure_installed('sale')
            order, (paid, late) = self._order_and_attempts('draft', 'cancel')
            self._notify(paid)
            self._post_process_recording_calls(paid)
            late.state = 'cancel'

            self._notify(late, responseCode=None)
            self.assertTrue(late._epays_is_refused())
            notes = len(order.message_ids)
            changed = late.last_state_change
            late._epays_apply_payment_details(self._payment_data(late, responseCode=None))
            self.assertEqual(len(order.message_ids), notes, "Refused once.")
            self.assertEqual(late.last_state_change, changed)

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_voiding_the_other_attempts_does_not_keep_the_customer_waiting(self):
            self.ensure_installed('sale')
            _order, (paid, other) = self._order_and_attempts('draft', 'draft')
            self._notify(paid)
            timeouts = {}

            def handler(provider, method, path, timeout=None, **kwargs):
                timeouts[method] = timeout
                return {'status': 'Voided'}

            with self._patch_make_request(handler):
                paid._post_process()
                self._run_commit_hooks()
            self.assertEqual(other.state, 'cancel')
            self.assertEqual(timeouts, {
                'GET': const.POST_PROCESSING_TIMEOUT,
                'DELETE': const.POST_PROCESSING_TIMEOUT,
                'POST': const.POST_PROCESSING_TIMEOUT,
            })

        @mute_logger(TX_LOGGER, PAYMENT_TX_LOGGER, PROVIDER_LOGGER)
        def test_a_failure_after_the_commit_does_not_reach_the_request(self):
            tx = self._create_epays_transaction(state='done')

            def handler(provider, method, path, **kwargs):
                raise RuntimeError("not an API error")

            with self._patch_make_request(handler):
                tx._post_process()
                self._run_commit_hooks()  # Does not raise.

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

            with self._patch_make_request(lambda *args, **kwargs: {}):
                tx._post_process()
                self._run_commit_hooks()
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

            with self._patch_make_request(lambda *args, **kwargs: {}):
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
            with patch(REQUESTS_PATH, side_effect=self._fake_api(tx, calls)):
                self._run_cron()
            self._assert_payment_posted(tx)
            self._assert_sandbox_calls(calls)  # The acknowledgement too.

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
            self.assertEqual(
                [(call['method'], urlsplit(call['url']).path) for call in calls],
                [('GET', f'/api/v2/Payments/{tx.provider_reference}'),
                 ('POST', f'/api/v2/Payments/{tx.provider_reference}/acknowledgement')],
                "Post-processed by the ePays cron, the payment is acknowledged on the sandbox.",
            )

        def _assert_sandbox_calls(self, calls):
            self.assertTrue(calls)
            for call in calls:
                self.assertEqual(urlsplit(call['url']).hostname, 'testapi.epays.io')
                self.assertEqual(call['headers']['X-Api-Id'], 'sandbox-api-id')
                self.assertEqual(call['headers']['X-Api-Master-Key'], 'sandbox-master-key')
                self.assertEqual(call['headers']['X-Merchant-Domain'], 'shop.example.com')
