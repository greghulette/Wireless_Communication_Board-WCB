// Firmware OTA for the emulator (lib/navicore/emulator.js): what NaviCore does with ?OTALOCAL on a direct link, and
// what a tethered WCB with NaviCore behind it does with the tool's ?OTA relay lines in via-wcb mode. Each branch names
// the code it copies: NaviCore's navicore_ota.h (working tree, 2026-09-28) and this repo's Code/WCB/WCB_OTA.cpp.
//
// Direct (Transport A, navicore_ota.h:244-305): STATUS; BEGIN prints "\n[OTA:BEGIN,START]\n" before the blocking
// slot erase, then [OTA:BEGIN,OK|ERR,<cursor>]; DATA carries at most 1024 decoded bytes (:106) and is written only at
// the cursor: [OTA:ACK,<cursor>], else [OTA:NAK,<cursor>], and a line whose base64 does not decode gets no marker at
// all (:278-281); END prints [OTA:END,OK] and restarts 2 s later (:293-300), or [OTA:END,ERR]; ABORT. The cursor reads
// 0 once no session is active (:119).
// Via WCB (Transport B): the relay parses ?OTA,<SUB>,<target>,<session>,... (WCB_OTA.cpp:486-593), drops a DATA line
// whose "<offset>:<crc32>" suffix does not match (:545-557) and forwards the rest to NaviCore (mesh id 20), whose
// handlers (navicore_ota.h:346-411) answer every packet with its cursor; the relay prints each answer as
// [OTA:ACK,<src>,<session>,<offset>,<status>] (WCB_OTA.cpp:466-480). END's answer carries offset 0 (navicore_ota.h
// :393-403) and a verified END is answered three more times, 80 ms apart, before the target restarts (:404-410). While
// it forwards, the relay pauses the RC-JSON passthrough until 8 s after the last ?OTA line (WCB.ino:395-402, :6607-6613).
//
// emu.otaFault bends it; every Set entry fires once and is then removed:
//   lostData   DATA offsets whose line is lost on the wire (it never arrives)
//   lostAck    cursors whose ACK is lost (the write happened)
//   mangle     DATA offsets whose line loses 4 base64 characters mid-line: still valid base64, wrong bytes, which is
//              what a serial RX overflow does and what the relay's CRC exists to catch (WCB_OTA.cpp:526-543)
//   coalesce   n: markers leave n to a chunk (one read on the page side)
//   split      true: the first ACK leaves in two chunks, cut mid-marker
//   stale      lines printed when END arrives, before its verdict (late cursor ACKs from the in-flight tail)
//   foreign    lines printed with the first DATA answer (another target's or another session's ACKs)
//              (stale and foreign may be functions of the session id, which the tool picks at run time)
//   holdFrom   a cursor: that ACK and everything after it are held until emu.otaRelease()
//   verify     false: esp_ota_end's image check fails (:196-201)
//   beginErr   true: esp_ota_begin fails (:154)
// A verified END restarts the board (emu.otaReboot = { afterMs, goneMs, bootMs }): gone from USB for goneMs (open()
// fails, a held port errors "The device has been lost."), deaf for bootMs while it boots, then back as
// emu.otaNewVersion from the other slot. In via-wcb mode only NaviCore restarts: the tethered relay stays.

const SLOT_SIZE = 0x1E0000;                 // partitions.csv: app0 / app1
const SLOTS = { app0: 0x10000, app1: 0x1F0000 };
const OTA_LOCAL_SESSION = 1;                // navicore_ota.h:105
const OTA_LOCAL_MAX_CHUNK = 1024;           // :106
const OTA_ESPNOW_PAYLOAD = 192;             // :59
const OTA_PROGRESS_STEP = 65536;            // :107
const OTA_ST_OK = 0, OTA_ST_ERR = 1;        // :60-61
const RC_ID = 20;
const MAX_WCB_COUNT = 20;                   // WCB_OTA.cpp:500 (the relay's target range)

// calculateCRC32 (WCB.ino) / otaCrc32 (navicore_ota.h:315-323): reflected, poly 0xEDB88320, over the string's bytes.
function crc32(str) {
  let crc = 0xFFFFFFFF;
  for (const b of Buffer.from(str, 'latin1')) {
    crc ^= b;
    for (let j = 0; j < 8; j++) crc = (crc >>> 1) ^ (0xEDB88320 & -(crc & 1));
  }
  return (~crc) >>> 0;
}

// mbedtls_base64_decode into a buffer of `max` bytes: an invalid character, or '=' anywhere but the end, is
// MBEDTLS_ERR_BASE64_INVALID_CHARACTER (-0x2C); more output than fits is MBEDTLS_ERR_BASE64_BUFFER_TOO_SMALL (-0x2A).
function b64decode(s, max) {
  if (s.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(s)) return { rc: -44 };
  const buf = Buffer.from(s, 'base64');
  if (buf.length > max) return { rc: -42 };
  return { rc: 0, buf };
}

// Arduino String::toInt(): atol, leading whitespace skipped, stops at the first non-digit, 0 when there is none.
function toInt(s) { const n = parseInt(String(s).trim(), 10); return Number.isFinite(n) ? n : 0; }

const hex6 = (n) => n.toString(16).padStart(6, '0');

const methods = {
  _otaInit(opts) {
    this.otaFault = {};
    this.otaReboot = { afterMs: 2000, goneMs: 1200, bootMs: 600, ...(opts.otaReboot || {}) };
    this.otaNewVersion = opts.otaNewVersion || null;
    this.otaEraseMs = opts.otaEraseMs ?? 30;
    this.otaHopMs = opts.otaHopMs ?? 1;       // one ESP-NOW hop, relay <-> NaviCore
    this.ota = { active: false, session: 0, size: 0, written: 0, parts: [], lastProgress: 0 };
    this.otaSlot = { running: 'app0', next: 'app1' };
    this.otaBootNext = false;
    this.otaImage = null;                     // the last image a verified END accepted
    // STATUS's 'App SHA256:' (otaAppSha16, navicore_ota.h:238-248): the first 8 bytes of the running image's ELF SHA-256,
    // which elf2image stamps at 0xB0 of the app image; after a verified END, the new image's (see _restart).
    this.appSha = opts.appSha || '5a17c0de00c0ffee';
    this.otaLog = [];                         // { op, ... } for the specs: data / lost / mangled / crcDrop / nak / ack ...
    this.present = true;                      // on USB (shim.js FakeSerial refuses open() and writes while false)
    this.booting = false;                     // restarting: lines arrive and are ignored
    this.meshDown = false;                    // via-wcb: NaviCore restarting behind the relay
    this.reboots = 0;
    this._otaOutBuf = [];
    this._otaOutMarkers = 0;
    this._otaFlushTimer = null;
    this._otaHeld = null;
    this._otaForwardUntil = 0;
  },

  _otaFire(name, key) {
    const s = this.otaFault[name];
    if (s instanceof Set && s.has(key)) { s.delete(key); return true; }
    return false;
  },
  // otaFault.stale / .foreign: a list of lines, or a function of the session id (the tool picks it at run time).
  _otaFaultLines(name, session) {
    const v = this.otaFault[name];
    return [].concat(typeof v === 'function' ? v(session) : (v || []));
  },

  // OTA output keeps its order with a coalescing buffer: each command's lines leave together on the next tick, or
  // `coalesce` markers to a chunk.
  _otaPrint(line, marker = false) {
    if (this._otaHeld) { this._otaHeld.push(line); return; }
    if (marker && this.otaFault.holdFrom !== undefined) {
      const m = /^\[OTA:(?:ACK|NAK),(?:\d+,\d+,)?(\d+)/.exec(line);
      if (m && +m[1] >= this.otaFault.holdFrom) { this._otaHeld = [line]; return; }
    }
    this._otaOutBuf.push(line);
    if (marker) this._otaOutMarkers++;
    const n = this.otaFault.coalesce || 0;
    if (n > 1 && this._otaOutMarkers >= n) return this._otaFlush();
    if (!this._otaFlushTimer) {
      this._otaFlushTimer = n > 1 ? setTimeout(() => this._otaFlush(), 15) : setImmediate(() => this._otaFlush());
    }
  },
  _otaFlush() {
    if (this._otaFlushTimer) { clearTimeout(this._otaFlushTimer); clearImmediate(this._otaFlushTimer); this._otaFlushTimer = null; }
    if (!this._otaOutBuf.length) return;
    const text = this._otaOutBuf.map((l) => l + '\r\n').join('');
    this._otaOutBuf = [];
    this._otaOutMarkers = 0;
    const cut = this.otaFault.split ? text.indexOf('[OTA:ACK') : -1;
    if (cut >= 0) {
      this.otaFault.split = false;
      this.injectRaw(Buffer.from(text.slice(0, cut + 7), 'utf8'));
      this.injectRaw(Buffer.from(text.slice(cut + 7), 'utf8'));
      return;
    }
    this.injectRaw(Buffer.from(text, 'utf8'));
  },
  otaRelease() {
    const held = this._otaHeld || [];
    this._otaHeld = null;
    delete this.otaFault.holdFrom;
    for (const l of held) this._otaPrint(l, /^\[OTA:/.test(l));
  },

  // NaviCore's own Serial: on its USB in direct mode, nowhere the tool can see in via-wcb mode (the relay prints
  // only its own lines and the ACKs).
  _otaSay(line) { if (this.mode !== 'via-wcb') this._otaPrint(line); },

  // ── the transport-agnostic core (navicore_ota.h:139-224) ─────────────────────────────────────────────────
  _otaCursor() { return this.ota.active ? this.ota.written : 0; },
  _otaTeardown() { this.ota = { active: false, session: 0, size: 0, written: 0, parts: [], lastProgress: 0 }; },
  _otaBegin(session, size, family) {
    if (this.ota.active) this._otaTeardown();
    if (family !== 1) {
      this._otaSay(`[OTA] BEGIN rejected: image chip family ${family} != this board 1 (brick guard)`);
      return false;
    }
    const next = this.otaSlot.next;
    if (size === 0 || size > SLOT_SIZE) {
      this._otaSay(`[OTA] BEGIN rejected: image ${size} B exceeds partition '${next}' (${SLOT_SIZE} B)`);
      return false;
    }
    if (this.otaFault.beginErr) { this._otaSay('[OTA] esp_ota_begin failed: ESP_ERR_FLASH_OP_FAIL'); return false; }
    this.ota = { active: true, session, size, written: 0, parts: [], lastProgress: 0 };
    this._otaSay(`[OTA] BEGIN ok: session ${session}, ${size} B -> partition '${next}' @0x${hex6(SLOTS[next])} (${SLOT_SIZE} B)`);
    return true;
  },
  _otaWrite(session, offset, buf) {
    const o = this.ota;
    if (!o.active || session !== o.session) return false;
    if (offset !== o.written) return false;               // gap/dup -> caller rewinds to the cursor
    if (o.written + buf.length > o.size) {
      this._otaSay(`[OTA] write overruns image (${o.written} + ${buf.length} > ${o.size}) — aborting`);
      this._otaTeardown();
      return false;
    }
    o.parts.push(Buffer.from(buf));
    o.written += buf.length;
    if (o.written - o.lastProgress >= OTA_PROGRESS_STEP || o.written === o.size) {
      o.lastProgress = o.written;
      this._otaSay(`[OTA] ${o.written} / ${o.size} B (${Math.floor(o.written * 100 / o.size)}%)`);
    }
    return true;
  },
  _otaEnd(session) {
    const o = this.ota;
    if (!o.active || session !== o.session) { this._otaSay('[OTA] END: no matching active session'); return false; }
    if (o.written !== o.size) {
      this._otaSay(`[OTA] END rejected: incomplete ${o.written} / ${o.size} B`);
      this._otaTeardown();
      return false;
    }
    const image = Buffer.concat(o.parts);
    const v = this.otaFault.verify;
    const ok = typeof v === 'function' ? !!v(image) : v !== false;
    this._otaTeardown();                                  // ota.active is false on every path from here
    if (!ok) { this._otaSay('[OTA] END verify FAILED: ESP_ERR_OTA_VALIDATE_FAILED (image rejected, current app intact)'); return false; }
    this.otaImage = image;
    this.otaBootNext = true;
    this._otaSay(`[OTA] END ok: verified ${image.length} B -> next boot '${this.otaSlot.next}'`);
    return true;
  },
  _otaAbort(reason) {
    if (this.ota.active) this._otaSay(`[OTA] aborted: ${reason} (current app intact)`);
    this._otaTeardown();
  },
  // otaPrintStatus (navicore_ota.h:250-263).
  _otaStatus() {
    const s = this.otaSlot;
    const lines = [
      '---------- OTA Status ----------',
      'Chip:        ESP32-S3 (family 1)',
      `Firmware:    ${this.version}`,
      `App SHA256:  ${this.appSha}`,
      `Running:     '${s.running}' @0x${hex6(SLOTS[s.running])} (${SLOT_SIZE} B)`,
      `Next (OTA):  '${s.next}' @0x${hex6(SLOTS[s.next])} (${SLOT_SIZE} B)`,
      this.ota.active ? `Session:     ACTIVE id=${this.ota.session}  ${this.ota.written} / ${this.ota.size} B` : 'Session:     idle',
      '--------------------------------',
    ];
    for (const l of lines) this._otaSay(l);
  },

  // ESP.restart(): RAM state is gone; the boot slot flips if an END was verified.
  _restart() {
    this.reboots++;
    this._otaTeardown();
    this._stopMonitor();
    this.calib = false;
    this.debugFlags = 0;
    this._frag.clear();
    this._subscribedUntil = 0;
    this._buf = Buffer.alloc(0);
    if (this.otaBootNext) {
      this.otaBootNext = false;
      this.otaSlot = { running: this.otaSlot.next, next: this.otaSlot.running };
      if (this.otaNewVersion) this.version = this.otaNewVersion;
      if (this.otaImage && this.otaImage.length >= 0xB8) this.appSha = this.otaImage.subarray(0xB0, 0xB8).toString('hex');
    }
    const r = this.otaReboot;
    this.otaLog.push({ op: 'restart', t: Date.now() });
    if (this.mode === 'via-wcb') {
      this.meshDown = true;
      this.later(r.goneMs + r.bootMs, () => { this.meshDown = false; });
      return;
    }
    this.present = false;
    if (this.port && this.port.owner) this.port.fault('NetworkError');
    this.later(r.goneMs, () => {
      this.present = true;
      this.booting = true;
      this.later(r.bootMs, () => { this.booting = false; });
    });
  },

  // ── Transport A: ?OTALOCAL (navicore_ota.h:244-305) ───────────────────────────────────────────────────────
  _otaLocal(args) {
    const c1 = args.indexOf(',');
    const sub = (c1 < 0 ? args : args.substring(0, c1)).trim().toUpperCase();
    const rest = c1 < 0 ? '' : args.substring(c1 + 1);
    if (sub === 'STATUS' || sub === '') return this._otaStatus();
    if (sub === 'BEGIN') {
      const p = rest.indexOf(',');
      if (p < 0) return this._otaPrint('[OTA] BEGIN usage: ?OTALOCAL,BEGIN,<imageSize>,<family 0|1>');
      const size = toInt(rest.substring(0, p)) >>> 0, family = toInt(rest.substring(p + 1)) & 0xFF;
      this.otaLog.push({ op: 'begin', size, family });
      this._otaPrint('');
      this._otaPrint('[OTA:BEGIN,START]', true);
      this._otaFlush();                                   // Serial.flush() before the erase blocks the CPU
      this.later(this.otaEraseMs, () => {
        const ok = this._otaBegin(OTA_LOCAL_SESSION, size, family);
        this._otaPrint(`[OTA:BEGIN,${ok ? 'OK' : 'ERR'},${this._otaCursor()}]`, true);
      });
      return;
    }
    if (sub === 'DATA') {
      const p = rest.indexOf(',');
      if (p < 0) return this._otaPrint('[OTA] DATA usage: ?OTALOCAL,DATA,<offset>,<base64>');
      const offset = toInt(rest.substring(0, p)) >>> 0;
      const d = b64decode(rest.substring(p + 1).trim(), OTA_LOCAL_MAX_CHUNK);
      if (d.rc !== 0) {
        this.otaLog.push({ op: 'b64err', offset, rc: d.rc });
        return this._otaPrint(`[OTA] DATA base64 error ${d.rc} (chunk too big? max ${OTA_LOCAL_MAX_CHUNK} B decoded)`);
      }
      this.otaLog.push({ op: 'data', offset, len: d.buf.length, cursor: this._otaCursor() });
      if (!this._otaWrite(OTA_LOCAL_SESSION, offset, d.buf)) {
        const cur = this._otaCursor();
        this.otaLog.push({ op: 'nak', offset, cursor: cur });
        this._otaPrint(`[OTA] DATA rejected at offset ${offset} (write cursor at ${cur})`);
        return this._otaPrint(`[OTA:NAK,${cur}]`, true);
      }
      const cur = this._otaCursor();
      if (this._otaFire('lostAck', cur)) { this.otaLog.push({ op: 'lostAck', cursor: cur }); return; }
      return this._otaPrint(`[OTA:ACK,${cur}]`, true);
    }
    if (sub === 'END') {
      this.otaLog.push({ op: 'end' });
      for (const l of this._otaFaultLines('stale', OTA_LOCAL_SESSION)) this._otaPrint(l, true);
      if (this._otaEnd(OTA_LOCAL_SESSION)) {
        this._otaPrint('[OTA:END,OK]', true);
        this._otaPrint('[OTA] rebooting into new firmware in 2s...');
        this.later(this.otaReboot.afterMs, () => this._restart());
      } else {
        this._otaPrint('[OTA:END,ERR]', true);
      }
      return;
    }
    if (sub === 'ABORT') { this.otaLog.push({ op: 'abort' }); return this._otaAbort('local abort command'); }
    this._otaPrint(`[OTA] unknown subcommand '${sub}' (use STATUS|BEGIN|DATA|END|ABORT)`);
  },

  // A DATA line as it arrives on a direct link: lost, or processed.
  _otaLocalLine(line) {
    const m = /^\?OTALOCAL,DATA,(\d+),/.exec(line);
    if (m && this._otaFire('lostData', +m[1])) { this.otaLog.push({ op: 'lost', offset: +m[1] }); return; }
    this._otaLocal(line.substring(10));
  },

  // ── Transport B: the relay (WCB_OTA.cpp:486-593) and NaviCore's target handlers (navicore_ota.h:346-411) ──
  _otaRelayLine(line) {
    this._otaForwardUntil = Date.now() + 8000;            // WCB.ino:6612: the RC-JSON passthrough pauses
    let args = line.substring(5);
    const dm = /^DATA,\d+,\d+,(\d+)/i.exec(args);
    if (dm) {
      const off = +dm[1];
      if (this._otaFire('lostData', off)) { this.otaLog.push({ op: 'lost', offset: off }); return; }
      if (this._otaFire('mangle', off)) {               // a run of bytes dropped from the middle of the base64
        const k = args.lastIndexOf(',') + 9;
        args = args.slice(0, k) + args.slice(k + 4);
        this.otaLog.push({ op: 'mangled', offset: off });
      }
    }
    const c1 = args.indexOf(',');
    const sub = (c1 < 0 ? args : args.substring(0, c1)).trim().toUpperCase();
    const rest = c1 < 0 ? '' : args.substring(c1 + 1);
    const c2 = rest.indexOf(',');
    const target = toInt(c2 < 0 ? rest : rest.substring(0, c2)) & 0xFF;
    const r2 = c2 < 0 ? '' : rest.substring(c2 + 1);
    const c3 = r2.indexOf(',');
    const session = toInt(c3 < 0 ? r2 : r2.substring(0, c3)) & 0xFFFF;
    const r3 = c3 < 0 ? '' : r2.substring(c3 + 1);
    if (target < 1 || target > MAX_WCB_COUNT) return this._otaPrint(`[OTA] relay: invalid target ${target}`);
    const forward = (pkt) => {
      this.otaLog.push({ op: 'forward', ...pkt, data: undefined, len: pkt.data ? pkt.data.length : undefined });
      if (target !== RC_ID || this.meshDown) return;      // nobody there answers
      this.later(this.otaHopMs, () => this._otaTarget(pkt));
    };
    if (sub === 'BEGIN') {
      const p = r3.indexOf(',');
      const size = toInt(p < 0 ? r3 : r3.substring(0, p)) >>> 0;
      const family = p < 0 ? 0 : toInt(r3.substring(p + 1)) & 0xFF;
      return forward({ type: 'BEGIN', session, size, family });
    }
    if (sub === 'DATA') {
      const p = r3.indexOf(',');
      if (p < 0) return this._otaPrint('[OTA] relay DATA: ?OTA,DATA,<t>,<s>,<offset>[:<crc32>],<b64>');
      let offField = r3.substring(0, p);
      const b64 = r3.substring(p + 1).trim();
      const cpos = offField.indexOf(':');
      let crcHex = '';
      if (cpos >= 0) { crcHex = offField.substring(cpos + 1); offField = offField.substring(0, cpos); }
      const offset = toInt(offField) >>> 0;
      if (crcHex.length) {
        const want = parseInt(crcHex, 16) >>> 0, have = crc32(`${offField},${b64}`);
        if (want !== have) {
          this.otaLog.push({ op: 'crcDrop', offset });
          const h = (n) => n.toString(16).toUpperCase().padStart(8, '0');
          return this._otaPrint(`[OTA] relay DATA @${offset} DROPPED: crc ${h(have)} != ${h(want)} (b64 ${b64.length} chars)`);
        }
      } else if (!this._otaWarnedNoCrc) {
        this._otaWarnedNoCrc = true;
        this._otaPrint('[OTA] relay DATA has no crc32 suffix — sender predates it; transfer is UNPROTECTED against serial corruption');
      }
      const d = b64decode(b64, OTA_ESPNOW_PAYLOAD);
      if (d.rc !== 0) return this._otaPrint(`[OTA] relay DATA base64 error ${d.rc}`);
      return forward({ type: 'DATA', session, offset, data: d.buf });
    }
    if (sub === 'END' || sub === 'ABORT') return forward({ type: sub, session });
    this._otaPrint(`[OTA] relay: unknown subcommand '${sub}'`);
  },

  // NaviCore's answer travels back over the mesh and the relay prints it (WCB_OTA.cpp:466-480).
  _otaAck(session, status, offset) {
    const line = `[OTA:ACK,${RC_ID},${session},${offset},${status}]`;
    this.later(this.otaHopMs, () => {
      if (this._otaFire('lostAck', offset)) { this.otaLog.push({ op: 'lostAck', cursor: offset }); return; }
      this.otaLog.push({ op: 'ack', session, status, offset });
      this._otaPrint(line, true);
    });
  },
  _otaTarget(pkt) {
    if (this.meshDown) return;
    if (pkt.type === 'BEGIN') {
      // The target erases its slot before it answers.
      return this.later(this.otaEraseMs, () => {
        const ok = this._otaBegin(pkt.session, pkt.size, pkt.family);
        this._otaAck(pkt.session, ok ? OTA_ST_OK : OTA_ST_ERR, this._otaCursor());
      });
    }
    if (pkt.type === 'DATA') {
      const inSession = this.ota.active && pkt.session === this.ota.session;
      this.otaLog.push({ op: 'data', offset: pkt.offset, len: pkt.data.length, cursor: this._otaCursor(), inSession });
      const wrote = this._otaWrite(pkt.session, pkt.offset, pkt.data.subarray(0, OTA_ESPNOW_PAYLOAD));
      if (!wrote) this.otaLog.push({ op: 'nak', offset: pkt.offset, cursor: this._otaCursor() });
      const live = this.ota.active && pkt.session === this.ota.session;
      if (this.otaFault.foreign && !this._otaForeignSent) {
        this._otaForeignSent = true;
        for (const l of this._otaFaultLines('foreign', pkt.session)) this._otaPrint(l, true);
      }
      return this._otaAck(pkt.session, live ? OTA_ST_OK : OTA_ST_ERR, this._otaCursor());
    }
    if (pkt.type === 'END') {
      this.otaLog.push({ op: 'end' });
      for (const l of this._otaFaultLines('stale', pkt.session)) this._otaPrint(l, true);
      const ok = this._otaEnd(pkt.session);
      this._otaAck(pkt.session, ok ? OTA_ST_OK : OTA_ST_ERR, this._otaCursor());
      if (ok) {
        this._otaSay('[OTA] remote update verified — rebooting into new firmware...');
        for (let r = 1; r <= 3; r++) this.later(80 * r, () => this._otaAck(pkt.session, OTA_ST_OK, this._otaCursor()));
        this.later(80 * 3 + 150, () => this._restart());
      }
      return;
    }
    if (pkt.type === 'ABORT') {
      this.otaLog.push({ op: 'abort', session: pkt.session });
      this._otaAbort('remote abort');
      return this._otaAck(pkt.session, OTA_ST_OK, 0);
    }
  },
};

// The rows of otaLog of one kind.
function otaOps(emu, op) { return emu.otaLog.filter((r) => r.op === op); }

module.exports = { methods, crc32, b64decode, otaOps, SLOT_SIZE, OTA_LOCAL_MAX_CHUNK, OTA_ESPNOW_PAYLOAD };
