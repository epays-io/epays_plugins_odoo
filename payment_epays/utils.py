# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

import base64
import hashlib
import json
import re
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

from odoo.exceptions import ValidationError

from odoo.addons.payment_epays import const


class EpaysApiError(ValidationError):
    """ A failed call to the ePays API.

    The message is always generic, so that it can safely reach a customer. The details of the
    failure are kept on the exception for the admin-only diagnosis of the Test connection button.
    """

    def __init__(
        self, message, *, http_status=None, error_code=None, error_message=None, details=None,
        connection_error=None, timed_out=False,
    ):
        super().__init__(message)
        # The request was sent but not answered in time: ePays may still have processed it.
        self.timed_out = timed_out
        self.http_status = http_status
        self.error_code = error_code or ''
        self.error_message = error_message or ''
        self.details = details if isinstance(details, dict) else {}
        self.connection_error = connection_error

    @property
    def reason(self):
        return self.details.get('reason') or ''


def join_references(names, max_length=const.UDF_MAX_LENGTH):
    """ Join references with commas, cutting the list at a whole reference if it is too long.

    A cut list ends with an ellipsis, e.g. `S00001, S00002, …`.

    :param list names: The references to join.
    :param int max_length: The maximum length of the result.
    :return: The joined references.
    :rtype: str
    """
    names = [name for name in names if name]
    joined = const.UDF_LIST_SEPARATOR.join(names)
    if len(joined) <= max_length:
        return joined

    suffix = const.UDF_LIST_SEPARATOR + const.UDF_ELLIPSIS
    kept = ''
    for name in names:
        candidate = f'{kept}{const.UDF_LIST_SEPARATOR}{name}' if kept else name
        if len(candidate) + len(suffix) > max_length:
            break
        kept = candidate
    if not kept:  # Not even the first reference fits: cut it.
        return names[0][:max_length - len(const.UDF_ELLIPSIS)] + const.UDF_ELLIPSIS
    return kept + suffix


def get_payment_state(payment_data):
    """ Map the payment details returned by ePays to a transaction state.

    The canonical `status` field takes precedence. When it is absent (an ePays server that predates
    it), the raw `result` field is folded with the same table.

    :param dict payment_data: The `data` of the v2 payment details.
    :return: The target state (`pending`, `done`, `error` or `cancel`), `refunded`, or `None` if
             the value is not recognised, and the value that was mapped.
    :rtype: tuple[str|None, str]
    """
    value = payment_data.get('status')
    if value is None or (isinstance(value, str) and not value.strip()):
        value = payment_data.get('result')
    value = '' if value is None else str(value)
    normalized_value = value.strip().lower()
    for state, statuses in const.PAYMENT_STATUS_MAPPING.items():
        if normalized_value in statuses:
            return state, value.strip()
    return None, value.strip()


def is_encrypted(value):
    """ Return whether a stored master key is in the encrypted format. """
    return isinstance(value, str) and value.startswith(const.SECRET_PREFIX)


def _get_cipher(key_material):
    """ Return the cipher of a secret key material, derived for this one purpose. """
    digest = hashlib.sha256(f'payment_epays|{key_material}'.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plain, key_material):
    """ Encrypt a master key, so that the database only ever holds ciphertext.

    :param str plain: The master key.
    :param str key_material: The secret the encryption key is derived from.
    :return: The stored value.
    :rtype: str
    """
    return const.SECRET_PREFIX + _get_cipher(key_material).encrypt(plain.encode()).decode()


def decrypt_secret(stored, key_material):
    """ Decrypt a stored master key.

    A value that is not encrypted was stored before the addon encrypted the keys, and is returned
    as it is: a failed migration must not stop the payments.

    :param str stored: The stored value.
    :param str key_material: The secret the encryption key is derived from.
    :return: The master key, or `None` if it cannot be decrypted (the secret changed).
    :rtype: str|None
    """
    if not is_encrypted(stored):
        return stored
    try:
        token = stored[len(const.SECRET_PREFIX):].encode()
        return _get_cipher(key_material).decrypt(token).decode()
    except (InvalidToken, ValueError):
        return None


def _fit_order_lines(items, decimals, per_unit):
    """ Return the lines of the items, or `None` if a unit price does not divide its total.

    :param list items: The items, see `get_order_lines`.
    :param int decimals: The decimals of the charged currency.
    :param bool per_unit: Whether to send the real quantities, or one line per item.
    :return: The lines, as ePays reads them.
    :rtype: list[dict]|None
    """
    lines = []
    for item in items:
        quantity = item['quantity'] if per_unit else 1
        unit_price, remainder = divmod(item['total'], quantity)
        if remainder:
            return None
        name = item['name']
        if not per_unit and item['quantity'] != 1:
            name = f"{name} × {item['quantity']}"
        line = {
            'name': name[:const.TEXT_MAX_LENGTH],
            'quantity': quantity,
            'unitPrice': format_minor_units(unit_price, decimals),
        }
        if item.get('sku'):
            line['sku'] = item['sku'][:const.TEXT_MAX_LENGTH]
        lines.append(line)
    return lines


def get_order_lines(items, shipping, charged, decimals):
    """ Return the order lines to send to ePays, or `None` if they cannot add up to the charge.

    Tabby recomputes what it charges from them: the sum of `unitPrice × quantity` plus
    `shippingAmount` (tax and discount are not added). So they are sent only when that sum equals
    the charged amount exactly; otherwise ePays keeps the amount. A line whose total does not divide
    by its quantity is sent once, with its line total.

    :param list items: The items, as dicts with `name`, `quantity`, `sku` and `total`, their total
                       in minor units of the charged currency.
    :param int shipping: The shipping, in minor units.
    :param int charged: The amount charged, in minor units.
    :param int decimals: The decimals of the charged currency.
    :return: The lines, as ePays reads them.
    :rtype: list[dict]|None
    """
    if not items or any(item['total'] < 0 for item in items):
        return None
    if shipping + sum(item['total'] for item in items) != charged:
        return None
    return _fit_order_lines(items, decimals, True) or _fit_order_lines(items, decimals, False)


def to_minor_units(amount, decimals):
    """ Return an amount in minor units (e.g. fils), which add up exactly, unlike floats. """
    return round(amount * 10 ** decimals)


def format_minor_units(amount, decimals):
    """ Return minor units as the decimal string ePays reads, e.g. `12.500`. """
    return f'{amount / 10 ** decimals:.{decimals}f}'


def is_at_bank(payment_data):
    """ Return whether the customer submitted the payment and the gateway has not answered yet.

    ePays reports `inFlight` while it can no longer stop the customer's money, and refuses to void
    the payment. Older servers do not send it, and set `transactionId` when the customer pays: a
    void does not stop such a payment, as the bank's answer can still take the money on a voided
    payment. `transactionId` can stay empty until the gateway answers, e.g. in the BenefitPay
    wallet, so a payment voided during 3-D Secure cannot be told apart on those servers. A declined
    payment can be `inFlight` too, as ePays lets its customer pay again on the same link; its
    `transactionId` is the one of the decline.

    :param dict payment_data: The `data` of the v2 payment details.
    :return: Whether the customer's money may be moving.
    :rtype: bool
    """
    state = get_payment_state(payment_data)[0]
    if state == 'pending' and not is_empty(payment_data.get('transactionId')):
        return True
    return state in ('pending', 'error') and payment_data.get('inFlight') is True


def parse_json(value):
    """ Return the value parsed as JSON, or `None` if it is empty or not valid JSON. """
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def humanize_key(key):
    """ Turn a camelCase key into a label, e.g. `cardBrand` into `Card Brand`. """
    if key in const.GATEWAY_RESPONSE_LABELS:
        return const.GATEWAY_RESPONSE_LABELS[key]
    words = re.sub(r'(?<=[a-z0-9])(?=[A-Z])|[_\-]+', ' ', str(key)).split()
    return ' '.join(word[:1].upper() + word[1:] for word in words)


def get_first(data, *keys):
    """ Return the first value that carries information among `keys`, e.g. a v2 field and the name
    it had before.

    ePays renamed v2 fields on 2026-10-18 (`gatewayId` and `merchantGateway` became
    `subscriptionId`, the gateway type `gatewayId` became `gatewayTypeId`, `paidAt` became
    `completedAt`). A server not updated yet still sends the former names, so both are read, the
    new one first.

    :param dict data: The data received from ePays.
    :param str keys: The field names, the current one first.
    :return: The value, or `None`.
    """
    for key in keys:
        value = data.get(key)
        if not is_empty(value):
            return value
    return None


def parse_datetime(value):
    """ Return an ePays timestamp as an aware UTC datetime.

    ePays writes ISO 8601 with an offset, e.g. `2026-10-19T22:52:33+03:00`; a timestamp without one
    is in Bahrain time.

    :param str value: The timestamp.
    :return: The datetime in UTC, or `None` if the value is not a timestamp.
    :rtype: datetime|None
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = (parsed - const.EPAYS_TIMEZONE_OFFSET).replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def humanize_status(status):
    """ Return a v2 status as a reader expects it: ePays sends `completed` since 2026-10-18 (snake
    case), `Completed` before.

    :param str status: The `status` of the payment details.
    :return: The status, capitalised when ePays sent it in lower case.
    :rtype: str
    """
    text = str(status).strip().replace('_', ' ')
    return text[:1].upper() + text[1:] if text.islower() else text


def is_empty(value):
    """ Return whether a value carries no information. `0` and `False` are information. """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return not value
    return False


def format_value(value):
    """ Format a JSON value for display on a single line. """
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def get_gateway_response_lines(gateway_response):
    """ Return the gateway response as a list of (label, value) pairs, skipping empty values.

    :param dict gateway_response: The `gatewayResponse` object of the v2 payment details.
    :return: The humanised lines, in the order ePays sent them.
    :rtype: list[tuple[str, str]]
    """
    if not isinstance(gateway_response, dict):
        return []
    return [
        (humanize_key(key), format_value(value))
        for key, value in gateway_response.items()
        if not is_empty(value)
    ]
