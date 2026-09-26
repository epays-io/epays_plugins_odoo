# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

import json
import re

from odoo.exceptions import ValidationError

from odoo.addons.payment_epays import const


class EpaysApiError(ValidationError):
    """ A failed call to the ePays API.

    The message is always generic, so that it can safely reach a customer. The details of the
    failure are kept on the exception for the admin-only diagnosis of the Test connection button.
    """

    def __init__(
        self, message, *, http_status=None, error_code=None, error_message=None, details=None,
        connection_error=None,
    ):
        super().__init__(message)
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
