#!/usr/bin/env node
/**
 * watch-version.js — Dev-time version stamper for WCB Config Tool
 *
 * Watches the Wizard/ directory for file changes and immediately updates
 * the UI_VERSION constant in app.js with the current timestamp.
 *
 * Usage:  node watch-version.js
 *
 * Stop with Ctrl+C.  The pre-commit hook in .git/hooks/pre-commit.sh
 * will also stamp the version at commit time, so both are compatible.
 */

const fs   = require('fs');
const path = require('path');

const APP_JS     = path.join(__dirname, 'app.js');
const WATCH_DIR  = __dirname;   // Wizard/
const EXTENSIONS = new Set(['.js', '.html', '.css']);

// Files that should NOT trigger a self-update loop when app.js is written
const SELF = path.resolve(APP_JS);

let debounceTimer = null;

function formatVersion() {
  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  return `${now.getFullYear()}.${pad(now.getMonth() + 1)}.${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

function stampVersion() {
  let src;
  try { src = fs.readFileSync(APP_JS, 'utf8'); } catch { return; }

  const version = formatVersion();
  const next = src.replace(
    /const UI_VERSION = '[^']*';/,
    `const UI_VERSION = '${version}';`
  );

  if (next === src) {
    console.log(`[watch-version] No UI_VERSION pattern found in app.js`);
    return;
  }

  fs.writeFileSync(APP_JS, next, 'utf8');
  console.log(`[watch-version] UI_VERSION → ${version}`);
}

// Debounce so rapid saves (e.g. editor auto-format) only trigger once
function scheduleStamp() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(stampVersion, 150);
}

console.log(`[watch-version] Watching ${WATCH_DIR} for changes…  (Ctrl+C to stop)`);
stampVersion();   // stamp once immediately on start

fs.watch(WATCH_DIR, { persistent: true }, (event, filename) => {
  if (!filename) return;
  const ext  = path.extname(filename).toLowerCase();
  const full = path.resolve(path.join(WATCH_DIR, filename));

  // Ignore changes to app.js itself to avoid infinite loop
  if (full === SELF) return;
  if (!EXTENSIONS.has(ext)) return;

  scheduleStamp();
});
