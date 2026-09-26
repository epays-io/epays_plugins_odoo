# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

import ipaddress
import logging
import pprint
import re
from urllib.parse import urlsplit

import requests

from odoo import Command, _, api, fields, models, release
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.payment import utils as payment_utils
from odoo.addons.payment.const import REPORT_REASONS_MAPPING
from odoo.addons.payment_epays import const
from odoo.addons.payment_epays.utils import EpaysApiError

_logger = logging.getLogger(__name__)

# Whitespace and control characters: never part of a credential, and invalid in an HTTP header.
_INVALID_CREDENTIAL = re.compile(r'[\s\x00-\x1f\x7f]')


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[('epays', "ePays")], ondelete={'epays': 'set default'}
    )

    # Credentials.
    epays_api_id = fields.Char(
        string="API ID",
        help="The API ID of the ePays merchant, used in production.",
        required_if_provider='epays',
        groups='base.group_system',
        copy=False,
    )
    epays_master_key = fields.Char(
        string="API Master Key",
        help="The API master key of the ePays merchant, used in production. Once saved, it is "
             "never shown again: enter a new key to replace it.",
        required_if_provider='epays',
        groups='base.group_system',
        copy=False,
        exportable=False,
    )
    epays_sandbox_api_id = fields.Char(
        string="Sandbox API ID",
        help="The API ID used in Sandbox and Localhost mode. Leave empty to use the production "
             "API ID.",
        groups='base.group_system',
        copy=False,
    )
    epays_sandbox_master_key = fields.Char(
        string="Sandbox API Master Key",
        help="The API master key used in Sandbox and Localhost mode. Leave empty to use the "
             "production master key. Once saved, it is never shown again.",
        groups='base.group_system',
        copy=False,
        exportable=False,
    )
    epays_merchant_domain = fields.Char(
        string="Merchant Domain",
        help="The domain registered on the ePays licence. Leave empty to use the domain of the "
             "website at payment time.",
    )
    epays_mode = fields.Selection(
        string="Mode",
        help="The ePays server that takes the payments. Production takes real payments and is the "
             "only mode allowed when the provider is Enabled. Sandbox and Localhost (an ePays API "
             "running on this server, for development) send test payments with the sandbox "
             "credentials.",
        selection=const.ENVIRONMENTS,
        default='production',
    )
    epays_api_url = fields.Char(
        string="API URL", compute='_compute_epays_api_url',
    )
    # Not stored on the provider: see `const.SERVER_IP_PARAM`.
    epays_server_ip = fields.Char(
        string="This Server's IP Address",
        help="The public IP address this Odoo server calls ePays from, as last found by \"Find "
             "this server's IP address\" or the connection test. Your ePays licence must list it.",
        compute='_compute_epays_server_ip',
        groups='base.group_system',
    )

    # Options.
    epays_payment_lang = fields.Selection(
        string="Payment Page Language",
        help="The language of the ePays payment page. \"Customer's language\" uses the language "
             "of the website, then the language of the customer.",
        selection=[
            ('customer', "Customer's language"),
            ('en', "English"),
            ('ar', "Arabic"),
        ],
        default='customer',
    )
    epays_link_expiry = fields.Integer(
        string="Payment Link Expiry (minutes)",
        help="How long the customer has to complete the payment on the ePays page.",
        default=const.DEFAULT_LINK_EXPIRY_MINUTES,
    )
    epays_send_customer_details = fields.Boolean(
        string="Send Customer Details",
        help="Send the customer's name, email, phone and address to ePays.",
        default=True,
    )
    epays_gateway_ids = fields.One2many(
        string="ePays Gateways",
        help="The gateways the merchant subscribed to on ePays, as last synced.",
        comodel_name='payment.epays.gateway',
        inverse_name='provider_id',
    )

    #=== COMPUTE METHODS ===#

    @api.depends('epays_mode')
    def _compute_epays_api_url(self):
        for provider in self:
            provider.epays_api_url = const.API_URLS.get(provider.epays_mode or 'production')

    def _compute_epays_server_ip(self):
        server_ip = self.env['ir.config_parameter'].sudo().get_param(const.SERVER_IP_PARAM)
        for provider in self:
            provider.epays_server_ip = server_ip or False

    @api.model
    def _epays_set_server_ip(self, server_ip):
        """ Remember this server's public IP address, for every provider. """
        self.env['ir.config_parameter'].sudo().set_param(const.SERVER_IP_PARAM, server_ip)
        self.env['payment.provider'].invalidate_model(['epays_server_ip'])

    #=== ONCHANGE METHODS ===#

    @api.onchange('state')
    def _onchange_state_epays_mode(self):
        """ Follow the state: Enabled takes real payments, so it uses Production; switching to
        Test moves a provider off Production to the sandbox. A Test provider can still be set back
        to Production by hand, e.g. for staff to try the live gateways. """
        if self.code != 'epays':
            return
        if self.state == 'enabled':
            self.epays_mode = 'production'
        elif self.state == 'test' and self.epays_mode == 'production':
            self.epays_mode = 'sandbox'

    #=== CRUD METHODS ===#

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._epays_clean_credentials(vals)
        return super().create(vals_list)

    def write(self, vals):
        self._epays_clean_credentials(vals)
        return super().write(vals)

    @api.model
    def _epays_clean_credentials(self, vals):
        """ Trim pasted credentials, and ignore a secret written back as its mask: a client that
        read the record and saves it whole must not replace the key with the mask. """
        for field_name in const.CREDENTIAL_FIELDS:
            value = vals.get(field_name)
            if field_name in const.SECRET_FIELDS and value == const.SECRET_MASK:
                del vals[field_name]
            elif isinstance(value, str):
                vals[field_name] = value.strip() or False

    #=== SECRETS: NEVER READ, SEARCHED, GROUPED OR SORTED ===#

    def _read_format(self, fnames, load='_classic_read'):
        """ Override of `base` to send a mask instead of the master keys, to the web client and to
        any RPC call. The addon reads them as record attributes, which this does not affect. """
        result = super()._read_format(fnames, load=load)
        secret_fields = [name for name in const.SECRET_FIELDS if name in fnames]
        for values in result if secret_fields else []:
            for field_name in secret_fields:
                if values.get(field_name):
                    values[field_name] = const.SECRET_MASK
        return result

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None):
        """ Override of `base`: a search on a master key would reveal it character by
        character. """
        self._epays_check_no_secret_in(
            [leaf[0] for leaf in domain or [] if isinstance(leaf, (list, tuple)) and leaf],
            [order or ''],
        )
        return super()._search(domain, offset=offset, limit=limit, order=order)

    @api.model
    def _read_group(  # noqa: PLR0917 - Odoo's signature.
        self, domain, groupby=(), aggregates=(), having=(), offset=0, limit=None, order=None
    ):
        """ Override of `base`: grouping by a master key would list the keys. """
        self._epays_check_no_secret_in(list(groupby) + list(aggregates), [order or ''])
        return super()._read_group(
            domain, groupby=groupby, aggregates=aggregates, having=having, offset=offset,
            limit=limit, order=order,
        )

    @api.model
    def _epays_check_no_secret_in(self, field_specs, orders):
        """ Refuse field specs (`field`, `field.sub`, `field:agg`) and orders naming a secret.

        :raise AccessError: If one of them names a master key.
        """
        named = {str(spec).split(':')[0].split('.')[0].strip() for spec in field_specs}
        for order in orders:
            named.update(part.split()[0] for part in order.split(',') if part.split())
        if named & set(const.SECRET_FIELDS):
            raise AccessError(_("The ePays master keys cannot be searched, grouped or sorted."))

    #=== CONSTRAINT METHODS ===#

    @api.constrains(*const.CREDENTIAL_FIELDS)
    def _check_epays_credentials_format(self):
        """ A space or a line break in a credential makes every request fail, and `requests`
        repeats an invalid header value in its error message; the value is refused here instead.
        The message names the field, never its value. """
        for provider in self.sudo().filtered(lambda p: p.code == 'epays'):
            for field_name in const.CREDENTIAL_FIELDS:
                value = provider[field_name]
                if value and (_INVALID_CREDENTIAL.search(value) or value == const.SECRET_MASK):
                    raise ValidationError(_(
                        "The %s contains spaces or line breaks. Copy it again from ePays.",
                        provider._fields[field_name].string,
                    ))

    @api.constrains('state', 'epays_mode')
    def _check_epays_mode_when_enabled(self):
        """ An Enabled provider is published to customers: it must not confirm their orders with
        test payments. """
        for provider in self.filtered(lambda p: p.code == 'epays'):
            if provider.state == 'enabled' and provider.epays_mode not in (False, 'production'):
                raise ValidationError(_(
                    "An enabled ePays provider takes real payments: set its Mode to Production, "
                    "or set its state to Test."
                ))

    @api.constrains('is_published', 'epays_mode')
    def _check_epays_localhost_not_published(self):
        """ Localhost is the developer's own ePays server: a customer sent there would land on
        their own computer. Only internal users may see an unpublished Test provider. """
        for provider in self.filtered(lambda p: p.code == 'epays'):
            if provider.epays_mode == 'localhost' and provider.is_published:
                raise ValidationError(_(
                    "An ePays provider in Localhost mode cannot be published: customers would be "
                    "sent to their own computer. Unpublish it, or set its Mode to Sandbox."
                ))

    @api.constrains('epays_link_expiry')
    def _check_epays_link_expiry(self):
        for provider in self.filtered(lambda p: p.code == 'epays'):
            if provider.epays_link_expiry < 1:
                raise ValidationError(
                    _("The payment link expiry must be at least one minute.")
                )

    #=== BUSINESS METHODS ===#

    def _get_compatible_providers(  # noqa: PLR0917 - Odoo's signature.
        self, company_id, partner_id, amount, currency_id=None, force_tokenization=False,
        is_express_checkout=False, is_validation=False, report=None, **kwargs
    ):
        """ Override of `payment` to offer ePays only in a currency Odoo can convert to the one
        ePays charges in. """
        providers = super()._get_compatible_providers(
            company_id, partner_id, amount, currency_id=currency_id,
            force_tokenization=force_tokenization, is_express_checkout=is_express_checkout,
            is_validation=is_validation, report=report, **kwargs
        )
        currency = self.env['res.currency'].browse(currency_id).exists()
        company = self.env['res.company'].browse(company_id).exists()
        if currency and company and not self._epays_can_convert(currency, company):
            unfiltered_providers = providers
            providers = providers.filtered(lambda p: p.code != 'epays')
            payment_utils.add_to_report(
                report,
                unfiltered_providers - providers,
                available=False,
                reason=REPORT_REASONS_MAPPING['incompatible_currency'],
            )
        return providers

    @api.model
    def _epays_get_charge_currency(self):
        """ Return the currency ePays charges in (BHD). """
        return self.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', const.CHARGE_CURRENCY)], limit=1
        )

    @api.model
    def _epays_can_convert(self, currency, company):
        """ Return whether a payment in the currency can be converted to the one ePays charges in.

        Odoo converts with a rate of 1 when a currency has no rate at all, which would charge
        100 USD as 100 BHD. Both currencies must therefore have a rate, except the company's own
        currency, whose rate is always 1.

        :param res.currency currency: The currency of the payment.
        :param res.company company: The company of the payment.
        :return: Whether the payment can be converted.
        :rtype: bool
        """
        charge_currency = self._epays_get_charge_currency()
        if not charge_currency:
            return False
        if currency == charge_currency:
            return True
        return all(
            c == company.currency_id or self.env['res.currency.rate'].sudo().search_count([
                ('currency_id', '=', c.id),
                ('company_id', 'in', (False, company.root_id.id)),
            ], limit=1)
            for c in (currency, charge_currency)
        )

    def _get_default_payment_method_codes(self):
        """ Override of `payment` to return the default payment method codes. """
        default_codes = super()._get_default_payment_method_codes()
        if self.code != 'epays':
            return default_codes
        return const.DEFAULT_PAYMENT_METHOD_CODES

    @api.model
    def _epays_get_payment_methods(self, codes):
        """ Return the addon's payment methods with the given codes, active or not.

        :param iterable codes: The payment method codes.
        :return: The payment methods.
        :rtype: payment.method
        """
        return self.env['payment.method'].with_context(active_test=False).search(
            [('code', 'in', list(codes))]
        )

    @api.model
    def _epays_ensure_provider_per_company(self, companies=None):
        """ Give every company its own ePays provider, Disabled and without credentials.

        Odoo shows each company only its own providers, and creates the addon's provider in one
        company: without this, ePays would be missing from Payment Providers in every other
        company. Called from the data files on every install and update, and when a company is
        created.

        :param res.company companies: The companies to check; all of them by default.
        :return: None
        """
        base_provider = self.env.ref(
            'payment_epays.payment_provider_epays', raise_if_not_found=False
        )
        if not base_provider:
            return
        base_provider = base_provider.sudo()
        companies = (companies or self.env['res.company'].search([])).sudo()
        providers = self.sudo().with_context(active_test=False).search([('code', '=', 'epays')])
        missing_companies = companies - providers.company_id
        for company in missing_companies:
            base_provider.copy({
                'name': base_provider.name,
                'company_id': company.id,
                'state': 'disabled',
                'is_published': False,
            })
        if missing_companies:
            self._epays_setup_payment_methods()

    @api.model
    def _epays_setup_payment_methods(self):
        """ Link the addon's payment methods to every ePays provider, and activate the default ones
        of the providers that are not disabled.

        Called from the data files on every install and update, so that providers created before
        these payment methods existed, or duplicated for another company, get them too.

        :return: None
        """
        primary_methods = self._epays_get_payment_methods(
            (const.PRIMARY_PAYMENT_METHOD_CODE, *const.INDIVIDUAL_PAYMENT_METHOD_CODES)
        ).filtered('is_primary')
        providers = self.search([('code', '=', 'epays')])
        for provider in providers:
            linked_methods = provider.with_context(active_test=False).payment_method_ids
            if linked_methods != primary_methods:
                provider.payment_method_ids = [Command.set(primary_methods.ids)]
        providers.filtered(lambda p: p.state != 'disabled')._activate_default_pms()

    def _epays_get_environment(self):
        """ Return the ePays environment of new payments: the mode of the provider.

        Note: self.ensure_one()

        :return: `production`, `sandbox` or `localhost`.
        :rtype: str
        """
        self.ensure_one()
        return self.epays_mode or 'production'

    @api.model
    def _epays_get_environment_label(self, environment):
        """ Return the translated name of an environment, e.g. `Sandbox`. """
        labels = dict(self._fields['epays_mode']._description_selection(self.env))
        return labels.get(environment, environment)

    @api.model
    def _epays_get_api_url(self, environment):
        """ Return the base URL of the ePays API for the given environment, without trailing slash.

        :param str environment: `production`, `sandbox` or `localhost`.
        :return: The base URL.
        :rtype: str
        """
        return const.API_URLS.get(environment, const.API_URLS['production'])

    def _epays_get_credentials(self, environment):
        """ Return the API ID and master key to use in the given environment.

        Outside production, the sandbox pair is used when both of its fields are filled;
        otherwise, the production pair is used.

        Note: self.ensure_one()

        :param str environment: `production`, `sandbox` or `localhost`.
        :return: The API ID and the master key.
        :rtype: tuple[str, str]
        """
        self.ensure_one()
        provider = self.sudo()
        if environment != 'production' and provider.epays_sandbox_api_id \
                and provider.epays_sandbox_master_key:
            return provider.epays_sandbox_api_id.strip(), provider.epays_sandbox_master_key.strip()
        return (provider.epays_api_id or '').strip(), (provider.epays_master_key or '').strip()

    def _epays_get_merchant_domain(self):
        """ Return the merchant domain to authenticate with.

        Note: self.ensure_one()

        :return: The configured domain, or the host of the base URL of the provider.
        :rtype: str
        """
        self.ensure_one()
        configured_domain = (self.epays_merchant_domain or '').strip()
        if configured_domain:
            # Tolerate a pasted URL: keep only its host.
            if '//' in configured_domain:
                configured_domain = urlsplit(configured_domain).hostname or configured_domain
            return configured_domain.strip('/').lower()
        return (urlsplit(self.get_base_url()).hostname or '').lower()

    def _epays_get_user_agent(self, domain):
        """ Return the User-Agent of the requests to ePays: the Odoo and addon versions, the
        database and the site. ePays stores it as the payment's creation agent, which is how a
        payment is traced back to the Odoo instance that made it (udf6 and above are gateway
        overrides on ePays, not free fields).

        HTTP headers are Latin-1 only, so anything else is dropped.

        :param str domain: The merchant domain of the request.
        :return: The User-Agent, e.g.
                 `Odoo/18.0 payment_epays/18.0.1.0.0 (db prod; site shop.example.com)`.
        :rtype: str
        """
        module = self.env.ref('base.module_payment_epays', raise_if_not_found=False)
        module_version = module and (module.sudo().latest_version or module.installed_version) or ''
        user_agent = (
            f"Odoo/{release.version} payment_epays/{module_version} "
            f"(db {self.env.cr.dbname}; site {domain or '-'})"
        )
        return user_agent.encode('ascii', 'ignore').decode()

    @api.model
    def _epays_get_connection_error_message(self):
        """ Return the generic message of a failed call to ePays. It is safe to show to a
        customer: the details are logged, and shown to the admin by the Test connection button. """
        return "ePays: " + _("Could not establish the connection to the API.")

    def _epays_make_request(
        self, method, path, *, environment, domain, payload=None, idempotency_key=None
    ):
        """ Make a request to the ePays v2 API and return the `data` of the response envelope.

        Note: self.ensure_one()

        :param str method: The HTTP method of the request.
        :param str path: The path of the endpoint, starting with `/api/v2/`.
        :param str environment: The environment to reach: `production`, `sandbox` or
                                `localhost`.
        :param str domain: The merchant domain to authenticate with.
        :param dict payload: The JSON payload of the request, if any.
        :param str idempotency_key: The idempotency key of the request, if any.
        :return: The `data` of the response.
        :rtype: dict
        :raise EpaysApiError: If the request fails for any reason. The message is generic; the
                              details are logged and kept on the exception.
        """
        self.ensure_one()

        url = f'{self._epays_get_api_url(environment)}/{path.lstrip("/")}'
        api_id, master_key = self._epays_get_credentials(environment)
        headers = {
            'Accept': 'application/json',
            'X-Api-Id': api_id,
            'X-Api-Master-Key': master_key,
            'X-Merchant-Domain': domain or '',
            'User-Agent': self._epays_get_user_agent(domain),
        }
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        if idempotency_key:
            headers['Idempotency-Key'] = idempotency_key

        generic_message = self._epays_get_connection_error_message()
        # The headers are never logged: they hold the master key. The payload and the response
        # hold the customer's personal data, so they are logged at DEBUG level only.
        _logger.info("sending %s request to %s (environment: %s)", method, url, environment)
        _logger.debug("request payload:\n%s", pprint.pformat(payload))
        try:
            # Redirects are refused: `requests` would resend the master key header to the new host,
            # as it strips only the standard `Authorization` header.
            response = requests.request(
                method, url, json=payload, headers=headers, timeout=const.REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as error:
            # Neither the traceback nor the exception chain is kept: `requests` repeats an invalid
            # header value, i.e. a credential, in its error message.
            reason = _redact(str(error), (api_id, master_key))
            if isinstance(error, requests.exceptions.InvalidHeader):
                reason = "invalid credentials format"
            _logger.warning("unable to reach endpoint at %s: %s", url, reason)
            raise EpaysApiError(generic_message, connection_error=reason) from None

        if 300 <= response.status_code < 400:
            _logger.warning(
                "refusing the redirect from %s (HTTP %s) to %s", url, response.status_code,
                response.headers.get('Location'),
            )
            raise EpaysApiError(
                generic_message, http_status=response.status_code,
                error_message="unexpected redirect",
            )

        try:
            body = response.json()
        except ValueError:
            body = None

        is_success = (
            response.ok
            and isinstance(body, dict)
            and str(body.get('status', '')).upper() != 'ERROR'
        )
        if not is_success:
            error = body.get('error') if isinstance(body, dict) else None
            error = error if isinstance(error, dict) else {}
            _logger.warning(
                "invalid API request at %s (HTTP %s):\n%s",
                url, response.status_code,
                pprint.pformat(body if isinstance(body, dict) else response.text[:1000]),
            )
            raise EpaysApiError(
                generic_message,
                http_status=response.status_code,
                error_code=error.get('code'),
                error_message=error.get('message'),
                details=error.get('details'),
            )

        data = body.get('data')
        data = data if isinstance(data, dict) else {}
        _logger.info("response received from %s (HTTP %s)", url, response.status_code)
        _logger.debug("response data:\n%s", pprint.pformat(data))
        return data

    #=== ACTION METHODS ===#

    def _epays_check_admin_access(self):
        """ Allow the ePays buttons to the users who may configure the provider only.

        Button methods can be called over RPC by anyone who can read the record, and these ones use
        the master key and reveal the server's public IP address.

        :raise AccessError: If the user may not write on the provider.
        """
        self.check_access('write')

    def action_epays_test_connection(self):
        """ Make an authenticated call to the server of the provider's mode and report the result
        to the admin.

        This is the only place where the detailed reason of an authentication failure is shown.

        Note: self.ensure_one()

        :return: The client action displaying the result.
        :rtype: dict
        """
        self.ensure_one()
        self._epays_check_admin_access()
        environment = self._epays_get_environment()
        label = self._epays_get_environment_label(environment)
        domain = self._epays_get_merchant_domain()
        ok = False
        api_id, master_key = self._epays_get_credentials(environment)
        if not api_id or not master_key:
            result = _("the API ID and the API master key are required.")
        else:
            try:
                self._epays_make_request(
                    'GET', f'{const.PAYMENTS_PATH}?limit=1', environment=environment, domain=domain
                )
            except EpaysApiError as error:
                result = self._epays_diagnose_error(error, domain)
                self._epays_record_server_ip(error)
            else:
                ok = True
                result = _("the connection works.")

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("ePays connection test (merchant domain: %s)", domain or '-'),
                'message': _("%(env)s (%(url)s): %(result)s", env=label,
                             url=self._epays_get_api_url(environment), result=result),
                'type': 'success' if ok else 'warning',
                'sticky': not ok,
            },
        }

    def action_epays_find_server_ip(self):
        """ Find the public IP address of this Odoo server, to register on the ePays licence.

        ePays is asked first: when the licence refuses the server, ePays reports the exact address
        it saw, which is the one to register. Otherwise (no credentials yet, or the licence already
        accepts the server), a public IP echo service is asked.

        Note: self.ensure_one()

        :return: The client action displaying the result.
        :rtype: dict
        """
        self.ensure_one()
        self._epays_check_admin_access()
        title = _("This server's IP address")
        epays_verdict, server_ip = self._epays_ask_epays_for_server_ip()
        if not server_ip:
            server_ip = self._epays_get_public_ip()
        if not server_ip:
            return self._epays_notification(title, _(
                "The public IP address of this server could not be found: it may not reach the "
                "internet. Ask your hosting provider for its outbound IP address."
            ), 'warning')

        self._epays_set_server_ip(server_ip)
        if epays_verdict == 'refused':
            message = _(
                "ePays sees this server as %s, and your licence does not allow it yet. Ask ePays "
                "to add this IP address to your licence.", server_ip,
            )
        elif epays_verdict == 'accepted':
            message = _(
                "This server's public IP address is %s. Your ePays licence already allows it: the "
                "connection works.", server_ip,
            )
        else:
            message = _(
                "This server's public IP address is %s. Ask ePays to add it to your licence, then "
                "click Test connection.", server_ip,
            )
        return self._epays_notification(title, message, 'success')

    def _epays_ask_epays_for_server_ip(self):
        """ Make an authenticated call to ePays to learn whether it accepts this server's IP.

        :return: `accepted`, `refused` or `None` (no credentials, or another answer), and the IP
                 address ePays saw when it refused it.
        :rtype: tuple[str|None, str|None]
        """
        environment = self._epays_get_environment()
        api_id, master_key = self._epays_get_credentials(environment)
        if not api_id or not master_key:
            return None, None
        try:
            self._epays_make_request(
                'GET', f'{const.PAYMENTS_PATH}?limit=1', environment=environment,
                domain=self._epays_get_merchant_domain(),
            )
        except EpaysApiError as error:
            observed_ip = self._epays_get_observed_ip(error)
            return ('refused', observed_ip) if observed_ip else (None, None)
        return 'accepted', None

    @api.model
    def _epays_get_observed_ip(self, error):
        """ Return the IP address ePays saw, when it refused it (X004), if valid. """
        if error.http_status != 401 or error.reason != const.AUTH_REASON_INVALID_IP:
            return None
        return _to_ip_address(error.details.get('observedIp'))

    def _epays_record_server_ip(self, error):
        """ Keep the IP address ePays refused, so that the admin can copy it from the form. """
        observed_ip = self._epays_get_observed_ip(error)
        if observed_ip:
            self._epays_set_server_ip(observed_ip)

    @api.model
    def _epays_get_public_ip(self):
        """ Return this server's public IP address as a public echo service sees it, if any.

        No credential is sent; the answer must be a bare IP address.

        :return: The IP address, or `None`.
        :rtype: str|None
        """
        for url in const.IP_ECHO_URLS:
            try:
                response = requests.request(
                    'GET', url, headers={'Accept': 'text/plain'},
                    timeout=const.REQUEST_TIMEOUT, allow_redirects=False,
                )
            except requests.exceptions.RequestException as error:
                _logger.info("could not reach %s: %s", url, error)
                continue
            server_ip = _to_ip_address(response.text if response.ok else None)
            if server_ip:
                return server_ip
            _logger.info(
                "%s did not answer with an IP address (HTTP %s)", url, response.status_code
            )
        return None

    def action_epays_sync_gateways(self):
        """ Fetch the merchant's gateways from the server of the provider's mode and update the
        gateway table.

        Gateways that ePays no longer returns are removed. The payment method of a gateway already
        in the table is kept, since the admin may have corrected it.

        Note: self.ensure_one()

        :return: The client action displaying the result.
        :rtype: dict
        """
        self.ensure_one()
        self._epays_check_admin_access()
        environment = self._epays_get_environment()
        label = self._epays_get_environment_label(environment)
        domain = self._epays_get_merchant_domain()
        title = _("ePays gateways (%s)", label)

        api_id, master_key = self._epays_get_credentials(environment)
        if not api_id or not master_key:
            return self._epays_notification(
                title, _("The API ID and the API master key are required."), 'warning'
            )
        try:
            data = self._epays_make_request(
                'GET', const.GATEWAYS_PATH, environment=environment, domain=domain
            )
        except EpaysApiError as error:
            return self._epays_notification(
                title, self._epays_diagnose_error(error, domain), 'warning'
            )

        gateways = data.get('gateways')
        found, mapped = self._epays_update_gateways(
            environment, gateways if isinstance(gateways, list) else []
        )
        return self._epays_notification(
            title,
            _("%(found)s gateways found on ePays, %(mapped)s linked to a payment method.",
              found=found, mapped=mapped),
            'success',
        )

    def _epays_update_gateways(self, environment, gateways):
        """ Upsert the gateway rows of an environment from the list returned by ePays.

        Note: self.ensure_one()

        :param str environment: `production`, `sandbox` or `localhost`.
        :param list gateways: The `gateways` of the response of `GET /api/v2/gateways`.
        :return: The number of checkout gateways found, and how many are linked to a method.
        :rtype: tuple[int, int]
        """
        self.ensure_one()
        Gateway = self.env['payment.epays.gateway']
        existing_rows = Gateway.search(
            [('provider_id', '=', self.id), ('environment', '=', environment)]
        )
        rows_by_ref = {row.gateway_ref: row for row in existing_rows}
        methods_by_code = {
            method.code: method
            for method in self._epays_get_payment_methods(
                const.GATEWAY_TYPE_PAYMENT_METHOD_CODES.values()
            )
        }

        kept_rows = Gateway
        for gateway in gateways:
            if not isinstance(gateway, dict):
                continue
            gateway_ref = str(gateway.get('merchantGateway') or '').strip()
            if not gateway_ref or gateway_ref in kept_rows.mapped('gateway_ref'):
                continue
            gateway_type_id = _to_int(gateway.get('gatewayId'))
            if gateway_type_id in const.NON_CHECKOUT_GATEWAY_TYPES:
                continue  # An integration, not a checkout payment method.
            values = {
                'gateway_type_id': gateway_type_id,
                'name': str(gateway.get('gatewayName') or '').strip() or gateway_ref,
                'gateway_class': str(gateway.get('gatewayClass') or '').strip(),
                'status': str(gateway.get('status') or '').strip(),
            }
            default_method = methods_by_code.get(
                const.GATEWAY_TYPE_PAYMENT_METHOD_CODES.get(gateway_type_id)
            )
            row = rows_by_ref.get(gateway_ref)
            if row:
                if not row.payment_method_id and default_method:
                    values['payment_method_id'] = default_method.id
                row.write(values)
            else:
                row = Gateway.create({
                    'provider_id': self.id,
                    'environment': environment,
                    'gateway_ref': gateway_ref,
                    'payment_method_id': default_method.id if default_method else False,
                    **values,
                })
            kept_rows |= row

        (existing_rows - kept_rows).unlink()
        return len(kept_rows), len(kept_rows.filtered('payment_method_id'))

    @api.model
    def _epays_notification(self, title, message, notification_type):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': notification_type,
                'sticky': notification_type != 'success',
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _epays_diagnose_error(self, error, domain):
        """ Return a human-readable diagnosis of a failed request, for the admin only.

        :param EpaysApiError error: The failure.
        :param str domain: The merchant domain used for the request.
        :return: The diagnosis.
        :rtype: str
        """
        if error.connection_error:
            return _("the ePays API could not be reached (%s). Check the Mode.",
                     error.connection_error)
        if error.http_status == 401:
            reason = error.reason
            if reason == const.AUTH_REASON_INVALID_DOMAIN:
                return _("the ePays licence does not list the domain %s. Ask ePays to register it, "
                         "or set the Merchant Domain to the registered one.", domain or '-')
            if reason == const.AUTH_REASON_INVALID_IP:
                observed_ip = error.details.get('observedIp')
                if observed_ip:
                    return _("the ePays licence does not allow the IP address of this Odoo server "
                             "(%s). Ask ePays to register this IP address.", observed_ip)
                return _("the ePays licence does not allow the IP address of this Odoo server. "
                         "Ask ePays to register the public IP address of the server.")
            if reason == const.AUTH_REASON_MISSING_LICENSE:
                return _("ePays found no licence for this API ID. Check the API ID.")
            if reason == const.AUTH_REASON_TOO_MANY_ATTEMPTS:
                return _("too many failed attempts. Wait a few minutes before trying again.")
            return _("the API ID or the API master key is wrong.")
        if error.http_status:
            return _("ePays answered with HTTP %(status)s %(code)s %(message)s",
                     status=error.http_status, code=error.error_code,
                     message=error.error_message).strip()
        return _("the response of ePays could not be read.")


def _to_ip_address(value):
    """ Return the value as a normalised IP address, or `None` if it is not exactly one. """
    try:
        return str(ipaddress.ip_address(str(value or '').strip()))
    except ValueError:
        return None


def _redact(text, secrets):
    """ Return the text with the given secrets, raw or as Python would print them, masked. """
    for secret in secrets:
        if secret:
            for form in {secret, repr(secret)[1:-1]}:
                text = text.replace(form, const.SECRET_MASK)
    return text


def _to_int(value):
    """ Return the value as an integer, or 0 if it is not one. ePays may send ids as strings. """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0
