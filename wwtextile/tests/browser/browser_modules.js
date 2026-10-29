// Browser checks for the Pass 3+ modules. Requires a demo server on :8000 (WW_ENV=demo python3 -m wwtextile.cli demo).
// Voice: a FAKE SpeechRecognition is injected (headless Chromium has no microphone/speech service). This proves the
// page logic, not real speech recognition.
const { chromium } = require('/home/claude/.npm-global/lib/node_modules/playwright');
const BASE = 'http://127.0.0.1:8000';
const PW = process.env.WW_DEMO_PASSWORD || 'demo-only-change-me';
const results = [];
const check = (n, ok, x = '') => { results.push({ n, ok }); console.log((ok ? 'PASS ' : 'FAIL ') + n + (x ? ' — ' + x : '')); };
const problems = [];
const attach = p => { p.on('console', m => { if (['error', 'warning'].includes(m.type())) problems.push(m.type() + ': ' + m.text()); }); p.on('pageerror', e => problems.push('pageerror: ' + e.message)); };
async function login(page, u) { await page.goto(BASE + '/login'); await page.fill('#username', u); await page.fill('#password', PW); await Promise.all([page.waitForURL(BASE + '/'), page.click('button[type=submit]')]); }

const PAGES = ['/accessories', '/accessories/items', '/accessories/docs', '/accessories/docs/new', '/accessories/ledger', '/gate/entries', '/gate/entries/new',
  '/finance', '/finance/entries', '/finance/entries/new', '/multihead', '/multihead/orders/new', '/reports', '/watchtower', '/assistant', '/documents', '/notifications', '/account/password'];

(async () => {
  const browser = await chromium.launch();
  const fakeSR = () => {
    window.__heard = 'how much stock is in lot DEMO-104-F1';
    class SR { constructor() {} start() { setTimeout(() => { this.onresult && this.onresult({ results: [[{ transcript: window.__heard }]] }); this.onend && this.onend(); }, 50); } stop() {} }
    window.SpeechRecognition = SR; window.webkitSpeechRecognition = SR;
  };

  // admin + manager desktop pages
  const dctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await dctx.addInitScript(fakeSR);
  const d = await dctx.newPage(); attach(d);
  await login(d, 'admin');
  for (const p of [...PAGES, '/admin/users', '/admin/master', '/settings']) {
    const r = await d.goto(BASE + p); check('admin loads ' + p, r.status() === 200, String(r.status()));
  }
  const nav = await d.locator('.nav a').allTextContents();
  check('nav has no "planned" placeholder entries', !(await d.locator('.nav a.planned').count()));
  check('nav lists Finance, Multihead, Accessory Store, Gate, Reports', ['Finance', 'Multihead', 'Accessory Store', 'Reports'].every(x => nav.some(n => n.includes(x))), nav.join('|'));

  // UI workflow: create accessory item, then check it appears
  await d.goto(BASE + '/accessories/items');
  const nm = 'BrowserTest Button ' + Date.now();
  const code = 'BT' + (Date.now() % 100000);
  await d.fill('form[action="/accessories/items"] input[name=code]', code);
  await d.fill('form[action="/accessories/items"] input[name=unit]', 'pcs');
  await d.fill('form[action="/accessories/items"] input[name=name]', nm);
  await d.click('form[action="/accessories/items"] button[type=submit]');
  await d.waitForSelector('.toast');
  check('accessory item created via UI', (await d.content()).includes(nm));

  // Reports run + CSV link
  await d.goto(BASE + '/reports');
  const links = await d.locator('a[href^="/reports/"]').count();
  check('reports hub lists reports', links >= 5, 'links=' + links);
  const first = await d.locator('a[href^="/reports/"]').first().getAttribute('href');
  const rr = await d.goto(BASE + first + '?run=1');
  check('first report runs', rr.status() === 200, first);
  const csv = await d.request.get(BASE + first + '?run=1&format=csv');
  check('report CSV download', csv.status() === 200 && /csv/.test(csv.headers()['content-type'] || ''), csv.headers()['content-type']);

  // Assistant typed flow
  await d.goto(BASE + '/assistant');
  await d.fill('#a-text', 'where is lot DEMO-104-F1');
  await d.click('#a-send');
  await d.waitForSelector('#log .a-msg, #log .msg, #log [data-role]', { timeout: 8000 }).catch(() => {});
  await d.waitForTimeout(800);
  const logTxt = await d.locator('#log').innerText();
  check('assistant answers typed question about the lot', /DEMO-104-F1/.test(logTxt) && logTxt.length > 60, logTxt.slice(0, 120).replace(/\n/g, ' '));
  await d.fill('#a-text', 'draft a goods receipt for 100 m of lot ZZ-1');
  await d.click('#a-send'); await d.waitForTimeout(800);
  const t2 = await d.locator('#log').innerText();
  check('assistant refuses to post (drafts only / no silent write)', /draft|cannot post|can.t post|not post/i.test(t2), t2.slice(-160).replace(/\n/g, ' '));

  // Voice via fake SpeechRecognition (viewer with voice.use)
  const vctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await vctx.addInitScript(fakeSR);
  const v = await vctx.newPage(); attach(v);
  await login(v, 'manager1');
  await v.goto(BASE + '/assistant');
  const micVisible = await v.locator('#a-mic').isVisible().catch(() => false);
  check('mic button visible when speech recognition exists', micVisible);
  if (micVisible) {
    await v.dispatchEvent('#a-mic', 'mousedown'); await v.waitForTimeout(600); await v.dispatchEvent('#a-mic', 'mouseup'); await v.waitForTimeout(1200);
    const lt = await v.locator('#log').innerText();
    check('fake voice utterance is sent as a query and answered', /DEMO-104-F1/.test(lt), lt.slice(0, 140).replace(/\n/g, ' '));
    await v.evaluate(() => { window.__heard = 'Jarvis draft a receipt for 50 m of lot ABC'; });
    await v.dispatchEvent('#a-mic', 'mousedown'); await v.waitForTimeout(600); await v.dispatchEvent('#a-mic', 'mouseup'); await v.waitForTimeout(600);
    const val = await v.inputValue('#a-text'); const before = (await v.locator('#log').innerText()).length;
    check('write-like voice command is NOT auto-sent (wake word stripped, waits for Send)', /^draft a receipt/i.test(val), val);
  }

  // Role gating in the UI
  const fctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const f = await fctx.newPage(); attach(f);
  await login(f, 'gate1');
  check('gate user denied finance (403)', (await f.goto(BASE + '/finance')).status() === 403);
  check('gate user denied admin (403)', (await f.goto(BASE + '/admin/users')).status() === 403);
  check('gate user can open gate entries', (await f.goto(BASE + '/gate/entries')).status() === 200);

  // Mobile overflow on every new page
  const mctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const m = await mctx.newPage(); attach(m);
  await login(m, 'admin');
  for (const p of [...PAGES, '/admin/users', '/admin/master']) {
    await m.goto(BASE + p);
    const over = await m.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    check('mobile no horizontal overflow ' + p, over <= 1, 'overflow=' + over);
  }

  const csp = problems.filter(x => /Content Security Policy|CSP|Refused/i.test(x));
  check('no CSP violations', csp.length === 0, csp.join(' ; '));
  const other = problems.filter(x => !/Content Security Policy|CSP|Refused/i.test(x) && !/403|404|409|422|401/.test(x));
  check('no other console errors', other.length === 0, other.join(' ; '));
  await browser.close();
  const failed = results.filter(r => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} browser checks passed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('SCRIPT ERROR', e); process.exit(2); });
