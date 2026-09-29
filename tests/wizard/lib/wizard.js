const { test } = require('./fixtures');   // the extended test: test.skip() here marks the running test skipped

// Driving the Wizard page. The Wizard is classic scripts with top-level `let`/`const` state (boardConfigs,
// boardBaselines, _boardPullInFlight, boardPushOutcome in Wizard/app.js), which page.evaluate() reaches by name.

async function openWizard(page) {
  // The persistent profile also keeps Chrome's HTTP cache, and a server that sends Last-Modified (python's
  // http.server, which reuseExistingServer adopts if one is already on the port; serve.js sends none) lets Chrome
  // reuse a cached script without asking: on 2026-09-23 the page ran the previous parser.js after an edit, and
  // wizard.kyber_release_order failed against code that was no longer on disk. Every test must drive the Wizard
  // as it is now, so the cache is off for this page (Chromium DevTools protocol; the fixtures launch Chromium).
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
  await page.goto('/Wizard/index.html');
  // A persistent profile keeps localStorage between runs; 'wcbSharedSlot' in particular steers the next connect
  // into shared-hub failover. The Web Serial grant lives elsewhere in the profile and survives this.
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await page.waitForFunction(() => typeof boardManualConnect === 'function');
  await page.locator('#splash-overlay button[onclick="splashGoConfig()"]').click();
}

// Indices into navigator.serial.getPorts() — the order the Wizard's own port picker lists them in — whose
// USB VID/PID match the harness's device.
function grantedMatches(page, ctx) {
  return page.evaluate(async ({ vid, pid }) => {
    const ports = await navigator.serial.getPorts();
    return ports.map((p) => p.getInfo())
      .map((i, k) => (i.usbVendorId === vid && i.usbProductId === pid ? k : -1))
      .filter((k) => k >= 0);
  }, { vid: ctx.vid, pid: ctx.pid });
}

// First run for a profile: open Chrome's own port dialog and wait for Greg to pick the COM port the harness named.
// requestPort() needs a user gesture; a Playwright click is a real one. The grant then persists in the profile.
//
// Needing the dialog at all means nobody has authorized this board yet, so cancelling it — or leaving it
// unanswered — SKIPS the test rather than failing it: it says no one is at the keyboard, not that the Wizard is
// broken. An unattended run then reports SKIP with the reason instead of a stack trace (2026-09-22: a cancelled
// dialog failed wizard.pull and wizard.push_label). Picking the WRONG port is still a failure.
// WIZ_NO_AUTHORIZE=1 skips without ever opening the dialog, for a run nobody is watching.
async function authorize(page, ctx) {
  const say = `Pick ${ctx.com} (${ctx.device}) in Chrome's port dialog — once per profile`;
  if (process.env.WIZ_NO_AUTHORIZE) {
    test.skip(true, `${ctx.device} (${ctx.com}) is not authorized in .profiles/${ctx.device} and ` +
                    'WIZ_NO_AUTHORIZE is set — run this once with someone at the keyboard to authorize it');
  }
  console.log(`>>> ${say}`);
  await page.evaluate(({ say, vid, pid }) => {
    const b = document.createElement('button');
    b.id = 'hil-authorize';
    b.textContent = `HIL: ${say}`;
    b.style.cssText = 'position:fixed;top:8px;left:50%;transform:translateX(-50%);z-index:99999;' +
                      'padding:10px 16px;font:bold 14px sans-serif;background:#bf8700;color:#fff;border:0;border-radius:6px';
    b.onclick = async () => {
      try {
        const port = await navigator.serial.requestPort();
        const i = port.getInfo();
        if (i.usbVendorId !== vid || i.usbProductId !== pid) {
          await port.forget();      // don't leave a wrong board granted in this device's profile
          b.dataset.done = `wrong port (VID ${i.usbVendorId} PID ${i.usbProductId})`;
        } else {
          b.dataset.done = 'ok';
        }
      } catch (e) {
        b.dataset.done = `cancelled (${e.name})`;
      }
    };
    document.body.appendChild(b);
  }, { say, vid: ctx.vid, pid: ctx.pid });
  await page.locator('#hil-authorize').click();
  let done;
  try {
    const handle = await page.waitForFunction(() => document.getElementById('hil-authorize')?.dataset.done,
                                              null, { timeout: 180_000 });
    done = await handle.jsonValue();
  } catch {
    done = 'nobody answered the port dialog within 3 minutes';
  }
  await page.evaluate(() => document.getElementById('hil-authorize')?.remove()).catch(() => {});
  if (done === 'ok') return;
  if (done.startsWith('wrong port')) {
    throw new Error(`authorizing ${ctx.com} for ${ctx.device}: ${done} — that grant was revoked; pick ${ctx.com}`);
  }
  test.skip(true, `${ctx.device} (${ctx.com}) was not authorized: ${done}. Run a wizard test with someone at ` +
                  `the keyboard and pick ${ctx.com} in Chrome's port dialog.`);
}

// Connect Wizard slot <wcb> to the harness's board through the Wizard's own picker, and wait for the config pull.
async function connectBoard(page, ctx) {
  const n = ctx.wcb;
  let matches = await grantedMatches(page, ctx);
  if (matches.length === 0) {
    await authorize(page, ctx);
    matches = await grantedMatches(page, ctx);
  }
  if (matches.length !== 1) {
    throw new Error(`${matches.length} granted ports in .profiles/${ctx.device} match ${ctx.device}'s VID/PID — ` +
                    `delete that folder and let the next run authorize ${ctx.com} again`);
  }
  // boardManualConnect() awaits the picker, then connects and schedules the pull; don't await it here.
  page.evaluate((n) => { boardManualConnect(n); }, n).catch(() => {});
  await page.locator('#port-picker-modal.open .port-picker-item').nth(matches[0]).click();
  await page.waitForFunction((n) => boardBaselines[n] && !_boardPullInFlight.has(n), n, { timeout: 45_000 });
  const got = await page.evaluate((n) => boardBaselines[n].wcbNumber, n);
  if (got !== n) throw new Error(`connected to WCB ${got}, expected WCB ${n} on ${ctx.com}`);
  return n;
}

// Set a field the way a user would when it is on screen; otherwise set it and fire the handlers the Wizard
// listens on (simple mode hides some sections, and those handlers are what a user's edit reaches anyway).
async function setField(page, selector, value) {
  const el = page.locator(selector);
  if (await el.isVisible()) {
    await el.fill(value);
    await el.press('Tab');
    return;
  }
  await el.evaluate((node, value) => {
    node.value = value;
    node.dispatchEvent(new Event('input', { bubbles: true }));
    node.dispatchEvent(new Event('change', { bubbles: true }));
  }, value);
}

// Click Push Config and return boardGo()'s recorded outcome (boardPushOutcome in Wizard/app.js). Waits on boardGo's
// own promise: polling the outcome is racy, because boardGo writes a provisional one before it marks the push as
// in flight. The button's onclick looks boardGo up globally at click time, so the wrapper sees the real click.
async function pushConfig(page, n) {
  await page.evaluate(() => {
    if (!window.__hilBoardGo) {
      window.__hilBoardGo = boardGo;
      window.boardGo = (...a) => (window.__hilPush = window.__hilBoardGo(...a));
    }
    window.__hilPush = null;
  });
  await page.locator(`#b${n}-btn-go`).click();
  await page.waitForFunction(() => window.__hilPush, null, { timeout: 10_000 });
  return page.evaluate(async (n) => { await window.__hilPush; return boardPushOutcome[n]; }, n);
}

// connectBoard's board as a DIRECT connection. boardManualConnect makes the first board of a page the shared-hub port
// (establishConnection's auto-share), which some paths treat differently - a reboot push stays on the port instead
// of closing and reopening it (boardGo) - so a test of the direct path connects through here: allowShare=false, as the
// bulk auto-detect does. The open asserts DTR, which resets the board, so the pull waits 3 s as boardManualConnect's
// does, and once more on an incomplete answer.
async function connectBoardDirect(page, ctx) {
  const n = ctx.wcb;
  let matches = await grantedMatches(page, ctx);
  if (matches.length === 0) {
    await authorize(page, ctx);
    matches = await grantedMatches(page, ctx);
  }
  if (matches.length !== 1) {
    throw new Error(`${matches.length} granted ports in .profiles/${ctx.device} match ${ctx.device}'s VID/PID`);
  }
  await page.evaluate(async ({ n, idx }) => {
    const port = (await navigator.serial.getPorts())[idx];
    await establishConnection(n, port, new Set(), false);
    delete remoteRelayForBoard[n];
    updateConnectionUI(n, true);
    for (let i = 0; i < 2 && !boardBaselines[n]; i++) {
      await new Promise((r) => setTimeout(r, 3000));
      await boardPull(n);
    }
  }, { n, idx: matches[0] });
  await page.waitForFunction((n) => boardBaselines[n] && !_boardPullInFlight.has(n), n, { timeout: 45_000 });
  const got = await page.evaluate((n) => [boardBaselines[n].wcbNumber, !!boardConnections[n]?._shared], n);
  if (got[0] !== n) throw new Error(`connected to WCB ${got[0]}, expected WCB ${n} on ${ctx.com}`);
  if (got[1]) throw new Error('the connection came up shared, not direct');
  return n;
}

// Manage WCB<target> through the relay in slot <relay> and pull its config, as "Manage via relay" does: resolves true
// once remoteBoardPull's onComplete says the pull landed (it retries on its own, within PULL_DEADLINE_MS).
function manageRemote(page, relay, target) {
  return page.evaluate(({ relay, target }) => new Promise((resolve) => {
    if (!document.getElementById(`section-board-${target}`)) addDiscoveredBoards([target]);
    setRemoteConnected(target, relay);
    const guard = setTimeout(() => resolve(false), 55_000);
    const r = remoteBoardPull(relay, target, 1, 3, (ok) => { clearTimeout(guard); resolve(ok); });
    if (r && typeof r.catch === 'function') r.catch(() => {});
  }), { relay, target });
}

// Keep every line slot <n>'s connection hears, with its time, in the page. Nothing is returned wholesale: a board's
// output includes its config (and so its passwords) on every pull, so a spec reads only the lines it matches.
async function recordLines(page, n) {
  await page.evaluate((n) => {
    window.__lines = window.__lines || {};
    const rec = (window.__lines[n] = []);
    boardConnections[n].onData((line) => rec.push({ at: Date.now(), line }));
  }, n);
}
// The recorded lines of slot <n> matching `re` (a RegExp source), from index `since` on; with their times.
function linesMatching(page, n, re, since = 0) {
  return page.evaluate(({ n, re, since }) => (window.__lines?.[n] || []).slice(since)
    .filter((x) => new RegExp(re).test(x.line)).map((x) => ({ at: x.at, line: x.line })), { n, re, since });
}
function lineMark(page, n) {
  return page.evaluate((n) => (window.__lines?.[n] || []).length, n);
}

// What a push of slot <n> would send, as its commands' verbs (LABEL, HW, ...), without sending anything: boardGo's and
// boardGoRemote's own preparation (the sync*ToConfig reads, autoComputeKyberTargets, the General inputs) on a copy of the
// config, then buildCommandString against the baseline. A bench spec checks this before it pushes, so an edit that
// would carry anything it did not intend - another board's WCB quantity (W-16), a Kyber line (W-20) - stops the test
// before a real board is written. Verbs only: a command's value can be a password.
function plannedVerbs(page, n) {
  return page.evaluate((n) => {
    syncSerialUIToConfig(n); syncMaestrosToConfig(n); syncKyberToConfig(n); syncMP3ToConfig(n);
    syncHCRToConfig(n); syncDFPToConfig(n); syncWLEDsToConfig(n); autoComputeKyberTargets(n);
    const c = JSON.parse(JSON.stringify(boardConfigs[n]));
    c.sequences = getSequencesFromUI(n);
    c.variables = getVariablesFromUI(n) ?? c.variables;
    const g = (id) => document.getElementById(id);
    c.espnowPassword = g('g-password').value || 'change_me_or_risk_takeover';
    c.macOctet2 = g('g-mac2').value?.toUpperCase() || '00';
    c.macOctet3 = g('g-mac3').value?.toUpperCase() || '00';
    c.meshChannel = parseInt(g('g-meshch')?.value) || 1;
    c.delimiter = g('g-delimiter').value || '^';
    c.funcChar = g('g-funcchar').value || '?';
    c.cmdChar = g('g-cmdchar').value || ';';
    c.wcbQuantity = parseInt(g('g-wcbq').value) || 1;
    const s = WCBParser.buildCommandString(c, boardBaselines[n] ?? null, !boardBaselines[n]);
    return s ? s.split(c.delimiter + c.funcChar).map((x, i) => (i === 0 ? x.slice(1) : x).split(',')[0]) : [];
  }, n);
}

module.exports = { openWizard, connectBoard, connectBoardDirect, manageRemote, recordLines, linesMatching, lineMark,
                   plannedVerbs, setField, pushConfig };
