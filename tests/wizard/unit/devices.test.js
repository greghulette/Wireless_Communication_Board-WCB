// Wizard/parser.js: the device cards (MP3 Trigger, HCR, DFPlayer, WLED, Maestro), stored sequences and variables -
// what parseBackupString reads and what buildCommandString sends for a change. No browser, no board (`npm run unit`,
// wizard.parser in the harness, CI). parser.test.js covers identity and ports on the real W1 fixture; these start from
// that fixture too (docs/HIL_TEST_AUDIT.md WP8).
//
// The rules come from the firmware, not from the parser's own comments:
// - a device's route (?HCR,REMOTE,W<n>: this board forwards ;H to WCB<n>) is a separate axis from its local host
//   (?HCR,PORT,S<port>:<baud>). CLEAR drops the host only; REMOTE,OFF drops the route (WCB_HCR.cpp, WCB_MP3.cpp).
// - a WLED slot is one per id and a Maestro slot one per (id, port) on this board (CLAUDE.md rule 5: the same id on
//   another board is a different device). Proxies a board learned over WDP (W<host>S0) are the firmware's to keep, so
//   a push never re-sends them and never sends a blanket ?WLED,CLEAR.
// - a sequence value keeps its commas and its ^ separators (?SEQ,SAVE,key,value; the extractor splits on "^?").
// - a variable is an int; TRUE/FALSE are accepted spellings of 1/0; names are case-sensitive.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const P = require('../../../Wizard/parser.js');
const BACKUP = fs.readFileSync(path.join(__dirname, '..', 'fixtures', 'w1-backup.txt'), 'utf8');

const clone = (o) => JSON.parse(JSON.stringify(o));
const parsed = () => P.parseBackupString(BACKUP);
// A bare ?-chain, the form a push and a saved system file take (parseBackupString's chained path).
const chain = (...tokens) => P.parseBackupString(['?WCB,1', ...tokens].join('^'));
// What a push sends, one command per entry. Split on "^?" like the board does: a sequence value carries "^" inside.
const commands = (cfg, baseline = null, fullPush = false) => {
  const s = P.buildCommandString(cfg, baseline, fullPush);
  return s ? s.split(/\^(?=\?)/) : [];
};
const only = (cmds, ...prefixes) => cmds.filter((c) => prefixes.some((p) => c.startsWith(p)));

test('MP3 / DFP / HCR: local host, ONERR, route and CLEAR parse as separate axes', () => {
  const c = chain('?MP3,S2:9600:V0', '?MP3,ONERR,Leia', '?DFP,S3:9600:V15', '?DFP,ONERR,seqA',
                  '?HCR,PORT,S1:9600', '?HCR,POLL,5');
  assert.deepEqual(c.mp3, { enabled: true, port: 2, baud: 9600, volume: 0, onError: 'Leia', remoteWCB: 0 });
  assert.deepEqual(c.dfp, { enabled: true, port: 3, baud: 9600, volume: 15, onError: 'seqA', remoteWCB: 0 });
  assert.deepEqual(c.hcr, { enabled: true, port: 1, baud: 9600, poll: 5, remoteWCB: 0 });

  const routed = chain('?MP3,REMOTE,W2', '?HCR,REMOTE,W3', '?DFP,REMOTE,W2');
  assert.equal(routed.mp3.remoteWCB, 2);
  assert.equal(routed.mp3.enabled, false);
  assert.equal(routed.hcr.remoteWCB, 3);
  assert.equal(routed.dfp.remoteWCB, 2);

  // CLEAR drops the local host and leaves the route alone; REMOTE,OFF drops the route.
  const cleared = chain('?MP3,S2:9600:V0', '?MP3,REMOTE,W2', '?MP3,CLEAR');
  assert.equal(cleared.mp3.enabled, false);
  assert.equal(cleared.mp3.port, null);
  assert.equal(cleared.mp3.remoteWCB, 2);
  assert.equal(chain('?HCR,REMOTE,W2', '?HCR,REMOTE,OFF').hcr.remoteWCB, 0);
});

test('a device change pushes only that device: host lines, or the route, or the CLEAR forms', () => {
  const baseline = parsed();                 // W1: HCR routed to W2, no MP3, no DFP

  const host = clone(baseline);
  Object.assign(host.mp3, { enabled: true, port: 2, volume: 0, onError: 'Leia' });
  assert.deepEqual(commands(host, baseline), ['?MP3,S2:9600:V0', '?MP3,ONERR,Leia']);

  const dfp = clone(baseline);
  Object.assign(dfp.dfp, { enabled: true, port: 3, volume: 20 });
  assert.deepEqual(commands(dfp, baseline), ['?DFP,S3:9600:V20']);

  // Taking the HCR in-house: ?HCR,PORT makes this board the host, which is what clears the route on the board.
  const hcrHost = clone(baseline);
  Object.assign(hcrHost.hcr, { enabled: true, port: 3, remoteWCB: 0 });
  assert.deepEqual(commands(hcrHost, baseline), ['?HCR,PORT,S3:9600', '?HCR,POLL,10']);

  // Dropping the route: REMOTE,OFF first (the baseline had one), then CLEAR.
  const unrouted = clone(baseline);
  unrouted.hcr.remoteWCB = 0;
  assert.deepEqual(commands(unrouted, baseline), ['?HCR,REMOTE,OFF', '?HCR,CLEAR']);

  // Moving the route sends the new route alone.
  const rerouted = clone(baseline);
  rerouted.hcr.remoteWCB = 3;
  assert.deepEqual(commands(rerouted, baseline), ['?HCR,REMOTE,W3']);
});

test('a full push re-states every device: hosts and routes as they are, CLEAR for the rest, never a REMOTE,OFF', () => {
  const full = commands(parsed(), null, true);
  assert.deepEqual(only(full, '?MP3', '?HCR', '?DFP'), ['?MP3,CLEAR', '?HCR,REMOTE,W2', '?DFP,CLEAR']);
});

test('WLED: one local slot per id, the legacy PORT form is id 1, targeted clears, and a learned proxy is never pushed', () => {
  const c = chain('?WLED,3:W1S4:9600', '?WLED,3:W1S5:19200', '?WLED,2:W2S0:115200', '?WLED,PORT,S3:9600');
  assert.deepEqual(c.wleds, [{ id: 3, port: 5, baud: 19200 }, { id: 1, port: 3, baud: 9600 }]);
  assert.deepEqual(c.wledRemotes, [{ id: 2, host: 2, baud: 115200 }]);
  assert.deepEqual(chain('?WLED,3:W1S4:9600', '?WLED,4:W1S5:9600', '?WLED,CLEAR,3').wleds, [{ id: 4, port: 5, baud: 9600 }]);
  const all = chain('?WLED,3:W1S4:9600', '?WLED,2:W2S0:115200', '?WLED,CLEAR');
  assert.deepEqual(all.wleds, []);
  assert.deepEqual(all.wledRemotes, [{ id: 2, host: 2, baud: 115200 }]);   // a local clear-all does not touch proxies

  const baseline = parsed();                                                // W1 has only the W2-hosted WLED 1
  const added = clone(baseline);
  added.wleds.push({ id: 3, port: 4, baud: 9600 });
  assert.deepEqual(commands(added, baseline), ['?WLED,3:W1S4:9600']);
  const rebauded = clone(added);
  rebauded.wleds[0].baud = 19200;
  assert.deepEqual(commands(rebauded, added), ['?WLED,3:W1S4:19200']);      // the id's slot is reused: no clear
  const removed = clone(added);
  removed.wleds = [];
  assert.deepEqual(commands(removed, added), ['?WLED,CLEAR,3']);             // targeted, never a blanket ?WLED,CLEAR
  assert.deepEqual(only(commands(baseline, null, true), '?WLED'), []);       // the proxy belongs to the firmware
});

test('Maestro: slots by (id, port), the same id on another board is a Kyber target, and a push clears what moved', () => {
  const c = chain('?MAESTRO,M1:W1S1:57600', '?MAESTRO,M1:W1S1:9600', '?MAESTRO,M1:W1S2:57600', '?MAESTRO,M1:W2S1:57600');
  assert.deepEqual(c.maestros, [{ id: 1, port: 1, baud: 9600 }, { id: 1, port: 2, baud: 57600 }]);   // baud upserts; a second port is a second slot
  assert.deepEqual(c.kyber.targets, [{ id: 1, wcb: 2, port: 1, baud: 57600 }]);

  const baseline = parsed();                                                // M1 on S1 at 57600
  const added = clone(baseline);
  added.maestros.push({ id: 2, port: 3, baud: 9600 });
  assert.deepEqual(only(commands(added, baseline), '?MAESTRO'), ['?MAESTRO,M1:W1S1:57600,M2:W1S3:9600']);
  const moved = clone(baseline);
  moved.maestros[0].port = 2;
  assert.deepEqual(only(commands(moved, baseline), '?MAESTRO'), ['?MAESTRO,CLEAR,M1:W1S1', '?MAESTRO,M1:W1S2:57600']);
  const none = clone(baseline);
  none.maestros = [];
  assert.deepEqual(only(commands(none, baseline), '?MAESTRO'), ['?MAESTRO,CLEAR,ALL']);
});

test('sequences: values keep commas and ^ chains; a push sends only the changed keys and SEQ,CLEAR for removed ones', () => {
  const c = parsed();
  const seqs = Object.fromEntries(c.sequences.map((s) => [s.key, s.value]));
  assert.equal(seqs['03'], ';m11^;t4000^;w2testing');                    // a ^ inside a value must not split the chain
  assert.equal(seqs['Leia'], ';w2;s3test***Testing Leie Message');
  assert.equal(chain('?SEQ,SAVE,k,;w1;s5,testing,more').sequences[0].value, ';w1;s5,testing,more');

  const edited = clone(c);
  edited.sequences.find((s) => s.key === 'Leia').value = ';w2;s3new';
  assert.deepEqual(only(commands(edited, c), '?SEQ'), ['?SEQ,SAVE,Leia,;w2;s3new']);
  const removed = clone(c);
  removed.sequences = removed.sequences.filter((s) => s.key !== '03');
  assert.deepEqual(only(commands(removed, c), '?SEQ'), ['?SEQ,CLEAR,03']);
  const added = clone(c);
  added.sequences.push({ key: 'new1', value: ';t100^;s2hi' });
  assert.deepEqual(only(commands(added, c), '?SEQ'), ['?SEQ,SAVE,new1,;t100^;s2hi']);
  assert.deepEqual(only(commands(c, null, true), '?SEQ'),
                   ['?SEQ,SAVE,03,;m11^;t4000^;w2testing', '?SEQ,SAVE,Leia,;w2;s3test***Testing Leie Message']);
});

test('variables: ints and TRUE/FALSE, case-sensitive names, and a per-name diff push', () => {
  const c = chain('?VAR,SET,count,5', '?VAR,SET,flag,TRUE', '?VAR,SET,Flag,FALSE', '?VAR,SET,count,7', '?VAR,GET,count');
  assert.deepEqual(c.variables, [{ name: 'count', value: 7 }, { name: 'flag', value: 1 }, { name: 'Flag', value: 0 }]);

  const edited = clone(c);
  edited.variables.find((v) => v.name === 'flag').value = 0;
  edited.variables.push({ name: 'extra', value: 12 });
  edited.variables = edited.variables.filter((v) => v.name !== 'Flag');
  assert.deepEqual(only(commands(edited, c), '?VAR'), ['?VAR,SET,flag,0', '?VAR,SET,extra,12', '?VAR,CLEAR,Flag']);
  assert.deepEqual(only(commands(c, clone(c)), '?VAR'), []);
  assert.deepEqual(only(commands(c, null, true), '?VAR'), ['?VAR,SET,count,7', '?VAR,SET,flag,1', '?VAR,SET,Flag,0']);
});

test('a board with every device round-trips through a full push', () => {
  const a = parsed();
  Object.assign(a.mp3, { enabled: true, port: 2, volume: 3, onError: 'Leia' });
  Object.assign(a.dfp, { enabled: true, port: 3, volume: 20 });
  Object.assign(a.hcr, { enabled: true, port: 4, poll: 0, remoteWCB: 0 });
  a.wleds.push({ id: 3, port: 5, baud: 9600 });
  a.variables = [{ name: 'count', value: 7 }];
  const b = P.parseBackupString(P.buildCommandString(a, null, true));
  for (const k of ['mp3', 'dfp', 'hcr', 'wleds', 'maestros', 'sequences', 'variables']) {
    assert.deepEqual(b[k], a[k], k);
  }
});
