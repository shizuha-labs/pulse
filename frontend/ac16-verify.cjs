const { chromium } = require('@playwright/test');

const ACCESS = process.env.SHIZUHA_ACCESS_TOKEN;
const REFRESH = process.env.SHIZUHA_REFRESH_TOKEN;
const USER = process.env.SHIZUHA_USER_JSON;
if (!ACCESS || !REFRESH || !USER || !process.env.BASE_URL) {
  throw new Error('Set BASE_URL, SHIZUHA_ACCESS_TOKEN, SHIZUHA_REFRESH_TOKEN and SHIZUHA_USER_JSON');
}
JSON.parse(USER); // Reject an invalid session before opening a browser.

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  try {
    await page.goto(process.env.BASE_URL + '/id/login', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.evaluate(([a, r, u]) => {
      localStorage.setItem('shizuha_access_token', a);
      localStorage.setItem('shizuha_refresh_token', r);
      localStorage.setItem('shizuha_user', u);
    }, [ACCESS, REFRESH, USER]);

    await page.goto(process.env.BASE_URL + '/pulse/tasks/AC-16', { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(1500);
    await page.screenshot({ path: '/tmp/ac16-detail.png', fullPage: true });

    // Capture button text near the workflow bar
    const labels = await page.locator('button').allTextContents();
    const interesting = labels.filter(t => /To Do|Progress|Done|Schedule|Reopen|Start|Mark|Move/i.test(t)).slice(0, 20);
    console.log('Workflow buttons visible: ' + JSON.stringify(interesting));

    // The "No transitions available" banner — should be gone
    const noTrans = await page.locator('text=/No transitions available/i').count();
    console.log('"No transitions available" banner count: ' + noTrans);
    if (noTrans > 0) throw new Error('banner still showing');

    // Check for the 3 expected transitions
    const startProg = await page.locator('button:has-text("Start Progress")').count();
    const markDone = await page.locator('button:has-text("Mark Done")').count();
    const schedule = await page.locator('button:has-text("Schedule")').count();
    console.log(`Transition buttons: Start Progress=${startProg}, Mark Done=${markDone}, Schedule=${schedule}`);

    if (!startProg || !markDone || !schedule) throw new Error('expected 3 transition buttons missing');
    console.log('SUCCESS');
  } catch (e) {
    console.log('FAILURE: ' + e.message);
    await page.screenshot({ path: '/tmp/ac16-error.png', fullPage: true });
    process.exit(1);
  } finally {
    await browser.close();
  }
})();
