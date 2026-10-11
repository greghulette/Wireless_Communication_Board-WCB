// The terminal's input side (docs/hil_plan/NAVICORE.md §2, L1): a typed line goes to the board as is over USB and
// ;w20,-wrapped over the bridge with the clean echo, the bridge WCB gets its own column and raw lines, the history keys,
// the timestamps and auto-scroll toggles, Clear and Close (initTerminal, config_tool/index.html ~5995-6070). The
// receive side - framing, markers, the debug chips - is nctool.rx_framing_markers.
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const input = (page) => page.locator('#terminal-input');
const paneLines = (page, pane = 'main') => page.locator(pane === 'wcb' ? '#terminal-output-wcb' : '#terminal-output').evaluate((e) => e.childElementCount);

test.describe('the terminal over Direct USB', () => {
  test.use({ emuOptions: { config: BENCH } });

  test('nctool.terminal_input the terminal opens from the toolbar and its input is disabled until a board is connected; a line typed and entered goes to the board as is and is echoed, the reply shows; blanks send nothing; Up/Down walk the history (no consecutive duplicates); Timestamps and Auto-scroll toggle and are remembered; Clear empties the pane; Close closes it', async ({ page, emu }) => {
    await T.openTool(page);
    await expect(page.locator('#terminal-panel')).not.toHaveClass(/open/);
    await page.locator('#btn-terminal').click();
    await expect(page.locator('#terminal-panel')).toHaveClass(/open/);
    await expect(page.locator('body')).toHaveClass(/terminal-open/);
    await expect(input(page)).toBeDisabled();
    await expect(page.locator('#terminal-send')).toBeDisabled();
    await T.connectUsb(page, emu);
    await expect(input(page)).toBeEnabled();
    const sent = () => emu.lines(/^\?HIL/);
    // A line: sent as typed, echoed with '> ', answered.
    await input(page).fill('?HILTERM');
    await input(page).press('Enter');
    await expect.poll(sent).toEqual(['?HILTERM']);
    await expect(input(page)).toHaveValue('');
    expect(await T.termLines(page, /^> \?HILTERM$/)).toHaveLength(1);
    await expect.poll(() => T.termLines(page, /Unknown command: \?HILTERM$/)).toHaveLength(1);
    // Blank: nothing goes out.
    await input(page).fill('   ');
    await page.locator('#terminal-send').click();
    expect(sent()).toEqual(['?HILTERM']);
    // History: Up walks back, Down forward to the draft; a repeat of the last line is not stored twice.
    await input(page).fill('?HILTWO');
    await input(page).press('Enter');
    await input(page).fill('?HILTWO');
    await input(page).press('Enter');
    await expect.poll(sent).toEqual(['?HILTERM', '?HILTWO', '?HILTWO']);
    await input(page).fill('?HILdraft');
    await input(page).press('ArrowUp');
    await expect(input(page)).toHaveValue('?HILTWO');
    await input(page).press('ArrowUp');
    await expect(input(page)).toHaveValue('?HILTERM');
    await input(page).press('ArrowUp');
    await expect(input(page), 'the oldest line stays').toHaveValue('?HILTERM');
    await input(page).press('ArrowDown');
    await expect(input(page)).toHaveValue('?HILTWO');
    await input(page).press('ArrowDown');
    await expect(input(page), 'back to the draft').toHaveValue('?HILdraft');
    await input(page).fill('');
    // Timestamps prefix every later line; the setting is remembered. Auto-scroll toggles the same way.
    const ts = page.locator('#terminal-timestamps'), as = page.locator('#terminal-autoscroll');
    await expect(ts).not.toHaveClass(/active/);
    await ts.click();
    await expect(ts).toHaveClass(/active/);
    expect(await page.evaluate(() => localStorage.getItem('rcTerminalTimestamps'))).toBe('1');
    await input(page).fill('?HILTS');
    await input(page).press('Enter');
    await expect.poll(() => T.termLines(page, /^\[\d{1,2}:\d{2}:\d{2}[^\]]*\] > \?HILTS$/)).toHaveLength(1);
    await ts.click();
    await expect(ts).not.toHaveClass(/active/);
    expect(await page.evaluate(() => localStorage.getItem('rcTerminalTimestamps'))).toBe('0');
    await expect(as).toHaveClass(/active/);
    await as.click();
    await expect(as).not.toHaveClass(/active/);
    expect(await page.evaluate(() => localStorage.getItem('rcTerminalAutoScroll'))).toBe('0');
    await as.click();
    await expect(as).toHaveClass(/active/);
    // Clear, then Close.
    expect(await paneLines(page)).toBeGreaterThan(3);
    await page.locator('#terminal-clear').click();
    expect(await paneLines(page)).toBe(0);
    await page.locator('#terminal-collapse').click();
    await expect(page.locator('#terminal-panel')).not.toHaveClass(/open/);
    await expect(page.locator('body')).not.toHaveClass(/terminal-open/);
    T.expectNoPageErrors(page);
  });
});

test.describe('the terminal through a WCB', () => {
  test.use({ emuOptions: { config: BENCH, mode: 'via-wcb' } });

  test('nctool.terminal_input_via_wcb over the bridge a typed line is echoed clean and sent ;w20,-wrapped, and the [TERM:20] reply lands in the same pane; the WCB column, once shown, sends raw lines to the bridge WCB and shows its answers; Clear empties both panes', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectViaWcb(page);
    await page.locator('#btn-terminal').click();
    await expect(input(page)).toBeEnabled();
    await input(page).fill('?HILTERM');
    await input(page).press('Enter');
    await expect.poll(() => emu.lines(/HILTERM/)).toEqual([';w20,?HILTERM']);
    expect(await T.termLines(page, /^> \?HILTERM$/), 'the clean echo').toHaveLength(1);
    expect(await T.termLines(page, /;w20,\?HILTERM/), 'never the wrapped form of a typed line').toHaveLength(0);
    await expect.poll(() => T.termLines(page, /Unknown command: \?HILTERM$/)).toHaveLength(1);
    // The bridge WCB's own column.
    await expect(page.locator('#terminal-col-wcb')).toBeHidden();
    await page.locator('#term-vis-cb-wcb').check();
    await expect(page.locator('#terminal-col-wcb')).toBeVisible();
    const inputW = page.locator('#terminal-input-wcb');
    await inputW.fill('?HILWCB');
    await inputW.press('Enter');
    await expect.poll(() => emu.lines(/HILWCB/)).toEqual(['?HILWCB']);
    expect(await T.termLines(page, /^> \?HILWCB$/, 'wcb')).toHaveLength(1);
    await expect.poll(async () => (await T.termLines(page, /WCB: HILWCB ok/, 'wcb')).length + (await T.termLines(page, /WCB: HILWCB ok/)).length).toBe(1);
    await page.locator('#terminal-clear').click();
    expect([await paneLines(page), await paneLines(page, 'wcb')]).toEqual([0, 0]);
    T.expectNoPageErrors(page);
  });
});
