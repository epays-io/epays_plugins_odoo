# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

import logging
import pprint
from urllib.parse import urlsplit

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from odoo.addons.payment.controllers.post_processing import PaymentPostProcessing
from odoo.addons.payment_epays import const

_logger = logging.getLogger(__name__)


class EpaysController(http.Controller):
    _return_url = '/payment/epays/return'

    @http.route(
        [_return_url, f'{_return_url}/<string:environment>'], type='http', auth='public',
        methods=['GET', 'POST'], csrf=False, save_session=False,
    )
    def epays_return_from_checkout(self, environment=None, **data):
        """ Process the notification data sent by ePays after redirection, or by its server ping.

        ePays redirects the customer to this route with `?paymentId=` and also sends one server GET
        to it. The data only identifies the payment: its status is always fetched from ePays.

        The route is flagged with `save_session=False` to prevent Odoo from assigning a new session
        to the user if they are redirected to this route with a POST request. Indeed, as the session
        cookie is created without a `SameSite` attribute, some browsers that don't implement the
        recommended default `SameSite=Lax` behavior will not include the cookie in the redirection
        request from the payment provider to Odoo. As the redirection to the '/payment/status' page
        will satisfy any specification of the `SameSite` attribute, the session of the user will be
        retrieved and with it the transaction which will be immediately post-processed.

        :param str environment: The ePays environment of the payment, named in the return URL
                                since a payment id is only unique within one environment.
        :param dict data: The notification data (only `paymentId`).
        :return: The redirection to the payment status page.
        """
        _logger.info("handling redirection from ePays with data:\n%s", pprint.pformat(data))
        if environment:
            data[const.NOTIFICATION_ENVIRONMENT_KEY] = environment
        try:
            with request.env.cr.savepoint():
                request.env['payment.transaction'].sudo()._handle_notification_data('epays', data)
        except ValidationError as error:
            # An expected, handled case - a notification for a payment this database does not hold,
            # or ePays briefly unreachable - so one line, not a traceback. The status page is shown
            # anyway and the scheduled action retries any payment Odoo does hold.
            _logger.warning(
                "unable to handle the notification data (%s); redirecting to the status page",
                error.args[0] if error.args else error,
            )
        return request.redirect('/payment/status')


class EpaysPostProcessing(PaymentPostProcessing):

    @http.route()
    def poll_status(self, **kwargs):
        """ Override of `payment` to send a shop customer whose ePays payment failed or was
        cancelled back to the payment step instead of the order confirmation.

        The shop's own landing route empties the cart and shows the unpaid order as if it were
        confirmed, with the reason in small print; from the payment step the customer sees why and
        pays again with one click.
        """
        values = super().poll_status(**kwargs)
        is_shop_failure = (
            values.get('provider_code') == 'epays'
            and values.get('state') in ('error', 'cancel')
            and urlsplit(values.get('landing_route') or '').path == const.SHOP_LANDING_ROUTE
        )
        if is_shop_failure:
            values['landing_route'] = f'{const.SHOP_PAYMENT_ROUTE}?{const.FAILURE_QUERY_PARAM}=1'
        return values

    @http.route('/payment/epays/failure', type='json', auth='public')
    def epays_failure(self):
        """ Return why the customer's last ePays payment did not complete, for the payment step.

        Only the transaction monitored in the customer's own session is read.

        :return: The state (`error` or `cancel`) and the message, or an empty dict.
        :rtype: dict
        """
        tx = self._get_monitored_transaction()
        if tx.provider_code != 'epays' or tx.state not in ('error', 'cancel'):
            return {}
        return {'state': tx.state, 'message': tx.state_message or ''}
