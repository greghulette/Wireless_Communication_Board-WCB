// Needs no board: runs standalone (`npx playwright test`) and as the harness's wizard.smoke.
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');

test('wizard.smoke the Wizard loads and settles with no uncaught page errors', async ({ page }) => {
  await openWizard(page);
  await expect(page.locator('#b1-btn-connect')).toBeVisible();
  await page.waitForTimeout(2000);   // let the deferred init (GitHub version fetch, timers) run
  expect(page.wizErrors).toEqual([]);
});
