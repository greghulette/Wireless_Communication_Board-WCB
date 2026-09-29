// The offline docs viewer in a browser, on the crafted wiki the harness seeds into the stage
// (tests/intellex/fixtures/wiki; IX-WP4). Intellex src/wikidocs.py rewrites links in markdown-it's renderer rules
// (:104-162) and raw <img> tags afterwards (:170-206); src/host.py serves each page with a nonce CSP (_WIKI_CSP :675-712)
// because the docs share the control API's origin; src/wiki.html carries the one nonce'd filter script. The wiki's
// pages hold a <script>, an onerror handler and javascript: links: a window.__pwned sentinel must stay unset, and no
// request may leave 127.0.0.1 (the seeded wiki links out, but loads nothing from outside).
const { test, expect, skipUnlessHost, hostGuard } = require('../lib/fixtures');

skipUnlessHost(test);

test('intellex.ui_wiki the docs viewer on a crafted wiki', async ({ page, context, rec }) => {
  page.setDefaultTimeout(15_000);
  await hostGuard(context, rec);
  const outside = [];
  page.on('request', r => {
    const u = new URL(r.url());
    if (u.hostname !== '127.0.0.1' && u.protocol !== 'data:') outside.push(r.url());
  });
  const pwned = () => page.evaluate(() => window.__pwned);

  await page.goto('/wiki/', { waitUntil: 'load' });
  expect(new URL(page.url()).pathname, '/wiki/ goes to the first wiki on disk').toBe('/wiki/intellex/');
  const tabs = await page.evaluate(() => [...document.querySelectorAll('#bar a.tab')].map(a => ({
    text: a.textContent, href: a.getAttribute('href'), cur: a.getAttribute('aria-current') })));
  expect(tabs, 'a tab per wiki on disk, the current one marked').toEqual([
    { text: 'Intellex', href: '/wiki/intellex/', cur: 'page' }, { text: 'WCB', href: '/wiki/wcb/', cur: null }]);
  expect(await page.evaluate(() => [...document.querySelectorAll('#side li a')].map(a => a.getAttribute('href'))),
    'a wiki with no _Sidebar lists its pages').toEqual(['/wiki/intellex/Connecting', '/wiki/intellex/Home']);

  await page.goto('/wiki/wcb/', { waitUntil: 'load' });
  expect(await page.title()).toBe('WCB HIL Home · Intellex Docs');
  const links = await page.evaluate(() => Object.fromEntries([...document.querySelectorAll('#doc a')].map(a =>
    [a.textContent.trim(), { href: a.getAttribute('href'), target: a.getAttribute('target'), rel: a.getAttribute('rel'),
                             cls: a.className }])));
  const want = {
    'Getting Started': '/wiki/wcb/Getting-Started', 'Shown text': '/wiki/wcb/Target-Page',
    'A dash link': '/wiki/wcb/Page-Name', 'A page with a file suffix': '/wiki/wcb/Other-Page',
    External: 'https://example.com/', 'Anchor only': '#links' };
  for (const [label, href] of Object.entries(want)) {
    expect(links[label] && links[label].href, `the link '${label}'`).toBe(href);
  }
  expect([links.External.target, links.External.rel], 'an external link opens outside').toEqual(['_blank', 'noopener']);
  const offimg = Object.entries(links).filter(([, v]) => v.cls === 'offimg');
  expect(offimg.map(([k]) => k).sort(), 'missing images become links to the online copy').toEqual(
    ['🖼 missing image — view online', '🖼 raw gone — view online']);
  for (const [, v] of offimg) {
    expect(v.href, 'an offline image links to the wiki\'s raw copy').toMatch(
      /^https:\/\/raw\.githubusercontent\.com\/wiki\/greghulette\/Wireless_Communication_Board-WCB\/Images\//);
  }
  const imgs = await page.evaluate(() => [...document.querySelectorAll('#doc img')].map(i => ({
    alt: i.getAttribute('alt'), src: i.getAttribute('src'), ok: i.complete && i.naturalWidth > 0 })));
  const byAlt = Object.fromEntries(imgs.map(i => [i.alt, i]));
  for (const alt of ['present image', 'raw present']) {
    expect(byAlt[alt] && byAlt[alt].src, `the image '${alt}' is rewritten to the local copy`).toBe('/wiki/wcb/Images/pic.png');
    expect(byAlt[alt].ok, `the image '${alt}' loads`).toBe(true);
  }
  // Inside a code block nothing becomes a link or an image. (That its [[...]] text is rewritten all the same is
  // INTELLEX.md finding 15, pinned by the (should) test intellex.wiki_code_verbatim, not here.)
  const code = await page.evaluate(() => document.querySelector('#doc pre code').textContent);
  expect(code, 'a URL in a code block').toContain('https://example.com/in-code');
  expect(code, 'an <img> in a code block stays text').toContain('<img src="Images/pic.png">');
  expect(await page.evaluate(() => document.querySelectorAll('#doc pre a, #doc pre img').length),
    'links or images made inside a code block').toBe(0);
  expect(await page.evaluate(() => [...document.querySelectorAll('#doc a')].filter(a =>
    /^javascript:/i.test(a.getAttribute('href') || '') && a.id !== 'rawjs').length),
    'a markdown javascript: link was rendered as a link').toBe(0);

  await page.waitForTimeout(500);
  expect(await pwned(), 'a script in the page ran (the inline <script> or the onerror handler)').toBeUndefined();
  await page.click('#rawjs');
  await page.waitForTimeout(300);
  expect(await pwned(), 'a raw javascript: link ran').toBeUndefined();

  // The sidebar filter, the viewer's own nonce'd script.
  await page.fill('#find', 'getting');
  const visible = await page.evaluate(() => [...document.querySelectorAll('#side li')]
    .filter(li => !li.classList.contains('hidden')).map(li => li.textContent.trim()));
  expect(visible, 'the filter narrows the sidebar').toEqual(['Getting Started']);

  // A script in the URL is shown as text, never run.
  await page.goto('/wiki/wcb/' + encodeURIComponent("</title><script>window.__pwned='url'</script>"),
                  { waitUntil: 'load' });
  expect(await pwned(), 'a script in the URL ran').toBeUndefined();
  expect(await page.title(), 'the name from the URL, escaped into the title').toContain("<script>window.__pwned='url'");
  expect(await page.evaluate(() => document.querySelector('#doc .missing') !== null),
    'a page that is not on disk says so').toBe(true);

  // A wiki's own SVG, opened directly, is sandboxed: its script cannot reach this origin.
  await page.goto('/wiki/wcb/Images/evil.svg', { waitUntil: 'load' });
  expect(await page.evaluate(() => { try { return window.__pwned; } catch (_) { return 'no access'; } }),
    'the SVG\'s script ran with this origin').toBeUndefined();

  expect(outside, 'requests that left 127.0.0.1').toEqual([]);
  expect(rec.errors, 'page errors').toEqual([]);
});
