# Security

## Reporting a vulnerability

Email **info@epays.io** with "Security: ePays for Odoo" in the subject. Include the version, the steps
to reproduce, and the impact. Do not open a public issue. We answer within five working days.

## What the addon guarantees

- **Orders complete only on a verified payment.**
  - The return route and the ePays notification only identify a payment, and the transaction is found
    by the payment id stored when it was created, with its environment.
  - The status is read from ePays.
  - Amount, currency and the order and invoice numbers must match. A refunded or unrecognised status
    never completes an order.
- **The master keys:**
  - are stored encrypted (Fernet, prefix `epays1:`), with a key derived for this one purpose;
  - never reach a browser: reads return `********`, and saving the mask keeps the stored key;
  - cannot be searched, grouped, sorted or exported;
  - never appear in a log or an error message;
  - are never replaced by another key: one that can no longer be read warns the administrator, and
    ePays is not offered until it is entered again;
  - can only be changed by Settings administrators, and are refused over plain HTTP outside Localhost
    mode and a local server;
  - are cleared on neutralised copies of the database.
- **Outbound calls** go only to the fixed ePays URL of the mode, over verified https. Redirects are
  refused, so the key is never resent elsewhere. The only other calls are the public IP services of
  *Find this server's IP address*, which carry no credential.
- **The public routes:**
  - validate their input (payment id format, a fixed list of environments);
  - redirect only to Odoo's status page;
  - call ePays at most five times a minute per payment attempt, failed calls included, in each Odoo
    process;
  - expose nothing outside the customer's own session.
- **The payment page URL** is a bearer link, so it is kept to system administrators and out of the logs.
- **ePays data** (gateway names, decline reasons, gateway responses) is escaped wherever it is shown.
- **Customer data** in requests and responses is written to the log at DEBUG level only.

## Risks the addon cannot remove

- **A key in the database does not survive a full dump.** Without an encryption key of your own, the
  master keys are encrypted with Odoo's `database.secret`, which is in the same database. Set
  `epays_encryption_key` in `odoo.conf`, or the `EPAYS_ENCRYPTION_KEY` environment variable.
- **Anyone who can run code on the server** (a custom module, Studio server actions, file access) can
  read the keys, as with any addon. Keep administrator accounts to the people who need them.
- **Debug log levels.** With `--log-level=debug_rpc` or `debug_rpc_answer`, Odoo itself logs the values
  an administrator saves, including a new master key. Keep the log at INFO or above in production.
- **The read limit is per process.** Several workers can each make five reads a minute of one payment.
- **A void is best effort.** A customer can keep paying while a page is open, and ePays can complete a
  payment that was voided. The addon keeps those payments, acts on what ePays reports, and a customer
  whose payment ePays shows as with the bank waits for the answer instead of paying again. A payment
  voided during 3-D Secure shows nothing of the kind on an ePays server that does not withhold its
  charge, so a customer can still pay twice: the merchant is told to refund the second payment on
  ePays. A late payment that no longer matches the order is not recorded either: the order waits for
  the merchant, who releases it with *Allow a New Payment*. A payment on a cancelled order is refused
  and left for a refund.
- **A refund made on ePays does not reach Odoo.** Refunds are out of scope for this version.
- **Uninstalling drops the ePays columns of the transactions.** Odoo removes a module's fields with it.
  The internal notes on sales orders and invoices stay, with the payment details.
