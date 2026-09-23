// Board tests. Each is started by the HIL harness test of the same id (tests/hil/suites/s30_wizard.py), which hands
// this board's COM port to Chrome and owns the before/after config check; hilCtx.args carries what it decided.
const { test, expect, hil } = require('../lib/fixtures');
const { openWizard, connectBoard, setField, pushConfig } = require('../lib/wizard');

test.beforeEach(() => {
  test.skip(!hil.present, 'board tests run under the HIL harness: python tests/hil/run.py "wizard.*"');
});

test('wizard.pull shows the saved bauds and labels of the board it pulled', async ({ page, hilCtx }) => {
  await openWizard(page);
  const n = await connectBoard(page, hilCtx);
  let checked = 0;
  for (const t of hilCtx.args.tokens) {
    let m = t.match(/^\?BAUD,S([1-5]),(\d+)$/i);
    if (m) {
      await expect(page.locator(`#b${n}-s${m[1]}-baud`), t).toHaveValue(m[2]);
      checked++;
    }
    m = t.match(/^\?LABEL,S([1-5]),(.*)$/i);
    if (m) {
      await expect(page.locator(`#b${n}-s${m[1]}-label`), t).toHaveValue(m[2]);
      checked++;
    }
  }
  expect(checked, 'the harness sent no ?BAUD / ?LABEL tokens to compare').toBeGreaterThan(0);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.push_label pushes a port label typed into the board card', async ({ page, hilCtx }) => {
  const { port, label } = hilCtx.args;
  await openWizard(page);
  const n = await connectBoard(page, hilCtx);
  await setField(page, `#b${n}-s${port}-label`, label);
  const outcome = await pushConfig(page, n);
  expect(outcome, JSON.stringify(outcome)).toMatchObject({ ok: true, aborted: false });
  expect(page.wizErrors).toEqual([]);
  // The harness reads ?backup once Chrome lets go of the port, and restores the label.
});

test('wizard.terminal_wire a command typed in the terminal reaches the probe', async ({ page, hilCtx }) => {
  const { wcb, port } = hilCtx.args;
  await openWizard(page);
  const n = await connectBoard(page, hilCtx);
  const text = `WIZ${Date.now().toString(36).toUpperCase()}`;
  const w = hil.wire(wcb, port);
  const since = await w.mark();
  await page.evaluate(({ n, cmd }) => {
    document.getElementById(`term-pane-input-${n}`).value = cmd;
    return sendTerminalCommandTo(n);
  }, { n, cmd: `;${port}${text}` });
  await w.expect(`${text}\r`, since, 4);
  expect(page.wizErrors).toEqual([]);
});
