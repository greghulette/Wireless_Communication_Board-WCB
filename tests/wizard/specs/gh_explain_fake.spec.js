// Needs no board: when the firmware download from GitHub fails, the Wizard says whose problem it is - the internet,
// GitHub's rate limit, GitHub being down, a passing GitHub error - instead of an HTTP code (the "gh-explain" block in
// Wizard/flasher.js, the same code as NaviCore's config_tool/flasher.js; nctool.fw_gh_explain_same_code compares them).
// GitHub's file list (api.github.com), its raw downloads (raw.githubusercontent.com) and GitHub's status page
// (www.githubstatus.com/api/v2/components.json) are page.route stand-ins whose answers each case picks, shaped and
// CORS-headed as the real ones are (api.github.com exposes its X-RateLimit-* headers to scripts). Nothing is flashed;
// no port is opened. Runs standalone, in CI, and under the harness by id (s30_wizard.py).
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');
const { install } = require('../lib/fake');

const FAKE_ESPTOOL = fs.readFileSync(path.join(__dirname, '..', 'lib', 'fake_esptool_rescan.mjs'), 'utf8');
const NAME = (k) => `WCB_9.9.9_010101RJAN2030_${k}.bin`;
const FILES = ['ESP32', 'ESP32_part', 'ESP32_boot', 'ESP32S3', 'ESP32S3_part', 'ESP32S3_boot_16MB', 'ESP32S3_boot_8MB'];
// What api.github.com sends a page on another origin (its Access-Control-Expose-Headers, abridged).
const CORS = { 'access-control-allow-origin': '*',
               'access-control-expose-headers': 'ETag, Link, Location, Retry-After, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Used, X-RateLimit-Resource, X-RateLimit-Reset' };

// The sentences (flasher.js gh-explain). Exact, so a reworded message fails here first.
const OFFLINE = "Can't reach the internet - check this computer's connection.";
const rateLimit = (when) => "GitHub's download limit for this network is used up (60 requests an hour without signing in). "
  + `${when} This is GitHub's limit, not a fault in the WCB tools.`;
const trouble = (parts) => `GitHub is having problems right now (${parts}). The firmware download depends on GitHub, so this `
  + "isn't something on your side or in the WCB tools - try again once GitHub recovers. Live status: githubstatus.com";
const server = (code) => `GitHub's servers answered with an error (${code}). That's usually a short-lived problem on GitHub's `
  + 'side - try again in a few minutes. If it keeps happening, check githubstatus.com.';

// githubstatus.com's components.json: all operational but the parts named, plus parts a download never uses.
function components(down = {}) {
  const names = ['Git Operations', 'API Requests', 'Webhooks', 'Issues', 'Actions', 'Packages', 'Pages', 'Codespaces', 'Copilot'];
  return { page: { id: 'kctbh9vrtdwd', name: 'GitHub', url: 'https://www.githubstatus.com' },
           components: names.map((n) => ({ id: n.toLowerCase().replace(/ /g, ''), name: n, status: down[n] || 'operational', group: false })) };
}

// The stand-ins. gh.list / gh.file / gh.status: 'ok' (the real thing), 'abort' (no answer: the request fails), or
// { status, headers, body } to answer with. gh.hits counts the requests each got; gh.files is the listing's contents.
async function fakeGitHub(page) {
  const gh = { list: 'ok', file: 'ok', status: components(), files: [...FILES], hits: { list: 0, file: 0, status: 0 } };
  const answer = (route, how, ok) => {
    if (how === 'abort') return route.abort('internetdisconnected');
    if (how === 'ok') return ok();
    return route.fulfill({ status: how.status, headers: { ...CORS, ...(how.headers || {}) }, contentType: 'application/json',
                           body: typeof how.body === 'string' ? how.body : JSON.stringify(how.body || { message: 'HIL' }) });
  };
  await page.route('**/vendor/esptool-js/**', (route) => route.fulfill({ status: 200, contentType: 'text/javascript', body: FAKE_ESPTOOL }));
  await page.route((u) => u.hostname === 'api.github.com', (route) => {
    gh.hits.list++;
    return answer(route, gh.list, () => route.fulfill({ status: 200, headers: CORS, contentType: 'application/json',
      body: JSON.stringify(gh.files.map((k) => ({ type: 'file', name: NAME(k),
        download_url: `https://raw.githubusercontent.com/greghulette/Wireless_Communication_Board-WCB/main/Code/bin/${NAME(k)}` }))) }));
  });
  await page.route((u) => u.hostname === 'raw.githubusercontent.com', (route) => {
    gh.hits.file++;
    return answer(route, gh.file, () => route.fulfill({ status: 200, headers: CORS, contentType: 'application/octet-stream',
                                                        body: Buffer.alloc(32, 0xE9) }));
  });
  await page.route((u) => u.hostname === 'www.githubstatus.com', (route) => {
    gh.hits.status++;
    const how = gh.status;
    if (how === 'abort' || (how && how.status)) return answer(route, how);
    return route.fulfill({ status: 200, headers: { 'access-control-allow-origin': '*' }, contentType: 'application/json', body: JSON.stringify(how) });
  });
  return gh;
}

// getBinaryData('ESP32') as the flash and both OTA paths call it, with the status page's remembered answer cleared:
// the error it threw (or null), its log and the toasts it raised.
function fetchBins(page, { keepStatus = false } = {}) {
  return page.evaluate(async (keepStatus) => {
    if (!keepStatus) _ghStatusCache = null;   // eslint-disable-line no-undef
    const logs = [];
    const t0 = __fake.toasts.length;
    let error = null;
    try { await getBinaryData('ESP32', (m) => logs.push(m)); } catch (e) { error = e.message; }
    return { error, logs: logs.join('\n'), toasts: __fake.toasts.slice(t0).map((t) => t.message).join('\n') };
  }, keepStatus);
}

// The page's own short local time for an epoch, the way the rate-limit sentence words it.
const localTime = (page, ms) => page.evaluate((ms) => new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }), ms);

test('wizard.flasher_fake_gh_explain a firmware download GitHub cannot serve says why in one sentence - no internet (the browser offline, or neither GitHub nor its status page answering), the hourly rate limit with its reset time, GitHub reporting Git Operations / API Requests / Pages down, a GitHub error while all is well - with the HTTP code in the log only; a 404 and a missing file keep their own message; a download that works never asks the status page, which is asked at most once a minute', async ({ page, context }) => {
  // getBinaryData (Wizard/flasher.js) and the gh-explain block's ghFetch, ghHttpError, ghStatusTrouble and ghExplain.
  const gh = await fakeGitHub(page);
  await openWizard(page);
  await install(page);

  // A download that works: the whole set, and not one request to the status page (the load's version check included).
  let r = await fetchBins(page);
  expect(r.error).toBe(null);
  expect(r.logs).toContain('Loaded 0 KB (boot + partitions + app)');
  expect(gh.hits.status, 'requests to githubstatus.com on a download that worked').toBe(0);

  // 1. The browser is offline (navigator.onLine false; a routed request would still be answered, so GitHub's are
  //    failed too): said at once, nothing asked.
  Object.assign(gh, { list: 'abort', status: 'abort' });
  await context.setOffline(true);
  try {
    expect(await page.evaluate(() => navigator.onLine)).toBe(false);
    r = await fetchBins(page);
  } finally {
    await context.setOffline(false);
  }
  expect(r.error).toBe(OFFLINE);
  expect(r.logs).toContain('GitHub fetch failed: no answer from GitHub for the firmware list');
  expect(r.toasts, 'getBinaryData leaves the toast to its caller').toBe('');
  expect(gh.hits.status).toBe(0);
  // ...online, but neither GitHub nor its status page answers.
  r = await fetchBins(page);
  expect(r.error).toBe(OFFLINE);
  expect(gh.hits.status).toBe(1);

  // 2. GitHub's hourly limit, spent: the reset time from x-ratelimit-reset, the status page not asked.
  const reset = Math.floor(Date.now() / 1000) + 1800;
  Object.assign(gh, { list: { status: 403, headers: { 'x-ratelimit-limit': '60', 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': String(reset) },
                              body: { message: 'API rate limit exceeded for 203.0.113.9. (But here\'s the good news: Authenticated requests get a higher rate limit.)' } },
                      status: components() });
  const s0 = gh.hits.status;
  r = await fetchBins(page);
  expect(r.error).toBe(rateLimit(`It resets at ${await localTime(page, reset * 1000)}.`));
  expect(r.logs).toContain('GitHub fetch failed: GitHub API: HTTP 403');
  expect(r.error).not.toContain('403');
  // ...a 429 that only says so in its message: no reset time to give.
  gh.list = { status: 429, body: { message: 'API rate limit exceeded for 203.0.113.9.' } };
  r = await fetchBins(page);
  expect(r.error).toBe(rateLimit('It resets within the hour.'));
  expect(gh.hits.status - s0, 'a rate limit says why itself: no status request').toBe(0);

  // 3. GitHub down: the listing came, the image 503s, and the status page lists the download parts that are not
  //    operational - Copilot's outage is not one of them.
  Object.assign(gh, { list: 'ok', file: { status: 503, body: '<html>Service Unavailable</html>' },
                      status: components({ Pages: 'partial_outage', 'Git Operations': 'degraded_performance', Copilot: 'major_outage' }) });
  r = await fetchBins(page);
  expect(r.error).toBe(trouble('Git Operations: degraded performance; Pages: partial outage'));
  expect(r.logs).toContain(`GitHub fetch failed: HTTP 503 fetching ${NAME('ESP32')}`);
  expect(r.logs).toContain(`✕ ${r.error}`);
  gh.status = components({ 'API Requests': 'under_maintenance' });
  r = await fetchBins(page);
  expect(r.error).toBe(trouble('API Requests: under maintenance'));
  // ...no answer from GitHub at all, while the status page reports trouble: still GitHub's.
  Object.assign(gh, { list: 'abort', status: components({ 'API Requests': 'major_outage' }) });
  r = await fetchBins(page);
  expect(r.error).toBe(trouble('API Requests: major outage'));

  // 4. A GitHub error while the status page says all is well (only Copilot down), or cannot be read.
  Object.assign(gh, { list: { status: 503, body: 'upstream connect error' }, status: components({ Copilot: 'major_outage' }) });
  r = await fetchBins(page);
  expect(r.error).toBe(server(503));
  expect(r.logs).toContain('GitHub fetch failed: GitHub API: HTTP 503');
  gh.status = { status: 500, body: 'oops' };
  r = await fetchBins(page);
  expect(r.error, 'the status page answering 500').toBe(server(503));
  gh.status = 'abort';
  r = await fetchBins(page);
  expect(r.error, 'the status page not answering').toBe(server(503));
  Object.assign(gh, { list: 'abort', status: components() });
  r = await fetchBins(page);
  expect(r.error, 'no answer from GitHub, the status page all operational').toBe(server('no response'));

  // 5. Ours, not GitHub's: today's message, toasted as before, and the status page not asked.
  const s5 = gh.hits.status;
  gh.list = { status: 404, body: { message: 'Not Found', documentation_url: 'https://docs.github.com/rest' } };
  r = await fetchBins(page);
  expect(r.error).toBe('GitHub fetch failed: GitHub API: HTTP 404');
  expect(r.toasts).toContain('Could not fetch firmware from GitHub: GitHub API: HTTP 404');
  Object.assign(gh, { list: 'ok', files: ['ESP32_part', 'ESP32_boot'] });
  r = await fetchBins(page);
  expect(r.error).toBe('GitHub fetch failed: No file ending with "_ESP32.bin" found in Code/bin/');
  expect(gh.hits.status - s5, 'a 404 or a missing file asked the status page').toBe(0);
  gh.files = [...FILES];

  // The status page's answer is remembered for a minute: a second failure inside it asks nothing; an older one asks.
  Object.assign(gh, { list: { status: 502, body: 'bad gateway' }, status: components() });
  const s6 = gh.hits.status;
  r = await fetchBins(page);
  r = await fetchBins(page, { keepStatus: true });
  expect(r.error).toBe(server(502));
  expect(gh.hits.status - s6, 'two failures inside a minute').toBe(1);
  await page.evaluate(() => { _ghStatusCache.at -= 61_000; });   // eslint-disable-line no-undef
  r = await fetchBins(page, { keepStatus: true });
  expect(gh.hits.status - s6, 'a failure after the minute').toBe(2);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.flasher_fake_gh_explain_shown the explanation is what the user sees: OTA over USB toasts "OTA failed: <why>" with the HTTP code in the board\'s terminal only, a flash stops with it as its error, and the firmware check button toasts it, while a check that works toasts the latest version and asks nothing', async ({ page }) => {
  // boardOtaSerial's catch, flashFirmware's Step 2, boardCheckFwUpdate / fetchLatestFirmwareVersion (Wizard/app.js).
  const gh = await fakeGitHub(page);
  await openWizard(page);
  await install(page);
  const why = trouble('Pages: major outage');
  Object.assign(gh, { list: { status: 503, body: 'unicorn' }, status: components({ Pages: 'major_outage' }) });

  // OTA over USB on a board in slot 1: STATUS answers, the image cannot be fetched.
  const ota = await page.evaluate(async () => {
    _ghStatusCache = null;   // eslint-disable-line no-undef
    const c = __fake.conn(1);
    c.sendAndCollect = async (s) => (/OTALOCAL,STATUS/.test(s) ? 'Chip:        ESP32-D0WD (family 0)\r\nSession:     idle' : '');
    const term = [];
    const realTerm = window.termLog;
    window.termLog = (n, text, type) => { term.push(`${n}|${type}|${text}`); return realTerm(n, text, type); };
    const t0 = __fake.toasts.length;
    try { await boardOtaSerial(1); } finally { window.termLog = realTerm; }
    return { toasts: __fake.toasts.slice(t0).map((t) => `${t.type}|${t.message}`), term };
  });
  expect(ota.toasts).toEqual([`error|OTA failed: ${why}`]);
  expect(ota.term).toContain('1|sys|GitHub fetch failed: GitHub API: HTTP 503');
  expect(ota.term).toContain(`1|err|[OTA] ✕ ${why}`);

  // flashFirmware (boardGo's call): the explanation is its error, and boardGo toasts "Flash failed: " + its first line.
  const flash = await page.evaluate(async () => {
    try {
      await flashFirmware({ fake: true }, 1, { onProgress() {}, onLog() {}, onStatus() {} });
      return null;
    } catch (e) { return e.message; }
  });
  expect(flash).toBe(why);
  expect(flash.split('\n')).toHaveLength(1);

  // The firmware check button: the explanation, the code in the terminal.
  const check = await page.evaluate(async () => {
    _ghStatusCache = null;   // eslint-disable-line no-undef
    _fwCheckLastMs = 0;      // eslint-disable-line no-undef
    const term = [];
    const realTerm = window.termLog;
    window.termLog = (n, text, type) => { term.push(`${n}|${text}`); return realTerm(n, text, type); };
    const t0 = __fake.toasts.length;
    try { await boardCheckFwUpdate(1); } finally { window.termLog = realTerm; }
    return { toasts: __fake.toasts.slice(t0).map((t) => `${t.type}|${t.message}`), term };
  });
  expect(check.toasts).toEqual([`warning|${why}`]);
  expect(check.term).toContain('1|Firmware check failed: GitHub API: HTTP 503');

  // A check that works: the version, and no status request.
  Object.assign(gh, { list: 'ok', status: components() });
  const s0 = gh.hits.status;
  const ok = await page.evaluate(async () => {
    _fwCheckLastMs = 0;      // eslint-disable-line no-undef
    const t0 = __fake.toasts.length;
    await boardCheckFwUpdate(1);
    return __fake.toasts.slice(t0).map((t) => `${t.type}|${t.message}`);
  });
  expect(ok).toEqual(['success|Latest firmware on GitHub: v9.9.9_010101RJAN2030']);
  expect(gh.hits.status - s0).toBe(0);
  expect(page.wizErrors).toEqual([]);
});
