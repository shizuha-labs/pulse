import { chromium } from 'playwright';
const browser = await chromium.launch({ args: ['--ignore-certificate-errors', '--no-sandbox'] });
const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1400, height: 900 } });
const page = await ctx.newPage();
page.on('console', m => {
  const t = m.text().slice(0, 200);
  if (/bridge|Connect|agent→conv|send_message/.test(t)) console.log('[BR]', t);
});
await page.goto('http://172.18.0.1:8016', { waitUntil: 'domcontentloaded', timeout: 30000 });
await page.waitForTimeout(1500);
await page.locator('input[type="text"]').first().fill('shizuha');
await page.locator('input[type="password"]').fill('testpw-verify-1234');
await page.locator('button:has-text("Sign in")').click();
await page.waitForTimeout(4000);
await page.locator('button:has-text("Shizuha"), text="Shizuha"').first().click({ timeout: 3000 }).catch(()=>{});
await page.waitForTimeout(3500);
// Send a fresh ping
const input = page.locator('textarea, [contenteditable="true"], input[placeholder*="Message"]').first();
await input.fill('ping from verify-2');
await page.waitForTimeout(400);
await input.press('Enter');
console.log('Sent ping from verify-2');
// Wait for Shizuha to reply (up to 60s)
await page.waitForTimeout(45000);
await page.screenshot({ path: '/app/final-chat.png', fullPage: false });
console.log('Screenshot saved. Chat area text:');
const main = await page.locator('main, [role="main"], body').first().innerText().catch(() => '');
const relevant = main.split('\n').filter(l => l.length > 0 && l.length < 300).slice(-30);
console.log(relevant.join('\n'));
await browser.close();
