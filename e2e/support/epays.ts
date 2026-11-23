// The ePays payment page: paying with an MPGS test card through its 3-D Secure emulator.
// Adapted from the ePays API's own checkout suite (`epays_api_dotnet/e2e/support/checkout.ts`).
import { expect, test, type Frame, type Page } from '@playwright/test';

export interface Card { number: string; month: string; year: string; cvv?: string }

// MPGS test cards: expiry 01/39 approves, 05/39 declines. Its test merchant blocks a card that pays
// too often in a short time, so the tests take turns, starting from a different card each minute.
const CARDS = ['5123450000000008', '4508750015741019', '2223000000000007', '4012000033330026'];
let turn = Math.floor(Date.now() / 60_000);
export const approvedCard = (): Card => ({ number: CARDS[turn++ % CARDS.length], month: '01', year: '39' });
export const declinedCard = (): Card => ({ number: CARDS[turn++ % CARDS.length], month: '05', year: '39' });

/**
 * Pay on the ePays page with a card, through 3-D Secure, until the browser is back on the shop.
 * Returns the shop URL it came back to. Skips the test when MPGS's risk rules blocked the card.
 */
export async function payByCard(page: Page, shopURL: string, card: Card): Promise<string> {
  // The card method selected and MPGS's card fields loaded.
  await page.locator('#HOSTED_SESSION-btn').click();
  await page.waitForFunction(() => document.getElementById('#card-number'), null, { timeout: 45_000 });
  await expect(page.locator('#payButton')).toBeEnabled({ timeout: 45_000 });
  await fillCard(page, card);
  await page.locator('#payButton').click();

  const deadline = Date.now() + 150_000;
  while (Date.now() < deadline) {
    await page.waitForTimeout(1500);
    const url = page.url();
    // Odoo's status page polls the payment, then moves on.
    if (url.startsWith(shopURL) && !/\/payment\/status/.test(url)) {
      // ePays reports the block as a decline, which the shop shows the customer.
      if (/\/shop\/payment/.test(url)) skipIfRiskBlocked(await page.locator('body').innerText().catch(() => ''));
      return url;
    }
    skipIfRiskBlocked(await page.locator('body').innerText().catch(() => ''));
    for (const frame of page.frames()) await answerAcsEmulator(frame, 'Y');
  }
  throw new Error(`not back on the shop: ${page.url()}`);
}

/** MPGS's risk rules blocked the card: a limit of its shared test merchant, not a shop failure. */
function skipIfRiskBlocked(text: string): void {
  test.skip(/security reasons|blocked by risk|لأسباب أمنية/i.test(text),
    'MPGS test merchant risk rules blocked the card (too many recent payments): rerun later');
}

/**
 * Type a card into MPGS's hosted fields. Each field is MPGS's own iframe, with hidden autofill inputs
 * (.sr-only) beside the real one and, in Firefox, focus traps (hs-…). MPGS reformats as it goes and
 * now and then drops a keystroke: the field is read back and typed again.
 */
async function fillCard(page: Page, card: Card): Promise<void> {
  const fields: Array<[string, string]> = [
    ['#card-number', card.number], ['#expiry-month', card.month], ['#expiry-year', card.year],
    ['#security-code', card.cvv ?? '100'], ['#cardholder-name', 'E2E Test'],
  ];
  for (const [frameId, value] of fields) {
    const input = page.frameLocator(`iframe[id="${frameId}"]`).locator('input:not(.sr-only):not([id^="hs-"])');
    for (let attempt = 1; ; attempt++) {
      await input.focus();
      await input.fill('');
      await input.pressSequentially(value, { delay: 20 });
      const typed = await input.inputValue();
      const matches = frameId === '#cardholder-name' ? typed === value : typed.replace(/\D/g, '') === value;
      if (matches || attempt === 3) break;
    }
  }
}

/** MPGS's "ACS Emulator for 3DS V2": choose the authentication result and submit. */
async function answerAcsEmulator(frame: Frame, result: 'Y' | 'N'): Promise<void> {
  const submit = frame.locator('input[type=submit]').first();
  if (!(await submit.count().catch(() => 0))) return;
  const select = frame.locator('select').first();
  if (await select.count().catch(() => 0)) {
    const value = await select.evaluate((s: HTMLSelectElement, r) =>
      [...s.options].find(o => o.text.trim().startsWith(`(${r})`))?.value, result);
    if (value) await select.selectOption(value);
  }
  await submit.click().catch(() => {});
}
