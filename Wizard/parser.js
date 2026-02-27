/**
 * WCB Backup Parser & Data Model
 * 
 * Parses the output of the ?backup command from WCB firmware.
 * Builds a structured JavaScript object (BoardConfig) from the
 * chained command string.
 * 
 * The ?backup command outputs two formats:
 *   1. Configured format  — uses board's current delimiter & funcchar
 *   2. Factory reset format — always uses ^ delimiter and ? funcchar
 * 
 * The parser extracts the factory reset format line (always last)
 * since it has a known, stable format regardless of board config.
 */

// ─────────────────────────────────────────────
// Default BoardConfig — represents a fresh/unconfigured board
// All fields set to firmware defaults
// ─────────────────────────────────────────────
function createDefaultBoardConfig() {
  return {
    // Board Identity
    hwVersion:    0,       // 0 = not set, 1=1.0, 21=2.1, 23=2.3, 24=2.4, 31=3.1, 32=3.2
    wcbNumber:    1,
    wcbQuantity:  1,

    // Network
    espnowPassword: 'change_me_or_risk_takeover',
    macOctet2:    '00',
    macOctet3:    '00',

    // Command Characters
    delimiter:    '^',
    funcChar:     '?',
    cmdChar:      ';',

    // Serial Ports — index 0 = Serial1 ... index 4 = Serial5
    serialPorts: [
      { baud: 9600, broadcastIn: true,  broadcastOut: true, label: '', claimedBy: null },
      { baud: 9600, broadcastIn: true,  broadcastOut: true, label: '', claimedBy: null },
      { baud: 9600, broadcastIn: true,  broadcastOut: true, label: '', claimedBy: null },
      { baud: 9600, broadcastIn: true,  broadcastOut: true, label: '', claimedBy: null },
      { baud: 9600, broadcastIn: true,  broadcastOut: true, label: '', claimedBy: null },
    ],

    // Kyber
    kyber: {
      mode: 'none',   // 'none' | 'local' | 'remote'
      port: null,     // 1-5, only relevant for 'local'
    },

    // Maestros — array of { id, port, baud }
    maestros: [],

    // Mappings — array of { type, sourcePort, rawMode, destinations: [{ wcbNumber, port }] }
    mappings: [],

    // ETM
    etm: {
      enabled:          false,
      timeoutMs:        30000,
      heartbeatSec:     10,
      missedHeartbeats: 3,
      bootHeartbeatSec: 30,
      messageCount:     3,
      messageDelayMs:   100,
    },

    // Stored Sequences — array of { key, value }
    sequences: [],

    // PWM Output Ports — array of port numbers that are PWM outputs
    pwmOutputPorts: [],

    // Firmware version (populated from GitHub Releases, not from board)
    fwVersion: null,

    // Source tracking — where did this config come from?
    // 'default' | 'board' | 'file'
    source: 'default',
  };
}

// ─────────────────────────────────────────────
// Default SystemConfig — wraps general + per-board configs
// ─────────────────────────────────────────────
function createDefaultSystemConfig() {
  return {
    // Droid name — stored in file header only, not on board
    droidName: '',

    // General settings — must match across all boards
    general: {
      wcbQuantity:    1,
      espnowPassword: 'change_me_or_risk_takeover',
      macOctet2:      '00',
      macOctet3:      '00',
      delimiter:      '^',
      funcChar:       '?',
      cmdChar:        ';',
      etm: {
        enabled:          false,
        timeoutMs:        30000,
        heartbeatSec:     10,
        missedHeartbeats: 3,
        bootHeartbeatSec: 30,
        messageCount:     3,
        messageDelayMs:   100,
      },
    },

    // Per-board configs — array indexed by board position (not WCB number)
    boards: [],

    // Source tracking
    source: 'default',
    lastModified: null,
  };
}

// ─────────────────────────────────────────────
// Hardware Version Helpers
// ─────────────────────────────────────────────
const HW_VERSION_MAP = {
  '1':  { display: '1.0',  binary: 'ESP32',   value: 1  },
  '21': { display: '2.1',  binary: 'ESP32',   value: 21 },
  '23': { display: '2.3',  binary: 'ESP32',   value: 23 },
  '24': { display: '2.4',  binary: 'ESP32',   value: 24 },
  '31': { display: '3.1',  binary: 'ESP32S3', value: 31 },
  '32': { display: '3.2',  binary: 'ESP32S3', value: 32 },
};

function hwValueToDisplay(value) {
  return HW_VERSION_MAP[String(value)]?.display ?? 'Unknown';
}

function hwValueToBinary(value) {
  return HW_VERSION_MAP[String(value)]?.binary ?? 'ESP32';
}

// ─────────────────────────────────────────────
// Core Backup String Parser
// 
// Accepts either:
//   - The full raw ?backup serial output (multi-line)
//   - Just the chained command string line
//
// Returns a BoardConfig object.
// ─────────────────────────────────────────────
function parseBackupString(rawOutput) {
  const config = createDefaultBoardConfig();
  config.source = 'board';

  // Extract the factory reset command string from the raw output.
  // It's the line after "=== For Factory Reset/Fresh Boards ===" header.
  // If we can't find it, try to parse the whole thing as a raw command string.
  let commandString = extractFactoryResetLine(rawOutput);
  if (!commandString) {
    // Maybe caller passed just a raw command string directly
    commandString = rawOutput.trim();
  }

  if (!commandString) {
    console.warn('[WCB Parser] No command string found in backup output');
    return config;
  }

  // The factory reset format always uses ^ and ? so we can split safely
  const tokens = commandString.split('^');

  for (const token of tokens) {
    const trimmed = token.trim();
    if (!trimmed || !trimmed.startsWith('?')) continue;

    // Strip leading ? to get the command body
    const body = trimmed.substring(1);
    parseToken(body, config);
  }

  // After parsing, evaluate port claims
  evaluatePortClaims(config);

  return config;
}

// ─────────────────────────────────────────────
// Extract the factory reset command line from raw ?backup output
// ─────────────────────────────────────────────
function extractFactoryResetLine(rawOutput) {
  const lines = rawOutput.split(/\r?\n/);

  let foundHeader = false;
  for (const line of lines) {
    const trimmed = line.trim();

    // Look for the factory reset section header
    if (trimmed.includes('For Factory Reset') || trimmed.includes('Fresh Boards')) {
      foundHeader = true;
      continue;
    }

    // The next non-empty, non-comment line after the header is our command string
    if (foundHeader && trimmed.length > 0 && !trimmed.startsWith('***')) {
      return trimmed;
    }
  }

  // Fallback: look for any line that starts with ?HW, which is always first in backup
  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith('?HW,') || trimmed.startsWith('?hw,')) {
      return trimmed;
    }
  }

  return null;
}

// ─────────────────────────────────────────────
// Parse a single command token into the config object
// token is the command body with leading ? already stripped
// e.g. "HW,31" or "BAUD,S1,9600" or "ETM,ON"
// ─────────────────────────────────────────────
function parseToken(body, config) {
  // Normalize to uppercase for matching, but preserve original case for values
  const upper = body.toUpperCase();
  const parts  = body.split(',');
  const upperParts = upper.split(',');
  const cmd = upperParts[0];

  switch (cmd) {

    // ── Board Identity ──
    case 'HW':
      config.hwVersion = parseInt(parts[1]) || 0;
      break;

    case 'WCB':
      // ?WCB,1  (not WCBQ)
      if (upperParts[1] && !upperParts[1].startsWith('Q')) {
        config.wcbNumber = parseInt(parts[1]) || 1;
      }
      break;

    case 'WCBQ':
      config.wcbQuantity = parseInt(parts[1]) || 1;
      break;

    // ── Network ──
    case 'EPASS':
      // Password may contain commas — rejoin everything after EPASS,
      config.espnowPassword = parts.slice(1).join(',');
      break;

    case 'MAC':
      if (upperParts[1] === '2') config.macOctet2 = parts[2]?.toUpperCase() ?? '00';
      if (upperParts[1] === '3') config.macOctet3 = parts[2]?.toUpperCase() ?? '00';
      break;

    // ── Command Characters ──
    case 'DELIM':
      config.delimiter = parts[1] ?? '^';
      break;

    case 'FUNCCHAR':
      config.funcChar = parts[1] ?? '?';
      break;

    case 'CMDCHAR':
      config.cmdChar = parts[1] ?? ';';
      break;

    // ── Serial Baud ──
    case 'BAUD': {
      // ?BAUD,S1,9600
      const portStr = upperParts[1]; // "S1" - "S5"
      const portIdx = parseInt(portStr?.replace('S', '')) - 1;
      if (portIdx >= 0 && portIdx < 5) {
        config.serialPorts[portIdx].baud = parseInt(parts[2]) || 9600;
      }
      break;
    }

    // ── Serial Labels ──
    case 'LABEL': {
      // ?LABEL,S1,MyLabel
      const portStr = upperParts[1];
      const portIdx = parseInt(portStr?.replace('S', '')) - 1;
      if (portIdx >= 0 && portIdx < 5) {
        config.serialPorts[portIdx].label = parts.slice(2).join(',');
      }
      break;
    }

    // ── Broadcast Output ──
    case 'BCAST': {
      // ?BCAST,OUT,S1,ON  or  ?BCAST,IN,S1,OFF
      const direction = upperParts[1]; // 'OUT' or 'IN'
      const portStr   = upperParts[2]; // 'S1' - 'S5'
      const state     = upperParts[3]; // 'ON' or 'OFF'
      const portIdx   = parseInt(portStr?.replace('S', '')) - 1;
      if (portIdx >= 0 && portIdx < 5) {
        const enabled = state === 'ON';
        if (direction === 'OUT') config.serialPorts[portIdx].broadcastOut = enabled;
        if (direction === 'IN')  config.serialPorts[portIdx].broadcastIn  = enabled;
      }
      break;
    }

    // ── Kyber ──
    case 'KYBER': {
      // ?KYBER,LOCAL  or  ?KYBER,REMOTE  or  ?KYBER,CLEAR
      // ?KYBER,LOCAL,S1  (local includes port)
      const mode = upperParts[1];
      if (mode === 'LOCAL') {
        config.kyber.mode = 'local';
        if (upperParts[2]) {
          config.kyber.port = parseInt(upperParts[2].replace('S', '')) || null;
        }
      } else if (mode === 'REMOTE') {
        config.kyber.mode = 'remote';
        config.kyber.port = null;
      } else {
        config.kyber.mode = 'none';
        config.kyber.port = null;
      }
      break;
    }

    // ── Maestro ──
case 'MAESTRO': {
  // Format: ?MAESTRO,M<id>:W<wcb>S<port>:<baud>
  // Chained: ?MAESTRO,M1:W1S2:57600,M2:W2S1:57600
  // Skip control subcommands
  const sub = upperParts[1];
  if (sub === 'LIST' || sub === 'CLEAR' || sub === 'ENABLE' || sub === 'DISABLE') break;

  // Parse all M<id>:W<wcb>S<port>:<baud> entries (parts[1], parts[2], ...)
  for (let i = 1; i < parts.length; i++) {
    const entry = parts[i].toUpperCase();
    const mMatch = entry.match(/^M(\d+):W(\d+)S(\d+):(\d+)$/);
    if (mMatch) {
      config.maestros.push({
        id:   parseInt(mMatch[1]),
        port: parseInt(mMatch[3]),
        baud: parseInt(mMatch[4])
      });
    }
  }
  break;
}

    // ── ETM ──
    case 'ETM': {
      const subCmd = upperParts[1];
      switch (subCmd) {
        case 'ON':      config.etm.enabled          = true;                          break;
        case 'OFF':     config.etm.enabled          = false;                         break;
        case 'TIMEOUT': config.etm.timeoutMs         = parseInt(parts[2]) || 30000;  break;
        case 'HB':      config.etm.heartbeatSec      = parseInt(parts[2]) || 10;     break;
        case 'MISS':    config.etm.missedHeartbeats  = parseInt(parts[2]) || 3;      break;
        case 'BOOT':    config.etm.bootHeartbeatSec  = parseInt(parts[2]) || 30;     break;
        case 'COUNT':   config.etm.messageCount      = parseInt(parts[2]) || 3;      break;
        case 'DELAY':   config.etm.messageDelayMs    = parseInt(parts[2]) || 100;    break;
      }
      break;
    }

    // ── Stored Sequences ──
    case 'SEQ': {
      // ?SEQ,SAVE,key,value  — value may contain commas
      const subCmd = upperParts[1];
      if (subCmd === 'SAVE') {
        const key   = parts[2];
        const value = parts.slice(3).join(',');
        if (key) {
          config.sequences.push({ key, value });
        }
      }
      break;
    }

    // ── PWM Output Port ──
    case 'MAP': {
      const mapType = upperParts[1];

      if (mapType === 'PWM' && upperParts[2] === 'OUT') {
        // ?MAP,PWM,OUT,S3  — declares a port as PWM output
        const portStr = upperParts[3];
        const port    = parseInt(portStr?.replace('S', ''));
        if (port >= 1 && port <= 5) {
          if (!config.pwmOutputPorts.includes(port)) {
            config.pwmOutputPorts.push(port);
          }
        }

      } else if (mapType === 'PWM') {
        // ?MAP,PWM,S1,S2,W2S3  — PWM input mapping with destinations
        parseMappingToken('PWM', parts, upperParts, config);

      } else if (mapType === 'SERIAL') {
        // ?MAP,SERIAL,S1  or  ?MAP,SERIAL,S1,R,S2,W2S3  — serial mapping
        parseMappingToken('SERIAL', parts, upperParts, config);
      }
      break;
    }

    // ── Checksum — skip, handled separately ──
    case 'CHK':
      break;

    default:
      // Unknown token — log for debugging but don't throw
      if (body.length > 0) {
        console.debug('[WCB Parser] Unknown token:', body);
      }
      break;
  }
}

// ─────────────────────────────────────────────
// Parse a MAP token into a mapping entry
// ─────────────────────────────────────────────
function parseMappingToken(type, parts, upperParts, config) {
  // Serial: ?MAP,SERIAL,S1,R,S2,W2S3
  //   parts[2] = source port (S1-S5)
  //   parts[3] = 'R' if raw mode, or first destination
  //   parts[4+] = destinations
  //
  // PWM: ?MAP,PWM,S1,S2,W2S3
  //   parts[2] = source port
  //   parts[3+] = destinations

  const sourcePortStr = upperParts[2];
  const sourcePort    = parseInt(sourcePortStr?.replace('S', ''));
  if (!sourcePort || sourcePort < 1 || sourcePort > 5) return;

  let rawMode    = false;
  let destStart  = 3;

  if (type === 'SERIAL' && upperParts[3] === 'R') {
    rawMode   = true;
    destStart = 4;
  }

  const destinations = [];
  for (let i = destStart; i < parts.length; i++) {
    const dest = parseDestination(parts[i].toUpperCase());
    if (dest) destinations.push(dest);
  }

  config.mappings.push({
    type,
    sourcePort,
    rawMode,
    destinations,
  });
}

// ─────────────────────────────────────────────
// Parse a destination string
// Formats: "S2" (local serial), "W2S3" (remote WCB2 Serial3)
// ─────────────────────────────────────────────
function parseDestination(destStr) {
  if (!destStr) return null;

  // Remote: W2S3 or W2S3
  const remoteMatch = destStr.match(/^W(\d+)S(\d+)$/);
  if (remoteMatch) {
    return {
      wcbNumber: parseInt(remoteMatch[1]),
      port:      parseInt(remoteMatch[2]),
    };
  }

  // Local: S2
  const localMatch = destStr.match(/^S(\d+)$/);
  if (localMatch) {
    return {
      wcbNumber: 0,   // 0 = local board
      port:      parseInt(localMatch[1]),
    };
  }

  console.debug('[WCB Parser] Unknown destination format:', destStr);
  return null;
}

// ─────────────────────────────────────────────
// After all tokens parsed, evaluate port claims
// and set claimedBy on each serial port accordingly
// ─────────────────────────────────────────────
function evaluatePortClaims(config) {
  // Reset all claims first
  for (const port of config.serialPorts) {
    port.claimedBy = null;
  }

  // Kyber claims its port (local mode only)
  if (config.kyber.mode === 'local' && config.kyber.port) {
    const idx = config.kyber.port - 1;
    if (idx >= 0 && idx < 5) {
      config.serialPorts[idx].claimedBy = { type: 'kyber' };
    }
  }

  // Maestros claim their ports
  for (const maestro of config.maestros) {
    const idx = maestro.port - 1;
    if (idx >= 0 && idx < 5) {
      config.serialPorts[idx].claimedBy = { type: 'maestro', id: maestro.id };
    }
  }

  // PWM mappings claim their source ports
  for (const mapping of config.mappings) {
    if (mapping.type === 'PWM') {
      const idx = mapping.sourcePort - 1;
      if (idx >= 0 && idx < 5) {
        config.serialPorts[idx].claimedBy = { type: 'pwm' };
      }
    }
  }

  // PWM output port declarations also claim ports
  for (const port of config.pwmOutputPorts) {
    const idx = port - 1;
    if (idx >= 0 && idx < 5 && !config.serialPorts[idx].claimedBy) {
      config.serialPorts[idx].claimedBy = { type: 'pwm' };
    }
  }
}

// ─────────────────────────────────────────────
// System Config File Parser
// 
// Parses a WCB_system.txt file into a SystemConfig object.
// File format:
//   # comments
//   [GENERAL]
//   command string
//   [WCB1]
//   command string
//   [WCB2]
//   ...
// ─────────────────────────────────────────────
function parseSystemFile(fileContent) {
  const system = createDefaultSystemConfig();
  system.source = 'file';
  system.lastModified = new Date().toISOString();

  const lines = fileContent.split(/\r?\n/);

  // Extract droid name from header comment
  for (const line of lines) {
    const trimmed = line.trim();
    const nameMatch = trimmed.match(/^#\s*Droid:\s*(.+)$/i);
    if (nameMatch) {
      system.droidName = nameMatch[1].trim();
      break;
    }
  }

  // Split into sections
  const sections = {};
  let currentSection = null;
  let currentLines   = [];

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;

    const sectionMatch = trimmed.match(/^\[([^\]]+)\]$/);
    if (sectionMatch) {
      if (currentSection !== null) {
        sections[currentSection] = currentLines.join('\n');
      }
      currentSection = sectionMatch[1].toUpperCase();
      currentLines   = [];
    } else if (currentSection !== null) {
      currentLines.push(trimmed);
    }
  }
  if (currentSection !== null) {
    sections[currentSection] = currentLines.join('\n');
  }

  // Parse [GENERAL] section
  if (sections['GENERAL']) {
    const generalConfig = parseBackupString(sections['GENERAL']);
    system.general.wcbQuantity    = generalConfig.wcbQuantity;
    system.general.espnowPassword = generalConfig.espnowPassword;
    system.general.macOctet2      = generalConfig.macOctet2;
    system.general.macOctet3      = generalConfig.macOctet3;
    system.general.delimiter      = generalConfig.delimiter;
    system.general.funcChar       = generalConfig.funcChar;
    system.general.cmdChar        = generalConfig.cmdChar;
    system.general.etm            = generalConfig.etm;
  }

  // Parse [WCB1], [WCB2], etc.
  for (const [sectionName, content] of Object.entries(sections)) {
    if (sectionName === 'GENERAL') continue;
    const wcbMatch = sectionName.match(/^WCB(\d+)$/);
    if (wcbMatch) {
      const boardConfig = parseBackupString(content);
      boardConfig.source = 'file';
      // Apply general settings to board (general overrides board-level for shared fields)
      applyGeneralToBoard(system.general, boardConfig);
      system.boards.push(boardConfig);
    }
  }

  // Sort boards by WCB number
  system.boards.sort((a, b) => a.wcbNumber - b.wcbNumber);

  // Update quantity to match actual board count if file has more boards
  if (system.boards.length > system.general.wcbQuantity) {
    system.general.wcbQuantity = system.boards.length;
  }

  return system;
}

// ─────────────────────────────────────────────
// Apply general settings to a board config
// General settings take precedence for shared fields
// ─────────────────────────────────────────────
function applyGeneralToBoard(general, board) {
  board.wcbQuantity    = general.wcbQuantity;
  board.espnowPassword = general.espnowPassword;
  board.macOctet2      = general.macOctet2;
  board.macOctet3      = general.macOctet3;
  board.delimiter      = general.delimiter;
  board.funcChar       = general.funcChar;
  board.cmdChar        = general.cmdChar;
  board.etm            = { ...general.etm };
}

// ─────────────────────────────────────────────
// Config to Command String Builder
//
// Converts a BoardConfig back to a chained command string
// that can be sent to the board via WebSerial.
//
// fullPush: if true, include all fields (post-flash)
//           if false, only include fields that differ from baseline
// ─────────────────────────────────────────────
function buildCommandString(config, baseline = null, fullPush = false) {
  const lfi   = config.funcChar   || '?';
  const delim = config.delimiter  || '^';
  const cmds  = [];

  function add(cmd) { cmds.push(lfi + cmd); }
  function changed(key, value) {
    if (fullPush || baseline === null) return true;
    return getNestedValue(baseline, key) !== value;
  }

  // ── Board Identity ──
  if (fullPush || !baseline || baseline.hwVersion !== config.hwVersion)
    add(`HW,${config.hwVersion}`);

  if (fullPush || !baseline || baseline.wcbNumber !== config.wcbNumber)
    add(`WCB,${config.wcbNumber}`);

  if (fullPush || !baseline || baseline.wcbQuantity !== config.wcbQuantity)
    add(`WCBQ,${config.wcbQuantity}`);

  // ── Network ──
  if (fullPush || !baseline || baseline.macOctet2 !== config.macOctet2)
    add(`MAC,2,${config.macOctet2}`);

  if (fullPush || !baseline || baseline.macOctet3 !== config.macOctet3)
    add(`MAC,3,${config.macOctet3}`);

  if (fullPush || !baseline || baseline.espnowPassword !== config.espnowPassword)
    add(`EPASS,${config.espnowPassword}`);

  // ── Command Characters ──
  if (fullPush || !baseline || baseline.delimiter !== config.delimiter)
    add(`DELIM,${config.delimiter}`);

  if (fullPush || !baseline || baseline.funcChar !== config.funcChar)
    add(`FUNCCHAR,${config.funcChar}`);

  if (fullPush || !baseline || baseline.cmdChar !== config.cmdChar)
    add(`CMDCHAR,${config.cmdChar}`);

  // ── Serial Baud Rates ──
  for (let i = 0; i < 5; i++) {
    const cur  = config.serialPorts[i];
    const base = baseline?.serialPorts?.[i];
    if (fullPush || !base || base.baud !== cur.baud)
      add(`BAUD,S${i+1},${cur.baud}`);
  }

  // ── Serial Labels ──
  for (let i = 0; i < 5; i++) {
    const cur  = config.serialPorts[i];
    const base = baseline?.serialPorts?.[i];
    if (cur.label && (fullPush || !base || base.label !== cur.label))
      add(`LABEL,S${i+1},${cur.label}`);
  }

  // ── Broadcast Settings ──
  for (let i = 0; i < 5; i++) {
    const cur  = config.serialPorts[i];
    const base = baseline?.serialPorts?.[i];
    if (fullPush || !base || base.broadcastOut !== cur.broadcastOut)
      add(`BCAST,OUT,S${i+1},${cur.broadcastOut ? 'ON' : 'OFF'}`);
    if (fullPush || !base || base.broadcastIn !== cur.broadcastIn)
      add(`BCAST,IN,S${i+1},${cur.broadcastIn ? 'ON' : 'OFF'}`);
  }

  // ── Kyber ──
  const kyberChanged = fullPush || !baseline ||
    baseline.kyber.mode !== config.kyber.mode ||
    baseline.kyber.port !== config.kyber.port;

  if (kyberChanged) {
    if (config.kyber.mode === 'local')
      add(`KYBER,LOCAL,S${config.kyber.port || 2}`);
    else if (config.kyber.mode === 'remote')
      add('KYBER,REMOTE');
    else
      add('KYBER,CLEAR');
  }

  // ── Maestros ──
  // Format: ?MAESTRO,M<id>:W<wcb>S<port>:<baud>,M<id>:W<wcb>S<port>:<baud>,...
  const maestrosChanged = fullPush || !baseline ||
    JSON.stringify(baseline.maestros) !== JSON.stringify(config.maestros);

  if (maestrosChanged && config.maestros.length > 0) {
    const chain = config.maestros
      .map(m => `M${m.id}:W${config.wcbNumber}S${m.port}:${m.baud}`)
      .join(',');
    add(`MAESTRO,${chain}`);
  } else if (maestrosChanged && config.maestros.length === 0 && baseline?.maestros?.length > 0) {
    add('MAESTRO,CLEAR,ALL');
  }

  // ── ETM ──
  const etm  = config.etm;
  const betm = baseline?.etm;
  if (fullPush || !betm || betm.enabled !== etm.enabled)
    add(etm.enabled ? 'ETM,ON' : 'ETM,OFF');
  if (etm.enabled) {
    if (fullPush || !betm || betm.timeoutMs         !== etm.timeoutMs)         add(`ETM,TIMEOUT,${etm.timeoutMs}`);
    if (fullPush || !betm || betm.heartbeatSec      !== etm.heartbeatSec)      add(`ETM,HB,${etm.heartbeatSec}`);
    if (fullPush || !betm || betm.missedHeartbeats  !== etm.missedHeartbeats)  add(`ETM,MISS,${etm.missedHeartbeats}`);
    if (fullPush || !betm || betm.bootHeartbeatSec  !== etm.bootHeartbeatSec)  add(`ETM,BOOT,${etm.bootHeartbeatSec}`);
    if (fullPush || !betm || betm.messageCount      !== etm.messageCount)      add(`ETM,COUNT,${etm.messageCount}`);
    if (fullPush || !betm || betm.messageDelayMs    !== etm.messageDelayMs)    add(`ETM,DELAY,${etm.messageDelayMs}`);
  }

  // ── PWM Output Ports ──
  const pwmChanged = fullPush || !baseline ||
    JSON.stringify(baseline.pwmOutputPorts) !== JSON.stringify(config.pwmOutputPorts);
  if (pwmChanged) {
    for (const port of config.pwmOutputPorts) {
      add(`MAP,PWM,OUT,S${port}`);
    }
  }

  // ── Mappings ──
  const mappingsChanged = fullPush || !baseline ||
    JSON.stringify(baseline.mappings) !== JSON.stringify(config.mappings);
  if (mappingsChanged) {
    for (const m of config.mappings) {
      let cmd = `MAP,${m.type},S${m.sourcePort}`;
      if (m.type === 'SERIAL' && m.rawMode) cmd += ',R';
      for (const dest of m.destinations) {
        cmd += dest.wcbNumber === 0
          ? `,S${dest.port}`
          : `,W${dest.wcbNumber}S${dest.port}`;
      }
      add(cmd);
    }
  }

  // ── Stored Sequences ──
  const seqChanged = fullPush || !baseline ||
    JSON.stringify(baseline.sequences) !== JSON.stringify(config.sequences);
  if (seqChanged) {
    for (const seq of config.sequences) {
      add(`SEQ,SAVE,${seq.key},${seq.value}`);
    }
  }

  return cmds.join(delim);
}

// ─────────────────────────────────────────────
// System File Builder
//
// Converts a SystemConfig back to WCB_system.txt format
// ─────────────────────────────────────────────
function buildSystemFile(system) {
  const lines = [];
  const date  = new Date().toLocaleDateString('en-US');

  lines.push('# WCB System Config');
  if (system.droidName) lines.push(`# Droid: ${system.droidName}`);
  lines.push(`# Created: ${date}`);
  lines.push('# Tool: WCB Web Config Tool');
  lines.push('');

  // Build a synthetic board config for the general section
  const generalBoard = createDefaultBoardConfig();
  applyGeneralToBoard(system.general, generalBoard);
  const generalCmd = buildCommandString(generalBoard, null, true);

  lines.push('[GENERAL]');
  lines.push(generalCmd);
  lines.push('');

  for (const board of system.boards) {
    lines.push(`[WCB${board.wcbNumber}]`);
    lines.push(buildCommandString(board, null, true));
    lines.push('');
  }

  return lines.join('\n');
}

// ─────────────────────────────────────────────
// Config Diff
//
// Returns an array of field paths that differ between two BoardConfigs.
// Used to highlight yellow fields in the UI.
// ─────────────────────────────────────────────
function diffConfigs(configA, configB) {
  const diffs = [];

  function check(path, a, b) {
    if (JSON.stringify(a) !== JSON.stringify(b)) {
      diffs.push({ path, from: a, to: b });
    }
  }

  check('hwVersion',      configA.hwVersion,      configB.hwVersion);
  check('wcbNumber',      configA.wcbNumber,       configB.wcbNumber);
  check('wcbQuantity',    configA.wcbQuantity,     configB.wcbQuantity);
  check('espnowPassword', configA.espnowPassword,  configB.espnowPassword);
  check('macOctet2',      configA.macOctet2,       configB.macOctet2);
  check('macOctet3',      configA.macOctet3,       configB.macOctet3);
  check('delimiter',      configA.delimiter,       configB.delimiter);
  check('funcChar',       configA.funcChar,        configB.funcChar);
  check('cmdChar',        configA.cmdChar,         configB.cmdChar);
  check('kyber',          configA.kyber,           configB.kyber);
  check('etm',            configA.etm,             configB.etm);
  check('maestros',       configA.maestros,        configB.maestros);
  check('mappings',       configA.mappings,        configB.mappings);
  check('sequences',      configA.sequences,       configB.sequences);
  check('pwmOutputPorts', configA.pwmOutputPorts,  configB.pwmOutputPorts);

  for (let i = 0; i < 5; i++) {
    const pa = configA.serialPorts[i];
    const pb = configB.serialPorts[i];
    check(`serialPorts[${i}].baud`,         pa.baud,         pb.baud);
    check(`serialPorts[${i}].broadcastIn`,  pa.broadcastIn,  pb.broadcastIn);
    check(`serialPorts[${i}].broadcastOut`, pa.broadcastOut, pb.broadcastOut);
    check(`serialPorts[${i}].label`,        pa.label,        pb.label);
  }

  return diffs;
}

// ─────────────────────────────────────────────
// Collect all Maestro definitions from all boards in a system
// Used for topology propagation — push all Maestros to every board
// ─────────────────────────────────────────────
function collectAllMaestros(system) {
  const all = [];
  for (const board of system.boards) {
    for (const m of board.maestros) {
      all.push({ ...m, wcbNumber: board.wcbNumber });
    }
  }
  return all;
}

// ─────────────────────────────────────────────
// Get list of available (unclaimed) ports for a board
// Returns array of port numbers [1-5]
// ─────────────────────────────────────────────
function getAvailablePorts(config) {
  return config.serialPorts
    .map((port, idx) => ({ port: idx + 1, claimed: port.claimedBy !== null }))
    .filter(p => !p.claimed)
    .map(p => p.port);
}

// ─────────────────────────────────────────────
// Utility: deep get nested value by dot-path string
// ─────────────────────────────────────────────
function getNestedValue(obj, path) {
  return path.split('.').reduce((acc, key) => acc?.[key], obj);
}

// ─────────────────────────────────────────────
// Exports — available to app.js and parser.test.js
// ─────────────────────────────────────────────
if (typeof module !== 'undefined' && typeof module.exports !== 'undefined' && typeof window === 'undefined') {
  // Node.js / test environment only (not browser)
  module.exports = {
    createDefaultBoardConfig,
    createDefaultSystemConfig,
    parseBackupString,
    parseSystemFile,
    buildCommandString,
    buildSystemFile,
    diffConfigs,
    collectAllMaestros,
    getAvailablePorts,
    evaluatePortClaims,
    HW_VERSION_MAP,
    hwValueToDisplay,
    hwValueToBinary,
  };
} else {
  // Browser environment — attach to window
  window.WCBParser = {
    createDefaultBoardConfig,
    createDefaultSystemConfig,
    parseBackupString,
    parseSystemFile,
    buildCommandString,
    buildSystemFile,
    diffConfigs,
    collectAllMaestros,
    getAvailablePorts,
    evaluatePortClaims,
    HW_VERSION_MAP,
    hwValueToDisplay,
    hwValueToBinary,
  };
}