/**
 * K4 verification for ori-expo web build — corrected login flow.
 * Ori-expo login page has: Server URL field + Email + Password.
 * We leave the default "Shizuha Platform" server selected.
 */
const { chromium } = require('playwright');
function log(m) { console.log(`[${new Date().toISOString().split('T')[1].split('.')[0]}] ${m}`); }

(async () => {
  const browser = await chromium.launch({ args: ['--ignore-certificate-errors'] });
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 390, height: 844 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.setDefaultNavigationTimeout(30000);

  const wsFrames = [];
  page.on('websocket', (ws) => {
    if (!ws.url().includes('/connect/ws/connect/user/')) return;
    log(`WS: ${ws.url().slice(0, 90)}`);
    ws.on('framesent', (frame) => {
      try {
        const d = JSON.parse(frame.payload);
        if (d.type === 'sync' || d.type === 'send_message') {
          wsFrames.push({ dir: '>', type: d.type, payload: d });
          log(`>> ${d.type}${d.since_event_id !== undefined ? ` since=${d.since_event_id}` : ''}`);
        }
      } catch {}
    });
    ws.on('framereceived', (frame) => {
      try {
        const d = JSON.parse(frame.payload);
        if (d.type === 'sync_complete' || d.type === 'missed_message' || d.type === 'new_message') {
          const eid = d?.message?.event_id;
          wsFrames.push({ dir: '<', type: d.type, event_id: eid });
          if (d.type === 'sync_complete') log(`<< sync_complete since=${d.since_event_id}`);
          else log(`<< ${d.type} event_id=${eid}`);
        }
      } catch {}
    });
  });

  page.on('console', (m) => {
    const t = m.text();
    if (/ConnectWS|sync_complete|sync sent/.test(t)) log(`page: ${t.slice(0, 160)}`);
  });

  await page.goto('http://ori-expo:19006/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);
  await page.evaluate(() => { try { localStorage.clear(); } catch {} });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);

  // Click "Shizuha Platform" radio first
  try { await page.click('text=Shizuha Platform'); await page.waitForTimeout(400); } catch {}

  // Now find all input boxes — last two should be Email + Password
  const inputs = await page.locator('input').all();
  log(`Found ${inputs.length} inputs`);
  if (inputs.length >= 2) {
    // Email is the 2nd-to-last; password is the last (has type="password" or placeholder=password)
    // Use direct placeholder lookup first
    const emailBox = await page.locator('input[placeholder*="mail" i], input[placeholder*="Email" i]').first();
    const pwBox = await page.locator('input[type="password"], input[placeholder*="password" i]').first();
    await emailBox.fill('hritik').catch(async () => { await inputs[inputs.length - 2].fill('hritik'); });
    await pwBox.fill('Dbz123!!').catch(async () => { await inputs[inputs.length - 1].fill('Dbz123!!'); });
    await page.waitForTimeout(500);
    await page.locator('text=Sign In').last().click().catch(async (e) => {
      log(`click("Sign In") failed: ${e.message}`);
    });
    await page.waitForTimeout(10000);
    log('Login attempted');
  }

  await page.waitForTimeout(5000);

  const cursor = await page.evaluate(() => localStorage.getItem('connect_cursor_v1'));
  const sentSync = wsFrames.find((f) => f.dir === '>' && f.type === 'sync');
  const gotSyncComplete = wsFrames.find((f) => f.dir === '<' && f.type === 'sync_complete');

  log('');
  log('=== K4 ori-expo ===');
  log(`cursor:        ${cursor ?? '(none)'}`);
  log(`sync frame:    ${sentSync ? JSON.stringify(sentSync.payload) : '(NONE)'}`);
  log(`sync_complete: ${gotSyncComplete ? 'yes' : '(NONE)'}`);

  await page.screenshot({ path: '/tmp/k4-oriexpo.png', fullPage: false }).catch(() => {});
  const pass = !!sentSync;
  log(pass ? '\n✅ ori-expo K4: PASS' : '\n⚠️  could not verify (login path tricky)');
  await browser.close();
  process.exit(pass ? 0 : 1);
})().catch((e) => { console.error('FATAL:', e.message); process.exit(2); });
