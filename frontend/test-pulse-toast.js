const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  
  // Login first
  await page.goto('http://shizuha-nginx/id/login');
  await page.fill('input[name="email"], input[type="email"]', 'admin@shizuha.com');
  await page.fill('input[name="password"], input[type="password"]', 'admin');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(2000);
  
  // Navigate to task 193
  await page.goto('http://shizuha-nginx/pulse/tasks/193');
  await page.waitForTimeout(3000);
  
  // Screenshot the page
  await page.screenshot({ path: '/tmp/pulse-task-193.png', fullPage: false });
  console.log('Screenshot saved');
  
  // Try changing the status dropdown to 'pending'
  const statusSelect = await page.$('select.input');
  if (statusSelect) {
    const currentVal = await statusSelect.inputValue();
    console.log('Current status value:', currentVal);
    
    // Try changing to pending
    await statusSelect.selectOption('pending');
    await page.waitForTimeout(3000);
    
    // Screenshot after the change attempt
    await page.screenshot({ path: '/tmp/pulse-task-193-after.png', fullPage: false });
    console.log('After screenshot saved');
  } else {
    console.log('Status select not found');
  }
  
  await browser.close();
})();
