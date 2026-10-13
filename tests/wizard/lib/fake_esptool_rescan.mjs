// A stand-in for the Wizard's vendored esptool-js for specs/flasher_rescan_fake.spec.js, served by page.route in place
// of Wizard/vendor/esptool-js/*. lib/fake_esptool_wcb.mjs's surface (main() and chip.CHIP_NAME, getFlashSize() in KB or
// readFlashId(), readFlash(), writeFlash(), afterFlash(), the Transport's disconnect/setDTR/setRTS) with the knobs the
// re-scan's flasher rows need, read from globalThis.__wcbEsp:
//   { chip, flashKB, flashId, flash: { <address>: [bytes] }, calls: [],
//     noReadFlash,      // the loader has no readFlash (an esptool-js that cannot read flash)
//     readFlashThrows,  // readFlash rejects
//     readFlashThrowsAt,// readFlash rejects for that address only
//     writeFails,       // the first k writeFlash calls reject
//     noAfterFlash,     // the loader has no afterFlash / hard_reset (the vendored 0.4.x has neither)
//     progressSteps }   // writeFlash reports each file's progress in this many steps, in COMPRESSED counts (half
//                       // the file's length), as esptool-js does with compress:true
// Nothing here touches a port.
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
    if (!cfg.noReadFlash) {
      this.readFlash = async (address, length) => {
        cfg.calls.push({ op: 'read', address, length });
        if (cfg.readFlashThrows || cfg.readFlashThrowsAt === address) throw new Error('read failed (fake)');
        const out = new Uint8Array(length).fill(0xFF);
        const src = (cfg.flash || {})[address];
        if (src) out.set(src.slice(0, length));
        return out;
      };
    }
    if (!cfg.noAfterFlash) this.afterFlash = async (mode) => { cfg.calls.push({ op: 'afterFlash', mode }); };
  }
  async main() {
    const cfg = S();
    cfg.calls.push({ op: 'main' });
    this.chip = { CHIP_NAME: cfg.chip };
    return cfg.chip;
  }
  async writeFlash(o) {
    const cfg = S();
    cfg.calls.push({ op: 'write', files: o.fileArray.map((f) => ({ address: f.address, length: f.data.length,
                                                                   first: f.data.charCodeAt(0) })) });
    if (cfg.writeFails > 0) { cfg.writeFails--; throw new Error('write timeout (fake)'); }
    const steps = cfg.progressSteps || 1;
    o.fileArray.forEach((f, i) => {
      const total = Math.max(1, Math.ceil(f.data.length / 2));
      for (let s = 1; s <= steps; s++) o.reportProgress(i, Math.round(total * s / steps), total);
    });
  }
}
