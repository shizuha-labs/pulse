const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  
  // Login
  await page.goto('http://shizuha-nginx/id/login?continue=/pulse/tasks/193');
  await page.waitForSelector('#username');
  await page.fill('#username', 'hritik');
  await page.fill('#password', 'test1234');
  await page.click('button:has-text("Sign in")');
  await page.waitForURL('**/pulse/**', { timeout: 10000 }).catch(() => {});
  await page.waitForTimeout(3000);
  
  const url = page.url();
  console.log('Current URL:', url);
  
  // Find status select
  const selects = await page.$$('select');
  if (selects.length > 0) {
    const statusSelect = selects[0];
    const currentVal = await statusSelect.inputValue();
    console.log('Current status:', currentVal);
    
    // Try "blocked" — there's no transition from pending to blocked in the simple workflow
    console.log('Attempting invalid transition: pending -> blocked');
    await statusSelect.selectOption('blocked');
    await page.waitForTimeout(4000);
    
    await page.screenshot({ path: '/tmp/pulse-toast-error.png', fullPage: false });
    console.log('Screenshot saved — should show error toast');
  }
  
  await browser.close();
})();
