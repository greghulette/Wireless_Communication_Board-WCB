// Driving the NaviCore config tool (NaviCore/config_tool/index.html). It is classic scripts with top-level let/const
// state (config, _configBaseline, viaWcbActive, _pendingSaveBaseline ...), which page.evaluate reaches by name, the
// same way lib/wizard.js reaches the Wizard's.
//
// Rules every spec follows (docs/hil_plan/NAVICORE.md §5.3, D-NC5):
//   - nothing reads #terminal-output wholesale: it holds the CONFIG echo, which carries every password field. Use
//     termLines(page, re), which returns only the lines a pattern picks and never a CONFIG line.
//   - every spec ends with expectNoPageErrors(page).
const { expect } = require('@playwright/test');

const TOOL_PATH = '/NaviCore/config_tool/index.html';

// Open the tool with the HTTP cache off (the openWizard lesson: a cached script once ran against code no longer on
// disk), optionally on Playwright's fake clock (installed before the page's scripts, time still flowing), and wait
// for the startup command-library load so no spec races it.
async function openTool(page, { clock = true, path = TOOL_PATH } = {}) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
  if (clock) await page.clock.install();
  await page.goto(path);
  await page.waitForFunction(() => typeof openPortAndStart === 'function' && typeof handleBoardMessage === 'function');
  await page.evaluate(() => _cmdlibReady);
}

// Connect the way a user does: Connect -> "Connect via USB" in the chooser -> the fake port is granted -> the
// tool's own openPortAndStart(p, 4000) handshake. The 4 s settle is skipped with the fake clock (fastForward), not
// by calling a shorter path. Resolves once the handshake's last step (SET_DEBUG_FLAGS) went out and, when the
// device answers, CONFIG was applied.
// With { handshake: false } it only waits for the tool's own end of the handshake (isMonitoring: START_MONITOR has
// gone out, on whichever transport the tool decided on), for specs about a misdetected transport.
async function connectUsb(page, emu, { expectConfig = true, settle = true, handshake = true } = {}) {
  await page.locator('#btn-connect').click();
  await expect(page.locator('#connect-modal')).toHaveClass(/open/);
  await page.locator('#connect-modal .connect-opt').first().click();
  await page.waitForFunction(() => window.__hilSerial.isOpen());
  if (settle) await page.clock.fastForward(4000);
  if (!handshake) {
    await page.waitForFunction(() => isMonitoring === true, null, { timeout: 20_000 });
    return;
  }
  await expect.poll(() => emu.requests('SET_DEBUG_FLAGS').length + (emu.mode === 'via-wcb' ? 1 : 0),
                    { timeout: 20_000 }).toBeGreaterThan(0);
  if (expectConfig) await page.waitForFunction(() => _configLoaded === true, null, { timeout: 20_000 });
}

// Connect "Via a WCB": the chooser's second option -> connectViaWcbOpt -> connectSharedPort, which elects this tab
// the WcbSerialHub leader (Web Locks + BroadcastChannel, config_tool/serial-hub.js) over the fake port and handshakes
// through it with ;w20,-wrapped JSON. Resolves once the bridged CONFIG was reassembled and applied.
async function connectViaWcb(page, { expectConfig = true } = {}) {
  await page.locator('#btn-connect').click();
  await expect(page.locator('#connect-modal')).toHaveClass(/open/);
  await page.locator('#connect-modal .connect-opt').nth(1).click();
  await page.waitForFunction(() => sharedActive && viaWcbActive, null, { timeout: 20_000 });
  if (expectConfig) await page.waitForFunction(() => _configLoaded === true, null, { timeout: 20_000 });
}

// A shortcut for specs whose subject is not the handshake: the same openPortAndStart the post-flash reconnect uses,
// with a short settle, on the port the fake grants. Waits for the same end state as connectUsb.
async function connectFast(page, emu, { expectConfig = true } = {}) {
  await page.evaluate(async () => {
    const p = await navigator.serial.requestPort();
    window.__hilConnect = openPortAndStart(p, 20);
  });
  await page.evaluate(() => window.__hilConnect);
  if (expectConfig) await page.waitForFunction(() => _configLoaded === true, null, { timeout: 20_000 });
  await expect.poll(() => emu.rx.length).toBeGreaterThan(0);
}

// Tool state, by name.
function state(page) {
  return page.evaluate(() => ({
    viaWcbActive, sharedActive, configLoaded: _configLoaded, connected: !!port, isMonitoring,
    status: document.getElementById('status-text').textContent,
    connectLabel: document.getElementById('btn-connect').textContent,
    pendingSave: !!_pendingSaveBaseline,
  }));
}

// The terminal lines a pattern picks — never a CONFIG echo (D-NC5: it carries the password fields).
function termLines(page, re, pane = 'main') {
  return page.evaluate(({ src, flags, pane }) => {
    const out = document.getElementById(pane === 'wcb' ? 'terminal-output-wcb' : 'terminal-output');
    if (!out) return [];
    const r = new RegExp(src, flags);
    return [...out.children].map((d) => d.textContent)
      .filter((t) => !/"type"\s*:\s*"CONFIG"/.test(t) && !/password/i.test(t))
      .filter((t) => r.test(t));
  }, { src: re.source, flags: re.flags, pane });
}

// Visible toast texts (showToast builds them in #toast-host).
function toasts(page) {
  return page.evaluate(() => [...(document.getElementById('toast-host')?.children || [])].map((t) => t.textContent));
}

// Wait until the emulator has seen `n` requests of a type (the tool's writes are async).
async function waitRequests(emu, type, n = 1, timeout = 10_000) {
  await expect.poll(() => emu.requests(type).length, { timeout, message: `waiting for ${n} x ${type}` }).toBeGreaterThanOrEqual(n);
  return emu.requests(type);
}

function expectNoPageErrors(page) {
  expect(page.ncErrors, 'uncaught page errors').toEqual([]);
}

// Record every confirm()/alert()/prompt() and answer it: `answers` is a list of booleans / strings consumed in order
// (default: dismiss). Returns the list of dialogs seen, filled in as they happen.
function answerDialogs(page, answers = []) {
  const seen = [];
  page.on('dialog', async (d) => {
    const a = answers.length ? answers.shift() : false;
    seen.push({ type: d.type(), message: d.message(), answer: a });
    if (d.type() === 'alert') await d.accept();
    else if (a === false) await d.dismiss();
    else if (typeof a === 'string') await d.accept(a);
    else await d.accept();
  });
  return seen;
}

// The fw buttons' enabled state (the transport gate, _updateFirmwareBtnState).
function fwButtons(page) {
  return page.evaluate(() => Object.fromEntries(['btn-fw-flash', 'btn-fw-wipe', 'btn-fw-ota', 'btn-fw-ota-wcb']
    .map((id) => [id, !document.getElementById(id).disabled])));
}

module.exports = {
  TOOL_PATH, openTool, connectUsb, connectViaWcb, connectFast, state, termLines, toasts, waitRequests,
  expectNoPageErrors, answerDialogs, fwButtons,
};
