// (should) INTELLEX.md finding 2 (IX-WP4): the Wizard's guided setup shows ../Images/LabelOnly.jpg on its
// hardware-version (identity) step (Wizard/app.js:10691) and ../Images/PololuLogo.png on its Maestro step (:10952), and
// Intellex bundles neither (WCB_IMAGES, Intellex tools/fetch_webui.py:150) - the harness's stage reads that same list
// (hil/intellex.py seed_bundles), so it has the same gap as an install. Two checks: every ../Images file the served
// index.html and app.js name answers 200 from the host, and the two steps, rendered with the Wizard's own openWizard()
// and wizardRenderStep(), show no broken picture. Fails until fetch_webui.py bundles them.
const { test, expect, skipUnlessHost } = require('../lib/fixtures');

skipUnlessHost(test);

test('intellex.ui_wizard_setup_images every Wizard image is bundled', async ({ page, rec, guarded }) => {
  await page.goto('/wcb/Wizard/', { waitUntil: 'load' });
  await expect.poll(() => rec.intellex.some(l => l.includes('tool: WCB Wizard')), { timeout: 15_000 }).toBe(true);
  const refs = new Set();
  for (const f of ['/wcb/Wizard/index.html', '/wcb/Wizard/app.js']) {
    const text = await (await page.request.get(f)).text();
    for (const m of text.matchAll(/\.\.\/Images\/([A-Za-z0-9._-]+)/g)) refs.add(m[1]);
  }
  expect(refs.size, 'the Wizard names no ../Images files at all').toBeGreaterThan(0);
  const missing = [];
  for (const name of [...refs].sort()) {
    const r = await page.request.get('/wcb/Images/' + name);
    if (r.status() !== 200) missing.push(`${name} (${r.status()})`);
  }

  await page.evaluate(() => openWizard());                                    // eslint-disable-line no-undef
  const broken = [];
  for (const step of ['identity', 'maestro']) {
    const shown = await page.evaluate((key) => {
      const i = wizardState.steps.indexOf(key);                               // eslint-disable-line no-undef
      if (i < 0) return false;
      wizardState.currentIdx = i;                                             // eslint-disable-line no-undef
      wizardRenderStep();                                                     // eslint-disable-line no-undef
      return true;
    }, step);
    if (!shown) { broken.push(`the guided setup has no '${step}' step`); continue; }
    await page.waitForTimeout(1500);                                          // the step's images load or fail
    const imgs = await page.evaluate(() => [...document.querySelectorAll('#wizard-modal img')].map(i => ({
      src: i.getAttribute('src'), ok: i.complete && i.naturalWidth > 0 })));
    for (const i of imgs) if (!i.ok) broken.push(`${step} step: ${i.src} does not load`);
  }
  expect([...missing, ...broken], 'Wizard images Intellex does not serve (INTELLEX.md finding 2)').toEqual([]);
});
