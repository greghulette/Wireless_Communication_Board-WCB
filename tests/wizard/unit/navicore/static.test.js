// nctool.static — the NaviCore config tool checked without running it (docs/hil_plan/NAVICORE.md §5.4, L0 items 1-6).
// The page has no build step, so a syntax slip silently breaks all event wiring, and pages-deploy.yml publishes it
// with no check; Intellex ships the same bytes. Everything here reads files only.
//
//   node --test unit/navicore/static.test.js      (tests/wizard; the harness test nctool.static runs exactly this)
//
// Skips when the NaviCore repo is not beside this one (CI without the second checkout). A constant or pattern that
// cannot be found FAILS, naming it: a rename must never switch a check off. Failure messages name constants and files,
// never file content (wcb_config.h also holds the compile-time mesh password; only one #define is read from it).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { navicoreRoot, intellexRoot, toolFile, ROOT } = require('../../lib/navicore/paths');
const { toolHtml, inlineScripts, lift, sandbox, compiles } = require('./extract');

const NC = navicoreRoot();
const skip = NC ? false : 'the NaviCore repo is not beside this one (clone it, or set NAVICORE_REPO)';
const read = (...p) => fs.readFileSync(path.join(NC, ...p), 'utf8');

// One number out of a file by pattern; a miss fails naming the constant.
function num(text, re, what) {
  const m = re.exec(text);
  assert.ok(m, `${what}: pattern ${re} not found`);
  return Number(m[1]);
}

// The firmware as a release build compiles it, which is what the tool meets: every `#ifdef NAVICORE_HIL_HOOKS` block
// dropped and its #else branch kept (NaviCore 703a0e7: the HIL hooks add a debug bit, DBG_WIRE, and give the overflow
// guard in rcConfigToJSON one `if (...) {` per branch, which a brace count would otherwise read as two).
function releaseOnly(src) {
  return src.replace(/^[ \t]*#ifdef NAVICORE_HIL_HOOKS\b[^\n]*\n([\s\S]*?)^[ \t]*#endif\b[^\n]*(?:\n|$)/gm, (m, body) => {
    const e = /^[ \t]*#else\b[^\n]*\n/m.exec(body);
    return e ? body.slice(e.index + e[0].length) : '';
  });
}

// The body of a C++ function, by brace matching that skips strings, char literals and comments.
function cBody(src, signature) {
  const at = src.indexOf(signature);
  assert.ok(at >= 0, `${signature}: not found`);
  let i = src.indexOf('{', at), depth = 0;
  for (; i < src.length; i++) {
    const c = src[i];
    if (c === '"' || c === "'") { for (i++; i < src.length && src[i] !== c; i++) if (src[i] === '\\') i++; continue; }
    if (c === '/' && src[i + 1] === '/') { i = src.indexOf('\n', i); continue; }
    if (c === '/' && src[i + 1] === '*') { i = src.indexOf('*/', i) + 1; continue; }
    if (c === '{') depth++;
    if (c === '}' && --depth === 0) return src.slice(src.indexOf('{', at), i + 1);
  }
  assert.fail(`${signature}: no end`);
}

test('nctool.static: every inline <script> of config_tool/index.html compiles', { skip }, () => {
  const scripts = inlineScripts(toolHtml());
  assert.ok(scripts.length >= 2, `expected the tool's two inline scripts, found ${scripts.length}`);
  assert.ok(scripts[0].text.length > 500_000, 'the main script is unexpectedly small — was the page cut short?');
  for (const s of scripts) {
    assert.ok(compiles(s.text), `the inline <script> starting at config_tool/index.html:${s.line} does not compile`);
  }
});

test('nctool.static: flasher.js and serial-hub.js pass node --check', { skip }, () => {
  for (const f of ['flasher.js', 'serial-hub.js']) {
    const r = spawnSync(process.execPath, ['--check', toolFile(f)], { encoding: 'utf8' });
    assert.equal(r.status, 0, `node --check config_tool/${f} failed:\n${r.stderr}`);
  }
});

test('nctool.static: the firmware/tool constant pairs agree (CONFIG_SCHEMA.md §6 and the field caps)', { skip }, () => {
  const cfg = read('rc_config.h'), tel = read('rc_telemetry.h'), ino = read('NaviCore.ino');
  const html = toolHtml();
  const fwDeviceId = num(read('wcb_config.h'), /^#define\s+WCB_DEVICE_ID\s+(\d+)/m, 'WCB_DEVICE_ID');
  const pairs = [
    // [what, firmware value, tool value]
    ['tap tiers', num(cfg, /#define\s+RC_NUM_TAP_TIERS\s+(\d+)/, 'RC_NUM_TAP_TIERS'), num(html, /^const NUM_TAP_TIERS\s*=\s*(\d+)/m, 'NUM_TAP_TIERS')],
    ['long-press tap', num(cfg, /#define\s+RC_TAP_LONG\s+(\d+)/, 'RC_TAP_LONG'), num(html, /^const TAP_LONG\s*=\s*(\d+)/m, 'TAP_LONG')],
    ['actions per tier', num(cfg, /#define\s+RC_ACTIONS_PER_TIER\s+(\d+)/, 'RC_ACTIONS_PER_TIER'), num(html, /^const MAX_ACTIONS_PER_TIER\s*=\s*(\d+)/m, 'MAX_ACTIONS_PER_TIER')],
    ['knob outputs', num(cfg, /#define\s+RC_KNOB_MAX_OUTPUTS\s+(\d+)/, 'RC_KNOB_MAX_OUTPUTS'), num(html, /^const KNOB_MAX_OUTPUTS\s*=\s*(\d+)/m, 'KNOB_MAX_OUTPUTS')],
    ['Maestro slots', num(cfg, /#define\s+RC_NUM_MAESTROS\s+(\d+)/, 'RC_NUM_MAESTROS'), num(html, /^const NUM_MAESTRO_SLOTS\s*=\s*(\d+)/m, 'NUM_MAESTRO_SLOTS')],
    ['WLED slots', num(cfg, /#define\s+RC_NUM_WLED\s+(\d+)/, 'RC_NUM_WLED'), num(html, /^const NUM_WLED_SLOTS\s*=\s*(\d+)/m, 'NUM_WLED_SLOTS')],
    ['WCB profiles', num(cfg, /#define\s+RC_MAX_WCB_PROFILES\s+(\d+)/, 'RC_MAX_WCB_PROFILES'), num(html, /^const WCB_MAX_PROFILES\s*=\s*(\d+)/m, 'WCB_MAX_PROFILES')],
    ['matrix slots', num(cfg, /#define\s+RC_NUM_THRESHOLDS\s+(\d+)/, 'RC_NUM_THRESHOLDS'),
      num(html, /^const RC_PHYSICAL_SLOTS\s*=\s*(\d+)/m, 'RC_PHYSICAL_SLOTS') + num(html, /^const RC_LOGICAL_COUNT\s*=\s*(\d+)/m, 'RC_LOGICAL_COUNT')],
    ['physical slots', num(cfg, /#define\s+RC_NUM_PHYSICAL\s+(\d+)/, 'RC_NUM_PHYSICAL'), num(html, /^const RC_PHYSICAL_SLOTS\s*=\s*(\d+)/m, 'RC_PHYSICAL_SLOTS')],
    ['smoothing profiles', num(cfg, /#define\s+RC_NUM_SMOOTH_PROFILES\s+(\d+)/, 'RC_NUM_SMOOTH_PROFILES'),
      num(lift('_smoothProfiles'), /config\.smoothProfiles\.length\s*<\s*(\d+)/, '_smoothProfiles pad')],
    ['action note (chars)', num(cfg, /char\s+note\[(\d+)\];\s*\/\/ human-readable label/, 'RcAction.note') - 1, num(html, /noteInput\.maxLength\s*=\s*(\d+)/, 'action note maxLength')],
    ['tier note (chars)', num(cfg, /char\s+note\[(\d+)\];\s*\/\/ per-tier caption/, 'RcTier.note') - 1, num(lift('saveButton'), /\.slice\(0,\s*(\d+)\)/, 'saveButton tier-note slice')],
    ['action cmd (chars)', num(cfg, /char\s+cmd\[(\d+)\];/, 'RcAction.cmd') - 1, num(html, /^const RC_ACTION_CMD_MAX\s*=\s*(\d+)/m, 'RC_ACTION_CMD_MAX')],
    ['button label (chars)', num(cfg, /struct RcThreshold\s*\{[^}]*char\s+label\[(\d+)\]/, 'RcThreshold.label') - 1, num(html, /class="logical-label"[^>]*maxlength="(\d+)"/, 'logical-label maxlength')],
    ['mesh password (chars)', num(cfg, /struct RcWcbNetwork\s*\{[^}]*char\s+password\[(\d+)\]/, 'RcWcbNetwork.password') - 1, num(html, /id="wcb-password" maxlength="(\d+)"/, 'wcb-password maxlength')],
    ['mode-report template (chars)', num(cfg, /char\s+tmpl\[(\d+)\]/, 'RcModeReport.tmpl') - 1, num(html, /id="modereport-template" maxlength="(\d+)"/, 'modereport-template maxlength')],
    ['fragment chunk', num(tel, /FRAG_CHUNK_BYTES\s*=\s*(\d+)/, 'rcTelemetry::FRAG_CHUNK_BYTES'), num(html, /^const FRAG_CHUNK_BYTES\s*=\s*(\d+)/m, 'FRAG_CHUNK_BYTES')],
    ['fragment receive pool (upload cap)', num(tel, /FRAG_MAX_PARTS\s*=\s*(\d+)/, 'rcTelemetry::FRAG_MAX_PARTS'), num(html, /^const FRAG_MAX_PARTS\s*=\s*(\d+)/m, 'FRAG_MAX_PARTS')],
    ['fragment send cap (download)', num(tel, /FRAG_SEND_MAX_PARTS\s*=\s*(\d+)/, 'rcTelemetry::FRAG_SEND_MAX_PARTS'), num(html, /^const FRAG_MAX_PARTS_RECV\s*=\s*(\d+)/m, 'FRAG_MAX_PARTS_RECV')],
    ['envelope cap', num(tel, /MAX_ENV_BYTES\s*=\s*(\d+)/, 'MAX_ENV_BYTES'), num(html, /^const FRAG_MAX_ENV_BYTES\s*=\s*(\d+)/m, 'FRAG_MAX_ENV_BYTES')],
    ['fragment timeout', num(tel, /FRAG_TIMEOUT_MS\s*=\s*(\d+)/, 'rcTelemetry::FRAG_TIMEOUT_MS'), num(html, /^const FRAG_TIMEOUT_MS\s*=\s*(\d+)/m, 'FRAG_TIMEOUT_MS')],
    ['NaviCore mesh id', fwDeviceId, num(html, /^const RC_WCB_DEVICE_ID\s*=\s*(\d+)/m, 'RC_WCB_DEVICE_ID')],
  ];
  const bad = pairs.filter(([, fw, tool]) => fw !== tool).map(([w, fw, tool]) => `${w}: firmware ${fw}, tool ${tool}`);
  assert.deepEqual(bad, [], 'constant pairs that disagree');
  // The mapping keys are t1..t<RC_NUM_TAP_TIERS> on both sides (rc_config.h prints "t" + (ti + 1)).
  const { TIER_KEYS } = sandbox(['TIER_KEYS']);
  assert.deepEqual([...TIER_KEYS], Array.from({ length: pairs[0][1] }, (_, i) => `t${i + 1}`), 'TIER_KEYS');
});

test('nctool.static: the debug chips, knob and switch tables match the firmware', { skip }, () => {
  const ino = releaseOnly(read('NaviCore.ino')), cfg = read('rc_config.h');
  // DBG_* bits (NaviCore.ino) vs DEBUG_CATEGORIES (the chips send their bitmask in SET_DEBUG_FLAGS).
  const fwBits = {};
  for (const m of ino.matchAll(/^#define\s+DBG_([A-Z0-9]+)\s+\(1u\s*<<\s*(\d+)\)/gm)) fwBits[m[1].toLowerCase()] = 1 << Number(m[2]);
  assert.ok(Object.keys(fwBits).length >= 7, `found only ${Object.keys(fwBits).length} DBG_* bits in NaviCore.ino`);
  const { DEBUG_CATEGORIES } = sandbox(['DEBUG_CATEGORIES']);
  const toolBits = Object.fromEntries(DEBUG_CATEGORIES.map((c) => [c.key, c.bit]));
  assert.deepEqual(toolBits, fwBits, 'DEBUG_CATEGORIES bits vs DBG_* in NaviCore.ino');
  // The knob and switch label tables: the X20 model lists every control the firmware has, and no model lists one
  // the firmware lacks.
  const list = (re, what) => { const m = re.exec(cfg); assert.ok(m, `${what}: not found`); return [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]); };
  const fwKnobs = list(/RC_KNOB_LABELS\[RC_NUM_KNOBS\]\s*=\s*\{([^}]*)\}/, 'RC_KNOB_LABELS');
  const fwSwitches = list(/RC_SWITCH_LABELS\[RC_NUM_SWITCHES\]\s*=\s*\{([^}]*)\}/, 'RC_SWITCH_LABELS');
  const { TX_MODELS, TX_MODEL_X20 } = sandbox(['TX_MODEL_X18', 'TX_MODEL_TWIN_X_LITE', 'TX_MODEL_X20', 'TX_MODELS']);
  assert.deepEqual([...TX_MODELS[TX_MODEL_X20].knobs], fwKnobs, 'X20 knobs vs RC_KNOB_LABELS');
  assert.deepEqual([...TX_MODELS[TX_MODEL_X20].switches], fwSwitches, 'X20 switches vs RC_SWITCH_LABELS');
  for (const id of Object.keys(TX_MODELS)) {
    for (const k of TX_MODELS[id].knobs) assert.ok(fwKnobs.includes(k), `model ${id} knob ${k} is not a firmware knob`);
    for (const s of TX_MODELS[id].switches) assert.ok(fwSwitches.includes(s), `model ${id} switch ${s} is not a firmware switch`);
  }
});

test('nctool.static: the bulk-transfer constants match WCB_Client', { skip }, (t) => {
  const client = path.join(path.dirname(NC), 'WCBClient', 'src', 'WCB_Client.h');
  if (!fs.existsSync(client)) { t.skip('the WCBClient repo is not beside NaviCore'); return; }
  const h = fs.readFileSync(client, 'utf8'), html = toolHtml();
  assert.equal(num(html, /^const BULK_CHUNK_RAW\s*=\s*(\d+)/m, 'BULK_CHUNK_RAW'), num(h, /#define\s+WCB_BULK_CHUNK_RAW\s+(\d+)/, 'WCB_BULK_CHUNK_RAW'));
  assert.equal(num(html, /^const BULK_MAX_CHUNKS\s*=\s*(\d+)/m, 'BULK_MAX_CHUNKS'), num(h, /#define\s+WCB_BULK_MAX_CHUNKS\s+(\d+)/, 'WCB_BULK_MAX_CHUNKS'));
});

test('nctool.static: every top-level key rcConfigToJSON writes is read by applyConfig', { skip }, () => {
  const body = cBody(releaseOnly(read('rc_config.h')), 'String rcConfigToJSON()');
  const fwKeys = new Set([...body.matchAll(/\bdoc\["(\w+)"\]\s*=/g), ...body.matchAll(/\bdoc\.createNested(?:Object|Array)\("(\w+)"\)/g)].map((m) => m[1]));
  assert.ok(fwKeys.size >= 30, `found only ${fwKeys.size} top-level keys in rcConfigToJSON`);
  const applied = new Set([...lift('applyConfig').matchAll(/\bdata\.(\w+)/g)].map((m) => m[1]));
  const unread = [...fwKeys].filter((k) => !applied.has(k));
  assert.deepEqual(unread, [], 'keys the firmware prints that applyConfig never reads (a Save would then diff against a default)');
});

test('nctool.static: Intellex ships the tool byte for byte (src/webui)', { skip }, (t) => {
  const ix = intellexRoot();
  if (!ix) { t.skip('the Intellex repo is not beside this one'); return; }
  for (const f of ['index.html', 'flasher.js', 'serial-hub.js']) {
    const a = fs.readFileSync(toolFile(f)), b = fs.readFileSync(path.join(ix, 'src', 'webui', f));
    assert.ok(a.equals(b), `Intellex/src/webui/${f} differs from NaviCore/config_tool/${f} (${b.length} vs ${a.length} bytes)`);
  }
});

test('nctool.static: the tool and the Wizard speak the same shared-hub protocol (serial-hub.js)', { skip }, () => {
  const proto = (text, who) => {
    const name = (k) => { const m = new RegExp(`${k}:\\s*'([^']+)'`).exec(text); assert.ok(m, `${who}: ${k} not found`); return m[1]; };
    const sent = new Set([...text.matchAll(/_post\(\{\s*t:\s*'(\w+)'/g)].map((m) => m[1]));
    const handled = new Set([...text.matchAll(/case\s+'(\w+)':/g)].map((m) => m[1]));
    return { channel: name('channelName'), lock: name('lockName'), sent: [...sent].sort(), handled: [...handled].sort() };
  };
  const tool = proto(fs.readFileSync(toolFile('serial-hub.js'), 'utf8'), 'config_tool/serial-hub.js');
  const wiz = proto(fs.readFileSync(path.join(ROOT, 'Wizard', 'serial-hub.js'), 'utf8'), 'Wizard/serial-hub.js');
  assert.deepEqual(tool, wiz);
  for (const t of ['hello', 'claim', 'yield', 'tx', 'rx', 'state', 'bye']) assert.ok(tool.handled.includes(t), `no handler for '${t}'`);
});

test('nctool.static: what the Intellex shim drives in the tool exists (ids, globals, the branch key)', { skip }, () => {
  const html = toolHtml(), main = inlineScripts(html)[0].text, flasher = fs.readFileSync(toolFile('flasher.js'), 'utf8');
  for (const id of ['btn-connect', 'btn-fw-flash', 'btn-fw-wipe', 'btn-fw-ota', 'btn-fw-ota-wcb', 'status-text']) {
    assert.ok(html.includes(`id="${id}"`), `#${id} is gone (intellex_shim.js drives it)`);
  }
  for (const fn of ['connectDirect', 'onViaWcbToggle']) {
    assert.ok(new RegExp(`^(?:async\\s+)?function\\s+${fn}\\s*\\(`, 'm').test(main), `${fn}() is gone (intellex_shim.js calls it)`);
  }
  assert.ok(/window\.flashFirmware\s*=|function\s+flashFirmware\s*\(/.test(flasher), 'flashFirmware is gone from flasher.js');
  assert.ok(flasher.includes("'rc_fw_branch'"), "flasher.js no longer reads localStorage 'rc_fw_branch' (Intellex sets it)");
  const ix = intellexRoot();
  if (ix) {
    const shim = fs.readFileSync(path.join(ix, 'src', 'intellex_shim.js'), 'utf8');
    for (const s of ['btn-fw-flash', 'btn-fw-wipe', 'btn-fw-ota', 'btn-fw-ota-wcb', 'connectDirect', 'onViaWcbToggle', 'rc_fw_branch']) {
      assert.ok(shim.includes(s), `intellex_shim.js no longer mentions ${s}: update this list`);
    }
  }
});
