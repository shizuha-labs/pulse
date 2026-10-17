const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ args: ['--ignore-certificate-errors'] });
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);

  await page.goto('https://172.18.0.1:8015/', { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => localStorage.clear());
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);
  await page.fill('input[type="text"]', 'shizuha');
  await page.fill('input[type="password"]', 'Dbz123!!');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(6000);

  // Get a JWT via /v1/connect-jwt
  const jwt = await page.evaluate(async () => {
    const r = await fetch('/v1/connect-jwt', { method: 'POST', credentials: 'same-origin' });
    return (await r.json()).access;
  });
  console.log('JWT len=', jwt.length);

  // Test malformed since_event_id values by opening raw WS inside the browser
  const results = await page.evaluate(async (jwt) => {
    const cases = [
      { name: 'negative',  value: -1 },
      { name: 'zero',      value: 0 },
      { name: 'string',    value: 'abc' },
      { name: 'huge',      value: 99999999999 },
      { name: 'null_val',  value: null },
      { name: 'array',     value: [1,2,3] },
      { name: 'missing',   skip: true },  // omit the field
    ];
    const out = [];
    for (const c of cases) {
      const r = await new Promise((resolve) => {
        const ws = new WebSocket(`wss://${location.host}/connect/ws/connect/user/?token=${jwt}`);
        let sawComplete = false;
        let sawClose = false;
        let closeCode = null;
        const t = setTimeout(() => { try { ws.close(); } catch {} resolve({ name: c.name, sawComplete, sawClose, closeCode, tookTooLong: true }); }, 5000);
        ws.onopen = () => {
          const frame = { type: 'sync' };
          if (!c.skip) frame.since_event_id = c.value;
          ws.send(JSON.stringify(frame));
        };
        ws.onmessage = (evt) => {
          try {
            const d = JSON.parse(evt.data);
            if (d.type === 'sync_complete') { sawComplete = true; clearTimeout(t); ws.close(); }
          } catch {}
        };
        ws.onclose = (evt) => { sawClose = true; closeCode = evt.code; clearTimeout(t); resolve({ name: c.name, sawComplete, sawClose, closeCode }); };
        ws.onerror = () => {};
      });
      out.push(r);
    }
    return out;
  }, jwt);

  console.log('\n=== Invalid since_event_id test results ===');
  for (const r of results) {
    const verdict = r.sawComplete ? '✅ sync_complete' : (r.closeCode ? `close=${r.closeCode}` : '❌ hung');
    console.log(`  ${r.name.padEnd(10)} → ${verdict}`);
  }
  await browser.close();
})().catch(e => { console.error('FATAL', e.message); process.exit(1); });
