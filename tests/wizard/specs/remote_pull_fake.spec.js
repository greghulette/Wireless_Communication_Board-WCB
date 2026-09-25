// Needs no board: remoteBoardPull (Wizard/app.js) driven through a FAKE relay connection in the real page. Runs
// standalone (`npx playwright test`), in CI (.github/workflows/wizard-tests.yml runs every spec; the board specs skip
// themselves), and under the harness by id.
//
// The fake is a real BoardConnection in slot 1 whose send() only records, and whose lines go through the real
// _handleLine - so the listener isolation, the terminal display rule and the pull listener all run as they do on a
// USB relay. Time is Playwright's fake clock (page.clock): the pull's 6 s attempt timer, 2.5 s retry gap and 40 s
// deadline are stepped, not waited out.
//
// What every scenario checks, because it is what callers depend on: onComplete fires exactly ONCE (relayRouteAll
// awaits it with no watchdog, Intellex behind a 45 s one), and the pull leaves nothing behind - not in _pullingBoards
// (a leftover makes every later pull "already in progress"), not on the relay queue, not as a listener - and sends
// nothing more once it has settled.
//
// The config text below carries a dummy mesh password (SECRET). The relay pane must never show config text: a real
// one holds the board's WiFi passphrase and mesh password in its first ~300 characters.
const { test, expect } = require('../lib/fixtures');
const { openWizard } = require('../lib/wizard');
const P = require('../../../Wizard/parser.js');

const SECRET = 'dummy_not_a_real_mesh_password';
const FFFD = String.fromCharCode(0xFFFD);
const hex8 = (n) => n.toString(16).toUpperCase().padStart(8, '0');
const utf8 = (s) => Buffer.from(s, 'utf8');

// A config chain for WCB n. Every board shares the general fields, so no general-settings mismatch modal opens. A
// multi-byte alias and labels, as a real board can hold. `seqs` long sequences push it past one reply (2912).
function chainFor(n, seqs = 0) {
  const tokens = ['?HW,32', `?WCB,${n}`, `?ALIAS,Dôme ${n}`, '?WCBQ,8', '?WCBCH,1', `?EPASS,${SECRET}`,
                  '?LABEL,S1,Périscope €', '?LABEL,S2,Clef 𝄞', '?BAUD,S1,57600'];
  for (let i = 0; i < seqs; i++) tokens.push(`?SEQ,SAVE,s${i},;m1${i % 10}^;t400^;w${n}hello ${'x'.repeat(170)}`);
  return tokens.join('^');
}
const reply = (chain) => `[VER:6.2.1_FAKE]${chain}^?CHK${hex8(P.crc32(chain))}`;

// The relay's lines for a reply sent in parts, cut the way the target cuts them (grid of D bytes, each cut backed off
// a UTF-8 continuation byte, at most 3 steps). Same helper as unit/pull.test.js.
function partLines(text, src, { D = 2880, id = 'A1B2' } = {}) {
  const b = Buffer.isBuffer(text) ? text : utf8(text);
  const K = Math.ceil(b.length / D);
  const cuts = [0];
  for (let k = 1; k < K; k++) {
    let x = k * D;
    for (let i = 0; i < 3 && (b[x] & 0xC0) === 0x80; i++) x--;
    if ((b[x] & 0xC0) === 0x80) x = k * D;
    cuts.push(x);
  }
  cuts.push(b.length);
  const lines = [];
  for (let k = 1; k <= K; k++) {
    lines.push(`[MGMT:CFGPART,${src}]P${id},${k},${K}:${b.subarray(cuts[k - 1], cuts[k]).toString('utf8')}~`);
  }
  return lines;
}

// Open the Wizard on the fake clock and put the fake relay in slot 1, managing boards 2..8 the way "Manage via relay"
// does. Returns the relay's idle state (its listener count with no pull in flight: the ETM listener).
async function setup(page) {
  await page.clock.install();
  await openWizard(page);
  await page.evaluate(() => {
    // The 12 s mesh poll would find the fake relay and park a ?WDP,DUMP collector on it for 4 s at a time.
    window._wdpMeshConn = () => null;
    const relay = new BoardConnection(1);
    relay._connected = true;
    window.__sent = [];
    window.__onSend = null;
    relay.send = async (s) => { window.__sent.push({ at: Date.now(), s }); window.__onSend?.(s); };
    boardConnections[1] = relay;
    const targets = [2, 3, 4, 5, 6, 7, 8];
    addDiscoveredBoards(targets);
    for (const n of targets) setRemoteConnected(n, 1);
    window.__done = {};
    window.__pull = (n, maxAttempts = 3) => {
      __done[n] = [];
      return remoteBoardPull(1, n, 1, maxAttempts, (ok) => __done[n].push({ ok, at: Date.now() }))
        .then(() => 'sent', (e) => `rejected: ${e.message}`);
    };
    window.__feed = (lines) => { for (const l of lines) boardConnections[1]._handleLine(l); };
    window.__pulls = (n) => __sent.filter((x) => x.s.includes(`MGMT,PULL,${n},`)).map((x) => ({ at: x.at, s: x.s }));
    window.__pane = () => document.getElementById('term-pane-output-1')?.textContent ?? '';
    window.__state = () => ({
      pulling: [..._pullingBoards].sort(),
      active: [..._activePulls.keys()].sort(),
      relayBusy: !!_pullRelayQueues[1]?.active,
      waiting: _pullRelayQueues[1]?.waiting.length ?? 0,
      listeners: boardConnections[1]._dataCallbacks.length,
    });
  });
  await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 1000);
  await page.clock.runFor(2000);   // setRemoteConnected's RTERM,START sends (3 x, 250 ms apart)
  return page.evaluate(() => __state());
}

const pulls = (page, n) => page.evaluate((n) => __pulls(n), n);
const done = (page, n) => page.evaluate((n) => __done[n], n);
const feed = (page, lines) => page.evaluate((lines) => __feed(lines), lines);

// Nothing left behind, and nothing more happens: a minute later there is still one onComplete and no new request.
async function expectSettled(page, n, idle, ok) {
  const sentBefore = (await pulls(page, n)).length;
  await page.clock.runFor(60_000);
  const d = await done(page, n);
  expect(d.map((x) => x.ok), `onComplete for WCB${n}`).toEqual([ok]);
  expect((await pulls(page, n)).length, `requests for WCB${n} after it settled`).toBe(sentBefore);
  expect(await page.evaluate(() => __state())).toEqual({ ...idle, pulling: [], active: [], relayBusy: false, waiting: 0 });
}

test('wizard.remote_pull_fake_api the page exposes the one parser api, crc32 matches zlib, and remoteBoardPull is on window', async ({ page }) => {
  await openWizard(page);
  const r = await page.evaluate(() => ({
    keys: Object.keys(window.WCBParser).sort(),
    check: WCBParser.crc32('123456789'),
    utf: WCBParser.crc32('Dôme €𝄞'),
    pull: typeof window.remoteBoardPull,
    deadline: PULL_DEADLINE_MS,
  }));
  // The browser object is the Node one: a function missing from either list used to pass node --test and then
  // throw 'WCBParser.x is not a function' inside the pull listener.
  expect(r.keys).toEqual(Object.keys(P).sort());
  expect(r.check).toBe(0xCBF43926);
  expect(r.utf).toBe(0x38D63D51);             // zlib.crc32('Dôme €𝄞'.encode()), the firmware's CRC-32
  expect(r.pull).toBe('function');            // Intellex calls window.remoteBoardPull(slot, n, 1, 3, cb)
  expect(r.deadline).toBeLessThan(45_000);    // ... behind a 45 s watchdog
  expect(page.wizErrors).toEqual([]);
});

test('wizard.remote_pull_fake_legacy a single [MGMT:CONFIG,n] reply is stored exactly as before, and the pane shows only its length', async ({ page }) => {
  const idle = await setup(page);
  const chain = chainFor(2);
  expect(await page.evaluate(() => __pull(2))).toBe('sent');
  expect((await pulls(page, 2)).map((x) => x.s)).toEqual(['?MGMT,PULL,2,P\r']);
  expect(await page.evaluate(() => __state())).toMatchObject({ pulling: [2], active: [2], relayBusy: true });

  await feed(page, [`[MGMT:CONFIG,2]${reply(chain)}`]);
  await page.clock.runFor(500);
  const r = await page.evaluate((chain) => ({
    stored: JSON.stringify(boardBaselines[2]),
    parsed: JSON.stringify(WCBParser.parseBackupString(chain)),
    fw: boardConfigs[2].fwVersion,
    wcb: boardConfigs[2].wcbNumber,
    alias: boardConfigs[2].alias,
    pane: __pane(),
  }), chain);
  expect(r.stored).toBe(r.parsed);          // the baseline is exactly what the parser makes of the chain
  expect([r.fw, r.wcb, r.alias]).toEqual(['6.2.1_FAKE', 2, 'Dôme 2']);
  expect(r.pane).toContain(`[MGMT:CONFIG,2] <${reply(chain).length} chars received>`);
  expect(r.pane).not.toContain(SECRET);
  await expectSettled(page, 2, idle, true);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.remote_pull_fake_parts three parts out of order, with a duplicate, noise and another board\'s part in between, join to the config', async ({ page }) => {
  const idle = await setup(page);
  const chain = chainFor(2, 30);
  const lines = partLines(reply(chain), 2, { id: '5EED' });
  expect(lines.length).toBe(3);
  await page.evaluate(() => __pull(2));
  await feed(page, [lines[2], 'unrelated relay output', `[MGMT:CFGPART,3]P5EED,1,3:${SECRET}~`,
                    lines[0], lines[0], '[MGMT:STATS,2]not a pull reply']);
  await page.clock.runFor(100);
  expect(await done(page, 2)).toEqual([]);
  await feed(page, [lines[1]]);
  await page.clock.runFor(500);
  const r = await page.evaluate((chain) => ({
    stored: JSON.stringify(boardBaselines[2]),
    parsed: JSON.stringify(WCBParser.parseBackupString(chain)),
    seqs: boardConfigs[2].sequences.length,
    pane: __pane(),
  }), chain);
  expect(r.stored).toBe(r.parsed);
  expect(r.seqs).toBe(30);
  expect(r.pane).toContain('[MGMT:CFGPART,2] <part 1/3 (id 5EED)');
  expect(r.pane).toContain('3 config parts joined, checksum OK');
  expect(r.pane).not.toContain(SECRET);     // not from any part, nor from the other board's
  expect((await pulls(page, 2)).length).toBe(1);
  await expectSettled(page, 2, idle, true);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.remote_pull_fake_timer every NEW part restarts the 6 s attempt timer, a duplicate does not, and the retry is a new job', async ({ page }) => {
  const idle = await setup(page);
  const chain = chainFor(2, 20);
  const a = partLines(reply(chain), 2, { id: 'AAAA' });
  const b = partLines(reply(chain), 2, { id: 'BBBB' });
  expect(a.length).toBe(2);
  await page.evaluate(() => __pull(2));
  const t0 = (await pulls(page, 2))[0].at;

  await page.clock.runFor(5000);
  await feed(page, [a[0]]);                 // t0+5 s: progress, the attempt now ends at t0+11 s, not t0+6 s
  await page.clock.runFor(5000);
  await feed(page, [a[0]]);                 // t0+10 s: a duplicate is not progress
  expect((await pulls(page, 2)).length).toBe(1);
  await page.clock.runFor(1100);            // t0+11.1 s: timed out; the relay is free during the retry gap
  expect(await page.evaluate(() => __state())).toMatchObject({ pulling: [2], active: [2], relayBusy: false });
  expect(await page.evaluate(() => document.getElementById('b2-status-badge')?.textContent)).toContain('Retrying');
  await page.clock.runFor(2500);
  const sent = await pulls(page, 2);
  expect(sent.length).toBe(2);
  expect(sent[1].at - t0).toBe(11_000 + 2500);

  await feed(page, b);                      // the retry's job: a new id, collected from scratch
  await page.clock.runFor(500);
  expect((await done(page, 2)).map((x) => x.ok)).toEqual([true]);
  await expectSettled(page, 2, idle, true);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.remote_pull_fake_fifo one attempt on the air per relay: a second board waits, fills the first one\'s retry gap, and a duplicate is refused', async ({ page }) => {
  const idle = await setup(page);
  await page.evaluate(() => { __pull(2); __pull(3); });
  expect((await page.evaluate(() => __sent.filter((x) => x.s.includes('MGMT,PULL')).map((x) => x.s))))
    .toEqual(['?MGMT,PULL,2,P\r']);
  expect(await page.evaluate(() => __state())).toMatchObject({ pulling: [2, 3], active: [2, 3], relayBusy: true, waiting: 1 });
  expect(await page.evaluate(() => __pane())).toContain('Pull for WCB3 queued');

  // A second pull of a board already in flight is refused at once, and changes nothing.
  const dup = await page.evaluate(() => new Promise((res) => remoteBoardPull(1, 2, 1, 3, res)));
  expect(dup).toBe(false);

  const t0 = (await pulls(page, 2))[0].at;
  await page.clock.runFor(6000);            // WCB2's attempt 1 times out: WCB3 goes on the air in the gap
  const w3 = await pulls(page, 3);
  expect(w3.length).toBe(1);
  expect(w3[0].at - t0).toBe(6000);
  await feed(page, [`[MGMT:CONFIG,3]${reply(chainFor(3))}`]);
  await page.clock.runFor(2600);            // WCB2's retry, after the gap
  const w2 = await pulls(page, 2);
  expect(w2.length).toBe(2);
  expect(w2[1].at - t0).toBe(8500);
  await feed(page, [`[MGMT:CONFIG,2]${reply(chainFor(2))}`]);
  await page.clock.runFor(500);
  expect((await done(page, 3)).map((x) => x.ok)).toEqual([true]);
  await expectSettled(page, 2, idle, true);
  expect((await done(page, 3)).length).toBe(1);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.remote_pull_fake_errors CFGERR NOMEM retries (its trailing empty reply is not a second failure), NOPARTS retries, TOOBIG stops at once, not-UTF-8 retries once and stops on a second job\'s, empty replies fail after 3 attempts, a CRC failure retries', async ({ page }) => {
  const idle = await setup(page);

  // NOMEM, then the empty legacy reply a new target sends after it: one failed attempt, then success.
  await page.evaluate(() => __pull(4));
  await feed(page, ['[MGMT:CFGERR,4]NOMEM,need 2990 free 2400 largest 1800', '[MGMT:CONFIG,4]']);
  await page.clock.runFor(2600);
  expect((await pulls(page, 4)).length).toBe(2);
  expect(await page.evaluate(() => __pane())).toContain('out of memory');
  await feed(page, [`[MGMT:CONFIG,4]${reply(chainFor(4))}`]);
  await expectSettled(page, 4, idle, true);

  // NOPARTS: the request for parts was lost - one failed attempt, then success.
  await page.evaluate(() => __pull(9));
  await feed(page, ['[MGMT:CFGERR,9]NOPARTS,3100 chars']);
  await page.clock.runFor(2600);
  expect((await pulls(page, 9)).length).toBe(2);
  await feed(page, [`[MGMT:CONFIG,9]${reply(chainFor(9))}`]);
  await expectSettled(page, 9, idle, true);

  // Permanent: stop at once, one request, the reason shown.
  for (const [code, why] of [['TOOBIG', 'too large to pull']]) {
    await page.evaluate(() => __pull(5));
    await feed(page, [`[MGMT:CFGERR,5]${code},L=50000`]);
    await page.clock.runFor(100);
    expect((await done(page, 5)).map((x) => x.ok), code).toEqual([false]);
    expect(await page.evaluate(() => __pane())).toContain(why);
    const before = (await pulls(page, 5)).length;
    await expectSettled(page, 5, idle, false);
    expect((await pulls(page, 5)).length).toBe(before);
  }
  expect((await pulls(page, 5)).length).toBe(1);   // one request for the one pull above

  // An empty legacy reply every time: retried, and reported after the last attempt.
  await page.evaluate(() => __pull(6));
  for (let i = 0; i < 3; i++) {
    await feed(page, ['[MGMT:CONFIG,6]']);
    await page.clock.runFor(2600);
  }
  expect((await pulls(page, 6)).length).toBe(3);
  expect(await page.evaluate(() => __pane())).toContain('failed after 3 attempts — empty config response');
  await expectSettled(page, 6, idle, false);

  // A join that fails its CRC is retried; the retry's parts complete it.
  const chain7 = chainFor(7, 20);
  const bad = partLines(reply(chain7).replace('Périscope', 'Periscope'), 7, { id: 'BAD0' });
  await page.evaluate(() => __pull(7));
  await feed(page, bad);
  await page.clock.runFor(2600);
  expect((await pulls(page, 7)).length).toBe(2);
  expect(await page.evaluate(() => __pane())).toContain('failed the checksum');
  await feed(page, partLines(reply(chain7), 7, { id: 'C0DE' }));
  await expectSettled(page, 7, idle, true);

  // A byte that is not UTF-8 in the target's NVS: every job's join holds U+FFFD and can never verify. One join cannot
  // tell that from a byte lost on the way, so the first is retried; the same job's parts delivered again prove
  // nothing; a second job failing the same way stops the pull.
  const raw = Buffer.concat([utf8(chainFor(8, 20)), Buffer.from([0x5E, 0x3F, 0x4C, 0x41, 0x42, 0x45, 0x4C, 0x2C,
                                                                 0x53, 0x35, 0x2C, 0x43, 0x61, 0x66, 0xE9])]);
  const text = Buffer.concat([utf8('[VER:6.2.1_FAKE]'), raw, utf8(`^?CHK${hex8(P.crc32(raw))}`)]);
  const job = (id) => partLines(text, 8, { id });
  expect(job('FFFD').join('')).toContain(FFFD);
  await page.evaluate(() => __pull(8));
  await feed(page, job('FFFD'));
  await page.clock.runFor(2600);
  expect((await pulls(page, 8)).length).toBe(2);
  expect(await page.evaluate(() => __pane())).toContain('not valid UTF-8 (a byte lost on the way');
  await feed(page, job('FFFD'));            // the first job again: not a second observation
  await page.clock.runFor(100);
  expect(await done(page, 8)).toEqual([]);
  await feed(page, job('FFFE'));            // the retry's job, the same bytes: they are stored that way
  await page.clock.runFor(100);
  expect((await done(page, 8)).map((x) => x.ok)).toEqual([false]);
  expect(await page.evaluate(() => __pane())).toContain('not valid UTF-8 in two replies');
  await expectSettled(page, 8, idle, false);
  expect((await pulls(page, 8)).length).toBe(2);

  // A byte lost on the way inside 'é' (C3 A9 -> C3): the same U+FFFD from a config that is fine on the target. The
  // retry's parts arrive whole and the pull completes.
  const chain3 = chainFor(3, 20);
  const whole = utf8(reply(chain3));
  const at = whole.indexOf(0xA9);
  const lossy = partLines(Buffer.concat([whole.subarray(0, at), whole.subarray(at + 1)]), 3, { id: '1057' });
  expect(lossy.join('')).toContain(FFFD);
  await page.evaluate(() => __pull(3));
  await feed(page, lossy);
  await page.clock.runFor(2600);
  expect((await pulls(page, 3)).length).toBe(2);
  await feed(page, partLines(reply(chain3), 3, { id: '600D' }));
  await page.clock.runFor(500);
  expect(await page.evaluate((chain) => JSON.stringify(boardBaselines[3]) === JSON.stringify(WCBParser.parseBackupString(chain)), chain3))
    .toBe(true);
  await expectSettled(page, 3, idle, true);

  expect(await page.evaluate(() => __pane())).not.toContain(SECRET);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.remote_pull_fake_deadline a pull ends at PULL_DEADLINE_MS from the call, once: parts that trickle in forever, and a pull queued behind three dead targets inside Intellex\'s 45 s watchdog', async ({ page }) => {
  const idle = await setup(page);
  // K = 7, and part 7 never comes: each new part restarts the attempt timer, so without the cap a pull like this
  // runs past Intellex's 45 s watchdog. The fake target answers every request with a fresh job, a part every 5 s.
  const text = reply(chainFor(2, 6));
  const K = 7;
  const D = Math.ceil(utf8(text).length / K);
  await page.evaluate(({ text, D, K }) => {
    let timers = [];
    let job = 0;
    window.__onSend = (s) => {
      if (!s.includes('MGMT,PULL,2,')) return;
      timers.forEach(clearTimeout);
      const id = (0xA000 + ++job).toString(16).toUpperCase();
      const bytes = new TextEncoder().encode(text);
      timers = [];
      for (let k = 1; k < K; k++) {
        // Cut anywhere: part K never comes, so these parts are never joined.
        const part = new TextDecoder().decode(bytes.subarray((k - 1) * D, k * D));
        timers.push(setTimeout(() => __feed([`[MGMT:CFGPART,2]P${id},${k},${K}:${part}~`]), 5000 * k));
      }
    };
  }, { text, D, K });

  await page.evaluate(() => __pull(2));
  const t0 = (await pulls(page, 2))[0].at;
  await page.clock.runFor(45_000);
  const d = await done(page, 2);
  expect(d.map((x) => x.ok)).toEqual([false]);
  expect(d[0].at - t0).toBe(40_000);
  expect(await page.evaluate(() => __pane())).toContain('gave up after 40 s');
  await page.evaluate(() => { window.__onSend = null; });
  await expectSettled(page, 2, idle, false);

  // Three pulls of boards that never answer are queued ahead (ETM went-OFFLINE verify pulls after the dome ring loses
  // power), then WCB6 is pulled the way Intellex does it. Each dead target holds the relay 6 s per attempt, so WCB6's
  // first request goes out 18 s after its call; a cap counted from that send ended 58 s after the call, and Intellex's
  // watchdog had fired at 45 s. Counted from the call, onComplete comes at 40 s, and nothing is sent at the cap.
  const tCall = await page.evaluate(() => { __pull(3); __pull(4); __pull(5); const t = Date.now(); __pull(6); return t; });
  await page.clock.runFor(45_000);
  const d6 = await done(page, 6);
  expect(d6.map((x) => x.ok), 'onComplete for WCB6 within 45 s of its call').toEqual([false]);
  expect(d6[0].at - tCall).toBeLessThan(45_000);
  expect(d6[0].at - tCall).toBe(40_000);
  const sent6 = await pulls(page, 6);
  expect(sent6[0].at - tCall).toBe(18_000);
  expect(sent6.filter((x) => x.at - tCall >= 40_000), 'requests for WCB6 at or after its cap').toEqual([]);
  for (const n of [3, 4, 5]) expect((await done(page, n)).map((x) => x.ok), `WCB${n}`).toEqual([false]);
  await expectSettled(page, 6, idle, false);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.remote_pull_fake_display a throwing listener costs no other listener its line, config lines are summarised with no pull running, and a stats capture, relayed or direct, skips pull replies', async ({ page }) => {
  const idle = await setup(page);

  // A listener that throws on every line, ahead of the pull's own.
  await page.evaluate(() => {
    window.__bad = () => { throw new Error('listener bug'); };
    boardConnections[1]._dataCallbacks.unshift(window.__bad);
  });
  await page.evaluate(() => __pull(2));
  await feed(page, [`[MGMT:CONFIG,2]${reply(chainFor(2))}`]);
  await page.clock.runFor(500);
  expect((await done(page, 2)).map((x) => x.ok)).toEqual([true]);
  expect(await page.evaluate(() => __pane())).toContain('[Wizard] a line listener failed: listener bug');
  await page.evaluate(() => {
    boardConnections[1]._dataCallbacks = boardConnections[1]._dataCallbacks.filter((cb) => cb !== window.__bad);
  });
  await expectSettled(page, 2, idle, true);

  // No pull running: a config or part line is still only a length, an error line is shown whole.
  await feed(page, [`[MGMT:CONFIG,9]${reply(chainFor(9))}`, `[MGMT:CFGPART,9]P0042,1,2:?EPASS,${SECRET}~`,
                    `[MGMT:CFGPART,9]garbled ${SECRET}`, '[MGMT:CFGERR,9]NOMEM,need 4000 free 3000 largest 2000']);
  const pane = await page.evaluate(() => __pane());
  expect(pane).toContain('[MGMT:CFGPART,9] <part 1/2 (id 0042)');
  expect(pane).toContain('[MGMT:CFGERR,9]NOMEM,need 4000 free 3000 largest 2000');
  expect(pane).not.toContain(SECRET);
  // ... and the same inside a mirrored board's [TERM:n] output (WCB2 relaying someone else's pull of WCB3).
  await feed(page, [`[TERM:2][MGMT:CFGPART,3]P0043,1,2:?EPASS,${SECRET}~`, `[TERM:2][MGMT:CONFIG,3]${reply(chainFor(3))}`]);
  const pane2 = await page.evaluate(() => document.getElementById('term-pane-output-2')?.textContent ?? '');
  expect(pane2).toContain('[MGMT:CFGPART,3] <part 1/2 (id 0043)');
  expect(pane2).toContain('[MGMT:CONFIG,3] <');
  expect(pane2).not.toContain(SECRET);

  // A relayed ?MGMT,STATS capture: the pull replies arriving meanwhile stay out of it.
  await page.evaluate(() => openStatsModal(2, 'stats'));
  await page.clock.runFor(100);
  expect(await page.evaluate(() => __sent.at(-1).s)).toBe('?MGMT,STATS,2\r');
  await feed(page, ['[MGMT:STATS,2]--- ESP-NOW Statistics ---', 'Packets sent: 5',
                    `[MGMT:CFGPART,3]P0042,1,2:?EPASS,${SECRET}~`, '[MGMT:CFGERR,3]NOMEM,x',
                    `[TERM:2][MGMT:CFGPART,3]P0044,1,2:?EPASS,${SECRET}~`,
                    `[MGMT:CONFIG,3]${reply(chainFor(3))}`, '[MGMT:SEQ,3]x', 'Packets received: 7',
                    '--- End of ESP-NOW Statistics ---']);
  await page.clock.runFor(500);
  const out = await page.evaluate(() => document.getElementById('stats-modal-output').textContent);
  expect(out).toContain('Packets sent: 5');
  expect(out).toContain('Packets received: 7');
  expect(out).not.toContain('MGMT:');
  expect(out).not.toContain(SECRET);

  // The relay's own ?STATS, on the direct path: the pulls it relays meanwhile print into the same 5 s window.
  await page.evaluate(() => openStatsModal(1, 'stats'));
  await page.clock.runFor(100);
  expect(await page.evaluate(() => __sent.at(-1).s)).toBe('?STATS\r');
  await feed(page, ['--- ESP-NOW Statistics ---', 'Packets sent: 11',
                    `[MGMT:CFGPART,3]P0045,1,2:?EPASS,${SECRET}~`, `[TERM:2][MGMT:CFGPART,3]P0046,1,2:?EPASS,${SECRET}~`,
                    `[MGMT:CONFIG,3]${reply(chainFor(3))}`, '[MGMT:CFGERR,3]NOMEM,x', 'Packets received: 13']);
  await page.clock.runFor(5100);
  const direct = await page.evaluate(() => document.getElementById('stats-modal-output').textContent);
  expect(direct).toContain('Packets sent: 11');
  expect(direct).toContain('Packets received: 13');
  expect(direct).not.toContain('MGMT:');
  expect(direct).not.toContain(SECRET);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.remote_pull_fake_reader parts read in small chunks, cut inside multi-byte characters, reassemble through the direct reader and the shared-hub path alike', async ({ page }) => {
  const idle = await setup(page);
  // CRLF lines exactly as a relay prints them, handed over 7 (then 5) bytes at a time: many reads end inside a
  // 2-, 3- or 4-byte character. The direct reader used to decode each read on its own, which made those U+FFFD and
  // would fail every parts pull's checksum.
  const chunked = (lines, size) => {
    const b = utf8(lines.map((l) => `${l}\r\n`).join(''));
    const out = [];
    for (let i = 0; i < b.length; i += size) out.push([...b.subarray(i, i + size)]);
    return out;
  };
  const chain2 = chainFor(2, 20);
  const chain3 = chainFor(3, 20);
  const direct = chunked(partLines(reply(chain2), 2, { id: 'D1EC' }), 7);
  const shared = chunked(partLines(reply(chain3), 3, { id: '5A4E' }), 5);

  // Direct: a port whose readable stream we feed, read by the real _startReading loop.
  await page.evaluate(() => {
    const relay = boardConnections[1];
    relay.port = { readable: new ReadableStream({ start(c) { window.__portCtl = c; } }) };
    relay._startReading();
    __pull(2);
  });
  await page.evaluate((chunks) => { for (const c of chunks) __portCtl.enqueue(Uint8Array.from(c)); }, direct);
  await page.clock.runFor(500);
  expect((await done(page, 2)).map((x) => x.ok)).toEqual([true]);
  expect(await page.evaluate(() => [boardConfigs[2].alias, boardConfigs[2].serialPorts[1].label]))
    .toEqual(['Dôme 2', 'Clef 𝄞']);

  // Shared hub: the same relay rejoined through a stand-in hub; the bytes arrive through its 'data' event.
  await page.evaluate(() => {
    const handlers = {};
    window.__hub = { portOpen: true, on: (ev, fn) => { (handlers[ev] ??= []).push(fn); }, off() {}, join() {}, leave() {},
                     emit: (ev, arg) => (handlers[ev] || []).forEach((fn) => fn(arg)) };
    boardConnections[1].connectShared(window.__hub);
    __pull(3);
  });
  await page.evaluate((chunks) => { for (const c of chunks) __hub.emit('data', Uint8Array.from(c)); }, shared);
  await page.clock.runFor(500);
  expect((await done(page, 3)).map((x) => x.ok)).toEqual([true]);
  expect(await page.evaluate(() => boardConfigs[3].alias)).toBe('Dôme 3');
  expect(await page.evaluate(() => JSON.stringify([boardBaselines[2], boardBaselines[3]])))
    .not.toContain(FFFD);
  await expectSettled(page, 3, idle, true);
  expect(page.wizErrors).toEqual([]);
});

test('wizard.remote_pull_fake_edges a relay that is not connected, a send that throws, and a pull superseded after the guard was cleared each settle once', async ({ page }) => {
  const idle = await setup(page);

  // The relay is gone at call time.
  const r1 = await page.evaluate(() => {
    boardConnections[1]._connected = false;
    const got = [];
    remoteBoardPull(1, 2, 1, 3, (ok) => got.push(ok));
    boardConnections[1]._connected = true;
    return got;
  });
  expect(r1).toEqual([false]);

  // The relay drops during the retry gap: the retry settles the pull instead of stranding the guard.
  await page.evaluate(() => __pull(2));
  await page.clock.runFor(6100);
  await page.evaluate(() => { boardConnections[1]._connected = false; });
  await page.clock.runFor(2500);
  await page.evaluate(() => { boardConnections[1]._connected = true; });
  expect((await done(page, 2)).map((x) => x.ok)).toEqual([false]);
  await expectSettled(page, 2, idle, false);

  // The send throws on attempt 1: onComplete(false) once, and the returned promise rejects as it always has.
  const r2 = await page.evaluate(async () => {
    const relay = boardConnections[1];
    const send = relay.send;
    relay.send = async () => { throw new Error('port gone'); };
    const res = await __pull(3);
    relay.send = send;
    return res;
  });
  expect(r2).toBe('rejected: port gone');
  await expectSettled(page, 3, idle, false);

  // The post-OTA paths clear _pullingBoards by hand and pull again: the older pull is settled (false) at once, the
  // new one completes, and the old one's end does not release the new one's guard.
  await page.evaluate(() => __pull(4));
  const r3 = await page.evaluate(() => {
    const first = __done[4];
    _pullingBoards.delete(4);
    window.__second = [];
    remoteBoardPull(1, 4, 1, 3, (ok) => __second.push(ok));
    return { first: first.map((x) => x.ok), pulling: [..._pullingBoards], active: [..._activePulls.keys()] };
  });
  expect(r3).toEqual({ first: [false], pulling: [4], active: [4] });
  await feed(page, [`[MGMT:CONFIG,4]${reply(chainFor(4))}`]);
  await page.clock.runFor(500);
  expect(await page.evaluate(() => __second)).toEqual([true]);
  expect(await page.evaluate(() => __state())).toEqual({ ...idle, pulling: [], active: [], relayBusy: false, waiting: 0 });
  expect(page.wizErrors).toEqual([]);
});
