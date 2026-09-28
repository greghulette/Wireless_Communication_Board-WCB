// Where the NaviCore (and Intellex) repos are, for serve.js's /NaviCore/ alias, the NaviCore specs and the node
// tests. The config tool is served straight out of Greg's NaviCore checkout, never copied: the specs must test the
// page as it is on disk (docs/hil_plan/NAVICORE.md, D-NC10).
//
// "The sibling repo" is ROOT/../NaviCore in a plain clone and on a CI runner that checks NaviCore out beside this
// repo. A git worktree of this repo sits deeper (<repo>/.claude/worktrees/<name>), so the search walks up from ROOT
// and takes the first ancestor whose NaviCore/config_tool/index.html exists. NAVICORE_REPO / INTELLEX_REPO override
// the search; a path given there that does not hold the tool is an error, not a silent fall-back.
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..', '..', '..');   // this repo's root (tests/wizard/lib/navicore -> 4 up)

function findSibling(name, marker, envVar) {
  const forced = process.env[envVar];
  if (forced) {
    const p = path.resolve(forced);
    if (!fs.existsSync(path.join(p, marker))) {
      throw new Error(`${envVar}=${forced} does not contain ${marker}`);
    }
    return p;
  }
  let dir = ROOT;
  for (let i = 0; i < 8; i++) {
    const parent = path.dirname(dir);
    if (parent === dir) break;
    const cand = path.join(parent, name);
    if (fs.existsSync(path.join(cand, marker))) return cand;
    dir = parent;
  }
  return null;
}

let _nc;
let _ix;
// The NaviCore repo root, or null when it is not on this machine (CI without the second checkout).
function navicoreRoot() {
  if (_nc === undefined) _nc = findSibling('NaviCore', path.join('config_tool', 'index.html'), 'NAVICORE_REPO');
  return _nc;
}
// The Intellex repo root, or null. Only nctool.static reads it (the byte identity of src/webui).
function intellexRoot() {
  if (_ix === undefined) _ix = findSibling('Intellex', path.join('src', 'webui', 'index.html'), 'INTELLEX_REPO');
  return _ix;
}

// The tool's files, by the name the page loads them under.
function toolFile(name) {
  const r = navicoreRoot();
  return r ? path.join(r, 'config_tool', name) : null;
}

module.exports = { ROOT, navicoreRoot, intellexRoot, toolFile };
