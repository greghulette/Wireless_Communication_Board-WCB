// Needs no board: pulls, pushes, managing a board through a relay, and the guards around flashing, erasing and OTA
// (Wizard/app.js, Wizard/flasher.js) - the low-risk gaps the 2026-10-12 coverage re-scan found
// (docs/hil_plan/RESCAN_2026-10-12.md, rows in docs/hil_plan/rescan_2026-10-12/wizard.json, named at the end of each
// test's comment). Fake boards and relays in the real page (lib/fake.js): boardPull, boardGo, boardGoRemote,
// remoteBoardPull and boardGoAll run as on the bench, and only the wire is recorded; flashFirmware runs against a fake
// esptool-js (lib/fake_esptool_rescan.mjs) where the flasher itself is the subject. Runs standalone, in CI, and under
// the harness by id (s30_wizard.py, _FAKE_RESCAN_L). Verify pulls are held back where a spec pushes
// (lib/rescan.js holdVerifyPulls).
//
// Nothing here is a real credential: the mesh password is lib/fake.js's dummy.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../lib/fixtures');
const { board, chain, pullFake, push, edit, relayFake } = require('../lib/fake');
const { setup, holdVerifyPulls, toastMark, toastsSince, shown } = require('../lib/rescan');

const sentOn = (page, slot) => page.evaluate((slot) => __fake.sent(slot), slot);
const collects = (page, slot) => page.evaluate((slot) => __fake.log.filter((e) => e.slot === slot && e.op === 'collect').map((e) => e.s), slot);
const sections = (page) => page.evaluate(() => [...document.querySelectorAll('#boards-container [id^="section-board-"]')]
  .map((el) => +el.id.replace('section-board-', '')));
const badge = (page, n) => page.evaluate((n) => document.getElementById(`b${n}-status-badge`).textContent, n);
const pulls = (page, relay) => page.evaluate((relay) => __fake.sent(relay).filter((s) => /MGMT,PULL,\d+,P$/.test(s)), relay);
const FW = '6.2.1_290236RSEP2026';
// A WDP dump a MgmtRelay (WCB19) gives: itself, WCB2 and WCB3 (app_fake.spec.js builds the same from the firmware's
// formats).
const RELAY_DUMP = [
  `[WDP:N=19,CLIENT=0,ALIAS=Relay,HW=24,HWREV=,FW=${FW},CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=0,SEEN=1,PEER=3]`,
  `[WDP:N=2,CLIENT=0,ALIAS=Dome,HW=24,HWREV=,FW=${FW},CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=3,SEEN=1,PEER=1]`,
  `[WDP:N=3,CLIENT=0,ALIAS=Legs,HW=24,HWREV=,FW=${FW},CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=2,SEEN=1,PEER=1]`,
  '[WDPCFG:EN=1,AUTOJOIN=1,PEERS=2]', '[WDP:END,count=3]'].join('\n');

test('wizard.pull_fake_wcbq_renders_floor a pulled WCB quantity above 1 renders that many board sections and nav buttons, a larger one later grows the grid to it, and a board reporting 1 leaves the grid as it is', async ({ page }) => {
  // boardPull's `if (config.wcbQuantity > 1) renderBoards(...)` (app.js:7550), renderBoards / reconcileBoardGrid.
  // Row pull.wcbq_renders_floor.
  await setup(page);
  expect(await sections(page)).toEqual([1]);
  await pullFake(page, 1, board([], { wcbq: 4 }));
  expect(await sections(page)).toEqual([1, 2, 3, 4]);
  expect(await page.evaluate(() => [_boardFloor, document.querySelectorAll('#wcb-nav .wcb-nav-btn').length])).toEqual([4, 4]);
  await pullFake(page, 2, board([], { wcb: 2, wcbq: 6 }));
  expect(await sections(page)).toEqual([1, 2, 3, 4, 5, 6]);
  await pullFake(page, 3, board([], { wcb: 3, wcbq: 1 }));
  expect(await sections(page), 'WCBQ 1 renders nothing new and takes nothing away').toEqual([1, 2, 3, 4, 5, 6]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.pull_fake_success_state a pull shows Pulling... on its button, stores the config and baseline, shows Connected and a toast, asks the version, and gives the button back; a second pull of the same slot while one runs is dropped; a Pull pressed with the board not connected says so, a background one is silent', async ({ page }) => {
  // boardPull (app.js:7437-7646): the in-flight guard, the manual-only toast, the success path. Row pull.success_state.
  await setup(page);
  await pullFake(page, 1, board());
  // Slow the board's backup down so two pulls overlap.
  await page.evaluate(() => {
    const c = boardConnections[1];
    const real = c.sendAndCollect;
    c.sendAndCollect = async (cmd, ...a) => {
      if (cmd === 'WCB_WEBTOOL_CONFIG_PULL') await new Promise((r) => setTimeout(r, 600));
      return real(cmd, ...a);
    };
    boardBaselines[1] = null;
    __fake.log.length = 0;
  });
  const m = await toastMark(page);
  await page.evaluate(() => { window.__p1 = boardPull(1); window.__p2 = boardPull(1); });
  expect(await page.evaluate(() => [document.getElementById('b1-btn-pull').textContent, document.getElementById('b1-btn-pull').disabled,
                                    _boardPullInFlight.has(1)])).toEqual(['Pulling…', true, true]);
  await page.evaluate(async () => { await window.__p1; await window.__p2; });
  await expect.poll(() => collects(page, 1)).toEqual(['WCB_WEBTOOL_CONFIG_PULL', '?version']);
  expect(await page.evaluate(() => [document.getElementById('b1-btn-pull').textContent, document.getElementById('b1-btn-pull').disabled,
                                    _boardPullInFlight.has(1), boardBaselines[1]?.wcbNumber, boardConfigs[1].wcbNumber]))
    .toEqual(['Pull Config', false, false, 1, 1]);
  expect(await badge(page, 1)).toBe('● Connected');
  expect((await toastsSince(page, m)).split('\n').filter((t) => t === 'Config pulled from WCB 1')).toHaveLength(1);

  // Not connected: the Pull button's manual pull says so; a background pull is silent. Neither sends anything.
  await page.evaluate(() => { boardConnections[1]._connected = false; __fake.log.length = 0; });
  let t = await toastMark(page);
  await page.evaluate(() => boardPull(1));
  expect(await toastsSince(page, t)).toBe('');
  await page.evaluate(() => boardPull(1, { manual: true }));
  expect(await toastsSince(page, t)).toBe('Board not connected');
  expect(await page.evaluate(() => [__fake.log.length, _boardPullInFlight.has(1)])).toEqual([0, false]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.pull_fake_remote_delegation Pull on a relayed board asks its relay (?MGMT,PULL); a slot recorded as its own relay is refused with a toast and nothing sent; Retry puts the badge back to Remote and pulls again through the relay, and on a board no longer remote says so', async ({ page }) => {
  // boardPull's delegation (app.js:7439), boardPullRemote and boardRetryPull (9210-9226). Row pull.remote_delegation.
  await setup(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  await page.evaluate(() => { __fake.log.length = 0; });
  // Its own relay: refused.
  await page.evaluate(() => { remoteRelayForBoard[2] = 2; });
  let m = await toastMark(page);
  await page.locator('#b2-btn-pull').click();
  expect(await toastsSince(page, m)).toBe('WCB2 is the relay — connect directly');
  expect(await sentOn(page, 1)).toEqual([]);
  // Back on WCB1: Pull goes through it.
  await page.evaluate(() => { remoteRelayForBoard[2] = 1; });
  await page.locator('#b2-btn-pull').click();
  await expect.poll(() => pulls(page, 1)).toEqual(['?MGMT,PULL,2,P']);
  // That pull gives up (a reply no retry changes): Pull Failed. Retry: Remote again, and a fresh pull.
  await page.evaluate(() => boardConnections[1]._handleLine('[MGMT:CFGERR,2]TOOBIG,L=50000'));
  expect(await badge(page, 2)).toBe('✕ Pull Failed');
  await page.evaluate(() => boardRetryPull(2));
  expect(await badge(page, 2)).toBe('📡 Remote');
  await expect.poll(() => pulls(page, 1)).toEqual(['?MGMT,PULL,2,P', '?MGMT,PULL,2,P']);
  await page.evaluate(() => boardConnections[1]._handleLine('[MGMT:CFGERR,2]TOOBIG,L=50000'));
  // A board that is no longer remote.
  m = await toastMark(page);
  await page.evaluate(() => boardRetryPull(5));
  expect(await toastsSince(page, m)).toBe('Board is no longer set as remote');
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_fake_progress_and_history a push shows its progress on the card ("Pushing... k/n settings") as each setting is answered, hides the bar when done, gives the Push button back, releases the mesh-poll hold and clears the controller switch-back history', async ({ page }) => {
  // boardGo's push loop (app.js:8278-8302), reportPushProgress and setFlashUI (10243-10296), its finally. The mesh-poll
  // hold is also wizard.app_fake_mesh_tick's. Row push.progress_and_history.
  await setup(page);
  await pullFake(page, 1, board());
  await holdVerifyPulls(page);
  await page.evaluate(() => {
    window.__progress = [];
    const real = reportPushProgress;
    window.reportPushProgress = (n, done, total) => {
      real(n, done, total);
      const q = (id) => document.getElementById(id);
      window.__progress.push({ status: q(`b${n}-flash-status`).textContent, pct: q(`b${n}-flash-pct`).textContent,
                               bar: q(`b${n}-flash-bar`).style.width, wrap: q(`b${n}-flash-wrap`).style.display,
                               go: q(`b${n}-btn-go`).textContent, pushing: _pushingBoards.has(n) });
    };
    controllerRoleHistory = { navicore: { boards: {} }, kyber: { boards: {} } };   // as a controller switch leaves it
  });
  await edit(page, '#b1-s1-label', 'HILA');
  await edit(page, '#b1-s2-label', 'HILB');
  const r = await push(page, 1, { skipReboot: true });
  expect(r.sent).toEqual(['1:?LABEL,S1,HILA', '1:?LABEL,S2,HILB']);
  expect(await page.evaluate(() => window.__progress)).toEqual([
    { status: 'Pushing… 1/2 settings (50%)', pct: '50%', bar: '50%', wrap: '', go: 'Pushing…', pushing: true },
    { status: 'Pushing… 2/2 settings (100%)', pct: '100%', bar: '100%', wrap: '', go: 'Pushing…', pushing: true },
  ]);
  expect(await page.evaluate(() => {
    const q = (id) => document.getElementById(id);
    return [q('b1-flash-wrap').style.display, q('b1-btn-go').textContent, q('b1-btn-go').disabled, _pushingBoards.has(1),
            controllerRoleHistory];
  })).toEqual(['none', 'Push Config', false, false, { navicore: null, kyber: null }]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.rpush_fake_guards a push to a relayed board is refused, with a toast and the reason in its outcome, when its relay is not connected, when the board has no config, and when no relay is set; nothing is sent', async ({ page }) => {
  // boardGoRemote's guards (app.js:8582-8605). Row rpush.guards.
  await setup(page);
  await relayFake(page, 1, { 2: chain(board([], { wcb: 2 })) });
  await holdVerifyPulls(page);
  await page.evaluate(() => { __fake.log.length = 0; });
  const run = async (prep) => {
    await page.evaluate(prep);
    const m = await toastMark(page);
    const r = await page.evaluate(async () => { await boardGo(2); return boardPushOutcome[2]; });
    return { outcome: r, toasts: await toastsSince(page, m), sent: await sentOn(page, 1),
             go: await page.evaluate(() => [document.getElementById('b2-btn-go').textContent, document.getElementById('b2-btn-go').disabled]) };
  };
  let r = await run(() => { boardConnections[1]._connected = false; });
  expect(r).toEqual({ outcome: { ok: false, aborted: true, reason: 'relay WCB 1 not connected' }, toasts: 'WCB 1 (relay) not connected',
                      sent: [], go: ['Push Config', false] });
  r = await run(() => { boardConnections[1]._connected = true; delete boardConfigs[2]; });
  expect(r).toEqual({ outcome: { ok: false, aborted: true, reason: 'no config for this board' }, toasts: 'No config for this board',
                      sent: [], go: ['Push Config', false] });
  // No relay set: boardGo never delegates then, so boardGoRemote is called as Push All's remote stage calls it.
  const m = await toastMark(page);
  const o = await page.evaluate(async () => {
    delete remoteRelayForBoard[2];
    boardPushOutcome[2] = { ok: false, aborted: true, reason: 'push did not run' };
    await boardGoRemote(2);
    return boardPushOutcome[2];
  });
  expect(o).toEqual({ ok: false, aborted: true, reason: 'no relay set for this board' });
  expect(await toastsSince(page, m)).toBe('No relay board set for this board');
  expect(await sentOn(page, 1)).toEqual([]);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.pushall_fake_exclusions Push All with no board connected says "No boards connected" and sends nothing - a board whose port has dropped and a MgmtRelay do not count', async ({ page }) => {
  // boardGoAll's start (app.js:9438-9456). The relay and client exclusions are wizard.app_fake_relay_card's and
  // wizard.app_fake_identity's. Row pushall.exclusions.
  await setup(page);
  let m = await toastMark(page);
  await page.evaluate(() => boardGoAll());
  expect(await toastsSince(page, m)).toBe('No boards connected');
  // A board pulled, then its port lost; and a MgmtRelay on USB.
  await pullFake(page, 1, board());
  await pullFake(page, 5, board(['RELAY,1'], { wcb: 19 }));
  await page.evaluate(() => { boardConnections[1]._connected = false; __fake.log.length = 0; });
  m = await toastMark(page);
  await page.evaluate(() => boardGoAll());
  expect(await toastsSince(page, m)).toBe('No boards connected');
  expect(await page.evaluate(() => __fake.log.filter((e) => e.op === 'send' || e.op === 'await').length)).toBe(0);
  expect(page.wizErrors).toEqual([]);
});
