// Export and Import (docs/hil_plan/NAVICORE.md §2, nct.io.*, L1): the full JSON backup and the legacy CSV. No board:
// the emulator holds the config, so "the board has it back" is checkable, and every file goes through the page's own
// <a download> / <input type=file> paths (Playwright captures the one and answers the other).
const fs = require('node:fs');
const path = require('node:path');
const { test, expect } = require('../../lib/navicore/fixtures');
const T = require('../../lib/navicore/tool');

const BENCH = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'fixtures', 'navicore', 'config.bench.json'), 'utf8'));
const pageConfig = (page) => page.evaluate(() => JSON.parse(JSON.stringify(config)));
const importFile = (page, name, text) => T.chooseFile(page, () => page.locator('#btn-import-cfg').click(), name, text);

test.describe('the bench config over USB', () => {
  test.use({ emuOptions: { config: BENCH } });

  test('nctool.export_import_json Export writes the whole config; after the board is reset, importing that file restores it in the tool and a Save puts the board back as it was; a Wizard system file, a non-NaviCore JSON and {} are refused with nothing changed; a file from before a full-replace branch existed wipes that branch (pinned)', async ({ page, emu }) => {
    const dialogs = T.answerDialogs(page);
    const files = T.captureDownloads(page);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    const onBoard = emu.config.toJSON();

    await page.locator('#btn-export-cfg').click();
    await expect.poll(() => files.length).toBe(1);
    expect(files[0].name).toMatch(/^navicore-config-\d{4}-\d{2}-\d{2}\.json$/);
    const text = await files[0].ready;
    const exported = JSON.parse(text);
    expect(Object.keys(exported)).toEqual(['navicore', 'savedAt', 'note', 'config']);
    expect(exported.navicore).toBe('config-backup/v1');
    expect(exported.config).toEqual(await pageConfig(page));
    await expect.poll(() => T.toasts(page)).toContainEqual(expect.stringContaining('Exported full config backup (JSON)'));

    // The board loses its config (a factory reset); Refresh shows the defaults.
    emu.config.loadDefaults();
    const gets = emu.requests('GET_CONFIG').length;
    await page.locator('#btn-refresh').click();
    await T.waitRequests(emu, 'GET_CONFIG', gets + 1);
    await expect.poll(() => page.evaluate(() => Object.keys(config.mappings || {}).length)).toBe(0);

    // Import the backup: the tool holds exactly what was exported, and the next Save carries it to the board.
    await importFile(page, files[0].name, text);
    await expect.poll(() => dialogs.length).toBe(1);
    expect(dialogs[0].message).toBe('Full config imported (JSON). Review it, then Save to write it to the NaviCore.');
    expect(await pageConfig(page)).toEqual(exported.config);
    await page.evaluate(() => saveConfigToBoard());
    await T.waitRequests(emu, 'SET_CONFIG', 1);
    await expect.poll(() => T.state(page).then((s) => s.pendingSave)).toBe(false);
    expect(emu.config.toJSON(), 'the board after Export, reset, Import, Save').toEqual(onBoard);

    // Refused, and nothing changes: a WCB Wizard system file, a JSON that is not a NaviCore config, and {}.
    const before = await pageConfig(page);
    const refusals = [
      ['WCB_system_2026-09-28.txt', '[SYSTEM]\r\nquantity=2\r\n\r\n[WCB1]\r\nwcbNumber=1\r\nalias=dome\r\n',
       'Import failed: CSV must contain at least type and control columns.'],
      ['wizard.json', JSON.stringify({ boards: [{ wcbNumber: 1, alias: 'dome' }], quantity: 2 }), 'Import failed: that file is not a NaviCore config'],
      ['empty.json', '{}', 'Import failed: that file is not a NaviCore config'],
    ];
    for (const [name, body, message] of refusals) {
      const n = dialogs.length;
      await importFile(page, name, body);
      await expect.poll(() => dialogs.length).toBe(n + 1);
      expect(dialogs[n].message, name).toBe(message);
      expect(await pageConfig(page), name).toEqual(before);
    }

    // PINNED (the plan's nct.io.import): applyConfig replaces wcbProfiles, serialLabels, serialBcast, modeReport and
    // statsReport whole (index.html:9737, :9756-9778), so a file written before those keys existed empties them in the
    // tool, and the next Save would carry the emptied branches to the board.
    const old = JSON.parse(JSON.stringify(exported));
    for (const k of ['wcbProfiles', 'serialLabels', 'serialBcast', 'modeReport', 'statsReport']) delete old.config[k];
    const n = dialogs.length;
    await importFile(page, 'navicore-config-2026-01-01.json', JSON.stringify(old));
    await expect.poll(() => dialogs.length).toBe(n + 1);
    const after = await pageConfig(page);
    expect(after.wcbProfiles).toEqual([]);
    expect(after.modeReport.enabled).toBe(false);
    expect(after.statsReport).toEqual({ enabled: false, wcb: 0 });
    expect(await page.evaluate(() => Object.keys(_diffConfigBranches(config, _configBaseline)).sort()))
      .toEqual(['modeReport', 'statsReport', 'wcbProfiles']);
    T.expectNoPageErrors(page);
  });

  test('nctool.csv_roundtrip (should) the legacy CSV export, imported straight back, leaves nothing for Save to send', async ({ page, emu }) => {
    test.fail(true, 'suspected tool defect: the CSV importer rebuilds every button band as center +-10 (index.html:19432-19433) where the tool\'s own defaults ' +
                    'and the firmware\'s are +-12 (defaultThresholds :20600-20605; the firmware default table rc_config.h:831-850, loaded at :861-866), relabels the physical buttons with ' +
                    'getBtnLabel (:19431), and turns exclusive:false into an absent key (:18914, :19438); a Save after the round trip ' +
                    'narrows every band on the board by 2 us each side. The export has no button any more, but Import still reads old CSVs');
    const dialogs = T.answerDialogs(page);
    const files = T.captureDownloads(page);
    await T.openTool(page);
    await T.connectUsb(page, emu);
    const before = await pageConfig(page);
    await page.evaluate(() => exportConfigCsv());                 // no button any more; Import still reads the format
    await expect.poll(() => files.length).toBe(1);
    expect(files[0].name).toMatch(/^RC_config_.+\.csv$/);
    const csv = await files[0].ready;
    await importFile(page, files[0].name, csv);
    await expect.poll(() => dialogs.length).toBe(1);
    expect(dialogs[0].message).toMatch(/^Config CSV imported successfully/);
    const after = await pageConfig(page);
    expect(after.switches, 'switch actions survive').toEqual(before.switches);
    expect(Object.keys(after.mappings).sort(), 'every mapping survives').toEqual(Object.keys(before.mappings).sort());
    const changed = await page.evaluate(() => Object.keys(_diffConfigBranches(config, _configBaseline)).sort());
    expect(changed, 'branches a Save would send after the round trip').toEqual([]);
    T.expectNoPageErrors(page);
  });
});
