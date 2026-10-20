# ePays for Odoo — what is left

Status on 2026-10-20. The addon (`payment_epays` 18.0.1.0.5) is feature-complete and tested:
136 tests with sales and accounting, 131 with the module alone, lint and store checks clean,
upgraded from 18.0.1.0.3 and 18.0.1.0.4 (command line and Apps > Upgrade). End-to-end against the
local ePays core API (new v2 names) and the sandbox (old names): shop approval, decline and cancel
in English and Arabic, the individual Card method, Sync from ePays, a USD invoice paid from its
portal link (charged in BHD), a payment request answered too late and its orphan voided. What
remains is shipping it, the ePays-side release steps below, and real payment methods.

## 1. Tests still to do

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
- [ ] **Scan error on 2026-09-28**: the store reports *No module found in repository
      `ssh://git@github.com/epays-io/epays_plugins_odoo.git#18.0`*. The repository is not the
      cause: `18.0` is public, `payment_epays/` is at its root, the manifest parses, and nothing
      was pushed after the successful scan of 2026-09-26. The failing URL ends in `.git`, the registered one
      does not, so it is probably a second entry for the same repository. Check the repositories
      in the apps.odoo.com account: delete the duplicate and re-scan the original; if it still
      fails, write to apps@odoo.com.

## 3. Release order with the ePays core API (2026-10-20)

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
- [ ] **ePays currency check**: `CURRENCY_NOT_SUPPORTED` is defined but never raised, so ePays takes
      any currency code. The addon always sends BHD; other clients are not protected.
- [ ] **Exchange rates**: shops selling in other currencies need up-to-date rates (Accounting >
      Settings > *Automatic Currency Rates*); include it in merchant onboarding?
- [ ] **Refunds**: out of scope for this version (payments only).

## 6. Clean up the development environment

- [ ] Docker containers `epays-e2e-odoo` and `epays-e2e-pg`, and the volume `epays-e2e-data`:
      `docker rm -f epays-e2e-odoo epays-e2e-pg && docker volume rm epays-e2e-data`.
- [ ] The test child merchant `86952418` on the development ePays.
- [ ] The secret files kept for testing outside the repositories (`parent_master_key.txt`,
      `child2.json` in the session scratchpad).
- [ ] The local ePays provider is Test + Localhost + Published, a combination the addon now refuses
      on the next save: unpublish it or switch it to Sandbox.
