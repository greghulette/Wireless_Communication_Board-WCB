"""NaviCore on the mesh (docs/hil_plan/NAVICORE.md NC-WP6, ids ncmesh.*): the JSON bridge through W1 and its fragment
layer, the management relay NaviCore runs for the Wizard, its remote terminal, WDP in both directions, the telemetry W1
relays, and what NaviCore does while boards go quiet.

How NaviCore is reached and watched (hil/ncmesh.py, INF6):
- Bridged JSON is ';W20,{json}' typed on W1. W1 prints NaviCore's replies - and any mesh payload that starts with '{' -
  on its USB only while its relay window is open: 20 s from the last ';W20,' payload starting with '{' typed on it
  (WCB.ino:8019-8021; the relay :5513-5520 and :5821-5826), a fragment envelope included. A reply too long for one
  packet comes back as fragment envelopes with no "sys", one per FRAG_PACING_MS (150 ms, rc_telemetry.h:529, :557-598);
  ncmesh.reassemble joins them. A transfer longer than the window is kept open with a bridged STOP_MONITOR, which
  NaviCore accepts over the mesh and ignores (rc_telemetry.h:2399-2402).
- ncmesh.deaf flips a WCB's third MAC octet, which changes what the board RECEIVES at once and what it transmits only at
  its next boot. A deaf W2 therefore still heartbeats and stays ONLINE on NaviCore, which marks a board online on a
  heartbeat or a boot announce only (WCB_Client.cpp:2716-2769) and offline 50 s after the last one (10 s x 5,
  :2480-2508; WCB_Client.h:1097-1098). A deaf board is one that never ACKs. To take W2 off NaviCore's roster, its own
  heartbeat interval is stretched instead (ncmesh.online_tracking_flip). W1, by contrast, counts any valid ETM packet
  as presence (WCB.ino:5322-5340), so NaviCore's 2 s rc_hb keeps it online there unless W1 itself is deaf.
- The remote terminal: a '?...' or '#...' line from the mesh waits in a 3-deep queue (NaviCore.ino:4843, :2925-2930),
  runs in loop() and is teed back to its sender in 160-byte RTERM packets (navicore_rterm.h:48-86; drainRemoteCli,
  NaviCore.ino:5010-5024), which a WCB prints as '[TERM:20]<text>', dropping empty ones (WCB_RemoteTerm.cpp:178-207).
  NaviCore's own USB shows the same lines (ncmesh.rterm_pieces predicts the split).
- The probe is NaviCore's peer through ncmesh.probe_peer: joined with quantity 20, NaviCore's duplicate window burnt.

What never goes through W1: NaviCore's GET_CONFIG (it carries the mesh password, the AP password and the AP's name,
rc_config.h:1226-1236, :1365), its ?backup (?EPASS), a stored sequence's value that this suite did not write, or the
bench's own command library. W1's console lines reach session.log unredacted (runner.REDACT_KINDS covers NaviCore and
the SBUS controller only), and a secret cut across two fragment envelopes would slip past any redaction. The bridge's
fragment sender is exercised with GET_WCB_META (the same String-backed sender as CONFIG) and with a command library the
suite writes first. The probe tests join the mesh through the existing helpers, which read W1's chain and hand the mesh
password to the probe exactly as every probe test does (suites/common.py mesh_params, probe_in_mesh).

Writes: NaviCore's config only inside nc_guard (D-NC2); a WCB's only inside config_guard, and each test undoes its own
WCB writes in a finally (_put_back, _seq_clear, W2's ETM heartbeat): config_guard fails a test whose board does not end
as it began, even when it puts the token back itself. Line numbers are NaviCore's hil-week
tree (6925773, the bench image), the WCB_Client and WcbCmd sketchbook copies it compiles (Arduino-Code/libraries,
WCB_Client 1.17.1), and this repo's Code/WCB.
"""
import json
import re
import statistics
import time

from hil import ncmesh
from hil.checkpoint import redacted_diff
from hil.nc_guard import nc_guard
from hil.navicore import DBG_MAESTRO, SBUS_FULL_FPS, NaviCore, fnv1a32, merge_mesh_stats, parse_wdp
from hil.ncmesh import bridged, deaf, fragments, probe_peer, reassemble, send_fragments
from hil.runner import Skip, test
from hil.wcb import PULL_MAX, WCB, Pull
from suites.common import config_guard, link, marker, nonce, probe_in_mesh, require_tokens, token, usb_wcb
from suites.s03_wcb import _factory_reply, _reply_problems
from suites.s05_mesh import FRAG_CHUNK, FRAG_GAP_S
from suites.s05_mesh import _sid as _frag_sid
from suites.s17_seq_inventory import _names, _seqval
from suites.s18_etm_config_wdp import _etm, _forget_everywhere, _joined
from suites.s20_ota import _crc, _session_id
from suites.s40_navicore_config import (DEFAULTS, HOOK_SKIP, SAVED, _defaults_live_effects, _flushed, _hooks,
                                        _inert_keys)
from suites.s41_navicore_engine import _count, _engine_inert, _wait_all

KEEPALIVE = {"type": "STOP_MONITOR"}    # accepted over the mesh and ignored (rc_telemetry.h:2399-2402)
KEEPALIVE_S = 8.0                       # a renewal well inside W1's 20 s relay window
SEQ_FRAG, SEQVAL_FRAG = 14, 16          # PACKET_TYPE_SEQ_FRAG / SEQVAL_FRAG: a WCB's sequence replies (WCB.ino:281-287)
ETM_RUN_MAX_S = 60.0                    # W2's characterization with one peer took about 4 s (run 20260929-025701)
ETM_REPLY_WAIT_S = 3.0                  # after W2's last frag, for NaviCore's [MGMT:ETM,2] line
ACK_SETTLE_S = 0.8                      # after a TEST_ACTION's ACK, room for a second run's (fragment_reassembly_edges)
FRAG_GAP_MIN_S = 0.12                   # FRAG_PACING_MS 150 (rc_telemetry.h:529) less the host's line stamping
PACKET_MAX = 185                        # a bridged WCB_STATUS / MESH_STATS page must fit this (rc_telemetry.h:1590)
OFFLINE_S = 50.0                        # WCB_Client: 10 s heartbeat x 5 missed (WCB_Client.h:1097-1098)
STALL_MS = 3000                         # the #L90 stall remote_cli_order_and_drop uses on a hook image
CLI_QUEUE = 3                           # remoteCliQueue's depth (NaviCore.ino:4843)
PENDING_MAX = 10                        # WCB_PENDING_MAX (WCB_Client.h:84): ETM slots before a send degrades
ETM_RETRY_S = 0.5                       # ETM_RETRY_INTERVAL_MS (WCB_Client.h:130), 3 retries (:131)
IDENTITY = ("wcbNetwork", "wcbProfiles", "boardType", "wifiEnabled", "wifiSsid", "wifiPassword")
DOT = "·"                          # onWcbStatus separates board and alias with ' · ' (NaviCore.ino:5333)


# ------------------------------------------------------------------ helpers
def _nc(bench):
    return NaviCore(bench.dev("navicore"))


def _nid(nc):
    return nc.wcb_status()["self"]


def _w2(bench):
    """W2's own USB console (hil.wcb.WCB), or Skip: the tests that use it change a W2 setting or read W2's side, and
    a relayed terminal cannot reach a board that is being made silent."""
    own = bench.usb_wcbs().get(2)
    if not own:
        raise Skip("W2 has no USB console of its own on this bench")
    return WCB(bench.dev(own))


def _open_relay(nc, w1):
    """A bridged PING -> NaviCore's id; opens W1's relay window and renews NaviCore's rc_ch subscription. Skip when no
    PONG comes back: the bridge is down, and nothing here could be observed."""
    nid = _nid(nc)
    if bridged(w1, {"type": "PING"}, rf'"type":"PONG","id":{nid}', timeout=4.0, target=nid).match is None:
        raise Skip("no bridged PONG from NaviCore: W1's relay window cannot be opened")
    return nid


def _keep(w1, nid):
    """Renew W1's relay window without asking NaviCore for anything."""
    w1.send(f";W{nid},{ncmesh.compact(KEEPALIVE)}")


def _json_since(w1, since, pred=None):
    """Every '{...}' line on W1 since mark `since` that parses, optionally filtered by pred(dict)."""
    out = []
    for x in w1.dev.since(since):
        if x.startswith("{"):
            try:
                o = json.loads(x)
            except ValueError:
                continue
            if isinstance(o, dict) and (pred is None or pred(o)):
                out.append(o)
    return out


def _await_json(w1, since, pred, timeout):
    """The first JSON line on W1 since `since` that satisfies pred, waited for up to `timeout` s, or None."""
    deadline = time.monotonic() + timeout
    while True:
        got = _json_since(w1, since, pred)
        if got:
            return got[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def _await_reassembled(w1, nid, since, sid, timeout, sample=None):
    """NaviCore's fragmented reply with fragment sid `sid`, reassembled from W1's console since `since` -> its text, or
    None after `timeout`. W1's relay window is renewed every KEEPALIVE_S; sample(), when given, runs about once a
    second meanwhile."""
    deadline = time.monotonic() + timeout
    renewed = time.monotonic()
    while True:
        text = next((t for s, t in reassemble(w1.dev.since(since)) if s == sid), None)
        if text is not None or time.monotonic() >= deadline:
            return text
        if time.monotonic() - renewed >= KEEPALIVE_S:
            _keep(w1, nid)
            renewed = time.monotonic()
        if sample:
            sample()
        time.sleep(0.9 if sample else 0.2)


def _envelope_times(w1, since, sid):
    """Host times of the fragment envelopes of `sid` W1 printed since `since`, in order."""
    return [ts for ts, x in list(w1.dev.lines[since:]) if x.startswith('{"f":') and f'"sid":{sid},' in x]


def _full_fps(nc):
    """#L09's fps once it reads full rate, re-read once a second for up to 4 s (NaviCore.sbus_full_rate): a save or a
    long transfer just before leaves low one-second windows for a while - nc_guard's snapshot, which reads the whole
    command library, left 27 and then 77 before bridged_cmdlib's gate (run 20260929-042105) while no frame was lost."""
    return nc.sbus_full_rate()["fps"]


def _require_full_rate(nc):
    fps = _full_fps(nc)
    if fps < SBUS_FULL_FPS:
        raise Skip(f"NaviCore reads its SBUS input at {fps} fps before the test (navicore.bench_health's job): no "
                   f"baseline to compare with")
    return fps


def _sbus_sample(nc, samples):
    """Append (host time, #L09 dump) to `samples`: one reading for _sbus_kept_up."""
    samples.append((time.monotonic(), nc.sbus_dump()))


def _sbus_kept_up(samples):
    """What #L09 readings taken before, during and after a transfer say about NaviCore's SBUS input -> a problem, or
    None. The claim ('#L09 fps unaffected', the plan's nc.bridge.get_config row) is that the frame counter rises at the
    full rate from the first reading to the last - at least SBUS_FULL_FPS frames a second on host time, about 111 on
    this bench - and that no reading shows lost or failsafe. The one-second fps field is not judged: it counts a window
    a busy loop() can stretch, and it read 88 while the counter rose 118 in 1.06 s (bridged_cmdlib, run
    20260929-025701). Readings under 0.8 s apart are too close for the rate: host time is good to about 50 ms."""
    bad = [(d.get("lost"), d.get("failsafe")) for _, d in samples if (d.get("lost"), d.get("failsafe")) != ("no", "no")]
    if bad:
        return f"#L09 showed lost/failsafe {bad} around the transfer"
    if len(samples) < 2 or samples[-1][0] - samples[0][0] < 0.8:
        return None
    (t0, d0), (t1, d1) = samples[0], samples[-1]
    try:
        rate = (int(d1["frames"]) - int(d0["frames"])) / (t1 - t0)
    except (TypeError, ValueError, KeyError):
        return "#L09 printed no frame counter"
    if rate < SBUS_FULL_FPS:
        return f"NaviCore counted {rate:.0f} SBUS frames a second across the transfer (about 111 at full rate)"
    return None


def _put_back(w, before, prefix, clear):
    """Undo a test's own write on WCB console `w`: the token starting with `prefix` from config_guard's snapshot
    `before`, or `clear` when the snapshot had none. config_guard fails a test whose board ends up different even when
    it puts the token back itself (it is the net, not the undo), so each test undoes its writes in a finally."""
    w.run(token(before, prefix) or clear, timeout=8)


def _seq_clear(w, *keys):
    """?SEQ,CLEAR each of the test's own sequence keys on WCB console `w`; a key never stored is only dropped from the
    key list if it is there ("No stored value found ... removed from list if present")."""
    for k in keys:
        w.run(f"?SEQ,CLEAR,{k}", timeout=8)


def _reply_leg(w, since, n, nid, ptype):
    """Which leg of a W<n> management reply to NaviCore went missing, from W<n>'s ?DEBUG,MGMT lines since `since`: each
    request handler prints '[MGMT] ... request ... from WCB<nid>' (WCB.ino:4659, :4689, :4760, :4772) and
    sendResultFrags prints 'Sent result frags (<k> chunks, type <ptype>) to WCB<nid>' (:4648-4649)."""
    lines = [x.rstrip() for x in w.dev.since(since)]
    sent = [m.group(1) for x in lines
            for m in [re.search(rf"^\[MGMT\] Sent result frags \((\d+) chunks, type {ptype}\) to WCB{nid}\b", x)] if m]
    if sent:
        return f"W{n} sent its reply in {sent[-1]} broadcast frag(s) and NaviCore took none (tracker #109)"
    if any(re.search(rf"^\[MGMT\] .*request.* from WCB{nid}\b", x) for x in lines):
        return f"W{n} logged the request and sent no reply"
    return f"W{n} logged no request: NaviCore's request was lost"


def _seq_ask(w, n, nid, call, ptype, notes):
    """call(), a NaviCore sequence pull of W<n> that raises on ok:false (NaviCore.seq / seqval), with W<n>'s ?DEBUG,MGMT
    on (console `w`, RAM only), asked once more after 'no reply' -> its result. W<n> answers a pull once, in broadcast
    frames with no second pass (sendResultFrags, WCB.ino:4611-4650; tracker #109), and NaviCore's request is itself
    three unacknowledged broadcast frames (WCB_Client.cpp:1284-1291): one lost frame costs the answer, and NaviCore says
    'no reply' 6 s later (rc_telemetry.h:1559-1562; seq_pull's first W2 pull in run 20260929-042105). The note names
    the lost leg (_reply_leg); a second 'no reply' raises with it."""
    w.debug("MGMT", True)
    try:
        for ask in (1, 2):
            m = w.dev.mark()
            try:
                return call()
            except AssertionError as e:
                if "no reply" not in str(e):
                    raise
                leg = _reply_leg(w, m, n, nid, ptype)
                if ask == 2:
                    raise AssertionError(f"{e}, twice; the second time {leg}") from None
                notes.append(f"a pull of W{n} got no reply ({leg}); asked once more")
    finally:
        w.debug("MGMT", False)


def _etm_block(lines):
    """The ETM characterization result block in console `lines` (right-stripped): its 'WCB<n> ETM Network
    Characterization' header to the closing rule (buildETMCharResultsString, WCB.ino:2298-2344), or []."""
    start = next((k for k, x in enumerate(lines) if re.match(r"^-+ WCB\d+ ETM Network Characterization -+$", x)), None)
    if start is None:
        return []
    end = next((k for k in range(start + 1, len(lines)) if re.match(r"^-{20,}$", lines[k])), len(lines) - 1)
    return lines[start:end + 1]


def _no_plain_fanout(nc):
    """Skip while NaviCore writes unprefixed mesh text out an aux port (serialBcast out, NaviCore.ino:2973-2984): the
    plain-text markers these tests send would reach whatever is wired there."""
    out = [p for p, v in (nc.config().get("serialBcast") or {}).items() if isinstance(v, dict) and v.get("out")]
    if out:
        raise Skip(f"NaviCore writes plain mesh text out {', '.join(out)} (serialBcast out)")


def _rx_seen(nc, since, sender, texts):
    """The texts of `texts` NaviCore logged as '[WCB RX] from WCB<sender>: <text>' since `since` (DBG_MAESTRO on,
    NaviCore.ino:3124-3125), after a flush of a held last line."""
    lines = _flushed(nc, since, settle=0.3)
    return [t for t in texts if f"[WCB RX] from WCB{sender}: {t}" in lines]


def _term_lines(w1, since, nid):
    """The [TERM:<nid>] texts W1 printed since `since`, prefix removed. Only CR/LF is stripped: a piece cut at 160 bytes
    can end in a space, and the relay keeps it (WCB_RemoteTerm.cpp:201-206)."""
    p = f"[TERM:{nid}]"
    return [x[len(p):].rstrip("\r\n") for x in w1.dev.since(since) if x.startswith(p)]


def _relay_lost(w1, since):
    """W1's own report of RTERM or relayed-JSON lines it dropped (WCB_RemoteTerm.cpp:211-215; WCB.ino:436-439)."""
    return [x.rstrip() for x in w1.dev.since(since) if x.startswith(("[RTERM] ", "[RCBRG] relay queue FULL"))
            and ("lost" in x or "FULL" in x)]


def _status_line(nc, n):
    """onWcbStatus's line for board n (NaviCore.ino:5330-5335): '[WCB] WCB<n>[ · <alias>] ONLINE|OFFLINE'."""
    return re.compile(rf"^\[WCB\] WCB{n}(?: {DOT} .*)? (ONLINE|OFFLINE)$")


def _mesh_row(nc, board):
    """GET_MESH_STATS' row for `board` as a dict, or zeros."""
    r = nc.mesh_stats()["peers"].get(board) or [board, 0, 0, 0, 0, 0, 0]
    return dict(zip(("id", "sent", "ackd", "rty", "fail", "ung", "recv"), r))


def _delta(a, b):
    return {k: b[k] - a[k] for k in a if k != "id"}


def _changed(a, b, keys):
    """The keys whose values differ between config dicts a and b - names only, never a value (wifiSsid and the
    passwords are among them)."""
    return [k for k in keys if a.get(k) != b.get(k)]


def _nav_regex(nc, since, pattern, timeout, what):
    """The first NaviCore console line matching `pattern` since `since`, waited for with a '#L12' flush every second (a
    lone line can sit unsent until more output follows it, docs/HIL_TESTING.md §5) -> the re.Match, or AssertionError
    naming `what` and a line count (never a line: NaviCore's console can carry the config)."""
    rx = re.compile(pattern)
    deadline = time.monotonic() + timeout
    flushed = time.monotonic()
    while True:
        for x in nc.dev.since(since):
            m = rx.search(x)
            if m:
                return m
        if time.monotonic() >= deadline:
            raise AssertionError(f"NaviCore printed no {what} within {timeout:g} s ({nc.dev.mark() - since} lines since, "
                                 f"not quoted)")
        if time.monotonic() - flushed >= 1.0:
            nc.dev.send("#L12")
            flushed = time.monotonic()
        time.sleep(0.05)


def _test_library(tag, size):
    """A command library of about `size` bytes that is plainly this suite's: one board 'HIL<tag>' with 'HIL' commands
    only, in the {"boards", "enums"} shape the tool stores (the store is opaque: GET_CMDLIB returns the bytes as sent)."""
    cmds, n = [], 0
    while len(json.dumps({"boards": [{"id": tag, "cmds": cmds}], "enums": {}}, separators=(",", ":"))) < size:
        cmds.append(f";S2{tag}{n:03d}")
        n += 1
    return json.dumps({"boards": [{"id": tag, "cmds": cmds}], "enums": {}}, separators=(",", ":"))


# ============================================================ the bridge
@test("ncmesh.bridged_wcb_meta", "A bridged GET_WCB_META reaches W1 as fragment envelopes at least 150 ms apart that "
      "reassemble to exactly the aliases, port labels and sequence hashes of the USB GET_WCB_STATUS; NaviCore's START "
      "and COMPLETE lines name the fragments W1 got, and its SBUS input stays at full rate (the fragment sender GET_CONFIG "
      "uses, fed a payload that carries no secret)", needs=["navicore", "wcb1"], links=[])
def bridged_wcb_meta(bench):
    """GET_WCB_META parks its requester (rc_telemetry.h:2288-2291); tick() arms _startFragSend with buildWcbMeta()
    (:1497-1501, :1986-2030) on the OutboundSend machine and pump GET_CONFIG uses (:617-675, :946-970): code-point-safe
    slices of at most 143 escaped bytes, one unicast every FRAG_PACING_MS (:529), the periodic telemetry held back
    meanwhile (:1577). The plan's ncmesh.bridged_get_config is not run: a CONFIG reply carries the mesh password, the AP
    password and the AP's name (rc_config.h:1226-1236, :1365), and W1's lines reach session.log unredacted, where a
    secret cut across two envelopes would slip past any redaction. What GET_CONFIG adds to this sender - its ERROR guard
    and its single-flight 'dropped' line (rc_telemetry.h:748-751, :2263-2272) - stays unexercised. The USB WCB_STATUS is
    read before and after the pull; the META must equal one of them (an advert may rename a board in between) over the
    same roster, wcbHighestKnown on both sides. The SBUS input is judged by _sbus_kept_up: #L09's frame counter from
    before the pull to after it, not the one-second fps."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    _require_full_rate(nc)
    nid = _open_relay(nc, w1)
    before = nc.wcb_status()
    samples = []
    _sbus_sample(nc, samples)
    nm, wm = nc.dev.mark(), w1.dev.mark()
    w1.send(f';W{nid},{{"type":"GET_WCB_META"}}')
    start = _nav_regex(nc, nm, r"\[RC\] WCB_META send START: (\d+) bytes \S+ (\d+) fragments to W(\d+) \(sid=(\d+)\)", 5,
                       "'WCB_META send START' line")
    total, to, sid = int(start.group(2)), int(start.group(3)), int(start.group(4))
    text = _await_reassembled(w1, nid, wm, sid, 5 + total * 0.5, sample=lambda: _sbus_sample(nc, samples))
    _sbus_sample(nc, samples)
    after = nc.wcb_status()
    lines = _flushed(nc, nm, settle=0.2)
    times = _envelope_times(w1, wm, sid)
    gaps = [round(b - a, 3) for a, b in zip(times, times[1:])]
    bench.note(f"WCB_META: {total} fragments (sid {sid}); W1 printed {len(times)}, gaps {gaps} s; SBUS fps "
               f"{[d['fps'] for _, d in samples]}, frames {[d['frames'] for _, d in samples]}")
    problems = []
    if to != bench.usb_wcb_number():
        problems.append(f"the reply went to W{to}, not to W{bench.usb_wcb_number()}, which asked")
    if text is None:
        raise AssertionError(f"W1 never got the whole WCB_META reply: {len(times)} of {total} envelopes of sid {sid}"
                             + (f"; {_relay_lost(w1, wm)}" if _relay_lost(w1, wm) else ""))
    meta = json.loads(text)
    if meta.get("sys") != 1 or meta.get("type") != "WCB_META":
        problems.append(f"the reassembled reply starts {text[:40]!r}")
    for key in ("aliases", "portLabels", "seqHash"):
        if meta.get(key) not in (before.get(key), after.get(key)):
            problems.append(f"{key} {meta.get(key)} equals neither USB WCB_STATUS read ({after.get(key)})")
    if len(times) != total:
        problems.append(f"W1 printed {len(times)} envelopes of sid {sid}; NaviCore sent {total}")
    if f"[RC] send COMPLETE: {total} fragments (sid={sid})" not in lines:
        problems.append(f"no 'send COMPLETE: {total} fragments (sid={sid})' line")
    if len(gaps) >= 1 and statistics.median(gaps) < FRAG_GAP_MIN_S:
        problems.append(f"the envelopes came {gaps} s apart; the sender paces them 150 ms")
    sbus = _sbus_kept_up(samples)
    if sbus:
        problems.append(sbus)
    assert not problems, "; ".join(problems)


@test("ncmesh.bridged_set_config", "A bridged SET_CONFIG, in one packet and as fragments, is applied, saved to LittleFS "
      "and ACKed to W1 with its saveId and NaviCore's id; a wcbNetwork.deviceId in it is ignored with its own line, and "
      "the mesh identity reads back unchanged", needs=["navicore", "wcb1"], links=[])
def bridged_set_config(bench):
    """handle() stashes a one-packet SET_CONFIG whole (rc_telemetry.h:2327-2341) and a fragmented one once every part is
    in (:2120-2159); tick() applies it on Core 1 (_applyReassembled :1097-1203): wcbNetwork.deviceId, macOct2, macOct3,
    password and quantity are stripped (:1137-1156; the deviceId with its own line), then rcConfigFromJSON, the LittleFS
    save, rcAdvertiseSerialLabels and applyConfigSideEffects run, and {"sys":1,"type":"ACK","of":"SET_CONFIG","id":<id>,
    "ok":...,"saveId":<n>} goes back to the sender (:1197-1201). Inside nc_guard. The one-packet save changes chRateHz
    (rc_ch's rate, broadcast only while a tool is subscribed; nothing moves) and names wcbNetwork.deviceId as NaviCore's
    own id, so a strip that failed would change nothing. The fragmented save puts a mapping on an inert matrix slot
    (s40 _inert_keys) whose actions, ?-queries to W1, never fire. Nothing sent carries a password, so the strip of the
    other identity fields is not observable here without changing the mesh identity (D-NC14): the deviceId line is the
    one proof, and the identity must read back unchanged."""
    w1 = usb_wcb(bench)
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        nid = _open_relay(nc, w1)
        hz = 6 if g.before.get("chRateHz") != 6 else 7
        save1 = int(nonce(), 16) % 900000 + 1000
        nm = nc.dev.mark()
        r = bridged(w1, {"sys": 1, "type": "SET_CONFIG", "saveId": save1,
                         "data": {"chRateHz": hz, "wcbNetwork": {"deviceId": nid}}},
                    rf'"of":"SET_CONFIG".*"saveId":{save1}\b', timeout=8.0, target=nid)
        ack = next((o for o in r.sys if o.get("of") == "SET_CONFIG" and o.get("saveId") == save1), None)
        want = {"sys": 1, "type": "ACK", "of": "SET_CONFIG", "id": nid, "ok": True, "saveId": save1}
        if ack != want:
            problems.append(f"one packet: ACK {ack}, expected {want}")
        lines = _flushed(nc, nm, settle=0.3)
        for rx, what in ((r"^\[RC\] SET_CONFIG \S+ deferred to main loop$", "deferred to main loop"),
                         (rf"^\[RC\] SET_CONFIG: ignoring incoming wcbNetwork\.deviceId={nid} ", "deviceId ignored"),
                         (r"^\[RC\] SET_CONFIG \S+ applied \+ saved to LittleFS$", "applied + saved"),
                         (SAVED.pattern, "RC config saved to LittleFS")):
            if not any(re.search(rx, x) for x in lines):
                problems.append(f"one packet: no '{what}' line")
        cfg = nc.config()
        if cfg.get("chRateHz") != hz:
            problems.append(f"one packet: chRateHz reads {cfg.get('chRateHz')}, not {hz}")
        if cfg.get("wcbNetwork") != g.before.get("wcbNetwork"):
            problems.append("one packet: wcbNetwork changed: " + "; ".join(redacted_diff(g.before.get("wcbNetwork"),
                                                                                        cfg.get("wcbNetwork"), limit=6)))
        key = _inert_keys(g.before, 1)[0]
        tag = marker("SC")
        acts = [{"type": "wcb_unicast", "target": str(bench.usb_wcb_number()), "cmd": "?HILNOOP", "note": f"{tag}{i}"}
                for i in range(4)]
        save2 = save1 + 1
        envs = fragments({"sys": 1, "type": "SET_CONFIG", "saveId": save2,
                          "data": {"mappings": {key: {"exclusive": False, "t1": acts, "t1note": tag}}}},
                         sid=int(nonce(), 16) % 60000 + 1)
        if len(envs) < 2:
            raise AssertionError(f"the fragmented save fits one envelope ({len(envs)}): the test's payload is too small")
        nm = nc.dev.mark()
        wm = send_fragments(w1, envs, target=nid)
        ack2 = _await_json(w1, wm, lambda o: o.get("of") == "SET_CONFIG" and o.get("saveId") == save2, 8.0)
        if ack2 != dict(want, saveId=save2):
            problems.append(f"{len(envs)} fragments: ACK {ack2}, expected ok:true with saveId {save2}")
        lines = _flushed(nc, nm, settle=0.3)
        if not any(re.search(r"^\[RC\] frag sid=\d+ COMPLETE, deferring \d+ bytes to main loop$", x) for x in lines):
            problems.append(f"{len(envs)} fragments: no 'frag sid=... COMPLETE, deferring' line")
        m = (nc.config().get("mappings") or {}).get(key) or {}
        if [a.get("note") for a in m.get("t1") or []] != [a["note"] for a in acts] or m.get("t1note") != tag:
            problems.append(f"{len(envs)} fragments: mapping {key} reads back {len(m.get('t1') or [])} actions, "
                            f"note {m.get('t1note')!r}")
        bench.note(f"one-packet save (chRateHz {hz}) and a {len(envs)}-fragment save (mapping {key}) over the bridge")
    assert not problems, "; ".join(problems)


@test("ncmesh.bridged_set_config_strip", "(should) A bridged SET_CONFIG cannot change NaviCore's radio settings: "
      "wifiEnabled (like wcbNetwork.channel) is stripped as deviceId, the MAC octets, the password and quantity are",
      needs=["navicore", "wcb1"], links=[])
def bridged_set_config_strip(bench):
    """NAVICORE.md D-NC18. _applyReassembled strips deviceId, macOct2, macOct3, password and quantity from a bridged
    SET_CONFIG (rc_telemetry.h:1137-1156) and nothing else, so wcbNetwork.channel and wifiEnabled, wifiSsid and
    wifiPassword are applied and saved (rc_config.h:1526-1531, :1783-1784): a Save over the bridge can move the droid to
    another mesh channel, or switch its SoftAP off or rename it, at the next boot. The change sent is wifiEnabled
    flipped: read only at boot (NaviCore.ino:4711), so nothing changes live, and nc_guard writes it back over USB within
    seconds; it is neither a credential nor the mesh identity. The channel is not sent: a valid other channel would be
    saved and take NaviCore off the mesh at its next boot (D-NC14), and a value the clamp turns back into the current
    one (0, or 12 and up, rc_config.h:1783-1784) cannot show whether it was stripped. Recommendation: strip channel and
    the wifi* fields too; the tool already strips channel."""
    w1 = usb_wcb(bench)
    with nc_guard(bench) as g:
        nid = _open_relay(g.nc, w1)
        was = bool(g.before.get("wifiEnabled"))
        save = int(nonce(), 16) % 900000 + 1000
        r = bridged(w1, {"sys": 1, "type": "SET_CONFIG", "saveId": save, "data": {"wifiEnabled": not was}},
                    rf'"of":"SET_CONFIG".*"saveId":{save}\b', timeout=8.0, target=nid)
        ack = next((o for o in r.sys if o.get("of") == "SET_CONFIG" and o.get("saveId") == save), None)
        now = bool(g.nc.config().get("wifiEnabled"))
    assert ack is not None and ack.get("ok") is True, f"the bridged SET_CONFIG was not ACKed ok:true ({ack})"
    assert now == was, (f"(should, D-NC18) a bridged SET_CONFIG changed wifiEnabled {was} -> {now} and saved it: "
                        f"rc_telemetry.h:1137-1156 strips only deviceId, the MAC octets, the password and quantity "
                        f"(the guard put it back)")


def _reset_effects(before):
    """[(what, pattern)] of the live re-apply lines a RESET_DEFAULTS through applyConfigSideEffects prints for bench
    config `before` (NaviCore.ino:3248-3318): a re-open for every port whose baud the defaults change (s40 DEFAULTS), and
    SBUS OUT's line when the bench has it on and the defaults turn it off."""
    have, dflt = before.get("auxBaud") or {}, DEFAULTS["auxBaud"]
    out = [(f"{k} re-open", rf"^\[AUX\] {k} re-open @ {dflt[k]} baud") for k in ("S3", "S4", "S5")
           if have.get(k) != dflt[k]]
    if have.get("maestro") != dflt["maestro"]:
        out.append(("Maestro re-open", rf"^\[Serial2\] Local Maestro re-open @ {dflt['maestro']} baud"))
    if before.get("sbusOutEnabled") and not DEFAULTS["sbusOutEnabled"]:
        out.append(("SBUS OUT off", r"^\[SBUS\] OUT disabled"))
    return out


def _bridged_reset(nc, w1, nid):
    """A bridged RESET_DEFAULTS -> (its ACK dict or None, NaviCore's lines from the send, flushed)."""
    nm = nc.dev.mark()
    r = bridged(w1, {"type": "RESET_DEFAULTS"}, r'"of":"RESET_DEFAULTS"', timeout=6.0, target=nid)
    ack = next((o for o in r.sys if o.get("of") == "RESET_DEFAULTS"), None)
    return ack, _flushed(nc, nm, settle=0.5)


def _defaults_ok(nc):
    """Skip unless the factory defaults would decode nothing from the live SBUS input (s40 _defaults_live_effects,
    D-NC44): nothing here restarts NaviCore, so a press or mode the defaults read would outlive the guard's restore."""
    effects = _defaults_live_effects(nc, nc.config())
    if effects:
        raise Skip("the factory defaults would act on the live SBUS input before the guard restores the config: "
                   + "; ".join(effects))


@test("ncmesh.bridged_reset_defaults", "A bridged RESET_DEFAULTS is ACKed to W1, loads the factory defaults into RAM "
      "(no mappings, every knob off, the default bauds), saves nothing, and runs the live side effects the USB path "
      "skips - ports re-opened, SBUS OUT off; plain text from W1 still arrives; the relayed terminal answers again once "
      "nc_guard has put the config back over USB (SBUS OUT stops for a few seconds)", needs=["navicore", "wcb1"],
      links=[])
def bridged_reset_defaults(bench):
    """handle() parks RESET_DEFAULTS (rc_telemetry.h:2451-2454); tick() runs rcConfigLoadDefaults() and
    applyConfigSideEffects() and ACKs {"sys":1,"type":"ACK","of":"RESET_DEFAULTS","ok":true} (:1538-1546), while the USB
    handler only loads the defaults (NaviCore.ino:4012-4016) - D-NC16's other half. applyConfigSideEffects re-opens
    each port whose baud changed and turns SBUS OUT off when the defaults do (:3248-3318, :3333-3364). Nothing is saved:
    the guard's restore is a USB SET_CONFIG of the snapshot, which re-opens the ports and turns SBUS OUT on again. Until
    then the RAM mesh password may be the compile-time one while the ETM stack keeps its boot copy (D-NC16, D-NC17): the
    relayed terminal's [TERM:20] reply is then lost, which is noted, not asserted, since it depends on D-NC16 (its
    (should) is ncmesh.bridged_reset_keeps_identity). What must hold either way: a plain-text command from W1 arrives
    (the ETM path), and after the restore the relayed terminal answers. Skips, before anything is sent, when the
    defaults would read a held matrix button or another mode from the live SBUS input (_defaults_ok). hil/servos.py lists
    it: SBUS OUT is off until the restore."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    _defaults_ok(nc)
    _no_plain_fanout(nc)
    problems, notes = [], []
    with nc_guard(bench) as g:
        nc = g.nc
        nid = _open_relay(nc, w1)
        ack, lines = _bridged_reset(nc, w1, nid)
        if ack != {"sys": 1, "type": "ACK", "of": "RESET_DEFAULTS", "ok": True}:
            problems.append(f"ACK {ack}")
        if not any(re.search(rf"^\[RC\] RESET_DEFAULTS from W{bench.usb_wcb_number()} \S+ live config reset to factory "
                             rf"defaults \(not persisted\)", x) for x in lines):
            problems.append("no 'RESET_DEFAULTS from W1 -> live config reset' line")
        for what, rx in _reset_effects(g.before):
            if not any(re.search(rx, x) for x in lines):
                problems.append(f"no {what} line: the live side effects did not run")
        if any(SAVED.match(x) for x in lines):
            problems.append("the reset saved the config")
        cfg = nc.config()
        if cfg.get("mappings"):
            problems.append(f"{len(cfg['mappings'])} mappings left after the reset")
        on = [k for k, v in (cfg.get("knobs") or {}).items() if v.get("function")]
        if on:
            problems.append(f"knobs {on} still have a function after the reset")
        if cfg.get("auxBaud") != DEFAULTS["auxBaud"] or cfg.get("sbusOutEnabled") != DEFAULTS["sbusOutEnabled"]:
            problems.append(f"auxBaud {cfg.get('auxBaud')} / sbusOutEnabled {cfg.get('sbusOutEnabled')} are not the "
                            f"defaults")
        same_pw = (cfg.get("wcbNetwork") or {}).get("password") == (g.before.get("wcbNetwork") or {}).get("password")
        tag = marker("RD")
        with nc.debug(DBG_MAESTRO):
            nm, wm = nc.dev.mark(), w1.dev.mark()
            w1.send(f";W{nid},{tag}")
            time.sleep(1.0)
            if not _rx_seen(nc, nm, bench.usb_wcb_number(), [tag]):
                problems.append("plain text from W1 did not reach NaviCore after the reset")
            w1.send(f";W{nid},#L12")
            time.sleep(2.0)
            term = any(x.startswith("Mode=") for x in _term_lines(w1, wm, nid))
        notes.append(f"after the reset the RAM mesh password {'is' if same_pw else 'is NOT'} the saved one and the "
                     f"relayed #L12 {'came back' if term else f'got no [TERM:{nid}] reply'} (D-NC16, D-NC17)")
    wm = w1.dev.mark()
    w1.send(f";W{nid},#L12")
    time.sleep(2.0)
    if not any(x.startswith("Mode=") for x in _term_lines(w1, wm, nid)):
        problems.append(f"after the guard's restore ;W{nid},#L12 gets no [TERM:{nid}] reply")
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("ncmesh.bridged_reset_keeps_identity", "(should) A bridged RESET_DEFAULTS keeps the network identity - "
      "wcbNetwork, wcbProfiles, boardType and the wifi fields - so a later save cannot move the droid onto the default "
      "mesh credentials, pin profile or SoftAP (SBUS OUT stops for a few seconds)", needs=["navicore", "wcb1"], links=[])
def bridged_reset_keeps_identity(bench):
    """NAVICORE.md D-NC16, the mesh path. rcConfigLoadDefaults resets the whole block - the compile-time mesh password,
    deviceId 20, quantity 4, boardType 0, the SoftAP off (rc_config.h:801-968) - and the bridged path then re-applies it
    live (rc_telemetry.h:1538-1546), so the next Save over either transport persists it; until then the raw-packet
    paths already use the default password (D-NC17). nccfg.reset_defaults_keeps_identity asks the same of the USB path.
    Restored by nc_guard over USB, no restart. The failure names the fields that changed, never a value (the SSID and
    the passwords are among them). Skips like ncmesh.bridged_reset_defaults when the defaults would act on the live SBUS
    input."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    _defaults_ok(nc)
    with nc_guard(bench) as g:
        nid = _open_relay(g.nc, w1)
        ack, _ = _bridged_reset(g.nc, w1, nid)
        cfg = g.nc.config()
    assert ack is not None, "the bridged RESET_DEFAULTS was not ACKed: nothing to judge"
    lost = _changed(g.before, cfg, IDENTITY)
    assert not lost, (f"(should, D-NC16) a bridged RESET_DEFAULTS reset {lost} to the factory values (values not "
                      f"shown); rc_telemetry.h:1538-1546 loads every default and re-applies it live")


def _bulk_push(w1, nid, data, sid, hash_=None, gap_s=0.12, rounds=3):
    """Stream `data` from W1 into NaviCore's bulk sink -> the FINAL {"bs":sid,"done":1,...} W1 printed, or None. BEGIN,
    a pause while the sink pre-extends its staging file (bulkBegin, rc_telemetry.h:796-815), each CHUNK `gap_s` apart;
    with no FINAL, DONE asks for a STATUS and only the chunks it lists as missing go again (WCB_Client.cpp:1097-1107,
    :1122-1137); a 'need begin' answer starts over."""
    begin, parts, done = ncmesh.bulk_frames(data, sid, hash_=hash_)
    m = w1.dev.mark()
    w1.send(f";W{nid},{begin}")
    time.sleep(0.4)
    todo = list(range(len(parts)))

    def final(o):
        return o.get("bs") == sid and o.get("done") == 1
    for rnd in range(rounds):
        for q in todo:
            w1.send(f";W{nid},{parts[q]}")
            time.sleep(gap_s)
        fin = _await_json(w1, m, final, 3.0)
        if fin is not None:
            return fin
        sm = w1.dev.mark()
        w1.send(f";W{nid},{done(rnd + 1)}")
        st = _await_json(w1, sm, lambda o: o.get("bs") == sid, 3.0)
        if st is None:
            continue
        if st.get("done") == 1:
            return st
        if st.get("nb"):
            w1.send(f";W{nid},{begin}")
            time.sleep(0.4)
            todo = list(range(len(parts)))
            continue
        todo = [q for q in st.get("miss") or [] if isinstance(q, int) and 0 <= q < len(parts)] or todo
    return None


@test("ncmesh.bridged_cmdlib", "The command library over the bridge: CMDLIB_META equals USB's; a library written over "
      "USB streams back to W1 as fragments equal to it byte for byte, the SBUS input at full rate meanwhile; a SET_CMDLIB "
      "in one packet and one in fragments are stored and ACKed with their size and FNV-1a hash; a bulk bb/bc transfer is "
      "published, and one whose hash does not match is discarded (nc_guard puts the library back)",
      needs=["navicore", "wcb1"], links=[])
def bridged_cmdlib(bench):
    """GET_CMDLIB_META is answered from tick() in one packet (rc_telemetry.h:2282-2285, :1567-1576). GET_CMDLIB stages
    {"type":"CMDLIB","size","hash","data":<library>} in a LittleFS file and streams it through the file-backed fragment
    sender, 80-byte slices one per 150 ms (:868-926, :683-733). SET_CMDLIB, one packet (:2327-2341) or reassembled, stores
    the bytes between "data": and the last '}' and ACKs their size and hash (:1204-1224). The bulk layer
    (WCB_Client.cpp:959-1172; its sink rc_telemetry.h:775-860) takes {"bb":sid,"n","t","h","g":"cmdlib"}, then
    {"bc":sid,"q":i,"s":<base64 of 96 bytes>} per chunk, answers FINAL {"bs":sid,"done":1,"ok":0|1,...} once every chunk
    is in, and publishes only when the FNV-1a of what landed equals "h" ('hash mismatch ... discarded' otherwise). Skips
    when no library is stored (NAVICORE.md D-NC13: an empty store cannot be put back). The bench's own library never
    crosses W1 (module docstring): what streams back is one this test wrote over USB first. nc_guard restores the
    original over USB. The SBUS input during GET_CMDLIB is judged by _sbus_kept_up (the frame counter across the
    transfer): the one-second fps read 88 in run 20260929-025701 while the counter rose at 111 a second."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    size0, hash0 = nc.cmdlib_meta()
    if not size0:
        raise Skip("no command library is stored: nothing could put 'none' back (NAVICORE.md D-NC13)")
    nid = _open_relay(nc, w1)
    problems, notes = [], []
    r = bridged(w1, {"type": "GET_CMDLIB_META"}, r'"type":"CMDLIB_META"', timeout=5.0, target=nid)
    meta = next((o for o in r.sys if o.get("type") == "CMDLIB_META"), None)
    if meta != {"sys": 1, "type": "CMDLIB_META", "size": size0, "hash": hash0}:
        problems.append(f"bridged CMDLIB_META {meta}, USB says size {size0} hash {hash0}")
    tag = marker("CL")
    with nc_guard(bench) as g:
        nc = g.nc
        _require_full_rate(nc)
        lib1 = _test_library(tag, 1500)
        nc.set_cmdlib(lib1)
        samples = []
        _sbus_sample(nc, samples)
        nm, wm = nc.dev.mark(), w1.dev.mark()
        w1.send(f';W{nid},{{"type":"GET_CMDLIB"}}')
        start = _nav_regex(nc, nm, r"\[RC\] CMDLIB file-send START: (\d+) bytes \S+ (\d+) fragments to W\d+ \(sid=(\d+)\)",
                           6, "'CMDLIB file-send START' line")
        total, sid = int(start.group(2)), int(start.group(3))
        text = _await_reassembled(w1, nid, wm, sid, 8 + total * 0.4, sample=lambda: _sbus_sample(nc, samples))
        _sbus_sample(nc, samples)
        body = lib1.encode("utf-8")
        notes.append(f"GET_CMDLIB: {len(body)} bytes of library in {total} fragments; SBUS fps "
                     f"{[d['fps'] for _, d in samples]}, frames {[d['frames'] for _, d in samples]}")
        m = re.match(r'^\{"type":"CMDLIB","size":(\d+),"hash":(\d+),"data":(.*)\}$', text or "", re.S)
        if not m:
            problems.append(f"GET_CMDLIB: no whole CMDLIB reply on W1 ({len(_envelope_times(w1, wm, sid))} of {total} "
                            f"envelopes)")
        elif m.group(3).encode("utf-8") != body or (int(m.group(1)), int(m.group(2))) != (len(body), fnv1a32(body)):
            problems.append(f"GET_CMDLIB: {len(m.group(3))} bytes size {m.group(1)} hash {m.group(2)} came back for "
                            f"{len(body)} bytes with hash {fnv1a32(body)}")
        sbus = _sbus_kept_up(samples)
        if sbus:
            problems.append(f"GET_CMDLIB: {sbus}")
        obj2 = {"boards": [], "enums": {}, "hil": tag}
        lib2 = ncmesh.compact(obj2)
        r = bridged(w1, {"sys": 1, "type": "SET_CMDLIB", "data": obj2}, r'"of":"SET_CMDLIB"', timeout=6.0, target=nid)
        ack = next((o for o in r.sys if o.get("of") == "SET_CMDLIB"), None)
        want = (len(lib2.encode()), fnv1a32(lib2))
        if ack != {"sys": 1, "type": "ACK", "of": "SET_CMDLIB", "ok": True, "size": want[0], "hash": want[1]} \
                or nc.cmdlib_meta() != want:
            problems.append(f"one-packet SET_CMDLIB: ACK {ack}, META {nc.cmdlib_meta()}, expected {want}")
        obj3 = json.loads(_test_library(tag + "F", 700))
        lib3 = ncmesh.compact(obj3)
        envs = fragments({"sys": 1, "type": "SET_CMDLIB", "data": obj3}, sid=int(nonce(), 16) % 60000 + 1)
        wm = send_fragments(w1, envs, target=nid)
        ack = _await_json(w1, wm, lambda o: o.get("of") == "SET_CMDLIB", 8.0)
        want = (len(lib3.encode()), fnv1a32(lib3))
        if not ack or not ack.get("ok") or (ack.get("size"), ack.get("hash")) != want or nc.cmdlib_meta() != want:
            problems.append(f"{len(envs)}-fragment SET_CMDLIB: ACK {ack}, META {nc.cmdlib_meta()}, expected {want}")
        lib4 = _test_library(tag + "B", 400)
        sid4 = int(nonce(), 16) % 60000 + 1
        nm = nc.dev.mark()
        fin = _bulk_push(w1, nid, lib4, sid4)
        want = (len(lib4.encode()), fnv1a32(lib4))
        lines = _flushed(nc, nm, settle=0.3)
        if not fin or fin.get("ok") != 1 or fin.get("hash") != want[1]:
            problems.append(f"bulk transfer: FINAL {fin}, expected ok 1 with hash {want[1]}")
        if f"[RC] bulk cmdlib sid={sid4} published ({want[0]} bytes, hash {want[1]})" not in lines:
            problems.append(f"bulk transfer: no 'bulk cmdlib sid={sid4} published' line")
        if nc.cmdlib_meta() != want:
            problems.append(f"bulk transfer: META {nc.cmdlib_meta()}, expected {want}")
        lib5 = _test_library(tag + "X", 300)
        sid5 = sid4 % 60000 + 1
        nm = nc.dev.mark()
        fin = _bulk_push(w1, nid, lib5, sid5, hash_=fnv1a32(lib5) ^ 1)
        lines = _flushed(nc, nm, settle=0.3)
        if not fin or fin.get("ok") != 0:
            problems.append(f"bulk transfer with a wrong hash: FINAL {fin}, expected ok 0")
        if not any(x.startswith(f"[RC] bulk cmdlib sid={sid5} hash mismatch") and x.endswith("discarded") for x in lines):
            problems.append(f"bulk transfer with a wrong hash: no 'sid={sid5} hash mismatch ... discarded' line")
        if nc.cmdlib_meta() != want:
            problems.append(f"bulk transfer with a wrong hash: META {nc.cmdlib_meta()}; the previous library "
                            f"{want} must stay")
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("ncmesh.bridged_cmdlib_keys_after_data", "(should) A bridged SET_CMDLIB whose \"data\" is not the last key stores "
      "the library alone, as the USB path does: its ACK's size and hash, and CMDLIB_META, are the library's, not the "
      "library's plus the keys after it", needs=["navicore", "wcb1"], links=[])
def bridged_cmdlib_keys_after_data(bench):
    """NAVICORE.md D-NC47 (found writing NC-WP6: the plan's nc.cmdlib.bridge_divergence, which it puts inside
    ncmesh.bridged_cmdlib; a test of its own here, so that one stays green). The bridged SET_CMDLIB stores everything
    from after '"data":' to the message's last '}' (rc_telemetry.h:1209-1216), so a key after "data" is stored with the
    library. The USB handler matches brackets instead (NaviCore.ino:3902-3935), because exactly this stored a trailing
    ',"sys":1' in the library; the config tool now stamps "sys" first to stay clear of it and says the firmware no longer
    depends on key order (index.html:5611-5617), which holds for USB only. The same one-packet shape,
    {"type":"SET_CMDLIB","data":<library>,"sys":1}, goes over USB first (the control: the library stored whole) and then
    over the bridge with another library. Skips when no library is stored (D-NC13); inside nc_guard, which writes the
    original back over USB. Recommendation: one extraction for both paths, the USB path's bracket matching."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    size0, _ = nc.cmdlib_meta()
    if not size0:
        raise Skip("no command library is stored: nothing could put 'none' back (NAVICORE.md D-NC13)")
    tag = marker("CK")
    lib_u = ncmesh.compact({"boards": [], "enums": {}, "hil": tag + "U"})
    lib_b = ncmesh.compact({"boards": [], "enums": {}, "hil": tag + "B"})
    want_u, want_b = (len(lib_u.encode()), fnv1a32(lib_u)), (len(lib_b.encode()), fnv1a32(lib_b))
    with nc_guard(bench) as g:
        nid = _open_relay(g.nc, w1)
        usb = g.nc.ack({"type": "SET_CMDLIB", "data": json.loads(lib_u), "sys": 1}, of="SET_CMDLIB")
        r = bridged(w1, '{"type":"SET_CMDLIB","data":' + lib_b + ',"sys":1}', r'"of":"SET_CMDLIB"', timeout=6.0,
                    target=nid)
        ack = next((o for o in r.sys if o.get("of") == "SET_CMDLIB"), None)
        meta = g.nc.cmdlib_meta()
    bench.note(f"a {want_b[0]}-byte library with a key after it: USB ACK {usb}; bridged ACK {ack}; META after {meta}")
    assert (usb.get("ok"), usb.get("size"), usb.get("hash")) == (True,) + want_u, \
        f"the control over USB: ACK {usb}, expected ok:true with size and hash {want_u}"
    assert ack is not None, "the bridged SET_CMDLIB was not ACKed: nothing to judge"
    assert (ack.get("size"), ack.get("hash")) == want_b and meta == want_b, (
        f"(should, D-NC47) a bridged SET_CMDLIB with a key after \"data\" stored {ack.get('size')} bytes, hash "
        f"{ack.get('hash')} (META {meta}), for a {want_b[0]}-byte library with hash {want_b[1]}: rc_telemetry.h:1209-1216 "
        f"keeps everything up to the message's last '}}'")


def _fsid():
    return int(nonce(), 16) % 60000 + 1


def _ta_payload(tag, board, pad):
    """A TEST_ACTION whose action writes `tag` + CR out of W<board> S2, with `pad` bytes of an ignored key so its JSON
    needs several envelopes. TEST_ACTION is dispatched from the reassembled message too (rc_telemetry.h:1225-1235,
    NaviCore.ino:2175-2190) and ACKs the sender."""
    return {"sys": 1, "type": "TEST_ACTION", "action": {"type": "wcb_unicast", "target": str(board), "cmd": f";S2{tag}"},
            "pad": "x" * pad}


@test("ncmesh.fragment_reassembly_edges", "NaviCore's fragment reassembly: a message sent in order, reversed, or with "
      "every part twice runs once; a part whose 'of' disagrees is dropped and the right one still completes; parts 3.5 s "
      "apart complete (the timeout is idle, not total); malformed envelopes are dropped and take no pool slot; a fourth "
      "session while three are open is dropped with 'Fragment pool exhausted'; a part 6.5 s late is lost with its "
      "session", needs=["navicore", "wcb1"], links=["W1S2"])
def fragment_reassembly_edges(bench):
    """handle()'s fragment branch (rc_telemetry.h:2074-2161): an envelope with f < 1, of < 1, of > 192, f > of or sid 0
    is dropped before a session is claimed (:2079-2081); sessions match on (sid, sender) in a pool of 3
    (_findOrAllocSession :194-226), a full pool prints '[RC] Fragment pool exhausted — dropping' (:2083-2086); a session's
    "of" is fixed by its first part and a disagreeing one is dropped (:2091); a repeat of a part is not counted twice
    (:2095-2100); every part, repeats included, pushes the session's deadline FRAG_TIMEOUT_MS (5 s) on (:2110), and an
    expired session is reclaimed before the next claim (:203-207). The payload is a TEST_ACTION writing a marker out of
    W1 S2 (the W1S2 probe), dispatched once the message is whole, with its ACK to W1. That ACK reaches W1 about 0.1 s
    after the marker reaches the probe, so a case that expects a run waits for its ACKs and ACK_SETTLE_S more (a second
    run would come about 0.2 s after the first) before the next case starts: counted at the marker, the reversed and
    the 'of' cases read 0 and the cases after them 2, each ACK landing one case late (run 20260929-025701; NaviCore ran
    every message exactly once). The pool case runs after the malformed one, so malformed envelopes that took slots
    would exhaust it early; the late-part case runs last, as the orphan session it leaves holds a slot for 5 s."""
    l12 = link(bench, 1, "S2")
    nc, w1 = _nc(bench), usb_wcb(bench)
    nid = _open_relay(nc, w1)
    me = bench.usb_wcb_number()
    problems, notes = [], []

    def ta_acks(since):
        return len(_json_since(w1, since, lambda o: o.get("of") == "TEST_ACTION"))

    def settle_acks(since, want, timeout=2.0):
        """The TEST_ACTION ACKs on W1 since `since` once `want` are in (or `timeout` passed), and ACK_SETTLE_S later."""
        deadline = time.monotonic() + timeout
        while ta_acks(since) < want and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(ACK_SETTLE_S)
        return ta_acks(since)

    def case(label, tag, send, want, wait=3.0):
        pm, wm = l12.mark(), w1.dev.mark()
        send()
        if want:
            _wait_all(l12, pm, [tag], wait)
            acks = settle_acks(wm, want)
        else:
            time.sleep(wait)
            acks = ta_acks(wm)
        n = _count(l12.received(pm), tag)
        notes.append(f"{label}: ran {n}, {acks} ACK")
        if n != want:
            problems.append(f"{label}: the action ran {n} time(s), expected {want}")
        if acks != want:
            problems.append(f"{label}: {acks} TEST_ACTION ACK(s) on W1, expected {want}")

    def envs_for(tag, pad=300, sid=None):
        e = fragments(_ta_payload(tag, me, pad), sid or _fsid())
        if len(e) < 2:
            raise AssertionError(f"the test payload fits one envelope (pad {pad}): nothing to reassemble")
        return e

    t = marker("F1")
    e = envs_for(t)
    case(f"{len(e)} parts in order", t, lambda: send_fragments(w1, e, target=nid), 1)
    t = marker("F2")
    e = envs_for(t)
    case("reversed", t, lambda: send_fragments(w1, e, order=list(range(len(e)))[::-1], target=nid), 1)
    t = marker("F3")
    e = envs_for(t)
    case("every part twice", t, lambda: send_fragments(w1, e, order=[i for i in range(len(e)) for _ in (0, 1)],
                                                       target=nid), 1)
    t, sid = marker("F4"), _fsid()
    e = envs_for(t, sid=sid)
    last = json.loads(e[-1])
    wrong = ncmesh.envelope(last["f"], last["of"] + 1, sid, last["s"])
    # The disagreeing part does not renew the session's deadline, so the right one must follow within the timeout.
    case("the last part with 'of' + 1", t, lambda: send_fragments(w1, e[:-1] + [wrong], target=nid), 0,
         wait=min(1.5, ncmesh.FRAG_TIMEOUT_S * 0.4))
    case("... then the right last part", t, lambda: send_fragments(w1, [e[-1]], target=nid), 1)
    t, gap = marker("F5"), round(ncmesh.FRAG_TIMEOUT_S * 0.7, 2)
    e = envs_for(t)
    case(f"{len(e)} parts {gap} s apart", t, lambda: send_fragments(w1, e, target=nid, gap_s=gap), 1)
    t, sid = marker("F6"), _fsid()
    one = json.loads(envs_for(t, sid=sid)[0])
    bad = [ncmesh.envelope(0, one["of"], sid, one["s"]), ncmesh.envelope(one["of"] + 1, one["of"], sid, one["s"]),
           ncmesh.envelope(1, 0, sid, one["s"]), ncmesh.envelope(1, ncmesh.MAX_PARTS + 1, sid, one["s"]),
           ncmesh.envelope(1, one["of"], 0, one["s"])]
    case("five malformed envelopes", t, lambda: send_fragments(w1, bad, target=nid), 0, wait=1.5)
    tags = [marker(f"P{i}") for i in range(4)]
    sess = [envs_for(x, pad=150) for x in tags]
    pm, nm, wm = l12.mark(), nc.dev.mark(), w1.dev.mark()
    send_fragments(w1, [s[0] for s in sess], target=nid)                     # three open sessions, then a fourth
    send_fragments(w1, [x for s in sess[:3] for x in s[1:]], target=nid)     # the three complete
    _wait_all(l12, pm, tags[:3], 4.0)
    send_fragments(w1, sess[3], target=nid)                                  # the fourth, whole, now there is room
    _wait_all(l12, pm, tags, 4.0)
    acks = settle_acks(wm, 4)
    got = l12.received(pm)
    lines = _flushed(nc, nm, settle=0.3)
    full = sum(1 for x in lines if x.startswith("[RC] Fragment pool exhausted"))
    counts = [_count(got, x) for x in tags]
    notes.append(f"pool: runs {counts}, {acks} ACK, {full} 'pool exhausted' line(s)")
    if counts != [1, 1, 1, 1] or acks != 4:
        problems.append(f"pool: the four sessions ran {counts} times with {acks} ACK(s), expected once each")
    if full < 1:
        problems.append("pool: no 'Fragment pool exhausted' line for the fourth session while three were open")
    t = marker("F8")
    e = envs_for(t)

    def late():
        send_fragments(w1, e[:-1], target=nid)
        time.sleep(ncmesh.FRAG_TIMEOUT_S + 1.5)
        send_fragments(w1, [e[-1]], target=nid)
    case(f"the last part {ncmesh.FRAG_TIMEOUT_S + 1.5:g} s late", t, late, 0, wait=1.5)
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


def _free_id(nc, candidates):
    """The first of `candidates` NaviCore does not know (GET_WCB_STATUS known[]: learned peers, and anything heard in the
    last 180 s), or Skip."""
    known = nc.wcb_status().get("known") or []
    for p in candidates:
        if not (p <= len(known) and known[p - 1]):
            return p
    raise Skip(f"every candidate probe id {list(candidates)} is known to NaviCore")


@test("ncmesh.bridged_status_meta_stats", "Bridged GET_WCB_STATUS fits one packet (185 bytes) and names W1 as the relay, "
      "and with a temporary probe joined above the floor it still lists every board the USB reply knows (sparse rows "
      "when the arrays do not fit, the probe marked temporary); bridged GET_MESH_STATS pages each fit a packet and merge "
      "to the USB rows; a bridged RESET_MESH_STATS zeroes the counters and sends no reply",
      needs=["navicore", "wcb1", "probe2"], links=[])
def bridged_status_meta_stats(bench):
    """The bridged WCB_STATUS is built in tick() (rc_telemetry.h:1586-1621) by buildWcbStatus (:1879-1979): aliases shed
    first, then the relay's name, then the sparse form [id, online, client, temporary] per known board, and only then
    the roster trimmed from the top - so every known board stays listed while one fits; the relaying board is kept out
    of the roster's extension (wcbHighestKnownExcl :1870-1877) and named in "relay". MESH_STATS goes in pages of at most
    185 bytes, 150 ms apart, the last one "last":1 (:1631-1652, buildMeshStatsPage :1361-1433), merged by board id
    (hil.navicore.merge_mesh_stats). RESET_MESH_STATS only raises a flag that loop() drains, printing '[WCB] mesh stats
    cleared' (:2374-2377, NaviCore.ino:5507-5513). The probe (probe2) joins as a TEMPORARY client above the floor and
    sends nothing; probe_in_mesh forgets it on every WCB afterwards, and NaviCore ages it out."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    nid = _open_relay(nc, w1)
    me = bench.usb_wcb_number()
    problems, notes = [], []

    def bridged_status(label):
        r = bridged(w1, {"type": "GET_WCB_STATUS"}, r'^\{"sys":1,"type":"WCB_STATUS"', timeout=5.0, target=nid)
        if r.match is None:
            problems.append(f"{label}: no bridged WCB_STATUS")
            return None
        line = r.match.string.rstrip()
        try:
            st = json.loads(line)
        except ValueError:
            problems.append(f"{label}: the bridged WCB_STATUS ({len(line)} bytes) does not parse")
            return None
        form = "sparse" if "rows" in st else "positional" + ("" if "aliases" in st else " without aliases")
        notes.append(f"{label}: {len(line.encode())} bytes, {form}")
        if len(line.encode()) > PACKET_MAX:
            problems.append(f"{label}: {len(line.encode())} bytes, over one packet ({PACKET_MAX})")
        if st.get("relay") != me:
            problems.append(f"{label}: relay {st.get('relay')}, expected W{me}")
        return st

    def compare(label, st, usb):
        rows, want = ncmesh.status_rows(st), ncmesh.status_rows(usb)
        for n, u in want.items():
            if not u["known"] or n == usb.get("self"):
                continue
            b = rows.get(n)
            if not b or not b["known"]:
                problems.append(f"{label}: WCB{n}, known over USB, is missing from the bridged reply")
            elif (b["online"], b["client"]) != (u["online"], u["client"]):
                problems.append(f"{label}: WCB{n} online/client {b['online']}/{b['client']} bridged, "
                                f"{u['online']}/{u['client']} over USB")
        return rows
    usb = nc.wcb_status()
    st = bridged_status("idle mesh")
    if st:
        if (st.get("self"), st.get("quantity")) != (usb.get("self"), usb.get("quantity")):
            problems.append(f"self/quantity {st.get('self')}/{st.get('quantity')} bridged, "
                            f"{usb.get('self')}/{usb.get('quantity')} over USB")
        compare("idle mesh", st, usb)
    pid = _free_id(nc, (14, 15, 13, 12, 11, 10))
    with probe_in_mesh(bench, "probe2", pid):
        deadline, polled = time.monotonic() + 12, False
        while not (ncmesh.status_rows(nc.wcb_status()).get(pid) or {}).get("known"):
            if time.monotonic() > deadline:
                raise AssertionError(f"NaviCore never listed the probe as WCB{pid}")
            if not polled and time.monotonic() > deadline - 6:
                w1.run("?WDP,POLL")
                polled = True
            time.sleep(0.5)
        usb2 = nc.wcb_status()
        st2 = bridged_status(f"probe as WCB{pid}")
        if st2:
            rows = compare(f"probe as WCB{pid}", st2, usb2)
            p = rows.get(pid)
            if not p or not p["known"]:
                problems.append(f"the bridged reply lost WCB{pid}, the probe")
            elif "rows" in st2 and (p["client"], p["temporary"]) != (1, 1):
                problems.append(f"the sparse row for WCB{pid} reads client {p['client']} temporary {p['temporary']}, "
                                f"expected 1 and 1")
    wm = w1.dev.mark()
    w1.send(f';W{nid},{{"type":"GET_MESH_STATS"}}')
    last = _await_json(w1, wm, lambda o: o.get("type") == "MESH_STATS" and o.get("last") == 1, 8.0)
    raw = [x.rstrip() for x in w1.dev.since(wm) if x.startswith('{"sys":1,"type":"MESH_STATS"')]
    after = nc.mesh_stats()
    if last is None:
        problems.append(f"bridged GET_MESH_STATS: no page with \"last\":1 ({len(raw)} page(s))")
    else:
        pages = _json_since(w1, wm, lambda o: o.get("type") == "MESH_STATS")
        merged = merge_mesh_stats(pages)
        notes.append(f"MESH_STATS: {len(pages)} page(s), largest {max(len(x.encode()) for x in raw)} bytes")
        if any(len(x.encode()) > PACKET_MAX for x in raw):
            problems.append(f"a MESH_STATS page is over {PACKET_MAX} bytes")
        if set(merged["peers"]) != set(after["peers"]):
            problems.append(f"MESH_STATS rows {sorted(merged['peers'])} bridged, {sorted(after['peers'])} over USB")
        for b, row in merged["peers"].items():
            u = after["peers"].get(b)
            if u and any(x > y for x, y in zip(row[1:], u[1:])):
                problems.append(f"WCB{b}: bridged counters {row[1:]} above the USB read after them {u[1:]}")
    nm = nc.dev.mark()
    r = bridged(w1, {"type": "RESET_MESH_STATS"}, None, timeout=1.5, target=nid)
    replies = [o for o in r.sys if not str(o.get("type", "")).startswith("rc_")]
    try:
        _nav_regex(nc, nm, r"^\[WCB\] mesh stats cleared$", 4, "'mesh stats cleared' line")
    except AssertionError as e:
        problems.append(str(e))
    agg = nc.mesh_stats()["agg"]
    if replies:
        problems.append(f"the bridged RESET_MESH_STATS was answered: {replies}")
    if agg.get("sent", 0) > 5 or agg.get("recv", 0) > 5:
        problems.append(f"after the bridged RESET_MESH_STATS the counters read {agg}")
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("ncmesh.bridged_wcb_send_findings", "(should) A bridged WCB_SEND reports what the library did and survives "
      "fragmenting: to a board NaviCore cannot send to it answers ok:false (as USB does), and one too long for a packet "
      "runs and is ACKed", needs=["navicore", "wcb1"], links=["W1S2"])
def bridged_wcb_send_findings(bench):
    """NAVICORE.md D-NC27. tick() performs a parked WCB_SEND and sets ok = true whatever send() or broadcast() returned
    (rc_telemetry.h:1516-1531, ok = true at :1523); the USB handler reports the library's answer
    (NaviCore.ino:4063-4084, the #7 fix). And a WCB_SEND too long for one packet - the tool fragments anything over 187
    bytes - reaches _applyReassembled, which knows SET_CONFIG, SET_CMDLIB and TEST_ACTION only: 'reassembled payload had
    unexpected type 'WCB_SEND' — dropping' (rc_telemetry.h:1236), no ACK, nothing sent. The unreachable board is one
    NaviCore has no ESP-NOW peer for (not in the floor, not learned, not the special peer): esp_now_send refuses it, and
    the USB WCB_SEND to it must say ok:false first, or the test skips. What it carries is '?HILNOOP', which goes
    nowhere. The fragmented one carries a ';S2<marker>' for W1 S2 (the W1S2 probe) padded to 150 characters, inside the
    160-byte buffer the one-packet path parks (rc_telemetry.h:292). Recommendation: report the send result; handle and
    ACK the fragmented form."""
    l12 = link(bench, 1, "S2")
    nc, w1 = _nc(bench), usb_wcb(bench)
    ghost = _free_id(nc, (17, 18, 16, 15, 14, 13, 12, 11))
    usb = nc.wcb_send(ghost, "?HILNOOP")
    if usb.get("ok") is not False:
        raise Skip(f"a USB WCB_SEND to WCB{ghost} answered {usb}: NaviCore has a peer there, so no send can fail")
    nid = _open_relay(nc, w1)
    r = bridged(w1, {"type": "WCB_SEND", "target": ghost, "cmd": "?HILNOOP"}, r'"of":"WCB_SEND"', timeout=5.0,
                target=nid)
    ack1 = next((o for o in r.sys if o.get("of") == "WCB_SEND"), None)
    me = bench.usb_wcb_number()
    t = marker("WS")
    cmd = (f";S2{t}" + "Z" * 150)[:150]
    envs = fragments({"sys": 1, "type": "WCB_SEND", "target": me, "cmd": cmd}, _fsid())
    if len(envs) < 2:
        raise AssertionError("the long WCB_SEND fits one envelope: nothing to fragment")
    pm, nm = l12.mark(), nc.dev.mark()
    wm = send_fragments(w1, envs, target=nid)
    got = _wait_all(l12, pm, [cmd[3:]], 4.0)
    ack2 = _await_json(w1, wm, lambda o: o.get("of") == "WCB_SEND", 1.0)
    dropped = any("reassembled payload had unexpected type 'WCB_SEND'" in x for x in _flushed(nc, nm, settle=0.2))
    problems = []
    if not ack1 or ack1.get("ok") is not False:
        problems.append(f"a bridged WCB_SEND to WCB{ghost}, which the library refuses (USB says ok:false), was ACKed "
                        f"{ack1} (rc_telemetry.h:1523)")
    n = _count(got, cmd[3:])
    if n != 1 or not ack2 or not ack2.get("ok"):
        problems.append(f"a {len(envs)}-fragment WCB_SEND reached W1 S2 {n} time(s) with ACK {ack2}"
                        + (" - dropped as an unexpected type (rc_telemetry.h:1236)" if dropped else ""))
    assert not problems, "(should, D-NC27) " + "; ".join(problems)


@test("ncmesh.bridged_usb_only_types", "START_MONITOR, SET_DEBUG_FLAGS, CALIB and STOP_MONITOR bridged from W1 are "
      "accepted and do nothing: no reply, no 'Unknown inbound type' line, no PWM_UPDATE frame, no debug flag set, no "
      "calibration mute", needs=["navicore", "wcb1"], links=[])
def bridged_usb_only_types(bench):
    """rc_telemetry.h:2391-2402 accepts the four with no effect: over the mesh their live state streams as rc_hb / rc_ch
    instead, and the config tool's connect sequence sends them whatever the transport. Their USB twins act
    (NaviCore.ino:3983-4000, :4107-4112). Seen on NaviCore's own USB: no PWM_UPDATE after the bridged START_MONITOR, no
    '[DBG] flags=' or '[CALIB]' line, and no [DISPATCH] trace for a USB TEST_ACTION afterwards (the flags stayed 0; the
    action is a ?-query to W1). The debug flags, the monitor and CALIB are set back over USB however it ends."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    nc.set_debug_flags(0)
    nid = _open_relay(nc, w1)
    problems = []
    try:
        for obj in ({"type": "START_MONITOR"}, {"type": "SET_DEBUG_FLAGS", "flags": 127}, {"type": "CALIB", "on": True},
                    {"type": "STOP_MONITOR"}):
            nm = nc.dev.mark()
            r = bridged(w1, obj, None, timeout=1.5, target=nid)
            lines = _flushed(nc, nm, settle=0.3)
            replies = [o for o in r.sys if not str(o.get("type", "")).startswith("rc_")]
            if replies:
                problems.append(f"{obj['type']}: answered {replies}")
            if any("Unknown inbound type" in x for x in lines):
                problems.append(f"{obj['type']}: 'Unknown inbound type'")
            if any(x.startswith('{"type":"PWM_UPDATE"') for x in lines):
                problems.append(f"{obj['type']}: PWM_UPDATE frames on NaviCore's USB")
            if any(x.startswith(("[DBG] flags=", "[CALIB]")) for x in lines):
                problems.append(f"{obj['type']}: acted as the USB command does")
        nm = nc.dev.mark()
        nc.test_action({"type": "wcb_unicast", "target": str(bench.usb_wcb_number()), "cmd": "?HILNOOP"})
        if any(x.startswith("[DISPATCH]") for x in _flushed(nc, nm, settle=0.3)):
            problems.append("the bridged SET_DEBUG_FLAGS turned NaviCore's dispatch trace on")
    finally:
        nc.ack({"type": "STOP_MONITOR"})
        nc.calib(False)
        nc.set_debug_flags(0)
    assert not problems, "; ".join(problems)


# ============================================================ the management relay and the remote terminal
FILL = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 12


@test("ncmesh.mgmt_stats_frag", "NaviCore's management relay: ?MGMT,STATS,2 answers [MGMT:STATS,2] carrying W2's own "
      "per-board ETM rows (asked once more when the reply is lost on the air, tracker #109); a one-chunk ?MGMT,FRAG "
      "?SEQ,SAVE reaches W2 as an ordinary command and is stored; an over-long ?MGMT line is refused with '[mgmt] line "
      "too long' and sends nothing", needs=["navicore", "wcb1", "wcb2"], links=[])
def mgmt_stats_frag(bench):
    """WcbMgmt::handleLine (WCB_Client WCB_Mgmt.h:367-403). STATS sends one PT_STATS_REQ (7) raw packet (:398); W2
    answers with buildStatsString (WCB.ino:4653-4662, :2105-2242) as type-9 fragments, once, in broadcast frames with no
    second pass (sendResultFrags, WCB.ino:4611-4650; tracker #109), which service() prints in ONE printf as
    '[MGMT:STATS,2]<text>' (WCB_Mgmt.h:496-511) - the text keeps its newlines, so its rows follow on lines of their own.
    A reply that never came is asked for once more, with ?DEBUG,MGMT on W2 (RAM only) so the note says which leg was
    lost (_reply_leg). A one-chunk FRAG goes to W2 as an ordinary command ('[relay] MGMT -> WCB2 (1/1): <payload>',
    WCB_Mgmt.h:336-342); the multi-chunk form is ncmesh.mgmt_frag_multichunk (D-NC48). A line whose text after '?MGMT,'
    is 400 bytes or more is refused before anything is parsed (WCB_Mgmt.h:389-393). All typed on NaviCore's USB. The
    push runs in config_guard(bench, 2), and the test clears its sequence in a finally (the guard fails a test that
    leaves one, run 20260929-025701)."""
    nc, w2 = _nc(bench), _w2(bench)
    nid = _nid(nc)
    problems, notes = [], []
    block = None
    w2.debug("MGMT", True)
    try:
        for ask in (1, 2):
            nm, m2 = nc.dev.mark(), w2.dev.mark()
            nc.dev.send("?MGMT,STATS,2")
            try:
                _nav_regex(nc, nm, r"^\[MGMT:STATS,2\]", 6, "[MGMT:STATS,2] reply")
            except AssertionError:
                notes.append(f"ask {ask}: no [MGMT:STATS,2] reply ({_reply_leg(w2, m2, 2, nid, 9)})")
                continue
            time.sleep(1.0)
            block = nc.dev.since(nm)
            break
    finally:
        w2.debug("MGMT", False)
    if block is None:
        raise AssertionError("NaviCore printed no [MGMT:STATS,2] reply to two asks: " + "; ".join(notes))
    i = next(k for k, x in enumerate(block) if x.startswith("[MGMT:STATS,2]"))
    relayed, own = ncmesh.stats_rows(block[i:]), ncmesh.stats_rows(w2.run("?STATS", timeout=8))
    if not relayed or set(relayed) != set(own):
        problems.append(f"[MGMT:STATS,2] lists boards {sorted(relayed)}; W2's own ?STATS lists {sorted(own)}")
    key1 = f"HILF{nonce()[:4]}"
    val1 = f";S3{marker('F')}"
    with config_guard(bench, 2):
        nm = nc.dev.mark()
        try:
            nc.dev.send(f"?MGMT,FRAG,2,{_frag_sid()},0,1,?SEQ,SAVE,{key1},{val1}")
            try:
                _nav_regex(nc, nm, rf"^\[relay\] MGMT -> WCB2 \(1/1\): \?SEQ,SAVE,{key1},", 4, "one-chunk relay line")
            except AssertionError as e:
                problems.append(str(e))
            time.sleep(1.0)
            if _seqval(w2, key1) != f"[MGMT:SEQVAL,2]{key1},OK,{val1}":
                problems.append(f"the one-chunk push: W2 reads {key1} as {_seqval(w2, key1)!r}")
        finally:
            _seq_clear(w2, key1)
    nm = nc.dev.mark()
    nc.dev.send("?MGMT,HIL" + "x" * 420)
    try:
        _nav_regex(nc, nm, r"^\[mgmt\] line too long \(423 B\) - dropped$", 4, "'[mgmt] line too long (423 B)' line")
    except AssertionError as e:
        problems.append(str(e))
    if any(x.startswith("[relay]") for x in nc.dev.since(nm)):
        problems.append("the over-long line was relayed")
    if notes:
        bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("ncmesh.mgmt_frag_multichunk", "(should) A three-chunk ?MGMT,FRAG push through NaviCore reaches W2 and is stored "
      "whole, as through a WCB relay: NaviCore sends each chunk in the 226-byte MGMT frame a WCB takes",
      needs=["navicore", "wcb2"], links=[])
def mgmt_frag_multichunk(bench):
    """NAVICORE.md D-NC48 (found by the second bench run, 20260929-042105). WcbMgmt forwards each chunk of a multi-chunk
    FRAG as a raw type-3 packet built in its 230-byte config_frag struct (WCB_Client WCB_Mgmt.h:343-360, the struct at
    :97-106: sourceWCB, requesterWCB, payload[183]). A WCB takes a MGMT frag only as its 226-byte espnow_struct_mgmt
    (targetWCB, payload[180]; WCB.ino:1077-1086), matched by size (:5181-5183); a 230-byte frame goes to the config-frag
    branch, which knows no type 3 and drops it without a word (:5202-5213). So no chunk of a multi-chunk push through
    NaviCore reaches W2, while NaviCore prints '[relay] MGMT -> WCB2 frag i/n (session XXXX)' for each: both bench runs
    left the key NOTFOUND on W2 after two sessions. WCB_Client's own MgmtRelay example builds the right frame
    (wcb_packet_mgmt_t, WCB_Client.h:197-205; MgmtRelay.ino:859-869), and so does a WCB relay. The push is a
    395-character ?SEQ,SAVE in the Wizard's 179-character chunks 0.25 s apart (s05 FRAG_CHUNK, FRAG_GAP_S), read back on
    W2's console; a push W2 did not store goes once more under a new session. Inside config_guard(bench, 2), and the
    test clears the sequence in a finally. Recommendation: build the frame from wcb_packet_mgmt_t in
    WcbMgmt::handleMgmtFrag, as MgmtRelay.ino does."""
    nc, w2 = _nc(bench), _w2(bench)
    key = f"HILG{nonce()[:4]}"
    val = f";S3{marker('G')}{FILL}"[:395]
    payload = f"?SEQ,SAVE,{key},{val}"
    parts = [payload[k:k + FRAG_CHUNK] for k in range(0, len(payload), FRAG_CHUNK)]
    notes, stored = [], False
    with config_guard(bench, 2):
        try:
            for ask in (1, 2):
                sid = _frag_sid()
                nm = nc.dev.mark()
                for k, part in enumerate(parts):
                    if k:
                        time.sleep(FRAG_GAP_S)
                    nc.dev.send(f"?MGMT,FRAG,2,{sid},{k},{len(parts)},{part}")
                time.sleep(1.5)
                relay = [x.rstrip() for x in nc.dev.since(nm) if x.startswith("[relay] MGMT -> WCB2 frag ")]
                want = [f"[relay] MGMT -> WCB2 frag {k + 1}/{len(parts)} (session {sid.upper()})"
                        for k in range(len(parts))]
                if relay != want:
                    raise AssertionError(f"NaviCore printed the relay lines {relay}, expected {want}: nothing to judge")
                stored = _seqval(w2, key) == f"[MGMT:SEQVAL,2]{key},OK,{val}"
                notes.append(f"push {ask} (session {sid}, {len(parts)} chunks): W2 stored it {stored}")
                if stored:
                    break
        finally:
            _seq_clear(w2, key)
    bench.note("; ".join(notes))
    assert stored, (f"(should, D-NC48) a {len(parts)}-chunk push through NaviCore never reached W2 ({key} NOTFOUND "
                    f"after two sessions): WCB_Mgmt.h:343-360 sends each chunk as a 230-byte config_frag, and a WCB "
                    f"takes a MGMT frag only as its 226-byte espnow_struct_mgmt (WCB.ino:1077-1086)")


@test("ncmesh.mgmt_etm_char", "?MGMT,ETM,CHAR,2 on NaviCore asks W2 for its ETM characterization with one request (W2 "
      "logs one from WCB20 per ask), and the answer comes back as one [MGMT:ETM,2] reply whose text is the result block "
      "W2 printed; a reply W2 sent that never arrived is asked for once more (slow: about 10 s an ask)",
      needs=["navicore", "wcb2"], links=[])
def mgmt_etm_char(bench):
    """WcbMgmt sends ONE PT_ETM_REQ (8) raw packet for ETM,CHAR (WCB_Mgmt.h:399; a PULL goes three times, :265-267). W2
    runs its characterization for NaviCore (handleETMReqPacket, WCB.ino:4766-4785; a request while a run is going is
    ignored, :4773-4776, and one that cannot run is answered at once with the reason, which is a reply too) and at the
    end sends the result with sendResultFrags: type-10 frags, ONCE, as broadcast frames 20 ms apart with no retry
    (WCB.ino:4611-4650, called at :2358-2362). They leave while the 10 s load the run started on its peers is still on
    the air (the generator, WCB.ino:2044-2053; W2's own phases take about 4 s), and one lost frag loses the reply: in run
    20260929-025701 W2 printed '[MGMT] Sent result frags (3 chunks, type 10) to WCB20' and NaviCore printed nothing for
    150 s. A config reply goes in two passes for this (WCB.ino:4552-4556); STATS, ETM and sequence replies do not. So
    the test waits for W2's own send line (?DEBUG,MGMT, RAM only, on meanwhile), then ETM_REPLY_WAIT_S for NaviCore's
    '[MGMT:ETM,2]<text>' (WCB_Mgmt.h:496-511), and asks once more when W2 sent and NaviCore printed nothing (noted).
    The text starts with a newline (buildETMCharResultsString, WCB.ino:2298-2344), so the tag line is bare and the
    block follows on lines of its own, as a STATS reply's rows do: it must equal the block W2 printed on its own console
    for the same run, header to closing rule (in run 20260929-042105 it did, and the tag line alone read as an empty
    reply). A run that could not start answers with its reason on the tag line (noted). Skips while NaviCore writes
    plain mesh text out an aux port: the run's traffic reaches NaviCore too."""
    nc, w2 = _nc(bench), _w2(bench)
    _no_plain_fanout(nc)
    nid = _nid(nc)
    sent_rx = rf"^\[MGMT\] Sent result frags \((\d+) chunks, type 10\) to WCB{nid}\b"
    asks, notes = [], []
    w2.debug("MGMT", True)
    try:
        for ask in (1, 2):
            nm, m2 = nc.dev.mark(), w2.dev.mark()
            nc.dev.send("?MGMT,ETM,CHAR,2")
            try:
                frags = int(w2.dev.expect(sent_rx, timeout=ETM_RUN_MAX_S, since=m2).group(1))
            except AssertionError:
                raise AssertionError(f"ask {ask}: W2 never sent an ETM reply to WCB{nid} within {ETM_RUN_MAX_S:g} s of "
                                     f"the request") from None
            try:
                _nav_regex(nc, nm, r"^\[MGMT:ETM,2\]", ETM_REPLY_WAIT_S, "[MGMT:ETM,2] reply")
            except AssertionError:
                pass
            time.sleep(1.0)
            got = [x.rstrip() for x in nc.dev.since(nm)]
            own = [x.rstrip() for x in w2.dev.since(m2)]
            replies = [k for k, x in enumerate(got) if x.startswith("[MGMT:ETM,2]")]
            reqs = [x for x in own if x.startswith(f"[MGMT] ETM char request from WCB{nid}")]
            relayed = ([got[replies[0]][len("[MGMT:ETM,2]"):]] if replies and got[replies[0]] != "[MGMT:ETM,2]"
                       else _etm_block(got[replies[0] + 1:]) if replies else [])
            asks.append((len(reqs), frags, len(replies), relayed, _etm_block(own)))
            if replies:
                break
            notes.append(f"ask {ask}: W2 sent its reply in {frags} broadcast frag(s) and NaviCore printed none within "
                         f"{ETM_REPLY_WAIT_S + 1:g} s (a frag lost under the run's own load)")
    finally:
        w2.debug("MGMT", False)
    _, frags, replies, relayed, own = asks[-1]
    could_not = bool(relayed) and "could not run" in relayed[0]
    notes.append(f"{len(asks)} ask(s); the last: {replies} [MGMT:ETM,2] reply(ies) from {frags} frag(s), "
                 f"{len(relayed)} line(s) of text" + (f" - the run could not start: {relayed[0][:100]!r}"
                                                      if could_not else ""))
    bench.note("; ".join(notes))
    problems = [f"ask {k + 1}: W2 logged {n} ETM char requests from WCB{nid}; the relay sends one"
                for k, (n, *_) in enumerate(asks) if n != 1]
    if replies != 1:
        problems.append(f"W2 sent its reply {len(asks)} time(s) and NaviCore printed {replies} [MGMT:ETM,2] line(s) "
                        f"for the last")
    elif not relayed:
        problems.append("the [MGMT:ETM,2] reply carries no text: neither on its tag line nor a result block after it")
    elif not could_not and relayed != own:
        first = next((k for k, (a, b) in enumerate(zip(relayed, own)) if a != b), min(len(relayed), len(own)))
        problems.append(f"the relayed block ({len(relayed)} lines) differs from the {len(own)} W2 printed for the same "
                        f"run, first at line {first}")
    assert not problems, "; ".join(problems)


@test("ncmesh.mgmt_rterm_relay", "A remote terminal on W2 through NaviCore: ?MGMT,FRAG carries ?RTERM,START,20 to W2, whose "
      "console then comes back on NaviCore's USB as [TERM:2] lines ('[RTERM] Session started', W2's own version for a "
      "relayed ?VERSION) until ?RTERM,STOP ends the session", needs=["navicore", "wcb1", "wcb2"], links=[])
def mgmt_rterm_relay(bench):
    """A one-chunk ?MGMT,FRAG reaches W2 as an ordinary command (WCB_Mgmt.h:336-342), so '?RTERM,START,20' makes W2
    mirror its console to WCB20 in 160-byte type-7 packets (WCB_RemoteTerm.cpp:98-127); NaviCore's raw hook hands them
    to WcbMgmt (navicore_ota.h:606-618) and service() prints '[TERM:2]<line>' (processRemoteTerm, WCB_Mgmt.h:516-532).
    W2 reaches WCB20 through its controller peer, so W2 must carry ?CONTROLLER,ON,20. '[RTERM] Session stopped' prints
    on W2's own console only, after the relay is dropped (stopSession, WCB_RemoteTerm.cpp:152-158), and from then on
    nothing W2 prints reaches NaviCore. STOP goes in a finally."""
    nc, w2 = _nc(bench), _w2(bench)
    nid = _nid(nc)
    require_tokens(bench, 2, f"?CONTROLLER,ON,{nid}")
    ver = w2.version()
    problems = []
    nm = nc.dev.mark()
    nc.dev.send(f"?MGMT,FRAG,2,{_frag_sid()},0,1,?RTERM,START,{nid}")
    try:
        _nav_regex(nc, nm, r"^\[TERM:2\]\[RTERM\] Session started", 6, "[TERM:2][RTERM] Session started")
        vm = nc.dev.mark()
        nc.dev.send(f"?MGMT,FRAG,2,{_frag_sid()},0,1,?VERSION")
        got = _nav_regex(nc, vm, r"^\[TERM:2\]Software Version: (\S+)", 6, "[TERM:2]Software Version line")
        if got.group(1) != ver:
            problems.append(f"the relayed ?VERSION says {got.group(1)}, W2's own console {ver}")
    finally:
        sm = w2.dev.mark()
        nc.dev.send(f"?MGMT,FRAG,2,{_frag_sid()},0,1,?RTERM,STOP")
        try:
            w2.dev.expect(r"^\[RTERM\] Session stopped", timeout=5, since=sm)
            stopped = True
        except AssertionError:
            stopped = False
    am = nc.dev.mark()
    w2.version()
    time.sleep(1.5)
    leaked = [x for x in nc.dev.since(am) if x.startswith("[TERM:2]")]
    if not stopped:
        problems.append("W2 printed no '[RTERM] Session stopped' after the relayed ?RTERM,STOP")
    if leaked:
        problems.append(f"after the STOP, W2's console still reached NaviCore ({len(leaked)} [TERM:2] lines)")
    assert not problems, "; ".join(problems)


@test("ncmesh.raw_hook_interleave", "A config pull of W2 through NaviCore's relay and OTA relay traffic to W2 at the same "
      "time both complete: the [MGMT:CONFIG,2] line equals W2's own chain and every relayed ?OTA line gets its "
      "[OTA:ACK,2,...] (no BEGIN: nothing is erased)", needs=["navicore", "wcb2"], links=[])
def raw_hook_interleave(bench):
    """NaviCore has one raw-packet hook (WCB_Client's onRawPacket takes one callback): otaRawPacketHook keeps the 55- and
    243-byte OTA structs and offers every other size to WcbMgmt::onRawPacket (navicore_ota.h:597-619), whose queue
    service() drains in loop() (WCB_Mgmt.h:409-419, :538-545). A ?MGMT,PULL,2 brings W2's config back as 230-byte
    type-6 frags; meanwhile ?OTA,DATA to W2 with no session and ?OTA,ABORT bring back 55-byte ACKs, [OTA:ACK,2,<s>,0,1]
    and [OTA:ACK,2,<s>,0,0] (s47 ncota.navicore_as_relay_nonerasing pins each alone; no BEGIN goes, so nothing is
    erased). The pull must equal W2's own factory chain (s03 _factory_reply, as navicore.mgmt_pull checks it), and every
    OTA line must get its ACK. The reply carries the mesh password: it is compared, never printed, and NaviCore's
    console is redacted in session.log."""
    nc, w2 = _nc(bench), _w2(bench)
    want = _factory_reply(w2, w2.version())
    if len(want) > PULL_MAX:
        raise Skip(f"W2's pull reply is {len(want)} characters: this test needs a one-line config (<= {PULL_MAX})")
    s = _session_id()
    ack = re.compile(rf"^\[OTA:ACK,2,{s},(\d+),(\d+)\]$")
    lines = {"DATA": f"?OTA,DATA,2,{s},0:{_crc('0,AAAA')},AAAA", "ABORT": f"?OTA,ABORT,2,{s}"}
    om = nc.dev.mark()
    pull = Pull(nc.dev, 2, parts=False, timeout=12.0)
    order = ["DATA", "ABORT"] * 3
    for what in order:
        nc.dev.send(lines[what])
        time.sleep(0.12)
    while not pull.poll():
        time.sleep(0.05)
    time.sleep(1.5)
    nc.dev.send("#L12")
    time.sleep(0.3)
    reply = pull.reply(verify=False)
    acks = sorted((int(m.group(1)), int(m.group(2))) for x in nc.dev.since(om) for m in [ack.match(x.rstrip())] if m)
    problems = [f"the pull: {p}" for p in _reply_problems(reply, want, [], "legacy", 1)]
    if acks != sorted([(0, 1)] * 3 + [(0, 0)] * 3):
        problems.append(f"OTA ACKs {acks} (offset, status); expected (0, 1) for each of the 3 DATA and (0, 0) for each "
                        f"of the 3 ABORT")
    bench.note(f"a {len(want)}-character pull of W2 with 6 relayed OTA lines in flight; {len(acks)} ACKs")
    assert not problems, "; ".join(problems)


@test("ncmesh.remote_cli_order_and_drop", "The relayed terminal: ;W20,?WDP,DUMP comes back on W1 as [TERM:20] lines equal, "
      "in order, to NaviCore's own; an unknown command over the mesh answers 'Unknown command:', and that reply, over 160 "
      "bytes, arrives cut at 160 with nothing lost; on a NAVICORE_HIL_HOOKS image, of 5 lines that arrive during a "
      "stall exactly the first 3 run, in order (that part stalls loop() 3 s)", needs=["navicore", "wcb1"], links=[])
def remote_cli_order_and_drop(bench):
    """onWCBCommand queues a '?' or '#' line for drainRemoteCli without waiting (NaviCore.ino:3025-3060, :2925-2930; a
    3-deep queue, :4843), which runs one per loop() pass with Serial teed to the RTERM sink (:5010-5024; rc_serial.h's
    capture takes only the arming core's output, so a Core-0 line never enters it); the sink sends a packet per line and
    hard-wraps at 160 bytes (navicore_rterm.h:48-62), and W1 prints each as [TERM:20]<text>, dropping empty ones
    (WCB_RemoteTerm.cpp:178-207) - ncmesh.rterm_pieces predicts the result from what NaviCore printed on its own USB. An
    unrecognised line prints 'Unknown command: <line>' (NaviCore.ino:5019-5020). The queue-depth part needs INF9's #L90
    stall, and first makes the SBUS side of the engine inert inside nc_guard (s41 _engine_inert: a stall past ~100 ms
    can overflow the SBUS UART); it skips on another image, and when W1 took too long to send the five. The dropped
    lines are ACKed first (WCB_Client.cpp:2825-2845), so nothing retries them. The plan's '?backup over RTERM' is not
    sent: it would print ?EPASS on W1's console, which session.log keeps unredacted (nccfg.backup_block covers ?backup
    on USB)."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    nid = _nid(nc)
    problems, notes = [], []
    nm, wm = nc.dev.mark(), w1.dev.mark()
    w1.send(f";W{nid},?WDP,DUMP")
    try:
        w1.dev.expect(rf"^\[TERM:{nid}\]\[WDP:END,count=\d+\]", timeout=8, since=wm)
    except AssertionError:
        problems.append(f"no [TERM:{nid}][WDP:END,...] line on W1")
    _flushed(nc, nm, settle=0.5)
    own = [x.rstrip("\r\n") for x in nc.dev.since(nm) if x.startswith("[WDP")]
    want, got = ncmesh.rterm_pieces(own), _term_lines(w1, wm, nid)
    notes.append(f"?WDP,DUMP: {len(own)} lines, {len(want)} pieces expected, {len(got)} came, longest line "
                 f"{max((len(x.encode()) for x in own), default=0)} bytes")
    if got != want:
        problems.append(f"?WDP,DUMP over RTERM: {len(got)} [TERM:{nid}] lines differ from the {len(want)} NaviCore's own "
                        f"console predicts (first difference at "
                        f"{next((k for k, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))})")
    text = (f"?HILU{nonce()}" + FILL)[:150]
    nm, wm = nc.dev.mark(), w1.dev.mark()
    w1.send(f";W{nid},{text}")
    time.sleep(2.0)
    line = f"Unknown command: {text}"
    if line not in _flushed(nc, nm, settle=0.2):
        problems.append("NaviCore did not print 'Unknown command: <the line>' for an unknown relayed command")
    got = _term_lines(w1, wm, nid)
    if got != ncmesh.rterm_pieces([line]):
        problems.append(f"the {len(line)}-byte 'Unknown command:' reply came back as pieces of "
                        f"{[len(x) for x in got]} bytes, expected {[len(x) for x in ncmesh.rterm_pieces([line])]}")
    lost = _relay_lost(w1, wm)
    if lost:
        problems.append(f"W1 dropped relayed lines: {lost}")
    if not _hooks(nc):
        notes.append(f"not a NAVICORE_HIL_HOOKS image: the queue-depth part did not run ({HOOK_SKIP})")
    else:
        with nc_guard(bench) as g:
            off = _engine_inert(g.nc, g.before)
            time.sleep(1.0)
            tag = nonce()
            nm = nc.dev.mark()
            t0 = time.monotonic()
            nc.dev.send(f"#L90,{STALL_MS}")
            time.sleep(0.15)
            for k in range(1, 6):
                w1.send(f";W{nid},?HILQ{tag}{k}")
            sent = time.monotonic() - t0
            nc.dev.expect(r"\[HIL\] #L90: loop\(\) resumed after \d+ ms", timeout=STALL_MS / 1000 + 5, since=nm)
            time.sleep(1.5)
            lines = _flushed(nc, nm, settle=0.2)
            ran = [k for k in range(1, 6) if f"Unknown command: ?HILQ{tag}{k}" in lines]
            notes.append(f"5 lines sent {sent:.2f} s into a {STALL_MS} ms stall (knobs off meanwhile: {off}); ran {ran}")
            if sent > STALL_MS / 1000 - 1.0:
                notes.append("W1 took too long to send the five: the queue-depth result is not judged")
            elif ran != list(range(1, CLI_QUEUE + 1)):
                problems.append(f"of 5 relayed lines that arrived during the stall, {ran} ran; the {CLI_QUEUE}-deep queue "
                                f"keeps the first {CLI_QUEUE}")
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


# ============================================================ WDP
def _wcb_view(w):
    """A WCB's ?WDP,DUMP as hil.navicore.parse_wdp reads NaviCore's: {'rows', 'ifaces', 'cfg', 'count'}."""
    return parse_wdp([x.rstrip() for x in w.run("?WDP,DUMP", timeout=8)])


def _row_of(view, n):
    return next((r for r in view["rows"] if r.get("N") == str(n)), None)


def _ifaces_of(view, n):
    return {int(i["S"]): i.get("DEV", "") for i in view["ifaces"] if i.get("N") == str(n) and str(i.get("S", "")).isdigit()}


def _await_row(w, n, tries=2):
    """W's WDP row for board n, with a ?WDP,POLL and a short wait between tries (a solicit brings every advert within
    about a second: NaviCore arms one on the solicit, WCB_Client.cpp:2062-2081) -> (row or None, view)."""
    view = _wcb_view(w)
    for _ in range(tries):
        row = _row_of(view, n)
        if row is not None:
            return row, view
        w.run("?WDP,POLL")
        time.sleep(2.0)
        view = _wcb_view(w)
    return _row_of(view, n), view


def _identity_want(cfg, fw):
    """The fields a WCB's WDP row for NaviCore must carry, from GET_CONFIG and the PONG version: setIdentity("NaviCore",
    FW_VERSION, board profile, "rc sbus maestro hcr") (NaviCore.ino:4905-4907) decoded by the WCB (WCB_WDP.cpp:568-600,
    DEVTYPE doubling as the alias of a client, no HWVER TLV so HW=0, FW kept to 27 characters) and printed
    (:1823-1826). MAESTRO lists the local Maestro devices (ncmesh.local_maestro_ids). PEER is a membership fact of
    the reading board, not part of the advert: _peer_want."""
    return {"CLIENT": "1", "ALIAS": "NaviCore", "HW": "0",
            "HWREV": "NaviCore v2" if cfg.get("boardType", 0) == 0 else "WCB 3.2", "FW": fw[:27], "CAP": "0000",
            "CTRL": "0", "CAPTAGS": "rc sbus maestro hcr",
            "MAESTRO": ".".join(str(d) for d in ncmesh.local_maestro_ids(cfg)) or "-", "SEEN": "1"}


def _peer_want(bench, n, nid, view):
    """The PEER W<n>'s row for NaviCore carries (WCB_WDP.cpp:1817-1822): 0 while NaviCore is W<n>'s controller
    (?CONTROLLER,ON,<nid> in its saved config: the special peer comes in through the controller path and auto-join
    skips it, :671-678), else 2 once auto-join has learned it from two adverts (AUTOJOIN=1 in W<n>'s [WDPCFG]), else
    0. WCB20 is outside the floor on this bench, so never 1."""
    toks = {t.upper() for t in bench.config_tokens(n, refresh=True)}
    if f"?CONTROLLER,ON,{nid}" in toks and "?CONTROLLER,OFF" not in toks:
        return "0"
    return "2" if view["cfg"].get("AUTOJOIN") == "1" else "0"


@test("ncmesh.wdp_identity_fields", "W1's WDP row for NaviCore is there and carries its advert: CLIENT 1, ALIAS NaviCore, "
      "FW = the PONG version, HWREV for its board profile, CAPTAGS 'rc sbus maestro hcr', CAP 0000, CTRL 0, "
      "MAESTRO = its local Maestro devices, PEER 0 where NaviCore is the controller (2 where W1 learned it); W2's row "
      "agrees; NaviCore's own SELF row carries PEER 3, HW 32 and the same version", needs=["navicore", "wcb1"],
      links=[])
def wdp_identity_fields(bench):
    """The plan's row: the N=20 row is REQUIRED here - s18's wdp.dump_fields checks it only when present (s18:1207), so
    on a bench where NaviCore's advert never reached W1 it passes vacuously. A missing row gets one ?WDP,POLL (NaviCore
    answers a solicit within about 600 ms, WCB_Client.cpp:2062-2081) and then fails. NaviCore's own SELF row comes from
    WcbMgmt's printWdpDump (WCB_Mgmt.h:225-228): CLIENT 0, ALIAS NaviCore, HW 32 ('not a real WCB'), CAP 0000, PEER 3.
    W2's row is read on W2's own console when it has one. Read only."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    cfg, fw, nid = nc.config(), nc.ping(), _nid(nc)
    want = _identity_want(cfg, fw)
    problems = []
    boards = [(bench.usb_wcb_number(), w1)] + ([(2, _w2(bench))] if bench.usb_wcbs().get(2) else [])
    for n, w in boards:
        name = f"W{n}"
        row, view = _await_row(w, nid)
        if row is None:
            problems.append(f"{name}'s ?WDP,DUMP has no row for WCB{nid}, even after a ?WDP,POLL")
            continue
        wn = dict(want, PEER=_peer_want(bench, n, nid, view))
        bad = {k: row.get(k) for k, v in wn.items() if row.get(k) != v}
        if bad:
            problems.append(f"{name}'s row for WCB{nid}: {bad}, expected {({k: wn[k] for k in bad})}")
        if int(row.get("AGE") or 999) > 75:
            problems.append(f"{name}'s row for WCB{nid} is {row.get('AGE')} s old; NaviCore advertises every 60 s")
    me = nc.self_row() or {}
    for k, v in (("N", str(nid)), ("CLIENT", "0"), ("ALIAS", "NaviCore"), ("HW", "32"), ("FW", fw), ("CAP", "0000"),
                 ("PEER", "3")):
        if me.get(k) != v:
            problems.append(f"NaviCore's SELF row {k}={me.get(k)}, expected {v}")
    bench.note(f"NaviCore {fw}, rows read on {[f'W{n}' for n, _ in boards]}")
    assert not problems, "; ".join(problems)


def _await_iface(w1, nid, port, want, timeout):
    """W1's [WDPIF:N=<nid>,S=<port>] label once it equals `want` (None: no such row), polled -> (label, seconds), with
    the label as last read after `timeout`."""
    t0 = time.monotonic()
    while True:
        got = _ifaces_of(_wcb_view(w1), nid).get(port)
        if got == want or time.monotonic() - t0 >= timeout:
            return got, time.monotonic() - t0
        time.sleep(0.3)


@test("ncmesh.wdp_port_labels", "NaviCore's [WDPIF] rows on W1 are the labels its config derives (a device's name on the "
      "aux port it is routed to, 'Maestro' on port 4, a user label first); a label saved over USB, and another saved over "
      "the bridge, each reach W1 within 3 s, and the restore takes them back", needs=["navicore", "wcb1"], links=[])
def wdp_port_labels(bench):
    """rcAdvertiseSerialLabels (NaviCore.ino:4453-4457) runs after every save on both transports (USB SET_CONFIG through
    applyConfigSideEffects' caller; the bridge at rc_telemetry.h:1183) and hands each port's effective label to
    setPortLabel, which re-advertises twice at once on a change (WCB_Client.cpp:1930-1941, 200 ms then 1.3 s later,
    :2021-2033) - so a WCB shows it in about a second instead of at the next 60 s advert. ncmesh.port_labels derives
    what each port must read (rc_config.h:1957-2000). Inside nc_guard: a user label on an aux port that has none (S5,
    else S4, else S3; WDP port 3, 2 or 1), first over USB, then another over the bridge; serialLabels is replaced whole
    when present (rc_config.h:1866-1881), so both writes carry the labels already set. The guard's own restore is a
    USB save, which re-advertises the original."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    nid = _nid(nc)
    problems = []
    cfg = nc.config()
    want0 = ncmesh.port_labels(cfg)
    have0 = _ifaces_of(_wcb_view(w1), nid)
    if have0 != want0:
        problems.append(f"W1 shows NaviCore's labels {have0}; its config derives {want0}")
    labels0 = dict(cfg.get("serialLabels") or {})
    key = next((k for k in ("S5", "S4", "S3") if not labels0.get(k)), None)
    if key is None:
        raise Skip("every aux port of NaviCore already has a user label")
    port = ("S3", "S4", "S5").index(key) + 1
    t1, t2 = marker("LU"), marker("LB")
    with nc_guard(bench) as g:
        nid = _open_relay(g.nc, w1)
        g.nc.set_config({"serialLabels": dict(labels0, **{key: t1})})
        got, took = _await_iface(w1, nid, port, t1, 3.0)
        if got != t1:
            problems.append(f"a label saved over USB: W1 shows {got!r} on WDP port {port} after {took:.1f} s")
        save = int(nonce(), 16) % 900000 + 1000
        bridged(w1, {"sys": 1, "type": "SET_CONFIG", "saveId": save, "data": {"serialLabels": dict(labels0, **{key: t2})}},
                rf'"saveId":{save}\b', timeout=8.0, target=nid)
        got, took = _await_iface(w1, nid, port, t2, 3.0)
        if got != t2:
            problems.append(f"a label saved over the bridge: W1 shows {got!r} on WDP port {port} after {took:.1f} s")
    got, took = _await_iface(w1, nid, port, want0.get(port), 3.0)
    if got != want0.get(port):
        problems.append(f"after the restore W1 shows {got!r} on WDP port {port}, expected {want0.get(port)!r}")
    assert not problems, "; ".join(problems)


@test("ncmesh.wdp_solicit_keeps_rows", "A ?WDP,POLL from W1 leaves NaviCore's row and port labels for W1 as they were "
      "(the solicit is not decoded as an advert) and makes NaviCore advertise within about two seconds",
      needs=["navicore", "wcb1"], links=[])
def wdp_solicit_keeps_rows(bench):
    """_handleWdpAdvert looks for the SOLICIT TLV before touching the neighbour table (WCB_Client.cpp:2051-2084): a
    solicit carries no facts, and decoded as an advert it would blank the sender's alias, labels, capabilities, Maestro
    ids and sequence hash until its next real advert, up to 60 s (the comment at :2051-2056). It arms a short advert
    burst instead, staggered by id (up to 600 ms). So after W1's ?WDP,POLL, NaviCore's row for W1 must hold the same
    fields and [WDPIF] rows, and W1's row for NaviCore must be fresh (AGE 0-2). Read only."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    me, nid = bench.usb_wcb_number(), _nid(nc)
    v0 = nc.wdp_view()
    row0 = _row_of(v0, me)
    if row0 is None:
        raise Skip(f"NaviCore has no WDP row for W{me} to compare (not heard in the last 180 s)")
    w1.run("?WDP,POLL")
    time.sleep(2.0)
    v1 = nc.wdp_view()
    row1 = _row_of(v1, me)
    problems = []
    keys = ("CLIENT", "ALIAS", "HW", "FW", "CAP", "CTRL", "MAESTRO", "PEER")
    if row1 is None:
        problems.append(f"NaviCore lost its row for W{me} after the solicit")
    else:
        bad = {k: (row0.get(k), row1.get(k)) for k in keys if row0.get(k) != row1.get(k)}
        if bad:
            problems.append(f"NaviCore's row for W{me} changed with the solicit: {bad}")
    if _ifaces_of(v1, me) != _ifaces_of(v0, me):
        problems.append(f"NaviCore's [WDPIF] rows for W{me} went {_ifaces_of(v0, me)} -> {_ifaces_of(v1, me)}")
    row20 = _row_of(_wcb_view(w1), nid)
    if row20 is None or int(row20.get("AGE") or 999) > 2:
        problems.append(f"W1's row for WCB{nid} is {row20 and row20.get('AGE')} s old two seconds after the solicit: "
                        f"NaviCore did not answer it")
    assert not problems, "; ".join(problems)


@test("ncmesh.wdp_neighbour_table", "NaviCore's ?WDP,DUMP: its SELF row first (PEER 3); a row per WCB equal to that WCB's "
      "own SELF row on every field it advertises, and [WDPIF] rows equal to its own; [WDPCFG] with auto-join on and PEERS "
      "counting the floor and the learned peers; the END count. A W2 port label holding ',', ']', '\"' and a backslash is "
      "scrubbed there and leaves GET_WCB_STATUS and the bridged GET_WCB_META parseable", needs=["navicore", "wcb1", "wcb2"],
      links=[])
def wdp_neighbour_table(bench):
    """WcbMgmt's printWdpDump (WCB_Client WCB_Mgmt.h:219-262): the SELF row, then per neighbour ALIAS, FW, HWREV and
    CAPTAGS through wdpScrub (',' ']' and control characters to '_', :169-174), HW, CAP, CTRL and MAESTRO as decoded
    (WCB_Client.cpp:2090-2160), PEER 2 learned / 1 in the floor / 0 otherwise, [WDPIF] per label (scrubbed), then
    [WDPCFG:EN=1,AUTOJOIN=..,PEERS=<floor + learned, silent ones too>] and [WDP:END,count=<rows without SELF>]. A WCB's
    own dump prints its SELF row and labels through the same scrub (WCB_WDP.cpp:1760-1792). GET_WCB_STATUS and
    GET_WCB_META strip '"', backslash and control characters from a label instead (NaviCore.ino:4190-4201,
    rc_telemetry.h:2007-2011), so a hostile label leaves both parseable. The label goes on W2's S5 inside
    config_guard(bench, 2), and the test puts S5 back in a finally (config_guard only proves it: it fails a test that
    leaves the label, run 20260929-025701); a ?WDP,POLL makes W2 advertise it, and another after the restore brings
    NaviCore the original back."""
    nc, w1, w2 = _nc(bench), usb_wcb(bench), _w2(bench)
    me, nid = bench.usb_wcb_number(), _nid(nc)
    quantity = (nc.config().get("wcbNetwork") or {}).get("quantity")
    problems = []
    v = nc.wdp_view()
    rows = {int(r["N"]): r for r in v["rows"] if str(r.get("N", "")).isdigit()}
    if not v["rows"] or v["rows"][0].get("PEER") != "3" or v["rows"][0].get("N") != str(nid):
        problems.append(f"NaviCore's first row is {v['rows'][:1]}, not its SELF row (N={nid}, PEER=3)")
    for n, w in ((me, w1), (2, w2)):
        own = _wcb_view(w)
        sr = next((r for r in own["rows"] if r.get("PEER") == "3"), {})
        nr = rows.get(n)
        if nr is None:
            problems.append(f"NaviCore has no row for W{n}")
            continue
        bad = {k: (nr.get(k), sr.get(k)) for k in ("ALIAS", "HW", "FW", "CAP", "CTRL", "MAESTRO") if nr.get(k) != sr.get(k)}
        if bad:
            problems.append(f"W{n}: NaviCore's row / W{n}'s SELF row differ on {bad}")
        if (nr.get("CLIENT"), nr.get("HWREV"), nr.get("CAPTAGS")) != ("0", "", ""):
            problems.append(f"W{n}: CLIENT/HWREV/CAPTAGS {nr.get('CLIENT')}/{nr.get('HWREV')}/{nr.get('CAPTAGS')}")
        if n <= (quantity or 0) and nr.get("PEER") != "1":
            problems.append(f"W{n} is in the floor (quantity {quantity}) but PEER={nr.get('PEER')}")
        if _ifaces_of(v, n) != _ifaces_of(own, n):
            problems.append(f"W{n}'s labels on NaviCore {_ifaces_of(v, n)}, on W{n} {_ifaces_of(own, n)}")
    learned = sum(1 for r in v["rows"] if r.get("PEER") == "2")
    cfgline = v.get("cfg") or {}
    peers = int(cfgline.get("PEERS") or -1)
    if cfgline.get("EN") != "1" or cfgline.get("AUTOJOIN") != "1" or peers < (quantity or 0) + learned:
        problems.append(f"[WDPCFG] {cfgline}, expected EN=1, AUTOJOIN=1, PEERS >= {quantity} + {learned} learned")
    if v.get("count") != len(v["rows"]) - 1:
        problems.append(f"[WDP:END,count={v.get('count')}] for {len(v['rows']) - 1} neighbour rows")
    label = f"HIL{nonce()[:4]},]\"\\x"
    with config_guard(bench, 2) as before:
        try:
            out = w2.run(f"?LABEL,S5,{label}")
            if any("Invalid" in x or "too long" in x for x in out):
                raise Skip(f"W2 refused the test label: {out[:2]}")
            w1.run("?WDP,POLL")
            deadline = time.monotonic() + 6
            while _ifaces_of(nc.wdp_view(), 2).get(5) != ncmesh.wdp_scrub(label) and time.monotonic() < deadline:
                time.sleep(0.5)
            got = _ifaces_of(nc.wdp_view(), 2).get(5)
            if got != ncmesh.wdp_scrub(label):
                problems.append(f"NaviCore's [WDPIF:N=2,S=5] reads {got!r}, expected the scrubbed "
                                f"{ncmesh.wdp_scrub(label)!r}")
            try:
                st = nc.wcb_status()
                lab = (ncmesh.status_rows(st).get(2) or {}).get("labels") or []
                if len(lab) < 5 or lab[4] != ncmesh.json_strip(label):
                    problems.append(f"GET_WCB_STATUS' label for W2 S5 is {lab[4:5]}, expected {ncmesh.json_strip(label)!r}")
            except ValueError:
                problems.append("GET_WCB_STATUS does not parse with the label set")
            nid = _open_relay(nc, w1)
            nm, wm = nc.dev.mark(), w1.dev.mark()
            w1.send(f';W{nid},{{"type":"GET_WCB_META"}}')
            sid = int(_nav_regex(nc, nm, r"\[RC\] WCB_META send START: \d+ bytes \S+ \d+ fragments to W\d+ "
                                 r"\(sid=(\d+)\)", 5, "'WCB_META send START' line").group(1))
            text = _await_reassembled(w1, nid, wm, sid, 8)
            try:
                meta = json.loads(text or "")
                if (meta.get("portLabels") or [[]] * 2)[1][4:5] != [ncmesh.json_strip(label)]:
                    problems.append(f"GET_WCB_META's label for W2 S5 is {meta['portLabels'][1][4:5]}")
            except (ValueError, IndexError, TypeError):
                problems.append("the bridged GET_WCB_META does not parse with the label set")
        finally:
            _put_back(w2, before[2], "?LABEL,S5,", "?LABEL,CLEAR,S5")
    w1.run("?WDP,POLL")
    time.sleep(2.0)
    if _ifaces_of(nc.wdp_view(), 2).get(5) is not None and _ifaces_of(nc.wdp_view(), 2).get(5) == ncmesh.wdp_scrub(label):
        problems.append("NaviCore still shows the test label on W2 S5 after the restore and a ?WDP,POLL")
    assert not problems, "; ".join(problems)


@test("ncmesh.alias_whoami", "With W2's ?ALIAS cleared and NaviCore's cached name for it emptied (a wcb_alias message from "
      "W1), NaviCore's status polls send W2 at most 4 ?WHOAMI in its online session and then stop; W2's alias and "
      "NaviCore's name for it come back afterwards", needs=["navicore", "wcb1", "wcb2"], links=[])
def alias_whoami(bench):
    """maybeQueryAlias (rc_telemetry.h:1822-1831), called for each online board by every GET_WCB_STATUS (USB,
    NaviCore.ino:4160-4165; bridged, rc_telemetry.h:1948): '?WHOAMI' only while the cached alias is empty, at most
    ALIAS_MAX_TRIES (4) per online session, re-armed only by an offline-to-online edge (rc_telemetry.h:1808-1831). The
    cache fills from each WDP advert's name (onWcbNeighbor, NaviCore.ino:5269-5281) - but an advert with no name leaves
    it alone - and from a wcb_alias reply, which overwrites it, empty included (rc_telemetry.h:2201-2204). A WCB with no
    alias advertises no ALIAS TLV (WCB_WDP.cpp:248-251) and answers ?WHOAMI with an empty alias (WCB.ino:5567-5588), so
    the cache stays empty and the cap is what stops the queries. The cache is emptied by a wcb_alias message sent from
    W1, as W2 would answer. W2 logs each ?WHOAMI it receives under ?DEBUG,ETM (RAM only, on for the test). Eight USB
    status polls, half a second apart. Skips when none goes out: the session's budget was spent earlier (it re-arms only
    when W2 goes offline and back). The test puts W2's alias back in a finally inside config_guard(bench, 2), which
    only proves it (it fails a test that leaves the alias cleared, run 20260929-025701), and a ?WDP,POLL brings NaviCore
    its name again."""
    nc, w1, w2 = _nc(bench), usb_wcb(bench), _w2(bench)
    nid = _nid(nc)
    tok = token(bench.config_tokens(2, refresh=True), "?ALIAS,")
    alias = tok[len("?ALIAS,"):] if tok else ""
    polls = []
    with config_guard(bench, 2) as before:
        try:
            w2.run("?ALIAS,CLEAR")
            w1.run("?WDP,POLL")
            time.sleep(1.5)
            w1.send(f';W{nid},{{"type":"wcb_alias","id":2,"alias":""}}')
            time.sleep(1.0)
            w2.debug("ETM", True)
            try:
                m2 = w2.dev.mark()
                for _ in range(8):
                    polls.append((ncmesh.status_rows(nc.wcb_status()).get(2) or {}).get("alias"))
                    time.sleep(0.5)
                time.sleep(1.0)
                who = re.compile(rf"\[ETM\] Received seq \d+ from WCB{nid}(?: \[wizard\])?: \?WHOAMI$")
                asked = [x for x in w2.dev.since(m2) if who.search(x.rstrip())]
            finally:
                w2.debug("ETM", False)
        finally:
            _put_back(w2, before[2], "?ALIAS,", "?ALIAS,CLEAR")
    w1.run("?WDP,POLL")
    deadline = time.monotonic() + 6
    back = None
    while time.monotonic() < deadline:
        back = (ncmesh.status_rows(nc.wcb_status()).get(2) or {}).get("alias")
        if back == ncmesh.json_strip(alias)[:24]:
            break
        time.sleep(0.5)
    bench.note(f"W2 got {len(asked)} ?WHOAMI over 8 polls; NaviCore's name for W2 during them {set(polls)}, after "
               f"{back!r}")
    if not asked:
        raise Skip("NaviCore sent W2 no ?WHOAMI: this online session's budget of 4 was spent earlier (it re-arms only "
                   "when W2 goes offline and back)")
    problems = []
    if len(asked) > 4:
        problems.append(f"NaviCore sent W2 {len(asked)} ?WHOAMI in one online session; the cap is 4")
    if any(p for p in polls):
        problems.append(f"NaviCore named W2 {set(polls)} while W2 had no alias")
    if back != ncmesh.json_strip(alias)[:24]:
        problems.append(f"after the restore NaviCore names W2 {back!r}, expected {ncmesh.json_strip(alias)[:24]!r}")
    assert not problems, "; ".join(problems)


@test("ncmesh.wdp_learn_forget", "OPT-IN (navicore_nvs): a permanent client above the floor (the probe) is learned by "
      "NaviCore on its advert burst (PEER 2, PEERS + 1), FORGET_PEER drops it again (row gone, PEERS back), and a learned "
      "id that comes back advertising TEMPORARY is forgotten on its own", needs=["navicore", "wcb1", "probe2"],
      links=[], opt_in="navicore_nvs")
def wdp_learn_forget(bench):
    """WCB_Client auto-join (WCB_Client.cpp:2165-2170): a non-temporary advert counted twice flags a join, which
    update() performs - esp_now_add_peer and the learned mask saved to NVS, '[WCB_Client] auto-joined WCB<n>'
    (:1698-1720). FORGET_PEER over USB (NaviCore.ino:4086-4105, doForgetPeer :3000-3012) calls forgetPeer: the mask
    saved, the neighbour record dropped and its advert count reset, '[WCB_Client] forgot WCB<n>'
    (WCB_Client.cpp:1778-1799). A learned id whose advert now carries the TEMPORARY flag is forgotten by update()
    (WCB_Client.cpp:249-255), the downgrade. The probe (probe2) joins PERMANENT, so W1 and W2 learn and persist it too
    (s18 _joined); every board forgets it in the finally (s18 _forget_everywhere, which also sends NaviCore
    FORGET_PEER). Four NVS writes on NaviCore (learn, forget, learn, downgrade): the reason for the opt-in."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    n = _free_id(nc, (13, 12, 14, 11))
    if _row_of(_wcb_view(w1), n) is not None:
        raise Skip(f"W1 has a WDP row for WCB{n}: the id is in use")
    problems, notes = [], []

    def learned(since, timeout=12.0):
        try:
            _nav_regex(nc, since, rf"^\[WCB_Client\] auto-joined WCB{n}$", timeout, f"'auto-joined WCB{n}' line")
            return True
        except AssertionError:
            return False

    def peer_flag():
        return (_row_of(nc.wdp_view(), n) or {}).get("PEER")

    def peers():
        p = (nc.wdpcfg() or {}).get("PEERS", "")
        return int(p) if str(p).isdigit() else None
    p0 = peers()
    try:
        nm = nc.dev.mark()
        with _joined(bench, "probe2", n, temporary=False, forget=False) as probe:
            if not learned(nm):
                w1.run("?WDP,POLL")
                if not learned(nm, 8.0):
                    raise AssertionError(f"NaviCore never learned the permanent probe as WCB{n}")
            time.sleep(1.0)
            notes.append(f"learned: PEER={peer_flag()}, PEERS {p0} -> {peers()}")
            if peer_flag() != "2" or peers() != (p0 or 0) + 1:
                problems.append(f"after the join: PEER={peer_flag()}, PEERS={peers()} (before {p0})")
            nm = nc.dev.mark()
            ack = nc.ack({"type": "FORGET_PEER", "id": n}, of="FORGET_PEER")
            lines = _flushed(nc, nm, settle=0.5)
            if ack != {"type": "ACK", "of": "FORGET_PEER", "ok": True, "id": n}:
                problems.append(f"FORGET_PEER {n}: ACK {ack}")
            for want in (f"[WCB_Client] forgot WCB{n}", f"[WCB] forgot learned WCB {n}"):
                if want not in lines:
                    problems.append(f"FORGET_PEER {n}: no '{want}' line")
            if peer_flag() == "2" or peers() != p0:
                problems.append(f"after FORGET_PEER: PEER={peer_flag()}, PEERS={peers()} (before the join {p0})")
            probe.mesh_leave()
        nm = nc.dev.mark()
        with _joined(bench, "probe2", n, temporary=False, forget=False) as probe:
            if not learned(nm):
                w1.run("?WDP,POLL")
                learned(nm, 8.0)
            probe.mesh_leave()
        relearned = peer_flag() == "2"
        nm = nc.dev.mark()
        with _joined(bench, "probe2", n, temporary=True, forget=False):
            try:
                _nav_regex(nc, nm, rf"^\[WCB_Client\] forgot WCB{n}$", 12, f"'forgot WCB{n}' line for the downgrade")
                downgraded = True
            except AssertionError:
                downgraded = False
            flag = peer_flag()
        notes.append(f"re-learned {relearned}; downgraded {downgraded}, PEER={flag}")
        if not relearned:
            problems.append(f"the second permanent join was not learned again (PEER={peer_flag()})")
        elif not downgraded or flag == "2":
            problems.append(f"a learned WCB{n} advertising TEMPORARY stayed learned (PEER={flag})")
    finally:
        _forget_everywhere(bench, n, navicore=True)
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


# ============================================================ membership and failure
@test("ncmesh.etm_ack_sender_side", "Ten unicasts from W1 to NaviCore are all ACKed: after ?STATS,RESET W1's row for "
      "WCB20 reads Sent 10, ACKd 10, no retry, no failure, and NaviCore counts ten more received from W1",
      needs=["navicore", "wcb1"], links=[])
def etm_ack_sender_side(bench):
    """WCB_Client ACKs every COMMAND addressed to it first thing, before its CRC check and duplicate window
    (WCB_Client.cpp:2825-2845), and onWCBCommand counts each delivery per sender (NaviCore.ino:3025-3041,
    g_meshRxFrom). W1 tracks each ETM unicast to its controller in the '(special)' row of ?STATS (WCB.ino:2203-2214),
    so W1 must carry ?CONTROLLER,ON,20. The ten are '#L12', a harmless read NaviCore runs and answers over RTERM (raw
    packets, outside both counters), 0.3 s apart. A retry means an ACK lost on the air: this asserts a clean link."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    me, nid = bench.usb_wcb_number(), _nid(nc)
    require_tokens(bench, me, f"?CONTROLLER,ON,{nid}")
    r0 = _mesh_row(nc, me)
    w1.run("?STATS,RESET")
    for _ in range(10):
        w1.send(f";W{nid},#L12")
        time.sleep(0.3)
    time.sleep(2.0)
    st = w1.etm_board_stats().get(nid)
    r1 = _mesh_row(nc, me)
    bench.note(f"W1's row for WCB{nid}: {st}; NaviCore's recv from W{me} {r0['recv']} -> {r1['recv']}")
    assert st, f"W1's ?STATS has no row for WCB{nid}"
    assert st["sent"] >= 10 and st["ackd"] == st["sent"] and (st["retries"], st["failed"]) == (0, 0), \
        f"W1's row for WCB{nid}: sent {st['sent']}, ackd {st['ackd']}, retries {st['retries']}, failed {st['failed']}"
    assert r1["recv"] - r0["recv"] >= 10, f"NaviCore counted {r1['recv'] - r0['recv']} deliveries from W{me}, not 10"


@test("ncmesh.w1_tracks_20", "With W1 deaf, W1 marks NaviCore (WCB20, its controller) OFFLINE once its own offline window "
      "has passed with nothing heard, and ONLINE within seconds of hearing again; W2 is back online on W1 before the test "
      "ends (slow, about 70 s)", needs=["navicore", "wcb1"], links=[])
def w1_tracks_20(bench):
    """W1 counts any valid ETM packet from a peer as presence (WCB.ino:5322-5340), so NaviCore's 2 s rc_hb keeps it online
    on W1 until W1 hears nothing at all: ncmesh.deaf(1) flips W1's receive filter on its own console. W1 sweeps its peers
    and its special peer against (?ETM,HB + 1) x ?ETM,MISS seconds of its own settings (WCB.ino:1400-1419: '[ETM] WCB20
    (special peer) went OFFLINE (no heartbeat for <n>s)'), counted from the last packet heard, at most 2 s before the
    flip; the first packet after the octet is back prints '[ETM] WCB20 came ONLINE' (:5334-5339). W2 goes offline on W1
    the same way meanwhile, and the test waits for its next heartbeat before it ends. W1 must carry ?CONTROLLER,ON,20."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    me, nid = bench.usb_wcb_number(), _nid(nc)
    require_tokens(bench, me, f"?CONTROLLER,ON,{nid}")
    etm = _etm(bench.config_tokens(me, refresh=True))
    window = (int(etm["HB"]) + 1) * int(etm["MISS"])
    off_rx = rf"^\[ETM\] WCB{nid} \(special peer\) went OFFLINE \(no heartbeat for {window}s\)"
    wm = w1.dev.mark()
    with deaf(bench, me):
        t0 = time.monotonic()
        w1.dev.expect(off_rx, timeout=window + 15, since=wm)
        took = time.monotonic() - t0
        bm = w1.dev.mark()      # W1 hears nothing until the octet is back, so ONLINE can only come after this mark,
    t1 = time.monotonic()       # and it may print while deaf() is still confirming the octet
    w1.dev.expect(rf"^\[ETM\] WCB{nid} came ONLINE", timeout=15, since=bm)
    back = time.monotonic() - t1
    others = [n for n in bench.wcb_numbers() if n != me]
    deadline = time.monotonic() + 15
    while any(ncmesh.stats_rows(w1.run("?STATS")).get(n) != "Online" for n in others) and time.monotonic() < deadline:
        time.sleep(1.0)
    rows = ncmesh.stats_rows(w1.run("?STATS"))
    bench.note(f"W1 deaf: WCB{nid} OFFLINE after {took:.1f} s (window {window} s); ONLINE {back:.1f} s after the octet "
               f"came back; W1's rows now {rows}")
    assert window - 3 <= took <= window + 3, \
        f"W1 marked WCB{nid} offline {took:.1f} s into its deafness; its window is {window} s"
    assert back <= 12, f"W1 took {back:.1f} s to see WCB{nid} again (NaviCore broadcasts rc_hb every 2 s)"
    assert rows.get(nid) == "Online" and all(rows.get(n) == "Online" for n in others), f"W1's rows after: {rows}"


@test("ncmesh.online_tracking_flip", "With W2's heartbeat stretched to 60 s (on W2's own console), NaviCore prints "
      "'[WCB] WCB2 ... OFFLINE' about 50 s after W2's last heartbeat, lists it offline in GET_WCB_STATUS and keeps its "
      "SBUS input at full rate; with the heartbeat put back, W2's next beat prints ONLINE, and a WCB_SEND to W2 lands and "
      "is ACKed (slow, about 80 s)", needs=["navicore", "wcb1", "wcb2"], links=["W2S3"])
def online_tracking_flip(bench):
    """NaviCore marks a board online only on its heartbeat or boot announce (WCB_Client.cpp:2716-2769) and offline 50 s
    after the last one (10 s x 5, :2480-2508), printing onWcbStatus's line (NaviCore.ino:5330-5335). The plan deafened
    W2 for this; that cannot work: a deaf WCB still transmits (only its receive filter changes, hil/ncmesh.deaf), so
    NaviCore keeps it online. W2's heartbeat is stretched instead: '?ETM,HB,60' on W2's own console (range 1-3600,
    saved, WCB.ino:6469-6485) takes effect after the beat already scheduled, up to 11 s away (scheduleNextHeartbeat
    :1357-1377), so W2 is silent 59-61 s after it - past NaviCore's 50 s. The old value goes back as soon as the OFFLINE
    line is in (in a finally, on W2's console), and W2's next beat, still on the 60 s schedule, brings it ONLINE. W1 also
    sees W2 offline for a few seconds ((10 + 1) x 5 = 55 s window), which is noted. config_guard(bench, 2) proves W2's
    settings as they were. The WCB_SEND afterwards writes a marker out of W2 S3 (the W2S3 probe)."""
    l23 = link(bench, 2, "S3")
    nc, w2 = _nc(bench), _w2(bench)
    if not (ncmesh.status_rows(nc.wcb_status()).get(2) or {}).get("online"):
        raise Skip("NaviCore does not have W2 online to begin with")
    _require_full_rate(nc)
    off_rx = rf"^\[WCB\] WCB2(?: {DOT} .*)? OFFLINE$"
    on_rx = rf"^\[WCB\] WCB2(?: {DOT} .*)? ONLINE$"
    problems = []
    with config_guard(bench, 2) as before:
        hb0 = _etm(before[2])["HB"]
        nm = nc.dev.mark()
        try:
            if not any("ETM heartbeat set to 60 sec" in x for x in w2.run("?ETM,HB,60")):
                raise AssertionError("W2 did not take ?ETM,HB,60")
            t_set = time.monotonic()
            _nav_regex(nc, nm, off_rx, 11 + OFFLINE_S + 12, "OFFLINE line for WCB2")
            took = time.monotonic() - t_set
            row = ncmesh.status_rows(nc.wcb_status()).get(2) or {}
            fps = _full_fps(nc)
        finally:
            for _ in range(2):
                try:
                    if any(f"ETM heartbeat set to {hb0} sec" in x for x in w2.run(f"?ETM,HB,{hb0}")):
                        break
                except AssertionError:
                    time.sleep(0.5)
        om = nc.dev.mark()
        _nav_regex(nc, om, on_rx, 30, "ONLINE line for WCB2")
        back = time.monotonic() - t_set
        row2 = ncmesh.status_rows(nc.wcb_status()).get(2) or {}
        r0 = _mesh_row(nc, 2)
        pm, t = l23.mark(), marker("OF")
        ack = nc.wcb_send(2, f";S3{t}")
        try:
            l23.expect(t.encode() + b"\r", timeout=3, since=pm)
            landed = True
        except AssertionError:
            landed = False
        time.sleep(1.0)
        r1 = _mesh_row(nc, 2)
    bench.note(f"WCB2 OFFLINE on NaviCore {took:.1f} s after ?ETM,HB,60, ONLINE {back:.1f} s after; fps {fps}; "
               f"then sent {_delta(r0, r1)}")
    if not 40 <= took <= 11 + OFFLINE_S + 3:
        problems.append(f"NaviCore marked W2 offline {took:.1f} s after its heartbeat was stretched; expected 50-61 s")
    if row.get("online"):
        problems.append("GET_WCB_STATUS still listed W2 online after the OFFLINE line")
    if fps < SBUS_FULL_FPS:
        problems.append(f"SBUS read {fps} fps with W2 offline")
    if not row2.get("online"):
        problems.append("GET_WCB_STATUS does not list W2 online after the ONLINE line")
    if not ack.get("ok") or not landed:
        problems.append(f"the WCB_SEND to W2 afterwards: ACK {ack}, landed on W2 S3 {landed}")
    if _delta(r0, r1)["ackd"] < 1:
        problems.append(f"W2's MESH_STATS row did not count the ACK: {_delta(r0, r1)}")
    assert not problems, "; ".join(problems)


@test("ncmesh.ensured_degrade", "With W2 deaf (still heartbeating, so online on NaviCore, but never ACKing), 12 USB "
      "WCB_SENDs to W2 in a burst fill NaviCore's 10 ETM slots: the first 8 at least answer ok:true, the ones after the "
      "table filled ok:false ('send refused by WCB_Client'); W2's MESH_STATS row counts those unguaranteed and the rest "
      "failed once their retries ran out; nothing reaches W2 S3, even after W2 hears again; SBUS stays at full rate",
      needs=["navicore", "wcb1", "wcb2"], links=["W2S3"])
def ensured_degrade(bench):
    """_sendPacket (WCB_Client.cpp:2207-2324): each ensured unicast takes one of WCB_PENDING_MAX (10) slots, which stays
    busy while its board is online and has not ACKed - retried every 500 ms, three times, then failed (update()
    :283-330, _ensuredComplete :2364-2381). With every slot outstanding, a send goes out once untracked, is counted
    'unguaranteed' and returns false (:2306-2323), which the USB WCB_SEND reports as ok:false (NaviCore.ino:4063-4084).
    A deaf W2 is exactly that board: it keeps heartbeating (only its receive filter changed, hil/ncmesh.deaf), so
    NaviCore keeps it online, and it hears none of the sends. NaviCore's own periodic unicasts (the mode report to W2,
    the stats report to W1) can hold a slot too, hence 'the first 8'. Each send is ';S3<marker>' for W2 S3 (the W2S3
    probe): none may arrive, and the burst waits out every retry before W2 hears again, so none arrives late."""
    l23 = link(bench, 2, "S3")
    nc = _nc(bench)
    if not (ncmesh.status_rows(nc.wcb_status()).get(2) or {}).get("online"):
        raise Skip("NaviCore does not have W2 online to begin with")
    _require_full_rate(nc)
    tag = marker("DG")
    pm = l23.mark()
    oks, lat, msgs = [], [], set()
    with deaf(bench, 2):
        time.sleep(0.5)
        r0 = _mesh_row(nc, 2)
        for k in range(12):
            t0 = time.monotonic()
            ack = nc.wcb_send(2, f";S3{tag}{k:02d}")
            lat.append(time.monotonic() - t0)
            oks.append(ack.get("ok"))
            if ack.get("ok") is False:
                msgs.add(ack.get("msg"))
        time.sleep(ETM_RETRY_S * 4 + 1.5)
        r1 = _mesh_row(nc, 2)
        fps = _full_fps(nc)
    time.sleep(2.0)
    got = l23.received(pm)
    d = _delta(r0, r1)
    refused = oks.count(False)
    bench.note(f"ok per send {oks}; W2 row delta {d}; slowest ACK {max(lat) * 1000:.0f} ms; fps {fps}")
    problems = []
    if refused < 1 or not all(oks[:PENDING_MAX - 2]):
        problems.append(f"the burst answered {oks}: expected ok:true until the 10 slots filled, then ok:false")
    if msgs - {"send refused by WCB_Client"}:
        problems.append(f"a refused WCB_SEND said {sorted(msgs, key=str)}; NaviCore.ino:4076-4079 says 'send refused by "
                        f"WCB_Client'")
    if d["sent"] < 12 or d["ung"] < refused or d["fail"] < 12 - refused or d["ackd"]:
        problems.append(f"W2's MESH_STATS row moved {d}; expected sent +12, unguaranteed +{refused}, failed "
                        f"+{12 - refused}, no ACK")
    if tag.encode() in got:
        problems.append("a send reached W2 S3 although W2 was deaf until every retry was over")
    if fps < SBUS_FULL_FPS:
        problems.append(f"SBUS read {fps} fps during the burst")
    assert not problems, "; ".join(problems)


@test("ncmesh.mesh_loss_local_survives", "With W1 and W2 both deaf, so nothing NaviCore sends is ACKed, NaviCore's local "
      "work goes on: SBUS at full rate, its local Maestro answers ?MAE,GET, a TRIGGER still emits rc_trig, and each USB "
      "WCB_SEND is answered within half a second even once its ETM table is full", needs=["navicore", "wcb1", "wcb2"],
      links=[])
def mesh_loss_local_survives(bench):
    """Nothing NaviCore does locally waits on the mesh: WCB_SEND returns as soon as the frame is queued, tracked or
    degraded (WCB_Client.cpp:2207-2324), ETM retries run in wcb->update() one pass at a time (:283-330), and SBUS,
    the local Maestro (Serial2) and rcDispatch run in the same loop(). Both WCBs deafened (ncmesh.deaf on each own
    console) is the closest this bench comes to losing the mesh: both still transmit, so NaviCore keeps them online and
    every send waits for ACKs that never come, filling the pending table. Fourteen WCB_SENDs alternate between them;
    each carries '?HILNOOP', which prints 'Unknown command' at most. ?MAE,GET reads NaviCore's first local Maestro
    (nothing moves); the TRIGGER is on an inert key (s40 _inert_keys), so it emits rc_trig and runs nothing."""
    nc = _nc(bench)
    cfg = nc.config()
    slots = nc.local_slots(cfg)
    mode, btn = divmod(int(_inert_keys(cfg, 1)[0]), 100)
    _require_full_rate(nc)
    lat, oks = [], []
    with deaf(bench, 1):
        with deaf(bench, 2):
            for k in range(14):
                t0 = time.monotonic()
                oks.append(nc.wcb_send(1 + k % 2, "?HILNOOP").get("ok"))
                lat.append(time.monotonic() - t0)
            fps = [_full_fps(nc) for _ in range(2)]
            mae = nc.mae_get(slots[0][0], 0) if slots else None
            ack, trig = nc.trigger(mode, btn, 1)
            time.sleep(ETM_RETRY_S * 4 + 1.0)
    bench.note(f"ok per send {oks}; slowest {max(lat) * 1000:.0f} ms; fps {fps}; ?MAE,GET {mae}; rc_trig {trig}")
    problems = []
    if max(lat) > 0.5:
        problems.append(f"a WCB_SEND took {max(lat) * 1000:.0f} ms to answer")
    if min(fps) < SBUS_FULL_FPS:
        problems.append(f"SBUS read {min(fps)} fps")
    if slots and not isinstance(mae, int):
        problems.append(f"?MAE,GET on local slot {slots[0][0]} answered {mae!r}")
    if not ack.get("ok") or trig is None:
        problems.append(f"TRIGGER {mode}/{btn}: ACK {ack}, rc_trig {trig}")
    if False not in oks:
        bench.note("no WCB_SEND was refused: the table never filled (the ACK-less window was shorter than expected)")
    assert not problems, "; ".join(problems)


@test("ncmesh.dedup_after_w1_reboot", "NaviCore forgets W1's command window when W1's boot announce arrives: 20 plain "
      "commands from W1 after one reboot and 20 more after a second reboot all reach NaviCore, although W1's sequence "
      "numbers start again each time (two W1 reboots, about a minute)", needs=["navicore", "wcb1"], links=[])
def dedup_after_w1_reboot(bench):
    """WCB_Client drops a COMMAND whose sequence number it has seen from that sender, up to 32 below the highest
    (_cmdSeqDup, WCB_Client.cpp:879-898), and forgets a sender's window once per boot announce (:2744-2769). A WCB
    restarts its sequence numbers at boot. One reboot proves nothing: after a long uptime W1's old high is far above
    the new numbers, which are then too old to remember and pass anyway (:897). So W1 reboots, sends 20 commands
    (NaviCore's window for W1 is now about 20 high), reboots again and sends 20 more: without the forget, the second
    twenty reuse numbers the window marks as seen and are dropped, ACKed and silent. Plain text, seen on NaviCore under
    DBG_MAESTRO ('[WCB RX] from WCB1: <text>', NaviCore.ino:3124-3125); skips while NaviCore fans plain mesh text out an
    aux port. A WCB reboot moves nothing (hil/servos.py)."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    me, nid = bench.usb_wcb_number(), _nid(nc)
    _no_plain_fanout(nc)
    seen = {}
    with nc.debug(DBG_MAESTRO):
        for rnd in (1, 2):
            w1.reboot()
            deadline = time.monotonic() + 12
            while ncmesh.stats_rows(w1.run("?STATS")).get(nid) != "Online" and time.monotonic() < deadline:
                time.sleep(1.0)
            tag = marker(f"D{rnd}")
            texts = [f"{tag}{k:02d}" for k in range(20)]
            nm = nc.dev.mark()
            for t in texts:
                w1.send(f";W{nid},{t}")
                time.sleep(0.12)
            time.sleep(1.5)
            seen[rnd] = len(_rx_seen(nc, nm, me, texts))
    bench.note(f"after the first reboot NaviCore ran {seen[1]} of 20, after the second {seen[2]} of 20")
    assert seen[1] == 20, f"after W1's first reboot only {seen[1]} of 20 commands reached NaviCore"
    assert seen[2] == 20, (f"after W1's second reboot only {seen[2]} of 20 commands reached NaviCore: its window for W1 "
                           f"still held the numbers of the first twenty")


@test("ncmesh.crc_namespace_gates", "NaviCore's receive gates, seen from the probe: joined as the bench is it runs a "
      "command; joined with checksums off its command is ACKed (no retry) and rejected with 'Missing CRC'; joined with "
      "the wrong mesh password nothing it sends is even seen", needs=["navicore", "wcb1", "probe1"], links=[])
def crc_namespace_gates(bench):
    """_handleReceive (WCB_Client.cpp:2655-2905): a packet whose password is not NaviCore's is dropped before anything
    else (:2694-2700), with no line; a COMMAND is ACKed first (:2825-2845), then its CRC checked while checksums are on,
    which NaviCore's always are, '[WCB_Client] Missing CRC from WCB<n> — packet rejected' when there is none
    (:2856-2880). The plan says a CRC-less command gets no ACK: the code ACKs it on purpose, so the sender stops
    retrying (:2825-2827). The probe has no ACK counter, so the ACK is read from NaviCore: an ACKed command is sent
    once, one 'Missing CRC' line; an unACKed one is retried three times, four lines. The source-MAC namespace check
    (:2657-2674) is not reached: a probe on other octets could not even address NaviCore. The probe (probe1) joins with
    quantity 20 so it can unicast NaviCore; the first join burns NaviCore's duplicate window (ncmesh.probe_peer), and
    the checksum-off and wrong-password joins do not need it (the CRC check runs before the window, the password check
    before everything). Plain text is seen under DBG_MAESTRO ('[WCB RX] from WCB<n>: <text>'), set inside the
    probe_peer block: the burn ends by putting NaviCore's debug flags to 0 (burn_window's NaviCore.debug), so a
    DBG_MAESTRO set around probe_peer is gone before the control command goes (run 20260929-025701: the control 'did
    not reach NaviCore', and the two negative checks after it saw nothing because nothing could be printed). Skips
    while NaviCore fans plain mesh text out an aux port."""
    nc = _nc(bench)
    _no_plain_fanout(nc)
    pid = _free_id(nc, (16, 15, 17, 18))
    problems = []
    tag = marker("G")
    with probe_peer(bench, pid) as probe:
        with nc.debug(DBG_MAESTRO):
            nm = nc.dev.mark()
            probe.mesh_send(ncmesh.NAVICORE_ID, f"{tag}A")
            time.sleep(1.5)
            if not _rx_seen(nc, nm, pid, [f"{tag}A"]):
                problems.append("joined as the bench is, the probe's command did not reach NaviCore")
    with nc.debug(DBG_MAESTRO):
        with probe_in_mesh(bench, "probe1", pid, checksum=False, quantity=ncmesh.NAVICORE_ID) as probe:
            time.sleep(1.0)
            nm = nc.dev.mark()
            for k in range(3):
                probe.mesh_send(ncmesh.NAVICORE_ID, f"{tag}C{k}")
                time.sleep(1.0)
            time.sleep(2.5)
            lines = _flushed(nc, nm, settle=0.3)
            missing = sum(1 for x in lines if x.startswith(f"[WCB_Client] Missing CRC from WCB{pid}"))
            ran = _rx_seen(nc, nm, pid, [f"{tag}C{k}" for k in range(3)])
            if not 3 <= missing <= 4:
                problems.append(f"checksums off: {missing} 'Missing CRC' lines for 3 commands; 3 means each was ACKed "
                                f"once, 12 would mean none was")
            if ran:
                problems.append(f"checksums off: {ran} ran anyway")
        with probe_in_mesh(bench, "probe1", pid, password="HIL" + nonce(), quantity=ncmesh.NAVICORE_ID) as probe:
            time.sleep(1.0)
            nm = nc.dev.mark()
            probe.mesh_send(ncmesh.NAVICORE_ID, f"{tag}P")
            time.sleep(3.0)
            lines = _flushed(nc, nm, settle=0.3)
            if any(f"WCB{pid}" in x and ("CRC" in x or f"{tag}P" in x) for x in lines):
                problems.append("with the wrong password the probe's command still reached NaviCore's checks")
    assert not problems, "; ".join(problems)


@test("ncmesh.long_command_truncation", "(should) A command longer than 199 characters that reaches NaviCore in "
      "fragments is refused with a line, never run cut short: a 300-character relayed CLI line must not answer "
      "'Unknown command:' with its first 199 characters", needs=["navicore", "wcb1", "probe1"], links=[])
def long_command_truncation(bench):
    """NAVICORE.md D-NC26. WCB_Client reassembles a command sent to NaviCore in MGMT fragments whole, up to 16 x 179
    characters (_maybeHandleMgmtFrag, WCB_Client.cpp:2576-2640), and hands it to onWCBCommand, whose queues copy it with
    strlcpy into RemoteCliMsg.cmd[200] or SerialFwdMsg.text[201] (NaviCore.ino:90, :327, :2925-2946), cutting it at 199
    or 200 characters without a word. The CLI path is the one used here: '?HILT<nonce>' padded to 300 characters, which
    NaviCore does not know, so it answers 'Unknown command: <line>' (:5019-5020) - today with the first 199 characters.
    The serial path ';s<n>' would write NaviCore's own aux port, which this bench does not watch (navicore_aux_tx); the
    ;M path already drops an over-long line (queueMaestroCmd :2951-2958). The probe (probe1, ncmesh.probe_peer) sends
    it, and its own console shows the fragmented send completing; a 60-character twin first proves the path.
    Recommendation: refuse an over-long command with a log line, never truncate."""
    nc = _nc(bench)
    pid = _free_id(nc, (17, 18, 16, 15))
    tag = nonce()
    short = f"?HILT{tag}S" + "s" * 50
    long_ = (f"?HILT{tag}L" + FILL * 2)[:300]
    with probe_peer(bench, pid) as probe:
        nm = nc.dev.mark()
        probe.mesh_send(ncmesh.NAVICORE_ID, short)
        time.sleep(1.5)
        ok_short = f"Unknown command: {short}" in _flushed(nc, nm, settle=0.2)
        nm, pm = nc.dev.mark(), probe.dev.mark()
        probe.mesh_send(ncmesh.NAVICORE_ID, long_)
        time.sleep(3.0)
        lines = _flushed(nc, nm, settle=0.3)
        left = any(re.search(rf"fragmented send to WCB{ncmesh.NAVICORE_ID} complete", x) for x in probe.dev.since(pm))
    cut = [x for x in lines if x.startswith("Unknown command: ?HILT" + tag + "L")]
    bench.note(f"short twin answered {ok_short}; the probe's fragmented send completed {left}; 'Unknown command:' lines "
               f"for the long one: {[len(x) - len('Unknown command: ') for x in cut]} characters")
    if not ok_short:
        raise AssertionError("the 60-character twin got no 'Unknown command:' answer: the probe does not reach NaviCore")
    if not left:
        raise AssertionError("the probe's 300-character send never completed: nothing to judge")
    short_runs = [len(x) - len("Unknown command: ") for x in cut if len(x) - len("Unknown command: ") < len(long_)]
    assert not short_runs, (f"(should, D-NC26) a {len(long_)}-character command ran cut to {short_runs[0]} characters: "
                            f"RemoteCliMsg.cmd[200] (NaviCore.ino:90) truncates it silently in queueRemoteCli (:2925-2930)")


# ============================================================ content
@test("ncmesh.wcb_status_content", "USB GET_WCB_STATUS agrees with the WCBs themselves: each online, known, not a client "
      "and not temporary, with the alias, port labels and sequence hash it advertises, and NaviCore's own slot online "
      "when it is listed", needs=["navicore", "wcb1", "wcb2"], links=[])
def wcb_status_content(bench):
    """The USB WCB_STATUS (NaviCore.ino:4136-4234): online from the ETM heartbeat for a WCB, the self slot forced
    online; known = in the floor, online, advertising or learned (wcbBoardKnown, rc_telemetry.h:1845-1855); clients and
    temporary from the WDP neighbour; aliases from the advert's name (onWcbNeighbor, NaviCore.ino:5269-5281) with '"',
    backslash and control characters stripped (setWcbAlias, rc_telemetry.h:1787-1800); portLabels the advertised labels
    stripped the same way (NaviCore.ino:4190-4201); seqHash the advert's SEQHASH. Each WCB's side is read on its own
    console: the alias from its config chain, the labels from its own ?WDP,DUMP SELF rows (scrubbed for the dump, so
    compared through ncmesh.wdp_scrub), the hash from ?SEQ,NAMES. Read only."""
    nc = _nc(bench)
    w1, w2 = usb_wcb(bench), _w2(bench)
    st, cfg = nc.wcb_status(), nc.config()
    rows = ncmesh.status_rows(st)
    problems = []
    net = cfg.get("wcbNetwork") or {}
    if (st.get("self"), st.get("quantity")) != (net.get("deviceId"), net.get("quantity")):
        problems.append(f"self/quantity {st.get('self')}/{st.get('quantity')}; the config says "
                        f"{net.get('deviceId')}/{net.get('quantity')}")
    for n, w in ((bench.usb_wcb_number(), w1), (2, w2)):
        r = rows.get(n)
        if not r:
            problems.append(f"GET_WCB_STATUS has no slot for W{n}")
            continue
        if (r["online"], r["known"], r["client"], r["temporary"]) != (1, 1, 0, 0):
            problems.append(f"W{n}: online/known/client/temporary {r['online']}/{r['known']}/{r['client']}/"
                            f"{r['temporary']}, expected 1/1/0/0")
        tok = token(bench.config_tokens(n, refresh=True), "?ALIAS,")
        alias = ncmesh.json_strip(tok[len("?ALIAS,"):])[:ncmesh.LABEL_MAX] if tok else ""
        if (r["alias"] or "") != alias:
            problems.append(f"W{n}: alias {r['alias']!r}, W{n} is {alias!r}")
        own = _wcb_view(w)
        mine = {p: v for p, v in _ifaces_of(own, n).items()}
        labels = r["labels"] or []
        got = {p + 1: ncmesh.wdp_scrub(v) for p, v in enumerate(labels) if v}
        if got != {p: ncmesh.json_strip(v) for p, v in mine.items()}:
            problems.append(f"W{n}: labels {got}, W{n} advertises {mine}")
        h, _, _ = _names(w)
        if r["seq"] != int(h, 16):
            problems.append(f"W{n}: seqHash {r['seq']}, W{n}'s ?SEQ,NAMES hash {h}")
    me = rows.get(st.get("self"))
    if me is not None and me["online"] != 1:
        problems.append("NaviCore's own slot is listed offline")
    assert not problems, "; ".join(problems)


def _seq_reply(nc, since, kind, wcb, timeout=9.0):
    """The first {"sys":1,"type":"<kind>",...,"wcb":<wcb>} line on NaviCore's USB since `since`, parsed, or None."""
    deadline = time.monotonic() + timeout
    while True:
        for x in nc.dev.since(since):
            if x.startswith(f'{{"sys":1,"type":"{kind}"'):
                try:
                    o = json.loads(x)
                except ValueError:
                    continue
                if o.get("wcb") == wcb:
                    return o
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


@test("ncmesh.seq_pull", "GET_WCB_SEQ returns each WCB's stored-sequence names and inventory hash as its own "
      "?SEQ,NAMES does, over USB and, for W2, over the bridge; GET_WCB_SEQVAL returns a sequence written for the test and "
      "NOTFOUND for a missing key; each refusal carries its text: 'busy' for a second pull while one is out, 'wcb out of "
      "range' for boards 0 and 21, 'key required', 'key too long', 'request rejected' for a key with a comma, and 'no "
      "reply' 6 s after asking a board that is not there", needs=["navicore", "wcb1", "wcb2"], links=[])
def seq_pull(bench):
    """startSeqPull (rc_telemetry.h:449-467): range, readiness, one-at-a-time and key checks answered at once
    (_seqReplyError :398-405), then requestSequenceNames / requestSequence, which broadcast the request and refuse a key
    with a comma ('request rejected'; WCB_Client.cpp:1231-1236, :1295-1298); a board that never answers gets 'no reply'
    after WCB_SEQ_TIMEOUT_MS, 6 s (rc_telemetry.h:334, :1559-1562) - asked here of an id nothing on the bench uses. An
    answer comes back through seqNamesReply / seqValueReply (:423-442) as WCB_SEQ {"hash","names":[...]} (buildWcbSeq
    :352-375: each name cut to 15 characters, then JSON-hostile characters dropped) or WCB_SEQVAL {"key","status",
    "value"} (0 OK, 1 NOTFOUND, 2 TOOBIG, :380-393); over the bridge the reply goes back fragmented (_seqDeliver
    :414-419, tick :1549-1554). Status 2 is not produced: a WCB answers TOOBIG only when the key, the value and a
    4-character header pass 2912 characters, 16 chunks of 182 (WCB.ino:4748-4754). A WCB's own ?SEQ,NAMES is
    '[MGMT:SEQ,n]<hash>,<count>,<names>' (s17 _names). The value test writes its own plain sequence on W2 inside
    config_guard(bench, 2); the bench's sequences' values are never pulled (a value could hold a credential and the
    bridged reply crosses W1's unredacted console) - names only. The test clears its sequence in a finally; config_guard
    only proves it (it fails a test that leaves one, run 20260929-025701). Every pull that reaches a WCB is asked once
    more after 'no reply' (_seq_ask; the busy pair and the bridged pull likewise): the WCB answers once, in broadcast
    frames, and a lost frame cost the first W2 pull its answer in run 20260929-042105 (tracker #109)."""
    nc, w1, w2 = _nc(bench), usb_wcb(bench), _w2(bench)
    me, nid = bench.usb_wcb_number(), _nid(nc)
    problems, notes = [], []
    usb = {}
    for n, w in ((me, w1), (2, w2)):
        h, count, names = _names(w)
        got = _seq_ask(w, n, nid, lambda n=n: nc.seq(n), SEQ_FRAG, notes)
        usb[n] = got
        want = [ncmesh.json_strip(x[:15]) for x in names]     # cut to 15, then stripped, as buildWcbSeq does
        notes.append(f"W{n}: {count} sequences")
        if got["names"] != want or got["hash"] != int(h, 16):
            problems.append(f"W{n}: GET_WCB_SEQ names {got['names'][:5]}... hash {got['hash']}; the board lists "
                            f"{want[:5]}... hash {int(h, 16)}")
    key, val = f"HILV{nonce()[:4]}", f";S3{marker('V')}"
    with config_guard(bench, 2):
        try:
            if not any(f"Stored: Key='{key}'" in x for x in w2.run(f"?SEQ,SAVE,{key},{val}", timeout=8)):
                raise AssertionError(f"W2 did not store the test sequence {key}")
            got = _seq_ask(w2, 2, nid, lambda: nc.seqval(2, key), SEQVAL_FRAG, notes)
            if (got["key"], got["status"], got["value"]) != (key, 0, val):
                problems.append(f"GET_WCB_SEQVAL 2 {key}: {got}, expected status 0 and the stored value")
            nokey = f"HILNO{nonce()[:6]}"
            missing = _seq_ask(w2, 2, nid, lambda: nc.seqval(2, nokey), SEQVAL_FRAG, notes)
            if missing["status"] != 1:
                problems.append(f"GET_WCB_SEQVAL of a missing key: status {missing['status']}, expected 1 (NOTFOUND)")
        finally:
            _seq_clear(w2, key)
    for ask in (1, 2):
        nm = nc.dev.mark()
        nc.dev.send(json.dumps({"type": "GET_WCB_SEQ", "wcb": me}, separators=(",", ":")))
        nc.dev.send(json.dumps({"type": "GET_WCB_SEQ", "wcb": 2}, separators=(",", ":")))
        first, second = _seq_reply(nc, nm, "WCB_SEQ", me), _seq_reply(nc, nm, "WCB_SEQ", 2)
        if ask == 1 and first and first.get("msg") == "no reply":
            notes.append(f"the busy pair's pull of W{me} got no reply (tracker #109); asked once more")
            continue
        break
    if not (first and first.get("ok")) or not second or second.get("ok") is not False or second.get("msg") != "busy":
        problems.append(f"two pulls back to back: {first and first.get('ok')} / {second}; expected the second 'busy'")
    for b in (0, 21):
        nm = nc.dev.mark()
        nc.dev.send(json.dumps({"type": "GET_WCB_SEQ", "wcb": b}, separators=(",", ":")))
        r = _seq_reply(nc, nm, "WCB_SEQ", b, timeout=3.0)
        if not r or r.get("ok") is not False or r.get("msg") != "wcb out of range":
            problems.append(f"GET_WCB_SEQ {b}: {r}, expected ok:false 'wcb out of range'")
    ghost = _free_id(nc, (17, 18, 16, 15))
    for obj, why, wait in (({"type": "GET_WCB_SEQVAL", "wcb": 2, "key": ""}, "key required", 3.0),
                           ({"type": "GET_WCB_SEQVAL", "wcb": 2, "key": "HILK" + "x" * 12}, "key too long", 3.0),
                           ({"type": "GET_WCB_SEQVAL", "wcb": 2, "key": "HIL,K"}, "request rejected", 3.0),
                           ({"type": "GET_WCB_SEQ", "wcb": ghost}, "no reply", 9.0)):
        nm = nc.dev.mark()
        t0 = time.monotonic()
        nc.dev.send(json.dumps(obj, separators=(",", ":")))
        r = _seq_reply(nc, nm, "WCB_SEQVAL" if "key" in obj else "WCB_SEQ", obj["wcb"], timeout=wait)
        took = time.monotonic() - t0
        if not r or r.get("ok") is not False or r.get("msg") != why:
            problems.append(f"{obj['type']} {obj['wcb']} key {obj.get('key')!r}: {r}, expected ok:false '{why}'")
        elif why == "no reply" and not 5.0 <= took <= 7.5:
            problems.append(f"'no reply' for board {ghost} came {took:.1f} s after the request; NaviCore waits 6 s")
    nid = _open_relay(nc, w1)
    for ask in (1, 2):
        nm, wm = nc.dev.mark(), w1.dev.mark()
        w1.send(f';W{nid},{{"type":"GET_WCB_SEQ","wcb":2}}')
        try:
            start = _nav_regex(nc, nm, r"\[RC\] WCB_SEQ send START: \d+ bytes \S+ (\d+) fragments to W\d+ "
                                       r"\(sid=(\d+)\)", 10, "'WCB_SEQ send START' line")
        except AssertionError as e:
            err = _json_since(w1, wm, lambda o: o.get("type") == "WCB_SEQ" and o.get("ok") is False)
            if ask == 1 and err and err[-1].get("msg") == "no reply":
                notes.append("the bridged pull of W2 got no reply (tracker #109); asked once more")
                continue
            problems.append(f"the bridged GET_WCB_SEQ 2: {e}" + (f"; W1 got {err[-1]}" if err else ""))
            break
        text = _await_reassembled(w1, nid, wm, int(start.group(2)), 10)
        o = json.loads(text) if text else None
        if not o or (o.get("names"), o.get("hash")) != (usb[2]["names"], usb[2]["hash"]):
            problems.append(f"the bridged GET_WCB_SEQ 2 came back {o and (len(o.get('names') or []), o.get('hash'))}, "
                            f"USB {len(usb[2]['names'])} names hash {usb[2]['hash']}")
        break
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("ncmesh.seqval_verbatim", "(should) GET_WCB_SEQVAL returns a stored sequence as W2 stores it: a value holding JSON "
      "quotes (a ;L command's body) arrives with its quotes, escaped, not stripped", needs=["navicore", "wcb1", "wcb2"],
      links=[])
def seqval_verbatim(bench):
    """NAVICORE.md D-NC46 (found writing NC-WP6). buildWcbSeqVal passes the value through _seqAppendJsonSafe, which
    drops every '"', backslash and control character rather than escaping it (rc_telemetry.h:340-346, :380-393; names
    and the key go the same way, :352-375), so a sequence holding JSON - a ;L WLED command's body, say - reaches the
    config tool with its quotes gone: the command library shows a value W2 does not hold, and one saved back through
    saveSequence would be stored altered. The comment at :377-379 says nothing there may reformat the value. Inside
    config_guard(bench, 2), W2 stores 'HILQ<n>' = ';L9,{"hil":"<nonce>"}' - never recalled, and WLED 9 exists nowhere -
    it is pulled over USB, and the test clears it in a finally (config_guard fails a test that leaves it).
    Recommendation: JSON-escape the value (and the names) instead of stripping it."""
    nc, w2 = _nc(bench), _w2(bench)
    nid, notes = _nid(nc), []
    key, val = f"HILQ{nonce()[:4]}", f';L9,{{"hil":"{nonce()}"}}'
    with config_guard(bench, 2):
        try:
            if not any(f"Stored: Key='{key}'" in x for x in w2.run(f"?SEQ,SAVE,{key},{val}", timeout=8)):
                raise AssertionError(f"W2 did not store the test sequence {key}")
            stored = _seqval(w2, key)
            got = _seq_ask(w2, 2, nid, lambda: nc.seqval(2, key), SEQVAL_FRAG, notes)
        finally:
            _seq_clear(w2, key)
    if notes:
        bench.note("; ".join(notes))
    assert stored == f"[MGMT:SEQVAL,2]{key},OK,{val}", f"W2 reads its own sequence back as {stored!r}"
    assert got["status"] == 0 and got["value"] == val, (f"(should, D-NC46) GET_WCB_SEQVAL returned {got['value']!r} for a "
                                                        f"sequence W2 stores as {val!r}: rc_telemetry.h:340-346 drops "
                                                        f"'\"' and '\\' instead of escaping them")


@test("ncmesh.telemetry_fields_rates", "The telemetry W1 relays: rc_hb every 2 s with NaviCore's id, firmware, mode and a "
      "full SBUS rate; rc_ch carrying the 24 channels NaviCore reads at chRateHz, 10 and 2 applied live; rc_ch stops "
      "15 s after the last JSON from a tool while W1's 20 s window still relays rc_hb", needs=["navicore", "wcb1"],
      links=[])
def telemetry_fields_rates(bench):
    """tick() (rc_telemetry.h:1654-1728): rc_hb every HB_INTERVAL_MS (2 s), best effort, always - {"sys":1,"type":"rc_hb",
    "id","fw","up","mode","model","sbusFps","sbusAge","sbusLost","sbusFail"}; rc_ch every 1000/chRateHz ms, read live from
    the config (:993-997), only while a tool has sent JSON in the last WCB_SUBSCRIPTION_MS (15 s, :1012-1017; renewed by
    any JSON but a fragment or an rc_* frame, :2063-2067). A PING forces both at once (:2193-2194). W1 relays either
    only inside its own 20 s window (WCB.ino:8019-8021), so NaviCore's 15 s gate is visible between 15 and 20 s after
    one PING. Host times are W1's lines as read (one read, up to ~100 ms, of stamping error). The channels are compared
    with #L09 within 3 counts (the controller's sticks rest). chRateHz is set inside nc_guard: rc_ch's rate only, nothing
    moves. rc_mode is left to navicore.set_mode: a mode change moves the dome."""
    nc, w1 = _nc(bench), usb_wcb(bench)
    fw, mode = nc.ping(), nc.mode()
    _require_full_rate(nc)
    problems, notes = [], []
    nid = _open_relay(nc, w1)

    def frames(since, kind):
        return [(ts, json.loads(x)) for ts, x in list(w1.dev.lines[since:])
                if x.startswith(f'{{"sys":1,"type":"{kind}"')]

    wm = w1.dev.mark()
    time.sleep(8.0)
    dump = nc.sbus_dump()
    hbs, chs = frames(wm, "rc_hb"), frames(wm, "rc_ch")
    gaps = [round(b[0] - a[0], 2) for a, b in zip(hbs, hbs[1:])]
    notes.append(f"8 s: {len(hbs)} rc_hb, gaps {gaps}; {len(chs)} rc_ch")
    if len(hbs) < 3 or any(not 1.6 <= g <= 2.5 for g in gaps):
        problems.append(f"rc_hb came {len(hbs)} times in 8 s, {gaps} s apart; expected every 2 s")
    for _, o in hbs[-1:]:
        want = {"id": nid, "fw": fw, "mode": mode, "sbusLost": False, "sbusFail": False}
        bad = {k: o.get(k) for k in want if o.get(k) != want[k]}
        if bad:
            problems.append(f"rc_hb fields {bad}, expected {({k: want[k] for k in bad})}")
        if abs((o.get("sbusFps") or 0) - dump["fps"]) > 15:
            problems.append(f"rc_hb sbusFps {o.get('sbusFps')}, #L09 {dump['fps']}")
    if chs:
        ch = chs[-1][1].get("ch") or []
        if len(ch) != 24 or any(abs(a - b) > 3 for a, b in zip(ch, dump["channels"])):
            problems.append(f"rc_ch carries {len(ch)} values; #L09 reads {dump['channels'][:8]}..., rc_ch {ch[:8]}...")
    else:
        problems.append("no rc_ch within 8 s of a bridged PING")
    with nc_guard(bench) as g:
        for hz in (10, 2):
            g.nc.set_config({"chRateHz": hz})
            _open_relay(g.nc, w1)
            time.sleep(1.0)
            wm = w1.dev.mark()
            time.sleep(5.0)
            n = len(frames(wm, "rc_ch"))
            notes.append(f"chRateHz {hz}: {n} rc_ch in 5 s")
            if not 0.7 * 5 * hz <= n <= 1.15 * 5 * hz + 1:
                problems.append(f"chRateHz {hz}: {n} rc_ch in 5 s, expected about {5 * hz}")
    _open_relay(nc, w1)
    t0 = time.monotonic()
    wm = w1.dev.mark()
    time.sleep(19.0)
    chs, hbs = frames(wm, "rc_ch"), frames(wm, "rc_hb")
    last_ch = chs[-1][0] - t0 if chs else None
    late_hb = [round(ts - t0, 1) for ts, _ in hbs if ts - t0 > 15.6]
    notes.append(f"one PING: last rc_ch at {last_ch and round(last_ch, 2)} s, rc_hb after 15.6 s at {late_hb}")
    if last_ch is None or not 14.0 <= last_ch <= 15.8:
        problems.append(f"after one PING the last rc_ch came {last_ch and round(last_ch, 2)} s later; NaviCore's gate "
                        f"is 15 s")
    if not late_hb:
        problems.append("no rc_hb after 15.6 s: W1's 20 s window did not stay open, so the 15 s gate is not shown")
    bench.note("; ".join(notes))
    assert not problems, "; ".join(problems)


@test("ncmesh.w_route_chains", "A WCB_SEND to W1 of ';W2;S3<text>' is routed on to W2 and written out of W2 S3 exactly "
      "once; a Maestro verb for W2's Maestro sent to W1 over the mesh is not routed on (a mesh command gets one hop), "
      "while the same verb sent to W2 reaches its Maestro", needs=["navicore", "wcb1"], links=["W2S3", "W2S1"])
def w_route_chains(bench):
    """NaviCore sends WCB_SEND's text verbatim to the target (NaviCore.ino:4063-4084). A WCB that receives
    ';W2;S3<text>' over the mesh routes it on as a unicast (the ;W route is not a broadcast, so the loop-prevention gate
    of WCB CLAUDE.md rule 3 does not stop it), and W2 writes <text> + CR out of S3 (the W2S3 probe). A Maestro verb for
    a device the receiving WCB does not host is dropped when it arrived over the mesh (a get-query such as getErrors,
    WCB_Maestro.cpp:395-404 and :566; any other verb, :490-497; docs/HIL_TESTING.md §6): ';M2,getErrors' sent to W1 must
    not reach Maestro 2 on W2 S1 (the listen-only tap), while sent to W2 it puts the getErrors frame AA 02 21 there
    (bench.json port_stimulus). getErrors reads and clears Maestro 2's error register and moves nothing."""
    l23, l21 = link(bench, 2, "S3"), link(bench, 2, "S1")
    nc = _nc(bench)
    me = bench.usb_wcb_number()
    problems = []
    t = marker("WR")
    pm = l23.mark()
    ack = nc.wcb_send(me, f";W2;S3{t}")
    got = _wait_all(l23, pm, [t], 3.0)
    time.sleep(1.0)
    n = _count(l23.received(pm), t)
    if not ack.get("ok") or n != 1:
        problems.append(f";W2;S3 through W1: ACK {ack}, {n} copies on W2 S3 (expected 1)")
    frame = bytes([0xAA, 0x02, 0x21])
    pm = l21.mark()
    nc.wcb_send(me, ";M2,getErrors")
    time.sleep(2.0)
    if frame in l21.received(pm):
        problems.append("a ;M2 verb sent to W1 over the mesh was routed on to Maestro 2 on W2")
    pm = l21.mark()
    nc.wcb_send(2, ";M2,getErrors")
    try:
        l21.expect(frame, timeout=3, since=pm)
    except AssertionError:
        problems.append(f"the control: ;M2,getErrors sent to W2 never put {frame.hex(' ')} on W2 S1")
    assert not problems, "; ".join(problems)
