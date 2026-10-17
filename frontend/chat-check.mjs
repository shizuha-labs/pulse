import { chromium } from 'playwright';
(async () => {
  const browser = await chromium.launch({ args: ['--ignore-certificate-errors', '--no-sandbox'] });
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1400, height: 900 } });
  const page = await ctx.newPage();
  page.on('pageerror', e => console.log('[PAGEERROR]', e.message.slice(0, 200)));

  await page.goto('http://172.18.0.1:8016', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(1000);
  // Login
  await page.locator('input[placeholder="shizuha"], input[type="text"]').first().fill('shizuha');
  await page.locator('input[type="password"]').fill('shizuha');
  await page.locator('button:has-text("Sign in")').click();
  await page.waitForTimeout(3500);
  console.log('AFTER LOGIN URL:', page.url());
  await page.screenshot({ path: '/app/dash-2-loggedin.png', fullPage: false });

  // Find and click Kai in agent list
  const kaiClicked = await page.locator('text=/^Kai$/i').first().click({ timeout: 5000 }).then(() => true).catch(() => false);
  console.log('Clicked Kai:', kaiClicked);
  await page.waitForTimeout(3500);
  await page.screenshot({ path: '/app/dash-3-kai-chat.png', fullPage: false });

  // Try to capture chat-related text
  const main = await page.locator('main, [class*="chat"], [class*="messages"]').first().innerText().catch(() => '');
  console.log('--- chat area text (first 800 chars) ---');
  console.log(main.slice(0, 800));

  await browser.close();
})().catch(e => { console.error('FATAL:', e.message); process.exit(1); });
