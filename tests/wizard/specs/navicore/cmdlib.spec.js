// The Command Library picker (docs/hil_plan/NAVICORE.md §2, nct.cmdlib.catalog, nct.cmdlib.encode_decode,
// nct.cmdlib.custom_sync, nct.cmdlib.seq_source, L1): the list grouped and searched (_cmdlibRenderList,
// config_tool/index.html ~13929), the composer's fields and live preview (_cmdlibShowComposer ~14066), Use writing the
// row with the destination the command's routing implies (_cmdlibUse ~14286), a row's command re-opened for editing
// (ncDecodeCommand ~13678), the two refusals; a library file imported and kept private to the browser, saved to the
// NaviCore (SET_CMDLIB) and loaded back into a fresh browser (loadCmdlibFromBoard ~14662, saveCmdlibToBoard ~14855),
// and removed; a DroidNet manifest fetched from a URL with one board failing (_cmdlibImportFromManifest ~14940); and
// the stored-sequence dropdown fed by WCB_SEQ / WCB_SEQVAL with the body shown (_wcbSeqFieldHtml ~13108). The vendored
// catalog is served from the NaviCore checkout like the rest of the tool. The via-WCB bulk save is a bench item (the
// emulator has no bulk envelopes).
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');
const { fnv1a, extractData } = require('../../lib/navicore/emulator');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const modal = (page) => page.locator('#cmdlib-modal');
const item = (page, board, cmd) => page.locator(`.cmdlib-item[data-board="${board}"][data-cmd="${cmd}"]`);
const preview = (page) => page.locator('#cmdlib-preview');
const builder = (page, fid) => page.locator(`#${fid} button`, { hasText: /Command Builder|Change/ });
const customStore = (page) => page.evaluate(() => { const s = localStorage.getItem('nc_cmdlib_custom_v1'); return s ? JSON.parse(s).boards.map((b) => b.id) : null; });
const modalApply = (page) => page.locator('#modal .modal-footer .btn-success').click();
const rowAction = (page, fid) => page.evaluate((f) => {
  const a = readActionFromFid(f, document.getElementById(f.replace(/^fields-/, 'type-')).value);
  return JSON.parse(JSON.stringify(a));
}, fid);

// Mode 2, button 2 has no mapping on the bench: its editor is the row the picker writes into.
async function openUnmappedButton(page) {
  await page.evaluate(() => setEditingMode(2));
  await page.locator('#assignment-list > div').nth(1).click();
  await expect(page.locator('#modal-title')).toHaveText(/^Button 2:/);
}
async function search(page, text) {
  await page.locator('#cmdlib-search').fill(text);
}

const LIB = {
  schema: 'navicore-command-library/v1',
  enums: { 'hil.dir': { label: 'Direction', values: [{ code: 'L', label: 'Left' }, { code: 'R', label: 'Right' }] } },
  boards: [{ id: 'hil-custom', name: 'HIL Custom', commands: [
    { id: 'hil.go', name: 'HIL Go', template: ';S3HILGO,{dir},{n}', params: [{ name: 'dir', enum: 'hil.dir' }, { name: 'n', type: 'int', default: 3, pad: 2 }] }] }],
};

test.describe('the picker', () => {
  test.use({ emuOptions: { config: BENCH } });

  test('nctool.cmdlib_catalog_codec the picker opens from an action row with the catalog grouped and collapsed, search expands the matches and reports none; a WCB verb composes from its enums with a live preview and Back discards the draft; Use writes the row as a unicast to the device\'s configured WCB, a device string as a serial-port broadcast on the first WCB, a WLED verb as a WLED action and a NaviCore clip as a local play; Change re-opens a row\'s command decoded and keeps its destination; a custom-encoder command and an over-long one are refused; Apply saves what the picker wrote', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openUnmappedButton(page);
    await expect(modal(page)).not.toHaveClass(/open/);
    await builder(page, 'fields-1-0').click();
    await expect(modal(page)).toHaveClass(/open/);
    await expect(page.locator('#cmdlib-search')).toBeFocused();
    await expect(page.locator('#cmdlib-load-board')).toBeEnabled();
    await expect(page.locator('#cmdlib-save-board')).toBeEnabled();
    // Grouped: the WCB and AstroPixels clusters fold their libraries; everything starts collapsed.
    await expect(page.locator('#cmdlib-list > details.cmdlib-board > summary', { hasText: /\d+ libraries/ })).toHaveCount(2);
    await expect(page.locator('#cmdlib-list > details.cmdlib-board > summary', { hasText: /\bWCB\b[^·]*·\s*\d+ libraries/ })).toHaveCount(1);
    await expect(page.locator('#cmdlib-list > details.cmdlib-board > summary', { hasText: /AstroPixels[^·]*·\s*\d+ libraries/ })).toHaveCount(1);
    expect(await page.locator('#cmdlib-list details.cmdlib-board').count()).toBeGreaterThan(10);
    await expect(page.locator('#cmdlib-list details[open]')).toHaveCount(0);
    // Search opens what matches, across the groups; nothing matching says so.
    await search(page, 'stimulus');
    expect(await page.locator('#cmdlib-list details[open]').count()).toBeGreaterThan(0);
    await expect(item(page, 'wcb-hcr', 'hcr.stim')).toBeVisible();
    await expect(item(page, 'hcr-native', 'hcr.native.stimulus')).toBeVisible();
    await expect(page.locator('#cmdlib-list')).toContainText(/\d+ match(es)?/);
    await search(page, 'zzqqxx');
    await expect(page.locator('#cmdlib-list')).toHaveText(/No commands match\. Try Import to add a library\./);
    // The composer: enums seeded with their first code, the preview following every edit; Back discards.
    await search(page, 'Emotional Stimulus');
    await item(page, 'wcb-hcr', 'hcr.stim').click();
    await expect(page.locator('#cmdlib-body')).toContainText('Emotional Stimulus');
    await expect(page.locator('#cmdlib-body')).toContainText('WCB · HCR Vocalizer');
    await expect(page.locator('#cmdlib-p-0')).toHaveValue('H');
    await expect(page.locator('#cmdlib-p-0 option:checked')).toHaveText('Happy (H)');
    await expect(page.locator('#cmdlib-p-1')).toHaveValue('MOD');
    await expect(preview(page)).toHaveText(';H,STIM,H,MOD');
    await page.locator('#cmdlib-p-0').selectOption('S');
    await expect(preview(page)).toHaveText(';H,STIM,S,MOD');
    await page.locator('#cmdlib-p-1').selectOption('STRONG');
    await expect(preview(page)).toHaveText(';H,STIM,S,STRONG');
    await page.locator('#cmdlib-back').click();
    await expect(page.locator('#cmdlib-search')).toHaveValue('');
    await search(page, 'Emotional Stimulus');
    await item(page, 'wcb-hcr', 'hcr.stim').click();
    await expect(preview(page), 'Back dropped the draft').toHaveText(';H,STIM,H,MOD');
    await page.locator('#cmdlib-p-0').selectOption('S');
    await page.locator('#cmdlib-p-1').selectOption('STRONG');
    // Use: an HCR verb goes to the HCR's configured WCB (hcrDest target 2), as a unicast.
    expect(BENCH.hcrDest).toMatchObject({ transport: 'wcb', target: '2' });
    await page.locator('#cmdlib-use').click();
    await expect(modal(page)).not.toHaveClass(/open/);
    await expect(page.locator('#type-1-0')).toHaveValue('wcb_unicast');
    await expect(page.locator('#fields-1-0-cmd')).toHaveValue(';H,STIM,S,STRONG');
    await expect(page.locator('#fields-1-0-destsel')).toHaveValue('2');
    expect(await rowAction(page, 'fields-1-0')).toMatchObject({ type: 'wcb_unicast', target: '2', cmd: ';H,STIM,S,STRONG' });
    // Change: the row's command comes back decoded into the composer, and the destination the row holds is kept (the
    // row's Send-to is its WCB + port selects, synced into the hidden -destsel).
    await page.locator('#fields-1-0-wcbsel').selectOption('0');
    await expect(page.locator('#fields-1-0-destsel')).toHaveValue('0');
    await builder(page, 'fields-1-0').click();
    await expect(page.locator('#cmdlib-p-0')).toHaveValue('S');
    await expect(page.locator('#cmdlib-p-1')).toHaveValue('STRONG');
    await page.locator('#cmdlib-p-0').selectOption('M');
    await page.locator('#cmdlib-use').click();
    await expect(page.locator('#type-1-0')).toHaveValue('wcb_broadcast');
    await expect(page.locator('#fields-1-0-cmd')).toHaveValue(';H,STIM,M,STRONG');
    expect(await rowAction(page, 'fields-1-0')).toMatchObject({ type: 'wcb_broadcast', cmd: ';H,STIM,M,STRONG' });
    // A device-native string (no WCB routing) goes out the first WCB's Serial 1.
    await page.locator('#tier-card-1 .btn-add-action').click();
    await builder(page, 'fields-1-1').click();
    await search(page, 'Emotional Stimulus');
    await item(page, 'hcr-native', 'hcr.native.stimulus').click();
    const intensity = await page.evaluate(() => _cmdlibEnum('hcr.intensity').values[0].code);
    await expect(preview(page)).toHaveText(`<SH${intensity}>`);
    await page.locator('#cmdlib-use').click();
    await expect(page.locator('#type-1-1')).toHaveValue('wcb_broadcast');
    expect(await rowAction(page, 'fields-1-1')).toMatchObject({ type: 'wcb_broadcast', cmd: `;W1;S1<SH${intensity}>` });
    // A WLED verb is a WLED action (routed by id in the WLED tab).
    await page.locator('#tier-card-1 .btn-add-action').click();
    await builder(page, 'fields-1-2').click();
    await search(page, 'Lights on');
    await item(page, 'wcb-wled', 'wled.on').click();
    await expect(preview(page)).toHaveText(';L1,ON');
    await page.locator('#cmdlib-p-0').fill('3');
    await expect(preview(page)).toHaveText(';L3,ON');
    await page.locator('#cmdlib-use').click();
    await expect(page.locator('#type-1-2')).toHaveValue('wled');
    expect(await rowAction(page, 'fields-1-2')).toMatchObject({ type: 'wled', cmd: ';L3,ON' });
    // A NaviCore clip: a local play with its loop flag, no destination; Change pre-fills from the row; Cancel keeps it.
    await page.locator('#tier-card-1 .btn-add-action').click();
    await builder(page, 'fields-1-3').click();
    await search(page, 'Play clip');
    await item(page, 'navicore', 'nc.play').click();
    await expect(page.locator('#cmdlib-body')).toContainText('Runs on this controller');
    await expect(preview(page)).toHaveCount(0);
    await page.locator('#cmdlib-p-0').fill('dome_spin');
    await page.locator('#cmdlib-p-1').check();
    await page.locator('#cmdlib-use').click();
    await expect(page.locator('#type-1-3')).toHaveValue('play');
    await expect(page.locator('#fields-1-3-clip')).toHaveValue('dome_spin');
    await expect(page.locator('#fields-1-3-loop')).toBeChecked();
    await builder(page, 'fields-1-3').click();
    await expect(page.locator('#cmdlib-p-0')).toHaveValue('dome_spin');
    await expect(page.locator('#cmdlib-p-1')).toBeChecked();
    await page.locator('#cmdlib-cancel2').click();
    await expect(modal(page)).not.toHaveClass(/open/);
    await expect(page.locator('#fields-1-3-clip')).toHaveValue('dome_spin');
    // Refused: a command the catalog encodes with a custom encoder this tool lacks, and one over the 95-char store.
    await page.locator('#tier-card-1 .btn-add-action').click();
    await builder(page, 'fields-1-4').click();
    await search(page, 'Logic Effect');
    await item(page, 'rseries-logic', 'rseries.effect').click();
    await page.locator('#cmdlib-use').click();
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual(expect.stringContaining('cannot be built here yet'));
    await expect(modal(page)).toHaveClass(/open/);
    await page.locator('#cmdlib-back').click();
    await search(page, 'Set color');
    await item(page, 'wcb-wled', 'wled.color').click();
    await page.evaluate(() => { _cmdlibValues.color = 'F'.repeat(100); _cmdlibUpdatePreview(); });
    await page.locator('#cmdlib-use').click();
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual(expect.stringMatching(/That command is 1\d\d characters — the board stores only 95/));
    await expect(modal(page)).toHaveClass(/open/);
    await page.locator('#cmdlib-close').click();
    await expect(modal(page)).not.toHaveClass(/open/);
    await expect(page.locator('#fields-1-4-cmd')).toHaveValue('');
    // Apply: the four rows the picker wrote are the mapping saved (the empty fifth is not).
    await modalApply(page);
    const [sent] = await T.waitRequests(emu, 'SET_CONFIG');
    expect(sent.data.mappings['202'].t1).toEqual([
      { type: 'wcb_broadcast', cmd: ';H,STIM,M,STRONG' },
      { type: 'wcb_broadcast', cmd: `;W1;S1<SH${intensity}>` },
      { type: 'wled', cmd: ';L3,ON' },
      { type: 'play', cmd: 'dome_spin', fn: 1 },
    ]);
    T.expectNoPageErrors(page);
  });

  test('nctool.cmdlib_sync Save with nothing custom is refused; a library file imported shows as a private board with its enum, persists in the browser and composes; Save to NaviCore sends SET_CMDLIB with sys first and the store as data, the ACK\'s hash becomes the browser\'s droid signature; a fresh browser hears the NaviCore has a different library, Load from NaviCore brings the board back as custom and the next connect is in sync; the board\'s ✕ removes it after a confirm', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page, [true, true]);   // the alert, then the remove confirm
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await expect.poll(() => T.termLines(page, /^< \[cmdlib\] NaviCore has no stored library$/)).toHaveLength(1);
    await openUnmappedButton(page);
    await builder(page, 'fields-1-0').click();
    await page.locator('#cmdlib-save-board').click();
    await expect.poll(() => dialogs.map((d) => d.message)).toContainEqual(expect.stringContaining('No custom boards to save yet'));
    expect(emu.requests('SET_CMDLIB')).toEqual([]);
    // Import: the board lands as "custom" with its ✕, in localStorage, and composes with the enum and the padded int.
    await T.chooseFile(page, () => page.locator('#cmdlib-import').click(), 'hil-lib.json', JSON.stringify(LIB));
    await expect(page.locator('#cmdlib-list details.cmdlib-board summary', { hasText: 'HIL Custom' })).toContainText('· custom');
    await expect(page.locator('.cmdlib-remove-custom[data-board="hil-custom"]')).toHaveCount(1);
    expect(await customStore(page)).toEqual(['hil-custom']);
    await search(page, 'HIL Go');
    await item(page, 'hil-custom', 'hil.go').click();
    await expect(page.locator('#cmdlib-p-0 option:checked')).toHaveText('Left (L)');
    await expect(preview(page)).toHaveText(';S3HILGO,L,03');
    await page.locator('#cmdlib-p-0').selectOption('R');
    await page.locator('#cmdlib-p-1').fill('7');
    await expect(preview(page)).toHaveText(';S3HILGO,R,07');
    await page.locator('#cmdlib-back').click();
    // Save to NaviCore: one SET_CMDLIB, sys first, the whole private store as data; the ACK's hash is remembered.
    await page.locator('#cmdlib-save-board').click();
    await expect.poll(() => emu.lines(/"type":"SET_CMDLIB"/)).toHaveLength(1);
    const line = emu.lines(/"type":"SET_CMDLIB"/)[0];
    expect(line.startsWith('{"sys":1,"type":"SET_CMDLIB","data":')).toBe(true);
    const data = extractData(line);
    expect(JSON.parse(data)).toEqual(LIB);
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('Command library saved to NaviCore'));
    expect(emu.cmdlib).toBe(data);
    expect(await page.evaluate(() => localStorage.getItem('nc_cmdlib_droid_sig'))).toBe(String(fnv1a(data)));
    expect(await T.termLines(page, /\[cmdlib\] ✓ saved to the NaviCore/)).toHaveLength(1);
    // A fresh browser: nothing private, the NaviCore holds the library; Load from NaviCore brings it back.
    await page.evaluate(() => localStorage.clear());
    await T.openTool(page, { clock: false });
    await T.connectUsb(page, emu);
    await expect.poll(() => T.termLines(page, /^< \[cmdlib\] NaviCore has a different library \(\d+ KB\)/)).toHaveLength(1);
    await openUnmappedButton(page);
    await builder(page, 'fields-1-0').click();
    await search(page, 'HIL Go');
    await expect(item(page, 'hil-custom', 'hil.go')).toHaveCount(0);
    await search(page, '');
    await page.locator('#cmdlib-load-board').click();
    await T.waitRequests(emu, 'GET_CMDLIB');
    await expect.poll(() => T.termLines(page, /^< \[cmdlib\] loaded 1 custom board\(s\) from the NaviCore$/)).toHaveLength(1);
    await expect(page.locator('#cmdlib-list details.cmdlib-board summary', { hasText: 'HIL Custom' })).toContainText('· custom');
    expect(await customStore(page)).toEqual(['hil-custom']);
    expect(await page.evaluate(() => localStorage.getItem('nc_cmdlib_droid_sig'))).toBe(String(fnv1a(data)));
    await page.locator('#cmdlib-close').click();
    await T.openTool(page, { clock: false });
    await T.connectUsb(page, emu);
    await expect.poll(() => T.termLines(page, /^< \[cmdlib\] in sync with the NaviCore$/)).toHaveLength(1);
    // ✕ on the private board: a confirm, then it is gone from the list and the browser (the NaviCore keeps its copy).
    await openUnmappedButton(page);
    await builder(page, 'fields-1-0').click();
    await page.locator('.cmdlib-remove-custom[data-board="hil-custom"]').click();
    await expect.poll(() => dialogs.map((d) => d.type + ':' + d.message)).toContainEqual(expect.stringMatching(/^confirm:Remove your private "HIL Custom" board/));
    await expect(page.locator('#cmdlib-list details.cmdlib-board summary', { hasText: 'HIL Custom' })).toHaveCount(0);
    expect(await customStore(page)).toEqual([]);
    expect(emu.cmdlib).toBe(data);
    T.expectNoPageErrors(page);
  });

  test('nctool.cmdlib_fetch unconnected, Load and Save to the NaviCore are disabled with a reason; Fetch reads a DroidNet manifest from the URL typed, pulls each board file relative to it, logs each one and the count, keeps going past a board that fails, and the boards fetched are session-only (not private, not persisted) but compose and Use; a manifest without boards is reported', async ({ page }) => {
    const BASE = 'https://hil.test/lib/';
    const manifest = { boards: [{ id: 'hil-fetched', name: 'HIL Fetched', file: 'boards/hil-fetched.json' }, { id: 'hil-missing', name: 'HIL Missing', file: 'boards/missing.json' }] };
    const board = { $schema: 'droidnet-command-library/library/v1', components: [{ id: 'hil-fetched', name: 'HIL Fetched', commands: [{ id: 'hil.f', name: 'HIL Fetched Cmd', template: ';S4HILF' }] }] };
    const hits = [];
    await page.route((url) => url.href.startsWith('https://hil.test/'), async (route) => {
      const url = route.request().url();
      hits.push(url);
      const json = (body) => route.fulfill({ status: 200, contentType: 'application/json', headers: { 'access-control-allow-origin': '*' }, body: JSON.stringify(body) });
      if (url === BASE + 'manifest.json') return json(manifest);
      if (url === BASE + 'boards/hil-fetched.json') return json(board);
      if (url === 'https://hil.test/bad/manifest.json') return json({ hello: 1 });
      return route.fulfill({ status: 404, headers: { 'access-control-allow-origin': '*' }, body: 'no' });
    });
    await T.openTool(page);
    await openUnmappedButton(page);
    await builder(page, 'fields-1-0').click();
    await expect(page.locator('#cmdlib-load-board')).toBeDisabled();
    await expect(page.locator('#cmdlib-load-board')).toHaveAttribute('title', 'Connect to a NaviCore first to load its stored library');
    await expect(page.locator('#cmdlib-save-board')).toBeDisabled();
    await expect(page.locator('#cmdlib-manifest-url')).toHaveValue(/^https:\/\/raw\.githubusercontent\.com\/travisccook\/Droidnet-Command-Library\/.*\/manifest\.json$/);
    // The online check is folded away (a full library is already loaded): open it.
    await expect(page.locator('#cmdlib-manifest-url')).toBeHidden();
    await page.locator('#cmdlib-body details > summary', { hasText: 'Check online for a newer library' }).click();
    await page.locator('#cmdlib-manifest-url').fill(BASE + 'manifest.json');
    await page.locator('#cmdlib-fetch').click();
    await expect(page.locator('#cmdlib-fetchlog')).toHaveText(/Done — 1 board\(s\) imported, 1 failed\./);
    expect((await page.locator('#cmdlib-fetchlog').textContent()).split('\n')).toEqual([
      'Fetching manifest…', 'Manifest OK — 2 board file(s) listed. Fetching…', '  ✓ HIL Fetched', '  ✗ HIL Missing — HTTP 404', 'Done — 1 board(s) imported, 1 failed.']);
    expect(hits).toEqual([BASE + 'manifest.json', BASE + 'boards/hil-fetched.json', BASE + 'boards/missing.json']);
    await expect(page.locator('#cmdlib-fetch')).toBeEnabled();
    await expect(page.locator('#cmdlib-fetch')).toHaveText('Fetch');
    const summary = page.locator('#cmdlib-list details.cmdlib-board summary', { hasText: 'HIL Fetched' });
    await expect(summary).toHaveCount(1);
    await expect(summary, 'fetched, not private').not.toContainText('custom');
    expect(await customStore(page)).toBeNull();
    await search(page, 'HIL Fetched Cmd');
    await item(page, 'hil-fetched', 'hil.f').click();
    await expect(preview(page)).toHaveText(';S4HILF');
    await page.locator('#cmdlib-use').click();
    await expect(page.locator('#fields-1-0-cmd')).toHaveValue(';S4HILF');
    expect(await rowAction(page, 'fields-1-0')).toMatchObject({ type: 'wcb_unicast', target: '1', cmd: ';S4HILF' });
    // A manifest with no boards[].
    await builder(page, 'fields-1-0').click();
    await page.locator('#cmdlib-back').click();
    await page.locator('#cmdlib-body details > summary', { hasText: 'Check online for a newer library' }).click();
    await page.locator('#cmdlib-manifest-url').fill('https://hil.test/bad/manifest.json');
    await page.locator('#cmdlib-fetch').click();
    await expect(page.locator('#cmdlib-fetchlog')).toHaveText(/✗ Manifest has no boards\[\] — is this a DroidNet-shaped manifest URL\?/);
    T.expectNoPageErrors(page);
  });
});

test.describe('the stored-sequence source', () => {
  // WCB 1 online: _wcbSeqBoards asks only boards the 3 s status poll reports online, so without it the ⟳ re-read
  // asked no one whenever the poll had landed first (a race the spec lost when it ran slower).
  test.use({ emuOptions: { config: BENCH, roster: { online: [1], known: [1] }, seqs: { 1: { wave: ';A,PLAY,1^;H,STIM,H,MOD *** says hello', spin: ';M11' } } } });

  test('nctool.seq_source_parity a sequence-keyed command\'s field is a dropdown read off the mesh: opening the composer pulls each WCB\'s names (GET_WCB_SEQ) and groups them per board, picking one previews the verb and reads the body (GET_WCB_SEQVAL) shown line by line with its comment, the manual entry types a key the boards do not have, ⟳ re-reads, and Use writes the verb as a unicast; Change re-opens with the key selected', async ({ page, emu }) => {
    await T.openTool(page);
    await T.connectUsb(page, emu);
    await openUnmappedButton(page);
    await builder(page, 'fields-1-0').click();
    await search(page, 'Run Sequence (short)');
    await item(page, 'wcb-sequences', 'wcb.runSeq').click();
    const sel = page.locator('#cmdlib-p-0');
    await expect(sel).toHaveAttribute('data-pseq', 'key');
    const [req] = await T.waitRequests(emu, 'GET_WCB_SEQ');
    expect(req).toMatchObject({ wcb: 1 });
    await expect(page.locator('#cmdlib-p-0 optgroup[label="WCB 1"] option')).toHaveText(['wave', 'spin']);
    await expect(page.locator('#cmdlib-p-0-manual')).toBeHidden();
    await sel.selectOption('wave');
    await expect(preview(page)).toHaveText(';Cwave');
    await expect.poll(() => emu.requests('GET_WCB_SEQVAL')).toEqual([expect.objectContaining({ wcb: 1, key: 'wave' })]);
    const detail = page.locator('#cmdlib-p-0-detail');
    await expect(detail).toContainText('WCB 1 — 2 lines');
    await expect(detail).toContainText(';A,PLAY,1');
    await expect(detail).toContainText(';H,STIM,H,MOD');
    await expect(detail.locator('span', { hasText: '*** says hello' })).toHaveCount(1);
    // A key typed by hand.
    await sel.selectOption('-- type manually --');
    await expect(page.locator('#cmdlib-p-0-manual')).toBeVisible();
    await page.locator('#cmdlib-p-0-manual').fill('custom_key');
    await expect(preview(page)).toHaveText(';Ccustom_key');
    await expect(detail).toContainText('Not on a board we can see');
    // ⟳ reads the boards again.
    await page.locator('#cmdlib-p-0-refresh').click();
    await expect.poll(() => emu.requests('GET_WCB_SEQ').length).toBe(2);
    await expect(page.locator('#cmdlib-p-0-refresh')).toBeEnabled();
    await sel.selectOption('spin');
    await expect(preview(page)).toHaveText(';Cspin');
    await expect(detail).toContainText('WCB 1 — 1 line');
    await page.locator('#cmdlib-use').click();
    await expect(page.locator('#type-1-0')).toHaveValue('wcb_unicast');
    expect(await rowAction(page, 'fields-1-0')).toMatchObject({ type: 'wcb_unicast', target: '1', cmd: ';Cspin' });
    await builder(page, 'fields-1-0').click();
    await expect(sel).toHaveValue('spin');
    await expect(preview(page)).toHaveText(';Cspin');
    T.expectNoPageErrors(page);
  });
});
