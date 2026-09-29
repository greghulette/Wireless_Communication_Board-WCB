// Fake boards for the Wizard specs that need no board (push_more_fake, app_fake). A fake is a real BoardConnection
// in the page whose I/O is replaced: every send, ACK-paced send, collect, close and reconnect is recorded in
// window.__fake.log as { slot, op, s, at } - slot being the connection's slot at that moment, so a board that
// boardPull moves to its own number records under the new one - and the replies come from what the spec gave it. So boardPull, boardGo,
// boardGoRemote, boardGoAll, the editors and the mesh panel run exactly as they do on the bench - only the wire is
// fake. specs/push_fake.spec.js keeps its own copy of the ?backup builder, deliberately untouched.
//
// Nothing here is a real credential: the mesh password below is a dummy, and every SSID and passphrase a spec uses
// is spelled as one.
const EPASS = 'dummy_not_a_real_mesh_password';

// The board every fixture starts from, in collectConfigCommands' order (WCB.ino), then the fixture's own lines. Later
// lines win, as they do on the board.
function board(extra = [], { wcb = 1, wcbq = 2 } = {}) {
  return ['HW,24', 'MAC,2,05', 'MAC,3,4B', `WCB,${wcb}`, `WCBQ,${wcbq}`, 'WCBCH,1', 'WIFI,OFF', 'PEERSLIVE,2', `EPASS,${EPASS}`,
          'CMDCHAR,;', 'KYBER,CLEAR', 'ETM,ON', 'ETM,TIMEOUT,500', 'ETM,HB,10', 'ETM,MISS,5', 'ETM,BOOT,2',
          'ETM,COUNT,20', 'ETM,DELAY,100', 'ETM,CHKSM,ON', ...extra];
}

// A ?backup as printBackupConfig prints it: one command per line behind the board's CURRENT function identifier.
// `end: false` cuts it before the end marker (a reply lost halfway).
function backup(tokens, { lfi = '?', end = true } = {}) {
  const lines = ['', '*** ========================================', '*** WCB Configuration Backup',
                 '*** Copy and paste these commands to restore', '*** ========================================', '',
                 ...tokens.map((t) => lfi + t), '',
                 "*** === For Configured Boards (Current Delimiter: '^') ===", '(chain not read by the Wizard)', '',
                 "*** === For Factory Reset/Fresh Boards (Uses Default '^' Delimiter) ===", '(chain not read by the Wizard)', ''];
  if (end) lines.push('--------- End of Backup ---------', '');
  return lines.join('\r\n');
}

// The same config as the one-line chain a relay pull carries (?-prefixed, '^'-joined).
const chain = (tokens) => tokens.map((t) => `?${t}`).join('^');

// Install window.__fake once per page load. Toasts are recorded too: most outcomes a user sees are toasts, and they
// leave the DOM after a few seconds.
async function install(page) {
  await page.evaluate(() => {
    if (window.__fake) return;
    const log = [];
    const toasts = [];
    const realToast = window.showToast;
    window.showToast = (message, type, duration) => {
      toasts.push({ message: String(message), type: type || 'info', at: Date.now() });
      return realToast(message, type, duration);
    };
    window.confirm = () => true;     // wdpForgetPeer / wdpForgetDevice / wdpClearLearned ask first
    const realMeshConn = window._wdpMeshConn;
    window._wdpMeshConn = () => null;   // the 12 s mesh poll would send ?WDP,DUMP through a fake; meshOn() puts it back
    const rec = (slot, op, s) => log.push({ slot, op, s: String(s ?? '').replace(/\r$/, ''), at: Date.now() });
    window.__fake = {
      log, toasts,
      meshOn() { window._wdpMeshConn = realMeshConn; },
      // replies: { '<command>': text | (command) => text } for sendAndCollect; acks: { '<command>': [{ ok, lines }, ...] }
      // consumed per call by sendAndAwaitIdle (default: answered). backup: what WCB_WEBTOOL_CONFIG_PULL returns.
      conn(slot, { backup = '', replies = {}, acks = {}, shared = false, reconnects = true } = {}) {
        const c = new BoardConnection(slot);
        c._connected = true;
        c.__backup = backup;
        c.__replies = replies;
        c.__acks = acks;
        c.send = async (s) => { rec(c.boardIndex, 'send', s); };
        c.sendAndAwaitIdle = async (s) => {
          rec(c.boardIndex, 'await', s);
          const q = c.__acks[s];
          if (Array.isArray(q) && q.length) return q.shift();
          return { ok: true, lines: ['[fake] ok'] };
        };
        c.sendAndCollect = async (s) => {
          rec(c.boardIndex, 'collect', s);
          if (s === 'WCB_WEBTOOL_CONFIG_PULL') return c.__backup;
          const r = c.__replies[s];
          return typeof r === 'function' ? r(s) : (r ?? '');
        };
        if (shared) {
          // A shared-hub board: no SerialPort of its own, the hub holds it open (BoardConnection.isConnected, connectShared).
          // Its close and reconnect are the class's own, recorded: on a portless connection they are what the Wizard
          // really runs, so a stub here would hide what happens to a shared board.
          c._shared = true;
          c.port = null;
          c._hub = { portOpen: true, role: 'leader', _portOpen: true, lockName: 'wcb-fake-hub',
                     send: async (d) => rec(c.boardIndex, 'send', d), on() {}, off() {}, leave() {}, join() {} };
          const realClose = c.closeForReconnect.bind(c);
          const realReconnect = c.reconnect.bind(c);
          c.closeForReconnect = async () => { rec(c.boardIndex, 'close', ''); return realClose(); };
          c.reconnect = async (...a) => { const ok = await realReconnect(...a); rec(c.boardIndex, 'reconnect', String(ok)); return ok; };
        } else {
          c.closeForReconnect = async () => { rec(c.boardIndex, 'close', ''); c._connected = false; };
          c.reconnect = async () => { rec(c.boardIndex, 'reconnect', String(reconnects)); c._connected = reconnects; return reconnects; };
        }
        boardConnections[slot] = c;
        return c;
      },
      // What slot sent, in order: plain sends and ACK-paced sends (not the pull's collects unless asked).
      sent(slot, { collects = false } = {}) {
        return log.filter((e) => e.slot === slot && (e.op === 'send' || e.op === 'await' || (collects && e.op === 'collect')))
                  .map((e) => e.s);
      },
      mark() { return log.length; },
      since(m) { return log.slice(m); },
      toastText() { return toasts.map((t) => t.message).join('\n'); },
    };
  });
}

// A fake USB board in `slot`, pulled through the real boardPull. Returns once its baseline is stored.
async function pullFake(page, slot, tokens, opts = {}) {
  await install(page);
  const text = typeof tokens === 'string' ? tokens : backup(tokens, { lfi: opts.lfi || '?' });
  await page.evaluate(async ({ slot, text, opts }) => {
    __fake.conn(slot, { ...opts, backup: text });
    await boardPull(slot);
  }, { slot, text, opts: { shared: !!opts.shared, replies: opts.replies || {}, acks: opts.acks || {} } });
}

// boardGo(slot) as the Push button runs it: what it sent and how it ended.
async function push(page, slot, opts = {}) {
  return page.evaluate(async ({ slot, opts }) => {
    const m = __fake.mark();
    const r = await boardGo(slot, opts);
    return { returned: r ?? null, outcome: boardPushOutcome[slot],
             sent: __fake.since(m).filter((e) => e.op === 'send' || e.op === 'await').map((e) => `${e.slot}:${e.s}`) };
  }, { slot, opts });
}

// Set a field the way a user would, and fire the handlers the page listens on.
function edit(page, selector, value) {
  return page.evaluate(({ selector, value }) => {
    const el = document.querySelector(selector);
    if (!el) throw new Error(`no element ${selector}`);
    if (el.type === 'checkbox' || el.type === 'radio') el.checked = value;
    else el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }, { selector, value });
}

// A fake relay in `relay` (a real BoardConnection whose sends are recorded) managing `targets`, each seeded with a
// pulled config as its baseline - the state a relay pull leaves. The General inputs are synced from the first.
async function relayFake(page, relay, targets) {
  await install(page);
  await page.evaluate(async ({ relay, targets }) => {
    __fake.conn(relay, { backup: '' });
    for (const [n, text] of Object.entries(targets)) {
      addDiscoveredBoards([+n]);
      setRemoteConnected(+n, relay);
      const cfg = WCBParser.parseBackupString(text);
      boardConfigs[n] = cfg;
      boardBaselines[n] = JSON.parse(JSON.stringify(cfg));
      populateUIFromConfig(+n, cfg);
    }
    const first = Object.values(targets)[0];
    if (first) syncGeneralFromConfig(WCBParser.parseBackupString(first));
    await new Promise((r) => setTimeout(r, 1000));   // setRemoteConnected's RTERM,START sends (3 x, 250 ms apart)
  }, { relay, targets });
}

module.exports = { EPASS, board, backup, chain, install, pullFake, push, edit, relayFake };
