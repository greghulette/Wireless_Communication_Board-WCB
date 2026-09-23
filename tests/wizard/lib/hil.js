// Client for the HIL harness bridge (tests/hil/hil/bridge.py). The harness owns the bench; a browser test asks
// it for what Chrome cannot see — the probes on the WCB ports, the other boards, the session log.

const BRIDGE = process.env.HIL_BRIDGE || '';

async function call(path, body = {}) {
  const r = await fetch(BRIDGE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const reply = await r.json();
  if (reply.skip) throw new Error(`harness has no such wire: ${reply.skip}`);
  if (!r.ok) throw new Error(`harness ${path}: ${reply.error}`);
  return reply;
}

// Wire helpers take the port key parts the harness uses: wcb number and 'S1'..'S5'.
function wire(wcb, port) {
  return {
    mark: async () => (await call('/wire/mark', { wcb, port })).mark,
    received: async (since) => Buffer.from((await call('/wire/received', { wcb, port, since })).hex, 'hex'),
    expect: (text, since, timeout = 3) => call('/wire/expect', { wcb, port, text, since, timeout }),
    send: (text) => call('/wire/send', { wcb, port, text }),
  };
}

module.exports = {
  present: !!BRIDGE,
  context: () => call('/context'),
  note: (text) => call('/note', { text }),
  config: async (wcb) => (await call('/config', { wcb })).tokens,
  wire,
};
