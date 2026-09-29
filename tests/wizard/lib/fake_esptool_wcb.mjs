// A stand-in for the Wizard's vendored esptool-js (Wizard/vendor/esptool-js/esptool-js-0.4.7.bundle.js), served by
// page.route in specs/flasher_fake.spec.js. It has the surface Wizard/flasher.js flashFirmware uses (:351-761): main()
// and chip.CHIP_NAME, getFlashSize() in KB or readFlashId(), readFlash(), writeFlash(), afterFlash(), and the
// Transport's disconnect/setDTR/setRTS. Nothing here touches a port. Each test configures it through
// globalThis.__wcbEsp = { chip, flashKB, flashId, flash: { <address>: [bytes] }, calls: [] } before calling
// flashFirmware; a size source left null is absent, as on a loader that lacks it.
const S = () => globalThis.__wcbEsp || (globalThis.__wcbEsp = { calls: [] });

export class Transport {
  constructor(device, tracing) { S().calls.push({ op: 'transport', tracing }); this.device = device; }
  async disconnect() { S().calls.push({ op: 'disconnect' }); }
  async setDTR(v) { S().calls.push({ op: 'dtr', v }); }
  async setRTS(v) { S().calls.push({ op: 'rts', v }); }
}

export class ESPLoader {
  constructor(opts) {
    this.opts = opts;
    const cfg = S();
    cfg.calls.push({ op: 'loader', baudrate: opts.baudrate, romBaudrate: opts.romBaudrate });
    if (cfg.flashKB != null) this.getFlashSize = async () => cfg.flashKB;
    if (cfg.flashId != null) this.readFlashId = async () => cfg.flashId;
  }
  async main() {
    const cfg = S();
    cfg.calls.push({ op: 'main' });
    this.chip = { CHIP_NAME: cfg.chip };
    return cfg.chip;
  }
  async readFlash(address, length) {
    const cfg = S();
    cfg.calls.push({ op: 'read', address, length });
    const out = new Uint8Array(length).fill(0xFF);
    const src = (cfg.flash || {})[address];
    if (src) out.set(src.slice(0, length));
    return out;
  }
  async writeFlash(o) {
    S().calls.push({ op: 'write', files: o.fileArray.map((f) => ({ address: f.address, length: f.data.length,
                                                                   first: f.data.charCodeAt(0) })) });
    o.fileArray.forEach((f, i) => o.reportProgress(i, f.data.length, f.data.length));
  }
  async afterFlash(mode) { S().calls.push({ op: 'afterFlash', mode }); }
}
