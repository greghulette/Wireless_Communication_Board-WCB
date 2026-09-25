// Needs no board: pure push-generator logic, run in the real page. Runs standalone (`npx playwright test`) and as
// the harness's wizard.kyber_release_order (tests/hil/suites/s30_wizard.py).
//
// Tracker #73 D5/D9. Every way out of Kyber LOCAL - ?KYBER,CLEAR, ?MAESTRO,REMOTE, and a ?KYBER,LOCAL port move -
// puts the port it gives up back to 9600 with broadcasts on (kyberReleasePort, WCB_Storage.cpp). So the push must
// send the release AHEAD of the BAUD/BCAST lines, and a delta push must re-send the released port's rows, or the
// release undoes the push's own settings for that port while the Wizard still shows them. A targets-only diff on a
// remote/none board must send no release at all: it would ask for a reboot for nothing (commandStringNeedsReboot).
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');

test('wizard.kyber_release_order leaving or moving a local Kyber releases its port before the BAUD/BCAST lines', async ({ page }) => {
  await openWizard(page);
  const r = await page.evaluate(() => {
    const P = window.WCBParser;
    const clone = (o) => JSON.parse(JSON.stringify(o));
    const build = (cfg, base, full) => P.buildCommandString(cfg, base, full).split('^');
    // Baseline: Kyber LOCAL on S2, which the LOCAL claim left at 115200 with both broadcast flags off.
    const base = P.createDefaultBoardConfig();
    base.kyber.mode = 'local';
    base.kyber.port = 2;
    Object.assign(base.serialPorts[1], { baud: 115200, broadcastOut: false, broadcastIn: false });
    const variant = (fn) => { const c = clone(base); fn(c); return c; };

    const remoteBase = P.createDefaultBoardConfig();
    remoteBase.kyber.mode = 'remote';
    remoteBase.kyber.targets = [{ id: 2, wcb: 2, port: 1, baud: 57600 }];
    const remoteTargets = clone(remoteBase);
    remoteTargets.kyber.targets = [{ id: 2, wcb: 3, port: 1, baud: 57600 }];

    return {
      remote: build(variant((c) => { c.kyber.mode = 'remote'; c.kyber.port = null; }), clone(base), false),
      none:   build(variant((c) => { c.kyber.mode = 'none';   c.kyber.port = null; }), clone(base), false),
      move:   build(variant((c) => { c.kyber.port = 1; }), clone(base), false),
      targetsOnly: build(variant((c) => { c.kyber.targets = [{ id: 1, wcb: 2, port: 1, baud: 57600 }]; }), clone(base), false),
      fullRemote: build(variant((c) => { c.kyber.mode = 'remote'; c.kyber.port = null; }), clone(base), true),
      remoteToRemote: build(remoteTargets, remoteBase, false),
    };
  });

  const idx = (cmds, prefix) => cmds.findIndex((c) => c.startsWith(prefix));
  const count = (cmds, exact) => cmds.filter((c) => c === exact).length;
  const lastIdx = (cmds, prefix) => cmds.map((c) => c.startsWith(prefix)).lastIndexOf(true);
  const s2Rows = ['?BAUD,S2,115200', '?BCAST,OUT,S2,OFF', '?BCAST,IN,S2,OFF'];

  // Local -> remote: MAESTRO,REMOTE before S2's BAUD, and S2's rows re-sent although they did not change.
  const rm = r.remote;
  expect(idx(rm, '?MAESTRO,REMOTE'), `remote: ${rm.join(' | ')}`).toBeGreaterThanOrEqual(0);
  expect(idx(rm, '?MAESTRO,REMOTE'), `remote: ${rm.join(' | ')}`).toBeLessThan(idx(rm, '?BAUD,S2,'));
  for (const row of s2Rows) expect(rm, `remote re-sends ${row}`).toContain(row);
  expect(idx(rm, '?KYBER,CLEAR'), `remote sends no KYBER,CLEAR: ${rm.join(' | ')}`).toBe(-1);

  // Local -> none: exactly one KYBER,CLEAR, before S2's BAUD, and S2's rows re-sent.
  const nn = r.none;
  expect(count(nn, '?KYBER,CLEAR'), `none: ${nn.join(' | ')}`).toBe(1);
  expect(idx(nn, '?KYBER,CLEAR'), `none: ${nn.join(' | ')}`).toBeLessThan(idx(nn, '?BAUD,S2,'));
  for (const row of s2Rows) expect(nn, `none re-sends ${row}`).toContain(row);

  // Local S2 -> local S1: KYBER,CLEAR before S2's BAUD, S2's rows re-sent, and the claim after the last BCAST line.
  const mv = r.move;
  expect(idx(mv, '?KYBER,CLEAR'), `move: ${mv.join(' | ')}`).toBeGreaterThanOrEqual(0);
  expect(idx(mv, '?KYBER,CLEAR'), `move: ${mv.join(' | ')}`).toBeLessThan(idx(mv, '?BAUD,S2,'));
  for (const row of s2Rows) expect(mv, `move re-sends ${row}`).toContain(row);
  expect(idx(mv, '?KYBER,LOCAL,S1'), `move: ${mv.join(' | ')}`).toBeGreaterThan(lastIdx(mv, '?BCAST,'));

  // Same port, targets changed: a claim only, no release.
  const tg = r.targetsOnly;
  expect(idx(tg, '?KYBER,CLEAR'), `same port: ${tg.join(' | ')}`).toBe(-1);
  expect(idx(tg, '?MAESTRO,REMOTE'), `same port: ${tg.join(' | ')}`).toBe(-1);
  expect(idx(tg, '?KYBER,LOCAL,S2,'), `same port: ${tg.join(' | ')}`).toBeGreaterThanOrEqual(0);

  // Full push of a remote board: MAESTRO,REMOTE before the first BAUD line.
  const fr = r.fullRemote;
  expect(idx(fr, '?MAESTRO,REMOTE'), 'full push remote sends MAESTRO,REMOTE').toBeGreaterThanOrEqual(0);
  expect(idx(fr, '?MAESTRO,REMOTE'), 'full push remote: MAESTRO,REMOTE before ?BAUD,S1').toBeLessThan(idx(fr, '?BAUD,S1,'));

  // Remote -> remote with different targets (the proxy ?MAESTRO lines): no release, so no reboot prompt.
  const rr = r.remoteToRemote;
  expect(idx(rr, '?MAESTRO,REMOTE'), `remote->remote: ${rr.join(' | ')}`).toBe(-1);
  expect(idx(rr, '?KYBER,CLEAR'), `remote->remote: ${rr.join(' | ')}`).toBe(-1);

  expect(page.wizErrors).toEqual([]);
});

// Tracker #73 D4. A local Kyber reserves only its own port - the firmware's kyberModeReservesPort, which the
// HCR/MP3/DFP/WLED guards and canUsePWMOnPort call - so the other hardware port must carry no claim (it takes a
// device or PWM like any port), a bare ?KYBER,LOCAL still claims S2 (loadKyberSettings' fallback), and Maestro
// REMOTE keeps S1 ('kyber-reserved'). With S1 now offered to devices, a delta push from REMOTE to local must
// release REMOTE's S1 reservation (KYBER,CLEAR) before the device blocks, or the board refuses the device.
test('wizard.kyber_local_frees_other_port a local Kyber claims only its own port, and leaving REMOTE releases S1 before the device lines', async ({ page }) => {
  await openWizard(page);
  const r = await page.evaluate(() => {
    const P = window.WCBParser;
    const clone = (o) => JSON.parse(JSON.stringify(o));
    const claims = (mode, port) => {
      const c = P.createDefaultBoardConfig();
      c.kyber.mode = mode;
      c.kyber.port = port;
      P.evaluatePortClaims(c);
      return c.serialPorts.map((sp) => (sp.claimedBy ? sp.claimedBy.type : null));
    };
    const remoteBase = P.createDefaultBoardConfig();
    remoteBase.kyber.mode = 'remote';
    const toLocal = clone(remoteBase);
    Object.assign(toLocal.kyber, { mode: 'local', port: 2 });
    Object.assign(toLocal.hcr, { enabled: true, port: 1, baud: 9600 });
    const sameLocal = clone(toLocal);
    sameLocal.hcr.baud = 38400;
    return {
      local1: claims('local', 1), local2: claims('local', 2), localBare: claims('local', null),
      remote: claims('remote', null), none: claims('none', null),
      toLocal: P.buildCommandString(toLocal, clone(remoteBase), false).split('^'),
      sameLocal: P.buildCommandString(sameLocal, clone(toLocal), false).split('^'),
    };
  });

  expect(r.local1, 'local S1').toEqual(['kyber', null, null, null, null]);
  expect(r.local2, 'local S2').toEqual([null, 'kyber', null, null, null]);
  expect(r.localBare, 'bare local = S2').toEqual([null, 'kyber', null, null, null]);
  expect(r.remote, 'remote keeps S1').toEqual(['kyber-reserved', null, null, null, null]);
  expect(r.none, 'no Kyber').toEqual([null, null, null, null, null]);

  const idx = (cmds, prefix) => cmds.findIndex((c) => c.startsWith(prefix));
  const tl = r.toLocal;
  expect(idx(tl, '?KYBER,CLEAR'), `remote->local: ${tl.join(' | ')}`).toBeGreaterThanOrEqual(0);
  expect(idx(tl, '?KYBER,CLEAR'), `remote->local: ${tl.join(' | ')}`).toBeLessThan(idx(tl, '?HCR,PORT,S1:'));
  expect(idx(tl, '?HCR,PORT,S1:'), `remote->local: ${tl.join(' | ')}`).toBeLessThan(idx(tl, '?KYBER,LOCAL,S2'));
  // Local -> local on the same port, device edit only: no release, no claim.
  const sl = r.sameLocal;
  expect(idx(sl, '?KYBER,'), `same local: ${sl.join(' | ')}`).toBe(-1);

  expect(page.wizErrors).toEqual([]);
});

test('wizard.kyber_auto_targets a local Kyber board targets every connected board\'s Maestros (its own included), keeps every remembered target of a board that is not connected (the same id on another board included), drops the remembered ones of a connected board, and a remote board is left alone', async ({ page }) => {
  await openWizard(page);
  const r = await page.evaluate(() => {
    const P = window.WCBParser;
    const mk = (n, fn) => { const c = P.createDefaultBoardConfig(); c.wcbNumber = n; fn(c); return c; };
    const list = (c) => c.kyber.targets.map((t) => `M${t.id}:W${t.wcb}S${t.port}:${t.baud}`);
    boardConfigs[1] = mk(1, (c) => {
      c.kyber.mode = 'local';
      c.kyber.port = 2;
      c.kyber.targets = [{ id: 5, wcb: 3, port: 1, baud: 57600 },    // from the last ?backup: W3 is not connected
                         { id: 1, wcb: 3, port: 2, baud: 9600 },      // same id as W1's own Maestro, hosted on W3
                         { id: 9, wcb: 2, port: 5, baud: 9600 }];     // remembered on W2, which reports a live list without it
      c.maestros = [{ id: 1, port: 1, baud: 57600 }];
    });
    boardConfigs[2] = mk(2, (c) => { c.maestros = [{ id: 2, port: 1, baud: 115200 }, { id: 1, port: 3, baud: 9600 }]; });
    boardConfigs[3] = undefined;
    autoComputeKyberTargets(1);
    const local = list(boardConfigs[1]);
    boardConfigs[4] = mk(4, (c) => { c.kyber.mode = 'remote'; c.kyber.targets = [{ id: 9, wcb: 9, port: 9, baud: 9600 }]; });
    autoComputeKyberTargets(4);
    return { local, remote: list(boardConfigs[4]) };
  });
  // Connected boards first, in board order, the Kyber board's own Maestro included; then every remembered target on
  // the unconnected W3, the M1 that shares its id with W1's own Maestro included (CLAUDE.md rule 5). The remembered M9
  // on W2 is gone: W2 reported a live list. (Until 2026-09-24 the fallback was keyed on the id alone and dropped the
  // M1 - docs/HIL_TEST_AUDIT.md F4.)
  expect(r.local).toEqual(['M1:W1S1:57600', 'M2:W2S1:115200', 'M1:W2S3:9600', 'M5:W3S1:57600', 'M1:W3S2:9600']);
  expect(r.remote).toEqual(['M9:W9S9:9600']);
});
