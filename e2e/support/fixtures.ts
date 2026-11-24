// The `shop` fixture: a shop that is up, the admin logged in, no open cart, no answer held.
import { test as base } from '@playwright/test';
import { Shop } from './shop';

export const test = base.extend<{ shop: Shop }>({
  shop: async ({ page, baseURL }, use, testInfo) => {
    const shop = new Shop(page, baseURL!, testInfo);
    base.skip(!(await shop.isUp()), `no shop at ${baseURL}: run \`python e2e/scripts/shops.py up\` (README.md)`);
    shop.releaseAnswers();
    await shop.login();
    await shop.cancelOpenCarts();
    await use(shop);
    shop.releaseAnswers();
  },
});

export { expect } from '@playwright/test';
