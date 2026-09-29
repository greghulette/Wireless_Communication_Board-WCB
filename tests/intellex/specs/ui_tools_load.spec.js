// Both tools load through Intellex with nothing attached (IX-WP4). The harness starts the host unattached, offline
// and with no serial port allowed, so any request the page makes past the host is either guarded or fails loudly.
// intellex.ui_tools_load stages the tools from the working trees; intellex.ui_tools_load_shipped runs the same body
// against Intellex's own bundles, which is what a user of the current Intellex runs (INTELLEX.md DX3).
const { test, expect, skipUnlessHost } = require('../lib/fixtures');

skipUnlessHost(test);

async function toolsLoad({ page, rec }) {
  for (const [path, tool] of [['/', 'NaviCore config tool'], ['/wcb/Wizard/', 'WCB Wizard']]) {
    rec.reset();
    await page.goto(path, { waitUntil: 'load' });
    const saw = (s) => rec.intellex.some(l => l.includes(s));
    // The shim runs before any tool code: it replaces navigator.serial with the host's /_link.
    await expect.poll(() => saw('navigator.serial is backed by'), { timeout: 15_000,
      message: `${path}: the shim never said it backs navigator.serial` }).toBe(true);
    await expect.poll(() => saw(`tool: ${tool}`), { timeout: 15_000,
      message: `${path}: the shim did not identify the tool as ${tool}` }).toBe(true);
    await expect.poll(() => saw('host has no transport attached'), { timeout: 20_000,
      message: `${path}: the shim did not notice the host has nothing attached` }).toBe(true);
    await page.waitForTimeout(1500);                   // late errors surface after start-up
    expect(rec.errors, `${path}: page errors`).toEqual([]);
    // The host is offline and this stage has no firmware cache, so its GitHub proxy answers the Wizard's firmware
    // listing with 502 - the designed con-floor answer, which the page must survive (it did if rec.errors is empty).
    // Anything else that failed is a real problem.
    const bad = rec.bad.filter(b => !/^502 .*\/_api\/gh\//.test(b));
    expect(bad, `${path}: failed requests`).toEqual([]);
    // requestPort() needs no user gesture and no picker: the shim answers with the host's one port.
    const info = await page.evaluate(async () => (await navigator.serial.requestPort()).getInfo());
    expect(info.usbVendorId, `${path}: requestPort() did not return the shim's port`).toBe(0x303A);
  }
}

test('intellex.ui_tools_load both tools load with nothing attached', async ({ page, rec, guarded }) => {
  await toolsLoad({ page, rec });
});

test('intellex.ui_tools_load_shipped both shipped tools load with nothing attached', async ({ page, rec, guarded }) => {
  await toolsLoad({ page, rec });
});
