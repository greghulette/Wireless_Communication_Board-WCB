// Helpers shared by the medium-risk re-scan specs (specs/*_rescan_fake.spec.js, 2026-10-12): the page set up with the
// fake-board recorder (lib/fake.js), the verify pulls held back, and the toasts read since a mark.
const { openWizard } = require('./wizard');
const { install, pullFake, push } = require('./fake');

async function setup(page) {
  await openWizard(page);
  await install(page);
}

// boardGo pulls the board again 300 ms after a push that needs no reboot (and boardPull runs on other timers too). A
// fake answers that pull with the backup it was given, not with what was pushed, so a spec that pushes more than once
// holds those pulls back: each push is then judged against the pulled baseline alone. The page's own calls look
// boardPull up by name, so the wrapper sees them all; pull() below is a pull the spec asks for, which always runs.
const holdVerifyPulls = (page) => page.evaluate(() => {
  if (!window.__realBoardPull) {
    window.__realBoardPull = boardPull;
    window.boardPull = (...a) => (window.__pullsHeld ? Promise.resolve() : window.__realBoardPull(...a));
  }
  window.__pullsHeld = true;
});
async function pull(page, slot, tokens, opts) {
  await page.evaluate(() => { window.__heldBefore = !!window.__pullsHeld; window.__pullsHeld = false; });
  await pullFake(page, slot, tokens, opts);
  await page.evaluate(() => { window.__pullsHeld = window.__heldBefore; });
}

const toastMark = (page) => page.evaluate(() => __fake.toasts.length);
const toastsSince = (page, m) => page.evaluate((m) => __fake.toasts.slice(m).map((t) => t.message).join('\n'), m);
// A select's option values, and whether an element is shown (its own display style).
const options = (page, id) => page.evaluate((id) => [...document.getElementById(id).options].map((o) => o.value), id);
const shown = (page, id) => page.evaluate((id) => document.getElementById(id).style.display !== 'none', id);
// What a push of slot n sends, with the reboot left out (boardGo's skipReboot), as 'n:command'.
const sent = async (page, n) => (await push(page, n, { skipReboot: true })).sent;

// A fake navigator.serial with the ports in `specs` ({ A: { vid, pid }, ... }) granted, installed once the page has
// loaded (as specs/connect_wizard_fake.spec.js's, with a writable side and scriptable failures). window.__serial.ports[id]:
// .feed(text) puts bytes on its wire and .end() ends its stream; .openFails = k makes the next k opens throw
// (.openError: the message, or 'already' for Chrome's already-open error), .closeFails = k the next k closes;
// .writeDelay slows each write; .writes holds what was written, as text. Every open, close, setSignals, reconfigure and
// write is in window.__serial.log as { id, op, at }. __serial.fire('connect', id) raises navigator.serial's connect
// event for a port, as a USB re-enumeration does.
async function fakeSerial(page, specs, { granted } = {}) {
  await page.evaluate(({ specs, granted }) => {
    const log = [];
    const ports = {};
    for (const [id, { vid, pid }] of Object.entries(specs)) {
      const p = {
        id, state: 'closed', stream: null, ctrl: null, pending: [], writes: [],
        openFails: 0, openError: 'Failed to open serial port.', closeFails: 0, writeDelay: 0, _writable: null,
        getInfo: () => ({ usbVendorId: vid, usbProductId: pid }),
        get readable() {
          if (p.state !== 'open') return null;
          if (!p.stream) {
            p.stream = new ReadableStream({
              start(c) { p.ctrl = c; for (const u of p.pending.splice(0)) c.enqueue(u); },
              cancel() { p.ctrl = null; p.stream = null; },
            });
          }
          return p.stream;
        },
        get writable() {
          if (p.state !== 'open') return null;
          if (!p._writable) {
            p._writable = new WritableStream({
              async write(chunk) {
                if (p.writeDelay) await new Promise((r) => setTimeout(r, p.writeDelay));
                const s = new TextDecoder().decode(chunk);
                p.writes.push(s);
                log.push({ id, op: 'write', s, at: Date.now() });
              },
            });
          }
          return p._writable;
        },
        async open(opts) {
          log.push({ id, op: 'open', baud: opts?.baudRate, at: Date.now() });
          if (p.openFails > 0) {
            p.openFails--;
            if (p.openError === 'already') throw new DOMException('The port is already open.', 'InvalidStateError');
            throw new DOMException(p.openError, 'NetworkError');
          }
          if (p.state === 'open') throw new DOMException('The port is already open.', 'InvalidStateError');
          p.state = 'open';
          p.pending = [];
        },
        async close() {
          log.push({ id, op: 'close', at: Date.now() });
          if (p.closeFails > 0) { p.closeFails--; throw new DOMException('Failed to close serial port.', 'NetworkError'); }
          if (p.state !== 'open') throw new DOMException('The port is already closed.', 'InvalidStateError');
          if (p.stream?.locked) throw new TypeError('Cannot cancel a locked stream');
          try { await p.stream?.cancel(); } catch (_) {}
          p.state = 'closed'; p.stream = null; p.ctrl = null; p._writable = null;
        },
        async setSignals(s) { log.push({ id, op: 'signals', s: JSON.stringify(s), at: Date.now() }); },
        async reconfigure(o) { log.push({ id, op: 'reconfigure', baud: o?.baudRate, at: Date.now() }); },
        feed(text) {
          if (p.state !== 'open') return;
          const u = new TextEncoder().encode(text);
          if (p.ctrl) p.ctrl.enqueue(u); else p.pending.push(u);
        },
        end() {
          log.push({ id, op: 'end', at: Date.now() });
          if (p.ctrl) { p.ctrl.close(); p.ctrl = null; p.stream = null; }
        },
      };
      ports[id] = p;
    }
    const listeners = {};
    const serial = {
      granted: (granted || Object.keys(ports)).map((k) => ports[k]),
      pick: null,
      async getPorts() { log.push({ op: 'getPorts', at: Date.now() }); return serial.granted.slice(); },
      async requestPort() {
        log.push({ op: 'requestPort', at: Date.now() });
        if (!serial.pick) throw new DOMException('No port selected by the user.', 'NotFoundError');
        return serial.pick;
      },
      addEventListener(type, cb) { (listeners[type] = listeners[type] || []).push(cb); },
      removeEventListener(type, cb) { listeners[type] = (listeners[type] || []).filter((f) => f !== cb); },
    };
    Object.defineProperty(navigator, 'serial', { value: serial, configurable: true, writable: true, enumerable: false });
    window.__serial = {
      serial, ports, log, listeners,
      of: (id) => log.filter((e) => e.id === id).map((e) => e.op),
      fire: (type, id) => (listeners[type] || []).forEach((cb) => cb({ port: ports[id] })),
    };
  }, { specs, granted });
}

module.exports = { setup, holdVerifyPulls, pull, toastMark, toastsSince, options, shown, sent, fakeSerial };
