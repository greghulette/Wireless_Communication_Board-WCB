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

module.exports = { openWizard, connectBoard, setField, pushConfig };
