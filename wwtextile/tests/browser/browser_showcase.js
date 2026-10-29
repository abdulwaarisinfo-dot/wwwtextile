// Browser checks on the rich showcase data. Needs a running demo: python deploy/run_demo.py --local --reset
// Usage: WW_DEMO_PASSWORD=... node tests/browser/browser_showcase.js [baseUrl]
const { chromium } = require('/home/claude/.npm-global/lib/node_modules/playwright');
const BASE = process.argv[2] || 'http://127.0.0.1:8000';
const PW = process.env.WW_DEMO_PASSWORD;
const OUT = process.env.SHOT_DIR || '/tmp';
const results = [];
const check = (n, ok, x = '') => { results.push(ok); console.log((ok ? 'PASS ' : 'FAIL ') + n + (x ? ' — ' + x : '')); };
const problems = [];
const attach = p => { p.on('console', m => { if (['error', 'warning'].includes(m.type())) problems.push(m.text()); }); p.on('pageerror', e => problems.push('pageerror: ' + e.message)); };
async function login(page, u) { await page.goto(BASE + '/login'); await page.fill('#username', u); await page.fill('#password', PW); await Promise.all([page.waitForURL(BASE + '/'), page.click('button[type=submit]')]); }
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 } });
  const p = await ctx.newPage(); attach(p);
  await login(p, 'ceo');
  check('3 SVG charts on the CEO dashboard', (await p.locator('.charts svg').count()) === 3);
  check('charts show real bars', (await p.locator('.charts svg rect').count()) > 10);
  check('demo banner says fictional', (await p.locator('.banner').innerText()).toLowerCase().includes('fictional'));
  await p.screenshot({ path: OUT + '/showcase-dashboard.png', fullPage: true });
  const metricVals = await p.locator('.metric .value').allTextContents();
  check('dashboard metrics are populated (not all zero)', metricVals.some(v => v.trim() && v.trim() !== '0'), metricVals.slice(0, 8).join(' | '));
  // search and filter on showcase data
  await p.goto(BASE + '/search?q=Zarbaft');
  check('search finds supplier-linked records', (await p.locator('table a').count()) >= 2);
  await p.goto(BASE + '/fabrics?kind=finished_store'); check('fabric filter by location works', (await p.locator('table tbody tr').count()) >= 3);
  await p.goto(BASE + '/processing?status=open'); check('processing filter works', (await p.locator('table tbody tr').count()) >= 1);
  await p.goto(BASE + '/watchtower'); check('watchtower has live alerts', (await p.locator('table tbody tr, .alert-row').count()) >= 3);
  await p.goto(BASE + '/reports/loss-reconciliation?run=1').catch(() => {});
  await p.goto(BASE + '/demo'); check('demo guide lists integrations as Not connected', (await p.content()).includes('Not connected'));
  await p.screenshot({ path: OUT + '/showcase-demo-guide.png', fullPage: true });
  // assistant on showcase data
  await p.goto(BASE + '/assistant');
  await p.fill('#a-text', 'what is on hand in the finished store');
  await p.click('#a-send'); await p.waitForTimeout(1200);
  const t = await p.locator('#log').innerText();
  check('assistant answers from live data', /m\b/.test(t) && t.length > 80, t.slice(0, 120).replace(/\n/g, ' '));
  // mobile
  const m = await (await b.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })).newPage(); attach(m);
  await login(m, 'ceo');
  for (const path of ['/', '/demo', '/fabrics', '/finance', '/reports', '/watchtower', '/gate/entries', '/multihead', '/accessories', '/processing', '/dispatch']) {
    await m.goto(BASE + path);
    const over = await m.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    check('mobile no horizontal overflow ' + path, over <= 1, 'overflow=' + over);
  }
  await m.goto(BASE + '/'); await m.screenshot({ path: OUT + '/showcase-dashboard-mobile.png', fullPage: true });
  const csp = problems.filter(x => /Content Security Policy|Refused/i.test(x));
  check('no CSP violations', csp.length === 0, csp.slice(0, 2).join(' ; ').slice(0, 300));
  const other = problems.filter(x => !/Content Security Policy|Refused|40[0-9]|422|409/.test(x));
  check('no other console errors', other.length === 0, other.join(' ; ').slice(0, 300));
  await b.close();
  const f = results.filter(x => !x).length;
  console.log(`\n${results.length - f}/${results.length} browser checks passed`); process.exit(f ? 1 : 0);
})().catch(e => { console.error('SCRIPT ERROR', e); process.exit(2); });
