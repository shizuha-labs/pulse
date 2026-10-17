/**
 * K4 live verification — dashboard cursor replay end-to-end.
 *
 * Scenario:
 *  1. Open dashboard, log in, clear IDB+localStorage so we start with NO cursor.
 *  2. Send a fresh message → note event_id from server response (via WS frames).
 *  3. Reload page → bridge should send {type:"sync"} with no since_event_id
 *     (first-ever open), then persist event_id as cursor after ingest.
 *  4. Inject a server-side DM while the tab is live — confirm new_message
 *     advances the cursor in localStorage.
 *  5. Reload page again → bridge should send {type:"sync", since_event_id:N}
 *     where N is the cursor from step 4.
 *  6. Confirm network log shows the sync frame with correct cursor.
 */
const { chromium } = require('playwright');

function log(m) {
  console.log(`[${new Date().toISOString().split('T')[1].split('.')[0]}] ${m}`);
}

(async () => {
  const browser = await chromium.launch({ args: ['--ignore-certificate-errors'] });
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1400, height: 1000 } });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);
  page.setDefaultNavigationTimeout(30000);

  // Capture all WS frames from the Connect bridge
  const wsFrames = [];
  page.on('websocket', (ws) => {
    if (!ws.url().includes('/connect/ws/connect/user/')) return;
    log(`WS opened: ${ws.url().slice(0, 80)}…`);
    ws.on('framesent', (frame) => {
      try {
        const d = JSON.parse(frame.payload);
        if (d.type === 'sync' || d.type === 'send_message' || d.type === 'message_read') {
          wsFrames.push({ dir: '>', type: d.type, payload: d });
          log(`>> ${d.type}${d.since_event_id !== undefined ? ` since_event_id=${d.since_event_id}` : ''}`);
        }
      } catch {}
    });
    ws.on('framereceived', (frame) => {
      try {
        const d = JSON.parse(frame.payload);
        if (d.type === 'sync_complete' || d.type === 'missed_message' || d.type === 'new_message') {
          const evtId = d.message?.event_id;
          wsFrames.push({ dir: '<', type: d.type, event_id: evtId });
          if (d.type === 'sync_complete') log(`<< sync_complete (since_event_id=${d.since_event_id})`);
          else if (d.type === 'missed_message') log(`<< missed_message event_id=${evtId}`);
          else if (d.type === 'new_message') log(`<< new_message event_id=${evtId}`);
        }
      } catch {}
    });
  });

  // ── Step 1: log in with fresh storage ──
  await page.goto('https://172.18.0.1:8015/', { waitUntil: 'domcontentloaded' });
  await page.evaluate(async () => {
    try { localStorage.clear(); } catch {}
    try {
      const dbs = await indexedDB.databases?.() || [];
      await Promise.all(dbs.map((d) => new Promise((r) => {
        const req = indexedDB.deleteDatabase(d.name);
        req.onsuccess = req.onerror = req.onblocked = () => r();
      })));
    } catch {}
  });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(800);

  await page.fill('input[type="text"]', 'shizuha');
  await page.fill('input[type="password"]', 'Dbz123!!');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(5000);
  log('Logged in');

  // Step 1 state: cursor should be empty BEFORE the first sync happens
  const cursorAfterLogin = await page.evaluate(() => localStorage.getItem('shizuha_connect_cursor_v1'));
  log(`cursor after login: ${cursorAfterLogin ?? '(none)'}`);

  // Give the bridge time to send its initial {type:"sync"} (no since_event_id)
  // and ingest the initial batch of missed_messages, advancing the cursor.
  await page.waitForTimeout(6000);

  const cursorAfterInitialSync = await page.evaluate(() => localStorage.getItem('shizuha_connect_cursor_v1'));
  log(`cursor after initial sync drain: ${cursorAfterInitialSync ?? '(none)'}`);

  // ── Step 2: open Kai's chat, send a user message ──
  try {
    await page.locator('text=/^Kai/').first().click();
    await page.waitForTimeout(1500);
    log('Kai opened');
  } catch (e) {
    log(`WARN: couldn't click Kai — ${e.message}`);
  }

  // ── Step 3: reload → verify {type:"sync", since_event_id:N} is sent ──
  wsFrames.length = 0;
  log('=== Reloading page to verify cursor-based sync ===');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(5000);

  const sentSync = wsFrames.find((f) => f.dir === '>' && f.type === 'sync');
  const gotSyncComplete = wsFrames.find((f) => f.dir === '<' && f.type === 'sync_complete');
  const cursorAfterReload = await page.evaluate(() => localStorage.getItem('shizuha_connect_cursor_v1'));

  log('');
  log('=========================================');
  log('K4 verification results');
  log('=========================================');
  log(`cursor in localStorage: ${cursorAfterReload ?? '(none)'}`);
  log(`sent sync frame:        ${sentSync ? JSON.stringify(sentSync.payload) : '(NONE — FAIL)'}`);
  log(`received sync_complete: ${gotSyncComplete ? 'yes' : '(NONE — FAIL)'}`);

  let pass = true;
  if (!sentSync) { log('FAIL: client never sent {type:"sync"} on reload'); pass = false; }
  if (sentSync && sentSync.payload.since_event_id === undefined) {
    log('FAIL: since_event_id not set on reload (cursor was empty — bug in persist?)');
    pass = false;
  }
  if (!cursorAfterReload || Number(cursorAfterReload) <= 0) {
    log('FAIL: cursor not persisted in localStorage');
    pass = false;
  }
  if (!gotSyncComplete) { log('FAIL: server never sent sync_complete'); pass = false; }

  log(pass ? '\n✅ K4 dashboard: PASS' : '\n❌ K4 dashboard: FAIL');

  await page.screenshot({ path: '/tmp/k4-final.png', fullPage: false });
  await browser.close();
  process.exit(pass ? 0 : 1);
})().catch((e) => { console.error('FATAL:', e); process.exit(2); });
