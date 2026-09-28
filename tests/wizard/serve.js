// Static file server for the tests: the repo root on 127.0.0.1:8778, so /Wizard/index.html can also load
// ../Images/*. Node's own http+fs, so it needs no dependency and behaves the same on Windows and on a CI runner
// (`python` is not guaranteed there). Playwright starts it — see playwright.config.js webServer.
//
// The ORIGIN must never change: Chrome stores each board's Web Serial grant per origin, so a different host or
// port would make every profile in .profiles/ need authorizing again.
//
// /NaviCore/... is an alias for the sibling NaviCore repo (lib/navicore/paths.js finds it), so the NaviCore config
// tool loads from /NaviCore/config_tool/index.html with its relative cmdlib/ fetches working, and shares an origin
// with /Wizard/ (the shared-hub specs need both tabs on one origin: Web Locks and BroadcastChannel are per origin).
// The NaviCore specs use their own origin, 127.0.0.1:8779 (a second copy of this server), so they never touch the
// Wizard's 8778 grants. A missing NaviCore repo is a 404 naming the fix, and the NaviCore specs skip.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { navicoreRoot } = require('./lib/navicore/paths');

const ROOT = path.join(__dirname, '..', '..');
const PORT = Number(process.argv[2] || 8778);
const NC_PREFIX = '/NaviCore/';

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8',
  '.wasm': 'application/wasm',
};

http.createServer((req, res) => {
  const url = decodeURIComponent((req.url || '/').split('?')[0]);
  let base = ROOT;
  let rel = url;
  if (url === '/NaviCore' || url.startsWith(NC_PREFIX)) {
    base = navicoreRoot();
    if (!base) {
      res.writeHead(404, { 'Content-Type': 'text/plain' })
        .end('NaviCore repo not found beside this one — clone it next to the WCB repo or set NAVICORE_REPO');
      return;
    }
    rel = url.slice('/NaviCore'.length) || '/';
  }
  const file = path.join(base, rel.endsWith('/') ? path.join(rel, 'index.html') : rel);
  // Never serve outside the repo (or the NaviCore repo, for the alias), whatever the request says.
  const abs = path.resolve(file);
  const top = path.resolve(base);
  if (abs !== top && !abs.startsWith(top + path.sep)) {
    res.writeHead(403).end('forbidden');
    return;
  }
  fs.readFile(file, (err, data) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain' }).end('not found');
      return;
    }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream' });
    res.end(data);
  });
}).listen(PORT, '127.0.0.1', () => console.log(`serving ${ROOT} on http://127.0.0.1:${PORT}`));
