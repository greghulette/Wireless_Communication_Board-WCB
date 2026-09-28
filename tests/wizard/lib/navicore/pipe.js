// The L2 device behind the fake Web Serial port (lib/navicore/shim.js): the real board, reached through the HIL
// harness's own COM handle via the bridge's /serial routes (tests/hil/hil/bridge.py; docs/hil_plan/NAVICORE.md §5.2).
// The harness keeps the port open for the whole spec (hil/wizard.py run_wizard_test(..., pipe=True)), so:
//   - opening the "port" in the page never resets NaviCore (Chrome's own open asserts DTR/RTS, which reset its S3);
//   - every line both ways passes through SerialDevice and Bench.log, whose credential filter applies;
//   - setSignals() is recorded by the bridge and never applied.
// SerialDevice is line-oriented, so this is too: the page's writes are gathered into whole lines and posted one at a
// time (the bridge paces a long one as the tool does), and the board's lines come back from a 20 ms poll with a
// CRLF each. The harness's synthetic lines ("<<reopened COM5>>", "<<serial error: ...>>") are kept out of the page.
const BRIDGE = process.env.HIL_BRIDGE || '';

async function post(path, body) {
  const r = await fetch(BRIDGE + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const reply = await r.json();
  if (!r.ok) throw new Error(`bridge ${path}: ${reply.error || reply.skip || r.status}`);
  return reply;
}

class BridgePipe {
  constructor({ device, pollMs = 20 } = {}) {
    if (!BRIDGE) throw new Error('BridgePipe needs HIL_BRIDGE (run the spec through tests/hil/run.py)');
    this.device = device;
    this.pollMs = pollMs;
    this.sink = () => {};
    this.mark = null;
    this.timer = null;
    this.polling = false;
    this.buf = Buffer.alloc(0);
    this.chain = Promise.resolve();   // writes go out in order, one line at a time
    this.errors = [];                 // bridge failures, for the spec to assert on (never thrown into the page)
    this.linesOut = 0;
    this.linesIn = 0;
    this.sentTypes = [];              // the "type" of every JSON line written, in order (never the line itself)
    this.synthetic = [];
  }

  attach(sink) { this.sink = sink; }

  async onOpen() {
    try {
      this.mark = (await post('/serial/mark', { device: this.device })).mark;
    } catch (e) { this.errors.push(String(e)); return; }
    this.timer = setInterval(() => this._poll(), this.pollMs);
  }

  onClose() { this._stop(); }

  onSignals(s) {
    post('/serial/signals', { device: this.device, dtr: s.dataTerminalReady, rts: s.requestToSend })
      .catch((e) => this.errors.push(String(e)));
  }

  onWrite(bytes) {
    this.buf = Buffer.concat([this.buf, bytes]);
    let i;
    while ((i = this.buf.indexOf(0x0A)) !== -1) {
      const line = this.buf.subarray(0, i).toString('utf8').replace(/\r$/, '');
      this.buf = this.buf.subarray(i + 1);
      if (!line) continue;
      this.linesOut++;
      const t = /"type"\s*:\s*"([A-Za-z0-9_]+)"/.exec(line);   // the request TYPE only: a line may carry a password
      this.sentTypes.push(t ? t[1] : line[0] === '{' ? 'json' : 'text');
      this.chain = this.chain.then(() => post('/serial/write', { device: this.device, text: line }))
        .catch((e) => this.errors.push(String(e)));
    }
  }

  // Resolves once every line written so far has been handed to the board.
  flush() { return this.chain; }

  async _poll() {
    if (this.polling || this.mark === null) return;
    this.polling = true;
    try {
      const r = await post('/serial/read', { device: this.device, since: this.mark });
      this.mark = r.next;
      for (const line of r.lines) {
        if (/^<<.*>>$/.test(line)) { this.synthetic.push(line); continue; }
        this.linesIn++;
        this.sink(Buffer.from(line + '\r\n', 'utf8'));
      }
    } catch (e) {
      this.errors.push(String(e));
    } finally {
      this.polling = false;
    }
  }

  _stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  stop() { this._stop(); }
}

// Ask the harness for one RAM-only SBUS controller verb (the live-grid spec moves the rx stick with it).
const sbus = (verb) => post('/sbus', verb);

module.exports = { BridgePipe, sbus };
