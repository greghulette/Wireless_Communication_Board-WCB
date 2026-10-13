// The Firmware tab when GitHub cannot serve the firmware (config_tool/flasher.js's "gh-explain" block, the same code as
// the WCB Wizard's): the Latest check, Update Firmware and OTA over USB say whose problem it is - the internet, GitHub's
// rate limit, GitHub being down, a passing GitHub error - instead of an HTTP code, which stays in the log. No board and
// nothing leaving 127.0.0.1: GitHub's firmware/ listing, raw downloads and the CDNs are lib/navicore/firmware.js's
// stand-ins, and each case below overrides the listing, the downloads or GitHub's status page
// (www.githubstatus.com/api/v2/components.json) with page.route, which takes precedence (last registered wins).
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');
const F = require('../../lib/navicore/firmware');
const { toolFile, ROOT } = require('../../lib/navicore/paths');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const SET = F.firmwareSet();
// What api.github.com sends a page on another origin (its Access-Control-Expose-Headers, abridged).
const CORS = { 'access-control-allow-origin': '*',
               'access-control-expose-headers': 'ETag, Link, Location, Retry-After, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Used, X-RateLimit-Resource, X-RateLimit-Reset' };

const OFFLINE = "Can't reach the internet - check this computer's connection.";
const rateLimit = (when) => "GitHub's download limit for this network is used up (60 requests an hour without signing in). "
  + `${when} This is GitHub's limit, not a fault in the WCB tools.`;
const trouble = (parts) => `GitHub is having problems right now (${parts}). The firmware download depends on GitHub, so this `
  + "isn't something on your side or in the WCB tools - try again once GitHub recovers. Live status: githubstatus.com";
const server = (code) => `GitHub's servers answered with an error (${code}). That's usually a short-lived problem on GitHub's `
  + 'side - try again in a few minutes. If it keeps happening, check githubstatus.com.';

function components(down = {}) {
  const names = ['Git Operations', 'API Requests', 'Webhooks', 'Issues', 'Actions', 'Packages', 'Pages', 'Codespaces', 'Copilot'];
  return { page: { id: 'kctbh9vrtdwd', name: 'GitHub', url: 'https://www.githubstatus.com' },
           components: names.map((n) => ({ id: n.toLowerCase().replace(/ /g, ''), name: n, status: down[n] || 'operational', group: false })) };
}

// Faults over F.mockFirmware's GitHub: f.list / f.file null (the mock's real answer), 'abort' (no answer), or
// { status, headers, body }; f.status is the components.json githubstatus.com answers (or 'abort' / { status }).
// f.hits counts each.
async function faults(page) {
  const f = { list: null, file: null, status: components(), hits: { list: 0, file: 0, status: 0 } };
  const answer = (route, how) => {
    if (how === null) return route.fallback();
    if (how === 'abort') return route.abort('internetdisconnected');
    return route.fulfill({ status: how.status, headers: { ...CORS, ...(how.headers || {}) }, contentType: 'application/json',
                           body: typeof how.body === 'string' ? how.body : JSON.stringify(how.body || { message: 'HIL' }) });
  };
  await page.route((u) => u.hostname === 'api.github.com', (route) => { f.hits.list++; return answer(route, f.list); });
  await page.route((u) => u.hostname === 'raw.githubusercontent.com', (route) => { f.hits.file++; return answer(route, f.file); });
  await page.route((u) => u.hostname === 'www.githubstatus.com', (route) => {
    f.hits.status++;
    if (f.status === 'abort' || (f.status && f.status.status)) return answer(route, f.status);
    return route.fulfill({ status: 200, headers: { 'access-control-allow-origin': '*' }, contentType: 'application/json',
                           body: JSON.stringify(f.status) });
  });
  return f;
}

async function openFirmwareTab(page) {
  await page.locator('#btn-hwsetup').click();
  await page.locator('#cfg-tabs .cfg-tab[data-tab="firmware"]').click();
  await expect(page.locator('#cfg-pane-firmware')).toHaveClass(/active/);
}

// The ↻ Latest check, with the status page's remembered answer cleared: what #fw-update-status says, and its hover.
async function latestCheck(page) {
  await page.evaluate(() => { _ghStatusCache = null; });   // eslint-disable-line no-undef
  await page.locator('#btn-fw-check').click();
  await expect(page.locator('#fw-latest-version')).not.toHaveText('checking…');
  await expect(page.locator('#fw-update-status')).not.toHaveText('');
  const el = page.locator('#fw-update-status');
  return { text: await el.textContent(), title: await el.getAttribute('title') };
}

// Update Firmware with no session (a port granted earlier): the status line and the log once it has stopped.
async function updateFirmware(page) {
  await page.evaluate(() => { _ghStatusCache = null; });   // eslint-disable-line no-undef
  await page.locator('#btn-fw-flash').click();
  await expect(page.locator('#fw-status')).toHaveText(/^Flash (failed|complete)/, { timeout: 30_000 });
  await expect(page.locator('#btn-fw-flash')).toHaveText('⬆ Update Firmware');
  return { status: await page.locator('#fw-status').textContent(), log: await page.locator('#fw-log').textContent() };
}

test.describe('GitHub trouble, explained', () => {
  test.use({ emuOptions: { config: BENCH } });

  test('nctool.fw_gh_explain the Latest check and Update Firmware say why GitHub could not serve the firmware - no internet, the spent hourly rate limit (not retried) with its reset time, GitHub reporting a download part down, a GitHub error while all is well - with the HTTP code in the log (or on hover) only; a 404 keeps its own message; a download that works never asks the status page', async ({ page, context }) => {
    const gh = await F.mockFirmware(page, SET);
    const f = await faults(page);
    await T.openTool(page);
    await page.evaluate(() => window.__hilSerial.grant(true));      // a port this origin was granted earlier
    await openFirmwareTab(page);

    // Working: the version, and the whole set downloads - and not one request to githubstatus.com.
    await expect(page.locator('#fw-latest-version')).toHaveText(SET.version);
    const got = await page.evaluate(async () => (await prepareFirmwareFlash(() => {}, () => {})).length);
    expect(got).toBe(3);
    expect(f.hits.status, 'requests to githubstatus.com while everything worked').toBe(0);

    // The Latest check. 1: the browser offline (navigator.onLine false; a routed request would still be answered, so
    // GitHub's are failed too) - said at once, nothing asked.
    Object.assign(f, { list: 'abort', status: 'abort' });
    await context.setOffline(true);
    let r;
    try { r = await latestCheck(page); } finally { await context.setOffline(false); }
    expect(r.text).toBe(OFFLINE);
    expect(r.title).toContain('no answer from GitHub for the firmware list');
    expect(f.hits.status).toBe(0);
    // ...or neither GitHub nor its status page answering.
    r = await latestCheck(page);
    expect(r.text).toBe(OFFLINE);
    expect(f.hits.status).toBe(1);
    // 2: the hourly limit spent, its reset time in the page's own clock.
    const reset = Math.floor((await page.evaluate(() => Date.now())) / 1000) + 2400;
    const when = await page.evaluate((ms) => new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }), reset * 1000);
    Object.assign(f, { list: { status: 403, headers: { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': String(reset) },
                               body: { message: 'API rate limit exceeded for 203.0.113.9.' } }, status: components() });
    r = await latestCheck(page);
    expect([r.text, r.title]).toEqual([rateLimit(`It resets at ${when}.`), 'GitHub API: HTTP 403']);
    // 3 and 4: GitHub down (Pages), or erroring while all is well.
    Object.assign(f, { list: { status: 503, body: 'unicorn' }, status: components({ Pages: 'major_outage', Copilot: 'major_outage' }) });
    r = await latestCheck(page);
    expect(r.text).toBe(trouble('Pages: major outage'));
    f.status = components();
    r = await latestCheck(page);
    expect([r.text, r.title]).toEqual([server(503), 'GitHub API: HTTP 503']);
    // 5: a 404 is ours - today's words, no status request.
    const s5 = f.hits.status;
    f.list = { status: 404, body: { message: 'Not Found' } };
    r = await latestCheck(page);
    expect(r.text).toBe('Could not reach GitHub (GitHub API: HTTP 404).');
    expect(f.hits.status).toBe(s5);

    // Update Firmware. GitHub down while the images download: each 503 retried as before (Retry-After 0 keeps it
    // quick), then the status page names the parts, on the status line; the code in the log.
    Object.assign(f, { list: null, file: { status: 503, headers: { 'retry-after': '0' }, body: 'down' },
                       status: components({ 'Git Operations': 'partial_outage', 'API Requests': 'degraded_performance' }) });
    const n0 = f.hits.file;
    let u = await updateFirmware(page);
    let why = trouble('Git Operations: partial outage; API Requests: degraded performance');
    expect(u.status).toBe(`Flash failed: ${why}`);
    expect(u.log).toContain(`GitHub fetch failed: HTTP 503 fetching ${SET.names.app}`);
    expect(u.log).toContain(`✖ Firmware download failed: ${why}`);
    expect(f.hits.file - n0, 'a 503 is still retried (4 attempts)').toBe(4);
    expect(u.status).not.toContain('503');
    // The hourly limit spent: no retries - it lasts until the hour resets - and the reset time said.
    Object.assign(f, { file: null, list: { status: 403, headers: { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': String(reset) },
                                           body: { message: 'API rate limit exceeded' } } });
    const l0 = f.hits.list, s0 = f.hits.status;
    u = await updateFirmware(page);
    expect(u.status).toBe(`Flash failed: ${rateLimit(`It resets at ${when}.`)}`);
    expect(f.hits.list - l0, 'a spent hourly limit is not retried').toBe(1);
    expect(u.log).not.toContain('retrying');
    expect(f.hits.status - s0).toBe(0);
    // The listing 503s while all is well.
    Object.assign(f, { list: { status: 503, headers: { 'retry-after': '0' }, body: 'down' }, status: components() });
    u = await updateFirmware(page);
    expect(u.status).toBe(`Flash failed: ${server(503)}`);
    expect(u.log).toContain('GitHub fetch failed: GitHub API: HTTP 503');
    // A 404 listing: ours, the status line as before.
    f.list = { status: 404, body: { message: 'Not Found' } };
    u = await updateFirmware(page);
    expect(u.status).toBe('Flash failed.');
    expect(u.log).toContain('✖ Firmware download failed: GitHub API: HTTP 404');
    expect((await F.esptool(page)).calls.filter((c) => c.op === 'writeFlash'), 'nothing was written').toEqual([]);
    expect(gh.fetched.length, 'the mock served only the first, working download').toBe(3);
    T.expectNoPageErrors(page);
  });

  test('nctool.fw_gh_explain_ota Update over USB (OTA) whose image GitHub cannot serve stops on "OTA failed: <why>" with the HTTP code in the log only, and starts nothing on the board', async ({ page, emu }) => {
    await F.mockFirmware(page, SET);
    const f = await faults(page);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openFirmwareTab(page);
    await expect(page.locator('#fw-latest-version')).toHaveText(SET.version);
    Object.assign(f, { file: { status: 502, headers: { 'retry-after': '0' }, body: 'bad gateway' },
                       status: components({ 'Git Operations': 'major_outage' }) });
    await page.evaluate(() => { _ghStatusCache = null; });   // eslint-disable-line no-undef
    const rx0 = emu.rx.length;
    await page.locator('#btn-fw-ota').click();
    const why = trouble('Git Operations: major outage');
    await expect(page.locator('#fw-status')).toHaveText(`OTA failed: ${why}`, { timeout: 20_000 });
    await expect(page.locator('#fw-log')).toContainText(`GitHub fetch failed: HTTP 502 fetching ${SET.names.app}`);
    await expect(page.locator('#fw-log')).toContainText(`OTA failed: ${why}`);
    const sent = emu.rx.slice(rx0).map((r) => (r.json ? r.json.type : r.line));
    expect(sent.filter((t) => /OTALOCAL,(BEGIN|DATA)/.test(String(t))), 'OTA commands sent').toEqual([]);
    await expect(page.locator('#btn-fw-ota')).toBeEnabled();
    T.expectNoPageErrors(page);
  });
});

test('nctool.fw_gh_explain_same_code the GitHub-trouble explainer in config_tool/flasher.js is byte-identical to the WCB Wizard\'s (Wizard/flasher.js), so the two tools word a failure the same way', async () => {
  // Node only: the "gh-explain" block between its BEGIN and END lines in both flasher.js files.
  test.skip(!toolFile('flasher.js'), 'the NaviCore repo is not beside this one');
  const block = (file) => {
    const s = fs.readFileSync(file, 'utf8');
    const a = s.indexOf('// ─── BEGIN gh-explain');
    const b = s.indexOf('// ─── END gh-explain');
    expect(a, `${file}: no BEGIN gh-explain line`).toBeGreaterThanOrEqual(0);
    expect(b, `${file}: no END gh-explain line`).toBeGreaterThan(a);
    return s.slice(a, s.indexOf('\n', b));
  };
  const tool = block(toolFile('flasher.js'));
  const wizard = block(path.join(ROOT, 'Wizard', 'flasher.js'));
  expect(tool.length).toBeGreaterThan(2000);
  expect(tool, 'config_tool/flasher.js and Wizard/flasher.js gh-explain blocks differ').toBe(wizard);
});
