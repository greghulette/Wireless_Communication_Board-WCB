// An in-Node NaviCore for the config tool's no-board specs (L1, docs/hil_plan/NAVICORE.md §5.3). It answers the
// lines the tool writes the way the firmware does, and each branch names the code it copies (NaviCore working tree,
// 2026-09-28; NaviCore.ino processInputLine :3773-4282, rc_telemetry.h handle() :2150-2460, _applyReassembled
// :1097-1230). The config itself is lib/navicore/model.js (rc_config.h's load/print rules).
//
// Three modes, set per spec:
//   direct   a NaviCore on the port (USB JSON + ? / # CLI). Lines that start with anything else are ignored, as the
//            firmware ignores them (a bare ;w20,... PING gets no reply: that is what makes the tool's Via-WCB probe
//            fail over a direct link).
//   via-wcb  a tethered WCB with NaviCore at mesh id 20 behind it: ;w20,<json> goes over the "mesh" to the
//            rc_telemetry handler, whose replies come back as the bridge prints them (raw JSON lines, fragmented
//            CONFIG / CMDLIB / WCB_META), and ;w20,<cli> comes back as [TERM:20] lines. Other lines are the WCB's own.
//   doorway  a WCB (or relay) the tool was told is a NaviCore: a bare JSON line is forwarded to the mesh and answered
//            in the RELAYED shapes ({"sys":1,...,"id":20}), and a ? line is the WCB's own (Intellex's case,
//            intellex_shim.js:411-437).
// Firmware OTA is lib/navicore/ota.js: ?OTALOCAL in direct mode, the tethered WCB's ?OTA relay in via-wcb mode, and
// the restart after a verified image (off the bus, deaf while booting, back on the other slot). Record/replay clips are
// lib/navicore/clips.js: ?REC over a clip store, with the ranged download and the indexed upload.
// Anything a spec needs to bend: hold(type) swallows a request type's replies until release(type), delay[type] = ms
// defers them, override[type] = (msg, emu) => [lines] | null replaces them, and inject(line) / injectRaw(bytes)
// write whatever a spec wants the tool to read. Every received line is in rx (with the parsed JSON when it parsed).
const { NcConfig, strl } = require('./model');

const FRAG_ESC_BUDGET = 180 - (26 + 3 + 3 + 5);   // rc_telemetry.h:516-518
const FRAG_CHUNK_MAX_BYTES = 160;                  // :522
const FRAG_SEND_MAX_PARTS = 512;                   // :147
const FRAG_MAX_PARTS = 192;                        // :141 — the receive pool
const FRAG_TIMEOUT_MS = 5000;                      // :149
const MAX_ENV_BYTES = 187;
const RC_ID = 20;

// FNV-1a over the bytes (rcCmdlibHash, rc_config.h:2159-2164).
function fnv1a(str) {
  let h = 2166136261 >>> 0;
  for (const b of Buffer.from(str, 'utf8')) { h ^= b; h = Math.imul(h, 16777619) >>> 0; }
  return h >>> 0;
}

// The escaped cost of one byte inside the envelope's "s" string (rc_telemetry.h _jsonEscCost :507-512).
function escCost(c) {
  if (c === 0x22 || c === 0x5C) return 2;
  if (c === 8 || c === 12 || c === 10 || c === 13 || c === 9) return 2;
  if (c < 0x20) return 6;
  return 1;
}
// The firmware's outbound slicing (_startFragSend :617-651): codepoint-safe byte boundaries, each slice filled to the
// measured escaped budget and capped at 160 raw bytes. Returns the slices as strings, or null past 512 parts.
function fragSlices(wrapped) {
  const b = Buffer.from(wrapped, 'utf8');
  const N = b.length;
  const out = [];
  let i = 0;
  while (i < N && out.length < FRAG_SEND_MAX_PARTS) {
    let take = 0, esc = 0;
    while (i + take < N) {
      const c = b[i + take];
      let cp = c < 0x80 ? 1 : (c >> 5) === 0x6 ? 2 : (c >> 4) === 0xE ? 3 : (c >> 3) === 0x1E ? 4 : 1;
      if (i + take + cp > N) cp = 1;
      let cost = 0;
      for (let k = 0; k < cp; k++) cost += escCost(b[i + take + k]);
      if (esc + cost > FRAG_ESC_BUDGET) break;
      if (take + cp > FRAG_CHUNK_MAX_BYTES) break;
      take += cp; esc += cost;
    }
    if (take === 0) take = 1;
    out.push(b.subarray(i, i + take).toString('utf8'));
    i += take;
  }
  return i < N ? null : out;
}

class NaviEmulator {
  constructor(opts = {}) {
    this.mode = opts.mode || 'direct';
    this.version = opts.version || 'v0.2.0_281200QSEP26';
    this.config = new NcConfig();
    if (opts.config) this.config.fromJSON(JSON.parse(JSON.stringify(opts.config)));
    this.cmdlib = opts.cmdlib ?? null;            // the stored /cmdlib.json text, or null (nothing stored)
    this.fragPaceMs = opts.fragPaceMs ?? 5;       // the firmware paces 150 ms (FRAG_PACING_MS); specs need not wait
    this.wcbDebug = !!opts.wcbDebug;              // the bridge WCB echoes our relay traffic (debug on)
    this.relayId = opts.relayId ?? 0;             // via-wcb: the bridge's own WCB number in WCB_STATUS "relay"
    this.relayName = opts.relayName || '';
    this.roster = opts.roster || null;            // { online:[], known:[], clients:[], temporary:[], aliases:[], portLabels:[], seqHash:[] }
    this.sparseStatus = !!opts.sparseStatus;      // via-wcb: answer GET_WCB_STATUS with rows[] (buildWcbStatus sparse)
    this.monitorHz = opts.monitorHz ?? 0;         // PWM_UPDATE frames while monitoring (the firmware: every 50 ms)
    this.pwmFrame = opts.pwmFrame || null;        // () => the PWM_UPDATE object to send
    this.seqs = opts.seqs || {};                  // { <wcb>: { <key>: <value> } } for GET_WCB_SEQ / SEQVAL
    this.mode_ = 1;                               // FunctionSwState
    this.rx = [];                                 // { t, line, json }
    this.opens = 0;
    this.signals = [];
    this.hold = new Set();
    this.held = [];
    this.delay = {};
    this.override = {};
    this.monitor = false;
    this.calib = false;
    this.debugFlags = 0;
    this._buf = Buffer.alloc(0);
    this._timers = new Set();
    this._frag = new Map();                       // sid -> { parts, got, total, expireAt }
    this._outSid = 1;
    this._outBusy = false;                        // one fragmented send at a time (_outSend)
    this._subscribedUntil = 0;                    // the bridge's rcJsonRelay window (WCB.ino: 20 s after a ;w20,{)
    this._pwmTimer = null;
    this.sink = () => {};
    this.port = null;                             // the FakeSerial in front of it (a restart errors a held port)
    this._otaInit(opts);                          // lib/navicore/ota.js: ?OTALOCAL, the ?OTA relay, the restart
    this._clipsInit(opts);                        // lib/navicore/clips.js: ?REC, the clip store, EDITLOAD / EDITEV
  }

  // ── transport (lib/navicore/shim.js FakeSerial) ──────────────────────────────────────────────────────────
  attach(sink, port) { this.sink = sink; this.port = port || null; }
  onOpen() { this.opens++; this._buf = Buffer.alloc(0); }
  onClose() { this._stopMonitor(); this._buf = Buffer.alloc(0); }
  onSignals(s) { this.signals.push({ t: Date.now(), ...s }); }
  onWrite(buf) {
    // handleSerialInput (NaviCore.ino:4287-4315): '\n' or '\r' ends a line, which is trimmed; empty lines vanish.
    this._buf = Buffer.concat([this._buf, buf]);
    let idx;
    while ((idx = this._buf.findIndex((c) => c === 0x0A || c === 0x0D)) !== -1) {
      const line = this._buf.subarray(0, idx).toString('utf8').trim();
      this._buf = this._buf.subarray(idx + 1);
      if (line) this._line(line);
    }
  }

  // Everything the page reads. Serial.println ends lines with CRLF.
  send(line) { this.sink(Buffer.from(line + '\r\n', 'utf8')); }
  inject(line) { this.send(line); }
  injectRaw(bytes) { this.sink(Buffer.from(bytes)); }
  later(ms, fn) {
    const h = setTimeout(() => { this._timers.delete(h); fn(); }, ms);
    this._timers.add(h);
    return h;
  }
  stop() {
    for (const h of this._timers) clearTimeout(h);
    this._timers.clear();
    this._stopMonitor();
    if (this._otaFlushTimer) { clearTimeout(this._otaFlushTimer); clearImmediate(this._otaFlushTimer); this._otaFlushTimer = null; }
  }
  release(type) {
    this.hold.delete(type);
    const keep = [];
    for (const h of this.held) { if (h.type === type) h.fn(); else keep.push(h); }
    this.held = keep;
  }

  // The request lines seen, parsed where they parse. `unwrap` strips ;w20, and joins fragment envelopes as the
  // mesh side sees them (via-wcb).
  requests(type) { return this.rx.filter((r) => r.json && r.json.type === type).map((r) => r.json); }
  lines(re) { return this.rx.filter((r) => re.test(r.line)).map((r) => r.line); }

  // ── dispatch ─────────────────────────────────────────────────────────────────────────────────────────────
  _line(line) {
    const rec = { t: Date.now(), line, json: null, via: null };
    this.rx.push(rec);
    if (this.mode === 'silent') return;
    if (this.booting) { rec.via = 'booting'; return; }   // restarting: setup() has not reached the serial loop
    if (this.mode === 'via-wcb') return this._bridgeLine(line, rec);
    if (this.mode === 'doorway') return this._doorwayLine(line, rec);
    return this._directLine(line, rec);
  }

  // Reply `lines` to a request of `type`, through hold / delay / override.
  _answer(type, msg, make, out = (l) => this.send(l)) {
    const fn = () => {
      const custom = this.override[type];
      const lines = custom ? custom(msg, this) : make();
      if (lines) for (const l of [].concat(lines)) out(l);
    };
    if (this.hold.has(type)) { this.held.push({ type, fn }); return; }
    const d = this.delay[type];
    if (d) this.later(d, fn); else setImmediate(fn);
  }

  // processInputLine (NaviCore.ino:3773-4282).
  _directLine(line, rec) {
    if (line === 'WCB_WEBTOOL_CONFIG_PULL') return;
    if (line.startsWith('?OTALOCAL,')) return this._otaLocalLine(line);   // execCliLine :3360, case-sensitive
    if (line[0] === '?' || line[0] === '#') return this._cli(line, (l) => this.send(l));
    if (line[0] !== '{') return;
    let msg;
    try { msg = JSON.parse(line); } catch (e) {
      this._answer('ERROR', null, () => `{"type":"ERROR","msg":"JSON parse failed (InvalidInput)","rxLen":${Buffer.byteLength(line)}}`);
      return;
    }
    rec.json = msg;
    this._usbJson(msg, line);
  }

  _usbJson(msg, line) {
    const type = typeof msg.type === 'string' ? msg.type : '';
    const ack = (extra = '') => `{"type":"ACK","ok":true${extra}}`;
    switch (type) {
      case 'PING': case 'ping':
        this.calib = false;   // :3835-3838: a fresh connect clears a stale calibration mute
        return this._answer('PING', msg, () => `{"type":"PONG","version":"${this.version}"}`);
      case 'GET_CONFIG':
        return this._answer('GET_CONFIG', msg, () => `{"type":"CONFIG","data":${this.config.text()}}`);
      case 'GET_CMDLIB': {
        const lib = this.cmdlib || '{"boards":[],"enums":{}}';
        return this._answer('GET_CMDLIB', msg, () =>
          `{"type":"CMDLIB","size":${Buffer.byteLength(lib)},"hash":${fnv1a(lib)},"data":${lib}}`);
      }
      case 'GET_CMDLIB_META': {
        const lib = this.cmdlib;
        return this._answer('GET_CMDLIB_META', msg, () =>
          `{"type":"CMDLIB_META","size":${lib ? Buffer.byteLength(lib) : 0},"hash":${lib ? fnv1a(lib) : 0}}`);
      }
      case 'SET_CMDLIB': {
        const lib = extractData(line);   // bracket-matched, not the last '}' (:3871-3902)
        let ok = false;
        if (lib) { this.cmdlib = lib; ok = true; }
        return this._answer('SET_CMDLIB', msg, () =>
          `{"type":"ACK","of":"SET_CMDLIB","ok":${ok},"size":${ok ? Buffer.byteLength(lib) : 0},"hash":${ok ? fnv1a(lib) : 0}}`);
      }
      case 'SET_CONFIG': {
        const saveId = Number.isInteger(msg.saveId) ? msg.saveId : 0;
        if (!msg.data || typeof msg.data !== 'object') {
          return this._answer('SET_CONFIG', msg, () =>
            `{"type":"ACK","of":"SET_CONFIG","ok":false,"msg":"missing data","saveId":${saveId}}`);
        }
        this.config.fromJSON(msg.data);
        return this._answer('SET_CONFIG', msg, () => `{"type":"ACK","of":"SET_CONFIG","ok":true,"saveId":${saveId}}`);
      }
      case 'START_MONITOR':
        this._startMonitor();
        return this._answer('START_MONITOR', msg, () => ack());
      case 'STOP_MONITOR':
        this._stopMonitor();
        this.calib = false;
        return this._answer('STOP_MONITOR', msg, () => ack());
      case 'CALIB':
        this.calib = !!msg.on;
        return this._answer('CALIB', msg, () => [
          `[CALIB] action dispatch ${this.calib ? 'SUPPRESSED (calibrating)' : 'resumed'}`, ack()]);
      case 'RESET_DEFAULTS':
        this.config.loadDefaults();   // RAM only, like the firmware (:3989-3992)
        return this._answer('RESET_DEFAULTS', msg, () => ack());
      case 'TEST_ACTION':
        return this._answer('TEST_ACTION', msg, () => `{"type":"ACK","of":"TEST_ACTION","ok":true}`);
      case 'REBOOT':
        return this._answer('REBOOT', msg, () => '{"type":"ACK","ok":true,"msg":"rebooting"}');
      case 'TRIGGER': {
        const mode = Number.isInteger(msg.mode) ? msg.mode : 1, btn = Number.isInteger(msg.btn) ? msg.btn : 0;
        const tap = Number.isInteger(msg.tap) ? msg.tap : 1;
        if (btn < 1 || btn > 36 || mode < 1 || mode > 3 || tap < 1 || tap > 4) {
          return this._answer('TRIGGER', msg, () => '{"type":"ACK","ok":false,"msg":"bad mode/btn/tap"}');
        }
        return this._answer('TRIGGER', msg, () => [`[TRIGGER] mode=${mode} btn=${btn} tap=${tap}`, ack()]);
      }
      case 'WCB_SEND':
        return this._answer('WCB_SEND', msg, () => ack());
      case 'FORGET_PEER': {
        const id = Number.isInteger(msg.id) ? msg.id : 0;
        if (msg.all === true) return this._answer('FORGET_PEER', msg, () => '{"type":"ACK","of":"FORGET_PEER","ok":true,"all":true}');
        if (id >= 1 && id <= 20) {
          this._forget(id);
          return this._answer('FORGET_PEER', msg, () => `{"type":"ACK","of":"FORGET_PEER","ok":true,"id":${id}}`);
        }
        return this._answer('FORGET_PEER', msg, () =>
          `{"type":"ACK","of":"FORGET_PEER","ok":false,"msg":"id ${id} out of range (1-20) or set all:true"}`);
      }
      case 'SET_DEBUG_FLAGS': {
        this.debugFlags = Number.isInteger(msg.flags) ? msg.flags >>> 0 : 0;
        const hex = this.debugFlags.toString(16).toUpperCase().padStart(2, '0');
        return this._answer('SET_DEBUG_FLAGS', msg, () => [`[DBG] flags=0x${hex}`, ack()]);
      }
      case 'GET_WCB_SEQ': case 'GET_WCB_SEQVAL':
        return this._answer(type, msg, () => this._seqReply(type, msg, false));
      case 'GET_WCB_STATUS':
        return this._answer('GET_WCB_STATUS', msg, () => this._wcbStatusUsb());
      case 'RESET_MESH_STATS':
        return this._answer('RESET_MESH_STATS', msg, () => '{"type":"ACK","of":"RESET_MESH_STATS","ok":true}');
      case 'GET_MESH_STATS':
        return this._answer('GET_MESH_STATS', msg, () => this._meshStats(false));
      default:
        return this._answer(type || 'unknown', msg, () => '{"type":"ERROR","msg":"unknown type"}');
    }
  }

  // ── the NaviCore CLI (execCliLine) ─────────────────────────────────────────────────────────────────────────
  // A spec's `cli(line, out, emu)` hook answers first (return true when it handled the line), then the built-in
  // verbs. Replies are deferred like every JSON reply, so they leave in the order the lines arrived: the firmware
  // handles one line at a time and prints its answer before reading the next.
  _cli(line, out0) {
    const out = (l) => setImmediate(() => out0(l));
    if (this.cli && this.cli(line, out, this)) return;
    if (this._builtinCli(line, out)) return;
    if (line[0] === '?') out(`Unknown command: ${line}`);
  }
  _builtinCli(line, out) {
    // ?REC,... (NaviCore.ino:3444: a case-insensitive "?REC" prefix) — lib/navicore/clips.js.
    if (/^\?REC/i.test(line)) { this._recCli(line, out, !!this._relayedCli); return true; }
    // ?MAE,GET,<slot>,<ch> / MOVING,<slot> / ERR,<slot> / <slot>,<ch>,<pos> (NaviCore.ino:3396-3439): a query answers
    // with a [MAE:<slot>]{...} marker (maestroReportQuery :741-755); a remote slot's answer comes later, off the mesh.
    const m = /^\?MAE,(\w+)(?:,(\d+))?(?:,(\d+))?(?:,(\d+))?$/i.exec(line);
    if (!m) return false;
    const verb = m[1].toUpperCase();
    this.mae = this.mae || {};
    if (/^\d+$/.test(m[1])) {                    // set target: no reply
      const slot = +m[1], ch = +(m[2] || 0), pos = +(m[3] || 0);
      (this.mae[slot] = this.mae[slot] || {})[ch] = pos;
      return true;
    }
    const slot = +(m[2] || 0), ch = +(m[3] || 0);
    const kind = verb === 'GET' ? 'pos' : verb === 'MOVING' ? 'mov' : verb === 'ERR' ? 'err' : null;
    if (!kind) return true;                      // FREE: no reply
    const type = (this.config.s.maestros[slot - 1] || {}).type || 0;
    const err = type === 0 ? 'disabled' : (this.maeTimeout && this.maeTimeout.has(slot)) ? 'timeout' : null;
    const val = kind === 'pos' ? ((this.mae[slot] || {})[ch] ?? 6000) : 0;
    const body = err ? (kind === 'pos' ? `{"q":"pos","ch":${ch},"err":"${err}"}` : `{"q":"${kind}","err":"${err}"}`)
                     : (kind === 'pos' ? `{"q":"pos","ch":${ch},"val":${val}}` : `{"q":"${kind}","val":${val}}`);
    if (type === 2 && !err) this.later(40, () => out(`[MAE:${slot}]${body}`));   // the hosting WCB's :MQR, relayed
    else out(`[MAE:${slot}]${body}`);
    return true;
  }

  // ── via-wcb: the tethered WCB, and NaviCore behind it on the mesh ─────────────────────────────────────────
  _bridgeLine(line, rec) {
    const m = /^;w(\d+),([\s\S]*)$/i.exec(line);
    if (!m) { this._wcbOwn(line); return; }
    const target = +m[1], payload = m[2];
    if (this.wcbDebug) {
      this.send(`Processing input from Serial0: ${line}`);
      if (target === RC_ID) this.send(`Sending Unicast ESP-NOW message to WCB${target}: ${payload}`);
    }
    if (target !== RC_ID) return;
    rec.via = payload;
    if (this.meshDown) return;                      // NaviCore is restarting: nothing on the mesh answers
    if (payload[0] === '{') {
      this._subscribedUntil = Date.now() + 20000;   // WCB.ino: a ;w20,{json} opens the ~20 s relay window
      let msg;
      try { msg = JSON.parse(payload); } catch (_) { return; }
      rec.json = msg;
      this._meshJson(msg, payload, (l) => this._relay(l));
      return;
    }
    // A text command for NaviCore's remote terminal: every line it prints comes back as [TERM:20]<line>.
    // The capture sink is armed for this one command (rcSerial.captureArmed(): EDITLOAD's relay caps key off it), and
    // each line goes back in 160-byte RTERM packets (clips.js _termOut).
    this._relayedCli = true;
    try { this._cli(payload, (l) => this._termOut(l)); } finally { this._relayedCli = false; }
  }

  // What the bridge prints of a mesh JSON line: only inside its relay window (rcJsonRelaySubscribed), and not while it
  // forwards an OTA (otaRelayForwarding, WCB.ino:395-402, :5454, :5762).
  _relay(line) {
    if (line[0] === '{' && (Date.now() >= this._subscribedUntil || Date.now() < this._otaForwardUntil)) return;
    this.send(line);
  }

  // The bridge WCB's own console: its ?OTA relay (lib/navicore/ota.js), then whatever a spec's wcbConsole answers.
  _wcbOwn(line) {
    if (/^\?OTA,/i.test(line)) return this._otaRelayLine(line);
    if (this.wcbConsole) { const r = this.wcbConsole(line, this); if (r) for (const l of [].concat(r)) this.send(l); return; }
    if (/^\?/.test(line)) this.send(`WCB: ${line.slice(1)} ok`);
  }

  // doorway: a WCB in front of the mesh. Bare JSON goes to NaviCore and comes back relayed, an explicit ;w20, is
  // relayed as over any bridge, and '?' is the WCB's own console (doorwayCli).
  _doorwayLine(line, rec) {
    if (/^;w/i.test(line)) return this._bridgeLine(line, rec);
    if (line[0] === '{') {
      let msg;
      try { msg = JSON.parse(line); } catch (_) { return; }
      rec.json = msg;
      this._subscribedUntil = Date.now() + 20000;
      this._meshJson(msg, line, (l) => this._relay(l));
      return;
    }
    if (line[0] === '?') { const r = this.doorwayCli && this.doorwayCli(line, this); if (r) for (const l of [].concat(r)) this.send(l); }
  }

  // rcTelemetry::handle (rc_telemetry.h:2037-2460) for one JSON payload from the tool, and _applyReassembled.
  _meshJson(msg, raw, out) {
    if (typeof msg.f === 'number' && typeof msg.of === 'number' && typeof msg.sid === 'number') {
      return this._meshFragment(msg, out);
    }
    const type = typeof msg.type === 'string' ? msg.type : '';
    if (!type) return;
    const id = this.config.s.wcbNetwork.deviceId;
    const reply = (t, make) => this._answer(t, msg, make, out);
    switch (type) {
      case 'PING':
        return reply('PING', () => `{"sys":1,"type":"PONG","id":${id},"version":"${this.version}","model":${this.config.s.txModel},"mode":${this.mode_}}`);
      case 'GET_CONFIG':
        return reply('GET_CONFIG', () => { this._fragSend(`{"type":"CONFIG","id":${id},"data":${this.config.text()}}`, out); return null; });
      case 'GET_CMDLIB': {
        const lib = this.cmdlib || '{"boards":[],"enums":{}}';
        return reply('GET_CMDLIB', () => {
          this._fragSend(`{"type":"CMDLIB","size":${Buffer.byteLength(lib)},"hash":${fnv1a(lib)},"data":${lib}}`, out);
          return null;
        });
      }
      case 'GET_CMDLIB_META': {
        const lib = this.cmdlib;
        return reply('GET_CMDLIB_META', () =>
          `{"sys":1,"type":"CMDLIB_META","size":${lib ? Buffer.byteLength(lib) : 0},"hash":${lib ? fnv1a(lib) : 0}}`);
      }
      case 'GET_WCB_META':
        return reply('GET_WCB_META', () => { this._fragSend(this._wcbMeta(), out); return null; });
      case 'GET_WCB_SEQ': case 'GET_WCB_SEQVAL':
        return reply(type, () => this._seqReply(type, msg, true));
      case 'SET_CONFIG': case 'SET_CMDLIB': case 'TEST_ACTION':
        return this._applyReassembled(raw, out);
      case 'GET_WCB_STATUS':
        return reply('GET_WCB_STATUS', () => this._wcbStatusBridged());
      case 'GET_MESH_STATS':
        return reply('GET_MESH_STATS', () => this._meshStats(true));
      case 'START_MONITOR': case 'STOP_MONITOR': case 'SET_DEBUG_FLAGS': case 'CALIB': case 'RESET_MESH_STATS':
        return;   // accepted silently over the mesh (:2399-2403)
      case 'FORGET_PEER':
        if (Number.isInteger(msg.id)) this._forget(msg.id);
        return;
      case 'WCB_SEND':
        return reply('WCB_SEND', () => '{"sys":1,"type":"ACK","of":"WCB_SEND","ok":true}');
      case 'RESET_DEFAULTS':
        this.config.loadDefaults();
        return reply('RESET_DEFAULTS', () => '{"sys":1,"type":"ACK","of":"RESET_DEFAULTS","ok":true}');
      case 'TRIGGER': {
        const mode = msg.mode | 0, btn = msg.btn | 0;
        let tap = Number.isInteger(msg.tap) ? msg.tap : 1;
        if (mode >= 1 && mode <= 3 && btn >= 1 && btn <= 36) {
          tap = Math.max(1, Math.min(4, tap));
          return reply('TRIGGER', () => `{"sys":1,"type":"rc_trig","id":${id},"mode":${mode},"btn":${btn},"tap":${tap}}`);
        }
        return;
      }
      case 'SET_MODE': {
        const mode = msg.mode | 0;
        if (mode >= 1 && mode <= 3 && mode !== this.mode_) {
          this.mode_ = mode;
          return reply('SET_MODE', () => `{"sys":1,"type":"rc_mode","id":${id},"mode":${mode}}`);
        }
        return;
      }
      default:
        return;
    }
  }

  // The receive pool (rc_telemetry.h:2037-2148): FRAG_MAX_PARTS parts, an idle timeout refreshed by every fragment
  // of the session (duplicates included), reassembled only when every part is present.
  _meshFragment(msg, out) {
    const now = Date.now();
    for (const [sid, s] of this._frag) if (now >= s.expireAt) this._frag.delete(sid);
    const { f, of, sid } = msg;
    if (!Number.isInteger(of) || of < 1 || of > FRAG_MAX_PARTS || !Number.isInteger(f) || f < 1 || f > of) return;
    let s = this._frag.get(sid);
    if (s && s.total !== of) { this._frag.delete(sid); s = null; }
    if (!s) { s = { parts: new Array(of), got: 0, total: of, expireAt: 0, sid }; this._frag.set(sid, s); }
    if (s.parts[f - 1] === undefined) { s.parts[f - 1] = typeof msg.s === 'string' ? msg.s : ''; s.got++; }
    s.expireAt = now + FRAG_TIMEOUT_MS;
    this.fragLog = this.fragLog || [];
    this.fragLog.push({ t: now, sid, f, of });
    if (s.got >= s.total && s.parts.every((p) => p !== undefined)) {
      this._frag.delete(sid);
      const full = s.parts.join('');
      this.reassembled = this.reassembled || [];
      this.reassembled.push(full);
      this._applyReassembled(full, out);
    }
  }

  // _applyReassembled (:1097-1230): the SET_CONFIG strip, then the apply and the sys ACK.
  _applyReassembled(json, out) {
    let doc;
    try { doc = JSON.parse(json); } catch (_) { return; }
    const type = typeof doc.type === 'string' ? doc.type : '';
    if (type === 'SET_CONFIG') {
      if (!doc.data || typeof doc.data !== 'object' || Array.isArray(doc.data)) return;
      const data = doc.data;
      if (data.wcbNetwork && typeof data.wcbNetwork === 'object') {
        for (const k of ['deviceId', 'macOct2', 'macOct3', 'password', 'quantity']) delete data.wcbNetwork[k];
        if (Object.keys(data.wcbNetwork).length === 0) delete data.wcbNetwork;
      }
      this.config.fromJSON(data);
      const saveId = Number.isInteger(doc.saveId) ? doc.saveId : 0;
      this.bridgedSaves = (this.bridgedSaves || 0) + 1;
      return this._answer('SET_CONFIG', doc,
        () => `{"sys":1,"type":"ACK","of":"SET_CONFIG","id":${this.config.s.wcbNetwork.deviceId},"ok":true,"saveId":${saveId}}`, out);
    }
    if (type === 'SET_CMDLIB') {
      const k = json.indexOf('"data":'), end = json.lastIndexOf('}');   // the mesh path still slices to the last '}'
      let ok = false, lib = '';
      if (k >= 0 && end > k + 7) { lib = json.substring(k + 7, end).trim(); if (lib) { this.cmdlib = lib; ok = true; } }
      return this._answer('SET_CMDLIB', doc,
        () => `{"sys":1,"type":"ACK","of":"SET_CMDLIB","ok":${ok},"size":${ok ? Buffer.byteLength(lib) : 0},"hash":${ok ? fnv1a(lib) : 0}}`, out);
    }
    if (type === 'TEST_ACTION') {
      return this._answer('TEST_ACTION', doc, () => '{"sys":1,"type":"ACK","of":"TEST_ACTION","ok":true}', out);
    }
  }

  // Send a wrapped payload as fragment envelopes, paced (the pump in tick(), one at a time: _outSend).
  _fragSend(wrapped, out) {
    const slices = fragSlices(wrapped);
    if (!slices) return false;
    const sid = this._outSid++;
    if (this._outSid > 65535) this._outSid = 1;
    this.fragSends = this.fragSends || [];
    this.fragSends.push({ sid, total: slices.length, wrapped });
    slices.forEach((s, i) => {
      const env = JSON.stringify({ f: i + 1, of: slices.length, sid, s });
      if (Buffer.byteLength(env) > MAX_ENV_BYTES) throw new Error(`emulator fragment ${i + 1} is ${Buffer.byteLength(env)} B`);
      this.later(i * this.fragPaceMs, () => out(env));
    });
    return true;
  }

  // ── rosters ───────────────────────────────────────────────────────────────────────────────────────────────
  _roster() {
    const q = Math.max(0, Math.min(20, this.config.s.wcbNetwork.quantity));
    const r = this.roster || {};
    const hi = Math.max(q, (r.known || []).length, (r.online || []).length);
    const pick = (a, i, d) => (Array.isArray(a) && i < a.length ? a[i] : d);
    const rows = [];
    for (let i = 1; i <= hi; i++) {
      rows.push({
        id: i,
        online: i === this.config.s.wcbNetwork.deviceId ? 1 : +!!pick(r.online, i - 1, 0),
        known: +!!pick(r.known, i - 1, i <= q ? 1 : 0),
        client: +!!pick(r.clients, i - 1, 0),
        temporary: +!!pick(r.temporary, i - 1, 0),
        alias: String(pick(r.aliases, i - 1, '')),
        labels: pick(r.portLabels, i - 1, ['', '', '', '', '']),
        seqHash: pick(r.seqHash, i - 1, 0) >>> 0,
      });
    }
    return { q, rows };
  }
  _forget(id) {
    if (!this.roster || !Array.isArray(this.roster.known)) return;
    if (id >= 1 && id - 1 < this.roster.known.length && id > this.config.s.wcbNetwork.quantity) this.roster.known[id - 1] = 0;
  }
  // The USB WCB_STATUS (NaviCore.ino:4089-4190): dense arrays, aliases, port labels and seqHash inline.
  _wcbStatusUsb() {
    const { q, rows } = this._roster();
    const self = this.config.s.wcbNetwork.deviceId;
    const j = (f) => rows.map(f).join(',');
    return `{"type":"WCB_STATUS","quantity":${q},"self":${self},"online":[${j((r) => r.online)}],` +
      `"known":[${j((r) => r.known)}],"clients":[${j((r) => r.client)}],"temporary":[${j((r) => r.temporary)}],` +
      `"aliases":[${j((r) => JSON.stringify(r.id === self ? '' : r.alias))}],` +
      `"portLabels":[${j((r) => JSON.stringify(r.client ? ['', '', '', '', ''] : r.labels))}],` +
      `"seqHash":[${j((r) => (r.client ? 0 : r.seqHash))}]}`;
  }
  // The bridged WCB_STATUS (buildWcbStatus, rc_telemetry.h:1879-1985): no aliases / labels, the relay named apart.
  _wcbStatusBridged() {
    const { q, rows } = this._roster();
    const self = this.config.s.wcbNetwork.deviceId;
    const relay = this.relayId;
    const shown = rows.filter((r) => r.id !== relay || r.id <= q);
    let s = `{"sys":1,"type":"WCB_STATUS","quantity":${q},"self":${self},"relay":${relay}`;
    if (relay >= 1 && this.relayName) s += `,"relayName":"${this.relayName}"`;
    if (this.sparseStatus) {
      const rs = shown.filter((r) => r.known).map((r) => `[${r.id},${r.online},${r.client},${r.temporary}]`);
      return `${s},"rows":[${rs.join(',')}]}`;
    }
    const j = (f) => shown.map(f).join(',');
    return `${s},"online":[${j((r) => r.online)}],"known":[${j((r) => r.known)}],"clients":[${j((r) => r.client)}]}`;
  }
  // buildWcbMeta (:1986-2030), sent fragmented.
  _wcbMeta() {
    const { rows } = this._roster();
    const self = this.config.s.wcbNetwork.deviceId;
    const j = (f) => rows.map(f).join(',');
    return `{"sys":1,"type":"WCB_META","aliases":[${j((r) => JSON.stringify(r.id === self ? '' : r.alias))}],` +
      `"portLabels":[${j((r) => JSON.stringify(r.client ? ['', '', '', '', ''] : r.labels))}],` +
      `"seqHash":[${j((r) => (r.client ? 0 : r.seqHash))}]}`;
  }
  _seqReply(type, msg, sys) {
    const n = Number.isInteger(msg.wcb) ? msg.wcb : 0;
    const pre = sys ? '{"sys":1,' : '{';
    const store = this.seqs[n];
    if (type === 'GET_WCB_SEQ') {
      if (!store) return `${pre}"type":"WCB_SEQ","ok":false,"wcb":${n},"msg":"timeout"}`;
      return `${pre}"type":"WCB_SEQ","ok":true,"wcb":${n},"hash":${fnv1a(JSON.stringify(store))},"names":${JSON.stringify(Object.keys(store))}}`;
    }
    const key = typeof msg.key === 'string' ? msg.key : '';
    if (!store) return `${pre}"type":"WCB_SEQVAL","ok":false,"wcb":${n},"key":${JSON.stringify(key)},"msg":"timeout"}`;
    if (!(key in store)) return `${pre}"type":"WCB_SEQVAL","ok":true,"wcb":${n},"key":${JSON.stringify(key)},"status":1,"value":""}`;
    return `${pre}"type":"WCB_SEQVAL","ok":true,"wcb":${n},"key":${JSON.stringify(key)},"status":0,"value":${JSON.stringify(store[key])}}`;
  }
  _meshStats(sys) {
    const custom = this.meshStats;
    if (custom) return custom(sys, this);
    const pre = sys ? '{"sys":1,' : '{';
    return `${pre}"type":"MESH_STATS","pg":0,"self":${this.config.s.wcbNetwork.deviceId},"upMs":123456,` +
      '"sent":0,"ackd":0,"retries":0,"failed":0,"unguaranteed":0,"bcast":0,"recv":0,"peers":[],"last":1}';
  }

  // ── the live monitor (sendPWMUpdate, NaviCore.ino:3119-3160) ───────────────────────────────────────────────
  _startMonitor() {
    this.monitor = true;
    if (!this.monitorHz || this._pwmTimer) return;
    this._pwmTimer = setInterval(() => {
      if (!this.monitor) return;
      this.send(JSON.stringify(this.pwmFrame ? this.pwmFrame(this) : this.defaultPwmFrame()));
    }, Math.round(1000 / this.monitorHz));
  }
  _stopMonitor() {
    this.monitor = false;
    if (this._pwmTimer) { clearInterval(this._pwmTimer); this._pwmTimer = null; }
  }
  defaultPwmFrame() {
    const c = this.config.s;
    const modeCh = c.modeSwitch >= 0 && c.modeSwitch < 10 ? c.switches[c.modeSwitch].channel : 0;
    const channels = Array.from({ length: 16 }, () => 992);
    return { type: 'PWM_UPDATE', matrixCh: c.matrixChannel, modeCh, matrixVal: 992, modeVal: 172, btn: 0, mode: this.mode_,
             sbus: { ok: true, fps: 111, frames: 1000, ageMs: 4, lost: false, failsafe: false, chCount: 16, frameLen: 25, channels } };
  }
}

// SET_CMDLIB's data value, found by bracket matching from the first "data": (NaviCore.ino:3871-3902).
function extractData(line) {
  const k = line.indexOf('"data":');
  if (k < 0) return null;
  let s = k + 7;
  while (s < line.length && /\s/.test(line[s])) s++;
  const open = line[s];
  if (open !== '{' && open !== '[') return null;
  const close = open === '{' ? '}' : ']';
  let depth = 0, inStr = false, esc = false;
  for (let i = s; i < line.length; i++) {
    const c = line[i];
    if (esc) { esc = false; continue; }
    if (inStr) { if (c === '\\') esc = true; else if (c === '"') inStr = false; continue; }
    if (c === '"') { inStr = true; continue; }
    if (c === open) { depth++; continue; }
    if (c === close && --depth === 0) return line.substring(s, i + 1).trim() || null;
  }
  return null;
}

Object.assign(NaviEmulator.prototype, require('./ota').methods, require('./clips').methods);

module.exports = { NaviEmulator, fnv1a, fragSlices, extractData, strl, RC_ID, FRAG_MAX_PARTS };
