// The customer goes back from the ePays page and clicks Pay again: one payable link per order.
import { test, expect } from '../support/fixtures';

const paymentPage = (url: string) => url.split('?')[0];

test('S1 back from the ePays page, then Pay again with nothing changed: the same page', async ({ shop, page }) => {
  const orderId = await shop.cartOnPaymentStep();
  expect((await shop.clickPay()).to).toBe('epays');
  const first = paymentPage(page.url());

  await page.goBack();
  await page.waitForURL(/\/shop\/payment/);
  expect((await shop.clickPay()).to).toBe('epays');

  expect(paymentPage(page.url()), 'the same ePays payment').toBe(first);
  const { txs } = await shop.orderState(orderId);
  // The new attempt took the payment over; no second payment was created.
  expect(txs.map(tx => [tx.state, tx.provider_reference])).toEqual([
    ['cancel', txs[0].provider_reference], ['draft', txs[0].provider_reference],
  ]);
});

test('S5 back from the ePays page, change the cart, then Pay again: the old link is voided', async ({ shop, page }) => {
  const orderId = await shop.cartOnPaymentStep();
  expect((await shop.clickPay()).to).toBe('epays');
  const first = paymentPage(page.url());
  const since = new Date().toISOString();

  await page.goBack();
  await page.waitForURL(/\/shop\/payment/);
  await shop.addToCart();
  await shop.openPaymentStep();
  expect((await shop.clickPay()).to).toBe('epays');

  expect(paymentPage(page.url()), 'a new ePays payment').not.toBe(first);
  const { txs } = await shop.orderState(orderId);
  expect(txs.map(tx => tx.state)).toEqual(['cancel', 'draft']);
  expect(txs[0].provider_reference).not.toBe(txs[1].provider_reference);
  expect(shop.logSince(since), 'the old link voided on ePays').toMatch(new RegExp(`DELETE request to \\S+/api/v2/Payments/${txs[0].provider_reference}\\b`));
});
