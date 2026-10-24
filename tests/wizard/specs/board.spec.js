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

// Wizard/app.js seqStoreBytes against the board's own figure for its sequence store (?NVS, read by the harness).
test('wizard.seq_bytes_match_board the pager shows the bytes the board reports for its sequence store', async ({ page, hilCtx }) => {
  const { count, bytes, cap } = hilCtx.args;
  await openWizard(page);
  const n = await connectBoard(page, hilCtx);
  const fmt = (v) => v.toLocaleString('en-US');
  await expect(page.locator(`#b${n}-seq-bytes`), 'the pager\'s byte figure')
    .toHaveText(`· ${fmt(bytes)} of ${fmt(cap)} bytes`, { timeout: 15_000 });
  expect(await page.evaluate((n) => getSequencesFromUI(n).length, n), 'sequences in the page').toBe(count);
  expect(page.wizErrors).toEqual([]);
});

// A full sequence store pulled over USB (s30 _pull_full_store): the harness fills W1's store and passes the throwaway
// keys with their value lengths, the store's count and bytes. Never a value: only lengths are compared.
async function expectFullStorePulled(page, hilCtx) {
  const { seqs, count, bytes, cap } = hilCtx.args;
  await openWizard(page);
  const t0 = Date.now();
  const n = await connectBoard(page, hilCtx);
  const ms = Date.now() - t0;
  const got = await page.evaluate(({ n, keys }) => {
    const want = new Set(keys), lens = {};
    for (const s of boardBaselines[n]?.sequences || []) if (want.has(s.key)) lens[s.key] = String(s.value).length;
    return { lens, inPage: getSequencesFromUI(n).length, baseline: (boardBaselines[n]?.sequences || []).length };
  }, { n, keys: Object.keys(seqs) });
  expect(got.lens, 'every throwaway sequence in the pulled config, whole').toEqual(seqs);
  expect(got.baseline, 'the pulled config holds every sequence on the board').toBe(count);
  expect(got.inPage, 'every sequence in the page').toBe(count);
  const fmt = (v) => v.toLocaleString('en-US');
  await expect(page.locator(`#b${n}-seq-pager .seq-pager-info`), 'the pager counts them all').toContainText(`of ${count} sequences`);
  await expect(page.locator(`#b${n}-seq-bytes`), 'the pager\'s byte figure').toHaveText(`· ${fmt(bytes)} of ${fmt(cap)} bytes`);
  expect(page.wizErrors).toEqual([]);
  await hil.note(`pulled ${count} sequences (${fmt(bytes)} bytes) over USB in ${(ms / 1000).toFixed(1)} s`);
}

test('wizard.pull_full_store pulls a board whose sequence store is full of long sequences over USB', async ({ page, hilCtx }) => {
  await expectFullStorePulled(page, hilCtx);
});

test('wizard.pull_many_sequences pulls a board whose sequence store is full of the shortest sequences over USB', async ({ page, hilCtx }) => {
  await expectFullStorePulled(page, hilCtx);
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
