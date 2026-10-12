"""Intellex's GitHub proxy (src/ghproxy.py) on its ONLINE path, under Intellex's venv, with GitHub stood in for:
flash.reachable() answers True and flash._get() hands back a canned listing or image and records every URL it was asked
for. So the proxy stores what it "fetched" in the firmware cache - the staged src/firmware, where a non-frozen Intellex
writes it (paths.write_dir) - with no network at all.

group "ref" (intellex.gh_proxy_ref_dotdot, plan finding 20): the `ref` a page passes names the cache directory
(fwcache._key flattens only '/' and '\\') and, for raw(), a path segment of the raw.githubusercontent.com URL. A ref of
'..' filed the listing one level up, at the cache root, and '../../<owner>/<repo>/<branch>' walked the raw URL out of the
firmware repository. A ref that is not a branch name (settings.valid_branch, the rule the stored branch already obeys)
must be refused before anything is fetched or written; a real branch, 'feature/x' included, still works.
"""
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import case, check  # noqa: E402

OWNER, REPO, PATH = "greghulette", "Wireless_Communication_Board-WCB", "Code/bin"
NAME = "WCB_6.2.1_250646RSEP2026_hilproxy_ESP32.bin"
LISTING = ('[{"name": "%s", "path": "Code/bin/%s", "type": "file", "size": 6, '
           '"download_url": "https://raw.githubusercontent.com/x/y/main/Code/bin/%s"}]' % (NAME, NAME, NAME)).encode()


class _GitHub:
    """flash.reachable / flash._get stand-ins: every URL asked for, and canned bytes back."""

    def __init__(self):
        self.urls = []

    def get(self, url, _log=None):
        self.urls.append(url)
        return LISTING if "api.github.com" in url else b"IMAGE!"


def _files(root):
    out = set()
    for d, _, fs in os.walk(root):
        for f in fs:
            out.add(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/"))
    return out


def _online(ctx, fn):
    """Run fn(ghproxy) with GitHub stood in for -> (what it returned or raised, the URLs asked, files added under
    src/firmware)."""
    import flash
    import ghproxy
    fw = os.path.join(ctx.src, "firmware")
    os.makedirs(fw, exist_ok=True)
    before = _files(fw)
    gh = _GitHub()
    r0, g0 = flash.reachable, flash._get
    flash.reachable, flash._get = (lambda *a, **k: True), gh.get
    try:
        try:
            got = fn(ghproxy)
        except ghproxy.ProxyError as e:
            got = e
    finally:
        flash.reachable, flash._get = r0, g0
    return got, gh.urls, sorted(_files(fw) - before)


@case("control: online, a real branch's listing and image are fetched and cached under firmware/wcb/<branch>",
      group="ref")
def real_branches(ctx):
    for ref, key in (("main", "main"), ("feature/x", "feature__x"), ("v6.2.1-rc.1", "v6.2.1-rc.1")):
        got, urls, added = _online(ctx, lambda g: g.contents(OWNER, REPO, PATH, ref))
        check(not isinstance(got, Exception), f"contents ref {ref!r} was refused: {got}")
        body, cached = got
        check(cached is False and b"/_api/gh/raw?" in body, f"contents ref {ref!r}: not served live, or download_url "
                                                            f"not pointed back at the host")
        check(len(urls) == 1 and urls[0].endswith("?ref=" + urllib.parse.quote(ref)),
              f"contents ref {ref!r} asked GitHub for {urls}")
        check(added == [f"wcb/{key}/listing.json"], f"contents ref {ref!r} cached {added}, expected wcb/{key}/listing.json")
        got, urls, added = _online(ctx, lambda g: g.raw(OWNER, REPO, ref, NAME))
        check(not isinstance(got, Exception) and got[0] == b"IMAGE!", f"raw ref {ref!r}: {got}")
        check(len(urls) == 1 and f"/{OWNER}/{REPO}/{urllib.parse.quote(ref)}/{PATH}/{NAME}" in urls[0],
              f"raw ref {ref!r} asked GitHub for {urls}")
        check(f"wcb/{key}/{NAME}" in added and set(added) <= {f"wcb/{key}/{NAME}", "manifest.json"},
              f"raw ref {ref!r} cached {added}, expected wcb/{key}/{NAME} (and the manifest)")


@case("a ref that is a path trick ('..', '.', '../..', 'a/../..', '../../<owner>/<repo>/main') is refused before "
      "anything is fetched or written - neither the listing nor an image lands outside firmware/wcb/<branch>", group="ref")
def dotted_refs_refused(ctx):
    import ghproxy
    problems = []
    for ref in ("..", ".", "../..", "a/../..", "../../someone/elsewhere/main", "x/.", "./main"):
        for what, call in (("contents", lambda g, r=ref: g.contents(OWNER, REPO, PATH, r)),
                           ("raw", lambda g, r=ref: g.raw(OWNER, REPO, r, NAME))):
            got, urls, added = _online(ctx, call)
            if not isinstance(got, ghproxy.ProxyError):
                problems.append(f"{what} ref {ref!r} was served ({type(got).__name__})")
            if urls:
                problems.append(f"{what} ref {ref!r} asked GitHub for {urls[0]}")
            if added:
                problems.append(f"{what} ref {ref!r} wrote {added} under src/firmware (fwcache._key flattens only '/' "
                                f"and '\\\\': fwcache.py:81-89)")
    check(not problems, "; ".join(problems[:8]) + (f" (+{len(problems) - 8} more)" if len(problems) > 8 else ""))


if __name__ == "__main__":
    sys.exit(_ixpy.main())
