// The runtime contract between Intellex's shim and the two tools it does not own (IX-WP4; INTELLEX.md §1.5): every
// function, piece of state, DOM id and string the shim reaches for is there and of the kind it expects, asserted in
// the page itself. A tool change that breaks the shim fails here before a user meets it. Run on the working trees
// (intellex.ui_contract_wizard / _navicore) and on Intellex's shipped bundles (..._shipped, what users run today).
// The Wizard's state lives in top-level lets and consts, readable by bare name only (intellex_shim.js:1176-1181).
// Also functional: each tool's own GitHub constants, fetched through the shim's rewrite (intellex_shim.js:472-495),
// must land on a repository and directory Intellex's proxy serves (ghproxy.py:47-61, :82-84) - offline the proxy then
// answers 502 naming its cache product, where a drifted constant gets 'not a firmware repository/directory'.
const { test, expect, skipUnlessHost, hilContext } = require('../lib/fixtures');

skipUnlessHost(test);

const TOOLS = {
  wizard: {
    path: '/wcb/Wizard/', product: 'wcb',
    state: [['boardConnections', 'object'], ['boardConfigs', 'object'], ['boardBaselines', 'object'],
            ['remoteRelayForBoard', 'object'], ['_pullingBoards', 'Set'], ['UI_VERSION', 'string'],
            ['latestFirmwareVersion', 'declared'], ['_meshBoards', 'Set'], ['_meshClients', 'Map'],
            ['BoardConnection', 'function'], ['GITHUB_OWNER', 'string'], ['GITHUB_REPO', 'string'],
            ['GITHUB_BIN_PATH', 'string']],
    funcs: ['splashGoConfig', 'relayManageOne', 'relayRouteAll', 'renderRelayCard', 'establishConnection',
            '_modalDoConnect', 'setRemoteConnected', 'boardGo', 'remoteBoardPull', 'renderWdpMesh', '_wdpMeshConn',
            'flashFirmware', 'getFirmwareBranch'],
    ids: ['relay-cards', 'toast-container', 'splash-overlay', 'wizard-modal'],
    // [the name whose source must hold these, [strings]]: fields the shim reads off what they return
    sources: [['_wdpMeshConn', ['wcbNum', 'slot']], ['BoardConnection', ['this.port', 'isConnected']]],
    text: [['/wcb/Wizard/app.js', 'Firmware source is overridden', 'retireBranchToast finds the toast by it'],
           ['/wcb/Wizard/app.js', 'toast-dismiss', 'retireBranchToast clicks the toast\'s own dismiss button'],
           ['/wcb/Wizard/app.js', 'rc_config_tool_url', 'the Wizard reads its RC-tool link from this key'],
           ['/wcb/Wizard/app.js', "client: m[2] === '1'", 'routeMeshThroughBoard takes the CLIENT flag from the sweep'],
           ['/wcb/Wizard/app.js', 'onclick="relayManageOne(', 'unmanagedOnCard counts Manage buttons by it'],
           ['/wcb/Wizard/index.html', 'id="section-board-{N}"', 'the section id routeMeshThroughBoard falls back on'],
           ['/wcb/Wizard/flasher.js', "localStorage.getItem('wcb_fw_branch')", 'the branch key the shim sets']],
    stamp: ['/wcb/Wizard/app.js', /\bUI_VERSION\s*=\s*['"]([^'"]+)['"]/, 'host.py _WCB_VER_RE'],
  },
  navicore: {
    path: '/', product: 'navicore',
    state: [['viaWcbActive', 'boolean'], ['GITHUB_OWNER', 'string'], ['GITHUB_REPO', 'string'],
            ['GITHUB_BIN_PATH', 'string']],
    funcs: ['connectDirect', 'onViaWcbToggle'],
    ids: ['btn-fw-flash', 'btn-fw-wipe', 'btn-fw-ota', 'btn-fw-ota-wcb', 'fw-status', 'fw-log', 'fw-progress-wrap',
          'fw-progress-bar', 'fw-progress-pct', 'status-text', 'btn-connect', 'footer-dtg', 'fw-current-version'],
    sources: [],
    text: [['/index.html', "msg.type === 'PONG'", 'the PONG check the doorway rule is built around (rule 10)'],
           ['/flasher.js', "localStorage.getItem('rc_fw_branch')", 'the branch key the shim sets'],
           ['/flasher.js', 'https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/${GITHUB_BIN_PATH}',
            'the Contents URL the shim\'s fetch rewrite matches']],
    stamp: ['/index.html', /id="footer-dtg"[^>]*>([^<]+)</, 'host.py _DTG_RE'],
  },
};

async function contract(page, rec, which) {
  const t = TOOLS[which];
  const ctx = await hilContext();
  await page.goto(t.path, { waitUntil: 'load' });
  await expect.poll(() => rec.intellex.some(l => l.includes('navigator.serial is backed by')), { timeout: 15_000 })
    .toBe(true);
  await page.waitForTimeout(1500);                  // the shim's load+400 ms wiring, and the tool's own init
  const got = await page.evaluate(({ state, funcs, ids, sources }) => {
    const out = [];
    for (const [name, kind] of state) {
      let v;
      try { v = eval(name); } catch (e) { out.push(`${name}: not defined`); continue; }   // eslint-disable-line no-eval
      const ok = kind === 'Set' ? v instanceof Set : kind === 'Map' ? v instanceof Map
        : kind === 'object' ? (v !== null && typeof v === 'object')
        : kind === 'declared' ? true : typeof v === kind;
      if (!ok) out.push(`${name}: ${v === null ? 'null' : typeof v}, not ${kind}`);
    }
    for (const f of funcs) if (typeof window[f] !== 'function') out.push(`window.${f}: ${typeof window[f]}`);
    for (const id of ids) if (!document.getElementById(id)) out.push(`#${id}: missing`);
    for (const [name, words] of sources) {
      let src = '';
      try { src = String(eval(name)); } catch (_) { /* reported above */ }                 // eslint-disable-line no-eval
      for (const w of words) if (!src.includes(w)) out.push(`${name}'s source no longer mentions ${w}`);
    }
    return out;
  }, { state: t.state, funcs: t.funcs, ids: t.ids, sources: t.sources });
  const problems = [...got];
  for (const [url, text, why] of t.text) {
    const r = await page.request.get(url);
    const body = await r.text();
    if (r.status() !== 200) problems.push(`${url}: ${r.status()}`);
    else if (!body.includes(text)) problems.push(`${url} no longer contains ${JSON.stringify(text)} (${why})`);
  }
  const [url, rx, who] = t.stamp;
  const m = rx.exec(await (await page.request.get(url)).text());
  if (!m || !m[1].trim()) problems.push(`${url}: no version stamp for ${who}`);

  const consts = await page.evaluate(() => [GITHUB_OWNER, GITHUB_REPO, GITHUB_BIN_PATH]);  // eslint-disable-line no-undef
  const want = (ctx.args && ctx.args.repos || {})[t.product];
  if (want && JSON.stringify(consts) !== JSON.stringify(want)) {
    problems.push(`the tool's GITHUB_OWNER/REPO/BIN_PATH ${JSON.stringify(consts)} differ from Intellex's ` +
                  `${JSON.stringify(want)} (flash.py / wcb_flash.py; ghproxy.REPOS serves only those)`);
  }
  const proxied = await page.evaluate(async ([o, r, p]) => {
    const res = await fetch(`https://api.github.com/repos/${o}/${r}/contents/${p}?ref=hil-no-such-branch`);
    let msg = '';
    try { msg = (await res.json()).message || ''; } catch (_) { /* not JSON */ }
    return { status: res.status, url: res.url, msg };
  }, consts);
  if (!proxied.url.includes('/_api/gh/contents')) problems.push(`the shim did not rewrite the tool's Contents URL`);
  else if (proxied.status !== 502 || !proxied.msg.includes(`firmware listing for ${t.product}`)) {
    problems.push(`the proxy did not recognise the tool's firmware repo: ${proxied.status} ${proxied.msg.slice(0, 120)}`);
  }
  expect(problems, `${which}: the shim's contract`).toEqual([]);
  expect(rec.errors, `${which}: page errors`).toEqual([]);
}

test('intellex.ui_contract_wizard the shim contract with the Wizard (working tree)', async ({ page, rec, guarded }) => {
  await contract(page, rec, 'wizard');
});

test('intellex.ui_contract_navicore the shim contract with the config tool (working tree)',
  async ({ page, rec, guarded }) => { await contract(page, rec, 'navicore'); });

test('intellex.ui_contract_wizard_shipped the shim contract with the shipped Wizard', async ({ page, rec, guarded }) => {
  await contract(page, rec, 'wizard');
});

test('intellex.ui_contract_navicore_shipped the shim contract with the shipped config tool',
  async ({ page, rec, guarded }) => { await contract(page, rec, 'navicore'); });
