# ePays for Odoo — what is left

Status on 2026-09-26. The addon (`payment_epays` 18.0.1.0.3) is feature-complete and tested:
121 tests with sales and accounting, 116 with the module alone, lint and store checks clean,
installed and upgraded from the release zip on fresh databases. What remains is shipping it and
testing it with real payment methods.

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

- [ ] Register the GitHub repository (`ssh://git@github.com/epays-io/epays_plugins_odoo#18.0`) and check that
      the scan publishes the listing without a manifest error.
- [x] Add **screenshots** to `payment_epays/static/description/` (provider form, checkout,
      payment step after a decline) and reference them in `index.html`.
- [ ] Choose the first public version: keep `18.0.1.0.3`, or restart at `18.0.1.0.0` and remove
      the development migrations (`migrations/18.0.1.0.1`, `18.0.1.0.3`), which only matter to
      databases that installed the development builds.
- [ ] Download the zip from the store and install it on a fresh Odoo 18.

## 3. Onboard each merchant (ePays side)

For every shop that uses the addon:

- [ ] Active licence with at least one active gateway (card, Benefit, …).
- [ ] Issue the **API master key** (and a sandbox pair for testing).
- [ ] Register the shop's **domain** and the Odoo server's **public IP address** on the licence.
      The admin gets the IP from *Find this server's IP address* on the provider form.
- [ ] On Odoo.sh, re-check the IP after the project moves servers: the outbound address can change.

## 4. Decisions still open

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

## 5. Clean up the development environment

- [ ] Docker containers `epays-e2e-odoo` and `epays-e2e-pg`, and the volume `epays-e2e-data`:
      `docker rm -f epays-e2e-odoo epays-e2e-pg && docker volume rm epays-e2e-data`.
- [ ] The test child merchant `86952418` on the development ePays.
- [ ] The secret files kept for testing outside the repositories (`parent_master_key.txt`,
      `child2.json` in the session scratchpad).
- [ ] The local ePays provider is Test + Localhost + Published, a combination the addon now refuses
      on the next save: unpublish it or switch it to Sandbox.
