// Playwright for Intellex. The HIL harness (tests/hil/hil/intellex.py) stages a copy of Intellex, starts its host with
// the leash on (offline, only the serial port the test names) and passes its address as INTELLEX_URL - the host IS the
// web server, so there is no webServer entry here. Always headless: Intellex's pages talk to the host, not to Web
// Serial, so there is no port grant and no one needs to be at the keyboard. Same Playwright version as tests/wizard
// (1.63.0), so both use the one Chromium this PC has installed.
const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './specs',
  workers: 1,                // one host, one bench
  fullyParallel: false,
  retries: 0,
  timeout: 300_000,
  reporter: 'line',
  use: { baseURL: process.env.INTELLEX_URL, headless: true },
});
