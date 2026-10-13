// Under Intellex the tools' GitHub firmware calls go to the host (the shim rewrites the Contents URL to /_api/gh/contents,
// src/ghproxy.py), and when the host cannot get the firmware either - GitHub down, throttled or out of reach, nothing
// cached - it works out why (src/ghstatus.py) and answers with `explain`, the sentence for the user. These check that the
// pages SHOW that sentence (their flasher.js "gh-explain" block reads it off a same-origin reply) and do not go asking
// githubstatus.com themselves. The harness starts the host offline (INTELLEX_OFFLINE) on a stage with no firmware cache,
// so the host's real answer is its offline-mode sentence - nothing here is faked but the githubstatus.com counter.
const { test, expect, skipUnlessHost } = require('../lib/fixtures');

skipUnlessHost(test);

const HOST_SAYS = 'Intellex is in offline mode (INTELLEX_OFFLINE is set), so it did not try GitHub. '
  + 'Intellex has no saved copy of this firmware to use instead.';

// Every request the page makes to GitHub's status page (answered 503 here, so none could ever reach it) and to the
// host's GitHub proxy.
async function watch(context) {
  const seen = { status: 0, proxy: [] };
  await context.route((u) => u.hostname === 'www.githubstatus.com', (route) => {
    seen.status++;
    return route.fulfill({ status: 503, body: 'guarded by the HIL spec' });
  });
  context.on('request', (r) => { if (r.url().includes('/_api/gh/')) seen.proxy.push(new URL(r.url()).pathname); });
  return seen;
}

test('intellex.ui_gh_explain_navicore the config tool shows the host\'s explanation for a firmware download it could not serve - the Latest check and fetchFirmwareImages (both OTA buttons) - once, without retrying the host or asking githubstatus.com', async ({ page, context, rec, guarded }) => {
  const seen = await watch(context);
  await page.goto('/', { waitUntil: 'load' });
  await expect.poll(() => rec.intellex.some((l) => l.includes('firmware listings now come from the host')),
    { timeout: 15_000 }).toBe(true);
  await page.waitForFunction(() => typeof checkFirmwareLatest === 'function' && typeof ghExplain === 'function');

  // The Latest check (the Firmware tab's ↻): its status line is the host's sentence.
  await page.evaluate(() => checkFirmwareLatest(true));
  await expect(page.locator('#fw-update-status')).toHaveText(HOST_SAYS, { timeout: 30_000 });
  expect(await page.locator('#fw-update-status').getAttribute('title')).toBe('GitHub API: HTTP 502');

  // fetchFirmwareImages, as Update over USB / over WCB (OTA) call it: the host's sentence is the error, the 502 in the
  // log, and the host asked ONCE (it already retried GitHub - the page's own throttle retries are for GitHub).
  seen.proxy.length = 0;
  const r = await page.evaluate(async () => {
    const log = [];
    try { await fetchFirmwareImages((m) => log.push(m)); return { error: null, log }; }
    catch (e) { return { error: e.message, explained: e.ghExplained || null, log }; }
  });
  expect(r.error).toBe(HOST_SAYS);
  expect(r.explained).toBe(HOST_SAYS);
  expect(r.log).toContain('GitHub fetch failed: GitHub API: HTTP 502');
  expect(seen.proxy, 'requests to the host\'s GitHub proxy').toEqual(['/_api/gh/contents']);
  expect(seen.status, 'requests to githubstatus.com from the page').toBe(0);
  expect(rec.errors, 'page errors').toEqual([]);
});

test('intellex.ui_gh_explain_wizard the Wizard shows the host\'s explanation for a firmware download it could not serve (getBinaryData: both OTA paths) without asking githubstatus.com', async ({ page, context, rec, guarded }) => {
  const seen = await watch(context);
  await page.goto('/wcb/Wizard/', { waitUntil: 'load' });
  await expect.poll(() => rec.intellex.some((l) => l.includes('firmware listings now come from the host')),
    { timeout: 15_000 }).toBe(true);
  await page.waitForFunction(() => typeof getBinaryData === 'function' && typeof ghExplain === 'function');
  const r = await page.evaluate(async () => {
    const log = [];
    try { await getBinaryData('ESP32', (m) => log.push(m)); return { error: null, log }; }
    catch (e) { return { error: e.message, log }; }
  });
  expect(r.error).toBe(HOST_SAYS);
  expect(r.log).toContain('GitHub fetch failed: GitHub API: HTTP 502');
  expect(r.log).toContain(`✕ ${HOST_SAYS}`);
  expect(seen.status, 'requests to githubstatus.com from the page').toBe(0);
  expect(rec.errors, 'page errors').toEqual([]);
});
