// Wizard/watch-version.js, the dev-time stamper - no browser, no board. `node --test` (npm run unit), and wizard.parser in
// the harness. It runs against a COPY of the Wizard's app.js in a temp folder (the script works on the folder it sits
// in), so the real app.js is never written.
//
// What it must do: stamp `const UI_VERSION = '...'` with the local time as DD.HH:MM.R.MON.YYYY when it starts and again
// after any .js/.html/.css change in the folder (debounced), never for its own write to app.js (that would loop), not for
// other files, and say so when app.js has no UI_VERSION line. A change within the same minute as the last stamp leaves
// the value as it is, and said "No UI_VERSION pattern found" until 2026-10-13; it now says the value is current. The
// page shows the constant in its footer (wizard.init_fake_boot_steps). Re-scan row version.watch_script (docs/hil_plan/rescan_2026-10-12/wizard.json).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const WIZARD = path.join(__dirname, '..', '..', '..', 'Wizard');
const RE = /const UI_VERSION = '([^']*)';/;
const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
const stamp = (d) => {
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getDate())}.${p(d.getHours())}:${p(d.getMinutes())}.R.${MONTHS[d.getMonth()]}.${d.getFullYear()}`;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Start the stamper in a fresh folder holding a copy of watch-version.js and the given app.js text. Returns the folder,
// the child, and a function counting its "UI_VERSION →" lines.
function start(appJs) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wcb-watch-version-'));
  fs.copyFileSync(path.join(WIZARD, 'watch-version.js'), path.join(dir, 'watch-version.js'));
  fs.writeFileSync(path.join(dir, 'app.js'), appJs);
  const child = spawn(process.execPath, [path.join(dir, 'watch-version.js')], { cwd: dir, stdio: ['ignore', 'pipe', 'pipe'] });
  let out = '';
  child.stdout.on('data', (b) => { out += b; });
  child.stderr.on('data', (b) => { out += b; });
  // A stamp: written, or the same minute's value already there.
  const stamps = () => out.split('\n').filter((l) => /UI_VERSION (→|already )/.test(l)).length;
  const until = async (fn, ms = 5000) => {
    const t0 = Date.now();
    while (!fn()) {
      if (Date.now() - t0 > ms) throw new Error(`timed out; output so far:\n${out}`);
      await sleep(25);
    }
  };
  const stop = () => { child.kill(); fs.rmSync(dir, { recursive: true, force: true }); };
  return { dir, child, stamps, until, out: () => out, stop };
}

test('stamps app.js\'s UI_VERSION with the local time on start and again after a Wizard .js/.html/.css change, but not after its own write or another kind of file', async () => {
  const real = fs.readFileSync(path.join(WIZARD, 'app.js'), 'utf8');
  assert.match(real, RE, 'the Wizard\'s app.js has the line the stamper rewrites');
  const w = start(real.replace(RE, "const UI_VERSION = '01.00:00.R.JAN.2000';"));
  try {
    const t0 = new Date();
    await w.until(() => w.stamps() === 1);
    const t1 = new Date();
    const v = fs.readFileSync(path.join(w.dir, 'app.js'), 'utf8').match(RE)[1];
    assert.ok([stamp(t0), stamp(t1)].includes(v), `stamped ${v}, expected ${stamp(t0)}`);
    assert.match(v, /^\d\d\.\d\d:\d\d\.R\.[A-Z]{3}\.\d{4}$/);
    // Only that line changed.
    assert.equal(fs.readFileSync(path.join(w.dir, 'app.js'), 'utf8').replace(RE, 'X'), real.replace(RE, 'X'));

    // Its own write (and a .txt) start nothing; give the 150 ms debounce room to show it.
    await sleep(600);
    fs.writeFileSync(path.join(w.dir, 'notes.txt'), 'hil');
    await sleep(600);
    assert.equal(w.stamps(), 1, 'no stamp for app.js\'s own write or a .txt');

    // A change to another Wizard file: stamped again, once for a burst of saves (debounced).
    for (const f of ['parser.js', 'index.html', 'styles.css']) fs.writeFileSync(path.join(w.dir, f), `/* ${f} */`);
    await w.until(() => w.stamps() === 2);
    await sleep(600);
    assert.equal(w.stamps(), 2, 'one stamp for a burst of saves, and none for the stamp\'s own write');
    assert.ok(!w.out().includes('No UI_VERSION pattern found'), 'the line is there: never reported missing');
    assert.match(fs.readFileSync(path.join(w.dir, 'app.js'), 'utf8').match(RE)[1], /^\d\d\.\d\d:\d\d\.R\.[A-Z]{3}\.\d{4}$/);
  } finally {
    w.stop();
  }
});

test('says so, and writes nothing, when app.js has no UI_VERSION line', async () => {
  const w = start('// no version here\n');
  try {
    await w.until(() => w.out().includes('No UI_VERSION pattern found in app.js'));
    assert.equal(fs.readFileSync(path.join(w.dir, 'app.js'), 'utf8'), '// no version here\n');
  } finally {
    w.stop();
  }
});
