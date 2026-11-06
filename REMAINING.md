# ePays for Odoo — what is left

Status on 2026-11-06. The addon (`payment_epays` 18.0.1.0.8, not yet on the store) is
feature-complete and tested: 278 tests with sales and accounting, 229 with the module alone,
lint clean, upgraded from 18.0.1.0.7. 18.0.1.0.8 fixes what the audit against `PLUGIN_CHECKLIST.md`
found: a held charge is never applied, also after its release, failed reads count towards the
public limit, and more. It also takes the WooCommerce plugin's last review fixes, see *Review fixes
from WooCommerce* below. 18.0.1.0.7 aligns it with the WooCommerce plugin (see *Parity* in
`CLAUDE.md`) and encrypts the master keys, tested end-to-end against the local API with
`PAYMENT_IN_FLIGHT` (commit `c27da288`): shop checkout and decline in Arabic, card payment, decline
and retry, Back to Store, a void during 3-D Secure, a cancelled order voided by the cron, also at
3-D Secure. A charge landing on a voided payment cannot be forced on that server: mock tests only.
18.0.1.0.6 acknowledges finished payments on ePays
(`POST /api/v2/Payments/{id}/acknowledgement`): completed, declined and cancelled, but not money
ePays took that Odoo refused. It also follows ePays when a payment Odoo voided is charged anyway
(3-D Secure answered after the void), tested on the local API with two tabs. 18.0.1.0.5 was tested
end-to-end against the local API (new v2 names) and the sandbox (old names): shop approval,
decline and cancel in English and Arabic, the individual Card method, Sync from ePays, a USD
invoice paid from its portal link (charged in BHD), a payment request answered too late and its
orphan voided. What remains is shipping it, the ePays-side release steps below, and real payment
methods.

## Review fixes from WooCommerce (18.0.1.0.8)

The plugin's last review changed shared payment logic, so each fix was checked against the addon.
An independent review of the port then found seven more problems, fixed in the same version.

Browser pass on `19f90a0`, the merge with the audit, against the local API: card payment, decline
and retry, Back to Store, a void during 3-D Secure, a cancelled order's link voided by the cron, also
at 3-D Secure, a slow 3-D Secure answer, a declined payment paid on its own link, the Arabic shop,
and an Arabic contact on the English website, where the website's language wins: 10 of 10 scenarios
pass. A payment at the bank is covered by mock tests only: ePays reports `inFlight` while the bank
answers, not on 3-D Secure, so a browser cannot hold it there.

| Fix | Here | Why |
|---|---|---|
| A read that fails or is limited goes back to the cron | Ported | The notification had it (`0354b17`); the status page now does too, and the test is the cron's next run, as a read on its last one has no later one |
| A failed read in the cron itself keeps the payment watched | Added in both | S30: `_epays_record_failed_read`, for at most `CRON_MAX_AGE` after the first failure; a good read resets it. The S30 row is not in `CLAUDE.md` yet |
| The cron voids links nobody reads | Already here | The cron picks transactions by state, not orders, so it reads a held or paid document's links, and a charge on one is refused and reported |
| The customer paid another order while one was at the bank | Not applicable | WooCommerce only: Odoo keeps one cart order |
| Still at the bank past the watch: watched again | Ported | `_epays_record_at_bank`, S9 |
| A completed payment that could not be applied is read again, and the merchant told once | Ported | `_epays_apply_or_hand_back`: here a failed apply raises, as nothing returns a failure, S10, S29 |
| A void of gateway-approved money is not acknowledged | Ported | `_epays_took_no_money` read the status, not the code, S16. An orphan is listed as pending, as in WooCommerce |
| A release answers one refusal; a later one holds again | Already here | `epays_review_needed` is per transaction |
| A released payment is still read before a new one | Already here | `_epays_check_replaced_payments` and `_epays_find_live_attempts` |
| No release on a cancelled or refunded order | Ported | `epays_review_releasable`: documents that expect nothing, paid ones included |
| A declined payment paid again waits for the bank, and is kept | Ported | The retry already kept it. It was shown as declined, which sent the customer to pay again. It is not acknowledged meanwhile, S26 |
| "No money was taken" needs `inFlight` and no approved code | Not applicable | S24: Odoo's session follows the replacement, so it has no such message |
| A completed payment on a refunded order is reported | Already here, one difference | Odoo has no refunded order: the accounting decides. A refunded invoice owes nothing and its charge is reported. An order paid outside any transaction whose invoice was reversed is due again, as a reversal can also correct an invoice |
| The order total is compared in fils | Ported | `_epays_compare_in_charge_currency`, S15 |
| Merchant emails in the site language | Ported | `_epays_for_merchant`: the company's language, for every note, S28 |

## 1. Tests still to do

- [ ] **18.0.1.0.8 end to end** on the local ePays API: the 12 items of Part 4 in `CHECKLIST_ODOO.md`,
      once the WooCommerce end-to-end run frees the machine.
- [ ] **18.0.1.0.8 on real gateways** with sandbox credentials and real devices: the 6 items below it.
- [ ] A **real card payment** typed into the ePays page in a browser, up to the confirmed order and
      the posted payment. So far the gateway sessions were started, not completed with a card.
- [ ] **Benefit** and **BenefitPay** with test credentials.
- [ ] **Apple Pay** on Safari (iPhone or Mac). Hiding it on other browsers is only proven in Chrome.
- [ ] **Google Pay**, **Samsung Pay** and **Tabby**.
- [ ] A **declined** payment, then a successful retry, on a real gateway.
- [ ] The **QA site** `qa-erp.qissah-bh.com`:
  - [x] Confirm it runs Odoo **18.0**.
  - [x] Install the addon, set it to Test with sandbox keys, run the checkout end to end.
  - [x] A portal invoice payment and a payment link.
  - [ ] An order in another currency (converted to BHD).

## 2. Publish on the Odoo Apps Store

- [x] Register the GitHub repository (`ssh://git@github.com/epays-io/epays_plugins_odoo#18.0`) and check that
      the scan publishes the listing without a manifest error.
- [x] Add **screenshots** to `payment_epays/static/description/` (provider form, checkout,
      payment step after a decline) and reference them in `index.html`.
- [x] Choose the first public version: `18.0.1.0.3`, as published on the store; the migrations
      `18.0.1.0.1` and `18.0.1.0.3` stay.
- [x] Download the zip from the store and install it on a fresh Odoo 18: identical to the
      repository, installed cleanly with `website_sale` and `account_payment` (2026-09-26).
- [x] **Scan error on 2026-09-28**: resolved; the store scanned 18.0.1.0.4 and, on 2026-10-20,
      18.0.1.0.5.
- [x] **18.0.1.0.5 from the store** (2026-10-20): the downloaded zip is identical to commit
      `e3b74fa`; installed on fresh Odoo 18 databases (131 tests alone, 136 with `website_sale` and
      `account_payment`, all green); run on the test shop against the local ePays API: Sync from
      ePays (6 gateways), a Card payment opening only the card form and confirming the order, and
      an Arabic decline in Arabic for a customer whose contact is in English.

## 3. Release order with the ePays core API (2026-10-20)

- [ ] **Publish 18.0.1.0.8 before the ePays API with `PAYMENT_IN_FLIGHT` reaches production.** It
      refuses the void of a payment the customer may be paying (HTTP 409) and adds `inFlight`,
      `inFlightUntil` and `lateCapture` to the payment details. 18.0.1.0.6 and older treat that
      payment as voidable: the customer reads a connection error, and an orphan stays payable.
- [ ] **Publish 18.0.1.0.5 before the ePays API of 2026-10-18 reaches production.** That API
      renamed v2 fields (`gatewayId`/`merchantGateway` → `subscriptionId`, gateway type
      `gatewayId` → `gatewayTypeId`, `paidAt` → `completedAt`, statuses in snake case). On it,
      18.0.1.0.3 and 18.0.1.0.4 lose every synced gateway on *Sync from ePays* (and with them the
      individual payment methods) and open the full ePays page for Card, Benefit, etc.
      18.0.1.0.5 reads and sends both names.
- [ ] **ePays deploy runs `flyway migrate`**: `V20261018_1000` (idempotency `response_location`;
      without it payment creation took up to 15.8 s and Odoo timed out), `V20261015_0800`
      (double-encoded Arabic decline reasons), `V20261015_0700` (Arabic error messages). The
      sandbox has none of them yet: its Arabic declines fall back to the generic error.
- [ ] **ePays decisions** (eCore repository): keep the old v2 names for a while for merchants on
      older addon versions; log or refuse unknown request fields (a rename broke features with no
      error); record the payer's language switch on the ePays page (`paymentLang` stays the one
      Odoo sent, so the messages follow the website's language, not the switch).
- [ ] **Sandbox payment list ignores `status` and `udf2`** and omits them from its items, so the
      orphan cleanup voids nothing there until it gets the current API.
- [ ] **Sandbox has no acknowledgement endpoint** (2026-11-03: `POST /api/v2/Payments/{id}/
      acknowledgement` answers a bare 404). 18.0.1.0.6 then only logs an info line and completes
      the payment as before; payments stay unacknowledged there until it gets the current API.
- [ ] **A sandbox payment with 18.0.1.0.6**: the sandbox keys were cleared from the test shop
      after the 18.0.1.0.5 tests; enter them again to run one.

## 4. Onboard each merchant (ePays side)

For every shop that uses the addon:

- [ ] Active licence with at least one active gateway (card, Benefit, …).
- [ ] Issue the **API master key** (and a sandbox pair for testing).
- [ ] Register the shop's **domain** and the Odoo server's **public IP address** on the licence.
      The admin gets the IP from *Find this server's IP address* on the provider form.
- [ ] On Odoo.sh, re-check the IP after the project moves servers: the outbound address can change.

## 5. Decisions still open

- [ ] **Arabic translations**: written by the developer; have a native speaker review
      `payment_epays/i18n/ar.po`.
- [ ] **HSTS in development**: ePays sends `Strict-Transport-Security` on HTTPS in every
      environment (`SecurityHeadersMiddleware`), so browsers force `https://localhost:8069` after
      visiting the dev ePays. Limit it to production?
- [x] **ePays currency check**: ePays refuses any currency but BHD with 422
      `CURRENCY_NOT_SUPPORTED` (2026-10-21). The addon always sends BHD.
- [ ] **Exchange rates**: shops selling in other currencies need up-to-date rates (Accounting >
      Settings > *Automatic Currency Rates*); include it in merchant onboarding?
- [ ] **Refunds**: out of scope for both integrations, a separate later release, although ePays has
      a refund API (`POST /api/v2/payments/{id}/refunds`). A customer who paid an order twice is
      told, and refunded by the merchant on ePays. A refund made on ePays after a payment completed
      does not reach Odoo or WooCommerce.
- [ ] **A payment corrected on ePays after Odoo cancelled it**: past its watch the shop only sees it
      when ePays resends the notification (`POST /api/v2/Payments/{id}/notify-deliveries`), or the
      merchant clicks *Check ePays Status*. Who at ePays runs that reconciliation, and when?
- [ ] **An encryption key on Odoo.sh**: without `odoo.conf` access, the keys are encrypted with
      `database.secret`. Is an environment variable available there?

## 6. WooCommerce parity after the 18.0.1.0.8 audit

To do once its end-to-end run is over (`epays-woocommerce`, not changed by the audit):

- [ ] **S24:** `Checkout.php` still answers a refused void that is not at the bank with
      `generic_error()`, "We could not reach the payment service". Odoo now asks the customer to try
      again, without a connection failure.
- [ ] **S1 and S20:** copy the reworded rows of `PLUGIN_CHECKLIST.md` and `CLAUDE.md` to its parity
      table: the link is reused if nothing changed, and only `lineItems` depends on the sum.
- [ ] **Order lines:** `OrderSummary::lines` still drops every quantity when one line does not
      divide. Odoo now does it for that line alone, as section 3.5 says.
- [ ] **Test names:** Odoo's main test of each scenario is now `test_s<N>_…`. Its tests mirrored
      these names.
- [ ] **Field names:** compare with Odoo's `read_payment`: section 3.1 asks for one place.

## 7. Confirm with ePays

Asked in the handover page; the release does not wait for the answers.

- [ ] The terms and privacy URLs in the listing, `https://epays.io/terms` and `https://epays.io/privacy`,
      are the ones in the WooCommerce readme: the site answers every path with the same page.

## 8. Clean up the development environment

- [ ] Docker containers `epays-e2e-odoo` and `epays-e2e-pg`, and the volume `epays-e2e-data`:
      `docker rm -f epays-e2e-odoo epays-e2e-pg && docker volume rm epays-e2e-data`.
- [ ] The test child merchant `86952418` on the development ePays.
- [ ] The secret files kept for testing outside the repositories (`parent_master_key.txt`,
      `child2.json` in the session scratchpad).
- [ ] The local ePays provider is Test + Localhost + Published, a combination the addon now refuses
      on the next save: unpublish it or switch it to Sandbox.
