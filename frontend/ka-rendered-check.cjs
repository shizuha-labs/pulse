const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();

  const consoleMessages = [];
  page.on('console', msg => consoleMessages.push(`${msg.type()}: ${msg.text()}`));
  page.on('pageerror', err => consoleMessages.push(`PAGEERROR: ${err.message}`));

  page.setDefaultTimeout(15000);
  page.setDefaultNavigationTimeout(30000);

  try {
    console.log('[1] Navigating to AT-60...');
    await page.goto('http://shizuha-nginx/pulse/tasks/AT-60');
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(1500);

    // Detect login by URL or by presence of "Sign in"
    const url = page.url();
    console.log('URL after nav:', url);
    if (url.includes('/login') || url.includes('/auth') || (await page.$('text=/Sign in to continue/i'))) {
      console.log('[2] Login screen — filling credentials...');
      await page.fill('input[placeholder*="username" i], input[placeholder*="email" i]', 'hritik');
      await page.fill('input[placeholder*="password" i], input[type="password"]', 'hritik');
      await page.click('button:has-text("Sign in")');
      await page.waitForLoadState('networkidle', { timeout: 20000 }).catch(() => {});
      await page.waitForTimeout(2500);
      console.log('URL after login:', page.url());
    }

    await page.screenshot({ path: '/tmp/ka-step1-task.png', fullPage: false });
    console.log('[3] Task page loaded, saved /tmp/ka-step1-task.png');

    // Step 2: Click map/workflow icon
    console.log('[4] Looking for workflow/map icon...');
    let mapBtn = await page.$('button[title*="workflow" i], button[title*="Workflow" i]');
    if (!mapBtn) mapBtn = await page.$('button[aria-label*="workflow" i]');
    if (!mapBtn) mapBtn = await page.$('button:has(svg.lucide-workflow)');
    if (!mapBtn) mapBtn = await page.$('button:has(svg.lucide-map)');
    if (!mapBtn) mapBtn = await page.$('button:has(svg.lucide-git-branch)');
    if (!mapBtn) mapBtn = await page.$('button:has(svg.lucide-network)');

    if (mapBtn) {
      const title = await mapBtn.getAttribute('title');
      console.log(`Found map button (title="${title}"), clicking...`);
      await mapBtn.click();
    } else {
      // Dump all buttons with titles to help locate
      const btns = await page.$$('button[title]');
      console.log(`No direct map button. Scanning ${btns.length} titled buttons...`);
      for (const b of btns) {
        const t = await b.getAttribute('title');
        console.log(`  title="${t}"`);
        if (t && /workflow|map|flow|diagram/i.test(t)) {
          console.log(`  Clicking ${t}`);
          await b.click();
          mapBtn = b;
          break;
        }
      }
    }
    await page.waitForTimeout(2000);
    await page.screenshot({ path: '/tmp/ka-step2-afterworkflow.png', fullPage: false });

    // Step 3: Click the Channel Message Review KA card
    console.log('[5] Looking for Channel Message Review Overview KA card...');
    // Find the KA card specifically (text "Channel Message Review Overview" is unique to the KA card)
    let kaCard = await page.$('text="Channel Message Review Overview"');
    if (kaCard) {
      console.log('Found KA card (Overview), clicking parent card...');
      // Click the card container (parent), not just the text
      await page.locator('text="Channel Message Review Overview"').first().click();
      await page.waitForTimeout(2500);
    } else {
      console.log('KA card not visible; dumping visible text:');
      const txt = await page.evaluate(() => document.body.innerText.substring(0, 3000));
      console.log(txt);
    }

    // Step 4: Screenshot the rendered modal
    await page.screenshot({ path: '/tmp/ka-rendered-markdown.png', fullPage: false });
    console.log('[6] Saved /tmp/ka-rendered-markdown.png');

    // Inspect DOM
    const domInfo = await page.evaluate(() => {
      const proseEls = document.querySelectorAll('[class*="prose"]');
      const results = {
        proseCount: proseEls.length,
        h1s: [],
        h2s: [],
        uls: [],
        strongs: [],
        hashLiteralFound: false,
        dashLiteralFound: false,
        sampleText: '',
      };
      for (const p of proseEls) {
        p.querySelectorAll('h1').forEach(h => results.h1s.push({
          text: h.innerText.substring(0, 120),
          fontSize: getComputedStyle(h).fontSize,
          fontWeight: getComputedStyle(h).fontWeight,
        }));
        p.querySelectorAll('h2').forEach(h => results.h2s.push({
          text: h.innerText.substring(0, 120),
          fontSize: getComputedStyle(h).fontSize,
          fontWeight: getComputedStyle(h).fontWeight,
        }));
        p.querySelectorAll('ul').forEach(u => results.uls.push({
          itemCount: u.querySelectorAll('li').length,
          listStyle: getComputedStyle(u).listStyleType,
          paddingLeft: getComputedStyle(u).paddingLeft,
        }));
        p.querySelectorAll('strong').forEach(s => results.strongs.push({
          text: s.innerText.substring(0, 80),
          fontWeight: getComputedStyle(s).fontWeight,
        }));
        const txt = p.innerText;
        if (/^#{1,3}\s/m.test(txt)) results.hashLiteralFound = true;
        if (/^-\s/m.test(txt)) results.dashLiteralFound = true;
        if (!results.sampleText) results.sampleText = txt.substring(0, 500);
      }
      return results;
    });

    console.log('\n=== DOM INSPECTION ===');
    console.log(JSON.stringify(domInfo, null, 2));
    console.log('\n=== CONSOLE LOG ===');
    consoleMessages.forEach(m => console.log(m));

  } catch (err) {
    console.log('ERROR:', err.message);
    await page.screenshot({ path: '/tmp/ka-error.png', fullPage: false });
    console.log('\n=== CONSOLE LOG (on error) ===');
    consoleMessages.forEach(m => console.log(m));
  } finally {
    await browser.close();
  }
})();
