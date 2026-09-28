// Record/replay clips for the emulator (lib/navicore/emulator.js): NaviCore's ?REC CLI (NaviCore.ino:3440-3595) over
// its clip store (navicore_record.h): the list, record / stop / save / play, rename and delete, the ranged and batched
// download (editStream :730-879) and the indexed upload (editBegin / editAddEvent / editEnd :881-947). Relayed
// (via-wcb) replies travel as [TERM:20] lines hard-wrapped at 160 bytes (navicore_rterm.h:48-53), and a relayed
// EDITLOAD is capped at 512 events a slice and refused whole above 3000 events (NaviCore.ino:3547-3570).
//
// emu.clips       Map name -> { mode, events } — the files on the clips partition, events as the firmware prints them
//                 ({t,k,...}: k 0 an action in actionToJson's shape, 1 a Maestro keyframe, 2 an HCR-volume keyframe)
// emu.rec         the recorder: state (idle | recording | replaying | editing), the resident buffer (events, mode),
//                 loadedName / loadedFc (the ranged download's residency, cleared by anything that changes the buffer)
// emu.clipFault   dropLine   Set of event indices: the download line carrying one is lost, once (a batch line loses
//                            every keyframe it carries, as a real lost line does)
//                 dropAlways Set of event indices lost on every download
//                 lostAck    Set of EDITEV indices whose ACK is lost, once (the write happened)
//                 holdFrom   an EDITEV index: that line and every later one are not answered (the upload stalls)
//                 cap        the recorder's buffer capacity (loadClip truncates a longer file silently)
//                 fsTotal    the clips partition size [CLIPFS] reports (default 12 MB)
// emu.clipReplace(name, events[, mode]) rewrites a clip on flash (a Record/Play trigger saving a new take) and clears
// the residency, so the next range reloads it.
const { actionFrom } = require('./model');

const EV_BYTES = 140, HDR_BYTES = 16;              // the tool's own estimate of a RecEvent on flash (index.html:6919)
const RTERM_TEXT_SIZE = 160;                       // navicore_rterm.h:21
const MAX_RELAY_SLICE = 512, MAX_RELAY_WHOLE = 3000;

const isObj = (o) => o !== null && typeof o === 'object' && !Array.isArray(o);
const jInt = (v, d) => (typeof v === 'number' && Number.isInteger(v) ? v : d);

// _clipPath (navicore_record.h:522-533): only [A-Za-z0-9_-] survive, 32 at most; nothing left is no name at all.
function clipName(name) { return String(name || '').replace(/[^A-Za-z0-9_-]/g, '').slice(0, 32); }

// editAddEvent's parse (navicore_record.h:894-925), as editStream prints it back: null when the firmware NAKs it.
function normEvent(obj) {
  if (!isObj(obj)) return null;
  const t = jInt(obj.t, 0) >>> 0;
  const k = obj.k === undefined ? 0xFF : (jInt(obj.k, 0xFF) & 0xFF);
  if (k === 0) { const a = actionFrom(obj); return a ? { ...a, t, k: 0 } : null; }
  if (k === 1) return { t, k: 1, slot: jInt(obj.slot, 0) & 0xFF, ch: jInt(obj.ch, 0) & 0xFF, pos: jInt(obj.pos, 0) & 0xFFFF };
  if (k === 2) return { t, k: 2, chan: jInt(obj.chan, 0) & 0xFF, vol: jInt(obj.vol, 0) & 0xFF };
  return null;
}

function fnv(str) {
  let h = 0x811c9dc5;
  for (const b of Buffer.from(str, 'utf8')) { h ^= b; h = Math.imul(h, 0x01000193) >>> 0; }
  return h >>> 0;
}
const hex8 = (n) => n.toString(16).toUpperCase().padStart(8, '0');
const durOf = (events) => (events.length ? events[events.length - 1].t : 0);   // clipDurationMs: the LAST slot
const bytesOf = (c) => HDR_BYTES + EV_BYTES * c.events.length;

const methods = {
  _clipsInit(opts) {
    this.clips = new Map();
    for (const [name, c] of Object.entries(opts.clips || {})) {
      this.clips.set(name, { mode: c.mode || 1, events: c.events.map((e) => { const n = normEvent(e); if (!n) throw new Error(`clip ${name}: bad event ${JSON.stringify(e)}`); return n; }) });
    }
    this.rec = { state: 'idle', events: [], mode: 1, loadedName: '', loadedFc: 0 };
    this.clipFault = {};
    this.clipsFs = opts.clipsFs !== false;          // g_clipsReady: [CLIPFS] only when the partition mounted
    this.recLog = [];                               // { op, ... } for the specs
    this._recReplayTimer = null;
    this._ulHeld = null;
  },

  clipReplace(name, events, mode) {
    const old = this.clips.get(name);
    this.clips.set(name, { mode: mode || (old && old.mode) || 1, events: events.map(normEvent) });
    this.rec.loadedName = '';
  },
  clipRelease() {
    const held = this._ulHeld || [];
    this._ulHeld = null;
    delete this.clipFault.holdFrom;
    for (const fn of held) fn();
  },
  _clipFire(name, key) {
    const s = this.clipFault[name];
    if (s instanceof Set && s.has(key)) { s.delete(key); return true; }
    return false;
  },

  // A relayed CLI line reaches the tool as [TERM:20] packets of at most 160 bytes: CaptureSink flushes at 160 and
  // again at the newline, so a line of exactly 160 bytes is followed by an empty packet.
  _termOut(line) {
    const b = Buffer.from(line, 'utf8');
    const parts = [];
    for (let i = 0; i < b.length; i += RTERM_TEXT_SIZE) parts.push(b.subarray(i, i + RTERM_TEXT_SIZE));
    if (b.length % RTERM_TEXT_SIZE === 0) parts.push(Buffer.alloc(0));
    for (const p of parts) this._relay('[TERM:20]' + p.toString('utf8'));
  },

  _recLoad(name) {
    const c = this.clips.get(clipName(name));
    if (!c || this.rec.state !== 'idle') return false;
    const cap = this.clipFault.cap || 24000;
    this.rec.events = c.events.slice(0, cap).map((e) => ({ ...e }));
    this.rec.mode = c.mode;
    this.rec.loadedName = clipName(name);
    this.rec.loadedFc = c.events.length;
    return this.rec.events.length > 0;
  },
  _recSave(name) {
    const nm = clipName(name);
    if (this.rec.state !== 'idle') return `[REC] save aborted: recorder busy (state=${this.rec.state})`;
    if (!this.rec.events.length) return '[REC] save aborted: clip is empty';
    if (!nm) return `[REC] save aborted: bad clip name '${name}'`;
    this.clips.set(nm, { mode: this.rec.mode, events: this.rec.events.map((e) => ({ ...e })) });
    this.recLog.push({ op: 'save', name: nm, mode: this.rec.mode, n: this.rec.events.length });
    return null;
  },
  _recInfo(out) {
    const st = { recording: 'RECORDING', replaying: 'REPLAYING', editing: 'EDITING' }[this.rec.state] || 'idle';
    out(`[REC] state=${st}  events=${this.rec.events.length}/${this.clipFault.cap || 24000}  dur=${durOf(this.rec.events)}ms  drops=0  buf=ok`);
  },
  // What a spec's transmitter "does" while recording: appended as the recorder would capture it.
  recCapture(events) { if (this.rec.state === 'recording') for (const e of events) this.rec.events.push(normEvent(e)); },

  // ?REC,... (NaviCore.ino:3444-3595). `relayed` = the capture sink is armed (the line came over the mesh).
  _recCli(line, out, relayed) {
    const arg = (line.length > 5 ? line.substring(5) : '').trim();
    let sub = arg, name = '';
    let sep = arg.indexOf(','); if (sep < 0) sep = arg.indexOf(' ');
    if (sep >= 0) { sub = arg.substring(0, sep).trim(); name = arg.substring(sep + 1).trim(); }
    const S = sub.toUpperCase();
    const r = this.rec;
    this.recLog.push({ op: S || 'INFO', arg: name });
    if (S === 'START') {
      if (r.state !== 'idle') return out('[REC] busy / no buffer');
      r.state = 'recording'; r.events = []; r.mode = this.mode_; r.loadedName = '';
      return out('[REC] recording…');
    }
    if (S === 'STOP') {
      if (r.state === 'replaying') { clearTimeout(this._recReplayTimer); r.state = 'idle'; return out('[REC] replay stopped'); }
      if (r.state === 'recording') r.state = 'idle';
      return this._recInfo(out);
    }
    if (S === 'PLAY') {
      if (name && !this._recLoad(name)) return out(`[REC] clip '${name}' not found`);
      if (r.state !== 'idle' || !r.events.length) return out('[REC] busy / empty');
      r.state = 'replaying';
      const dur = durOf(r.events);
      this._recReplayTimer = this.later(Math.max(1, dur), () => { if (r.state === 'replaying') r.state = 'idle'; });
      return out(`[REC] replaying ${r.events.length} events over ${dur}ms — ?REC,STOP to abort`);
    }
    if (S === 'SAVE') {
      let nm = name;
      if (!nm) { let i = 1; while (this.clips.has(`rec_${i}`)) i++; nm = `rec_${i}`; }
      const err = this._recSave(nm);
      if (err) { out(err); return out('[REC] save failed (see reason above)'); }
      return out(`[REC] saved clip '${nm}'`);
    }
    if (S === 'LOAD') return out(this._recLoad(name) ? '[REC] loaded' : '[REC] load failed (not found / no FS)');
    if (S === 'LS') {
      if (this.clipsFs) {
        const used = [...this.clips.values()].reduce((s, c) => s + bytesOf(c), 0);
        out(`[CLIPFS]{"total":${this.clipFault.fsTotal || 12 * 1024 * 1024},"used":${used}}`);
      }
      out('[REC] clips:');
      out('[CLIPLIST:BEGIN]');
      for (const [nm, c] of this.clips) out(`[CLIPITEM]{"name":"${nm}","bytes":${bytesOf(c)},"dur":${durOf(c.events)},"n":${c.events.length}}`);
      return out('[CLIPLIST:END]');
    }
    if (S === 'RM') {
      const nm = clipName(name);
      r.loadedName = '';
      const ok = !!nm && this.clips.delete(nm);
      return out(ok ? '[REC] deleted' : '[REC] delete failed');
    }
    if (S === 'RENAME') {
      const c2 = name.indexOf(',');
      if (c2 <= 0) return out('[REC] usage: ?REC,RENAME,<from>,<to>');
      const from = clipName(name.substring(0, c2)), to = clipName(name.substring(c2 + 1));
      let ok = false;
      if (from && to && this.clips.has(from) && !this.clips.has(to)) {
        r.loadedName = '';
        const c = this.clips.get(from);
        this.clips.delete(from);
        this.clips.set(to, c);
        ok = true;
      }
      out(ok ? '[REC] renamed' : '[REC] rename failed (exists / not found)');
      return out(`[CLIPUL:RENAME,${ok ? 'OK' : 'ERR'}]`);
    }
    if (S === 'EDITLOAD') return this._recEditLoad(name, out, relayed);
    if (S === 'EDITBEGIN') {
      if (r.state !== 'idle') return out('[CLIPUL:BEGIN,ERR,busy]');
      r.events = []; r.loadedName = ''; r.state = 'editing';          // _mode is left as it was (D-NC33)
      return out('[CLIPUL:BEGIN,OK]');
    }
    if (S === 'EDITEV') {
      const ci = name.indexOf(',');
      const idx = ci > 0 ? parseInt(name.substring(0, ci), 10) || 0 : -1;
      const reply = () => {
        let ev = null;
        if (ci > 0 && r.state === 'editing' && idx <= r.events.length) {
          try { ev = normEvent(JSON.parse(name.substring(ci + 1))); } catch (_) { ev = null; }
        }
        if (!ev) { this.recLog.push({ op: 'nak', idx }); return out('[CLIPUL:NAK,bad event / bad index / not editing]'); }
        r.events[idx] = ev;                           // at the index: a resend overwrites, never appends
        if (this._clipFire('lostAck', idx)) { this.recLog.push({ op: 'lostAck', idx }); return; }
        out(`[CLIPUL:ACK,${name.substring(0, ci)}]`);
      };
      if (this.clipFault.holdFrom !== undefined && idx >= this.clipFault.holdFrom) {
        (this._ulHeld = this._ulHeld || []).push(reply);
        return;
      }
      return reply();
    }
    if (S === 'EDITEND') {
      if (r.state !== 'editing') return out('[CLIPUL:END,ERR,not-editing]');
      r.state = 'idle';
      if (!r.events.length) return out('[CLIPUL:END,ERR,empty-clip]');
      if (r.events.some((e, i) => i && e.t < r.events[i - 1].t)) r.events.sort((a, b) => a.t - b.t);
      const err = this._recSave(name);
      if (err) { out(err); return out('[CLIPUL:END,ERR,save-failed]'); }
      return out('[CLIPUL:END,OK]');
    }
    if (S === 'EDITCANCEL') { if (r.state === 'editing') r.state = 'idle'; return out('[CLIPUL:CANCEL,OK]'); }
    if (S === 'CLEAR') { r.events = []; r.loadedName = ''; return out('[REC] cleared'); }
    return this._recInfo(out);
  },

  // ?REC,EDITLOAD,<name>[,<from>[,<count>[,B]]] (NaviCore.ino:3495-3570) and editStream (navicore_record.h:730-879).
  _recEditLoad(arg, out, relayed) {
    const r = this.rec;
    let cname = arg, from = 0, want = 0xFFFFFFFF, ranged = false, batch = false;
    const c2 = arg.indexOf(',');
    if (c2 > 0) {
      cname = arg.substring(0, c2);
      const rest = arg.substring(c2 + 1).trim();
      const c3 = rest.indexOf(',');
      from = (parseInt(c3 > 0 ? rest.substring(0, c3) : rest, 10) || 0) >>> 0;
      if (c3 > 0) {
        let cw = rest.substring(c3 + 1);
        const c4 = cw.indexOf(',');
        if (c4 >= 0) { batch = cw.substring(c4 + 1).trim().toUpperCase() === 'B'; cw = cw.substring(0, c4); }
        want = (parseInt(cw, 10) || 0) >>> 0;
      }
      ranged = true;
    }
    if (r.loadedName !== clipName(cname) && !this._recLoad(cname)) return out(`[REC] clip '${cname}' not found`);
    if (relayed) {
      if (want > MAX_RELAY_SLICE) want = MAX_RELAY_SLICE;
      if (!ranged && r.events.length > MAX_RELAY_WHOLE) {
        return out(`[CLIPDL:ERR]clip too large to edit over the WCB bridge (${r.events.length} events) — connect over USB`);
      }
    }
    const count = r.events.length;
    if (from > count) from = count;
    const end = want > count - from ? count : from + want;
    const n = end - from;
    const fp = hex8(fnv(JSON.stringify([r.mode, r.events])));
    this.recLog.push({ op: 'range', name: r.loadedName, from, n, batch, relayed });
    if (ranged) {
      out(`[CLIPDL:BEGIN]{"count":${count},"durationMs":${durOf(r.events)},"mode":${r.mode},"from":${from},"n":${n},"fp":"${fp}","fc":${r.loadedFc},"nm":"${r.loadedName}"}`);
    } else {
      out(`[CLIPDL:BEGIN]{"count":${count},"durationMs":${durOf(r.events)},"mode":${r.mode}}`);
    }
    const lost = (idxs) => {
      let hit = false;
      for (const i of idxs) {
        if (this._clipFire('dropLine', i)) hit = true;
        const a = this.clipFault.dropAlways;
        if (a instanceof Set && a.has(i)) hit = true;
      }
      if (hit) this.recLog.push({ op: 'dropped', idxs });
      return hit;
    };
    let body = '', first = 0, idxs = [];
    const flush = () => {
      if (!body) return;
      if (!lost(idxs)) out(`[CLIPDL:EVB,${first}]{"e":[${body}]}`);
      body = ''; idxs = [];
    };
    for (let i = from; i < end; i++) {
      const ev = r.events[i];
      if (ranged && batch && ev.k !== 0) {
        if (body && body.length + 30 > 120) flush();
        if (!body) first = i;
        body += (body ? ',' : '') + (ev.k === 1 ? `[${ev.t},1,${ev.slot},${ev.ch},${ev.pos}]` : `[${ev.t},2,${ev.chan},${ev.vol},0]`);
        idxs.push(i);
        continue;
      }
      if (ranged && batch) flush();
      if (lost([i])) continue;
      out(`${ranged ? `[CLIPDL:EV,${i}]` : '[CLIPDL:EV]'}${JSON.stringify(ev)}`);
    }
    if (ranged && batch) flush();
    if (ranged) out(`[CLIPDL:END]{"from":${from},"n":${n},"fp":"${fp}","nm":"${r.loadedName}"}`);
    else out('[CLIPDL:END]');
  },
};

module.exports = { methods, normEvent, clipName, EV_BYTES };
