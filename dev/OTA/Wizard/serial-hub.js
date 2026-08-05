// ════════════════════════════════════════════════════════════════════════════
//  WcbSerialHub — share ONE WebSerial port across SAME-ORIGIN browser tabs
// ════════════════════════════════════════════════════════════════════════════
// The problem: a WebSerial port can be open in exactly ONE browsing context at a
// time. So the WCB Wizard and the NaviCore config tool can't each hold their own
// connection to the SAME USB-tethered board (a MgmtRelay / gateway WCB) — the
// second tab's open() fails. But both tools only ever speak line-oriented TEXT to
// that board (";"-commands out, text lines in), so they don't each NEED the raw
// port — they need a shared text pipe THROUGH it.
//
// This hub makes exactly one tab the "leader" that owns the physical SerialPort;
// every other same-origin tab is a "follower" that proxies its bytes to the leader
// over a BroadcastChannel. The leader mirrors everything it reads off the port back
// to all followers. To the tool on top, the hub is just: send(bytes) / on('data').
//
//   ┌─ Tab A (leader) ─────────┐        ┌─ Tab B (follower) ───────┐
//   │  hub.send() ─► SerialPort│        │  hub.send() ─► BC 'tx' ──┼─┐
//   │  read loop ─► on('data') │        │  on('data') ◄── BC 'rx' ─┼─┘ (from leader)
//   │        └──────► BC 'rx' ──┼───────►│                          │
//   └──────────────────────────┘        └──────────────────────────┘
//
// Leader election + failover ride on the Web Locks API: every tab queues for one
// exclusive lock; the holder is the leader, and when it closes (or resigns) the
// browser hands the lock to the next queued tab, which promotes itself and re-opens
// the (origin-shared, already-granted) port with no user gesture.
//
// HARD REQUIREMENT: all participating tabs must be SAME-ORIGIN (BroadcastChannel and
// Web Locks are origin-scoped). Serve the Wizard and the NaviCore config tool from
// one origin — e.g. both under https://<user>.github.io/… — or the tabs simply never
// see each other and each opens its own port (the pre-hub behaviour).
//
// Deliberately NOT handled here (stays the tool's job, exactly as today): flashing
// (esptool-js needs the raw port — only the leader tab can flash), the connect
// handshake / PING-PONG, config parsing, line-splitting. The hub trades in RAW BYTES
// so each tool keeps its own decoder + line rules.
//
// No ES-module import (works when served over http OR opened via file://) — this
// defines window.WcbSerialHub as a classic script.
// ════════════════════════════════════════════════════════════════════════════
(function () {
  'use strict';

  const DEFAULTS = {
    channelName: 'wcb-shared-serial',        // BroadcastChannel name — the cross-tab contract
    lockName:    'wcb-shared-serial-owner',  // Web Lock name — the single "who owns the port" token
    baudRate:    115200,
    // Do NOT assert DTR: the shared port is a WCB gateway, and DTR=true fires its
    // one-shot RC-differentiator RESET — the WCB reboots and drops off the mesh right
    // as we're handshaking, which made shared connects sporadic (config sometimes in,
    // rc_ch subscription often never established). The WCB forwards UART→USB WITHOUT
    // host DTR (NaviCore's normal Via-WCB works with no DTR at all), so leave it off.
    assertDTR:   false,
    // Visibility handoff is OFF by default. Handing the port to the foreground tab means
    // closing it in one tab and RE-OPENING it in the other — and re-opening a WCB toggles
    // DTR, firing its RC-differentiator reset. So a handoff = a WCB REBOOT on every tab
    // swap (shows up as "Power-on Reset"). Not worth it: keep ONE sticky owner (whichever
    // tab picks the port); the other stays a follower. (Failover on owner CLOSE still runs.)
    visibilityHandoff: false,
  };

  // Structured-clone over BroadcastChannel copies typed arrays fine, but to be safe
  // against a transferred/detached buffer we always send a standalone copy.
  function copyBytes(u8) { return u8 && u8.byteLength ? u8.slice() : new Uint8Array(0); }
  function toBytes(data) {
    if (data == null) return new Uint8Array(0);
    if (data instanceof Uint8Array) return data;
    if (data instanceof ArrayBuffer) return new Uint8Array(data);
    if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    return new TextEncoder().encode(String(data));   // strings (and anything else) → UTF-8
  }

  class WcbSerialHub {
    constructor(opts = {}) {
      const o = Object.assign({}, DEFAULTS, opts);
      this.channelName = o.channelName;
      this.lockName    = o.lockName;
      this.baudRate    = o.baudRate;
      this.assertDTR   = o.assertDTR;
      this._visibilityHandoff = o.visibilityHandoff;

      this._role       = 'idle';     // 'idle' | 'follower' | 'leader'
      this._joined     = false;
      this._leaving    = false;
      this._bc         = null;

      this._port       = null;       // our SerialPort handle (leader: the open one; follower: usually null)
      this._reader     = null;
      this._portOpen   = false;      // is OUR port open+reading (leader only)
      this._sendChain  = Promise.resolve();   // serializes writer.getWriter() calls

      this._releaseLock = null;      // resolve() that ends our reign as leader → releases the Web Lock
      this._lockAbort   = null;      // AbortController to cancel a QUEUED (not-yet-granted) lock request

      // Last port identity the current/most-recent leader announced — lets a promoted
      // follower re-find the right port in getPorts() with no user gesture.
      this._leaderPortInfo = null;
      this._leaderPortOpen = false;  // follower's view of whether SOME leader has the port open
      this._sawLeaderPort  = false;  // have we EVER seen a real leader hold an open port? gates failover adopt
      this._portlessRetries = 0;     // consecutive portless failover promotions → escalating step-down backoff (reset on any real port)

      this._listeners  = { data: [], role: [], state: [], log: [] };
    }

    // ── Public API ────────────────────────────────────────────────────────────

    static get supported() {
      return typeof navigator !== 'undefined'
        && 'serial' in navigator
        && 'locks'  in navigator
        && typeof BroadcastChannel !== 'undefined';
    }

    get role()     { return this._role; }
    get portOpen() { return this._portOpen || this._leaderPortOpen; }   // is the shared port open ANYWHERE?

    on(event, cb) {
      if (this._listeners[event]) this._listeners[event].push(cb);
      return this;
    }
    off(event, cb) {
      if (this._listeners[event]) this._listeners[event] = this._listeners[event].filter(f => f !== cb);
      return this;
    }

    // Start participating. Every tab becomes a follower immediately and campaigns for
    // leadership; the first to win the lock opens the port (if one is granted) — the
    // rest stay followers until they're promoted. Idempotent.
    join() {
      if (!WcbSerialHub.supported) throw new Error('WcbSerialHub: WebSerial / Web Locks / BroadcastChannel not supported in this browser');
      if (this._joined) return this;
      this._joined  = true;
      this._leaving = false;

      this._bc = new BroadcastChannel(this.channelName);
      this._bc.onmessage = (ev) => this._onBusMessage(ev.data);

      this._setRole('follower');
      this._post({ t: 'hello' });        // ask any existing leader to announce its state
      this._maybeCampaign();             // enter the lock election ONLY if we hold a port (or saw one) — a portless tab just follows, never squats the lock

      // Visibility-driven leadership handoff (OFF by default — see DEFAULTS): it would
      // give the foreground tab the port so its read loop isn't background-throttled, but
      // each handoff re-opens the port and thus REBOOTS the WCB. Disabled ⇒ the port has
      // ONE sticky owner (whichever tab picked it); failover still runs when that tab CLOSES.
      if (this._visibilityHandoff) {
        this._visHandler = () => this._onVisibilityChange();
        try { document.addEventListener('visibilitychange', this._visHandler); } catch (_) {}
        // If we loaded in the foreground behind an already-running (maybe hidden) leader,
        // claim once the lock election has settled.
        try { setTimeout(() => this._onVisibilityChange(), 400); } catch (_) {}
      }

      this._log('joined channel "' + this.channelName + '"');
      return this;
    }

    // User-gesture entry point: pick a port. If we're (or become) the leader, open it.
    // Call this from a click handler — requestPort() requires transient activation.
    async requestPort() {
      const p = await navigator.serial.requestPort();
      this._port = p;
      try { this._leaderPortInfo = p.getInfo ? p.getInfo() : null; } catch (_) {}
      this._maybeCampaign();   // now that we hold a port, enter the election if we weren't already (conditional-campaign)
      if (this._role === 'leader') await this._openAndRead();
      else this._log('port selected; will open it if this tab becomes the leader');
      return p;
    }

    // Like requestPort() but ADOPT an already-granted SerialPort (no picker, no user
    // gesture). Used when the caller already holds the port — auto-sharing a board the
    // user just connected directly, or re-sharing a port borrowed back for a flash.
    // Opens it now if we're already the leader; otherwise it opens when we win the lock.
    // The port may be open or closed: _openAndRead()'s "already open → use as-is" branch
    // adopts an open one without a reset.
    async adoptPort(port) {
      this._port = port;
      try { this._leaderPortInfo = port && port.getInfo ? port.getInfo() : null; } catch (_) {}
      this._maybeCampaign();   // now that we hold a port, enter the election if we weren't already (conditional-campaign)
      if (this._role === 'leader') await this._openAndRead();
      else this._log('port adopted; will open it if this tab becomes the leader');
      return port;
    }

    // Send bytes (or a string) to the shared port. Leader writes directly; a follower
    // relays to the leader over the bus. Returns a Promise that (for the leader) resolves
    // when the write actually completes — callers should AWAIT it so multi-fragment sends
    // stay flow-controlled (paced) instead of bursting and overwhelming the WCB's ESP-NOW
    // TX queue. Never throws for a follower with no leader yet — bytes are just dropped
    // (surface portOpen so the user knows why).
    send(data) {
      const bytes = toBytes(data);
      if (!bytes.byteLength) return Promise.resolve();
      if (this._role === 'leader') {
        return this._writeToPort(bytes).catch(e => this._log('write failed: ' + (e && e.message || e)));
      }
      this._post({ t: 'tx', b: copyBytes(bytes) });
      return Promise.resolve();
    }

    // Stop participating. Leader closes the port and releases the lock (promoting the
    // next tab); a queued follower aborts its pending lock request. Safe to call twice.
    async leave() {
      if (!this._joined) return;
      this._leaving = true;
      this._joined  = false;

      try { this._post({ t: 'bye' }); } catch (_) {}

      if (this._visHandler) { try { document.removeEventListener('visibilitychange', this._visHandler); } catch (_) {} this._visHandler = null; }
      if (this._claimTimer) { try { clearTimeout(this._claimTimer); } catch (_) {} this._claimTimer = null; }

      await this._closePort();

      if (this._lockAbort)   { try { this._lockAbort.abort(); } catch (_) {} this._lockAbort = null; }
      if (this._releaseLock) { try { this._releaseLock(); }    catch (_) {} this._releaseLock = null; }

      if (this._bc) { try { this._bc.close(); } catch (_) {} this._bc = null; }
      this._setRole('idle');
      this._log('left');
    }

    // Give up leadership + stop reading, but DELIBERATELY do NOT close the port — hand
    // the still-OPEN, reader-unlocked port back to the caller, which will drive it
    // directly (flash/erase auto-demote borrows it, then re-shares afterwards). This is
    // leave() minus the _closePort() so esptool / a direct read loop can take the port
    // over with no reopen (no WCB reset). Returns the open port (or null if we had none).
    async releasePortKeepOpen() {
      this._leaving = true;
      this._joined  = false;
      try { this._post({ t: 'bye' }); } catch (_) {}
      if (this._visHandler) { try { document.removeEventListener('visibilitychange', this._visHandler); } catch (_) {} this._visHandler = null; }
      if (this._claimTimer) { try { clearTimeout(this._claimTimer); } catch (_) {} this._claimTimer = null; }
      if (this._reader) {
        try { await this._reader.cancel(); }  catch (_) {}
        try { this._reader.releaseLock(); }   catch (_) {}
        this._reader = null;
      }
      this._portOpen = false;
      if (this._lockAbort)   { try { this._lockAbort.abort(); } catch (_) {} this._lockAbort = null; }
      if (this._releaseLock) { try { this._releaseLock(); }    catch (_) {} this._releaseLock = null; }
      if (this._bc) { try { this._bc.close(); } catch (_) {} this._bc = null; }
      const port = this._port;
      this._setRole('idle');
      this._log('released port (kept open) for direct use');
      return port;
    }

    // ── Leadership (Web Locks) ──────────────────────────────────────────────────

    // Enter the leader election ONLY when we have something to lead with: our own port, or
    // evidence a real shared port exists to inherit on failover (_sawLeaderPort). A pure
    // attach-only tab — e.g. a config tool opened with ?share=1 whose requestPort() had no
    // user gesture, so it holds NO port — must NOT campaign: if it won the single exclusive
    // lock it would hold it with no port and DEADLOCK every other tab (it just squats). Such a
    // tab simply follows and waits for a leader's 'state'. Idempotent: no-op if already queued
    // for, or holding, the lock.
    _maybeCampaign() {
      if (!this._joined || this._leaving) return;
      if (this._lockAbort || this._releaseLock) return;   // already campaigning or leading
      if (!this._port && !this._sawLeaderPort) return;     // nothing to lead with → stay a follower
      this._campaign();
    }

    _campaign() {
      // One exclusive lock, requested by every tab. The holder is THE leader; the rest
      // wait in the browser's lock queue. We hold the lock for our entire reign by
      // returning a promise that stays pending until _releaseLock() is called (resign
      // or leave). When we (or a crashed leader) release it, the browser fires the next
      // queued tab's callback → it promotes. This gives automatic, ordered failover.
      this._lockAbort = new AbortController();
      const opts = { mode: 'exclusive', signal: this._lockAbort.signal };
      navigator.locks.request(this.lockName, opts, () => new Promise((release) => {
        this._lockAbort   = null;           // granted — nothing left to abort
        this._releaseLock = release;
        this._becomeLeader().catch(e => this._log('becomeLeader error: ' + (e && e.message || e)));
      })).catch((e) => {
        // AbortError = we called leave() while still queued. Anything else = surfaced.
        if (e && e.name !== 'AbortError') this._log('lock request ended: ' + (e && e.message || e));
      });
    }

    async _becomeLeader() {
      if (this._leaving) { this._releaseLock && this._releaseLock(); return; }
      this._setRole('leader');
      this._log('became LEADER');
      // Only INHERIT an origin-granted port on a genuine FAILOVER — i.e. we actually saw a
      // prior leader holding an open port and are now taking its place. On a cold start we
      // must NOT grab some random granted port: it may be open in another app entirely
      // (that's exactly the "port is already open" failure). Wait for requestPort() instead.
      if (!this._port && this._sawLeaderPort) await this._adoptGrantedPort();
      if (this._port)  { this._portlessRetries = 0; await this._openAndRead(); }
      else             this._stepDownPortless();   // won the lock but have no port (device gone on a failover) → yield, don't squat
    }

    // We won the lock on a failover but have no port and couldn't adopt one — the shared
    // device is physically gone. Do NOT squat the exclusive lock (that re-creates the very
    // deadlock conditional-campaigning prevents): announce the dead state, release the lock,
    // then re-queue after a jittered backoff so a real port-holder queued behind us is served,
    // and so we retry cleanly when the device returns. The backoff prevents a microtask-speed
    // self-spin when we're the only candidate.
    _stepDownPortless() {
      this._announceState();                 // portOpen:false → followers know nothing is open
      this._setRole('follower');
      const release = this._releaseLock; this._releaseLock = null;
      if (release) { try { release(); } catch (_) {} }
      // Escalate the backoff so a permanently-absent device settles to a slow poll instead of
      // ~1 Hz leader↔follower + '[hub] became LEADER' churn, while still auto-recovering when it
      // returns (the counter is reset on any real port — _becomeLeader success + 'state' portOpen).
      this._portlessRetries++;
      const delay = Math.min(800 * Math.pow(2, this._portlessRetries - 1), 30000) + Math.floor(Math.random() * 700);
      setTimeout(() => { if (this._joined && !this._leaving) this._maybeCampaign(); }, delay);
    }

    // A promoted follower usually has no SerialPort object of its own — but the port was
    // granted to the ORIGIN (getPorts() is shared across same-origin tabs), so we can
    // re-find and open it with no user gesture. Match the identity the old leader
    // announced; fall back to the sole granted port if there's exactly one.
    async _adoptGrantedPort() {
      let ports = [];
      try { ports = await navigator.serial.getPorts(); } catch (_) { return; }
      if (!ports.length) return;
      const want = this._leaderPortInfo;
      let pick = null;
      if (want && (want.usbVendorId != null || want.usbProductId != null)) {
        pick = ports.find(p => {
          const i = (p.getInfo && p.getInfo()) || {};
          return i.usbVendorId === want.usbVendorId && i.usbProductId === want.usbProductId;
        }) || null;
      }
      if (!pick && ports.length === 1) pick = ports[0];
      if (pick) {
        this._port = pick;
        try { this._leaderPortInfo = pick.getInfo ? pick.getInfo() : null; } catch (_) {}
        this._log('adopted origin-granted port for failover');
      } else {
        this._log('promoted to leader but could not disambiguate the granted port — call requestPort()');
      }
    }

    // ── Port I/O (leader only) ──────────────────────────────────────────────────

    async _openAndRead(attempts = 4, delayMs = 300) {
      if (!this._port) return;
      // Retry the open: on failover the just-closed leader's context may not have
      // released the OS port handle yet when the browser promotes us, so the first
      // open() can transiently fail. A few short retries ride over that window.
      for (let i = 0; i < attempts; i++) {
        if (this._leaving || this._role !== 'leader') return;
        try {
          await this._port.open({ baudRate: this.baudRate });
          break;   // opened cleanly
        } catch (e) {
          const msg = (e && e.message) || '';
          const alreadyOpen = msg.includes('already open') || msg.includes('already been opened');
          if (alreadyOpen && this._port.readable && !this._port.readable.locked) break;   // usable as-is
          if (i === attempts - 1) { this._log('open failed: ' + msg); this._announceState(); return; }
          await new Promise(r => setTimeout(r, delayMs));
        }
      }
      // A leave()/resign that raced the (possibly >1s) open() above set _leaving or
      // flipped _role, but close() on a still-OPENING port is a no-op that _closePort
      // swallowed — so the port can finish opening AFTER teardown. Re-check here and
      // close the now-open port instead of latching _portOpen + starting a dead read
      // loop, which would orphan an open SerialPort in a hub the app already dropped.
      if (this._leaving || this._role !== 'leader') { try { await this._port.close(); } catch (_) {} return; }
      if (this.assertDTR) { try { await this._port.setSignals({ dataTerminalReady: true }); } catch (_) {} }
      this._portOpen = true;
      this._announceState();
      this._readLoop();   // fire-and-forget
    }

    async _readLoop() {
      // Read the port; when a session ends/errors, RECOVER — wait a short backoff, then
      // re-acquire. A transient blip must not kill telemetry. But cap it: if we get NO
      // data across several consecutive sessions the port is genuinely dead/erroring, so
      // STOP instead of spinning (a tight re-acquire loop with no delay froze the tab —
      // Chrome "page unresponsive"). The backoff is what prevents the spin; the empty-
      // session cap is the give-up. A healthy stream never ends between reads, so this
      // loop just sits inside the inner read() for the life of the connection.
      let emptySessions = 0;
      while (this._role === 'leader' && this._port && this._port.readable && !this._leaving) {
        let reader = null;
        try { reader = this._reader = this._port.readable.getReader(); }
        catch (_) { this._reader = null; break; }
        let gotData = false;
        try {
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            if (value && value.byteLength) {
              gotData = true;
              this._emit('data', value);             // our own tool
              this._post({ t: 'rx', b: copyBytes(value) });   // followers
            }
          }
        } catch (_) {
          // stream error — recover below unless it's persistent
        } finally {
          try { reader && reader.releaseLock(); } catch (_) {}
          this._reader = null;
        }
        if (this._leaving || this._role !== 'leader') break;
        if (gotData) emptySessions = 0;
        else if (++emptySessions >= 6) break;   // ~6 empty sessions in a row → port dead, give up
        await new Promise(r => setTimeout(r, 250));   // backoff BETWEEN re-acquires — prevents the tight spin
      }
      if (this._portOpen && !this._leaving && this._role === 'leader') {
        this._portOpen = false;
        this._announceState();
        this._log('port read loop ended (unplug?)');
      }
    }

    async _writeToPort(bytes) {
      if (!this._port || !this._port.writable) throw new Error('port not open');
      // Serialize: the WritableStream allows only ONE active writer, so chain sends.
      const prev = this._sendChain;
      let release;
      this._sendChain = new Promise(r => { release = r; });
      try {
        await prev;
        const writer = this._port.writable.getWriter();
        try { await writer.write(bytes); }
        finally { writer.releaseLock(); }
      } finally {
        release();
      }
    }

    async _closePort() {
      this._portOpen = false;
      if (this._reader) {
        try { await this._reader.cancel(); } catch (_) {}
        try { this._reader.releaseLock(); } catch (_) {}
        this._reader = null;
      }
      if (this._port) { try { await this._port.close(); } catch (_) {} }
      // keep this._port + this._leaderPortInfo so a re-join can reopen without re-picking
    }

    // ── Visibility-driven leadership handoff ────────────────────────────────────
    _isVisible() {
      try { return document.visibilityState === 'visible'; } catch (_) { return true; }
    }

    _onVisibilityChange() {
      if (this._leaving) return;
      if (this._isVisible()) {
        if (this._role !== 'leader') this._claimLeadership();
      } else {
        if (this._claimTimer) { clearTimeout(this._claimTimer); this._claimTimer = null; }
        // A leader going to the background invites a visible tab to take over so telemetry
        // doesn't drop to the background-throttled ~1 Hz.
        if (this._role === 'leader') this._post({ t: 'yield' });
      }
    }

    _claimLeadership() {
      if (this._claimTimer) { clearTimeout(this._claimTimer); this._claimTimer = null; }
      // Debounce: only claim if we STAY visible briefly, so a quick alt-tab flick doesn't
      // force a handoff (each handoff reopens the port → resets the tethered board).
      this._claimTimer = setTimeout(() => {
        this._claimTimer = null;
        if (!this._leaving && this._isVisible() && this._role !== 'leader') {
          this._log('foreground — claiming the shared port');
          this._post({ t: 'claim' });
        }
      }, 600);
    }

    // Hidden leader gives up the port so a visible tab can own it (and read un-throttled).
    async _relinquish() {
      if (this._role !== 'leader') return;
      this._log('handing the shared port to the foreground tab');
      if (this._portOpen) this._sawLeaderPort = true;   // a port exists → the claimer can adopt it
      await this._closePort();                          // free the physical port for the claimer
      this._setRole('follower');
      this._leaderPortOpen = false;
      const release = this._releaseLock; this._releaseLock = null;
      if (release) release();     // release the lock → the queued claimer promotes to leader
      this._maybeCampaign();      // re-enter the queue so we can reclaim leadership later (only while still eligible)
    }

    // ── Cross-tab bus ───────────────────────────────────────────────────────────

    _post(msg) { if (this._bc) { try { this._bc.postMessage(msg); } catch (_) {} } }

    _onBusMessage(msg) {
      if (!msg || this._leaving) return;
      switch (msg.t) {
        case 'rx':                                   // leader → followers: bytes read off the port
          if (this._role !== 'leader' && msg.b) this._emit('data', new Uint8Array(msg.b));
          break;
        case 'tx':                                   // follower → leader: bytes to write
          if (this._role === 'leader' && msg.b)
            this._writeToPort(new Uint8Array(msg.b)).catch(e => this._log('relayed write failed: ' + (e && e.message || e)));
          break;
        case 'state':                                // leader announcing port state
          if (this._role !== 'leader') {
            this._leaderPortOpen = !!msg.portOpen;
            if (msg.portInfo) this._leaderPortInfo = msg.portInfo;
            if (msg.portOpen && msg.portInfo) { this._sawLeaderPort = true; this._portlessRetries = 0; this._maybeCampaign(); }  // a real leader owns a port → become failover-eligible + enter the election; a device that came back resets the step-down backoff
            this._emitState();
          }
          break;
        case 'hello':                                // a tab joined — leader re-announces
          if (this._role === 'leader') this._announceState();
          break;
        case 'claim':                                // a now-VISIBLE tab wants to own the port
          // Hand off only if WE are the leader AND we're hidden (background-throttled). A
          // visible leader keeps it — no throttling to escape, and relinquishing would just
          // thrash (and reset the board) between two live tabs.
          if (this._role === 'leader' && !this._isVisible()) this._relinquish();
          break;
        case 'yield':                                // the (now-hidden) leader invites a visible tab to take over
          if (this._role !== 'leader' && this._isVisible()) this._claimLeadership();
          break;
        case 'bye':
          break;
      }
    }

    _announceState() {
      this._post({ t: 'state', portOpen: this._portOpen, portInfo: this._leaderPortInfo });
      this._emitState();
    }

    // ── Events ──────────────────────────────────────────────────────────────────

    _setRole(role) { if (role !== this._role) { this._role = role; this._emit('role', role); this._emitState(); } }
    _emitState() {
      this._emit('state', {
        role: this._role,
        portOpen: this.portOpen,
        leader: this._role === 'leader',
        portInfo: this._leaderPortInfo,
      });
    }
    _log(m) { this._emit('log', m); }
    _emit(event, arg) {
      const ls = this._listeners[event];
      if (!ls) return;
      for (const cb of ls) { try { cb(arg); } catch (e) { /* a listener throwing must not break the hub */ } }
    }
  }

  window.WcbSerialHub = WcbSerialHub;
})();
