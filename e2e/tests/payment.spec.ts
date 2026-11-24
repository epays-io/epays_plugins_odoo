// Paying an order with a card on the ePays page, and a decline followed by a retry.
import { test, expect } from '../support/fixtures';
import { approvedCard, declinedCard, payByCard } from '../support/epays';

test('@card an approved card payment confirms the order', async ({ shop, page, baseURL }) => {
  const orderId = await shop.cartOnPaymentStep();
  expect((await shop.clickPay()).to).toBe('epays');

  expect(await payByCard(page, baseURL!, approvedCard())).toMatch(/\/shop\/confirmation/);
  const { order, txs } = await shop.orderState(orderId);
  expect(order.state).toBe('sale');
  expect(txs.map(tx => tx.state)).toEqual(['done']);
});

test('@card S4 a declined card, then a retry on the same order', async ({ shop, page, baseURL }) => {
  const orderId = await shop.cartOnPaymentStep();
  expect((await shop.clickPay()).to).toBe('epays');

  // Back on the payment step, with the reason. The addon's script drops `epays_failed=1` from the
  // URL once it has shown it.
  expect(await payByCard(page, baseURL!, declinedCard())).toMatch(/\/shop\/payment/);
  await expect(page.locator('.alert:has-text("Payment not completed")')).toContainText(/declined/i);

  expect((await shop.clickPay()).to, 'the retry reaches the ePays page').toBe('epays');
  expect(await payByCard(page, baseURL!, approvedCard())).toMatch(/\/shop\/confirmation/);
  const { order, txs } = await shop.orderState(orderId);
  expect(order.state).toBe('sale');
  // The retry voided the declined link first: that attempt ends replaced, not declined.
  expect(txs.map(tx => tx.state)).toEqual(['cancel', 'done']);
});
