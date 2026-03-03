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

// ─── Init ─────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  checkBrowserCompat();
  loadThemePreference();
  initSystemConfig();
  loadModePreference();
  initTerminalResize();
  showSplash();
});

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
      claimNote.textContent = 'Managed by Kyber';
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
  const mode     = document.querySelector(`input[name="b${n}-kyber"]:checked`)?.value ?? 'none';
  const portWrap    = document.getElementById(`b${n}-kyber-port-wrap`);
  const baudWrap    = document.getElementById(`b${n}-kyber-baud-wrap`);
  const targetsWrap = document.getElementById(`b${n}-kyber-targets-wrap`);
  portWrap.style.display = mode === 'local' ? '' : 'none';
  if (baudWrap)    baudWrap.style.display    = mode === 'local' ? '' : 'none';
  if (targetsWrap) targetsWrap.style.display = mode === 'local' ? '' : 'none';

  const config = boardConfigs[n];
  if (!config) return;

  for (const port of config.serialPorts) {
    if (port.claimedBy?.type === 'kyber') port.claimedBy = null;
  }

  config.kyber.mode = mode;
  if (mode !== 'local') config.kyber.port = null;

  updateKyberPortDropdown(n);

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
  }

  WCBParser.evaluatePortClaims(config);
  updatePortClaimUI(n);
  updateKyberPortDropdown(n);
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
    if (!claim || claim.type === 'kyber') {
      const opt = document.createElement('option');
      opt.value = p;
      opt.textContent = `Serial ${p}`;
      if (p === currentPort) opt.selected = true;
      portSel.appendChild(opt);
    }
  }
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

function onBoardModeChange(n) {
  const mode = document.querySelector(`input[name="b${n}-mode"]:checked`)?.value;
  const wipeWrap = document.getElementById(`b${n}-wipe-nvs-wrap`);
  if (wipeWrap) {
    wipeWrap.style.display = mode === 'configure' ? '' : 'none';
    if (mode !== 'configure') {
      const wipeEl = document.getElementById(`b${n}-wipe-nvs`);
      if (wipeEl) wipeEl.checked = false;
    }
  }
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

  const fwEl = document.getElementById(`b${n}-fw-version`);
  if (fwEl && hwVal > 0) {
    const info = WCBParser.HW_VERSION_MAP[hwVal];
    fwEl.textContent = info ? `${info.binary} binary` : '';
  }
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
  } else {
    config.kyber.port = null;
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
    updateKyberPortDropdown(n);
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
    connected:  ['badge-accent', '● Connected'],
    remote:     ['badge-accent', '📡 Remote'],
    error:      ['badge-red',    '✕ Error'],
    default:    ['badge-default','Not Connected'],
  };
  const [cls, text] = states[state] ?? states.default;
  badge.classList.add(cls);
  badge.textContent = text;
}

// ─── Maestro ──────────────────────────────────────────────────────
function addMaestroRow(n) {
  appendMaestroRow(n, { id: 1, port: null, baud: 57600 });
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
    // Exclude ports claimed by kyber or other maestro rows
    const claim = config?.serialPorts?.[p - 1]?.claimedBy;
    const claimedByKyber = claim?.type === 'kyber';
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
              const displayed = this._lineTransform ? this._lineTransform(line) : line;
              if (displayed !== null) termLog(this.boardIndex, displayed, 'out');
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
  if (connBtn)     { connBtn.textContent = 'Disconnect'; connBtn.classList.remove('btn-detecting'); }
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

function clearRemoteConnected(n) {
  const relayN = remoteRelayForBoard[n];
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
  openConnectModal(n);        // show modal immediately
  boardAutoDetect(n);         // start detecting in parallel — intentionally not awaited
}

async function boardDisconnect(n) {
  const btn = document.getElementById(`b${n}-btn-connect`);
  if (btn) { btn.textContent = 'Disconnecting…'; btn.disabled = true; }
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
      if (mismatches.length > 0) {
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
      }
    } else {
      // ── Board number matches slot (normal path) ───────────────────
      boardConfigs[n]   = config;
      boardBaselines[n] = JSON.parse(JSON.stringify(config));
      populateUIFromConfig(n, config);
      updateBoardStatusBadge(n, 'connected');
      showToast(`Config pulled from WCB ${n}`, 'success');
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

async function boardGo(n, opts = {}) {
  // Delegate to the remote push path when this board is reached via relay
  if (remoteRelayForBoard[n]) return boardGoRemote(n, opts);
  const skipReboot = opts.skipReboot ?? false;

  const conn = boardConnections[n];
  if (!conn?.isConnected()) { showToast('Board not connected', 'error'); return; }

  const mode      = document.querySelector(`input[name="b${n}-mode"]:checked`)?.value;
  const isFlash   = mode === 'flash';
  const isUpdate  = mode === 'update';
  const isFactory = mode === 'factory';
  const btn       = document.getElementById(`b${n}-btn-go`);

  btn.disabled = true;
  btn.textContent = (isFlash || isUpdate || isFactory) ? 'Flashing…' : 'Pushing…';

  if (isFlash || isUpdate || isFactory) {
    // ── Validate HW version ──────────────────────────────────────
    const hwVersion = boardConfigs[n]?.hwVersion;
    if (!hwVersion) {
      showToast('Select a hardware version before flashing', 'error');
      btn.disabled = false;
      btn.textContent = 'Push Config';
      return;
    }

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
        // Switch back to configure mode regardless
        const configRadio = document.querySelector(`input[name="b${n}-mode"][value="configure"]`);
        if (configRadio) configRadio.checked = true;

        if (isUpdate) {
          // App-only update: NVS is intact, just pull to verify config survived
          termLog(n, 'Reconnected — pulling config to verify NVS…', 'sys');
          showToast(`WCB ${n} updated — verifying config…`, 'success');
          setTimeout(() => boardPull(n), 4000);
        } else {
          // Flash or Factory Reset: NVS is blank, push full config.
          // Snapshot the wizard config right now — boardPull can still fire
          // (e.g. from the initial connect handler) and overwrite boardConfigs[n]
          // plus the shared general DOM fields before the 4 s push window expires.
          const configSnapshot  = boardConfigs[n] ? JSON.parse(JSON.stringify(boardConfigs[n])) : null;
          const generalSnapshot = captureGeneralDOMSnapshot();
          boardBaselines[n] = null;
          const resetLabel = isFactory ? 'factory reset' : 'flashed';
          termLog(n, `Reconnected after ${resetLabel} — pushing config in 4s…`, 'sys');
          showToast(`WCB ${n} reconnected — pushing config…`, 'success');
          setTimeout(() => {
            // Restore wizard config in case boardPull overwrote it during the window
            if (configSnapshot) {
              boardConfigs[n]   = JSON.parse(JSON.stringify(configSnapshot));
              boardBaselines[n] = null;
              populateUIFromConfig(n, boardConfigs[n]);
            }
            restoreGeneralDOMSnapshot(generalSnapshot);
            boardGo(n);
          }, 4000);
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

  // ── Wipe NVS before configure if checkbox is checked ─────────────
  // This erases all NVS memory, reboots the board, then re-calls boardGo
  // to push the config to a fresh board — same result as factory reset
  // but without reflashing the firmware.
  const wipeFirst = document.getElementById(`b${n}-wipe-nvs`)?.checked;
  if (wipeFirst) {
    const configSnapshot  = boardConfigs[n] ? JSON.parse(JSON.stringify(boardConfigs[n])) : null;
    const generalSnapshot = captureGeneralDOMSnapshot();
    boardBaselines[n] = null;
    try {
      termLog(n, '?ERASE,NVS', 'in');
      await conn.send('?ERASE,NVS\r');
      // Firmware counts down ~3 s before erasing and rebooting.
      // Closing the serial port immediately causes a USB-disconnect reset that
      // fires BEFORE the erase runs — wait 4 s to let it finish.
      termLog(n, 'Waiting for firmware erase countdown…', 'sys');
      showToast(`WCB ${n} erasing — do not disconnect…`, 'warning', 5000);
      await sleep(4000);
      termLog(n, 'NVS erased — board rebooting…', 'sys');

      await conn.closeForReconnect();
      updateConnectionUI(n, false);
      termLog(n, 'Board disconnected — attempting reconnect…', 'sys');

      const reconnected = await conn.reconnect(10, 1500);
      if (reconnected) {
        updateConnectionUI(n, true);
        termLog(n, 'Reconnected after erase — pushing config in 4s…', 'sys');
        // Uncheck the wipe box so the re-call doesn't erase again
        const wipeEl = document.getElementById(`b${n}-wipe-nvs`);
        if (wipeEl) wipeEl.checked = false;
        setTimeout(() => {
          // Restore config in case boardPull fired during reconnect window
          if (configSnapshot) {
            boardConfigs[n]   = JSON.parse(JSON.stringify(configSnapshot));
            boardBaselines[n] = null;
            populateUIFromConfig(n, boardConfigs[n]);
          }
          restoreGeneralDOMSnapshot(generalSnapshot);
          boardGo(n);
        }, 4000);
      } else {
        termLog(n, 'Reconnect failed — connect manually', 'err');
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
        await sleep(300);
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
    if (!connected) connBtn.classList.remove('btn-detecting');
  }
  updateBoardStatusBadge(n, connected ? 'connected' : 'default');
  updateSequencePlayButtons(n);

  // Create pane on first connect; update dot/input state on any connect/disconnect
  if (connected) ensureTerminalPane(n);
  updateTerminalPaneDot(n, connected);
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
      }
    } else if (offlineMatch) {
      const bn = parseInt(offlineMatch[1]);
      if (remoteRelayForBoard[bn] === relayN) {
        document.getElementById(`b${bn}-dot`)?.classList.remove('connected');
        showToast(`⚠️ WCB ${bn}: ETM timeout — board may be offline`, 'warning', 6000);
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
  syncKyberTargetsToConfig(n);
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
async function remoteBoardPull(relayN, targetN) {
  const relayConn = boardConnections[relayN];
  if (!relayConn?.isConnected()) { showToast('Relay board not connected', 'error'); return; }

  termLog(relayN, `[Remote] Requesting config from WCB${targetN}…`, 'sys');
  showToast(`Pulling config from WCB${targetN} via WCB${relayN}…`, 'info');

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

    const configStr = line.slice(prefix.length).trim();
    if (!configStr) {
      showToast(`WCB${targetN}: empty config response`, 'error');
      termLog(relayN, `[Remote] WCB${targetN} returned empty config`, 'err');
      return;
    }

    const config = WCBParser.parseBackupString(configStr);
    boardConfigs[targetN]   = config;
    boardBaselines[targetN] = JSON.parse(JSON.stringify(config));
    populateUIFromConfig(targetN, config);
    // Preserve remote badge and connection label (setRemoteConnected may have run before us)
    updateBoardStatusBadge(targetN, remoteRelayForBoard[targetN] ? 'remote' : 'configured');
    showToast(`Config pulled from WCB${targetN} (remote via WCB${relayN})`, 'success');
    termLog(relayN, `[Remote] Config received from WCB${targetN} (${configStr.length} chars)`, 'sys');
  };

  relayConn._dataCallbacks.push(onLine);
  timer = setTimeout(() => {
    if (done) return;
    done = true;
    cleanup();
    showToast(`WCB${targetN}: config pull timed out`, 'error');
    termLog(relayN, `[Remote] Config pull from WCB${targetN} timed out`, 'err');
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
  systemConfig.general.etm.enabled    = document.getElementById('g-etm-enabled').checked;

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
  const connected = boardConnections[n]?.isConnected() ?? false;

  // Debug buttons
  for (const { key } of DEBUG_MODES) {
    const btn = document.getElementById(`dbg-btn-${n}-${key}`);
    if (!btn) continue;
    btn.disabled = !connected;
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
  const conn = boardConnections[n];
  if (!conn?.isConnected()) return;

  const state    = ensureDebugState(n);
  state[modeKey] = !state[modeKey];
  const onOff    = state[modeKey] ? 'ON' : 'OFF';

  const funcChar = boardConfigs[n]?.funcChar ?? '?';
  const modeInfo = DEBUG_MODES.find(m => m.key === modeKey);
  if (!modeInfo) return;

  const cmd = `${funcChar}${modeInfo.cmd},${onOff}`;
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

function ensureTerminalPane(n) {
  if (document.getElementById(`term-pane-${n}`)) return; // already exists

  // Hide the empty-state placeholder
  const noBoards = document.getElementById('terminal-no-boards');
  if (noBoards) noBoards.style.display = 'none';

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
  if (e.key === 'Enter') sendTerminalCommandTo(n);
}

async function sendTerminalCommandTo(n) {
  const input = document.getElementById(`term-pane-input-${n}`);
  const cmd   = input?.value?.trim();
  if (!cmd) return;
  input.value = '';

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
  config.kyber.targets = [];
  for (let b = 1; b <= 9; b++) {
    const bc = boardConfigs[b];
    if (!bc?.maestros?.length) continue;
    const wcbNum = bc.wcbNumber || b;
    for (const m of bc.maestros) {
      config.kyber.targets.push({ id: m.id, wcb: wcbNum, port: m.port, baud: m.baud });
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

// ─── NVS Erase ────────────────────────────────────────────────────
async function boardEraseNVS(n) {
  const conn = boardConnections[n];
  if (!conn?.isConnected()) { showToast('Board not connected', 'error'); return; }

  const confirmed = confirm(
    `⚠️ ERASE ALL NVS DATA ON WCB ${n}?\n\n` +
    `This will wipe ALL stored settings including:\n` +
    `  • Baud rates & broadcast settings\n` +
    `  • MAC address octets\n` +
    `  • ESP-NOW password\n` +
    `  • WCB number & quantity\n` +
    `  • Hardware version\n` +
    `  • Stored sequences & commands\n` +
    `  • Kyber settings\n\n` +
    `The board will reboot after erasing.\n\nAre you sure?`
  );
  if (!confirmed) return;

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
        termLog(n, 'Reconnected after erase', 'sys');
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
    kyberEnabled:  false,
    kyberBoard:    1,
    kyberPort:     2,
    kyberBaud:     115200,
    kyberTargets:  [],
    maestroEnabled: false,
    maestros:      [],            // [{ boardSlot, id, port, baud }]
    etmEnabled:    true,
    etmConfig:     { timeoutMs:30000, heartbeatSec:10, missedHeartbeats:3,
                     bootHeartbeatSec:30, messageCount:3, messageDelayMs:100 },
    needsFirmware: false,
    eraseNvs:      false,
    activeBoardTab: 0,
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

  // After firmware choice is made, stamp the board mode radios on the config page
  if (key === 'firmware') {
    const modeVal = !wizardState.needsFirmware ? 'configure'
                  : wizardState.eraseNvs       ? 'factory'
                  :                              'update';
    wizardState.boards.forEach((b, i) => {
      const n = i + 1;
      const radio = document.querySelector(`input[name="b${n}-mode"][value="${modeVal}"]`);
      if (radio) radio.checked = true;
      // Show/hide the wipe-nvs wrap based on the stamped mode
      onBoardModeChange(n);
      // If configure-only with erase, pre-check the per-board wipe checkbox
      if (!wizardState.needsFirmware && wizardState.eraseNvs) {
        const wipeEl = document.getElementById(`b${n}-wipe-nvs`);
        if (wipeEl) wipeEl.checked = true;
      }
    });
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

// ── Per-step HTML builders ─────────────────────────────────────────
function wizardBuildStepHTML(key) {
  switch (key) {
    case 'welcome':      return wizardHTMLWelcome();
    case 'quantity':     return wizardHTMLQuantity();
    case 'network':      return wizardHTMLNetwork();
    case 'identity':     return wizardHTMLIdentity();
    case 'serial':       return wizardHTMLSerial();
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
    <div class="modal-option" style="cursor:default">
      <span class="modal-option-icon">ℹ</span>
      <div class="modal-option-text">
        <div class="modal-option-title">What you'll need</div>
        <div class="modal-option-desc">Chrome or Edge browser · About 5 minutes</div>
      </div>
    </div>
    <div class="modal-option" style="cursor:default">
      <span class="modal-option-icon">🔌</span>
      <div class="modal-option-text">
        <div class="modal-option-title">Connecting your boards</div>
        <div class="modal-option-desc">
          <strong>Preferred:</strong> Connect all boards via their own USB cable simultaneously — the wizard will push to each one automatically.<br><br>
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
      <label>ESP-NOW Password</label>
      <input id="wiz-password" type="text" value="${escHtml(password)}" spellcheck="false">
      <button class="wizard-gen-btn" title="Generate random" onclick="wizardFillGen('wiz-password','password')">🎲</button>
    </div>
    <div class="wizard-field-row">
      <label>MAC Octet 2</label>
      <input id="wiz-mac2" type="text" maxlength="2" value="${escHtml(mac2)}"
             style="text-transform:uppercase;max-width:80px" spellcheck="false">
      <button class="wizard-gen-btn" title="Generate random" onclick="wizardFillGen('wiz-mac2','mac')">🎲</button>
    </div>
    <div class="wizard-field-row">
      <label>MAC Octet 3</label>
      <input id="wiz-mac3" type="text" maxlength="2" value="${escHtml(mac3)}"
             style="text-transform:uppercase;max-width:80px" spellcheck="false">
      <button class="wizard-gen-btn" title="Generate random" onclick="wizardFillGen('wiz-mac3','mac')">🎲</button>
    </div>

    <div class="wizard-section-title" style="margin-top:18px">Command Characters</div>
    <div class="wizard-section-desc">Advanced — only change these if your system requires non-default characters. All boards must share the same values.</div>
    <div class="wizard-field-row">
      <label>Delimiter</label>
      <input id="wiz-delim" type="text" maxlength="1" value="${escHtml(delimiter)}"
             style="max-width:60px;font-family:monospace" spellcheck="false">
      <span class="wizard-hint">Separates commands&nbsp;(default: <code>^</code>)</span>
    </div>
    <div class="wizard-field-row">
      <label>Function Char</label>
      <input id="wiz-funcchar" type="text" maxlength="1" value="${escHtml(funcChar)}"
             style="max-width:60px;font-family:monospace" spellcheck="false">
      <span class="wizard-hint">Prefixes board commands&nbsp;(default: <code>?</code>)</span>
    </div>
    <div class="wizard-field-row">
      <label>Command Char</label>
      <input id="wiz-cmdchar" type="text" maxlength="1" value="${escHtml(cmdChar)}"
             style="max-width:60px;font-family:monospace" spellcheck="false">
      <span class="wizard-hint">Prefixes routing commands&nbsp;(default: <code>;</code>)</span>
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
          <label>WCB Number</label>
          <input id="wiz-b${i}-wcbnum" type="number" min="1" max="99" value="${b.wcbNumber}" style="max-width:80px">
        </div>
        <div class="wizard-field-row">
          <label>Hardware Version</label>
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
      <img src="../Images/21cropped.jpg" class="wizard-hw-label-svg" alt="WCB PCB label showing version number location">
      <div class="wizard-hw-label-note">Every WCB hardware version has this text printed on the board. The number after <code>VER:</code> is what you need — e.g. <code>VER:2.4</code> → select <strong>2.4</strong>.</div>
    </div>`;

  return `
    <div class="wizard-section-title">Board Identity</div>
    <div class="wizard-section-desc">Assign a unique number and hardware version to each board.</div>
    ${labelSVG}
    ${tabs}${panels}`;
}

function wizardHTMLSerial() {
  const tabs   = wizardBoardTabs('serial');
  const panels = wizardState.boards.map((b, i) => {
    const boardSlot = i + 1;

    // Build a claim map: portNum (1-based) → { owner, baud }
    // Kyber claims its port at 115200; each Maestro claims its port at its configured baud
    const claims = {};
    if (wizardState.kyberEnabled && wizardState.kyberBoard === boardSlot) {
      claims[wizardState.kyberPort] = { owner: 'Kyber', baud: wizardState.kyberBaud };
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
          <thead><tr><th>Port</th><th>Baud Rate</th><th>Label</th></tr></thead>
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
  const { kyberBoard, kyberPort, kyberBaud, quantity } = wizardState;
  const boardOpts = Array.from({length: quantity}, (_, i) =>
    `<option value="${i+1}" ${(i+1) === kyberBoard ? 'selected' : ''}>Board ${i+1} (WCB ${wizardState.boards[i].wcbNumber})</option>`
  ).join('');
  const portOpts = Array.from({length: 5}, (_, p) =>
    `<option value="${p+1}" ${(p+1) === kyberPort ? 'selected' : ''}>Serial ${p+1}</option>`
  ).join('');
  const kyberBaudRates = [9600, 38400, 57600, 115200];
  const baudOpts = kyberBaudRates.map(r =>
    `<option value="${r}" ${r === kyberBaud ? 'selected' : ''}>${r.toLocaleString()}</option>`
  ).join('');
  return `
    <div class="wizard-section-title">Kyber Setup</div>
    <div class="wizard-section-desc">Select which board and serial port the Kyber sound controller is connected to. Maestro targets can be added on the next screen and in the config page.</div>
    <div class="wizard-field-row">
      <label>Local Kyber Board</label>
      <select id="wiz-kyber-board">${boardOpts}</select>
    </div>
    <div class="wizard-field-row">
      <label>Serial Port</label>
      <select id="wiz-kyber-port">${portOpts}</select>
    </div>
    <div class="wizard-field-row">
      <label>Baud Rate</label>
      <select id="wiz-kyber-baud">${baudOpts}</select>
      <span style="font-size:11px;color:var(--text3);margin-left:8px">115,200 for current Kyber · 57,600 for older versions</span>
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
    // Exclude the Kyber port if this maestro is on the Kyber board
    const kyberPortForBoard = (wizardState.kyberEnabled && m.boardSlot === wizardState.kyberBoard)
      ? wizardState.kyberPort : null;
    const portOpts = Array.from({length: 5}, (_, p) => {
      const portNum = p + 1;
      if (portNum === kyberPortForBoard) return '';
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
      <td><select id="wiz-m${mi}-baud">${baudOpts}</select></td>
      <td><button class="btn btn-danger btn-sm btn-icon" onclick="wizardRemoveMaestro(${mi})">🗑</button></td>
    </tr>`;
  }).join('');
  return `
    <div class="wizard-section-title">Maestro Setup</div>
    <div class="wizard-section-desc">Add one row per Maestro. Select its ID, which board it's on, and which serial port it uses.</div>
    <table class="wizard-serial-table" style="margin-bottom:4px">
      <thead><tr><th>#</th><th>ID</th><th>Board</th><th>Port</th><th>Baud</th><th></th></tr></thead>
      <tbody id="wiz-maestro-tbody">${rows}</tbody>
    </table>
    <button class="btn btn-ghost btn-sm" onclick="wizardAddMaestro()">+ Add Maestro</button>`;
}

function wizardHTMLEtm() {
  const { etmEnabled, etmConfig } = wizardState;
  const detailDisplay = etmEnabled ? '' : 'display:none';
  return `
    <div class="wizard-section-title">Ensure Transmission Mode (ETM)</div>
    <div class="wizard-section-desc">ETM improves message reliability by tracking receipt of each transmission and automatically retransmitting if delivery is not acknowledged — so commands always get through.</div>
    <div class="wizard-etm-toggle">
      <input type="checkbox" id="wiz-etm-enabled" ${etmEnabled ? 'checked' : ''}
             onchange="wizardToggleEtmDetail(this.checked)">
      <label for="wiz-etm-enabled">Enable ETM on all boards</label>
    </div>
    <div class="wizard-etm-fields" id="wiz-etm-fields" style="${detailDisplay}">
      <div class="wizard-field-row">
        <label>Timeout (ms)</label>
        <input id="wiz-etm-timeout" type="number" value="${etmConfig.timeoutMs}">
      </div>
      <div class="wizard-field-row">
        <label>Heartbeat (sec)</label>
        <input id="wiz-etm-hb" type="number" value="${etmConfig.heartbeatSec}">
      </div>
      <div class="wizard-field-row">
        <label>Missed before action</label>
        <input id="wiz-etm-miss" type="number" value="${etmConfig.missedHeartbeats}">
      </div>
      <div class="wizard-field-row">
        <label>Boot heartbeat (sec)</label>
        <input id="wiz-etm-boot" type="number" value="${etmConfig.bootHeartbeatSec}">
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
  const eraseDesc = yes
    ? 'Erases all saved settings (passwords, port config, sequences) before flashing — use for a true factory reset.'
    : 'Erases all saved settings before pushing config — boards will reboot to a clean slate before receiving new configuration.';
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
    <div style="margin-top:12px;padding:10px 14px;background:var(--card-bg);border:1px solid var(--border);border-radius:6px">
      <label style="display:flex;align-items:flex-start;gap:10px;cursor:pointer">
        <input type="checkbox" id="wiz-erase-nvs" style="margin-top:2px;cursor:pointer" ${wizardState.eraseNvs ? 'checked' : ''}>
        <div>
          <div style="font-weight:600;font-size:13px">Overwrite existing board memory</div>
          <div style="font-size:11px;color:var(--text2);margin-top:2px">${eraseDesc}</div>
        </div>
      </label>
    </div>`;
}

function wizardFirmwareChoice(val, btn) {
  wizardState.needsFirmware = val;
  // Re-render to update the checkbox description text
  document.getElementById('wizard-body').innerHTML = wizardBuildStepHTML('firmware');
}

function wizardHTMLConnect() {
  const rows = wizardState.boards.map((b, i) => {
    const n   = i + 1;
    const fwBadge = !wizardState.needsFirmware ? ''
      : wizardState.eraseNvs
        ? `<span class="badge badge-red"    style="font-size:9px;padding:2px 6px">Factory Reset</span>`
        : `<span class="badge badge-yellow" style="font-size:9px;padding:2px 6px">Upload FW</span>`;
    return `<div class="wizard-connect-row" id="wiz-connect-row-${n}">
      <span class="wizard-connect-label">
        WCB ${n} ${fwBadge}
        <span style="color:var(--text3);font-weight:400;font-size:11px;display:block">Board #${b.wcbNumber}</span>
      </span>
      <div style="display:flex;gap:6px;flex-shrink:0;align-items:center">
        <span id="wiz-connect-btns-${n}" style="display:flex;gap:6px">
          <button class="btn btn-primary btn-sm" id="wiz-detect-btn-${n}"
                  onclick="wizardAutoDetect(${n})">⊙ Auto-Detect</button>
          <button class="btn btn-ghost btn-sm"   id="wiz-manual-btn-${n}"
                  onclick="wizardManualConnect(${n})">🔌 Manual</button>
        </span>
        <button class="btn btn-ghost btn-sm" id="wiz-cancel-btn-${n}"
                style="display:none" onclick="wizardCancelConnect(${n})">✕ Cancel</button>
      </div>
      <span class="wizard-connect-status" id="wiz-connect-status-${n}">Waiting…</span>
    </div>`;
  }).join('');
  return `
    <div class="wizard-section-title">Connect &amp; Push</div>
    <div class="wizard-section-desc">
      For each board: click <strong>Auto-Detect</strong> (then press the reset button on the board),
      or <strong>Manual</strong> to pick a COM port. The config will push automatically after connecting.
    </div>
    ${rows}`;
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

  // Exclude Kyber port only when this maestro is on the Kyber board
  const kyberPortForBoard = (wizardState.kyberEnabled && newBoardSlot === wizardState.kyberBoard)
    ? wizardState.kyberPort : null;

  portSel.innerHTML = Array.from({length: 5}, (_, p) => {
    const portNum = p + 1;
    if (portNum === kyberPortForBoard) return '';
    return `<option value="${portNum}" ${portNum === curPort ? 'selected' : ''}>Serial ${portNum}</option>`;
  }).join('');

  // If the currently selected port was the excluded Kyber port, bump to first available
  if (curPort === kyberPortForBoard) {
    const firstAvail = portSel.querySelector('option');
    if (firstAvail) portSel.value = firstAvail.value;
  }

  wizardMaestroPortChange(mi); // refresh baud options for the newly selected port
}

function wizardAddMaestro() {
  wizardSaveStep('maestro-config'); // preserve existing rows before adding
  wizardState.maestros.push({ boardSlot: 1, id: wizardState.maestros.length + 1, port: 1, baud: 57600 });
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
      wizardState.kyberBoard = parseInt(get('wiz-kyber-board')?.value ?? 1);
      wizardState.kyberPort  = parseInt(get('wiz-kyber-port')?.value  ?? 2);
      wizardState.kyberBaud  = parseInt(get('wiz-kyber-baud')?.value  ?? 115200);
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
            return `Maestro ID ${m.id} uses Serial ${m.port}, which is already claimed by Kyber on Board ${wizardState.kyberBoard}. Choose a different port.`;
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
        cfg.serialPorts[ws.kyberPort - 1].baud = ws.kyberBaud;
        // Include ALL maestros (local and remote) as Kyber targets so the firmware
        // knows every Maestro it must forward commands to — both those on this same
        // board (via local serial) and those on remote boards (via ESP-NOW).
        // The 'wcb' property name must match what parser.js buildCommandString
        // uses when building M<id>:W<wcb>S<port>:<baud> target strings.
        cfg.kyber.targets = ws.maestros
          .map(m => ({
            id:   m.id,
            wcb:  m.boardSlot,
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

    // Maestro
    const myMaestros = ws.maestros.filter(m => m.boardSlot === (i + 1));
    cfg.maestros = myMaestros.map(m => ({ id: m.id, port: m.port, baud: m.baud }));

    // ETM
    if (ws.etmEnabled) {
      cfg.etm = { enabled: true, ...ws.etmConfig };
    }

    boardConfigs[n] = cfg;
    populateUIFromConfig(n, cfg);
    onBoardFieldChange(n);

    // Note: firmware mode radios are stamped in wizardNext when leaving the firmware step
  });

  // ETM global toggle
  if (ws.etmEnabled) {
    const etmEl = document.getElementById('g-etm-enabled');
    if (etmEl) { etmEl.checked = true; onETMToggle(); }
    setEl('g-etm-timeout', ws.etmConfig.timeoutMs);
    setEl('g-etm-hb',      ws.etmConfig.heartbeatSec);
    setEl('g-etm-miss',    ws.etmConfig.missedHeartbeats);
    setEl('g-etm-boot',    ws.etmConfig.bootHeartbeatSec);
    setEl('g-etm-count',   ws.etmConfig.messageCount);
    setEl('g-etm-delay',   ws.etmConfig.messageDelayMs);
    systemConfig.general.etm = { enabled: true, ...ws.etmConfig };
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
  // Stop auto-detect if it's still running
  _detecting[n] = false;
  // Drop any partial connection
  try { await boardConnections[n]?.disconnect(); } catch (_) {}
  delete boardConnections[n];
  // Reset UI back to the initial waiting state
  wizardEnableConnectBtns(n);
  wizardSetConnectStatus(n, '', 'Waiting…');
}

function wizardAutoDetect(n) {
  wizardDisableConnectBtns(n);
  wizardSetConnectStatus(n, 'busy', 'Detecting… (press reset)');
  boardAutoDetect(n);
  wizardWatchForConnect(n);
}

async function wizardManualConnect(n) {
  wizardDisableConnectBtns(n);
  wizardSetConnectStatus(n, 'busy', 'Select port…');
  try {
    await boardManualConnect(n);
    wizardWatchForConnect(n);
  } catch(e) {
    wizardEnableConnectBtns(n);
    wizardSetConnectStatus(n, '', 'Waiting…');
  }
}

function wizardWatchForConnect(n) {
  // Snapshot the wizard-applied config now, before the boardPull that fires
  // ~500 ms after the board connects can overwrite boardConfigs[n] and the
  // shared general DOM fields (g-password, g-wcbq, etc.).
  const configSnapshot  = boardConfigs[n] ? JSON.parse(JSON.stringify(boardConfigs[n])) : null;
  const generalSnapshot = captureGeneralDOMSnapshot();

  let attempts = 0;
  const iv = setInterval(async () => {
    attempts++;
    if (boardConnections[n]?.isConnected()) {
      clearInterval(iv);
      delete wizardConnectWatchers[n];
      wizardEnableConnectBtns(n);           // hide Cancel, restore Auto/Manual
      wizardSetConnectStatus(n, 'busy', 'Pushing…');

      // Restore wizard config — the initial boardPull may have overwritten it.
      if (configSnapshot) {
        boardConfigs[n]   = JSON.parse(JSON.stringify(configSnapshot));
        boardBaselines[n] = null;   // force a full push (NVS is blank after flash)
        populateUIFromConfig(n, boardConfigs[n]);
      }
      restoreGeneralDOMSnapshot(generalSnapshot);

      try {
        await boardGo(n);
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
}

function wizardStartConnectWatchers() {
  // Auto-start push for already-connected boards
  for (let i = 0; i < wizardState.quantity; i++) {
    const n = i + 1;
    if (boardConnections[n]?.isConnected()) {
      wizardDisableConnectBtns(n);
      wizardSetConnectStatus(n, 'busy', 'Pushing…');
      boardGo(n)
        .then(() => { wizardEnableConnectBtns(n); wizardSetConnectStatus(n, 'ok', '✓ Done'); })
        .catch(() => { wizardEnableConnectBtns(n); wizardSetConnectStatus(n, 'err', '✕ Push failed'); });
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