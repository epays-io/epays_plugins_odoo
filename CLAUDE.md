# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single Odoo **18.0** addon, `payment_epays` (LGPL-3), adding the ePays payment provider: the
customer is redirected to the ePays hosted payment page, and Odoo always re-fetches the payment
status from the ePays API before confirming. Branch `18.0` is the release branch the Odoo Apps
Store scans. `README.md` covers install/update for merchants; `payment_epays/README.rst` is the
module documentation; `REMAINING.md` tracks open work (shipping, real-gateway tests, store listing).

## Commands

Tests run inside the official `odoo:18` image against Postgres (from the repo root):

```sh
docker network create epays-net
docker run -d --name epays-pg --network epays-net \
  -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=odoo -e POSTGRES_DB=postgres postgres:16
docker run --rm --network epays-net -e HOST=epays-pg -e USER=odoo -e PASSWORD=odoo \
  -v "$PWD":/mnt/extra-addons odoo:18 \
  odoo -d test -i payment_epays,website_sale,account_payment \
  --test-enable --test-tags /payment_epays --stop-after-init
```

- Single class / method: `--test-tags /payment_epays:EpaysTest` or
  `--test-tags /payment_epays:EpaysTest.test_some_method`.
- CI (`.github/workflows/ci.yml`) runs two configurations: `-i payment_epays` alone and with
  `website_sale,account_payment`. Tests needing sale/accounting skip themselves in the first
  (`EpaysAccountingTest` is only defined when `account_payment` imports). CI also fails a run that
  executed 0 tests. Keep both configurations green.
- Lint: `pre-commit run --all-files` — pre-commit-hooks, pylint-odoo mandatory/store checks
  (`.pylintrc-mandatory`, `--valid-odoo-versions=18.0`), and ruff (`ruff.toml`: line length 100,
  py310, isort sections `odoo` and `odoo.addons` separate). Odoo `_()` uses `%` placeholders, not
  f-strings (UP031 ignored).
- Translations: after changing a user-facing string, re-export `payment_epays/i18n/payment_epays.pot`
  (`odoo -d test --i18n-export=/mnt/extra-addons/payment_epays/i18n/payment_epays.pot --modules=payment_epays --stop-after-init`)
  and merge into `i18n/ar.po` (e.g. `msgmerge`).
- Release zip, from the tagged commit, without tests: `zip -r dist/payment_epays-<version>.zip payment_epays -x '*/__pycache__/*' '*.pyc' '*/tests/*'`.

## Architecture

Standard Odoo `payment` provider extension (`provider_code == 'epays'`); every override returns
early for other providers.

- **`models/payment_provider.py`** — credentials (production + sandbox API id / master key),
  `epays_mode` (`production` / `sandbox` / `localhost`, URLs in `const.API_URLS`), the single HTTP
  entry point `_epays_make_request` (all tests mock it or `requests.request`, see
  `tests/common.py`), admin actions (*Test connection*, *Find this server's IP address*, *Sync from
  ePays* which fills `payment.epays.gateway`), and 401 diagnosis by ePays reason codes.
- **`models/payment_transaction.py`** — the flow:
  1. `_get_specific_rendering_values` first takes the documents' lock (`_epays_lock_documents`: an
     update, as a row lock would leave the second click on a stale snapshot; Odoo retries it). It
     POSTs `/api/v2/Payments` (with an idempotency key), stores
     `provider_reference` = ePays `paymentId` plus environment, domain, expiry, language, charged
     amount/currency/rate and rate source, and the `udf2`/`udf3` custom fields, then redirects. It first reads every live
     attempt on the same documents, reuses the newest if it can and voids the others, a declined
     one too (`_epays_find_live_attempts`: one live payment per order).
  2. ePays returns to `/payment/epays/return[/<environment>]?paymentId=…` (controller). The
     transaction is found **only by stored paymentId (+ environment)**, never by data from the
     notification, and the status is always pulled with `GET /api/v2/Payments/{id}`.
  3. `_epays_apply_payment_details` maps status via `const.PAYMENT_STATUS_MAPPING`; `done` requires
     amount/currency/udf fields to match (`_epays_get_mismatches`), otherwise error.
  4. `_cron_epays_poll_pending` (`data/ir_cron.xml`) polls pending transactions in an age window and
     voids expired links on ePays, and the links of cancelled documents: this addon depends on
     `payment` alone and cannot extend the cancellation of an order.
  5. A payment request that times out (`const.PAYMENT_REQUEST_TIMEOUT`) is rolled back, but ePays
     may still create the payment. `payment.epays.unanswered.request` records it in its own cursor
     (it must survive the rollback), and the cron voids the order's pending payments that no
     transaction holds (`GET /api/v2/Payments?status=pending&udf2=…`). Each attempt has its own
     idempotency key (`_epays_get_idempotency_key`): the rolled-back reference comes back. Tests
     for it cannot use Odoo's `assertRaises`, which rolls the separate cursor's write back too.
  6. `_post_process` registers a commit hook, `_epays_close_on_epays`, because `account_payment`
     wraps this override: the accounting payment does not exist yet when it runs. The hook runs in
     its own cursor, as writes after a commit are lost on the committed one, and swallows failures,
     as the request is already committed. A test must call `_run_commit_hooks`: test cursors
     never run them. It acknowledges `done`, `error` and `cancel` payments
     (`POST /api/v2/Payments/{id}/acknowledgement`), except money ePays took that Odoo refused
     (`_epays_is_refused`: `error` with response code `00`, written even if the server sent none),
     left for the merchant in ePays's unacknowledged list. `error` and `cancel` are read from ePays first
     (`_epays_took_no_money`): Odoo's state can lag behind a charge. ePays clears the mark when a
     payment later completes, so a declined payment paid on the same link is notified again. It is
     informational: a failure, e.g. the bare 404 of a server before the endpoint, is only logged.
     Odoo's state and `is_post_processed` prevent double processing.
     With `account_payment` installed, Odoo's `PaymentCommon` stubs its `_post_process`, cutting the
     ePays override out too: `EpaysAccountingTest` tests it for real.
  7. **ePays is the source of truth, not Odoo's `cancel`.** A server without `inFlight` cannot
     refuse a charge once the customer is past its page: a payment voided during 3-D Secure is still
     charged and turns `completed`. A server with it withholds that charge, but one that lost the
     race to a void still completes (`lateCapture`).
     So `_set_canceled` sets `epays_watch_until` (`_epays_get_watch_limit`), which the
     cron reads to, and the return route reads it at any age, as ePays can correct a payment days
     later and resend its notification (`POST /api/v2/Payments/{id}/notify-deliveries`; correct the
     status first, Odoo reads it). Public reads are limited (`const.FETCH_LIMIT`, per process; a
     read counts when it starts, a failed one too, as failed calls are what locks a licence, and a
     read that cannot run hands the transaction back to the cron at any age, `_epays_watch_again`, as
     ePays counts its call as delivered whatever Odoo did). `_epays_owns_payment`: not
     when a newer transaction took the payment over. Only a `done` target moves it
     (`_set_done`/`_set_error` with `cancel` allowed). `_epays_complete` locks the documents'
     transactions, then asks the accounting what is due (`_epays_get_amount_due`: residuals and
     Odoo's paid transactions, not a sum of attempts; a cancelled document is owed nothing). Nothing
     due: refused with response code `00`, noted on the documents, emailed to the company
     (`_epays_alert_merchant`) and left for a refund on ePays. A charge above what is due, or a late
     replaced one that is not what a new payment would charge now, is held (`_epays_hold_payment`,
     `epays_review_needed`): the other attempts are stopped and no new payment is taken until the
     merchant clicks *Allow a New Payment* (an admin, or staff who can edit every document). A refused
     charge is final, released or not: `_epays_complete` stops on `epays_charge_refused`, as its
     `error` state no longer says it was late, and response code `00` alone does not tell: a payment
     ePays declined with it can still be paid on the same link. A second transaction holding the same ePays payment
     (`_epays_get_payment_holders`) is one charge, not a refusal. A done payment voids the other
     attempts no longer needed on its documents (`_epays_void_other_attempts`, declined ones too:
     ePays lets them retry on the same link). `poll_status` and `_get_last` follow the transaction
     that paid, so a customer who paid never waits on, or reads about, a replacement that was
     voided; a refused charge is the exception, shown even when it is the older transaction.
  8. **A payment with the gateway is not voided** (`epays_utils.is_at_bank`: `inFlight`, on a
     server that sends it, also on a declined payment, as ePays lets its customer pay again on the
     link; or pending with a `transactionId`, which stays empty in the BenefitPay wallet). The
     bank's answer can arrive after
     the void and take the money. `_epays_check_replaced_payments` reads the replaced attempts
     before anything is voided, as raising rolls the request back but not a void; the customer is
     asked to wait (`_epays_raise_still_confirming`), and the status page keeps waiting
     (`poll_status`, and the `state_header` template). Only the expiry void and cancelled documents
     void it. A server with `inFlight` refuses such a void with HTTP 409 `PAYMENT_IN_FLIGHT`: nothing
     reads the code, as a refused void always re-reads the payment. `_epays_void_other_attempts`
     runs once, so the cron voids what it left open (`_epays_is_left_open`), and a refused orphan
     keeps its `payment.epays.unanswered.request`. On a server without `inFlight`, a payment voided
     during 3-D Secure has no `transactionId` and cannot be told apart.
- **Currency**: ePays only charges `BHD` (`const.CHARGE_CURRENCY`); other currencies are converted
  with Odoo rates at initiation and the conversion recorded on the transaction.
- **Payment methods** (`data/payment_method_data.xml`, `models/payment_method.py`): one generic
  "ePays" method (customer picks on the ePays page, with brand icons) plus individual methods
  (card, Benefit, BenefitPay, Apple Pay, …) that send the gateway's v2 `subscriptionId` to open it
  directly; these are only offered when a matching active synced gateway exists. Gateway type →
  method code is `const.GATEWAY_TYPE_PAYMENT_METHOD_CODES`.
- **Controllers** (`controllers/main.py`): the return route, and a `PaymentPostProcessing`
  override that sends failed/cancelled shop payments back to `/shop/payment?epays_failed=1`, where
  `static/src/js/payment_form.js` fetches the reason from `/payment/epays/failure`. The JS also hides
  Apple Pay on browsers without `ApplePaySession`.
- **Multi-company**: one ePays provider per company, created on install and for new companies
  (`res_company.py`, `_epays_ensure_provider_per_company`).
- `const.py` holds all tunables and protocol constants; `utils.py` holds `EpaysApiError` and pure
  helpers, among them the mismatch and API failure checks. Customer messages are in
  `_epays_compose_customer_message`, except the generic one of a failed call.

## Rules that are easy to break

- **ePays renames v2 fields.** On 2026-10-18 `gatewayId`/`merchantGateway` became `subscriptionId`,
  the gateway type `gatewayId` became `gatewayTypeId`, `paidAt` became `completedAt`, and statuses
  went snake case. Servers update at different times, so read new and old names with
  `epays_utils.get_first(data, new, old)`, send both in requests (ePays ignores unknown fields), and
  compare statuses case-insensitively. ePays ignores unknown request fields silently: a rename
  breaks features without an error, so check eCore's DTOs when it changes.

- **Do not add stored fields to `payment.provider`.** A new column makes *Apps > Upgrade* fail on
  databases that lack it (Odoo recomputes provider colour, reading every column, before the upgrade
  adds it). Use a system parameter (like `const.SERVER_IP_PARAM`) or an addon model.
  `test_the_addon_adds_no_new_column_to_payment_provider` guards this.
  `migrations/18.0.1.0.3/pre-migrate.py` is how the one such column was moved to a parameter and
  dropped.
- **Master keys never reach the browser**: reads return `const.SECRET_MASK`, writing the mask back
  keeps the stored value, and `_read_format` / `_search` / `_read_group` / `_export_rows` overrides refuse secret
  fields. Keep secrets out of logs (`_redact`).
- **Master keys are encrypted in their columns** (`const.SECRET_PREFIX`, `_epays_clean_credentials`):
  read them with `_epays_read_secret`, never as field values. The key is `epays_encryption_key` in
  `odoo.conf` or `EPAYS_ENCRYPTION_KEY`, else `database.secret`. An unreadable key is no key: it must
  not fall back on another one.
- Test + Localhost + Published is refused by a constraint; Localhost mode is for a local ePays API.
- Bump `version` in `__manifest__.py` for any release installed databases must pick up, and put data
  changes for existing databases in `payment_epays/migrations/<version>/`; migrations only undo
  values the addon set itself and keep what an administrator chose (see `18.0.1.0.1`).
- `data/neutralize.sql` runs on neutralised (staging) databases: it clears master keys, moves
  providers off production and deletes the server-IP parameter. A new secret or server-specific
  value must be added there (`test_neutralize_clears_the_keys`).
- Match the existing style: files start with the `# Part of ePays Payment Provider…` header;
  docstrings in Odoo's `""" … """` style with `:param:` / `:return:`; comments explain *why*.

## Parity with the WooCommerce plugin

This addon and `epays-woocommerce` are one integration on two platforms. A change to shared payment logic
is made in both, with a test named after its scenario in both. Refunds are out of scope for both.

| # | Scenario | Expected in both |
|---|---|---|
| S1 | Retry while the current payment is at the bank and its link is live | Its link is reused if nothing changed and at least 5 minutes are left; otherwise not voided, no new payment, "still being confirmed" |
| S2 | Retry while a replaced payment is at the bank | Same, and nothing is voided first |
| S3 | Retry when a replaced payment has completed | No new payment; the order is settled |
| S4 | Retry after a decline | Declined link voided before the new payment |
| S5 | Retry with under 5 minutes left on the link | Old link voided, new payment created |
| S6 | Void refused on retry | Re-read; no second payment unless it turned out paid |
| S7 | Another payment settles the order | Other attempts voided, except any at the bank; the scheduler voids what stays open, until the watch ends |
| S8 | Order or document cancelled with an open link | Link voided on ePays, even at the bank |
| S9 | Replaced payment watched by the scheduler | Until `max(link expiry + 30 min, replaced + 2 h)`; `max(link expiry, replaced) + 3 days` at the bank, also when found there later or still there after the watch |
| S10 | Callback for a replaced payment | Read at any age; a read that is limited, fails or cannot be applied goes back to the scheduler |
| S11 | Late charge that matches what is due now | Order paid with it; other payment voided |
| S12 | Late charge that does not match what is due now | Nothing recorded, other attempts stopped, new payments blocked until released; merchant told by note, log and email; customer told |
| S13 | Second charge on a paid order | Nothing recorded; merchant told by note, log and email; customer told |
| S14 | Current payment mismatch | Not paid, customer told to contact the shop, merchant told by note, log and email, new payments blocked until released |
| S15 | Order total changed after the payment was created | Compared in fils at the stored rate. A charge above what is due is refused as in S12 |
| S16 | Acknowledgement | Completed, and declined, cancelled or voided with no money taken: acknowledged. Refused, refunded, or voided after the gateway approved the money: not |
| S17 | Public routes | At most 5 ePays reads a minute per payment attempt; the scheduler and admin action are exempt; malformed ids rejected without a lookup |
| S18 | Waiting page | Each poll reads ePays within the limit; a customer whose replaced payment is at the bank keeps waiting |
| S19 | Admin status check | Reads the current and all replaced payments, at any age |
| S20 | Request content | Same fields; `lineItems` only when they add up to the fils; description "Order N" in the payment language |
| S21 | Master key handling | Encrypted with prefix `epays1:`; refused over plain HTTP; an unreadable key warns the admin and never falls back to another key |
| S22 | Admin warnings | Unreadable key, sandbox shown to customers, no exchange rate, rates over 7 days old |
| S23 | Gateway sync | Follows `nextCursor`, up to 10 pages |
| S24 | Void refused while the customer may be paying | Not reported as a connection failure, no second payment, void retried once ePays allows it |
| S25 | Merchant releases a refused payment | Answers that refusal only: a later one holds again, and other payments are still read before a new one. Not offered when the document expects nothing |
| S26 | Declined payment paid again on its link | Not reported as declined while at the bank; the customer waits; it is not replaced |
| S27 | Completed payment on a refunded order | Reported, not confirmed |
| S28 | What the merchant is told | In the site's language, whoever triggered the read |
| S29 | Completed payment that cannot be applied | Read again; the merchant is told once, by note, log and email |

**The one intentional difference in logic:** a late charge on a cancelled order. WooCommerce restores
the order; Odoo refuses the charge and leaves a refund, as reopening a cancelled Odoo order can have
undone pickings and invoices.

**Platform differences, same intent:**

- S3: Odoo settles on the next cron run, as raising rolls the request back and a second cursor can
  deadlock. S8: Odoo voids on the next cron run, as it depends on `payment` alone: within 15
  minutes, or 20 for a payment under 5 minutes old (`CRON_MIN_AGE`).
- S15 below what is due: Odoo keeps a partial payment, unconfirmed; WooCommerce has none and holds the
  order. Only a late payment can be told apart from an intended partial one.
- S12, S14: WooCommerce holds an order with On hold or Failed and the *Allow a new ePays payment*
  action; Odoo has no such state, so `epays_review_needed` and *Allow a New Payment* do it.
- S17: the limit is per transaction and per Odoo process; WooCommerce shares one limit between
  an order's attempts and its workers, and is the stricter. A limited read goes back to the
  scheduler in both.
- S24: WooCommerce tells a customer back from a replaced payment that no money was taken, on the
  order-pay page. Odoo's session follows the replacement, so that customer never reads it there.
- S25: Odoo also hides the release on paid documents, as they expect no payment.
- S27: Odoo has no refunded order, so the accounting decides. A refunded invoice owes nothing; an
  order paid outside any transaction whose invoice was reversed is due again, as a reversal can also
  correct an invoice.
- S28: Odoo uses the company's language, as it has no site locale.
- WooCommerce only: retiring the links of the customer's other orders (Odoo keeps one cart order),
  one at the bank included;
  credentials in `wp-config.php` (Odoo providers are per company); the Localhost redirect exception;
  Classic and Blocks; multi-currency adapters; the privacy eraser; the hold-stock guard; a
  cancelled order's thank-you text (Odoo's status page shows the payment as cancelled).
- Odoo only: the merchant domain as a setting (several websites); multi-document `udf2`/`udf3`;
  orphan cleanup limited to orders (ePays lists by `udf2`); multi-company; neutralize.
