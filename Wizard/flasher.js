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
// Fetches bootloader(s), partition table, and app from GitHub Contents API.
// Returns { images, boot, hasBootPart }:
//   images — [{ buf, address }, ...]: partition (0x8000, when available) and
//            app (0x10000). The bootloader is intentionally NOT in this list —
//            it is selected at flash time, AFTER the real chip and flash size
//            have been detected (see flashFirmware Step 3a).
//   boot   — bootloader candidate buffers:
//              ESP32   → { single: buf|null }                  flashed at 0x1000
//              ESP32S3 → { '8MB': buf|null, '16MB': buf|null } flashed at 0x0
//            The S3 bootloader header declares the flash size; writing the
//            wrong-size variant silently corrupts NVS, so BOTH sizes are
//            fetched here and the matching one is chosen after detection.
//            Legacy builds publish only `_ESP32S3_boot.bin`, which IS the
//            16 MB build — used as the 16MB fallback when the sized name
//            is missing.
//
// Flash map (NVS at 0x9000 is intentionally NOT written):
//   ESP32:   boot→0x1000  part→0x8000  app→0x10000
//   ESP32S3: boot→0x0     part→0x8000  app→0x10000
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

    // Fetch app (required) + bootloader and partitions (optional — may not exist yet
    // on first build after adding multi-file support)
    const appBuf = await fetchBySuffix(`_${binaryType}.bin`);
    const images = [{ buf: appBuf, address: 0x10000 }];
    const boot   = binaryType === 'ESP32S3'
      ? { '8MB': null, '16MB': null }
      : { single: null };

    let hasBootPart = true;
    try {
      const partBuf = await fetchBySuffix(`_${binaryType}_part.bin`);
      if (binaryType === 'ESP32S3') {
        // 16 MB: prefer the sized name; the legacy unsized bin IS the 16 MB
        // build (kept published for back-compat).
        try {
          boot['16MB'] = await fetchBySuffix(`_${binaryType}_boot_16MB.bin`);
        } catch (_) {
          boot['16MB'] = await fetchBySuffix(`_${binaryType}_boot.bin`);
        }
        // 8 MB: sized name only. Missing is non-fatal here — the flash-time
        // guard refuses to write the bootloader on an 8 MB board without it.
        try {
          boot['8MB'] = await fetchBySuffix(`_${binaryType}_boot_8MB.bin`);
        } catch (e8) {
          onLog(`⚠️ No 8 MB ESP32-S3 bootloader available (${e8.message}) — 8 MB boards cannot receive a bootloader this run`);
        }
      } else {
        boot.single = await fetchBySuffix(`_${binaryType}_boot.bin`);
      }
      images.unshift({ buf: partBuf, address: 0x8000 });
    } catch (e) {
      // The _boot.bin/_part.bin files DO exist on GitHub — landing here means
      // a transient fetch failure (rate limit, network). Without the partition
      // image we cannot run the partition-table migration check, so make this
      // loud instead of pretending it's expected.
      hasBootPart = false;
      onLog(`⚠️ Could not fetch bootloader/partition files (${e.message}) — flashing app only;`);
      onLog('   the partition-table migration check was SKIPPED this run. If this persists,');
      onLog('   wait a minute (GitHub rate limit) and try again.');
      showToast(`⚠️ Could not fetch bootloader/partition files (${e.message}) — flashing app only; partition-table migration check SKIPPED this run`, 'warning', 10000);
    }

    const bootKB = Object.values(boot).reduce((s, b) => s + (b ? b.byteLength : 0), 0);
    const totalKB = Math.round((images.reduce((s, i) => s + i.buf.byteLength, 0) + bootKB) / 1024);
    onLog(`Loaded ${totalKB} KB (${hasBootPart ? 'boot + partitions + app' : 'app only'})`);
    return { images, boot, hasBootPart };

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

// ─── Partition-table comparison ──────────────────────────────────
// Compares the on-board partition table at 0x8000 against the new partition
// image we're about to flash. Returns:
//   'match'      — tables identical, app-only flash is safe
//   'mismatch'   — partition scheme changed (e.g. default → min_spiffs);
//                  a full bootloader+partitions+app flash is required
//   'unreadable' — flash read unavailable/failed; caller decides how to react
//                  (auto-detect mode forces a safe full flash; update mode
//                  stays conservative and keeps app-only)
async function comparePartitionTable(loader, partImg) {
  try {
    const readFlashFn = loader.readFlash ?? loader.read_flash;
    if (typeof readFlashFn !== 'function') return 'unreadable';
    const CMP_LEN = 0x200;  // covers all partition entries; before any MD5/padding
    const want = new Uint8Array(partImg.buf, 0, Math.min(CMP_LEN, partImg.buf.byteLength));
    const got0 = await readFlashFn.call(loader, 0x8000, want.length);
    const got  = got0 instanceof Uint8Array ? got0 : new Uint8Array(got0.buffer ?? got0);
    let same = got.length >= want.length;
    for (let i = 0; same && i < want.length; i++) if (got[i] !== want[i]) same = false;
    return same ? 'match' : 'mismatch';
  } catch (_) {
    return 'unreadable';
  }
}

// ─── Flash-size detection ────────────────────────────────────────
// Asks the connected chip for its real SPI flash size. esptool-js 0.4.7
// exposes ESPLoader.getFlashSize() (returns KB, mapped from the SPI flash ID
// via DETECTED_FLASH_SIZES_NUM); readFlashId() is the lower-level fallback —
// bits 16-23 of the flash ID are log2(size in bytes). Returns the size in
// whole MB (e.g. 8 or 16), or null if it cannot be determined — callers must
// treat null as "do NOT write the bootloader" (its header declares the flash
// size; a mismatch silently corrupts NVS).
async function detectFlashSizeMB(loader) {
  try {
    const getFlashSizeFn = loader.getFlashSize ?? loader.get_flash_size;
    if (typeof getFlashSizeFn === 'function') {
      const kb = await getFlashSizeFn.call(loader);
      if (Number.isFinite(kb) && kb > 0) return kb / 1024;
    }
  } catch (_) { /* fall through to raw flash-ID read */ }
  try {
    const readFlashIdFn = loader.readFlashId ?? loader.read_flash_id ?? loader.flashId ?? loader.flash_id;
    if (typeof readFlashIdFn === 'function') {
      const flashId = await readFlashIdFn.call(loader);
      const szId = (flashId >> 16) & 0xFF;
      if (szId >= 0x12 && szId <= 0x19) return (1 << szId) / (1024 * 1024);
    }
  } catch (_) { /* undetectable */ }
  return null;
}

// ─── Main flash function ──────────────────────────────────────────
//
// port       — WebSerial SerialPort, must be CLOSED before calling
// hwVersion  — numeric HW version (1, 21, 23, 24, 31, 32). Used only as a
//              pre-fetch hint for which firmware family to download — the
//              CONNECTED CHIP is authoritative (Step 3a re-fetches and logs
//              if the manual selection was wrong).
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

  // ── Step 2: Pre-fetch firmware images ───────────────────────────
  // The hwVersion-derived type is only a pre-fetch GUESS so the download can
  // happen before the bootloader handshake. Step 3a detects the real chip
  // family and re-fetches if the manual selection was wrong.
  let binaryType = WCBParser.HW_VERSION_MAP[String(hwVersion)]?.binary ?? 'ESP32';
  if (hwVersion) onLog(`HW${hwVersion} → ${binaryType} binary (pre-fetch; chip auto-detection is authoritative)`);
  else           onLog(`No HW version selected — pre-fetching ${binaryType} binary (chip auto-detection is authoritative)`);
  onStatus(`Loading ${binaryType} firmware…`);

  let fw;  // { images, boot, hasBootPart } — see getBinaryData
  try {
    fw = await getBinaryData(binaryType, onLog);
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

  // ── Step 3a: Detect chip family + flash size, select bootloader ──
  // The connected chip is the ground truth. If the user picked the wrong HW
  // version (e.g. selected 3.2 but plugged in a classic ESP32 board), flashing
  // the hwVersion-mapped binary would brick the board — detection overrides.
  let detectedType = null;
  const chipName = loader?.chip?.CHIP_NAME ?? String(chip ?? '');
  if (/ESP32[-_]?S3/i.test(chipName)) detectedType = 'ESP32S3';
  else if (/ESP32/i.test(chipName))   detectedType = 'ESP32';

  if (detectedType && detectedType !== binaryType) {
    onLog(`⚠ Detected chip "${chipName}" does not match the selected HW version (${binaryType} firmware)`);
    onLog(`  → auto-detection overrides the manual selection: flashing ${detectedType} firmware instead`);
    showToast(`Detected ${detectedType === 'ESP32S3' ? 'an ESP32-S3' : 'a classic ESP32'} chip — flashing ${detectedType} firmware (overrides the selected HW version)`, 'warning', 10000);
    binaryType = detectedType;
    onStatus(`Loading ${binaryType} firmware…`);
    fw = await getBinaryData(binaryType, onLog);   // re-fetch the right family
  } else if (!detectedType) {
    onLog(`Could not determine chip family from "${chipName}" — falling back to the HW version selection (${binaryType})`);
  } else {
    onLog(`Chip family confirmed: ${detectedType}`);
  }

  // Real flash size — needed to pick the right S3 bootloader (its header
  // declares the flash size; a mismatched header silently corrupts NVS).
  const flashSizeMB = await detectFlashSizeMB(loader);
  if (flashSizeMB) onLog(`Flash size detected: ${flashSizeMB} MB`);
  else             onLog('⚠ Could not detect flash size');

  // ESP32-S3 bootloader lives at 0x0; classic ESP32 uses 0x1000
  const bootAddr = binaryType === 'ESP32S3' ? 0x0 : 0x1000;
  let bootImage       = null;  // { buf, address } | null — included in the full image set
  let bootBlockReason = null;  // set → any path that writes the bootloader must ABORT
  if (binaryType === 'ESP32S3') {
    const candidate = flashSizeMB === 16 ? fw.boot['16MB']
                    : flashSizeMB === 8  ? fw.boot['8MB']
                    : null;
    if (candidate) {
      bootImage = { buf: candidate, address: bootAddr };
      onLog(`Selected the ${flashSizeMB} MB ESP32-S3 bootloader`);
    } else if (fw.hasBootPart) {
      bootBlockReason = flashSizeMB
        ? `No ${flashSizeMB} MB ESP32-S3 bootloader is available`
        : 'Cannot determine flash size';
    }
    // !fw.hasBootPart → transient fetch failure already warned loudly in
    // getBinaryData; no bootloader exists to write, so nothing to block.
  } else if (fw.boot.single) {
    // Classic ESP32: single compiled bootloader (PICO, 8 MB) — unchanged.
    bootImage = { buf: fw.boot.single, address: bootAddr };
  }

  // Full image set: bootloader (when selected) + partitions + app.
  // [{ buf: ArrayBuffer, address: number }, ...]
  const flashImages = bootImage ? [bootImage, ...fw.images] : fw.images;

  // ── Step 3b: Decide what to flash ──────────────────────────────
  let imagesToFlash;

  if (appOnly) {
    // Explicit app-only update — but FIRST check for a partition-scheme
    // migration (default → min_spiffs). Boards that only ever use "Update FW"
    // would otherwise never migrate to the new table, and once the app
    // outgrows the old scheme's app slot an app-only write would brick them.
    // On mismatch, escalate ONCE to the full image set (bootloader +
    // partitions + app). NVS at 0x9000 is NOT in that set and is never
    // erased here, so saved configuration survives.
    let migrate = false;
    const partImg = flashImages.find(img => img.address === 0x8000);
    if (partImg) {
      const cmp = await comparePartitionTable(loader, partImg);
      if (cmp === 'mismatch') {
        migrate = true;
        onLog('⚠ Partition table mismatch — performing one-time full update to migrate to the new layout (settings preserved)');
        showToast('Partition layout updated — performing a one-time full update (settings preserved)', 'info', 8000);
      } else if (cmp === 'match') {
        onLog('Partition table matches — proceeding with app-only update');
      } else {
        // Update mode is conservative by contract: a failed read must NOT
        // force a full flash. But that is only safe while the app still fits
        // the OLD default-scheme ota_0 slot (0x140000) — on an un-migrated
        // board a larger app would overrun into the next partition. If we
        // can't verify the layout AND the app no longer fits, refuse.
        const OLD_OTA0_SIZE = 0x140000;  // default-scheme ota_0 slot size
        const appImg = flashImages.find(img => img.address === 0x10000);
        if (appImg && appImg.buf.byteLength > OLD_OTA0_SIZE) {
          const msg = 'Cannot verify partition layout and the firmware no longer fits the legacy layout — use the full Flash path';
          onLog(`✕ ${msg}`);
          showToast(msg, 'error', 10000);
          try { await transport.disconnect(); } catch (_) {}
          throw new Error(msg);
        }
        onLog('Could not read on-board partition table — migration check skipped, flashing app only');
      }
    } else {
      onLog('No partition image available — migration check skipped, flashing app only');
    }
    if (migrate) {
      imagesToFlash = flashImages;
      onLog('Full update — bootloader, partitions, and app will be written (NVS untouched)');
    } else {
      imagesToFlash = flashImages.filter(img => img.address === 0x10000);
      onLog('App-only update — bootloader, partitions, and NVS will not be touched');
    }
  } else if (eraseNvs) {
    // Factory reset: always write the full image (bootloader + partition table + app).
    // The auto-detect below would see the existing valid bootloader (magic 0xE9) and
    // fall back to app-only, but a factory reset must guarantee a completely fresh
    // firmware install — no stale partition table, no stale bootloader.
    imagesToFlash = flashImages;
    onLog('Factory reset — flashing full image (bootloader + partitions + app)');
  } else {
    // Auto-detect: read bootloader magic to decide blank vs. programmed vs. bricked
    // (bootAddr was derived from the DETECTED chip family in Step 3a)
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
            const cmp = await comparePartitionTable(loader, partImg);
            if (cmp === 'match') {
              onLog('Existing firmware, partition table matches — flashing app only (NVS preserved)');
            } else if (cmp === 'mismatch') {
              forceFull = true;
              onLog('⚠ Partition table changed — full flash required (boot + partitions + app).');
              onLog('  NVS at 0x9000 is NOT erased — your saved configuration is preserved.');
              showToast('Partition layout updated — performing a one-time full flash (config preserved)', 'info', 8000);
            } else {
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

  // ── Step 3b-guard: NEVER write a mismatched bootloader ─────────
  // Every path that intends a full flash (blank board, factory reset,
  // forceFull recovery, partition migration) assigns the full `flashImages`
  // set, which would write the bootloader region. If no size-matched
  // bootloader could be selected (flash size undetectable, or no bootloader
  // built for the detected size), ABORT — a bootloader whose header declares
  // the wrong flash size silently corrupts NVS. App-only updates filter down
  // to 0x10000 and never touch the bootloader, so they pass through.
  if (imagesToFlash === flashImages && bootBlockReason) {
    const msg = `${bootBlockReason} / no matching bootloader — refusing to write bootloader region`;
    onLog(`✕ ${msg}`);
    onLog('  (a bootloader built for the wrong flash size silently corrupts saved settings)');
    showToast(msg, 'error', 12000);
    try { await transport.disconnect(); } catch (_) {}
    throw new Error(msg);
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
