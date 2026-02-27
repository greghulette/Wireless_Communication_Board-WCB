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

// ─── Init ─────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  checkBrowserCompat();
  loadThemePreference();
  initSystemConfig();    // creates boards first
  loadModePreference();  // then apply mode (so advanced-only in boards is found)
  setupDropZone();
});

function checkBrowserCompat() {
  if (!('serial' in navigator)) {
    document.getElementById('browser-warning').classList.remove('hidden');
  }
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
}

// ─── Serial Table ─────────────────────────────────────────────────
function renderSerialTable(n) {
  const tbody = document.getElementById(`b${n}-serial-tbody`);
  if (!tbody) return;
  tbody.innerHTML = '';

  for (let p = 1; p <= 5; p++) {
    const baudOptions = BAUD_RATES.map(b =>
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
  const portWrap = document.getElementById(`b${n}-kyber-port-wrap`);
  portWrap.style.display = mode === 'local' ? '' : 'none';

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
  }

  WCBParser.evaluatePortClaims(config);
  updatePortClaimUI(n);
  updateKyberPortDropdown(n);
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
}

function onGeneralMacChange() {
  const m2 = document.getElementById('g-mac2').value.toUpperCase();
  const m3 = document.getElementById('g-mac3').value.toUpperCase();
  systemConfig.general.macOctet2 = m2;
  systemConfig.general.macOctet3 = m3;
  for (const n in boardConfigs) { boardConfigs[n].macOctet2 = m2; boardConfigs[n].macOctet3 = m3; }
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
}

function onETMToggle() {
  const enabled   = document.getElementById('g-etm-enabled').checked;
  const etmDetail = document.getElementById('etm-detail');
  systemConfig.general.etm.enabled = enabled;
  if (appMode === 'advanced' && enabled) etmDetail.classList.add('visible');
  else etmDetail.classList.remove('visible');
  for (const n in boardConfigs) boardConfigs[n].etm.enabled = enabled;
}

// ─── Board Field Handlers ─────────────────────────────────────────
function onBoardFieldChange(n) {
  updateBoardStatusBadge(n, 'unsaved');
}

function onSerialFieldChange(n) {
  syncSerialUIToConfig(n);
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
    document.getElementById(`b${n}-kyber-port-wrap`).style.display = config.kyber.mode === 'local' ? '' : 'none';
    updateKyberPortDropdown(n);
    if (config.kyber.mode === 'local' && config.kyber.port) {
      const portSel = document.getElementById(`b${n}-kyber-port`);
      if (portSel) portSel.value = config.kyber.port;
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
  const badge = document.getElementById(`b${n}-status-badge`);
  if (!badge) return;
  badge.className = 'badge';
  const states = {
    configured: ['badge-green',  '✅ Configured'],
    unsaved:    ['badge-yellow', '⚠ Unsaved'],
    connected:  ['badge-accent', '● Connected'],
    error:      ['badge-red',    '✕ Error'],
    default:    ['badge-default','Not Configured'],
  };
  const [cls, text] = states[state] ?? states.default;
  badge.classList.add(cls);
  badge.textContent = text;
}

// ─── Maestro ──────────────────────────────────────────────────────
function addMaestroRow(n) {
  appendMaestroRow(n, { id: 1, port: null, baud: 57600 });
}

function appendMaestroRow(n, maestro) {
  const tbody = document.getElementById(`b${n}-maestro-tbody`);
  if (!tbody) return;

  const rowNum = tbody.rows.length + 1;
  const rowId  = `maestro-row-${n}-${Date.now()}`;

  const idOptions = [1,2,3,4,5,6,7,8].map(v =>
    `<option value="${v}" ${v === maestro.id ? 'selected' : ''}>${v}</option>`
  ).join('');

  const baudOptions = BAUD_RATES.map(b =>
    `<option value="${b}" ${b === maestro.baud ? 'selected' : ''}>${b.toLocaleString()}</option>`
  ).join('');

  const tr = document.createElement('tr');
  tr.id = rowId;
  tr.innerHTML = `
    <td style="color:var(--text3)">${rowNum}</td>
    <td><select id="${rowId}-id" onchange="onMaestroChange(${n})">${idOptions}</select></td>
    <td><select id="${rowId}-port" onchange="onMaestroChange(${n})">
      <option value="">— Select —</option>
    </select></td>
    <td><select id="${rowId}-baud" onchange="onMaestroChange(${n})">${baudOptions}</select></td>
    <td><button class="btn btn-danger btn-sm btn-icon" onclick="removeMaestroRow(${n},'${rowId}')">🗑</button></td>
  `;
  tbody.appendChild(tr);

  // Populate port dropdown with available ports
  refreshMaestroPortDropdown(n, rowId, maestro.port);
}

function refreshMaestroPortDropdown(n, rowId, selectedPort) {
  const portSel = document.getElementById(`${rowId}-port`);
  if (!portSel) return;
  const config = boardConfigs[n];

  portSel.innerHTML = '<option value="">— Select —</option>';
  for (let p = 1; p <= 5; p++) {
    const claim = config?.serialPorts?.[p - 1]?.claimedBy;
    // Available = unclaimed OR currently claimed by this maestro row's own assignment
    const currentlyMine = claim?.type === 'maestro' && portSel.dataset.lastPort == p;
    if (!claim || currentlyMine) {
      const opt = document.createElement('option');
      opt.value = p;
      opt.textContent = `Serial ${p}`;
      if (p === selectedPort) opt.selected = true;
      portSel.appendChild(opt);
    }
  }
}

function onMaestroChange(n) {
  syncMaestrosToConfig(n);
  onBoardFieldChange(n);
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
  for (const m of config.maestros) appendMaestroRow(n, m);
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
  const wcbQty = parseInt(document.getElementById('g-wcbq')?.value) || 8;
  const mapType = document.getElementById(`${rowId}-type`)?.value ?? 'Serial';
  const isSerial = mapType === 'Serial';

  const wcbOptions = `<option value="0">Local</option>` +
    Array.from({length: wcbQty}, (_,i) =>
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
function addSequenceRow(n) { appendSequenceRow(n, '', ''); }

function appendSequenceRow(n, key, value) {
  const tbody = document.getElementById(`b${n}-seq-tbody`);
  if (!tbody) return;
  const rowId = `seq-row-${n}-${Date.now()}-${Math.random().toString(36).slice(2,6)}`;
  const tr = document.createElement('tr');
  tr.id = rowId;
  tr.innerHTML = `
    <td><input class="seq-key-input" type="text" value="${escHtml(key)}" placeholder="KeyName" spellcheck="false"></td>
    <td><input class="seq-val-input" type="text" value="${escHtml(value)}" placeholder="Command string…" spellcheck="false"></td>
    <td style="white-space:nowrap">
      <button class="btn btn-ghost btn-sm btn-icon" title="Play" onclick="playSequence(${n},'${rowId}')" id="${rowId}-play">▶</button>
      <button class="btn btn-danger btn-sm btn-icon" title="Delete" onclick="document.getElementById('${rowId}').remove()">🗑</button>
    </td>
  `;
  tbody.appendChild(tr);
  updateSequencePlayButtons(n);
}

function updateSequencePlayButtons(n) {
  const connected = boardConnections[n]?.isConnected() ?? false;
  document.querySelectorAll(`[id^="seq-row-${n}-"] [title="Play"]`).forEach(btn => {
    btn.disabled = !connected;
  });
}

function playSequence(n, rowId) {
  const row = document.getElementById(rowId);
  if (!row) return;
  const key = row.querySelector('.seq-key-input')?.value?.trim();
  if (!key) { showToast('Sequence key is empty', 'error'); return; }
  const conn = boardConnections[n];
  if (!conn?.isConnected()) { showToast('Board not connected', 'error'); return; }
  const cmdChar = boardConfigs[n]?.cmdChar ?? ';';
  const cmd = `${cmdChar}SEQ,${key}`;
  conn.send(cmd + '\r');
  termLog(n, cmd, 'in');
  showToast(`Sent: ${cmd}`, 'info');
}

function getSequencesFromUI(n) {
  const sequences = [];
  document.getElementById(`b${n}-seq-tbody`)?.querySelectorAll('tr').forEach(row => {
    const key = row.querySelector('.seq-key-input')?.value?.trim();
    const val = row.querySelector('.seq-val-input')?.value?.trim();
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
  }

  isConnected() { return this._connected; }

  async connect() {
    if (!('serial' in navigator)) throw new Error('WebSerial not supported in this browser');
    this.port = await navigator.serial.requestPort();
    await this.port.open({ baudRate: 115200 });
    this._connected = true;
    this._startReading();
  }

  async disconnect() {
    try {
      if (this.reader) { await this.reader.cancel(); this.reader = null; }
      if (this.port?.readable?.locked === false) await this.port.close();
    } catch (_) {}
    this._connected = false;
    this.port = null;
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
    while (this.port?.readable) {
      this.reader = this.port.readable.getReader();
      try {
        while (true) {
          const { value, done } = await this.reader.read();
          if (done) break;
          this._readBuffer += new TextDecoder().decode(value);
          let nl;
          while ((nl = this._readBuffer.indexOf('\n')) !== -1) {
            const line = this._readBuffer.slice(0, nl).replace(/\r$/, '').trim();
            this._readBuffer = this._readBuffer.slice(nl + 1);
            if (line) {
              this._dataCallbacks.forEach(cb => cb(line));
              termLog(this.boardIndex, line, 'out');
            }
          }
        }
      } catch (e) {
        if (e.name !== 'CancelledError') {
          this._connected = false;
          updateConnectionUI(this.boardIndex, false);
          termLog(this.boardIndex, `Disconnected: ${e.message}`, 'err');
        }
      } finally {
        try { this.reader?.releaseLock(); } catch (_) {}
        this.reader = null;
      }
    }
  }
}

// ─── Board Actions ────────────────────────────────────────────────
async function boardConnect(n) {
  const btn = document.getElementById(`b${n}-btn-connect`);
  btn.disabled = true;

  try {
    if (boardConnections[n]?.isConnected()) {
      await boardConnections[n].disconnect();
      updateConnectionUI(n, false);
      btn.textContent = 'Connect';
      btn.disabled = false;
      return;
    }

    btn.textContent = 'Connecting…';
    const conn = new BoardConnection(n);
    await conn.connect();
    boardConnections[n] = conn;
    updateConnectionUI(n, true);
    showToast(`Connected to WCB ${n}`, 'success');
  } catch (e) {
    if (e.name !== 'NotFoundError') showToast(`Connection failed: ${e.message}`, 'error');
    btn.textContent = 'Connect';
  }
  btn.disabled = false;
}

async function boardPull(n) {
  const conn = boardConnections[n];
  if (!conn?.isConnected()) { showToast('Board not connected', 'error'); return; }

  const btn = document.getElementById(`b${n}-btn-pull`);
  btn.disabled = true;
  btn.textContent = 'Pulling…';
  termLog(n, '?backup', 'in');

  try {
    const raw    = await conn.sendAndCollect('?backup', 8000);
    const config = WCBParser.parseBackupString(raw);

    boardConfigs[n]   = config;
    boardBaselines[n] = JSON.parse(JSON.stringify(config));
    populateUIFromConfig(n, config);

    // Sync general fields from first board pull
    syncGeneralFromConfig(config);
    if (config.wcbQuantity > 1) renderBoards(config.wcbQuantity);

    updateBoardStatusBadge(n, 'connected');
    showToast(`Config pulled from WCB ${n}`, 'success');
  } catch (e) {
    showToast(`Pull failed: ${e.message}`, 'error');
    termLog(n, `Pull error: ${e.message}`, 'err');
  }

  btn.textContent = 'Pull Config';
  btn.disabled = false;
}

async function boardGo(n) {
  const conn = boardConnections[n];
  if (!conn?.isConnected()) { showToast('Board not connected', 'error'); return; }

  const mode    = document.querySelector(`input[name="b${n}-mode"]:checked`)?.value;
  const isFlash = mode === 'flash';
  const btn     = document.getElementById(`b${n}-btn-go`);

  btn.disabled = true;
  btn.textContent = isFlash ? 'Flashing…' : 'Pushing…';

  if (isFlash) {
    showToast('Flash integration coming in Phase 2 — use Configure Only for now', 'info');
    btn.disabled = false;
    btn.textContent = '▶ Go';
    return;
  }

  try {
    // Collect current UI state into config
    syncSerialUIToConfig(n);
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
      btn.textContent = '▶ Go';
      return;
    }

    // Send each command individually with small gap
    const commands = cmdString.split(config.delimiter).filter(c => c.trim());
    for (const cmd of commands) {
      const t = cmd.trim();
      if (!t) continue;
      await conn.send(t + '\r');
      termLog(n, t, 'in');
      await sleep(80);
    }

    // Reboot
    await sleep(300);
    await conn.send('?reboot\r');
    termLog(n, '?reboot', 'in');

    updateBoardStatusBadge(n, 'configured');
    showToast(`WCB ${n} configured — rebooting`, 'success');

    setTimeout(async () => {
      await boardConnections[n]?.disconnect();
      updateConnectionUI(n, false);
    }, 2500);

  } catch (e) {
    showToast(`Push failed: ${e.message}`, 'error');
    termLog(n, `Push error: ${e.message}`, 'err');
  }

  btn.disabled = false;
  btn.textContent = '▶ Go';
}

function updateConnectionUI(n, connected) {
  document.getElementById(`b${n}-dot`)?.classList.toggle('connected', connected);
  const label = document.getElementById(`b${n}-conn-label`);
  if (label) label.textContent = connected ? 'Connected' : 'Not connected';
  const pullBtn = document.getElementById(`b${n}-btn-pull`);
  const goBtn   = document.getElementById(`b${n}-btn-go`);
  const connBtn = document.getElementById(`b${n}-btn-connect`);
  if (pullBtn) pullBtn.disabled = !connected;
  if (goBtn)   goBtn.disabled   = !connected;
  if (connBtn) connBtn.textContent = connected ? 'Disconnect' : 'Connect';
  updateBoardStatusBadge(n, connected ? 'connected' : 'default');
  updateSequencePlayButtons(n);
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

// ─── File Import ──────────────────────────────────────────────────
function setupDropZone() {
  const zone = document.getElementById('drop-zone');
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('drag-over');
    const file = e.dataTransfer.files[0];
    if (file) readFile(file);
  });
}

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
function toggleTerminal() {
  document.getElementById('terminal-drawer').classList.toggle('open');
}

function clearTerminal() {
  document.getElementById('terminal-output').innerHTML = '';
}

function termLog(boardIndex, text, type = 'out') {
  const output = document.getElementById('terminal-output');
  if (!output) return;
  const line = document.createElement('div');
  line.className = `terminal-line-${type}`;
  line.textContent = (boardIndex > 0 ? `[WCB${boardIndex}] ` : '') + text;
  output.appendChild(line);
  output.scrollTop = output.scrollHeight;
}

function onTerminalKeydown(e) {
  if (e.key === 'Enter') sendTerminalCommand();
}

async function sendTerminalCommand() {
  const input = document.getElementById('terminal-input');
  const cmd   = input.value.trim();
  if (!cmd) return;
  input.value = '';

  // Find first connected board
  const conn = Object.values(boardConnections).find(c => c.isConnected());
  if (!conn) { termLog(0, 'No board connected', 'err'); return; }

  termLog(conn.boardIndex, cmd, 'in');
  try { await conn.send(cmd + '\r'); }
  catch (e) { termLog(0, `Send error: ${e.message}`, 'err'); }
}

// ─── Toast Notifications ──────────────────────────────────────────
function showToast(message, type = 'info', duration = 3500) {
  const container = document.getElementById('toast-container');
  const toast     = document.createElement('div');
  toast.className = `toast ${type}`;
  const icon = type === 'success' ? '✅' : type === 'error' ? '❌' : 'ℹ';
  toast.innerHTML = `<span>${icon}</span><span>${escHtml(message)}</span>`;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), duration);
}

// ─── Utilities ────────────────────────────────────────────────────
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function escHtml(str) {
  return String(str)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}