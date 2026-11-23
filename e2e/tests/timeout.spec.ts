// A payment request ePays answers too late: ePays created the payment, Odoo gave up and rolled back.
// The live failure of 2026-11-22: 18.0.1.0.3 sent the same Idempotency-Key on the next click, and
// ePays refused every click with 409 IDEMPOTENCY_CONFLICT for 24 hours.
import { test, expect } from '../support/fixtures';
import { approvedCard, payByCard } from '../support/epays';

test('@card a payment request answered too late: the next click pays, the scheduler voids the orphan', async ({ shop, page, baseURL }) => {
  const orderId = await shop.cartOnPaymentStep();
  const since = new Date().toISOString();

  shop.holdAnswers();
  const first = await shop.clickPay(90_000);
  shop.releaseAnswers();
  expect(first.to, 'the first click times out').toBe('error');
  expect(shop.logSince(since)).toMatch(/answered the payment request of transaction \S+ too late/);

  const second = await shop.clickPay();
  expect(second.to, 'the next click reaches the ePays page').toBe('epays');
  expect(shop.logSince(since)).not.toContain('HTTP 409');
  expect(await payByCard(page, baseURL!, approvedCard())).toMatch(/\/shop\/confirmation/);
  const { order, txs } = await shop.orderState(orderId);
  expect(order.state).toBe('sale');
  expect(txs.map(tx => tx.state)).toEqual(['done']);

  // The payment ePays created for the first click is voided once no click can still wait for it:
  // two minutes after the request, and after ePays's own creation date. Not on the sandbox, whose
  // payment list ignores `status` and `udf2`: the addon cannot tell its orphans there.
  if (shop.api.api === 'sandbox') {
    test.info().annotations.push({ type: 'not checked', description: 'orphan cleanup: the sandbox payment list ignores its filters' });
    return;
  }
  await page.waitForTimeout(125_000);
  await shop.runScheduledCheck();
  expect(shop.logSince(since)).toMatch(new RegExp(`voided the ePays payment \\S+ of ${order.name}: no transaction holds it`));
  expect(await shop.rpc('payment.epays.unanswered.request', 'search_count', [[]])).toBe(0);
});

test('@v18.0.1.0.3 the live failure: every click after a timed-out request gets 409', async ({ shop }) => {
  await shop.cartOnPaymentStep();
  const since = new Date().toISOString();

  shop.holdAnswers();
  expect((await shop.clickPay(90_000)).to, 'the first click times out').toBe('error');
  shop.releaseAnswers();

  for (const click of ['second', 'third']) {
    const outcome = await shop.clickPay();
    expect(outcome.to, `the ${click} click is refused`).toBe('error');
    if (outcome.to === 'error') expect(outcome.text).toContain('Could not establish the connection to the API');
  }
  expect(shop.logSince(since).match(/invalid API request at \S+\/api\/v2\/Payments \(HTTP 409\)/g)).toHaveLength(2);
});
