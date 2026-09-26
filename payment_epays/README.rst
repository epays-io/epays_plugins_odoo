======================
ePays Payment Provider
======================

Take payments on Odoo 18.0 (Community and Enterprise) through the ePays hosted payment page:
cards (Visa, Mastercard), Benefit, BenefitPay, Apple Pay, Google Pay, Samsung Pay and Tabby, as
enabled on your ePays merchant account.

The customer picks ePays at checkout, pays on the ePays page, and comes back to Odoo. Odoo then
asks ePays for the result and completes the order the standard way: the sales order is confirmed,
the payment is posted in the provider's journal and the invoice is reconciled. The addon works
with website checkout, portal payments of quotations and invoices, and payment links.

ePays is an external, paid service. You need an ePays merchant account.

Installation
============

Odoo installs a payment provider only from the server's addons path. Apps > Import Module cannot
install it, because it contains Python code. **Odoo Online (SaaS) is not supported:** it cannot
run third-party Python modules.

The module depends only on ``payment``. Install ``website_sale`` for the shop and
``account_payment`` to post payments in accounting. In the commands below, replace
``<database>`` with your database name; the paths are those of a standard Linux install
(configuration in ``/etc/odoo/odoo.conf``, service ``odoo``).

**On-premise**

1. Put the module in a folder of your own:

   ::

      sudo mkdir -p /opt/odoo/custom-addons
      sudo unzip payment_epays-18.0.1.0.3.zip -d /opt/odoo/custom-addons
      sudo chown -R odoo:odoo /opt/odoo/custom-addons

2. Add that folder to ``addons_path`` in ``/etc/odoo/odoo.conf``, and enable ``proxy_mode`` behind
   a reverse proxy:

   ::

      [options]
      addons_path = /usr/lib/python3/dist-packages/odoo/addons,/opt/odoo/custom-addons
      proxy_mode = True

3. Install it, with the shop and accounting:

   ::

      sudo systemctl stop odoo
      sudo -u odoo odoo -c /etc/odoo/odoo.conf -d <database> \
          -i website_sale,account_payment,payment_epays --stop-after-init
      sudo systemctl start odoo

   Or restart Odoo (``sudo systemctl restart odoo``), activate the developer mode, then
   Apps > Update Apps List, search for *ePays* and click Install.

**Docker** (official ``odoo:18`` image): put the module in the folder mounted on
``/mnt/extra-addons``, then:

::

   docker compose run --rm odoo odoo -d <database> \
       -i website_sale,account_payment,payment_epays --stop-after-init
   docker compose restart odoo

**Odoo.sh:** add the module to your project repository and push, then install it from Apps on
that branch:

::

   unzip payment_epays-18.0.1.0.3.zip -d <odoo-sh-project>
   cd <odoo-sh-project>
   git add payment_epays && git commit -m "Add the ePays payment provider" && git push

Installing creates an **ePays** provider in every company. It starts **Disabled**, so nothing
changes at checkout until you configure it.

**ePays is not under Payment Providers?** Odoo lists only the providers of the company you are
working in (top-right company switcher), and only once the module is installed:

* Apps: remove the *Apps* filter, search for *ePays*; it must say *Installed*. If it is not
  listed, run *Update Apps List* (developer mode) and check that the ``payment_epays`` folder is on
  the ``addons_path`` of the server.
* Or from the server::

     sudo -u odoo psql -d <database> -c "SELECT name, state, latest_version FROM ir_module_module WHERE name = 'payment_epays';"
     sudo -u odoo psql -d <database> -c "SELECT id, state, company_id FROM payment_provider WHERE code = 'epays';"

* The install log tells why it failed, e.g. an Odoo version other than 18.0::

     sudo grep -iE "payment_epays.*(error|warning)" /var/log/odoo/odoo-server.log

Updating the module
===================

Replace the files, **update the module in the database**, then check. Replacing the files alone is
not enough: Odoo then fails with ``column payment_provider.epays_... does not exist``. Payments in
progress are not affected.

1. Back up the database and the current folder::

      sudo -u odoo pg_dump -Fc <database> > /tmp/<database>-before-epays-update.dump
      sudo cp -a /opt/odoo/custom-addons/payment_epays /tmp/payment_epays-previous

2. Replace the whole folder (do not unzip over it)::

      sudo rm -rf /opt/odoo/custom-addons/payment_epays
      sudo unzip payment_epays-18.0.1.0.3.zip -d /opt/odoo/custom-addons
      sudo chown -R odoo:odoo /opt/odoo/custom-addons/payment_epays

3. Update the module::

      sudo systemctl stop odoo
      sudo -u odoo odoo -c /etc/odoo/odoo.conf -d <database> -u payment_epays --stop-after-init
      sudo systemctl start odoo

   Or, after restarting Odoo: Apps (remove the *Apps* filter) > *ePays* > **Upgrade**. With
   Docker: ``docker compose run --rm odoo odoo -d <database> -u payment_epays --stop-after-init``.
   On Odoo.sh, push the new folder: a higher version is updated when the branch builds.

4. Check the version, reload the browser (Ctrl+F5) and click **Test connection**::

      sudo -u odoo psql -d <database> -c "SELECT latest_version FROM ir_module_module WHERE name = 'payment_epays';"

The update runs the version's migrations on its own. If it fails with
``column ... does not exist``, step 3 was not run on that database (with 18.0.1.0.2, the browser
*Upgrade* could fail this way: use the command line once, or install 18.0.1.0.3 or later).

How to use it
=============

1. **Get your ePays details** (see *ePays onboarding* below): the API ID and API master key, a
   sandbox pair if ePays gives you one, and a licence listing your shop domain and the public IP
   address of your Odoo server.
2. **Enter the keys.** As an administrator, open Invoicing (or Website) > Configuration > Payment
   Providers > **ePays**, Credentials tab. Fill in the API ID and API Master Key, the sandbox
   pair, and the Merchant Domain if it differs from your website's. Click **Test connection**: it
   confirms the connection or says what to fix (a wrong key, a domain missing from the licence,
   or the exact IP address to ask ePays to register). **Find this server's IP address** shows the
   public IP address to give ePays, even before you have keys; copy it from *This Server's IP
   Address*.
3. **Choose the payment methods.** *ePays* is on by default: one option showing every logo, where
   the customer picks the method on the ePays page. To offer Card, Benefit, BenefitPay, Apple Pay,
   Google Pay, Samsung Pay or Tabby as separate options, turn them on with **Enable Payment
   Methods**, then click **Sync from ePays** in the Configuration tab.
4. **Test on your live shop.** Set the state to **Test**: the mode switches to Sandbox and only
   logged-in staff see ePays at checkout. Click **Sync from ePays**, place an order in BHD, pay on
   the ePays test page and check that the order is confirmed.
5. **Go live.** Set the state to **Enabled**: the mode switches to Production and ePays is shown to
   every customer. Click **Test connection** and **Sync from ePays** again, since the production
   server has its own gateways. To stop taking ePays payments, set the state to **Disabled**; your
   keys and settings are kept, and payments already in progress still complete.
6. **Every day:**

   * *Shop:* the customer picks ePays, pays on the ePays page and returns to the order
     confirmation. The sales order is confirmed and the payment is posted in the provider's
     journal.
   * *Quotations, invoices and payment links:* customers can also pay from the customer portal,
     or from a link made with *Generate a Payment Link* on a sales order or invoice.
   * *Payment details:* each paid order and invoice gets an internal note with the ePays payment
     id, the gateway reference and the masked card; the same details are on the payment
     transaction.
   * *Customer closed the browser after paying:* the scheduled action *ePays: check pending
     payments* completes the order within 15 minutes. Keep it active.
   * *Customer goes back and pays again:* the same ePays payment is reused, so an order never
     has two payable links.
   * *Reconciliation:* in the ePays portal, find a payment by its sales order number (udf2) or
     invoice number (udf3).
7. **Find a payment in Odoo.** Every attempt to pay is a *payment transaction*, listed in
   developer mode only (Settings > Developer Tools > *Activate the developer mode*):

   * *All ePays payments:* Invoicing (or Accounting) > Configuration > Online Payments >
     **Payment Transactions**, or Website > Configuration > eCommerce > **Payment Transactions**.
     Filter or group by provider *ePays*; search the *Provider Reference* for an ePays payment id.
   * *One transaction:* its state and the message the customer saw, and an **ePays** section with
     the environment, the sales order and invoice numbers sent, the ePays transaction id, the
     gateway reference, the response code and description, and the gateway response (masked
     card, authorisation code, 3-D Secure result).
   * *From an order or an invoice:* each finished payment writes an *ePays payment details* note
     in its chatter, and the payment recorded in accounting links to its transaction.

ePays takes payments in Bahraini dinar (BHD). Orders and invoices in another currency are
converted to BHD with Odoo's exchange rate of the day (see *Standard settings* below).

ePays onboarding
================

Ask ePays for:

1. A merchant **API ID** and **API master key** (and, if ePays gives you one, a separate sandbox
   pair).
2. Your licence to list your **shop domain** (for example ``shop.example.com``) and the **public IP
   address of the Odoo server**. Every call from Odoo is checked against both.
3. The gateways (card, Benefit, BenefitPay, wallets, Tabby) activated on your merchant account.
   Odoo reads them with **Sync from ePays**; you do not need to copy any id by hand.

Configuration
=============

Go to Invoicing (or Website) > Configuration > Payment Providers > **ePays**.

Credentials tab
    * **API ID**, **API Master Key**: required once the provider is not Disabled.
    * **Sandbox API ID**, **Sandbox API Master Key**: used in Sandbox mode. Leave them empty to
      use the production pair there too.
    * **Merchant Domain**: the domain registered on your ePays licence. Leave it empty to use the
      domain of the website at payment time.
    * **Mode**: the ePays server that takes the payments. The **API URL** below it shows the
      address.

      ========== ============================ ==============================================
      Mode       API URL                      Use
      ========== ============================ ==============================================
      Production ``https://api.epays.io``     Real payments. The only mode allowed when the
                                              provider is Enabled.
      Sandbox    ``https://testapi.epays.io`` Test payments, with the sandbox keys.
      ========== ============================ ==============================================

      Switching the state to Enabled sets the mode to Production; switching to Test moves it from
      Production to Sandbox. In Test state you can still choose Production, for your staff to try
      the live gateways.
    * **Find this server's IP address**: the public IP address to register on your ePays
      licence, kept in *This Server's IP Address* with a copy button. With keys, ePays is asked
      first: when the licence refuses the server, ePays reports the exact address it saw.
      Otherwise a public echo service is asked (``api.ipify.org``, then ``icanhazip.com``; no
      credential is sent). On Odoo.sh the address can change when the project moves to another
      server: if payments fail with an IP error, click it again and send ePays the new address.
    * **Test connection**: makes a harmless authenticated call to the server of the current mode
      and tells you whether it works. If the licence refuses the domain or the server IP address, the message
      says which one, and shows the IP address that ePays saw so you can ask ePays to register it.
      These details are shown only here, never to customers: at checkout a customer only sees a
      generic error.

Configuration tab (ePays group)
    * **Payment Page Language**: the customer's (website) language, English or Arabic.
    * **Payment Link Expiry (minutes)**: default 60.
    * **Send Customer Details**: sends the customer's name, email, phone and address (default on).

Standard settings
    Payment methods (see below), payment journal, countries, maximum amount and messages.
    **Currency:** ePays takes payments in Bahraini dinar (BHD). An order or invoice in another
    currency (e.g. from a USD or EUR pricelist) is still paid with ePays: Odoo converts its amount
    to BHD with the exchange rate of the day (Accounting > Configuration > Currencies) and the
    customer pays that BHD amount on the ePays page. The payment is recorded in the order's
    currency, so the invoice is reconciled as usual. The transaction shows the BHD amount charged
    and the rate, and the note on the order repeats them.

    ePays is not offered in a currency without any exchange rate in Odoo: Odoo would otherwise
    convert at a rate of 1 (100 USD charged as 100 BHD). Keep the rates up to date, e.g. with
    Accounting > Settings > *Automatic Currency Rates*; the conversion uses the latest rate Odoo
    has, however old.

Payment methods at checkout
===========================

The addon brings its own payment methods, with ePays artwork, so other payment providers are not
affected.

* **ePays** (on by default): one option showing the logos of every method (Visa, Mastercard,
  Benefit, BenefitPay, Apple Pay, Google Pay, Samsung Pay, Tabby). The customer chooses the method
  on the ePays page.
* **Individual methods** (off by default): **Card**, **Benefit**, **BenefitPay**, **Apple Pay**,
  **Google Pay**, **Samsung Pay** and **Tabby**. Each one opens its ePays gateway directly. Turn
  them on with **Enable Payment Methods** on the provider form. Benefit and BenefitPay are offered
  only when the billing address is in Bahrain; Tabby only when it is in Bahrain, Saudi Arabia,
  the UAE or Kuwait. At shop checkout this is the billing address selected for the order, even
  for a logged-in user whose own contact is in another country. Apple Pay is offered only on
  browsers that support it (Safari on Apple devices).

To link the individual methods to your ePays gateways, click **Sync from ePays** in the
**ePays Gateways** section of the Configuration tab. Odoo reads the gateways activated on your
merchant account on the server of the provider's mode and links each one to its payment method by gateway type. You can correct a link in
the table; later syncs keep your correction. Gateways that ePays no longer returns are removed,
and integrations that are not payment methods (Zoho, Odoo) are not listed. Sync again after you
change the mode.

If an individual method has no active gateway in the table, a customer who picks it is sent to the
ePays page to choose, and the Odoo log records a warning. After the payment, the transaction's
payment method is set from the gateway that actually took it, e.g. Apple Pay for a customer who
chose Apple Pay on the ePays page.

.. warning::

   Do not clear the **Payment Journal** while ePays payments may still be in progress. Odoo could
   not record those payments and would retry in vain.

Behind a reverse proxy
    Run Odoo with ``proxy_mode = True``. Odoo then builds the return URL sent to ePays with the
    public https host.

Several companies
    Every company gets its own ePays provider: at installation, at each update of the module, and
    when a company is created. It starts Disabled and without credentials: in each company that
    takes ePays payments, open it and set the credentials and the journal.

Scheduled action
    *ePays: check pending payments* runs every 15 minutes (Settings > Technical > Scheduled
    Actions). Keep it active: ePays notifies Odoo only once, so the action completes payments
    whose customer closed the browser, voids expired unpaid payment links, and finishes the Odoo
    post-processing of ePays payments even when no provider is enabled any more.

Test mode, publishing and disabling
===================================

* **Test**: Odoo unpublishes the provider automatically, so only logged-in internal users see it:
  your staff can test safely on a live shop. Click **Published** to test as a customer. The mode
  moves to Sandbox; in Sandbox mode ePays receives ``testMode: true``.
* **Enabled**: the provider is published, in Production mode.
* **Unpublish**: hidden from customers (website, portal, payment links); internal users still see
  it.
* **Disabled**: hidden from everyone. Credentials and settings are kept. Payments that were already
  in progress still complete, on the server (mode) they started with.
* **Uninstall**: possible only before any ePays payment has been posted in accounting; after that
  Odoo blocks it by design, and *Disabled* is the way to switch ePays off. Transactions are always
  kept.

Reconciliation: the references sent to ePays
=============================================

Each payment carries the order and invoice numbers in two ePays custom fields, so a payment in the
ePays portal or export can be matched to Odoo:

============= ============================================ =======================================
Field         Value                                        Example
============= ============================================ =======================================
udf2          Sales order number(s), comma-separated       ``S00012``
udf3          Invoice number(s), comma-separated. Filled   ``INV/2026/00012``
              for portal and payment-link invoice
              payments; empty for shop orders, whose
              invoice is created after the payment.
============= ============================================ =======================================

Both fields are always sent, empty when there is no such document. Lists longer than 255
characters are cut at a whole reference and end with ``…``. When a payment completes, Odoo checks
that ePays returns the same udf2 and udf3 values; a mismatch puts the transaction in error.

Paying the same order again
---------------------------

Odoo creates a new payment transaction each time the customer clicks Pay, for example after going
back to the cart. **Going back and paying again reuses the same ePays payment**, so an order has
one live ePays payment at a time. When the customer pays a sales order (or, without sales order,
the same invoices) again, Odoo checks the latest earlier ePays payment of those documents, if its
link is still valid for more than five minutes and it is in the same mode:

* still pending, same amount, currency and payment method: the customer returns to the same ePays
  payment page; the earlier transaction is cancelled ("Replaced by transaction ...");
* still pending, but the amount changed (the cart changed) or the customer chose another payment
  method (a page opened for Card shows Card only): Odoo voids the earlier ePays payment, cancels
  the earlier transaction ("... (amount changed)" or "... (payment method changed)") and creates
  a new payment for the chosen method. If ePays
  refuses the void, no new payment is created: the customer sees "This order has already been
  paid." if it was paid meanwhile, or the generic error otherwise;
* already paid: no new payment is created and the customer sees "This order has already been
  paid."; the earlier transaction is completed by its return, the ePays notification or the
  scheduled action;
* a status Odoo does not recognise (it may be a gateway's own word for a completed payment):
  handled like a pending payment that cannot be reused. Odoo voids it first (ePays voids only a
  pending payment, never a paid one) and creates a new payment only if the void succeeds;
* failed, cancelled or expired: the earlier transaction is updated and a new payment is created.

If ePays cannot be reached during this check, the customer sees the generic error rather than
getting a second payable link. Payments without sales order or invoice (a bare payment link) are
never reused.

Older ePays servers may also hand back the payment of an earlier attempt by themselves (a payment
whose page was never opened, with no result, less than ten minutes old, with the same custom
fields). Odoo then cancels the earlier transaction the same way and the payment belongs to the new
one.

The addon never sends ``udf6`` to ``udf10``: ePays uses them as gateway settings (``udf6`` is the
MPGS merchant ID and the Benefit alias, ``udf7``/``udf8`` the MPGS display name and logo, ``udf9``
the integration type, ``udf10`` MPGS overrides). The database, website and versions are sent in
the request's User-Agent instead, e.g. ``Odoo/18.0 payment_epays/18.0.1.0.0 (db prod; site
shop.example.com)``, which ePays records on each payment.

In Odoo, the transaction's **Provider Reference** is the ePays payment id. The transaction form
has an **ePays** section with the ePays transaction id, gateway reference, response code and
description, ePays gateway id and the gateway response. When a payment finishes, the same details are
written as an internal note on the linked sales orders and invoices.

Data shared with ePays
======================

For each payment Odoo sends: the amount in BHD (converted when the order is in another
currency), the sales order and invoice numbers (udf2 and
udf3), the database and website names and the Odoo and addon versions (in the User-Agent) and,
when *Send Customer Details* is on, the customer's name, email, phone and
address. ePays returns the payment result, including masked card data (last four digits) and
authorisation details, which Odoo stores on the transaction.

Security
========

What the addon does:

* Odoo never trusts the return URL or the ePays notification: it always reads the payment back from
  ePays, and completes the order only if the amount, the currency and the order and invoice numbers
  match. A refunded payment never completes an order.
* Calls to ePays use https with certificate checking, on the fixed server of the chosen mode, and
  never follow a redirect, so the master key is only ever sent to ePays.
* The API master keys never leave the server. Once saved, a key is shown as ``********`` and is
  never sent to a browser, not even an administrator's: the form, RPC reads and exports never
  return it, and it cannot be searched, grouped or sorted on. To change a key, type the new one
  over the mask; saving the form without touching it keeps the stored key. Only Settings
  administrators can set the API IDs and keys.
* The keys are never written to the server log or to an error message, even when a request fails.
  A key pasted with a space or a line break inside is refused when saved.
* The keys are not copied when the provider is duplicated, and are cleared in neutralised copies of
  the database (e.g. Odoo.sh staging). Only users who may configure the provider can use **Test
  connection** and **Sync from ePays**.
* The customer's personal data is not written to the server log, except at DEBUG level.

What to do on a live shop:

* Set the **Merchant Domain** explicitly. When it is empty, it follows ``web.base.url``, which Odoo
  changes when an administrator logs in from another address.
* Do not publish the provider in **Test** state: a published test provider confirms orders paid in
  the sandbox. Localhost mode cannot be published at all.
* Keep the server log at INFO level or above in production. In particular, never run Odoo with
  ``--log-level=debug_rpc`` or ``debug_rpc_answer``: Odoo itself then logs the values an
  administrator saves, including a new master key.
* Keep the exchange rates up to date if the shop sells in other currencies than BHD.

Uninstalling
============

Uninstalling resets the provider (Odoo's standard behaviour for payment providers) and removes the
ePays fields and the scheduled action. It is blocked once ePays payments exist in accounting.

Support
=======

ePays IT Solutions, Manama, Bahrain

- Email: info@epays.io
- Phone: +973 3322 0204
- Website: https://epays.io
