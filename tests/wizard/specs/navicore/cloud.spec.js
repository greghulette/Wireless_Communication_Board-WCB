// The hidden cloud config backup (docs/hil_plan/NAVICORE.md §2, nct.io.cloud_backup, L1): the "NaviCore" wordmark clicked
// four times opens a modal (cfgOpenCloudModal, config_tool/index.html ~20599) that backs the live config up to the
// cheat-sheet Worker's /cfg/ route, encrypted in the browser under a username + password that also address the slots
// (cfgBackup ~20431: a ring of CFG_BACKUP_MAX slots and an encrypted manifest), lists, renames, downloads, restores and
// uploads them, and parks the droid WiFi fields. The Worker is never reached: page.route answers its /cfg/ routes from
// an in-memory store the way tools/cheatsheet-relay-worker.js does, token check, 404 and the failure statuses included.
// The upload token the page carries is read into Node to check the header and never printed (D-NC5).
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');
const { normEvent } = require('../../lib/navicore/clips');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const ENDPOINT = 'https://navicore-cheatsheet.greg-hulette.workers.dev';
const CORS = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'POST, GET, PUT, OPTIONS',
               'Access-Control-Allow-Headers': 'Content-Type, X-CS-Token' };
const USER = 'hil-user', PW = 'hil-pass-word';   // test credentials for the fake Worker only, never a real account
const BEEP = [{ t: 0, k: 1, slot: 2, ch: 0, pos: 5000 }, { t: 100, k: 0, type: 'stop' }, { t: 200, k: 1, slot: 2, ch: 0, pos: 7000 }].map(normEvent);

// The Worker's /cfg/<key> routes (tools/cheatsheet-relay-worker.js:68-102) over a Map: GET 404 / 200, PUT with the
// token check and the 4 MB cap, OPTIONS for the preflight a PUT with a custom header needs. `fail` makes the next
// requests fail: get = a status for every GET, put = for a slot PUT, putManifest = for the manifest PUT.
async function mockWorker(page) {
  const token = await page.evaluate(() => CS_QR_TOKEN);
  const kv = new Map(), log = [], fail = { get: null, put: null, putManifest: null };
  await page.route((url) => url.href.startsWith(ENDPOINT + '/cfg/'), async (route) => {
    const req = route.request();
    const key = new URL(req.url()).pathname.slice('/cfg/'.length);
    const method = req.method();
    if (method === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS });
    const rec = { method, key, status: 0 };
    log.push(rec);
    const reply = (status, body, type = 'text/plain') => {
      rec.status = status;
      return route.fulfill({ status, headers: { 'Content-Type': type, ...CORS }, body });
    };
    if (!/^[a-z0-9_]+$/i.test(key) || key.length > 128) return reply(400, 'bad key');
    if (method === 'GET') {
      if (fail.get) return reply(fail.get, 'boom');
      return kv.has(key) ? reply(200, kv.get(key)) : reply(404, 'Not found');
    }
    if (method === 'PUT') {
      if (req.headers()['x-cs-token'] !== token) return reply(403, '{"error":"bad token"}', 'application/json');
      const st = key.endsWith('m') ? fail.putManifest : fail.put;
      if (st) return reply(st, '{"error":"x"}', 'application/json');
      const body = req.postData() || '';
      if (body.length > 4 * 1024 * 1024) return reply(413, '{"error":"too large"}', 'application/json');
      kv.set(key, body);
      return reply(200, '{"ok":true}', 'application/json');
    }
    return reply(405, 'Method not allowed');
  });
  // puts(): the slot and manifest keys that landed, in order; attempts(): every PUT the page made, refused ones too.
  return { kv, log, fail,
           puts: () => log.filter((r) => r.method === 'PUT' && r.status === 200).map((r) => r.key),
           attempts: () => log.filter((r) => r.method === 'PUT').map((r) => r.key) };
}

// The pre-PBKDF2 slot base (_cfgSlotBaseLegacy): where backups taken before the change still live.
const legacyBase = (user, pw) => crypto.createHash('sha256')
  .update('navicore-cfg-slot-v1|' + encodeURIComponent(user) + '|' + encodeURIComponent(pw)).digest('hex');

async function openCloud(page) {
  const brand = page.locator('.brand-word');
  for (let i = 0; i < 4; i++) await brand.click();
  await expect(page.locator('#cfg-cloud-modal')).toBeVisible();
}
async function creds(page, user = USER, pw = PW) {
  await page.locator('#cfg-cloud-user').fill(user);
  await page.locator('#cfg-cloud-pw').fill(pw);
}
const status = (page) => page.locator('#cfg-cloud-status');
// The listed backups, newest first, as the rename buttons carry them: [{ i, note }].
const listed = (page) => page.locator('#cfg-cloud-list-wrap .cfg-cloud-note-edit').evaluateAll((els) => els.map((e) => ({ i: +e.dataset.i, note: e.dataset.note })));
const rowButton = (page, note, cls) => page.locator('#cfg-cloud-list-wrap > div').filter({ has: page.locator(`.cfg-cloud-note-edit[data-note="${note}"]`) }).locator(`.${cls}`);
async function backup(page, note, { clips = false } = {}) {
  await page.locator('#cfg-cloud-note').fill(note);
  await page.locator('#cfg-cloud-clips').setChecked(clips);
  await page.locator('#cfg-cloud-backup').click();
}
// Base64 that holds no readable config: the Worker only ever sees ciphertext.
function expectCiphertext(body) {
  expect(body).toMatch(/^[A-Za-z0-9+/=]+$/);
  const bytes = Buffer.from(body, 'base64').toString('latin1');
  expect(bytes).not.toContain('"config"');
  expect(bytes).not.toContain('thresholds');
}

test.describe('the cloud backup', () => {
  test.use({ emuOptions: { config: BENCH, clips: { beep: { mode: 1, events: BEEP } } } });

  test('nctool.cloud_backup_mocked the wordmark clicked 4x opens the hidden modal; a backup lands as ciphertext in a PBKDF2-addressed slot plus an encrypted manifest and is listed by date and note; a second one with the clips included, a rename (manifest only), a download (clips carried), a restore that offers the clips separately; a wrong password sees an empty list; a backup under the old slot base is still found; remembered credentials re-list on open', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page, [true, 'HIL two renamed', true, false]);   // the clips size warning; the rename prompt; Restore?; write the clips?
    const downloads = T.captureDownloads(page);
    await T.openTool(page);
    const w = await mockWorker(page);
    await T.connectUsb(page, emu);
    expect(await page.evaluate(() => !!document.getElementById('cfg-cloud-modal'))).toBe(false);
    await openCloud(page);
    // No credentials: nothing leaves the page.
    await page.locator('#cfg-cloud-backup').click();
    await expect(status(page)).toHaveText('Enter a username and password first.');
    expect(w.log).toEqual([]);
    // The first backup: the manifest is looked up (404 under the current and the legacy base), the slot and the
    // manifest are PUT under a 64-hex base, both ciphertext.
    await creds(page);
    await backup(page, 'HIL one');
    await expect.poll(listed.bind(null, page)).toEqual([{ i: 0, note: 'HIL one' }]);
    const base = w.puts()[0].slice(0, 64);
    expect(base).toMatch(/^[0-9a-f]{64}$/);
    expect(base).not.toBe(legacyBase(USER, PW));
    expect(w.puts()).toEqual([base + 's0', base + 'm']);
    // The index lookup, its legacy-base fallback, then the list's own read once the backup landed.
    expect(w.log.filter((r) => r.method === 'GET').map((r) => r.key)).toEqual([base + 'm', legacyBase(USER, PW) + 'm', base + 'm']);
    expectCiphertext(w.kv.get(base + 's0'));
    expectCiphertext(w.kv.get(base + 'm'));
    expect(await page.locator('#cfg-cloud-note').inputValue(), 'the note box is cleared after a backup').toBe('');
    // A second backup of an edited config, with the one clip included (the size warning is accepted).
    await page.evaluate(() => { config.holdMs = 777; });
    await backup(page, 'HIL two', { clips: true });
    await expect.poll(listed.bind(null, page)).toEqual([{ i: 1, note: 'HIL two' }, { i: 0, note: 'HIL one' }]);
    expect(dialogs[0].message).toMatch(/^Download all 1 clip\(s\)\?/);
    expect(w.puts()).toEqual([base + 's0', base + 'm', base + 's1', base + 'm']);
    // Rename rewrites only the manifest.
    await rowButton(page, 'HIL two', 'cfg-cloud-note-edit').click();
    await expect.poll(listed.bind(null, page)).toEqual([{ i: 1, note: 'HIL two renamed' }, { i: 0, note: 'HIL one' }]);
    expect(w.puts().slice(4)).toEqual([base + 'm']);
    // Download decrypts a slot into a readable backup file; the second carries the clip.
    await rowButton(page, 'HIL one', 'cfg-cloud-dl').click();
    await expect(status(page)).toHaveText('✓ Downloaded as a config .json file.');
    await rowButton(page, 'HIL two renamed', 'cfg-cloud-dl').click();
    await expect.poll(() => downloads.length).toBe(2);
    const one = JSON.parse(await downloads[0].ready), two = JSON.parse(await downloads[1].ready);
    expect(downloads[0].name).toMatch(/^navicore-config-HIL_one-\d{4}-\d{2}-\d{2}\.json$/);
    expect(one).toMatchObject({ navicore: 'config-backup/v1', note: 'HIL one' });
    expect(one.config.holdMs).toBe(BENCH.holdMs);
    expect(one.clips).toBeUndefined();
    expect(two.config.holdMs).toBe(777);
    expect(two.clips.map((c) => c.name)).toEqual(['beep']);
    expect(two.clips[0].events).toHaveLength(BEEP.length);
    // Restore the second: the config lands in the tool unsaved, the clips are offered apart and declined here.
    await page.evaluate(() => { config.holdMs = 999; });
    await rowButton(page, 'HIL two renamed', 'cfg-cloud-restore').click();
    await expect(page.locator('#cfg-cloud-modal')).toBeHidden();
    expect(dialogs.slice(-2).map((d) => d.message.split('\n')[0])).toEqual(['Restore this backup? It replaces the config in the tool (you can still review before Saving to the NaviCore).',
                                                                            'Config restored to the tool.']);
    expect(dialogs.at(-1).message).toContain('This backup also holds 1 clip(s).');
    expect(await page.evaluate(() => [config.holdMs, _configLoaded])).toEqual([777, true]);
    expect(await T.termLines(page, /\[backup\] cloud config restored/)).toHaveLength(1);
    expect(emu.lines(/EDITBEGIN|EDITEV/), 'declined: no clip written to the board').toEqual([]);
    // A wrong password addresses a different namespace: an empty list, nothing decrypted, nothing written.
    const puts = w.puts().length;
    await openCloud(page);
    await creds(page, USER, PW + 'x');
    await page.locator('#cfg-cloud-list').click();
    await expect(page.locator('#cfg-cloud-list-wrap')).toHaveText('No backups yet for this username + password.');
    expect(w.puts().length).toBe(puts);
    // A backup taken before the slot base moved to PBKDF2 lives under the legacy base: still listed, and the next
    // backup is written under the current base.
    const legacy = legacyBase(USER, PW);
    for (const k of [...w.kv.keys()]) { w.kv.set(legacy + k.slice(64), w.kv.get(k)); w.kv.delete(k); }
    await creds(page);
    await page.locator('#cfg-cloud-list').click();
    await expect.poll(listed.bind(null, page)).toEqual([{ i: 1, note: 'HIL two renamed' }, { i: 0, note: 'HIL one' }]);
    await backup(page, 'HIL three');
    await expect.poll(listed.bind(null, page)).toHaveLength(3);
    expect(w.puts().slice(puts)).toEqual([base + 's2', base + 'm']);
    // Remembered credentials: stored on List, prefilled and auto-listed on the next open, forgotten when unticked.
    await page.locator('#cfg-cloud-remember').check();
    await page.locator('#cfg-cloud-list').click();
    await expect.poll(() => page.evaluate(() => { const c = JSON.parse(localStorage.getItem('navicore-cfg-creds-v1') || 'null'); return c && c.u && c.p === 'hil-pass-word'; })).toBe(true);
    await expect(status(page)).toHaveText('');           // the list is in (closing under a loading list throws in the page)
    await page.locator('#cfg-cloud-close').click();
    await openCloud(page);
    await expect(page.locator('#cfg-cloud-user')).toHaveValue(USER);
    await expect(page.locator('#cfg-cloud-remember')).toBeChecked();
    await expect.poll(listed.bind(null, page)).toHaveLength(3);
    await page.locator('#cfg-cloud-remember').uncheck();
    await page.locator('#cfg-cloud-list').click();
    await expect.poll(() => page.evaluate(() => localStorage.getItem('navicore-cfg-creds-v1'))).toBeNull();
    T.expectNoPageErrors(page);
  });

  test('nctool.cloud_backup_failures a 5xx on the index aborts a backup before anything is written; the slot PUT\'s 413, 403 and 5xx each name what to do; a manifest PUT that fails after the slot landed says which half did, and the next backup reuses that slot; the eleventh backup overwrites the oldest; a config file uploads to a slot and loads into the tool; clips need a connection', async ({ page }) => {
    T.answerDialogs(page, [true]);   // Load into the tool?
    await T.openTool(page);
    const w = await mockWorker(page);
    await openCloud(page);
    // Not connected: a backup with the clips ticked stops before anything is sent.
    await creds(page);
    await backup(page, 'with clips', { clips: true });
    await expect(status(page)).toHaveText('Connect to the board to include clips (or untick it).');
    expect(w.log).toEqual([]);
    // A transient error reading the index must not be read as "no backups": nothing is written.
    await backup(page, 'first');
    await expect.poll(listed.bind(null, page)).toEqual([{ i: 0, note: 'first' }]);
    const base = w.puts()[0].slice(0, 64);
    w.fail.get = 500;
    await backup(page, 'lost');
    await expect(status(page)).toHaveText('fetch failed (500)');
    await page.locator('#cfg-cloud-list').click();
    await expect(status(page)).toHaveText('fetch failed (500)');
    w.fail.get = null;
    expect(w.puts()).toEqual([base + 's0', base + 'm']);
    // The slot PUT's failures, each with its instruction.
    for (const [st, text] of [[413, "this passed the tool's own 4.00 MB check, so the deployed Worker has an OLDER, smaller limit. Redeploy tools/cheatsheet-relay-worker.js"],
                              [403, "the Worker rejected the upload token; its UPLOAD_TOKEN secret and this tool's CS_QR_TOKEN must match"],
                              [404, 'the deployed Worker has no /cfg/ route. Redeploy'],
                              [429, "the Worker's hourly upload cap"],
                              [500, 'the Worker errored; check its KV binding CS']]) {
      w.fail.put = st;
      await backup(page, `fail ${st}`);
      await expect(status(page)).toHaveText(new RegExp(`^cloud upload failed \\(${st}\\) — ${text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`));
    }
    w.fail.put = null;
    expect(w.puts(), 'no slot was written').toEqual([base + 's0', base + 'm']);
    expect(w.attempts().slice(2), 'each try went for slot 1').toEqual(Array(5).fill(base + 's1'));
    // The slot lands, the manifest does not (three tries): the message says so, and the next backup takes the same
    // slot again because `next` never advanced.
    w.fail.putManifest = 500;
    await backup(page, 'half');
    await page.clock.fastForward(3000);
    await expect(status(page)).toHaveText(/^the backup data uploaded, but its index did not \(cloud upload failed \(500\)/, { timeout: 15_000 });
    expect(w.attempts().slice(7)).toEqual([base + 's1', base + 'm', base + 'm', base + 'm']);
    expect(w.puts().slice(2)).toEqual([base + 's1']);
    w.fail.putManifest = null;
    await backup(page, 'second');
    await expect.poll(listed.bind(null, page)).toEqual([{ i: 1, note: 'second' }, { i: 0, note: 'first' }]);
    expect(w.puts().slice(3)).toEqual([base + 's1', base + 'm']);
    // Ten slots: the eleventh backup overwrites slot 0 and drops 'first' from the list.
    const s0 = w.kv.get(base + 's0');
    for (let n = 3; n <= 11; n++) {
      await backup(page, `n${n}`);
      await expect.poll(() => listed(page).then((l) => l[0] && l[0].note)).toBe(`n${n}`);
    }
    const notes = (await listed(page)).map((e) => e.note);
    expect(notes).toEqual(['n11', 'n10', 'n9', 'n8', 'n7', 'n6', 'n5', 'n4', 'n3', 'second']);
    expect((await listed(page))[0].i, 'slot 0 reused').toBe(0);
    expect(w.kv.get(base + 's0')).not.toBe(s0);
    expect([...w.kv.keys()].sort()).toEqual([...Array.from({ length: 10 }, (_, i) => `${base}s${i}`), `${base}m`].sort());
    // A config file uploads into the next slot (note "<file note> (uploaded)"); a Wizard file is refused.
    const file = JSON.stringify({ navicore: 'config-backup/v1', savedAt: 1, note: 'from disk', config: { ...BENCH, holdMs: 555 } });
    await T.chooseFile(page, () => page.locator('#cfg-cloud-upload').click(), 'disk.json', file);
    await expect.poll(() => listed(page).then((l) => l[0])).toEqual({ i: 1, note: 'from disk (uploaded)' });
    expect(await listed(page)).toHaveLength(10);
    await T.chooseFile(page, () => page.locator('#cfg-cloud-upload').click(), 'wizard.json', '{"wizard":1,"boards":[]}');
    await expect(status(page)).toHaveText('that file is not a NaviCore config');
    // Load puts a file straight into the tool, unsaved, and closes the modal.
    await T.chooseFile(page, () => page.locator('#cfg-cloud-loadfile').click(), 'disk.json', file);
    await expect(page.locator('#cfg-cloud-modal')).toBeHidden();
    expect(await page.evaluate(() => [config.holdMs, _configLoaded])).toEqual([555, true]);
    expect(await T.termLines(page, /\[backup\] config loaded from file/)).toHaveLength(1);
    T.expectNoPageErrors(page);
  });

  test('nctool.cloud_hidden_wifi the droid WiFi fields parked in the cloud modal edit the config in place (nothing is sent), the note says what Save and a reboot will do and flags a password under 8 characters, the AP name placeholder is NaviCore-<id>, and Save then sends exactly wifiEnabled, wifiSsid and wifiPassword', async ({ page, emu }) => {
    await T.openTool(page);
    await mockWorker(page);
    await T.connectUsb(page, emu);
    await openCloud(page);
    // The bench NaviCore runs an access point, so the fields open enabled with its name (never echoed here).
    const note = page.locator('#cfg-wifi-note');
    const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    expect(BENCH.wifiEnabled).toBe(true);
    await expect(page.locator('#cfg-wifi-enable')).toBeChecked();
    await expect(note).toHaveText(new RegExp(`^WiFi will be ON after you Save and reboot\\. Join "${esc(BENCH.wifiSsid)}" from the app\\.$`));
    expect(await page.locator('#cfg-wifi-ssid').getAttribute('placeholder')).toBe(`AP name (blank = NaviCore-${BENCH.wcbNetwork.deviceId})`);
    await page.locator('#cfg-wifi-enable').uncheck();
    await expect(note).toHaveText('WiFi will be OFF after you Save and reboot.');
    await page.locator('#cfg-wifi-enable').check();
    await page.locator('#cfg-wifi-pw').fill('short');
    await expect(note).toHaveText('Password is 5 characters — WPA2 needs 8. The board will refuse to start the AP.');
    await page.locator('#cfg-wifi-pw').fill('');
    await expect(note).toHaveText('Set a password (8+ characters). The board will not start an open AP.');
    await page.locator('#cfg-wifi-pw').fill('hil-wifi-pw');
    await page.locator('#cfg-wifi-ssid').fill('');
    await expect(note).toHaveText(`WiFi will be ON after you Save and reboot. Join "NaviCore-${BENCH.wcbNetwork.deviceId}" from the app.`);
    await page.locator('#cfg-wifi-ssid').fill('HILdroid');
    await expect(note).toHaveText('WiFi will be ON after you Save and reboot. Join "HILdroid" from the app.');
    await page.locator('#cfg-wifi-pw-eye').click();
    expect(await page.locator('#cfg-wifi-pw').getAttribute('type')).toBe('text');
    await page.locator('#cfg-wifi-enable').uncheck();
    expect(emu.requests('SET_CONFIG'), 'nothing is sent by the fields').toEqual([]);
    await page.locator('#cfg-cloud-close').click();
    await page.evaluate(() => saveConfigToBoard());
    const [sent] = await T.waitRequests(emu, 'SET_CONFIG');
    expect(sent.data).toEqual({ wifiEnabled: false, wifiSsid: 'HILdroid', wifiPassword: 'hil-wifi-pw' });
    await expect.poll(() => page.evaluate(() => !!_pendingSaveBaseline)).toBe(false);
    expect([emu.config.toJSON().wifiEnabled, emu.config.toJSON().wifiSsid]).toEqual([false, 'HILdroid']);
    T.expectNoPageErrors(page);
  });
});
