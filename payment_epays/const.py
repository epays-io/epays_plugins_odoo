# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

from datetime import timedelta

# The ePays servers the provider can use (its mode), and their base URLs. The mode is stored on
# every transaction and gateway row as its environment.
API_URLS = {
    'production': 'https://api.epays.io',
    'sandbox': 'https://testapi.epays.io',
    # An ePays API running on the Odoo server itself, for development.
    'localhost': 'https://localhost:7124',
}
ENVIRONMENTS = [
    ('production', "Production"),
    ('sandbox', "Sandbox"),
    ('localhost', "Localhost"),
]

# Public services that answer with the IP address a request comes from, over https, as plain text.
# Used to show the admin this server's public IP address when ePays cannot tell it.
IP_ECHO_URLS = (
    'https://api.ipify.org',
    'https://icanhazip.com',
)

# The system parameter holding this server's public IP address, as last found. It describes the
# server, not a provider, and must not be a column of payment.provider: a new column there makes
# Apps > Upgrade fail, as Odoo recomputes the providers' colour before the column exists.
SERVER_IP_PARAM = 'payment_epays.server_ip'

# The API paths used by the addon.
PAYMENTS_PATH = '/api/v2/Payments'
GATEWAYS_PATH = '/api/v2/gateways'

# The shop's landing route after a payment (website_sale), and where a customer whose ePays payment
# failed or was cancelled is sent instead: back to the payment step, with the cart kept, and the
# query parameter that makes the page show why.
SHOP_LANDING_ROUTE = '/shop/payment/validate'
SHOP_PAYMENT_ROUTE = '/shop/payment'
FAILURE_QUERY_PARAM = 'epays_failed'

# The key under which the return route passes the environment named in its URL to the transaction
# lookup.
NOTIFICATION_ENVIRONMENT_KEY = 'epays_environment'

# The timeout of every request made to the ePays API, in seconds.
REQUEST_TIMEOUT = 10

# The credentials of a provider, and among them the secrets. A secret never leaves the server:
# reading it returns SECRET_MASK once it is set, and writing SECRET_MASK back keeps the stored
# value.
CREDENTIAL_FIELDS = (
    'epays_api_id', 'epays_master_key', 'epays_sandbox_api_id', 'epays_sandbox_master_key',
)
SECRET_FIELDS = ('epays_master_key', 'epays_sandbox_master_key')
SECRET_MASK = '********'

# The only currency ePays takes payments in (ISO 4217). A payment in another currency is converted
# to it with Odoo's exchange rate before it is sent to ePays.
CHARGE_CURRENCY = 'BHD'

# The code of the single "ePays" payment method, with which the customer chooses the payment
# method on the ePays page.
PRIMARY_PAYMENT_METHOD_CODE = 'epays'

# The brands displayed next to the "ePays" payment method.
PRIMARY_PAYMENT_METHOD_BRAND_CODES = {
    'epays_brand_visa',
    'epays_brand_mastercard',
    'epays_brand_benefit',
    'epays_brand_benefit_pay',
    'epays_brand_apple_pay',
    'epays_brand_google_pay',
    'epays_brand_samsung_pay',
    'epays_brand_tabby',
}

# The payment methods that open one ePays gateway directly. The admin activates them.
INDIVIDUAL_PAYMENT_METHOD_CODES = (
    'epays_card',
    'epays_benefit',
    'epays_benefit_pay',
    'epays_apple_pay',
    'epays_google_pay',
    'epays_samsung_pay',
    'epays_tabby',
)

# Every payment method of the addon offered at checkout (the brands are not offered on their own).
EPAYS_METHOD_CODES = {PRIMARY_PAYMENT_METHOD_CODE, *INDIVIDUAL_PAYMENT_METHOD_CODES}

# The codes of the payment methods to activate when ePays is activated: only the single "ePays"
# option and its brands.
DEFAULT_PAYMENT_METHOD_CODES = {PRIMARY_PAYMENT_METHOD_CODE} | PRIMARY_PAYMENT_METHOD_BRAND_CODES

# The default payment method of an ePays gateway, by ePays gateway type id.
GATEWAY_TYPE_PAYMENT_METHOD_CODES = {
    1: 'epays_benefit',  # Benefit debit card.
    2: 'epays_card',
    3: 'epays_google_pay',
    4: 'epays_apple_pay',
    5: 'epays_tabby',
    6: 'epays_benefit_pay',
    9: 'epays_samsung_pay',
}

# ePays gateway types that are integrations rather than checkout payment methods (Zoho, Odoo).
NON_CHECKOUT_GATEWAY_TYPES = {7, 8}

# The status of an ePays gateway that can take payments.
ACTIVE_GATEWAY_STATUS = 'active'

# Mapping of transaction states to ePays payment statuses.
# The values are compared case-insensitively after trimming. They cover both the canonical `status`
# field of the v2 payment details and the raw `result` field that older ePays servers return alone.
# Any value not listed here (including `SUCCESS` and `Unknown`) leaves the transaction untouched.
# `refunded` is not a transaction state: the money went back, so it never completes an order (see
# `payment.transaction._epays_apply_payment_details`).
PAYMENT_STATUS_MAPPING = {
    'pending': ('pending', '3ds authentication initiated'),
    'done': ('completed', 'captured', 'approved'),
    'refunded': ('refunded',),
    'error': ('failed', 'declined', 'error'),
    'cancel': ('cancelled', 'canceled', 'expired', 'voided'),
}

# ePays interprets `expiryDate` in Bahrain time (UTC+3, no daylight saving time).
EPAYS_TIMEZONE_OFFSET = timedelta(hours=3)
EPAYS_DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%S'

# The default validity of a payment link, in minutes.
DEFAULT_LINK_EXPIRY_MINUTES = 60

# The size of the ePays custom field columns used by the addon (`udf2` and `udf3` are varchar(255)).
UDF_MAX_LENGTH = 255
UDF_LIST_SEPARATOR = ', '
UDF_ELLIPSIS = '…'

# When the customer pays the same order again, the earlier ePays payment is reused only if its link
# stays valid at least this long.
REUSE_MIN_REMAINING_VALIDITY = timedelta(minutes=5)

# The polling cron picks transactions in this age window.
CRON_MIN_AGE = timedelta(minutes=5)
CRON_MAX_AGE = timedelta(days=3)
# A payment still pending this long after its link expired is voided on ePays.
CRON_VOID_GRACE_PERIOD = timedelta(minutes=30)
# Finished transactions are post-processed by the cron for this long after their last state change.
CRON_POST_PROCESS_LIMIT = timedelta(days=4)

# The reasons published by ePays in `error.details.reason` of a 401 response.
AUTH_REASON_INVALID_DOMAIN = 'X002_INVALID_LICENSE_DOMAIN'
AUTH_REASON_INVALID_IP = 'X004_INVALID_LICENSE_IP'
AUTH_REASON_MISSING_LICENSE = 'X000_MISSING_LICENSE'
AUTH_REASON_TOO_MANY_ATTEMPTS = 'TOO_MANY_ATTEMPTS'

# Keys of `gatewayResponse` whose humanised label is not a plain title-cased split of the key.
GATEWAY_RESPONSE_LABELS = {
    'rrn': 'RRN',
}
