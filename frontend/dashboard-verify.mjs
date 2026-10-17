// Full live verification of the dashboard chat flow after my fixes.
// 1. Login
// 2. Wait for bridge to connect + refreshConversations to run
// 3. Inspect the bridge's console logs for diagnostic info
// 4. Click Shizuha, type "status", click send
// 5. Observe:
//    - No tool-call cards in the chat tab
//    - Eventually Shizuha's DM reply appears
// 6. Screenshot
import { chromium } from 'playwright';

const DASHBOARD = 'http://172.18.0.1:8016';
const USER = 'shizuha';
const PW = 'testpw-verify-1234';

const browser = await chromium.launch({ args: ['--ignore-certificate-errors', '--no-sandbox'] });
const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1400, height: 900 } });
const page = await ctx.newPage();

// Capture bridge-related logs
const bridgeLogs = [];
const allLogs = [];
page.on('console', m => {
  const text = m.text().slice(0, 300);
  const entry = `[${m.type()[0]}] ${text}`;
  allLogs.push(entry);
  if (/connect-bridge|bridge|Connect|sendChatMessage|agentToConv|refreshConv|send_message/.test(text)) {
    bridgeLogs.push(entry);
    console.log('[BRIDGE]', text);
  }
});
page.on('pageerror', e => console.log('[PAGEERROR]', e.message.slice(0, 300)));

// Capture network activity related to bridge
const netEvents = [];
page.on('request', r => {
  if (/connect|agents|bridge|send/i.test(r.url())) {
    netEvents.push(`REQ ${r.method()} ${r.url().replace('http://172.18.0.1:8016', '')}`);
  }
});
page.on('response', r => {
  if (/connect|agents|bridge|send/i.test(r.url())) {
    netEvents.push(`RES ${r.status()} ${r.url().replace('http://172.18.0.1:8016', '')}`);
  }
});

await page.goto(DASHBOARD, { waitUntil: 'domcontentloaded', timeout: 30000 });
await page.waitForTimeout(1500);

// Login
await page.locator('input[type="text"]').first().fill(USER);
await page.locator('input[type="password"]').fill(PW);
await page.locator('button:has-text("Sign in")').click();
await page.waitForTimeout(4000);
console.log('AFTER LOGIN URL:', page.url());
await page.screenshot({ path: '/app/check-1-loggedin.png' });

// Find Shizuha in the agent list. Try a few selectors.
let clicked = false;
for (const sel of [
  'button:has-text("Shizuha")', 'a:has-text("Shizuha")',
  '[data-testid="agent-shizuha"]',
  'text="Shizuha"',
]) {
  if (await page.locator(sel).first().click({ timeout: 3000 }).then(() => true).catch(() => false)) {
    clicked = true;
    console.log('Clicked via:', sel);
    break;
  }
}
console.log('Clicked Shizuha:', clicked);
await page.waitForTimeout(2500);
await page.screenshot({ path: '/app/check-2-on-shizuha.png' });

// Snapshot: agentToConv state via window.connectBridge if exposed, or via a heuristic
// Extract the bridge's state through a deliberate probe
const probe = await page.evaluate(async () => {
  const agentsResp = await fetch('/v1/agents', { credentials: 'same-origin' });
  const agents = await agentsResp.json();
  const jwtResp = await fetch('/v1/connect-jwt', { method: 'POST', credentials: 'same-origin' });
  if (!jwtResp.ok) return { error: `/v1/connect-jwt → ${jwtResp.status}` };
  const jwt = (await jwtResp.json()).access;
  const convsResp = await fetch('/connect/api/conversations/', { headers: { Authorization: `Bearer ${jwt}` } });
  const convsData = await convsResp.json();
  const convs = Array.isArray(convsData) ? convsData : (convsData.results ?? []);
  const shizuha = (agents.agents ?? []).find(a => a.username === 'shizuha');
  const shizuhaConv = convs.find(c => c.conversation_type === 'direct' && c.participants?.some(p => Number(p.user_id) === shizuha?.platformUserId));
  return {
    agentsCount: (agents.agents ?? []).length,
    shizuhaUserId: shizuha?.platformUserId,
    convsCount: convs.length,
    shizuhaConvId: shizuhaConv?.id,
    shizuhaConvParticipants: shizuhaConv?.participants?.map(p => ({ user_id: p.user_id, participant_type: p.participant_type, agent_id: p.agent_id })),
  };
});
console.log('PROBE:', JSON.stringify(probe, null, 2));

// Type "status" and send
const input = page.locator('textarea, [contenteditable="true"], input[placeholder*="Message"]').first();
await input.fill('status');
await page.waitForTimeout(500);
await page.screenshot({ path: '/app/check-3-typed.png' });

// Send
let sent = false;
for (const sel of ['button:has-text("Send")', '[aria-label="Send"]', 'button[type="submit"]']) {
  if (await page.locator(sel).first().click({ timeout: 2000 }).then(() => true).catch(() => false)) { sent = true; break; }
}
if (!sent) await input.press('Enter');
console.log('Sent:', sent ? 'via button' : 'via Enter');
await page.waitForTimeout(3000);
await page.screenshot({ path: '/app/check-4-aftersend.png' });

// Wait for agent reply (up to 60s)
console.log('Waiting for agent reply...');
await page.waitForTimeout(30000);
await page.screenshot({ path: '/app/check-5-maybe-reply.png', fullPage: true });

// Save the logs
const fs = await import('node:fs');
fs.writeFileSync('/app/bridge-logs.txt', bridgeLogs.join('\n'));
fs.writeFileSync('/app/all-logs.txt', allLogs.join('\n'));
fs.writeFileSync('/app/network.txt', netEvents.join('\n'));

await browser.close();
console.log('DONE — screenshots at /app/check-*.png, logs at /app/*-logs.txt');
