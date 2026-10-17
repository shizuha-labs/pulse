const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ args: ['--ignore-certificate-errors'] });
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await ctx.newPage();

  await page.goto('https://172.18.0.1:8015/', { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => localStorage.clear());
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);
  await page.fill('input[type="text"]', 'shizuha');
  await page.fill('input[type="password"]', 'Dbz123!!');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(6000);

  const jwt = await page.evaluate(async () => {
    const r = await fetch('/v1/connect-jwt', { method: 'POST', credentials: 'same-origin' });
    return (await r.json()).access;
  });

  // Send history requests for Kai's conv. Conv ID is known from earlier tests.
  const results = await page.evaluate(async (jwt) => {
    const convId = 'acd2f64a-462e-45bb-aab1-4b9d3293ff38';
    const cases = [
      { name: 'before=2000 limit=5', body: { type: 'history', conversation_id: convId, before_event_id: 2000, limit: 5 } },
      { name: 'before=null limit=3', body: { type: 'history', conversation_id: convId, limit: 3 } },
      { name: 'nonexistent conv',     body: { type: 'history', conversation_id: '00000000-0000-0000-0000-000000000000', before_event_id: 2000, limit: 5 } },
      { name: 'limit=1000 (capped)', body: { type: 'history', conversation_id: convId, before_event_id: 3000, limit: 1000 } },
    ];
    const out = [];
    for (const c of cases) {
      const r = await new Promise((resolve) => {
        const ws = new WebSocket(`wss://${location.host}/connect/ws/connect/user/?token=${jwt}`);
        let history = null;
        const t = setTimeout(() => { ws.close(); resolve({ name: c.name, history, timeout: true }); }, 6000);
        ws.onopen = () => {
          // Wait briefly for the initial sync/missed_messages to drain, then send history.
          setTimeout(() => ws.send(JSON.stringify(c.body)), 1500);
        };
        ws.onmessage = (evt) => {
          try {
            const d = JSON.parse(evt.data);
            if (d.type === 'history' && d.conversation_id === c.body.conversation_id) {
              history = { count: d.messages?.length || 0, first: d.messages?.[0]?.event_id, last: d.messages?.[d.messages.length - 1]?.event_id };
              clearTimeout(t);
              ws.close();
            }
          } catch {}
        };
        ws.onclose = () => resolve({ name: c.name, history });
      });
      out.push(r);
    }
    return out;
  }, jwt);

  console.log('\n=== K5 history results ===');
  for (const r of results) {
    if (r.history === null) {
      console.log(`  ${r.name.padEnd(26)} → no history frame${r.timeout ? ' (timeout)' : ' (closed silently, expected for non-participant)'}`);
    } else {
      console.log(`  ${r.name.padEnd(26)} → count=${r.history.count} event_ids: ${r.history.first}..${r.history.last}`);
    }
  }
  await browser.close();
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
