# ePays for Odoo — what is left

Status on 2026-09-26. The addon (`payment_epays` 18.0.1.0.3) is feature-complete and tested:
121 tests with sales and accounting, 116 with the module alone, lint and store checks clean,
installed and upgraded from the release zip on fresh databases. What remains is shipping it and
testing it with real payment methods.

## 1. Save the work in Git

- [x] **Addon repository** (`D:\GitHub\epays-odoo`, branch `18.0`), pushed to
      <https://github.com/epays-io/epays_plugins_odoo>.
  - [x] First commit (`fb77586`).
  - [x] Push the `18.0` branch to `epays-io/epays_plugins_odoo`.
  - [x] Make `18.0` the default branch on GitHub.
  - [x] Delete the unrelated `master` branch on GitHub; `18.0` is the only branch.
  - [x] Replace the `<org>/epays-odoo` placeholders in `README.md` (clone and submodule URLs,
        folder names) with `epays-io/epays_plugins_odoo`.
  - [x] Check that the CI workflow (`.github/workflows/ci.yml`: lint + tests in two configurations)
        passes on GitHub: green on `fbe636c` (116 tests alone, 121 with sale and accounting).
- [ ] **ePays repository** (`D:\GitHub\epays_api_dotnet`, branch `feature/odoo-payment-provider`):
      30 changed files, uncommitted.
  - [ ] Run the full gate first: `.\ci-local.ps1` (Release, `-warnaserror`, full suite).
  - [ ] Commit, open the pull request to `master`.

## 2. Deploy the ePays API changes

The addon relies on these ePays v2 changes, which exist only on the development server so far:

| Change | Without it |
|---|---|
| Payment details return `status`, `customFields`, `responseDescription`, `gatewayReference`, `gatewayResponse`, `updatedAt` | Falls back to the raw `result`; no reference check, no gateway details in the order note |
| X004 error carries `error.details.observedIp` | *Test connection* cannot show the IP ePays saw (*Find this server's IP address* still works via a public echo service) |
| v2 `gatewayId` narrows the payment to that gateway (`INVALID_GATEWAY_ID` otherwise) | **Card, Benefit, Apple Pay, … do not open their gateway directly** |
| A v2 initiation with an `Idempotency-Key` skips the legacy duplicate check | **Two attempts on one order can share one ePays payment** (the addon cancels the older one, but should not have to) |
| `AddMerchant` refuses a missing name, domain or URL; `trackId` accepted only when numeric | Onboarding and data-quality fixes |

- [ ] Deploy to **testapi.epays.io** (sandbox) and repeat the end-to-end test against it.
- [ ] Deploy to **api.epays.io** (production).

## 3. Tests still to do

- [ ] A **real card payment** typed into the ePays page in a browser, up to the confirmed order and
      the posted payment. So far the gateway sessions were started, not completed with a card.
- [ ] **Benefit** and **BenefitPay** with test credentials.
- [ ] **Apple Pay** on Safari (iPhone or Mac). Hiding it on other browsers is only proven in Chrome.
- [ ] **Google Pay**, **Samsung Pay** and **Tabby**.
- [ ] A **declined** payment, then a successful retry, on a real gateway.
- [ ] The **QA site** `qa-erp.qissah-bh.com`:
  - [ ] Confirm it runs Odoo **18.0**.
  - [ ] Install the addon, set it to Test with sandbox keys, run the checkout end to end.
  - [ ] A portal invoice payment and a payment link.
  - [ ] An order in another currency (converted to BHD).

## 4. Publish on the Odoo Apps Store

- [ ] Register the GitHub repository (`ssh://git@github.com/epays-io/epays_plugins_odoo#18.0`) and check that
      the scan publishes the listing without a manifest error.
- [ ] Add **screenshots** to `payment_epays/static/description/` (provider form, checkout,
      payment step after a decline) and reference them in `index.html`. Only the icon and the
      banner exist today.
- [ ] Choose the first public version: keep `18.0.1.0.3`, or restart at `18.0.1.0.0` and remove
      the development migrations (`migrations/18.0.1.0.1`, `18.0.1.0.3`), which only matter to
      databases that installed the development builds.
- [ ] Download the zip from the store and install it on a fresh Odoo 18.

## 5. Onboard each merchant (ePays side)

For every shop that uses the addon:

- [ ] Active licence with at least one active gateway (card, Benefit, …).
- [ ] Issue the **API master key** (and a sandbox pair for testing).
- [ ] Register the shop's **domain** and the Odoo server's **public IP address** on the licence.
      The admin gets the IP from *Find this server's IP address* on the provider form.
- [ ] On Odoo.sh, re-check the IP after the project moves servers: the outbound address can change.

## 6. Decisions still open

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

## 7. Clean up the development environment

- [ ] Docker containers `epays-e2e-odoo` and `epays-e2e-pg`, and the volume `epays-e2e-data`:
      `docker rm -f epays-e2e-odoo epays-e2e-pg && docker volume rm epays-e2e-data`.
- [ ] The test child merchant `86952418` on the development ePays.
- [ ] The secret files kept for testing outside the repositories (`parent_master_key.txt`,
      `child2.json` in the session scratchpad).
- [ ] The local ePays provider is Test + Localhost + Published, a combination the addon now refuses
      on the next save: unpublish it or switch it to Sandbox.
