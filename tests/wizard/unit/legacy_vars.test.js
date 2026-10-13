// Wizard/parser.js on its own - no browser, no board. `node --test` (npm run unit), and wizard.parser in the harness.
//
// Older firmware printed a board's variables as runtime commands, <cmdChar>V,name,value (";V,foo,1"), not as the
// ?VAR,SET config form. parseBackupString reads both shapes a config arrives in - a board's ?backup, one command per
// line (extractIndividualCommandLines, parser.js:336-397), and a chain from a system file or a relay pull
// (extractChainedTokens, 403-442) - and turns a legacy ;V into VAR,SET either way (buildLegacyVarRe, 325-328). In a
// chain the ;V segment is glued to the token before it, so it must be peeled off that token, or it lands inside a
// ?SEQ,SAVE value. Re-scan row parser.backup_tokens (docs/hil_plan/rescan_2026-10-12/wizard.json).
const test = require('node:test');
const assert = require('node:assert');

const P = require('../../../Wizard/parser.js');

// A ?backup as printBackupConfig prints it: one command per line behind the board's function identifier.
const backup = (lines, lfi = '?') => ['', '*** WCB Configuration Backup', '', ...lines.map((l) => (l.startsWith(';') || l.startsWith('/') ? l : lfi + l)),
  '', "*** === For Configured Boards (Current Delimiter: '^') ===", '?WCB,9^;V,never,9', '--------- End of Backup ---------', ''].join('\r\n');
const vars = (c) => c.variables.map((v) => [v.name, v.value]);

test('a per-line backup: a legacy ;V line is a variable, beside the ?VAR,SET form; its verb in either case, TRUE/FALSE values', () => {
  const c = P.parseBackupString(backup(['WCB,1', 'VAR,SET,lights,3', ';V,foo,1', ';v,Foo,-2', ';V,flag,TRUE', ';V,off,false',
                                        'SEQ,SAVE,k,;S1x']));
  assert.deepEqual(vars(c), [['lights', 3], ['foo', 1], ['Foo', -2], ['flag', 1], ['off', 0]]);
  assert.deepEqual(c.sequences, [{ key: 'k', value: ';S1x' }]);
  // Nothing after the chained-section header is read.
  assert.equal(c.wcbNumber, 1);
});

test('a per-line backup on a board with another command character and function identifier: its own cmdChar marks a ;V', () => {
  const c = P.parseBackupString(backup(['WCB,2', 'FUNCCHAR,!', 'CMDCHAR,/', '/V,bar,2', ';V,notvar,5'], '!'));
  assert.deepEqual(vars(c), [['bar', 2]]);
  assert.equal(c.wcbNumber, 2);
});

test('a chain: a ;V segment glued to a ?SEQ,SAVE value is peeled off it - the sequence intact, the variable kept, stacked ones in order', () => {
  const c = P.parseBackupString('?WCB,1^?WCBQ,2^?VAR,SET,a,5^?SEQ,SAVE,k,;S1x^;t500^;S2y^;V,bar,2^;V,baz,-3^?LABEL,S1,Dome');
  assert.deepEqual(c.sequences, [{ key: 'k', value: ';S1x^;t500^;S2y' }]);
  assert.deepEqual(vars(c), [['a', 5], ['bar', 2], ['baz', -3]]);
  assert.equal(c.serialPorts[0].label, 'Dome');
  // At the end of the chain too, and with the board's own command character.
  const d = P.parseBackupString('?WCB,1^?CMDCHAR,/^?SEQ,SAVE,go,/S1x^/V,q,7');
  assert.deepEqual([d.sequences, vars(d)], [[{ key: 'go', value: '/S1x' }], [['q', 7]]]);
  // A sequence that merely ends in something like it is left alone: a ;V needs a name and a number or TRUE/FALSE.
  const e = P.parseBackupString('?WCB,1^?SEQ,SAVE,s,;S1x^;V,bad name,1');
  assert.deepEqual([e.sequences, vars(e)], [[{ key: 's', value: ';S1x^;V,bad name,1' }], []]);
});

test('a system file\'s board section takes a legacy ;V the same way, and a re-export writes it as ?VAR,SET', () => {
  const sys = P.parseSystemFile(['[GENERAL]', '?WCBQ,1^?WCBCH,1', '[WCB1]', '?WCB,1^?SEQ,SAVE,k,;S1x^;V,lights,4'].join('\n'));
  assert.deepEqual(sys.boards.map((b) => [b.sequences, vars(b)]), [[[{ key: 'k', value: ';S1x' }], [['lights', 4]]]]);
  assert.match(P.buildCommandString(sys.boards[0], null, true), /\?VAR,SET,lights,4/);
});
