import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end tests of the addon in an Odoo 18 shop, against the local ePays API and MPGS's test
 * environment. Start the shops first: `python e2e/scripts/shops.py up`. See README.md.
 *
 * One test at a time: tests share the local ePays merchant, and MPGS's test merchant blocks a card
 * that pays too often.
 */
export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 300_000,
  expect: { timeout: 20_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    ...devices['Desktop Chrome'],
    // The system Chrome: Playwright's own Chromium build is not required.
    channel: 'chrome',
    // The local ePays API's development certificate.
    ignoreHTTPSErrors: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    // Odoo is reached on 127.0.0.1, not localhost: the local ePays API sends HSTS for localhost,
    // after which the browser would only open the shop over HTTPS.
    { name: 'current', use: { baseURL: 'http://127.0.0.1:8069' }, grepInvert: /@v18\.0\.1\.0\.3/ },
    { name: 'v18.0.1.0.3', use: { baseURL: 'http://127.0.0.1:8070' }, grep: /@v18\.0\.1\.0\.3/ },
  ],
});
