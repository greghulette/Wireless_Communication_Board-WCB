// The shim's glue in both tools with nothing attached (IX-WP4, the rest of the plan's intellex.ui_tools_load row).
// Intellex src/intellex_shim.js: the branch key (:336-345) and the Wizard's RC-tool link (:360-366); the Wizard's port
// sharing off (:784-794), branch toast retired after BRANCH_TOAST_MS (:812-832), splash answered with its own
// splashGoConfig (:856-877), flashFirmware replaced (:1318-1395), the chip (:1502-1528); the config tool's flash buttons
// wired to the host with their native titles (:539-607) and its OTA button left as USB on a non-WiFi link (:630-666).
// The branch is changed through /_api/branch, which writes only the stage's own settings.json.
const { test, expect, skipUnlessHost, hostPost } = require('../lib/fixtures');

skipUnlessHost(test);

const NATIVE_FLASH = "Update via the host's native esptool. Saved configuration (NVS) is preserved.";
const NATIVE_WIPE = "Full wipe via the host's native esptool. ERASES saved configuration (NVS).";

test('intellex.ui_tools_glue the shim glue in both tools', async ({ page, rec, guarded }) => {
  const saw = (s) => rec.intellex.some(l => l.includes(s));
  const storage = (k) => page.evaluate((key) => localStorage.getItem(key), k);

  // ── The Wizard, on main ──────────────────────────────────────────────────
  await page.goto('/wcb/Wizard/', { waitUntil: 'load' });
  // The splash is raised during the Wizard's init and answered with its own splashGoConfig within the shim's 150 ms
  // poll: the plan allows 2 s from load. The console line proves it was up and was answered, not merely never shown.
  await expect.poll(() => saw('dismissed the Wizard start-up splash'), { timeout: 2_000,
    message: 'the Wizard\'s start-up splash was not dismissed within 2 s of load' }).toBe(true);
  expect(await page.evaluate(() => {
    const e = document.getElementById('splash-overlay');
    return !!e && !e.classList.contains('open');
  }), 'the start-up splash is still open').toBe(true);
  for (const line of ['cross-tab port sharing disabled', 'flashFirmware() now runs on the host']) {
    await expect.poll(() => saw(line), { timeout: 15_000, message: `the Wizard: no '[Intellex] ... ${line}'` }).toBe(true);
  }
  expect(await storage('rc_config_tool_url'), 'the Wizard\'s RC-tool link does not point at this host').toBe('/');
  expect(await storage('wcb_fw_branch'), 'wcb_fw_branch is set although the branch is main').toBeNull();
  await expect.poll(() => page.evaluate(() => (document.getElementById('intellex-chip') || {}).textContent || ''),
    { timeout: 6_000, message: 'the Wizard chip' }).toBe('Intellex · not attached ▾');
  const info = await page.evaluate(async () => (await navigator.serial.requestPort()).getInfo());
  expect(info, 'requestPort().getInfo()').toEqual({ usbVendorId: 0x303A, usbProductId: 0x1001 });
  expect(await page.evaluate(() => typeof flashFirmware === 'function' && flashFirmware === window.flashFirmware
                                   && window.__intellexWcbFlash === true), 'the shim\'s flashFirmware').toBe(true);

  // ── The Wizard, on a branch: the key is set before app.js reads it, and the warning toast retires itself ────
  const set = await hostPost('/_api/branch', { product: 'wcb', branch: 'hil-glue' });
  expect(set.status, `POST /_api/branch: ${JSON.stringify(set.json)}`).toBe(200);
  try {
    rec.reset();
    await page.reload({ waitUntil: 'load' });
    expect(await storage('wcb_fw_branch'), 'the Wizard\'s branch key after the host moved to hil-glue').toBe('hil-glue');
    await expect.poll(() => saw('firmware branch for this tool: hil-glue'), { timeout: 10_000 }).toBe(true);
    const toast = () => page.evaluate(() => [...document.querySelectorAll('#toast-container .toast.warning')]
      .filter(t => /Firmware source is overridden/i.test(t.textContent || '')).length);
    await expect.poll(toast, { timeout: 10_000, message: 'no branch-override toast on a branch' }).toBeGreaterThan(0);
    await expect.poll(toast, { timeout: 15_000, message: 'the branch toast was not retired (BRANCH_TOAST_MS 9 s)' })
      .toBe(0);
  } finally {
    await hostPost('/_api/branch', { product: 'wcb', branch: 'main' });
  }
  await page.reload({ waitUntil: 'load' });
  expect(await storage('wcb_fw_branch'), 'wcb_fw_branch is left behind after the branch went back to main').toBeNull();

  // ── The NaviCore config tool ────────────────────────────────────────────────
  rec.reset();
  await page.goto('/', { waitUntil: 'load' });
  for (const line of ['tool: NaviCore config tool', 'firmware listings now come from the host',
                      'host has no transport attached']) {
    await expect.poll(() => saw(line), { timeout: 15_000, message: `the config tool: no '[Intellex] ... ${line}'` })
      .toBe(true);
  }
  const btn = (id) => page.evaluate((i) => {
    const b = document.getElementById(i);
    return b ? { disabled: b.disabled, title: b.title, text: (b.textContent || '').trim() } : null;
  }, id);
  await expect.poll(() => btn('btn-fw-flash'), { timeout: 10_000, message: 'Update Firmware: wired to the host' })
    .toEqual(expect.objectContaining({ disabled: false, title: NATIVE_FLASH }));
  await expect.poll(() => btn('btn-fw-wipe'), { timeout: 10_000, message: 'Full Wipe: wired to the host' })
    .toEqual(expect.objectContaining({ disabled: false, title: NATIVE_WIPE }));
  const ota = await btn('btn-fw-ota');
  expect(ota && ota.text, 'the OTA button').toContain('Update over USB (OTA)');
  expect(ota.text, 'the OTA button says WiFi on a link that is not WiFi').not.toContain('WiFi');
  expect(await storage('rc_fw_branch'), 'rc_fw_branch is set although the branch is main').toBeNull();
  // Made on the shim's first 2 s tick (intellex_shim.js:1416-1433, :1530-1532), beside the tool's own status text.
  await expect.poll(() => page.evaluate(() => {
    const el = document.getElementById('intellex-transport');
    return !!el && el.previousElementSibling === document.getElementById('status-text');
  }), { timeout: 6_000, message: 'no transport label beside #status-text' }).toBe(true);
  expect(await page.evaluate(() => document.getElementById('intellex-transport').textContent),
    'the transport label shows a link while disconnected').toBe('');
  expect(rec.errors, 'page errors').toEqual([]);
});
