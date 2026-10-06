"""Intellex's discovery and its WiFi bounce against NaviCore's own access point, under its venv (INTELLEX.md IX-WP9:
intellex.wifi_discover, intellex.wifi_bounce_scoped). The harness puts the PC's spare WiFi adapter on that access point
before either runs (suites/s45_navicore_wifi.py _on_ap, hil/wlan.py pc_on_ap) and takes it back afterwards.

discover: discover.scan([host]) and discover.probe(host) must call the host a NaviCore - an id-less PONG decides it
(Intellex src/discover.py probe, :263-413) - with the version NaviCore gave the harness over USB, reached from the
adapter's own address on the host's network (local_ip_for, a UDP connect that sends nothing). ssid_for_host(host) must
name the network that adapter is on (:475-532; two read-only netsh queries of Intellex's own). The harness hands this
script a SHA-256 of NaviCore's SSID and its length, never the name, so neither the name nor anything derived from it
beyond a match can reach this script's output or its results file.

bounce (attended, opt-in intellex_wifi_join): discover.wifi_bounce(ssid) (:535-608) disconnects the ONE adapter
associated with that network and connects it again through the profile Windows keeps under the network's name. The
SSID comes in the environment (IX_SSID), never in argv or args; every message reported here has it, and any other
network name netsh or Intellex quotes, replaced. The harness watches the adapters while this runs.

args: host (default 192.168.4.1); version (NaviCore's PONG over USB); ssid_sha, ssid_len (discover).
"""
import hashlib
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import SkipCase, case, check  # noqa: E402


def _host(ctx):
    return ctx.args.get("host") or "192.168.4.1"


def _on_net(ip, host):
    return bool(ip) and ip.rsplit(".", 1)[0] == host.rsplit(".", 1)[0]


def scrubbed(text, ssid):
    """`text` with `ssid` and every double-quoted name in it (netsh's and wifi_bounce's '<adapter> is on "<network>"')
    replaced by <network>."""
    text = re.sub(r'"[^"\r\n]*"', '"<network>"', str(text or ""))
    return text.replace(ssid, "<network>") if ssid else text


@case("scan and probe call NaviCore's access point a NaviCore on its USB version, reached from the adapter's lease",
      group="discover")
def discover_navicore(ctx):
    import discover
    host, want = _host(ctx), ctx.args.get("version")
    via = discover.local_ip_for(host)
    check(_on_net(via, host), f"this PC would reach {host} from {via or 'nothing'}, not from an address on its network: "
                              f"the harness's join did not hold")
    rows = discover.scan([host])
    check(len(rows) == 1, f"scan([{host}]) returned {len(rows)} rows, not 1")
    r = rows[0]
    shape = {k: r.get(k) for k in ("routable", "reachable", "kind", "isNaviCore", "isRelay", "isWcb", "isMesh")}
    check(r.get("kind") == "navicore" and r.get("isNaviCore") is True, f"scan calls {host} {shape}")
    check(r.get("routable") is True and r.get("reachable") is True, f"scan: {shape}")
    check(not r.get("isMesh") and not r.get("isRelay") and not r.get("isWcb"), f"scan calls NaviCore a doorway: {shape}")
    check(r.get("via") == via, f"scan went out {r.get('via')!r}, the routing table says {via!r}")
    check(r.get("version") == want, f"scan's version {r.get('version')!r}, NaviCore's PONG over USB {want!r}")
    check(r.get("relayId") is None and r.get("peers") == [], f"a NaviCore row carries relayId {r.get('relayId')!r} and "
                                                             f"{len(r.get('peers') or [])} peer(s): a droid, not a doorway")
    p = discover.probe(host)
    check(p.get("kind") == "navicore" and p.get("version") == want,
          f"probe({host}): kind {p.get('kind')!r}, version {p.get('version')!r} (want navicore, {want!r})")
    ctx.note(f"scan and probe: {host} is a NaviCore on {r.get('version')}, reached from {via}")


@case("ssid_for_host names the network the spare adapter holds its lease on (compared by SHA-256)", group="discover")
def discover_ssid(ctx):
    import discover
    if sys.platform != "win32":
        raise SkipCase("ssid_for_host reads netsh: elsewhere it answers None by design (Intellex src/discover.py), and "
                       "the macOS bounce finds its adapter by route instead")
    host = _host(ctx)
    t0 = time.monotonic()
    got = discover.ssid_for_host(host)
    secs = time.monotonic() - t0
    check(got is not None, f"ssid_for_host({host}) found no WiFi adapter holding an address on its network "
                           f"({secs:.1f} s)")
    match = hashlib.sha256(got.encode("utf-8")).hexdigest() == ctx.args.get("ssid_sha")
    check(match, f"ssid_for_host({host}) names another network than NaviCore's: {len(got)} characters, NaviCore's "
                 f"{ctx.args.get('ssid_len')} (neither quoted)")
    ctx.note(f"ssid_for_host({host}) names NaviCore's network ({len(got)} characters) in {secs:.1f} s")


@case("wifi_bounce disconnects and reconnects the one adapter on NaviCore's network, through Windows' profile",
      group="bounce")
def bounce(ctx):
    import discover
    ssid = os.environ.get("IX_SSID") or ""
    if not ssid:
        raise SkipCase("no IX_SSID in the environment: the harness names NaviCore's network there")
    t0 = time.monotonic()
    ok, msg = discover.wifi_bounce(ssid)
    shown = scrubbed(msg, ssid)[:200]
    ctx.note(f"wifi_bounce: {'ok' if ok else 'FAILED'} after {time.monotonic() - t0:.1f} s ({shown})")
    check(ok, f"wifi_bounce did not reconnect the adapter: {shown}")


if __name__ == "__main__":
    sys.exit(_ixpy.main())
