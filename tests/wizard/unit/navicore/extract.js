// Lift named top-level declarations out of the NaviCore config tool's inline <script> into a vm sandbox, so the node
// tests (nctool.unit) run the page's own pure functions without a browser. The tool has no module like the Wizard's
// parser.js; this is the seam (docs/hil_plan/NAVICORE.md §5.4, L0 item 7).
//
// A declaration's end is found by the real JS parser, not by counting braces: the text from its start to each
// candidate end ('}' for a function, ';' or a newline for a const/let) is compiled with vm.Script, and the first that
// compiles is the declaration. A '}' inside a string, a regex or a comment cannot close a function early, because
// the prefix ending there does not compile. A name that is not found throws, naming it: a rename in the tool must
// fail these tests, never silently skip them.
const fs = require('node:fs');
const vm = require('node:vm');
const { toolFile } = require('../../lib/navicore/paths');

let _html = null;
function toolHtml() {
  if (_html === null) {
    const f = toolFile('index.html');
    _html = f ? fs.readFileSync(f, 'utf8') : '';
  }
  return _html;
}

// Every inline <script> (no src=) of the page: { text, line } with the 1-based line its body starts on.
function inlineScripts(html = toolHtml()) {
  const out = [];
  const re = /<script(\s[^>]*)?>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(html))) {
    if (m[1] && /\bsrc\s*=/.test(m[1])) continue;
    const bodyAt = m.index + m[0].indexOf('>') + 1;
    out.push({ text: m[2], line: html.slice(0, bodyAt).split('\n').length });
  }
  return out;
}

const compiles = (code) => { try { new vm.Script(code); return true; } catch (_) { return false; } };
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// The source text of top-level declaration `name` in the page's main script.
function lift(name, src = inlineScripts()[0]?.text || '') {
  const fn = new RegExp(`^(?:async\\s+)?function\\s+${esc(name)}\\s*\\(`, 'm').exec(src);
  const vr = new RegExp(`^(?:const|let|var)\\s+${esc(name)}\\s*=`, 'm').exec(src);
  const m = fn || vr;
  if (!m) throw new Error(`${name}: no top-level declaration of it in the script (config_tool/index.html unless named) — renamed?`);
  const start = m.index;
  // A const ends at a ';' (a newline only as a last resort: `const x = a` compiles on its own even when the
  // statement goes on with `+ b` on the next line).
  for (const enders of fn ? [/\}/g] : [/;/g, /\n/g]) {
    enders.lastIndex = start + m[0].length;
    let e;
    while ((e = enders.exec(src))) {
      const code = src.slice(start, e.index + 1);
      if (compiles(code)) return code;
    }
  }
  throw new Error(`${name}: found at offset ${start} but no end of it compiles`);
}

// Run the named declarations in one sandbox and return them. `globals` seeds the sandbox (window, document, a
// TextEncoder ...); code that a lifted function calls but that was not lifted is a ReferenceError at call time,
// which names what else to lift. `src` is the tool's main script unless given (the Wizard's app.js, for parity).
function sandbox(names, globals = {}, src = inlineScripts()[0]?.text || '') {
  const code = names.map((n) => lift(n, src)).join('\n\n') + `\n;({ ${names.join(', ')} })`;
  const ctx = vm.createContext({ TextEncoder, TextDecoder, console, JSON, Math, Number, String, Array, Object,
                                 RegExp, Set, Map, Error, ...globals });
  return vm.runInContext(code, ctx, { filename: 'config_tool/index.html (lifted)' });
}

module.exports = { toolHtml, inlineScripts, lift, sandbox, compiles };
