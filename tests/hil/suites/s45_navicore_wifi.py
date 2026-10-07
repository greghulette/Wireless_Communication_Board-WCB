"""NaviCore's SoftAP and WebSocket endpoint, with the PC on its access point (docs/hil_plan/NAVICORE.md NC-WP8, ids
ncwifi.*, opt-in navicore_wifi).

For each test the PC's spare WiFi adapter joins NaviCore's access point (hil/wlan.py pc_on_ap, as s28's tests join W1's)
and talks to ws://192.168.4.1/ws through hil/ncws.py NcWs, the endpoint as a line device the INF1 driver runs over:
NaviCore(NcWs(ip)) beside NaviCore(bench.dev("navicore")). The PC goes back to its own network afterwards. D-NC14: the
tests run only on a spare adapter, so the PC stays online; a PC whose only WiFi adapter carries its default route skips
them. Three also need navicore_reboot, checked in the body: ap_boot_lines and refuse_short_password restart NaviCore,
and ws_stalled_client may, if D-NC62's out-of-bounds write crashes it.

Off Windows (the Mac) the harness joins nothing: the spare adapter must already be on NaviCore's access point, and each
test first proves it is NaviCore's (_navicore_ap: its socket's PONG is the one NaviCore gives over USB), since every
board's access point is 192.168.4.1 and the SSID cannot be read there. After a restart the adapter has to come back by
itself. refuse_short_password skips there: its scan and its join attempt are the PC's own.

What the endpoint is (navicore_wsserver.h; hil/ncws.py's docstring has the detail): a second mouth for the command
surface USB speaks (processInputLine), and a CONSOLE MIRROR - everything the loop core prints while a client is
connected goes to every client, replies to USB and to other sockets included, and NaviCore's USB sees the replies to
what a socket sends. So wherever both transports are in play a test takes a barrier first (_sync): a line only that
transport answers, and the '#L12' poke's reply after it; everything printed before it has then arrived there.

Rules every test here keeps:
- NaviCore's access point name and password come from its own GET_CONFIG at run time (wifiSsid, wifiPassword:
  rc_config.h:1235-1237; hil/checkpoint.py SHOWN_AS_HASH and SECRET_KEY name them) and are handed only to Windows'
  temporary profile, which pc_on_ap deletes. Neither is printed, noted, stored or put in a message: SSIDs are compared
  by value, and no message quotes a CONFIG line (hil/ncws.py shown()). NcWs logs every line through redact_text, as
  Bench.log does for NaviCore's USB, so session.log holds the passwords only as hashes (the SSID in a CONFIG line stays
  as USB logs it, D49).
- A config change goes through nc_guard over USB (hil/nc_guard.py): only refuse_short_password saves one (a 3-character
  AP password for one boot), and it writes the snapshot back and restarts NaviCore onto it however its body ends. The
  tests that start the monitor or set debug flags run inside nc_guard too, which stops both however the test ends.
- NaviCore's endpoint holds three sockets (navicore_wsserver.h:73, :488); a test opens a fourth only to see the oldest
  evicted, and closes each client it is done with.
- Heap: NaviCore reports none (no ?STATS or #L code prints it; tracker #111 is the WCB's AP heap); ap_boot_lines notes
  the one memory figure its boot prints, the free PSRAM. It is an ESP32-S3 with 8 MB of PSRAM, where the endpoint's
  buffers live (:410-413).
A restore by hand, if a run dies mid-test: on the PC `netsh wlan delete profile name=HIL-<NaviCore's SSID>
interface=<adapter>` and reconnect the adapter; NaviCore's config comes back through the resume (navicore_snapshot.json).
"""
import hashlib
import json
import re
import secrets
import string
import time
from contextlib import contextmanager

from hil import ncflash, optin, wlan
from hil.checkpoint import redact_text, redact_tokens
from hil.intellex import handed_over
from hil.nc_guard import nc_guard
from hil.navicore import DBG_WCB, SBUS_FULL_FPS, NaviCore, parse_mae, parse_pwm_update, parse_wdp
from hil.ncws import NcWs
from hil.runner import Skip, test
from hil.wcb import PULL_MAX, WCB, pull_config
from hil.wlan import (NOT_WINDOWS_SKIP, SPARE_ONLY_SKIP, default_routes, internet_adapter, networks, pc_on_ap,
                      pick_adapter)
from suites.common import Console, link, marker, usb_wcb
from suites.s02_navicore import _bench_wcb_ids
from suites.s03_wcb import _factory_reply, _reply_problems
from suites.s20_ota import _crc, _crun, _session_id
from suites.s20_ota import _status as _wcb_status
from suites.s40_navicore_config import _inert_keys
from suites.s41_navicore_engine import _act, _hosted_remote_slot
from suites.s44_navicore_devices import _await_mae
from suites.s46_navicore_boot import _line1, _restart, _restartable, _restarted, _uptime_ms, banner_lines

NC_AP_IP = "192.168.4.1"        # WiFi.softAPIP(): NaviCore never calls softAPConfig (NaviCore.ino:4758-4759)
WS_MAX_CLIENTS = 3              # navicore_wsserver.h:73; httpd max_open_sockets 3 with LRU purge (:488-489)
WS_FRAME_MAX = 98304            # a frame this long or longer fails the handler, which closes the socket (:406-408)
WS_ACC_MAX = 98304 + 2048       # a socket's unfinished line past this is dropped and the socket closed (:436-438)
WS_SINK = 2048                  # WsSink's buffer: a flush at every 2048 bytes of output (:154-167, :259)
SEND_WAIT_S = 5                 # HTTPD_DEFAULT_CONFIG's send_wait_timeout, which begin() keeps (:478-492)
MONITOR_HZ = 20                 # a PWM_UPDATE every WS_MONITOR_INTERVAL_MS, 50 ms (NaviCore.ino:509, :3131-3135)
UNKNOWN = "Unknown command: "   # every '?' line nobody owns echoes itself (NaviCore.ino:3814-3816)
PING = {"type": "PING"}
PING_LINE = '{"type":"PING"}'
PONG = re.compile(r'^\{"type":"PONG","version":"([^"]+)"\}$')                       # NaviCore.ino:3855-3863
QUEUE_FULL = "[WS] command queue full - dropped a line"                             # navicore_wsserver.h:464
# The SoftAP block of setup() (NaviCore.ino:4711-4792) and naviws::begin() (navicore_wsserver.h:478-510).
SOFTAP = re.compile(r'^\[WIFI\] SoftAP "(.*)" up on channel (\d+) — (\d+\.\d+\.\d+\.\d+)$')   # :4727-4728
ESPNOW_SHARE = "[WIFI] ESP-NOW will share this channel (WIFI_AP_STA)."                   # :4729
DHCP_NO_GW = "[WIFI] DHCP offers no default gateway — clients keep their own route."     # :4781
WS_READY = re.compile(r"^\[WS\] command endpoint ready — ws://(\d+\.\d+\.\d+\.\d+)/ws$")  # navicore_wsserver.h:507-508
REFUSED_SHORT = re.compile(r"^\[WIFI\] REFUSED: password is (\d+) character\(s\); WPA2 requires 8\.$")   # :4714
NOT_OPEN = "[WIFI] Not starting an open AP — set a longer password and reboot."          # :4715
AP_FAILS = ("FAILED", "could not", "not ready", "REFUSED", "disabled", "alloc failed")    # :4714-4791; wsserver :480, :495
PSRAM = re.compile(r"^\[MEM\] rcConfig \((\d+) bytes\) allocated in PSRAM, free PSRAM now (\d+)")
DISPATCH_WCB = "[DISPATCH] WCB→{}  {}"                                              # NaviCore.ino:2059
COEXIST_S = 40                  # mesh_coexist's streaming window
SOAK_S = 300                    # ws_ping_soak: five minutes (NAVICORE.md nc.ws.ping_serialisation)
STALL_S = 3 * SEND_WAIT_S       # ws_stalled_client: room for two send timeouts on the stalled socket, and a margin
STALL_RCVBUF = 4096             # the stalled client's receive buffer: its window fills within a second of the monitor
UTF8_CHARS = ("é", "€", "\U0001F600")    # 2-, 3- and 4-byte characters (é, €, an emoji)


def _nc(bench):
    return NaviCore(bench.dev("navicore"))


def _opted(bench, key):
    return key in optin.enabled(bench.cfg)


def _reboot_too(bench):
    """Skip unless navicore_reboot is ticked as well: for the tests gated on navicore_wifi that restart NaviCore."""
    if not _opted(bench, "navicore_reboot"):
        raise Skip(optin.skip_reason("navicore_reboot"))


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def echo(tag):
    """What NaviCore prints for the line '?<tag>' (NaviCore.ino:3814-3816): no '?' command owns a HIL marker."""
    return f"{UNKNOWN}?{tag}"


def _echo_rx(tag):
    return "^" + re.escape(echo(tag)) + "$"


# ------------------------------------------------------------------ pure helpers (selftest.py feeds them)
def ap_of(cfg):
    """(ssid, password) NaviCore's access point runs under, from GET_CONFIG `cfg`, as setup() derives them
    (NaviCore.ino:4711-4726): wifiSsid, or 'NaviCore-<deviceId>' when it is empty; None when no access point comes up
    at boot - wifiEnabled false, or a password under 8 bytes, which is refused (:4712-4718; strlen counts bytes).
    Neither value is ever printed."""
    if not cfg.get("wifiEnabled"):
        return None
    pw = cfg.get("wifiPassword") or ""
    if len(pw.encode("utf-8")) < 8:
        return None
    return cfg.get("wifiSsid") or f"NaviCore-{(cfg.get('wcbNetwork') or {}).get('deviceId')}", pw


def route_problems(before, during, name):
    """The DHCP no-gateway check (NaviCore.ino:4731-4783) on the PC's default routes, 'InterfaceAlias|NextHop' strings
    (hil/wlan.py default_routes): none on the adapter on NaviCore's access point, and every other adapter's as before.
    Messages give next hops on the joined adapter and counts elsewhere: another adapter's routes are the PC's."""
    if before is None or during is None:
        return ["the PC's default routes could not be read (hil/wlan.py default_routes)"]
    mine = [r for r in during if r.startswith(name + "|")]
    others = [r for r in during if not r.startswith(name + "|")]
    was = [r for r in before if not r.startswith(name + "|")]
    out = []
    if mine:
        out.append(f"NaviCore's DHCP gave the adapter a default route (next hop {[r.split('|', 1)[1] for r in mine]})")
    if others != was:
        out.append(f"the other adapters' default routes changed while on NaviCore's access point ({len(was)} -> "
                   f"{len(others)} routes)")
    return out


def intellex_verdict(lines):
    """What Intellex's discovery makes of a host from these socket lines, as src/discover.py probe() reads them
    (:323-412): an id-less PONG is decisive ('navicore', read no further); an id-bearing PONG counts only when its id is
    the SELF row's (PEER=3); a SELF row alone is a 'relay' when a '[relay]' line came, else a 'wcb'; nothing is
    'unknown'. -> (kind, PONG version, SELF row's N)."""
    direct, mesh_id, version, self_id, said_relay = False, None, None, None, False
    for raw in lines:
        line = raw.strip()
        if line.startswith("[WDP:END"):
            break
        if line.startswith("[relay]"):
            said_relay = True
            continue
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "PONG":
                version = str(obj.get("version", "unknown"))
                if "id" in obj:
                    mesh_id = str(obj.get("id"))
                else:
                    direct = True
                    break
            continue
        if line.startswith("[WDP:"):
            body = line[line.index(":") + 1:].rstrip("]")
            f = {k.strip(): v.strip() for k, _, v in (p.partition("=") for p in body.split(",") if "=" in p)}
            if f.get("PEER") == "3":
                self_id = f.get("N")
    if direct or (mesh_id is not None and mesh_id == self_id):
        return "navicore", version, self_id
    if self_id is not None or mesh_id is not None:
        return ("relay" if said_relay else "wcb"), version, self_id
    return "unknown", version, self_id


def ap_block(banner, cfg):
    """The access point's block of a boot banner (banner_lines) against GET_CONFIG `cfg` -> (problems, facts). For an
    access point the config brings up (ap_of): the SoftAP line with the config's SSID on wcbNetwork.channel at
    192.168.4.1, then ESP-NOW sharing the channel, DHCP with no gateway (NaviCore.ino:4727-4781) and the WebSocket
    endpoint at the same address (navicore_wsserver.h:507-508), in that order, and no [WIFI] or [WS] line carrying a
    failure word (AP_FAILS). The SSID is compared and never quoted, so no [WIFI] line is quoted. facts: 'ip' and
    'psram_free', the [MEM] line's free PSRAM, the only memory figure NaviCore prints."""
    ssid, _ = ap_of(cfg) or ("", "")
    ch = (cfg.get("wcbNetwork") or {}).get("channel")
    problems, facts, at = [], {"ip": None, "psram_free": None}, {}
    for i, x in enumerate(banner):
        m = PSRAM.match(x)
        if m:
            facts["psram_free"] = int(m.group(2))
        m = SOFTAP.match(x)
        if m and "softap" not in at:
            at["softap"] = i
            facts["ip"] = m.group(3)
            if m.group(1).encode("utf-8") != ssid.encode("utf-8"):
                problems.append("the SoftAP line names another network than the config's SSID (neither quoted)")
            if int(m.group(2)) != ch:
                problems.append(f"the SoftAP is on channel {m.group(2)}, the mesh on {ch}")
            if m.group(3) != NC_AP_IP:
                problems.append(f"the SoftAP is at {m.group(3)}, not {NC_AP_IP}")
        elif x == ESPNOW_SHARE:
            at.setdefault("espnow", i)
        elif x == DHCP_NO_GW:
            at.setdefault("dhcp", i)
        else:
            m = WS_READY.match(x)
            if m and "ws" not in at:
                at["ws"] = i
                if facts["ip"] and m.group(1) != facts["ip"]:
                    problems.append(f"the WebSocket endpoint is at {m.group(1)}, the SoftAP at {facts['ip']}")
        if x.startswith(("[WIFI]", "[WS]")) and any(w in x for w in AP_FAILS):
            problems.append("a [WIFI] failure line (not quoted: it can name the network)" if x.startswith("[WIFI]")
                            else f"a failure line: {redact_text(x)[:120]!r}")
    order = ("softap", "espnow", "dhcp", "ws")
    names = {"softap": "the SoftAP line", "espnow": f"{ESPNOW_SHARE!r}", "dhcp": f"{DHCP_NO_GW!r}",
             "ws": "the '[WS] command endpoint ready' line"}
    for k in order:
        if k not in at:
            problems.append(f"missing: {names[k]}")
    seen = [at[k] for k in order if k in at]
    if seen != sorted(seen):
        problems.append("the access point's lines are out of order (SoftAP, ESP-NOW, DHCP, WebSocket)")
    return problems, facts


def refused_block(banner, n):
    """A banner whose saved AP password is `n` bytes (1-7): the two REFUSED lines (NaviCore.ino:4713-4715), and no
    SoftAP or WebSocket line, since nothing brings them up (:4719-4788) -> problems."""
    problems = []
    k = next((i for i, x in enumerate(banner) if REFUSED_SHORT.match(x)), None)
    if k is None:
        problems.append("no '[WIFI] REFUSED: password is <n> character(s); WPA2 requires 8.' line")
    else:
        got = int(REFUSED_SHORT.match(banner[k]).group(1))
        if got != n:
            problems.append(f"the REFUSED line counts {got} characters, the saved password has {n}")
        if k + 1 >= len(banner) or banner[k + 1] != NOT_OPEN:
            problems.append(f"{NOT_OPEN!r} does not follow the REFUSED line")
    if any(SOFTAP.match(x) for x in banner):
        problems.append("a SoftAP came up although its password is too short")
    if any(WS_READY.match(x) for x in banner):
        problems.append("the WebSocket endpoint started although no access point came up")
    return problems


def utf8_lines(tag):
    """Lines whose 'Unknown command:' echo is over 2 KB of 2-, 3- or 4-byte characters, each run shifted by 0-2 ASCII
    characters, so WsSink's flush at 2048 bytes (navicore_wsserver.h:154-167) lands inside a character for most of them
    and pump() must hold its first bytes back for the next frame (:196-208, :236-252) -> [line]."""
    return [f"?{tag}{'x' * pad}" + ch * (3000 // len(ch.encode("utf-8"))) for ch in UTF8_CHARS for pad in range(3)]


def frame_problems(frames):
    """TEXT frames (bytes) that are not valid UTF-8 on their own (RFC 6455 §8.1; the endpoint's promise, :198-208) ->
    [problem]: one line, counting them."""
    bad = 0
    for f in frames:
        try:
            f.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            bad += 1
    return [f"{bad} of {len(frames)} text frame(s) are not valid UTF-8 on their own"] if bad else []


def held_back(frames):
    """How many frames stop short of a flush's 2048 bytes by 1-3 and are followed by one that starts with a UTF-8 lead
    byte: the trace of pump() holding a cut character back (:207). A count for the note, not an assertion: where a flush
    falls also depends on what the sink held before."""
    n = 0
    for a, b in zip(frames, frames[1:]):
        if WS_SINK - 3 <= len(a) < WS_SINK and b and (b[0] & 0xC0) == 0xC0:
            n += 1
    return n


def data_line(offset=1048576):
    """The config tool's line for one 1024-byte ?OTALOCAL chunk: '?OTALOCAL,DATA,<offset>,<base64>', 1391 characters at
    a 7-digit offset. Its bytes are zeros; the offset is never the session's cursor, so no session could write it."""
    import base64
    return f"?OTALOCAL,DATA,{offset}," + base64.b64encode(bytes(1024)).decode("ascii")


# ------------------------------------------------------------------ the PC on NaviCore's access point
def navicore_home(bench):
    """Where the Mac's spare adapter rests between tests, for pc_on_ap's home= (hil/wlan.py realtek_on_ap): (NaviCore's
    SSID, "NaviCore's", its identify), or None when NaviCore is not on this bench or hosts no access point."""
    if "navicore" not in (bench.cfg.get("devices") or {}):
        return None
    try:
        ap = ap_of(_nc(bench).config())
    except AssertionError:
        return None
    return None if ap is None else (ap[0], "NaviCore's", _navicore_ap(bench))


@contextmanager
def _on_ap(bench, problems):
    """The PC on NaviCore's access point for the block (hil/wlan.py pc_on_ap, a spare adapter only, D-NC14) -> (nc,
    cfg, the adapter's name): nc NaviCore's USB driver, cfg its GET_CONFIG. Skip unless NaviCore hosts an access point
    (ap_of)."""
    nc = _nc(bench)
    cfg = nc.config()
    ap = ap_of(cfg)
    if ap is None:
        raise Skip("NaviCore hosts no access point: wifiEnabled is off or its AP password is under 8 characters")
    ssid, pw = ap
    with pc_on_ap(bench, problems, ssid, pw, "NaviCore's", spare_only=True, reach=(NC_AP_IP, 80),
                  identify=_navicore_ap(bench)) as name:
        yield nc, cfg, name


def _navicore_ap(bench, version=None):
    """pc_on_ap's identify off Windows (hil/wlan.py _prejoined), where the spare adapter is already on an access point
    the harness cannot name: None when ws://192.168.4.1/ws, reached through it, answers PING with NaviCore's own PONG -
    `version`, or what NaviCore's USB answers now - else what it answered instead. The socket open is retried for 10 s
    (_open): a restarted NaviCore takes a TCP connect a moment before its endpoint answers."""
    def identify(name):
        want = version or _nc(bench).ping()
        try:
            ws = _open(bench, name="ncws-id", wait=10.0)
        except AssertionError as e:
            return _line1(e)
        try:
            m = ws.mark()
            ws.send(PING_LINE)
            got = ws.expect(PONG.pattern, timeout=4, since=m).group(1)
        except AssertionError:
            return f"ws://{NC_AP_IP}/ws answered no NaviCore PONG within 4 s"
        finally:
            ws.close()
        return None if got == want else f"its PONG names {got}, NaviCore's over USB {want}"
    return identify


def _spare_adapter(bench):
    """The WiFi adapter pc_on_ap would take (hil/wlan.py pick_adapter), checked before a test restarts NaviCore, so a
    PC with no spare adapter skips before anything changes -> the adapter; Skip otherwise, with pc_on_ap's reasons."""
    adapter, why = pick_adapter(bench)
    if not adapter:
        raise Skip("this PC has no WiFi adapter" if not why else "bench.json wifi_test_interface names no WiFi adapter "
                                                                 "here")
    if adapter["name"] == internet_adapter():
        raise Skip(SPARE_ONLY_SKIP)
    return adapter


def _open(bench, name="ncws", wait=15.0, **kw):
    """A NcWs on NaviCore's endpoint, retried for `wait` s (the lease is already held), logging into session.log.
    AssertionError when it cannot connect."""
    end = time.monotonic() + wait
    while True:
        try:
            return NcWs(NC_AP_IP, name=name, log=bench.log, **kw)
        except OSError as e:
            last = e
        if time.monotonic() >= end:
            raise AssertionError(f"could not open ws://{NC_AP_IP}/ws with a lease held ({type(last).__name__}: {last})")
        time.sleep(1.5)


def _sync(dev, timeout=6.0):
    """A barrier on one transport (NaviCore's USB console, or a NcWs) -> the mark after it. '?HILB<nonce>' goes out
    there, then '#L12'; NaviCore runs a transport's lines in order and prints each reply before it takes the next
    (handleSerialInput yields after a '?' line, NaviCore.ino:4296-4306; drain runs one line a pass,
    navicore_wsserver.h:512-558), and each transport delivers in order, so once the echo and the poke's Mode= line
    arrived, everything printed before them - the mirrored replies to the other transport's commands too - has. The
    poke also releases a line NaviCore's USB holds back (docs/HIL_TESTING.md §5)."""
    tag = marker("B")
    m = dev.mark()
    dev.send(f"?{tag}")
    dev.send("#L12")
    dev.expect(_echo_rx(tag), timeout=timeout, since=m)
    k = m + next(i for i, x in enumerate(dev.since(m)) if x == echo(tag))
    dev.expect(r"Mode=\d+", timeout=timeout, since=k + 1)
    return dev.mark()


def _count(dev, since, rx, want=None, timeout=3.0, settle=0.5):
    """How many lines matching `rx` arrived after mark `since`: waits until `want` arrived or `timeout` passed, then
    `settle` s more for any extra."""
    pat = re.compile(rx) if isinstance(rx, str) else rx
    deadline = time.monotonic() + timeout
    while True:
        n = sum(1 for x in dev.since(since) if pat.search(x))
        if (want is not None and n >= want) or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    time.sleep(settle)
    return sum(1 for x in dev.since(since) if pat.search(x))


def _pwm(lines):
    """(PWM_UPDATE frames that parse, lines that start as one and do not) among `lines` (parse_pwm_update)."""
    good, bad = [], 0
    for x in lines:
        if x.startswith('{"type":"PWM_UPDATE"'):
            try:
                good.append(parse_pwm_update(x))
            except ValueError:
                bad += 1
    return good, bad


def _uptime_after(nc, wait_s=25.0):
    """NaviCore's uptime in ms (s46 _uptime_ms: GET_MESH_STATS), asked again each second for up to `wait_s` while it
    does not answer: a NaviCore that crashed is still booting."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return _uptime_ms(nc)
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1.0)


def _quiet_usb(bench):
    """SET_DEBUG_FLAGS 0 and STOP_MONITOR over NaviCore's USB (both RAM only), for a block that ran with the port
    handed over; failures are noted, not raised."""
    try:
        nc = _nc(bench)
        nc.set_debug_flags(0)
        nc.ack({"type": "STOP_MONITOR"})
    except Exception as e:  # noqa: BLE001 - a port that did not come back is handed_over's failure to report
        bench.note(f"after the port came back, NaviCore's debug flags and monitor were not cleared: {_line1(e)}")


# ============================================================ joining, and the discovery contract
@test("ncwifi.pc_joins_ws_ping", "OPT-IN (navicore_wifi): the PC's spare WiFi adapter joins NaviCore's access point (SSID "
      "and password from its config) and holds a 192.168.4.x lease with no default route, the PC's own routes "
      "unchanged; ws://192.168.4.1/ws answers PING with USB's PONG, bare (no id); six PINGs in one frame get six PONGs "
      "and no '[WS] command queue full'; Intellex's pipelined probe (PING, ?RELAY,WIFI, ?WDP,DUMP) reads it as a "
      "NaviCore, and the SELF row names NaviCore's id", needs=["navicore"], links=[], opt_in="navicore_wifi")
def pc_joins_ws_ping(bench):
    """NAVICORE.md nc.wifi.dhcp_no_gateway, nc.ws.dispatch, nc.ws.intellex_client. The SoftAP's DHCP server offers no
    router (get, stop, set 0, start: NaviCore.ino:4731-4783), so the adapter holds a lease and no default route. A line
    from the socket runs through processInputLine, the dispatcher USB uses (navicore_wsserver.h:5-8, :545), and a PING
    is answered bare, {"type":"PONG","version":...} (NaviCore.ino:3855-3863). Six lines in one frame are six queued
    commands: the queue is 8 deep and the handler waits WS_ENQUEUE_WAIT_MS (50 ms) for room before dropping one with
    '[WS] command queue full - dropped a line' (:51-65, :458-466), printed on the httpd task, so on USB only (rc_serial.h
    :62-74). Intellex's discovery sends PING, ?RELAY,WIFI and ?WDP,DUMP as three frames and calls the host a NaviCore
    on an id-less PONG (Intellex src/discover.py:323-412, intellex_verdict); NaviCore owns no ?RELAY verb (WCB_Mgmt.h
    handleLine :367-403), so that line is an unknown command and no '[relay]' line comes, and ?WDP,DUMP's SELF row is
    PEER=3 (WCB_Mgmt.h:225-228)."""
    problems, facts = [], {}
    before = default_routes()
    with _on_ap(bench, problems) as (nc, cfg, name):
        time.sleep(2.0)                                   # the lease's routes settle
        problems += route_problems(before, default_routes(), name)
        nid = nc.wcb_status()["self"]
        usb_pong = nc.json_cmd(PING, PONG.pattern).string.rstrip()
        ws = _open(bench)
        try:
            m = _sync(ws)
            ws.send(PING_LINE)
            got = ws.expect(PONG.pattern, timeout=4, since=m).string.rstrip()
            if got != usb_pong:
                problems.append(f"PING over the socket answered {got!r}, USB {usb_pong!r}")
            m, um = _sync(ws), nc.dev.mark()
            ws.send_frames(['{"type":"PING"}\n' * 6])
            n = _count(ws, m, PONG, want=6, timeout=4.0)
            facts["burst"] = n
            if n != 6:
                problems.append(f"six PINGs in one frame: {n} PONGs on the socket, not 6")
            if any(x.startswith(QUEUE_FULL) for x in nc.dev.since(um)):
                problems.append(f"NaviCore dropped a line of the burst ({QUEUE_FULL!r} on USB)")
            m = _sync(ws)
            ws.send_frames(['{"type":"PING"}\n', "?RELAY,WIFI\n", "?WDP,DUMP\n"])
            try:
                ws.expect(r"^\[WDP:END,count=\d+\]", timeout=6, since=m)
            except AssertionError:
                problems.append("Intellex's probe: no [WDP:END] within 6 s")
            lines = ws.since(m)
            kind, version, self_id = intellex_verdict(lines)
            facts["intellex"] = kind
            if kind != "navicore" or version != PONG.match(usb_pong).group(1):
                problems.append(f"Intellex's discovery would read this host as {kind!r} (PONG version "
                                f"{'as USB' if version == PONG.match(usb_pong).group(1) else version!r})")
            rows = parse_wdp(lines)["rows"]
            selfs = [r.get("N") for r in rows if r.get("PEER") == "3"]
            if selfs != [str(nid)]:
                problems.append(f"?WDP,DUMP's SELF rows name {selfs}, NaviCore is {nid}")
            if any(x.startswith("[relay]") for x in lines) or echo("RELAY,WIFI") not in lines:
                problems.append(f"?RELAY,WIFI: expected {echo('RELAY,WIFI')!r} and no '[relay]' line")
        finally:
            ws.close()
    bench.note(f"ncwifi.pc_joins_ws_ping: {facts}")
    assert not problems, "; ".join(problems)


# ============================================================ the same surface as USB
@test("ncwifi.ws_parity", "OPT-IN (navicore_wifi): the WebSocket answers as USB does: PING, GET_CONFIG's whole line "
      "(compared by SHA-256, never shown), a TRIGGER on an inert key (its ACK and rc_trig) and a refused one, a "
      "TEST_ACTION whose marker reaches W1 S2 from either transport, ?REC,LS, ?version, ?OTALOCAL,STATUS and #L12",
      needs=["navicore"], links=["W1S2"], opt_in="navicore_wifi")
def ws_parity(bench):
    """NAVICORE.md nc.ws.surface_parity and nc.ws.large_reply. Both transports feed processInputLine (NaviCore.ino
    :3793-4287; navicore_wsserver.h:545), so each command's reply must be the same text. GET_CONFIG is one ~14 KB line
    (:3865-3882), which the socket gets through WsSink's inline flush at every 2 KB (navicore_wsserver.h:151-167) in
    several frames; it must arrive byte for byte. The TRIGGER is on an inert key (s40 _inert_keys: a 0/0 band, no
    mapping), so it dispatches nothing but its rc_trig (NaviCore.ino:2225-2279) and ACK (:4051-4062); USB and the
    socket each use their own tap number, so a mirrored rc_trig of the other transport's TRIGGER cannot stand in for
    one. A bad button is refused with '{"type":"ACK","ok":false,"msg":"bad mode/btn/tap"}' (:4055-4056). The
    TEST_ACTION is a wcb_unicast of ';S2<marker>' to W1 (s41 _act), which lands on the W1S2 probe (:4017-4027). Every
    read goes after a barrier on its own transport (_sync), since the socket mirrors USB's replies and USB the
    socket's."""
    s2 = link(bench, 1, "S2")
    problems, got = [], {}
    with _on_ap(bench, problems) as (nc, cfg, _):
        key = _inert_keys(cfg, 1)[0]
        mode, btn = divmod(int(key), 100)
        ws = _open(bench)
        try:
            wnc = NaviCore(ws)
            for label, drv, tap in (("USB", nc, 1), ("WS", wnc, 2)):
                r = got[label] = {}
                _sync(drv.dev)
                r["pong"] = drv.json_cmd(PING, PONG.pattern).string.rstrip()
                _sync(drv.dev)
                try:
                    text = drv.config(raw=True, timeout=15.0)
                    r["config"] = (len(text), _sha(text))
                except AssertionError as e:
                    r["config"] = None
                    problems.append(f"GET_CONFIG over {label}: {_line1(e)}")
                _sync(drv.dev)
                ack, trig = drv.trigger(mode, btn, tap)
                r["trigger"] = (ack, trig)
                if trig != {"sys": 1, "type": "rc_trig", "id": (cfg.get("wcbNetwork") or {}).get("deviceId"),
                            "mode": mode, "btn": btn, "tap": tap}:
                    problems.append(f"{label}: the TRIGGER of inert key {key} (tap {tap}) gave rc_trig {trig}")
                _sync(drv.dev)
                r["refused"] = drv.ack_line({"type": "TRIGGER", "mode": mode, "btn": 0, "tap": 1})
                _sync(drv.dev)
                t = marker(f"P{label[0]}")
                lm = s2.mark()
                r["action"] = drv.test_action(_act(t))
                try:
                    s2.expect(t.encode() + b"\r", timeout=4, since=lm)
                except AssertionError:
                    problems.append(f"the TEST_ACTION sent over {label} never put its marker on W1 S2")
                _sync(drv.dev)
                r["clips"] = [x.rstrip() for x in drv.clips()[0] if x.startswith(("[CLIP", "[REC]"))]
                _sync(drv.dev)
                r["version"] = [x.rstrip() for x in drv.cli("?version", until=r"^End of Version")
                                if x.startswith(("Software Version:", "End of Version"))]
                _sync(drv.dev)
                st = ncflash.ota_status(drv)
                r["ota"] = {k: st[k] for k in ("chip", "family", "firmware", "running", "next", "active", "app_sha")}
                _sync(drv.dev)
                r["mode"] = drv.mode()
        finally:
            ws.close()
    usb, ws_ = got.get("USB", {}), got.get("WS", {})
    for k in ("pong", "refused", "clips", "version", "ota", "mode"):
        if k in usb and k in ws_ and usb[k] != ws_[k]:
            problems.append(f"{k}: the socket gave {ws_[k]!r}, USB {usb[k]!r}")
    if usb.get("config") and ws_.get("config") and usb["config"] != ws_["config"]:
        problems.append(f"GET_CONFIG over the socket differs from USB's ({ws_['config'][0]} vs {usb['config'][0]} "
                        f"characters; compared by SHA-256, never shown)")
    if "trigger" in usb and "trigger" in ws_ and usb["trigger"][0] != ws_["trigger"][0]:
        problems.append(f"the TRIGGER's ACK: socket {ws_['trigger'][0]}, USB {usb['trigger'][0]}")
    if "action" in usb and "action" in ws_ and usb["action"] != ws_["action"]:
        problems.append(f"the TEST_ACTION's ACK: socket {ws_['action']}, USB {usb['action']}")
    bench.note(f"ncwifi.ws_parity: GET_CONFIG {(usb.get('config') or (None,))[0]} characters; inert key {key}; "
               f"clips {len(usb.get('clips') or [])} lines")
    assert not problems, "; ".join(problems)


@test("ncwifi.ws_console_mirror", "OPT-IN (navicore_wifi): the socket mirrors NaviCore's console: a monitor started over it "
      "streams PWM_UPDATE there at about 20 a second and nothing after STOP_MONITOR's ACK; one started over USB streams "
      "to the socket too; the dispatch trace (DBG_WCB) and a USB TRIGGER's rc_trig reach it; a USB reply appears on the "
      "socket and a socket reply on USB; and with nothing reading NaviCore's USB the monitor, rc_trig and the trace "
      "still reach the socket", needs=["navicore"], links=["W1S2"], opt_in="navicore_wifi")
def ws_console_mirror(bench):
    """NAVICORE.md nc.ws.console_mirror. The capture tee stays armed while a client is connected (drain re-arms it every
    pass, navicore_wsserver.h:515-530), so whatever the loop core prints goes to the socket as to USB (rc_serial.h
    :62-74): replies to either transport, PWM_UPDATE (sendPWMUpdate, NaviCore.ino:3131-3181, every 50 ms while
    START_MONITOR holds, :3983-3993), the [DISPATCH] trace (vlogf :1569-1593, '[DISPATCH] WCB->1  <cmd>' :2059) and
    rc_trig (:2249-2279). Each of the three is guarded on USB's room and, when USB has none - no host reading it, as in
    every real WiFi session - goes to the socket alone (printlnDirect, writeDirect: navicore_wsserver.h:296-325). The
    last part hands NaviCore's port away (hil/intellex.py handed_over): with no reader the USB TX FIFO stays full
    (NaviCore.ino:5390-5398), its ring fills within a second of the monitor, and from then on those fallbacks carry the
    stream; the port comes back with a PING. A monitor frame the sink cannot take is dropped whole (:101-104), so rates
    are floors, not exact."""
    s2 = link(bench, 1, "S2")
    problems, facts = [], {}
    with _on_ap(bench, problems) as (nc0, cfg, _):
        key = _inert_keys(cfg, 1)[0]
        mode, btn = divmod(int(key), 100)
        nid = (cfg.get("wcbNetwork") or {}).get("deviceId")
        ws = _open(bench)
        try:
            wnc = NaviCore(ws)
            with nc_guard(bench, nc=nc0) as g:
                nc = g.nc
                _sync(ws)
                frames = wnc.monitor(3.0)
                facts["ws_monitor"] = len(frames)
                if len(frames) < MONITOR_HZ * 3 * 0.6:
                    problems.append(f"a monitor started over the socket: {len(frames)} PWM_UPDATE in 3 s there")
                stop = ws.mark()
                time.sleep(0.6)
                late = [x for x in ws.since(stop) if x.startswith('{"type":"PWM_UPDATE"')]
                if late:
                    problems.append(f"{len(late)} PWM_UPDATE line(s) after STOP_MONITOR's ACK")
                m = _sync(ws)
                nc.ack({"type": "START_MONITOR"})
                time.sleep(2.0)
                nc.ack({"type": "STOP_MONITOR"})
                good, bad = _pwm(ws.since(m))
                facts["usb_monitor_on_ws"] = len(good)
                if len(good) < MONITOR_HZ * 2 * 0.5 or bad:
                    problems.append(f"a monitor started over USB: {len(good)} whole PWM_UPDATE frames on the socket in "
                                    f"2 s, {bad} cut short")
                with nc.debug(DBG_WCB):
                    t = marker("D")
                    m, lm = _sync(ws), s2.mark()
                    nc.test_action(_act(t))
                    try:
                        ws.expect("^" + re.escape(DISPATCH_WCB.format(1, f";S2{t}")) + "$", timeout=3, since=m)
                    except AssertionError:
                        problems.append("the dispatch trace of a USB TEST_ACTION did not reach the socket")
                    try:
                        s2.expect(t.encode() + b"\r", timeout=4, since=lm)
                    except AssertionError:
                        problems.append("the traced TEST_ACTION's marker never reached W1 S2")
                m = _sync(ws)
                nc.trigger(mode, btn, 3)
                want = f'{{"sys":1,"type":"rc_trig","id":{nid},"mode":{mode},"btn":{btn},"tap":3}}'
                try:
                    ws.expect("^" + re.escape(want) + "$", timeout=3, since=m)
                except AssertionError:
                    problems.append("a USB TRIGGER's rc_trig did not reach the socket")
                m = _sync(ws)
                nc.cli("?version", until=r"^End of Version")
                if not any(x.startswith("Software Version:") for x in ws.since(m)):
                    problems.append("a USB ?version's reply did not reach the socket")
                um = _sync(nc.dev)
                ws.send(PING_LINE)
                try:
                    nc.dev.expect(PONG.pattern, timeout=3, since=um)
                except AssertionError:
                    problems.append("a PING sent over the socket was not answered on USB as well")
            # Nothing reading NaviCore's USB: the fallbacks alone carry the stream to the socket.
            t = marker("U")
            try:
                with handed_over(bench, "navicore"):
                    m = _sync(ws)
                    wnc.set_debug_flags(DBG_WCB)
                    wnc.ack({"type": "START_MONITOR"})
                    time.sleep(1.5)                            # USB's TX ring fills: availableForWrite() reaches 0
                    fm = ws.mark()
                    time.sleep(3.0)
                    wnc.trigger(mode, btn, 2)
                    wnc.test_action(_act(t))
                    time.sleep(0.5)
                    wnc.ack({"type": "STOP_MONITOR"})
                    wnc.set_debug_flags(0)
                    good, bad = _pwm(ws.since(fm))
                    facts["unread_usb_monitor"] = len(good)
                    lines = ws.since(m)
                    if len(good) < MONITOR_HZ * 3 * 0.5 or bad:
                        problems.append(f"with NaviCore's USB unread: {len(good)} whole PWM_UPDATE frames on the socket "
                                        f"in 3 s, {bad} cut short")
                    if not any(re.fullmatch(rf'\{{"sys":1,"type":"rc_trig","id":{nid},"mode":{mode},"btn":{btn},'
                                            rf'"tap":2\}}', x) for x in lines):
                        problems.append("with NaviCore's USB unread, a TRIGGER's rc_trig did not reach the socket")
                    if DISPATCH_WCB.format(1, f";S2{t}") not in lines:
                        problems.append("with NaviCore's USB unread, the dispatch trace did not reach the socket")
            finally:
                _quiet_usb(bench)
        finally:
            ws.close()
    bench.note(f"ncwifi.ws_console_mirror: {facts}")
    assert not problems, "; ".join(problems)


@test("ncwifi.ws_multi_client", "OPT-IN (navicore_wifi): three sockets at once, each seeing every reply; a fourth evicts "
      "the least recently active, whose socket closes, and the three left all get the newcomer's reply; half-lines "
      "sent on two sockets interleaved stay apart, each run once", needs=["navicore"], links=[],
      opt_in="navicore_wifi")
def ws_multi_client(bench):
    """NAVICORE.md nc.ws.multi_client. httpd holds three sockets with LRU purge (navicore_wsserver.h:488-489), so a
    fourth connection closes the session that exchanged traffic least recently (esp_http_server.h:1591-1597); its close
    hook drops it from the sink and frees its line buffer (wsClose :372-388). The sink sends the console to every
    client it holds (:119-128, :286-290), and each socket has its own line accumulator (accFor :335-365), so a line
    split over frames on one socket never fuses with another's. The sink's own 'evict slot 0' branch (:141-142) is not
    reached here: the purge frees a slot before the newcomer's handshake. Each client speaks in turn at the start, which
    orders them for the purge: the first is the least recently active."""
    problems, facts = [], {}
    with _on_ap(bench, problems):
        c = []
        try:
            for k in range(WS_MAX_CLIENTS):
                c.append(_open(bench, name=f"ncws{k + 1}"))
                _sync(c[k])
            tag = marker("M")
            marks = [x.mark() for x in c]
            c[1].send(f"?{tag}")
            for k, x in enumerate(c):
                try:
                    x.expect(_echo_rx(tag), timeout=4, since=marks[k])
                except AssertionError:
                    problems.append(f"client {k + 1} of three did not get a reply another client asked for")
            fourth = _open(bench, name="ncws4")
            c.append(fourth)
            if not c[0].wait_closed(6.0):
                problems.append("a fourth client connected and the least recently active one was not closed")
            facts["first_client"] = "closed" if c[0].closed else "open"
            tag = marker("N")
            survivors = c[1:]
            marks = [x.mark() for x in survivors]
            fourth.send(f"?{tag}")
            for k, x in enumerate(survivors):
                try:
                    x.expect(_echo_rx(tag), timeout=4, since=marks[k])
                except AssertionError:
                    problems.append(f"client {k + 2} (of the three held after the eviction) missed the newcomer's reply")
            if c[0].connected and any(x == echo(tag) for x in c[0].since(0)):
                problems.append("the evicted client still receives the console")
            a, b = c[1], c[2]
            tag = marker("I")
            time.sleep(0.3)
            marks = [x.mark() for x in survivors]
            a.send_frames(['{"type":"PI'])
            b.send_frames([f"?{tag}\n"])
            time.sleep(0.3)
            a.send_frames(['NG"}\n'])
            for k, x in enumerate(survivors):
                pongs = _count(x, marks[k], PONG, want=1, timeout=3.0)
                echoes = _count(x, marks[k], _echo_rx(tag), want=1, timeout=1.0, settle=0.2)
                if (pongs, echoes) != (1, 1):
                    problems.append(f"interleaved half-lines: client {k + 2} saw {pongs} PONG(s) and {echoes} reply(ies) "
                                    f"to the other line, not one each")
        finally:
            for x in c:
                x.close()
    bench.note(f"ncwifi.ws_multi_client: {facts}")
    assert not problems, "; ".join(problems)


# ============================================================ framing, both ways
@test("ncwifi.ws_line_framing", "OPT-IN (navicore_wifi): the endpoint's line assembly: CR and LF each end a line (two "
      "lines in one frame both run, CRLF runs once), a line split over two frames runs once, the config tool's "
      "1391-character ?OTALOCAL,DATA line in 512-byte frames decodes whole (NAK with no session, not a base64 error), a "
      "closed client's unfinished line never runs, and a 98304-byte frame, or an unended line past 100 KB, closes that "
      "socket with nothing run", needs=["navicore"], links=[], opt_in="navicore_wifi")
def ws_line_framing(bench):
    """NAVICORE.md nc.ws.line_framing. wsHandler appends each frame to the socket's accumulator and queues every line
    ended by CR or LF, skipping empty ones, and keeps the tail for the next frame (navicore_wsserver.h:419-472), which
    is what lets the config tool send any line over 512 bytes as several frames (:337-341). processOtaLocalCommand
    decodes a DATA line's base64 before it asks for a session (navicore_ota.h:295-311), so with none open the whole
    line answers '[OTA] DATA rejected at offset <o> (write cursor at 0)' and '[OTA:NAK,0]', and a line that reached it
    in pieces would answer '[OTA] DATA base64 error' instead: the plan's check, and no session is ever opened (STATUS
    idle before and after; Skip when another tool has one open). A client that closes mid-line leaves nothing behind:
    its close hook zeroes its accumulator (:343-345, :367-388) and a slot is zeroed when claimed (:360-364). A frame of
    98304 bytes or more fails the handler before anything is allocated (:406-408), and a socket's unended line growing
    past 98304 + 2048 bytes is dropped (:436-438); a failed handler closes its socket (esp_http_server.h:435-437).
    There is no line-length cap below that: the endpoint takes what USB takes, whose own ceiling is 98304 characters
    (NaviCore.ino:4314). At most two sockets are open at once here."""
    problems, facts = [], {}
    with _on_ap(bench, problems) as (nc, cfg, _):
        st0 = ncflash.ota_status(nc)
        if st0["active"]:
            raise Skip(f"NaviCore has an OTA session open ({st0['session']}): something else is flashing it")
        ws = _open(bench)
        try:
            m = _sync(ws)
            ws.send_frames(['{"type":"PING"}\r{"type":"PING"}\n'])
            n = _count(ws, m, PONG, want=2, timeout=4.0)
            if n != 2:
                problems.append(f"two lines in one frame (CR, then LF): {n} PONGs, not 2")
            m = _sync(ws)
            ws.send_frames(['{"type":"PING"}\r\n'])
            n = _count(ws, m, PONG, want=1, timeout=4.0)
            if n != 1:
                problems.append(f"a line ended by CRLF: {n} PONGs, not 1")
            m = _sync(ws)
            ws.send_frames(['{"type":"PI', 'NG"}\n'], gap_s=0.3)
            n = _count(ws, m, PONG, want=1, timeout=4.0)
            if n != 1:
                problems.append(f"a line split over two frames: {n} PONGs, not 1")
            line = data_line()
            pieces = [line[i:i + 512] for i in range(0, len(line), 512)]
            pieces[-1] += "\n"
            m = _sync(ws)
            ws.send_frames(pieces, gap_s=0.004)                  # the tool's pacing: USB_CHUNK 512, 4 ms
            try:
                ws.expect(r"^\[OTA:NAK,\d+\]$|^\[OTA\] DATA base64 error", timeout=5, since=m)
            except AssertionError:
                pass
            ota = [x.rstrip() for x in ws.since(m) if x.startswith("[OTA")]
            want = ["[OTA] DATA rejected at offset 1048576 (write cursor at 0)", "[OTA:NAK,0]"]
            if ota != want:
                problems.append(f"the {len(line)}-character DATA line in {len(pieces)} frames: {ota}, expected {want}")
            tail = marker("T")
            a = _open(bench, name="ncws-a")
            try:
                a.send_frames([f"?{tail}"])                          # no end: the line waits in a's accumulator
                time.sleep(0.5)
            finally:
                a.close()
            um = nc.dev.mark()
            b = _open(bench, name="ncws-b")
            try:
                b.send_frames(["\n"])                                # would end a tail left in b's slot
                time.sleep(1.0)
                _sync(b)
            finally:
                b.close()
            if any(tail in x for x in nc.dev.since(um)):
                problems.append("a line a closed client left unfinished ran on the next client's first line end")
            big, run = marker("F"), marker("R")
            c = _open(bench, name="ncws-c")
            try:
                c.send_frame((f"?{big}" + "x" * WS_FRAME_MAX)[:WS_FRAME_MAX].encode("ascii"))
            except AssertionError:
                pass                                                 # the board closed it mid-frame
            if not c.wait_closed(8.0):
                problems.append(f"a {WS_FRAME_MAX}-byte frame did not close the socket")
                c.close()
            d = _open(bench, name="ncws-d")
            sent = 0
            try:
                for k in range(WS_ACC_MAX // 4096 + 2):
                    d.send_frame(((f"?{run}" if k == 0 else "") + "x" * 4096)[:4096].encode("ascii"))
                    sent += 1
                    time.sleep(0.05)
                    if d.closed:
                        break
            except AssertionError:
                pass
            facts["runaway_frames"] = sent
            if not d.wait_closed(6.0):
                problems.append(f"{sent} frames of 4096 bytes with no line end ({sent * 4096} bytes) did not close the "
                                f"socket")
                d.close()
            time.sleep(1.0)
            ran = [x for x in nc.dev.since(um) if big in x or run in x]
            if ran:
                problems.append(f"{len(ran)} line(s) from the oversized frame or the unended line ran")
            _sync(ws)                                                # the endpoint still serves
        finally:
            ws.close()
        st1 = ncflash.ota_status(nc)
        if st1["active"] or st1["running"] != st0["running"]:
            problems.append(f"afterwards NaviCore's OTA session is {'ACTIVE' if st1['active'] else 'idle'} and it runs "
                            f"'{(st1['running'] or {}).get('label')}' (before '{st0['running']['label']}')")
    bench.note(f"ncwifi.ws_line_framing: {facts}")
    assert not problems, "; ".join(problems)


@test("ncwifi.ws_utf8_and_latch", "OPT-IN (navicore_wifi): output over 2 KB of 2-, 3- and 4-byte characters (NaviCore's "
      "echo of an unknown command) arrives whole and every text frame is valid UTF-8 on its own; and a client that "
      "drops mid-reply, the last one connected, costs the next client nothing: its first reply and its GET_CONFIG arrive "
      "whole (three rounds)", needs=["navicore"], links=[], opt_in="navicore_wifi")
def ws_utf8_and_latch(bench):
    """NAVICORE.md nc.ws.utf8_and_latch. WsSink flushes at every 2048 bytes of output (write(), navicore_wsserver.h
    :151-167) and pump() cuts the frame back to a whole UTF-8 character, holding at most 3 bytes for the next frame
    (:196-208, _utf8SafeLen :236-252), because a TEXT frame must be valid UTF-8 on its own (RFC 6455 §8.1). NaviCore
    echoes any '?' line nobody owns, whole (NaviCore.ino:3814-3816), so a line of 3000 bytes of one multi-byte
    character after 0-2 ASCII pad characters puts that flush inside a character in most of the nine cases (utf8_lines),
    with no config written: the plan's multi-byte label in a guarded config gives no control over where the flush
    falls. The latch: _dropping, set when a pump fails as the last client leaves mid-line, once ate the next client's
    first whole line; begin() clears it and the buffer on the change to live (:129-143). Each round a client asks for
    GET_CONFIG and resets its connection at once, then a new one asks for a marker and GET_CONFIG: its first reply must
    be its own, whole, and GET_CONFIG must equal USB's byte for byte (compared by SHA-256). The main socket is closed
    first, so the dropping client is the last one."""
    problems, facts = [], {}
    with _on_ap(bench, problems) as (nc, cfg, _):
        ws = _open(bench)
        try:
            tag = marker("U")
            lines = utf8_lines(tag)
            m, fm = _sync(ws), ws.frame_mark()
            um = nc.dev.mark()
            for x in lines:
                ws.send(x)
                time.sleep(0.1)                                  # one 3 KB echo a loop pass, well inside the queue
            try:
                ws.expect(_echo_rx(lines[-1][1:]), timeout=8, since=m)
            except AssertionError:
                pass
            got = [x for x in ws.since(m) if x.startswith(f"{UNKNOWN}?{tag}")]
            want = [f"{UNKNOWN}{x}" for x in lines]
            if got != want:
                problems.append(f"{sum(1 for x in want if x in got)} of {len(want)} multi-byte echoes arrived whole on the "
                                f"socket ({len(got)} echo lines)")
            usb = [x for x in nc.dev.since(um) if x.startswith(f"{UNKNOWN}?{tag}")]
            if usb != want:
                problems.append(f"USB shows {sum(1 for x in want if x in usb)} of the {len(want)} echoes whole")
            frames = ws.frames_since(fm)
            problems += frame_problems(frames)
            facts["frames"], facts["held_back"] = len(frames), held_back(frames)
        finally:
            ws.close()
        want_cfg = nc.config(raw=True, timeout=15.0)
        time.sleep(1.0)                                          # the closed socket's session ends on the board
        for rnd in range(3):
            a = _open(bench, name="ncws-a")
            a.send('{"type":"GET_CONFIG"}')
            time.sleep(0.05 * (rnd + 1))                         # mid-reply, at three depths
            a.abort()
            b = _open(bench, name="ncws-b")
            try:
                tag = marker("L")
                b.send(f"?{tag}")
                try:
                    b.expect(_echo_rx(tag), timeout=4, since=0)
                except AssertionError:
                    problems.append(f"round {rnd + 1}: the next client's first reply did not arrive")
                    continue
                first = b.since(0)
                k = first.index(echo(tag))
                stray = [x for x in first[:k] if ('"mappings"' in x or '"thresholds"' in x)
                         and not x.startswith('{"type":"CONFIG","data":')]
                if stray:
                    problems.append(f"round {rnd + 1}: the next client got {len(stray)} piece(s) of the dropped client's "
                                    f"GET_CONFIG before its own reply")
                try:
                    text = NaviCore(b).config(raw=True, timeout=15.0)
                    if text != want_cfg:
                        problems.append(f"round {rnd + 1}: the next client's GET_CONFIG differs from USB's ({len(text)} vs "
                                        f"{len(want_cfg)} characters; compared by SHA-256)")
                except AssertionError as e:
                    problems.append(f"round {rnd + 1}: the next client's GET_CONFIG: {_line1(e)}")
            finally:
                b.close()
            time.sleep(0.5)
    bench.note(f"ncwifi.ws_utf8_and_latch: {facts}")
    assert not problems, "; ".join(problems)


# ============================================================ the Wizard's surface, through NaviCore, over WiFi
@test("ncwifi.ws_wizard_surface", "OPT-IN (navicore_wifi): the WCB Wizard's surface over the socket as over USB: ?backup "
      "and WCB_WEBTOOL_CONFIG_PULL give the same tokens (?EPASS compared as a hash), ?WDP,DUMP the same rows; "
      "?MGMT,PULL,2 brings W2's whole config back as one CRC-valid line equal to W2's own chain; and relayed ?OTA lines "
      "to W2 with no session are answered by W2's [OTA:ACK] on the socket, nothing erased", needs=["navicore", "wcb2"],
      links=[], opt_in="navicore_wifi")
def ws_wizard_surface(bench):
    """NAVICORE.md nc.ws.wizard_over_ws. The Wizard manages the fleet through this board with WCB_Client's WcbMgmt
    (?backup / ?version / ?WDP,DUMP / ?MGMT,*: WCB_Mgmt.h handleLine :367-403; WCB_WEBTOOL_CONFIG_PULL before the '?'
    branch, NaviCore.ino:3809-3812), which prints to Serial, the tee (NaviCore.ino:4883; rc_serial.h:98-99), so its
    replies reach the socket. A pull's answer comes back over the mesh and is printed by WcbMgmt::service() from loop()
    (:5433): hil/wcb.py pull_config reads it off the socket like any relay console, and it must equal W2's own factory
    chain behind [VER:] (s03 _factory_reply and _reply_problems, as navicore.mgmt_pull checks it over USB). A relayed
    OTA ACK is printed from loop() too (handleOtaAckRelay, navicore_ota.h:450-469, from drainOtaPackets :576-587),
    which is what makes relay OTA work over WiFi at all. The OTA lines are s47's non-erasing pair: DATA and ABORT to W2
    with no session open (W2's STATUS read idle first), answered ERR at 0 and OK at 0. Every token list goes through
    redact_tokens, and the chains are compared, never shown."""
    w2 = WCB(bench.dev("wcb2"))
    want = _factory_reply(w2, w2.version())
    assert len(want) <= PULL_MAX, (f"W2's pull reply is {len(want)} characters; this test needs a one-line config "
                                   f"(<= {PULL_MAX}) - a leftover from an aborted size test?")
    problems, facts = [], {}
    with _on_ap(bench, problems) as (nc, cfg, _):
        ws = _open(bench)
        try:
            wnc = NaviCore(ws)
            _sync(nc.dev)
            usb_backup = nc.backup()
            _sync(ws)
            ws_backup = wnc.backup()
            if ws_backup != usb_backup:
                problems.append(f"?backup over the socket: {ws_backup}, USB {usb_backup}")
            m = _sync(ws)
            ws.send("WCB_WEBTOOL_CONFIG_PULL")
            try:
                ws.expect(r"^-+ End of Backup -+", timeout=4, since=m)
            except AssertionError:
                problems.append("WCB_WEBTOOL_CONFIG_PULL over the socket: no 'End of Backup'")
            boot = redact_tokens([x.strip() for x in ws.since(m) if x.startswith("?")])
            if boot != usb_backup:
                problems.append(f"WCB_WEBTOOL_CONFIG_PULL over the socket: {boot}, ?backup on USB {usb_backup}")
            _sync(nc.dev)
            uv = nc.wdp_view()
            _sync(ws)
            wv = wnc.wdp_view()

            def rows(v):
                return {r.get("N"): {k: x for k, x in r.items() if k != "AGE"} for r in v["rows"]}
            if rows(wv) != rows(uv) or wv["cfg"] != uv["cfg"] or wv["count"] != uv["count"]:
                problems.append(f"?WDP,DUMP over the socket: rows {sorted(rows(wv))}, {wv['cfg']}, count {wv['count']}; "
                                f"USB: rows {sorted(rows(uv))}, {uv['cfg']}, count {uv['count']}")
            _sync(ws)
            try:
                r = pull_config(ws, 2, parts=False, timeout=10, verify=False)
                problems += [f"?MGMT,PULL,2 over the socket: {p}" for p in _reply_problems(r, want, [], "legacy", 1)]
                facts["pull"] = len(r.text)
            except AssertionError as e:
                problems.append(f"?MGMT,PULL,2 over the socket: {_line1(e)}")
            with Console(bench, 2) as c2:
                st2 = _wcb_status(_crun(c2, "?OTALOCAL,STATUS"))
                if st2["session"] != "idle":
                    raise Skip(f"W2 has an OTA session open ({st2['session']})")
                s = _session_id()
                acks = {}
                for what, cmd in (("DATA", f"?OTA,DATA,2,{s},0:{_crc('0,AAAA')},AAAA"), ("ABORT", f"?OTA,ABORT,2,{s}")):
                    m = _sync(ws)
                    ws.send(cmd)
                    try:
                        g = ws.expect(rf"^\[OTA:ACK,2,{s},(\d+),(\d+)\]$", timeout=6, since=m)
                        acks[what] = (int(g.group(1)), int(g.group(2)))
                    except AssertionError:
                        acks[what] = None
                st2b = _wcb_status(_crun(c2, "?OTALOCAL,STATUS"))
            if acks != {"DATA": (0, 1), "ABORT": (0, 0)}:
                problems.append(f"relayed OTA lines over the socket: W2's ACKs {acks} (offset, status), expected DATA "
                                f"(0, 1) and ABORT (0, 0)")
            if st2b["session"] != "idle" or st2b["running"] != st2["running"]:
                problems.append(f"W2 afterwards: session {st2b['session']}, running {st2b['running']} (before "
                                f"{st2['running']})")
        finally:
            ws.close()
    bench.note(f"ncwifi.ws_wizard_surface: {facts}")
    assert not problems, "; ".join(problems)


# ============================================================ the mesh while the socket streams
@test("ncwifi.mesh_coexist", "OPT-IN (navicore_wifi): for 40 s with the monitor streaming to a socket, the mesh carries "
      "on: NaviCore sees every bench WCB online, its WCB_SENDs reach W1 S2 and W2 S2 with no failed send in its ETM "
      "counters, ;W20,?version typed on W1 comes back as [TERM:20], and W1 never marks NaviCore offline; the socket "
      "gets whole PWM_UPDATE frames throughout", needs=["navicore", "wcb1"], links=["W1S2", "W2S2"],
      opt_in="navicore_wifi")
def mesh_coexist(bench):
    """NAVICORE.md nc.wifi.mesh_coexist. The SoftAP and ESP-NOW share one radio on the mesh channel: setup() raises the
    AP on wcbNetwork.channel before WCB_Client starts, which then keeps WIFI_AP_STA (NaviCore.ino:4694-4729,
    :4796-4800), and the httpd task is pinned to core 0 beside the ESP-NOW callbacks (navicore_wsserver.h:482-487). So
    while the monitor streams PWM_UPDATE to the socket at about 20 a second (NaviCore.ino:3131-3181), each round (about
    every 5 s) re-runs navicore.online, navicore.wcb_send and navicore.cli_over_mesh (s02, s10): GET_WCB_STATUS lists
    every bench WCB, a tracked WCB_SEND to each of W1 and W2 puts its marker on the S2 probe, and a CLI line relayed
    from W1 answers as [TERM:20]. W1 marks a board offline only after its missed-heartbeat count ('[ETM] WCB<n> went
    OFFLINE', WCB.ino:1406-1416) and prints 'came ONLINE' when it hears it again (:5336), so neither line may appear
    for NaviCore: no WCB prints anything per heartbeat, so that is the plan's 'no missed heartbeats' as far as the WCBs
    report it. NaviCore's own ETM counters (GET_MESH_STATS, rc_telemetry.h:1361-1432) must show no failed send to
    either WCB across the window. The monitor runs inside nc_guard, which stops it however the test ends."""
    s12, s22 = link(bench, 1, "S2"), link(bench, 2, "S2")
    w1 = usb_wcb(bench)
    problems, facts = [], {"rounds": 0}
    with _on_ap(bench, problems) as (nc0, cfg, _):
        fw = nc0.ping()
        nid = nc0.wcb_status()["self"]
        want_online = _bench_wcb_ids(bench)
        ws = _open(bench)
        try:
            wnc = NaviCore(ws)
            with nc_guard(bench, nc=nc0) as g:
                nc = g.nc
                stats0 = nc.mesh_stats()
                wm, fm = w1.dev.mark(), _sync(ws)
                wnc.ack({"type": "START_MONITOR"})
                t0 = time.monotonic()
                try:
                    while time.monotonic() - t0 < COEXIST_S:
                        rs = time.monotonic()
                        facts["rounds"] += 1
                        k = facts["rounds"]
                        missing = sorted(want_online - nc.online_ids())
                        if missing:
                            problems.append(f"round {k}: NaviCore sees WCB {missing} offline")
                        for wcb, l in ((1, s12), (2, s22)):
                            t = marker("C")
                            lm = l.mark()
                            ack = nc.wcb_send(wcb, f";S2{t}")
                            if not ack.get("ok"):
                                problems.append(f"round {k}: WCB_SEND to W{wcb} answered {ack}")
                            try:
                                l.expect(t.encode() + b"\r", timeout=4, since=lm)
                            except AssertionError:
                                problems.append(f"round {k}: WCB_SEND's marker never reached W{wcb} S2")
                        m = w1.dev.mark()
                        w1.send(f";W{nid},?version")
                        try:
                            w1.dev.expect(rf"^\[TERM:{nid}\]Software Version: {re.escape(fw)}$", timeout=5, since=m)
                        except AssertionError:
                            problems.append(f"round {k}: ;W{nid},?version on W1 got no [TERM:{nid}] answer")
                        time.sleep(max(0.0, 5.0 - (time.monotonic() - rs)))
                finally:
                    try:
                        wnc.ack({"type": "STOP_MONITOR"})
                    except AssertionError:
                        nc.ack({"type": "STOP_MONITOR"})
                secs = time.monotonic() - t0
                stats1 = nc.mesh_stats()
            good, bad = _pwm(ws.since(fm))
            facts["pwm"], facts["secs"] = len(good), round(secs, 1)
            if len(good) < MONITOR_HZ * secs * 0.5 or bad:
                problems.append(f"the socket got {len(good)} whole PWM_UPDATE frames in {secs:.0f} s and {bad} cut "
                                f"short")
            if ws.closed:
                problems.append(f"the socket ended while the mesh was busy ({ws.closed})")
        finally:
            ws.close()
    edges = [x for x in w1.dev.since(wm) if re.match(rf"^\[ETM\] WCB{nid} (\(special peer\) )?(went OFFLINE|came ONLINE)",
                                                     x)]
    if edges:
        problems.append(f"W1 printed {len(edges)} offline/online edge(s) for WCB{nid} while the socket streamed")
    for peer in sorted(want_online):
        a, b = stats0["peers"].get(peer), stats1["peers"].get(peer)
        if a and b:
            d = [y - x for x, y in zip(a[1:], b[1:])]        # sent, ackd, rty, fail, ung, recv
            facts[f"W{peer}"] = dict(zip(("sent", "ackd", "rty", "fail", "ung", "recv"), d))
            if d[3] > 0:
                problems.append(f"NaviCore's ETM counters show {d[3]} failed send(s) to WCB{peer} in the window")
    bench.note(f"ncwifi.mesh_coexist: {facts}")
    assert not problems, "; ".join(problems)


@test("ncwifi.ws_capture_slot", "OPT-IN (navicore_wifi): the console tee's single slot: a remote ?MAE read asked over the "
      "socket answers there when W2's reply lands; the same read asked through W1 answers on W1 as [TERM:20]; after "
      "each, and after a CLI line relayed from W1, the socket gets the console again (reads only: getPosition on "
      "Maestro 2)", needs=["navicore", "wcb1"], links=[], opt_in="navicore_wifi")
def ws_capture_slot(bench):
    """NAVICORE.md nc.ws.capture_slot (low). rcSerial's capture is one slot (rc_serial.h:78-94): drainRemoteCli arms it
    for a CLI line relayed over the mesh and disarms it after (NaviCore.ino:5010-5024), and maePumpRemoteEmits arms it
    again for a remote read's late answer when that read came over the bridge, then disarms (:829-847, the relay
    latched by maeLatchRemoteRelay :802-804). drain() re-arms the socket's tee every loop pass (navicore_wsserver.h
    :515-530), so the socket loses at most the rest of that pass. A read asked over the socket latches no relay, so its
    [MAE:<slot>] marker, printed from loop() when W2's :MQR lands (s44 ncdev.mae_remote_read), is teed to the socket:
    the note at navicore_wsserver.h:553-557, that such an answer goes to Serial only, predates the standing tee
    (:88-93) and is stale. Whether the W1-asked read's marker and the relayed ?version also reached the socket is noted,
    not asserted: while the relay holds the slot they go to it and to USB. The read is getPosition on channel 0 of
    whichever remote slot's device a WCB hosts (s41 _hosted_remote_slot: Maestro 2 on W2), which moves nothing; W2
    stores the answer in m<dev>pos0, cleared afterwards as s44 does."""
    w1 = usb_wcb(bench)
    problems, notes = [], []
    nc0 = _nc(bench)
    slot, dev, host = _hosted_remote_slot(nc0, nc0.config())      # Skip before the PC joins when no WCB hosts one
    with _on_ap(bench, problems) as (nc, cfg, _):
        nid = nc.wcb_status()["self"]
        ws = _open(bench)
        try:
            wnc = NaviCore(ws)
            try:
                m = _sync(ws)
                ws.send(f"?MAE,GET,{slot},0")
                got = _await_mae(wnc, m, slot, "pos", 0, timeout=6.0)
                if not isinstance(got, int):
                    problems.append(f"?MAE,GET,{slot},0 over the socket: no [MAE:{slot}] marker with a value there "
                                    f"within 6 s ({got})")
                m, wm = _sync(ws), w1.dev.mark()
                w1.send(f";W{nid},?MAE,GET,{slot},0")
                try:
                    w1.dev.expect(rf'^\[TERM:{nid}\]\[MAE:{slot}\]\{{"q":"pos","ch":0,"val":\d+\}}', timeout=6, since=wm)
                except AssertionError:
                    problems.append(f"';W{nid},?MAE,GET,{slot},0' on W1: no [TERM:{nid}][MAE:{slot}] marker came back")
                time.sleep(0.5)
                seen = parse_mae("\n".join(ws.since(m)), slot, "pos", 0)
                notes.append(f"the W1-asked read's marker {'reached' if seen is not None else 'did not reach'} the "
                             f"socket")
                try:
                    _sync(ws)
                except AssertionError:
                    problems.append("after a read asked through W1 answered, the socket got no console output")
                m, wm = _sync(ws), w1.dev.mark()
                w1.send(f";W{nid},?version")
                try:
                    w1.dev.expect(rf"^\[TERM:{nid}\]Software Version: ", timeout=5, since=wm)
                except AssertionError:
                    problems.append(f"';W{nid},?version' on W1 got no [TERM:{nid}] answer")
                time.sleep(0.5)
                reached = any(x.startswith("Software Version:") for x in ws.since(m))
                notes.append(f"the relayed ?version {'reached' if reached else 'did not reach'} the socket")
                m = _sync(ws)
                ws.send("?version")
                try:
                    ws.expect(r"^Software Version: ", timeout=3, since=m)
                except AssertionError:
                    problems.append("after a CLI line relayed from W1, ?version over the socket was not answered there")
            finally:
                with Console(bench, host) as ch:
                    ch.send(f"?VAR,CLEAR,m{dev}pos0")
                    time.sleep(0.2)
        finally:
            ws.close()
    bench.note(f"ncwifi.ws_capture_slot: slot {slot} = device {dev} on W{host}; " + "; ".join(notes))
    assert not problems, "; ".join(problems)


@test("ncwifi.ws_ping_soak", "OPT-IN (navicore_wifi): five minutes of the monitor streaming to a socket that pings "
      "every second: the socket stays open (no 1002 close, no frame out of step), every ping is answered, PWM_UPDATE "
      "arrives whole at close to 20 a second, and NaviCore's SBUS input stays at full rate",
      needs=["navicore"], links=[], opt_in="navicore_wifi",
      opt_in_why="the PC's spare WiFi adapter joins NaviCore's access point for about 6 minutes while NaviCore streams "
                 "to it")
def ws_ping_soak(bench):
    """NAVICORE.md nc.ws.ping_serialisation (nightly). httpd answers a client's PING with a PONG from its own task on
    core 0 while pump() runs on core 1; before every send was marshalled onto the httpd task with httpd_queue_work, the
    two wrote one socket at once and a pinging client died with 1002 about every 90 s (navicore_wsserver.h:176-195,
    wsSendWork :274-294). So: a ping each second for SOAK_S under the monitor's ~20 frames a second. A frame out of step
    would show as a reserved opcode, a close, or a PWM_UPDATE that does not parse (hil/ncws.py counts opcodes). The
    monitor runs inside nc_guard; NaviCore's USB stays open and carries the same stream (it has room, so sendPWMUpdate
    prints to Serial, which tees to the socket)."""
    problems, facts = [], {}
    with _on_ap(bench, problems) as (nc0, cfg, _):
        with nc_guard(bench, nc=nc0) as g:
            fps0 = g.nc.sbus_full_rate()["fps"]
            ws = _open(bench)
            pings = 0
            try:
                wnc = NaviCore(ws)
                m = _sync(ws)
                wnc.ack({"type": "START_MONITOR"})
                t0 = time.monotonic()
                try:
                    while time.monotonic() - t0 < SOAK_S and ws.connected:
                        try:
                            ws.ping(str(pings).encode("ascii"))
                        except AssertionError:
                            break                                # the socket ended: ws.closed says why, below
                        pings += 1
                        if pings % 60 == 0:
                            bench.note(f"ncwifi.ws_ping_soak: {pings} s, {len(ws.pongs)} pongs, {len(ws.lines)} lines")
                        time.sleep(1.0)
                finally:
                    secs = time.monotonic() - t0
                    try:
                        (wnc if ws.connected else g.nc).ack({"type": "STOP_MONITOR"})
                    except AssertionError:
                        g.nc.ack({"type": "STOP_MONITOR"})
                time.sleep(1.5)
                good, bad = _pwm(ws.since(m))
                answered = {p for _, p in ws.pongs}
                missing = [i for i in range(pings) if str(i).encode("ascii") not in answered]
                odd = {op: n for op, n in ws.opcodes.items() if op not in (0x1, 0x9, 0xA)}
                facts.update(secs=round(secs), pings=pings, pongs=len(ws.pongs), pwm=len(good), opcodes=ws.opcodes)
                if ws.closed:
                    problems.append(f"the socket ended during the soak ({ws.closed}, after {secs:.0f} s)")
                if len(missing) > 1:
                    problems.append(f"{len(missing)} of {pings} pings got no pong (first missing: {missing[0]})")
                if len(good) < MONITOR_HZ * secs * 0.8 or bad:
                    problems.append(f"{len(good)} whole PWM_UPDATE frames in {secs:.0f} s (under 80% of {MONITOR_HZ} a "
                                    f"second) and {bad} cut short")
                if odd:
                    problems.append(f"frames with unexpected opcodes {odd}: the stream fell out of step")
            finally:
                ws.close()
            fps1 = g.nc.sbus_full_rate()["fps"]
            facts.update(fps=(fps0, fps1))
            if fps1 < SBUS_FULL_FPS:
                problems.append(f"NaviCore's SBUS input read {fps1} fps after the soak (before {fps0})")
    bench.note(f"ncwifi.ws_ping_soak: {facts}")
    assert not problems, "; ".join(problems)


# ============================================================ the access point at boot (navicore_reboot too)
def _join_and_ping(bench, problems, cfg, version, label):
    """After a restart: the PC joins the access point `cfg` brings up and PING over the socket answers with `version`
    -> seconds from the call to the PONG. A problem is added, not raised, when the socket does not answer."""
    ssid, pw = ap_of(cfg)
    t0 = time.monotonic()
    with pc_on_ap(bench, problems, ssid, pw, "NaviCore's", spare_only=True, identify=_navicore_ap(bench, version)):
        ws = _open(bench, wait=20.0)
        try:
            m = ws.mark()
            ws.send(PING_LINE)
            got = ws.expect(PONG.pattern, timeout=4, since=m)
            if got.group(1) != version:
                problems.append(f"{label}: PONG over the socket names {got.group(1)}, USB {version}")
        except AssertionError as e:
            problems.append(f"{label}: PING over the socket: {_line1(e)}")
        finally:
            ws.close()
    return round(time.monotonic() - t0, 1)


@test("ncwifi.ap_boot_lines", "OPT-IN (navicore_wifi, and navicore_reboot): after a REBOOT NaviCore's banner brings the "
      "access point up as its config says - the SoftAP line with the config's SSID on the mesh channel at 192.168.4.1, "
      "ESP-NOW sharing the channel, DHCP with no gateway, then the WebSocket endpoint at the same address, in that order "
      "and with no [WIFI] failure line - and the PC then joins it and PING is answered over the socket (NaviCore "
      "restarts)", needs=["navicore"], links=[], opt_in="navicore_wifi")
def ap_boot_lines(bench):
    """NAVICORE.md nc.wifi.ap_bringup. setup() raises the SoftAP before WCB_Client starts (NaviCore.ino:4694-4700) under
    the config's SSID, or NaviCore-<deviceId> for an empty one, on wcbNetwork.channel (:4719-4729), clears the DHCP
    server's router option (:4731-4783) and only then starts the endpoint (:4785-4788; naviws::begin prints its address,
    navicore_wsserver.h:507-508). ap_block checks those lines against GET_CONFIG, comparing the SSID and never quoting
    it. ncboot.banner_order anchors the same lines in the whole banner; this test adds the proof that the access point
    it announces takes a client after a restart. The PC joins after NaviCore is back (not across the restart: REBOOT
    deauthenticates nobody, since otaFarewellAP() is deliberately empty, navicore_ota.h:184-204 - the comment at
    NaviCore.ino:4034-4047 that says it does is stale). The banner's free-PSRAM figure is noted: NaviCore prints no
    heap figure anywhere else (tracker #111 is the WCB's)."""
    _reboot_too(bench)
    _spare_adapter(bench)
    nc = _nc(bench)
    _restartable(nc)
    problems, facts = [], {}
    with nc_guard(bench, nc=nc) as g:
        if ap_of(g.before) is None:
            raise Skip("NaviCore hosts no access point: wifiEnabled is off or its AP password is under 8 characters")
        version = g.nc.ping()
        _, lines, _ = _restart(g.nc, "json")
        banner = banner_lines(lines)
        if not banner:
            problems.append("no boot banner arrived after the REBOOT (not from '=== NaviCore ===' on)")
        else:
            got, facts = ap_block(banner, g.before)
            problems += got
        if not problems:
            facts["join_s"] = _join_and_ping(bench, problems, g.before, version, "after the restart")
    bench.note(f"ncwifi.ap_boot_lines: {facts}")
    assert not problems, "; ".join(problems)


@test("ncwifi.refuse_short_password", "OPT-IN (navicore_wifi, and navicore_reboot): with a 3-character AP password saved, "
      "NaviCore refuses its access point at the next boot - 'REFUSED: password is 3 character(s)' and 'Not starting an "
      "open AP', no SoftAP or WebSocket line - and the PC's fresh scan shows no open network under its name and the PC "
      "cannot join it; the saved password written back and a second restart bring the access point back, which the PC "
      "joins (NaviCore restarts twice)", needs=["navicore"], links=[], opt_in="navicore_wifi")
def refuse_short_password(bench):
    """NAVICORE.md nc.cfg.wifi_fields and nc.wifi.ap_bringup. rcConfigFromJSON stores wifiPassword as sent, with no
    length check (rc_config.h:1529-1530); setup() refuses a password of 1-7 bytes and starts no access point at all,
    never an open one (NaviCore.ino:4711-4718: 'fail closed', rc_config.h:646-653). The short password is a random
    3-letter throwaway, never used for a network and never printed (Bench.log hashes it on NaviCore's lines). What the
    PC sees: a WlanScan first (hil/wlan.py networks), since netsh lists the last scan's results, and no network under
    NaviCore's SSID may be Open; whether the name is still listed is noted, not failed (Windows can keep a network it
    no longer hears in its list for a while). The proof the access point is down is the join: with the real password
    it must fail to associate. Then the snapshot is written back (SET_CONFIG of the exact text) and NaviCore restarts
    onto it however the body ended, and ap_block and a join prove the access point back. nc_guard proves the config
    byte-identical afterwards. Off Windows the scan and the join go through the Realtek menu (bench.json wifi_switch,
    hil/wlan.py realtek_scan and realtek_on_ap; D82): a fresh scan must not list NaviCore's network, or, still listed
    (a scan can keep a network it no longer hears), the join must not be proved. The menu shows names only, so 'not
    open' rests on that failed join. Without the switch the test skips: a spare adapter that merely stays off a refused
    access point cannot tell refused from open."""
    if not wlan.ON_WINDOWS and not wlan.realtek_enabled(bench):
        raise Skip(f"{NOT_WINDOWS_SKIP}: this test's fresh scan and its join attempt are the PC's own, and off Windows "
                   f"the harness joins nothing")
    _reboot_too(bench)
    adapter = _spare_adapter(bench) if wlan.ON_WINDOWS else None
    nc = _nc(bench)
    _restartable(nc)
    problems, facts = [], {}
    with nc_guard(bench, nc=nc) as g:
        ap = ap_of(g.before)
        if ap is None:
            raise Skip("NaviCore hosts no access point: wifiEnabled is off or its AP password is under 8 characters")
        ssid, pw = ap
        version = g.nc.ping()
        failure = None
        try:
            short = "".join(secrets.choice(string.ascii_lowercase) for _ in range(3))
            g.nc.set_config({"wifiPassword": short})
            _, lines, _ = _restart(g.nc, "json")
            banner = banner_lines(lines)
            if not banner:
                problems.append("no boot banner arrived after the first REBOOT")
            else:
                problems += refused_block(banner, 3)
            if adapter is not None:
                nets, fresh = networks(adapter["name"])
                mine = [n for n in nets if n["ssid"] == ssid]
                facts.update(scan="fresh" if fresh else "cached", visible=len(nets), still_listed=bool(mine))
                if any((n["auth"] or "").lower() == "open" for n in mine):
                    problems.append("the PC sees an OPEN network under NaviCore's access point name")
                refused = ("did not associate",)
            else:
                listed = wlan.realtek_scan()
                facts.update(scan="fresh (Realtek menu)", visible=len(listed), still_listed=ssid in listed)
                refused = ("does not list", "was not proved on")
            if adapter is not None or facts.get("still_listed"):
                try:
                    with pc_on_ap(bench, problems, ssid, pw, "NaviCore's", spare_only=True, reach=(NC_AP_IP, 80),
                                  identify=_navicore_ap(bench, version)):
                        problems.append("the PC joined NaviCore's access point while its saved password was 3 "
                                        "characters")
                except AssertionError as e:
                    if not any(r in str(e) for r in refused):
                        problems.append(f"joining the refused access point: {_line1(e)}")
        except Exception as e:  # noqa: BLE001 - raised after the put-back below
            failure = e
        try:
            g.nc.set_config(g.before_text)
            _, lines, _ = _restart(g.nc, "json")
        except AssertionError as e:
            raise AssertionError((f"{failure}\n" if failure is not None else "") + f"NaviCore's saved AP password was "
                                 f"not put back and applied: {_line1(e)}; nc_guard writes the config back, and `python -m "
                                 f"hil.ncflash reset` (from tests/hil, with no run holding the bench) restarts NaviCore "
                                 f"onto it") from None
        if failure is not None:
            raise failure
        back = banner_lines(lines)
        got, _ = ap_block(back, g.before) if back else (["no boot banner arrived after the second REBOOT"], {})
        problems += [f"after the password was put back: {p}" for p in got]
        if not got:
            facts["join_s"] = _join_and_ping(bench, problems, g.before, version, "after the password was put back")
    bench.note(f"ncwifi.refuse_short_password: {facts}")
    assert not problems, "; ".join(problems)


# ============================================================ findings (should)
@test("ncwifi.ws_line_trim", "(should) OPT-IN (navicore_wifi): a line with a leading or a trailing space or tab runs over "
      "the WebSocket as it does over USB, which trims every line: ' {\"type\":\"PING\"}', '\\t#L12' and '?version ' "
      "answered on both", needs=["navicore"], links=[], opt_in="navicore_wifi")
def ws_line_trim(bench):
    """NAVICORE.md D-NC61. handleSerialInput trims each USB line before it dispatches it (serialInputBuf.trim(),
    NaviCore.ino:4296-4306); the socket's lines go to the same processInputLine untrimmed (wsHandler copies the bytes
    between line ends, navicore_wsserver.h:447-468; drain passes them on, :545). processInputLine switches on the first
    character - '?', '{' or '#' (NaviCore.ino:3814, :3820, :4281) - so a leading space or tab drops the line without a
    word, and WcbMgmt matches '?version' whole (strcasecmp, WCB_Mgmt.h:166, :373), so a trailing space turns it into
    'Unknown command: ?version '. navicore_wsserver.h's header and PROTOCOLS.md both say the socket carries the same
    protocol as USB because it feeds the same dispatcher (:5-8; PROTOCOLS.md 'there is no second command surface'): the
    framing is where they part. The USB half checks the harness's own premise, and fails the test normally."""
    cases = ((' {"type":"PING"}', PONG.pattern), ("\t#L12", r"Mode=\d+"), ("?version ", r"^Software Version: "))
    problems, ws_missing = [], []
    with _on_ap(bench, problems) as (nc, cfg, _):
        ws = _open(bench)
        try:
            for line, rx in cases:
                m = _sync(ws)
                ws.send(line)
                try:
                    ws.expect(rx, timeout=3, since=m)
                except AssertionError:
                    ws_missing.append(repr(line))
                time.sleep(0.3)
        finally:
            ws.close()
        for line, rx in cases:
            m = _sync(nc.dev)
            nc.dev.send(line)
            try:
                nc.dev.expect(rx, timeout=3, since=m)
            except AssertionError:
                problems.append(f"USB did not answer {line!r}: the trim this test compares with is gone")
    assert not problems, "; ".join(problems)
    assert not ws_missing, (f"(should, D-NC61) the WebSocket did not answer {', '.join(ws_missing)}: USB trims each "
                            f"line (NaviCore.ino:4299) and the socket's go to processInputLine as sent "
                            f"(navicore_wsserver.h:447-468, :545), so a leading space or tab drops a line silently and "
                            f"a trailing one makes '?version ' an unknown command; trim in drain() as USB does")


@test("ncwifi.ws_stalled_client", "(should) OPT-IN (navicore_wifi, and navicore_reboot): a WebSocket client that stops "
      "reading for 15 s while NaviCore streams costs the other client none of its replies, and is not left open and "
      "deaf: once it reads again a line it sends is answered on its own socket, or the socket is closed so it can "
      "reconnect; NaviCore does not restart (it may, if D-NC62's overrun hits)", needs=["navicore"], links=[],
      opt_in="navicore_wifi")
def ws_stalled_client(bench):
    """NAVICORE.md D-NC62. Every socket write is a work item on the one httpd task (wsSendWork, navicore_wsserver.h
    :274-294), which sends it to each client in turn with a blocking send (HTTPD_DEFAULT_CONFIG's 5 s
    send_wait_timeout, which begin() keeps, :478-492; esp_http_server.h:53-68). A client that stops reading holds that
    task for up to 5 s a frame, every other client waits behind it, and pump() keeps queueing (:196-233) into a control
    socket that holds 6 (CONFIG_LWIP_UDP_RECVMBOX_SIZE, with CONFIG_HTTPD_QUEUE_WORK_BLOCKING off in core 3.3.4's
    sdkconfig): a work item past those is refused, and then WsSink::write() overruns its 2 KB buffer (it stores at
    _buf[_len++] after a pump() that could not drain it, :151-167 with :212-227), or is lost with its PSRAM copy. When a
    send to the stalled client finally fails, wsSendWork drops it from the sink (:288-289) but leaves its session open,
    and only a handshake adds a client (:395-398): it can still send lines that run, and never receives another. Here
    client A reads nothing (a 4 KB receive buffer, hil/ncws.py pause) while client B starts the monitor and PINGs every
    2 s; then the monitor stops (over USB), A reads again and sends a marker line. Fails today as designed if A's line
    runs (USB shows it) but A gets nothing back on an open socket, if a PING of B's is never answered, or if NaviCore
    restarted (its uptime). B's worst PONG delay is noted. navicore_reboot must be ticked too: the overrun can crash
    NaviCore, which restarts it."""
    _reboot_too(bench)
    problems, facts, should = [], {}, []
    with _on_ap(bench, problems) as (nc0, cfg, _):
        _restartable(nc0)
        with nc_guard(bench, nc=nc0) as g:
            up0, t0 = _uptime_ms(g.nc), time.monotonic()
            a = _open(bench, name="ncws-a", rcvbuf=STALL_RCVBUF)
            b = _open(bench, name="ncws-b")
            answered, ran, delays, lost = None, False, [], 0
            try:
                _sync(a)
                _sync(b)
                a.pause()
                NaviCore(b).ack({"type": "START_MONITOR"})
                t_end = time.monotonic() + STALL_S
                while time.monotonic() < t_end and b.connected:
                    ts, m = time.monotonic(), b.mark()
                    try:
                        b.send(PING_LINE)
                        b.expect(PONG.pattern, timeout=8, since=m)
                        delays.append(round(time.monotonic() - ts, 2))
                    except AssertionError:
                        lost += 1
                    time.sleep(max(0.0, 2.0 - (time.monotonic() - ts)))
                try:
                    g.nc.ack({"type": "STOP_MONITOR"})
                except AssertionError as e:
                    facts["stop_monitor"] = _line1(e)            # a NaviCore that crashed is still booting
                a.resume()
                time.sleep(3.0)                                  # A reads what its window held
                tag = marker("A")
                am, um = a.mark(), g.nc.dev.mark()
                try:
                    a.send(f"?{tag}")
                    a.expect(_echo_rx(tag), timeout=5, since=am)
                    answered = "answered"
                except AssertionError:
                    answered = "closed" if a.closed else "deaf"
                time.sleep(0.5)
                ran = echo(tag) in g.nc.dev.since(um)
            finally:
                a.close()
                b.close()
            up1, since_ms = _uptime_after(g.nc), (time.monotonic() - t0) * 1000
    facts.update(a=answered, a_line_ran=ran, b_delays=delays, b_lost=lost, a_opcodes=a.opcodes)
    bench.note(f"ncwifi.ws_stalled_client: {facts}")
    assert not problems, "; ".join(problems)
    if answered == "deaf":
        should.append(f"client A, reading again, got nothing on its still-open socket (its line "
                      f"{'ran: USB shows it' if ran else 'did not run'})")
    if lost:
        should.append(f"{lost} of client B's PINGs got no PONG within 8 s while A stalled (worst answered delay "
                      f"{max(delays) if delays else '-'} s)")
    if _restarted(up1, since_ms):
        should.append(f"NaviCore restarted during the stall (uptime {up1} ms, {since_ms:.0f} ms after it began)")
    assert not should, ("(should, D-NC62) " + "; ".join(should) + ": one stalled WebSocket client holds the single "
                        "httpd send task (5 s send timeout, navicore_wsserver.h:274-294, :478-492), the work items queued "
                        "meanwhile are refused or lost, and a client whose send fails is dropped from the sink with its "
                        "session left open (:288-289, :395-398); close the session on a failed send, bound the time one "
                        "client can hold the task, and drop the whole line in write() when pump() cannot drain the "
                        "buffer (:151-167)")


EDITLOAD_N = 600                # a range over the 512 events a relayed EDITLOAD is cut to (NaviCore.ino:3573-3575)
CLIP_EV = re.compile(r"^\[CLIPDL:EV,(\d+)\]")
CLIP_EVB = re.compile(r"^\[CLIPDL:EVB,(\d+)\](.*)$")


def editload_counts(lines):
    """A ranged ?REC,EDITLOAD reply -> (the n its [CLIPDL:BEGIN] echoes or None, how many event indices arrived): one per
    [CLIPDL:EV,<i>] line and one per tuple of a [CLIPDL:EVB,<first>] line (navicore_record.h editStream :731-879).
    Read here rather than through the driver's parse_clip_range, which refuses a range answered shorter than asked."""
    n, idx = None, set()
    for x in lines:
        if x.startswith("[CLIPDL:BEGIN]") and n is None:
            try:
                n = json.loads(x[len("[CLIPDL:BEGIN]"):]).get("n")
            except ValueError:
                pass
            continue
        m = CLIP_EV.match(x)
        if m:
            idx.add(int(m.group(1)))
            continue
        m = CLIP_EVB.match(x)
        if m:
            try:
                body = json.loads(m.group(2))
            except ValueError:
                continue
            idx.update(int(m.group(1)) + k for k in range(len(body.get("e", []))))
    return n, len(idx)


def _editload(nc, name, n, timeout=20.0):
    """?REC,EDITLOAD,<name>,0,<n>,B over `nc`'s transport and its #L12 poke -> editload_counts of the reply. The clip
    goes into the recorder's buffer (the caller has checked it was empty, and empties it after)."""
    m = nc.dev.mark()
    nc.dev.send(f"?REC,EDITLOAD,{name},0,{n},B")
    nc.dev.send("#L12")
    nc.dev.expect(r"^\[CLIPDL:(END|ERR)\]|^\[REC\] clip '", timeout=timeout, since=m)
    return editload_counts(nc.dev.since(m))


@test("ncwifi.usb_editload_with_socket", "(should) OPT-IN (navicore_wifi): with a socket connected, a ?REC,EDITLOAD range "
      "over USB is not taken for one relayed over the mesh: 600 events of a saved clip come back as 600 events, as they "
      "do with no socket (reads only; the recorder's buffer, empty before, is emptied after)", needs=["navicore"],
      links=[], opt_in="navicore_wifi")
def usb_editload_with_socket(bench):
    """NAVICORE.md D-NC63. EDITLOAD decides whether a download is going over the mesh from rcSerial.captureArmed()
    (NaviCore.ino:3559-3564), which was true only while a relayed CLI line ran (rc_serial.h:85-87). The WebSocket's tee
    keeps the capture armed for a client's whole session (navicore_wsserver.h:88-93, :526-530), and drain() re-arms it
    every pass before handleSerialInput() runs (NaviCore.ino:5444, :5525), so while any socket is open every EDITLOAD,
    a USB one included, is handled as relayed: a range over 512 events is cut to 512 (:3573-3575,
    and BEGIN and END echo the cut n, navicore_record.h:750-753, :876-877), a whole clip over 3000 events is refused with
    'connect over USB' (:3576-3579), and the stream is paced for RTERM and never waits for USB room (navicore_record.h
    :809-812, :829, :859), the wait that stopped events vanishing from a download a busy host fell behind on. The
    request is 600 events of the largest saved clip that has more (the bench has rec_4, 698, and rec_10, 640); first
    with no socket open, where it must come back whole (the premise, a normal failure), then over USB with one socket
    connected. The recorder must be idle and empty (else Skip: the load would replace a take), and ?REC,CLEAR empties it
    again (NaviCore.ino:3605)."""
    nc = _nc(bench)
    state, events = nc.rec_info()[:2]
    if state != "idle" or int(events):
        raise Skip(f"NaviCore's recorder is {state} with {events} events: loading a clip would replace them")
    clips = sorted((c for c in nc.rec_ls() if c[3] > EDITLOAD_N), key=lambda c: -c[3])
    if not clips:
        raise Skip(f"no saved clip on NaviCore holds over {EDITLOAD_N} events")
    name = clips[0][0]
    problems, got = [], {}
    try:
        # A socket an earlier test left - its PC end went away with the temporary profile, with no FIN or RST - keeps
        # the tee armed: WsSink.live() holds while any fd is in the sink (navicore_wsserver.h:146-149), and an fd
        # leaves it only when a send to it fails (:288-289), which a peer that is gone never makes happen (runs
        # 20260929-114920 and -120247 cut this baseline with no socket open; PINGs did not clear it). With
        # navicore_reboot ticked, a cut baseline is asked again after a restart, which empties the sink.
        got["alone"] = _editload(nc, name, EDITLOAD_N)
        if got["alone"] != (EDITLOAD_N, EDITLOAD_N) and _opted(bench, "navicore_reboot"):
            bench.note("the no-socket baseline came back cut: restarting NaviCore to clear a WebSocket session it holds")
            _restart(nc, "json")
            nc = _nc(bench)
            got["alone"] = _editload(nc, name, EDITLOAD_N)
        with _on_ap(bench, problems) as (nc1, cfg, _):
            ws = _open(bench)
            try:
                _sync(ws)                                        # the socket's tee is armed: drain() has run
                _sync(nc1.dev)
                got["with_socket"] = _editload(nc1, name, EDITLOAD_N)
            finally:
                ws.close()
    finally:
        try:
            _nc(bench).cli("?REC,CLEAR", until=r"^\[REC\] cleared")
        except AssertionError as e:
            problems.append(f"?REC,CLEAR did not empty the recorder's buffer again: {_line1(e)}")
    bench.note(f"ncwifi.usb_editload_with_socket: clip {name} ({clips[0][3]} events); (BEGIN's n, events) {got}")
    if got.get("alone") != (EDITLOAD_N, EDITLOAD_N):
        problems.append(f"with no socket open, EDITLOAD of {EDITLOAD_N} events came back as {got.get('alone')} "
                        f"(BEGIN's n, events), after a restart too if navicore_reboot is ticked: a WebSocket session "
                        f"NaviCore still holds keeps the tee armed (D-NC62), so the comparison has no baseline")
    assert not problems, "; ".join(problems)
    assert got.get("with_socket") == (EDITLOAD_N, EDITLOAD_N), (
        f"(should, D-NC63) with a socket connected, EDITLOAD of {EDITLOAD_N} events over USB came back as "
        f"{got.get('with_socket')} (BEGIN's n, events): rcSerial.captureArmed() (NaviCore.ino:3564) is held true for a "
        f"socket's whole session (navicore_wsserver.h:526-530), so a USB download is cut to 512 events a range, a "
        f"whole clip over 3000 is refused, and the stream stops waiting for USB room (navicore_record.h:809-812); "
        f"decide 'relayed' from the transport the line came in on")
