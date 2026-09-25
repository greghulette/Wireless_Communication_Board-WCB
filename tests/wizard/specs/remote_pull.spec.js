// The Wizard's remote config pull against real boards (F13). Each test is started by the HIL harness test of the same
// id (tests/hil/suites/s30_wizard.py), which hands W1's COM port to Chrome and keeps W2 - the board pulled through
// W1 - on its own USB. The harness grows W2 first and passes only what to compare: W2's firmware version, the throwaway
// sequence keys with their value lengths, and the reply length. Never a token: a pull carries the mesh password and
// the WiFi passphrase, and whatever a spec is given, logs or fails with lands in its Playwright report and in
// session.log. For the same reason the page-side spy below keeps the tag, board, lengths and part numbers of each
// relay line, never its text, and nothing here compares whole configs.
const { test, expect, hil } = require('../lib/fixtures');
const { openWizard, connectBoard } = require('../lib/wizard');

test.beforeEach(() => {
  test.skip(!hil.present, 'board tests run under the HIL harness: python tests/hil/run.py "wizard.remote_pull*"');
});

// Manage WCB<target> through the relay slot, then pull it the way Intellex's shim does: remoteBoardPull(slot, n, 1, 3,
// cb), positional, with an onComplete that must fire exactly once. Resolves 3 s after the first onComplete, so a second
// call would be seen. The relay's terminal pane is opened first, so a raw part line would have somewhere to show.
async function pullThroughRelay(page, relay, target) {
  return page.evaluate(async ({ relay, target }) => {
    const conn = boardConnections[relay];
    const seen = [];
    const sent = [];
    const spy = (line) => {
      const m = /^\[MGMT:(CONFIG|CFGPART|CFGERR),(\d+)\]([\s\S]*)$/.exec(line);
      if (!m) return;
      const rec = { tag: m[1], n: Number(m[2]), len: m[3].length };
      if (m[1] === 'CFGPART') {
        const p = /^P([0-9A-F]{4}),(\d+),(\d+):/.exec(m[3]);
        if (p) {
          Object.assign(rec, { id: p[1], k: Number(p[2]), K: Number(p[3]),
                               dataLen: m[3].length - p[0].length - 1, framed: m[3].endsWith('~') });
        }
      }
      if (m[1] === 'CFGERR') {
        // The code is everything before the first comma, so config text under the tag (the bug these checks are for)
        // would put a token or a bare secret there: it is kept only shaped like the firmware's (hil/wcb.py
        // screen_code).
        const code = m[3].split(',')[0].trim();
        rec.code = /^[A-Z]{2,12}$/.test(code) ? code : 'MALFORMED';
      }
      seen.push(rec);
    };
    const ownSend = Object.prototype.hasOwnProperty.call(conn, 'send');
    const origSend = conn.send;
    conn.send = function (data, ...rest) {
      const s = String(data);
      if (/MGMT,PULL/.test(s)) sent.push(s.trim());
      return origSend.call(this, data, ...rest);
    };
    conn._dataCallbacks.push(spy);
    try {
      ensureTerminalPane(relay);
      if (!document.getElementById(`section-board-${target}`)) addDiscoveredBoards([target]);
      setRemoteConnected(target, relay);
      const calls = [];
      const t0 = Date.now();
      await new Promise((resolve) => {
        const guard = setTimeout(resolve, 55_000);   // the Wizard settles every pull within 40 s (PULL_DEADLINE_MS)
        const r = remoteBoardPull(relay, target, 1, 3, (ok) => {
          calls.push({ ok, ms: Date.now() - t0 });
          if (calls.length === 1) { clearTimeout(guard); setTimeout(resolve, 3000); }
        });
        if (r && typeof r.catch === 'function') r.catch(() => {});
      });
      const cfg = boardConfigs[target] || {};
      const seqs = {};
      for (const s of cfg.sequences || []) if (/^HILP\d\d$/.test(s.key)) seqs[s.key] = String(s.value).length;
      const panes = [...document.querySelectorAll('[id^="term-pane-output-"]')].map((el) => el.textContent).join('\n');
      return {
        calls, sent, seen, seqs,
        wcbNumber: cfg.wcbNumber ?? null,
        fwVersion: cfg.fwVersion ?? null,
        baseline: !!boardBaselines[target],
        stillPulling: _pullingBoards.has(target),
        rawInTerminal: /\?SEQ,SAVE,HILP\d\d,/.test(panes),
      };
    } finally {
      conn._dataCallbacks = conn._dataCallbacks.filter((cb) => cb !== spy);
      if (ownSend) conn.send = origSend; else delete conn.send;
    }
  }, { relay, target });
}

// What every remote pull must leave behind, one line or parts. With noParts, CFGERR NOPARTS lines are let through: on a
// pull over the limit one means every type-19 copy of the request was lost while a type-5 copy got through, and the
// Wizard asks again (parser.js: NOPARTS is retryable), so it passes only with onComplete [true] (here) and a whole set
// of parts after the last of them (the caller). Under the limit a target never answers NOPARTS, whatever it was asked.
function expectPulled(res, a, page, { noParts = false } = {}) {
  const mine = res.seen.filter((s) => s.n === a.target);
  expect(res.calls.map((c) => c.ok), 'onComplete: called exactly once, with true').toEqual([true]);
  expect(res.sent.length, 'the Wizard sent a pull').toBeGreaterThan(0);
  for (const s of res.sent) expect(s, 'the Wizard asks for parts').toMatch(new RegExp(`^.MGMT,PULL,${a.target},P$`));
  expect(mine.filter((s) => s.tag === 'CFGERR' && !(noParts && s.code === 'NOPARTS')).map((s) => s.code),
         noParts ? 'no CFGERR but a NOPARTS the Wizard asked again after' : 'no CFGERR').toEqual([]);
  expect(res.wcbNumber, 'the pulled config names its board').toBe(a.target);
  expect(res.fwVersion, 'the version from [VER:]').toBe(a.ver);
  expect(res.seqs, 'every throwaway sequence arrived whole').toEqual(a.seqs);
  expect(res.baseline, 'a baseline was stored').toBe(true);
  expect(res.stillPulling, 'the board left _pullingBoards').toBe(false);
  expect(res.rawInTerminal, 'the terminal shows no raw pull text').toBe(false);
  expect(page.wizErrors).toEqual([]);
  return mine;
}

test('wizard.remote_pull pulls W2 through W1 as the one [MGMT:CONFIG,2] line', async ({ page, hilCtx }) => {
  const a = hilCtx.args;
  await openWizard(page);
  await connectBoard(page, hilCtx);
  const res = await pullThroughRelay(page, a.relay, a.target);
  const mine = expectPulled(res, a, page);
  const lines = mine.filter((s) => s.tag === 'CONFIG');
  expect(mine.filter((s) => s.tag === 'CFGPART').length, 'no part for a one-line config').toBe(0);
  expect(lines.length, 'a [MGMT:CONFIG,2] line arrived').toBeGreaterThan(0);
  expect(lines.map((s) => s.len), 'each [MGMT:CONFIG,2] line is the whole reply').toEqual(lines.map(() => a.length));
  await hil.note(`remote pull: ${lines.length} config line(s) of ${a.length} characters, settled in ${res.calls[0].ms} ms`);
});

test('wizard.remote_pull_parts pulls W2 through W1 in parts and joins them', async ({ page, hilCtx }) => {
  const a = hilCtx.args;
  await openWizard(page);
  await connectBoard(page, hilCtx);
  const res = await pullThroughRelay(page, a.relay, a.target);
  const mine = expectPulled(res, a, page, { noParts: true });
  const parts = mine.filter((s) => s.tag === 'CFGPART');
  expect(parts.every((s) => s.id && s.framed), 'every part line is P<id>,<k>,<K>:<data>~').toBe(true);
  // The id whose parts all arrived: an attempt that lost a part leaves an incomplete id behind, and a retry uses a new one.
  // Only parts after the last NOPARTS count, so a NOPARTS passes only when the Wizard's next attempt brought a whole
  // id.
  const noParts = mine.filter((s) => s.tag === 'CFGERR' && s.code === 'NOPARTS').length;
  const after = mine.slice(mine.map((s) => s.tag === 'CFGERR' && s.code === 'NOPARTS').lastIndexOf(true) + 1);
  const byId = new Map();
  for (const s of after.filter((x) => x.tag === 'CFGPART')) {
    const e = byId.get(s.id) || { K: s.K, lens: new Map() };
    if (!e.lens.has(s.k)) e.lens.set(s.k, s.dataLen);
    byId.set(s.id, e);
  }
  const whole = [...byId.values()].filter((e) => e.lens.size === e.K);
  expect(whole.length, `a whole set of parts${noParts ? ' after the last NOPARTS' : ''} (ids seen: ` +
                      `${[...byId.keys()].join(', ') || 'none'})`).toBeGreaterThan(0);
  const e = whole[whole.length - 1];
  expect(e.K, 'parts in the reply').toBeGreaterThanOrEqual(a.minParts);
  expect([...e.lens.values()].reduce((x, y) => x + y, 0), 'the parts add up to the reply').toBe(a.length);
  expect(mine.filter((s) => s.tag === 'CONFIG' && s.len > 0).length, 'no [MGMT:CONFIG,2] line with a body').toBe(0);
  await hil.note(`remote pull: ${e.K} parts of ${[...e.lens.values()].join('+')} characters under ${byId.size} id(s), ` +
                 `settled in ${res.calls[0].ms} ms` +
                 (noParts ? `, after ${noParts} NOPARTS asked again` : ''));
});
