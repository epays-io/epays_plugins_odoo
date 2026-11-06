# Changelog

Each version is for Odoo 18.0. Update notes are in [README.md](README.md#version-history).

## 18.0.1.0.8

Fixes from the audit against `PLUGIN_CHECKLIST.md`.

Payment logic:

- A held or refused charge is never recorded afterwards, also after *Allow a New Payment*. Before, the
  next read could record a held charge below what was due. `epays_charge_refused` records the refusal,
  as response code `00` alone also fits a payment ePays declined, which can still be paid on its link.
- Requests paying the same documents wait for each other, so two first clicks cannot create two
  payable links.
- Failed ePays reads count towards the limit of 5 a minute on public routes.
- A payment ePays reports as cancelled or failed with response code `00` is not acknowledged.
- A replaced payment last seen at the bank holds up a retry even after its link expired.
- The at-bank state is recorded at every read.
- A retry in another language opens a new page on a server that does not report the language.
- An order line that does not divide by its quantity loses its quantity alone, not all lines.

Security and privacy:

- Exports refuse the master keys, also through a related record. Before, they returned the stored
  ciphertext.
- ePays is not offered at checkout without credentials the mode can use, such as an unreadable key.
- The payment link, full error responses and the notification data no longer reach the log above
  DEBUG level.
- A return URL naming an unknown environment is refused without a lookup.

Customers and staff:

- A void ePays refuses is no longer reported to the customer as a connection failure.
- Staff who can edit every document on a transaction can use *Allow a New Payment*, not only
  administrators.
- The transaction shows the page language, the exchange rate source, and whether a charge was refused
  or needs a review.

## 18.0.1.0.7

- One payment logic with the WooCommerce plugin: reads every live attempt before voiding, holds a
  document whose charge does not fit it until the merchant allows a new payment, and watches replaced
  payments on the schedule both integrations share.
- Master keys encrypted in the database (`epays1:`), with an optional key of your own, and refused over
  plain HTTP.
- Warnings on the provider form: unreadable key, sandbox shown to customers, no exchange rate, rates
  over 7 days old.
- A late charge on a document's own payment is checked against what it charged, not against today's
  rate.

## 18.0.1.0.6

- Finished payments are acknowledged on ePays: completed, declined and cancelled, but not money ePays
  took that Odoo refused.
- ePays is the source of truth: a payment Odoo voided that ePays charged anyway completes the order
  or is refused, and a corrected payment is read at any age.

## 18.0.1.0.5

- Works with both the old and the renamed ePays v2 API.

## 18.0.1.0.4

- Messages in the language the customer paid in.

## 18.0.1.0.3

- First public version.
- This server's IP address is stored as a system parameter, which fixes *Upgrade* from the browser.

## 18.0.1.0.2

- *Find this server's IP address*, and an ePays provider in every company.

## 18.0.1.0.1

- Payments in other currencies converted to BHD, and master keys never sent to the browser.

## 18.0.1.0.0

- First version.
