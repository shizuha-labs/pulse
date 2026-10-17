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
  console.log('URL:', url);
  if (!url.includes('/pulse/')) {
    console.log('Login failed');
    await page.screenshot({ path: '/tmp/prod-fail.png' });
    await browser.close();
    return;
  }
  
  await page.screenshot({ path: '/tmp/prod-task-193.png', fullPage: false });
  
  // Try an invalid transition to trigger error toast
  const selects = await page.$$('select');
  console.log('Selects found:', selects.length);
  if (selects.length > 0) {
    const val = await selects[0].inputValue();
    console.log('Status:', val);
    // Try blocked (invalid for simple workflow)
    await selects[0].selectOption('blocked');
    await page.waitForTimeout(4000);
    await page.screenshot({ path: '/tmp/prod-toast-error.png', fullPage: false });
    console.log('Error toast screenshot saved');
  }
  
  await browser.close();
})();
