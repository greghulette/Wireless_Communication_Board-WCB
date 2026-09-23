// Static file server for the tests: the repo root on 127.0.0.1:8778, so /Wizard/index.html can also load
// ../Images/*. Node's own http+fs, so it needs no dependency and behaves the same on Windows and on a CI runner
// (`python` is not guaranteed there). Playwright starts it — see playwright.config.js webServer.
//
// The ORIGIN must never change: Chrome stores each board's Web Serial grant per origin, so a different host or
// port would make every profile in .profiles/ need authorizing again.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', '..');
const PORT = Number(process.argv[2] || 8778);

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
  const file = path.join(ROOT, url.endsWith('/') ? path.join(url, 'index.html') : url);
  // Never serve outside the repo, whatever the request says.
  if (!path.resolve(file).startsWith(path.resolve(ROOT))) {
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
