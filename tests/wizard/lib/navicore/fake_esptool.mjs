// A stand-in for esptool-js 0.4.7 (https://cdn.jsdelivr.net/npm/esptool-js@0.4.7/+esm), served by page.route
// (lib/navicore/firmware.js). It has the surface config_tool/flasher.js flashFirmware uses (:276-420) and records every
// call in globalThis.__fakeEsptool for the spec: which regions the tool hands writeFlash, their bytes' FNV-1a, whether
// a region is all 0xFF (an erase), what the tool's calculateMD5Hash returns for each, and the flash options. Like the
// real Transport it opens the port at the ROM baud when main() connects and closes it on disconnect(), so the fake
// serial port (lib/navicore/shim.js) sees the handover. A spec makes a step fail with __fakeEsptool.fail.main /
// .writeFlash = 'message'.
const S = globalThis.__fakeEsptool || (globalThis.__fakeEsptool = { calls: [], fail: {} });

function fnv(s) {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i) & 0xff; h = Math.imul(h, 0x01000193) >>> 0; }
  return h >>> 0;
}

function allFF(s) {
  for (let i = 0; i < s.length; i++) if (s.charCodeAt(i) !== 0xff) return false;
  return s.length > 0;
}

export class Transport {
  constructor(device, tracing) {
    this.device = device;
    S.calls.push({ op: 'transport', tracing, info: device && device.getInfo ? device.getInfo() : null });
  }
  async connect(baud = 115200) {
    S.calls.push({ op: 'connect', baud });
    await this.device.open({ baudRate: baud });
  }
  async disconnect() {
    S.calls.push({ op: 'disconnect' });
    try { await this.device.close(); } catch (e) { S.calls.push({ op: 'closeError', name: e.name }); }
  }
}

export class ESPLoader {
  constructor(opts) {
    this.opts = opts;
    this.transport = opts.transport;
    S.calls.push({ op: 'loader', baudrate: opts.baudrate, romBaudrate: opts.romBaudrate, terminal: !!opts.terminal });
  }
  async main() {
    S.calls.push({ op: 'main' });
    if (S.fail.main) throw new Error(S.fail.main);
    await this.transport.connect(this.opts.romBaudrate);
    this.opts.terminal.writeLine('Chip is ESP32-S3 (QFN56) (revision v0.2)');
    return 'ESP32-S3';
  }
  async writeFlash(o) {
    const files = o.fileArray.map((f) => ({
      address: f.address, len: f.data.length, fnv: fnv(f.data), allFF: allFF(f.data), md5: o.calculateMD5Hash(f.data),
    }));
    S.calls.push({ op: 'writeFlash', files, flashSize: o.flashSize, flashMode: o.flashMode, flashFreq: o.flashFreq,
                   eraseAll: o.eraseAll, compress: o.compress });
    if (S.fail.writeFlash) throw new Error(S.fail.writeFlash);
    o.fileArray.forEach((f, i) => {
      o.reportProgress(i, f.data.length >> 1, f.data.length);
      o.reportProgress(i, f.data.length, f.data.length);
    });
  }
  async afterFlash(mode) { S.calls.push({ op: 'afterFlash', mode }); }
}
