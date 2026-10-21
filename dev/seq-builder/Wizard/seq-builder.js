// ════════════════════════════════════════════════════════════════
//  WCB Config Tool — seq-builder.js
//  The sequence builder: lays one stored sequence out on a timeline, with
//  the command library NaviCore's config tool presents (cmdlib/) as the
//  palette. It opens from a sequence row and writes its result back into
//  that row, so SAVE/UPDATE, TEST and a push treat it like typed text.
//  Design, and the firmware rules it encodes: docs/SEQUENCE_BUILDER.md
// ════════════════════════════════════════════════════════════════
(function () {
'use strict';

// ─── Firmware rules the timeline encodes ─────────────────────────
// A sequence is one delimited chain. A ;T<ms> token starts a new group that fires <ms> after the group before it fired,
// and the commands between two delays run together (command_timer.cpp parseCommandGroups). The timeline's absolute
// times are therefore written as the gaps between distinct times.
// Each delay is capped at 30 minutes (parsedDelayLimit) and consecutive delays add up, so a longer gap is written as
// several delays.
const SB_DELAY_CAP_MS    = 1800000;
// The store takes 1-3,999 characters; saving more than about 2,400 runs a classic ESP32 short of heap
// (docs/SEQUENCE_INVENTORY.md §3b, §3d).
const SB_VALUE_MAX       = 3999;
const SB_VALUE_HEAP_WARN = 2400;

function sbChars(c) {
  return { delim: (c && c.delim) || '^', cmd: (c && c.cmd) || ';', func: (c && c.func) || '?' };
}

function sbFmt(ms) {
  ms = Math.max(0, Math.round(+ms || 0));
  if (ms < 60000) return (ms / 1000).toFixed(3) + ' s';
  const m = Math.floor(ms / 60000);
  return m + ':' + ((ms % 60000) / 1000).toFixed(3).padStart(6, '0');
}

// <cmdChar>T|t, then a delay: what parseCommandGroups reads as a group break. ;t500,<cmd> carries its first command
// inline. Anything else is null.
function sbTimerToken(tok, ch) {
  if (tok.length < 3 || tok[0] !== ch.cmd || (tok[1] !== 'T' && tok[1] !== 't')) return null;
  const rest  = tok.slice(2);
  const comma = rest.indexOf(',');
  const m = /^(-?)(\d+)/.exec((comma >= 0 ? rest.slice(0, comma) : rest).trim());
  if (!m) return null;
  return { ms: parseInt(m[2], 10), neg: m[1] === '-', inline: comma >= 0 ? rest.slice(comma + 1).trim() : '' };
}

const sbIsIf = (tok) => /^if,/i.test(tok);   // isIfChainToken (WCB_Variables.cpp)

// ?SEQ,SAVE / ?CS / ?MGMT carry a value that may itself hold the delimiter and a ;T, so the board keeps a chain carrying
// one off the timer engine altogether (chainCarriesValueVerb, WCB.ino): every delay in it is stored, never run.
function sbIsValueVerb(tok, ch) {
  const u = tok.toUpperCase(), f = ch.func;
  return u.startsWith(f + 'SEQ,SAVE,') || u.startsWith(f + 'CS') || u.startsWith(f + 'MGMT,');
}

// A stored value → { events: [{t, cmd, conds, note}], markers: [{t, text}], warnings, timed, valueVerb, refused }.
// Mirrors parseCommandGroups: times accumulate over delay tokens, an IF gates the next command, *** starts a comment.
// Comments follow the Wizard's text editor (seqValueToLines): first, or after a ^^, a comment stands on its own and
// becomes a marker; after a single ^ it belongs to the command before it and becomes that step's note.
function sbParse(value, chars) {
  const ch  = sbChars(chars);
  const out = { events: [], markers: [], warnings: [], timed: false, valueVerb: false, refused: false };
  if (!value) return out;
  const warned = new Set();
  const warn = (level, code, text) => {
    if (warned.has(code)) return;
    warned.add(code);
    out.warnings.push({ level, code, text });
  };
  let t = 0, conds = [], condNote = '', prevEmpty = false, last = null;
  const push = (cmd, note) => {
    out.events.push({ t, cmd, conds, note: [condNote, note].filter(Boolean).join(' ') });
    conds = []; condNote = ''; last = 'event';
  };
  // A marker remembers whether steps at its time came before it, so it is written back on the same side of them.
  const marker = (text) => {
    const prev = out.events[out.events.length - 1];
    out.markers.push({ t, text, after: !!prev && prev.t === t });
  };
  for (const raw of String(value).split(ch.delim)) {
    const tok = raw.trim();
    if (!tok) { prevEmpty = true; continue; }
    if (tok.startsWith('***')) {
      const text = tok.slice(3).trim();
      const ev = out.events[out.events.length - 1];
      if (!prevEmpty && last === 'event' && ev) ev.note = ev.note ? ev.note + ' ' + text : text;
      else if (text) marker(text);
      prevEmpty = false;
      continue;
    }
    prevEmpty = false;
    const ci   = tok.indexOf('***');
    const body = (ci >= 0 ? tok.slice(0, ci) : tok).trim();
    const note = ci >= 0 ? tok.slice(ci + 3).trim() : '';
    if (!body) continue;
    if (sbIsValueVerb(body, ch)) out.valueVerb = true;
    if (sbIsIf(body)) {
      conds.push(body.slice(3).trim());
      if (note) condNote = condNote ? condNote + ' ' + note : note;
      last = 'if';
      continue;
    }
    const tm = sbTimerToken(body, ch);
    if (tm) {
      out.timed = true;
      if (tm.neg) warn('error', 'negDelay', `${body} is a negative delay: the board refuses this whole sequence and runs none of it.`);
      if (tm.neg) out.refused = true;
      let ms = tm.neg ? 0 : tm.ms;
      if (ms > SB_DELAY_CAP_MS) {
        warn('warn', 'capped', `The board cuts a delay over 30 minutes (${body}) to 30 minutes; the timeline shows what it runs.`);
        ms = SB_DELAY_CAP_MS;
      }
      // ifGateConsumeToken drops a gated pure delay along with the command it gates, so on the board a false condition
      // here also pulls every later step earlier. The timeline cannot draw that; it keeps the delay (sbCompile).
      if (conds.length) warn('warn', 'ifDelay', 'An IF sits before a delay. On the board a false condition skips that delay too, so every later step runs early. The timeline keeps the delay and writes the IF after it.');
      t += ms;
      if (tm.inline) push(tm.inline, note);
      else { last = 'timer'; if (note) marker(note); }
      continue;
    }
    push(body, note);
  }
  if (conds.length) warn('warn', 'ifDangling', `IF,${conds.join(' and IF,')} at the end gates nothing, so it was left out.`);
  return out;
}

// The timeline → a stored value. Steps sort by time (steps at one time keep their order) and one ;T per distinct time
// carries the gap to it. An IF goes AFTER the delay, right before its command: before a delay, a false condition would
// skip the delay as well (ifGateConsumeToken) and shift every later step. Notes ride inline (cmd***note) and markers
// stand on their own (^^***text), as the text editor writes them; a marker goes before the steps at its time unless it
// was read after them (`after`). A marker past the last step goes at the end with no delay of its own: a trailing ;T
// delays nothing.
function sbCompile(model, chars) {
  const ch  = sbChars(chars);
  const evs = (model.events || []).map((e, i) => ({ e, i }))
    .sort((a, b) => (a.e.t - b.e.t) || (a.i - b.i)).map(x => x.e);
  const lastT = evs.length ? evs[evs.length - 1].t : 0;
  const mks = (model.markers || []).map((m, i) => ({ m, i }))
    .sort((a, b) => (a.m.t - b.m.t) || ((a.m.after ? 1 : 0) - (b.m.after ? 1 : 0)) || (a.i - b.i)).map(x => x.m);
  const clean = (s) => String(s == null ? '' : s).split(ch.delim).join(' ').trim();
  const out = [];
  const delay = (ms) => {
    while (ms > 0) { const d = Math.min(ms, SB_DELAY_CAP_MS); out.push(ch.cmd + 'T' + d); ms -= d; }
  };
  const marker = (text) => {
    const s = clean(text);
    if (!s) return;
    if (out.length) out.push('');
    out.push('***' + s);
  };
  let cur = 0, ei = 0, mi = 0;
  while (ei < evs.length || (mi < mks.length && mks[mi].t <= lastT)) {
    const tm = (mi < mks.length && mks[mi].t <= lastT) ? mks[mi].t : Infinity;
    const te = ei < evs.length ? evs[ei].t : Infinity;
    const T  = Math.max(0, Math.round(Math.min(tm, te)));
    if (T > cur) { delay(T - cur); cur = T; }
    while (mi < mks.length && (mks[mi].t < T || (mks[mi].t === T && !mks[mi].after))) marker(mks[mi++].text);
    while (ei < evs.length && evs[ei].t <= T) {
      const e = evs[ei++];
      const cmd = String(e.cmd == null ? '' : e.cmd).trim();
      if (!cmd) continue;
      for (const c of e.conds || []) { const s = clean(c); if (s) out.push('IF,' + s); }
      const note = clean(e.note);
      out.push(cmd + (note ? '***' + note : ''));
    }
    while (mi < mks.length && mks[mi].t === T) marker(mks[mi++].text);
  }
  while (mi < mks.length) marker(mks[mi++].text);
  return out.join(ch.delim);
}

// ;W<n>,<cmd> and ;W<d><cmd> (one digit, no comma) route to WCB n; ;S<p><text> writes serial port p, minus one comma
// after the digit (processWCBMessage / processSerialMessage, WCB.ino). ;SEQ… is a recall, not a port: the port is a
// digit. The alias form (;wdome,<cmd>) is left whole.
function sbSplitRoute(cmd, ch) {
  const route = { wcb: 0, port: null, alias: false };
  let s = String(cmd == null ? '' : cmd).trim();
  if (s.length > 2 && s[0] === ch.cmd && (s[1] === 'W' || s[1] === 'w')) {
    const m = /^(\d+),/.exec(s.slice(2));
    if (m) { route.wcb = +m[1]; s = s.slice(2 + m[0].length); }
    else if (/\d/.test(s[2]) && s.length > 3) { route.wcb = +s[2]; s = s.slice(3); }
    else { route.alias = true; return { route, body: s }; }
  }
  if (s.length >= 3 && s[0] === ch.cmd && (s[1] === 'S' || s[1] === 's') && /\d/.test(s[2])) {
    route.port = +s[2];
    s = s.slice(3);
    if (s[0] === ',') s = s.slice(1);
  }
  return { route, body: s.trim() };
}

// The inverse. A route to the board running the sequence needs no ;W; a body that starts with a comma gets a second one,
// because the board strips the first.
function sbJoinRoute(route, body, ch, selfWcb) {
  let s = String(body == null ? '' : body);
  if (route && route.port != null) s = ch.cmd + 'S' + route.port + (s[0] === ',' ? ',' : '') + s;
  if (route && route.wcb && route.wcb !== selfWcb) s = ch.cmd + 'W' + route.wcb + ',' + s;
  return s;
}

// ;C<key> / ;SEQ<key>[,L] run on this board → the key (recallStoredCommand, WCB.ino). A routed recall runs elsewhere.
function sbRecallKey(cmd, ch) {
  const { route, body } = sbSplitRoute(cmd, ch);
  if (route.wcb || route.port != null || route.alias || body[0] !== ch.cmd) return null;
  const m = body.slice(1);
  let key;
  if (/^seq/i.test(m)) key = m.slice(3);
  else if (/^c/i.test(m)) key = m.slice(1);
  else return null;
  key = key.trim();
  const c = key.indexOf(',');
  if (c >= 0 && /^(l|local)$/i.test(key.slice(c + 1).trim())) key = key.slice(0, c).trim();
  return key || null;
}

// Does a stored value run on the timer engine? A delimited timer token, and no value verb (chainHasTimerToken).
function sbHasTimer(value, chars) {
  const ch = sbChars(chars);
  const toks = String(value || '').split(ch.delim).map(s => s.trim());
  if (toks.some(t => sbIsValueVerb(t, ch))) return false;
  return toks.some(t => !!sbTimerToken(t, ch));
}

// Problems with a timeline, as [{level: 'error'|'warn'|'info', text, ids}]. Errors block Apply.
// ctx: { chars, key, value (compiled), seqValue(key) → this board's value or null }.
function sbAnalyze(model, ctx) {
  const ch  = sbChars(ctx.chars);
  const out = [];
  const evs = model.events || [];
  const timed = evs.some(e => e.t > 0);
  const trunc = (s) => (s.length > 40 ? s.slice(0, 39) + '…' : s);
  for (const e of evs) {
    const cmd = String(e.cmd == null ? '' : e.cmd).trim();
    const at = sbFmt(e.t);
    if (!cmd) { out.push({ level: 'error', ids: [e.id], text: `The step at ${at} has no command.` }); continue; }
    if (cmd.includes(ch.delim)) {
      out.push({ level: 'error', ids: [e.id], text: `${trunc(cmd)} (${at}) contains the delimiter ${ch.delim}, which would split it into separate commands.` });
    }
    if (sbIsIf(cmd)) out.push({ level: 'error', ids: [e.id], text: `${trunc(cmd)} (${at}): put a condition in the step's IF field instead.` });
    if (sbTimerToken(cmd, ch)) out.push({ level: 'error', ids: [e.id], text: `${trunc(cmd)} (${at}) is a delay. Move steps on the timeline to space them out.` });
    if (sbIsValueVerb(cmd, ch)) {
      out.push(timed
        ? { level: 'error', ids: [e.id], text: `${trunc(cmd)} carries a value the board stores as is, and the board runs a sequence holding one without any of its delays.` }
        : { level: 'warn', ids: [e.id], text: `${trunc(cmd)} stores something from inside a sequence; check that is what you mean.` });
    }
    if (e.conds && e.conds.some(c => !String(c).trim())) out.push({ level: 'error', ids: [e.id], text: `The step at ${at} has an empty IF condition.` });
    const rk = sbRecallKey(cmd, ch);
    if (!rk) continue;
    if (ctx.key && rk === ctx.key) {
      out.push({ level: 'error', ids: [e.id], text: `The step at ${at} runs this sequence itself; the board's cycle guard refuses it.` });
      continue;
    }
    const v = ctx.seqValue ? ctx.seqValue(rk) : null;
    if (v == null) {
      out.push({ level: 'warn', ids: [e.id], text: `No sequence "${rk}" on this board. A sequence that runs another runs only this board's copy.` });
    } else if (sbHasTimer(v, ch) && evs.some(x => x.t > e.t)) {
      out.push({ level: 'warn', ids: [e.id], text: `"${rk}" has delays of its own. The board runs one timed sequence at a time, so starting it at ${at} stops this one: the steps after it never run.` });
    }
  }
  if (timed && evs.some(e => e.conds && e.conds.length)) {
    out.push({ level: 'info', ids: [], text: 'IF conditions are checked when the sequence starts, not when their step fires.' });
  }
  const times = [...new Set(evs.map(e => e.t))].sort((a, b) => a - b);
  if (times.some((t, i) => t - (i ? times[i - 1] : 0) > SB_DELAY_CAP_MS)) {
    out.push({ level: 'info', ids: [], text: 'A gap over 30 minutes is written as several delays: the board caps each one at 30 minutes.' });
  }
  const len = (ctx.value || '').length;
  if (len > SB_VALUE_MAX) {
    out.push({ level: 'error', ids: [], text: `${len} characters: the board stores at most ${SB_VALUE_MAX}. Split it into sequences that run one another.` });
  } else if (len > SB_VALUE_HEAP_WARN) {
    out.push({ level: 'warn', ids: [], text: `${len} characters: a classic ESP32 (HW 2.x / 3.1) can run out of memory saving more than about ${SB_VALUE_HEAP_WARN}.` });
  }
  return out;
}

// ─── The command library ─────────────────────────────────────────
// The DroidNet schema NaviCore uses: a file holds `enums` and `components` (or `boards`), a component holds `commands`,
// a command a `template` with {param} placeholders. See NaviCore docs/CONFIG_TOOL.md §6.
//
// Boards the picker does not offer. wcb-native is setup, not actions (NaviCore hides it too). maestro-native's commands
// are NaviCore firmware verbs (setTarget,0,6000 as text): a Maestro on a WCB serial port does not understand them, and
// wcb-maestro's ;M<id>,setTarget is the WCB form. Hidden boards still decode, so a step holding one of their commands
// keeps its name.
const SB_HIDDEN_BOARDS = new Set(['wcb-native', 'maestro-native']);
// vendored id → the board that replaces it when both are present: the vendored ;M board's ranges are narrower than
// the firmware's (WcbMaestro.cpp).
const SB_SUPERSEDE = { 'maestro': 'wcb-maestro' };
// Commands that manage sequences rather than run in one.
const SB_HIDDEN_CMDS = new Set(['wcb.seqSave', 'wcb.seqList', 'wcb.seqClearAll', 'wcb.seqClear']);
// Upstream still ships the sequence verbs inside wcb-native; a fetch of it would list them twice (NaviCore's
// _cmdlibDropMovedCmds).
const SB_MOVED_CMDS = [
  { from: 'wcb-native', to: 'wcb-sequences',
    ids: ['wcb.seqSave', 'wcb.seqList', 'wcb.seqClearAll', 'wcb.seqClear', 'wcb.runSeq', 'wcb.runSeqLong'] },
];
// NaviCore's order (NC_CMDLIB_ORDER_TOP) with the WCB's own boards: the WCB group first and contiguous, since a group
// renders where its first member sits.
const SB_ORDER_TOP = [
  'wcb-sequences', 'wcb-hcr', 'wcb-mp3', 'wcb-dfplayer', 'wcb-maestro', 'wcb-wled', 'wcb-actions', 'wcb-native',
  'hcr-native', 'chirp',
  'flthy-hps', 'psi-pro', 'rseries-logic', 'ia-magic-panel', 'pd-magic-panel',
  'r2uppityspinner-alt', 'roam-a-dome',
  'astropixels-plus', 'astropixels-sound', 'astropixels-psi',
  'astropixels-logics', 'astropixels-holo', 'astropixels-servo',
];
const sbRank = (id) => { const i = SB_ORDER_TOP.indexOf(id); return i >= 0 ? i : 1000; };

function sbNewLibrary() { return { enums: {}, boards: [], gen: 0 }; }

function sbMergeLibrary(lib, data, opts) {
  if (data && data.enums && typeof data.enums === 'object') Object.assign(lib.enums, data.enums);
  const raw = (data && (data.boards || data.components)) || [];
  let added = 0;
  for (const b of Array.isArray(raw) ? raw : []) {
    if (!b || !b.id) continue;
    const nb = {
      id: String(b.id), name: b.name || String(b.id), kind: b.kind || '',
      routing: (b.routing && typeof b.routing === 'object') ? b.routing : null,
      labels: Array.isArray(b.labels) ? b.labels : null,
      commands: Array.isArray(b.commands) ? b.commands : [],
      custom: !!(opts && opts.custom),
      // NaviCore's custom-board fields (its _cmdlibMergeLibraryData). local: an action on the NaviCore itself, with no
      // place in a WCB sequence. deliver:'wcb' + dest.target: a mesh client that takes raw text by unicast (;W<n>,text).
      local: b.local || null,
      deliver: b.deliver || null,
      dest: (b.dest && typeof b.dest === 'object') ? b.dest : null,
    };
    const i = lib.boards.findIndex(x => x.id === nb.id);
    if (i >= 0) lib.boards[i] = nb; else lib.boards.push(nb);
    added++;
  }
  lib.gen++;
  return added;
}

function sbNormalizeLibrary(lib) {
  const has = (id) => lib.boards.some(b => b.id === id);
  for (const mv of SB_MOVED_CMDS) {
    const from = lib.boards.find(b => b.id === mv.from);
    if (from && has(mv.to)) from.commands = from.commands.filter(c => !mv.ids.includes(c && c.id));
  }
  for (const b of lib.boards) b.hidden = SB_HIDDEN_BOARDS.has(b.id) || !!b.local || !!(SB_SUPERSEDE[b.id] && has(SB_SUPERSEDE[b.id]));
  lib.boards = lib.boards.map((b, i) => ({ b, i }))
    .sort((a, z) => (sbRank(a.b.id) - sbRank(z.b.id)) || (a.i - z.i)).map(x => x.b);
  lib.gen++;
}

const sbReEsc = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// A command's template as a regex (NaviCore's _ncCommandPattern): a fixed-width param → \d{N}, an enum → its codes
// longest first, an int → digits, free text → a lazy group bounded by the next literal. Two free-text params back to
// back, or a template that is nothing but one (wcb.serialText: it would match anything), cannot be decoded. Memoized
// per library generation, since an enum can change under a command.
function sbPattern(lib, cmd) {
  if (cmd.__sbRe && cmd.__sbRe.gen === lib.gen) return cmd.__sbRe;
  const byName = {};
  (cmd.params || []).forEach(p => { byName[p.name] = p; });
  const parts = String(cmd.template || '').split(/\{([A-Za-z][A-Za-z0-9_]*)\}/);
  let re = '^', lit = 0, free = 0, prevFree = false, ambiguous = false;
  const order = [];
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 0) { re += sbReEsc(parts[i]); lit += parts[i].length; continue; }
    const p = byName[parts[i]] || { name: parts[i] };
    order.push(p.name);
    let isFree = false;
    if (p.width) re += '(\\d{' + (+p.width) + '})';
    else if (p.enum) {
      const en = lib.enums[p.enum];
      const codes = ((en && en.values) || []).map(v => sbReEsc(String(v.code))).sort((a, b) => b.length - a.length);
      if (codes.length) re += '(' + codes.join('|') + ')';
      else { isFree = true; re += '(.+?)'; }
    } else if (p.type === 'int') re += (p.min != null && +p.min < 0) ? '(-?\\d+)' : '(\\d+)';
    else { isFree = true; re += '(.+?)'; }
    if (parts[i - 1] === '' && prevFree && isFree) ambiguous = true;
    prevFree = isFree;
    if (isFree) free++;
  }
  re += '$';
  // caseInsensitive: the device takes its fixed text in either case (DroidNet's @A / @a). A template with no fixed text
  // decodes only when nothing in it is free text (flthy-hps's lone enum "{special}" does; serial text's "{text}" not).
  const regex = (ambiguous || !cmd.template || (lit === 0 && free > 0)) ? null : new RegExp(re, cmd.caseInsensitive ? 'i' : '');
  return (cmd.__sbRe = { gen: lib.gen, regex, order, lit, free });
}

// Duration suffix (|<seconds>) a board accepts on some commands, e.g. FlthyHPs A001|30.
function sbDurationOf(board, cmd) {
  const ds = board && board.routing && board.routing.durationSuffix;
  return (ds && ds.supported && cmd && cmd.supportsDuration) ? ds : null;
}

// A body (route stripped, characters canonical) → the most specific library command it matches, or null. Most specific:
// a visible board over a hidden one, then the most literal characters, then the fewest free-text params, so
// ;V,x,TOGGLE reads as the toggle command and not as "set x to TOGGLE".
function sbDecodeBody(lib, body) {
  let best = null;
  const tryMatch = (text, dur) => {
    for (const board of lib.boards) {
      for (const cmd of board.commands || []) {
        if (dur != null && !sbDurationOf(board, cmd)) continue;
        const pat = sbPattern(lib, cmd);
        if (!pat.regex) continue;
        const m = pat.regex.exec(text);
        if (!m) continue;
        const score = (board.hidden ? 0 : 1e6) + pat.lit * 100 - pat.free;
        if (best && score <= best.score) continue;
        const values = {};
        pat.order.forEach((n, i) => { values[n] = m[i + 1]; });
        if (dur != null) values.__dur = dur;
        best = { board, cmd, values, score };
      }
    }
  };
  tryMatch(body, null);
  if (!best) {
    const d = /^(.*)\|(\d+)$/.exec(body);
    if (d) tryMatch(d[1], d[2]);
  }
  return best;
}

function sbEncode(cmd, values) {
  let out = cmd.template || '';
  (cmd.params || []).forEach(p => {
    let v = (values && values[p.name] !== undefined && values[p.name] !== '') ? values[p.name]
          : (p.default !== undefined ? p.default : '');
    if (p.type === 'int' && p.pad) v = String(v).padStart(p.pad, '0');
    out = out.split('{' + p.name + '}').join(String(v));
  });
  return out;
}

// 'wcb': a WCB verb or setting the board routes itself (;A, ;M, ?…). 'device': a device's own string, which goes out a
// serial port (NaviCore's _cmdlibDestMode, where WLED is a third mode only because NaviCore routes it differently).
function sbModeOf(board, cmd) {
  const t = String((cmd && cmd.template) || (cmd && cmd.examples && cmd.examples[0]) || '').trim();
  return (t[0] === ';' || t[0] === '?') ? 'wcb' : 'device';
}

// The library speaks the factory characters (; and ?); a board may use others. Only the first character is a verb.
const sbToCanon = (body, ch) => (body[0] === ch.cmd ? ';' + body.slice(1) : body[0] === ch.func ? '?' + body.slice(1) : body);
const sbFromCanon = (body, ch) => (body[0] === ';' ? ch.cmd + body.slice(1) : body[0] === '?' ? ch.func + body.slice(1) : body);

function sbDecodeCmd(lib, cmd, chars) {
  const ch = sbChars(chars);
  const { route, body } = sbSplitRoute(cmd, ch);
  const hit = (!route.alias && body) ? sbDecodeBody(lib, sbToCanon(body, ch)) : null;
  return { route, body, hit };
}

// A library board's name as a lane name: "WCB · " and any parenthetical dropped ("HCR Vocalizer (native <…>)" →
// "HCR Vocalizer").
const sbShortName = (name) => String(name || '').replace(/^WCB\s*·\s*/i, '').replace(/\s*\([^)]*\)\s*/g, ' ').trim();

// Which lane a command draws in: the device it reaches. ctx: { chars, self, boardName(w), portLabel(w, p) }. dev: the
// library board a device string decoded to ({ id, name }), so two devices on one port get a lane each - one per port
// put every unlabelled device in one "S1" lane. Returns { key, label, name, sub, fam, order }: label is name and sub
// on one line, the lane shows them on two; key is absolute (board numbers, not "this board"), so a device keeps its
// colour across sequences on different boards; order sorts the lanes.
function sbLaneOf(cmd, ctx, dev) {
  const ch = sbChars(ctx.chars);
  const { route, body } = sbSplitRoute(cmd, ch);
  const on = (route.wcb && route.wcb !== ctx.self) ? route.wcb : 0;
  const bname = (w) => (ctx.boardName ? ctx.boardName(w) : 'WCB ' + w);
  const isText = !route.alias && body[0] !== ch.cmd && body[0] !== ch.func;
  if (dev && (route.port != null || isText)) {
    const w = on || ctx.self;
    const where = route.port != null ? `${on ? bname(on) : 'This board'} · S${route.port}${route.port === 0 ? ' (USB)' : ''}`
                : on ? `every port on ${bname(on)}` : 'every port (broadcast)';
    const name = sbShortName(dev.name) || dev.id;
    return { key: `D:${dev.id}:${route.port != null ? `W${w}S${route.port}` : on ? 'W' + on : 'B'}`,
             label: `${name} · ${where}`, name, sub: where, fam: 'serial', board: String(dev.id).split(':')[0],
             order: 50 + (w === ctx.self ? 0 : w) / 100 + (route.port != null ? route.port : 9) / 1000 };
  }
  if (route.port != null) {
    const w = on || ctx.self;
    const lbl = ctx.portLabel ? ctx.portLabel(w, route.port) : '';
    const where = on ? bname(on) : 'This board';
    return { key: `S:${w}:${route.port}`, label: `${where} · S${route.port}${lbl ? ' · ' + lbl : ''}`,
             name: `S${route.port}${lbl ? ' · ' + lbl : ''}`, sub: where, fam: 'serial',
             order: 50 + (w === ctx.self ? 0 : w) / 100 + route.port / 1000 };   // all ports together, this board's first
  }
  const pre  = on ? bname(on) + ' · ' : '';
  const kpre = on ? 'W' + on + ':' : '';
  // board: the library board whose colour the lane wears (sbBoardSlot), the one its verb belongs to.
  const lane = (key, label, fam, order, board = null) => ({ key: kpre + key, label: pre + label, name: label,
                                                            sub: on ? 'on ' + bname(on) : '', fam, order: order + (on ? 0.5 : 0), board });
  if (route.alias) return lane('alias', 'Routed by board name', 'other', 90);
  if (body[0] === ch.cmd && body.length > 1) {
    const v = body[1].toUpperCase();
    if (v === 'M') {
      const id = /\d/.test(body[2] || '') ? body[2] : '';
      return lane('M' + id, id === '0' ? 'All Maestros' : id === '9' ? "This board's Maestros" : 'Maestro ' + (id || '?'), 'servo', 10, 'wcb-maestro');
    }
    if (v === 'A') return lane('A', 'MP3 Trigger', 'sound', 20, 'wcb-mp3');
    if (v === 'D') return lane('D', 'DFPlayer', 'sound', 21, 'wcb-dfplayer');
    if (v === 'H') return lane('H', 'HCR Vocalizer', 'voice', 22, 'wcb-hcr');
    if (v === 'L') {
      const id = (/^\d+/.exec(body.slice(2)) || [''])[0];
      return lane('L' + id, id ? 'WLED ' + id : "This board's WLED", 'light', 30, 'wcb-wled');
    }
    if (v === 'C' || /^seq/i.test(body.slice(1))) return lane('C', 'Sequences', 'seq', 5, 'wcb-sequences');
    if (v === 'V') return lane('V', 'Variables', 'var', 40, 'wcb-actions');
    if (v === 'P') return lane('P' + (body[2] || ''), 'PWM · S' + (body[2] || '?'), 'pwm', 45, 'wcb-actions');
    return lane('X', 'Other commands', 'other', 95);
  }
  if (body[0] === ch.func) return lane('F', 'Board settings', 'board', 85);
  return lane('B', on ? 'every port (broadcast text)' : 'Broadcast to every port', 'other', 80);
}

const SB_CORE = {
  SB_DELAY_CAP_MS, SB_VALUE_MAX, SB_VALUE_HEAP_WARN,
  sbParse, sbCompile, sbAnalyze, sbTimerToken, sbSplitRoute, sbJoinRoute, sbRecallKey, sbHasTimer, sbFmt,
  sbNewLibrary, sbMergeLibrary, sbNormalizeLibrary, sbDecodeCmd, sbDecodeBody, sbEncode, sbModeOf, sbLaneOf,
  sbToCanon, sbFromCanon, SB_HIDDEN_CMDS,
};
if (typeof module !== 'undefined' && module && module.exports) module.exports = SB_CORE;
if (typeof window === 'undefined' || typeof document === 'undefined') return;

// ════════════════════════════════════════════════════════════════
//  Browser
// ════════════════════════════════════════════════════════════════
// app.js keeps its state in top-level `let`s (boardConfigs, boardConnections, remoteRelayForBoard), which are shared
// with this classic script by name but are not properties of window; its functions are. Every reference is guarded so
// the builder still loads on a page without them.
const app = {
  cfgs:   () => (typeof boardConfigs !== 'undefined' && boardConfigs) || {},
  conns:  () => (typeof boardConnections !== 'undefined' && boardConnections) || {},
  relays: () => (typeof remoteRelayForBoard !== 'undefined' && remoteRelayForBoard) || {},
  fn:     (name) => (typeof window[name] === 'function' ? window[name] : null),
};
const toast = (msg, type, ms) => { const f = app.fn('showToast'); if (f) f(msg, type, ms); else console.log(msg); };
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const $ = (id) => document.getElementById(id);

// ─── Loading the library ─────────────────────────────────────────
// Same-origin static files beside this page (cmdlib/, see its NOTICE.md), through the code path a "Check online"
// fetch takes. fetch() of a relative file is blocked under file://, which is why the Wizard must be served over http.
const SB_MANIFESTS        = ['cmdlib/droidnet/manifest.json', 'cmdlib/wcb/manifest.json'];
const SB_UPSTREAM         = 'https://raw.githubusercontent.com/travisccook/Droidnet-Command-Library/main/libraries/manifest.json';
const SB_CUSTOM_KEY       = 'wcbWizard.cmdlibCustom';
// Boards a user imported into NaviCore's tool on this browser. Both tools are served from the same origin
// (<user>.github.io), so they share localStorage: read, never written.
const SB_NC_CUSTOM_KEY    = 'nc_cmdlib_custom_v1';

const sbLib = sbNewLibrary();
let sbLibError = '';

async function sbFetchJson(url) {
  const res = await fetch(url, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function sbLoadManifest(url, log) {
  log = log || (() => {});
  log('Fetching manifest…');
  const manifest = await sbFetchJson(url);
  const boards = Array.isArray(manifest.boards) ? manifest.boards : [];
  if (!boards.length) throw new Error('the manifest lists no boards');
  const base = url.slice(0, url.lastIndexOf('/') + 1);
  let added = 0;
  for (const entry of boards) {
    if (!entry || !entry.file) continue;
    try {
      added += sbMergeLibrary(sbLib, await sbFetchJson(base + entry.file));
      log('  ✓ ' + (entry.name || entry.id || entry.file));
    } catch (e) {
      log('  ✗ ' + (entry.name || entry.id || entry.file) + ' — ' + e.message);
    }
  }
  sbNormalizeLibrary(sbLib);
  log(`Done: ${added} board(s).`);
  return added;
}

function sbReadStore(key) {
  try { const s = localStorage.getItem(key); return s ? JSON.parse(s) : null; } catch (_) { return null; }
}

const sbLibReady = (async () => {
  for (const url of SB_MANIFESTS) {
    try { await sbLoadManifest(url); }
    catch (e) { sbLibError = `Couldn't load ${url} (${e.message}). Open the Wizard over http, not as a file.`; }
  }
  for (const key of [SB_NC_CUSTOM_KEY, SB_CUSTOM_KEY]) {
    const data = sbReadStore(key);
    if (data) sbMergeLibrary(sbLib, data, { custom: true });
  }
  sbNormalizeLibrary(sbLib);
})();

function sbLibCmd(ref) {
  if (!ref) return null;
  const board = sbLib.boards.find(b => b.id === ref.b);
  const cmd = board && (board.commands || []).find(c => c.id === ref.c);
  return cmd ? { board, cmd } : null;
}

// The device a command's library board is, when the command is that device's own string: what gives it a lane of its
// own (sbLaneOf). Serial text is no device. Cached per command text, because every row's timing strip asks for every
// one of its commands and the library holds ~400 templates; the cache resets when the library changes.
const sbDevCache = new Map();
let sbDevCacheGen = -1;
// A command may name its own lane (laneName: wcbmaestro.droidnet is "Maestro via DroidNet", not its board's "Maestro").
function sbDevOfLib(L) {
  if (!L || L.cmd.id === 'wcb.serialText' || sbModeOf(L.board, L.cmd) !== 'device') return null;
  return L.cmd.laneName ? { id: L.board.id + ':' + L.cmd.id, name: L.cmd.laneName } : { id: L.board.id, name: L.board.name };
}
function sbDevOfCmd(cmd, chars) {
  if (sbDevCacheGen !== sbLib.gen) { sbDevCache.clear(); sbDevCacheGen = sbLib.gen; }
  const k = chars.delim + chars.cmd + chars.func + cmd;
  if (!sbDevCache.has(k)) {
    const d = sbDecodeCmd(sbLib, cmd, chars);
    sbDevCache.set(k, d.hit ? sbDevOfLib({ board: d.hit.board, cmd: d.hit.cmd }) : null);
  }
  return sbDevCache.get(k);
}

// ─── Colour ──────────────────────────────────────────────────────
// Colour is the device. Each library board has a home slot among the eight categorical colours (seq-builder.css
// --sb-c1..8: the dataviz reference palette, validated on the Wizard's own timeline surfaces in both themes): the
// library shows a board in it, and a lane for that board's device wears it. The WCB devices used together most -
// Maestro, MP3, HCR, WLED, sequences - sit on different slots, and the two HCR boards share one, being one device.
// A lane takes its board's home slot unless a lane already on the timeline has it, then the first free slot, so lanes
// on screen never share a colour; lanes are assigned by first appearance in time, so a row's timing strip and its
// timeline open alike. A slot then stays with its lane while the builder is open: a lane that comes or goes repaints
// no other. A ninth lane is grey, and identity is never colour alone: every lane and step is labelled.
const SB_SLOTS = 8;
const SB_BOARD_SLOT = {
  'wcb-maestro': 0, 'wcb-mp3': 1, 'wcb-sequences': 2, 'wcb-wled': 3, 'wcb-hcr': 4, 'wcb-dfplayer': 5,
  'wcb-actions': 6, 'wcb-native': 6,
  'hcr-native': 4, 'chirp': 1, 'flthy-hps': 7, 'psi-pro': 3, 'rseries-logic': 6, 'ia-magic-panel': 2,
  'pd-magic-panel': 5, 'r2uppityspinner-alt': 0, 'roam-a-dome': 1, 'maestro': 0, 'maestro-native': 0,
  'astropixels-plus': 2, 'astropixels-sound': 1, 'astropixels-psi': 3, 'astropixels-logics': 6,
  'astropixels-holo': 7, 'astropixels-servo': 0,
};
// A board's home slot: from the table, else the same slot every time for the same id (custom and new boards).
function sbBoardSlot(id) {
  if (id == null) return -1;
  if (SB_BOARD_SLOT[id] != null) return SB_BOARD_SLOT[id];
  let h = 2166136261;
  for (const c of String(id)) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  return (h >>> 0) % SB_SLOTS;
}
function sbColorMap() { return new Map(); }
// present: the lanes on screen now. With every slot taken, one held by a lane that is gone is reused. home: the slot
// the lane's board would have (sbBoardSlot), taken when no other lane holds it.
function sbColorOf(map, key, present, home = -1) {
  if (map.has(key) && map.get(key) >= 0) return map.get(key);
  const used = new Set(map.values());
  let slot = -1;
  if (home >= 0) {
    // Its home slot, if free - or held only by a lane that is gone (a step half typed was briefly another lane).
    const holder = [...map].find(([, s]) => s === home);
    if (!holder) slot = home;
    else if (present && !present.has(holder[0])) { map.delete(holder[0]); used.delete(home); slot = home; }
  }
  for (let i = 0; slot < 0 && i < SB_SLOTS; i++) if (!used.has(i)) slot = i;
  if (slot < 0 && present) {
    for (const [k, s] of map) if (s >= 0 && !present.has(k)) { slot = s; map.delete(k); break; }
  }
  map.set(key, slot);
  return slot;
}
const sbColorClass = (slot) => (slot >= 0 ? 'sbc' + slot : 'sbcx');

// Device labels (Wizard/device-labels.js vocabulary) each board corresponds to: a step from that board is sent to the
// port a board has under that label. A copy of NaviCore's NC_BOARD_DEVICE_LABELS, minus NaviCore's own boards.
const SB_DEVICE_LABELS = {
  'wcb-hcr': ['H-CR'], 'hcr-native': ['H-CR'],
  'wcb-mp3': ['MP3 Trigger'], 'wcb-dfplayer': ['DFPlayer'],
  'ia-magic-panel': ['Magic Panel(IA)'], 'pd-magic-panel': ['Magic Panel(PRINTDRD)'],
  'rseries-logic': ['R-Series Logics'],
  'psi-pro': ['PSI Front', 'PSI Rear'],
  'r2uppityspinner-alt': ['Uppity Spinner', 'Periscope'],
  'roam-a-dome': ['Roam-A-Dome'],
  'astropixels-plus': ['AstroPixels'], 'astropixels-sound': ['AstroPixels'],
  'astropixels-psi': ['AstroPixels'], 'astropixels-logics': ['AstroPixels'],
  'astropixels-holo': ['AstroPixels'], 'astropixels-servo': ['AstroPixels'],
  'flthy-hps': ['Flthy HP Controller', 'HP Controller'],
};
const sbNorm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');

// The groups NaviCore folds boards into (NC_CMDLIB_GROUPS), so the two pickers read the same.
const SB_GROUPS = [
  { id: 'wcb', name: 'WCB', prefix: 'wcb-',
    sub: (name) => {
      const s = String(name).replace(/^WCB\s*[·\-—:]?\s*/i, '').replace(/\(\s*via\s+WCB\s*\)/i, '').replace(/[()]/g, '').trim();
      return s ? s.charAt(0).toUpperCase() + s.slice(1) : 'General';
    } },
  { id: 'astropixels', name: 'AstroPixels', prefix: 'astropixels-',
    sub: (name) => String(name).replace(/^AstroPixelsPlus/i, '').replace(/[()]/g, '').trim() || 'General' },
];
const sbGroupOf = (id) => SB_GROUPS.find(g => String(id).startsWith(g.prefix)) || null;

// ─── Board context ───────────────────────────────────────────────
function sbCfgByWcb(w) {
  for (const [slot, c] of Object.entries(app.cfgs())) if (c && (c.wcbNumber || +slot) === w) return c;
  return null;
}
function sbBoardName(w) {
  const c = sbCfgByWcb(w);
  const a = c && (c.alias || c.clientAlias);
  return 'WCB ' + w + (a ? ' · ' + a : '');
}
// What is on serial port p of WCB w: the port's label, else the device the Wizard has configured there.
function sbPortLabel(w, p) {
  const c = sbCfgByWcb(w);
  if (!c) return '';
  const sp = c.serialPorts && c.serialPorts[p - 1];
  if (sp && sp.label) return sp.label;
  if (c.mp3 && c.mp3.enabled && c.mp3.port === p) return 'MP3 Trigger';
  if (c.dfp && c.dfp.enabled && c.dfp.port === p) return 'DFPlayer';
  if (c.hcr && c.hcr.enabled && c.hcr.port === p) return 'H-CR';
  const m = (c.maestros || []).find(x => !x.wcbNumber && x.port === p);
  if (m) return 'Maestro ' + m.id;
  const l = (c.wleds || []).find(x => x.port === p);
  if (l) return 'WLED ' + l.id;
  return '';
}
// Every WCB the Wizard knows, the one running the sequence first.
function sbBoards(self) {
  const seen = new Set();
  for (const [slot, c] of Object.entries(app.cfgs())) {
    if (!c || c.type === 'client') continue;
    const w = c.wcbNumber || +slot;
    if (w > 0) seen.add(w);
  }
  seen.add(self);
  return [...seen].sort((a, b) => (a === self ? -1 : b === self ? 1 : a - b));
}
function sbCtx(n) {
  const cfg = app.cfgs()[n] || {};
  return {
    n,
    chars: sbChars({ delim: cfg.delimiter, cmd: cfg.cmdChar, func: cfg.funcChar }),
    self: cfg.wcbNumber || n,
    boardName: sbBoardName,
    portLabel: sbPortLabel,
  };
}
function sbRowValue(n, row) {
  const ta = row && row.querySelector('.seq-val-textarea');
  if (!ta) return '';
  const f = app.fn('_seqRowValue');
  const delim = sbCtx(n).chars.delim;
  return f ? f(row, ta, delim) : ta.value.split('\n').map(s => s.trim()).filter(Boolean).join(delim);
}
// Sequences of every loaded board, read from their tables (what a push would send).
function sbAllSequences() {
  const out = [];
  const get = app.fn('getSequencesFromUI');
  for (const slot of Object.keys(app.cfgs())) {
    const c = app.cfgs()[slot];
    if (!c || c.type === 'client') continue;
    const list = get ? get(+slot) : (c.sequences || []);
    for (const s of list) out.push({ slot: +slot, wcb: c.wcbNumber || +slot, key: s.key, value: s.value });
  }
  return out;
}

// ─── Builder state ───────────────────────────────────────────────
const SB_GUTTER   = 184;          // lane label column, px
const SB_ROW_H    = 30;           // one row of steps in a lane
const SB_PPS_MIN  = 10;           // zoom, px per second ...
const SB_PPS_MAX  = 5000;         // ... 5000 = 5 px per ms
const SB_SNAPS    = [0, 1, 5, 10, 25, 50, 100, 250, 500, 1000];
const SB_SNAP_KEY = 'wcbWizard.seqBuilderSnap';
const SB_UNDO_MAX = 200;

const S = {
  n: null, rowId: null, isNew: false, ctx: null,
  key: '', baseKey: '', baseValue: '',
  events: [], markers: [], nextId: 1,
  sel: new Set(), selMarker: null,
  playhead: 0, pps: 200, snap: 10,
  undo: [], redo: [], dirty: false,
  play: null, composer: null, replacing: null, libFilter: '', clip: null,
  libOpen: new Set(), libScroll: null,   // the library list's open sections and scroll, kept across the composer
  importWarnings: [], closeArmed: 0,
  colors: sbColorMap(),      // lane key → colour slot, for this session (sbColorOf)
  refs: [], selRef: null,    // reference tracks: other sequences to line this one up against (view only, never saved)
};
try { const v = parseInt(localStorage.getItem(SB_SNAP_KEY), 10); if (SB_SNAPS.includes(v)) S.snap = v; } catch (_) { /* default */ }

// ─── Reference tracks ────────────────────────────────────────────
// "Line up with…" puts other sequences beside this one, read only: one line each, its steps in their devices' colours.
// A reference starts where this sequence runs it (a ;C/;SEQ step for that key, on this board) or else with this one,
// until it is dragged or given a start of its own. Snapping (sbPlace) makes this sequence's steps stick to its steps.
// Which references a sequence had is remembered in this browser, per board and key.
const SB_REFS_KEY = 'wcbWizard.seqBuilderRefs';
let sbRefSeq = 1;
function sbRefAnchor(r) {
  if (r.wcb !== S.ctx.self) return null;
  let best = null;
  for (const e of S.events) if (sbRecallKey(e.cmd, S.ctx.chars) === r.key && (!best || e.t < best.t)) best = e;
  return best;
}
function sbRefStart(r) {
  if (r.offset != null) return r.offset;
  const a = sbRefAnchor(r);
  return a ? a.t : 0;
}
function sbRefsLoad() {
  if (!S.key) return [];
  try { return (JSON.parse(localStorage.getItem(SB_REFS_KEY) || '{}')[`${S.ctx.self}/${S.key}`]) || []; } catch (_) { return []; }
}
function sbRefsSave() {
  if (!S.key) return;
  try {
    const all = JSON.parse(localStorage.getItem(SB_REFS_KEY) || '{}');
    const k = `${S.ctx.self}/${S.key}`;
    if (S.refs.length) all[k] = S.refs.map(r => ({ wcb: r.wcb, key: r.key, offset: r.offset }));
    else delete all[k];
    localStorage.setItem(SB_REFS_KEY, JSON.stringify(all));
  } catch (_) { /* not remembered */ }
}
// A reference from what the board's table holds now. Its lanes are worked out from its own board, so a device on WCB 2
// keeps the colour it has in this sequence.
function sbMakeRef(spec) {
  const r = { id: 'r' + (sbRefSeq++), wcb: spec.wcb, key: spec.key, offset: spec.offset != null ? spec.offset : null,
              events: [], missing: true };
  const seq = sbAllSequences().find(s => s.wcb === spec.wcb && s.key === spec.key);
  if (!seq) return r;
  const ctx = sbCtx(seq.slot);
  r.missing = false;
  r.events = sbParse(seq.value, ctx.chars).events.map((e, i) => {
    const d = sbDecodeCmd(sbLib, e.cmd, ctx.chars);
    const L = d.hit ? { board: d.hit.board, cmd: d.hit.cmd } : null;
    return { i, t: e.t, cmd: e.cmd, label: sbLabelOf(L, d.hit ? d.hit.values : {}, e.cmd),
             lane: sbLaneOf(e.cmd, ctx, sbDevOfLib(L)) };
  });
  return r;
}

// ─── Placing a time ──────────────────────────────────────────────
// A time being placed sticks, within 8 px, to a step, a reference step, a note or the playhead - so steps line up with
// what they are timed against - and otherwise snaps to the grid. Alt places it to the millisecond. Returns
// { t, to }: `to` is what it stuck to, for the guide line. exclude: step ids being moved (a moving step is no target,
// nor a reference that follows one); refId: a reference being moved; playhead: false while moving the playhead.
const SB_MAGNET_PX = 8;
function sbPlace(t, free, { exclude = null, refId = null, playhead = true } = {}) {
  if (free) return { t: Math.max(0, Math.round(t)), to: null };
  const lim = SB_MAGNET_PX * 1000 / S.pps;
  let best = null;
  const consider = (c) => { const d = Math.abs(c - t); if (d <= lim && (!best || d < best.d)) best = { c, d }; };
  for (const e of S.events) if (!exclude || !exclude.has(e.id)) consider(e.t);
  for (const r of S.refs) {
    if (r.id === refId) continue;
    const a = r.offset == null ? sbRefAnchor(r) : null;
    if (a && exclude && exclude.has(a.id)) continue;
    const s = sbRefStart(r);
    for (const e of r.events) consider(s + e.t);
  }
  for (const m of S.markers) consider(m.t);
  if (playhead) consider(S.playhead);
  if (best) return { t: Math.max(0, Math.round(best.c)), to: Math.max(0, Math.round(best.c)) };
  return { t: sbSnapT(t, false), to: null };
}
function sbGuide(to) {
  const g = $('sb-guide');
  if (!g) return;
  g.hidden = to == null;
  if (to != null) g.style.left = (SB_GUTTER + to * S.pps / 1000) + 'px';
}

const sbSnapT = (t, free) => {
  t = Math.max(0, Math.round(t));
  return (free || !S.snap) ? t : Math.round(t / S.snap) * S.snap;
};
const sbEv = (id) => S.events.find(e => e.id === id);
const sbSnapshot = () => JSON.stringify({
  events: S.events.map(e => ({ id: e.id, t: e.t, cmd: e.cmd, conds: e.conds, note: e.note, lib: e.lib, values: e.values, dest: e.dest })),
  markers: S.markers,
});
function sbRestore(snap) {
  const d = JSON.parse(snap);
  S.events = d.events;
  S.markers = d.markers;
  S.sel = new Set([...S.sel].filter(id => sbEv(id)));
  if (S.selMarker && !S.markers.some(m => m.id === S.selMarker)) S.selMarker = null;
}
// Every discrete change goes through here: one undo step, then a redraw.
function sbCommit(fn, opts) {
  S.undo.push(sbSnapshot());
  if (S.undo.length > SB_UNDO_MAX) S.undo.shift();
  S.redo = [];
  fn();
  S.dirty = true;
  if (sbEditSnap) sbEditSnap = sbSnapshot();   // a field still focused starts its next edit from here
  sbRender(opts);
}
// A field being typed into: one undo step for the whole edit, taken at its first change.
let sbEditSnap = null;
function sbEditTouch() {
  if (sbEditSnap) { S.undo.push(sbEditSnap); S.redo = []; sbEditSnap = null; }
  S.dirty = true;
}
function sbUndo() {
  if (!S.undo.length) return;
  S.redo.push(sbSnapshot());
  sbRestore(S.undo.pop());
  S.dirty = true;
  sbRender();
}
function sbRedo() {
  if (!S.redo.length) return;
  S.undo.push(sbSnapshot());
  sbRestore(S.redo.pop());
  S.dirty = true;
  sbRender();
}

// A step from parsed text: decoded against the library so the inspector can show its fields.
function sbMakeEvent(e) {
  const ev = { id: S.nextId++, t: Math.max(0, Math.round(e.t || 0)), cmd: String(e.cmd || ''),
               conds: (e.conds || []).slice(), note: e.note || '', lib: null, values: {}, dest: 'auto' };
  sbDecodeInto(ev);
  return ev;
}
function sbRouteValue(route, mode) {
  if (route.port != null) return 'W' + (route.wcb || S.ctx.self) + 'S' + route.port;
  if (route.wcb && route.wcb !== S.ctx.self) return 'W' + route.wcb;
  return mode === 'device' ? 'bcast' : 'auto';
}
function sbRouteFrom(dest) {
  let m = /^W(\d+)S(\d)$/.exec(dest || '');
  if (m) return { wcb: +m[1], port: +m[2] };
  m = /^W(\d+)$/.exec(dest || '');
  if (m) return { wcb: +m[1], port: null };
  return { wcb: 0, port: null };
}
function sbDecodeInto(ev) {
  const ch = S.ctx.chars;
  const d = sbDecodeCmd(sbLib, ev.cmd, ch);
  if (d.hit) {
    ev.lib = { b: d.hit.board.id, c: d.hit.cmd.id };
    ev.values = Object.assign({}, d.hit.values);
    ev.dest = sbRouteValue(d.route, sbModeOf(d.hit.board, d.hit.cmd));
  } else if (!d.route.alias && d.body && d.body[0] !== ch.cmd && d.body[0] !== ch.func && sbLibCmd({ b: 'wcb-actions', c: 'wcb.serialText' })) {
    // A device string no library board knows: still a serial step, with its text editable.
    ev.lib = { b: 'wcb-actions', c: 'wcb.serialText' };
    ev.values = { text: d.body };
    ev.dest = sbRouteValue(d.route, 'device');
  } else {
    ev.lib = null; ev.values = {}; ev.dest = 'auto';
  }
}
// Rebuild a step's command text from its library command, field values and destination.
function sbReencode(ev) {
  const L = sbLibCmd(ev.lib);
  if (!L) return;
  const ch = S.ctx.chars;
  const mode = sbModeOf(L.board, L.cmd);
  let body = sbEncode(L.cmd, ev.values);
  const ds = sbDurationOf(L.board, L.cmd);
  if (ds && String(ev.values.__dur || '').trim()) body += (ds.sep || '|') + String(ev.values.__dur).trim();
  if (mode === 'wcb') body = sbFromCanon(body, ch);
  ev.cmd = sbJoinRoute(sbRouteFrom(ev.dest), body, ch, S.ctx.self);
}

// A field's value against its declaration: '' when fine, else why not.
function sbParamProblem(p, v) {
  v = String(v == null ? '' : v);
  if (v === '') return p.required && p.default === undefined ? 'required' : '';
  if (S.ctx && v.includes(S.ctx.chars.delim)) return `contains the delimiter ${S.ctx.chars.delim}`;
  if (p.type === 'int') {
    if (!/^-?\d+$/.test(v)) return 'a whole number';
    if (p.min != null && +v < +p.min) return `at least ${p.min}`;
    if (p.max != null && +v > +p.max) return `at most ${p.max}`;
    if (p.width && v.length !== +p.width) return `${p.width} digit${+p.width === 1 ? '' : 's'}`;
  }
  if (p.pattern) { try { if (!new RegExp(p.pattern).test(v)) return 'not the expected form'; } catch (_) { /* bad pattern: no check */ } }
  return '';
}
function sbEventProblems(ev) {
  const L = sbLibCmd(ev.lib);
  if (!L) return [];
  return (L.cmd.params || []).map(p => ({ p, why: sbParamProblem(p, ev.values[p.name]) })).filter(x => x.why);
}

// What a step reads as on the timeline. The lane already names the device, so ids are left out.
function sbLabelOf(L, values, cmd) {
  if (!L) return cmd || '(type a command)';
  if (L.cmd.id === 'wcb.serialText') return values.text || '(empty)';
  const vals = [];
  for (const p of L.cmd.params || []) {
    if (p.name === 'maestroId' || p.name === 'wledId') continue;
    const v = values[p.name];
    if (v == null || v === '') continue;
    if (p.enum) {
      const en = sbLib.enums[p.enum];
      const hit = en && (en.values || []).find(x => String(x.code) === String(v));
      vals.push(hit ? hit.label : v);
    } else if (p.unit === 'servoPulse') vals.push(Math.round(+v / 4) + ' µs');
    else vals.push(String(v).length > 18 ? String(v).slice(0, 17) + '…' : v);
  }
  if (values.__dur) vals.push(values.__dur + ' s');
  return (L.cmd.shortName || L.cmd.name) + (vals.length ? ' · ' + vals.join(' · ') : '');
}
const sbEvLabel = (ev) => sbLabelOf(sbLibCmd(ev.lib), ev.values, ev.cmd);
const sbEvLane = (ev) => sbLaneOf(ev.cmd, S.ctx, sbDevOfLib(sbLibCmd(ev.lib)));
// How long a step visibly lasts: a duration suffix, or the length of the timed sequence it starts. 0 = a moment.
function sbEvSpan(ev) {
  const L = sbLibCmd(ev.lib);
  if (L && sbDurationOf(L.board, L.cmd) && ev.values.__dur) return (+ev.values.__dur || 0) * 1000;
  const rk = sbRecallKey(ev.cmd, S.ctx.chars);
  if (rk) {
    const v = sbSeqValueHere(rk);
    if (v && sbHasTimer(v, S.ctx.chars)) {
      const p = sbParse(v, S.ctx.chars);
      return p.events.reduce((m, e) => Math.max(m, e.t), 0);
    }
  }
  return 0;
}
// This board's copy of a sequence, from its table (the one being edited excluded: its value is this timeline).
function sbSeqValueHere(key) {
  const tbody = $(`b${S.n}-seq-tbody`);
  if (!tbody) return null;
  for (const row of tbody.querySelectorAll('tr')) {
    if (row.id === S.rowId) continue;
    if ((row.querySelector('.seq-key-input')?.value || '').trim() === key) return sbRowValue(S.n, row);
  }
  return null;
}

function sbCompiled() { return sbCompile({ events: S.events, markers: S.markers }, S.ctx.chars); }
function sbEndT() {
  let end = 0;
  for (const e of S.events) end = Math.max(end, e.t + sbEvSpan(e));
  for (const m of S.markers) end = Math.max(end, m.t);
  for (const r of S.refs) for (const e of r.events) end = Math.max(end, sbRefStart(r) + e.t);
  return end;
}

// ─── DOM ─────────────────────────────────────────────────────────
function sbBuild() {
  if ($('sb-overlay')) return;
  const el = document.createElement('div');
  el.className = 'sb-overlay';
  el.id = 'sb-overlay';
  el.innerHTML = `
  <div class="sb-shell" role="dialog" aria-modal="true" aria-labelledby="sb-title">
    <header class="sb-head">
      <span class="sb-title" id="sb-title">Sequence Builder</span>
      <span class="sb-board" id="sb-board"></span>
      <label class="sb-key">Key <input id="sb-key" type="text" maxlength="15" spellcheck="false" autocomplete="off" placeholder="name"></label>
      <span class="sb-key-err" id="sb-key-err"></span>
      <span class="sb-spacer"></span>
      <button type="button" class="btn btn-ghost btn-sm" id="sb-keys-btn" aria-expanded="false" title="Keyboard and mouse shortcuts">Shortcuts</button>
      <button type="button" class="sb-x" id="sb-close" title="Close" aria-label="Close">✕</button>
    </header>
    <div class="sb-keys" id="sb-keys" hidden>
      <div><b>Drag</b> a command from the library onto the timeline, or click it, set it up and press <b>Add</b>.</div>
      <div><b>Drag</b> a step to move it (snaps to the grid; hold <b>Alt</b> for 1 ms). <b>Shift</b>-click adds to the selection.</div>
      <div><b>←/→</b> nudge 10 ms · <b>Shift</b> 100 ms · <b>Alt</b> 1 ms &nbsp; <b>Del</b> remove &nbsp; <b>Ctrl+D</b> duplicate &nbsp; <b>Ctrl+C/V</b> copy, paste at the playhead</div>
      <div><b>Space</b> preview from the playhead &nbsp; <b>Home</b> playhead to 0 &nbsp; <b>Ctrl+Z / Ctrl+Y</b> undo, redo &nbsp; <b>Ctrl+wheel</b> or <b>+/−</b> zoom &nbsp; <b>F</b> fit</div>
      <div>Click the ruler or an empty spot to move the playhead. <b>Line up with…</b> shows other sequences above yours; drag one sideways to move it.</div>
      <div>A step you drag sticks to a step, a reference step, a note or the playhead within a few pixels; a dashed line shows what it stuck to.</div>
    </div>
    <div class="sb-body">
      <aside class="sb-lib" id="sb-lib" aria-label="Command library"></aside>
      <section class="sb-main">
        <div class="sb-toolbar">
          <button type="button" class="btn btn-primary btn-sm" id="sb-play" title="Preview the timing here, without a board (Space)">▶ Preview</button>
          <button type="button" class="btn btn-ghost btn-sm" id="sb-home" title="Playhead to 0 (Home)">⏮</button>
          <span class="sb-clock" id="sb-clock" title="Playhead">0.000 s</span>
          <span class="sb-now" id="sb-now"></span>
          <span class="sb-spacer"></span>
          <span class="sb-refpick-wrap">
            <button type="button" class="btn btn-ghost btn-sm" id="sb-ref-add" aria-expanded="false" title="Show other sequences beside this one, to time it against them">⇆ Line up with…</button>
            <div class="sb-refpick" id="sb-refpick" hidden>
              <input type="search" id="sb-refpick-q" placeholder="Find a sequence…" spellcheck="false" autocomplete="off">
              <div class="sb-refpick-list" id="sb-refpick-list"></div>
            </div>
          </span>
          <button type="button" class="btn btn-ghost btn-sm" id="sb-custom" title="Add a command you type yourself, at the playhead">+ Custom</button>
          <button type="button" class="btn btn-ghost btn-sm" id="sb-marker" title="Add a note to the timeline at the playhead (stored as a *** comment)">+ Note</button>
          <label class="sb-snap" title="Steps you drag land on this grid (hold Alt to place to the millisecond)">Snap
            <select id="sb-snap">${SB_SNAPS.map(s => `<option value="${s}">${s ? s + ' ms' : 'off'}</option>`).join('')}</select>
          </label>
          <button type="button" class="btn btn-ghost btn-sm" id="sb-zout" title="Zoom out (−)">−</button>
          <button type="button" class="btn btn-ghost btn-sm" id="sb-zin" title="Zoom in (+)">+</button>
          <button type="button" class="btn btn-ghost btn-sm" id="sb-fit" title="Fit the whole sequence (F)">Fit</button>
          <button type="button" class="btn btn-ghost btn-sm" id="sb-undo" title="Undo (Ctrl+Z)">↶</button>
          <button type="button" class="btn btn-ghost btn-sm" id="sb-redo" title="Redo (Ctrl+Y)">↷</button>
        </div>
        <div class="sb-tl" id="sb-tl" tabindex="0" aria-label="Timeline">
          <div class="sb-tl-inner" id="sb-tl-inner">
            <div class="sb-ruler" id="sb-ruler"></div>
            <div class="sb-lanes" id="sb-lanes"></div>
            <div class="sb-guides" id="sb-guides"></div>
            <div class="sb-guide sb-guide-snap" id="sb-guide" hidden></div>
            <div class="sb-playhead" id="sb-playhead"></div>
            <div class="sb-dropline" id="sb-dropline" hidden></div>
            <div class="sb-tip" id="sb-tip" hidden></div>
          </div>
        </div>
        <div class="sb-insp" id="sb-insp"></div>
      </section>
    </div>
    <footer class="sb-foot">
      <div class="sb-out">
        <div class="sb-out-head"><span>Sequence</span><span id="sb-stats"></span><span class="sb-spacer"></span><span id="sb-len"></span></div>
        <code class="sb-code" id="sb-out"></code>
      </div>
      <ul class="sb-warn" id="sb-warn"></ul>
      <div class="sb-actions">
        <button type="button" class="btn btn-ghost" id="sb-cancel">Cancel</button>
        <button type="button" class="btn btn-ghost" id="sb-savetest" title="Apply, save it to the board and run it there (the board's TEST)">Save &amp; Test</button>
        <button type="button" class="btn btn-primary" id="sb-apply" title="Write this into the sequence row; SAVE/UPDATE or a push sends it to the board">Apply to sequence</button>
      </div>
    </footer>
  </div>`;
  document.body.appendChild(el);
  sbWire();
}

function sbWire() {
  $('sb-close').onclick = () => sbClose();
  $('sb-cancel').onclick = () => sbClose();
  $('sb-apply').onclick = () => { if (sbApply()) sbClose(true); };
  $('sb-savetest').onclick = () => sbSaveTest();
  $('sb-keys-btn').onclick = () => {
    const k = $('sb-keys');
    k.hidden = !k.hidden;
    $('sb-keys-btn').setAttribute('aria-expanded', String(!k.hidden));
  };
  $('sb-key').oninput = () => { S.key = $('sb-key').value.trim(); S.dirty = true; sbRenderKey(); sbRenderOut(); };
  $('sb-play').onclick = () => sbTogglePlay();
  $('sb-home').onclick = () => sbSetPlayhead(0, true);
  $('sb-custom').onclick = () => sbAddCustom();
  $('sb-marker').onclick = () => sbAddMarker();
  $('sb-snap').value = String(S.snap);
  $('sb-snap').onchange = () => {
    S.snap = parseInt($('sb-snap').value, 10) || 0;
    try { localStorage.setItem(SB_SNAP_KEY, String(S.snap)); } catch (_) { /* not remembered */ }
  };
  $('sb-zin').onclick = () => sbZoom(1.5);
  $('sb-zout').onclick = () => sbZoom(1 / 1.5);
  $('sb-fit').onclick = () => sbFit();
  $('sb-undo').onclick = () => sbUndo();
  $('sb-redo').onclick = () => sbRedo();
  $('sb-ref-add').onclick = () => sbRefPick(!!$('sb-refpick').hidden);
  $('sb-refpick-q').addEventListener('input', sbRenderRefPick);
  $('sb-refpick-list').addEventListener('click', (e) => {
    const b = e.target.closest('[data-refadd]');
    if (!b || b.disabled) return;
    const [wcb, ...rest] = b.dataset.refadd.split('/');
    sbAddRef(+wcb, rest.join('/'));
  });
  document.addEventListener('pointerdown', (e) => {
    if ($('sb-refpick') && !$('sb-refpick').hidden && !e.target.closest('.sb-refpick-wrap')) sbRefPick(false);
  });

  const tl = $('sb-tl');
  tl.addEventListener('wheel', (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    sbZoom(e.deltaY < 0 ? 1.25 : 0.8, e.clientX);
  }, { passive: false });
  tl.addEventListener('pointerdown', sbOnPointerDown);
  tl.addEventListener('dragover', sbOnDragOver);
  tl.addEventListener('dragleave', (e) => { if (!tl.contains(e.relatedTarget)) $('sb-dropline').hidden = true; });
  tl.addEventListener('drop', sbOnDrop);
  $('sb-overlay').addEventListener('keydown', sbOnKey);
  $('sb-insp').addEventListener('focusin', () => { if (!sbEditSnap) sbEditSnap = sbSnapshot(); });
  $('sb-insp').addEventListener('focusout', () => { sbEditSnap = null; });
  $('sb-warn').addEventListener('click', (e) => {
    const li = e.target.closest('[data-ids]');
    if (!li || !li.dataset.ids) return;
    S.sel = new Set(li.dataset.ids.split(',').map(Number).filter(id => sbEv(id)));
    S.selMarker = null;
    sbRender();
    sbScrollTo(Math.min(...[...S.sel].map(id => sbEv(id).t)));
  });
  window.addEventListener('resize', () => { if (sbIsOpen()) sbRenderTimeline(); });
}

const sbIsOpen = () => !!$('sb-overlay') && $('sb-overlay').classList.contains('open');

// ─── Open / close / apply ────────────────────────────────────────
async function open(n, rowId) {
  const row = rowId ? $(rowId) : null;
  if (rowId && !row) return;
  sbBuild();
  S.n = n; S.rowId = rowId || null; S.isNew = !row; S.ctx = sbCtx(n);
  const value = row ? sbRowValue(n, row) : '';
  const parsed = sbParse(value, S.ctx.chars);
  if (parsed.valueVerb) {
    toast('This sequence stores a value of its own (?SEQ,SAVE, ?CS or ?MGMT), which the board runs without delays: edit it as text.', 'error', 9000);
    return;
  }
  S.key = row ? (row.querySelector('.seq-key-input')?.value || '').trim() : '';
  S.baseKey = S.key;
  S.baseValue = value;
  S.nextId = 1;
  S.events = [];
  S.markers = parsed.markers.map(m => ({ id: S.nextId++, t: m.t, text: m.text, after: m.after }));
  S.sel = new Set(); S.selMarker = null;
  S.undo = []; S.redo = []; S.dirty = false; S.clip = S.clip || null;
  S.composer = null; S.playhead = 0; S.closeArmed = 0;
  S.importWarnings = parsed.warnings;
  sbStop();
  $('sb-board').textContent = sbBoardName(S.ctx.self) + (S.isNew ? ' · new sequence' : '');
  $('sb-key').value = S.key;
  sbRenderKey();
  if (parsed.warnings.length) {
    toast(`Read this sequence with ${parsed.warnings.length} note${parsed.warnings.length === 1 ? '' : 's'}: see under the timeline.`, 'warning', 6000);
  }
  $('sb-overlay').classList.add('open');
  document.body.classList.add('sb-noscroll');
  sbRefPick(false);
  S.events = parsed.events.map(sbMakeEvent);   // decoded as far as the library has loaded
  const refSpecs = sbRefsLoad();
  S.refs = refSpecs.map(sbMakeRef);
  S.selRef = null;
  sbSeedColors();
  sbRenderLib();
  sbRender();
  sbFit();
  (S.isNew ? $('sb-key') : $('sb-tl')).focus();
  await sbLibReady;
  if (!sbIsOpen() || S.n !== n || S.rowId !== (rowId || null)) return;
  for (const ev of S.events) sbDecodeInto(ev);   // again, now the whole library is in
  S.refs = S.refs.map(r => Object.assign(sbMakeRef(r), { id: r.id }));
  sbSeedColors();
  sbRenderLib();
  sbRender();
}

// Colours by first appearance in time order, this sequence's lanes first and then its references' - the order its
// timing strip uses, so the two match on opening.
function sbSeedColors() {
  S.colors = sbColorMap();
  for (const ev of S.events.slice().sort((a, b) => a.t - b.t)) { const ln = sbEvLane(ev); sbColorOf(S.colors, ln.key, null, sbBoardSlot(ln.board)); }
  for (const r of S.refs) for (const e of r.events) sbColorOf(S.colors, e.lane.key, null, sbBoardSlot(e.lane.board));
}

function sbClose(force) {
  if (!force && S.dirty && Date.now() - S.closeArmed > 4000) {
    S.closeArmed = Date.now();
    $('sb-cancel').textContent = 'Discard changes?';
    $('sb-cancel').classList.add('btn-danger');
    setTimeout(() => {
      if (!$('sb-cancel')) return;
      $('sb-cancel').textContent = 'Cancel';
      $('sb-cancel').classList.remove('btn-danger');
    }, 4000);
    return;
  }
  sbStop();
  $('sb-cancel').textContent = 'Cancel';
  $('sb-cancel').classList.remove('btn-danger');
  $('sb-overlay').classList.remove('open');
  document.body.classList.remove('sb-noscroll');
  S.dirty = false;
}

// 1-15 characters, no comma, not a name the store keeps for itself, not another row's key (docs/SEQUENCE_INVENTORY.md
// §3b). '' when fine.
function sbKeyProblem(key) {
  if (!key) return 'Give the sequence a key.';
  if (key.length > 15) return 'A key is at most 15 characters.';
  if (key.includes(',')) return 'A key cannot contain a comma.';
  if (key === 'key_list' || key === 'seq_mig_done') return `"${key}" is reserved.`;
  const tbody = $(`b${S.n}-seq-tbody`);
  for (const row of tbody ? tbody.querySelectorAll('tr') : []) {
    if (row.id === S.rowId) continue;
    if ((row.querySelector('.seq-key-input')?.value || '').trim() === key) return `This board already has a sequence "${key}".`;
  }
  return '';
}

function sbProblems() {
  const value = sbCompiled();
  const list = sbAnalyze({ events: S.events, markers: S.markers },
    { chars: S.ctx.chars, key: S.key, value, seqValue: sbSeqValueHere });
  for (const ev of S.events) {
    for (const { p, why } of sbEventProblems(ev)) {
      list.unshift({ level: 'error', ids: [ev.id], text: `${sbEvLabel(ev)} at ${sbFmt(ev.t)}: ${sbParamLabel(p)} must be ${why}.` });
    }
    if (ev.lib && sbLibCmd(ev.lib) && sbModeOf(sbLibCmd(ev.lib).board, sbLibCmd(ev.lib).cmd) === 'device' && ev._guessed) {
      list.push({ level: 'warn', ids: [ev.id], text: `No board has a port labelled for ${sbLibCmd(ev.lib).board.name}: ${sbEvLabel(ev)} goes to ${sbDestLabel(ev.dest)}. Pick the port it is wired to.` });
    }
  }
  return { value, list };
}

// Write the timeline into the sequence row. False when it cannot (a bad key or an error).
function sbApply() {
  const { value, list } = sbProblems();
  const keyWhy = sbKeyProblem(S.key);
  if (keyWhy) { sbRenderKey(); $('sb-key').focus(); toast(keyWhy, 'error'); return false; }
  const err = list.find(w => w.level === 'error');
  if (err) { toast(err.text, 'error', 8000); return false; }
  if (!value) { toast('The sequence is empty: add a command first.', 'error'); return false; }
  const n = S.n;
  const cfg = app.cfgs()[n] || {};
  const delim = S.ctx.chars.delim;
  if (S.isNew) {
    const append = app.fn('appendSequenceRow');
    if (!append) { toast('The sequence table is not available.', 'error'); return false; }
    append(n, S.key, value);
    const rows = document.querySelectorAll(`#b${n}-seq-tbody tr`);
    const row = rows[rows.length - 1];
    S.rowId = row ? row.id : null;
    S.isNew = false;
    // Like addSequenceRow: drop the filter and turn to the last page, so the new row is on screen.
    const pager = app.fn('_seqPager') && app.fn('_seqPager')(n);
    if (pager) {
      pager.filter = '';
      const f = $(`b${n}-seq-filter`);
      if (f) f.value = '';
      pager.page = Infinity;
    }
    app.fn('applySeqPager') && app.fn('applySeqPager')(n);
    app.fn('refreshAllSeqSharedIndicators') && app.fn('refreshAllSeqSharedIndicators')();
  } else {
    const row = $(S.rowId);
    if (!row) { toast('That sequence row is gone.', 'error'); return false; }
    const keyIn = row.querySelector('.seq-key-input');
    const ta = row.querySelector('.seq-val-textarea');
    const toLines = app.fn('seqValueToLines');
    if (keyIn && keyIn.value.trim() !== S.key) {
      keyIn.value = S.key;
      keyIn.dispatchEvent(new Event('input', { bubbles: true }));
    }
    if (ta && value !== sbRowValue(n, row)) {
      ta.value = toLines ? toLines(value, delim, cfg.cmdChar || ';') : value.split(delim).join('\n');
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }
  app.fn('updateActionSummary') && app.fn('updateActionSummary')(n);
  S.baseKey = S.key;
  S.baseValue = value;
  S.dirty = false;
  renderRowThumb(n, S.rowId);
  return true;
}

async function sbSaveTest() {
  const n = S.n;
  const connected = !!(app.conns()[n] && app.conns()[n].isConnected && app.conns()[n].isConnected())
                 || app.relays()[n] !== undefined;
  if (!connected) { toast('Connect this board (directly or through a relay) to save and test.', 'error'); return; }
  if (!sbApply()) return;
  const save = app.fn('updateSequence'), play = app.fn('playSequence');
  if (!save || !play || !S.rowId) return;
  const btn = $('sb-savetest');
  btn.disabled = true;
  try {
    await save(n, S.rowId);
    await new Promise(r => setTimeout(r, 300));
    await play(n, S.rowId);
    sbStop();
    sbSetPlayhead(0);
    sbTogglePlay();
  } finally {
    btn.disabled = false;
  }
}

// ─── Rendering ───────────────────────────────────────────────────
function sbRender(opts) {
  sbRenderTimeline();
  if (!opts || opts.insp !== false) sbRenderInspector();
  sbRenderOut();
  $('sb-undo').disabled = !S.undo.length;
  $('sb-redo').disabled = !S.redo.length;
}

function sbRenderKey() {
  const why = S.key === S.baseKey && !S.isNew ? '' : sbKeyProblem(S.key);
  $('sb-key-err').textContent = why;
  $('sb-key').classList.toggle('sb-bad', !!why);
}

function sbTickStep() {
  const steps = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000];
  for (const s of steps) if (s * S.pps / 1000 >= 80) return s;
  return 600000;
}
function sbTickLabel(ms, step) {
  const d = step >= 1000 ? 0 : step >= 100 ? 1 : step >= 10 ? 2 : 3;
  if (ms < 60000) return (ms / 1000).toFixed(d) + ' s';
  return Math.floor(ms / 60000) + ':' + ((ms % 60000) / 1000).toFixed(d).padStart(d ? d + 3 : 2, '0');
}

// Stack a lane's steps into rows where their labels would overlap; past three rows, steps too close to label at this
// zoom shrink to markers (their tooltip still names them). items: { at (ms), label, span (px), hasIf }.
function sbStack(items) {
  const place = (compact) => {
    const ends = [];
    for (const it of items.slice().sort((a, b) => a.at - b.at)) {
      const x = it.at * S.pps / 1000;
      const w = compact ? 14 : Math.min(190, 26 + it.label.length * 6.4 + (it.hasIf ? 22 : 0));
      let r = ends.findIndex(end => end + 3 <= x);
      if (r < 0) { r = ends.length; ends.push(0); }
      ends[r] = x + Math.max(w, it.span);
      Object.assign(it, { row: r, x, w, compact });
    }
    return Math.max(1, ends.length);
  };
  const rows = place(false);
  return rows > 3 ? place(true) : rows;
}

// This sequence's lanes (one per device) and its reference tracks, placed and coloured.
function sbLayout() {
  const lanes = new Map();
  for (const ev of S.events) {
    const ln = sbEvLane(ev);
    if (!lanes.has(ln.key)) lanes.set(ln.key, Object.assign(ln, { items: [] }));
    ev._label = sbEvLabel(ev);
    lanes.get(ln.key).items.push({ ev, at: ev.t, label: ev._label, span: sbEvSpan(ev) * S.pps / 1000, hasIf: ev.conds.length > 0 });
  }
  const list = [...lanes.values()].sort((a, b) => (a.order - b.order) || a.label.localeCompare(b.label));
  const present = new Set(list.map(l => l.key));
  for (const r of S.refs) for (const e of r.events) present.add(e.lane.key);
  for (const ln of list) { ln.slot = sbColorOf(S.colors, ln.key, present, sbBoardSlot(ln.board)); ln.rows = sbStack(ln.items); }
  const refs = S.refs.map(r => {
    const start = sbRefStart(r);
    const items = r.events.map(e => ({ r, e, at: start + e.t, label: e.label, span: 0, hasIf: false,
                                       slot: sbColorOf(S.colors, e.lane.key, present, sbBoardSlot(e.lane.board)) }));
    return { r, start, items, rows: items.length ? sbStack(items) : 1 };
  });
  return { lanes: list, refs };
}

function sbRenderTimeline() {
  const tl = $('sb-tl');
  if (!tl) return;
  const { lanes, refs } = sbLayout();
  const endT = Math.max(sbEndT(), S.playhead);
  const trackW = Math.max(tl.clientWidth - SB_GUTTER - 2, endT * S.pps / 1000 + 260);
  const inner = $('sb-tl-inner');
  inner.style.width = (SB_GUTTER + trackW) + 'px';

  // Ruler: ticks, then the markers on it.
  const step = sbTickStep();
  const minor = step / 5;
  let ticks = '';
  const totalMs = trackW * 1000 / S.pps;
  for (let i = 0; i * minor <= totalMs; i++) {
    const ms = i * minor;
    const x = ms * S.pps / 1000;
    ticks += i % 5 === 0
      ? `<div class="sb-tick sb-tick-major" style="left:${x}px"><span>${sbTickLabel(Math.round(ms), step)}</span></div>`
      : `<div class="sb-tick" style="left:${x}px"></div>`;
  }
  let mks = '';
  for (const m of S.markers) {
    mks += `<div class="sb-mk${S.selMarker === m.id ? ' sel' : ''}" data-mk="${m.id}" style="left:${m.t * S.pps / 1000}px" title="${esc(sbFmt(m.t) + ' · ' + m.text)}"><span>${esc(m.text)}</span></div>`;
  }
  $('sb-ruler').innerHTML = `<div class="sb-corner">${S.events.length} step${S.events.length === 1 ? '' : 's'}</div>
    <div class="sb-ruler-track" style="width:${trackW}px">${ticks}${mks}</div>`;

  // Lanes, with a faint grid line at each major tick: the reference tracks first, then this sequence's.
  const grid = `background-size:${step * S.pps / 1000}px 100%`;
  const pos = (it) => `left:${it.x}px;top:${it.row * SB_ROW_H + 3}px;width:${it.w}px`;
  const laneLabel = (name, sub, title, extra = '') =>
    `<div class="sb-lane-label" title="${esc(title)}"${extra}><i></i><span class="sb-ln-name">${esc(name)}</span>`
    + (sub ? `<span class="sb-ln-sub">${esc(sub)}</span>` : '');
  let html = '';
  for (const { r, start, items, rows } of refs) {
    const anchor = r.offset == null ? sbRefAnchor(r) : null;
    const sub = r.missing ? `not on ${sbBoardName(r.wcb)} any more`
      : `${r.wcb === S.ctx.self ? 'this board' : sbBoardName(r.wcb)} · from ${sbFmt(start)}${anchor ? ', run by this one' : ''}`;
    let pills = '';
    for (const it of items) {
      pills += `<div class="sb-ev sb-rev ${sbColorClass(it.slot)}${it.compact ? ' compact' : ''}" data-rev="${r.id}:${it.e.i}" style="${pos(it)}"`
        + ` title="${esc([sbFmt(it.at), it.label, it.e.cmd, `(${r.key}, ${sbFmt(it.e.t)} into it)`].join('\n'))}">`
        + (it.compact ? '' : `<span class="sb-ev-label">${esc(it.label)}</span>`) + '</div>';
    }
    html += `<div class="sb-lane sb-ref${S.selRef === r.id ? ' sel' : ''}" style="height:${rows * SB_ROW_H + 6}px">`
      + laneLabel('⇆ ' + r.key, sub, `${r.key} on ${sbBoardName(r.wcb)}: drag its line to move it; click to select`, ` data-refsel="${r.id}"`)
      + `<button type="button" class="sb-ref-x" data-refx="${r.id}" title="Stop showing ${esc(r.key)}" aria-label="Stop showing ${esc(r.key)}">✕</button></div>`
      + `<div class="sb-lane-track" data-reftrack="${r.id}" style="width:${trackW}px;${grid}">${pills}</div></div>`;
  }
  if (refs.length) {
    html += `<div class="sb-lane sb-divider"><div class="sb-lane-label"><span class="sb-ln-name">${esc(S.key || 'This sequence')}</span></div>`
      + `<div class="sb-lane-track" style="width:${trackW}px"></div></div>`;
  }
  if (!S.events.length) {
    html += `<div class="sb-lane sb-lane-empty"><div class="sb-lane-label">—</div>
      <div class="sb-lane-track sb-empty-hint" style="width:${trackW}px;${grid}">Drag a command here from the library, or click one and press Add.</div></div>`;
  }
  const problems = new Set(S.events.filter(ev => sbEventProblems(ev).length || !String(ev.cmd).trim()).map(ev => ev.id));
  for (const ln of lanes) {
    let pills = '';
    for (const it of ln.items) {
      const ev = it.ev;
      const cls = ['sb-ev', sbColorClass(ln.slot)];
      if (S.sel.has(ev.id)) cls.push('sel');
      if (it.compact) cls.push('compact');
      if (problems.has(ev.id)) cls.push('bad');
      const tip = [sbFmt(ev.t), it.label, ev.cmd, ev.conds.length ? 'IF ' + ev.conds.join(' and ') : '', ev.note ? '*** ' + ev.note : '']
        .filter(Boolean).join('\n');
      pills += `<div class="${cls.join(' ')}" data-ev="${ev.id}" style="${pos(it)}" title="${esc(tip)}">`
        + (it.span > it.w ? `<div class="sb-ev-span" style="left:${it.w - 4}px;width:${it.span - it.w}px"></div>` : '')
        + (ev.conds.length ? '<b class="sb-if">IF</b>' : '')
        + (it.compact ? '' : `<span class="sb-ev-label">${esc(it.label)}</span>`)
        + '</div>';
    }
    html += `<div class="sb-lane ${sbColorClass(ln.slot)}" style="height:${ln.rows * SB_ROW_H + 6}px">`
      + laneLabel(ln.name || ln.label, ln.sub, ln.label) + '</div>'
      + `<div class="sb-lane-track" style="width:${trackW}px;${grid}">${pills}</div></div>`;
  }
  $('sb-lanes').innerHTML = html;

  // A selected reference draws its steps' times through every lane, to line steps up by eye.
  const sel = refs.find(x => x.r.id === S.selRef);
  $('sb-guides').innerHTML = sel
    ? sel.items.map(it => `<div class="sb-guide ${sbColorClass(it.slot)}" style="left:${SB_GUTTER + it.x}px"></div>`).join('')
    : '';
  sbPlacePlayhead();
}

function sbPlacePlayhead() {
  const x = SB_GUTTER + S.playhead * S.pps / 1000;
  $('sb-playhead').style.left = x + 'px';
  $('sb-clock').textContent = sbFmt(S.playhead);
}

function sbDestLabel(dest) {
  if (dest === 'auto') return 'the board that has the device';
  if (dest === 'bcast') return 'every port (broadcast)';
  const r = sbRouteFrom(dest);
  const where = r.wcb === S.ctx.self ? 'this board' : sbBoardName(r.wcb);
  if (r.port == null) return where;
  const l = sbPortLabel(r.wcb, r.port);
  return `${where} S${r.port}${l ? ' (' + l + ')' : ''}`;
}

// The "Send to" choices for a mode. The current value is always offered, even for a board the Wizard has not loaded.
function sbDestOptions(mode, cur) {
  const self = S.ctx.self;
  const opts = [];
  if (mode === 'device') {
    for (const w of sbBoards(self)) {
      for (let p = 1; p <= 5; p++) {
        const l = sbPortLabel(w, p);
        opts.push({ v: `W${w}S${p}`, t: `${w === self ? 'This board' : sbBoardName(w)} · Serial ${p}${l ? ' · ' + l : ''}`, g: w === self ? 'This board' : sbBoardName(w) });
      }
      // Serial 0 is the USB port: what DroidNet listens on (wcbmaestro.droidnet).
      opts.push({ v: `W${w}S0`, t: `${w === self ? 'This board' : sbBoardName(w)} · Serial 0 (USB)`, g: w === self ? 'This board' : sbBoardName(w) });
    }
    opts.push({ v: 'bcast', t: 'Every port, every board (broadcast)', g: 'Broadcast' });
    for (const w of sbBoards(self)) if (w !== self) opts.push({ v: 'W' + w, t: `Every port on ${sbBoardName(w)}`, g: 'Broadcast' });
  } else {
    opts.push({ v: 'auto', t: 'Auto: the board that has the device', g: '' });
    for (const w of sbBoards(self)) if (w !== self) opts.push({ v: 'W' + w, t: `Run it on ${sbBoardName(w)}`, g: '' });
  }
  if (cur && !opts.some(o => o.v === cur)) opts.push({ v: cur, t: sbDestLabel(cur), g: '' });
  let html = '', group = null;
  for (const o of opts) {
    if (o.g !== group) {
      if (group) html += '</optgroup>';
      if (o.g) html += `<optgroup label="${esc(o.g)}">`;
      group = o.g || null;
    }
    html += `<option value="${esc(o.v)}"${o.v === cur ? ' selected' : ''}>${esc(o.t)}</option>`;
  }
  if (group) html += '</optgroup>';
  return html;
}

// Suggestions for a field: the sequence keys, variables, WLED ids and Maestro ids the loaded boards have.
function sbSuggest(board, p) {
  const out = [];
  const add = (v, l) => { if (v !== '' && v != null && !out.some(o => o.v === String(v))) out.push({ v: String(v), l }); };
  if (p.source === 'wcb.sequences' || p.name === 'key') {
    for (const s of sbAllSequences()) add(s.key, `WCB ${s.wcb}`);
  } else if (p.source === 'wcb.variables') {
    for (const c of Object.values(app.cfgs())) for (const v of (c && c.variables) || []) add(v.name, `WCB ${c.wcbNumber}`);
  } else if (p.name === 'wledId') {
    for (const c of Object.values(app.cfgs())) {
      for (const l of (c && c.wleds) || []) add(l.id, `on WCB ${c.wcbNumber}`);
      for (const l of (c && c.wledRemotes) || []) add(l.id, `on WCB ${l.host}`);
    }
  } else if (p.name === 'maestroId' || (board.id === 'maestro' && p.name === 'id')) {
    for (const c of Object.values(app.cfgs())) for (const m of (c && c.maestros) || []) add(m.id, m.wcbNumber ? `on WCB ${m.wcbNumber}` : `on WCB ${c.wcbNumber}`);
  }
  return out;
}

// A field's name: its own label, else its enum's (DroidNet files label the enum, not the param), else the param name
// spaced out ("moveTime" → "Move time").
function sbParamLabel(p) {
  if (p.label) return p.label;
  const en = p.enum && sbLib.enums[p.enum];
  if (en && en.label) return en.label;
  const s = String(p.name).replace(/([a-z])([A-Z])/g, '$1 $2').replace(/_/g, ' ').toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// A library command's fields. pfx keeps ids apart between the composer ('sbc') and the inspector ('sbi').
function sbFieldsHtml(pfx, L, values) {
  let h = '';
  (L.cmd.params || []).forEach((p, i) => {
    const id = `${pfx}-p${i}`;
    const v = values[p.name] != null ? String(values[p.name]) : '';
    const why = sbParamProblem(p, v);
    const bad = why ? ' sb-bad' : '';
    let f;
    if (p.unit === 'servoPulse') {
      const us = v === '' ? '' : Math.round(+v / 4);
      f = `<span class="sb-pulse"><input type="number" class="sb-in${bad}" id="${id}" data-pulse="${esc(p.name)}" min="500" max="2500" step="1" value="${esc(us)}" title="Pulse width in µs (500-2500)"><small>µs</small>`
        + `<input type="number" class="sb-in" id="${id}-mv" data-pulsemv="${esc(p.name)}" min="0" max="16383" step="1" value="${esc(v)}" title="Maestro target (µs × 4)"><small>Maestro</small></span>`;
    } else if (p.type === 'bool') {
      const on = v && v !== 'false' && v !== '0';
      f = `<input type="checkbox" id="${id}" data-pname="${esc(p.name)}"${on ? ' checked' : ''}>`;
    } else if (p.enum) {
      const en = sbLib.enums[p.enum];
      const vals = (en && en.values) || [];
      let o = vals.map(x => `<option value="${esc(x.code)}"${String(x.code) === v ? ' selected' : ''}>${esc(x.label)} (${esc(x.code)})</option>`).join('');
      if (v && !vals.some(x => String(x.code) === v)) o += `<option value="${esc(v)}" selected>${esc(v)}</option>`;
      f = `<select class="sb-in${bad}" id="${id}" data-pname="${esc(p.name)}">${o}</select>`;
    } else {
      const sugg = sbSuggest(L.board, p);
      const dl = sugg.length ? `<datalist id="${id}-dl">${sugg.map(s => `<option value="${esc(s.v)}">${esc(s.l)}</option>`).join('')}</datalist>` : '';
      const isInt = p.type === 'int';
      f = `<input type="${isInt ? 'number' : 'text'}" class="sb-in${bad}${isInt ? '' : ' sb-wide'}" id="${id}" data-pname="${esc(p.name)}"`
        + (isInt && p.min != null ? ` min="${esc(p.min)}"` : '') + (isInt && p.max != null ? ` max="${esc(p.max)}"` : '')
        + (sugg.length ? ` list="${id}-dl"` : '') + ` value="${esc(v)}" spellcheck="false" autocomplete="off">${dl}`;
    }
    h += `<label class="sb-field" for="${id}"><span>${esc(sbParamLabel(p))}</span>${f}<em class="sb-why" id="${id}-why">${why ? esc(why) : ''}</em></label>`;
  });
  const ds = sbDurationOf(L.board, L.cmd);
  if (ds) {
    h += `<label class="sb-field" for="${pfx}-dur"><span>Runs for (${esc(ds.unit || 'seconds')}, optional)</span>`
      + `<input type="number" class="sb-in" id="${pfx}-dur" data-pname="__dur" min="0" ${ds.max ? `max="${ds.max}"` : ''} value="${esc(values.__dur || '')}"><em class="sb-why"></em></label>`;
  }
  return h;
}

function sbWireFields(box, L, values, onChange) {
  const params = {};
  (L.cmd.params || []).forEach(p => { params[p.name] = p; });
  const check = (el, p) => {
    const why = p ? sbParamProblem(p, values[p.name]) : '';
    el.classList.toggle('sb-bad', !!why);
    const w = document.getElementById(el.id + '-why');
    if (w) w.textContent = why;
  };
  box.querySelectorAll('[data-pname]').forEach(el => {
    const fn = () => {
      values[el.dataset.pname] = el.type === 'checkbox' ? (el.checked ? '1' : '') : el.value;
      check(el, params[el.dataset.pname]);
      onChange();
    };
    el.addEventListener('input', fn);
    el.addEventListener('change', fn);
  });
  // A servo position as two linked fields: pulse width in µs, and the Maestro value (µs × 4) the command carries.
  box.querySelectorAll('[data-pulse]').forEach(us => {
    const name = us.dataset.pulse;
    const mv = document.getElementById(us.id + '-mv');
    us.addEventListener('input', () => {
      if (us.value.trim() === '' || isNaN(+us.value)) return;
      values[name] = String(Math.round(+us.value * 4));
      if (mv) mv.value = values[name];
      check(us, params[name]);
      onChange();
    });
    if (mv) mv.addEventListener('input', () => {
      if (mv.value.trim() === '' || isNaN(+mv.value)) return;
      values[name] = String(Math.round(+mv.value));
      us.value = String(Math.round(+mv.value / 4));
      check(us, params[name]);
      onChange();
    });
  });
}

// ─── Library panel (NaviCore's picker, as a side panel) ──────────
function sbRenderLib() {
  const box = $('sb-lib');
  if (!box) return;
  if (S.composer) return sbRenderComposer();
  box.innerHTML = `
    <div class="sb-lib-head">
      <input type="search" id="sb-lib-search" placeholder="Search commands…" spellcheck="false" autocomplete="off" value="${esc(S.libFilter)}">
      <button type="button" class="btn btn-ghost btn-sm" id="sb-lib-import" title="Import a command library .json (a DroidNet board file): kept in this browser">Import…</button>
      <input type="file" id="sb-lib-file" accept=".json,application/json" hidden>
    </div>
    ${sbLibError ? `<div class="sb-lib-err">${esc(sbLibError)}</div>` : ''}
    <div class="sb-lib-list" id="sb-lib-list"></div>
    <details class="sb-lib-online">
      <summary>Check online for a newer library (optional: a full library is already loaded)</summary>
      <div class="sb-lib-fetch"><input type="text" id="sb-lib-url" value="${esc(SB_UPSTREAM)}" spellcheck="false">
        <button type="button" class="btn btn-ghost btn-sm" id="sb-lib-fetch">Fetch</button></div>
      <pre class="sb-lib-log" id="sb-lib-log"></pre>
    </details>`;
  const search = $('sb-lib-search');
  search.addEventListener('input', () => { S.libFilter = search.value; sbRenderLibList(); });
  $('sb-lib-import').onclick = () => $('sb-lib-file').click();
  $('sb-lib-file').onchange = sbImportFile;
  $('sb-lib-fetch').onclick = sbFetchOnline;
  sbRenderLibList();
}

// A section stays open across redraws (S.libOpen) - going to the composer and back, or a search cleared.
const sbSecOpen = (key, f) => (f || S.libOpen.has(key) ? ' open' : '');

function sbBoardSection(board, f, label) {
  const matches = (board.commands || []).filter(c => c && c.template && !SB_HIDDEN_CMDS.has(c.id)
    && (!f || (c.name + ' ' + (c.category || '') + ' ' + c.template + ' ' + board.name).toLowerCase().includes(f)));
  if (!matches.length) return { html: '', count: 0 };
  const cnt = matches.length;
  const cntLabel = f ? `${cnt} match${cnt === 1 ? '' : 'es'}` : `${cnt} cmd${cnt === 1 ? '' : 's'}`;
  const col = sbColorClass(sbBoardSlot(board.id));   // the colour this device's lane takes on the timeline
  let html = `<details class="sb-libsec ${col}" data-sec="b:${esc(board.id)}"${sbSecOpen('b:' + board.id, f)}><summary><span class="sb-caret">▸</span><i class="sb-sw"></i>`
    + `<span class="sb-libsec-name">${esc(label || board.name)}${board.custom ? ' <em>· custom</em>' : ''}</span><span class="sb-libsec-cnt">${cntLabel}</span></summary><div class="sb-libsec-body">`;
  for (const c of matches) {
    html += `<button type="button" class="sb-libitem ${col}" draggable="true" data-b="${esc(board.id)}" data-c="${esc(c.id)}" title="Click to set it up, or drag it onto the timeline">`
      + `<div class="sb-libitem-name">${esc(c.name)}</div><div class="sb-libitem-tpl">${esc(c.template)}</div></button>`;
  }
  return { html: html + '</div></details>', count: cnt, slot: sbBoardSlot(board.id) };
}

function sbRenderLibList() {
  const list = $('sb-lib-list');
  if (!list) return;
  const f = S.libFilter.trim().toLowerCase();
  const entries = [], at = {};
  for (const board of sbLib.boards) {
    if (board.hidden) continue;
    const g = sbGroupOf(board.id);
    if (!g) { entries.push({ board }); continue; }
    if (at[g.id] === undefined) { at[g.id] = entries.length; entries.push({ group: g, boards: [board] }); }
    else entries[at[g.id]].boards.push(board);
  }
  let html = '';
  for (const e of entries) {
    if (e.board) { html += sbBoardSection(e.board, f).html; continue; }
    let inner = '', total = 0;
    const slots = [];
    for (const b of e.boards) {
      const r = sbBoardSection(b, f, e.group.sub(b.name));
      inner += r.html; total += r.count;
      if (r.html && !slots.includes(r.slot)) slots.push(r.slot);
    }
    if (!inner) continue;
    const cnt = f ? `${total} match${total === 1 ? '' : 'es'}` : `${total} cmds · ${e.boards.length} libraries`;
    // A group shows its members' colours: swatches, and its edge striped in them (--grp, seq-builder.css).
    const sw = slots.map(s => `<i class="sb-sw ${sbColorClass(s)}"></i>`).join('');
    const stripe = slots.length
      ? 'linear-gradient(to bottom,' + slots.map((s, i) => {
          const c = s >= 0 ? `var(--sb-c${s + 1})` : 'var(--sb-cx)';
          return `${c} ${(i * 100 / slots.length).toFixed(2)}% ${((i + 1) * 100 / slots.length).toFixed(2)}%`;
        }).join(',') + ')'
      : '';
    html += `<details class="sb-libsec sb-libgrp" data-sec="g:${esc(e.group.id)}"${sbSecOpen('g:' + e.group.id, f)}><summary${stripe ? ` style="--grp:${stripe}"` : ''}><span class="sb-caret">▸</span>`
      + `<span class="sb-libsec-name">${esc(e.group.name)}</span><span class="sb-sws">${sw}</span>`
      + `<span class="sb-libsec-cnt">${cnt}</span></summary><div class="sb-libgroup">${inner}</div></details>`;
  }
  if (!html) html = sbLib.boards.length
    ? '<div class="sb-lib-none">No commands match. Use + Custom to type one.</div>'
    : '<div class="sb-lib-none">Loading the command library…</div>';
  list.innerHTML = html;
  // Remember what is open (not while a search opens everything), and come back to where the list was scrolled.
  list.querySelectorAll('details[data-sec]').forEach(d => d.addEventListener('toggle', () => {
    if (S.libFilter.trim()) return;
    if (d.open) S.libOpen.add(d.dataset.sec); else S.libOpen.delete(d.dataset.sec);
  }));
  if (S.libScroll != null) { list.scrollTop = S.libScroll; S.libScroll = null; }
  list.querySelectorAll('.sb-libitem').forEach(btn => {
    btn.addEventListener('click', () => {
      // "Change…" in the inspector armed S.replacing: this pick replaces that step and keeps its time.
      const replace = S.replacing != null && sbEv(S.replacing) ? S.replacing : undefined;
      S.replacing = null;
      S.libScroll = list.scrollTop;   // the list comes back scrolled here
      sbOpenComposer({ b: btn.dataset.b, c: btn.dataset.c }, replace);
    });
    btn.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData('text/plain', 'sbcmd:' + btn.dataset.b + '|' + btn.dataset.c);
      e.dataTransfer.effectAllowed = 'copy';
    });
  });
}

// New field values for a command: each param's default, an enum's first code, and the first id the boards have for a
// Maestro or WLED (so a pick starts out pointing at real hardware).
function sbDefaults(L) {
  const values = {};
  for (const p of L.cmd.params || []) {
    let v = p.default !== undefined ? String(p.default) : '';
    if (v === '' && p.enum) {
      const en = sbLib.enums[p.enum];
      if (en && en.values && en.values.length) v = String(en.values[0].code);
    }
    if (p.name === 'maestroId' || p.name === 'wledId') {
      const s = sbSuggest(L.board, p);
      if (s.length) v = s[0].v;
    }
    values[p.name] = v;
  }
  return values;
}
// Where a new step from this board goes: 'auto' for a WCB verb; for a device string, the port a board has under the
// device's label. _guessed marks a fallback the user should check.
function sbDefaultDest(L) {
  if (sbModeOf(L.board, L.cmd) !== 'device') return { dest: 'auto', guessed: false };
  if (L.cmd.defaultPort != null) return { dest: `W${S.ctx.self}S${L.cmd.defaultPort}`, guessed: false };   // e.g. DroidNet: S0
  const home = L.board.dest && parseInt(L.board.dest.target, 10);
  if (L.board.deliver === 'wcb' && home > 0) return { dest: 'W' + home, guessed: false };
  const want = (SB_DEVICE_LABELS[L.board.id] || L.board.labels || []).map(sbNorm);
  for (const w of sbBoards(S.ctx.self)) {
    for (let p = 1; p <= 5; p++) {
      const l = sbNorm(sbPortLabel(w, p));
      if (l && want.includes(l)) return { dest: `W${w}S${p}`, guessed: false };
    }
  }
  return { dest: `W${S.ctx.self}S1`, guessed: L.cmd.id !== 'wcb.serialText' };
}

function sbOpenComposer(ref, replaceId) {
  const L = sbLibCmd(ref);
  if (!L) return;
  let values, dest, guessed = false;
  const ev = replaceId != null ? sbEv(replaceId) : null;
  if (ev && ev.lib && ev.lib.b === ref.b && ev.lib.c === ref.c) {
    values = Object.assign({}, ev.values);
    dest = ev.dest;
  } else {
    values = sbDefaults(L);
    const d = sbDefaultDest(L);
    dest = d.dest; guessed = d.guessed;
    if (ev && sbLibCmd(ev.lib) && sbModeOf(sbLibCmd(ev.lib).board, sbLibCmd(ev.lib).cmd) === sbModeOf(L.board, L.cmd)) dest = ev.dest;
  }
  S.composer = { ref, values, dest, guessed, replaceId: replaceId != null ? replaceId : null };
  sbRenderComposer();
}

function sbComposerCmd() {
  const c = S.composer;
  const tmp = { lib: c.ref, values: c.values, dest: c.dest, cmd: '' };
  sbReencode(tmp);
  return tmp.cmd;
}

function sbRenderComposer() {
  const box = $('sb-lib');
  const c = S.composer;
  const L = sbLibCmd(c.ref);
  if (!L) { S.composer = null; return sbRenderLib(); }
  const mode = sbModeOf(L.board, L.cmd);
  const replacing = c.replaceId != null && sbEv(c.replaceId);
  box.innerHTML = `
    <div class="sb-comp ${sbColorClass(sbBoardSlot(L.board.id))}">
      <button type="button" class="btn btn-ghost btn-sm sb-back" id="sb-comp-back">← Back to the list</button>
      <div class="sb-comp-title">${esc(L.cmd.name)}</div>
      <div class="sb-comp-board"><i class="sb-sw"></i>${esc(L.board.name)}</div>
      <div class="sb-fields" id="sb-comp-fields">${sbFieldsHtml('sbc', L, c.values)}</div>
      <label class="sb-field" for="sb-comp-dest"><span>Send to</span><select class="sb-in sb-wide" id="sb-comp-dest">${sbDestOptions(mode, c.dest)}</select>
        <em class="sb-why" id="sb-comp-dest-why">${c.guessed ? 'No board has a port labelled for this device: pick the one it is wired to.' : ''}</em></label>
      ${/^;L/i.test(L.cmd.template || '') ? '<div class="sb-hint">WLED commands find their WLED by id: leave this on Auto.</div>' : ''}
      <div class="sb-comp-prev-label">Command <small>(drag it onto the timeline to place it)</small></div>
      <code class="sb-code sb-comp-prev" id="sb-comp-prev" draggable="true" title="Drag onto the timeline"></code>
      <div class="sb-comp-actions">
        <button type="button" class="btn btn-ghost btn-sm" id="sb-comp-cancel">Cancel</button>
        <button type="button" class="btn btn-primary btn-sm" id="sb-comp-add">${replacing ? 'Replace the step' : 'Add at ' + esc(sbFmt(S.playhead))}</button>
      </div>
    </div>`;
  const prev = () => { $('sb-comp-prev').textContent = sbComposerCmd(); };
  sbWireFields($('sb-comp-fields'), L, c.values, prev);
  $('sb-comp-dest').onchange = () => { c.dest = $('sb-comp-dest').value; c.guessed = false; $('sb-comp-dest-why').textContent = ''; prev(); };
  $('sb-comp-back').onclick = () => { S.composer = null; sbRenderLib(); };
  $('sb-comp-cancel').onclick = () => { S.composer = null; sbRenderLib(); };
  $('sb-comp-add').onclick = () => sbComposerCommit(S.playhead);
  $('sb-comp-prev').addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('text/plain', 'sbcomposer:');
    e.dataTransfer.effectAllowed = 'copy';
  });
  prev();
  const first = $('sb-comp-fields').querySelector('input,select');
  if (first) first.focus();
}

function sbComposerCommit(t) {
  const c = S.composer;
  if (!c) return;
  const L = sbLibCmd(c.ref);
  const bad = (L.cmd.params || []).find(p => sbParamProblem(p, c.values[p.name]));
  if (bad) { toast(`${sbParamLabel(bad)} must be ${sbParamProblem(bad, c.values[bad.name])}.`, 'error'); return; }
  const target = c.replaceId != null ? sbEv(c.replaceId) : null;
  sbCommit(() => {
    const ev = target || { id: S.nextId++, t: sbSnapT(t, true), cmd: '', conds: [], note: '' };
    ev.lib = Object.assign({}, c.ref);
    ev.values = Object.assign({}, c.values);
    ev.dest = c.dest;
    ev._guessed = c.guessed;
    sbReencode(ev);
    if (!target) S.events.push(ev);
    S.sel = new Set([ev.id]);
    S.selMarker = null;
  });
  // Back to the list as it was (search, open sections, scroll): the new step is selected, and the inspector below has
  // the same fields to finish it with.
  S.composer = null;
  sbRenderLib();
}

async function sbImportFile(e) {
  const file = e.target.files && e.target.files[0];
  e.target.value = '';
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    const n = sbMergeLibrary(sbLib, data, { custom: true });
    if (!n) throw new Error('no boards in it (expected a DroidNet board file)');
    const store = sbReadStore(SB_CUSTOM_KEY) || { enums: {}, boards: [] };
    if (data.enums) Object.assign(store.enums, data.enums);
    for (const b of (data.boards || data.components || [])) {
      if (!b || !b.id) continue;
      const i = store.boards.findIndex(x => x.id === b.id);
      if (i >= 0) store.boards[i] = b; else store.boards.push(b);
    }
    try { localStorage.setItem(SB_CUSTOM_KEY, JSON.stringify(store)); } catch (_) { toast('Imported for this session only: this browser would not store it.', 'warning'); }
    sbNormalizeLibrary(sbLib);
    toast(`Imported ${n} board${n === 1 ? '' : 's'} into the library.`, 'success');
    sbRenderLibList();
  } catch (err) {
    toast(`Import failed: ${err.message}`, 'error');
  }
}

async function sbFetchOnline() {
  const btn = $('sb-lib-fetch'), log = $('sb-lib-log');
  const url = ($('sb-lib-url').value || '').trim();
  if (!url) return;
  log.textContent = '';
  const line = (s) => { log.textContent += (log.textContent ? '\n' : '') + s; log.scrollTop = log.scrollHeight; };
  btn.disabled = true;
  try {
    await sbLoadManifest(url, line);
    sbRenderLibList();
    for (const ev of S.events) if (!ev.lib) sbDecodeInto(ev);
    sbRender();
  } catch (err) {
    line('✗ ' + err.message);
  } finally {
    btn.disabled = false;
  }
}

// ─── Inspector ───────────────────────────────────────────────────
// Redrawn after each change; the field that had focus gets it back.
function sbRenderInspector() {
  const box = $('sb-insp');
  if (!box) return;
  const a = document.activeElement;
  const fid = a && box.contains(a) && a.id ? a.id : null;
  sbRenderInspectorBody(box);
  if (fid && $(fid)) $(fid).focus();
}
function sbRenderInspectorBody(box) {
  const sel = [...S.sel].map(sbEv).filter(Boolean);
  if (S.selMarker != null) return sbInspectMarker(box);
  if (sel.length === 1) return sbInspectEvent(box, sel[0]);
  if (sel.length > 1) return sbInspectMany(box, sel);
  const ref = S.refs.find(r => r.id === S.selRef);
  if (ref) return sbInspectRef(box, ref);
  box.innerHTML = `
    <div class="sb-insp-row">
      <span class="sb-insp-title">Playhead ${esc(sbFmt(S.playhead))}</span>
      <span class="sb-muted">Select a step to edit it.</span>
      <span class="sb-spacer"></span>
      <label class="sb-field sb-inline"><span>Gap at the playhead</span>
        <input type="number" class="sb-in" id="sb-gap" min="1" step="1" value="500"><small>ms</small></label>
      <button type="button" class="btn btn-ghost btn-sm" id="sb-gap-ins" title="Push every step at or after the playhead later">Insert gap</button>
      <button type="button" class="btn btn-ghost btn-sm" id="sb-gap-del" title="Pull every step after the playhead earlier (not past the playhead)">Close gap</button>
      <button type="button" class="btn btn-ghost btn-sm" id="sb-sel-after" title="Select every step at or after the playhead">Select after playhead</button>
    </div>
    ${S.importWarnings.length ? `<ul class="sb-import-warn">${S.importWarnings.map(w => `<li class="lvl-${w.level}">${esc(w.text)}</li>`).join('')}</ul>` : ''}`;
  const gap = () => Math.max(1, parseInt($('sb-gap').value, 10) || 0);
  $('sb-gap-ins').onclick = () => sbCommit(() => {
    const g = gap();
    for (const e of S.events) if (e.t >= S.playhead) e.t += g;
    for (const m of S.markers) if (m.t >= S.playhead) m.t += g;
  });
  $('sb-gap-del').onclick = () => sbCommit(() => {
    const g = gap(), ph = S.playhead;
    const shift = (t) => (t > ph ? Math.max(ph, t - g) : t);
    for (const e of S.events) e.t = shift(e.t);
    for (const m of S.markers) m.t = shift(m.t);
  });
  $('sb-sel-after').onclick = () => {
    S.sel = new Set(S.events.filter(e => e.t >= S.playhead).map(e => e.id));
    sbRender();
  };
}

function sbInspectEvent(box, ev) {
  const L = sbLibCmd(ev.lib);
  const sorted = S.events.slice().sort((a, b) => a.t - b.t);
  const prevT = sorted.filter(e => e.t < ev.t).reduce((m, e) => Math.max(m, e.t), -1);
  const siblings = S.events.filter(e => e.t === ev.t);
  const idx = siblings.indexOf(ev);
  const mode = L ? sbModeOf(L.board, L.cmd) : null;
  box.innerHTML = `
    <div class="sb-insp-row">
      <label class="sb-field sb-inline"><span>Time</span><input type="number" class="sb-in sb-time" id="sbi-t" min="0" step="1" value="${ev.t}"><small>ms</small></label>
      <span class="sb-nudge">${[-100, -10, -1, 1, 10, 100].map(d => `<button type="button" class="btn btn-ghost btn-sm" data-nudge="${d}">${d > 0 ? '+' : '−'}${Math.abs(d)}</button>`).join('')}</span>
      <span class="sb-muted">${esc(sbFmt(ev.t))}${prevT >= 0 ? ` · ${ev.t - prevT} ms after the step before` : ev.t ? ' · the first step' : ' · at the start'}</span>
      ${siblings.length > 1 ? `<span class="sb-order">order at this time ${idx + 1}/${siblings.length}
        <button type="button" class="btn btn-ghost btn-sm" id="sbi-up"${idx === 0 ? ' disabled' : ''} title="Run it earlier at this time">↑</button>
        <button type="button" class="btn btn-ghost btn-sm" id="sbi-down"${idx === siblings.length - 1 ? ' disabled' : ''} title="Run it later at this time">↓</button></span>` : ''}
      <span class="sb-spacer"></span>
      <button type="button" class="btn btn-ghost btn-sm" id="sbi-dup" title="Duplicate (Ctrl+D)">Duplicate</button>
      <button type="button" class="btn btn-danger btn-sm" id="sbi-del" title="Remove (Del)">Remove</button>
    </div>
    <div class="sb-insp-row sb-insp-cmd">
      ${L ? `<span class="sb-insp-title" title="${esc(L.board.name)}">📚 ${esc(L.board.name)} · ${esc(L.cmd.name)}</span>
             <button type="button" class="btn btn-ghost btn-sm" id="sbi-change" title="Pick a different command for this step, keeping its time">Change…</button>`
          : '<span class="sb-insp-title">Custom command</span>'}
    </div>
    <div class="sb-fields" id="sbi-fields">
      ${L ? sbFieldsHtml('sbi', L, ev.values) : ''}
      ${L ? `<label class="sb-field" for="sbi-dest"><span>Send to</span><select class="sb-in sb-wide" id="sbi-dest">${sbDestOptions(mode, ev.dest)}</select>
              <em class="sb-why">${ev._guessed ? 'No board has a port labelled for this device: pick the one it is wired to.' : ''}</em></label>` : ''}
      <label class="sb-field" for="sbi-if"><span>IF (optional)</span><input type="text" class="sb-in sb-wide" id="sbi-if" value="${esc(ev.conds.join(',AND,'))}" placeholder="e.g. mode=2" spellcheck="false" autocomplete="off">
        <em class="sb-why">Checked when the sequence starts.</em></label>
      <label class="sb-field" for="sbi-note"><span>Note (optional)</span><input type="text" class="sb-in sb-wide" id="sbi-note" value="${esc(ev.note)}" spellcheck="false"><em class="sb-why"></em></label>
    </div>
    <label class="sb-field sb-cmdtext" for="sbi-cmd"><span>Command text</span>
      <input type="text" class="sb-in sb-mono" id="sbi-cmd" value="${esc(ev.cmd)}" spellcheck="false" autocomplete="off" placeholder="e.g. ;S3:PP100">
      <em class="sb-why">${L ? 'Edit the fields above, or the text itself.' : 'Type any WCB command.'}</em></label>`;

  const tIn = $('sbi-t');
  tIn.addEventListener('change', () => sbCommit(() => { ev.t = Math.max(0, Math.round(+tIn.value || 0)); }));
  box.querySelectorAll('[data-nudge]').forEach(b => { b.onclick = () => sbNudge(+b.dataset.nudge); });
  if ($('sbi-up')) $('sbi-up').onclick = () => sbReorder(ev, -1);
  if ($('sbi-down')) $('sbi-down').onclick = () => sbReorder(ev, 1);
  $('sbi-dup').onclick = () => sbDuplicate();
  $('sbi-del').onclick = () => sbDelete();
  if ($('sbi-change')) $('sbi-change').onclick = () => {
    S.composer = null;
    sbRenderLib();
    const s = $('sb-lib-search');
    if (s) s.focus();
    toast('Pick the new command in the library: it replaces this step and keeps its time.', 'info', 5000);
    S.replacing = ev.id;
  };
  const afterEdit = () => { sbEditTouch(); sbRenderTimeline(); sbRenderOut(); };
  if (L) {
    sbWireFields($('sbi-fields'), L, ev.values, () => { sbReencode(ev); $('sbi-cmd').value = ev.cmd; afterEdit(); });
    $('sbi-dest').onchange = () => {
      sbEditTouch();
      ev.dest = $('sbi-dest').value; ev._guessed = false;
      sbReencode(ev);
      sbRender();
    };
  }
  $('sbi-if').addEventListener('input', () => {
    const s = $('sbi-if').value.trim();
    ev.conds = s ? [s] : [];
    afterEdit();
  });
  $('sbi-note').addEventListener('input', () => { ev.note = $('sbi-note').value; afterEdit(); });
  const cmdIn = $('sbi-cmd');
  cmdIn.addEventListener('input', () => { ev.cmd = cmdIn.value; afterEdit(); });
  cmdIn.addEventListener('change', () => {
    const before = JSON.stringify(ev.lib);
    sbDecodeInto(ev);
    if (JSON.stringify(ev.lib) !== before) sbRender(); else sbRenderTimeline();
  });
}

function sbInspectMany(box, sel) {
  const t0 = Math.min(...sel.map(e => e.t)), t1 = Math.max(...sel.map(e => e.t));
  box.innerHTML = `
    <div class="sb-insp-row">
      <span class="sb-insp-title">${sel.length} steps</span>
      <span class="sb-muted">${esc(sbFmt(t0))} to ${esc(sbFmt(t1))}</span>
      <label class="sb-field sb-inline"><span>Move by</span><input type="number" class="sb-in" id="sbm-by" step="1" value="100"><small>ms</small></label>
      <button type="button" class="btn btn-ghost btn-sm" id="sbm-later">Later</button>
      <button type="button" class="btn btn-ghost btn-sm" id="sbm-earlier">Earlier</button>
      <button type="button" class="btn btn-ghost btn-sm" id="sbm-align" title="Move them together so the first one is at the playhead">First one to the playhead</button>
      <span class="sb-spacer"></span>
      <button type="button" class="btn btn-ghost btn-sm" id="sbm-dup">Duplicate</button>
      <button type="button" class="btn btn-danger btn-sm" id="sbm-del">Remove</button>
    </div>`;
  const by = () => parseInt($('sbm-by').value, 10) || 0;
  $('sbm-later').onclick = () => sbShiftSel(by());
  $('sbm-earlier').onclick = () => sbShiftSel(-by());
  $('sbm-align').onclick = () => sbShiftSel(S.playhead - t0);
  $('sbm-dup').onclick = () => sbDuplicate();
  $('sbm-del').onclick = () => sbDelete();
}

function sbInspectRef(box, r) {
  const start = sbRefStart(r);
  const anchor = sbRefAnchor(r);
  const end = r.events.reduce((m, e) => Math.max(m, e.t), 0);
  const how = r.offset != null ? 'moved by hand'
    : anchor ? `started by this sequence's step at ${sbFmt(anchor.t)}` : 'starts with this sequence';
  box.innerHTML = `
    <div class="sb-insp-row">
      <span class="sb-insp-title">⇆ ${esc(r.key)}</span>
      <span class="sb-muted">${esc(r.wcb === S.ctx.self ? 'this board' : sbBoardName(r.wcb))} · ${r.events.length} cmd${r.events.length === 1 ? '' : 's'}${end ? ' · ' + esc(sbFmt(end)) + ' long' : ''}</span>
      <label class="sb-field sb-inline"><span>Starts at</span><input type="number" class="sb-in sb-time" id="sbr-t" min="0" step="1" value="${start}"><small>ms</small></label>
      <span class="sb-muted">${esc(how)}</span>
      ${anchor && r.offset != null ? '<button type="button" class="btn btn-ghost btn-sm" id="sbr-auto" title="Follow the step that runs it again">Start with its step</button>' : ''}
      <button type="button" class="btn btn-ghost btn-sm" id="sbr-ph">Start at the playhead</button>
      <span class="sb-spacer"></span>
      <button type="button" class="btn btn-danger btn-sm" id="sbr-del">Stop showing it</button>
    </div>
    <div class="sb-muted sb-pad">Shown for timing only: nothing here changes "${esc(r.key)}". While it is selected its steps' times run through every lane, and a step you drag sticks to them.</div>`;
  const set = (v) => { r.offset = v; sbRefsSave(); sbRender(); };
  $('sbr-t').addEventListener('change', () => set(Math.max(0, Math.round(+$('sbr-t').value || 0))));
  if ($('sbr-auto')) $('sbr-auto').onclick = () => set(null);
  $('sbr-ph').onclick = () => set(S.playhead);
  $('sbr-del').onclick = () => sbRemoveRef(r.id);
}

function sbInspectMarker(box) {
  const m = S.markers.find(x => x.id === S.selMarker);
  if (!m) { S.selMarker = null; return sbRenderInspectorBody(box); }
  box.innerHTML = `
    <div class="sb-insp-row">
      <span class="sb-insp-title">Note</span>
      <label class="sb-field sb-inline"><span>Time</span><input type="number" class="sb-in sb-time" id="sbk-t" min="0" step="1" value="${m.t}"><small>ms</small></label>
      <label class="sb-field sb-inline sb-grow"><span>Text</span><input type="text" class="sb-in sb-wide" id="sbk-text" value="${esc(m.text)}" spellcheck="false"></label>
      <button type="button" class="btn btn-danger btn-sm" id="sbk-del">Remove</button>
    </div>
    <div class="sb-muted sb-pad">Stored in the sequence as a <code>***</code> comment; the board skips it.</div>`;
  $('sbk-t').addEventListener('change', () => sbCommit(() => { m.t = Math.max(0, Math.round(+$('sbk-t').value || 0)); m.after = false; }));
  $('sbk-text').addEventListener('input', () => { m.text = $('sbk-text').value; sbEditTouch(); sbRenderTimeline(); sbRenderOut(); });
  $('sbk-del').onclick = () => sbCommit(() => { S.markers = S.markers.filter(x => x.id !== m.id); S.selMarker = null; });
}

// ─── Output ──────────────────────────────────────────────────────
function sbRenderOut() {
  const { value, list } = sbProblems();
  const ch = S.ctx.chars;
  const toks = value ? value.split(ch.delim) : [];
  $('sb-out').innerHTML = toks.length
    ? toks.map(t => {
        const cls = sbTimerToken(t, ch) ? 'tk-delay' : sbIsIf(t) ? 'tk-if' : t.startsWith('***') ? 'tk-note' : '';
        const ci = cls ? -1 : t.indexOf('***');
        const body = ci >= 0 ? esc(t.slice(0, ci)) + `<span class="tk-note">${esc(t.slice(ci))}</span>` : esc(t);
        return cls ? `<span class="${cls}">${body}</span>` : body;
      }).join(`<span class="tk-delim">${esc(ch.delim)}</span>`)
    : '<span class="sb-muted">empty</span>';
  const times = new Set(S.events.map(e => e.t));
  const end = S.events.reduce((m, e) => Math.max(m, e.t), 0);
  $('sb-stats').textContent = S.events.length
    ? `${S.events.length} command${S.events.length === 1 ? '' : 's'} · ${times.size} moment${times.size === 1 ? '' : 's'} · ${sbFmt(end)}`
    : '';
  const len = value.length;
  const lenEl = $('sb-len');
  lenEl.textContent = `${len.toLocaleString('en-US')} / ${SB_VALUE_MAX.toLocaleString('en-US')} characters`;
  lenEl.className = len > SB_VALUE_MAX ? 'sb-len-over' : len > SB_VALUE_HEAP_WARN ? 'sb-len-near' : '';
  const keyWhy = S.key === S.baseKey && !S.isNew ? '' : sbKeyProblem(S.key);
  const all = keyWhy ? [{ level: 'error', ids: [], text: keyWhy }].concat(list) : list;
  $('sb-warn').innerHTML = all.map(w => `<li class="lvl-${w.level}"${w.ids && w.ids.length ? ` data-ids="${w.ids.join(',')}" title="Click to select"` : ''}>${esc(w.text)}</li>`).join('');
  const err = all.find(w => w.level === 'error');
  $('sb-apply').disabled = !!err || !value;
  $('sb-apply').title = err ? err.text : !value ? 'Add a command first' : 'Write this into the sequence row; SAVE/UPDATE or a push sends it to the board';
  const connected = !!(app.conns()[S.n] && app.conns()[S.n].isConnected && app.conns()[S.n].isConnected()) || app.relays()[S.n] !== undefined;
  $('sb-savetest').disabled = !!err || !value || !connected;
  $('sb-savetest').title = !connected ? 'Connect this board to save and test from here' : 'Apply, save it to the board and run it there (the board\'s TEST)';
}

// ─── Timeline interaction ────────────────────────────────────────
function sbTimeAt(clientX) {
  const inner = $('sb-tl-inner').getBoundingClientRect();
  return (clientX - inner.left - SB_GUTTER) * 1000 / S.pps;
}
function sbSetPlayhead(t, scroll) {
  S.playhead = Math.max(0, Math.round(t));
  sbPlacePlayhead();
  if (scroll) sbScrollTo(S.playhead);
  if (!S.sel.size && S.selMarker == null) sbRenderInspector();
  if (S.composer && S.composer.replaceId == null && $('sb-comp-add')) $('sb-comp-add').textContent = 'Add at ' + sbFmt(S.playhead);
}
function sbScrollTo(t) {
  const tl = $('sb-tl');
  const x = SB_GUTTER + t * S.pps / 1000;
  if (x < tl.scrollLeft + SB_GUTTER + 20 || x > tl.scrollLeft + tl.clientWidth - 40) tl.scrollLeft = Math.max(0, x - SB_GUTTER - 80);
}
function sbZoom(factor, clientX) {
  const tl = $('sb-tl');
  const rect = tl.getBoundingClientRect();
  const fx = clientX != null ? clientX - rect.left : SB_GUTTER + (tl.clientWidth - SB_GUTTER) / 2;
  const tAt = (tl.scrollLeft + fx - SB_GUTTER) * 1000 / S.pps;
  S.pps = Math.min(SB_PPS_MAX, Math.max(SB_PPS_MIN, S.pps * factor));
  sbRenderTimeline();
  tl.scrollLeft = Math.max(0, SB_GUTTER + tAt * S.pps / 1000 - fx);
}
function sbFit() {
  const tl = $('sb-tl');
  const end = Math.max(sbEndT(), 1000);
  S.pps = Math.min(SB_PPS_MAX, Math.max(SB_PPS_MIN, (tl.clientWidth - SB_GUTTER - 220) * 1000 / end));
  sbRenderTimeline();
  tl.scrollLeft = 0;
}

function sbOnPointerDown(e) {
  if (e.button !== 0) return;
  const refX = e.target.closest('[data-refx]');
  if (refX) { e.preventDefault(); return sbRemoveRef(refX.dataset.refx); }
  const refSel = e.target.closest('[data-refsel]');
  if (refSel) return sbSelectRef(refSel.dataset.refsel);
  const refTrack = e.target.closest('[data-reftrack]');
  if (refTrack) return sbDragRef(e, refTrack.dataset.reftrack);
  const pill = e.target.closest('.sb-ev');
  const mk = e.target.closest('.sb-mk');
  const onLabel = !!e.target.closest('.sb-lane-label, .sb-corner');
  if (pill) return sbDragSteps(e, +pill.dataset.ev);
  if (mk) return sbDragMarker(e, +mk.dataset.mk);
  if (onLabel || !e.target.closest('#sb-tl-inner')) return;
  // The ruler or an empty spot: move the playhead, and scrub while the button is held.
  if (!e.shiftKey) { S.sel.clear(); S.selMarker = null; S.selRef = null; }
  const tl = $('sb-tl');
  const move = (ev) => sbSetPlayhead(sbPlace(sbTimeAt(ev.clientX), ev.altKey, { playhead: false }).t);
  move(e);
  sbRender({ insp: true });
  tl.setPointerCapture(e.pointerId);
  const up = () => {
    tl.removeEventListener('pointermove', move);
    tl.removeEventListener('pointerup', up);
    tl.removeEventListener('pointercancel', up);
  };
  tl.addEventListener('pointermove', move);
  tl.addEventListener('pointerup', up);
  tl.addEventListener('pointercancel', up);
}

function sbDragSteps(e, id) {
  const ev = sbEv(id);
  if (!ev) return;
  S.selMarker = null;
  if (e.shiftKey || e.ctrlKey || e.metaKey) {
    if (S.sel.has(id)) S.sel.delete(id); else S.sel.add(id);
    sbRender();
    return;
  }
  if (!S.sel.has(id)) { S.sel = new Set([id]); }
  S.selRef = null;
  const moving = [...S.sel].map(sbEv).filter(Boolean);
  const movingIds = new Set(moving.map(m => m.id));
  const orig = new Map(moving.map(m => [m.id, m.t]));
  const minOrig = Math.min(...orig.values());
  const startX = e.clientX;
  const tl = $('sb-tl');
  let moved = false, snap = null;
  tl.setPointerCapture(e.pointerId);
  const tip = $('sb-tip');
  const onMove = (pe) => {
    const dx = pe.clientX - startX;
    if (!moved && Math.abs(dx) < 4) return;
    if (!moved) { moved = true; snap = sbSnapshot(); }
    const pl = sbPlace(orig.get(id) + dx * 1000 / S.pps, pe.altKey, { exclude: movingIds });
    sbGuide(pl.to);
    const want = pl.t;
    const delta = Math.max(-minOrig, want - orig.get(id));
    for (const m of moving) {
      m.t = orig.get(m.id) + delta;
      const el = document.querySelector(`.sb-ev[data-ev="${m.id}"]`);
      if (el) el.style.left = (m.t * S.pps / 1000) + 'px';
    }
    tip.hidden = false;
    tip.textContent = `${sbFmt(ev.t)}  (${delta >= 0 ? '+' : '−'}${Math.abs(delta)} ms)`;
    const inner = $('sb-tl-inner').getBoundingClientRect();
    tip.style.left = (pe.clientX - inner.left + 12) + 'px';
    tip.style.top = (pe.clientY - inner.top - 28) + 'px';
    sbPlacePlayhead();
  };
  const onUp = () => {
    tl.removeEventListener('pointermove', onMove);
    tl.removeEventListener('pointerup', onUp);
    tl.removeEventListener('pointercancel', onUp);
    tip.hidden = true;
    sbGuide(null);
    if (moved && moving.some(m => m.t !== orig.get(m.id))) {
      S.undo.push(snap);
      S.redo = [];
      S.dirty = true;
    }
    sbRender();
  };
  tl.addEventListener('pointermove', onMove);
  tl.addEventListener('pointerup', onUp);
  tl.addEventListener('pointercancel', onUp);
  sbRender();
}

function sbDragMarker(e, id) {
  const m = S.markers.find(x => x.id === id);
  if (!m) return;
  S.sel.clear();
  S.selMarker = id;
  const t0 = m.t, startX = e.clientX, tl = $('sb-tl');
  let moved = false, snap = null;
  tl.setPointerCapture(e.pointerId);
  const onMove = (pe) => {
    const dx = pe.clientX - startX;
    if (!moved && Math.abs(dx) < 4) return;
    if (!moved) { moved = true; snap = sbSnapshot(); }
    m.t = sbSnapT(t0 + dx * 1000 / S.pps, pe.altKey);
    m.after = false;
    const el = document.querySelector(`.sb-mk[data-mk="${id}"]`);
    if (el) el.style.left = (m.t * S.pps / 1000) + 'px';
  };
  const onUp = () => {
    tl.removeEventListener('pointermove', onMove);
    tl.removeEventListener('pointerup', onUp);
    tl.removeEventListener('pointercancel', onUp);
    if (moved && m.t !== t0) { S.undo.push(snap); S.redo = []; S.dirty = true; }
    sbRender();
  };
  tl.addEventListener('pointermove', onMove);
  tl.addEventListener('pointerup', onUp);
  tl.addEventListener('pointercancel', onUp);
  sbRender();
}

function sbOnDragOver(e) {
  const types = e.dataTransfer && e.dataTransfer.types;
  if (!types || ![...types].includes('text/plain')) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
  const t = sbPlace(sbTimeAt(e.clientX), e.altKey).t;
  const line = $('sb-dropline');
  line.hidden = false;
  line.style.left = (SB_GUTTER + t * S.pps / 1000) + 'px';
  line.dataset.t = sbFmt(t);
}
function sbOnDrop(e) {
  const data = (e.dataTransfer && e.dataTransfer.getData('text/plain')) || '';
  $('sb-dropline').hidden = true;
  if (!data.startsWith('sbcmd:') && !data.startsWith('sbcomposer:')) return;
  e.preventDefault();
  const t = sbPlace(sbTimeAt(e.clientX), e.altKey).t;
  if (data.startsWith('sbcomposer:')) { sbComposerCommit(t); return; }
  const [b, c] = data.slice(6).split('|');
  const L = sbLibCmd({ b, c });
  if (!L) return;
  const d = sbDefaultDest(L);
  sbCommit(() => {
    const ev = { id: S.nextId++, t, cmd: '', conds: [], note: '', lib: { b, c }, values: sbDefaults(L), dest: d.dest, _guessed: d.guessed };
    sbReencode(ev);
    S.events.push(ev);
    S.sel = new Set([ev.id]);
    S.selMarker = null;
  });
  const first = $('sbi-fields') && $('sbi-fields').querySelector('input,select');
  if (first) first.focus();
}

// ─── Reference tracks: picking, moving, removing ─────────────────
function sbRefPick(show) {
  const box = $('sb-refpick');
  if (!box) return;
  box.hidden = !show;
  $('sb-ref-add').setAttribute('aria-expanded', String(!!show));
  if (!show) return;
  $('sb-refpick-q').value = '';
  sbRenderRefPick();
  $('sb-refpick-q').focus();
}
// Every other sequence the loaded boards hold, by board; the one being edited is left out.
function sbRenderRefPick() {
  const q = ($('sb-refpick-q').value || '').trim().toLowerCase();
  const have = new Set(S.refs.map(r => `${r.wcb}/${r.key}`));
  const byBoard = new Map();
  for (const s of sbAllSequences()) {
    if (s.wcb === S.ctx.self && s.key === S.baseKey && !S.isNew) continue;
    if (q && !s.key.toLowerCase().includes(q) && !s.value.toLowerCase().includes(q)) continue;
    if (!byBoard.has(s.wcb)) byBoard.set(s.wcb, []);
    byBoard.get(s.wcb).push(s);
  }
  let html = '';
  for (const w of [...byBoard.keys()].sort((a, b) => (a === S.ctx.self ? -1 : b === S.ctx.self ? 1 : a - b))) {
    html += `<div class="sb-refpick-board">${esc(w === S.ctx.self ? 'This board · ' + sbBoardName(w) : sbBoardName(w))}</div>`;
    for (const s of byBoard.get(w)) {
      const p = sbParse(s.value, sbCtx(s.slot).chars);
      const end = p.events.reduce((m, e) => Math.max(m, e.t), 0);
      const meta = `${p.events.length} cmd${p.events.length === 1 ? '' : 's'}${end ? ' · ' + sbFmt(end) : ''}`;
      const added = have.has(`${s.wcb}/${s.key}`);
      html += `<button type="button" class="sb-refpick-item" data-refadd="${esc(s.wcb + '/' + s.key)}"${added ? ' disabled' : ''}>`
        + `<span>${esc(s.key)}</span><small>${added ? 'shown' : esc(meta)}</small></button>`;
    }
  }
  $('sb-refpick-list').innerHTML = html || '<div class="sb-refpick-none">No other sequences on the loaded boards.</div>';
}
function sbAddRef(wcb, key) {
  if (S.refs.some(r => r.wcb === wcb && r.key === key)) return;
  const r = sbMakeRef({ wcb, key });
  S.refs.push(r);
  for (const e of r.events) sbColorOf(S.colors, e.lane.key, null, sbBoardSlot(e.lane.board));
  S.selRef = r.id;
  S.sel.clear(); S.selMarker = null;
  sbRefPick(false);
  sbRefsSave();
  sbRender();
}
function sbRemoveRef(id) {
  S.refs = S.refs.filter(r => r.id !== id);
  if (S.selRef === id) S.selRef = null;
  sbRefsSave();
  sbRender();
}
function sbSelectRef(id) {
  S.selRef = S.selRef === id ? null : id;
  S.sel.clear(); S.selMarker = null;
  sbRender();
}
// Drag a reference's line to move it in time; it sticks to this sequence's steps, notes and the playhead. A click
// without moving selects it, and on one of its steps also puts the playhead there.
function sbDragRef(e, id) {
  const r = S.refs.find(x => x.id === id);
  if (!r) return;
  const start0 = sbRefStart(r), startX = e.clientX, tl = $('sb-tl');
  const pill = e.target.closest('.sb-rev');
  let moved = false;
  tl.setPointerCapture(e.pointerId);
  const onMove = (pe) => {
    const dx = pe.clientX - startX;
    if (!moved && Math.abs(dx) < 4) return;
    moved = true;
    // Stick the reference's step under the pointer (or its start) to what this sequence has there.
    const ev = pill ? r.events[+pill.dataset.rev.split(':')[1]] : null;
    const lead = ev ? ev.t : 0;
    const pl = sbPlace(start0 + lead + dx * 1000 / S.pps, pe.altKey, { refId: r.id });
    sbGuide(pl.to);
    r.offset = Math.max(0, pl.t - lead);
    S.selRef = r.id;
    sbRenderTimeline();
  };
  const onUp = () => {
    tl.removeEventListener('pointermove', onMove);
    tl.removeEventListener('pointerup', onUp);
    tl.removeEventListener('pointercancel', onUp);
    sbGuide(null);
    if (moved) { sbRefsSave(); sbRender(); return; }
    S.selRef = r.id;
    S.sel.clear(); S.selMarker = null;
    if (pill) sbSetPlayhead(start0 + r.events[+pill.dataset.rev.split(':')[1]].t);
    sbRender();
  };
  tl.addEventListener('pointermove', onMove);
  tl.addEventListener('pointerup', onUp);
  tl.addEventListener('pointercancel', onUp);
}

// ─── Edits ───────────────────────────────────────────────────────
function sbNudge(ms) {
  if (S.selMarker != null) {
    const m = S.markers.find(x => x.id === S.selMarker);
    if (m) sbCommit(() => { m.t = Math.max(0, m.t + ms); m.after = false; });
    return;
  }
  sbShiftSel(ms);
}
function sbShiftSel(ms) {
  const sel = [...S.sel].map(sbEv).filter(Boolean);
  if (!sel.length || !ms) return;
  const d = Math.max(ms, -Math.min(...sel.map(e => e.t)));
  if (!d) return;
  sbCommit(() => { for (const e of sel) e.t += d; });
}
function sbReorder(ev, dir) {
  const sib = S.events.filter(e => e.t === ev.t);
  const j = sib.indexOf(ev) + dir;
  if (j < 0 || j >= sib.length) return;
  sbCommit(() => {
    const a = S.events.indexOf(ev), b = S.events.indexOf(sib[j]);
    [S.events[a], S.events[b]] = [S.events[b], S.events[a]];
  });
}
function sbClone(e, t) {
  return { id: S.nextId++, t, cmd: e.cmd, conds: e.conds.slice(), note: e.note,
           lib: e.lib ? Object.assign({}, e.lib) : null, values: Object.assign({}, e.values), dest: e.dest };
}
function sbDuplicate() {
  const sel = [...S.sel].map(sbEv).filter(Boolean);
  if (!sel.length) return;
  const t0 = Math.min(...sel.map(e => e.t)), t1 = Math.max(...sel.map(e => e.t));
  const off = Math.max(S.snap || 100, t1 - t0 + (S.snap || 100));
  sbCommit(() => {
    const copies = sel.map(e => sbClone(e, e.t + off));
    S.events.push(...copies);
    S.sel = new Set(copies.map(c => c.id));
  });
}
function sbDelete() {
  if (!S.sel.size && S.selMarker == null && S.selRef) return sbRemoveRef(S.selRef);
  if (S.selMarker != null) {
    sbCommit(() => { S.markers = S.markers.filter(m => m.id !== S.selMarker); S.selMarker = null; });
    return;
  }
  if (!S.sel.size) return;
  sbCommit(() => { S.events = S.events.filter(e => !S.sel.has(e.id)); S.sel.clear(); });
}
function sbAddCustom() {
  sbCommit(() => {
    const ev = { id: S.nextId++, t: S.playhead, cmd: '', conds: [], note: '', lib: null, values: {}, dest: 'auto' };
    S.events.push(ev);
    S.sel = new Set([ev.id]);
    S.selMarker = null;
  });
  if ($('sbi-cmd')) $('sbi-cmd').focus();
}
function sbAddMarker() {
  let id;
  sbCommit(() => {
    id = S.nextId++;
    S.markers.push({ id, t: S.playhead, text: 'Note' });
    S.sel.clear();
    S.selMarker = id;
  });
  if ($('sbk-text')) { $('sbk-text').focus(); $('sbk-text').select(); }
}

function sbOnKey(e) {
  const tag = (e.target && e.target.tagName) || '';
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(tag);
  const mod = e.ctrlKey || e.metaKey;
  if ((tag === 'BUTTON' || tag === 'SUMMARY') && (e.key === ' ' || e.key === 'Enter')) return;   // the control's own
  if (e.key === 'Escape') {
    if (typing) { e.target.blur(); return; }
    if (!$('sb-refpick').hidden) { sbRefPick(false); return; }
    if (S.composer) { S.composer = null; sbRenderLib(); return; }
    if (S.sel.size || S.selMarker != null || S.selRef) { S.sel.clear(); S.selMarker = null; S.selRef = null; sbRender(); return; }
    return;
  }
  if (typing) return;
  if (mod && (e.key === 'z' || e.key === 'Z')) { e.preventDefault(); if (e.shiftKey) sbRedo(); else sbUndo(); return; }
  if (mod && (e.key === 'y' || e.key === 'Y')) { e.preventDefault(); sbRedo(); return; }
  if (mod && (e.key === 'd' || e.key === 'D')) { e.preventDefault(); sbDuplicate(); return; }
  if (mod && (e.key === 'a' || e.key === 'A')) { e.preventDefault(); S.sel = new Set(S.events.map(x => x.id)); S.selMarker = null; sbRender(); return; }
  if (mod && (e.key === 'c' || e.key === 'C')) {
    const sel = [...S.sel].map(sbEv).filter(Boolean);
    if (!sel.length) return;
    const t0 = Math.min(...sel.map(x => x.t));
    S.clip = sel.map(x => Object.assign(sbClone(x, x.t - t0), { id: 0 }));
    toast(`Copied ${sel.length} step${sel.length === 1 ? '' : 's'}`, 'info', 1500);
    return;
  }
  if (mod && (e.key === 'v' || e.key === 'V')) {
    if (!S.clip || !S.clip.length) return;
    e.preventDefault();
    sbCommit(() => {
      const copies = S.clip.map(c => { const n = sbClone(c, S.playhead + c.t); sbDecodeInto(n); return n; });
      S.events.push(...copies);
      S.sel = new Set(copies.map(c => c.id));
      S.selMarker = null;
    });
    return;
  }
  if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); sbDelete(); return; }
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
    e.preventDefault();
    const step = e.altKey ? 1 : e.shiftKey ? 100 : 10;
    const ms = e.key === 'ArrowLeft' ? -step : step;
    if (S.sel.size || S.selMarker != null) sbNudge(ms);
    else sbSetPlayhead(S.playhead + ms, true);
    return;
  }
  if (e.key === ' ') { e.preventDefault(); sbTogglePlay(); return; }
  if (e.key === 'Home') { e.preventDefault(); sbSetPlayhead(0, true); return; }
  if (e.key === '+' || e.key === '=') { sbZoom(1.5); return; }
  if (e.key === '-' || e.key === '_') { sbZoom(1 / 1.5); return; }
  if (e.key === 'f' || e.key === 'F') { sbFit(); return; }
}

// ─── Preview ─────────────────────────────────────────────────────
// The playhead runs in real time and each step flashes as it passes: the timing as the board will run it, give or take
// the ~1 ms per command the board takes to queue a group (processCommandGroups yields between them).
function sbTogglePlay() {
  if (S.play) return sbStop();
  const end = sbEndT();
  if (!S.events.length) return;
  const t0 = S.playhead >= end ? 0 : S.playhead;
  // Every step of this sequence and of its reference tracks, at its time on the timeline.
  const cues = S.events.map(ev => ({ k: 'e' + ev.id, t: ev.t, sel: `.sb-ev[data-ev="${ev.id}"]`, label: () => ev._label || sbEvLabel(ev) }));
  for (const r of S.refs) {
    const s = sbRefStart(r);
    for (const e of r.events) cues.push({ k: `r${r.id}:${e.i}`, t: s + e.t, sel: `.sb-rev[data-rev="${r.id}:${e.i}"]`, label: () => `${r.key}: ${e.label}` });
  }
  S.play = { t0, start: performance.now(), end: end + 400, fired: new Set(cues.filter(c => c.t < t0).map(c => c.k)) };
  $('sb-play').textContent = '■ Stop';
  const tick = () => {
    if (!S.play) return;
    const now = S.play.t0 + (performance.now() - S.play.start);
    S.playhead = Math.round(now);
    sbPlacePlayhead();
    const tl = $('sb-tl');
    const x = SB_GUTTER + now * S.pps / 1000;
    if (x > tl.scrollLeft + tl.clientWidth - 60) tl.scrollLeft = x - SB_GUTTER - 60;
    for (const c of cues) {
      if (c.t > now || S.play.fired.has(c.k)) continue;
      S.play.fired.add(c.k);
      const el = document.querySelector(c.sel);
      if (el) { el.classList.remove('firing'); void el.offsetWidth; el.classList.add('firing'); }
      $('sb-now').textContent = '▸ ' + c.label();
    }
    if (now >= S.play.end) { sbStop(); return; }
    S.play.raf = requestAnimationFrame(tick);
  };
  S.play.raf = requestAnimationFrame(tick);
}
function sbStop() {
  if (S.play && S.play.raf) cancelAnimationFrame(S.play.raf);
  S.play = null;
  if ($('sb-play')) $('sb-play').textContent = '▶ Preview';
  if ($('sb-now')) $('sb-now').textContent = '';
}

// ─── The timing strip under each sequence row ────────────────────
// A small picture of when each command fires, coloured by device: the graphical view of every sequence in the table.
// Click it to open the builder.
const sbThumbTimers = {};
function renderRowThumb(n, rowId) {
  const row = rowId && $(rowId);
  const el = row && $(rowId + '-thumb');
  if (!el) return;
  const ctx = sbCtx(n);
  const value = sbRowValue(n, row);
  const p = sbParse(value, ctx.chars);
  if (!p.events.length) { el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  if (p.valueVerb) {
    el.innerHTML = '<span class="seq-thumb-meta">stores a value: edit as text</span>';
    el.title = '';
    return;
  }
  const end = p.events.reduce((m, e) => Math.max(m, e.t), 0);
  // One colour per lane, by first appearance (sbSeedColors does the same, so the timeline opens in these colours);
  // commands at one moment stack, each a part of the tick, so none hides another.
  const colors = sbColorMap();
  const byT = new Map();
  for (const e of p.events) {
    const ln = sbLaneOf(e.cmd, ctx, sbDevOfCmd(e.cmd, ctx.chars));
    const slot = sbColorOf(colors, ln.key, null, sbBoardSlot(ln.board));
    if (!byT.has(e.t)) byT.set(e.t, []);
    byT.get(e.t).push(slot);
  }
  let ticks = '';
  for (const [t, slots] of byT) {
    const pct = end ? (t / end) * 100 : 0;
    const h = 12 / slots.length;
    slots.forEach((slot, i) => {
      ticks += `<i class="${sbColorClass(slot)}" style="left:${pct.toFixed(2)}%;height:${h.toFixed(1)}px;bottom:${(i * h).toFixed(1)}px"></i>`;
    });
  }
  const moments = byT.size;
  const meta = end
    ? `${sbFmt(end)} · ${p.events.length} cmd${p.events.length === 1 ? '' : 's'} · ${moments} moments`
    : `${p.events.length} cmd${p.events.length === 1 ? '' : 's'} at once`;
  el.innerHTML = `<span class="seq-thumb-track">${ticks}</span><span class="seq-thumb-meta">${esc(meta)}</span>`;
  el.title = 'Open on the timeline';
  el.onclick = () => open(n, rowId);
}
function scheduleThumb(n, rowId) {
  clearTimeout(sbThumbTimers[rowId]);
  sbThumbTimers[rowId] = setTimeout(() => { delete sbThumbTimers[rowId]; renderRowThumb(n, rowId); }, 150);
}
// Strips drawn before the library loaded had no device lanes: draw them all again once it has.
sbLibReady.then(() => {
  document.querySelectorAll('.seq-thumb[id$="-thumb"]').forEach(el => {
    const rowId = el.id.slice(0, -'-thumb'.length);
    const m = /^seq-row-(\d+)-/.exec(rowId);
    if (m) renderRowThumb(+m[1], rowId);
  });
});

window.SeqBuilder = Object.assign({}, SB_CORE, {
  open: (n, rowId) => open(n, rowId),
  openNew: (n) => open(n, null),
  renderRowThumb, scheduleThumb,
  library: () => sbLib,
  ready: () => sbLibReady,
  // Where time t is drawn, for the specs (tests/wizard/specs/seq_builder_fake.spec.js): x = gutter + t * pps / 1000.
  view: () => ({ pps: S.pps, gutter: SB_GUTTER, playhead: S.playhead }),
});
})();
