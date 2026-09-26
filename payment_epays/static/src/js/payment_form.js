/** @odoo-module **/

import { _t } from '@web/core/l10n/translation';
import { rpc } from '@web/core/network/rpc';
import paymentForm from '@payment/js/payment_form';

// Set by the addon's status poll when a shop payment failed or was cancelled (see controllers).
const FAILURE_QUERY_PARAM = 'epays_failed';

/**
 * Return whether this browser can pay with Apple Pay.
 *
 * @return {boolean}
 */
function canUseApplePay() {
    try {
        return Boolean(window.ApplePaySession?.canMakePayments());
    } catch (error) {
        return false; // ApplePaySession throws outside of a secure context.
    }
}

paymentForm.include({

    /**
     * Remove the ePays Apple Pay option on browsers that cannot pay with Apple Pay, before the
     * payment form initialises its selected option, then tell a customer returning from a failed
     * ePays payment why it failed.
     *
     * @override
     */
    async start() {
        this._epaysRemoveUnsupportedOptions();
        const result = await this._super(...arguments);
        this._epaysShowFailure();
        return result;
    },

    /**
     * @private
     * @return {void}
     */
    _epaysRemoveUnsupportedOptions() {
        if (canUseApplePay()) {
            return;
        }
        const radios = this.el.querySelectorAll(
            'input[name="o_payment_radio"][data-provider-code="epays"]'
            + '[data-payment-method-code="epays_apple_pay"]'
        );
        let removedCheckedOption = false;
        for (const radio of radios) {
            removedCheckedOption = removedCheckedOption || radio.checked;
            (radio.closest('[name="o_payment_option"]') || radio).remove();
        }
        if (removedCheckedOption) {
            const firstRadio = this.el.querySelector('input[name="o_payment_radio"]');
            if (firstRadio) {
                firstRadio.checked = true;
            }
        }
    },

    /**
     * Show, above the payment form, why the customer's last ePays payment did not complete.
     *
     * The query parameter is removed first, so that reloading the page does not show it again.
     *
     * @private
     * @return {Promise<void>}
     */
    async _epaysShowFailure() {
        const url = new URL(window.location.href);
        if (!url.searchParams.has(FAILURE_QUERY_PARAM)) {
            return;
        }
        url.searchParams.delete(FAILURE_QUERY_PARAM);
        window.history.replaceState(window.history.state, '', url.toString());

        const failure = await rpc('/payment/epays/failure');
        if (!failure.state) {
            return;
        }
        const alert = document.createElement('div');
        alert.className = 'alert alert-danger d-flex gap-3 o_epays_failure';
        alert.setAttribute('role', 'alert');
        const icon = document.createElement('i');
        icon.className = 'fa fa-exclamation-triangle mt-1';
        const body = document.createElement('div');
        const heading = document.createElement('h5');
        heading.className = 'alert-heading';
        heading.textContent = failure.state === 'cancel'
            ? _t("Payment cancelled")
            : _t("Payment not completed");
        const message = document.createElement('p');
        message.className = 'mb-0';
        message.textContent = failure.message;
        body.append(heading, message);
        alert.append(icon, body);
        this.el.before(alert);
        alert.scrollIntoView({ block: 'center' });
    },

});
