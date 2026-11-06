# ePays integration checklist

What every ePays integration must meet before it goes live, whether it is a plugin for a shop platform
or a custom integration for one site. "Plugin" below means either, unless the table in *What applies to
you* says otherwise.

This checklist says **what must be true**. [INTEGRATION_GUIDE.md](INTEGRATION_GUIDE.md) says **how**,
with the API calls, parameters and curl commands. Read the guide first.

## How to use it

1. Copy this file into your repository at the start of the work.
2. Use *What applies to you* below to mark what you can skip.
3. Answer [Part 1](#part-1-decide-before-coding) before writing any code.
4. Copy the values in [Part 2](#part-2-fixed-values) as they are. Do not tune them.
5. Tick an item only when it is done and tested. Mark an item that cannot apply as `N/A`, with the
   reason.
6. You are finished when every box is ticked or `N/A`, and [Part 5](#part-5-acceptance-contract) has a
   test name in every row. An empty row means unfinished.

`S1` to `S24` are the acceptance scenarios in Part 5. An item marked `(S4)` is tested by scenario S4.

## What applies to you

| Section | Plugin | Custom integration for one site |
|---|---|---|
| Part 1 | All | All, answered for your site. Skip the marketplace item |
| Part 2 | All | All |
| 3.1 Project structure | All | Recommended |
| 3.2 Admin configuration | All | Settings can live in server configuration. Test the connection with curl. Skip the IP button |
| 3.3 Master keys | All | All, but a secret manager or protected environment variable can replace the `epays1:` format |
| 3.4 to 3.11 Payment logic | All | All. These protect the money |
| 3.12 to 3.15 Checkout, language, currency, gateways | All | What your site offers. A BHD-only site skips conversion, and a site that offers only the generic method skips gateway sync |
| 3.16 Merchant tools | All | All, but they can be an internal tool or script |
| 3.17 Data lifecycle | All | Skip upgrades and uninstall. Staging and privacy apply |
| Part 4 | All | All, except the marketplace check |
| Part 5 | All | All. Mark S21 and S22 `N/A` only where the rows above allow it |
| Part 6 | All | Skip the marketplace listing and the published package check |

## Reference code

This checklist comes from the two existing integrations. Read their code when an item is unclear, but
follow this checklist where they differ from it.

| Integration | Repository | Version | State on 2026-11-06 |
|---|---|---|---|
| Odoo 18 addon | `epays-io/epays_plugins_odoo` | 18.0.1.0.8 | On `18.0`, the branch the Odoo Apps Store scans |
| WooCommerce plugin | `epays-io/epays_plugins_woocommerce` | 1.0.0 | On `release-1.0.0`, not published |

| Topic | Odoo (`payment_epays/`) | WooCommerce (`src/`) |
|---|---|---|
| Fixed values | `const.py` | `Constants.php` |
| HTTP client | `models/payment_provider.py` | `Api/Client.php` |
| Creating a payment | `models/payment_transaction.py` | `Payment/Checkout.php`, `Api/PaymentRequest.php` |
| Order lines | `utils.py` | `Payment/OrderSummary.php` |
| Callback | `controllers/main.py` | `Http/ReturnController.php` |
| Applying a result | `models/payment_transaction.py` | `Payment/ResultApplier.php` |
| Late charges | `models/payment_transaction.py` | `Payment/LateCharges.php` |
| Scheduled job | `data/ir_cron.xml`, `models/payment_transaction.py` | `Scheduler/Jobs.php` |
| Encrypting keys | `utils.py` | `Settings/SecretStore.php` |
| Test scenarios | `tests/` | `tests/` at the repository root |

### Known gaps in the existing integrations

None known on 2026-11-06.

## Terms

| Term | Meaning |
|---|---|
| **Document** | What is being paid: an order, or an invoice. The guide says "order" |
| **Attempt** | One ePays payment created for a document. A document can have several |
| **Link** | The ePays page URL of an attempt. It has an expiry |
| **Void** | Closing a payment on ePays, so that it can no longer be paid |
| **Replaced attempt** | An earlier attempt the document stopped using. It is not over: see section 3.9 |
| **Watching** | Reading a replaced attempt on a schedule, as it can still be charged: see section 3.9 |
| **At the bank** | The customer's money may be moving: see section 3.7 |
| **Mismatch** | The amount, currency or references ePays reports differ from what was stored |
| **Held** | The document takes no payment until the merchant releases it: see section 3.9 |
| **Refused charge** | Money ePays took that the shop did not record |
| **Orphan** | A payment ePays created after the shop's request timed out, which no attempt holds |
| **Scheduler** | A task your system runs every 15 minutes: see section 3.10 |
| **Acknowledge** | Tell ePays the shop has dealt with a payment: see section 3.11 |
| **Fils** | One thousandth of a Bahraini dinar (BHD) |

---

## Part 1: Decide before coding

Answer each question for your platform or site, and record it in your developer guide. These answers
are where integrations may differ. The payment logic is not.

| Decision | Why it matters | Odoo | WooCommerce | Your answer |
|---|---|---|---|---|
| Where does each payment record live? | Replaced ones must be kept | One record per attempt | Order data, with a list of replaced payments | |
| Which state means held? | No new payment until released | None, so a review flag and a release button | On hold or Failed, and a release action | |
| Is reopening a cancelled order safe? | Decides a late charge on one | No: **refuse the charge** | Yes: **restore the order** | |
| Do you have partial payments? | Decides a charge below what is due | Yes: stays partial | No: held | |
| Can you hook an order's cancellation? | Otherwise the scheduler voids its link | No: scheduler | Yes: at once | |
| Does a changed cart keep its order? | If not, retire the other orders' links too | Keeps it | New order | |
| Does an error roll the request back? | A timeout record must survive it | Yes: own transaction | No | |
| What stops two requests paying one order? | Needed for every payment step | Database row locks | A named lock | |
| Where is the read limit counted? | Shared is stricter | Per process | Shared cache | |
| Where are keys kept, and with what cipher? | Keys must be encrypted at rest | Database, Fernet | Database or config file, libsodium | |
| Where does the exchange rate come from? | ePays charges only in BHD | Platform rates | Currency plugins or manual rates | |
| How do you know the customer's language? | Sets the ePays page language | Website language | The page's `lang`, posted by a script | |
| Is one install one licence? | Sets where credentials live | No: per company | Yes | |
| What protects a staging copy? | It must not take real money | Clears the keys | Shows a warning | |

- [ ] Every decision above is answered, in the *Your answer* column.
- [ ] Plugin only: the platform's marketplace rules are read: licence, review process, listing assets,
      external service disclosure, banned practices.
- [ ] The minimum platform and language versions are chosen.

---

## Part 2: Fixed values

Keep these in one constants file, with a comment explaining why for each tunable. The guide explains
how each one is used.

### API

| Item | Value |
|---|---|
| Production | `https://api.epays.io` |
| Sandbox | `https://testapi.epays.io` |
| Localhost, development only | `https://localhost:7124` |
| Envelope | `{status, data, error}`; `status` of `ERROR` is a failure even on HTTP 200 |
| Auth headers | `X-Api-Id`, `X-Api-Master-Key`, `X-Merchant-Domain` |
| Other headers | `Accept: application/json`, `User-Agent`, `Content-Type` with a body, `Idempotency-Key` on create |

| Call | Used for |
|---|---|
| `POST /api/v2/Payments` | Create a payment. Answers `paymentId` and `redirect` |
| `GET /api/v2/Payments/{id}` | Read a payment |
| `GET /api/v2/Payments?limit=1` | Test connection |
| `GET /api/v2/Payments?status=pending&udf2=…&limit=100` | Find orphans |
| `DELETE /api/v2/Payments/{id}` | Void. Answers 409 `PAYMENT_IN_FLIGHT` while the customer may be paying |
| `POST /api/v2/Payments/{id}/acknowledgement` | Mark as processed. Older servers answer 404 |
| `GET /api/v2/gateways?limit=100&cursor=…` | Sync gateways, following `nextCursor` |

### Limits and timing

| Item | Value |
|---|---|
| Request timeout | 10 s |
| Create-payment timeout | 30 s. The platform's process limit must outlive it (WooCommerce sets 90 s) |
| Acknowledgement timeout | 5 s |
| Default link validity | 60 min |
| `expiryDate` | `YYYY-MM-DDTHH:MM:SS` in Bahrain time (UTC+3, no daylight saving), no offset |
| Timestamps without an offset | Bahrain time |
| Reuse a link only with at least | 5 min left |
| Scheduler interval | 15 min |
| Scheduler picks attempts aged | 5 min to 3 days |
| Void grace after link expiry | 30 min |
| Late-charge window | 2 h |
| Unanswered request | Cleaned up from 2 min, given up after 1 day |
| Public reads | 5 per 60 s |
| Payment id format | `^[A-Za-z0-9_-]{1,64}$` |
| `description`, line texts, `udf2`, `udf3` | 255 characters |
| Reference list | Joined with `, `, cut at a whole reference, ending in `…` |
| Exchange rate is stale after | 7 days |
| Gateway sync | Up to 10 pages of 100 |
| Charge currency | `BHD`, 3 decimals |
| Approved response code | `00` |
| Secret mask | `********` |
| Encrypted secret prefix | `epays1:` |

### Status mapping

Compare after trimming and ignoring case. Read `status`, then `result` when it is empty. **Anything
else leaves the document untouched.**

| Outcome | ePays values |
|---|---|
| Pending | `pending`, `3ds authentication initiated` |
| Done | `completed`, `captured`, `approved` |
| Refunded | `refunded`: never completes a document |
| Error | `failed`, `declined`, `error` |
| Cancel | `cancelled`, `canceled`, `expired`, `voided` |

### Payment methods

| Gateway type | Method code | Billing countries |
|---|---|---|
| 1 | `epays_benefit` | BH |
| 2 | `epays_card` | Any |
| 3 | `epays_google_pay` | Any |
| 4 | `epays_apple_pay` | Any |
| 5 | `epays_tabby` | BH, SA, AE, KW |
| 6 | `epays_benefit_pay` | BH |
| 7, 8 | Platform integrations: never offered at checkout | |
| 9 | `epays_samsung_pay` | Any |

The generic method is `epays`, where the customer chooses on the ePays page. A gateway can take
payments only with status `active`.

### 401 reasons

Read from `error.details.reason`, and turn each into a clear message for the admin.

`X000_MISSING_LICENSE`, `X002_INVALID_LICENSE_DOMAIN`, `X004_INVALID_LICENSE_IP`, `TOO_MANY_ATTEMPTS`

---

## Part 3: Requirements

### 3.1 Project structure

- [ ] One constants file holds every protocol value and tunable.
- [ ] One class or method is the **only** HTTP entry point, so tests can mock it.
- [ ] One place reads ePays field names.
- [ ] One place holds every customer message.
- [ ] The HTTP layer, status mapping, mismatch check, amounts and order lines are pure code with unit
      tests.
- [ ] Platform hooks only delegate.
- [ ] Code comments explain why, not what.

### 3.2 Admin configuration

**Connection settings, all on one screen**

- [ ] API ID.
- [ ] API Master Key.
- [ ] Sandbox API ID and Sandbox API Master Key, optional. They work as a pair: outside Production they
      are used only when both are filled in, otherwise the production pair is used.
- [ ] Mode: Production, Sandbox, Localhost. The mode is stored on every attempt as its environment, as
      sandbox and production number payments independently.
- [ ] *Test connection* button. It tests the saved settings of the selected mode with
      `GET /api/v2/Payments?limit=1`, says when credentials are missing without calling ePays, names
      the mode, URL and domain, and shows the 401 reason in plain words.

**Other settings and actions**

- [ ] Production and Sandbox URLs are constants. Only Localhost may be configurable.
- [ ] Settings: link validity, payment page language (customer, English, Arabic), send customer
      details.
- [ ] *Find this server's IP address* button, for the licence. It asks ePays first, as a refusal
      reports the address ePays saw, then a public IP service.
- [ ] *Sync from ePays* button.
- [ ] Test modes cannot reach customers by accident: Localhost is never shown to them, and Sandbox
      is shown only by an explicit choice, with a warning (S22).
- [ ] The merchant domain sent is the shop's own host.
- [ ] Only administrators can change credentials, the mode or sandbox visibility, through every route
      the platform has (form, REST, RPC).
- [ ] The admin buttons check the same permission: they use the key and reveal the server's IP.

### 3.3 Master keys (S21)

- [ ] Encrypted at rest with prefix `epays1:`, with an encryption key derived for this one purpose.
- [ ] An external encryption key can be configured. The default is documented, including what it does
      not protect: a key in the same database does not survive a full dump.
- [ ] Reading a key in any way returns `********`. Saving the mask keeps the stored key.
- [ ] Inputs are never filled with the key, and it cannot be searched, grouped or exported.
- [ ] A key sent over plain HTTP is refused, except in Localhost mode.
- [ ] A key that cannot be decrypted warns the admin and is treated as no key. It **never** falls back
      on another key, such as production for sandbox.
- [ ] Keys never reach a browser, a log, an error message or a test fixture. Connection error text is
      redacted, as a client library can repeat a header value in it.
- [ ] Credentials travel in headers only. Headers are never logged.
- [ ] Payloads and responses hold customer data, so they are logged at debug level only.

### 3.4 API client

- [ ] Outbound calls go only to the fixed URL of the mode, over verified HTTPS.
- [ ] Redirects are refused, so the key is never resent elsewhere.
- [ ] Non-JSON, HTTP errors and an `ERROR` envelope all raise one error type, with a generic message
      that is safe to show a customer. The details stay on the error for the admin.
- [ ] That error records whether the request timed out, and the HTTP status, `error.code` and
      `error.details`.
- [ ] Payment ids are URL-encoded in paths.
- [ ] The `User-Agent` carries the platform version, plugin version and site, in ASCII. It is how
      ePays traces a payment to a shop.

**ePays renames fields, and servers update at different times.** On 2026-10-18 `gatewayId` and
`merchantGateway` became `subscriptionId`, the gateway type `gatewayId` became `gatewayTypeId`,
`paidAt` became `completedAt`, and statuses went snake case.

- [ ] Fields are read as new name, then old name.
- [ ] `subscriptionId` and `gatewayId` are both sent.
- [ ] Statuses are compared ignoring case.
- [ ] Test fixtures exist in both the old and the new shape.
- [ ] Request fields were compared with the current ePays API: it ignores unknown fields silently, so
      a rename breaks a feature with no error.

### 3.5 Creating a payment (S20)

**Fields**

- [ ] `amount` as a string with 3 decimals, and `currency`.
- [ ] `description`: "Order N" in the payment language ("Invoice N" for an invoice alone), cut to 255.
- [ ] `notifyUrl`: the return route, carrying the environment.
- [ ] `merchantUrl`, `merchantDomain`, `lang` (`en` or `ar`).
- [ ] `testMode`: true for anything but production.
- [ ] `expiryDate` in the format in Part 2.
- [ ] `customFields`: `udf2` is the order reference, `udf3` the invoice references or empty.
- [ ] **`udf6` to `udf10` are never sent.** ePays reads them as gateway overrides: a source string
      there made MPGS and Benefit reject payments. The `User-Agent` does that job.
- [ ] `subscriptionId` and `gatewayId` when a single method was chosen.
- [ ] `customer` when the setting allows, without empty values: `fullName`, `email`, `mobile`, `phone`,
      `country`, `city`, `state`, `zipCode`, `addressLine1`, `addressLine2`.

**Order details**

- [ ] `lineItems` is sent only when the lines plus shipping equal the charged amount **to the fils**. Tabby
      recomputes what it charges from them, adding `unitPrice × quantity` plus `shippingAmount`, not tax
      or discount.
- [ ] `order` also carries `taxAmount`, `shippingAmount` and `discountAmount`.
- [ ] Amounts are added in fils, as integers, not floats.
- [ ] A line whose total does not divide by its quantity is sent once with its line total.
- [ ] A negative line leaves the lines out.
- [ ] Each line has `name`, `quantity` (an integer), `unitPrice` and, when known, `sku`.

**The request itself**

- [ ] Each attempt has its own `Idempotency-Key`, and one attempt sends one key. A key built from
      something that returns after a rollback gets 409 `IDEMPOTENCY_CONFLICT` on every click.
- [ ] `paymentId` and `redirect` are checked. `redirect` must be HTTPS on the host of the mode's API.
- [ ] Stored on the attempt: payment id, environment, domain, expiry, charged amount, currency, rate,
      `udf2`, `udf3`, language, chosen gateway, and the document total if the platform cannot
      otherwise tell it changed.
- [ ] The payment URL is a bearer link: it stays out of logs, REST responses and order screens.
- [ ] **Timeout:** ePays may still create the payment. The request is recorded so that it survives any
      rollback, and the scheduler voids that reference's pending payments that no attempt holds, once
      no click can still be waiting for it. A void that is refused keeps the record (S24).

### 3.6 Return, notification and reading status

**Never trust the request.** The return URL and notification only say which payment to read.

- [ ] One route for the browser return and the ePays notification, accepting GET and POST.
- [ ] A malformed payment id is rejected with no lookup. The environment comes from a fixed list.
- [ ] The document is found **only** by stored payment id plus environment.
- [ ] The status always comes from `GET /api/v2/Payments/{id}`.
- [ ] The redirect target comes from the document, never from the request.
- [ ] Neither depends on a session or cookie. A cross-site return can arrive without one, and a server
      notification never has one. The document is still confirmed; only what the customer sees on the
      status page may use the session.
- [ ] Payment initiation stores on the attempt everything the later steps need, nothing in the session.
- [ ] Public routes read ePays at most 5 times a minute per attempt, so a script cannot get the
      merchant's licence locked out (S17).
- [ ] The scheduler and the admin status check are exempt from that limit.
- [ ] A read that is limited or fails hands the attempt back to the scheduler: the notification may be
      the only one (S10).
- [ ] Public routes expose nothing without the document's own access key or session.
- [ ] Applying a result twice changes nothing.

### 3.7 Applying the result

| Outcome | Result |
|---|---|
| Done, no mismatch, needed | Document paid |
| Done, with a mismatch | Not paid, held (S14) |
| Error | Failed, reason shown, customer can retry |
| Cancel | Not paid, customer can retry |
| Refunded, or not recognised | Never paid |

- [ ] `done` needs amount, currency and `udf2`/`udf3` to match what was stored. References are compared
      after trimming, and skipped only when ePays returns no custom fields.
- [ ] Amounts are compared at the precision of the charged currency.
- [ ] Gateway details are stored for the merchant: transaction id, gateway reference, response code and
      description, gateway response.
- [ ] Anything from ePays shown on a page is escaped.

**At the bank.** `is_at_bank` is:

```text
state is pending and transactionId is not empty
or state is pending or error, and inFlight is true
```

A declined payment can be `inFlight`, as ePays lets its customer pay again on the same link.
`transactionId` stays empty in the BenefitPay wallet. On a server without `inFlight`, a payment voided
during 3-D Secure cannot be told apart.

- [ ] The at-bank state is recorded at every read, including on a cancelled attempt.
- [ ] A payment at the bank is not voided on a retry or when another payment settles the document. Only
      the expiry void and a cancelled document void it.
- [ ] A void refused for any reason re-reads the payment. No code depends on the 409 code itself.
- [ ] A refused void is not reported as a connection failure (S24).

### 3.8 One payable link, and retries (S1 to S6)

A declined link stays payable on ePays, so it counts as open.

Before anything is voided or created, under the document's lock:

- [ ] Every attempt on the document that could still be live is read, the replaced ones included.
- [ ] Reads come **before** any void: an error can roll the request back, but not a void on ePays.
- [ ] One at the bank stops the retry: "still being confirmed", no void, no new payment. The one
      exception is the newest attempt when it can be reused, as below: nothing is voided, and its
      link is reused.
- [ ] One that completed stops the retry: no new payment, the document is settled.
- [ ] A held document stops the retry, showing its message.
- [ ] Only the newest attempt can be reused, only with at least 5 minutes left, and only if its amount,
      currency, language and gateway are what a new payment would send.
- [ ] Every other live attempt is voided first, a declined one too.
- [ ] A refused void re-reads: settled if paid, waiting if at the bank, otherwise no new payment.

After a payment settles the document:

- [ ] Other attempts are voided, declined ones too, except any at the bank (S7).
- [ ] The scheduler voids what stays open, until the watch ends.

### 3.9 Late charges: ePays is the source of truth

A cancelled attempt can still be charged: a void cannot always stop a customer past the ePays page,
and ePays can correct a payment days later. A server with `inFlight` withholds a charge voided during
3-D Secure, but one that lost the race to a void still completes (`lateCapture`).

**Watching (S9, S10)**

- [ ] Every replaced attempt is kept for good.
- [ ] The scheduler reads it until `max(link expiry + 30 min, replaced + 2 h)`.
- [ ] At the bank, until `max(link expiry, replaced) + 3 days`, also when found there later.
- [ ] The return route reads it at any age, so a resent notification still works.
- [ ] A replaced attempt moves only on a `done` result. Nothing else changes the document.
- [ ] A payment another attempt took over is not applied twice: two attempts holding one ePays payment
      are one charge, not a refusal.
- [ ] A payment the document already recorded cannot act on it again. An old return URL must not undo a
      merchant's cancellation.

**Deciding, under the document's lock.** Ask the platform what is due now, not a sum of attempts.

| Situation | Result |
|---|---|
| The attempt the document still uses, document unchanged, charged what was stored | Document paid, whatever the rate is today |
| A replaced attempt that matches what a new payment would charge now (S11) | Document paid, other payment voided |
| Does not match (S12) | Nothing recorded, other attempts stopped, held, everyone told |
| Above what is due, even on time (S15) | As S12 |
| Below what is due (S15) | Held, or partial and unconfirmed where the platform has partial payments |
| Nothing due: already paid (S13) | Nothing recorded, everyone told, left for a refund on ePays |
| Cancelled document | The Part 1 decision: restore or refuse, documented as the intentional difference |

For a replaced attempt, a moved exchange rate counts as a different amount, and no rate counts as a
mismatch.

**ePays corrects a payment later.** The customer says the money was taken, and ePays finds it true.

- [ ] ePays corrects the status first, then calls `POST /api/v2/Payments/{id}/notify-deliveries`. This
      works on a payment already acknowledged, and answers 409 `PAYMENT_NOT_FINAL` on a pending one.
- [ ] That notification pays the document at any age, whether its attempt is pending, declined or
      cancelled, with a test for each.
- [ ] ePays follows the redirect and counts the final page as delivered, even if the shop's read failed.
      So a read that fails or is limited goes back to the scheduler at any age, for every attempt that
      is not paid, not only replaced ones.
- [ ] The merchant can settle it with the admin status check, without ePays.
- [ ] `notifyUrl` is a public address: ePays refuses to resend to anything else (`INVALID_NOTIFY_URL`).

**Everyone told (S12 to S14)**

- [ ] Merchant: a note on the document, a warning in the log, and an email to the shop. A missing shop
      email is logged. The email is not sent if the transaction rolls back.
- [ ] Customer: a message on the order page, naming the reference and asking them to contact the shop.
      It stays there, as the plugin cannot see the refund.

**Held (S12, S14)**

- [ ] A held document stops its other attempts and takes no new payment.
- [ ] A release action (*Allow a new payment*) is on the document or the attempt, for staff who manage
      orders.
- [ ] The merchant is told to refund the charge on ePays if it is not needed, before releasing.
- [ ] After the release, that attempt no longer blocks new payments, and is not applied again.

### 3.10 Scheduler (S7 to S9)

The safety net: ePays gives up on a notification after three failed deliveries.

- [ ] Runs every 15 minutes, with no public trigger needed. The docs say what to do on a quiet site.
- [ ] Settles pending attempts aged 5 minutes to 3 days: this completes the customers who closed their
      browser.
- [ ] Reads a declined attempt until its link expires, as it can be paid again.
- [ ] Voids links expired by more than 30 minutes.
- [ ] Voids the open links of cancelled documents, even at the bank (S8), and finishes a cancellation
      that was interrupted. It checks the document is still cancelled, as a merchant may have restored
      it.
- [ ] Reads replaced attempts until their watch ends.
- [ ] Cleans up orphans of timed-out requests.
- [ ] One failing attempt does not stop the run.

### 3.11 Acknowledgement (S16)

ePays's unacknowledged list is the merchant's to-do list.

- [ ] Acknowledged: completed payments, and declined, cancelled or voided ones with no money taken.
- [ ] Not acknowledged: refused charges and refunded payments. A refused charge is recorded with
      response code `00`, even if the server sent none, so that it can be told apart.
- [ ] The shop's state can lag behind a charge, so `error` and `cancel` count as no money taken only
      from what ePays reports, and never with response code `00`.
- [ ] Acknowledging is informational: a failure, such as an older server's bare 404, is only logged and
      never fails the payment.
- [ ] It runs after the document's change is committed, on the short timeout.

### 3.12 Customer experience

- [ ] Checkout offers the generic method by default, with the brand icons.
- [ ] A single method is offered only with a matching active synced gateway, in its billing countries.
- [ ] Apple Pay is hidden on browsers without `ApplePaySession`.
- [ ] ePays is not offered when the mode has no credentials, or the currency cannot be converted.
- [ ] A failed or cancelled payment returns the customer to the payment step with the cart kept, and the
      reason shown (ePays's decline reason, in the payment language, when it has one).
- [ ] A waiting page reads ePays within the limit on each poll (S18).
- [ ] A customer whose payment, even a replaced one, is at the bank keeps waiting. They are not told it
      failed (S18).
- [ ] A customer who paid never waits on, or reads about, a replacement that was voided.
- [ ] A refused charge is shown even when it is on the older attempt.
- [ ] A cancelled document does not thank its customer.
- [ ] Customer messages are never technical. Details go to the admin.

### 3.13 Languages

- [ ] English and Arabic, right-to-left included, on checkout, waiting and failure pages.
- [ ] An Arabic website opens the ePays page in Arabic by default: the request field is `lang`, with
      `en` or `ar`. `paymentLang` is only what ePays reports back.
- [ ] `lang` is the language of the page the customer was on, or the admin's fixed choice.
- [ ] Messages follow the payment's language, not the language of whoever triggered the code. A
      notification or scheduler run has no customer language of its own.
- [ ] Multi-language plugins on the platform are handled, and their limits documented.
- [ ] The translation template is regenerated after any string change.
- [ ] Translations are reviewed by a native Arabic speaker before release.

### 3.14 Currency

- [ ] ePays charges in BHD only, and refuses anything else (422 `CURRENCY_NOT_SUPPORTED`). The plugin
      converts any other document currency before creating the payment.
- [ ] Charged amount, currency, rate and rate source are recorded on the attempt.
- [ ] No exchange rate means ePays is not offered. It never charges 1:1.
- [ ] The rate source is the platform's own, or its multi-currency plugins, each checked against its
      source code. A manual rate or an extension point covers the rest.

### 3.15 Gateway sync (S23)

- [ ] *Sync from ePays* follows `nextCursor`, up to 10 pages of 100.
- [ ] Removing stale rows happens only after every page is read.
- [ ] A failed sync keeps what was there.
- [ ] Types 7 and 8 and inactive gateways are never offered.

### 3.16 Merchant tools

- [ ] **Warnings on the settings page (S22):** unreadable key, sandbox shown to customers, no exchange
      rate, rates over 7 days old.
- [ ] **Admin status check (S19):** reads the current and all replaced payments, at any age, with no
      read limit, and reports the result.
- [ ] Attempt details are visible on the document: status, environment, charged amount, rate, gateway
      response, whether it is at the bank, watched until when, whether a review is needed.
- [ ] A log channel the merchant can reach, redacted.

### 3.17 Data lifecycle

- [ ] Upgrades are repeatable, and only change values the plugin set itself.
- [ ] A release that installed shops must pick up bumps the version everywhere it is stated.
- [ ] Upgrading from the previous version keeps payments authenticating.
- [ ] Uninstall removes settings, keys and scheduled jobs, and keeps orders and payment details: they
      are the shop's records.
- [ ] A staging copy cannot take real money: keys cleared or a warning shown, as the platform allows. A
      server-specific value, like the IP address, is cleared too.
- [ ] Any new secret is added to that protection, with a test.
- [ ] Personal data sent to ePays is listed, and the platform's privacy tools are supported.
- [ ] Multi-company or multi-site platforms keep each tenant's credentials and records apart.

### 3.18 Out of scope

- Refunds, in every integration. A later release, as ePays has a refund API
  (`POST /api/v2/payments/{id}/refunds`).
- A refund made on ePays after a payment completed does not reach the shop.

---

## Part 4: Verification

### Automated

- [ ] CI runs lint, static analysis and tests on every pull request.
- [ ] CI fails a run that executed 0 tests.
- [ ] CI runs the platform's minimum supported versions and its current ones.
- [ ] CI covers each optional configuration, such as the module alone and with sales and accounting, or
      each storage mode.
- [ ] CI scans for secrets (gitleaks).
- [ ] The marketplace's own check passes on the release package.
- [ ] Tests mock the single HTTP entry point. No test calls a real ePays server.
- [ ] Rollback paths are tested with real rollbacks, and after-commit hooks are actually run: test
      frameworks often skip or hide them.
- [ ] Every Part 5 scenario has a test named after it.

### End to end

Run in a browser on the final code, against the sandbox or a local ePays API. A run on earlier code
does not count. Items marked *newer server* need an ePays API with `inFlight`: ask ePays where to test
them.

- [ ] Payment approved, in each checkout type.
- [ ] Declined, then retried on the same document (S4).
- [ ] Back to store from the ePays page.
- [ ] Customer's language: English, Arabic, a bilingual site.
- [ ] Payment in another currency.
- [ ] A single method opening its own form.
- [ ] *Newer server:* voided during 3-D Secure, paid with another method: the charge is withheld, the
      replacement pays.
- [ ] Document cancelled with an open link, also at 3-D Secure (S8).
- [ ] A slow 3-D Secure answer.
- [ ] A payment request answered too late, with its orphan voided.
- [ ] Master key entered, saved, stored encrypted, and absent from pages and logs.
- [ ] Install on a fresh site from the release package.

Late charges cannot be forced on a newer server, and a real at-bank state lasts only while the bank
answers. Cover both with mock tests, and record that in your open-work list.

### On real gateways

- [ ] Sandbox: a full payment, then a declined one and a retry.
- [ ] A real card payment, up to the confirmed order.
- [ ] Benefit and BenefitPay.
- [ ] Apple Pay on Safari, iPhone or Mac.
- [ ] Google Pay and Samsung Pay on a device.
- [ ] Tabby, with order lines.

---

## Part 5: Acceptance contract

Each row needs a test named after the scenario, with this outcome. The test name proves the row.

| # | Scenario | Expected | Test |
|---|---|---|---|
| S1 | Retry while the current payment is at the bank and its link is live | Its link is reused if nothing changed and at least 5 minutes are left; otherwise not voided, no new payment, "still being confirmed" | |
| S2 | Retry while a replaced payment is at the bank | Same, and nothing is voided first | |
| S3 | Retry when a replaced payment has completed | No new payment; the order is settled | |
| S4 | Retry after a decline | Declined link voided before the new payment | |
| S5 | Retry with under 5 minutes left on the link | Old link voided, new payment created | |
| S6 | Void refused on retry | Re-read; no second payment unless it turned out paid | |
| S7 | Another payment settles the order | Other attempts voided, except any at the bank; the scheduler voids what stays open, until the watch ends | |
| S8 | Order or document cancelled with an open link | Link voided on ePays, even at the bank | |
| S9 | Replaced payment watched by the scheduler | Until `max(link expiry + 30 min, replaced + 2 h)`; `max(link expiry, replaced) + 3 days` at the bank, also when found there later | |
| S10 | Callback for a replaced payment | Read at any age; a read that is limited or fails goes back to the scheduler | |
| S11 | Late charge that matches what is due now | Order paid with it; other payment voided | |
| S12 | Late charge that does not match what is due now | Nothing recorded, other attempts stopped, new payments blocked until released; merchant told by note, log and email; customer told | |
| S13 | Second charge on a paid order | Nothing recorded; merchant told by note, log and email; customer told | |
| S14 | Current payment mismatch | Not paid, customer told to contact the shop, merchant told by note, log and email, new payments blocked until released | |
| S15 | Order total changed after the payment was created | A charge above what is due is refused as in S12 | |
| S16 | Acknowledgement | Completed, and declined, cancelled or voided with no money taken: acknowledged. Refused or refunded: not | |
| S17 | Public routes | At most 5 ePays reads a minute per payment attempt; the scheduler and admin action are exempt; malformed ids rejected without a lookup | |
| S18 | Waiting page | Each poll reads ePays within the limit; a customer whose replaced payment is at the bank keeps waiting | |
| S19 | Admin status check | Reads the current and all replaced payments, at any age | |
| S20 | Request content | Same fields; `lineItems` only when they add up to the fils; description "Order N" in the payment language | |
| S21 | Master key handling | Encrypted with prefix `epays1:`; refused over plain HTTP; an unreadable key warns the admin and never falls back to another key | |
| S22 | Admin warnings | Unreadable key, sandbox shown to customers, no exchange rate, rates over 7 days old | |
| S23 | Gateway sync | Follows `nextCursor`, up to 10 pages | |
| S24 | Void refused while the customer may be paying | Not reported as a connection failure, no second payment, void retried once ePays allows it | |

---

## Part 6: Documentation and release

### Documentation

- [ ] `README`: what it does, installing, updating.
- [ ] A developer guide: commands, architecture, easy-to-break rules, your Part 1 answers, and every
      difference from the other integrations with its reason.
- [ ] An open-work list: what is left, and what was not tested and why.
- [ ] Security notes: how to report a problem, what is guaranteed, the risks you cannot remove.
- [ ] `CHANGELOG`.
- [ ] Merchant setup: credentials, domain and IP on the licence, encryption key and how to rotate it,
      scheduler on a quiet site, exchange rates.
- [ ] Plugin only: marketplace listing: external service disclosure, data sent, ePays's terms and
      privacy URLs.
- [ ] Screenshots from a test shop, with no real keys, domains or customer data.

### Before release

- [ ] A scan of the package, logs and diffs finds no key.
- [ ] Development files stay out of the package: tests, tooling, and internal notes.
- [ ] The package is built from the tagged commit, and installed on a fresh site.
- [ ] All scenarios pass on the final code.
- [ ] Arabic reviewed by a native speaker.
- [ ] **Release order checked:** a plugin that handles a new ePays API goes out **before** that API
      reaches production.

### After release

- [ ] Plugin only: the published package is downloaded and confirmed identical to the commit.
- [ ] A real payment on a production shop, with production keys.
- [ ] A new platform plugin is added to the other integrations' developer guides, and to this file.

### Onboard each merchant

- [ ] Active licence with at least one active gateway.
- [ ] API master key, and a sandbox pair.
- [ ] The shop's domain and the server's public IP address on the licence. Check the IP again when a
      host moves servers.
- [ ] Exchange rates set up for shops selling in other currencies.
