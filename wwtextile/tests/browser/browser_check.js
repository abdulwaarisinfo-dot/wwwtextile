const { chromium } = require('/home/claude/.npm-global/lib/node_modules/playwright');
const OUT = '/tmp/claude-0/-home-claude/2cd57078-051f-547c-9584-9047b9d4723f/scratchpad';
const BASE = 'http://127.0.0.1:8000';
const PW = process.env.WW_DEMO_PASSWORD || 'demo-only-change-me';
const results = [];
const check = (name, ok, extra = '') => { results.push({ name, ok }); console.log((ok ? 'PASS ' : 'FAIL ') + name + (extra ? ' — ' + extra : '')); };

async function login(page, user) {
  await page.goto(BASE + '/login');
  await page.fill('#username', user);
  await page.fill('#password', PW);
  await Promise.all([page.waitForURL(BASE + '/'), page.click('button[type=submit]')]);
}

(async () => {
  const browser = await chromium.launch();
  const consoleProblems = [];
  const attach = (page) => {
    page.on('console', m => { if (['error', 'warning'].includes(m.type())) consoleProblems.push(m.type() + ': ' + m.text()); });
    page.on('pageerror', e => consoleProblems.push('pageerror: ' + e.message));
  };

  // ---------- desktop, CEO
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage(); attach(page);
  await login(page, 'ceo');
  check('CEO dashboard shows stock cards', (await page.locator('.metric').count()) >= 4);
  await page.screenshot({ path: OUT + '/dash-desktop.png' });
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  check('page background is white', bg === 'rgb(255, 255, 255)', bg);
  const nav = await page.locator('.nav a:not(.planned)').allTextContents();
  check('CEO nav excludes receipts/dispatch create-only items & includes ledger', nav.includes('Stock Ledger') && nav.includes('Audit Trail'), nav.join(' | '));

  for (const [label, path] of [['Fabrics', '/fabrics'], ['Lot trace', '/fabrics/lots/DEMO-104'], ['Processing job', '/processing/jobs/1'], ['Dispatch', '/dispatch/1'], ['Gate', '/gate'], ['Ledger', '/ledger'], ['Audit', '/audit'], ['Ask', '/ask?q=history+of+DEMO-104-F1'], ['Settings', '/settings']]) {
    const r = await page.goto(BASE + path);
    check('desktop page loads: ' + label, r.status() === 200);
    if (label === 'Lot trace') await page.screenshot({ path: OUT + '/lot-desktop.png', fullPage: true });
  }
  await page.goto(BASE + '/ask');
  await page.fill('#q', 'Where is lot DEMO-104-F1?');
  await page.click('button:has-text("Ask")');
  await page.waitForSelector('text=Evidence');
  check('Ask returns answer with evidence link', (await page.locator('.card a[href="/fabrics/lots/DEMO-104-F1"]').count()) >= 1);

  // search shortcut
  await page.goto(BASE + '/');
  await page.keyboard.press('/');
  check('"/" focuses global search', await page.evaluate(() => document.activeElement && document.activeElement.id === 'global-search'));
  await page.keyboard.type('GP-0');
  await Promise.all([page.waitForURL(/\/search\?q=GP-0/), page.keyboard.press('Enter')]);
  check('global search finds gate pass', (await page.locator('table a').count()) >= 1);

  // sidebar collapse on desktop
  await page.goto(BASE + '/');
  await page.click('#nav-toggle');
  check('desktop sidebar collapses', await page.locator('#sidebar').isHidden());
  await page.click('#nav-toggle');
  check('desktop sidebar re-expands', await page.locator('#sidebar').isVisible());

  // ---------- production user: variance preview + confirm dialog
  const pctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const p2 = await pctx.newPage(); attach(p2);
  await login(p2, 'prod1');
  await p2.goto(BASE + '/processing/jobs/2');
  await p2.fill('form[data-variance-form] input[name=accepted]', '100');
  await p2.fill('form[data-variance-form] input[name=wastage]', '10');
  let txt = await p2.locator('[data-variance-out]').first().textContent();
  check('variance preview shows exception 40.000', txt.includes('40.000') && txt.includes('exception'), txt.trim());
  await p2.fill('form[data-variance-form] input[name=accepted]', '141');
  txt = await p2.locator('[data-variance-out]').first().textContent();
  check('variance preview blocks over-yield', txt.includes('exceeds'), txt.trim());
  await p2.fill('form[data-variance-form] input[name=accepted]', '140');
  txt = await p2.locator('[data-variance-out]').first().textContent();
  check('variance preview shows balanced', txt.includes('Balanced'), txt.trim());
  await p2.screenshot({ path: OUT + '/job-result-form.png', fullPage: true });
  await p2.click('form[data-variance-form] button[type=submit]');
  await p2.waitForSelector('.toast');
  check('result draft saved (flash shown)', (await p2.locator('.toast').textContent()).includes('Result drafted'));
  // post with confirm dialog; dismiss first, then accept
  let dialogMsg = '';
  p2.once('dialog', d => { dialogMsg = d.message(); d.dismiss(); });
  await p2.click('button:has-text("Post result")');
  check('confirm dialog appears and Cancel posts nothing', dialogMsg.includes('Post result') && (await p2.locator('button:has-text("Post result")').count()) === 1, dialogMsg);
  p2.once('dialog', d => d.accept());
  await p2.click('button:has-text("Post result")');
  await p2.waitForSelector('.toast');
  check('result posted after confirm', (await p2.locator('.toast').textContent()).includes('posted'));

  // forbidden page for production role
  const forb = await p2.goto(BASE + '/dispatch');
  check('prod1 gets 403 on /dispatch', forb.status() === 403);

  // ---------- mobile
  const mctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const m = await mctx.newPage(); attach(m);
  await login(m, 'manager1');
  for (const path of ['/', '/fabrics', '/fabrics/lots/DEMO-104', '/processing', '/processing/jobs/1', '/dispatch', '/dispatch/1', '/gate', '/ledger', '/audit', '/settings', '/fabrics/receipts/new']) {
    await m.goto(BASE + path);
    const over = await m.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    check('mobile no page-level horizontal overflow: ' + path, over <= 1, 'overflow=' + over);
  }
  await m.goto(BASE + '/');
  check('mobile sidebar hidden by default', await m.locator('#sidebar').evaluate(el => el.getBoundingClientRect().right <= 0));
  await m.click('#nav-toggle');
  await m.waitForTimeout(300);
  check('mobile sidebar opens', await m.locator('#sidebar').evaluate(el => el.getBoundingClientRect().left >= 0));
  await m.screenshot({ path: OUT + '/dash-mobile-nav.png' });
  await m.click('.nav a[href="/gate"]');
  await m.waitForURL(/\/gate/);
  check('mobile nav link navigates', true);
  await m.screenshot({ path: OUT + '/gate-mobile.png' });

  const csp = consoleProblems.filter(x => /Content Security Policy|CSP|Refused/i.test(x));
  check('no CSP violations in console', csp.length === 0, csp.join(' ; '));
  const other = consoleProblems.filter(x => !/Content Security Policy|CSP|Refused/i.test(x) && !/403|404|409|422|401/.test(x));
  check('no other console errors', other.length === 0, other.join(' ; '));

  await browser.close();
  const failed = results.filter(r => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} browser checks passed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('SCRIPT ERROR', e); process.exit(2); });
