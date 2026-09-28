// A fake navigator.serial for the NaviCore config tool, installed before the page's own scripts
// (context.addInitScript), so the tool's unmodified serial code talks to a Node-side device instead of a COM port.
// The device is the in-Node emulator (L1, lib/navicore/emulator.js) or the harness's own COM handle through the
// bridge (L2, lib/navicore/pipe.js): docs/hil_plan/NAVICORE.md §5.2.
//
// It must be Object.defineProperty, not an assignment: Chrome exposes navigator.serial as a getter on
// Navigator.prototype, and a plain assignment throws under strict mode (Intellex/src/intellex_shim.js:282-300 hit
// exactly that). The port follows the Web Serial spec where the tool depends on it:
//   - readable/writable are created lazily while the port is open; a reader.cancel() or a RECOVERABLE read error
//     (BreakError, FramingError, ParityError, BufferOverrunError) leaves the next `port.readable` a fresh stream,
//     a FATAL one (NetworkError: "The device has been lost.") leaves it null. The tool's read loop is built on that
//     split (config_tool/index.html startReading, SERIAL_RECOVERABLE).
//   - close() rejects while either stream is locked, as a real port does, and every attempt is logged anyway
//     (the pagehide release in _releaseSerialOnUnload closes without unlocking).
//   - setSignals() is recorded and never applied to anything: on NaviCore's native USB, DTR/RTS reset the chip.
//   - a 'disconnect' event reaches navigator.serial listeners with ev.target === the port.
// Bytes cross the page boundary as base64: page -> Node through the binding __hilSerialCtl, Node -> page through
// page.evaluate(window.__hilSerial.push). One port object per device per page, like a granted port.

// ─── page side ───────────────────────────────────────────────────────────────────────────────────────────────
// Serialized by Playwright: no closure over anything in this file.
function pageShim(cfg) {
  if (window.__hilSerial) return;
  const ctl = (msg) => window.__hilSerialCtl(msg);
  const b64e = (u8) => { let s = ''; for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]); return btoa(s); };
  const b64d = (b) => { const s = atob(b); const u = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i); return u; };
  const log = [];
  const note = (op, extra) => { const e = Object.assign({ t: performance.now(), op }, extra || {}); log.push(e); return e; };
  const RECOVERABLE = new Set(['BufferOverrunError', 'BreakError', 'FramingError', 'ParityError']);
  const state = {
    requestMode: cfg.requestPort || 'grant',      // 'grant' | 'cancel'
    granted: new Set(cfg.granted ? [cfg.device] : []),
    dummies: cfg.dummies || 0,                    // extra granted ports that are not our device
  };

  class HilSerialPort {
    constructor(id, info) {
      this._id = id; this._info = info; this._state = 'closed';
      this._readable = null; this._writable = null; this._rx = null; this._pending = [];
      this._readFatal = false; this._writeFault = null; this.onconnect = null; this.ondisconnect = null;
    }
    getInfo() { return Object.assign({}, this._info); }
    get connected() { return true; }
    get readable() {
      if (this._readable) return this._readable;
      if (this._state !== 'opened' || this._readFatal) return null;
      const port = this;
      this._readable = new ReadableStream({
        start(c) { port._rx = c; for (const u of port._pending.splice(0)) c.enqueue(u); },
        cancel() { if (port._rx) { port._rx = null; port._readable = null; } note('rxcancel', { id: port._id }); },
      });
      return this._readable;
    }
    get writable() {
      if (this._writable) return this._writable;
      if (this._state !== 'opened') return null;
      const port = this;
      this._writable = new WritableStream({
        async write(chunk) {
          if (port._state !== 'opened') throw new DOMException('The port is closed.', 'InvalidStateError');
          if (port._readFatal) throw new DOMException('The device has been lost.', 'NetworkError');
          if (port._writeFault) {                  // one-shot, injected by a spec (window.__hilSerial.failNextWrite)
            const f = port._writeFault; port._writeFault = null;
            note('writefault', { id: port._id, name: f.name });
            throw new DOMException(f.message, f.name);
          }
          const u8 = chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk.buffer || chunk);
          const r = await ctl({ op: 'write', id: port._id, b64: b64e(u8), t: performance.now() });
          if (r && r.error) throw new DOMException(r.error.message, r.error.name);
        },
        close() { port._writable = null; },
        abort() { port._writable = null; },
      });
      return this._writable;
    }
    async open(opts) {
      note('open', { id: this._id, opts });
      if (this._state === 'opened') throw new DOMException('The port is already open.', 'InvalidStateError');
      const r = await ctl({ op: 'open', id: this._id, opts });
      if (r && r.error) throw new DOMException(r.error.message, r.error.name);
      this._state = 'opened'; this._readFatal = false; this._pending = []; this._writeFault = null;
    }
    async setSignals(signals) {
      note('signals', { id: this._id, signals });
      if (this._state !== 'opened') throw new DOMException('The port is closed.', 'InvalidStateError');
      await ctl({ op: 'signals', id: this._id, signals });
    }
    async getSignals() {
      return { dataCarrierDetect: false, clearToSend: false, ringIndicator: false, dataSetReady: false };
    }
    async close() {
      const rs = this._readable, ws = this._writable;
      const entry = note('close', { id: this._id, readLocked: !!(rs && rs.locked), writeLocked: !!(ws && ws.locked) });
      // Told to Node synchronously: a close from a dying page (pagehide) never gets to finish.
      ctl({ op: 'closeAttempt', id: this._id, readLocked: entry.readLocked, writeLocked: entry.writeLocked });
      if (this._state !== 'opened') { entry.result = 'not open'; throw new DOMException('The port is already closed.', 'InvalidStateError'); }
      if ((rs && rs.locked) || (ws && ws.locked)) { entry.result = 'locked'; throw new TypeError('Cannot cancel a locked stream'); }
      try { if (rs) await rs.cancel(); } catch (_) {}
      try { if (ws) await ws.abort(); } catch (_) {}
      this._state = 'closed'; this._readable = null; this._writable = null; this._rx = null; this._pending = [];
      entry.result = 'closed';
      await ctl({ op: 'close', id: this._id });
    }
    async forget() {
      note('forget', { id: this._id });
      state.granted.delete(this._id);
      if (this._state === 'opened') { try { await this.close(); } catch (_) {} }
    }
    // Node -> page: bytes off "the wire". Dropped while closed, held until a reader asks while open.
    _push(u8) {
      if (this._state !== 'opened') return;
      if (this._rx) this._rx.enqueue(u8); else this._pending.push(u8);
    }
    // Error the current read. A fatal one leaves readable null (and every later write failing), a recoverable one
    // leaves a fresh stream behind, as the spec's [[readFatal]] does.
    _fault(name, message) {
      const fatal = !RECOVERABLE.has(name);
      note('fault', { id: this._id, name, fatal });
      if (fatal) this._readFatal = true;         // and every later write fails too (see write())
      const c = this._rx;
      this._rx = null; this._readable = null;
      if (c) c.error(new DOMException(message, name));
    }
  }

  const info = { usbVendorId: cfg.vid, usbProductId: cfg.pid };
  const port = new HilSerialPort(cfg.device, info);
  const dummies = [];
  for (let i = 0; i < state.dummies; i++) dummies.push(new HilSerialPort('dummy' + i, { usbVendorId: 0x10c4, usbProductId: 0xea60 + i + 1 }));
  const byId = (id) => (id === port._id ? port : dummies.find((d) => d._id === id));
  const listeners = { connect: [], disconnect: [] };

  const serial = {
    async requestPort(options) {
      note('requestPort', { mode: state.requestMode });
      if (state.requestMode === 'cancel') throw new DOMException('No port selected by the user.', 'NotFoundError');
      state.granted.add(port._id);
      return port;
    },
    async getPorts() {
      const out = [];
      if (state.granted.has(port._id)) out.push(port);
      for (const d of dummies) out.push(d);
      note('getPorts', { n: out.length });
      return out;
    },
    addEventListener(type, fn) { (listeners[type] || (listeners[type] = [])).push(fn); },
    removeEventListener(type, fn) { const a = listeners[type]; if (a) { const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); } },
    dispatchEvent() { return true; },
    onconnect: null,
    ondisconnect: null,
  };
  Object.defineProperty(navigator, 'serial', { value: serial, configurable: true, writable: true, enumerable: false });

  window.__hilSerial = {
    log,
    port,
    config: cfg,
    push(id, b64) { const p = byId(id); if (p) p._push(b64d(b64)); },
    fault(id, name, message) { const p = byId(id); if (p) p._fault(name, message); },
    // A USB unplug: the event reaches navigator.serial's listeners with the port as its target.
    fireDisconnect(id) {
      const p = byId(id) || port;
      const ev = { type: 'disconnect', target: p };
      note('disconnectEvent', { id: p._id });
      for (const fn of (listeners.disconnect || []).slice()) { try { fn(ev); } catch (e) { console.error(e); } }
      if (typeof serial.ondisconnect === 'function') serial.ondisconnect(ev);
    },
    setRequestMode(m) { state.requestMode = m; },
    failNextWrite(name, message) { port._writeFault = { name: name || 'NetworkError', message: message || 'The device has been lost.' }; },
    grant(on) { if (on) state.granted.add(port._id); else state.granted.delete(port._id); },
    isOpen() { return port._state === 'opened'; },
  };
}

// ─── Node side ──────────────────────────────────────────────────────────────────────────────────────────────
// A device is anything with:
//   attach(sink, port)    sink(bytes) sends bytes to whichever page holds the port open (dropped when none does);
//                         port is this FakeSerial, for a device that errors a held port (a restart)
//   onOpen(opts, page)    the port was opened        onClose(page)   it was closed
//   onWrite(buf, meta)    bytes the page wrote       onSignals(s)    a setSignals() call (recorded, never applied)
//   present               optional: false while the device is off the bus (a restarting NaviCore re-enumerating),
//                         when open() fails as Chrome's does for an absent device and writes fail as for a lost one
// Only one page may hold the device open at a time, as with a real port: a second open() fails the way Chrome's
// does ("Failed to open serial port.", NetworkError).
class FakeSerial {
  constructor(device, opts = {}) {
    this.device = device;
    this.id = opts.id || 'navicore';
    this.cfg = {
      device: this.id,
      vid: opts.vid ?? 0x303a,          // Espressif USB-Serial/JTAG (ESP32-S3 native USB)
      pid: opts.pid ?? 0x1001,
      requestPort: opts.requestPort || 'grant',
      granted: !!opts.granted,
      dummies: opts.dummies || 0,
    };
    this.owner = null;                  // the page holding the port open
    this.events = [];                   // {t, op, ...}: open / close / write / signals, as Node saw them
    this._chains = new Map();           // page -> promise chain, so pushes arrive in order
    this._watched = new WeakSet();      // pages whose 'close' already releases the port
    this._pages = new WeakMap();        // page -> pg, its number in order of first contact (events carry it)
    device.attach((bytes) => this.push(bytes), this);
  }

  async install(context) {
    await context.exposeBinding('__hilSerialCtl', (source, msg) => this._ctl(source.page, msg));
    await context.addInitScript(pageShim, this.cfg);
  }

  _pg(page) {
    if (!this._pages.has(page)) this._pages.set(page, (this._pageCount = (this._pageCount || 0) + 1));
    return this._pages.get(page);
  }

  _ctl(page, msg) {
    const pg = this._pg(page);
    const id = msg && msg.id;
    if (id !== this.id) {
      // A dummy port: granted, but nothing is on the other end.
      if (msg.op === 'open') return { error: { name: 'NetworkError', message: 'Failed to open serial port.' } };
      return {};
    }
    switch (msg.op) {
      case 'open':
        if ((this.owner && this.owner !== page) || this.device.present === false) {
          this.events.push({ t: Date.now(), op: 'openFail', pg, absent: this.device.present === false });
          return { error: { name: 'NetworkError', message: 'Failed to open serial port.' } };
        }
        this.owner = page;
        this.events.push({ t: Date.now(), op: 'open', pg, opts: msg.opts });
        if (!this._watched.has(page)) {   // a closed tab releases the port, as the OS does for a dead browser context
          this._watched.add(page);
          page.on('close', () => { if (this.owner === page) { this.owner = null; this.device.onClose(page); } });
        }
        this.device.onOpen(msg.opts, page);
        return {};
      case 'close':
        this.events.push({ t: Date.now(), op: 'close', pg });
        if (this.owner === page) { this.owner = null; this.device.onClose(page); }
        return {};
      case 'signals':
        this.events.push({ t: Date.now(), op: 'signals', pg, signals: msg.signals });
        if (this.device.onSignals) this.device.onSignals(msg.signals);
        return {};
      case 'closeAttempt':
        this.events.push({ t: Date.now(), op: 'closeAttempt', pg, readLocked: msg.readLocked, writeLocked: msg.writeLocked });
        return {};
      case 'write': {
        if (this.owner !== page) return { error: { name: 'InvalidStateError', message: 'The port is closed.' } };
        if (this.device.present === false) return { error: { name: 'NetworkError', message: 'The device has been lost.' } };
        const buf = Buffer.from(msg.b64, 'base64');
        this.events.push({ t: Date.now(), op: 'write', pg, pageT: msg.t, len: buf.length, head: buf.subarray(0, 48).toString('latin1') });
        this.device.onWrite(buf, { pageT: msg.t });
        return {};
      }
      default:
        return {};
    }
  }

  // Bytes to the page that holds the port. Each call is one chunk on the page side (the emulator uses that to
  // split a UTF-8 character or a line across reads on purpose).
  push(bytes) {
    const page = this.owner;
    if (!page || page.isClosed()) return;
    const b64 = Buffer.from(bytes).toString('base64');
    this._enqueue(page, (p) => p.evaluate(([id, b]) => window.__hilSerial && window.__hilSerial.push(id, b), [this.id, b64]));
  }

  // Error the page's current read (NetworkError is fatal, BreakError etc. recoverable: see pageShim).
  fault(name, message = name === 'NetworkError' ? 'The device has been lost.' : 'Break received') {
    const page = this.owner;
    if (!page) return Promise.resolve();
    return this._enqueue(page, (p) => p.evaluate(([id, n, m]) => window.__hilSerial.fault(id, n, m), [this.id, name, message]));
  }

  // A USB unplug seen by navigator.serial (the tool listens for it: index.html handleLinkLost).
  fireDisconnect(page = this.owner) {
    if (!page) return Promise.resolve();
    return this._enqueue(page, (p) => p.evaluate((id) => window.__hilSerial.fireDisconnect(id), this.id));
  }

  // Resolves once every push queued so far has been delivered.
  drain(page = this.owner) { return page ? (this._chains.get(page) || Promise.resolve()) : Promise.resolve(); }

  _enqueue(page, fn) {
    const prev = this._chains.get(page) || Promise.resolve();
    const next = prev.then(() => (page.isClosed() ? null : fn(page))).catch(() => {});   // a navigating/closed page drops it
    this._chains.set(page, next);
    return next;
  }
}

module.exports = { FakeSerial, pageShim };
