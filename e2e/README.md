# End-to-end tests

The addon in a real Odoo 18 shop, in Chrome, against the **local ePays API** and MPGS's test
environment: real card payments through 3-D Secure, and what a customer sees when something goes
wrong. The unit tests in `payment_epays/tests` mock ePays; these do not.

Local only: they need the local ePays API and MPGS's test environment, so CI does not run them.
They are not part of the addon: the release zip holds `payment_epays/` alone.

## What is covered

| Test | Covers |
|---|---|
| `payment.spec.ts` | An approved card payment confirms the order. A declined card shows its reason on the payment step, and a retry on the same order voids the declined link and pays (S4) |
| `retry.spec.ts` | Back from the ePays page and Pay again: with nothing changed, the same page (S1); with a changed cart, the old link voided and a new payment (S5) |
| `timeout.spec.ts` | A payment request ePays answers too late: the next click reaches the ePays page and pays, and the scheduled check voids the payment ePays created for the first click. Also the live failure of 18.0.1.0.3, where every next click got 409 `IDEMPOTENCY_CONFLICT` |

Tests tagged `@card` pay with MPGS test cards; the 18.0.1.0.3 test runs only in its own project.

## Prerequisites

- **The local ePays API** answering on `https://localhost:7124`, from `epays_api_dotnet`
  (`dotnet run --project ePaysCore --launch-profile ePaysCore`).
- **A merchant of that API** whose licence allows the domain `localhost` and the address
  `127.0.0.1`, with an active card (MPGS) gateway that accepts customers from Bahrain. The API's
  smoke tool creates one (`Tools/smoke`); the shops script then finds its credentials by itself.
  Otherwise set `E2E_EPAYS_API_ID` and `E2E_EPAYS_MASTER_KEY`.
- **Docker**, **Python 3**, **Node.js 22+** and **Google Chrome**.
- **The API's certificate**: exported with `dotnet dev-certs` when `dotnet` is installed, or the PEM
  file named by `E2E_CA_CERT`.

## Run

```sh
cd e2e
npm install                        # once
python scripts/shops.py up         # the current code's shop on http://127.0.0.1:8069 (admin/admin)
npm test                           # every test of the current code (about 10 minutes)
npm run test:no-card               # without card payments
python scripts/shops.py down       # remove the shops, their database and the state folder
```

To reproduce the live failure of 18.0.1.0.3 as well, start its shop on port 8070 too:

```sh
python scripts/shops.py up --with-18.0.1.0.3
npm run test:18.0.1.0.3
```

### On the ePays sandbox

The same tests run against `https://testapi.epays.io`, with a sandbox merchant whose licence allows
the domain sent (any: the shop itself is on `127.0.0.1`) and this machine's public IP address, and
whose card gateway takes customers from Bahrain. Put its credentials outside the repository, in
`%LOCALAPPDATA%\epays-e2e\sandbox.json` (`~/epays-e2e/sandbox.json` elsewhere):

```json
{"apiId": "…", "masterKey": "…", "domain": "the licensed domain"}
```

or in `E2E_EPAYS_API_ID`, `E2E_EPAYS_MASTER_KEY` and `E2E_EPAYS_DOMAIN`. Then:

```sh
python scripts/shops.py up --api sandbox
npm test
```

The shops reach the sandbox through an HTTPS proxy in their container, which holds answers as the
forwarder does. Not checked there: the orphan cleanup (the sandbox's payment list ignores `status`
and `udf2`, so the addon cannot tell its orphans). Acknowledgements reach it (HTTP 200 since
2026-11-23). Its notifications cannot reach `127.0.0.1`: the browser's return confirms the payments.

`shops.py up` starts Postgres and the shops in Docker (`epays-e2e-*`), installs `website_sale`,
`account_payment` and the addon with demo data, and configures the ePays provider for the local API
in Localhost mode. Running it again keeps the database and updates the modules.

## How it works

- **Reaching the API.** The addon calls the fixed Localhost URL, `https://localhost:7124`. In each
  shop's container, `docker/forward.py` forwards that address to the API on the host, and Odoo
  trusts the API's certificate through `REQUESTS_CA_BUNDLE`.
- **A slow ePays.** While `.state/delay_<shop>.flag` exists, the forwarder holds ePays's answer for
  40 seconds, longer than the addon waits. ePays receives and runs the request, as when the live
  server answered too late. `Shop.holdAnswers()` creates the flag.
- **The shop's address.** Odoo is opened on `127.0.0.1`, not `localhost`: the local ePays API sends
  HSTS for `localhost`, after which the browser opens the shop only over HTTPS.
- **The customer.** The admin pays: a provider in Localhost mode cannot be published, so only staff
  see it. `docker/configure.py` gives the admin an address in Bahrain (`E2E_CUSTOMER_COUNTRY`), as a
  gateway can be limited to some countries.
- **Isolation.** Each test cancels the admin's open carts first, and one test runs at a time.

## Known limits

- **MPGS's risk rules.** Its test merchant blocks a card that pays too often in a short time. The
  tests take turns over four test cards, and a test whose card is blocked is skipped with that reason,
  not failed: `npm run report` shows it. Several runs in an hour will skip more and more; a skipped
  test can be run alone later (`npx playwright test -g "<name>"`).
- **One merchant for both shops.** The two shops number their orders alike, and the addon's orphan
  cleanup goes by order number: the current shop's scheduled check can void a payment of the
  18.0.1.0.3 shop. This is the gap listed in `CHECKLIST_ODOO.md`, not a test fault.
- **A payment at the bank** cannot be held in a browser: ePays reports `inFlight` only while the bank
  answers. The unit tests cover it.
