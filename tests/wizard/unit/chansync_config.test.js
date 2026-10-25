// Wizard/parser.js on its own - no browser, no board. `node --test` (npm run unit), and wizard.parser in the harness.
//
// The config lines channel sync and the WebSocket token added to the firmware (docs/CHANNEL_SYNC.md §6-7,
// docs/WIFI_DESIGN.md §7): ?REGION,<cc> and ?CHSYNC,ON|OFF fleet-wide like the mesh channel, ?WIFI,TOKEN,<token>|CLEAR
// per board; and the two status lines the mesh panel reads, [WDPCH:...] and [CHSYNC:...].
const test = require('node:test');
const assert = require('node:assert');

const P = require('../../../Wizard/parser.js');

const parse = (...tokens) => P.parseBackupString(['?HW,24', '?WCB,1', ...tokens.map((t) => `?${t}`)].join('^'));
const commands = (cfg, baseline = null, fullPush = false) => {
  const s = P.buildCommandString(cfg, baseline, fullPush);
  return s ? s.split(/\^(?=\?)/) : [];
};
const clone = (o) => JSON.parse(JSON.stringify(o));

test('REGION: a listed code is kept, a bare ?REGION (the query) or an unknown code is not; the default is 01', () => {
  assert.equal(P.createDefaultBoardConfig().region, '01');
  assert.equal(parse('REGION,DE').region, 'DE');
  assert.equal(parse('REGION,de').region, 'DE');
  assert.equal(parse('REGION,DE', 'REGION').region, 'DE');
  for (const bad of ['XX', 'DEU', '', '1']) assert.equal(parse(`REGION,${bad}`).region, '01', bad);
  assert.equal(parse('WCBCH,6').region, '01', 'firmware before channel sync: no line, the default');
});

test('regionMaxChannel: 01, US, CA and TW stop at 11; every other listed code is taken to allow 13; anything else 11', () => {
  for (const cc of ['01', 'US', 'CA', 'TW']) assert.equal(P.regionMaxChannel(cc), 11, cc);
  for (const cc of ['DE', 'GB', 'JP', 'AU', 'CN']) assert.equal(P.regionMaxChannel(cc), 13, cc);
  for (const cc of ['XX', '', null, undefined]) assert.equal(P.regionMaxChannel(cc), 11, String(cc));
  assert.equal(P.CHSYNC_REGIONS[0], '01');
  assert.equal(new Set(P.CHSYNC_REGIONS).size, P.CHSYNC_REGIONS.length);
});

test('CHSYNC: ON / OFF set it; SEARCH, MOVE and the bare query do not; the default is on', () => {
  assert.equal(P.createDefaultBoardConfig().chanSync, true);
  assert.equal(parse('CHSYNC,OFF').chanSync, false);
  assert.equal(parse('CHSYNC,OFF', 'CHSYNC,ON').chanSync, true);
  for (const q of ['CHSYNC', 'CHSYNC,SEARCH', 'CHSYNC,MOVE,6']) assert.equal(parse('CHSYNC,OFF', q).chanSync, false, q);
});

test('a full push writes REGION ahead of WCBCH (the board checks the channel against its region) and CHSYNC; a delta push only what changed', () => {
  const c = parse('REGION,DE', 'WCBCH,13', 'CHSYNC,OFF');
  const full = commands(c, null, true);
  assert.ok(full.includes('?REGION,DE') && full.includes('?WCBCH,13') && full.includes('?CHSYNC,OFF'), full.join(' | '));
  assert.ok(full.indexOf('?REGION,DE') < full.indexOf('?WCBCH,13'));
  assert.deepEqual(P.diffConfigs(c, P.parseBackupString(full.join('^'))), [], 'round trip');
  assert.deepEqual(commands(c, clone(c)), [], 'unchanged: nothing');
  const moved = clone(c);
  moved.region = 'US';
  moved.meshChannel = 6;
  moved.chanSync = true;
  assert.deepEqual(commands(moved, c), ['?REGION,US', '?WCBCH,6', '?CHSYNC,ON']);
  // The defaults on a full push too (the board may hold others), in a form firmware without them answers
  // "Unknown command" to - an answer, which is all a push waits for.
  const dflt = commands(P.createDefaultBoardConfig(), null, true);
  assert.ok(dflt.includes('?REGION,01') && dflt.includes('?CHSYNC,ON'));
});

test('region and channel sync go through a saved system file as General settings', () => {
  const sys = P.createDefaultSystemConfig();
  sys.general.region = 'GB';
  sys.general.meshChannel = 12;
  sys.general.chanSync = false;
  sys.boards = [Object.assign(P.createDefaultBoardConfig(), { wcbNumber: 1, hwVersion: 24 })];
  const back = P.parseSystemFile(P.buildSystemFile(sys));
  assert.deepEqual([back.general.region, back.general.meshChannel, back.general.chanSync], ['GB', 12, false]);
  assert.deepEqual([back.boards[0].region, back.boards[0].chanSync], ['GB', false]);
});

test('wifiTokenProblem is the firmware\'s rule: 8-32 printable ASCII, none of space , ^ ; ? " \\', () => {
  for (const ok of ['abcdefgh', 'A1!#$%&\'()*+-./:<=>@[]_`{|}~', 'x'.repeat(32)]) assert.equal(P.wifiTokenProblem(ok), null, ok);
  for (const bad of ['', 'short7c', 'x'.repeat(33), 'has space1', 'comma,abcd', 'caret^abcd', 'semi;abcde', 'quest?abcd',
                     'quote"abcd', 'back\\slash', 'tab\tabcdef', 'umlautäbcd']) {
    assert.ok(P.wifiTokenProblem(bad), JSON.stringify(bad));
  }
});

test('WIFI,TOKEN: parsed apart from the mode line, which it leaves alone; CLEAR clears; a token the firmware would refuse is not kept', () => {
  const c = parse('WIFI,JOIN,HIL-FAKE-NET,fake pass,1', 'WIFI,TOKEN,fake-token-1');
  assert.equal(c.wifiToken, 'fake-token-1');
  assert.deepEqual([c.wifi.mode, c.wifi.joinSsid, c.wifi.joinPass], ['join', 'HIL-FAKE-NET', 'fake pass,1']);
  assert.equal(parse('WIFI,TOKEN,fake-token-1', 'WIFI,TOKEN,CLEAR').wifiToken, '');
  assert.equal(parse('WIFI,TOKEN,short').wifiToken, '');
  assert.equal(parse('WIFI,TOKEN,fake-token-1', 'WIFI,OFF').wifiToken, 'fake-token-1', 'WIFI,OFF is a mode, not the token');
});

test('the token on a push: its own line ahead of the mode line; CLEAR only of one the board had, never on a full push', () => {
  const c = parse('WIFI,JOIN,HIL-FAKE-NET,fakepass1', 'WIFI,TOKEN,fake-token-1');
  const full = commands(c, null, true);
  assert.ok(full.indexOf('?WIFI,TOKEN,fake-token-1') >= 0 && full.indexOf('?WIFI,TOKEN,fake-token-1') < full.indexOf('?WIFI,JOIN,HIL-FAKE-NET,fakepass1'),
            full.join(' | '));
  assert.deepEqual(P.diffConfigs(c, P.parseBackupString(full.join('^'))), [], 'round trip');
  const none = clone(c);
  none.wifiToken = '';
  assert.deepEqual(commands(none, c).filter((x) => x.startsWith('?WIFI')), ['?WIFI,TOKEN,CLEAR'], 'cleared: CLEAR, and no mode line');
  assert.ok(!commands(none, null, true).some((x) => x.startsWith('?WIFI,TOKEN')), 'a full push with none sends nothing');
  const other = clone(c);
  other.wifiToken = 'other-token-2';
  assert.deepEqual(commands(other, c), ['?WIFI,TOKEN,other-token-2']);
  assert.deepEqual(commands(c, clone(c)), [], 'unchanged: nothing');
});

test('[WDPCH:...]: read by key, so another order or an added field still parses; anything else is null', () => {
  assert.deepEqual(P.parseWdpChanLine('[WDPCH:N=3,CH=6,RANK=3,PIN=1,SYNC=1,CC=DE]'),
                   { n: 3, ch: 6, rank: 3, pinned: true, sync: true, cc: 'DE' });
  assert.deepEqual(P.parseWdpChanLine('[WDPCH:CC=01,SYNC=0,PIN=0,RANK=255,CH=1,N=19,NEW=7]'),
                   { n: 19, ch: 1, rank: 255, pinned: false, sync: false, cc: '01' });
  for (const no of ['[WDP:N=3,CLIENT=0]', '[WDPCH:CH=6]', 'WDPCH:N=3', '', null]) assert.equal(P.parseWdpChanLine(no), null, String(no));
});

test('[CHSYNC:...] anywhere in a ?CHSYNC reply; none (firmware before channel sync: "Unknown command") is null', () => {
  const reply = 'Channel sync: on\r\nLeader: NaviCore 20\n[CHSYNC:EN=1,CH=6,MAXCH=13,CC=DE,RANK=3,PIN=0,LEAD=20,LRANK=0,LPIN=1,STATE=search]\n';
  assert.deepEqual(P.parseChanSyncStatus(reply), { enabled: true, ch: 6, maxCh: 13, cc: 'DE', rank: 3, pinned: false,
                                                   leader: 20, leaderRank: 0, leaderPinned: true, state: 'search' });
  assert.equal(P.parseChanSyncStatus('Unknown command: CHSYNC\nType \'? ?\' for help'), null);
  assert.equal(P.parseChanSyncStatus(''), null);
});
