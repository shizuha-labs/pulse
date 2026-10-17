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
  
  // Wait for redirect to pulse
  await page.waitForURL('**/pulse/**', { timeout: 10000 }).catch(() => {});
  await page.waitForTimeout(3000);
  
  const url = page.url();
  console.log('Current URL:', url);
  
  if (!url.includes('/pulse/')) {
    await page.screenshot({ path: '/tmp/pulse-login-fail.png' });
    console.log('Login failed');
    await browser.close();
    return;
  }
  
  await page.screenshot({ path: '/tmp/pulse-task-193.png', fullPage: false });
  console.log('Screenshot 1: task detail page');
  
  // Find status selects
  const selects = await page.$$('select');
  console.log('Found', selects.length, 'select elements');
  
  for (let i = 0; i < selects.length; i++) {
    const val = await selects[i].inputValue();
    const opts = await selects[i].$$eval('option', os => os.map(o => `${o.value}(${o.textContent})`));
    console.log(`Select ${i}: value="${val}", options=[${opts.join(', ')}]`);
  }
  
  // The first select should be the status dropdown
  if (selects.length > 0) {
    const statusSelect = selects[0];
    const currentVal = await statusSelect.inputValue();
    const target = currentVal === 'pending' ? 'completed' : 'pending';
    console.log(`Changing status: "${currentVal}" -> "${target}"`);
    
    await statusSelect.selectOption(target);
    await page.waitForTimeout(4000);
    
    await page.screenshot({ path: '/tmp/pulse-task-193-after.png', fullPage: false });
    console.log('Screenshot 2: after status change attempt');
  }
  
  await browser.close();
})();
