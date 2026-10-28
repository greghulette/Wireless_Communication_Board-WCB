"""Intellex's two browser-tool checks that need GitHub Pages - the launcher's version check and the tool update - under
its venv, against a local stand-in for Pages. Nothing reaches the network:

- _Pages is a threaded HTTP server on 127.0.0.1:<free> laid out like gh-pages (/NaviCore/config_tool/, /NaviCore/Images/,
  /Wireless_Communication_Board-WCB/Wizard/, .../Images/ and .../dev/<branch>/...), answering only the files a case gives
  it (anything else is a 404) and recording every path asked for.
- _github_is(pages) points the code at it the way host_units.py tool_base does: urllib.request.urlopen answers
  https://greghulette.github.io/<path> with a real request to <stand-in>/<path>, keeping the code's own timeout and TLS
  context; a 127.0.0.1 URL (fetch_webui --base, this script's requests to the in-process host) goes through as it is;
  every other URL is refused and fails the case. flash.reachable and fetch_webui.reachable answer True for the block.
  INTELLEX_OFFLINE stays set (run_intellex_py), so anything left unpatched fails offline rather than going out.
- The certificate case serves HTTPS with a self-signed certificate made here (cryptography, which esptool brings into
  Intellex's venv), so the venv's real TLS verification fails as it does on a stock macOS Python.

group "webui_version" (intellex.webui_version_online, IX re-scan host.api.webui_version): Intellex src/host.py
api_webui_version, _published_dtg and _wcb_published_ver on its real build_app() in this process (bridge_units._Server).
A bundle older than the published copy is stale with checked true and no reason; an equal one is not stale; one whose
page loads files the copy lacks is stale whatever its stamp, `missing` naming them (Intellex 0.2.3, src/webfiles.py
missing_from); each tool asks its configured branch's copy (settings.tool_base: <site>/config_tool or
<site>/dev/<branch>/<leaf>), never main's for a branch; a failure says why - certs.ADVICE for a certificate the venv
cannot verify, the HTTP error otherwise - without hiding the other tool's answer.

group "fetch_webui" (intellex.fetch_webui_unit, IX re-scan tools.fetch_webui): Intellex tools/fetch_webui.py in this
process, its main() with each argv. _safe_relname (a pure unit); cmdlib_names' walk of each vendor manifest; each tool's
update as one atomic swap (a staging directory in tempfile's directory, then a rename that keeps the old bundle as
<dir>.prev), byte-identical to what was served; up to date, --check and --force; one missing file changes nothing and
leaves no staging directory - but a Wizard vendor/ file is a line, not a failure (Intellex 0.2.2: Intellex never loads
it); the Wizard also takes what its index.html loads beyond WCB_FILES, the cmdlib/ vendors its code names and the
images those name, and a same-stamp copy lacking one is fetched whole again (0.2.2-0.2.3); the branch's /dev/<branch>/
copy when it is published, else main's with a note; --tool all as two independent swaps.

group "tools_auto" (intellex.tools_auto_update_unit, Intellex 0.2.7): host._auto_update_tools_once - the pass the host
runs by itself 30 s after start and every 5 min - with the real fetch_webui run in this process in place of the
subprocess (host.proc.run), against the stand-in: what is fetched, what is not, the /_api/status token, autoUpdated.
"""
import contextlib
import datetime
import http.server
import io
import ipaddress
import json
import os
import shutil
import ssl
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import SkipCase, case, check  # noqa: E402

PAGES = "https://greghulette.github.io"
NC = "/NaviCore"
WCB = "/Wireless_Communication_Board-WCB"


# ------------------------------------------------------------------ the stand-in for Pages
class _Pages:
    """gh-pages on 127.0.0.1:<free>: files {server path: bytes}, a 404 for anything else, every path asked recorded.
    tls: HTTPS with a self-signed certificate (certdir holds it)."""

    def __init__(self, files=None, tls=False, certdir=None):
        self.files, self.asked, self.github = dict(files or {}), [], []
        self.lock = threading.Lock()
        srv = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                with srv.lock:
                    srv.asked.append(path)
                    body = srv.files.get(path)
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "9")
                    self.end_headers()
                    self.wfile.write(b"Not Found")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        scheme = "http"
        if tls:
            cert, key = _self_signed(certdir)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
            scheme = "https"
        self.url = f"{scheme}://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def serve(self, files):
        with self.lock:
            self.files = dict(files)

    def mark(self):
        with self.lock:
            return len(self.asked)

    def since(self, k):
        with self.lock:
            return list(self.asked[k:])

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _self_signed(d):
    """A self-signed certificate for greghulette.github.io and 127.0.0.1, made now -> (cert path, key path). Not issued
    by any CA the venv trusts, so a client that verifies refuses it - the failure a stock macOS Python has with every
    certificate."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        raise SkipCase("the venv has no cryptography package to make a self-signed certificate with") from None
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "HIL stand-in for greghulette.github.io")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("greghulette.github.io"),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    os.makedirs(d, exist_ok=True)
    cp, kp = os.path.join(d, "hil-pages-cert.pem"), os.path.join(d, "hil-pages-key.pem")
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cp, kp


@contextlib.contextmanager
def _github_is(pages):
    """https://greghulette.github.io/<path> answered by `pages` (a real request to it, the caller's timeout and TLS
    context kept; each such URL recorded in pages.github); a 127.0.0.1 URL as it is; anything else refused and
    recorded in the yielded list. flash.reachable and fetch_webui.reachable answer True."""
    import flash
    import fetch_webui
    real = urllib.request.urlopen
    refused = []

    def urlopen(url, *a, **k):
        u = url if isinstance(url, str) else url.full_url
        if u.startswith(PAGES + "/"):
            with pages.lock:
                pages.github.append(u)
            return real(pages.url + u[len(PAGES):], *a, **k)
        if u.startswith(("http://127.0.0.1:", "https://127.0.0.1:")):
            return real(url, *a, **k)
        refused.append(u)
        raise urllib.error.URLError(f"HIL: no stand-in for {u} - this test never reaches the network")
    saved = (flash.reachable, fetch_webui.reachable)
    urllib.request.urlopen = urlopen
    flash.reachable = fetch_webui.reachable = lambda *a, **k: True
    try:
        yield refused
    finally:
        urllib.request.urlopen = real
        flash.reachable, fetch_webui.reachable = saved


# ------------------------------------------------------------------ what a published tool looks like
def _blob(tag, name):
    """Bytes a text-mode write, a re-encode or a newline translation would change: CRLF, a lone CR and LF, a NUL, a BOM
    mid-file and every byte value, after a line naming the file and its build."""
    return (f"/* HIL {tag} {name} */\r\n".encode() + b"crlf\r\nlf\nlone cr\rnul\x00 bom\xef\xbb\xbf end\r\n"
            + bytes(range(256)) + b"\r\n")


EXTRA_IMG = "hil-extra.png"     # a ../Images name the Wizard uses that WCB_IMAGES does not list
SEQ_IMG = "hil-seq.png"         # a ../Images name only a file index.html loads beyond WCB_FILES uses


def _wizard_site(prefix, ver, tag, drop=(), seq=False):
    """A published Wizard at <prefix>/Wizard with its <prefix>/Images sibling -> {server path: bytes}. app.js carries
    UI_VERSION; index.html names EXTRA_IMG. drop: server paths to leave out (a 404).

    seq: the page also loads hil-seq.css and hil-seq.js, which WCB_FILES does not list - as the seq-builder Wizard loads
    seq-builder.css and .js (Intellex 0.2.1 bundled it without them) - and hil-seq.js names two command-library vendors
    by their manifests, wcb (in CMDLIB_VENDORS) and hil-acme (not), and SEQ_IMG."""
    import fetch_webui as fw
    files = {}
    for name in list(fw.WCB_FILES) + list(fw.WCB_VENDOR):
        body = _blob(tag, name)
        if name == "app.js":
            body = f"const UI_VERSION = '{ver}';\r\n".encode() + body
        elif name == "index.html":
            tags = '<link rel="stylesheet" href="hil-seq.css"><script src="hil-seq.js"></script>\r\n' if seq else ""
            body = f'<img src="../Images/{EXTRA_IMG}" alt="">\r\n{tags}'.encode() + body
        files[f"{prefix}/Wizard/{name}"] = body
    images = list(fw.WCB_IMAGES) + [EXTRA_IMG]
    if seq:
        wiz = f"{prefix}/Wizard"
        files[f"{wiz}/hil-seq.css"] = _blob(tag, "hil-seq.css")
        files[f"{wiz}/hil-seq.js"] = (b"const LIBS = ['cmdlib/wcb/manifest.json', 'cmdlib/hil-acme/manifest.json'];\r\n"
                                      + f"const PIC = '../Images/{SEQ_IMG}';\r\n".encode() + _blob(tag, "hil-seq.js"))
        files[f"{wiz}/cmdlib/wcb/manifest.json"] = _manifest(["wcb-builder.json"])
        files[f"{wiz}/cmdlib/wcb/wcb-builder.json"] = _blob(tag, "wcb/wcb-builder.json")
        files[f"{wiz}/cmdlib/hil-acme/manifest.json"] = _manifest(["./boards/x.json"])
        files[f"{wiz}/cmdlib/hil-acme/boards/x.json"] = _blob(tag, "hil-acme/boards/x.json")
        files[f"{wiz}/cmdlib/hil-acme/LICENSE"] = _blob(tag, "hil-acme/LICENSE")
        images.append(SEQ_IMG)
    for name in images:
        files[f"{prefix}/Images/{name}"] = _blob(tag, name)
    return {p: b for p, b in files.items() if p not in drop}


def _wizard_disk(site, prefix):
    """What src/webui_wcb must hold after `site` was fetched: Wizard/... and Images/..., the same bytes."""
    return {p[len(prefix) + 1:]: b for p, b in site.items() if p.startswith(prefix + "/")}


def _manifest(entries):
    """A vendor catalog in the real shape (droidnet-command-library/catalog/v1), its boards' "file" entries as given."""
    return json.dumps({"$schema": "droidnet-command-library/catalog/v1", "schemaVersion": "v1",
                       "boards": [{"id": f"b{i}", "file": e, "name": f"HIL board {i}"} for i, e in enumerate(entries)]},
                      indent=1).encode()


def _config_site(prefix, dtg, tag, boards=("a", "b", "c", "d"), drop=()):
    """A published config tool at <prefix>/config_tool with its <prefix>/Images sibling -> {server path: bytes}.

    droidnet's manifest names its boards bare ('boards/a.json'), './'-led, vendor-prefixed ('droidnet/boards/c.json'),
    twice (c again bare), nested under another key, and once with a '..' segment that _safe_relname must refuse; a
    non-.json string is not a file. LICENSE is published for droidnet only, NOTICE.md for navicore only."""
    import fetch_webui as fw
    ct = f"{prefix}/config_tool"
    files = {f"{ct}/index.html": f'<footer>build <span id="footer-dtg">{dtg}</span></footer>\r\n'.encode()
             + _blob(tag, "index.html")}
    for name in fw.ROOT_FILES[1:]:
        files[f"{ct}/{name}"] = _blob(tag, name)
    for name in fw.IMAGES:
        files[f"{prefix}/Images/{name}"] = _blob(tag, name)
    entries = []
    for i, b in enumerate(boards):
        entries.append(("./" if i == 1 else "droidnet/" if i == 2 else "") + f"boards/{b}.json")
        files[f"{ct}/cmdlib/droidnet/boards/{b}.json"] = _blob(tag, f"droidnet/boards/{b}.json")
    if len(boards) > 2:
        entries.append(f"boards/{boards[2]}.json")                       # the vendor-prefixed one again, bare
    entries.append("boards/../../../../hil-escape.json")
    man = json.loads(_manifest(entries))
    man["extra"] = {"notes": "readme.txt", "count": 3, "more": [[f"boards/{boards[0]}.json"]]}
    files[f"{ct}/cmdlib/droidnet/manifest.json"] = json.dumps(man, indent=1).encode() + b"\r\n"
    files[f"{ct}/cmdlib/droidnet/LICENSE"] = _blob(tag, "droidnet/LICENSE")
    files[f"{ct}/cmdlib/navicore/manifest.json"] = _manifest(["navicore.json"])
    files[f"{ct}/cmdlib/navicore/navicore.json"] = _blob(tag, "navicore/navicore.json")
    files[f"{ct}/cmdlib/navicore/NOTICE.md"] = _blob(tag, "navicore/NOTICE.md")
    return {p: b for p, b in files.items() if p not in drop}


def _config_disk(site, prefix):
    """What src/webui must hold after `site` was fetched: config_tool's files at its root, Images/ beside them."""
    ct = f"{prefix}/config_tool/"
    out = {}
    for p, b in site.items():
        if p.startswith(ct):
            out[p[len(ct):]] = b
        elif p.startswith(f"{prefix}/Images/"):
            out[p[len(prefix) + 1:]] = b
    return out


# ------------------------------------------------------------------ disk helpers
def _tree(root):
    """{relative posix path: bytes} of every file under root ({} when it does not exist)."""
    out = {}
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root).replace(os.sep, "/")] = fh.read()
    return out


def _put(root, files):
    for rel, data in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)


def _diff(got, want):
    """Why two trees differ, briefly: missing, extra and changed paths."""
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    changed = sorted(k for k in set(got) & set(want) if got[k] != want[k])
    parts = []
    for label, names in (("missing", missing), ("extra", extra), ("bytes differ", changed)):
        if names:
            parts.append(f"{label} {names[:4]}{' +%d' % (len(names) - 4) if len(names) > 4 else ''}")
    return "; ".join(parts) or "identical"


def _run(argv):
    """fetch_webui.main() with argv -> (exit code, everything it printed)."""
    import fetch_webui
    out = io.StringIO()
    saved = sys.argv
    sys.argv = ["fetch_webui.py"] + list(argv)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                rc = fetch_webui.main()
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
    finally:
        sys.argv = saved
    return rc, out.getvalue()


@contextlib.contextmanager
def _scratch_tmp(ctx, name):
    """tempfile's directory pointed into the stage for the block -> its path, so a staging directory fetch_webui leaves
    behind is seen (and Greg's real temp directory is not written)."""
    d = os.path.join(ctx.stage, "scratch", name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    saved = tempfile.tempdir
    tempfile.tempdir = d
    try:
        yield d
    finally:
        tempfile.tempdir = saved


def _fresh_bundles():
    """Remove the stage's src/webui, src/webui_wcb and their .prev copies."""
    import fetch_webui as fw
    for d in (fw.WEBUI, fw.WEBUI_WCB):
        for p in (str(d), str(d.with_suffix(".prev"))):
            shutil.rmtree(p, ignore_errors=True)


def _branches(nc, wcb):
    import settings
    for product, b in (("navicore", nc), ("wcb", wcb)):
        err = settings.set_branch(product, b)
        check(err is None, f"could not set the stage's {product} branch to {b}: {err}")


# ------------------------------------------------------------------ group "webui_version"
_NC_INDEX = ("webui", "index.html")                     # under the stage's src/
_WZ_INDEX = ("webui_wcb", "Wizard", "index.html")
_WZ_APP = ("webui_wcb", "Wizard", "app.js")


def _bundle(ctx, dtg, ui):
    """The stage's bundled tools, as far as the version check reads them: the config tool's footer-dtg and the
    Wizard's UI_VERSION (None removes that file)."""
    nc = os.path.join(ctx.src, *_NC_INDEX)
    wz = os.path.join(ctx.src, *_WZ_APP)
    for p, text in ((nc, None if dtg is None else f'<span id="footer-dtg">{dtg}</span>\n'),
                    (wz, None if ui is None else f"const UI_VERSION = '{ui}';\n")):
        if text is None:
            if os.path.exists(p):
                os.remove(p)
            continue
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)


def _whole_again(ctx):
    """Back to the old stamps in copies whose pages load nothing (no Wizard index.html), so webui.missing_from finds
    nothing missing (Intellex src/webfiles.py missing_from) and a stale answer is the stamps' alone."""
    _bundle(ctx, "HIL-NC-OLD", "HIL-WZ-OLD")
    p = os.path.join(ctx.src, *_WZ_INDEX)
    if os.path.exists(p):
        os.remove(p)


def _ver_site(nc_path, dtg, wz_path, ui):
    files = {}
    if nc_path:
        files[nc_path] = f'<html><footer><span id="footer-dtg">{dtg}</span></footer></html>\n'.encode()
    if wz_path:
        files[wz_path] = f"// the Wizard\nconst UI_VERSION = '{ui}';\n".encode() + b"x" * 1000
    return files


@case("online, each tool's configured branch compared with its published copy: an older bundle is stale, checked "
      "true, no reason; an equal one is not stale, missing [] and repaired None; an equal one whose page loads files "
      "it lacks is stale, missing naming them (Intellex 0.2.3); a branch asks <site>/dev/<branch>/<leaf>, main asks "
      "<site>/<leaf>, and never main's copy for a branch", group="webui_version")
def webui_version_online(ctx):
    from bridge_units import _server
    s = _server()
    nc_main, nc_dev = f"{NC}/config_tool/index.html", f"{NC}/dev/hil-ncb/config_tool/index.html"
    wz_main, wz_dev = f"{WCB}/Wizard/app.js", f"{WCB}/dev/hil-wzb/Wizard/app.js"
    pages = _Pages()
    problems = []
    try:
        with _github_is(pages) as refused:
            # 1. NaviCore on main, the Wizard on a branch; both bundles older than what is published. Main's Wizard is
            # published too, with another stamp: comparing against it would show here.
            _bundle(ctx, "HIL-NC-OLD", "HIL-WZ-OLD")
            _branches("main", "hil-wzb")
            pages.serve({**_ver_site(nc_main, "HIL-NC-NEW", wz_dev, "HIL-WZ-NEW"),
                         **_ver_site(None, None, wz_main, "HIL-WZ-MAIN")})
            k = len(pages.github)
            code, body = s.req("GET", "/_api/webui-version", timeout=60)
            # missing/repaired (Intellex 0.2.3): what each copy's own page loads and the copy lacks - nothing, as these
            # copies' pages load nothing (the config tool's index.html is the stamp alone, the Wizard has none) - and
            # what the start's _repair_incomplete_tools fetched again, None: the stage started with no tool at all.
            # autoUpdated (Intellex 0.2.7): what the host's own update pass fetched since start, None - INTELLEX_AUTO_TOOLS
            # is 0 for every staged host (hil/intellex.py run_intellex_py).
            want = {"bundled": "HIL-NC-OLD", "published": "HIL-NC-NEW", "stale": True, "checked": True, "reason": "",
                    "missing": [], "repaired": None, "autoUpdated": None,
                    "wcb": {"bundled": "HIL-WZ-OLD", "published": "HIL-WZ-NEW", "stale": True, "checked": True,
                            "reason": "", "missing": [], "repaired": None, "autoUpdated": None}}
            if code != 200 or body != want:
                problems.append(f"bundles older than Pages (navicore main, wcb hil-wzb): {code} {body}, expected {want}")
            asked = sorted(pages.github[k:])
            if asked != sorted([PAGES + nc_main, PAGES + wz_dev]):
                problems.append(f"the check asked {asked}, expected main's config_tool/index.html and "
                                f"dev/hil-wzb/Wizard/app.js (settings.tool_base)")

            # 2. NaviCore on a branch too; both published copies equal to the bundles: checked, not stale.
            _branches("hil-ncb", "hil-wzb")
            pages.serve({**_ver_site(nc_dev, "HIL-NC-OLD", wz_dev, "HIL-WZ-OLD"),
                         **_ver_site(nc_main, "HIL-NC-MAIN", wz_main, "HIL-WZ-MAIN")})
            k = len(pages.github)
            code, body = s.req("GET", "/_api/webui-version", timeout=60)
            want = {"bundled": "HIL-NC-OLD", "published": "HIL-NC-OLD", "stale": False, "checked": True, "reason": "",
                    "missing": [], "repaired": None, "autoUpdated": None,
                    "wcb": {"bundled": "HIL-WZ-OLD", "published": "HIL-WZ-OLD", "stale": False, "checked": True,
                            "reason": "", "missing": [], "repaired": None, "autoUpdated": None}}
            if code != 200 or body != want:
                problems.append(f"bundles equal to their branch's Pages copy: {code} {body}, expected {want}")
            asked = sorted(pages.github[k:])
            if asked != sorted([PAGES + nc_dev, PAGES + wz_dev]):
                problems.append(f"with both on branches the check asked {asked}, expected the two dev/<branch>/ copies")

            # 2b. The same stamps, but each copy's page loads what the copy lacks (Intellex 0.2.1 bundled the
            # seq-builder Wizard without seq-builder.js, .css and cmdlib/, and the launcher called it up to date):
            # stale whatever the stamp says, `missing` naming each file in page order and "cmdlib/" for a library the
            # code loads with none beside it. Nothing more is asked of Pages for it.
            for p, text in ((_WZ_INDEX, '<link rel="stylesheet" href="hil-seq.css"><script src="app.js"></script>'
                                        '<script src="hil-seq.js"></script>\n'),
                            (_NC_INDEX, '<span id="footer-dtg">HIL-NC-OLD</span><script src="hil-nc.js"></script>\n'
                                        '<script>fetch("cmdlib/navicore/manifest.json")</script>\n')):
                _put_text(os.path.join(ctx.src, *p), text)
            with open(os.path.join(ctx.src, *_WZ_APP), "a", encoding="utf-8") as f:
                f.write("const HIL_LIB = 'cmdlib/wcb/manifest.json';\n")
            k = len(pages.github)
            code, body = s.req("GET", "/_api/webui-version", timeout=60)
            want = {"bundled": "HIL-NC-OLD", "published": "HIL-NC-OLD", "stale": True, "checked": True, "reason": "",
                    "missing": ["hil-nc.js", "cmdlib/"], "repaired": None, "autoUpdated": None,
                    "wcb": {"bundled": "HIL-WZ-OLD", "published": "HIL-WZ-OLD", "stale": True, "checked": True,
                            "reason": "", "missing": ["hil-seq.css", "hil-seq.js", "cmdlib/"], "repaired": None, "autoUpdated": None}}
            if code != 200 or body != want:
                problems.append(f"copies whose page loads what they lack, at their branch's stamps: {code} {body}, "
                                f"expected {want}")
            if sorted(pages.github[k:]) != sorted([PAGES + nc_dev, PAGES + wz_dev]):
                problems.append(f"incomplete copies: the check asked {sorted(pages.github[k:])}")
            _whole_again(ctx)

            # 3. Back on main: the main copies, older bundles stale again.
            _branches("main", "main")
            k = len(pages.github)
            code, body = s.req("GET", "/_api/webui-version", timeout=60)
            if (body or {}).get("published") != "HIL-NC-MAIN" or ((body or {}).get("wcb") or {}).get("published") != \
                    "HIL-WZ-MAIN" or body.get("stale") is not True or body["wcb"].get("stale") is not True:
                problems.append(f"both on main: {body}, expected main's published copies and stale")
            if sorted(pages.github[k:]) != sorted([PAGES + nc_main, PAGES + wz_main]):
                problems.append(f"both on main the check asked {sorted(pages.github[k:])}")
            if refused:
                problems.append(f"the version check asked the real network for {refused}")
    finally:
        pages.close()
        _branches("main", "main")
        _whole_again(ctx)
    check(not problems, "; ".join(problems))


@case("a failure says why: a certificate the venv cannot verify is certs.ADVICE for both tools, not the offline note; "
      "a 404 is the HTTP error; either tool failing leaves the other's answer checked", group="webui_version")
def webui_version_reasons(ctx):
    import certs
    import host
    from bridge_units import _server
    s = _server()
    problems = []
    _bundle(ctx, "HIL-NC-OLD", "HIL-WZ-OLD")
    _branches("main", "main")
    nc_main, wz_main = f"{NC}/config_tool/index.html", f"{WCB}/Wizard/app.js"

    # HTTPS with a certificate no CA the venv trusts has issued: the venv's own verification refuses it.
    tls = _Pages(_ver_site(nc_main, "HIL-NC-NEW", wz_main, "HIL-WZ-NEW"), tls=True,
                 certdir=os.path.join(ctx.stage, "scratch", "tls"))
    try:
        with _github_is(tls) as refused:
            code, body = s.req("GET", "/_api/webui-version", timeout=60)
            for label, part in (("navicore", body or {}), ("wcb", (body or {}).get("wcb") or {})):
                if part.get("reason") != certs.ADVICE or part.get("checked") is not False or part.get("published") \
                        or part.get("stale") is not False:
                    problems.append(f"{label} behind a certificate the venv cannot verify: checked "
                                    f"{part.get('checked')!r}, published {part.get('published')!r}, reason "
                                    f"{str(part.get('reason'))[:90]!r}; expected certs.ADVICE")
                if part.get("reason") == host.OFFLINE_NOTE:
                    problems.append(f"{label}: a certificate failure reported as offline")
            if (body or {}).get("bundled") != "HIL-NC-OLD" or ((body or {}).get("wcb") or {}).get("bundled") != \
                    "HIL-WZ-OLD":
                problems.append(f"the bundled stamps went missing beside a failed check: {body}")
            if sorted(tls.github) != sorted([PAGES + nc_main, PAGES + wz_main]):
                problems.append(f"the TLS stand-in was asked for {tls.github}")
            if refused:
                problems.append(f"the version check asked the real network for {refused}")
    finally:
        tls.close()

    # Plain HTTP: one tool's copy missing (404) while the other answers, then the other way round.
    pages = _Pages()
    try:
        with _github_is(pages) as refused:
            for gone, ok in (("navicore", "wcb"), ("wcb", "navicore")):
                site = _ver_site(nc_main, "HIL-NC-NEW", wz_main, "HIL-WZ-NEW")
                site.pop(nc_main if gone == "navicore" else wz_main)
                pages.serve(site)
                code, body = s.req("GET", "/_api/webui-version", timeout=60)
                parts = {"navicore": body or {}, "wcb": (body or {}).get("wcb") or {}}
                g, o = parts[gone], parts[ok]
                reason = str(g.get("reason"))
                if g.get("checked") is not False or not reason.startswith("HTTPError") or "404" not in reason:
                    problems.append(f"{gone}'s copy 404: checked {g.get('checked')!r}, reason {reason[:80]!r}; "
                                    f"expected 'HTTPError: HTTP Error 404: ...'")
                want_pub = "HIL-NC-NEW" if ok == "navicore" else "HIL-WZ-NEW"
                if o.get("checked") is not True or o.get("published") != want_pub or o.get("stale") is not True or \
                        o.get("reason") != "":
                    problems.append(f"{gone}'s copy 404: {ok} answered {o}, expected checked, published {want_pub}, "
                                    f"stale - one tool failing must not hide the other's answer")
            if refused:
                problems.append(f"the version check asked the real network for {refused}")
    finally:
        pages.close()
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "fetch_webui"
@case("_safe_relname refuses an empty name, an absolute or drive path, a UNC path and any '', '.' or '..' segment "
      "(either slash); accepts a plain relative path, dots inside a name included", group="fetch_webui")
def safe_relname(ctx):
    import fetch_webui
    problems = []
    for name in ("", "/etc/x.json", "\\x.json", "C:/x.json", "C:x.json", "c:\\x.json", "\\\\server\\share\\x.json",
                 "//server/share/x.json", "vendor/../../x.json", "boards/../../../evil.json", "../x.json", "..",
                 "a/./b.json", "./a.json", "a//b.json", "a/b/", "a\\..\\b.json", "boards/.."):
        if fetch_webui._safe_relname(name) is not False:
            problems.append(f"{name!r} accepted")
    for name in ("droidnet/boards/a.json", "boards/a.json", "a.json", "a..b.json", "boards/v1.2/x.json",
                 "navicore/NOTICE.md", ".hidden.json", "boards\\a.json"):
        if fetch_webui._safe_relname(name) is not True:
            problems.append(f"{name!r} refused")
    check(not problems, "; ".join(problems))


@case("cmdlib_names walks each vendor manifest: every .json string at any depth, normalised to the vendor directory "
      "('./x', bare and '<vendor>/x' alike), once each; a '..' entry refused out loud and never requested; LICENSE "
      "and NOTICE.md only where published; a vendor whose manifest 404s is skipped; whatever a manifest says, no name "
      "leaves its vendor directory", group="fetch_webui")
def manifest_walk(ctx):
    import fetch_webui
    pages = _Pages(_config_site(NC, "HIL-NC-1", "walk"))
    base = f"{pages.url}{NC}/config_tool"
    problems = []
    try:
        with _github_is(pages) as refused:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                names = fetch_webui.cmdlib_names(base)
            want = ["droidnet/manifest.json", "droidnet/boards/a.json", "droidnet/boards/b.json",
                    "droidnet/boards/c.json", "droidnet/boards/d.json", "droidnet/LICENSE",
                    "navicore/manifest.json", "navicore/navicore.json", "navicore/NOTICE.md"]
            if sorted(names) != sorted(want) or len(names) != len(set(names)):
                problems.append(f"cmdlib_names {names}, expected each of {want} once")
            if names[:1] != ["droidnet/manifest.json"] or names.index("navicore/manifest.json") < \
                    names.index("droidnet/LICENSE"):
                problems.append(f"not vendor by vendor, manifest first: {names}")
            if "skipping unsafe manifest entry: 'boards/../../../../hil-escape.json'" not in out.getvalue():
                problems.append(f"the '..' entry was not refused out loud: printed {out.getvalue()[-200:]!r}")
            if any("escape" in p for p in pages.asked):
                problems.append(f"the '..' entry was requested: {[p for p in pages.asked if 'escape' in p]}")

            # navicore's manifest gone; droidnet's names leading '../', '/' and drive paths. Whatever each becomes,
            # it stays inside cmdlib/droidnet/.
            site = _config_site(NC, "HIL-NC-1", "walk2")
            site.pop(f"{NC}/config_tool/cmdlib/navicore/manifest.json")
            site[f"{NC}/config_tool/cmdlib/droidnet/manifest.json"] = _manifest(
                ["boards/a.json", "../navicore/navicore.json", "../../../../hil-up.json", "/hil-abs.json",
                 "C:/hil-drive.json", "\\\\srv\\share\\hil-unc.json", "boards\\..\\..\\hil-back.json"])
            pages.serve(site)
            with contextlib.redirect_stdout(io.StringIO()):
                names = fetch_webui.cmdlib_names(base)
            if any(n.startswith("navicore/") for n in names):
                problems.append(f"navicore's manifest 404'd but navicore names were listed: {names}")
            for n in names:
                parts = n.replace("\\", "/").split("/")
                if parts[0] != "droidnet" or any(p in ("", ".", "..") for p in parts) or ":" in n:
                    problems.append(f"a manifest entry became {n!r}, outside cmdlib/droidnet/")
            if refused:
                problems.append(f"asked the real network for {refused}")
    finally:
        pages.close()
    check(not problems, "; ".join(problems))


@case("the config tool (--tool navicore --base <stand-in>): a first fetch lands byte-identical with no .prev claimed; a "
      "newer one replaces it whole, the old kept as webui.prev; up to date and --check fetch nothing more and change "
      "nothing; --force replaces .prev; a missing cmdlib file or no readable manifest changes nothing and leaves no "
      "staging directory", group="fetch_webui")
def navicore_swap(ctx):
    import fetch_webui as fw
    webui, prev = str(fw.WEBUI), str(fw.WEBUI.with_suffix(".prev"))
    _fresh_bundles()
    pages = _Pages()
    base = f"{pages.url}{NC}/config_tool"
    problems = []

    def go(site, *extra):
        pages.serve(site)
        k = pages.mark()
        rc, out = _run(["--tool", "navicore", "--base", base] + list(extra))
        return rc, out, pages.since(k)
    try:
        with _github_is(pages) as refused, _scratch_tmp(ctx, "tmp-nc") as tmp:
            v1 = _config_site(NC, "HIL-NC-1", "v1")
            rc, out, asked = go(v1)
            if rc != 0 or "updated to HIL-NC-1" not in out:
                problems.append(f"first fetch: exited {rc}, {out[-200:]!r}")
            if _tree(webui) != _config_disk(v1, NC):
                problems.append(f"first fetch: src/webui vs what was served: {_diff(_tree(webui), _config_disk(v1, NC))}")
            if os.path.exists(prev) or "previous kept" in out:
                problems.append("first fetch: a .prev was made or claimed with nothing before it")

            v2 = _config_site(NC, "HIL-NC-2", "v2", boards=("a", "b", "e"))      # a board gone, one new
            rc, out, asked = go(v2)
            if rc != 0 or "updated to HIL-NC-2" not in out or f"previous kept at {prev}" not in out:
                problems.append(f"second fetch: exited {rc}, {out[-200:]!r}")
            if _tree(webui) != _config_disk(v2, NC):
                problems.append(f"second fetch: src/webui vs what was served: {_diff(_tree(webui), _config_disk(v2, NC))}"
                                f" (an update must replace the bundle whole, not lay files over it)")
            if _tree(prev) != _config_disk(v1, NC):
                problems.append(f"second fetch: webui.prev vs the first bundle: {_diff(_tree(prev), _config_disk(v1, NC))}")

            rc, out, asked = go(v2)
            if rc != 0 or "up to date" not in out or asked != [f"{NC}/config_tool/index.html"]:
                problems.append(f"up to date: exited {rc}, asked {asked}, {out[-120:]!r}")
            v3 = _config_site(NC, "HIL-NC-3", "v3")
            rc, out, asked = go(v3, "--check")
            if rc != 0 or "update available" not in out or asked != [f"{NC}/config_tool/index.html"]:
                problems.append(f"--check: exited {rc}, asked {asked}, {out[-120:]!r}")
            if _tree(webui) != _config_disk(v2, NC) or _tree(prev) != _config_disk(v1, NC):
                problems.append("up to date or --check changed a bundle")

            rc, out, asked = go(v2, "--force")
            if rc != 0 or "updated to HIL-NC-2" not in out or len(asked) < 10:
                problems.append(f"--force on an up-to-date bundle: exited {rc}, {len(asked)} request(s), {out[-120:]!r}")
            if _tree(prev) != _config_disk(v2, NC) or _tree(webui) != _config_disk(v2, NC):
                problems.append(f"--force: webui.prev is not the bundle it replaced: {_diff(_tree(prev), _config_disk(v2, NC))}")
            if any(n.endswith(".prev") for n in os.listdir(prev)):
                problems.append(f"--force nested an old .prev inside the new one: {os.listdir(prev)}")

            before = (_tree(webui), _tree(prev))
            for label, site, words in (
                    ("a missing cmdlib board", _config_site(NC, "HIL-NC-4", "v4",
                                                            drop=(f"{NC}/config_tool/cmdlib/droidnet/boards/c.json",)),
                     "nothing was changed"),
                    ("a missing serial-hub.js", _config_site(NC, "HIL-NC-4", "v4",
                                                             drop=(f"{NC}/config_tool/serial-hub.js",)),
                     "nothing was changed"),
                    ("no readable cmdlib manifest", _config_site(NC, "HIL-NC-4", "v4", drop=(
                        f"{NC}/config_tool/cmdlib/droidnet/manifest.json",
                        f"{NC}/config_tool/cmdlib/navicore/manifest.json")), "refusing a partial update")):
                rc, out, asked = go(site)
                if rc != 1 or words not in out:
                    problems.append(f"{label}: exited {rc}, {out[-160:]!r}; expected 1 and {words!r}")
                if (_tree(webui), _tree(prev)) != before:
                    problems.append(f"{label}: the bundle changed: {_diff(_tree(webui), before[0])}; .prev "
                                    f"{_diff(_tree(prev), before[1])}")
                if os.listdir(tmp):
                    problems.append(f"{label}: a staging directory was left behind: {os.listdir(tmp)}")
                    shutil.rmtree(tmp)
                    os.makedirs(tmp)
            if refused:
                problems.append(f"asked the real network for {refused}")
    finally:
        pages.close()
    check(not problems, "; ".join(problems))


@case("the Wizard (--tool wcb --base <stand-in>): every Wizard, vendor and image file byte-identical, the ../Images "
      "name WCB_IMAGES lacks fetched with a line, a missing image a line and not a failure, the old bundle (its own "
      "extra file included) kept whole as webui_wcb.prev; up to date fetches app.js alone; a missing vendor file (a "
      "flasher library Intellex never loads) a line and not a failure; what index.html loads beyond WCB_FILES, the "
      "cmdlib/ vendors its code names and the images they name fetched too; a copy lacking one of them at the same "
      "stamp fetched whole again (--check only says so), then up to date; a missing page file or cmdlib file, or no "
      "readable manifest, changes nothing (Intellex 0.2.2-0.2.3)", group="fetch_webui")
def wizard_swap(ctx):
    import fetch_webui as fw
    wz, prev = str(fw.WEBUI_WCB), str(fw.WEBUI_WCB.with_suffix(".prev"))
    _fresh_bundles()
    old = {"Wizard/app.js": b"const UI_VERSION = 'HIL-WZ-OLD';\r\n", "Wizard/hil-old-only.js": b"old\r\n",
           "Images/hil-old.png": b"\x89PNG old"}
    _put(wz, old)
    pages = _Pages()
    base = f"{pages.url}{WCB}/Wizard"
    problems = []
    try:
        with _github_is(pages) as refused, _scratch_tmp(ctx, "tmp-wz") as tmp:
            gone_img = f"{WCB}/Images/{fw.WCB_IMAGES[-1]}"
            w1 = _wizard_site(WCB, "HIL-WZ-1", "w1", drop=(gone_img,))
            pages.serve(w1)
            rc, out = _run(["--tool", "wcb", "--base", base])
            if rc != 0 or "[wcb] updated to HIL-WZ-1" not in out or f"[wcb] previous kept at {prev}" not in out:
                problems.append(f"Wizard fetch: exited {rc}, {out[-200:]!r}")
            if _tree(wz) != _wizard_disk(w1, WCB):
                problems.append(f"src/webui_wcb vs what was served: {_diff(_tree(wz), _wizard_disk(w1, WCB))}")
            if _tree(prev) != old:
                problems.append(f"webui_wcb.prev vs the old bundle: {_diff(_tree(prev), old)}")
            if f"Images/{EXTRA_IMG}: the Wizard uses it and WCB_IMAGES does not list it" not in out:
                problems.append("no line for the ../Images file the Wizard names and WCB_IMAGES lacks")
            if f"could not fetch Images/{fw.WCB_IMAGES[-1]}" not in out:
                problems.append("no line for the image that 404'd")

            k = pages.mark()
            rc, out = _run(["--tool", "wcb", "--base", base])
            if rc != 0 or "[wcb] up to date" not in out or pages.since(k) != [f"{WCB}/Wizard/app.js"]:
                problems.append(f"up to date: exited {rc}, asked {pages.since(k)}, {out[-120:]!r}")

            # A vendor/ file the site lacks (an esptool-js bump renames it): reported and skipped since Intellex 0.2.2 -
            # Intellex flashes natively and never loads it, and failing on it held back the whole Wizard. The rest
            # lands as served, the bundle before kept whole as .prev.
            w1_disk = _tree(wz)
            w2 = _wizard_site(WCB, "HIL-WZ-2", "w2", drop=(f"{WCB}/Wizard/{fw.WCB_VENDOR[-1]}",))
            pages.serve(w2)
            rc, out = _run(["--tool", "wcb", "--base", base])
            if rc != 0 or "[wcb] updated to HIL-WZ-2" not in out or f"could not fetch {fw.WCB_VENDOR[-1]}" not in out:
                problems.append(f"a missing vendor file: exited {rc}, {out[-200:]!r}; expected 0, updated, and a "
                                f"'could not fetch {fw.WCB_VENDOR[-1]}' line")
            if _tree(wz) != _wizard_disk(w2, WCB):
                problems.append(f"a missing vendor file: src/webui_wcb vs what was served: "
                                f"{_diff(_tree(wz), _wizard_disk(w2, WCB))}")
            if _tree(prev) != w1_disk:
                problems.append(f"a missing vendor file: webui_wcb.prev vs the bundle before: "
                                f"{_diff(_tree(prev), w1_disk)}")

            # What the page loads beyond the lists (webfiles.page_refs), the cmdlib/ its code names - a vendor
            # CMDLIB_VENDORS lacks included (webfiles.named_vendors) - and the ../Images file one of those names.
            w3 = _wizard_site(WCB, "HIL-WZ-3", "w3", seq=True)
            pages.serve(w3)
            rc, out = _run(["--tool", "wcb", "--base", base])
            if rc != 0 or "[wcb] updated to HIL-WZ-3" not in out or \
                    "hil-seq.js: loaded by this Wizard's index.html" not in out or \
                    f"Images/{SEQ_IMG}: the Wizard uses it and WCB_IMAGES does not list it" not in out:
                problems.append(f"a Wizard loading files beyond WCB_FILES: exited {rc}, {out[-240:]!r}")
            if _tree(wz) != _wizard_disk(w3, WCB):
                problems.append(f"a Wizard loading files beyond WCB_FILES: src/webui_wcb vs what was served: "
                                f"{_diff(_tree(wz), _wizard_disk(w3, WCB))}")

            # A copy lacking some of it at the published stamp, as 0.2.1's fixed list left the seq-builder Wizard:
            # not up to date (webfiles.missing_from) - --check says so and changes nothing, a plain run fetches it
            # whole - and once whole, up to date again (one request), not refetched every time.
            shutil.rmtree(os.path.join(wz, "Wizard", "cmdlib"), ignore_errors=True)
            with contextlib.suppress(FileNotFoundError):        # (absent: the fetch above already failed)
                os.remove(os.path.join(wz, "Wizard", "hil-seq.css"))
            incomplete = _tree(wz)
            rc, out = _run(["--tool", "wcb", "--base", base, "--check"])
            if rc != 0 or "[wcb] the bundled copy lacks hil-seq.css, cmdlib/" not in out or \
                    "update available" not in out or _tree(wz) != incomplete:
                problems.append(f"--check on a copy lacking hil-seq.css and cmdlib/: exited {rc}, {out[-200:]!r}")
            rc, out = _run(["--tool", "wcb", "--base", base])
            if rc != 0 or "[wcb] the bundled copy lacks hil-seq.css, cmdlib/ - fetching it whole again" not in out or \
                    "[wcb] updated to HIL-WZ-3" not in out:
                problems.append(f"a copy lacking hil-seq.css and cmdlib/ at the published stamp: exited {rc}, "
                                f"{out[-200:]!r}; expected it fetched whole again")
            if _tree(wz) != _wizard_disk(w3, WCB) or _tree(prev) != incomplete:
                problems.append(f"the incomplete copy fetched again: {_diff(_tree(wz), _wizard_disk(w3, WCB))}; .prev "
                                f"vs the incomplete copy {_diff(_tree(prev), incomplete)}")
            k = pages.mark()
            rc, out = _run(["--tool", "wcb", "--base", base])
            if rc != 0 or "[wcb] up to date" not in out or pages.since(k) != [f"{WCB}/Wizard/app.js"]:
                problems.append(f"the whole copy again: exited {rc}, asked {pages.since(k)}, {out[-120:]!r}; expected "
                                f"up to date")

            # One file the page or its library loads missing, or no manifest to read: nothing changes.
            before = (_tree(wz), _tree(prev))
            for label, gone, words in (
                    ("a missing hil-seq.css", (f"{WCB}/Wizard/hil-seq.css",), "[wcb] nothing was changed"),
                    ("a missing cmdlib board", (f"{WCB}/Wizard/cmdlib/hil-acme/boards/x.json",),
                     "[wcb] nothing was changed"),
                    ("no readable cmdlib manifest", (f"{WCB}/Wizard/cmdlib/wcb/manifest.json",
                                                     f"{WCB}/Wizard/cmdlib/hil-acme/manifest.json"),
                     "refusing a partial update")):
                pages.serve(_wizard_site(WCB, "HIL-WZ-4", "w4", seq=True, drop=gone))
                rc, out = _run(["--tool", "wcb", "--base", base])
                if rc != 1 or words not in out:
                    problems.append(f"{label}: exited {rc}, {out[-160:]!r}; expected 1 and {words!r}")
                if (_tree(wz), _tree(prev)) != before:
                    problems.append(f"{label} changed the bundle: {_diff(_tree(wz), before[0])}; .prev "
                                    f"{_diff(_tree(prev), before[1])}")
            if os.listdir(tmp):
                problems.append(f"a staging directory was left behind: {os.listdir(tmp)}")
            if refused:
                problems.append(f"asked the real network for {refused}")
    finally:
        pages.close()
    check(not problems, "; ".join(problems))


@case("with no --base each tool comes from its configured branch: /dev/<branch>/ when published (probed once, then "
      "fetched from there), main's with 'branch <b> publishes no tool - using main's' when not; main probes nothing",
      group="fetch_webui")
def branch_fallback(ctx):
    import fetch_webui as fw
    _fresh_bundles()
    sites = {**_wizard_site(WCB, "HIL-WZ-MAIN", "wmain"), **_wizard_site(f"{WCB}/dev/hil-wzb", "HIL-WZ-BR", "wbr"),
             **_config_site(NC, "HIL-NC-MAIN", "cmain"), **_config_site(f"{NC}/dev/hil-ncb", "HIL-NC-BR", "cbr")}
    pages = _Pages(sites)
    problems = []
    try:
        with _github_is(pages) as refused:
            for tool, branch, prefix, note in (
                    ("wcb", "hil-wzb", f"{WCB}/dev/hil-wzb", "[wcb] branch hil-wzb"),
                    ("wcb", "hil-nopub", WCB, "[wcb] branch hil-nopub publishes no tool - using main's"),
                    ("wcb", "main", WCB, None),
                    ("navicore", "hil-ncb", f"{NC}/dev/hil-ncb", "[navicore] branch hil-ncb"),
                    ("navicore", "hil-nopub", NC, "[navicore] branch hil-nopub publishes no tool - using main's"),
                    ("navicore", "main", NC, None)):
                _branches(branch if tool == "navicore" else "main", branch if tool == "wcb" else "main")
                k = len(pages.github)
                rc, out = _run(["--tool", tool, "--force"])
                asked = [u[len(PAGES):] for u in pages.github[k:]]
                label = f"{tool} on {branch}"
                if rc != 0 or (note and note not in out) or (not note and "branch" in out.split("bundled")[0]):
                    problems.append(f"{label}: exited {rc}, printed {out[:160]!r}; expected {note or 'no branch note'!r}")
                site_root = WCB if tool == "wcb" else NC
                dev = f"{site_root}/dev/{branch}/{'Wizard' if tool == 'wcb' else 'config_tool'}/index.html"
                probes = [p for p in asked if "/dev/" in p and p.endswith("/index.html")]
                if branch == "main" and any("/dev/" in p for p in asked):
                    problems.append(f"{label}: asked a /dev/ path: {[p for p in asked if '/dev/' in p][:3]}")
                if branch != "main" and probes[:1] != [dev]:
                    problems.append(f"{label}: the first /dev/ request was {probes[:1]}, expected the probe {dev}")
                if branch == "hil-nopub" and (asked.count(dev) != 1 or any("/dev/" in p for p in asked if p != dev)):
                    problems.append(f"{label}: the unpublished branch was probed {asked.count(dev)} time(s) or "
                                    f"fetched from: {[p for p in asked if '/dev/' in p]}")
                # Everything else from the copy in use: the branch's for a published branch, main's (no /dev/ at
                # all) otherwise - the unpublished branch's one probe aside.
                other = [p for p in asked if p != dev and not (p.startswith(prefix + "/") and
                                                               (prefix != site_root or "/dev/" not in p))]
                if other:
                    problems.append(f"{label}: fetched outside {prefix}: {other[:3]}")
                if tool == "wcb":
                    got, want = _tree(str(fw.WEBUI_WCB)), _wizard_disk({p: b for p, b in sites.items()
                                                                       if p.startswith(prefix + "/Wizard/")
                                                                       or p.startswith(prefix + "/Images/")}, prefix)
                else:
                    got, want = _tree(str(fw.WEBUI)), _config_disk({p: b for p, b in sites.items()
                                                                   if p.startswith(prefix + "/config_tool/")
                                                                   or p.startswith(prefix + "/Images/")}, prefix)
                if got != want:
                    problems.append(f"{label}: the bundle is not {prefix}'s: {_diff(got, want)}")
            if refused:
                problems.append(f"asked the real network for {refused}")
    finally:
        pages.close()
        _branches("main", "main")
    check(not problems, "; ".join(problems))


@case("--tool all runs two independent swaps: the Wizard failing leaves the config tool's new bundle in place and its "
      "own old one; the config tool failing leaves the Wizard's new one; each exits 1; --base with all exits 2 and "
      "fetches nothing", group="fetch_webui")
def all_independent(ctx):
    import fetch_webui as fw
    _fresh_bundles()
    webui, wz = str(fw.WEBUI), str(fw.WEBUI_WCB)
    old_nc = {"index.html": b'<span id="footer-dtg">HIL-NC-OLD</span>', "cmdlib/droidnet/manifest.json": b"{}"}
    old_wz = {"Wizard/app.js": b"const UI_VERSION = 'HIL-WZ-OLD';"}
    _put(webui, old_nc)
    _put(wz, old_wz)
    _branches("main", "main")
    pages = _Pages()
    problems = []
    try:
        with _github_is(pages) as refused, _scratch_tmp(ctx, "tmp-all") as tmp:
            nc1 = _config_site(NC, "HIL-NC-1", "all1")
            wz_broken = _wizard_site(WCB, "HIL-WZ-1", "all1", drop=(f"{WCB}/Wizard/parser.js",))
            pages.serve({**nc1, **wz_broken})
            rc, out = _run(["--tool", "all"])
            if rc != 1 or "updated to HIL-NC-1" not in out or "[wcb] nothing was changed" not in out:
                problems.append(f"Wizard failing: exited {rc}, {out[-200:]!r}; expected 1, the config tool updated")
            if _tree(webui) != _config_disk(nc1, NC):
                problems.append(f"Wizard failing: the config tool did not keep its update: {_diff(_tree(webui), _config_disk(nc1, NC))}")
            if _tree(wz) != old_wz:
                problems.append(f"Wizard failing: its old bundle changed: {_diff(_tree(wz), old_wz)}")

            nc_broken = _config_site(NC, "HIL-NC-2", "all2", drop=(f"{NC}/config_tool/cmdlib/navicore/navicore.json",))
            wz1 = _wizard_site(WCB, "HIL-WZ-1", "all2")
            pages.serve({**nc_broken, **wz1})
            rc, out = _run(["--tool", "all"])
            if rc != 1 or "[wcb] updated to HIL-WZ-1" not in out or "nothing was changed" not in out.split("[wcb]")[0]:
                problems.append(f"config tool failing: exited {rc}, {out[-200:]!r}; expected 1, the Wizard updated")
            if _tree(webui) != _config_disk(nc1, NC):
                problems.append(f"config tool failing: its bundle changed: {_diff(_tree(webui), _config_disk(nc1, NC))}")
            if _tree(wz) != _wizard_disk(wz1, WCB):
                problems.append(f"config tool failing: the Wizard did not update: {_diff(_tree(wz), _wizard_disk(wz1, WCB))}")
            if os.listdir(tmp):
                problems.append(f"a staging directory was left behind: {os.listdir(tmp)}")

            k = pages.mark()
            rc, out = _run(["--tool", "all", "--base", f"{pages.url}{WCB}/Wizard"])
            if rc != 2 or pages.since(k):
                problems.append(f"--tool all --base: exited {rc}, asked {pages.since(k)}; expected 2 and nothing")
            if refused:
                problems.append(f"asked the real network for {refused}")
    finally:
        pages.close()
    check(not problems, "; ".join(problems))



# ------------------------------------------------------------------ group "tool_serving"
# intellex.tool_serving_unit (2026-10-15: after a branch switch and an Update the Wizard's files on disk were WIFI's and
# the window still ran main's app.js, out of the webview's own cache). Intellex src/host.py _tool_files, _versioned,
# webui_dir/wcb_dir and api_update_webui's changed/notes/--force, on its real build_app() in this process.
_WZ_PAGE = ('<!DOCTYPE html><html><head><link rel="stylesheet" href="styles.css">'
            '<link href="https://fonts.example/css2?family=x" rel="stylesheet"></head><body>'
            '<script src="parser.js"></script><script src="app.js"></script>'
            '<script src="gone.js"></script><script>/* inline */</script></body></html>\n')


def _put_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _token(path):
    st = os.stat(path)
    return f"{st.st_mtime_ns:x}.{st.st_size:x}"


def _refs(html):
    import re
    return re.findall(r'<(?:script|link)\b[^>]*?\b(?:src|href)="([^"]*)"', html, re.I)


def _wizard_bundle(root, ui):
    """A Wizard bundle at <root>/Wizard: index.html (_WZ_PAGE), app.js stamped `ui`, parser.js, styles.css, vendor/."""
    wz = os.path.join(str(root), "Wizard")
    _put_text(os.path.join(wz, "index.html"), _WZ_PAGE)
    _put_text(os.path.join(wz, "app.js"), f"const UI_VERSION = '{ui}';\n")
    _put_text(os.path.join(wz, "parser.js"), "// parser\n")
    _put_text(os.path.join(wz, "styles.css"), "body{}\n")
    os.makedirs(os.path.join(wz, "vendor"), exist_ok=True)
    return wz


@case("each tool's files are sent no-cache, and its served page asks for its own scripts and stylesheet at "
      "?v=<mtime>.<size> of each file and is otherwise the file on disk plus the shim tag; a file that changes gets a "
      "new URL, the shim, another site and a missing file keep theirs; a directory is 403, a missing file or a path out "
      "of the bundle 404", group="tool_serving")
def tool_files_revalidated(ctx):
    import host
    from bridge_units import _server
    s = _server()
    problems = []
    wz = _wizard_bundle(os.path.join(ctx.src, "webui_wcb"), "HIL-WZ-A")
    nc_index = os.path.join(ctx.src, "webui", "index.html")
    _put_text(nc_index, '<html><body><span id="footer-dtg">HIL-NC</span><script src="flasher.js"></script></body></html>\n')
    _put_text(os.path.join(ctx.src, "webui", "flasher.js"), "// flasher\n")
    tok = lambda name: _token(os.path.join(wz, name))  # noqa: E731

    def page(path):
        st, hdr, body = s.http("GET", path)
        return st, hdr, body.decode("utf-8", "replace")

    st, hdr, html = page("/wcb/Wizard/")
    want = [f"styles.css?v={tok('styles.css')}", "https://fonts.example/css2?family=x", "/_intellex.js",
            f"parser.js?v={tok('parser.js')}", f"app.js?v={tok('app.js')}", "gone.js"]
    if st != 200 or _refs(html) != want:
        problems.append(f"GET /wcb/Wizard/: {st}, references {_refs(html)}, expected {want}")
    import re
    plain = re.sub(r'\?v=[0-9a-f]+\.[0-9a-f]+"', '"', html).replace(host.SHIM_TAG + "\n", "", 1)
    if plain != _WZ_PAGE:
        problems.append("the served Wizard page differs from its file beyond the shim tag and the ?v= stamps")
    if "no-store" not in hdr.get("cache-control", ""):
        problems.append(f"the Wizard page's Cache-Control is {hdr.get('cache-control')!r}, not no-store")
    for name in ("app.js", "parser.js", "styles.css"):
        st, hdr, body = s.http("GET", f"/wcb/Wizard/{name}?v={tok(name)}")
        with open(os.path.join(wz, name), "rb") as f:
            disk = f.read()
        if st != 200 or body != disk or hdr.get("cache-control") != "no-cache":
            problems.append(f"GET /wcb/Wizard/{name}: {st}, {len(body)} bytes (file {len(disk)}), Cache-Control "
                            f"{hdr.get('cache-control')!r}; expected the file, no-cache")
    st, hdr, html = page("/")
    if f"flasher.js?v={_token(os.path.join(ctx.src, 'webui', 'flasher.js'))}" not in html:
        problems.append(f"the config tool's page asks for {_refs(html)}, not a stamped flasher.js")
    st, hdr, _ = s.http("GET", "/flasher.js")
    if st != 200 or hdr.get("cache-control") != "no-cache":
        problems.append(f"GET /flasher.js: {st}, Cache-Control {hdr.get('cache-control')!r}; expected 200 no-cache")

    # app.js replaced (as an update or a branch switch does): a new URL for it, the same for the others.
    old = tok("app.js")
    _put_text(os.path.join(wz, "app.js"), "const UI_VERSION = 'HIL-WZ-B';\n// another build, another length\n")
    st, hdr, html = page("/wcb/Wizard/")
    if f"app.js?v={tok('app.js')}" not in html or old == tok("app.js"):
        problems.append(f"after app.js changed the page asks for {_refs(html)}; expected app.js?v={tok('app.js')}")
    if f"parser.js?v={tok('parser.js')}" not in html:
        problems.append("an unchanged parser.js lost its stamp")

    for path, want_st in (("/wcb/Wizard/vendor/", 403), ("/wcb/Wizard/gone.js", 404), ("/no-such-file.js", 404),
                          ("/wcb/..%2fwebui/index.html", 404), ("/wcb/Wizard/..%2f..%2fwebui/index.html", 404),
                          ("/%2e%2e/%2e%2e/etc/hosts", 404)):
        st, _, body = s.http("GET", path)
        if st != want_st or b"HIL-NC" in body:
            problems.append(f"GET {path}: {st}, expected {want_st}")
    check(not problems, "; ".join(problems))


@case("a frozen app serves each tool from wherever paths.data_dir says it is NOW: the shipped copy while there is no "
      "user copy, then - once an update has written one - the user copy's files, page stamps and /_api/webui-version "
      "bundled stamp, with no restart (the first Update on a frozen install changed nothing on screen)",
      group="tool_serving")
def tool_dir_per_request(ctx):
    import paths
    from bridge_units import _server
    s = _server()
    problems = []
    _wizard_bundle(os.path.join(ctx.src, "webui_wcb"), "HIL-WZ-SHIPPED")
    user_root = paths.user_data_dir() / "webui_wcb"
    shutil.rmtree(user_root, ignore_errors=True)
    saved = paths.FROZEN
    paths.FROZEN = True
    try:
        st, _, body = s.http("GET", "/wcb/Wizard/app.js")
        if st != 200 or b"HIL-WZ-SHIPPED" not in body:
            problems.append(f"no user copy: GET app.js {st} {body[:60]!r}, expected the shipped copy")
        code, v = s.req("GET", "/_api/webui-version", timeout=60)
        if ((v or {}).get("wcb") or {}).get("bundled") != "HIL-WZ-SHIPPED":
            problems.append(f"no user copy: webui-version says {(v or {}).get('wcb')}")

        user = _wizard_bundle(user_root, "HIL-WZ-USER")
        _put_text(os.path.join(user, "app.js"), "const UI_VERSION = 'HIL-WZ-USER';\n// the user's copy\n")
        st, _, body = s.http("GET", "/wcb/Wizard/app.js")
        if st != 200 or b"HIL-WZ-USER" not in body:
            problems.append(f"after an update wrote the user copy: GET app.js {st} {body[:60]!r}, still not it")
        st, _, html = s.http("GET", "/wcb/Wizard/")
        if f"app.js?v={_token(os.path.join(user, 'app.js'))}" not in html.decode("utf-8", "replace"):
            problems.append(f"the page's app.js stamp is not the user copy's: {_refs(html.decode('utf-8', 'replace'))}")
        code, v = s.req("GET", "/_api/webui-version", timeout=60)
        if ((v or {}).get("wcb") or {}).get("bundled") != "HIL-WZ-USER":
            problems.append(f"after the update webui-version still says {(v or {}).get('wcb')}")
    finally:
        paths.FROZEN = saved
        shutil.rmtree(user_root, ignore_errors=True)
    check(not problems, "; ".join(problems))


@case("POST /_api/update-webui says which tools the run swapped in (changed, from the bundle directory, not the "
      "stamp: a forced fetch of the same stamp still counts) and the fetcher's branch lines (notes), and hands the "
      "fetcher --force for ?force=1 only", group="tool_serving")
def update_reports_changed(ctx):
    import subprocess
    import host
    from bridge_units import _server
    s = _server()
    problems = []
    calls = []
    root = os.path.join(ctx.src, "webui_wcb")
    _wizard_bundle(root, "HIL-WZ-SAME")
    out = ["[wcb] branch hil-wzb", "[wcb] bundled  : HIL-WZ-SAME", "[wcb] updated to HIL-WZ-SAME",
           "[navicore] branch hil-ncb publishes no tool - using main's", "bundled : HIL-NC"]

    def fake(argv, **_k):
        calls.append(list(argv))
        if argv[argv.index("--tool") + 1] in ("wcb", "all"):       # fetch_webui._swap_in: rename, keep .prev
            staged = root + ".hilstage"
            shutil.rmtree(staged, ignore_errors=True)
            shutil.rmtree(root + ".prev", ignore_errors=True)
            _wizard_bundle(staged, "HIL-WZ-SAME")
            os.rename(root, root + ".prev")
            os.rename(staged, root)
        return subprocess.CompletedProcess(argv, 0, "\n".join(out) + "\n", "")
    real = host.proc.run
    host.proc.run = fake
    try:
        code, r = s.req("POST", "/_api/update-webui?tool=wcb&force=1", {}, timeout=60)
        if code != 200 or (r or {}).get("changed") != ["wcb"] or r.get("wcbVersion") != "HIL-WZ-SAME":
            problems.append(f"a forced Wizard fetch, same stamp: {code} changed {(r or {}).get('changed')}, "
                            f"wcbVersion {(r or {}).get('wcbVersion')}; expected ['wcb'], HIL-WZ-SAME")
        if (r or {}).get("notes") != ["[wcb] branch hil-wzb", "[navicore] branch hil-ncb publishes no tool - using main's"]:
            problems.append(f"notes {(r or {}).get('notes')}, expected the two branch lines")
        if not calls or calls[-1][-1] != "--force":
            problems.append(f"?force=1 ran {calls[-1:]}, not ... --force")
        code, r = s.req("POST", "/_api/update-webui?tool=navicore", {}, timeout=60)
        if code != 200 or (r or {}).get("changed") != []:
            problems.append(f"a run that swapped nothing: changed {(r or {}).get('changed')}, expected []")
        if "--force" in calls[-1]:
            problems.append(f"without ?force=1 the fetcher got --force: {calls[-1]}")
    finally:
        host.proc.run = real
        shutil.rmtree(root + ".prev", ignore_errors=True)
    check(not problems, "; ".join(problems))


@case("a frozen app serves its own build's tools unless a copy downloaded since is the better one: the newer version "
      "stamp wins (a month-old download no longer hides the tools a new app ships); a copy of the branch now chosen "
      "beats one of another branch either way round; the wikis go by fetch time, and a download with no record loses to "
      "the app's own (Greg 2026-10-15: 'Config tools should be up to date as of the time of app creation/build')",
      group="tool_serving")
def frozen_copy_choice(ctx):
    import json as _json
    import pathlib
    import paths
    import settings
    root = pathlib.Path(ctx.stage, "scratch", "choice")
    shutil.rmtree(root, ignore_errors=True)
    bundle, user = root / "bundle", paths.user_data_dir()

    def wizard(base, ui, branch=None):
        shutil.rmtree(base / "webui_wcb", ignore_errors=True)
        _put_text(str(base / "webui_wcb" / "Wizard" / "app.js"), f"const UI_VERSION = '{ui}';\n")
        src = base / "webui_wcb.source.json"
        if branch:
            src.write_text(_json.dumps({"branch": branch, "stamp": ui}), encoding="utf-8")
        elif src.exists():
            src.unlink()

    def wiki(base, at):
        shutil.rmtree(base / "wiki" / "wcb", ignore_errors=True)
        _put_text(str(base / "wiki" / "wcb" / "Home.md"), "# home\n")
        src = base / "wiki" / "wcb.source.json"
        if at is None:
            src.unlink(missing_ok=True)
        else:
            src.write_text(_json.dumps({"at": at}), encoding="utf-8")

    saved = (paths.FROZEN, paths.BUNDLE_DIR)
    paths.FROZEN, paths.BUNDLE_DIR = True, bundle
    problems = []
    try:
        cases = (  # (bundled UI, bundled branch, user UI, user branch, chosen branch, expected winner)
            ("15.12:07.R.OCT.2026", "main", "09.13:25.R.SEP.2026", None, "main", "bundle"),
            ("15.12:07.R.OCT.2026", "main", "16.10:00.R.OCT.2026", None, "main", "user"),
            ("15.12:07.R.OCT.2026", "main", "15.12:07.R.OCT.2026", "main", "main", "user"),
            ("15.12:07.R.OCT.2026", "main", "01.09:00.R.OCT.2026", "WIFI", "WIFI", "user"),
            ("15.12:07.R.OCT.2026", "main", "20.09:00.R.OCT.2026", "WIFI", "main", "bundle"),
            ("15.12:07.R.OCT.2026", None, "not a stamp", None, "main", "bundle"),
        )
        for b_ui, b_br, u_ui, u_br, want_branch, winner in cases:
            wizard(bundle, b_ui, b_br)
            wizard(user, u_ui, u_br)
            settings.set_branch("wcb", want_branch)
            paths._choice_cache.clear()
            got = "user" if paths.data_dir("webui_wcb") == user / "webui_wcb" else "bundle"
            if got != winner:
                problems.append(f"bundled {b_ui} ({b_br}), downloaded {u_ui} ({u_br}), branch {want_branch}: served "
                                f"the {got} copy, expected the {winner}'s")
        for b_at, u_at, winner in ((2000.0, 1000.0, "bundle"), (1000.0, 2000.0, "user"), (2000.0, None, "bundle"),
                                   (None, None, "user")):
            wiki(bundle, b_at)
            wiki(user, u_at)
            got = "user" if paths.data_subdir("wiki", "wcb") == user / "wiki" / "wcb" else "bundle"
            if got != winner:
                problems.append(f"wiki fetched at {b_at} (app) and {u_at} (download): served the {got}'s, expected "
                                f"the {winner}'s")
    finally:
        paths.FROZEN, paths.BUNDLE_DIR = saved
        paths._choice_cache.clear()
        settings.set_branch("wcb", "main")
        for d in (user / "webui_wcb", user / "wiki"):
            shutil.rmtree(d, ignore_errors=True)
        (user / "webui_wcb.source.json").unlink(missing_ok=True)
    check(not problems, "; ".join(problems))

# ------------------------------------------------------------------ group "tools_auto"
@case("the tools update themselves (Intellex 0.2.7: host._auto_update_tools_once, the pass its loop runs 30 s after "
      "start and every 5 min) against the Pages stand-in, the REAL fetcher doing the fetch as Update tools does: a "
      "config tool older than its published copy is fetched whole and byte for byte, the Wizard at its published stamp "
      "is not fetched; /_api/status's tools token moves for the config tool alone and webui-version reports "
      "autoUpdated with the new stamp; a second pass fetches nothing; a newer copy that will not download whole "
      "changes nothing and records nothing", group="tools_auto")
def tools_auto_update(ctx):
    import asyncio
    import subprocess
    import fetch_webui as fw
    import host
    from bridge_units import _server
    s = _server()
    problems = []
    runs = []

    def inproc(argv, **_k):
        """host's proc.run(_fetcher_argv(...)): fetch_webui.main() here, under _github_is, as the subprocess would."""
        runs.append(list(argv))
        rc, out = _run(argv[2:])
        return subprocess.CompletedProcess(argv, rc, out, "")

    def one_pass():
        host._fetch_lock = asyncio.Lock()          # bound to this pass's loop, not the server's
        return asyncio.run(host._auto_update_tools_once())

    def tools_token():
        code, body = s.req("GET", "/_api/status", timeout=30)
        return (body or {}).get("tools") or {}

    nc_old = _config_site(NC, "HIL-NC-OLD", "old")
    nc_new = _config_site(NC, "HIL-NC-NEW", "new")
    wz = _wizard_site(WCB, "HIL-WZ-SAME", "same")
    pages = _Pages()
    real_run = host.proc.run
    host.proc.run = inproc
    host._tools_auto.clear()
    try:
        with _github_is(pages) as refused, _scratch_tmp(ctx, "tools_auto"):
            _fresh_bundles()
            _branches("main", "main")
            _put(str(fw.WEBUI), _config_disk(nc_old, NC))
            _put(str(fw.WEBUI_WCB), _wizard_disk(wz, WCB))
            pages.serve({**nc_new, **wz})
            tok0 = tools_token()
            k = pages.mark()
            changed = one_pass()
            asked = pages.since(k)
            if changed != ["navicore"] or len(runs) != 1 or runs[0][runs[0].index("--tool") + 1] != "navicore":
                problems.append(f"a newer config tool: changed {changed}, fetcher runs {runs}; expected ['navicore'], "
                                f"one run of --tool navicore")
            if _tree(str(fw.WEBUI)) != _config_disk(nc_new, NC):
                problems.append("the config tool on disk after the pass: "
                                + _diff(_tree(str(fw.WEBUI)), _config_disk(nc_new, NC)))
            wz_files = [p for p in asked if p.startswith(f"{WCB}/") and not p.endswith("/Wizard/app.js")]
            if wz_files:
                problems.append(f"the Wizard, at its published stamp, was fetched: {wz_files[:4]}")
            tok1 = tools_token()
            if not tok0 or tok1.get("navicore") == tok0.get("navicore") or tok1.get("wcb") != tok0.get("wcb"):
                problems.append(f"/_api/status tools: {tok0} -> {tok1}; expected the config tool's token alone to move")
            code, wv = s.req("GET", "/_api/webui-version", timeout=60)
            auto = (wv or {}).get("autoUpdated") or {}
            if auto.get("version") != "HIL-NC-NEW" or ((wv or {}).get("wcb") or {}).get("autoUpdated") is not None:
                problems.append(f"webui-version autoUpdated {auto}, wcb {((wv or {}).get('wcb') or {}).get('autoUpdated')};"
                                f" expected HIL-NC-NEW and None")
            # Everything current: a second pass fetches nothing.
            runs.clear()
            if one_pass() != [] or runs:
                problems.append(f"a second pass with every tool current ran the fetcher: {runs}")
            # A newer copy missing a file: the fetch fails, the copy on disk and what was recorded stay as they were.
            broken = _config_site(NC, "HIL-NC-NEWER", "newer", drop=(f"{NC}/config_tool/flasher.js",))
            pages.serve({**broken, **wz})
            before_disk, before_auto = _tree(str(fw.WEBUI)), dict(host._tools_auto)
            runs.clear()
            got = one_pass()
            if got != [] or len(runs) != 1 or _tree(str(fw.WEBUI)) != before_disk or host._tools_auto != before_auto:
                problems.append(f"a newer copy that cannot download whole: changed {got}, runs {len(runs)}, disk "
                                f"{_diff(_tree(str(fw.WEBUI)), before_disk)}, recorded {host._tools_auto}")
            if refused:
                problems.append(f"reached for the network: {refused[:3]}")
    finally:
        host.proc.run = real_run
        host._tools_auto.clear()
        pages.close()
        _fresh_bundles()
    check(not problems, "; ".join(problems))


if __name__ == "__main__":
    sys.exit(_ixpy.main())
