"""Intellex's offline-content fetchers (tools/fetch_firmware.py, tools/fetch_wiki.py) under its venv, with GitHub stood in
for: flash.reachable / wcb_flash.reachable answer True and every download is answered from this script, so nothing
reaches the network. The firmware lands in the staged src/firmware (where a non-frozen Intellex caches), the wikis in a
scratch directory inside the stage.

group "fetch_firmware" (intellex.fetch_firmware_unit): fetch_firmware.main() (:36-98) caches NaviCore's set and the WCB
sets for ESP32 and both ESP32-S3 flash sizes for the configured branches - app, table and the right bootloader each -
and reports a failure per product without aborting the other: a NaviCore listing that 404s exits 1 with the WCB sets
cached, a WCB listing that 404s exits 1 with NaviCore's cached; a build with no 8 MB S3 bootloader is a note, not a
failure.

group "fetch_wiki" (intellex.fetch_wiki_unit): fetch_wiki._targets reads both link dialects and image tags into pages
and assets, dropping a leading './' or '/' but keeping a leading '../' (_asset_path; it was lstrip("./"), which turned
'../x.png' into the wiki's own 'x.png'), _is_asset tells a file from a dotted page name; fetch_one crawls from Home and
_Sidebar when the page index cannot be read, skips a 404 page, leaves an image over 1 MB online (HEAD first), refuses
an asset path with a leading '../' out loud (a printed line and a problem, never requested) and one that escapes the
wiki's directory further in; _swap_in (:286-321) recovers a previous run's half-swap and puts
the old copy back when the final rename fails; main() (:338-381) swaps each wiki separately, keeps what is on disk for a
wiki that came back empty, and refuses at once offline.
"""
import contextlib
import io
import json
import os
import pathlib
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import case, check  # noqa: E402

TAG = "6.2.1_250646RSEP2026_hilfetch"
NC_TAG = "0.2.0_280000RSEP2026_hilfetch"


def _wcb_files():
    names = [f"WCB_{TAG}_{k}.bin" for k in ("ESP32", "ESP32_part", "ESP32_boot", "ESP32S3", "ESP32S3_part",
                                              "ESP32S3_boot_8MB", "ESP32S3_boot_16MB")]
    return {n: (n.encode() * 3) for n in names}


def _nc_files():
    import flash
    names = [f"NaviCore_{NC_TAG}_ESP32S3.bin", f"NaviCore_{NC_TAG}_ESP32S3_part.bin", flash.BOOTLOADER_NAME,
             "RC-Controller_1.0.0_ESP32S3.bin"]
    return {n: (n.encode() * 2) for n in names}


class _GitHub:
    """flash._get / wcb_flash._get: a listing per repo (or a 404), each file's bytes by name; every URL recorded."""

    def __init__(self, branch, wcb, nc, gone=()):
        import flash
        import wcb_flash
        self.urls = []
        self.repos = {}
        for repo, path, files in ((wcb_flash.GITHUB_REPO, wcb_flash.GITHUB_BIN_PATH, wcb),
                                  (flash.GITHUB_REPO, flash.GITHUB_BIN_PATH, nc)):
            base = f"https://raw.githubusercontent.com/greghulette/{repo}/{branch}/{path}/"
            listing = [{"name": n, "type": "file", "size": len(b), "download_url": base + n} for n, b in files.items()]
            self.repos[repo] = (json.dumps(listing).encode(), files)
        self.gone = set(gone)

    def get(self, url, _log=None, **_k):
        import flash
        self.urls.append(url)
        for repo, (listing, files) in self.repos.items():
            if f"/repos/greghulette/{repo}/contents/" in url:
                if repo in self.gone:
                    raise flash.FlashError(f"cannot reach GitHub: {url} -- HTTP Error 404: Not Found")
                return listing
            if f"/greghulette/{repo}/" in url and "raw.githubusercontent.com" in url:
                return files[url.rsplit("/", 1)[1]]
        raise flash.FlashError(f"HIL: no stand-in for {url}")


@contextlib.contextmanager
def _online(gh):
    import flash
    import wcb_flash
    saved = (flash.reachable, flash._get, wcb_flash.reachable, wcb_flash._get)
    flash.reachable = wcb_flash.reachable = lambda *a, **k: True
    flash._get = wcb_flash._get = gh.get
    try:
        yield gh
    finally:
        flash.reachable, flash._get, wcb_flash.reachable, wcb_flash._get = saved


def _run_main(module, argv):
    out = io.StringIO()
    saved = sys.argv
    sys.argv = [module.__name__] + list(argv)
    try:
        with contextlib.redirect_stdout(out):
            rc = module.main()
    finally:
        sys.argv = saved
    return rc, out.getvalue()


def _cached(ctx, product, branch):
    d = os.path.join(ctx.src, "firmware", product, branch.replace("/", "__"))
    return {n: open(os.path.join(d, n), "rb").read() for n in os.listdir(d)} if os.path.isdir(d) else {}


# ------------------------------------------------------------------ group "fetch_firmware"
@case("fetch_firmware caches NaviCore's set and the WCB sets for ESP32 and both ESP32-S3 sizes, and exits 0",
      group="fetch_firmware")
def fetch_all(ctx):
    import fetch_firmware
    import settings
    check(settings.set_branch("wcb", "hil-ff") is None and settings.set_branch("navicore", "hil-ff") is None,
          "could not set the stage's branches")
    wcb, nc = _wcb_files(), _nc_files()
    with _online(_GitHub("hil-ff", wcb, nc)) as gh:
        rc, out = _run_main(fetch_firmware, [])
    problems = []
    if rc != 0:
        problems.append(f"exited {rc}: {out[-300:]!r}")
    got = _cached(ctx, "wcb", "hil-ff")
    want = {n: b for n, b in wcb.items()}
    if {n: b for n, b in got.items() if n.endswith(".bin")} != want:
        problems.append(f"the WCB cache holds {sorted(n for n in got if n.endswith('.bin'))}, expected all of "
                        f"{sorted(want)} (ESP32 app/table/boot, S3 app/table and both sized bootloaders)")
    got = _cached(ctx, "navicore", "hil-ff")
    want_nc = {n: b for n, b in nc.items() if not n.startswith("RC-Controller")}
    if {n: b for n, b in got.items() if n.endswith(".bin")} != want_nc:
        problems.append(f"the NaviCore cache holds {sorted(n for n in got if n.endswith('.bin'))}, expected "
                        f"{sorted(want_nc)}")
    for label in ("WCB ESP32  (branch hil-ff)", "WCB ESP32S3 16 MB  (branch hil-ff)", "WCB ESP32S3 8 MB  (branch hil-ff)",
                  "NaviCore  (branch hil-ff)"):
        if label not in out:
            problems.append(f"no {label!r} in the fetcher's report")
    if "FAILED" in out:
        problems.append(f"a FAILED line in a run where everything was there: {out[-300:]!r}")
    if any(u.endswith("RC-Controller_1.0.0_ESP32S3.bin") for u in gh.urls):
        problems.append("another product's image in NaviCore's firmware/ was downloaded")
    check(not problems, "; ".join(problems))


@case("one product failing does not stop the other: NaviCore's listing 404 exits 1 with the WCB sets cached, the "
      "WCB listing 404 exits 1 with NaviCore's cached; a build with no 8 MB S3 bootloader is a note", group="fetch_firmware")
def fetch_partial(ctx):
    import fetch_firmware
    import flash
    import settings
    import wcb_flash
    problems = []
    for branch, gone, ok_product, bad_product in (("hil-ffnc", {flash.GITHUB_REPO}, "wcb", "navicore"),
                                                  ("hil-ffwcb", {wcb_flash.GITHUB_REPO}, "navicore", "wcb")):
        settings.set_branch("wcb", branch)
        settings.set_branch("navicore", branch)
        with _online(_GitHub(branch, _wcb_files(), _nc_files(), gone=gone)):
            rc, out = _run_main(fetch_firmware, [])
        n_ok = len([n for n in _cached(ctx, ok_product, branch) if n.endswith(".bin")])
        if rc != 1:
            problems.append(f"{bad_product} listing 404: exited {rc}, expected 1")
        if n_ok != (7 if ok_product == "wcb" else 3):
            problems.append(f"{bad_product} listing 404: {n_ok} {ok_product} image(s) cached - the failure stopped the "
                            f"other product")
        if _cached(ctx, bad_product, branch):
            problems.append(f"{bad_product} listing 404: something was cached for it anyway")
        if "FAILED" not in out or "404" not in out:
            problems.append(f"{bad_product} listing 404: the report does not say so ({out[-200:]!r})")

    branch = "hil-ffno8"
    settings.set_branch("wcb", branch)
    settings.set_branch("navicore", branch)
    wcb = {n: b for n, b in _wcb_files().items() if "boot_8MB" not in n}
    with _online(_GitHub(branch, wcb, _nc_files())):
        rc, out = _run_main(fetch_firmware, [])
    if rc != 0 or "note: no 8 MB ESP32-S3 bootloader in this build" not in out:
        problems.append(f"a build with no 8 MB bootloader: exited {rc}, report {out[-300:]!r}; expected 0 with a note")
    settings.set_branch("wcb", "main")
    settings.set_branch("navicore", "main")
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "fetch_wiki"
@case("_targets reads [[Text|Target]], [[Page Name]], [text](Page.md), [text](Page#anchor) and <img src>; skips "
      "external links and anchors; drops a leading ./ or / from an asset path but keeps a leading ../ for fetch_one to "
      "refuse; _is_asset tells a file from a dotted page name", group="fetch_wiki")
def targets(ctx):
    import fetch_wiki
    text = ("Intro [[Shown|Target-B]] and [[Page Name]], see [c](C.md) or [part](Some-Page#section).\n"
            "[ext](https://example.com/x) [top](#top) [mail](mailto:a@example.com) [v](version3.2_build)\n"
            '<img src="Images/p.png" width=10> ![q](Images/q.jpg) <IMG SRC=\'./Images/r.gif\'>\n'
            "![root](/Images/s.png) ![up](../Outside/u.png) ![up2](./../w.png) ![dot](.dotted.png)\n")
    pages, assets = fetch_wiki._targets(text)
    want_pages = {"Target-B", "Page-Name", "C", "Some-Page", "version3.2_build"}
    # A leading ../ is kept as written (it was stripped to 'Outside/u.png' and 'w.png', paths inside the wiki the page
    # never named), and a name that merely starts with a dot keeps it (lstrip("./") made it 'dotted.png').
    want_assets = {"Images/p.png", "Images/q.jpg", "Images/r.gif", "Images/s.png", "../Outside/u.png", "../w.png",
                   ".dotted.png"}
    problems = []
    if pages != want_pages:
        problems.append(f"pages {sorted(pages)}, expected {sorted(want_pages)}")
    if assets != want_assets:
        problems.append(f"assets {sorted(assets)}, expected {sorted(want_assets)}")
    for link, want in (("version3.2_build", False), ("Images/a.png", True), ("Home.md", False), ("v1.2", False),
                       ("clip.MP4", True), ("notes.tar.gz", True), ("Setup", False)):
        if fetch_wiki._is_asset(link) is not want:
            problems.append(f"_is_asset({link!r}) is {not want}")
    check(not problems, "; ".join(problems))


def _tree(d):
    out = {}
    for root, _, fs in os.walk(d):
        for f in fs:
            p = os.path.join(root, f)
            out[os.path.relpath(p, d).replace(os.sep, "/")] = open(p, encoding="utf-8").read()
    return out


def _put(d, files):
    for rel, text in files.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)


@case("_swap_in replaces a wiki with nothing left beside it, restores a previous run's half-swap (.prev, no wiki), "
      "and puts the old copy back when the final rename fails", group="fetch_wiki")
def swap(ctx):
    import shutil
    import fetch_wiki
    root = os.path.join(ctx.stage, "scratch", "swap")
    shutil.rmtree(root, ignore_errors=True)
    dest = os.path.join(root, "wiki", "wcb")
    problems = []
    _put(dest, {"Old.md": "old"})
    _put(os.path.join(root, "staged1"), {"New.md": "new"})
    fetch_wiki._swap_in(pathlib.Path(root, "staged1"), pathlib.Path(dest))
    if _tree(dest) != {"New.md": "new"} or sorted(os.listdir(os.path.join(root, "wiki"))) != ["wcb"]:
        problems.append(f"a plain swap left {_tree(dest)}, beside it {sorted(os.listdir(os.path.join(root, 'wiki')))}")

    # A previous run died mid-swap: wcb.prev holds the good copy and wcb is gone. This run's move then fails.
    shutil.rmtree(dest)
    _put(dest + ".prev", {"Good.md": "good"})
    try:
        fetch_wiki._swap_in(pathlib.Path(root, "no-such-staged"), pathlib.Path(dest))
        problems.append("a swap from a staged directory that does not exist did not raise")
    except OSError:
        pass
    if _tree(dest) != {"Good.md": "good"}:
        problems.append(f"after a half-swap and a failed move the wiki is {_tree(dest)}, expected the .prev copy restored")

    # The final rename fails: the old copy is put back, nothing half-made is left under the wiki's name.
    _put(os.path.join(root, "staged2"), {"Newer.md": "newer"})
    real = pathlib.Path.rename

    def rename(self, target):
        if self.name.endswith(".new"):
            raise OSError(16, "HIL: the directory is held open")
        return real(self, target)
    pathlib.Path.rename = rename
    try:
        fetch_wiki._swap_in(pathlib.Path(root, "staged2"), pathlib.Path(dest))
        problems.append("a failing final rename did not raise")
    except OSError:
        pass
    finally:
        pathlib.Path.rename = real
    if _tree(dest) != {"Good.md": "good"}:
        problems.append(f"after a failing final rename the wiki is {_tree(dest)}, expected the old copy put back")
    check(not problems, "; ".join(problems))


class _Wiki:
    """fetch_wiki's network: page_index, _get and _size_of answered from {page: markdown} and {asset: size}."""

    def __init__(self, pages, assets, index=()):
        self.pages, self.assets, self.index = pages, assets, list(index)
        self.got, self.heads = [], []

    def page_index(self, owner, repo):
        return list(self.index)

    def get(self, url, timeout=20.0):
        self.got.append(url)
        name = urllib.parse.unquote(url.rsplit("/", 1)[1])
        if name.endswith(".md") and name[:-3] in self.pages:
            return self.pages[name[:-3]].encode()
        for a, size in self.assets.items():
            if url.endswith(a):
                return b"x" * size
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    def size_of(self, url, timeout=15.0):
        self.heads.append(url)
        for a, size in self.assets.items():
            if url.endswith(a):
                return size
        return 0


@contextlib.contextmanager
def _wiki_net(net):
    import fetch_wiki
    saved = (fetch_wiki.page_index, fetch_wiki._get, fetch_wiki._size_of)
    fetch_wiki.page_index, fetch_wiki._get, fetch_wiki._size_of = net.page_index, net.get, net.size_of
    try:
        yield net
    finally:
        fetch_wiki.page_index, fetch_wiki._get, fetch_wiki._size_of = saved


@case("fetch_one with no page index crawls Home and _Sidebar in both dialects, skips a 404 page, leaves an image over "
      "1 MB online (asked with HEAD, never fetched) and refuses an asset path that leaves the wiki - a leading ../ "
      "with a line of its own, never re-read as a path inside it", group="fetch_wiki")
def crawl(ctx):
    import shutil
    import fetch_wiki
    staged = pathlib.Path(ctx.stage, "scratch", "crawl", "wcb")
    shutil.rmtree(staged.parent, ignore_errors=True)
    staged.mkdir(parents=True)
    # ../outside.png is served by the stand-in GitHub at the wiki's own outside.png too: stripped to that (as it was),
    # it would be requested and written; refused, it never is.
    net = _Wiki(pages={
        "Home": "[[Getting Started]] and [Wiring](Wiring) <img src=\"Images/big.png\"> ![s](Images/small.png) "
                "[esc](Images/../../../escape.png) ![up](../outside.png)",
        "_Sidebar": "[[Home]] [[Linked But Missing]]",
        "Getting-Started": "back to [[Home]]",
        "Wiring": "see [Getting Started](Getting-Started)",
    }, assets={"Images/big.png": fetch_wiki.MAX_ASSET_BYTES + 1, "Images/small.png": 10, "escape.png": 10,
               "outside.png": 10})
    printed = io.StringIO()
    with _wiki_net(net), contextlib.redirect_stdout(printed):
        pages, assets, problems_seen = fetch_wiki.fetch_one("wcb", staged)
    on_disk = sorted(str(p.relative_to(staged)).replace(os.sep, "/") for p in staged.rglob("*") if p.is_file())
    problems = []
    if pages != 4 or assets != 1:
        problems.append(f"fetch_one counted {pages} page(s), {assets} asset(s); expected 4 and 1")
    want = ["Getting-Started.md", "Home.md", "Images/small.png", "Wiring.md", "_Sidebar.md"]
    if on_disk != want:
        problems.append(f"staged {on_disk}, expected {want}")
    for words in ("page index unreadable - crawled links instead", "refused (path escapes the wiki directory)",
                  "1 image(s) over 1024 KB left online", "../outside.png: refused (a leading ../ leaves the wiki)"):
        if not any(words in p for p in problems_seen):
            problems.append(f"no {words!r} among the problems reported: {problems_seen}")
    if "[wcb] refused asset ../outside.png: a leading ../ leaves the wiki" not in printed.getvalue():
        problems.append(f"the ../ refusal was not logged as it happened: printed {printed.getvalue()[-300:]!r}")
    if any("outside.png" in u for u in net.got + net.heads):
        problems.append("the leading-../ asset was requested (as the wiki's own outside.png)")
    if any("outside" in p for p in on_disk) or os.path.exists(staged.parent / "outside.png"):
        problems.append("the leading-../ asset was written")
    if any(u.endswith("Images/big.png") for u in net.got):
        problems.append("the image over 1 MB was downloaded, not only asked about with HEAD")
    if any("escape.png" in u for u in net.got + net.heads):
        problems.append("the asset path that leaves the wiki was requested")
    if os.path.exists(staged.parent.parent / "escape.png") or any("escape" in p for p in on_disk):
        problems.append("the escaping asset was written")
    if any("Linked-But-Missing" in p for p in problems_seen):
        problems.append(f"a 404 page was reported as a problem: {problems_seen}")
    check(not problems, "; ".join(problems))


@case("main() swaps each wiki in separately, keeps what is on disk for one that came back empty (exit 1), and offline "
      "refuses at once with nothing touched", group="fetch_wiki")
def main_per_wiki(ctx):
    import shutil
    import fetch_wiki
    dest = pathlib.Path(ctx.stage, "scratch", "wikimain")
    shutil.rmtree(dest, ignore_errors=True)
    for product in fetch_wiki.WIKIS:
        _put(str(dest / product), {"Home.md": f"old {product}"})
    real = (fetch_wiki.DEST, fetch_wiki.reachable, fetch_wiki.fetch_one)

    def fetch_one(product, staged):
        if product == "navicore":
            return 0, 0, ["page index unreadable - crawled links instead"]       # nothing came back
        (staged / "Home.md").write_text(f"new {product}", encoding="utf-8")
        return 1, 0, []
    problems = []
    fetch_wiki.DEST, fetch_wiki.fetch_one = dest, fetch_one
    try:
        fetch_wiki.reachable = lambda *a, **k: True
        rc, out = _run_main(fetch_wiki, ["--wiki", "all"])
        got = {p: _tree(str(dest / p)) for p in fetch_wiki.WIKIS}
        want = {p: {"Home.md": ("old " if p == "navicore" else "new ") + p} for p in fetch_wiki.WIKIS}
        if rc != 1 or got != want:
            problems.append(f"one wiki empty: exited {rc}, on disk {got}; expected 1, that wiki kept and the others new")
        if "[navicore] nothing downloaded — keeping what is on disk" not in out:
            problems.append("no '[navicore] nothing downloaded - keeping what is on disk' line")
        before = {p: _tree(str(dest / p)) for p in fetch_wiki.WIKIS}
        fetch_wiki.reachable = lambda *a, **k: False
        rc, out = _run_main(fetch_wiki, ["--wiki", "all"])
        if rc != 1 or "github.com is not reachable — the bundled docs are unchanged." not in out:
            problems.append(f"offline: exited {rc}, {out[:120]!r}")
        if {p: _tree(str(dest / p)) for p in fetch_wiki.WIKIS} != before:
            problems.append("offline, the wikis on disk changed")
    finally:
        fetch_wiki.DEST, fetch_wiki.reachable, fetch_wiki.fetch_one = real
    check(not problems, "; ".join(problems))


if __name__ == "__main__":
    sys.exit(_ixpy.main())
