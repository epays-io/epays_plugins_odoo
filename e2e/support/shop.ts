// The Odoo shop as the tests drive it: the admin's cart, the payment step, and what Odoo recorded.
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { expect, type Page, type TestInfo } from '@playwright/test';

const STATE_DIR = path.resolve(__dirname, '..', '.state');
/** The demo product every test buys: one variant, no optional products. */
const PRODUCT_CODE = 'E-COM08';

export type PayOutcome = { to: 'epays' } | { to: 'error'; text: string };

export interface OrderState {
  order: { id: number; name: string; state: string };
  txs: Array<{ id: number; reference: string; state: string; provider_reference: string }>;
}

/** The ePays API the shops were started with (`shops.py up --api`): `local` or `sandbox`. */
export interface Api { api: 'local' | 'sandbox'; mode: string; host: string }

function readApi(): Api {
  try {
    return JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'api.json'), 'utf8'));
  } catch {
    return { api: 'local', mode: 'localhost', host: 'localhost:7124' };
  }
}

export class Shop {
  readonly key: 'current' | 'v103';
  readonly api: Api = readApi();

  constructor(readonly page: Page, readonly baseURL: string, testInfo: TestInfo) {
    this.key = testInfo.project.name === 'current' ? 'current' : 'v103';
  }

  /** The URLs of the ePays payment pages. */
  get paymentPages(): RegExp {
    return new RegExp(`https://${this.api.host.replace(/\./g, '\\.')}/Pay/`, 'i');
  }

  get container(): string { return `epays-e2e-${this.key}`; }
  private get flag(): string { return path.join(STATE_DIR, `delay_${this.key}.flag`); }

  /** Hold ePays's answers to the requests sent from now on: Odoo times out, ePays ran them. */
  holdAnswers(): void { fs.writeFileSync(this.flag, 'hold'); }
  releaseAnswers(): void { fs.rmSync(this.flag, { force: true }); }

  /** Odoo's log since `since` (an ISO date), for what the customer does not see. */
  logSince(since: string): string {
    // Odoo logs to stderr.
    const result = spawnSync('docker', ['logs', '--since', since, this.container], { encoding: 'utf8' });
    return `${result.stdout}${result.stderr}`;
  }

  isUp(): Promise<boolean> {
    return this.page.request.get(`${this.baseURL}/web/health`).then(r => r.ok()).catch(() => false);
  }

  async login(): Promise<void> {
    await this.page.goto('/web/login');
    await this.page.fill('input[name=login]', 'admin');
    await this.page.fill('input[name=password]', 'admin');
    await this.page.click('form.oe_login_form button[type=submit]');
    await this.page.waitForURL(/\/odoo|\/web/);
  }

  /** Call the ORM as the admin, from an Odoo tab: the customer's tab may be on the ePays page. */
  async rpc<T = any>(model: string, method: string, args: unknown[], kwargs: object = {}): Promise<T> {
    const onOdoo = this.page.url().startsWith(this.baseURL);
    const tab = onOdoo ? this.page : await this.page.context().newPage();
    try {
      if (!onOdoo) await tab.goto('/web/health');
      return await tab.evaluate(async ({ model, method, args, kwargs }) => {
        const response = await fetch(`/web/dataset/call_kw/${model}/${method}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ jsonrpc: '2.0', method: 'call', params: { model, method, args, kwargs } }),
        });
        const body = await response.json();
        if (body.error) throw new Error(JSON.stringify(body.error.data?.message ?? body.error));
        return body.result;
      }, { model, method, args, kwargs });
    } finally {
      if (!onOdoo) await tab.close();
    }
  }

  /** Cancel the admin's open carts, so that each test starts from an empty one. */
  async cancelOpenCarts(): Promise<void> {
    const ids = await this.rpc<number[]>('sale.order', 'search', [[['state', '=', 'draft'], ['website_id', '!=', false]]]);
    if (ids.length) await this.rpc('sale.order', 'action_cancel', [ids]);
  }

  async addToCart(quantity = 1): Promise<void> {
    const [productId] = await this.rpc<number[]>('product.product', 'search', [[['default_code', '=', PRODUCT_CODE]]], { limit: 1 });
    const result = await this.page.evaluate(async ({ productId, quantity }) => {
      const response = await fetch('/shop/cart/update_json', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ jsonrpc: '2.0', method: 'call', params: { product_id: productId, add_qty: quantity } }),
      });
      return (await response.json()).result;
    }, { productId, quantity });
    expect(result?.cart_quantity, 'the product is in the cart').toBeGreaterThan(0);
  }

  /** A new cart with one product, on the payment step. Returns the order id. */
  async cartOnPaymentStep(): Promise<number> {
    await this.page.goto('/shop');
    await this.addToCart();
    await this.openPaymentStep();
    const [order] = await this.rpc<Array<{ id: number }>>('sale.order', 'search_read',
      [[['website_id', '!=', false], ['state', '=', 'draft']]], { fields: ['id'], order: 'id desc', limit: 1 });
    return order.id;
  }

  async openPaymentStep(): Promise<void> {
    await this.page.goto('/shop/payment');
    if (!/\/shop\/payment/.test(this.page.url())) {
      // The address step: confirm the admin's own address.
      await this.page.locator('a[href*="/shop/confirm_order"], button:has-text("Confirm"), a:has-text("Confirm")').first().click();
      await this.page.waitForURL(/\/shop\/payment/);
    }
    await expect(this.epaysRadio).toBeAttached();
  }

  private get epaysRadio() {
    return this.page.locator('input[name="o_payment_radio"][data-provider-code="epays"]').first();
  }

  /** Click Pay with ePays: the browser reaches the ePays page, or Odoo shows an error. */
  async clickPay(timeout = 60_000): Promise<PayOutcome> {
    if (!(await this.epaysRadio.isChecked())) await this.epaysRadio.check({ force: true });
    await this.page.locator('button[name="o_payment_submit_button"]').click();
    const dialog = this.page.locator('.modal-dialog .modal-body').first();
    const reached = await Promise.race([
      this.page.waitForURL(this.paymentPages, { timeout }).then(() => 'epays' as const),
      dialog.waitFor({ state: 'visible', timeout }).then(() => 'error' as const),
    ]);
    if (reached === 'epays') return { to: 'epays' };
    return { to: 'error', text: await this.closeDialogs() };
  }

  /** Read and close Odoo's error dialogs: it can stack the payment form's and, for staff, the RPC error's. */
  private async closeDialogs(): Promise<string> {
    const texts: string[] = [];
    await this.page.waitForTimeout(1000); // The second dialog opens just after the first.
    for (let i = 0; i < 6; i++) {
      const open = this.page.locator('.modal-dialog:visible').last();
      if (!(await open.count())) break;
      texts.push((await open.innerText()).replace(/\s+/g, ' ').trim());
      await open.locator('.btn-close, button:has-text("Close"), button:has-text("Ok")').first().click()
        .catch(() => this.page.keyboard.press('Escape'));
      await open.waitFor({ state: 'detached', timeout: 5_000 }).catch(() => {});
    }
    return texts.join(' || ');
  }

  async orderState(orderId: number): Promise<OrderState> {
    const [order] = await this.rpc('sale.order', 'read', [[orderId], ['name', 'state']]);
    const txs = await this.rpc('payment.transaction', 'search_read', [[['sale_order_ids', 'in', [orderId]]]],
      { fields: ['reference', 'state', 'provider_reference'], order: 'id' });
    return { order, txs };
  }

  /** Run the addon's scheduled check now, as Odoo would every 15 minutes. */
  async runScheduledCheck(): Promise<void> {
    const [, cronId] = await this.rpc<[string, number]>('ir.model.data', 'check_object_reference',
      ['payment_epays', 'cron_epays_poll_pending']);
    await this.rpc('ir.cron', 'method_direct_trigger', [[cronId]]);
  }
}
