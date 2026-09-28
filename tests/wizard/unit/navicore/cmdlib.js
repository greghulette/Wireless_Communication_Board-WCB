// The config tool's command library as the page builds it at startup (NC_CMDLIB_SEED, then the vendored
// cmdlib/droidnet and cmdlib/navicore manifests through _cmdlibMergeLibraryData, then _cmdlibNormalize), built in a
// node sandbox from the page's own functions, plus the codec (ncEncodeCommand / ncDecodeCommand) bound to it.
const fs = require('node:fs');
const path = require('node:path');
const { toolFile } = require('../../lib/navicore/paths');
const { sandbox } = require('./extract');

const NAMES = [
  'NC_CMDLIB_SEED', 'NC_SEQ_SOURCE', 'NC_SEQ_SOURCE_CMDS', 'NC_CMDLIB_HIDDEN', 'NC_CMDLIB_HIDDEN_CMDS',
  'NC_CMDLIB_SUPERSEDE', 'NC_CMDLIB_ORDER_TOP', 'NC_CMDLIB_ORDER_BOTTOM', 'NC_CMDLIB_MOVED_CMDS',
  '_cmdlibBoardRank', '_cmdlibApplySeqSource', '_cmdlibDropMovedCmds', '_cmdlibNormalize', '_cmdlibMergeLibraryData',
  '_cmdlibGeneration', 'ncCommandLibrary', '_cmdlibEnum', '_reEscape', '_ncCommandPattern', 'ncEncodeCommand',
  'ncDecodeCommand',
];

function loadManifest(rel) {
  const file = toolFile(rel);
  const manifest = JSON.parse(fs.readFileSync(file, 'utf8'));
  const dir = path.dirname(file);
  return manifest.boards.filter((b) => b && b.file).map((b) => JSON.parse(fs.readFileSync(path.join(dir, b.file), 'utf8')));
}

function buildLibrary() {
  const window = {};
  const sb = sandbox(NAMES, { window });
  for (const rel of ['cmdlib/droidnet/manifest.json', 'cmdlib/navicore/manifest.json']) {
    for (const data of loadManifest(rel)) sb._cmdlibMergeLibraryData(data);
    sb._cmdlibNormalize();
  }
  return { lib: window.NC_COMMAND_LIBRARY, ...sb };
}

module.exports = { buildLibrary };
