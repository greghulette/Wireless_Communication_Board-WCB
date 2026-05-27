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
// The branch is resolved at fetch time by getFirmwareBranch() (defaults to
// 'main'; overridable via the Advanced → Firmware Source selector).
const GITHUB_OWNER  = 'greghulette';
const GITHUB_REPO   = 'Wireless_Communication_Board-WCB';
const GITHUB_BRANCH_DEFAULT = 'main';
const GITHUB_BIN_PATH = 'Code/bin';

// Branch the flasher pulls binaries from. Defaults to 'main' (released
// firmware). The Advanced → Firmware Source selector overrides this via
// localStorage for testing unreleased branches. Never hard-code anything
// but 'main' as the default — production must always pull released firmware.
//
// Whitelist what counts as a valid branch name: alphanumerics, '.', '_',
// '-', '/'. This is the safe subset of Git ref names AND keeps the value
// from being able to inject URL operators (?, &, #, =, /../) into the
// GitHub Contents API request when interpolated. A malformed localStorage
// value (manually edited, or stuffed by a malicious page hosting this
// tool in an iframe) is dropped silently and the default is used instead.
const FW_BRANCH_RE = /^[A-Za-z0-9._/-]+$/;
function isValidFwBranch(b) {
  return typeof b === 'string' && b.length > 0 && b.length <= 100 && FW_BRANCH_RE.test(b);
}
function getFirmwareBranch() {
  try {
    const b = (localStorage.getItem('wcb_fw_branch') || '').trim();
    return isValidFwBranch(b) ? b : GITHUB_BRANCH_DEFAULT;
  } catch (_) {
    return GITHUB_BRANCH_DEFAULT;
  }
}

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
  const GITHUB_BRANCH = getFirmwareBranch();
  const apiUrl = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/${GITHUB_BIN_PATH}?ref=${GITHUB_BRANCH}`;

  try {
    if (GITHUB_BRANCH !== GITHUB_BRANCH_DEFAULT)
      onLog(`⚠ Firmware source branch: ${GITHUB_BRANCH} (NOT released 'main')`);
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
// appOnly  — if true, skip blank-board detection and always flash app at 0x10000 only
// eraseNvs — if true, prepend a blank (0xFF) image at the NVS partition address
//            (0x9000, 24 KB) so esptool erases+rewrites it, producing a factory-fresh NVS
async function flashFirmware(port, hwVersion, { onProgress, onLog, onStatus, appOnly = false, eraseNvs = false }) {

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
    // On Windows, the CP210x driver doesn't fully release the readable-stream
    // lock during the 115200→460800 baud-rate change, causing a mid-write
    // "ReadableStreamDefaultReader already locked" failure.  Keep 115200 on
    // Windows to skip the baud-rate-change step entirely (4× slower but
    // reliable).  Mac/Linux can still use 460800 for fast uploads.
    baudrate: (typeof _isWindows !== 'undefined' && _isWindows) ? 115200 : 460800,
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

  // ── Step 3b: Decide what to flash ──────────────────────────────
  let imagesToFlash;

  if (appOnly) {
    // Explicit app-only update: skip detection, never touch bootloader/NVS
    imagesToFlash = flashImages.filter(img => img.address === 0x10000);
    onLog('App-only update — bootloader, partitions, and NVS will not be touched');
  } else if (eraseNvs) {
    // Factory reset: always write the full image (bootloader + partition table + app).
    // The auto-detect below would see the existing valid bootloader (magic 0xE9) and
    // fall back to app-only, but a factory reset must guarantee a completely fresh
    // firmware install — no stale partition table, no stale bootloader.
    imagesToFlash = flashImages;
    onLog('Factory reset — flashing full image (bootloader + partitions + app)');
  } else {
    // Auto-detect: read bootloader magic to decide blank vs. programmed vs. bricked
    const bootAddr = binaryType === 'ESP32S3' ? 0x0 : 0x1000;
    let isBlankBoard  = false;
    let forceFull     = false;   // partition-table change → must write boot+part+app
    try {
      const readFlashFn = loader.readFlash ?? loader.read_flash;
      if (typeof readFlashFn === 'function') {
        const sample = await readFlashFn.call(loader, bootAddr, 4);
        const view   = new DataView(sample.buffer ?? sample);
        const magic  = view.getUint8(0);
        // 0xFF = blank, 0xE9 = valid ESP image — anything else means bricked/corrupted.
        // Three cases:
        //   0xFF → blank board (no firmware yet)             → full flash, isBlankBoard
        //   0xE9 → valid image                                → app-only fast path (unless partition mismatch)
        //   else → corrupted bootloader                       → full flash to recover (forceFull)
        // The previous `(magic === 0xFF || magic !== 0xE9)` simplified to
        // `magic !== 0xE9`, making the `=== 0xFF` clause dead. Split into
        // explicit branches so the corrupted-recovery path is no longer
        // implicit / accidental.
        isBlankBoard = (magic === 0xFF);
        if (magic === 0xFF) {
          onLog('Blank board detected — will flash bootloader + partitions + app');
        } else if (magic === 0xE9) {
          // Existing firmware. App-only is the fast path — but ONLY if the
          // on-flash partition TABLE matches the firmware we're about to
          // write. A partition-scheme change (e.g. default → min_spiffs)
          // moves app1/spiffs; flashing the new, larger app over the old
          // 1.25 MB slot would overrun. Compare the table at 0x8000 and, on
          // any mismatch (or if it can't be read), force a full
          // bootloader+partition+app flash. NVS (0x9000) is NOT in this
          // image set and is never erased here, so saved config survives.
          const partImg = flashImages.find(img => img.address === 0x8000);
          if (!partImg) {
            onLog('Existing firmware detected — will flash app only (NVS preserved)');
          } else {
            try {
              const CMP_LEN = 0x200;  // covers all partition entries; before any MD5/padding
              const want = new Uint8Array(partImg.buf, 0, Math.min(CMP_LEN, partImg.buf.byteLength));
              const got0 = await readFlashFn.call(loader, 0x8000, want.length);
              const got  = got0 instanceof Uint8Array ? got0 : new Uint8Array(got0.buffer ?? got0);
              let same = got.length >= want.length;
              for (let i = 0; same && i < want.length; i++) if (got[i] !== want[i]) same = false;
              if (same) {
                onLog('Existing firmware, partition table matches — flashing app only (NVS preserved)');
              } else {
                forceFull = true;
                onLog('⚠ Partition table changed — full flash required (boot + partitions + app).');
                onLog('  NVS at 0x9000 is NOT erased — your saved configuration is preserved.');
                showToast('Partition layout updated — performing a one-time full flash (config preserved)', 'info', 8000);
              }
            } catch (_) {
              // Couldn't verify the table — be safe, not fast: a full
              // NVS-preserving flash can never brick; app-only on a
              // mismatched table can. Force full.
              forceFull = true;
              onLog('Could not read partition table — forcing a safe full flash (NVS preserved)');
            }
          }
        } else {
          // Corrupted bootloader — explicitly force full flash so a future
          // refactor can't accidentally short-circuit recovery via the
          // app-only fast path.
          forceFull = true;
          onLog(`Corrupted bootloader detected (0x${magic.toString(16).padStart(2,'0')}) — forcing full flash to recover`);
        }
      } else {
        onLog('Cannot read flash — forcing a safe full flash (NVS preserved)');
        forceFull = true;
      }
    } catch (_) {
      onLog('Flash read check failed — forcing a safe full flash (NVS preserved)');
      forceFull = true;
    }
    imagesToFlash = (isBlankBoard || forceFull)
      ? flashImages
      : flashImages.filter(img => img.address === 0x10000);
  }

  // ── Step 3c: Optionally prepend NVS + otadata erase images ────
  // WCB partition layout (PartitionScheme=min_spiffs):
  //   nvs     @ 0x9000,  size 0x5000 (20 KB)
  //   otadata @ 0xE000,  size 0x2000  (8 KB — two 4 KB flash sectors)
  //   ota_0   @ 0x10000, size 0x1E0000 (~1.9 MB)
  //   ota_1   @ 0x1F0000
  // (nvs & otadata offsets are identical to the old `default` scheme, so the
  //  erase addresses below are unchanged across the scheme transition.)
  //
  // Writing 0xFF buffers causes esptool to erase then rewrite those sectors.
  // Both otadata sectors MUST be erased: if either sector still holds a valid
  // OTA state pointing to ota_1 (e.g. from a previous OTA update), the
  // bootloader will try to boot ota_1, fail (nothing there after a flash to
  // ota_0), and the OTA rollback watchdog fires — causing an endless reboot loop.
  if (eraseNvs) {
    const nvsBlank     = new ArrayBuffer(0x5000);  // NVS: 20 KB @ 0x9000
    const otadataBlank = new ArrayBuffer(0x2000);  // otadata: 8 KB @ 0xE000
    new Uint8Array(nvsBlank).fill(0xFF);
    new Uint8Array(otadataBlank).fill(0xFF);
    // Insert in ascending address order, before the app images
    imagesToFlash = [
      { buf: nvsBlank,     address: 0x9000 },  // erase NVS
      { buf: otadataBlank, address: 0xE000 },  // erase OTA data → bootloader defaults to ota_0
      ...imagesToFlash,
    ];
    onLog('Factory reset — NVS (0x9000, 20 KB) and OTA data (0xE000, 8 KB) will be erased');
  }

  const totalBytes = imagesToFlash.reduce((sum, img) => sum + img.buf.byteLength, 0);

  // ── Step 4: Flash ──────────────────────────────────────────────
  // CRITICAL warning to the user: between the first byte being written and
  // the bootloader + partition + app trio all completing successfully, the
  // chip is in an inconsistent state. A USB disconnect / power loss in this
  // window leaves the board unbootable (it can be recovered with esptool +
  // the BOOT button, but it's not a great UX). Make this loud.
  const willWriteBootOrPart = imagesToFlash.some(
    img => img.address === 0x0 || img.address === 0x1000 || img.address === 0x8000
  );
  if (willWriteBootOrPart) {
    onLog('⚠️  DO NOT DISCONNECT THE BOARD until flashing completes!');
    onLog('   Boot loader and partition table are being rewritten — a mid-flash');
    onLog('   disconnect can leave the board needing manual recovery.');
  }
  onStatus(`Flashing ${chip}…`);
  onLog(`Writing ${Math.round(totalBytes / 1024)} KB across ${imagesToFlash.length} region(s)…`);
  onProgress(0, totalBytes);

  // esptool-js 0.4.x renamed write_flash → writeFlash (camelCase)
  const writeFlashFn = loader.writeFlash ?? loader.write_flash;
  if (typeof writeFlashFn !== 'function') {
    try { await transport.disconnect(); } catch (_) {}
    throw new Error('esptool-js: writeFlash method not found — unexpected library version');
  }

  // One automatic retry on transient failure. USB hiccups, Windows stream
  // locks, and timing-related glitches are usually one-shot — a single
  // immediate retry often recovers without bothering the user. After two
  // failures we give a clear recovery message instead of generic error text.
  const attemptWrite = async () => {
    let bytesWritten = 0;
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
  };

  try {
    await attemptWrite();
  } catch (e1) {
    onLog(`Flash failed once (${e1.message || e1}) — retrying automatically…`);
    onProgress(0, totalBytes);
    try {
      await attemptWrite();
    } catch (e2) {
      try { await transport.disconnect(); } catch (_) {}
      // Surface a recovery message instead of just rethrowing the raw error.
      const recoveryMsg = willWriteBootOrPart
        ? `\n\n⚠️  The board may be in an unbootable state. To recover:\n` +
          `   1. Hold the BOOT button on the ESP32 while pressing RESET\n` +
          `   2. Reconnect via the Flasher and try again\n` +
          `   3. If the board still does not flash, contact support.`
        : '';
      throw new Error(`Flash write failed after retry: ${e2.message}${recoveryMsg}`);
    }
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
