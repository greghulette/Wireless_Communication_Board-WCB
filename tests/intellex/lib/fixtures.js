// The fixture every Intellex spec uses.
//
// rec: what the page did - page errors, every console line, the [Intellex] ones on their own, and every response of
//      400 or more that the spec did not itself fulfil.
// guarded: routes that would reach past the host are answered here unless the spec lists them in `allow` -
//      /_api/identify opens a COM port, /_api/discover probes the droid's access point over the PC's second WiFi
//      adapter, /_api/wifi-bounce runs netsh, /_api/update-* go to GitHub. The host is leashed too
//      (hil/intellex.py); this keeps a page from even asking.
const base = require('@playwright/test');

const GUARD = /\/_api\/(identify|discover|wifi-bounce|update-webui|update-firmware|update-wiki)(\?|$)/;

exports.test = base.test.extend({
  allow: [[], { option: true }],
  rec: async ({ page }, use) => {
    const rec = { errors: [], console: [], intellex: [], bad: [], fulfilled: new Set() };
    page.on('pageerror', e => rec.errors.push(String(e && e.message || e)));
    page.on('console', m => {
      const t = m.text();
      rec.console.push(t);
      if (t.startsWith('[Intellex]')) rec.intellex.push(t);
    });
    page.on('response', r => {
      if (r.status() >= 400 && !rec.fulfilled.has(r.url())) rec.bad.push(`${r.status()} ${r.url()}`);
    });
    rec.reset = () => { rec.errors.length = rec.console.length = rec.intellex.length = rec.bad.length = 0; };
    await use(rec);
  },
  guarded: async ({ page, allow, rec }, use) => {
    await page.route(GUARD, route => {
      const url = route.request().url();
      if (allow.some(a => url.includes(a))) return route.continue();
      rec.fulfilled.add(url);
      return route.fulfill({ status: 403, contentType: 'application/json',
                             body: JSON.stringify({ ok: false, error: 'guarded by the HIL spec' }) });
    });
    await use(true);
  },
});

exports.expect = base.expect;

// A spec run outside the harness has no host to talk to.
exports.skipUnlessHost = (test) =>
  test.skip(!process.env.INTELLEX_URL,
            'INTELLEX_URL is not set - run it through the harness: python tests/hil/run.py "intellex.*"');
