// ════════════════════════════════════════════════════════════════
//  WCB Firmware Flasher
//
//  Uses esptool-js (Espressif's official browser flash library)
//  to flash ESP32 / ESP32-S3 firmware over WebSerial.
//
//  Dependencies loaded dynamically from CDN:
//    - CryptoJS  (MD5 verification required by esptool-js)
//    - esptool-js
// ════════════════════════════════════════════════════════════════

const ESPTOOL_CDN  = 'https://cdn.jsdelivr.net/npm/esptool-js@0.4.7/+esm';
const CRYPTOJS_CDN = 'https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.2.0/crypto-js.min.js';

// ─── Firmware source config ───────────────────────────────────────
// Binaries live in Code/bin/ on GitHub with versioned names like:
//   WCB_6.0_271328RFEB2026_multi_maestro_ESP32.bin
//   WCB_6.0_271328RFEB2026_multi_maestro_ESP32S3.bin
// The suffix (_ESP32.bin / _ESP32S3.bin) is stable; the prefix changes each build.
// We use the GitHub Contents API to find the right file, then fetch it.
// Update GITHUB_BRANCH when switching branches.
const GITHUB_OWNER  = 'greghulette';
const GITHUB_REPO   = 'Wireless_Communication_Board-WCB';
const GITHUB_BRANCH = 'multi_maestro';
const GITHUB_BIN_PATH = 'Code/bin';

// ─── Script loader ────────────────────────────────────────────────
function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) { resolve(); return; }
    const s    = document.createElement('script');
    s.src      = src;
    s.onload   = resolve;
    s.onerror  = () => reject(new Error(`Failed to load CDN script: ${src}`));
    document.head.appendChild(s);
  });
}

// ─── Binary fetching ──────────────────────────────────────────────
// Fetches bootloader, partition table, and app from GitHub Contents API.
// Returns an array of flash images: [{ buf, address }, ...]
//
// Flash map (NVS at 0x9000 is intentionally NOT written):
//   ESP32:   boot→0x1000  part→0x8000  app→0x10000
//   ESP32S3: boot→0x0     part→0x8000  app→0x10000
//
// Fallback: file picker returns app-only at 0x10000 (for manual recovery)
async function getBinaryData(binaryType, onLog) {
  const apiUrl = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/${GITHUB_BIN_PATH}?ref=${GITHUB_BRANCH}`;

  try {
    onLog(`Scanning ${GITHUB_BIN_PATH}/ on GitHub (${GITHUB_BRANCH})…`);
    const listResp = await fetch(apiUrl);
    if (!listResp.ok) throw new Error(`GitHub API: HTTP ${listResp.status}`);
    const files = await listResp.json();

    // Find a file by suffix and fetch its binary content
    async function fetchBySuffix(suffix) {
      const match = files.find(f => f.type === 'file' && f.name.endsWith(suffix));
      if (!match) throw new Error(`No file ending with "${suffix}" found in ${GITHUB_BIN_PATH}/`);
      onLog(`Found: ${match.name}`);
      const r = await fetch(match.download_url);
      if (!r.ok) throw new Error(`HTTP ${r.status} fetching ${match.name}`);
      const buf = await r.arrayBuffer();
      if (buf.byteLength === 0) throw new Error(`${match.name} is empty`);
      return buf;
    }

    // ESP32-S3 bootloader lives at 0x0; classic ESP32 uses 0x1000
    const bootAddr = binaryType === 'ESP32S3' ? 0x0 : 0x1000;

    // Fetch app (required) + bootloader and partitions (optional — may not exist yet
    // on first build after adding multi-file support)
    const appBuf = await fetchBySuffix(`_${binaryType}.bin`);
    const images = [{ buf: appBuf, address: 0x10000 }];

    let hasBootPart = true;
    try {
      const [bootBuf, partBuf] = await Promise.all([
        fetchBySuffix(`_${binaryType}_boot.bin`),
        fetchBySuffix(`_${binaryType}_part.bin`),
      ]);
      images.unshift(
        { buf: bootBuf, address: bootAddr },
        { buf: partBuf, address: 0x8000  },
      );
    } catch (_) {
      hasBootPart = false;
      onLog('Note: bootloader/partition files not on GitHub yet — flashing app only');
      showToast('Boot files not found — flashing app only (blank boards may need manual flash)', 'info', 8000);
    }

    const totalKB = Math.round(images.reduce((s, i) => s + i.buf.byteLength, 0) / 1024);
    onLog(`Loaded ${totalKB} KB (${hasBootPart ? 'boot + partitions + app' : 'app only'})`);
    return images;

  } catch (e) {
    onLog(`GitHub fetch failed: ${e.message}`);
    showToast(`Could not fetch firmware from GitHub: ${e.message}`, 'error');
    throw new Error(`GitHub fetch failed: ${e.message}`);
  }
}

// File picker fallback — only used if GitHub fetch fails
function pickBinaryFile() {
  return new Promise((resolve, reject) => {
    const input   = document.createElement('input');
    input.type    = 'file';
    input.accept  = '.bin';

    input.addEventListener('change', async () => {
      const file = input.files[0];
      if (!file) { reject(new Error('No file selected')); return; }
      try {
        const buf = await file.arrayBuffer();
        if (buf.byteLength === 0) {
          reject(new Error(`"${file.name}" is empty`));
        } else {
          resolve(buf);
        }
      } catch (e) {
        reject(e);
      }
    });

    // Modern Chrome (113+) fires a 'cancel' event when the picker is dismissed
    input.addEventListener('cancel', () => reject(new Error('No file selected')));

    document.body.appendChild(input);
    input.click();
    document.body.removeChild(input);
  });
}

// ─── Buffer helpers ───────────────────────────────────────────────
// esptool-js requires flash data as a Latin1 string, not a Uint8Array
function bufToLatin1(buf) {
  const u8 = new Uint8Array(buf);
  let s = '';
  // Process in chunks to avoid call stack overflow on large binaries
  const CHUNK = 65536;
  for (let i = 0; i < u8.length; i += CHUNK) {
    s += String.fromCharCode.apply(null, u8.subarray(i, i + CHUNK));
  }
  return s;
}

// ─── Main flash function ──────────────────────────────────────────
//
// port       — WebSerial SerialPort, must be CLOSED before calling
// hwVersion  — numeric HW version (1, 21, 23, 24, 31, 32)
// callbacks  — { onProgress(written, total), onLog(msg), onStatus(msg) }
//
// Throws on error (caller is responsible for UI cleanup).
async function flashFirmware(port, hwVersion, { onProgress, onLog, onStatus }) {

  // ── Step 1: Load CDN dependencies ──────────────────────────────
  onStatus('Loading flash tool…');

  onLog('Loading CryptoJS…');
  try {
    await loadScript(CRYPTOJS_CDN);
  } catch (e) {
    throw new Error(`Could not load CryptoJS from CDN — are you online?\n${e.message}`);
  }

  onLog('Loading esptool-js…');
  let ESPLoader, Transport;
  try {
    ({ ESPLoader, Transport } = await import(ESPTOOL_CDN));
  } catch (e) {
    throw new Error(`Could not load esptool-js from CDN — are you online?\n${e.message}`);
  }
  onLog('Flash tool loaded');

  // ── Step 2: Get firmware images ─────────────────────────────────
  const binaryType = WCBParser.HW_VERSION_MAP[String(hwVersion)]?.binary ?? 'ESP32';
  onLog(`HW${hwVersion} → ${binaryType} binary`);
  onStatus(`Loading ${binaryType} firmware…`);

  let flashImages;  // [{ buf: ArrayBuffer, address: number }, ...]
  try {
    flashImages = await getBinaryData(binaryType, onLog);
  } catch (e) {
    throw new Error(e.message);   // 'No file selected' or fetch error
  }

  // ── Step 3: Connect to ESP bootloader ──────────────────────────
  onStatus('Connecting to bootloader…');
  onLog('Connecting to ESP bootloader…');
  onLog('► Hold BOOT, tap RST, release BOOT — then watch for sync attempts below');

  // Route esptool-js internal logs to the terminal so the user can see sync progress
  const terminal = {
    clean:     ()    => {},
    writeLine: (msg) => { if (msg?.trim()) onLog(`[esptool] ${msg.trim()}`); },
    write:     (msg) => { if (msg?.trim()) onLog(`[esptool] ${msg.trim()}`); },
  };

  const transport = new Transport(port, false);
  const loader    = new ESPLoader({
    transport,
    baudrate:    460800,   // upload baud (esptool-js auto-syncs at 115200 first)
    romBaudrate: 115200,
    enableTracing: false,
    terminal,
  });

  let chip;
  try {
    chip = await loader.main();
    onLog(`Chip identified: ${chip}`);
  } catch (e) {
    try { await transport.disconnect(); } catch (_) {}
    throw new Error(
      `Bootloader connection failed: ${e.message}\n\n` +
      `To enter bootloader mode: hold BOOT, press RST, release BOOT, then try again.`
    );
  }

  // ── Step 3b: Detect blank vs. programmed board ─────────────────
  // Read 4 bytes at the bootloader address; 0xFFFFFFFF = erased flash (blank board).
  // On a programmed board the bootloader starts with 0xE9 (ESP image magic).
  // If already programmed, skip bootloader+partitions to preserve NVS.
  const bootAddr = binaryType === 'ESP32S3' ? 0x0 : 0x1000;
  let isBlankBoard = false;
  try {
    const readFlashFn = loader.readFlash ?? loader.read_flash;
    if (typeof readFlashFn === 'function') {
      const sample = await readFlashFn.call(loader, bootAddr, 4);
      const view   = new DataView(sample.buffer ?? sample);
      const magic  = view.getUint8(0);
      // 0xFF = blank, 0xE9 = valid ESP image — anything else means bricked/corrupted
      const needsFullFlash = (magic === 0xFF || magic !== 0xE9);
      isBlankBoard = needsFullFlash;
      if (magic === 0xFF) {
        onLog('Blank board detected — will flash bootloader + partitions + app');
      } else if (magic === 0xE9) {
        onLog('Existing firmware detected — will flash app only (NVS preserved)');
      } else {
        onLog(`Corrupted bootloader detected (0x${magic.toString(16).padStart(2,'0')}) — will flash full image to recover`);
      }
    } else {
      // Can't read flash — assume programmed board to be safe
      onLog('Cannot read flash — assuming existing firmware, flashing app only');
    }
  } catch (_) {
    onLog('Flash read check failed — assuming existing firmware, flashing app only');
  }

  // Filter images: blank board gets all three; programmed board gets app only
  const imagesToFlash = isBlankBoard
    ? flashImages
    : flashImages.filter(img => img.address === 0x10000);
  const totalBytes = imagesToFlash.reduce((sum, img) => sum + img.buf.byteLength, 0);

  // ── Step 4: Flash ──────────────────────────────────────────────
  onStatus(`Flashing ${chip}…`);
  onLog(`Writing ${Math.round(totalBytes / 1024)} KB across ${imagesToFlash.length} region(s)…`);
  onProgress(0, totalBytes);

  // esptool-js 0.4.x renamed write_flash → writeFlash (camelCase)
  const writeFlashFn = loader.writeFlash ?? loader.write_flash;
  if (typeof writeFlashFn !== 'function') {
    try { await transport.disconnect(); } catch (_) {}
    throw new Error('esptool-js: writeFlash method not found — unexpected library version');
  }

  let bytesWritten = 0;
  try {
    await writeFlashFn.call(loader, {
      fileArray: imagesToFlash.map(img => ({ data: bufToLatin1(img.buf), address: img.address })),
      flashSize: 'keep',
      flashMode: 'keep',
      flashFreq: 'keep',
      eraseAll:  false,
      compress:  true,
      reportProgress: (_fileIdx, written, total) => {
        // esptool-js resets written/total per file; accumulate for overall progress
        onProgress(bytesWritten + written, totalBytes);
        if (written === total) bytesWritten += total;
      },
      calculateMD5Hash: (img) =>
        CryptoJS.MD5(CryptoJS.enc.Latin1.parse(img)).toString(),
    });
  } catch (e) {
    try { await transport.disconnect(); } catch (_) {}
    throw new Error(`Flash write failed: ${e.message}`);
  }

  // ── Step 5: Reset into firmware ────────────────────────────────
  onLog('Resetting board into firmware…');
  onStatus('Resetting…');
  onProgress(totalBytes, totalBytes);

  // esptool-js 0.4.x renamed after_flash → afterFlash
  const afterFlashFn = loader.afterFlash ?? loader.after_flash;
  try { if (afterFlashFn) await afterFlashFn.call(loader, 'hard_reset'); } catch (_) {}
  try { await transport.disconnect(); }                                     catch (_) {}

  onLog('Flash complete — board rebooting');
  onStatus('Flash complete!');
}
