// NaviCore's RcConfig, in JS: rcConfigLoadDefaults, rcConfigFromJSON and rcConfigToJSON (NaviCore rc_config.h:801-966,
// :1502-1905, :1211-1480; working tree 2026-09-28), so the emulator stores a SET_CONFIG and prints GET_CONFIG the way
// the firmware does. The state mirrors the C struct rather than the JSON, because the printer's sparse rules depend on
// struct-level emptiness: a mapping with no actions, no note and exclusive false is left out (:1262), a Maestro slot's
// "channels" only when one is set (:1340-1341), "serialLabels" only when a label is (:1419-1426). A SET_CONFIG that
// omits a key leaves it alone, and one that names a mapping rebuilds that mapping from scratch (memset, :1580-1603):
// the trap nc_guard is built around.
//
// ArduinoJson 7's `|` returns the default unless the value has the right TYPE (JsonVariant::operator|, is<T>()):
// jInt/jBool/jStr below. So a numeric "target" reads as "", a skipRunning of 1 reads as false, a string delay as 0 —
// the firmware's own behaviour, which a spec can then observe. strlcpy truncates at BYTES: strl() cuts at the byte
// limit and drops a trailing partial UTF-8 character.
//
// No real credential is ever written here. The compile-time mesh password (wcb_config.h) is replaced by FACTORY_PW.

const THRESHOLDS = 36, TAP_TIERS = 4, ACTIONS = 5, SWITCHES = 10, KNOBS = 11, KNOB_OUTS = 10;
const MAESTROS = 8, MAE_CH = 32, WLED = 4, PROFILES = 6, SMOOTH = 6, MAX_BOARDS = 20;
const SWITCH_LABELS = ['SA', 'SB', 'SC', 'SD', 'SE', 'SF', 'SG', 'SH', 'SI', 'SJ'];
const SWITCH_DEFAULT_CH = [8, 9, 10, 11, 12, 13, 14, 15, 0, 0];
const SWITCH_DEFAULT_POS = [3, 3, 3, 3, 3, 2, 3, 2, 2, 2];
const KNOB_LABELS = ['S1', 'S2', 'LS', 'RS', 'S3', 'J1', 'J2', 'J3', 'J4', 'J5', 'J6'];
const KNOB_DEFAULT_CH = [5, 6, 0, 0, 0, 1, 2, 3, 4, 0, 0];
const SLBL_KEYS = ['S3', 'S4', 'S5', 'maestro'];
const SW_SE = 4;
const LOCAL_MAESTRO_BAUD = 115200;          // wcb_config.h:43
const FACTORY_PW = 'HILfactoryPass';         // stands in for the compile-time WCB_PASSWORD (never the real one)
const DEFAULT_BANDS = [
  ['B1', 1799, 1823], ['B2', 1758, 1782], ['B3', 1718, 1742], ['B4', 1676, 1700], ['B5', 1634, 1658],
  ['B6', 1594, 1618], ['T4 Left', 1553, 1577], ['T4 Right', 1512, 1536], ['T5 Left', 1471, 1495],
  ['T5 Right', 1430, 1454], ['T3 Up', 1389, 1413], ['T3 Down', 1348, 1372], ['T2 Up', 1308, 1332],
  ['T2 Down', 1266, 1290], ['T6 Left', 1225, 1249], ['T6 Right', 1184, 1208], ['T1 Left', 1143, 1167],
  ['T1 Right', 1103, 1127], ['L-Stick Click', 1062, 1086], ['R-Stick Click', 1021, 1045], ['Unassigned', 0, 0],
];
for (let i = 1; i <= 15; i++) DEFAULT_BANDS.push([`Logical ${i}`, 0, 0]);

const has = (o, k) => o !== null && typeof o === 'object' && !Array.isArray(o) && Object.prototype.hasOwnProperty.call(o, k);
const isObj = (o) => o !== null && typeof o === 'object' && !Array.isArray(o);
// ArduinoJson 7 operator| : the value when it is of the asked type, else the default.
const jInt = (v, d) => (typeof v === 'number' && Number.isInteger(v) ? v : d);
const jBool = (v, d) => (typeof v === 'boolean' ? v : d);
const jStr = (v, d) => (typeof v === 'string' ? v : d);
// as<int>() / an implicit numeric conversion: a number truncates, anything else is 0.
const asInt = (v) => (typeof v === 'number' && Number.isFinite(v) ? Math.trunc(v) : (typeof v === 'boolean' ? +v : 0));
const u8 = (n) => n & 0xFF;
const u16 = (n) => n & 0xFFFF;
const i8 = (n) => ((n & 0xFF) << 24) >> 24;
const arr = (v) => (Array.isArray(v) ? v : []);
// strlcpy(dst, s, size): at most size-1 BYTES, never half a UTF-8 character.
function strl(s, size) {
  const b = Buffer.from(String(s), 'utf8');
  if (b.length <= size - 1) return String(s);
  let n = size - 1;
  while (n > 0 && (b[n] & 0xC0) === 0x80) n--;
  return b.subarray(0, n).toString('utf8');
}

const emptyTier = () => ({ note: '', a: [] });
const emptyMapping = () => ({ exclusive: false, t: Array.from({ length: TAP_TIERS }, emptyTier) });

// actionFromJson (rc_config.h:1063-1156) -> the action as actionToJson prints it (:970-1061), or null when the type
// is not one the firmware knows (it is then dropped, and takes no slot).
function actionFrom(obj) {
  if (!isObj(obj)) return null;
  const type = jStr(obj.type, '');
  const delay = u16(jInt(obj.delay, 0));
  let a = null;
  if (type === 'wcb_unicast') {
    a = { type, target: strl(jStr(obj.target, ''), 6), cmd: strl(jStr(obj.cmd, ''), 96) };
    if (delay) a.delay = delay;
    if (jBool(obj.skipRunning, false)) a.skipRunning = true;
  } else if (type === 'wcb_broadcast') {
    a = { type, cmd: strl(jStr(obj.cmd, ''), 96) };
    if (delay) a.delay = delay;
    if (jBool(obj.skipRunning, false)) a.skipRunning = true;
  } else if (type === 'maestro_local') {
    a = { type, cmd: strl(jStr(obj.cmd, ''), 96) };
    if (delay) a.delay = delay;
  } else if (type === 'maestro' || type === 'maestro_remote') {
    a = { type: 'maestro', target: strl(jStr(obj.target, ''), 6), cmd: strl(jStr(obj.cmd, ''), 96) };
    if (delay) a.delay = delay;
    if (jBool(obj.skipRunning, false)) a.skipRunning = true;
  } else if (type === 'serial') {
    a = { type, port: strl(jStr(obj.port, ''), 6), cmd: strl(jStr(obj.cmd, ''), 96) };
    if (delay) a.delay = delay;
  } else if (type === 'hcr' || type === 'dfplayer') {
    a = { type, fn: u8(jInt(obj.fn, 0)), chan: i8(jInt(obj.chan, 0)), track: (jInt(obj.track, 0) << 16) >> 16 };
    if (delay) a.delay = delay;
  } else if (type === 'mp3') {
    a = { type, fn: u8(jInt(obj.fn, 0)), track: (jInt(obj.track, 0) << 16) >> 16 };
    if (delay) a.delay = delay;
  } else if (type === 'wled' || type === 'record') {
    a = { type, cmd: strl(jStr(obj.cmd, ''), 96) };
    if (delay) a.delay = delay;
  } else if (type === 'play') {
    a = { type, cmd: strl(jStr(obj.cmd, ''), 96), fn: u8(jInt(obj.fn, 0)) };
    if (delay) a.delay = delay;
  } else if (type === 'stop') {
    a = { type };
    if (delay) a.delay = delay;
  }
  if (!a) return null;
  const note = strl(jStr(obj.note, ''), 20);
  if (note) a.note = note;
  return a;
}

// Up to ACTIONS valid actions from a JSON array, invalid ones skipped (rc_config.h:1596-1601).
function actionsFrom(list) {
  const out = [];
  for (const o of arr(list)) {
    if (out.length >= ACTIONS) break;
    const a = actionFrom(o);
    if (a) out.push(a);
  }
  return out;
}

function knobOutsFrom(list) {   // rcReadKnobOuts (:1174-1193)
  const out = [];
  for (const o of arr(list)) {
    if (out.length >= KNOB_OUTS) break;
    const p = isObj(o) ? o : {};
    let target;
    if (has(p, 'target')) target = u8(jInt(p.target, 0));
    else { const legacy = u8(jInt(p.slot, 0)); target = legacy === 0 ? 1 : legacy; }
    out.push({
      target, maestroCh: u8(jInt(p.maestroCh, 0)), posMin: u16(jInt(p.posMin, 4000)), posMax: u16(jInt(p.posMax, 8000)),
      smoothSpeed: u16(jInt(p.smoothSpeed, 0)), smoothAccel: u8(jInt(p.smoothAccel, 0)),
      midClosed: jBool(p.midClosed, false), releaseIdleMs: u16(jInt(p.releaseIdleMs, 0)),
    });
  }
  return out;
}
function knobOutsTo(outs) {      // rcWriteKnobOuts (:1158-1171)
  return outs.slice(0, KNOB_OUTS).map((o) => {
    const j = { target: o.target, maestroCh: o.maestroCh, posMin: o.posMin, posMax: o.posMax };
    if (o.midClosed) j.midClosed = true;
    if (o.releaseIdleMs) j.releaseIdleMs = o.releaseIdleMs;
    return j;
  });
}

const sanBaud = (b, d) => (b >= 1200 && b <= 115200 ? b : d);   // rcSanBaud (:1495-1497)
const transportCode = (tp) => (tp === 'off' ? 2 : tp === 'wcb' ? 1 : 0);
const transportName = (c) => (c === 2 ? 'off' : c === 1 ? 'wcb' : 'serial');

class NcConfig {
  constructor(opts = {}) {
    this.factoryPassword = opts.factoryPassword || FACTORY_PW;
    this.loadDefaults();
  }

  loadDefaults() {   // rcConfigLoadDefaults (:801-966)
    const s = {};
    Object.assign(s, {
      txModel: 0, threeAxisGimbals: false, sbusOutEnabled: false, wifiEnabled: false, wifiSsid: '', wifiPassword: '',
      maeGateMs: 250, boardType: 0, tapWindowMs: 500, holdMs: 750, switchSettleMs: 80, chRateHz: 5, matrixChannel: 7,
      matrixDebounceFrames: 1, modeSwitch: SW_SE, peerActions: [], peerAlert: true,
    });
    s.thresholds = DEFAULT_BANDS.map(([label, mn, mx], i) => ({ id: i + 1, label, minPwm: mn, maxPwm: mx }));
    s.mappings = Array.from({ length: 3 * THRESHOLDS }, emptyMapping);
    s.switches = SWITCH_LABELS.map((_, i) => ({
      channel: SWITCH_DEFAULT_CH[i], positions: SWITCH_DEFAULT_POS[i], t: [emptyTier(), emptyTier(), emptyTier()],
    }));
    s.knobs = KNOB_LABELS.map((_, i) => ({
      channel: KNOB_DEFAULT_CH[i], function: 0, reverse: false, modeAware: false, modeSwitchOverride: -1,
      smoothProfile: -1, easeSwitchOverride: false, outputs: [], outputs2: [], outputs3: [],
    }));
    s.hcrDest = { transport: 2, target: 'S3', wcbPort: 1 };
    s.mp3Dest = { transport: 2, target: '2' };
    s.dfpDest = { transport: 2, target: 'S3' };
    s.wledSlots = Array.from({ length: WLED }, () => ({ wledID: 0, serialPort: 0, remoteWCB: 0, configured: false }));
    s.auxBaud = [9600, 9600, 9600];
    s.maestroBaud = LOCAL_MAESTRO_BAUD;
    s.serialLabels = ['', '', '', ''];
    s.bcastOut = [false, false, false];
    s.bcastIn = [false, false, false];
    s.modeReport = { enabled: false, wcb: 0, tmpl: '', cmds: ['', '', ''] };
    s.statsReport = { enabled: false, wcb: 0 };
    s.maestros = Array.from({ length: MAESTROS }, (_, i) => ({
      type: 0, device: 1 + i, channels: Array.from({ length: MAE_CH }, () => ({ name: '', min: 0, max: 0 })),
    }));
    s.smooth = Array.from({ length: SMOOTH }, (_, p) => ({
      name: p === 0 ? 'Default' : '',
      e: Array.from({ length: MAESTROS }, () => Array.from({ length: 32 }, () => ({ speed: 0, accel: 0 }))),
    }));
    s.wcbNetwork = { macOct2: 0, macOct3: 0, password: this.factoryPassword, quantity: 4, deviceId: 20, channel: 1 };
    s.wcbProfiles = [];
    this.s = s;
  }

  // rcConfigFromJSON (:1502-1905). Returns true, as the firmware does for any object.
  fromJSON(doc) {
    const s = this.s;
    if (!isObj(doc)) return true;
    if (has(doc, 'txModel')) s.txModel = u8(jInt(doc.txModel, 0));
    if (has(doc, 'threeAxisGimbals')) s.threeAxisGimbals = jBool(doc.threeAxisGimbals, false);
    if (has(doc, 'sbusOutEnabled')) s.sbusOutEnabled = jBool(doc.sbusOutEnabled, false);
    if (has(doc, 'wifiEnabled')) s.wifiEnabled = jBool(doc.wifiEnabled, false);
    if (has(doc, 'wifiSsid')) s.wifiSsid = strl(jStr(doc.wifiSsid, s.wifiSsid), 33);
    if (has(doc, 'wifiPassword')) s.wifiPassword = strl(jStr(doc.wifiPassword, s.wifiPassword), 64);
    if (has(doc, 'maeGateMs')) s.maeGateMs = u16(jInt(doc.maeGateMs, 250));
    if (has(doc, 'boardType')) s.boardType = u8(jInt(doc.boardType, 0));
    if (has(doc, 'tapWindowMs')) s.tapWindowMs = u16(asInt(doc.tapWindowMs));
    if (s.tapWindowMs < 100) s.tapWindowMs = 500;
    if (has(doc, 'holdMs')) s.holdMs = u16(asInt(doc.holdMs));
    if (s.holdMs < s.tapWindowMs + 100) s.holdMs = s.tapWindowMs + 250;
    if (s.holdMs > 5000) s.holdMs = 5000;
    if (has(doc, 'switchSettleMs')) { const v = jInt(doc.switchSettleMs, 80); s.switchSettleMs = v < 0 ? 0 : v > 1000 ? 1000 : v; }
    if (has(doc, 'chRateHz')) { let hz = jInt(doc.chRateHz, 5); if (hz < 1) hz = 5; if (hz > 20) hz = 20; s.chRateHz = hz; }
    if (has(doc, 'matrixChannel')) s.matrixChannel = asInt(doc.matrixChannel);
    if (has(doc, 'matrixDebounceFrames')) { const d = jInt(doc.matrixDebounceFrames, 1); s.matrixDebounceFrames = d < 1 ? 1 : d > 4 ? 4 : d; }
    if (has(doc, 'funcBindings')) s.modeSwitch = i8(jInt(isObj(doc.funcBindings) ? doc.funcBindings.mode : undefined, s.modeSwitch));
    if (has(doc, 'peerEvent')) {
      const pe = isObj(doc.peerEvent) ? doc.peerEvent : {};
      s.peerAlert = jBool(pe.alert, s.peerAlert);
      if (has(pe, 'actions')) s.peerActions = actionsFrom(pe.actions);
    }
    if (has(doc, 'thresholds')) {
      arr(doc.thresholds).slice(0, THRESHOLDS).forEach((th0, i) => {
        const th = isObj(th0) ? th0 : {};
        s.thresholds[i] = { id: jInt(th.id, i + 1), label: strl(jStr(th.label, ''), 24),
                            minPwm: jInt(th.minPwm, 0), maxPwm: jInt(th.maxPwm, 0) };
      });
    }
    if (has(doc, 'mappings') && isObj(doc.mappings)) {
      for (const [key, val] of Object.entries(doc.mappings)) {
        const id = parseInt(key, 10) || 0;   // String::toInt: leading digits, else 0
        const mode = Math.trunc(id / 100), btn = id % 100;
        if (mode < 1 || mode > 3 || btn < 1 || btn > THRESHOLDS) continue;
        const m = emptyMapping();              // memset — the whole mapping is rebuilt from what arrived
        const mo = isObj(val) ? val : {};
        m.exclusive = jBool(mo.exclusive, false);
        for (let ti = 0; ti < TAP_TIERS; ti++) {
          m.t[ti].note = strl(jStr(mo[`t${ti + 1}note`], ''), 20);
          if (has(mo, `t${ti + 1}`)) m.t[ti].a = actionsFrom(mo[`t${ti + 1}`]);
        }
        s.mappings[(mode - 1) * THRESHOLDS + (btn - 1)] = m;
      }
    }
    if (has(doc, 'switches') && isObj(doc.switches)) {
      SWITCH_LABELS.forEach((lbl, i) => {
        if (!has(doc.switches, lbl)) return;
        const so = isObj(doc.switches[lbl]) ? doc.switches[lbl] : {};
        const sw = s.switches[i];
        sw.t = [emptyTier(), emptyTier(), emptyTier()];
        sw.channel = jInt(so.channel, SWITCH_DEFAULT_CH[i]);
        sw.positions = u8(jInt(so.positions, SWITCH_DEFAULT_POS[i]));
        for (let pi = 0; pi < 3; pi++) {
          sw.t[pi].note = strl(jStr(so[`p${pi}note`], ''), 20);
          if (has(so, `p${pi}`)) sw.t[pi].a = actionsFrom(so[`p${pi}`]);
        }
      });
    }
    if (has(doc, 'knobs') && isObj(doc.knobs)) {
      KNOB_LABELS.forEach((lbl, i) => {
        if (!has(doc.knobs, lbl)) return;
        const ko = isObj(doc.knobs[lbl]) ? doc.knobs[lbl] : {};
        const kn = s.knobs[i];
        kn.channel = jInt(ko.channel, KNOB_DEFAULT_CH[i]);
        kn.function = u8(jInt(ko.function, 0));
        kn.reverse = jBool(ko.reverse, false);
        kn.modeAware = jBool(ko.modeAware, false);
        kn.modeSwitchOverride = has(ko, 'modeSwitchOverride') ? i8(asInt(ko.modeSwitchOverride)) : -1;
        kn.smoothProfile = has(ko, 'smoothProfile') ? i8(asInt(ko.smoothProfile)) : -1;
        kn.easeSwitchOverride = jBool(ko.easeSwitchOverride, false);
        kn.outputs = has(ko, 'outputs') ? knobOutsFrom(ko.outputs) : [];
        kn.outputs2 = []; kn.outputs3 = [];
        if (kn.modeAware) {
          kn.outputs2 = has(ko, 'outputs2') ? knobOutsFrom(ko.outputs2) : kn.outputs.map((o) => ({ ...o }));
          kn.outputs3 = has(ko, 'outputs3') ? knobOutsFrom(ko.outputs3) : kn.outputs.map((o) => ({ ...o }));
        }
      });
    }
    if (has(doc, 'smoothProfiles')) {
      s.smooth.forEach((p) => { p.name = ''; p.e.forEach((row) => row.forEach((c) => { c.speed = 0; c.accel = 0; })); });
      arr(doc.smoothProfiles).slice(0, SMOOTH).forEach((po0, p) => {
        const po = isObj(po0) ? po0 : {};
        s.smooth[p].name = strl(jStr(po.name, ''), 24);
        for (const e0 of arr(po.entries)) {
          const e = isObj(e0) ? e0 : {};
          const mid = jInt(e.mid, 1), ch = jInt(e.ch, 0);
          if (mid >= 1 && mid <= MAESTROS && ch >= 0 && ch < 32) {
            s.smooth[p].e[mid - 1][ch] = { speed: u16(jInt(e.spd, 0)), accel: u8(jInt(e.acc, 0)) };
          }
        }
      });
    }
    // Legacy per-output smoothing -> profile 0, for a passthrough knob with no profile (:1712-1726).
    s.knobs.forEach((kn) => {
      if (kn.function !== 1 || kn.smoothProfile !== -1) return;
      let any = false;
      for (const set of kn.modeAware ? [kn.outputs, kn.outputs2, kn.outputs3] : [kn.outputs]) {
        for (const o of set) {
          if (!(o.smoothSpeed || o.smoothAccel)) continue;
          if (o.target >= 1 && o.target <= MAESTROS && o.maestroCh < 32) {
            s.smooth[0].e[o.target - 1][o.maestroCh] = { speed: o.smoothSpeed, accel: o.smoothAccel };
            any = true;
          }
        }
      }
      if (any) { kn.smoothProfile = 0; if (!s.smooth[0].name) s.smooth[0].name = 'Default'; }
    });
    if (has(doc, 'maestros')) {
      arr(doc.maestros).slice(0, MAESTROS).forEach((mo0, i) => {
        const mo = isObj(mo0) ? mo0 : {};
        const slot = s.maestros[i];
        slot.type = u8(jInt(mo.type, 0));
        slot.device = has(mo, 'device') ? u8(asInt(mo.device)) : 1 + i;
        if (has(mo, 'channels')) {
          slot.channels = Array.from({ length: MAE_CH }, () => ({ name: '', min: 0, max: 0 }));
          for (const c0 of arr(mo.channels)) {
            const c = isObj(c0) ? c0 : {};
            if (!has(c, 'ch')) continue;
            const ch = asInt(c.ch);
            if (ch < 0 || ch >= MAE_CH) continue;
            slot.channels[ch] = { name: strl(jStr(c.name, ''), 24), min: u16(jInt(c.min, 0)), max: u16(jInt(c.max, 0)) };
          }
        }
      });
    }
    if (has(doc, 'hcrDest')) {
      const h = isObj(doc.hcrDest) ? doc.hcrDest : {};
      s.hcrDest.transport = transportCode(jStr(h.transport, 'serial'));
      if (s.hcrDest.transport === 1) {
        s.hcrDest.target = strl(jStr(h.target, '2'), 6);
        s.hcrDest.wcbPort = u8(jInt(h.wcbPort, 1));
      } else {
        s.hcrDest.target = strl(jStr(h.port, 'S3'), 6);
        s.hcrDest.wcbPort = 0;
      }
    }
    if (has(doc, 'wcbNetwork')) {
      const w = isObj(doc.wcbNetwork) ? doc.wcbNetwork : {};
      const n = s.wcbNetwork;
      if (has(w, 'macOct2')) n.macOct2 = u8(asInt(w.macOct2));
      if (has(w, 'macOct3')) n.macOct3 = u8(asInt(w.macOct3));
      n.password = strl(jStr(w.password, n.password), 40);
      n.quantity = u8(jInt(w.quantity, n.quantity));
      n.deviceId = u8(jInt(w.deviceId, n.deviceId));
      const ch = jInt(w.channel, n.channel);
      n.channel = ch < 1 || ch > 11 ? 1 : ch;
    }
    if (has(doc, 'wcbProfiles')) {
      s.wcbProfiles = arr(doc.wcbProfiles).slice(0, PROFILES).map((o0) => {
        const o = isObj(o0) ? o0 : {};
        const ch = jInt(o.channel, 1);
        return { name: strl(jStr(o.name, ''), 24), macOct2: u8(asInt(o.macOct2)), macOct3: u8(asInt(o.macOct3)),
                 password: strl(jStr(o.password, ''), 40), quantity: u8(jInt(o.quantity, 4)),
                 deviceId: u8(jInt(o.deviceId, 20)), channel: ch < 1 || ch > 11 ? 1 : ch };
      });
    }
    for (const [key, def] of [['mp3Dest', 'wcb'], ['dfpDest', 'serial']]) {
      if (!has(doc, key)) continue;
      const d = isObj(doc[key]) ? doc[key] : {};
      const dst = s[key];
      dst.transport = transportCode(jStr(d.transport, def));
      dst.target = dst.transport === 1 ? strl(jStr(d.target, '2'), 6) : strl(jStr(d.port, 'S3'), 6);
    }
    if (has(doc, 'wledSlots')) {
      const list = arr(doc.wledSlots).slice(0, WLED);
      s.wledSlots = Array.from({ length: WLED }, (_, i) => {
        if (i >= list.length) return { wledID: 0, serialPort: 0, remoteWCB: 0, configured: false };
        const w = isObj(list[i]) ? list[i] : {};
        return { wledID: u8(asInt(w.id)), serialPort: u8(asInt(w.port)), remoteWCB: u8(asInt(w.wcb)),
                 configured: jBool(w.configured, false) };
      });
    }
    if (has(doc, 'auxBaud')) {
      const a = isObj(doc.auxBaud) ? doc.auxBaud : {};
      s.auxBaud = ['S3', 'S4', 'S5'].map((k, i) => sanBaud(jInt(a[k], s.auxBaud[i]), 9600));
      s.maestroBaud = sanBaud(jInt(a.maestro, s.maestroBaud), LOCAL_MAESTRO_BAUD);
    }
    if (has(doc, 'serialLabels')) {
      s.serialLabels = ['', '', '', ''];
      const sl = isObj(doc.serialLabels) ? doc.serialLabels : {};
      for (const [k, v] of Object.entries(sl)) {
        const idx = SLBL_KEYS.indexOf(k);
        if (idx >= 0) s.serialLabels[idx] = strl(jStr(v, ''), 25);
      }
    }
    if (has(doc, 'serialBcast')) {
      const bc = isObj(doc.serialBcast) ? doc.serialBcast : {};
      ['S3', 'S4', 'S5'].forEach((k, i) => {
        if (!has(bc, k)) return;
        const p = isObj(bc[k]) ? bc[k] : {};
        s.bcastOut[i] = jBool(p.out, s.bcastOut[i]);
        s.bcastIn[i] = jBool(p.in, s.bcastIn[i]);
      });
    }
    if (has(doc, 'modeReport')) {
      const mr = isObj(doc.modeReport) ? doc.modeReport : {};
      s.modeReport.enabled = jBool(mr.enabled, s.modeReport.enabled);
      s.modeReport.wcb = u8(jInt(mr.wcb, s.modeReport.wcb));
      if (has(mr, 'template')) s.modeReport.tmpl = strl(jStr(mr.template, ''), 48);
      if (has(mr, 'cmds')) {
        const mc = Array.isArray(mr.cmds) ? mr.cmds : null;
        s.modeReport.cmds = [0, 1, 2].map((i) => strl(mc && i < mc.length ? jStr(mc[i], '') : '', 48));
      }
    }
    if (has(doc, 'statsReport')) {
      const sr = isObj(doc.statsReport) ? doc.statsReport : {};
      s.statsReport.enabled = jBool(sr.enabled, s.statsReport.enabled);
      s.statsReport.wcb = u8(jInt(sr.wcb, s.statsReport.wcb));
    }
    return true;
  }

  // rcConfigToJSON (:1211-1480): the object GET_CONFIG prints as "data", keys in the firmware's order.
  toJSON() {
    const s = this.s;
    const out = {
      txModel: s.txModel, threeAxisGimbals: s.threeAxisGimbals, sbusOutEnabled: s.sbusOutEnabled,
      wifiEnabled: s.wifiEnabled, wifiSsid: s.wifiSsid, wifiPassword: s.wifiPassword, maeGateMs: s.maeGateMs,
      boardType: s.boardType, tapWindowMs: s.tapWindowMs, holdMs: s.holdMs, switchSettleMs: s.switchSettleMs,
      chRateHz: s.chRateHz, matrixChannel: s.matrixChannel, matrixDebounceFrames: s.matrixDebounceFrames,
      funcBindings: { mode: s.modeSwitch },
      peerEvent: { alert: s.peerAlert, actions: s.peerActions.map((a) => ({ ...a })) },
      thresholds: s.thresholds.map((t) => ({ id: t.id, label: t.label, minPwm: t.minPwm, maxPwm: t.maxPwm })),
    };
    const maps = {};
    for (let mode = 1; mode <= 3; mode++) {
      for (let btn = 1; btn <= THRESHOLDS; btn++) {
        const m = s.mappings[(mode - 1) * THRESHOLDS + (btn - 1)];
        const any = m.t.some((t) => t.a.length > 0 || t.note);
        if (!any && !m.exclusive) continue;
        const o = { exclusive: m.exclusive };
        m.t.forEach((t, ti) => {
          if (t.a.length) o[`t${ti + 1}`] = t.a.map((a) => ({ ...a }));
          if (t.note) o[`t${ti + 1}note`] = t.note;
        });
        maps[String(mode * 100 + btn)] = o;
      }
    }
    out.mappings = maps;
    out.switches = {};
    SWITCH_LABELS.forEach((lbl, i) => {
      const sw = s.switches[i];
      const o = { channel: sw.channel, positions: sw.positions };
      sw.t.forEach((t, pi) => {
        if (t.a.length) o[`p${pi}`] = t.a.map((a) => ({ ...a }));
        if (t.note) o[`p${pi}note`] = t.note;
      });
      out.switches[lbl] = o;
    });
    out.knobs = {};
    KNOB_LABELS.forEach((lbl, i) => {
      const kn = s.knobs[i];
      const o = { channel: kn.channel, function: kn.function, reverse: kn.reverse, modeAware: kn.modeAware,
                  modeSwitchOverride: kn.modeSwitchOverride };
      if (kn.smoothProfile !== -1) o.smoothProfile = kn.smoothProfile;
      if (kn.easeSwitchOverride) o.easeSwitchOverride = true;
      o.outputs = knobOutsTo(kn.outputs);
      if (kn.modeAware) { o.outputs2 = knobOutsTo(kn.outputs2); o.outputs3 = knobOutsTo(kn.outputs3); }
      out.knobs[lbl] = o;
    });
    const h = s.hcrDest;
    out.hcrDest = h.transport === 1 ? { transport: 'wcb', target: h.target, wcbPort: h.wcbPort }
                                    : { transport: transportName(h.transport), port: h.target };
    out.maestros = s.maestros.map((m) => {
      const o = { type: m.type, device: m.device };
      const chans = [];
      m.channels.forEach((c, ch) => {
        if (!c.name && !c.min && !c.max) return;
        chans.push({ ch, name: c.name, min: c.min, max: c.max });
      });
      if (chans.length) o.channels = chans;
      return o;
    });
    out.wcbNetwork = { ...s.wcbNetwork };
    out.wcbProfiles = s.wcbProfiles.map((p) => ({ ...p }));
    for (const key of ['mp3Dest', 'dfpDest']) {
      const d = s[key];
      out[key] = d.transport === 1 ? { transport: 'wcb', target: d.target } : { transport: transportName(d.transport), port: d.target };
    }
    out.wledSlots = s.wledSlots.map((w) => ({ id: w.wledID, port: w.serialPort, wcb: w.remoteWCB, configured: w.configured }));
    out.auxBaud = { S3: s.auxBaud[0], S4: s.auxBaud[1], S5: s.auxBaud[2], maestro: s.maestroBaud };
    if (s.serialLabels.some(Boolean)) {
      out.serialLabels = {};
      SLBL_KEYS.forEach((k, i) => { if (s.serialLabels[i]) out.serialLabels[k] = s.serialLabels[i]; });
    }
    out.serialBcast = {};
    ['S3', 'S4', 'S5'].forEach((k, i) => { out.serialBcast[k] = { out: s.bcastOut[i], in: s.bcastIn[i] }; });
    out.modeReport = { enabled: s.modeReport.enabled, wcb: s.modeReport.wcb, template: s.modeReport.tmpl,
                       cmds: s.modeReport.cmds.slice() };
    out.statsReport = { enabled: s.statsReport.enabled, wcb: s.statsReport.wcb };
    out.smoothProfiles = s.smooth.map((p) => {
      const entries = [];
      for (let mid = 1; mid <= MAESTROS; mid++) {
        for (let ch = 0; ch < 32; ch++) {
          const e = p.e[mid - 1][ch];
          if (e.speed || e.accel) entries.push({ mid, ch, spd: e.speed, acc: e.accel });
        }
      }
      return { name: p.name, entries };
    });
    return out;
  }

  // GET_CONFIG's line body, exactly as serialized (ArduinoJson writes no spaces).
  text() { return JSON.stringify(this.toJSON()); }
}

module.exports = {
  NcConfig, actionFrom, strl, FACTORY_PW, THRESHOLDS, SWITCH_LABELS, KNOB_LABELS, SLBL_KEYS, MAX_BOARDS,
  constants: { THRESHOLDS, TAP_TIERS, ACTIONS, SWITCHES, KNOBS, KNOB_OUTS, MAESTROS, MAE_CH, WLED, PROFILES, SMOOTH },
};
