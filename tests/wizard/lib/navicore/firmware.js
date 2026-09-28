// The Firmware tab's outside world for the no-board specs (specs/navicore/firmware.spec.js; docs/hil_plan/NAVICORE.md
// §2, nct.fw.*), served by page.route so nothing leaves 127.0.0.1 (lib/navicore/fixtures.js aborts the rest):
//   - GitHub's contents listing of firmware/ (config_tool/flasher.js:66-79, :131-141) and the raw downloads it names
//     (:158-168), with a decoy for every wrong pick the flasher's comments warn about (:143-151, :192-201): another
//     product's image, CI's stock _boot.bin, a partition table from another build, and a directory with an app's name;
//   - the CryptoJS script and the esptool-js module (lib/navicore/fake_cryptojs.js, fake_esptool.mjs).
// firmwareSet() builds a deterministic image set, and fnv() is the FNV-1a both stand-ins record, over the same bytes.
const fs = require('node:fs');
const path = require('node:path');

const OWNER = 'greghulette', REPO = 'NaviCore', DIR = 'firmware';                                // flasher.js:40-43
const ESPTOOL_URL = 'https://cdn.jsdelivr.net/npm/esptool-js@0.4.7/+esm';                        // :26
const CRYPTOJS_URL = 'https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.2.0/crypto-js.min.js';  // :27
const BOOT_NAME = 'WCB_S3_custom_bootloader_16MB_wdt3s.bin';                                     // :202
const CORS = { 'access-control-allow-origin': '*' };

function fnv(buf) {
  let h = 0x811c9dc5;
  for (const b of buf) { h ^= b; h = Math.imul(h, 0x01000193) >>> 0; }
  return h >>> 0;
}

// A deterministic "image": xorshift bytes behind the ESP image magic 0xE9.
function image(size, seed) {
  const b = Buffer.alloc(size);
  let x = Math.imul(seed, 2654435761) >>> 0 || 1;
  for (let i = 0; i < size; i++) {
    x ^= x << 13; x >>>= 0; x ^= x >>> 17; x ^= x << 5; x >>>= 0;
    b[i] = x & 0xff;
  }
  if (size) b[0] = 0xE9;
  return b;
}

// The three files a release publishes (flasher.js:31-33), for `version`.
function firmwareSet({ version = 'v0.3.0_011200QOCT26', appSize = 20_000 } = {}) {
  return {
    version,
    app: image(appSize, 1),
    boot: image(0x4E80, 2),
    part: image(0xC00, 3),
    names: { app: `NaviCore_${version}_ESP32S3.bin`, part: `NaviCore_${version}_ESP32S3_part.bin`, boot: BOOT_NAME },
  };
}

// Serve `set` from GitHub's firmware/ on `branch`. The returned object is live: the listing is built from it on every
// request, so a spec can change it between flashes.
//   omit      ['app' | 'part' | 'boot']: leave those files out of the listing
//   throttle  n: answer the next n listing requests 429 with Retry-After: 1 (GitHub's per-IP throttle; GitHub exposes
//             Retry-After to scripts, so this one does too)
//   fetched   the raw files downloaded, in order          listings   every listing URL asked for
async function mockFirmware(page, set, { branch = 'main', omit = [], throttle = 0, decoys = true } = {}) {
  const gh = { set, omit: [...omit], throttle, throttled: 0, fetched: [], listings: [] };
  const raw = (name) => `https://raw.githubusercontent.com/${OWNER}/${REPO}/${branch}/${DIR}/${name}`;
  const files = () => {
    const out = [];
    if (decoys) {
      out.push({ name: 'NaviCore_old_ESP32S3.bin', type: 'dir', buf: null });                        // a directory
      out.push({ name: 'RC-Controller_v1.4.0_ESP32S3.bin', type: 'file', buf: image(4096, 9) });     // another product
      out.push({ name: `NaviCore_${gh.set.version}_ESP32S3_boot.bin`, type: 'file', buf: image(4096, 8) });   // CI's stock boot
      out.push({ name: 'NaviCore_v0.1.0_010000QJAN26_ESP32S3_part.bin', type: 'file', buf: image(3072, 7) }); // another build's table
    }
    for (const k of ['app', 'part', 'boot']) if (!gh.omit.includes(k)) out.push({ name: gh.set.names[k], type: 'file', buf: gh.set[k] });
    return out;
  };
  await page.route((u) => u.hostname === 'api.github.com', (route) => {
    const u = new URL(route.request().url());
    gh.listings.push(u.pathname + u.search);
    if (u.pathname !== `/repos/${OWNER}/${REPO}/contents/${DIR}` || u.searchParams.get('ref') !== branch) {
      return route.fulfill({ status: 404, headers: CORS, contentType: 'application/json', body: '{"message":"Not Found"}' });
    }
    if (gh.throttle > 0) {
      gh.throttle--;
      gh.throttled++;
      return route.fulfill({ status: 429, headers: { ...CORS, 'retry-after': '1', 'access-control-expose-headers': 'Retry-After' },
                             contentType: 'application/json', body: '{"message":"API rate limit exceeded"}' });
    }
    const body = files().map((f) => ({ name: f.name, path: `${DIR}/${f.name}`, type: f.type, size: f.buf ? f.buf.length : 0,
                                        download_url: f.type === 'file' ? raw(f.name) : null }));
    return route.fulfill({ status: 200, headers: CORS, contentType: 'application/json', body: JSON.stringify(body) });
  });
  await page.route((u) => u.hostname === 'raw.githubusercontent.com', (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split('/').pop());
    gh.fetched.push(name);
    const f = files().find((x) => x.name === name && x.type === 'file');
    if (!f) return route.fulfill({ status: 404, headers: CORS, body: '404: Not Found' });
    return route.fulfill({ status: 200, headers: CORS, contentType: 'application/octet-stream', body: f.buf });
  });
  await page.route((u) => u.href === CRYPTOJS_URL, (route) => route.fulfill({
    status: 200, headers: CORS, contentType: 'text/javascript', body: fs.readFileSync(path.join(__dirname, 'fake_cryptojs.js'), 'utf8'),
  }));
  await page.route((u) => u.href === ESPTOOL_URL, (route) => route.fulfill({
    status: 200, headers: CORS, contentType: 'text/javascript', body: fs.readFileSync(path.join(__dirname, 'fake_esptool.mjs'), 'utf8'),
  }));
  return gh;
}

// What the esptool-js stand-in recorded ({ calls: [], fail: {} } before the tool loads it).
function esptool(page) {
  return page.evaluate(() => JSON.parse(JSON.stringify(window.__fakeEsptool || { calls: [], fail: {} })));
}

// The marker the CryptoJS stand-in returns for these bytes.
function md5Marker(buf) { return `md5/${buf.length}/${fnv(buf)}`; }

module.exports = { firmwareSet, mockFirmware, esptool, fnv, md5Marker, image, BOOT_NAME, ESPTOOL_URL, CRYPTOJS_URL };
