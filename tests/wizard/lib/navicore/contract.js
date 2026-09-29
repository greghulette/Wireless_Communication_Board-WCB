// The emulator held to the real board (nctool.emulator_contract, L2; docs/hil_plan/NAVICORE.md §5.3). The read-only
// request set goes to the real NaviCore through the pipe and to the emulator, and the replies are compared by SHAPE:
// reply types, key sets, value types, the order of the CLI markers. Never by value, so nothing the board says reaches
// a report; a difference is named by its key path and the two value TYPES. The helpers here are pure, and
// unit/navicore/rig.test.js runs them against the emulator and against lines copied from the firmware's printf formats.
//
// Replies copied (NaviCore hil-week 6925773): PING -> {"type":"PONG","version"} (NaviCore.ino:3855-3863); GET_CONFIG ->
// {"type":"CONFIG","data":...} (:3865-3882); GET_CMDLIB_META (:3895-3900); GET_WCB_STATUS, the dense USB form
// (:4136-4234); GET_MESH_STATS, pages drained in one burst (:4242-4275, rc_telemetry.h buildMeshStatsPage :1361-1432);
// ?REC,LS: [CLIPFS] when the clips partition mounted, "[REC] clips:", then listClips (NaviCore.ino:3485-3491,
// navicore_record.h:960-982); ?OTALOCAL,STATUS: otaPrintStatus's block (navicore_ota.h:250-263).

// The request set, in the order the spec sends it. `json`: the reply type (every page until "last":1 when `last`);
// `cli`: the line that ends the reply. A CLI request is followed by '#L12', whose 'Mode=' line releases a reply line
// NaviCore holds back until more output follows (docs/HIL_TESTING.md §5); the emulator ignores it.
const REQUESTS = [
  { id: 'PING', send: ['{"type":"PING"}'], json: 'PONG' },
  { id: 'GET_CONFIG', send: ['{"type":"GET_CONFIG"}'], json: 'CONFIG' },
  { id: 'GET_CMDLIB_META', send: ['{"type":"GET_CMDLIB_META"}'], json: 'CMDLIB_META' },
  { id: 'GET_WCB_STATUS', send: ['{"type":"GET_WCB_STATUS"}'], json: 'WCB_STATUS' },
  { id: 'GET_MESH_STATS', send: ['{"type":"GET_MESH_STATS"}'], json: 'MESH_STATS', last: true },
  { id: '?REC,LS', send: ['?REC,LS', '#L12'], cli: /^\[CLIPLIST:END\]/ },
  { id: '?OTALOCAL,STATUS', send: ['?OTALOCAL,STATUS', '#L12'], cli: /^-{32}$/ },
];

const parse = (line) => { try { return JSON.parse(line); } catch (_) { return undefined; } };
const kind = (v) => (v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v);

// The lines of `lines` that answer request `r`, or null while the answer is incomplete. Anything else the board
// prints meanwhile (a WCB_Client log line, the #L12 poke's own output) is left out.
function reply(r, lines) {
  if (r.json) {
    const got = [];
    for (const l of lines) {
      const o = parse(l);
      if (!o || o.type !== r.json) continue;
      got.push(l);
      if (!r.last || o.last === 1) return got;
    }
    return null;
  }
  const start = r.id === '?REC,LS' ? /^(\[CLIPFS\]|\[REC\] clips:|\[CLIPLIST:BEGIN\])/ : /^-+ OTA Status -+$/;
  const i = lines.findIndex((l) => start.test(l));
  if (i < 0) return null;
  const j = lines.findIndex((l, k) => k > i && r.cli.test(l));
  return j < 0 ? null : lines.slice(i, j + 1);
}

// A JSON value's shape: objects by key (sorted), arrays by their first element ('*' when empty: compatible with any
// element), everything else by its type.
function shape(v) {
  if (Array.isArray(v)) return { array: v.length ? shape(v[0]) : '*' };
  if (v !== null && typeof v === 'object') {
    const o = {};
    for (const k of Object.keys(v).sort()) o[k] = shape(v[k]);
    return { object: o };
  }
  return kind(v);
}

// Where two shapes differ: 'path: <what the board has> vs <what the emulator has>', types and key names only.
function shapeDiff(a, b, path = '', out = []) {
  const name = (s) => (typeof s === 'string' ? s : s.array !== undefined ? 'array' : 'object');
  if (a === '*' || b === '*') return out;
  if (name(a) !== name(b)) { out.push(`${path || '(root)'}: ${name(a)} vs ${name(b)}`); return out; }
  if (typeof a === 'string') return out;
  if (a.array !== undefined) return shapeDiff(a.array, b.array, `${path}[]`, out);
  for (const k of new Set([...Object.keys(a.object), ...Object.keys(b.object)])) {
    const p = path ? `${path}.${k}` : k;
    if (!(k in b.object)) out.push(`${p}: only on the board`);
    else if (!(k in a.object)) out.push(`${p}: only in the emulator`);
    else shapeDiff(a.object[k], b.object[k], p, out);
  }
  return out;
}

// Key paths where two parsed JSON values differ, value TYPES only ('mappings.101.t1[0].cmd: string vs string' means
// the values differ). For the GET_CONFIG reprint, which compares values in memory and must not print them.
function valuePaths(a, b, path = '', out = []) {
  if (JSON.stringify(a) === JSON.stringify(b)) return out;
  if (a && b && typeof a === 'object' && typeof b === 'object' && Array.isArray(a) === Array.isArray(b)) {
    if (Array.isArray(a) && a.length !== b.length) out.push(`${path || '(root)'}: ${a.length} vs ${b.length} items`);
    const keys = Array.isArray(a) ? [...Array(Math.min(a.length, b.length)).keys()]
                                  : [...new Set([...Object.keys(a), ...Object.keys(b)])];
    for (const k of keys) {
      const p = Array.isArray(a) ? `${path}[${k}]` : (path ? `${path}.${k}` : k);
      if (!Array.isArray(a) && !(k in b)) out.push(`${p}: only on the board`);
      else if (!Array.isArray(a) && !(k in a)) out.push(`${p}: only in the model`);
      else valuePaths(a[k], b[k], p, out);
    }
  } else out.push(`${path || '(root)'}: ${kind(a)} vs ${kind(b)}`);
  return out;
}

// ?REC,LS as its marker sequence (a run of [CLIPITEM] lines is one '[CLIPITEM]*' step) and the key sets of the
// [CLIPFS] and first [CLIPITEM] objects.
function recLsShape(lines) {
  const markers = [];
  let clipfs = null, item = null;
  for (const l of lines) {
    const m = /^(\[CLIPFS\]|\[CLIPLIST:BEGIN\]|\[CLIPLIST:END\]|\[CLIPITEM\]|\[REC\] clips:)/.exec(l);
    if (!m) { markers.push('(other line)'); continue; }
    const tag = m[1] === '[CLIPITEM]' ? '[CLIPITEM]*' : m[1];
    if (markers[markers.length - 1] !== tag) markers.push(tag);
    const body = parse(l.slice(m[1].length));
    if (m[1] === '[CLIPFS]' && body) clipfs = Object.keys(body).sort();
    if (m[1] === '[CLIPITEM]' && body && !item) item = Object.keys(body).sort();
  }
  return { markers, clipfs, item };
}

// ?OTALOCAL,STATUS as the labels of its block, in order ('Chip', 'Firmware', 'App SHA256', 'Running', ...).
function otaStatusShape(lines) {
  return lines.filter((l) => !/^-+( OTA Status -+)?$/.test(l)).map((l) => (/^([^:]+):/.exec(l) || [, '(no label)'])[1].trim());
}

// The problems with the emulator's answer `sim` to request `r` against the board's `real` (both lines as reply()
// returns them), shapes only.
function compare(r, real, sim) {
  const out = [];
  if (r.json) {
    const a = real.map(parse), b = sim.map(parse);
    // Page 0 is compared whole; later MESH_STATS pages carry only rows, and their count follows the roster.
    for (const d of shapeDiff(shape(a[0]), shape(b[0]))) out.push(`${r.id}: ${d}`);
    return out;
  }
  if (r.id === '?REC,LS') {
    const a = recLsShape(real), b = recLsShape(sim);
    // An empty clip list prints no [CLIPITEM] line: compare the markers without it when either side has none.
    const strip = (m) => m.filter((x) => x !== '[CLIPITEM]*');
    const [ma, mb] = a.item && b.item ? [a.markers, b.markers] : [strip(a.markers), strip(b.markers)];
    if (JSON.stringify(ma) !== JSON.stringify(mb)) out.push(`${r.id}: markers ${ma.join(' ')} vs ${mb.join(' ')}`);
    if (JSON.stringify(a.clipfs) !== JSON.stringify(b.clipfs)) out.push(`${r.id}: [CLIPFS] keys ${a.clipfs} vs ${b.clipfs}`);
    if (a.item && b.item && JSON.stringify(a.item) !== JSON.stringify(b.item)) out.push(`${r.id}: [CLIPITEM] keys ${a.item} vs ${b.item}`);
    return out;
  }
  const a = otaStatusShape(real), b = otaStatusShape(sim);
  if (JSON.stringify(a) !== JSON.stringify(b)) out.push(`${r.id}: labels ${a.join(' | ')} vs ${b.join(' | ')}`);
  return out;
}

// A line device on top of a pipe or the emulator (anything with attach/onWrite, lib/navicore/shim.js's device
// contract): every line it prints is kept, and ask() writes lines and waits for reply() to find the answer.
class LineTap {
  constructor(device) {
    this.device = device;
    this.lines = [];
    this._buf = '';
    device.attach((bytes) => {
      this._buf += Buffer.from(bytes).toString('utf8');
      let i;
      while ((i = this._buf.search(/\r?\n/)) !== -1) {
        const line = this._buf.slice(0, i).replace(/\r$/, '');
        this._buf = this._buf.slice(this._buf[i] === '\r' ? i + 2 : i + 1);
        if (line) this.lines.push(line);
      }
    }, null);
  }

  async open() { if (this.device.onOpen) await this.device.onOpen({ baudRate: 115200 }, null); }

  async ask(r, timeoutMs = 10_000) {
    const mark = this.lines.length;
    for (const l of r.send) this.device.onWrite(Buffer.from(l + '\n', 'utf8'));
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const got = reply(r, this.lines.slice(mark));
      if (got) return got;
      if (Date.now() > deadline) throw new Error(`${r.id}: no whole answer within ${timeoutMs} ms (${this.lines.length - mark} lines since, not quoted)`);
      await new Promise((res) => setTimeout(res, 25));
    }
  }
}

module.exports = { REQUESTS, reply, shape, shapeDiff, valuePaths, recLsShape, otaStatusShape, compare, LineTap };
