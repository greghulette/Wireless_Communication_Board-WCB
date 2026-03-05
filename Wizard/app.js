// ════════════════════════════════════════════════════════════════
//  WCB Config Tool — app.js
//  WebSerial logic, BoardConnection class, UI handlers,
//  file import/export, command builder integration
// ════════════════════════════════════════════════════════════════

const BAUD_RATES = [110,300,600,1200,2400,9600,14400,19200,38400,57600,115200,128000,256000];

// HW_VERSION_MAP is defined in parser.js — use WCBParser.HW_VERSION_MAP

// ─── App State ────────────────────────────────────────────────────
let appMode        = 'simple';  // 'simple' | 'advanced'
let systemConfig   = null;
let boardConnections = {};      // { boardIndex: BoardConnection }
let boardConfigs     = {};      // { boardIndex: BoardConfig } — current UI state
let boardBaselines   = {};      // { boardIndex: BoardConfig } — last pulled from board
let boardFlashMode   = {};      // { boardIndex: 'configure'|'update'|'flash'|'factory' }
let postFlashGeneralSnapshot = {};  // { boardIndex: generalSnapshot } — saved before flash, restored on next push

// ─── Connect Modal State ───────────────────────────────────────────
let _connectModalSlot = null;   // which board slot the modal is open for
const _detecting = {};          // { [n]: true/false } — auto-detect active per slot

// ─── Remote Management State ───────────────────────────────────────
let remoteRelayForBoard = {};     // { boardSlot: relaySlot } — set when board is reached via relay
const _etmCallbacks = {};         // { relaySlot: callback } — one ETM listener per relay board
const MGMT_CHUNK_SIZE  = 180;   // max payload chars per ESP-NOW packet
const MGMT_CHUNK_DELAY = 250;   // ms between chunks — gives relay time to forward

// ─── General Settings Baseline ────────────────────────────────────
let generalBaseline = null;     // { sourceBoard: n|'file', fields: {...} } — source of truth

// ─── Wizard / Firmware Version ────────────────────────────────────
let _wizardOpen = false;             // suppress mismatch modals while wizard is open
let latestFirmwareVersion = null;    // e.g. 'v6.0' — fetched from GitHub on load

// ─── Init ─────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  checkBrowserCompat();
  loadThemePreference();
  initSystemConfig();
  loadModePreference();
  initTerminalResize();
  showSplash();
  fetchLatestFirmwareVersion();  // silent, best-effort
});

// ─── Firmware Version (fetched from GitHub) ───────────────────────
async function fetchLatestFirmwareVersion() {
  try {
    const r = await fetch(
      'https://api.github.com/repos/greghulette/Wireless_Communication_Board-WCB/contents/Code/bin?ref=multi_maestro'
    );
    if (!r.ok) return;
    const files = await r.json();
    const appFile = files.find(f => f.type === 'file' && f.name.endsWith('_ESP32.bin'));
    if (!appFile) return;
    const m = appFile.name.match(/WCB_([\d.]+_\d{6}R[A-Z]{3}\d{4})_/);
    if (!m) return;
    latestFirmwareVersion = `v${m[1]}`;
    // Re-evaluate version display for any boards that already have a version from the board
    for (let n = 1; n <= 8; n++) {
      if (boardConfigs[n]?.fwVersion) updateBoardSwVersionDisplay(n);
    }
  } catch (_) { /* offline or rate-limited — ignore */ }
}

// Parse the build timestamp embedded in a WCB version string.
// Format: "6.0_DDHHMM R MMMYYYY"  e.g. "6.0_031250RMAR2026" → March 3 2026 12:50
// Returns a Date for comparison, or null if the format is unrecognised.
function parseWCBVersion(ver) {
  const s = ver.startsWith('v') ? ver.slice(1) : ver;
  const m = s.match(/^[\d.]+_(\d{2})(\d{2})(\d{2})R([A-Z]{3})(\d{4})$/);
  if (!m) return null;
  const [, dd, hh, mm, mon, yyyy] = m;
  const MONTHS = { JAN:0, FEB:1, MAR:2, APR:3, MAY:4, JUN:5, JUL:6, AUG:7, SEP:8, OCT:9, NOV:10, DEC:11 };
  const month = MONTHS[mon];
  if (month === undefined) return null;
  return new Date(parseInt(yyyy), month, parseInt(dd), parseInt(hh), parseInt(mm));
}

// Shows the board's installed version and compares it against the latest GitHub version.
// Green  ✓      = up to date (or GitHub unavailable)
// Cyan   (dev)  = board is AHEAD of GitHub (local dev build)
// Yellow ↑      = board is BEHIND GitHub (update available) — also shows Update FW button
function updateBoardSwVersionDisplay(n) {
  const el        = document.getElementById(`b${n}-sw-version`);
  const updateBtn = document.getElementById(`b${n}-btn-update-fw`);
  if (!el) return;

  const _setUpdateBtn = (show) => { if (updateBtn) updateBtn.style.display = show ? '' : 'none'; };

  const boardVer = boardConfigs[n]?.fwVersion;
  if (!boardVer) {
    el.textContent = '—';
    el.style.color = 'var(--text3)';
    el.title = '';
    _setUpdateBtn(false);
    return;
  }
  const display = boardVer.startsWith('v') ? boardVer : `v${boardVer}`;
  const latest  = latestFirmwareVersion;   // e.g. 'v6.0_031250RMAR2026', or null if offline

  if (!latest || display === latest) {
    el.textContent = `${display} ✓`;
    el.style.color  = 'var(--green)';
    el.title = latest ? 'Up to date' : 'Installed on board (GitHub version unavailable)';
    _setUpdateBtn(false);
    return;
  }

  // Versions differ — parse timestamps to determine direction
  const boardDate  = parseWCBVersion(display);
  const latestDate = parseWCBVersion(latest);

  if (boardDate && latestDate) {
    if (boardDate > latestDate) {
      // Board is ahead of GitHub — local dev build
      el.textContent = `${display} (dev)`;
      el.style.color  = 'var(--accent)';
      el.title = `Dev build — ahead of GitHub (GitHub: ${latest})`;
      _setUpdateBtn(false);
    } else {
      // Board is behind GitHub — update available
      el.textContent = `${display} ↑`;
      el.style.color  = 'var(--yellow)';
      el.title = `Update available: ${latest}`;
      _setUpdateBtn(true);
    }
  } else {
    // Timestamps unrecognisable — flag the mismatch without assuming direction
    el.textContent = `${display} ≠`;
    el.style.color  = 'var(--yellow)';
    el.title = `Version differs from GitHub (${latest})`;
    _setUpdateBtn(true);
  }
}

function setBoardSwVersion(n, ver, installed = false) {
  const el = document.getElementById(`b${n}-sw-version`);
  if (!el) return;
  el.textContent = installed ? `${ver} ✓` : ver;
  el.style.color = installed ? 'var(--green)' : 'var(--text3)';
}

function checkBrowserCompat() {
  if (!('serial' in navigator)) {
    document.getElementById('browser-warning').classList.remove('hidden');
  }
}

// ─── Splash ───────────────────────────────────────────────────────
function showSplash() {
  document.getElementById('splash-overlay').classList.add('open');
}

function splashGoWizard() {
  document.getElementById('splash-overlay').classList.remove('open');
  openWizard();
}

function splashGoConfig() {
  document.getElementById('splash-overlay').classList.remove('open');
}

function loadModePreference() {
  const saved = localStorage.getItem('wcb-mode') || 'simple';
  setMode(saved, true);
}

function initSystemConfig() {
  systemConfig = WCBParser.createDefaultSystemConfig();
  renderBoards(1);
}

// ─── Theme ────────────────────────────────────────────────────────
function toggleTheme() {
  const isLight = document.documentElement.classList.toggle('light');
  localStorage.setItem('wcb-theme', isLight ? 'light' : 'dark');
  document.getElementById('theme-toggle').textContent = isLight ? '☀️' : '🌙';
}

function loadThemePreference() {
  const saved = localStorage.getItem('wcb-theme');
  if (saved === 'light') {
    document.documentElement.classList.add('light');
    document.getElementById('theme-toggle').textContent = '☀️';
  }
}

// ─── Mode Toggle ──────────────────────────────────────────────────
function setMode(mode, silent = false) {
  appMode = mode;
  localStorage.setItem('wcb-mode', mode);
  document.getElementById('btn-simple').classList.toggle('active', mode === 'simple');
  document.getElementById('btn-advanced').classList.toggle('active', mode === 'advanced');

  // Show/hide all advanced-only elements
  document.querySelectorAll('.advanced-only').forEach(el => {
    if (mode === 'advanced') el.classList.remove('hidden');
    else el.classList.add('hidden');
  });

  // ETM detail: only show when advanced AND ETM checkbox is checked
  const etmEnabled = document.getElementById('g-etm-enabled')?.checked ?? false;
  const etmDetail  = document.getElementById('etm-detail');
  if (etmDetail) {
    if (mode === 'advanced' && etmEnabled) etmDetail.classList.add('visible');
    else etmDetail.classList.remove('visible');
  }
}

// ─── Section Toggle ───────────────────────────────────────────────
function toggleSection(id) {
  document.getElementById(id).classList.toggle('open');
}

// ─── Render Boards ────────────────────────────────────────────────
function renderBoards(count) {
  const container = document.getElementById('boards-container');
  const existing  = container.querySelectorAll('[id^="section-board-"]').length;

  if (count > existing) {
    for (let i = existing + 1; i <= count; i++) addBoardSection(i);
  } else if (count < existing) {
    for (let i = existing; i > count; i--) {
      document.getElementById(`section-board-${i}`)?.remove();
      delete boardConnections[i];
      delete boardConfigs[i];
    }
  }
}

function addBoardSection(n) {
  const template = document.getElementById('board-template');
  const html     = template.innerHTML.replace(/\{N\}/g, n);
  const temp     = document.createElement('div');
  temp.innerHTML = html;
  const section  = temp.firstElementChild;
  section.classList.add(`board-color-${((n - 1) % 8) + 1}`);
  document.getElementById('boards-container').appendChild(section);

  renderSerialTable(n);
  boardConfigs[n] = WCBParser.createDefaultBoardConfig();
  boardConfigs[n].wcbNumber = n;
  const wcbNumSel = document.getElementById(`b${n}-wcb-number`);
  if (wcbNumSel) wcbNumSel.value = n;
}

// ─── Serial Table ─────────────────────────────────────────────────
function renderSerialTable(n) {
  const tbody = document.getElementById(`b${n}-serial-tbody`);
  if (!tbody) return;
  tbody.innerHTML = '';

  for (let p = 1; p <= 5; p++) {
    const maxBaud = p >= 3 ? 57600 : Infinity;
    const baudOptions = BAUD_RATES.filter(b => b <= maxBaud).map(b =>
      `<option value="${b}" ${b === 9600 ? 'selected' : ''}>${b.toLocaleString()}</option>`
    ).join('');

    const row = document.createElement('tr');
    row.id = `b${n}-serial-row-${p}`;
    row.innerHTML = `
      <td>S${p}</td>
      <td><select id="b${n}-s${p}-baud" onchange="onSerialFieldChange(${n})">${baudOptions}</select></td>
      <td><label class="toggle" style="justify-content:center">
        <input type="checkbox" id="b${n}-s${p}-bcin" checked onchange="onSerialFieldChange(${n})">
        <span class="toggle-track"></span>
      </label></td>
      <td><label class="toggle" style="justify-content:center">
        <input type="checkbox" id="b${n}-s${p}-bcout" checked onchange="onSerialFieldChange(${n})">
        <span class="toggle-track"></span>
      </label></td>
      <td><input type="text" id="b${n}-s${p}-label" placeholder="Label…" maxlength="20"
        oninput="onSerialFieldChange(${n})" spellcheck="false"></td>
      <td><span class="claimed-note" id="b${n}-s${p}-claim"></span></td>
    `;
    tbody.appendChild(row);
  }
  updateKyberPortDropdown(n);
}

function updatePortClaimUI(n) {
  const config = boardConfigs[n];
  if (!config) return;

  for (let p = 1; p <= 5; p++) {
    const claim   = config.serialPorts[p - 1].claimedBy;
    const row     = document.getElementById(`b${n}-serial-row-${p}`);
    if (!row) continue;

    row.classList.toggle('claimed', claim !== null);

    const claimNote = document.getElementById(`b${n}-s${p}-claim`);
    const baudSel   = document.getElementById(`b${n}-s${p}-baud`);
    const bcin      = document.getElementById(`b${n}-s${p}-bcin`);
    const bcout     = document.getElementById(`b${n}-s${p}-bcout`);
    const label     = document.getElementById(`b${n}-s${p}-label`);

    if (!claim) {
      claimNote.textContent = '';
      baudSel.disabled = false; bcin.disabled = false; bcout.disabled = false; label.disabled = false;
    } else if (claim.type === 'kyber') {
      claimNote.textContent = 'Managed by Kyber (Maestro port)';
      baudSel.disabled = true; bcin.disabled = true; bcout.disabled = true; label.disabled = true;
    } else if (claim.type === 'kyber-marc') {
      claimNote.textContent = 'Managed by Kyber (Marcuino port)';
      baudSel.disabled = true; bcin.disabled = true; bcout.disabled = true; label.disabled = true;
    } else if (claim.type === 'maestro') {
      claimNote.textContent = `Managed by Maestro ID ${claim.id}`;
      baudSel.disabled = true; bcin.disabled = true; bcout.disabled = true; label.disabled = true;
    } else if (claim.type === 'pwm') {
      claimNote.textContent = 'Managed by PWM';
      baudSel.disabled = true; bcin.disabled = true; bcout.disabled = true;
      label.disabled = false; // PWM: label still editable
    }
  }
}

// ─── Kyber ────────────────────────────────────────────────────────
function onKyberChange(n) {
  const mode        = document.querySelector(`input[name="b${n}-kyber"]:checked`)?.value ?? 'none';
  const portWrap    = document.getElementById(`b${n}-kyber-port-wrap`);
  const baudWrap    = document.getElementById(`b${n}-kyber-baud-wrap`);
  const marcWrap    = document.getElementById(`b${n}-kyber-marc-port-wrap`);
  const targetsWrap = document.getElementById(`b${n}-kyber-targets-wrap`);
  portWrap.style.display = mode === 'local' ? '' : 'none';
  if (baudWrap)    baudWrap.style.display    = mode === 'local' ? '' : 'none';
  if (marcWrap)    marcWrap.style.display    = mode === 'local' ? '' : 'none';
  if (targetsWrap) targetsWrap.style.display = mode === 'local' ? '' : 'none';

  const config = boardConfigs[n];
  if (!config) return;

  for (const port of config.serialPorts) {
    if (port.claimedBy?.type === 'kyber' || port.claimedBy?.type === 'kyber-marc')
      port.claimedBy = null;
  }

  config.kyber.mode = mode;
  if (mode !== 'local') {
    config.kyber.port = null;
    config.kyber.marcduinoPort = null;
  }

  updateKyberPortDropdown(n);
  updateKyberMarcPortDropdown(n);

  if (mode === 'local') {
    const portVal = parseInt(document.getElementById(`b${n}-kyber-port`)?.value);
    if (portVal >= 1 && portVal <= 5) {
      config.kyber.port = portVal;
      config.serialPorts[portVal - 1].claimedBy = { type: 'kyber' };
      // Sync kyber baud dropdown to the selected port's current baud
      const kyberBaud = config.serialPorts[portVal - 1].baud ?? config.kyber.baud ?? 115200;
      const baudSel = document.getElementById(`b${n}-kyber-baud`);
      if (baudSel) baudSel.value = kyberBaud;
    }
    const marcVal = parseInt(document.getElementById(`b${n}-kyber-marc-port`)?.value) || 0;
    if (marcVal >= 1 && marcVal <= 5) {
      config.kyber.marcduinoPort = marcVal;
      config.serialPorts[marcVal - 1].claimedBy  = { type: 'kyber-marc' };
      config.serialPorts[marcVal - 1].baud        = 9600;
      config.serialPorts[marcVal - 1].broadcastIn  = true;
      config.serialPorts[marcVal - 1].broadcastOut = true;
    }
  }

  WCBParser.evaluatePortClaims(config);
  updatePortClaimUI(n);
  updateKyberPortDropdown(n);
  updateKyberMarcPortDropdown(n);
}

function onKyberPortChange(n) {
  const config = boardConfigs[n];
  if (!config) return;

  for (const port of config.serialPorts) {
    if (port.claimedBy?.type === 'kyber') port.claimedBy = null;
  }

  const portVal = parseInt(document.getElementById(`b${n}-kyber-port`)?.value);
  if (portVal >= 1 && portVal <= 5) {
    config.kyber.port = portVal;
    config.serialPorts[portVal - 1].claimedBy = { type: 'kyber' };
    // Sync kyber baud dropdown to the new port's current baud
    const kyberBaud = config.serialPorts[portVal - 1].baud ?? config.kyber.baud ?? 115200;
    const baudSel = document.getElementById(`b${n}-kyber-baud`);
    if (baudSel) baudSel.value = kyberBaud;
  }

  WCBParser.evaluatePortClaims(config);
  updatePortClaimUI(n);
  updateKyberPortDropdown(n);
  updateKyberMarcPortDropdown(n);  // maestro port changed — re-filter marc dropdown
}

function onKyberBaudChange(n) {
  const config  = boardConfigs[n];
  if (!config) return;
  const baud    = parseInt(document.getElementById(`b${n}-kyber-baud`)?.value) || 115200;
  const portVal = config.kyber.port;
  config.kyber.baud = baud;
  if (portVal >= 1 && portVal <= 5) {
    // Mirror the baud into the serial port so ?BAUD,S<port>,<baud> is generated correctly
    config.serialPorts[portVal - 1].baud = baud;
    const serialBaudEl = document.getElementById(`b${n}-s${portVal}-baud`);
    if (serialBaudEl) serialBaudEl.value = baud;
  }
  onBoardFieldChange(n);
}

function updateKyberPortDropdown(n) {
  const portSel = document.getElementById(`b${n}-kyber-port`);
  if (!portSel) return;

  const config      = boardConfigs[n];
  const currentPort = config?.kyber?.port;

  portSel.innerHTML = '';
  for (let p = 1; p <= 5; p++) {
    const claim = config?.serialPorts?.[p - 1]?.claimedBy;
    // Exclude ports claimed by anything except the Kyber Maestro port itself
    if (!claim || claim.type === 'kyber') {
      const opt = document.createElement('option');
      opt.value = p;
      opt.textContent = `Serial ${p}`;
      if (p === currentPort) opt.selected = true;
      portSel.appendChild(opt);
    }
  }
}

function updateKyberMarcPortDropdown(n) {
  const marcSel = document.getElementById(`b${n}-kyber-marc-port`);
  if (!marcSel) return;

  const config      = boardConfigs[n];
  const currentPort = config?.kyber?.marcduinoPort;

  marcSel.innerHTML = '<option value="0">— None —</option>';
  for (let p = 1; p <= 5; p++) {
    const claim = config?.serialPorts?.[p - 1]?.claimedBy;
    // Exclude ports claimed by anything except the Kyber Marcuino port itself
    if (!claim || claim.type === 'kyber-marc') {
      const opt = document.createElement('option');
      opt.value = p;
      opt.textContent = `Serial ${p}`;
      if (p === currentPort) opt.selected = true;
      marcSel.appendChild(opt);
    }
  }
}

function onKyberMarcPortChange(n) {
  const config = boardConfigs[n];
  if (!config) return;

  // Clear any existing Kyber Marcuino claim and its label
  for (let i = 0; i < config.serialPorts.length; i++) {
    if (config.serialPorts[i].claimedBy?.type === 'kyber-marc') {
      config.serialPorts[i].claimedBy = null;
      if (config.serialPorts[i].label === 'Kyber Marcuino') {
        config.serialPorts[i].label = '';
        const labelEl = document.getElementById(`b${n}-s${i + 1}-label`);
        if (labelEl) labelEl.value = '';
      }
    }
  }

  const marcVal = parseInt(document.getElementById(`b${n}-kyber-marc-port`)?.value) || 0;
  config.kyber.marcduinoPort = marcVal >= 1 && marcVal <= 5 ? marcVal : null;

  if (config.kyber.marcduinoPort) {
    const idx = config.kyber.marcduinoPort - 1;
    const p   = config.kyber.marcduinoPort;
    config.serialPorts[idx].claimedBy    = { type: 'kyber-marc' };
    config.serialPorts[idx].baud         = 9600;
    config.serialPorts[idx].broadcastIn  = true;
    config.serialPorts[idx].broadcastOut = true;
    config.serialPorts[idx].label        = 'Kyber Marcuino';   // used to recover marcduinoPort after boardPull
    // Reflect into the serial port UI fields so the display matches
    const baudEl  = document.getElementById(`b${n}-s${p}-baud`);
    const bcinEl  = document.getElementById(`b${n}-s${p}-bcin`);
    const bcoutEl = document.getElementById(`b${n}-s${p}-bcout`);
    const labelEl = document.getElementById(`b${n}-s${p}-label`);
    if (baudEl)  baudEl.value    = 9600;
    if (bcinEl)  bcinEl.checked  = true;
    if (bcoutEl) bcoutEl.checked = true;
    if (labelEl) labelEl.value   = 'Kyber Marcuino';
  }

  WCBParser.evaluatePortClaims(config);
  updatePortClaimUI(n);
  updateKyberPortDropdown(n);
  updateKyberMarcPortDropdown(n);
  onBoardFieldChange(n);
}

// ─── General Field Handlers ────────────────────────────────────────
function onWCBQuantityChange() {
  const qty = parseInt(document.getElementById('g-wcbq').value) || 1;
  systemConfig.general.wcbQuantity = qty;
  renderBoards(qty);
}

function onGeneralPasswordChange() {
  const val = document.getElementById('g-password').value;
  systemConfig.general.espnowPassword = val;
  for (const n in boardConfigs) boardConfigs[n].espnowPassword = val;
  updateGeneralBaseline();
}

function onGeneralMacChange() {
  const m2 = document.getElementById('g-mac2').value.toUpperCase();
  const m3 = document.getElementById('g-mac3').value.toUpperCase();
  systemConfig.general.macOctet2 = m2;
  systemConfig.general.macOctet3 = m3;
  for (const n in boardConfigs) { boardConfigs[n].macOctet2 = m2; boardConfigs[n].macOctet3 = m3; }
  updateGeneralBaseline();
}

function validateMacOctet(input) {
  const val = input.value.trim().toUpperCase();
  const valid = /^[0-9A-F]{0,2}$/.test(val);
  if (!valid || (val.length > 0 && val.length < 2)) {
    input.style.borderColor = 'var(--red)';
    showToast(`Invalid hex value "${val}" — must be 00–FF`, 'error');
  } else {
    input.style.borderColor = '';
    // Pad single digit to two chars
    if (val.length === 1) input.value = '0' + val;
  }
}

function onGeneralCmdCharChange() {
  systemConfig.general.delimiter = document.getElementById('g-delimiter').value || '^';
  systemConfig.general.funcChar  = document.getElementById('g-funcchar').value  || '?';
  systemConfig.general.cmdChar   = document.getElementById('g-cmdchar').value   || ';';
  for (const n in boardConfigs) {
    boardConfigs[n].delimiter = systemConfig.general.delimiter;
    boardConfigs[n].funcChar  = systemConfig.general.funcChar;
    boardConfigs[n].cmdChar   = systemConfig.general.cmdChar;
  }
  updateGeneralBaseline();
}

// Keep generalBaseline in sync whenever the user edits general fields in the UI,
// so a subsequent board-reboot + auto-pull doesn't falsely report a mismatch.
function updateGeneralBaseline() {
  if (!generalBaseline) return;
  generalBaseline.fields = extractGeneralFields(systemConfig.general);
}

function onETMToggle() {
  const enabled   = document.getElementById('g-etm-enabled').checked;
  const etmDetail = document.getElementById('etm-detail');
  systemConfig.general.etm.enabled = enabled;
  if (appMode === 'advanced' && enabled) etmDetail.classList.add('visible');
  else etmDetail.classList.remove('visible');
  for (const n in boardConfigs) boardConfigs[n].etm.enabled = enabled;
}

function onETMChecksumToggle() {
  const enabled = document.getElementById('g-etm-chksm')?.checked ?? true;
  systemConfig.general.etm.checksumEnabled = enabled;
  for (const n in boardConfigs) boardConfigs[n].etm.checksumEnabled = enabled;
}

// ─── General Settings Conflict Helpers ────────────────────────────
const GENERAL_FIELD_LABELS = {
  espnowPassword: 'ESP-NOW Password',
  macOctet2:      'MAC Octet 2',
  macOctet3:      'MAC Octet 3',
  delimiter:      'Command Delimiter',
  funcChar:       'Local Function ID',
  cmdChar:        'Command Character',
};

function extractGeneralFields(config) {
  return {
    espnowPassword: config.espnowPassword ?? '',
    macOctet2:      config.macOctet2      ?? '00',
    macOctet3:      config.macOctet3      ?? '00',
    delimiter:      config.delimiter      ?? '^',
    funcChar:       config.funcChar       ?? '?',
    cmdChar:        config.cmdChar        ?? ';',
  };
}

function getGeneralMismatches(a, b) {
  return Object.keys(GENERAL_FIELD_LABELS)
    .filter(k => String(a[k]) !== String(b[k]))
    .map(k => ({ key: k, label: GENERAL_FIELD_LABELS[k], aVal: a[k], bVal: b[k] }));
}

// ─── Board Field Handlers ─────────────────────────────────────────
function onBoardFieldChange(n) {
  updateBoardStatusBadge(n, 'unsaved');
}

function boardUpdateFW(n) {
  boardGo(n, { mode: 'update' });
}

function onSerialFieldChange(n) {
  syncSerialUIToConfig(n);
  onBoardFieldChange(n);
}

function onWCBNumberChange(n) {
  const val = parseInt(document.getElementById(`b${n}-wcb-number`)?.value);
  if (boardConfigs[n] && val >= 1 && val <= 8) boardConfigs[n].wcbNumber = val;
  onBoardFieldChange(n);
}

function onHWVersionChange(n) {
  const hwVal = parseInt(document.getElementById(`b${n}-hw-version`)?.value);
  if (boardConfigs[n]) boardConfigs[n].hwVersion = hwVal;

  onBoardFieldChange(n);
}

function syncKyberToConfig(n) {
  const config = boardConfigs[n];
  if (!config) return;
  const mode = document.querySelector(`input[name="b${n}-kyber"]:checked`)?.value ?? 'none';
  config.kyber.mode = mode;
  if (mode === 'local') {
    const portVal = parseInt(document.getElementById(`b${n}-kyber-port`)?.value) || null;
    config.kyber.port = portVal;
    const baud = parseInt(document.getElementById(`b${n}-kyber-baud`)?.value) || 115200;
    config.kyber.baud = baud;
    if (portVal >= 1 && portVal <= 5) {
      // Keep serial port baud in sync so the ?BAUD command is generated correctly
      config.serialPorts[portVal - 1].baud = baud;
    }
    const marcVal = parseInt(document.getElementById(`b${n}-kyber-marc-port`)?.value) || 0;
    config.kyber.marcduinoPort = marcVal >= 1 && marcVal <= 5 ? marcVal : null;
  } else {
    config.kyber.port = null;
    config.kyber.marcduinoPort = null;
  }
}

function syncSerialUIToConfig(n) {
  const config = boardConfigs[n];
  if (!config) return;
  for (let p = 1; p <= 5; p++) {
    config.serialPorts[p - 1].baud         = parseInt(document.getElementById(`b${n}-s${p}-baud`)?.value) || 9600;
    config.serialPorts[p - 1].broadcastIn  = document.getElementById(`b${n}-s${p}-bcin`)?.checked ?? true;
    config.serialPorts[p - 1].broadcastOut = document.getElementById(`b${n}-s${p}-bcout`)?.checked ?? true;
    config.serialPorts[p - 1].label        = document.getElementById(`b${n}-s${p}-label`)?.value ?? '';
  }
}

function populateUIFromConfig(n, config) {
  const wcbNumSel = document.getElementById(`b${n}-wcb-number`);
  if (wcbNumSel) wcbNumSel.value = config.wcbNumber || n;

  const hwSel = document.getElementById(`b${n}-hw-version`);
  if (hwSel) { hwSel.value = config.hwVersion || 0; onHWVersionChange(n); }

  // Software version — show board version with update check, or '—' if not yet known
  updateBoardSwVersionDisplay(n);

  for (let p = 1; p <= 5; p++) {
    const sp = config.serialPorts[p - 1];
    const el = (id) => document.getElementById(`b${n}-s${p}-${id}`);
    if (el('baud'))  el('baud').value   = sp.baud;
    if (el('bcin'))  el('bcin').checked = sp.broadcastIn;
    if (el('bcout')) el('bcout').checked = sp.broadcastOut;
    if (el('label')) el('label').value  = sp.label;
  }

  // Kyber
  const kyberInput = document.querySelector(`input[name="b${n}-kyber"][value="${config.kyber.mode}"]`);
  if (kyberInput) {
    kyberInput.checked = true;
    const isLocal = config.kyber.mode === 'local';
    document.getElementById(`b${n}-kyber-port-wrap`).style.display = isLocal ? '' : 'none';
    const tw = document.getElementById(`b${n}-kyber-targets-wrap`);
    if (tw) tw.style.display = isLocal ? '' : 'none';
    const bw = document.getElementById(`b${n}-kyber-baud-wrap`);
    if (bw) bw.style.display = isLocal ? '' : 'none';
    const mw = document.getElementById(`b${n}-kyber-marc-port-wrap`);
    if (mw) mw.style.display = isLocal ? '' : 'none';
    updateKyberPortDropdown(n);
    updateKyberMarcPortDropdown(n);
    if (isLocal && config.kyber.port) {
      const portSel = document.getElementById(`b${n}-kyber-port`);
      if (portSel) portSel.value = config.kyber.port;
      // Derive kyber baud from the serial port baud (populated from ?BAUD commands in backup)
      const kyberBaud = config.serialPorts[config.kyber.port - 1]?.baud
                     ?? config.kyber.baud
                     ?? 115200;
      const baudSel = document.getElementById(`b${n}-kyber-baud`);
      if (baudSel) baudSel.value = kyberBaud;
    }
    if (isLocal) {
      const marcSel = document.getElementById(`b${n}-kyber-marc-port`);
      if (marcSel) marcSel.value = config.kyber.marcduinoPort ?? 0;
      populateKyberTargetsFromConfig(n, config);
    }
  }

  // Sequences
  const tbody = document.getElementById(`b${n}-seq-tbody`);
  if (tbody) {
    tbody.innerHTML = '';
    for (const seq of config.sequences) appendSequenceRow(n, seq.key, seq.value);
  }

  WCBParser.evaluatePortClaims(config);
  updatePortClaimUI(n);
  updateKyberPortDropdown(n);
  updateKyberMarcPortDropdown(n);
  populateMaestrosFromConfig(n, config);
  populateMappingsFromConfig(n, config);
}

function updateBoardStatusBadge(n, state) {
  const unsavedBadge = document.getElementById(`b${n}-unsaved-badge`);
  if (state === 'unsaved') {
    if (unsavedBadge) unsavedBadge.style.display = '';
    return;  // leave the connection badge alone
  }
  if (unsavedBadge) unsavedBadge.style.display = 'none';

  const badge = document.getElementById(`b${n}-status-badge`);
  if (!badge) return;
  badge.className = 'badge';
  const states = {
    configured: ['badge-green',  '✅ Configured'],
    connected:  ['badge-green',  '● Connected'],
    remote:     ['badge-green',  '📡 Remote'],
    retrying:   ['badge-yellow', '↻ Retrying…'],
    error:      ['badge-red',    '✕ Pull Failed'],
    default:    ['badge-default','Not Connected'],
  };
  const [cls, text] = states[state] ?? states.default;
  badge.classList.add(cls);
  badge.textContent = text;

  // Show retry button only when pull failed on a remote board
  const retryBtn = document.getElementById(`b${n}-btn-retry`);
  if (retryBtn) retryBtn.style.display = (state === 'error' && remoteRelayForBoard[n]) ? '' : 'none';
}

// ─── Maestro ──────────────────────────────────────────────────────
function addMaestroRow(n) {
  appendMaestroRow(n, { id: 1, port: null, baud: 115200 });
}

function appendMaestroRow(n, maestro, readOnly = false) {
  const tbody = document.getElementById(`b${n}-maestro-tbody`);
  if (!tbody) return;

  const rowNum   = tbody.rows.length + 1;
  const rowId    = `maestro-row-${n}-${Date.now()}`;
  const dis      = readOnly ? 'disabled' : '';
  const dimStyle = readOnly ? 'style="opacity:0.5"' : '';

  const idOptions = Array.from({length: 9}, (_, i) => i + 1).map(v =>
    `<option value="${v}" ${v === maestro.id ? 'selected' : ''}>${v}</option>`
  ).join('');

  // Baud options filtered by port (ports 3-5 cap at 57600)
  const maxBaud = (maestro.port >= 3) ? 57600 : Infinity;
  const safeBaud = Math.min(maestro.baud, maxBaud);
  const baudOptions = BAUD_RATES.filter(b => b <= maxBaud).map(b =>
    `<option value="${b}" ${b === safeBaud ? 'selected' : ''}>${b.toLocaleString()}</option>`
  ).join('');

  const kyberBoard = findKyberLocalBoard();
  const deleteBtn = readOnly
    ? `<span class="kyber-lock" title="Managed by Kyber targets on WCB ${kyberBoard}" style="opacity:0.4;font-size:18px;padding:0 8px">&#128274;</span>`
    : `<button class="btn btn-danger btn-sm btn-icon" onclick="removeMaestroRow(${n},'${rowId}')">&#128465;</button>`;

  const tr = document.createElement('tr');
  tr.id = rowId;
  tr.setAttribute('data-readonly', readOnly ? '1' : '0');
  tr.innerHTML = `
    <td style="color:var(--text3)" ${dimStyle}>${rowNum}</td>
    <td ${dimStyle}><select id="${rowId}-id" ${dis} onchange="onMaestroChange(${n})">${idOptions}</select></td>
    <td><select id="${rowId}-port" ${dis} onchange="onMaestroPortChange(${n},'${rowId}')">
      <option value="">&#8212; Select &#8212;</option>
    </select></td>
    <td ${dimStyle}><select id="${rowId}-baud" ${dis} onchange="onMaestroChange(${n})">${baudOptions}</select></td>
    <td>${deleteBtn}</td>
  `;
  tbody.appendChild(tr);

  refreshMaestroPortDropdown(n, rowId, maestro.port);
}

function refreshMaestroPortDropdown(n, rowId, selectedPort) {
  const portSel = document.getElementById(`${rowId}-port`);
  if (!portSel) return;
  const config = boardConfigs[n];

  // Find which ports are claimed by OTHER maestro rows (not this one)
  const otherClaims = new Set();
  document.getElementById(`b${n}-maestro-tbody`)?.querySelectorAll('tr').forEach(row => {
    if (row.id === rowId) return; // skip self
    const p = parseInt(row.querySelector('[id$="-port"]')?.value);
    if (p) otherClaims.add(p);
  });

  portSel.innerHTML = '<option value="">— Select —</option>';
  for (let p = 1; p <= 5; p++) {
    // Exclude ports claimed by kyber (either port) or other maestro rows
    const claim = config?.serialPorts?.[p - 1]?.claimedBy;
    const claimedByKyber = claim?.type === 'kyber' || claim?.type === 'kyber-marc';
    if (claimedByKyber || otherClaims.has(p)) continue;

    const opt = document.createElement('option');
    opt.value = p;
    opt.textContent = `Serial ${p}`;
    if (p === selectedPort) opt.selected = true;
    portSel.appendChild(opt);
  }
}

function onMaestroPortChange(n, rowId) {
  const portSel = document.getElementById(`${rowId}-port`);
  const baudSel = document.getElementById(`${rowId}-baud`);
  if (portSel && baudSel) {
    const port    = parseInt(portSel.value) || 0;
    const maxBaud = port >= 3 ? 57600 : Infinity;

    // Auto-fill baud from the serial port's configured value so the user
    // doesn't have to manually match them.  Falls back to current selection.
    const portBaud = port ? (boardConfigs[n]?.serialPorts?.[port - 1]?.baud ?? null) : null;
    const curBaud  = portBaud ?? parseInt(baudSel.value) ?? 57600;

    baudSel.innerHTML = BAUD_RATES.filter(b => b <= maxBaud).map(b =>
      `<option value="${b}" ${b === Math.min(curBaud, maxBaud) ? 'selected' : ''}>${b.toLocaleString()}</option>`
    ).join('');
  }
  onMaestroChange(n);
}

function onMaestroChange(n) {
  syncMaestrosToConfig(n);
  onBoardFieldChange(n);
  // Refresh the auto-computed Kyber targets display on the LOCAL board
  const kyberBoard = findKyberLocalBoard();
  if (kyberBoard !== null) {
    populateKyberTargetsFromConfig(kyberBoard, boardConfigs[kyberBoard]);
    // Warn if it's a different board — it will need a re-push to pick up the change
    if (kyberBoard !== n) showKyberRepushWarning(kyberBoard);
  }
}

function syncMaestrosToConfig(n) {
  const config = boardConfigs[n];
  if (!config) return;

  // Release all maestro claims
  for (const sp of config.serialPorts) {
    if (sp.claimedBy?.type === 'maestro') sp.claimedBy = null;
  }

  config.maestros = [];
  const tbody = document.getElementById(`b${n}-maestro-tbody`);
  if (!tbody) return;

  tbody.querySelectorAll('tr').forEach(row => {
    const id   = parseInt(row.querySelector('[id$="-id"]')?.value);
    const port = parseInt(row.querySelector('[id$="-port"]')?.value);
    const baud = parseInt(row.querySelector('[id$="-baud"]')?.value) || 57600;
    if (id && port) {
      config.maestros.push({ id, port, baud });
      config.serialPorts[port - 1].claimedBy = { type: 'maestro', id };
    }
  });

  WCBParser.evaluatePortClaims(config);
  updatePortClaimUI(n);
  // Refresh all port dropdowns in maestro rows to reflect new claims
  tbody.querySelectorAll('tr').forEach(row => {
    const portSel = row.querySelector('[id$="-port"]');
    if (portSel) {
      const cur = parseInt(portSel.value) || null;
      refreshMaestroPortDropdown(n, row.id, cur);
    }
  });
  updateKyberPortDropdown(n);
}

function removeMaestroRow(n, rowId) {
  document.getElementById(rowId)?.remove();
  // Renumber
  const tbody = document.getElementById(`b${n}-maestro-tbody`);
  tbody?.querySelectorAll('tr').forEach((row, i) => {
    row.cells[0].textContent = i + 1;
  });
  syncMaestrosToConfig(n);
}

function populateMaestrosFromConfig(n, config) {
  const tbody = document.getElementById(`b${n}-maestro-tbody`);
  if (!tbody) return;
  tbody.innerHTML = '';
  // Each board's local maestros are always editable — never grey them out.
  // (A maestro may also appear in the Kyber-local board's target list, but that
  // reflects how the Kyber LOCAL command reaches it via ESP-NOW; it doesn't make
  // the local row read-only on its own board.)
  for (const m of config.maestros) {
    appendMaestroRow(n, m);
  }
}

// ─── Mappings ─────────────────────────────────────────────────────
function addMappingRow(n) {
  appendMappingRow(n, { type: 'Serial', sourcePort: null, rawMode: false, destinations: [] });
}

function appendMappingRow(n, mapping) {
  const container = document.getElementById(`b${n}-mappings-container`);
  if (!container) return;

  const rowId = `map-row-${n}-${Date.now()}`;
  const div   = document.createElement('div');
  div.id      = rowId;
  div.style.cssText = 'background:var(--bg3);border:1px solid var(--border);border-radius:var(--radius);padding:12px;margin-bottom:10px;';

  div.innerHTML = `
    <div class="flex items-center gap-8 mb-12" style="flex-wrap:wrap">
      <div class="field" style="min-width:120px;margin-bottom:0">
        <label>Type</label>
        <select id="${rowId}-type" onchange="onMappingTypeChange('${rowId}',${n})">
          <option value="Serial" ${mapping.type==='Serial'?'selected':''}>Serial</option>
          <option value="PWM"    ${mapping.type==='PWM'   ?'selected':''}>PWM</option>
        </select>
      </div>
      <div class="field" style="min-width:140px;margin-bottom:0">
        <label>Source Port</label>
        <select id="${rowId}-src" onchange="onMappingChange('${rowId}',${n})">
          <option value="">— Select —</option>
        </select>
      </div>
      <div class="field" id="${rowId}-raw-wrap" style="min-width:100px;margin-bottom:0;${mapping.type==='Serial'?'':'display:none'}">
        <label>Raw Mode</label>
        <label class="toggle" style="margin-top:4px">
          <input type="checkbox" id="${rowId}-raw" ${mapping.rawMode?'checked':''} onchange="onMappingChange('${rowId}',${n})">
          <span class="toggle-track"></span>
          <span class="toggle-label">Raw</span>
        </label>
      </div>
      <div style="flex:1"></div>
      <button class="btn btn-danger btn-sm" onclick="removeMappingRow('${rowId}',${n})">🗑 Remove</button>
    </div>
    <div id="${rowId}-destinations" style="padding-left:8px;border-left:2px solid var(--border)"></div>
    <button class="btn btn-ghost btn-sm mt-8" onclick="addMappingDestination('${rowId}',${n})">+ Add Destination</button>
  `;
  container.appendChild(div);

  refreshMappingSourceDropdown(n, rowId, mapping.sourcePort);

  for (const dest of mapping.destinations) {
    appendMappingDestination(rowId, n, dest);
  }
}

function refreshMappingSourceDropdown(n, rowId, selectedPort) {
  const sel    = document.getElementById(`${rowId}-src`);
  if (!sel) return;
  const config = boardConfigs[n];

  sel.innerHTML = '<option value="">— Select —</option>';
  for (let p = 1; p <= 5; p++) {
    const claim = config?.serialPorts?.[p - 1]?.claimedBy;
    if (!claim || claim.type === 'pwm') {
      const opt = document.createElement('option');
      opt.value = p;
      opt.textContent = `Serial ${p}`;
      if (p === selectedPort) opt.selected = true;
      sel.appendChild(opt);
    }
  }
}

function onMappingTypeChange(rowId, n) {
  const type    = document.getElementById(`${rowId}-type`)?.value;
  const rawWrap = document.getElementById(`${rowId}-raw-wrap`);
  if (rawWrap) rawWrap.style.display = type === 'Serial' ? '' : 'none';

  // Rebuild destination port dropdowns to add/remove S0 (USB)
  const container = document.getElementById(`${rowId}-destinations`);
  if (container) {
    container.querySelectorAll('[id^="map-dest-"]').forEach(destRow => {
      const portSel  = destRow.querySelector('[id$="-port"]');
      const curPort  = parseInt(portSel?.value) ?? null;
      const isSerial = type === 'Serial';
      const portOptions = (isSerial ? [{ v:0, l:'S0 (USB)' }] : [])
        .concat([1,2,3,4,5].map(p => ({ v:p, l:`S${p}` })))
        .map(({ v, l }) => `<option value="${v}" ${curPort===v?'selected':''}>${l}</option>`)
        .join('');
      if (portSel) portSel.innerHTML = portOptions;
    });
  }

  onMappingChange(rowId, n);
}

function onMappingChange(rowId, n) {
  syncMappingsToConfig(n);
  onBoardFieldChange(n);
}

function syncMappingsToConfig(n) {
  const config    = boardConfigs[n];
  if (!config) return;

  // Release pwm claims
  for (const sp of config.serialPorts) {
    if (sp.claimedBy?.type === 'pwm') sp.claimedBy = null;
  }

  config.mappings      = [];
  config.pwmOutputPorts = [];

  const container = document.getElementById(`b${n}-mappings-container`);
  if (!container) return;

  container.querySelectorAll('[id^="map-row-"]').forEach(row => {
    const rowId = row.id;
    const type  = document.getElementById(`${rowId}-type`)?.value;
    const src   = parseInt(document.getElementById(`${rowId}-src`)?.value);
    const raw   = document.getElementById(`${rowId}-raw`)?.checked ?? false;
    if (!type || !src) return;

    const destinations = [];
    row.querySelectorAll('[id^="map-dest-"]').forEach(destRow => {
      const wcb  = parseInt(destRow.querySelector('[id$="-wcb"]')?.value) ?? 0;
      const port = parseInt(destRow.querySelector('[id$="-port"]')?.value);
      if (port) destinations.push({ wcbNumber: wcb || 0, port });
    });

    config.mappings.push({ type, sourcePort: src, rawMode: raw, destinations });
    if (src >= 1 && src <= 5 && !config.serialPorts[src-1].claimedBy) {
      config.serialPorts[src - 1].claimedBy = { type: 'pwm' };
    }
  });

  WCBParser.evaluatePortClaims(config);
  updatePortClaimUI(n);
}

function removeMappingRow(rowId, n) {
  document.getElementById(rowId)?.remove();
  syncMappingsToConfig(n);
}

function addMappingDestination(rowId, n) {
  appendMappingDestination(rowId, n, { wcbNumber: 0, port: null });
}

function appendMappingDestination(rowId, n, dest) {
  const container = document.getElementById(`${rowId}-destinations`);
  if (!container) return;

  const destId = `map-dest-${rowId}-${Date.now()}`;
  const mapType = document.getElementById(`${rowId}-type`)?.value ?? 'Serial';
  const isSerial = mapType === 'Serial';

  const wcbOptions = `<option value="0">Local</option>` +
    Array.from({length: 9}, (_,i) =>
      `<option value="${i+1}" ${dest.wcbNumber===i+1?'selected':''}>WCB ${i+1}</option>`
    ).join('');

  // S0 = USB Serial, only valid for Serial type mappings
  const portOptions = (isSerial ? [{ v:0, l:'S0 (USB)' }] : [])
    .concat([1,2,3,4,5].map(p => ({ v:p, l:`S${p}` })))
    .map(({ v, l }) => `<option value="${v}" ${dest.port===v?'selected':''}>${l}</option>`)
    .join('');

  const div = document.createElement('div');
  div.id    = destId;
  div.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:6px;';
  div.innerHTML = `
    <span class="text-muted" style="min-width:16px">→</span>
    <select id="${destId}-wcb" onchange="onMappingChange('${rowId}',${n})" style="background:var(--bg4);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);font-family:var(--mono);font-size:12px;padding:4px 8px">${wcbOptions}</select>
    <select id="${destId}-port" onchange="onMappingChange('${rowId}',${n})" style="background:var(--bg4);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);font-family:var(--mono);font-size:12px;padding:4px 8px">${portOptions}</select>
    <button class="btn btn-danger btn-sm btn-icon" onclick="document.getElementById('${destId}').remove();onMappingChange('${rowId}',${n})">🗑</button>
  `;
  container.appendChild(div);
}

function populateMappingsFromConfig(n, config) {
  const container = document.getElementById(`b${n}-mappings-container`);
  if (!container) return;
  container.innerHTML = '';
  for (const m of config.mappings) appendMappingRow(n, m);
}

// ─── Sequences ────────────────────────────────────────────────────

// Convert textarea lines → stored value string (delimiter-separated)
// Strips any whitespace between the command and an inline *** comment.
function seqTextareaToValue(text, delim) {
  return text.split('\n')
    .map(l => l.trim().replace(/\s+(\*\*\*)/, '$1'))
    .filter(Boolean)
    .join(delim);
}

// Convert stored value string → textarea lines (one command per line)
// Normalises inline *** comments to exactly one space: "CMD*** note" → "CMD *** note"
function seqValueToLines(value, delim) {
  if (!value) return '';
  return value.split(delim)
    .map(l => l.trim().replace(/(\S)\s*(\*\*\*)/, '$1 $2'))
    .filter(Boolean)
    .join('\n');
}

function autoResizeTextarea(ta) {
  ta.style.height = 'auto';
  ta.style.height = ta.scrollHeight + 'px';
}

function updateSeqKeyCount(rowId) {
  const row = document.getElementById(rowId);
  if (!row) return;
  const key = row.querySelector('.seq-key-input')?.value ?? '';
  const el = document.getElementById(`${rowId}-key-count`);
  if (el) el.textContent = `${key.length}/15`;
}

function updateSeqValCount(rowId, n) {
  const row = document.getElementById(rowId);
  if (!row) return;
  const ta = row.querySelector('.seq-val-textarea');
  const el = document.getElementById(`${rowId}-val-count`);
  if (el && ta) {
    const delim = boardConfigs[n]?.delimiter ?? '^';
    el.textContent = seqTextareaToValue(ta.value, delim).length;
  }
}

function addSequenceRow(n) { appendSequenceRow(n, '', ''); }

function appendSequenceRow(n, key, value) {
  const tbody = document.getElementById(`b${n}-seq-tbody`);
  if (!tbody) return;
  const rowId = `seq-row-${n}-${Date.now()}-${Math.random().toString(36).slice(2,6)}`;

  // Convert stored delimiter-separated value → one-command-per-line for display
  const delim = boardConfigs[n]?.delimiter ?? '^';
  const lines = seqValueToLines(value, delim);
  const keyLen  = key.length;
  const valLen  = value.length;

  const tr = document.createElement('tr');
  tr.id = rowId;
  tr.innerHTML = `
    <td class="seq-key-cell">
      <input class="seq-key-input" type="text" value="${escHtml(key)}"
             placeholder="KeyName" spellcheck="false" maxlength="15"
             oninput="updateSeqKeyCount('${rowId}')">
      <div class="seq-char-count" id="${rowId}-key-count">${keyLen}/15</div>
    </td>
    <td class="seq-val-cell">
      <textarea class="seq-val-textarea" placeholder="One command per line…" spellcheck="false"
                oninput="autoResizeTextarea(this); updateSeqValCount('${rowId}',${n})"
                >${escHtml(lines)}</textarea>
      <div class="seq-char-count" id="${rowId}-val-count">${valLen}</div>
    </td>
    <td class="seq-action-cell">
      <button class="btn btn-primary btn-sm" title="Test"
              onclick="playSequence(${n},'${rowId}')" id="${rowId}-play">TEST</button>
      <button class="btn btn-primary btn-sm" title="Update"
              onclick="updateSequence(${n},'${rowId}')" id="${rowId}-update">UPDATE</button>
      <button class="btn btn-danger btn-sm" title="Remove"
              onclick="removeSequenceRow(${n},'${rowId}')">REMOVE</button>
    </td>
  `;
  tbody.appendChild(tr);

  // Auto-size the textarea to its initial content
  const ta = tr.querySelector('.seq-val-textarea');
  if (ta) requestAnimationFrame(() => autoResizeTextarea(ta));

  updateSequencePlayButtons(n);
}

async function removeSequenceRow(n, rowId) {
  const row = document.getElementById(rowId);
  if (!row) return;
  const key = row.querySelector('.seq-key-input')?.value?.trim();

  // Remove from DOM immediately
  row.remove();

  // Patch in-memory config and baseline so the key doesn't resurface on next push
  for (const store of [boardConfigs[n], boardBaselines[n]]) {
    if (!store?.sequences) continue;
    const idx = store.sequences.findIndex(s => s.key === key);
    if (idx >= 0) store.sequences.splice(idx, 1);
  }

  if (!key) return;   // no key — nothing to tell the board

  // Send ?SEQ,CLEAR,key immediately so the board doesn't wait for a full push
  const funcChar = boardConfigs[n]?.funcChar ?? '?';
  const cmd = `${funcChar}SEQ,CLEAR,${key}`;
  const relayN = remoteRelayForBoard[n];

  try {
    if (relayN) {
      const relayConn = boardConnections[relayN];
      if (!relayConn?.isConnected()) return;   // relay gone — push will handle it later
      const sessionId = Math.floor(Math.random() * 0xFFFF).toString(16).toUpperCase().padStart(4, '0');
      const mgmtCmd = `?MGMT,FRAG,${n},${sessionId},0,1,${cmd}`;
      await relayConn.send(mgmtCmd + '\r');
      termLog(relayN, mgmtCmd, 'in');
      showToast(`Sequence "${key}" removed from WCB ${n} (remote)`, 'info');
    } else {
      const conn = boardConnections[n];
      if (!conn?.isConnected()) return;         // not connected — push will handle it later
      await conn.send(cmd + '\r');
      termLog(n, cmd, 'in');
      showToast(`Sequence "${key}" removed from WCB ${n}`, 'info');
    }
  } catch (e) {
    termLog(relayN ?? n, `SEQ,CLEAR error: ${e.message}`, 'err');
  }
}

function updateSequencePlayButtons(n) {
  const directConnected = boardConnections[n]?.isConnected() ?? false;
  const remoteConnected = remoteRelayForBoard[n] !== undefined;
  const anyConnected = directConnected || remoteConnected;
  // Both TEST and UPDATE work for direct and remote boards.
  // Remote path sends via MGMT FRAG through the relay.
  document.querySelectorAll(`[id^="seq-row-${n}-"] [title="Test"]`).forEach(btn => {
    btn.disabled = !anyConnected;
  });
  document.querySelectorAll(`[id^="seq-row-${n}-"] [title="Update"]`).forEach(btn => {
    btn.disabled = !anyConnected;
  });
}

async function playSequence(n, rowId) {
  const row = document.getElementById(rowId);
  if (!row) return;
  const key = row.querySelector('.seq-key-input')?.value?.trim();
  if (!key) { showToast('Sequence key is empty', 'error'); return; }
  const cmdChar = boardConfigs[n]?.cmdChar ?? ';';
  const cmd = `${cmdChar}SEQ${key}`;

  const relayN = remoteRelayForBoard[n];
  if (relayN) {
    // Remote board — deliver via MGMT FRAG through the relay
    const relayConn = boardConnections[relayN];
    if (!relayConn?.isConnected()) { showToast(`Relay WCB ${relayN} not connected`, 'error'); return; }
    const sessionId = Math.floor(Math.random() * 0xFFFF).toString(16).toUpperCase().padStart(4, '0');
    const mgmtCmd = `?MGMT,FRAG,${n},${sessionId},0,1,${cmd}`;
    await relayConn.send(mgmtCmd + '\r');
    termLog(relayN, mgmtCmd, 'in');
    showToast(`Sent: ${cmd} (remote)`, 'info');
  } else {
    const conn = boardConnections[n];
    if (!conn?.isConnected()) { showToast('Board not connected', 'error'); return; }
    conn.send(cmd + '\r');
    termLog(n, cmd, 'in');
    showToast(`Sent: ${cmd}`, 'info');
  }
}

async function updateSequence(n, rowId) {
  const row = document.getElementById(rowId);
  if (!row) return;
  const key = row.querySelector('.seq-key-input')?.value?.trim();
  if (!key) { showToast('Sequence key is empty', 'error'); return; }
  const ta = row.querySelector('.seq-val-textarea');

  const delim    = boardConfigs[n]?.delimiter ?? '^';
  const funcChar = boardConfigs[n]?.funcChar  ?? '?';
  const value    = seqTextareaToValue(ta?.value ?? '', delim);
  if (!value) { showToast('Sequence value is empty', 'error'); return; }

  const btn = document.getElementById(`${rowId}-update`);
  if (btn) btn.disabled = true;

  const relayN = remoteRelayForBoard[n];
  let logTarget = n;

  try {
    const cmd = `${funcChar}SEQ,SAVE,${key},${value}`;

    if (relayN) {
      // Remote board — send via MGMT FRAG through the relay
      const relayConn = boardConnections[relayN];
      if (!relayConn?.isConnected()) { showToast(`Relay WCB ${relayN} not connected`, 'error'); return; }
      const sessionId = Math.floor(Math.random() * 0xFFFF).toString(16).toUpperCase().padStart(4, '0');
      const mgmtCmd = `?MGMT,FRAG,${n},${sessionId},0,1,${cmd}`;
      await relayConn.send(mgmtCmd + '\r');
      logTarget = relayN;
      termLog(relayN, mgmtCmd, 'in');
      showToast(`Sequence "${key}" updated on WCB ${n} (remote)`, 'success');
    } else {
      // Direct connection
      const conn = boardConnections[n];
      if (!conn?.isConnected()) { showToast('Board not connected', 'error'); return; }
      await conn.send(cmd + '\r');
      termLog(n, cmd, 'in');
      showToast(`Sequence "${key}" updated on WCB ${n}`, 'success');
    }

    // Patch the in-memory config and baseline so delta pushes stay accurate
    for (const store of [boardConfigs[n], boardBaselines[n]]) {
      if (!store) continue;
      const idx = store.sequences.findIndex(s => s.key === key);
      if (idx >= 0) store.sequences[idx].value = value;
      else store.sequences.push({ key, value });
    }
  } catch (e) {
    showToast(`Update failed: ${e.message}`, 'error');
    termLog(logTarget, `Sequence update error: ${e.message}`, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function getSequencesFromUI(n) {
  const sequences = [];
  const delim = boardConfigs[n]?.delimiter ?? '^';
  document.getElementById(`b${n}-seq-tbody`)?.querySelectorAll('tr').forEach(row => {
    const key = row.querySelector('.seq-key-input')?.value?.trim();
    const ta  = row.querySelector('.seq-val-textarea');
    const val = ta ? seqTextareaToValue(ta.value, delim) : '';
    if (key && val) sequences.push({ key, value: val });
  });
  return sequences;
}

// ─── BoardConnection Class ────────────────────────────────────────
class BoardConnection {
  constructor(boardIndex) {
    this.boardIndex = boardIndex;
    this.port = null;
    this.reader = null;
    this._connected = false;
    this._readBuffer = '';
    this._dataCallbacks = [];
    this._lineTransform = null;  // optional (line) => displayLine | null — filters terminal output
  }

  isConnected() { return this._connected; }

  async connect(existingPort = null, usedPorts = new Set()) {
    if (!('serial' in navigator)) throw new Error('WebSerial not supported in this browser');
    if (existingPort) {
      this.port = existingPort;
    } else {
      this.port = await navigator.serial.requestPort();
      if (usedPorts.has(this.port)) {
        this.port = null;
        throw new Error('That port is already connected to another WCB. Please select a different port.');
      }
    }
    await this.port.open({ baudRate: 115200 });
    this._connected = true;
    this._startReading();
  }

  // Tear down the active connection without nulling this.port, so reconnect() can reopen it.
  // Cancels any pending reader.read() (unblocks hangs), sets _connected=false so
  // _startReading exits without trying its own reconnect.
  async closeForReconnect() {
    this._connected = false;
    try { if (this.reader) await this.reader.cancel(); } catch (_) {}
    try { if (this.port)   await this.port.close();   } catch (_) {}
  }

  // After a reboot, attempt to reopen the same port
  async reconnect(maxAttempts = 10, delayMs = 1500) {
    if (!this.port) return false;
    // Close first — after a device reboot Chrome may still consider the port "open"
    try { await this.port.close(); } catch (_) {}
    for (let i = 0; i < maxAttempts; i++) {
      await sleep(delayMs);
      try {
        await this.port.open({ baudRate: 115200 });
        this._connected = true;
        this._readBuffer = '';
        this._startReading();
        return true;
      } catch (_) {
        // Port not ready yet — keep trying
      }
    }
    return false;
  }

  async disconnect() {
    this._connected = false;
    try {
      if (this.reader) {
        await this.reader.cancel();
        this.reader = null;
      }
    } catch (_) {}
    try {
      if (this.port) {
        await this.port.close();
        this.port = null;
      }
    } catch (_) {}
  }

  async send(data) {
    if (!this._connected || !this.port?.writable) throw new Error('Not connected');
    const writer = this.port.writable.getWriter();
    await writer.write(new TextEncoder().encode(data));
    writer.releaseLock();
  }

  // Send a command and collect all response lines until timeout or sentinel
  async sendAndCollect(command, timeoutMs = 8000, sentinel = 'End of Backup') {
    return new Promise((resolve) => {
      const lines = [];
      let timer;
      let done = false;

      const finish = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        this._dataCallbacks = this._dataCallbacks.filter(cb => cb !== onLine);
        resolve(lines.join('\n'));
      };

      const onLine = (line) => {
        lines.push(line);
        if (line.includes(sentinel)) finish();
      };

      this._dataCallbacks.push(onLine);
      timer = setTimeout(finish, timeoutMs);
      this.send(command + '\r').catch(finish);
    });
  }

  onData(callback) { this._dataCallbacks.push(callback); }

  async _startReading() {
    outer: while (this._connected && this.port?.readable) {
      try {
        this.reader = this.port.readable.getReader();
      } catch (e) {
        // Port no longer readable — board disconnected/rebooted
        break outer;
      }
      try {
        while (this._connected) {
          const { value, done } = await this.reader.read();
          if (done) break outer;  // stream ended cleanly — treat as disconnect
          this._readBuffer += new TextDecoder().decode(value);
          let nl;
          while ((nl = this._readBuffer.indexOf('\n')) !== -1) {
            const line = this._readBuffer.slice(0, nl).replace(/\r$/, '').trim();
            this._readBuffer = this._readBuffer.slice(nl + 1);
            if (line) {
              this._dataCallbacks.forEach(cb => cb(line));
              // Route [TERM:N]<text> lines to the remote board's terminal pane
              // instead of the relay's own pane.
              const termMatch = line.match(/^\[TERM:(\d+)\](.*)/);
              if (termMatch) {
                termLog(parseInt(termMatch[1]), termMatch[2], 'out');
              } else {
                const displayed = this._lineTransform ? this._lineTransform(line) : line;
                if (displayed !== null) termLog(this.boardIndex, displayed, 'out');
              }
            }
          }
        }
      } catch (e) {
        // Board disconnected mid-read (e.g. reboot) — exit outer loop to trigger reconnect
        termLog(this.boardIndex, `Port closed: ${e.message}`, 'sys');
        break outer;
      } finally {
        try { this.reader?.releaseLock(); } catch (_) {}
        this.reader = null;
      }
      if (!this._connected) break outer;
    }
    // Clean up if we exited due to board reboot rather than user disconnect
    if (this._connected) {
      this._connected = false;
      updateConnectionUI(this.boardIndex, false);
      clearRemoteBoardsForRelay(this.boardIndex);   // drop any remote boards using this as relay
      termLog(this.boardIndex, 'Board disconnected — attempting reconnect…', 'sys');
      // Try to reconnect to the same port (board rebooting)
      const reconnected = await this.reconnect(10, 1500);
      if (reconnected) {
        updateConnectionUI(this.boardIndex, true);
        termLog(this.boardIndex, 'Reconnected after reboot', 'sys');
        showToast(`WCB ${this.boardIndex} reconnected`, 'success');
        termLog(this.boardIndex, 'Auto-pulling config…', 'sys');
        setTimeout(() => boardPull(this.boardIndex), 3000);
      } else {
        termLog(this.boardIndex, 'Could not reconnect — board may need manual reconnect', 'err');
        showToast(`WCB ${this.boardIndex} did not come back — reconnect manually`, 'error');
      }
    }
  }
}

// ─── Board Actions ────────────────────────────────────────────────
// ─── Connect Modal ────────────────────────────────────────────────
function openConnectModal(n) {
  _connectModalSlot = n;
  // Title reflects whether auto-detect is already running
  document.getElementById('connect-modal-title').textContent =
    _detecting[n] ? `WCB ${n}: Auto-Detecting…` : `Connect WCB ${n}`;

  // Populate remote relay options — boards already connected via USB that can relay
  const remoteSection = document.getElementById('connect-modal-remote-section');
  const relays = Object.entries(boardConnections)
    .filter(([k, c]) => parseInt(k) !== n && c?.isConnected())
    .map(([k]) => parseInt(k));

  if (relays.length > 0) {
    remoteSection.innerHTML =
      `<div class="modal-divider-label">or connect wirelessly</div>` +
      relays.map(r =>
        `<button class="modal-option" onclick="modalRemoteConnect(${r})">
          <span class="modal-option-icon">📡</span>
          <div class="modal-option-text">
            <div class="modal-option-title">Remote via WCB ${r}</div>
            <div class="modal-option-desc">Pull and push config wirelessly using WCB ${r} as relay over ESP-NOW.</div>
          </div>
        </button>`
      ).join('');
    remoteSection.style.display = 'flex';
  } else {
    remoteSection.innerHTML = '';
    remoteSection.style.display = 'none';
  }

  document.getElementById('connect-modal').classList.add('open');
}

function closeConnectModal(event) {
  if (event && event.target !== document.getElementById('connect-modal')) return;
  // Any dismissal (X, overlay, Cancel) stops auto-detect if it was running
  const n = _connectModalSlot;
  if (n !== null) _detecting[n] = false;
  document.getElementById('connect-modal').classList.remove('open');
  _connectModalSlot = null;
}

async function modalAutoDetect() {
  const n = _connectModalSlot;
  const alreadyDetecting = !!_detecting[n];
  document.getElementById('connect-modal').classList.remove('open');
  _connectModalSlot = null;
  // If already detecting, just close the modal — auto-detect keeps running
  if (n !== null && !alreadyDetecting) await boardAutoDetect(n);
}

async function modalManualSelect() {
  const n = _connectModalSlot;
  document.getElementById('connect-modal').classList.remove('open');
  _connectModalSlot = null;
  if (n !== null) {
    _detecting[n] = false; // cancel auto-detect if it was running
    await boardManualConnect(n);
  }
}

function modalRemoteConnect(relayN) {
  const n = _connectModalSlot;
  document.getElementById('connect-modal').classList.remove('open');
  _connectModalSlot = null;
  if (n === null) return;
  _detecting[n] = false;
  setRemoteConnected(n, relayN);
  remoteBoardPull(relayN, n);
}

function setRemoteConnected(n, relayN) {
  remoteRelayForBoard[n] = relayN;
  const label = document.getElementById(`b${n}-conn-label`);
  if (label) label.textContent = `Remote via WCB ${relayN}`;
  const connBtn     = document.getElementById(`b${n}-btn-connect`);
  const pullBtn     = document.getElementById(`b${n}-btn-pull`);
  const goBtn       = document.getElementById(`b${n}-btn-go`);
  const eraseBtn    = document.getElementById(`b${n}-btn-erase`);
  const identifyBtn = document.getElementById(`b${n}-btn-identify`);
  if (connBtn)     { connBtn.textContent = 'Disconnect'; connBtn.classList.remove('btn-detecting', 'btn-primary'); connBtn.classList.add('btn-danger'); }
  if (pullBtn)     { pullBtn.disabled = false; pullBtn.textContent = 'Pull Config'; }
  if (goBtn)       { goBtn.disabled = false; }  // Go now delegates to boardGoRemote for remote boards
  if (eraseBtn)    eraseBtn.disabled = true;    // erase not available via relay
  if (identifyBtn) identifyBtn.disabled = false; // identify works via MGMT channel
  // Disable flash/update/factory modes — remote boards can only be configured via relay
  ['flash', 'update', 'factory'].forEach(mode => {
    const radio = document.querySelector(`input[name="b${n}-mode"][value="${mode}"]`);
    if (radio) radio.disabled = true;
  });
  const configRadio = document.querySelector(`input[name="b${n}-mode"][value="configure"]`);
  if (configRadio) configRadio.checked = true;
  updateBoardStatusBadge(n, 'remote');
  ensureTerminalPane(relayN);          // remote traffic shows in relay's terminal
  updateTerminalPaneDot(n, true);
  updateSequencePlayButtons(n);        // enable UPDATE/TEST buttons for remote boards
  installEtmListener(relayN);         // track live/offline state via relay's ETM output
}

// When a relay board goes offline, disconnect every remote board that relied on it.
function clearRemoteBoardsForRelay(relayN) {
  Object.keys(remoteRelayForBoard)
    .map(Number)
    .filter(n => remoteRelayForBoard[n] === relayN)
    .forEach(n => {
      termLog(relayN, `[Remote] WCB${n} disconnected — relay WCB${relayN} went offline`, 'sys');
      clearRemoteConnected(n);
    });
}

function clearRemoteConnected(n) {
  const relayN = remoteRelayForBoard[n];
  // Best-effort: tell the target board to stop forwarding terminal output
  if (relayN) stopRemoteTermSession(relayN, n);
  delete remoteRelayForBoard[n];
  // Re-enable flash/update/factory mode radios that setRemoteConnected disabled
  ['flash', 'update', 'factory'].forEach(mode => {
    const radio = document.querySelector(`input[name="b${n}-mode"][value="${mode}"]`);
    if (radio) radio.disabled = false;
  });
  // Remove the ETM listener from the relay if no other boards still use it
  if (relayN && !Object.values(remoteRelayForBoard).includes(relayN)) {
    removeEtmListener(relayN);
  }
  updateConnectionUI(n, false);        // resets buttons and badge to disconnected state
}

// ─── Remote Terminal Session Management ───────────────────────────
// Starts a remote terminal session on board targetN by sending
// ?RTERM,START,<relayN> via MGMT (single-chunk push).
async function startRemoteTermSession(relayN, targetN) {
  const relayConn = boardConnections[relayN];
  if (!relayConn?.isConnected()) return;
  try {
    const sessionId = Math.floor(Math.random() * 0xFFFF).toString(16).toUpperCase().padStart(4, '0');
    await relayConn.send(`?MGMT,FRAG,${targetN},${sessionId},0,1,?RTERM,START,${relayN}\r`);
    termLog(relayN, `[Remote] WCB${targetN} remote terminal started`, 'sys');
  } catch (_) {}
}

// Stops the remote terminal session on board targetN.
async function stopRemoteTermSession(relayN, targetN) {
  const relayConn = boardConnections[relayN];
  if (!relayConn?.isConnected()) return;
  try {
    const sessionId = Math.floor(Math.random() * 0xFFFF).toString(16).toUpperCase().padStart(4, '0');
    await relayConn.send(`?MGMT,FRAG,${targetN},${sessionId},0,1,?RTERM,STOP\r`);
  } catch (_) {}
}

// ─── General Settings Conflict Modal ──────────────────────────────
function showGeneralMismatchModal(baselineBoard, baselineFields, newBoard, newFields, mismatches) {
  const srcLabel = typeof baselineBoard === 'number' ? `WCB ${baselineBoard}` : 'loaded file';
  const newLabel = `WCB ${newBoard}`;

  document.getElementById('general-conflict-body').innerHTML = `
    <p style="margin-bottom:12px;font-size:12px;color:var(--text2)">
      <strong style="color:var(--yellow)">${newLabel}</strong> reports different network settings
      than <strong style="color:var(--text)">${srcLabel}</strong>.
      These must match across all boards — choose which values to keep:
    </p>
    <table class="serial-table">
      <thead><tr>
        <th>Field</th>
        <th>${srcLabel} <span style="color:var(--text3);font-weight:400">(current)</span></th>
        <th>${newLabel} <span style="color:var(--text3);font-weight:400">(incoming)</span></th>
      </tr></thead>
      <tbody>${mismatches.map(m => `<tr>
        <td style="color:var(--text2)">${m.label}</td>
        <td style="color:var(--yellow);font-family:var(--mono);font-size:12px">${escHtml(String(m.aVal))}</td>
        <td style="color:var(--accent);font-family:var(--mono);font-size:12px">${escHtml(String(m.bVal))}</td>
      </tr>`).join('')}</tbody>
    </table>`;

  // Clone buttons to clear any stale listeners from previous invocations
  ['general-conflict-keep', 'general-conflict-use'].forEach(id => {
    const old = document.getElementById(id);
    const fresh = old.cloneNode(true);
    old.parentNode.replaceChild(fresh, old);
  });

  document.getElementById('general-conflict-keep').textContent = `Keep ${srcLabel} values`;
  document.getElementById('general-conflict-keep').addEventListener('click', () => {
    document.getElementById('general-conflict-modal').classList.remove('open');
    showToast(`Kept ${srcLabel} general settings`, 'info');
  });

  document.getElementById('general-conflict-use').textContent = `Use ${newLabel} values`;
  document.getElementById('general-conflict-use').addEventListener('click', () => {
    generalBaseline = { sourceBoard: newBoard, fields: newFields };
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
    set('g-password',  newFields.espnowPassword);
    set('g-mac2',      newFields.macOctet2);
    set('g-mac3',      newFields.macOctet3);
    set('g-delimiter', newFields.delimiter);
    set('g-funcchar',  newFields.funcChar);
    set('g-cmdchar',   newFields.cmdChar);
    onGeneralPasswordChange();
    onGeneralMacChange();
    onGeneralCmdCharChange();
    document.getElementById('general-conflict-modal').classList.remove('open');
    showToast(`General settings updated to match ${newLabel}`, 'success');
  });

  document.getElementById('general-conflict-modal').classList.add('open');
}

function closeGeneralConflictModal(event) {
  if (event && event.target !== document.getElementById('general-conflict-modal')) return;
  document.getElementById('general-conflict-modal').classList.remove('open');
}

// Single entry point:
//   Not connected, not detecting → open modal AND start auto-detect simultaneously
//   Detecting (clicked again)    → open modal (continue / switch to manual / cancel)
//   Connected                    → disconnect
function boardConnect(n) {
  if (boardConnections[n]?.isConnected()) { boardDisconnect(n); return; }
  if (remoteRelayForBoard[n]) { clearRemoteConnected(n); return; }
  if (_detecting[n]) { openConnectModal(n); return; } // options menu while detecting
  openConnectModal(n);        // show modal; user picks Auto-Detect / Manual / Remote
}

async function boardDisconnect(n) {
  const btn = document.getElementById(`b${n}-btn-connect`);
  if (btn) { btn.textContent = 'Disconnecting…'; btn.disabled = true; }
  clearRemoteBoardsForRelay(n);              // drop remote boards that relay through this one
  try { await boardConnections[n]?.disconnect(); } catch (_) {}
  delete boardConnections[n];
  delete remoteRelayForBoard[n];
  updateConnectionUI(n, false);
  if (btn) btn.disabled = false;
}

// ─── Manual connect (port picker) ─────────────────────────────────
async function boardManualConnect(n) {
  const btn = document.getElementById(`b${n}-btn-connect`);
  if (btn) { btn.textContent = 'Connecting…'; btn.disabled = true; }
  try {
    const usedPorts = new Set(
      Object.entries(boardConnections)
        .filter(([k, c]) => parseInt(k) !== n && c?.isConnected() && c.port)
        .map(([, c]) => c.port)
    );
    const conn = new BoardConnection(n);
    await conn.connect(null, usedPorts);
    boardConnections[n] = conn;
    delete remoteRelayForBoard[n];   // direct USB connection — not routed via relay
    updateConnectionUI(n, true);
    showToast(`Connected to WCB ${n} — pulling config…`, 'success');
    setTimeout(() => boardPull(n), 500);
  } catch (e) {
    if (e.name !== 'NotFoundError') showToast(`Connection failed: ${e.message}`, 'error');
  }
  if (btn) btn.disabled = false; // text already set correctly by updateConnectionUI
}

// ─── Auto-detect: monitor ports for reset, connect the one that resets ─
// Known USB-UART vendor IDs used on WCB hardware
const WCB_VENDOR_IDS = [
  0x10C4, // Silicon Labs (CP2102, CP2102N, CP2104)
  0x1A86, // QinHeng (CH340, CH341)
  0x0403, // FTDI (FT232R)
];

async function boardAutoDetect(n) {
  const btn = document.getElementById(`b${n}-btn-connect`);

  function setDetecting(yes) {
    if (btn) {
      if (yes) {
        btn.textContent = '⊙ Detecting… (cancel)';
      } else if (!boardConnections[n]?.isConnected() && remoteRelayForBoard[n] === undefined) {
        btn.textContent = 'Connect'; // only reset if we didn't end up connected (direct or remote)
      }
      btn.classList.toggle('btn-detecting', yes);
      btn.disabled = false; // always keep clickable so user can cancel
    }
  }

  _detecting[n] = true;
  setDetecting(true);

  // ── Step 1: get all already-authorized ports ──────────────────
  let knownPorts = await navigator.serial.getPorts();
  const firstTime = knownPorts.length === 0;

  if (firstTime) {
    showToast('No paired boards — select your WCB boards in the picker to authorize them.', 'info', 7000);
    try {
      const filters = WCB_VENDOR_IDS.map(id => ({ usbVendorId: id }));
      await navigator.serial.requestPort({ filters });
      knownPorts = await navigator.serial.getPorts();
    } catch {
      _detecting[n] = false; setDetecting(false); return;
    }
  }

  if (!_detecting[n]) { setDetecting(false); return; } // cancelled during picker

  // Filter to WCB-type ports not already in use
  const usedPorts = new Set(
    Object.values(boardConnections).filter(c => c?.isConnected() && c.port).map(c => c.port)
  );
  const available = knownPorts.filter(p => {
    if (usedPorts.has(p)) return false;
    const { usbVendorId } = p.getInfo();
    return WCB_VENDOR_IDS.includes(usbVendorId);
  });

  // ── First-time: connect ALL authorized boards immediately ─────
  if (firstTime) {
    if (available.length === 0) {
      showToast('No WCB boards found to connect.', 'info');
      _detecting[n] = false; setDetecting(false); return;
    }
    const freeSlots = [];
    for (let s = 1; s <= 8; s++) {
      if (!boardConnections[s]?.isConnected()) freeSlots.push(s);
    }
    let busy = 0;
    await Promise.all(available.slice(0, freeSlots.length).map(async (port, i) => {
      const slot = freeSlots[i];
      try {
        const conn = new BoardConnection(slot);
        await conn.connect(port);
        boardConnections[slot] = conn;
        updateConnectionUI(slot, true);
        setTimeout(() => boardPull(slot), 500);
      } catch (err) {
        if (/open|use|busy|access/i.test(err.message)) busy++;
      }
    }));
    if (busy > 0)
      showToast(`${busy} port${busy > 1 ? 's' : ''} skipped — already open in another application`, 'warning', 8000);
    _detecting[n] = false; setDetecting(false); return;
  }

  if (available.length === 0) {
    showToast('All known WCB boards are already connected.', 'info');
    _detecting[n] = false; setDetecting(false); return;
  }

  const resetToast = showToast(`WCB ${n}: press the reset button on the board you want here…`, 'info', 45000);

  // ── Monitor all available ports for reset (boot data) ─────────
  let triggered = false;
  const openReaders = new Map();

  async function cleanup(winningPort) {
    clearTimeout(timeoutId);
    for (const [port, reader] of openReaders) {
      try { await reader.cancel(); } catch (_) {}
      try { reader.releaseLock(); } catch (_) {}
      if (port !== winningPort) { try { await port.close(); } catch (_) {} }
    }
    openReaders.clear();
  }

  async function onReset(port) {
    triggered = true;
    await cleanup(port);
    resetToast?.remove(); // dismiss the "press reset" prompt immediately
    try { await port.close(); } catch (_) {}
    try {
      const conn = new BoardConnection(n);
      await conn.connect(port);
      boardConnections[n] = conn;
      updateConnectionUI(n, true);
      // Auto-close the connect modal if it's still open for this slot
      if (_connectModalSlot === n) {
        document.getElementById('connect-modal').classList.remove('open');
        _connectModalSlot = null;
      }
      showToast(`WCB ${n}: board detected — waiting for boot…`, 'success');
      setTimeout(() => boardPull(n), 3500);
    } catch (err) {
      showToast(`Auto-connect failed: ${err.message}`, 'error');
    }
    _detecting[n] = false; setDetecting(false);
  }

  async function monitorPort(port) {
    try { await port.open({ baudRate: 115200 }); } catch {
      showToast('One port skipped — already open in another application', 'warning', 6000);
      return;
    }
    const reader = port.readable.getReader();
    openReaders.set(port, reader);
    try {
      while (!triggered && _detecting[n]) {
        const { value, done } = await reader.read();
        if (done || triggered || !_detecting[n]) break;
        if (value?.length > 0 && !triggered) { await onReset(port); break; }
      }
    } catch (_) { /* cancelled by cleanup */ }
  }

  // Poll for cancel via Connect button click
  const cancelCheck = setInterval(async () => {
    if (!_detecting[n] && !triggered) {
      triggered = true;
      await cleanup(null);
      setDetecting(false);
      clearInterval(cancelCheck);
      showToast(`WCB ${n}: auto-detect cancelled`, 'info');
    }
    if (triggered) clearInterval(cancelCheck);
  }, 300);

  const timeoutId = setTimeout(async () => {
    if (triggered) return;
    triggered = true; _detecting[n] = false;
    await cleanup(null);
    setDetecting(false);
    showToast(`WCB ${n}: nothing detected — auto-detect timed out`, 'info');
  }, 45000);

  for (const port of available) monitorPort(port);
}

async function boardPull(n) {
  // Delegate to remote pull if this board is reached via relay
  if (remoteRelayForBoard[n]) { boardPullRemote(n); return; }

  const conn = boardConnections[n];
  if (!conn?.isConnected()) { showToast('Board not connected', 'error'); return; }

  const btn = document.getElementById(`b${n}-btn-pull`);
  if (btn) { btn.disabled = true; btn.textContent = 'Pulling…'; }
  termLog(n, '?backup', 'in');

  let migrated = false;
  try {
    const raw    = await conn.sendAndCollect('?backup', 8000);
    const config = WCBParser.parseBackupString(raw);

    // ── General settings: establish baseline on first pull, detect mismatches after ──
    const incomingGeneral = extractGeneralFields(config);
    if (!generalBaseline) {
      syncGeneralFromConfig(config);
      generalBaseline = { sourceBoard: config.wcbNumber || n, fields: incomingGeneral };
    } else {
      const mismatches = getGeneralMismatches(generalBaseline.fields, incomingGeneral);
      if (mismatches.length > 0 && !_wizardOpen) {
        setTimeout(() => showGeneralMismatchModal(
          generalBaseline.sourceBoard, generalBaseline.fields,
          config.wcbNumber || n, incomingGeneral, mismatches
        ), 200);
      }
    }
    if (config.wcbQuantity > 1) renderBoards(config.wcbQuantity);

    const detected = config.wcbNumber;

    if (detected !== n && detected >= 2 && detected <= 8) {
      // ── Board self-identifies as a different slot ─────────────────
      if (boardConnections[detected]?.isConnected()) {
        // Conflict: target slot already occupied → keep in current slot with a warning
        showToast(
          `WCB ${detected} detected in slot ${n} — slot ${detected} is already connected, keeping here`,
          'warning', 8000
        );
        boardConfigs[n]   = config;
        boardBaselines[n] = JSON.parse(JSON.stringify(config));
        populateUIFromConfig(n, config);
        updateBoardStatusBadge(n, 'connected');
        fetchBoardVersion(n);
      } else {
        // Migrate: move connection + config from slot n → slot detected
        conn.boardIndex          = detected;
        boardConnections[detected] = conn;
        delete boardConnections[n];

        boardConfigs[detected]   = config;
        boardBaselines[detected] = JSON.parse(JSON.stringify(config));
        delete boardConfigs[n];
        delete boardBaselines[n];

        populateUIFromConfig(detected, config);
        updateConnectionUI(n, false);        // clear vacated slot
        updateConnectionUI(detected, true);  // activate correct slot

        termLog(detected, `↑ Auto-migrated from slot ${n} — this board is WCB ${detected}`, 'sys');
        showToast(`WCB ${detected} detected — moved from slot ${n} → slot ${detected}`, 'info', 6000);
        migrated = true;
        fetchBoardVersion(detected);
      }
    } else {
      // ── Board number matches slot (normal path) ───────────────────
      boardConfigs[n]   = config;
      boardBaselines[n] = JSON.parse(JSON.stringify(config));
      populateUIFromConfig(n, config);
      updateBoardStatusBadge(n, 'connected');
      showToast(`Config pulled from WCB ${n}`, 'success');
      fetchBoardVersion(n);
    }
  } catch (e) {
    const isTimeout = /timeout/i.test(e.message);
    const msg = isTimeout
      ? `Pull timed out — board may have no firmware loaded. Try Flash mode first.`
      : `Pull failed: ${e.message}`;
    showToast(msg, 'error');
    termLog(n, msg, 'err');
  }

  // Only restore slot-n's pull button if we didn't migrate away from it
  // (migration calls updateConnectionUI(n, false) which already disables it)
  if (!migrated && btn) { btn.textContent = 'Pull Config'; btn.disabled = false; }
}

// Fetch the firmware version string directly from the board and display it.
// Sends ?version and parses "Software Version: <ver>" from the response.
// Updates the SOFTWARE VERSION field in the board panel with the full version string.
async function fetchBoardVersion(n) {
  const conn = boardConnections[n];
  if (!conn?.isConnected()) return;
  try {
    const raw   = await conn.sendAndCollect('?version', 3000, 'End of Version');
    const match = raw.match(/Software Version:\s*(\S+)/i);
    if (match) {
      const ver = match[1].trim();
      if (boardConfigs[n]) boardConfigs[n].fwVersion = ver;
      updateBoardSwVersionDisplay(n);
    }
  } catch (_) { /* version fetch is optional — ignore timeouts/errors */ }
}

async function boardGo(n, opts = {}) {
  // Delegate to the remote push path when this board is reached via relay
  if (remoteRelayForBoard[n]) return boardGoRemote(n, opts);
  const skipReboot = opts.skipReboot ?? false;

  const conn = boardConnections[n];
  if (!conn?.isConnected()) { showToast('Board not connected', 'error'); return; }

  const mode      = opts.mode ?? boardFlashMode[n] ?? 'configure';
  const isFlash   = mode === 'flash';
  const isUpdate  = mode === 'update';
  const isFactory = mode === 'factory';
  const isErase   = mode === 'erase';
  const btn       = document.getElementById(`b${n}-btn-go`);

  btn.disabled = true;
  btn.textContent = (isFlash || isUpdate || isFactory) ? 'Flashing…' : isErase ? 'Erasing…' : 'Pushing…';

  if (isFlash || isUpdate || isFactory) {
    // ── Validate HW version ──────────────────────────────────────
    const hwVersion = boardConfigs[n]?.hwVersion;
    if (!hwVersion) {
      showToast('Select a hardware version before flashing', 'error');
      btn.disabled = false;
      btn.textContent = 'Push Config';
      return;
    }

    // ── Snapshot config and DOM NOW — before the flash starts ────────────
    // The flash takes 30–60 s; during that window other boardPulls can
    // overwrite boardConfigs[n] and the shared general DOM fields (password,
    // MACs, WCBQ), so we must capture here while everything is still correct.
    const preFlashConfigSnapshot  = boardConfigs[n] ? JSON.parse(JSON.stringify(boardConfigs[n])) : null;
    const preFlashGeneralSnapshot = captureGeneralDOMSnapshot();

    // ── Save port ref, then close normal connection ───────────────
    const savedPort = conn.port;
    await conn.closeForReconnect();
    updateConnectionUI(n, false);

    btn.textContent = 'Flashing…';
    setFlashUI(n, true);

    let flashOk = false;
    try {
      await flashFirmware(savedPort, hwVersion, {
        onProgress: (written, total) => updateFlashBar(n, written, total),
        onLog:      (msg)            => termLog(n, msg, 'sys'),
        onStatus:   (msg)            => setFlashStatus(n, msg),
        appOnly:    isUpdate,         // Update FW: app-only, NVS preserved
        eraseNvs:   isFactory,        // Factory Reset: wipe NVS before flashing
      });
      flashOk = true;
      // Track flashed version so we can display it in the board section
      if (latestFirmwareVersion && boardConfigs[n])
        boardConfigs[n].fwVersion = latestFirmwareVersion.replace(/^v/, '');
      if (boardConfigs[n]?.fwVersion) updateBoardSwVersionDisplay(n);
      else setBoardSwVersion(n, '(flashed)', true);
      const label = isUpdate ? 'updated' : isFactory ? 'factory reset' : 'flashed';
      showToast(`WCB ${n} firmware ${label}!`, 'success');
    } catch (e) {
      showToast(`Flash failed: ${e.message.split('\n')[0]}`, 'error');
      termLog(n, `Flash error: ${e.message}`, 'err');
    }

    setFlashUI(n, false);

    if (flashOk) {
      termLog(n, 'Reconnecting after flash…', 'sys');
      const reconnected = await conn.reconnect(12, 2000);
      if (reconnected) {
        updateConnectionUI(n, true);
        boardFlashMode[n] = 'configure';   // reset mode after flash

        if (isUpdate) {
          // App-only update: NVS is intact, just pull to verify config survived
          termLog(n, 'Reconnected — pulling config to verify NVS…', 'sys');
          showToast(`WCB ${n} updated — verifying config…`, 'success');
          setTimeout(() => boardPull(n), 4000);
        } else {
          // Flash or Factory Reset: NVS is blank.
          // Restore the pre-flash config snapshot so boardConfigs[n] is ready,
          // then offer the user a Push Config button rather than auto-pushing.
          // This lets them verify the config is correct before it's sent.
          if (preFlashConfigSnapshot) {
            boardConfigs[n]   = JSON.parse(JSON.stringify(preFlashConfigSnapshot));
            boardBaselines[n] = null;   // force full push
            populateUIFromConfig(n, boardConfigs[n]);
          }
          // Store the pre-flash general snapshot so boardGo can restore it
          // just before reading the DOM (DOM may have drifted while flashing).
          postFlashGeneralSnapshot[n] = preFlashGeneralSnapshot;

          const resetLabel = isFactory ? 'factory reset' : 'flashed';
          termLog(n, `Reconnected after ${resetLabel} — click Push Config to send the configuration`, 'sys');
          showPostFlashPrompt(n, resetLabel);
        }
      } else {
        termLog(n, 'Reconnect failed — connect manually', 'err');
        showToast(`WCB ${n} did not come back — reconnect manually`, 'error');
      }
    } else {
      const recovered = await conn.reconnect(5, 1500);
      if (recovered) updateConnectionUI(n, true);
    }

    btn.disabled = false;
    btn.textContent = 'Push Config';
    return;
  }

  // ── Erase-only path (already installed, erase NVS then push config) ─────
  if (isErase) {
    // Snapshot the full wizard config NOW before any erase/reconnect happens.
    // After the board comes back, something (auto-detect, startup handler) may
    // trigger a boardPull that overwrites boardConfigs[n] with factory defaults.
    // We restore this snapshot inside the setTimeout callback — right before
    // calling boardGo — so the full push always uses the intended wizard config
    // regardless of any intervening pulls.
    const preEraseConfigSnapshot = boardConfigs[n] ? JSON.parse(JSON.stringify(boardConfigs[n])) : null;

    termLog(n, '?ERASE,NVS', 'in');
    try {
      await conn.send('?ERASE,NVS\r');
      // Firmware counts down ~3 s before erasing and rebooting.
      // Closing the serial port immediately causes a USB-disconnect reset that
      // fires BEFORE the erase runs — so we wait 4 s to let the firmware finish.
      termLog(n, 'Waiting for firmware erase countdown…', 'sys');
      showToast(`WCB ${n} erasing — do not disconnect…`, 'warning', 5000);
      await sleep(4000);
      termLog(n, 'NVS erased — board rebooting…', 'sys');
      updateBoardStatusBadge(n, 'default');

      await conn.closeForReconnect();
      updateConnectionUI(n, false);
      boardFlashMode[n] = 'configure';   // reset so follow-up push is a normal configure
      termLog(n, 'Board disconnected — attempting reconnect…', 'sys');

      const reconnected = await conn.reconnect(10, 1500);
      if (reconnected) {
        updateConnectionUI(n, true);
        termLog(n, 'Reconnected after NVS erase — pushing configuration…', 'sys');
        showToast(`WCB ${n} reconnected — pushing config…`, 'success');
        // NVS is blank; wait a moment for the board to fully boot, then restore
        // the pre-erase config snapshot and force a full push (baseline=null).
        // Restoring inside the callback guards against any boardPull that may
        // have fired during the wait and overwritten boardConfigs[n].
        // We also re-populate the UI from the snapshot so that syncKyberToConfig /
        // syncSerialUIToConfig / syncMaestrosToConfig read the correct DOM state —
        // without this, a pull during the wait would have reset the radio buttons and
        // dropdowns to factory defaults, causing buildCommandString to emit KYBER,CLEAR
        // instead of the intended KYBER,LOCAL configuration.
        setTimeout(() => {
          if (preEraseConfigSnapshot) {
            boardConfigs[n]   = JSON.parse(JSON.stringify(preEraseConfigSnapshot));
            boardBaselines[n] = null;   // force full push — board NVS is blank
            populateUIFromConfig(n, boardConfigs[n]);   // re-sync DOM to wizard config
          }
          boardGo(n, { mode: 'configure' });
        }, 3000);
      } else {
        termLog(n, 'Could not auto-reconnect — reconnect manually, then push config', 'err');
        showToast(`WCB ${n} did not come back — reconnect manually`, 'error');
      }
    } catch (e) {
      showToast(`Erase failed: ${e.message}`, 'error');
      termLog(n, `Erase error: ${e.message}`, 'err');
    }
    btn.disabled = false;
    btn.textContent = 'Push Config';
    return;
  }

  let _needsReboot = false;
  try {
    // If this push follows a flash/factory reset, restore the pre-flash general DOM
    // snapshot now so the correct network settings (password, MACs, WCBQ) are used.
    // The DOM may have drifted during the long flash operation.
    if (postFlashGeneralSnapshot[n]) {
      restoreGeneralDOMSnapshot(postFlashGeneralSnapshot[n]);
      delete postFlashGeneralSnapshot[n];
    }

    // Collect current UI state into config
    syncSerialUIToConfig(n);
    syncMaestrosToConfig(n);
    syncKyberToConfig(n);
    autoComputeKyberTargets(n);   // derive targets from all boards' Maestros
    const config = boardConfigs[n];
    config.sequences     = getSequencesFromUI(n);
    config.espnowPassword = document.getElementById('g-password').value || 'change_me_or_risk_takeover';
    config.macOctet2      = document.getElementById('g-mac2').value?.toUpperCase() || '00';
    config.macOctet3      = document.getElementById('g-mac3').value?.toUpperCase() || '00';
    config.delimiter      = document.getElementById('g-delimiter').value || '^';
    config.funcChar       = document.getElementById('g-funcchar').value  || '?';
    config.cmdChar        = document.getElementById('g-cmdchar').value   || ';';
    config.wcbQuantity    = parseInt(document.getElementById('g-wcbq').value) || 1;

    const fullPush  = !boardBaselines[n];
    const cmdString = WCBParser.buildCommandString(config, boardBaselines[n] ?? null, fullPush);

    if (!cmdString) {
      showToast('No changes to push', 'info');
      btn.disabled = false;
      btn.textContent = 'Push Config';
      return;
    }

    // Split on delimiter+funcChar boundary (e.g. "^?") rather than every "^",
    // so that sequence values containing "^" (chained device commands) are not torn
    // apart.  Each split piece after the first needs the funcChar re-prepended.
    const funcChar = config.funcChar || '?';
    const delim    = config.delimiter || '^';
    const rawParts = cmdString.split(delim + funcChar);
    const commands = rawParts.map((p, i) => (i === 0 ? p : funcChar + p).trim()).filter(Boolean);
    for (const cmd of commands) {
      await conn.send(cmd + '\r');
      termLog(n, cmd, 'in');
      await sleep(80);
    }

    _needsReboot = commandStringNeedsReboot(cmdString);
    if (_needsReboot) {
      if (!skipReboot) {
        // ── Reboot path ─────────────────────────────────────────────────
        await sleep(1500);   // allow board time to finish all NVS writes before rebooting
        await conn.send('?reboot\r');
        termLog(n, '?reboot', 'in');

        updateBoardStatusBadge(n, 'configured');
        showToast(`WCB ${n} configured — rebooting…`, 'success');

        // Close our side immediately — this cancels any pending reader.read() so
        // _startReading exits cleanly (sees _connected=false, skips its own reconnect).
        // Then we manage the reconnect ourselves in the background.
        await conn.closeForReconnect();
        updateConnectionUI(n, false);
        termLog(n, 'Board disconnected — attempting reconnect…', 'sys');

        // Fire-and-forget reconnect so the Go button re-enables right away
        (async () => {
          const reconnected = await conn.reconnect(10, 1500);
          if (reconnected) {
            updateConnectionUI(n, true);
            termLog(n, 'Reconnected after reboot', 'sys');
            showToast(`WCB ${n} reconnected`, 'success');
            termLog(n, 'Auto-pulling config…', 'sys');
            setTimeout(() => boardPull(n), 3000);
          } else {
            termLog(n, 'Could not auto-reconnect — reconnect manually', 'err');
            showToast(`WCB ${n} did not come back — reconnect manually`, 'error');
          }
        })();

      } else {
        // ── Deferred-reboot path — boardGoAll sends the reboot explicitly ──
        // Config is applied; connection stays open so the relay can forward
        // MGMT reboot commands to remote boards before it goes down itself.
        boardBaselines[n] = JSON.parse(JSON.stringify(config));
        updateBoardStatusBadge(n, 'configured');
        termLog(n, 'Config applied — reboot queued by Push All…', 'sys');
        showToast(`WCB ${n} configured`, 'info');
      }

    } else {
      // ── No-reboot path ────────────────────────────────────────
      // All sent commands take effect immediately; connection stays open.
      // Update the baseline optimistically, then pull to verify.
      boardBaselines[n] = JSON.parse(JSON.stringify(config));
      updateBoardStatusBadge(n, 'configured');
      termLog(n, 'Config applied (no reboot needed) — verifying…', 'sys');
      showToast(`WCB ${n} configured`, 'success');
      setTimeout(() => boardPull(n), 800);
    }

  } catch (e) {
    showToast(`Push failed: ${e.message}`, 'error');
    termLog(n, `Push error: ${e.message}`, 'err');
  }

  btn.disabled = false;
  btn.textContent = 'Push Config';
  return _needsReboot;
}

function updateConnectionUI(n, connected) {
  // Don't clobber a remote connection's UI with a spurious direct-disconnect event.
  // clearRemoteConnected() always deletes remoteRelayForBoard[n] first, so this
  // guard only fires for unintended callers (e.g. boardAutoDetect timing out).
  if (!connected && remoteRelayForBoard[n] !== undefined) return;
  document.getElementById(`b${n}-dot`)?.classList.toggle('connected', connected);
  const label = document.getElementById(`b${n}-conn-label`);
  if (label) label.textContent = connected ? 'Connected' : 'Not connected';
  const pullBtn     = document.getElementById(`b${n}-btn-pull`);
  const goBtn       = document.getElementById(`b${n}-btn-go`);
  const connBtn     = document.getElementById(`b${n}-btn-connect`);
  const eraseBtn    = document.getElementById(`b${n}-btn-erase`);
  const identifyBtn = document.getElementById(`b${n}-btn-identify`);
  if (pullBtn)     { pullBtn.disabled = !connected; if (!connected) pullBtn.textContent = 'Pull Config'; }
  if (goBtn)       goBtn.disabled       = !connected;
  if (eraseBtn)    eraseBtn.disabled    = !connected;
  if (identifyBtn) identifyBtn.disabled = !connected;
  if (connBtn) {
    connBtn.textContent = connected ? 'Disconnect' : 'Connect';
    connBtn.classList.toggle('btn-primary', !connected);
    connBtn.classList.toggle('btn-danger',   connected);
    if (!connected) connBtn.classList.remove('btn-detecting');
  }
  updateBoardStatusBadge(n, connected ? 'connected' : 'default');
  updateSequencePlayButtons(n);

  // Create pane on first connect; update dot/input state on any connect/disconnect
  if (connected) ensureTerminalPane(n);
  updateTerminalPaneDot(n, connected);
  if (connected) updatePaneVisibilityChip(n);
}

// ─── Remote Management ────────────────────────────────────────────

// Install a persistent ETM listener on a relay board's serial stream.
// Parses "[ETM] WCBn came ONLINE" / "[ETM] WCBn went OFFLINE" and drives
// the connected dot for whichever remote boards are behind that relay.
function installEtmListener(relayN) {
  if (_etmCallbacks[relayN]) return;            // already registered
  const relayConn = boardConnections[relayN];
  if (!relayConn) return;

  const cb = (line) => {
    const onlineMatch  = line.match(/\[ETM\] WCB(\d+) came ONLINE/);
    const offlineMatch = line.match(/\[ETM\] WCB(\d+) went OFFLINE/);
    if (onlineMatch) {
      const bn = parseInt(onlineMatch[1]);
      if (remoteRelayForBoard[bn] === relayN) {
        document.getElementById(`b${bn}-dot`)?.classList.add('connected');
        // Board announced itself — pull fresh config to confirm reachability and update state
        updateBoardStatusBadge(bn, 'remote');
        remoteBoardPull(relayN, bn);
      }
    } else if (offlineMatch) {
      const bn = parseInt(offlineMatch[1]);
      if (remoteRelayForBoard[bn] === relayN) {
        document.getElementById(`b${bn}-dot`)?.classList.remove('connected');
        // Guard: don't stack pulls if a verification is already running
        const badge = document.getElementById(`b${bn}-status-badge`);
        const alreadyVerifying = badge?.classList.contains('badge-yellow');
        if (!alreadyVerifying) {
          showToast(`⚠️ WCB ${bn}: ETM timeout — verifying reachability…`, 'warning', 4000);
          updateBoardStatusBadge(bn, 'retrying');
          remoteBoardPull(relayN, bn);
        }
      }
    }
  };

  relayConn.onData(cb);
  _etmCallbacks[relayN] = cb;
}

function removeEtmListener(relayN) {
  const cb = _etmCallbacks[relayN];
  if (!cb) return;
  const relayConn = boardConnections[relayN];
  if (relayConn) {
    relayConn._dataCallbacks = relayConn._dataCallbacks.filter(c => c !== cb);
  }
  delete _etmCallbacks[relayN];
}

function fragmentString(str, chunkSize) {
  const chunks = [];
  for (let i = 0; i < str.length; i += chunkSize) {
    chunks.push(str.slice(i, i + chunkSize));
  }
  return chunks;
}

async function boardGoRemote(n, opts = {}) {
  const skipReboot  = opts.skipReboot  ?? false;
  const skipConfirm = opts.skipConfirm ?? false;
  const btn = document.getElementById(`b${n}-btn-go`);

  const relayN = remoteRelayForBoard[n];
  if (!relayN) { showToast('No relay board set for this board', 'error'); return; }
  const relayConn = boardConnections[relayN];
  if (!relayConn?.isConnected()) { showToast(`WCB ${relayN} (relay) not connected`, 'error'); return; }

  // Sync all UI state into the config object
  syncSerialUIToConfig(n);
  syncMaestrosToConfig(n);
  syncKyberToConfig(n);
  autoComputeKyberTargets(n);
  const config = boardConfigs[n];
  if (!config) { showToast('No config for this board', 'error'); if (btn) { btn.disabled = false; btn.textContent = 'Push Config'; } return; }

  config.sequences      = getSequencesFromUI(n);
  config.espnowPassword = document.getElementById('g-password').value || 'change_me_or_risk_takeover';
  config.macOctet2      = document.getElementById('g-mac2').value?.toUpperCase() || '00';
  config.macOctet3      = document.getElementById('g-mac3').value?.toUpperCase() || '00';
  config.delimiter      = document.getElementById('g-delimiter').value || '^';
  config.funcChar       = document.getElementById('g-funcchar').value  || '?';
  config.cmdChar        = document.getElementById('g-cmdchar').value   || ';';
  config.wcbQuantity    = parseInt(document.getElementById('g-wcbq').value) || 1;

  // Diff-based push: only send commands that differ from the pulled baseline
  const fullPush  = !boardBaselines[n];
  const cmdString = WCBParser.buildCommandString(config, boardBaselines[n] ?? null, fullPush);
  if (!cmdString) { showToast('Nothing to push — no changes detected', 'info'); if (btn) { btn.disabled = false; btn.textContent = 'Push Config'; } return; }

  // ── Network-group change guard ─────────────────────────────────────
  // MAC octets and password define the ESP-NOW network group.  After our
  // firmware fix, a board with mismatched octets can no longer communicate
  // with the relay at all — so pushing these changes to a single remote board
  // will silently brick that board's wireless connection until all other boards
  // are updated too.  Require an explicit click-through before proceeding.
  if (!skipConfirm && commandStringChangesNetworkGroup(cmdString)) {
    const confirmed = await confirmNetworkGroupChange(n, cmdString);
    if (!confirmed) {
      if (btn) { btn.disabled = false; btn.textContent = 'Push Config'; }
      return;
    }
  }

  if (btn) { btn.disabled = true; btn.textContent = 'Pushing…'; }

  const changeCount = cmdString.split('^').filter(Boolean).length;
  const changeLabel = fullPush ? 'full push' : `${changeCount} change${changeCount !== 1 ? 's' : ''}`;

  const needsReboot = commandStringNeedsReboot(cmdString);

  // Embed ?reboot atomically at the end of the MGMT session payload when needed.
  // A MAC change (e.g. ?MAC,3,07) updates umac_oct2/3 in memory immediately on
  // the target board; any subsequent ESP-NOW packet arriving with the relay's old
  // src_addr is silently dropped by the firmware's MAC filter — making a separate
  // MGMT reboot session unreachable.  Appending the reboot to the same session
  // ensures the sequence is: config change → reboot, all inside one session
  // execution, with no ESP-NOW exchange in between.
  let cmdToSend = cmdString;
  if (needsReboot && !skipReboot) {
    const funcChar = config.funcChar || '?';
    const delim    = config.delimiter || '^';
    cmdToSend = cmdString + delim + funcChar + 'reboot';
    showToast(`⚠️ WCB ${n}: board will reboot after push — remote connection may be lost`, 'warning', 6000);
  }

  // Generate a random 4-char hex session ID
  const sessionId = Math.floor(Math.random() * 0xFFFF).toString(16).toUpperCase().padStart(4, '0');
  const chunks = fragmentString(cmdToSend, MGMT_CHUNK_SIZE);
  const total  = chunks.length;

  termLog(relayN, `[Remote] Pushing WCB ${n} config (${changeLabel}) — ${total} chunk(s), session ${sessionId}`, 'sys');

  let pushSucceeded = false;
  try {
    for (let i = 0; i < chunks.length; i++) {
      const cmd = `?MGMT,FRAG,${n},${sessionId},${i},${total},${chunks[i]}\r`;
      termLog(relayN, cmd.trim(), 'in');
      await relayConn.send(cmd);
      if (i < chunks.length - 1) await sleep(MGMT_CHUNK_DELAY);
    }
    showToast(`WCB ${n}: ${changeLabel} sent via WCB ${relayN}`, 'success');
    termLog(relayN, `[Remote] All chunks sent for WCB ${n}`, 'sys');
    // Update baseline so the next push is diff-based against this state
    boardBaselines[n] = JSON.parse(JSON.stringify(config));
    pushSucceeded = true;
  } catch (e) {
    showToast(`Remote push failed: ${e.message}`, 'error');
    termLog(relayN, `[Remote] Error: ${e.message}`, 'err');
  }

  return pushSucceeded && needsReboot;
}

// Pull config from a remote board via the relay's CONFIG_REQ/CONFIG_FRAG protocol.
// Sends ?MGMT,PULL,<targetN> to the relay; waits up to 15 s for [MGMT:CONFIG,<targetN>].
// Auto-retries up to MAX_PULL_ATTEMPTS times (10 s apart) before marking error.
const MAX_PULL_ATTEMPTS = 3;
async function remoteBoardPull(relayN, targetN, attempt = 1) {
  const relayConn = boardConnections[relayN];
  if (!relayConn?.isConnected()) { showToast('Relay board not connected', 'error'); return; }

  termLog(relayN, `[Remote] Requesting config from WCB${targetN} (attempt ${attempt}/${MAX_PULL_ATTEMPTS})…`, 'sys');
  showToast(attempt === 1
    ? `Pulling config from WCB${targetN} via WCB${relayN}…`
    : `Retrying pull from WCB${targetN} (attempt ${attempt}/${MAX_PULL_ATTEMPTS})…`, 'info');

  const prefix = `[MGMT:CONFIG,${targetN}]`;
  let done = false;
  let timer;

  // Replace the raw config blob in the terminal with a char-count summary
  relayConn._lineTransform = (line) => {
    if (line.startsWith(prefix)) {
      return `${prefix} <${line.length - prefix.length} chars received>`;
    }
    return line;
  };

  const cleanup = () => {
    relayConn._lineTransform = null;
    relayConn._dataCallbacks = relayConn._dataCallbacks.filter(cb => cb !== onLine);
  };

  const onLine = (line) => {
    if (done || !line.startsWith(prefix)) return;
    done = true;
    clearTimeout(timer);
    cleanup();

    let configStr = line.slice(prefix.length).trim();
    if (!configStr) {
      updateBoardStatusBadge(targetN, 'error');
      showToast(`WCB${targetN}: empty config response`, 'error');
      termLog(relayN, `[Remote] WCB${targetN} returned empty config`, 'err');
      return;
    }

    // Extract [VER:<version>] prefix added by buildConfigString() and strip it
    // before handing the command string to the parser.
    let remoteFwVersion = null;
    const verMatch = configStr.match(/^\[VER:([^\]]+)\]/);
    if (verMatch) {
      remoteFwVersion = verMatch[1].trim();
      configStr = configStr.slice(verMatch[0].length);
    }

    try {
      const config = WCBParser.parseBackupString(configStr);
      boardConfigs[targetN]   = config;
      boardBaselines[targetN] = JSON.parse(JSON.stringify(config));
      // Store the firmware version from the VER prefix (if present) and update display
      if (remoteFwVersion) {
        boardConfigs[targetN].fwVersion = remoteFwVersion;
        updateBoardSwVersionDisplay(targetN);
      }
      populateUIFromConfig(targetN, config);
      // Preserve remote badge and connection label (setRemoteConnected may have run before us)
      updateBoardStatusBadge(targetN, remoteRelayForBoard[targetN] ? 'remote' : 'configured');
      showToast(`Config pulled from WCB${targetN} (remote via WCB${relayN})`, 'success');
      termLog(relayN, `[Remote] Config received from WCB${targetN} (${configStr.length} chars)`, 'sys');
      // Enable the terminal input and debug buttons for this remote board
      ensureTerminalPane(targetN);
      updateTerminalPaneDot(targetN, true);
      updatePaneVisibilityChip(targetN);   // refresh label in case board switched USB→remote
      // Start the remote terminal session so WCB${targetN}'s Serial output is mirrored here
      startRemoteTermSession(relayN, targetN);
    } catch (e) {
      updateBoardStatusBadge(targetN, 'error');
      showToast(`WCB${targetN}: config parse failed — ${e.message}`, 'error');
      termLog(relayN, `[Remote] Config parse error for WCB${targetN}: ${e.message}`, 'err');
    }
  };

  relayConn._dataCallbacks.push(onLine);
  timer = setTimeout(() => {
    if (done) return;
    done = true;
    cleanup();
    if (attempt < MAX_PULL_ATTEMPTS) {
      updateBoardStatusBadge(targetN, 'retrying');
      termLog(relayN, `[Remote] Pull attempt ${attempt}/${MAX_PULL_ATTEMPTS} timed out — retrying in 10s…`, 'sys');
      setTimeout(() => remoteBoardPull(relayN, targetN, attempt + 1), 10000);
    } else {
      updateBoardStatusBadge(targetN, 'error');
      showToast(`WCB${targetN}: config pull failed after ${MAX_PULL_ATTEMPTS} attempts`, 'error');
      termLog(relayN, `[Remote] Config pull from WCB${targetN} failed after ${MAX_PULL_ATTEMPTS} attempts`, 'err');
    }
  }, 15000);

  await relayConn.send(`?MGMT,PULL,${targetN}\r`);
}

// Convenience wrapper — uses the relay tracked for this board
function boardPullRemote(n) {
  const relayN = remoteRelayForBoard[n];
  if (!relayN) { showToast('No relay board set for this board', 'error'); return; }
  if (relayN === n) { showToast(`WCB${n} is the relay — connect directly`, 'error'); return; }
  remoteBoardPull(relayN, n);
}

// Manual retry after a failed remote pull — resets badge and starts a fresh attempt cycle
function boardRetryPull(n) {
  const relayN = remoteRelayForBoard[n];
  if (!relayN) { showToast('Board is no longer set as remote', 'error'); return; }
  updateBoardStatusBadge(n, 'remote');
  remoteBoardPull(relayN, n);
}

function syncGeneralFromConfig(config) {
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
  set('g-wcbq',      config.wcbQuantity);
  set('g-password',  config.espnowPassword);
  set('g-mac2',      config.macOctet2);
  set('g-mac3',      config.macOctet3);
  set('g-delimiter', config.delimiter);
  set('g-funcchar',  config.funcChar);
  set('g-cmdchar',   config.cmdChar);

  // Keep systemConfig.general in sync — el.value assignments above don't fire
  // oninput/onchange, so systemConfig.general would otherwise stay at its defaults.
  onGeneralPasswordChange();
  onGeneralMacChange();
  onGeneralCmdCharChange();

  const etmEl = document.getElementById('g-etm-enabled');
  if (etmEl) etmEl.checked = config.etm.enabled;
  if (config.etm.enabled) {
    set('g-etm-timeout', config.etm.timeoutMs);
    set('g-etm-hb',      config.etm.heartbeatSec);
    set('g-etm-miss',    config.etm.missedHeartbeats);
    set('g-etm-boot',    config.etm.bootHeartbeatSec);
    set('g-etm-count',   config.etm.messageCount);
    set('g-etm-delay',   config.etm.messageDelayMs);
    if (appMode === 'advanced') document.getElementById('etm-detail').classList.add('visible');
  }
  const chksmEl = document.getElementById('g-etm-chksm');
  if (chksmEl) chksmEl.checked = config.etm.checksumEnabled ?? true;
}

// ─── General DOM Snapshot ─────────────────────────────────────────
// Capture / restore the shared general-network DOM fields so that a
// boardPull() triggered on connect cannot clobber the wizard-applied
// values before the config push fires.

function captureGeneralDOMSnapshot() {
  const g = (id) => document.getElementById(id)?.value ?? '';
  return {
    password:  g('g-password'),
    mac2:      g('g-mac2'),
    mac3:      g('g-mac3'),
    wcbq:      g('g-wcbq'),
    delimiter: g('g-delimiter'),
    funcchar:  g('g-funcchar'),
    cmdchar:   g('g-cmdchar'),
  };
}

function restoreGeneralDOMSnapshot(snap) {
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
  set('g-password',  snap.password);
  set('g-mac2',      snap.mac2);
  set('g-mac3',      snap.mac3);
  set('g-wcbq',      snap.wcbq);
  set('g-delimiter', snap.delimiter);
  set('g-funcchar',  snap.funcchar);
  set('g-cmdchar',   snap.cmdchar);
  // Keep systemConfig.general in sync (mirrors what syncGeneralFromConfig does)
  onGeneralPasswordChange();
  onGeneralMacChange();
  onGeneralCmdCharChange();
}

// ─── Reboot Detection ─────────────────────────────────────────────
// Returns true if any command in the built string requires a board reboot
// to take effect, based on WCB firmware documentation and source code.
//
// Reboot-required:  HW, WCB/WCBQ, MAC, KYBER, MAP PWM input (not OUT)
// Immediate effect: EPASS, DELIM, FUNCCHAR, CMDCHAR, BAUD, LABEL, BCAST,
//                   MAESTRO, ETM, MAP SERIAL, MAP PWM OUT, SEQ
function commandStringNeedsReboot(cmdString) {
  const u = cmdString.toUpperCase();
  if (u.includes('HW,'))    return true;   // Hardware version — pin map changes
  if (u.includes('WCB,'))   return true;   // Board number or quantity (WCBQ also matches)
  if (u.includes('MAC,'))   return true;   // MAC octets — ESP-NOW identity
  if (u.includes('KYBER,')) return true;   // Kyber mode — serial port reservation
  // PWM INPUT mapping (MAP,PWM,Sx,...) — firmware auto-reboots, but we signal it too
  // PWM OUTPUT declaration (MAP,PWM,OUT,Sx) does NOT need a reboot
  if (/MAP,PWM,S\d/i.test(cmdString)) return true;
  return false;
}

// Returns true if the command string changes any field that defines the ESP-NOW
// network group — MAC octets or the shared password.  Pushing these to a remote
// board breaks the relay↔remote link the moment the board reboots, because the
// relay still has the old values and the MAC-group check in the firmware will
// reject all packets from the now-mismatched board.
function commandStringChangesNetworkGroup(cmdString) {
  const u = cmdString.toUpperCase();
  if (u.includes('MAC,2,') || u.includes('MAC,3,')) return true;
  if (u.includes('EPASS,')) return true;
  return false;
}

// Shows a blocking confirmation modal and returns a Promise that resolves true
// (user confirmed) or false (user cancelled).  Called by boardGoRemote() when
// commandStringChangesNetworkGroup() is true.
function confirmNetworkGroupChange(n, cmdString) {
  return new Promise((resolve) => {
    const u = cmdString.toUpperCase();
    const changes = [];
    if (u.includes('MAC,2,') || u.includes('MAC,3,')) changes.push('MAC octets — ESP-NOW network group address');
    if (u.includes('EPASS,')) changes.push('ESP-NOW password');

    const list = changes.map(c => `<li style="margin-bottom:4px">${c}</li>`).join('');
    document.getElementById('network-group-change-body').innerHTML = `
      <p>This push to <strong>WCB ${n}</strong> (remote) changes:</p>
      <ul style="margin:8px 0 12px 20px">${list}</ul>
      <p>After the board reboots, the relay will no longer share the same network
      group settings — <strong>the remote connection will be lost</strong> until
      every board is updated and rebooted with matching values.</p>
      <p style="margin-top:10px;color:var(--warn)">
        ⚠ Use <strong>Push All</strong> to update every board at the same time
        and avoid losing connectivity mid-push.
      </p>
    `;

    const modal      = document.getElementById('network-group-change-modal');
    const confirmBtn = document.getElementById('network-group-change-confirm');
    const cancelBtn  = document.getElementById('network-group-change-cancel');

    const cleanup = () => {
      modal.classList.remove('open');
      confirmBtn.onclick = null;
      cancelBtn.onclick  = null;
    };

    confirmBtn.onclick = () => { cleanup(); resolve(true);  };
    cancelBtn.onclick  = () => { cleanup(); resolve(false); };

    modal.classList.add('open');
  });
}

// ─── Push All ─────────────────────────────────────────────────────
async function boardGoAll() {
  const directSlots = Object.keys(boardConnections)
    .filter(n => boardConnections[n]?.isConnected())
    .map(Number);
  // Remote boards that aren't also directly connected
  const remoteSlots = Object.keys(remoteRelayForBoard)
    .map(Number)
    .filter(n => !boardConnections[n]?.isConnected());

  const total = directSlots.length + remoteSlots.length;
  if (total === 0) {
    showToast('No boards connected', 'error');
    return;
  }

  // Push All uses a four-stage approach so that every board that needs a
  // reboot (MAC, HW, WCB#, KYBER, PWM map) actually gets one, in the right order:
  //
  //  1. Push configs to remote boards — reboot is embedded atomically in the same
  //     MGMT session payload (see boardGoRemote).  This avoids the MAC-filter race:
  //     after a ?MAC change the target immediately starts rejecting ESP-NOW packets
  //     with the relay's old src_addr, so a separate MGMT reboot would never arrive.
  //  2. Push configs to non-relay direct boards — their reboots don't affect anyone else.
  //  3. Push configs to relay boards — skipReboot so the relay stays alive for step 4.
  //  4. Reboot relay boards last — relay goes down after all remotes are done.
  //
  // This guarantees the relay never reboots before its remote boards have received
  // and executed both their config and their reboot command.
  const relayBoardSet   = new Set(Object.values(remoteRelayForBoard).map(Number));
  const directRelays    = directSlots.filter(n =>  relayBoardSet.has(n));
  const directNonRelays = directSlots.filter(n => !relayBoardSet.has(n));

  showToast(`Pushing to ${total} board${total > 1 ? 's' : ''}…`, 'info', 4000);

  const relayRebootList = [];   // relay board numbers that need a reboot

  // ── Stage 1: remote configs + embedded reboot (atomic, no race) ──────────
  // skipConfirm: true — boardGoAll skips per-board network-group modals since
  // we're updating every board at the same time (the whole point of Push All).
  for (const n of remoteSlots) {
    await boardGoRemote(n, { skipConfirm: true });
  }

  // ── Stage 2: non-relay direct boards (handle their own reboots normally) ──
  for (const n of directNonRelays) await boardGo(n);

  // ── Stage 3: relay configs (no reboot yet — relay must stay alive) ────────
  for (const n of directRelays) {
    const needsReboot = await boardGo(n, { skipReboot: true });
    if (needsReboot) relayRebootList.push(n);
  }

  // ── Stage 4: reboot relay boards last ────────────────────────────────────
  for (const n of relayRebootList) {
    const conn = boardConnections[n];
    if (!conn?.isConnected()) continue;
    await sleep(300);
    await conn.send('?reboot\r');
    termLog(n, '?reboot', 'in');
    showToast(`WCB ${n} rebooting…`, 'success');
    updateBoardStatusBadge(n, 'configured');
    await conn.closeForReconnect();
    updateConnectionUI(n, false);
    termLog(n, 'Board disconnected — attempting reconnect…', 'sys');
    (async () => {
      const reconnected = await conn.reconnect(10, 1500);
      if (reconnected) {
        updateConnectionUI(n, true);
        termLog(n, 'Reconnected after reboot', 'sys');
        showToast(`WCB ${n} reconnected`, 'success');
        setTimeout(() => boardPull(n), 3000);
      } else {
        termLog(n, 'Could not auto-reconnect — reconnect manually', 'err');
        showToast(`WCB ${n} did not come back — reconnect manually`, 'error');
      }
    })();
  }
}

// ─── File Import ──────────────────────────────────────────────────

function loadFileFromInput(event) {
  const file = event.target.files[0];
  if (file) readFile(file);
  event.target.value = '';
}

function readFile(file) {
  const reader = new FileReader();
  reader.onload = e => loadSystemFileContent(e.target.result);
  reader.readAsText(file);
}

function loadSystemFileContent(content) {
  try {
    const system = WCBParser.parseSystemFile(content);
    systemConfig = system;

    // File load establishes a new general baseline
    generalBaseline = {
      sourceBoard: 'file',
      fields: extractGeneralFields({
        espnowPassword: system.general.espnowPassword,
        macOctet2:      system.general.macOctet2,
        macOctet3:      system.general.macOctet3,
        delimiter:      system.general.delimiter,
        funcChar:       system.general.funcChar,
        cmdChar:        system.general.cmdChar,
      }),
    };

    syncGeneralFromConfig(Object.assign(WCBParser.createDefaultBoardConfig(), {
      wcbQuantity: system.general.wcbQuantity,
      espnowPassword: system.general.espnowPassword,
      macOctet2: system.general.macOctet2,
      macOctet3: system.general.macOctet3,
      delimiter: system.general.delimiter,
      funcChar:  system.general.funcChar,
      cmdChar:   system.general.cmdChar,
      etm: system.general.etm,
    }));

    renderBoards(system.general.wcbQuantity);
    for (let i = 0; i < system.boards.length; i++) {
      const board = system.boards[i];
      const n = board.wcbNumber || (i + 1);
      boardConfigs[n] = board;
      populateUIFromConfig(n, board);
      updateBoardStatusBadge(n, 'default');
    }

    showToast(`Loaded: ${system.droidName || 'unnamed droid'} — ${system.boards.length} board(s)`, 'success');
  } catch (e) {
    showToast(`Failed to parse file: ${e.message}`, 'error');
    console.error(e);
  }
}

// ─── File Export ──────────────────────────────────────────────────
function exportSystemFile() {
  systemConfig.general.wcbQuantity    = parseInt(document.getElementById('g-wcbq').value) || 1;
  systemConfig.general.espnowPassword = document.getElementById('g-password').value;
  systemConfig.general.macOctet2      = document.getElementById('g-mac2').value?.toUpperCase();
  systemConfig.general.macOctet3      = document.getElementById('g-mac3').value?.toUpperCase();
  systemConfig.general.delimiter      = document.getElementById('g-delimiter').value || '^';
  systemConfig.general.funcChar       = document.getElementById('g-funcchar').value  || '?';
  systemConfig.general.cmdChar        = document.getElementById('g-cmdchar').value   || ';';
  systemConfig.general.etm.enabled         = document.getElementById('g-etm-enabled').checked;
  systemConfig.general.etm.checksumEnabled = document.getElementById('g-etm-chksm')?.checked ?? true;

  systemConfig.boards = [];
  const qty = systemConfig.general.wcbQuantity;
  for (let n = 1; n <= qty; n++) {
    if (boardConfigs[n]) {
      syncSerialUIToConfig(n);
      boardConfigs[n].sequences = getSequencesFromUI(n);
      systemConfig.boards.push(boardConfigs[n]);
    }
  }

  const content  = WCBParser.buildSystemFile(systemConfig);
  const blob     = new Blob([content], { type: 'text/plain' });
  const url      = URL.createObjectURL(blob);
  const a        = document.createElement('a');
  a.href         = url;
  a.download     = `WCB_system_${new Date().toISOString().slice(0,10)}.txt`;
  a.click();
  URL.revokeObjectURL(url);
  showToast('Config file exported', 'success');
}

// ─── Terminal ─────────────────────────────────────────────────────
function initTerminalResize() {
  const handle  = document.getElementById('terminal-resize-handle');
  const drawer  = document.getElementById('terminal-drawer');
  if (!handle || !drawer) return;

  let startY, startH;

  handle.addEventListener('mousedown', e => {
    startY = e.clientY;
    startH = drawer.offsetHeight;
    handle.classList.add('dragging');
    document.addEventListener('mousemove', onDrag);
    document.addEventListener('mouseup', stopDrag);
    e.preventDefault();
  });

  function onDrag(e) {
    const delta  = startY - e.clientY; // drag up = increase height
    const newH   = Math.min(Math.max(startH + delta, 120), window.innerHeight * 0.8);
    drawer.style.height = newH + 'px';
    syncMainPadding(); // keep page content above the terminal while resizing
  }

  function stopDrag() {
    handle.classList.remove('dragging');
    document.removeEventListener('mousemove', onDrag);
    document.removeEventListener('mouseup', stopDrag);
    syncMainPadding(); // final sync after drag ends
  }
}

function syncMainPadding() {
  const drawer = document.getElementById('terminal-drawer');
  const main   = document.querySelector('.main');
  if (!drawer || !main) return;
  // When the terminal is open, push the page content above it so nothing is hidden behind it.
  // When closed, clear the inline style so the CSS default (120 px) takes over.
  main.style.paddingBottom = drawer.classList.contains('open')
    ? (drawer.offsetHeight + 24) + 'px'
    : '';
}

function toggleTerminal() {
  document.getElementById('terminal-drawer').classList.toggle('open');
  syncMainPadding();
}

// ─── Multi-pane Terminal ──────────────────────────────────────────

// Per-board terminal state (debug flags reset on reboot; timestamp/scroll are UI-only)
const boardDebugStates = {};
const boardTimestamp   = {};   // false = off (default)
const boardAutoScroll  = {};   // true  = on  (default)

// Per-board command history (persists for the session; index -1 = typing new command)
const boardCmdHistory  = {};   // { [n]: string[] }  oldest → newest
const boardHistoryIdx  = {};   // { [n]: number }    current recall position

const DEBUG_MODES = [
  { key: 'main',  label: 'COMMANDS', cmd: 'DEBUG'       },
  { key: 'kyber', label: 'KYBER',    cmd: 'DEBUG,KYBER' },
  { key: 'pwm',   label: 'PWM',      cmd: 'DEBUG,PWM'   },
  { key: 'etm',   label: 'ETM',      cmd: 'DEBUG,ETM'   },
  { key: 'mgmt',  label: 'MGMT',     cmd: 'DEBUG,MGMT'  },
];

function ensureDebugState(n) {
  if (!boardDebugStates[n])
    boardDebugStates[n] = { main: false, kyber: false, pwm: false, etm: false, mgmt: false };
  return boardDebugStates[n];
}

// Master refresh — call whenever connection state or toggle state changes
function updateTerminalControls(n) {
  const state     = boardDebugStates[n] ?? {};
  // Board is reachable if directly connected OR accessible via a relay
  const connected = boardConnections[n]?.isConnected() ?? false;
  const reachable = connected || (remoteRelayForBoard[n] !== undefined &&
                    boardConnections[remoteRelayForBoard[n]]?.isConnected());

  // Debug buttons
  for (const { key } of DEBUG_MODES) {
    const btn = document.getElementById(`dbg-btn-${n}-${key}`);
    if (!btn) continue;
    btn.disabled = !reachable;
    btn.classList.toggle('debug-on', !!state[key]);
  }

  // Timestamp button (always enabled — it's a display preference, not board-dependent)
  const tsBtn = document.getElementById(`term-ts-btn-${n}`);
  if (tsBtn) tsBtn.classList.toggle('debug-on', !!boardTimestamp[n]);

  // Auto-scroll button
  const scrollBtn = document.getElementById(`term-scroll-btn-${n}`);
  if (scrollBtn) {
    const on = boardAutoScroll[n] !== false;
    scrollBtn.classList.toggle('debug-on',       on);
    scrollBtn.classList.toggle('scroll-paused', !on);
    scrollBtn.title = on ? 'Auto-scroll ON — click to pause' : 'Auto-scroll PAUSED — click to resume';
  }
}

async function toggleDebug(n, modeKey) {
  const state    = ensureDebugState(n);
  state[modeKey] = !state[modeKey];
  const onOff    = state[modeKey] ? 'ON' : 'OFF';

  const funcChar = boardConfigs[n]?.funcChar ?? '?';
  const modeInfo = DEBUG_MODES.find(m => m.key === modeKey);
  if (!modeInfo) return;

  const cmd = `${funcChar}${modeInfo.cmd},${onOff}`;

  // Remote board — forward through the relay via MGMT
  const relayN = remoteRelayForBoard[n];
  if (relayN) {
    const relayConn = boardConnections[relayN];
    if (!relayConn?.isConnected()) { state[modeKey] = !state[modeKey]; return; } // revert
    termLog(n, cmd, 'in');
    try {
      const sessionId = Math.floor(Math.random() * 0xFFFF).toString(16).toUpperCase().padStart(4, '0');
      await relayConn.send(`?MGMT,FRAG,${n},${sessionId},0,1,${cmd}\r`);
    } catch (_) { state[modeKey] = !state[modeKey]; } // revert on error
    updateTerminalControls(n);
    return;
  }

  // Direct board
  const conn = boardConnections[n];
  if (!conn?.isConnected()) { state[modeKey] = !state[modeKey]; return; } // revert
  await conn.send(cmd + '\r');
  termLog(n, cmd, 'in');
  updateTerminalControls(n);
}

function toggleTimestamp(n) {
  boardTimestamp[n] = !boardTimestamp[n];
  updateTerminalControls(n);
}

function toggleAutoScroll(n) {
  boardAutoScroll[n] = boardAutoScroll[n] === false; // toggle (default is true)
  // If re-enabling, jump to bottom immediately
  if (boardAutoScroll[n] !== false) {
    const out = document.getElementById(`term-pane-output-${n}`);
    if (out) out.scrollTop = out.scrollHeight;
  }
  updateTerminalControls(n);
}

// Update the chip label to reflect how the board is connected
function updatePaneVisibilityChip(n) {
  const label = document.getElementById(`term-vis-label-${n}`);
  if (!label) return;
  const relayN = remoteRelayForBoard[n];
  label.textContent = relayN ? `WCB ${n} (via WCB ${relayN})` : `WCB ${n} (USB)`;
}

function togglePaneVisibility(n) {
  const cb   = document.getElementById(`term-vis-cb-${n}`);
  const pane = document.getElementById(`term-pane-${n}`);
  if (!pane) return;
  pane.style.display = cb?.checked ? '' : 'none';
}

function ensureTerminalPane(n) {
  if (document.getElementById(`term-pane-${n}`)) return; // already exists

  // Hide the empty-state placeholder
  const noBoards = document.getElementById('terminal-no-boards');
  if (noBoards) noBoards.style.display = 'none';

  // Add a visibility toggle chip to the terminal header
  const toggleBar = document.getElementById('terminal-visibility-toggles');
  if (toggleBar && !document.getElementById(`term-vis-cb-${n}`)) {
    const chip = document.createElement('label');
    chip.className = 'term-vis-chip';
    chip.id = `term-vis-chip-${n}`;
    chip.innerHTML =
      `<input type="checkbox" id="term-vis-cb-${n}" checked onchange="togglePaneVisibility(${n})">` +
      `<span id="term-vis-label-${n}">WCB ${n}</span>`;
    toggleBar.appendChild(chip);
  }
  updatePaneVisibilityChip(n);

  const debugBtns = DEBUG_MODES.map(({ key, label }) =>
    `<button class="btn btn-ghost btn-sm debug-btn" id="dbg-btn-${n}-${key}"
             onclick="toggleDebug(${n},'${key}')" disabled title="${label} debug">${label}</button>`
  ).join('');

  const pane = document.createElement('div');
  pane.className = 'terminal-pane';
  pane.id = `term-pane-${n}`;
  pane.innerHTML = `
    <div class="terminal-pane-header" id="term-pane-header-${n}">
      <span class="term-status-dot" id="term-dot-${n}"></span>
      <span class="term-pane-label">WCB ${n}</span>
      <span class="term-btn-divider"></span>
      <span class="debug-bar-label">DEBUG:</span>
      ${debugBtns}
      <span class="term-btn-divider"></span>
      <button class="btn btn-ghost btn-sm debug-btn debug-on" id="term-scroll-btn-${n}"
              onclick="toggleAutoScroll(${n})" title="Auto-scroll ON — click to pause">⇩</button>
      <button class="btn btn-ghost btn-sm debug-btn" id="term-ts-btn-${n}"
              onclick="toggleTimestamp(${n})" title="Toggle timestamps">⏱</button>
      <span class="term-btn-divider"></span>
      <button class="btn btn-ghost btn-sm debug-btn"
              onclick="clearTerminalPane(${n})" title="Clear output">✕</button>
    </div>
    <div class="terminal-pane-output" id="term-pane-output-${n}"></div>
    <div class="terminal-input-row">
      <input class="terminal-input" id="term-pane-input-${n}" type="text"
        placeholder="Send to WCB ${n}…"
        onkeydown="onTerminalKeydown(event,${n})"
        spellcheck="false" autocomplete="off">
      <button class="btn btn-ghost btn-sm" onclick="sendTerminalCommandTo(${n})">Send ↵</button>
    </div>`;
  document.getElementById('terminal-panes').appendChild(pane);

  // Auto-scroll intelligence: pause when user scrolls up, resume when at bottom
  const output = pane.querySelector('.terminal-pane-output');
  output.addEventListener('scroll', () => {
    const atBottom = output.scrollTop + output.clientHeight >= output.scrollHeight - 10;
    const wasOn    = boardAutoScroll[n] !== false;
    if (atBottom !== wasOn) {
      boardAutoScroll[n] = atBottom;
      updateTerminalControls(n);
    }
  });
}

function updateTerminalPaneDot(n, connected) {
  const dot    = document.getElementById(`term-dot-${n}`);
  const header = document.getElementById(`term-pane-header-${n}`);
  const input  = document.getElementById(`term-pane-input-${n}`);
  if (dot)    dot.classList.toggle('connected', connected);
  if (header) header.classList.toggle('disconnected', !connected);
  if (input) {
    input.disabled    = !connected;
    input.placeholder = connected ? `Send to WCB ${n}…` : 'Board disconnected';
  }
  // Reset debug flags on disconnect — board loses them on every reboot
  if (!connected) boardDebugStates[n] = { main: false, kyber: false, pwm: false, etm: false };
  // Timestamp and auto-scroll are UI preferences — keep them across reconnects
  updateTerminalControls(n);
}

function clearTerminalPane(n) {
  const out = document.getElementById(`term-pane-output-${n}`);
  if (out) out.innerHTML = '';
}

function clearAllTerminals() {
  document.querySelectorAll('[id^="term-pane-output-"]').forEach(el => el.innerHTML = '');
}

function termLog(boardIndex, text, type = 'out') {
  const addLine = (output, idx) => {
    const line = document.createElement('div');
    line.className = `terminal-line-${type}`;
    // Prepend timestamp if enabled for this board
    if (boardTimestamp[idx]) {
      const now = new Date();
      const ts  = now.toTimeString().slice(0, 8); // HH:MM:SS
      line.textContent = `[${ts}] ${text}`;
    } else {
      line.textContent = text;
    }
    output.appendChild(line);
    // Auto-scroll unless the user has scrolled up to review history
    if (boardAutoScroll[idx] !== false) output.scrollTop = output.scrollHeight;
  };

  // If a specific pane exists for this board, use it; otherwise create one on the fly
  if (boardIndex > 0) {
    ensureTerminalPane(boardIndex);
    const output = document.getElementById(`term-pane-output-${boardIndex}`);
    if (output) { addLine(output, boardIndex); return; }
  }
  // boardIndex 0 (system) → log to first available pane, or ignore
  const anyOutput = document.querySelector('[id^="term-pane-output-"]');
  if (anyOutput) {
    const idx = parseInt(anyOutput.id.replace('term-pane-output-', '')) || 0;
    addLine(anyOutput, idx);
  }
}

function onTerminalKeydown(e, n) {
  if (e.key === 'Enter') {
    sendTerminalCommandTo(n);
    return;
  }

  if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
    const history = boardCmdHistory[n];
    if (!history?.length) return;
    e.preventDefault();   // don't move the text-cursor

    let idx = boardHistoryIdx[n] ?? -1;

    if (e.key === 'ArrowUp') {
      idx = Math.min(idx + 1, history.length - 1);
    } else {
      idx = Math.max(idx - 1, -1);
    }
    boardHistoryIdx[n] = idx;

    const input = document.getElementById(`term-pane-input-${n}`);
    if (!input) return;
    input.value = idx === -1 ? '' : history[history.length - 1 - idx];
    // Move cursor to end of recalled text
    input.setSelectionRange(input.value.length, input.value.length);
  }
}

async function sendTerminalCommandTo(n) {
  const input = document.getElementById(`term-pane-input-${n}`);
  const cmd   = input?.value?.trim();
  if (!cmd) return;
  input.value = '';

  // Push to per-board history (skip duplicates of the last entry)
  if (!boardCmdHistory[n]) boardCmdHistory[n] = [];
  const hist = boardCmdHistory[n];
  if (hist[hist.length - 1] !== cmd) hist.push(cmd);
  boardHistoryIdx[n] = -1;   // reset to "typing new command"

  // Remote board — forward through the relay via MGMT (single-chunk push)
  const relayN = remoteRelayForBoard[n];
  if (relayN) {
    const relayConn = boardConnections[relayN];
    if (!relayConn?.isConnected()) { termLog(n, `Relay WCB${relayN} not connected`, 'err'); return; }
    termLog(n, cmd, 'in');
    try {
      const sessionId = Math.floor(Math.random() * 0xFFFF).toString(16).toUpperCase().padStart(4, '0');
      await relayConn.send(`?MGMT,FRAG,${n},${sessionId},0,1,${cmd}\r`);
    } catch (e) { termLog(n, `Send error: ${e.message}`, 'err'); }
    return;
  }

  // Direct board — send straight over the USB serial connection
  const conn = boardConnections[n];
  if (!conn?.isConnected()) { termLog(n, 'Board not connected', 'err'); return; }

  termLog(n, cmd, 'in');
  try { await conn.send(cmd + '\r'); }
  catch (e) { termLog(n, `Send error: ${e.message}`, 'err'); }
}


// ─── Kyber Board Helpers ──────────────────────────────────────────
function findKyberLocalBoard() {
  for (const [bn, config] of Object.entries(boardConfigs)) {
    if (config?.kyber?.mode === 'local') return parseInt(bn);
  }
  return null;
}

let _kyberWarnedBoards = new Set();
function showKyberRepushWarning(kyberBoardNum) {
  if (_kyberWarnedBoards.has(kyberBoardNum)) return;
  _kyberWarnedBoards.add(kyberBoardNum);
  showToast(
    `WCB ${kyberBoardNum} has Kyber Local — re-push WCB ${kyberBoardNum} after finishing all board configurations`,
    'info',
    7000
  );
  setTimeout(() => _kyberWarnedBoards.delete(kyberBoardNum), 30000);
}

// ─── Kyber Targets ────────────────────────────────────────────────
function addKyberTargetRow(n) {
  appendKyberTargetRow(n, { id: 1, wcb: n, port: 1, baud: 57600 });
  syncKyberTargetsToConfig(n);
  onBoardFieldChange(n);
}

function appendKyberTargetRow(n, target, readOnly = false) {
  const tbody = document.getElementById(`b${n}-kyber-target-tbody`);
  if (!tbody) return;

  const rowNum   = tbody.rows.length + 1;
  const rowId    = `kyber-target-row-${n}-${Date.now()}`;
  const dis      = readOnly ? 'disabled' : '';
  const dimStyle = readOnly ? 'style="opacity:0.5"' : '';

  const idOptions = Array.from({length: 9}, (_, i) => i + 1).map(v =>
    `<option value="${v}" ${v === target.id ? 'selected' : ''}>${v}</option>`
  ).join('');
  const wcbOptions = Array.from({length: 9}, (_, i) => i + 1).map(v =>
    `<option value="${v}" ${v === target.wcb ? 'selected' : ''}>WCB ${v}</option>`
  ).join('');

  const kyberPort = boardConfigs[n]?.kyber?.port;
  const portOptions = [1,2,3,4,5]
    .filter(v => v !== kyberPort)
    .map(v => `<option value="${v}" ${v === target.port ? 'selected' : ''}>S${v}</option>`)
    .join('');

  const baudOptions = BAUD_RATES.map(b =>
    `<option value="${b}" ${b === target.baud ? 'selected' : ''}>${b.toLocaleString()}</option>`
  ).join('');

  const deleteBtn = readOnly
    ? `<span title="From board backup" style="opacity:0.4;font-size:18px;padding:0 8px">&#128274;</span>`
    : `<button class="btn btn-danger btn-sm btn-icon" onclick="removeKyberTargetRow(${n},'${rowId}')">&#128465;</button>`;

  const tr = document.createElement('tr');
  tr.id = rowId;
  tr.setAttribute('data-readonly', readOnly ? '1' : '0');
  tr.innerHTML = `
    <td style="color:var(--text3)" ${dimStyle}>${rowNum}</td>
    <td ${dimStyle}><select id="${rowId}-id" ${dis} onchange="onKyberTargetsChange(${n})">${idOptions}</select></td>
    <td ${dimStyle}><select id="${rowId}-wcb" ${dis} onchange="onKyberTargetsChange(${n})">${wcbOptions}</select></td>
    <td ${dimStyle}><select id="${rowId}-port" ${dis} onchange="onKyberTargetsChange(${n})">${portOptions}</select></td>
    <td ${dimStyle}><select id="${rowId}-baud" ${dis} onchange="onKyberTargetsChange(${n})">${baudOptions}</select></td>
    <td>${deleteBtn}</td>
  `;
  tbody.appendChild(tr);
}

function removeKyberTargetRow(n, rowId) {
  document.getElementById(rowId)?.remove();
  const tbody = document.getElementById(`b${n}-kyber-target-tbody`);
  tbody?.querySelectorAll('tr').forEach((row, i) => {
    row.cells[0].textContent = i + 1;
  });
  syncKyberTargetsToConfig(n);
  onBoardFieldChange(n);
}

function onKyberTargetsChange(n) {
  syncKyberTargetsToConfig(n);
  onBoardFieldChange(n);
}

function syncKyberTargetsToConfig(n) {
  const config = boardConfigs[n];
  if (!config) return;
  if (!config.kyber) config.kyber = {};
  config.kyber.targets = [];
  const tbody = document.getElementById(`b${n}-kyber-target-tbody`);
  if (!tbody) return;
  tbody.querySelectorAll('tr').forEach(row => {
    const id   = parseInt(row.querySelector('[id$="-id"]')?.value);
    const wcb  = parseInt(row.querySelector('[id$="-wcb"]')?.value);
    const port = parseInt(row.querySelector('[id$="-port"]')?.value);
    const baud = parseInt(row.querySelector('[id$="-baud"]')?.value) || 57600;
    if (id && wcb && port) config.kyber.targets.push({ id, wcb, port, baud });
  });
}

function populateKyberTargetsFromConfig(n, _config) {
  // The Kyber targets info div is auto-computed from all boards' Maestros.
  // _config is unused — we always scan boardConfigs[] for live accuracy.
  const info = document.getElementById(`b${n}-kyber-targets-info`);
  if (!info) return;
  const parts = [];
  for (let b = 1; b <= 9; b++) {
    const bc = boardConfigs[b];
    if (!bc?.maestros?.length) continue;
    const wcbNum = bc.wcbNumber || b;
    for (const m of bc.maestros) {
      parts.push(`M${m.id} · WCB${wcbNum} S${m.port} · ${m.baud.toLocaleString()} baud`);
    }
  }
  info.innerHTML = parts.length > 0
    ? '<strong>Will target:</strong> ' + parts.join(' &nbsp;&nbsp;|&nbsp;&nbsp; ')
    : 'No Maestros configured yet — add Maestros to each board first, then push.';
}

// Scan all boardConfigs to build Kyber LOCAL targets automatically so the user
// never has to maintain a separate targets table.
function autoComputeKyberTargets(n) {
  const config = boardConfigs[n];
  if (!config || config.kyber.mode !== 'local') return;

  // Preserve the existing target list (populated from the last ?backup) so that
  // remote maestros on boards that haven't connected yet are still included.
  // Connected boards always win — their live boardConfigs[b].maestros take priority.
  const prevTargets = config.kyber.targets ?? [];

  config.kyber.targets = [];
  const foundIds = new Set();

  for (let b = 1; b <= 9; b++) {
    const bc = boardConfigs[b];
    if (!bc?.maestros?.length) continue;
    const wcbNum = bc.wcbNumber || b;
    for (const m of bc.maestros) {
      config.kyber.targets.push({ id: m.id, wcb: wcbNum, port: m.port, baud: m.baud });
      foundIds.add(m.id);
    }
  }

  // Fall back to previously-known targets for maestros whose board isn't connected yet.
  for (const t of prevTargets) {
    if (!foundIds.has(t.id)) {
      config.kyber.targets.push(t);
    }
  }
}


// ─── Flash Progress UI ────────────────────────────────────────────
function setFlashUI(n, visible) {
  const wrap = document.getElementById(`b${n}-flash-wrap`);
  if (!wrap) return;
  wrap.style.display = visible ? '' : 'none';
  if (visible) {
    const bar = document.getElementById(`b${n}-flash-bar`);
    if (bar) bar.style.width = '0%';
    const pct = document.getElementById(`b${n}-flash-pct`);
    if (pct) pct.textContent = '';
  }
}

function setFlashStatus(n, msg) {
  const el = document.getElementById(`b${n}-flash-status`);
  if (el) el.textContent = msg;
}

function updateFlashBar(n, written, total) {
  const pct = total > 0 ? Math.round(written / total * 100) : 0;
  const bar = document.getElementById(`b${n}-flash-bar`);
  if (bar) bar.style.width = `${pct}%`;
  const pctEl = document.getElementById(`b${n}-flash-pct`);
  if (pctEl) pctEl.textContent = total > 0 ? `${pct}%` : '';
  setFlashStatus(n, `Flashing… ${pct}%`);
}

// ─── Identify ─────────────────────────────────────────────────────
async function boardIdentify(n) {
  // Remote board — send ?IDENTIFY via the relay's MGMT channel
  if (remoteRelayForBoard[n]) {
    const relayN    = remoteRelayForBoard[n];
    const relayConn = boardConnections[relayN];
    if (!relayConn?.isConnected()) { showToast(`WCB ${relayN} (relay) not connected`, 'error'); return; }
    try {
      const sessionId = Math.floor(Math.random() * 0xFFFF).toString(16).toUpperCase().padStart(4, '0');
      const cmd = `?MGMT,FRAG,${n},${sessionId},0,1,?IDENTIFY`;
      termLog(relayN, cmd, 'in');
      await relayConn.send(cmd + '\r');
      showToast(`WCB ${n} identifying — watch the LED`, 'info', 5500);
    } catch (e) {
      showToast(`Identify failed: ${e.message}`, 'error');
    }
    return;
  }

  // Direct board — send straight over the serial connection
  const conn = boardConnections[n];
  if (!conn?.isConnected()) { showToast('Board not connected', 'error'); return; }
  try {
    await conn.send('?IDENTIFY\r');
    termLog(n, '?IDENTIFY', 'in');
    showToast(`WCB ${n} identifying — watch the LED`, 'info', 5500);
  } catch (e) {
    showToast(`Identify failed: ${e.message}`, 'error');
  }
}

// ─── Factory Reset ────────────────────────────────────────────────
let _factoryResetSlot = null;   // board slot the factory-reset modal is open for

function boardFactoryReset(n) {
  const conn = boardConnections[n];
  if (!conn?.isConnected()) { showToast('Board not connected', 'error'); return; }

  _factoryResetSlot = n;
  document.getElementById('factory-reset-modal-title').textContent = `🏭 Factory Reset WCB ${n}`;
  document.getElementById('factory-reset-modal').classList.add('open');
}

function closeFactoryResetModal(event) {
  if (event && event.target !== document.getElementById('factory-reset-modal')) return;
  document.getElementById('factory-reset-modal').classList.remove('open');
  _factoryResetSlot = null;
}

function doFactoryResetWithFw() {
  const n = _factoryResetSlot;
  document.getElementById('factory-reset-modal').classList.remove('open');
  _factoryResetSlot = null;
  if (!n) return;
  // Delegate to boardGo with factory mode — handles erase + flash + reconnect + push prompt
  boardGo(n, { mode: 'factory' });
}

async function doFactoryResetEraseOnly() {
  const n = _factoryResetSlot;
  document.getElementById('factory-reset-modal').classList.remove('open');
  _factoryResetSlot = null;
  if (!n) return;

  const conn = boardConnections[n];
  if (!conn?.isConnected()) { showToast('Board not connected', 'error'); return; }

  // ── Erase-only path ──────────────────────────────────────────────
  termLog(n, '?ERASE,NVS', 'in');
  try {
    await conn.send('?ERASE,NVS\r');
    // Firmware counts down ~3 s before erasing and rebooting.
    // Closing the serial port immediately causes a USB-disconnect reset that
    // fires BEFORE the erase runs — so we wait 4 s to let the firmware finish.
    termLog(n, 'Waiting for firmware erase countdown…', 'sys');
    showToast(`WCB ${n} erasing — do not disconnect…`, 'warning', 5000);
    await sleep(4000);
    termLog(n, 'NVS erased — board rebooting…', 'sys');
    updateBoardStatusBadge(n, 'default');

    await conn.closeForReconnect();
    updateConnectionUI(n, false);
    termLog(n, 'Board disconnected — attempting reconnect…', 'sys');

    (async () => {
      const reconnected = await conn.reconnect(10, 1500);
      if (reconnected) {
        updateConnectionUI(n, true);
        termLog(n, 'Reconnected after NVS erase', 'sys');
        showToast(`WCB ${n} reconnected`, 'success');
        termLog(n, 'Auto-pulling config…', 'sys');
        setTimeout(() => boardPull(n), 3000);
      } else {
        termLog(n, 'Could not auto-reconnect — reconnect manually', 'err');
        showToast(`WCB ${n} did not come back — reconnect manually`, 'error');
      }
    })();
  } catch (e) {
    showToast(`Erase failed: ${e.message}`, 'error');
  }
}

// ─── Toast Notifications ──────────────────────────────────────────
function showToast(message, type = 'info', duration = 3500) {
  const container = document.getElementById('toast-container');
  const toast     = document.createElement('div');
  toast.className = `toast ${type}`;
  const icon = type === 'success' ? '✅' : type === 'error' ? '❌' : 'ℹ';

  // Error toasts stay longer; all toasts get copy + dismiss buttons
  const effectiveDuration = type === 'error' ? Math.max(duration, 12000) : duration;

  const copySvg = `<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="5" y="5" width="9" height="9" rx="1.5"/><path d="M11 5V3.5A1.5 1.5 0 0 0 9.5 2H3.5A1.5 1.5 0 0 0 2 3.5v6A1.5 1.5 0 0 0 3.5 11H5"/></svg>`;

  toast.innerHTML = `
    <span>${icon}</span>
    <span class="toast-msg">${escHtml(message)}</span>
    <button class="toast-btn toast-copy" title="Copy message">${copySvg}</button>
    <button class="toast-btn toast-dismiss" title="Dismiss">✕</button>`;

  toast.querySelector('.toast-copy').addEventListener('click', () => {
    navigator.clipboard.writeText(message).catch(() => {});
    const btn = toast.querySelector('.toast-copy');
    btn.textContent = '✓';
    btn.style.color = 'var(--green2)';
    setTimeout(() => { btn.innerHTML = copySvg; btn.style.color = ''; }, 1500);
  });
  toast.querySelector('.toast-dismiss').addEventListener('click', () => toast.remove());

  container.appendChild(toast);
  setTimeout(() => toast.remove(), effectiveDuration);
  return toast;
}

// Shows a persistent action prompt after a flash/factory reset reconnect.
// Gives the user the chance to verify the config before pushing instead
// of auto-pushing with potentially stale DOM state.
function showPostFlashPrompt(n, resetLabel) {
  // Remove any previous prompt for this board
  document.getElementById(`post-flash-prompt-${n}`)?.remove();

  const container = document.getElementById('toast-container');
  const toast     = document.createElement('div');
  toast.id        = `post-flash-prompt-${n}`;
  toast.className = 'toast info';
  toast.innerHTML = `
    <span>⚡</span>
    <span class="toast-msg">WCB ${n} ${resetLabel} &amp; reconnected — push config to finish setup</span>
    <button class="toast-btn" id="post-flash-push-${n}"
            style="background:var(--accent);color:#fff;border:none;border-radius:4px;
                   padding:2px 10px;cursor:pointer;font-size:11px;white-space:nowrap">
      Push Config
    </button>
    <button class="toast-btn toast-dismiss" title="Dismiss">✕</button>`;

  toast.querySelector(`#post-flash-push-${n}`).addEventListener('click', () => {
    toast.remove();
    boardGo(n);
  });
  toast.querySelector('.toast-dismiss').addEventListener('click', () => {
    toast.remove();
    delete postFlashGeneralSnapshot[n];   // discard snapshot if user dismisses
  });

  container.appendChild(toast);
  // No auto-dismiss — stays until the user pushes or dismisses
}

// ─── Setup Wizard ─────────────────────────────────────────────────

// ── State ──────────────────────────────────────────────────────────
let wizardState = null;

function wizardDefaultState() {
  return {
    currentIdx:    0,
    steps:         [],
    quantity:      1,
    password:      '',
    mac2:          '00',
    mac3:          '00',
    delimiter:     '^',
    funcChar:      '?',
    cmdChar:       ';',
    boards:        [wizardDefaultBoard(1)],
    kyberEnabled:        false,
    kyberBoard:          1,
    kyberPort:           2,
    kyberBaud:           115200,
    kyberMarcduinoPort:  3,
    kyberTargets:        [],
    maestroEnabled: false,
    maestros:      [],            // [{ boardSlot, id, port, baud }]
    etmEnabled:    true,
    etmConfig:     { timeoutMs:30000, heartbeatSec:10, missedHeartbeats:3,
                     bootHeartbeatSec:30, messageCount:20, messageDelayMs:100,
                     checksumEnabled:true },
    needsFirmware: false,
    eraseNvs:      false,
    activeBoardTab: 0,
    connectMode:   null,   // null = not chosen yet, 'all', 'seq'
    connectSeqN:   1,      // current board slot in sequential connect mode
  };
}

function wizardDefaultBoard(slotIndex) {
  return {
    wcbNumber:   slotIndex,
    hwVersion:   0,
    serialPorts: Array.from({length: 5}, () => ({ baud: 9600, label: '' })),
  };
}

function wizardInitBoards(qty) {
  const prev = wizardState.boards;
  wizardState.boards = Array.from({length: qty}, (_, i) =>
    prev[i] ?? wizardDefaultBoard(i + 1)
  );
  if (wizardState.kyberBoard > qty) wizardState.kyberBoard = 1;
}

// ── Step list ──────────────────────────────────────────────────────
function buildWizardSteps() {
  // Kyber and Maestro come before Serial so claimed ports are visible when labelling
  const steps = ['welcome','quantity','network','identity','kyber'];
  if (wizardState.kyberEnabled) steps.push('kyber-config');
  steps.push('maestro');
  if (wizardState.maestroEnabled) steps.push('maestro-config');
  steps.push('serial','etm','review','firmware','connect');
  return steps;
}

// ── Open / Close ───────────────────────────────────────────────────
function openWizard() {
  _wizardOpen = true;
  wizardState = wizardDefaultState();
  wizardState.password = wizardGenPassword();
  wizardState.mac2     = wizardGenMacOctet();
  wizardState.mac3     = wizardGenMacOctet();
  wizardState.steps    = buildWizardSteps();
  document.getElementById('wizard-modal').classList.add('open');
  wizardRenderStep();
}

function closeWizard(event) {
  if (event && event.target !== document.getElementById('wizard-modal')) return;
  _wizardOpen = false;
  document.getElementById('wizard-modal').classList.remove('open');
}

// ── Navigation ─────────────────────────────────────────────────────
function wizardNext() {
  const key = wizardState.steps[wizardState.currentIdx];

  // Validate before saving
  const err = wizardValidateStep(key);
  if (err) { showToast(err, 'error'); return; }

  wizardSaveStep(key);

  // Rebuild steps in case yes/no answers changed
  const wasKyber   = wizardState.kyberEnabled;
  const wasMaestro = wizardState.maestroEnabled;
  wizardState.steps = buildWizardSteps();

  // If we're on the last step trigger apply + connect
  if (key === 'review') {
    wizardApplyConfig();
    wizardState.currentIdx++;
    wizardRenderStep();
    return;
  }

  // After firmware choice is made, record the flash mode for each board
  if (key === 'firmware') {
    const modeVal = !wizardState.needsFirmware ? (wizardState.eraseNvs ? 'erase' : 'configure')
                  : wizardState.eraseNvs       ? 'factory'
                  :                              'update';
    wizardState.boards.forEach((b, i) => { boardFlashMode[i + 1] = modeVal; });
  }

  if (wizardState.currentIdx < wizardState.steps.length - 1) {
    wizardState.currentIdx++;
    wizardRenderStep();
  }
}

function wizardBack() {
  if (wizardState.currentIdx > 0) {
    wizardSaveStep(wizardState.steps[wizardState.currentIdx]);
    wizardState.currentIdx--;
    wizardRenderStep();
  }
}

// ── Render shell ───────────────────────────────────────────────────
function wizardRenderStep() {
  const steps = wizardState.steps;
  const idx   = wizardState.currentIdx;
  const key   = steps[idx];

  // Title
  const titles = {
    'welcome':      'Setup Wizard',
    'quantity':     'Step 1 — System Size',
    'network':      'Step 2 — Network',
    'identity':     'Step 3 — Board Identity',
    'serial':       'Step 4 — Serial Ports',
    'kyber':        'Step 5 — Kyber Controller',
    'kyber-config': 'Step 5b — Kyber Setup',
    'maestro':      'Step 6 — Maestro Controller',
    'maestro-config':'Step 6b — Maestro Setup',
    'etm':          'Step 7 — Event Tracking (ETM)',
    'review':       'Review — Almost Done!',
    'firmware':     'Step 8 — Firmware',
    'connect':      'Connect & Push',
  };
  document.getElementById('wizard-modal-title').textContent = titles[key] ?? 'Setup Wizard';

  // Dots
  const dotWrap = document.getElementById('wizard-step-indicator');
  dotWrap.innerHTML = steps.map((s, i) => {
    const cls = i < idx ? 'wizard-dot done' : i === idx ? 'wizard-dot active' : 'wizard-dot';
    return `<span class="${cls}"></span>`;
  }).join('');

  // Nav buttons
  document.getElementById('wizard-back-btn').style.visibility = idx === 0 ? 'hidden' : '';
  const nextBtn = document.getElementById('wizard-next-btn');
  if (key === 'connect') {
    nextBtn.textContent = '✓ Done';
    nextBtn.onclick = () => closeWizard();
  } else if (key === 'review') {
    nextBtn.textContent = 'Apply & Connect →';
    nextBtn.onclick = wizardNext;
  } else {
    nextBtn.textContent = 'Next →';
    nextBtn.onclick = wizardNext;
  }

  // Body
  const body = document.getElementById('wizard-body');
  body.innerHTML = wizardBuildStepHTML(key);

  // Post-render hooks
  if (key === 'connect') wizardStartConnectWatchers();
}

// ── Tooltip helper — renders a ⓘ icon that shows a hint on hover ──
const wizHint = (tip) => `<span class="wiz-hint" data-tip="${tip}">ⓘ</span>`;

// ── Per-step HTML builders ─────────────────────────────────────────
function wizardBuildStepHTML(key) {
  switch (key) {
    case 'welcome':      return wizardHTMLWelcome();
    case 'quantity':     return wizardHTMLQuantity();
    case 'network':      return wizardHTMLNetwork();
    case 'identity':     return wizardHTMLIdentity();
    case 'serial':       wizardState.activeBoardTab = 0; return wizardHTMLSerial();
    case 'kyber':        return wizardHTMLKyber();
    case 'kyber-config': return wizardHTMLKyberConfig();
    case 'maestro':      return wizardHTMLMaestro();
    case 'maestro-config': return wizardHTMLMaestroConfig();
    case 'etm':          return wizardHTMLEtm();
    case 'review':       return wizardHTMLReview();
    case 'firmware':     return wizardHTMLFirmware();
    case 'connect':      return wizardHTMLConnect();
    default: return '';
  }
}

function wizardHTMLWelcome() {
  return `
    <div class="wizard-hero">
      <div class="wizard-hero-icon">📡</div>
      <div class="wizard-hero-title">Welcome to the WCB Setup Wizard</div>
      <div class="wizard-hero-desc">
        This wizard will walk you through configuring your Wireless Communication Board system
        step by step. No technical knowledge required — just answer the questions and we'll
        handle the rest. At the end we'll connect to each board and push the configuration
        automatically.
      </div>
    </div>
    <div class="wizard-info-block">
      <div class="wizard-info-row">
        <span class="wizard-info-icon">ℹ</span>
        <div>
          <strong>What you'll need</strong><br>
          Chrome or Edge browser · About 5 minutes
        </div>
      </div>
      <div class="wizard-info-divider"></div>
      <div class="wizard-info-row">
        <span class="wizard-info-icon">🔌</span>
        <div>
          <strong>Connecting your boards</strong><br>
          <strong>Preferred:</strong> Connect all boards via their own USB cable simultaneously — the wizard will push to each one automatically.<br>
          <strong>Alternatively:</strong> Connect and configure one board at a time, repeating the connect &amp; push step for each board.
        </div>
      </div>
    </div>`;
}

function wizardHTMLQuantity() {
  const q = wizardState.quantity;
  const btns = Array.from({length: 8}, (_, i) => {
    const n = i + 1;
    return `<button class="wizard-qty-btn ${n === q ? 'selected' : ''}"
              onclick="wizardSelectQty(${n})">${n}</button>`;
  }).join('');
  return `
    <div class="wizard-section-title">How many WCBs are in your system?</div>
    <div class="wizard-section-desc">Each board is one WCB unit connected via USB or ESP-NOW.</div>
    <div class="wizard-qty-grid">${btns}</div>`;
}

function wizardHTMLNetwork() {
  const { password, mac2, mac3, delimiter, funcChar, cmdChar } = wizardState;
  return `
    <div class="wizard-section-title">Network Configuration</div>
    <div class="wizard-section-desc">All boards in the same system must share these settings. We've suggested random secure values — feel free to change them.</div>
    <div class="wizard-field-row">
      <label>ESP-NOW Password ${wizHint('Shared password for all boards on this network. Every board must use the same password to communicate with each other.')}</label>
      <input id="wiz-password" type="text" value="${escHtml(password)}" spellcheck="false">
      <button class="wizard-gen-btn" title="Generate random" onclick="wizardFillGen('wiz-password','password')">🎲</button>
    </div>
    <div class="wizard-field-row">
      <label>MAC Octet 2 ${wizHint('Second byte of the custom MAC address. All boards on the same network must share identical octets — this is what separates your network from others nearby. Use 🎲 to randomize if the one presented does not work.')}</label>
      <input id="wiz-mac2" type="text" maxlength="2" value="${escHtml(mac2)}"
             style="text-transform:uppercase;max-width:80px" spellcheck="false">
      <button class="wizard-gen-btn" title="Generate random" onclick="wizardFillGen('wiz-mac2','mac')">🎲</button>
    </div>
    <div class="wizard-field-row">
      <label>MAC Octet 3 ${wizHint('Third byte of the broadcast MAC address. Together with Octet 2, this uniquely identifies your network group. Use 🎲 to randomize if the one presented does not work.')}</label>
      <input id="wiz-mac3" type="text" maxlength="2" value="${escHtml(mac3)}"
             style="text-transform:uppercase;max-width:80px" spellcheck="false">
      <button class="wizard-gen-btn" title="Generate random" onclick="wizardFillGen('wiz-mac3','mac')">🎲</button>
    </div>

    <div class="wizard-section-title" style="margin-top:18px">Command Characters</div>
    <div class="wizard-section-desc">Advanced — only change these if your system requires non-default characters. All boards must share the same values.</div>
    <div class="wizard-field-row">
      <label>Delimiter ${wizHint('Character that separates multiple commands in a single message. Default is ^. All boards must share the same value.')}</label>
      <input id="wiz-delim" type="text" maxlength="1" value="${escHtml(delimiter)}"
             style="max-width:60px;font-family:monospace" spellcheck="false">
      <span class="wizard-hint">default: <code>^</code></span>
    </div>
    <div class="wizard-field-row">
      <label>Function Char ${wizHint('Prefix character that marks a WCB board Configuration command (e.g. ?reboot, ?IDENTIFY). Default is ?. All boards must share the same value.')}</label>
      <input id="wiz-funcchar" type="text" maxlength="1" value="${escHtml(funcChar)}"
             style="max-width:60px;font-family:monospace" spellcheck="false">
      <span class="wizard-hint">default: <code>?</code></span>
    </div>
    <div class="wizard-field-row">
      <label>Command Char ${wizHint('Prefix character for routing commands to connected peripherals (e.g. Kyber, Maestro). Default is ;. All boards must share the same value.')}</label>
      <input id="wiz-cmdchar" type="text" maxlength="1" value="${escHtml(cmdChar)}"
             style="max-width:60px;font-family:monospace" spellcheck="false">
      <span class="wizard-hint">default: <code>;</code></span>
    </div>`;
}

function wizardHTMLIdentity() {
  const tabs   = wizardBoardTabs('identity');
  const panels = wizardState.boards.map((b, i) => {
    const hwOpts = Object.entries(WCBParser.HW_VERSION_MAP).map(([val, info]) =>
      `<option value="${val}" ${b.hwVersion == val ? 'selected' : ''}>${info.display}</option>`
    ).join('');
    return `
      <div class="wizard-tab-panel ${i === wizardState.activeBoardTab ? 'active' : ''}" id="wiz-panel-identity-${i}">
        <div class="wizard-field-row">
          <label>WCB Number ${wizHint('Unique ID for this board (1–8). Each board in your system must have a different number — this is how they identify themselves on the network.')}</label>
          <input id="wiz-b${i}-wcbnum" type="number" min="1" max="99" value="${b.wcbNumber}" style="max-width:80px">
        </div>
        <div class="wizard-field-row">
          <label>Hardware Version ${wizHint('PCB version printed on the board label as VER:X.X — see the photo below. Different versions have different pin assignments and must be set correctly.')}</label>
          <select id="wiz-b${i}-hwver">
            <option value="0">— Select version —</option>
            ${hwOpts}
          </select>
        </div>
      </div>`;
  }).join('');
  // Real board photo — shows the silkscreened label with VER: clearly visible.
  const labelSVG = `
    <div class="wizard-hw-label-hint">
      <div class="wizard-hw-label-caption">The version number is silkscreened directly onto the PCB. Look for <strong>VER:</strong> on the board label:</div>
      <img src="../Images/LabelOnly.jpg" class="wizard-hw-label-svg" alt="WCB PCB label showing version number location">
      <div class="wizard-hw-label-note">Every WCB hardware version has this text printed on the board. The number after <code>VER:</code> is what you need — e.g. <code>VER:2.4</code> → select <strong>2.4</strong>.</div>
    </div>`;

  return `
    <div class="wizard-section-title">Board Identity</div>
    <div class="wizard-section-desc">Assign a unique number and hardware version to each board.</div>
    ${tabs}${panels}
    ${labelSVG}`;
}

function wizardHTMLSerial() {
  const tabs   = wizardBoardTabs('serial');
  const panels = wizardState.boards.map((b, i) => {
    const boardSlot = i + 1;

    // Build a claim map: portNum (1-based) → { owner, baud }
    // Kyber claims its port at 115200; each Maestro claims its port at its configured baud
    const claims = {};
    if (wizardState.kyberEnabled && wizardState.kyberBoard === boardSlot) {
      claims[wizardState.kyberPort] = { owner: 'Kyber Maestro', baud: wizardState.kyberBaud };
      if (wizardState.kyberMarcduinoPort) {
        claims[wizardState.kyberMarcduinoPort] = { owner: 'Kyber Marcduino', baud: 9600 };
      }
    }
    for (const m of wizardState.maestros) {
      if (m.boardSlot === boardSlot) {
        claims[m.port] = { owner: `Maestro ID ${m.id}`, baud: m.baud };
      }
    }

    const rows = b.serialPorts.map((sp, p) => {
      const portNum  = p + 1;
      const claim    = claims[portNum];

      if (claim) {
        // Claimed port — baud locked, label pre-filled (user can still customise it)
        const displayLabel = sp.label || claim.owner;
        return `<tr style="background:var(--card-bg2,rgba(0,0,0,0.08))">
          <td>Serial ${portNum}</td>
          <td>
            <span style="font-family:var(--mono);font-size:12px">${claim.baud.toLocaleString()}</span>
            <span style="font-size:10px;color:var(--text3);margin-left:4px">(locked)</span>
            <input type="hidden" id="wiz-b${i}-s${p}-baud" value="${claim.baud}">
          </td>
          <td style="display:flex;align-items:center;gap:6px">
            <input type="text" id="wiz-b${i}-s${p}-label" value="${escHtml(displayLabel)}"
                   style="min-width:100px" placeholder="label">
            <span style="font-size:10px;color:var(--text3);white-space:nowrap">🔒 ${claim.owner}</span>
          </td>
        </tr>`;
      }

      // Normal editable port
      const maxBaud  = p >= 2 ? 57600 : Infinity;
      const safeBaud = Math.min(sp.baud, maxBaud);
      const baudOpts = BAUD_RATES.filter(r => r <= maxBaud).map(r =>
        `<option value="${r}" ${r === safeBaud ? 'selected' : ''}>${r.toLocaleString()}</option>`
      ).join('');
      return `<tr>
        <td>Serial ${portNum}</td>
        <td><select id="wiz-b${i}-s${p}-baud">${baudOpts}</select></td>
        <td><input type="text" id="wiz-b${i}-s${p}-label" value="${escHtml(sp.label)}"
                   placeholder="optional label" style="min-width:100px"></td>
      </tr>`;
    }).join('');

    return `
      <div class="wizard-tab-panel ${i === wizardState.activeBoardTab ? 'active' : ''}" id="wiz-panel-serial-${i}">
        <table class="wizard-serial-table">
          <thead><tr><th>Port</th><th>Baud Rate ${wizHint('Communication speed for this serial port. Must match the baud rate of the device connected to it.')}</th><th>Label ${wizHint('Optional name for this port to remind you what is connected (e.g. Kyber, Maestro, GPS). Useful for troubleshooting.')}</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }).join('');

  return `
    <div class="wizard-section-title">Serial Port Configuration</div>
    <div class="wizard-section-desc">Set the baud rate for each serial port and add an optional label.
      Ports already claimed by Kyber or Maestro are shown with a locked baud rate and a pre-filled label — you can still rename them.</div>
    ${tabs}${panels}`;
}

function wizardHTMLKyber() {
  const yes = wizardState.kyberEnabled === true;
  const no  = wizardState.kyberEnabled === false && wizardState.kyberEnabled !== null;
  return `
    <div class="wizard-section-title">Kyber Sound Controller</div>
    <div class="wizard-section-desc">Are any of your WCBs connected to a Kyber sound board?</div>
    <div class="wizard-choice-grid">
      <button class="wizard-choice-btn ${yes ? 'selected' : ''}" onclick="wizardSetChoice('kyberEnabled',true,this)">
        <span class="wizard-choice-icon">🔊</span>
        <span class="wizard-choice-label">Yes, I use Kyber</span>
        <span class="wizard-choice-desc">One WCB will control the Kyber board via serial</span>
      </button>
      <button class="wizard-choice-btn ${!yes ? 'selected-no' : ''}" onclick="wizardSetChoice('kyberEnabled',false,this)">
        <span class="wizard-choice-icon">✕</span>
        <span class="wizard-choice-label">No Kyber</span>
        <span class="wizard-choice-desc">Skip Kyber configuration</span>
      </button>
    </div>`;
}

function wizardHTMLKyberConfig() {
  const { kyberBoard, kyberPort, kyberBaud, kyberMarcduinoPort, quantity } = wizardState;
  const boardOpts = Array.from({length: quantity}, (_, i) =>
    `<option value="${i+1}" ${(i+1) === kyberBoard ? 'selected' : ''}>Board ${i+1} (WCB ${wizardState.boards[i].wcbNumber})</option>`
  ).join('');
  const maestroPortOpts = Array.from({length: 5}, (_, p) =>
    `<option value="${p+1}" ${(p+1) === kyberPort ? 'selected' : ''}>Serial ${p+1}</option>`
  ).join('');
  const kyberBaudRates = [9600, 38400, 57600, 115200];
  const baudOpts = kyberBaudRates.map(r =>
    `<option value="${r}" ${r === kyberBaud ? 'selected' : ''}>${r.toLocaleString()}</option>`
  ).join('');
  // Marcduino port excludes the Maestro port
  const marcPortOpts = `<option value="0">— None —</option>` +
    Array.from({length: 5}, (_, p) => {
      const pn = p + 1;
      if (pn === kyberPort) return '';
      return `<option value="${pn}" ${pn === kyberMarcduinoPort ? 'selected' : ''}>Serial ${pn}</option>`;
    }).join('');
  return `
    <div class="wizard-section-title">Kyber Setup</div>
    <div class="wizard-section-desc">Select which board and serial ports the Kyber sound controller is connected to. Maestro targets can be added on the next screen and in the config page.</div>

    <div class="wizard-field-row">
      <label>Local Kyber Board ${wizHint('Which WCB is physically connected to the Kyber via serial cable.')}</label>
      <select id="wiz-kyber-board">${boardOpts}</select>
    </div>

    <div class="wizard-subsection-title">Kyber&#39;s Maestro Port</div>
    <div class="wizard-field-row">
      <label>Serial Port ${wizHint('Which serial port on that board the Kyber TX/RX wires are connected to from the Maestro port on the Kyber.')}</label>
      <select id="wiz-kyber-port">${maestroPortOpts}</select>
    </div>
    <div class="wizard-field-row">
      <label>Baud Rate ${wizHint('Communication speed for the Kyber Maestro port. Use 115,200 for current Kyber firmware or 57,600 for older versions.')}</label>
      <select id="wiz-kyber-baud">${baudOpts}</select>
      <span style="font-size:11px;color:var(--text3);margin-left:8px">115,200 current · 57,600 older</span>
    </div>

    <div class="wizard-subsection-title" style="margin-top:14px">Kyber&#39;s Marcduino Port</div>
    <div class="wizard-section-desc" style="margin-bottom:8px">The Marcduino port lets Kyber receive broadcast commands. Baud is always 9,600 and broadcasts are enabled automatically.</div>
    <div class="wizard-field-row">
      <label>Serial Port ${wizHint('Which serial port on that board the Kyber Marcduino TX/RX wires are connected to. Broadcasts will be enabled on this port automatically.')}</label>
      <select id="wiz-kyber-marcduino-port">${marcPortOpts}</select>
    </div>`;
}

function wizardHTMLMaestro() {
  const yes = wizardState.maestroEnabled === true;
  return `
    <div class="wizard-section-title">Maestro Servo Controller</div>
    <div class="wizard-section-desc">Are any of your WCBs connected to a Pololu Maestro servo controller?</div>
    <div class="wizard-choice-grid">
      <button class="wizard-choice-btn ${yes ? 'selected' : ''}" onclick="wizardSetChoice('maestroEnabled',true,this)">
        <span class="wizard-choice-icon">⚙</span>
        <span class="wizard-choice-label">Yes, I use Maestro</span>
        <span class="wizard-choice-desc">One or more WCBs control Maestro boards via serial</span>
      </button>
      <button class="wizard-choice-btn ${!yes ? 'selected-no' : ''}" onclick="wizardSetChoice('maestroEnabled',false,this)">
        <span class="wizard-choice-icon">✕</span>
        <span class="wizard-choice-label">No Maestro</span>
        <span class="wizard-choice-desc">Skip Maestro configuration</span>
      </button>
    </div>`;
}

function wizardHTMLMaestroConfig() {
  const { maestros, quantity } = wizardState;
  const boardOpts = (sel) => Array.from({length: quantity}, (_, i) =>
    `<option value="${i+1}" ${(i+1) === sel ? 'selected' : ''}>Board ${i+1} (WCB ${wizardState.boards[i].wcbNumber})</option>`
  ).join('');
  const rows = maestros.map((m, mi) => {
    // Baud capped by port (ports 3-5 max 57600)
    const maxBaud = m.port >= 3 ? 57600 : Infinity;
    const safeBaud = Math.min(m.baud, maxBaud);
    const baudOpts = BAUD_RATES.filter(r => r <= maxBaud).map(r =>
      `<option value="${r}" ${r === safeBaud ? 'selected' : ''}>${r.toLocaleString()}</option>`
    ).join('');
    // Exclude the Kyber ports if this maestro is on the Kyber board
    const isKyberBoard = wizardState.kyberEnabled && m.boardSlot === wizardState.kyberBoard;
    const kyberPortForBoard = isKyberBoard ? wizardState.kyberPort : null;
    const marcPortForBoard  = isKyberBoard ? wizardState.kyberMarcduinoPort : null;
    const portOpts = Array.from({length: 5}, (_, p) => {
      const portNum = p + 1;
      if (portNum === kyberPortForBoard || portNum === marcPortForBoard) return '';
      return `<option value="${portNum}" ${portNum === m.port ? 'selected' : ''}>Serial ${portNum}</option>`;
    }).join('');
    const idOpts = Array.from({length: 9}, (_, i) =>
      `<option value="${i+1}" ${(i+1) === m.id ? 'selected' : ''}>${i+1}</option>`
    ).join('');
    // Column order: ID → Board → Port → Baud
    return `<tr id="wiz-maestro-row-${mi}">
      <td style="color:var(--text3);font-size:11px">${mi+1}</td>
      <td><select id="wiz-m${mi}-id">${idOpts}</select></td>
      <td><select id="wiz-m${mi}-board" onchange="wizardMaestroBoardChange(${mi})">${boardOpts(m.boardSlot)}</select></td>
      <td><select id="wiz-m${mi}-port" onchange="wizardMaestroPortChange(${mi})">${portOpts}</select></td>
      <td style="min-width:95px"><select id="wiz-m${mi}-baud">${baudOpts}</select></td>
      <td><button class="btn btn-danger btn-sm btn-icon" onclick="wizardRemoveMaestro(${mi})">🗑</button></td>
    </tr>`;
  }).join('');
  return `
    <div class="wizard-section-title">Maestro Setup</div>
    <div class="wizard-section-desc">Add one row per Maestro. Select its ID, which board it's on, and which serial port it uses.</div>
    <table class="wizard-serial-table" style="margin-bottom:4px">
      <thead><tr><th>#</th><th>ID ${wizHint('Maestro controller ID — set this in the Pololu Maestro Control Center under Device Settings > Serial Settings.')}</th><th>Board ${wizHint('Which WCB is physically connected to this Maestro via serial cable.')}</th><th>Port ${wizHint('Which serial port on that WCB the Maestro TX/RX wires are connected to.')}</th><th>Baud ${wizHint('Communication speed — must match the baud rate configured in the Pololu Maestro Control Center.')}</th><th></th></tr></thead>
      <tbody id="wiz-maestro-tbody">${rows}</tbody>
    </table>
    <button class="btn btn-ghost btn-sm" onclick="wizardAddMaestro()">+ Add Maestro</button>`;
}

function wizardHTMLEtm() {
  const { etmEnabled, etmConfig } = wizardState;
  const detailDisplay = etmEnabled ? '' : 'display:none';
  return `
    <div class="wizard-section-title">Ensure Transmission Mode (ETM)</div>
    <div class="wizard-section-desc">ETM improves message reliability by tracking receipt of each transmission and automatically retransmitting if delivery is not acknowledged — so commands always get through. ETM is <strong>enabled by default</strong> and is required for remote management of WCBs over ESP-NOW.</div>
    <div class="wizard-etm-toggle">
      <input type="checkbox" id="wiz-etm-enabled" ${etmEnabled ? 'checked' : ''}
             onchange="wizardToggleEtmDetail(this.checked)">
      <label for="wiz-etm-enabled">Enable ETM on all boards ${wizHint('Ensure Transmission Mode monitors board-to-board communication and automatically marks boards as offline when they stop responding.')}</label>
    </div>
    <div class="wizard-etm-fields" id="wiz-etm-fields" style="${detailDisplay}">
      <div class="wizard-field-row">
        <label>Timeout (ms) ${wizHint('How long to wait for an ETM acknowledgement before counting it as missed. Lower = faster detection but more sensitive to brief delays. Default: 250 ms.')}</label>
        <input id="wiz-etm-timeout" type="number" value="${etmConfig.timeoutMs}">
      </div>
      <div class="wizard-field-row">
        <label>Heartbeat (sec) ${wizHint('How often boards broadcast a keep-alive signal to each other. Lower = faster offline detection, but generates more wireless traffic. Default: 5 sec.')}</label>
        <input id="wiz-etm-hb" type="number" value="${etmConfig.heartbeatSec}">
      </div>
      <div class="wizard-field-row">
        <label>Missed before action ${wizHint('How many consecutive missed heartbeats before a board is marked offline. Higher = more tolerance for brief dropouts. Default: 3.')}</label>
        <input id="wiz-etm-miss" type="number" value="${etmConfig.missedHeartbeats}">
      </div>
      <div class="wizard-field-row">
        <label>Boot heartbeat (sec) ${wizHint('Extended heartbeat interval used during startup to give boards time to fully initialize before normal ETM monitoring begins. Default: 30 sec.')}</label>
        <input id="wiz-etm-boot" type="number" value="${etmConfig.bootHeartbeatSec}">
      </div>
      <div class="wizard-field-row">
        <label>Packet Checksum Verification ${wizHint('Adds a checksum to ETM packets so boards can detect and discard corrupted messages. Recommended — leave enabled unless debugging specific issues.')}</label>
        <input type="checkbox" id="wiz-etm-chksm" ${(etmConfig.checksumEnabled ?? true) ? 'checked' : ''}>
      </div>
    </div>`;
}

function wizardHTMLReview() {
  const { quantity, password, mac2, mac3, kyberEnabled, maestroEnabled, etmEnabled, boards } = wizardState;
  const boardRows = boards.map((b, i) => {
    const hwLabel = WCBParser.HW_VERSION_MAP[b.hwVersion]?.display ?? 'Not set';
    return `<div class="wizard-review-row">
      <span class="wizard-review-label">Board ${i+1}</span>
      <span class="wizard-review-value">WCB ${b.wcbNumber} · HW ${hwLabel}</span>
    </div>`;
  }).join('');
  return `
    <div class="wizard-section-title">Everything looks good!</div>
    <div class="wizard-section-desc">Review your settings below. Click "Apply & Connect" to write these to the config page and start connecting your boards.</div>

    <div class="wizard-review-section">
      <div class="wizard-review-heading">System</div>
      <div class="wizard-review-row"><span class="wizard-review-label">Number of WCBs</span><span class="wizard-review-value">${quantity}</span></div>
      <div class="wizard-review-row"><span class="wizard-review-label">ESP-NOW Password</span><span class="wizard-review-value">${escHtml(password)}</span></div>
      <div class="wizard-review-row"><span class="wizard-review-label">MAC Octets</span><span class="wizard-review-value">XX:XX:${mac2.toUpperCase()}:${mac3.toUpperCase()}:XX:XX</span></div>
    </div>
    <div class="wizard-review-section">
      <div class="wizard-review-heading">Boards</div>
      ${boardRows}
    </div>
    <div class="wizard-review-section">
      <div class="wizard-review-heading">Optional Features</div>
      <div class="wizard-review-row"><span class="wizard-review-label">Kyber</span><span class="wizard-review-value">${kyberEnabled ? '✅ Enabled' : '—'}</span></div>
      <div class="wizard-review-row"><span class="wizard-review-label">Maestro</span><span class="wizard-review-value">${maestroEnabled ? '✅ Enabled' : '—'}</span></div>
      <div class="wizard-review-row"><span class="wizard-review-label">ETM</span><span class="wizard-review-value">${etmEnabled ? '✅ Enabled' : '—'}</span></div>
    </div>`;
}

function wizardHTMLFirmware() {
  const yes = wizardState.needsFirmware === true;
  // Erase checkbox is shown for both paths, but description text differs
  const eraseHint  = yes
    ? wizHint('Erases all NVS (non-volatile storage) on the board before flashing — use for a true factory reset. Any previously saved passwords, sequences, or port settings will be wiped.')
    : wizHint('Sends ?ERASE,NVS to the board, waits for it to reboot, then pushes the configuration — use when you want a clean slate without re-flashing firmware. All saved settings will be cleared.');
  const eraseTitle = yes
    ? 'Erase board memory before flashing'
    : 'Erase board memory (NVS only — no firmware re-flash)';
  const eraseDesc  = yes
    ? 'Erases all saved settings (passwords, port config, sequences) before flashing — use for a true factory reset.'
    : 'Clears all saved settings on the board, then pushes configuration — no firmware re-flash required.';
  const eraseBlock = `
    <div style="margin-top:12px;padding:10px 14px;background:var(--card-bg);border:1px solid var(--border);border-radius:6px">
      <label style="display:flex;align-items:flex-start;gap:10px;cursor:pointer">
        <input type="checkbox" id="wiz-erase-nvs" style="margin-top:2px;cursor:pointer" ${wizardState.eraseNvs ? 'checked' : ''}>
        <div>
          <div style="font-weight:600;font-size:13px">${eraseTitle} ${eraseHint}</div>
          <div style="font-size:11px;color:var(--text2);margin-top:2px">${eraseDesc}</div>
        </div>
      </label>
    </div>`;
  return `
    <div class="wizard-section-title">Firmware Upload</div>
    <div class="wizard-section-desc">Does WCB firmware need to be uploaded to these boards? Choose "Yes" if the boards are brand new or have never had WCB firmware installed.</div>
    <div class="wizard-choice-grid">
      <button class="wizard-choice-btn ${yes ? 'selected' : ''}"
              onclick="wizardFirmwareChoice(true,this)">
        <span class="wizard-choice-icon">💾</span>
        <span class="wizard-choice-label">Yes, upload firmware</span>
        <span class="wizard-choice-desc">Brand new boards or boards without WCB firmware yet</span>
      </button>
      <button class="wizard-choice-btn ${!yes ? 'selected-no' : ''}"
              onclick="wizardFirmwareChoice(false,this)">
        <span class="wizard-choice-icon">✓</span>
        <span class="wizard-choice-label">No, already installed</span>
        <span class="wizard-choice-desc">Firmware is already on the boards — just push configuration</span>
      </button>
    </div>
    ${eraseBlock}`;
}

function wizardFirmwareChoice(val, btn) {
  wizardState.needsFirmware = val;
  // Re-render to update the checkbox description text
  document.getElementById('wizard-body').innerHTML = wizardBuildStepHTML('firmware');
}

function wizardHTMLConnect() {
  const { connectMode, connectSeqN, boards, needsFirmware, eraseNvs } = wizardState;

  // ── Step 0: Mode picker ─────────────────────────────────────────
  if (!connectMode) {
    return `
      <div class="wizard-section-title">Connect &amp; Push</div>
      <div class="wizard-section-desc">How are you connecting boards to your computer?</div>
      <div class="wizard-choice-grid">
        <button class="wizard-choice-btn" onclick="wizardSelectConnectMode('all')">
          <span class="wizard-choice-icon">🔌</span>
          <span class="wizard-choice-label">All at once</span>
          <span class="wizard-choice-desc">All boards connected via USB hub — configure them simultaneously</span>
        </button>
        <button class="wizard-choice-btn selected" onclick="wizardSelectConnectMode('seq')">
          <span class="wizard-choice-icon">↕</span>
          <span class="wizard-choice-label">One at a time <span style="font-size:10px;color:var(--text3)">(recommended)</span></span>
          <span class="wizard-choice-desc">Move the USB cable between boards — you'll be guided exactly when to plug in, reset, and unplug each board</span>
        </button>
      </div>`;
  }

  // ── Step 1+: Board rows ─────────────────────────────────────────
  const rows = boards.map((b, i) => {
    const n = i + 1;
    const isSeq    = connectMode === 'seq';
    const isActive = !isSeq || n === connectSeqN;
    const isDimmed = isSeq && n > connectSeqN;
    const fwBadge = !needsFirmware && !eraseNvs ? ''
      : needsFirmware && eraseNvs  ? `<span class="badge badge-red"    style="font-size:9px;padding:2px 6px">Factory Reset</span>`
      : needsFirmware              ? `<span class="badge badge-yellow" style="font-size:9px;padding:2px 6px">Upload FW</span>`
      :                              `<span class="badge badge-orange" style="font-size:9px;padding:2px 6px">Erase NVS</span>`;
    return `
      <div class="wizard-connect-row ${isDimmed ? 'wiz-connect-dimmed' : ''}" id="wiz-connect-row-${n}">
        <span class="wizard-connect-label">
          WCB ${n} ${fwBadge}
          <span style="color:var(--text3);font-weight:400;font-size:11px;display:block">Board #${b.wcbNumber}</span>
        </span>
        <div style="display:flex;gap:6px;flex-shrink:0;align-items:center">
          <span id="wiz-connect-btns-${n}" style="display:${isActive ? 'flex' : 'none'};gap:6px">
            <button class="btn btn-primary btn-sm" id="wiz-manual-btn-${n}"
                    onclick="wizardManualConnect(${n})">🔌 Connect</button>
          </span>
          <button class="btn btn-ghost btn-sm" id="wiz-cancel-btn-${n}"
                  style="display:none" onclick="wizardCancelConnect(${n})">✕ Cancel</button>
          <span id="wiz-connect-pending-${n}" style="${isDimmed ? '' : 'display:none'};color:var(--text3);font-size:11px">↓ Up next</span>
        </div>
        <span class="wizard-connect-status" id="wiz-connect-status-${n}">${isDimmed ? '' : 'Waiting…'}</span>
      </div>`;
  }).join('');

  const bannerHtml = wizardConnectBannerText();
  const banner = bannerHtml
    ? `<div class="wizard-connect-banner" id="wiz-connect-banner">${bannerHtml}</div>`
    : `<div id="wiz-connect-banner" style="display:none"></div>`;
  const changeBtn = `<button class="btn btn-ghost btn-sm" style="margin-bottom:10px;font-size:11px"
      onclick="wizardSelectConnectMode(null)">← Change connection method</button>`;

  const tipHtml = `
    <div class="wizard-connect-tip">
      <strong>Tip:</strong> Unplug all boards first, then plug them in one at a time.
      When the port picker appears, the newly-added port is your board — select it to assign it to the correct slot.
    </div>`;

  return `
    <div class="wizard-section-title">Connect &amp; Push</div>
    ${changeBtn}
    ${tipHtml}
    ${banner}
    ${rows}`;
}

function wizardConnectBannerText() {
  const { connectMode, connectSeqN, boards } = wizardState;
  if (connectMode !== 'seq') return '';
  if (connectSeqN > boards.length) return '✅ All boards configured!';
  const b = boards[connectSeqN - 1];
  if (connectSeqN === 1) {
    return `📌 Plug <strong>WCB ${b.wcbNumber}</strong> into USB now, then click <strong>⊙ Auto-Detect</strong> and press the reset button, or click <strong>🔌 Manual</strong> to select the port.`;
  }
  return `✓ Done! Unplug the previous board. Now plug in <strong>WCB ${b.wcbNumber}</strong>, then click <strong>⊙ Auto-Detect</strong> or <strong>🔌 Manual</strong> below.`;
}

function wizardSelectConnectMode(mode) {
  wizardState.connectMode = mode;
  wizardState.connectSeqN = 1;
  document.getElementById('wizard-body').innerHTML = wizardBuildStepHTML('connect');
  if (mode) wizardStartConnectWatchers();
}

function wizardSeqAdvance(completedSlot) {
  if (wizardState.connectMode !== 'seq') return;
  const nextSlot = completedSlot + 1;
  const bannerEl = document.getElementById('wiz-connect-banner');

  if (nextSlot > wizardState.quantity) {
    // All done — update banner
    if (bannerEl) { bannerEl.innerHTML = '✅ All boards configured!'; bannerEl.style.display = ''; }
    return;
  }
  wizardState.connectSeqN = nextSlot;

  // Activate next board
  const pendingEl = document.getElementById(`wiz-connect-pending-${nextSlot}`);
  if (pendingEl) pendingEl.style.display = 'none';
  const btnsEl = document.getElementById(`wiz-connect-btns-${nextSlot}`);
  if (btnsEl) btnsEl.style.display = 'flex';
  const statusEl = document.getElementById(`wiz-connect-status-${nextSlot}`);
  if (statusEl) statusEl.textContent = 'Waiting…';
  const rowEl = document.getElementById(`wiz-connect-row-${nextSlot}`);
  if (rowEl) rowEl.classList.remove('wiz-connect-dimmed');

  // Update banner
  if (bannerEl) { bannerEl.innerHTML = wizardConnectBannerText(); bannerEl.style.display = ''; }

  // Scroll next row into view
  if (rowEl) rowEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// ── Interactive helpers used in step HTML ──────────────────────────
function wizardSelectQty(n) {
  wizardState.quantity = n;
  wizardInitBoards(n);
  document.querySelectorAll('.wizard-qty-btn').forEach(btn => {
    btn.classList.toggle('selected', parseInt(btn.textContent) === n);
  });
}

function wizardSetChoice(field, value, btn) {
  wizardState[field] = value;
  const parent = btn.closest('.wizard-choice-grid');
  parent.querySelectorAll('.wizard-choice-btn').forEach(b => {
    b.classList.remove('selected','selected-no');
  });
  btn.classList.add(value ? 'selected' : 'selected-no');
}

function wizardFillGen(inputId, type) {
  const val = type === 'password' ? wizardGenPassword() : wizardGenMacOctet();
  const el  = document.getElementById(inputId);
  if (el) el.value = val;
}

function wizardSwitchBoardTab(step, i) {
  wizardSaveStep(step); // save current tab first
  wizardState.activeBoardTab = i;
  document.querySelectorAll(`.wizard-board-tab`).forEach((t, ti) =>
    t.classList.toggle('active', ti === i));
  document.querySelectorAll(`[id^="wiz-panel-${step}-"]`).forEach((p, pi) =>
    p.classList.toggle('active', pi === i));
}

function wizardBoardTabs(step) {
  return `<div class="wizard-board-tabs">
    ${wizardState.boards.map((b, i) =>
      `<button class="wizard-board-tab ${i === wizardState.activeBoardTab ? 'active' : ''}"
               onclick="wizardSwitchBoardTab('${step}',${i})">
        WCB&nbsp;${b.wcbNumber}
      </button>`
    ).join('')}
  </div>`;
}

function wizardToggleEtmDetail(on) {
  wizardState.etmEnabled = on;
  const el = document.getElementById('wiz-etm-fields');
  if (el) el.style.display = on ? '' : 'none';
}

function wizardAddKyberTarget() {
  wizardState.kyberTargets.push({ id: wizardState.kyberTargets.length + 1, wcbNumber: 1, port: 1, baud: 57600 });
  document.getElementById('wizard-body').innerHTML = wizardBuildStepHTML('kyber-config');
}
function wizardRemoveKyberTarget(ti) {
  wizardSaveStep('kyber-config');
  wizardState.kyberTargets.splice(ti, 1);
  document.getElementById('wizard-body').innerHTML = wizardBuildStepHTML('kyber-config');
}
function wizardMaestroPortChange(mi) {
  const portSel = document.getElementById(`wiz-m${mi}-port`);
  const baudSel = document.getElementById(`wiz-m${mi}-baud`);
  if (!portSel || !baudSel) return;
  const port    = parseInt(portSel.value) || 1;
  const maxBaud = port >= 3 ? 57600 : Infinity;
  const curBaud = parseInt(baudSel.value) || 57600;
  baudSel.innerHTML = BAUD_RATES.filter(b => b <= maxBaud).map(b =>
    `<option value="${b}" ${b === Math.min(curBaud, maxBaud) ? 'selected' : ''}>${b.toLocaleString()}</option>`
  ).join('');
}

function wizardMaestroBoardChange(mi) {
  // When the board for a maestro changes, rebuild the port dropdown so the
  // Kyber-port exclusion is applied only for the Kyber board, not others.
  const boardSel = document.getElementById(`wiz-m${mi}-board`);
  const portSel  = document.getElementById(`wiz-m${mi}-port`);
  if (!boardSel || !portSel) return;

  const newBoardSlot = parseInt(boardSel.value) || 1;
  const curPort      = parseInt(portSel.value)  || 1;

  // Exclude Kyber ports only when this maestro is on the Kyber board
  const isKyberBoard = wizardState.kyberEnabled && newBoardSlot === wizardState.kyberBoard;
  const kyberPortForBoard = isKyberBoard ? wizardState.kyberPort : null;
  const marcPortForBoard  = isKyberBoard ? wizardState.kyberMarcduinoPort : null;

  portSel.innerHTML = Array.from({length: 5}, (_, p) => {
    const portNum = p + 1;
    if (portNum === kyberPortForBoard || portNum === marcPortForBoard) return '';
    return `<option value="${portNum}" ${portNum === curPort ? 'selected' : ''}>Serial ${portNum}</option>`;
  }).join('');

  // If the currently selected port was an excluded port, bump to first available
  if (curPort === kyberPortForBoard || curPort === marcPortForBoard) {
    const firstAvail = portSel.querySelector('option');
    if (firstAvail) portSel.value = firstAvail.value;
  }

  wizardMaestroPortChange(mi); // refresh baud options for the newly selected port
}

function wizardAddMaestro() {
  wizardSaveStep('maestro-config'); // preserve existing rows before adding
  wizardState.maestros.push({ boardSlot: 1, id: wizardState.maestros.length + 1, port: 1, baud: 115200 });
  document.getElementById('wizard-body').innerHTML = wizardBuildStepHTML('maestro-config');
}
function wizardRemoveMaestro(mi) {
  wizardSaveStep('maestro-config');
  wizardState.maestros.splice(mi, 1);
  document.getElementById('wizard-body').innerHTML = wizardBuildStepHTML('maestro-config');
}

// ── Save step values into state ────────────────────────────────────
function wizardSaveStep(key) {
  const get = (id) => document.getElementById(id);
  switch (key) {
    case 'network':
      if (get('wiz-password')) wizardState.password = get('wiz-password').value.trim();
      if (get('wiz-mac2')) wizardState.mac2 = get('wiz-mac2').value.trim().toUpperCase().padStart(2,'0');
      if (get('wiz-mac3')) wizardState.mac3 = get('wiz-mac3').value.trim().toUpperCase().padStart(2,'0');
      wizardState.delimiter = get('wiz-delim')?.value.trim().charAt(0)    || '^';
      wizardState.funcChar  = get('wiz-funcchar')?.value.trim().charAt(0) || '?';
      wizardState.cmdChar   = get('wiz-cmdchar')?.value.trim().charAt(0)  || ';';
      break;
    case 'identity':
      wizardState.boards.forEach((b, i) => {
        const num = parseInt(get(`wiz-b${i}-wcbnum`)?.value);
        const hw  = parseInt(get(`wiz-b${i}-hwver`)?.value ?? 0);
        if (!isNaN(num)) b.wcbNumber = num;
        if (!isNaN(hw))  b.hwVersion = hw;
      });
      break;
    case 'serial':
      wizardState.boards.forEach((b, i) => {
        b.serialPorts.forEach((sp, p) => {
          const baud = parseInt(get(`wiz-b${i}-s${p}-baud`)?.value);
          const lbl  = get(`wiz-b${i}-s${p}-label`)?.value ?? '';
          if (!isNaN(baud)) sp.baud = baud;
          sp.label = lbl;
        });
      });
      break;
    case 'kyber-config':
      wizardState.kyberBoard         = parseInt(get('wiz-kyber-board')?.value ?? 1);
      wizardState.kyberPort          = parseInt(get('wiz-kyber-port')?.value  ?? 2);
      wizardState.kyberBaud          = parseInt(get('wiz-kyber-baud')?.value  ?? 115200);
      wizardState.kyberMarcduinoPort = parseInt(get('wiz-kyber-marcduino-port')?.value) || null;
      break;
    case 'firmware':
      // needsFirmware is set by wizardFirmwareChoice(); always read the checkbox for eraseNvs
      wizardState.eraseNvs = document.getElementById('wiz-erase-nvs')?.checked ?? false;
      break;
    case 'maestro-config':
      wizardState.maestros = wizardState.maestros.map((m, mi) => ({
        boardSlot: parseInt(get(`wiz-m${mi}-board`)?.value ?? m.boardSlot),
        id:        parseInt(get(`wiz-m${mi}-id`)?.value    ?? m.id),
        port:      parseInt(get(`wiz-m${mi}-port`)?.value  ?? m.port),
        baud:      parseInt(get(`wiz-m${mi}-baud`)?.value  ?? m.baud),
      }));
      break;
    case 'etm':
      if (get('wiz-etm-enabled')) wizardState.etmEnabled = get('wiz-etm-enabled').checked;
      if (wizardState.etmEnabled) {
        wizardState.etmConfig.timeoutMs        = parseInt(get('wiz-etm-timeout')?.value ?? 30000);
        wizardState.etmConfig.heartbeatSec     = parseInt(get('wiz-etm-hb')?.value      ?? 10);
        wizardState.etmConfig.missedHeartbeats = parseInt(get('wiz-etm-miss')?.value    ?? 3);
        wizardState.etmConfig.bootHeartbeatSec = parseInt(get('wiz-etm-boot')?.value    ?? 30);
      }
      wizardState.etmConfig.checksumEnabled = get('wiz-etm-chksm')?.checked ?? true;
      break;
  }
}

// ── Validation ─────────────────────────────────────────────────────
function wizardValidateStep(key) {
  switch (key) {
    case 'network': {
      const pw = document.getElementById('wiz-password')?.value?.trim() ?? '';
      if (pw.length < 8) return 'Password must be at least 8 characters.';
      const m2 = document.getElementById('wiz-mac2')?.value?.trim() ?? '';
      const m3 = document.getElementById('wiz-mac3')?.value?.trim() ?? '';
      if (!/^[0-9A-Fa-f]{2}$/.test(m2)) return 'MAC Octet 2 must be two hex digits (00–FF).';
      if (!/^[0-9A-Fa-f]{2}$/.test(m3)) return 'MAC Octet 3 must be two hex digits (00–FF).';
      break;
    }
    case 'identity': {
      const nums = wizardState.boards.map((_, i) =>
        parseInt(document.getElementById(`wiz-b${i}-wcbnum`)?.value ?? 0)
      );
      if (nums.some(n => n < 1 || n > 99)) return 'WCB numbers must be between 1 and 99.';
      if (new Set(nums).size !== nums.length) return 'Each board must have a unique WCB number.';
      const hvs = wizardState.boards.map((_, i) =>
        parseInt(document.getElementById(`wiz-b${i}-hwver`)?.value ?? 0)
      );
      if (hvs.some(h => h === 0)) return 'Please select a hardware version for every board.';
      break;
    }
    case 'maestro-config': {
      if (wizardState.kyberEnabled) {
        for (const m of wizardState.maestros) {
          if (m.boardSlot === wizardState.kyberBoard && m.port === wizardState.kyberPort) {
            return `Maestro ID ${m.id} uses Serial ${m.port}, which is already claimed by Kyber's Maestro port on Board ${wizardState.kyberBoard}. Choose a different port.`;
          }
          if (wizardState.kyberMarcduinoPort &&
              m.boardSlot === wizardState.kyberBoard && m.port === wizardState.kyberMarcduinoPort) {
            return `Maestro ID ${m.id} uses Serial ${m.port}, which is already claimed by Kyber's Marcduino port on Board ${wizardState.kyberBoard}. Choose a different port.`;
          }
        }
      }
      break;
    }
  }
  return null;
}

// ── Random generators ──────────────────────────────────────────────
function wizardGenPassword() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789';
  return Array.from({length: 14}, () => chars[Math.floor(Math.random() * chars.length)]).join('');
}
function wizardGenMacOctet() {
  return Math.floor(Math.random() * 256).toString(16).padStart(2, '0').toUpperCase();
}

// ── Apply config to the main config page ──────────────────────────
function wizardApplyConfig() {
  const ws = wizardState;

  // Quantity → render board sections
  const qtyEl = document.getElementById('g-wcbq');
  if (qtyEl) qtyEl.value = ws.quantity;
  renderBoards(ws.quantity);
  systemConfig.general.wcbQuantity = ws.quantity;

  // General network fields
  const setEl = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
  setEl('g-password', ws.password);
  setEl('g-mac2', ws.mac2);
  setEl('g-mac3', ws.mac3);
  setEl('g-delimiter', ws.delimiter);
  setEl('g-funcchar',  ws.funcChar);
  setEl('g-cmdchar',   ws.cmdChar);
  onGeneralPasswordChange();
  onGeneralMacChange();
  onGeneralCmdCharChange();

  // Per-board fields
  ws.boards.forEach((b, i) => {
    const n = i + 1;
    const cfg = WCBParser.createDefaultBoardConfig();
    cfg.wcbNumber  = b.wcbNumber;
    cfg.hwVersion  = b.hwVersion;
    cfg.espnowPassword = ws.password;
    cfg.macOctet2  = ws.mac2;
    cfg.macOctet3  = ws.mac3;
    cfg.delimiter  = ws.delimiter;
    cfg.funcChar   = ws.funcChar;
    cfg.cmdChar    = ws.cmdChar;

    b.serialPorts.forEach((sp, p) => {
      cfg.serialPorts[p].baud  = sp.baud;
      cfg.serialPorts[p].label = sp.label;
    });

    // Kyber
    if (ws.kyberEnabled) {
      if ((i + 1) === ws.kyberBoard) {
        cfg.kyber.mode = 'local';
        cfg.kyber.port = ws.kyberPort;
        cfg.kyber.baud = ws.kyberBaud;
        // Mirror kyber baud into the serial port so ?BAUD is generated correctly
        cfg.serialPorts[ws.kyberPort - 1].baud  = ws.kyberBaud;
        cfg.serialPorts[ws.kyberPort - 1].label = cfg.serialPorts[ws.kyberPort - 1].label || 'Kyber Maestro';
        // Marcduino port — always 9600, broadcasts enabled
        if (ws.kyberMarcduinoPort) {
          const marcIdx = ws.kyberMarcduinoPort - 1;
          cfg.kyber.marcduinoPort               = ws.kyberMarcduinoPort;
          cfg.serialPorts[marcIdx].baud         = 9600;
          cfg.serialPorts[marcIdx].label        = 'Kyber Marcuino';
          cfg.serialPorts[marcIdx].broadcastIn  = true;
          cfg.serialPorts[marcIdx].broadcastOut = true;
        }
        // Include ALL maestros (local and remote) as Kyber targets so the firmware
        // knows every Maestro it must forward commands to — both those on this same
        // board (via local serial) and those on remote boards (via ESP-NOW).
        // Use wcbNumber (not boardSlot) so the WCB numbers in the KYBER,LOCAL
        // command match those in the MAESTRO routing table.
        cfg.kyber.targets = ws.maestros
          .map(m => ({
            id:   m.id,
            wcb:  ws.boards[m.boardSlot - 1].wcbNumber,
            port: m.port,
            baud: m.baud,
          }));
      } else {
        // Any non-kyber board that has a Maestro with ID 1 or 2 must be
        // configured as KYBER_REMOTE so it receives Kyber commands via ESP-NOW
        const hasPrimaryMaestro = ws.maestros.some(
          m => m.boardSlot === (i + 1) && (m.id === 1 || m.id === 2)
        );
        if (hasPrimaryMaestro) {
          cfg.kyber.mode = 'remote';
        }
      }
    }

    // Maestro — local entries only (for port claiming, UI display, etc.)
    const myMaestros = ws.maestros.filter(m => m.boardSlot === (i + 1));
    cfg.maestros = myMaestros.map(m => ({ id: m.id, port: m.port, baud: m.baud }));
    // Full routing table — all maestros across all boards, with correct WCB numbers.
    // Used by buildCommandString to emit the complete ?MAESTRO routing table so that
    // every board knows where to route commands for remote maestros (via ESP-NOW).
    if (ws.maestros.length > 0) {
      cfg.maestroTable = ws.maestros.map(m => ({
        id:   m.id,
        wcb:  ws.boards[m.boardSlot - 1].wcbNumber,
        port: m.port,
        baud: m.baud,
      }));
    }

    // ETM
    if (ws.etmEnabled) {
      cfg.etm = { enabled: true, ...ws.etmConfig };
    } else {
      cfg.etm.enabled = false;
    }

    boardConfigs[n] = cfg;
    populateUIFromConfig(n, cfg);
    onBoardFieldChange(n);

    // Note: firmware mode radios are stamped in wizardNext when leaving the firmware step
  });

  // ETM global toggle
  const etmEl = document.getElementById('g-etm-enabled');
  if (ws.etmEnabled) {
    if (etmEl) { etmEl.checked = true; onETMToggle(); }
    setEl('g-etm-timeout', ws.etmConfig.timeoutMs);
    setEl('g-etm-hb',      ws.etmConfig.heartbeatSec);
    setEl('g-etm-miss',    ws.etmConfig.missedHeartbeats);
    setEl('g-etm-boot',    ws.etmConfig.bootHeartbeatSec);
    setEl('g-etm-count',   ws.etmConfig.messageCount);
    setEl('g-etm-delay',   ws.etmConfig.messageDelayMs);
    const chksmEl = document.getElementById('g-etm-chksm');
    if (chksmEl) chksmEl.checked = ws.etmConfig.checksumEnabled ?? true;
    systemConfig.general.etm = { enabled: true, ...ws.etmConfig };
  } else {
    if (etmEl) { etmEl.checked = false; onETMToggle(); }
    systemConfig.general.etm = { ...systemConfig.general.etm, enabled: false };
  }

  showToast('Config page updated from wizard', 'success');
}

// ── Connect & push each board ──────────────────────────────────────
const wizardConnectWatchers = {};   // interval IDs keyed by board slot

function wizardDisableConnectBtns(n) {
  const grp = document.getElementById(`wiz-connect-btns-${n}`);
  if (grp) grp.style.display = 'none';
  const cancel = document.getElementById(`wiz-cancel-btn-${n}`);
  if (cancel) cancel.style.display = '';
}
function wizardEnableConnectBtns(n) {
  const grp = document.getElementById(`wiz-connect-btns-${n}`);
  if (grp) grp.style.display = 'flex';
  const cancel = document.getElementById(`wiz-cancel-btn-${n}`);
  if (cancel) cancel.style.display = 'none';
}

async function wizardCancelConnect(n) {
  // Stop the watch-for-connect interval
  if (wizardConnectWatchers[n]) {
    clearInterval(wizardConnectWatchers[n]);
    delete wizardConnectWatchers[n];
  }
  // Drop any partial connection
  try { await boardConnections[n]?.disconnect(); } catch (_) {}
  delete boardConnections[n];
  // Reset UI back to the initial waiting state
  wizardEnableConnectBtns(n);
  wizardSetConnectStatus(n, '', 'Waiting…');
}

async function wizardManualConnect(n) {
  // Snapshot wizard config NOW — before boardManualConnect triggers boardPull,
  // which could overwrite boardConfigs[n] before wizardWatchForConnect runs.
  const configSnapshot  = boardConfigs[n] ? JSON.parse(JSON.stringify(boardConfigs[n])) : null;
  const generalSnapshot = captureGeneralDOMSnapshot();

  wizardDisableConnectBtns(n);
  wizardSetConnectStatus(n, 'busy', 'Select port…');
  try {
    await boardManualConnect(n);
    wizardWatchForConnect(n, configSnapshot, generalSnapshot);
  } catch(e) {
    wizardEnableConnectBtns(n);
    wizardSetConnectStatus(n, '', 'Waiting…');
  }
}

function wizardWatchForConnect(n, preConfigSnapshot, preGeneralSnapshot) {
  // Use pre-taken snapshots if provided (from wizardManualConnect, taken before
  // the board connects so boardPull cannot overwrite the wizard config first).
  // Fall back to snapshotting now for callers that connect before calling us.
  const configSnapshot  = preConfigSnapshot  ?? (boardConfigs[n] ? JSON.parse(JSON.stringify(boardConfigs[n])) : null);
  const generalSnapshot = preGeneralSnapshot ?? captureGeneralDOMSnapshot();

  let attempts = 0;
  const iv = setInterval(async () => {
    attempts++;
    // Wait until the board is connected AND the initial ?backup has completed.
    // boardBaselines[n] being non-null means boardPull finished — the board is
    // fully booted and responsive, so it's safe to push the full config.
    if (boardConnections[n]?.isConnected() && boardBaselines[n]) {
      clearInterval(iv);
      delete wizardConnectWatchers[n];
      wizardEnableConnectBtns(n);           // hide Cancel, restore Auto/Manual
      wizardSetConnectStatus(n, 'busy', 'Pushing…');

      // Restore wizard config — boardPull will have overwritten boardConfigs[n]
      // and the general DOM fields with the board's current values.  Put the
      // wizard's intended config back before pushing.
      if (configSnapshot) {
        boardConfigs[n]   = JSON.parse(JSON.stringify(configSnapshot));
        boardBaselines[n] = null;   // force a full push
        populateUIFromConfig(n, boardConfigs[n]);
      }
      restoreGeneralDOMSnapshot(generalSnapshot);

      try {
        wizardSetConnectStatus(n, 'busy', '📤 Pushing…');
        await boardGo(n);
        if (boardConnections[n]?.isConnected()) {
          wizardSetConnectStatus(n, 'busy', '🔍 Verifying…');
          await sleep(800);
          await boardPull(n);
        }
        wizardSetConnectStatus(n, 'ok', '✓ Done');
      } catch(e) {
        wizardSetConnectStatus(n, 'err', '✕ Push failed');
      }
    } else if (attempts > 180) { // 90s timeout
      clearInterval(iv);
      delete wizardConnectWatchers[n];
      wizardEnableConnectBtns(n);
      wizardSetConnectStatus(n, 'err', '✕ Timed out');
    }
  }, 500);
  wizardConnectWatchers[n] = iv;    // store so Cancel can clear it
}

function wizardSetConnectStatus(n, type, text) {
  const el = document.getElementById(`wiz-connect-status-${n}`);
  if (!el) return;
  el.className = `wizard-connect-status ${type}`;
  el.textContent = text;
  if (type === 'ok' || type === 'err') {
    wizardCheckAllDone();
    if (type === 'ok') wizardSeqAdvance(n);
  }
}

function wizardCheckAllDone() {
  let anyBusy    = false;
  let anyWaiting = false;
  let anyOk      = false;
  for (let n = 1; n <= wizardState.quantity; n++) {
    const el = document.getElementById(`wiz-connect-status-${n}`);
    if (!el) continue;
    if (el.classList.contains('busy'))               { anyBusy = true; break; }
    if (el.classList.contains('ok'))                   anyOk = true;
    else if (!el.classList.contains('err'))            anyWaiting = true; // still at initial state
  }
  // Only close when every board is resolved (ok or err) — don't close while any are still waiting
  if (!anyBusy && !anyWaiting && anyOk) {
    setTimeout(() => closeWizard(), 2500);
  }
}

function wizardStartConnectWatchers() {
  // For boards already connected when the wizard reaches the connect step,
  // trigger a fresh ?backup first (same as wizardWatchForConnect does for
  // newly-connected boards) so we confirm the board is booted and responsive
  // before pushing.
  for (let i = 0; i < wizardState.quantity; i++) {
    const n = i + 1;
    if (boardConnections[n]?.isConnected()) {
      const configSnapshot  = boardConfigs[n] ? JSON.parse(JSON.stringify(boardConfigs[n])) : null;
      const generalSnapshot = captureGeneralDOMSnapshot();

      wizardDisableConnectBtns(n);
      wizardSetConnectStatus(n, 'busy', '🔍 Pulling…');

      // Clear the baseline so we can detect when the fresh pull completes.
      boardBaselines[n] = null;
      boardPull(n);   // async — fires ?backup, sets boardBaselines[n] when done

      let attempts = 0;
      const iv = setInterval(async () => {
        attempts++;
        if (boardBaselines[n]) {
          // boardPull finished — board is booted and responsive.
          clearInterval(iv);
          wizardSetConnectStatus(n, 'busy', '📤 Pushing…');

          // Restore the wizard's intended config (boardPull overwrites boardConfigs[n]).
          if (configSnapshot) {
            boardConfigs[n]   = JSON.parse(JSON.stringify(configSnapshot));
            boardBaselines[n] = null;   // force full push
            populateUIFromConfig(n, boardConfigs[n]);
          }
          restoreGeneralDOMSnapshot(generalSnapshot);

          try {
            await boardGo(n);
            if (boardConnections[n]?.isConnected()) {
              wizardSetConnectStatus(n, 'busy', '🔍 Verifying…');
              await sleep(800);
              await boardPull(n);
            }
            wizardEnableConnectBtns(n);
            wizardSetConnectStatus(n, 'ok', '✓ Done');
          } catch(e) {
            wizardEnableConnectBtns(n);
            wizardSetConnectStatus(n, 'err', '✕ Push failed');
          }
        } else if (attempts > 60) { // 30s timeout for pull
          clearInterval(iv);
          wizardEnableConnectBtns(n);
          wizardSetConnectStatus(n, 'err', '✕ Pull timed out');
        }
      }, 500);
    }
  }
}

// ─── Utilities ────────────────────────────────────────────────────
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function escHtml(str) {
  return String(str)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}