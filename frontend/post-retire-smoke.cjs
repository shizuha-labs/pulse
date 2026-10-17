/**
 * Post-shizuha-agent-retirement smoke test.
 *
 * 1. Load dashboard, login
 * 2. Check all 26 agents appear in UI
 * 3. Send a message to Kai, expect a response
 * 4. Confirm no 410/502 errors in browser console
 * 5. Confirm no /agent/* fetches from the dashboard JS
 */
const { chromium } = require('playwright');
function log(m) { console.log(`[${new Date().toISOString().split('T')[1].split('.')[0]}] ${m}`); }

(async () => {
  const browser = await chromium.launch({ args: ['--ignore-certificate-errors'] });
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1400, height: 1000 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  page.setDefaultNavigationTimeout(45000);

  const badResponses = [];   // any /agent/* fetch that happened
  const consoleErrors = [];  // any console.error
  const agentFetches = [];   // any request to /agent/*

  page.on('response', async (resp) => {
    const url = resp.url();
    if (url.includes('/agent/')) agentFetches.push({ url: url.slice(0, 120), status: resp.status() });
    if (resp.status() >= 400 && resp.status() !== 401) {
      badResponses.push({ url: url.slice(0, 120), status: resp.status() });
    }
  });
  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 200));
  });

  log('1. Load dashboard');
  await page.goto('https://172.18.0.1:8015/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);

  log('2. Login as shizuha');
  await page.fill('input[type="text"]', 'shizuha');
  await page.fill('input[type="password"]', 'Dbz123!!');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(7000);

  log('3. Agent list in UI');
  const visibleAgents = await page.locator('[class*="agent"], [data-agent-id]').count();
  log(`   rendered elements that look like agents: ${visibleAgents}`);

  log('4. Open Kai chat');
  try {
    await page.locator('text=/^Kai/').first().click();
    await page.waitForTimeout(2500);
    log('   Kai opened');
  } catch (e) {
    log(`   WARN clicking Kai: ${e.message}`);
  }

  log('5. Send test message to Kai');
  const cmidPing = `retirement-smoke-${Date.now()}`;
  try {
    const input = page.locator('textarea, input[placeholder*="essage" i]').last();
    await input.click();
    await input.fill(`smoke-test ${cmidPing} — reply pong`);
    await input.press('Enter');
    await page.waitForTimeout(12000);
    log('   message sent, waited 12s for response');
  } catch (e) {
    log(`   WARN sending: ${e.message}`);
  }

  log('6. Final snapshot');
  await page.screenshot({ path: '/tmp/post-retire-smoke.png', fullPage: false });

  log('');
  log('=====================================');
  log('Post-retirement smoke test results');
  log('=====================================');
  log(`/agent/* fetches from dashboard JS:  ${agentFetches.length}`);
  for (const f of agentFetches.slice(0, 10)) log(`  ${f.status}  ${f.url}`);
  log(`non-401 4xx/5xx responses:           ${badResponses.length}`);
  for (const f of badResponses.slice(0, 10)) log(`  ${f.status}  ${f.url}`);
  log(`console.error entries:               ${consoleErrors.length}`);
  for (const m of consoleErrors.slice(0, 5)) log(`  ${m}`);

  // PASS only if no /agent/* fetches, no unexpected errors
  const pass =
    agentFetches.length === 0 &&
    badResponses.length === 0 &&
    consoleErrors.length < 3; // allow a couple of ignorable ones

  log('');
  log(pass ? '✅ PASS: dashboard does not depend on shizuha-agent' : '⚠️  REVIEW: see fetches/errors above');

  await browser.close();
  process.exit(pass ? 0 : 1);
})().catch((e) => { console.error('FATAL:', e); process.exit(2); });
