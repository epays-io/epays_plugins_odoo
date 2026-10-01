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
- Release zip: `zip -r dist/payment_epays-<version>.zip payment_epays -x '*/__pycache__/*' '*.pyc'`.

## Architecture

Standard Odoo `payment` provider extension (`provider_code == 'epays'`); every override returns
early for other providers.

- **`models/payment_provider.py`** — credentials (production + sandbox API id / master key),
  `epays_mode` (`production` / `sandbox` / `localhost`, URLs in `const.API_URLS`), the single HTTP
  entry point `_epays_make_request` (all tests mock it or `requests.request`, see
  `tests/common.py`), admin actions (*Test connection*, *Find this server's IP address*, *Sync from
  ePays* which fills `payment.epays.gateway`), and 401 diagnosis by ePays reason codes.
- **`models/payment_transaction.py`** — the flow:
  1. `_get_specific_rendering_values` POSTs `/api/v2/Payments` (with an idempotency key), stores
     `provider_reference` = ePays `paymentId` plus environment, domain, expiry, charged amount/
     currency/rate and the `udf2`/`udf3` custom fields, then redirects. It first tries to reuse or
     retire an earlier pending ePays payment on the same order (one live payment per order).
  2. ePays returns to `/payment/epays/return[/<environment>]?paymentId=…` (controller). The
     transaction is found **only by stored paymentId (+ environment)**, never by data from the
     notification, and the status is always pulled with `GET /api/v2/Payments/{id}`.
  3. `_epays_apply_payment_details` maps status via `const.PAYMENT_STATUS_MAPPING`; `done` requires
     amount/currency/udf fields to match (`_epays_get_mismatches`), otherwise error.
  4. `_cron_epays_poll_pending` (`data/ir_cron.xml`) polls pending transactions in an age window and
     voids expired links on ePays.
- **Currency**: ePays only charges `BHD` (`const.CHARGE_CURRENCY`); other currencies are converted
  with Odoo rates at initiation and the conversion recorded on the transaction.
- **Payment methods** (`data/payment_method_data.xml`, `models/payment_method.py`): one generic
  "ePays" method (customer picks on the ePays page, with brand icons) plus individual methods
  (card, Benefit, BenefitPay, Apple Pay, …) that send a v2 `gatewayId` to open one gateway directly;
  these are only offered when a matching active synced gateway exists. Gateway type → method code is
  `const.GATEWAY_TYPE_PAYMENT_METHOD_CODES`.
- **Controllers** (`controllers/main.py`): the return route, and a `PaymentPostProcessing`
  override that sends failed/cancelled shop payments back to `/shop/payment?epays_failed=1`, where
  `static/src/js/payment_form.js` fetches the reason from `/payment/epays/failure`. The JS also hides
  Apple Pay on browsers without `ApplePaySession`.
- **Multi-company**: one ePays provider per company, created on install and for new companies
  (`res_company.py`, `_epays_ensure_provider_per_company`).
- `const.py` holds all tunables and protocol constants; `utils.py` holds `EpaysApiError` and pure
  helpers.

## Rules that are easy to break

- **Do not add stored fields to `payment.provider`.** A new column makes *Apps > Upgrade* fail on
  databases that lack it (Odoo recomputes provider colour, reading every column, before the upgrade
  adds it). Use a system parameter (like `const.SERVER_IP_PARAM`) or an addon model.
  `test_the_addon_adds_no_new_column_to_payment_provider` guards this.
  `migrations/18.0.1.0.3/pre-migrate.py` is how the one such column was moved to a parameter and
  dropped.
- **Master keys never reach the browser**: reads return `const.SECRET_MASK`, writing the mask back
  keeps the stored value, and `_read_format` / `_search` / `_read_group` overrides refuse secret
  fields. Keep secrets out of logs (`_redact`).
- Test + Localhost + Published is refused by a constraint; Localhost mode is for a local ePays API.
- Bump `version` in `__manifest__.py` for any release installed databases must pick up, and put data
  changes for existing databases in `payment_epays/migrations/<version>/`; migrations only undo
  values the addon set itself and keep what an administrator chose (see `18.0.1.0.1`).
- `data/neutralize.sql` runs on neutralised (staging) databases: it clears master keys, moves
  providers off production and deletes the server-IP parameter. A new secret or server-specific
  value must be added there (`test_neutralize_clears_the_keys`).
- Match the existing style: files start with the `# Part of ePays Payment Provider…` header;
  docstrings in Odoo's `""" … """` style with `:param:` / `:return:`; comments explain *why*.
