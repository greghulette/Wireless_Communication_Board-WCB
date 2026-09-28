"""NaviCore over the mesh: JSON bridged through W1, the config tool's fragment envelopes built by hand, a WCB taken off
the air for a while, and the probe as one of NaviCore's mesh peers (docs/hil_plan/NAVICORE.md §3 INF6).

    r = bridged(w1, {"type": "PING"}, r'"type":"PONG"')       # r.match, r.lines (W1's console), r.sys (parsed)
    envs = fragments({"sys": 1, "type": "SET_CONFIG", ...}, sid=4242)
    send_fragments(w1, envs, order=[1, 0, 2])                  # the tool's pacing, any order, repeats allowed
    with deaf(bench, 2):                                       # W2 hears nothing until the block ends
        ...
    with probe_peer(bench, 16) as probe:                       # probe1 joined as WCB 16, NaviCore's window burnt
        probe.mesh_send(20, '{"type":"PING"}')

What every function here is shaped around:
- W1 prints NaviCore's JSON (every reply NaviCore sends it, and its broadcast telemetry) on its USB only while its RC
  relay window is open: 20 s from the last ';W20,{...}' typed on it (WCB.ino:7941-7943; the relay, :5454-5455 and :5762-5763). A
  bridged() send opens it, and a plain-text ';W20,<text>' never does. NaviCore answers a bridged request by unicast to
  the sender, "sys":1 first (rc_telemetry.h:2186-2195, the PONG). A reply too long for one packet comes back as
  fragment envelopes {"f","of","sid","s"} with no "sys" (rc_telemetry.h:556-595); reassemble() joins them.
- One mesh payload holds 187 bytes of JSON: WCB_Client appends '|CRC%08X' inside a 200-byte buffer, and an envelope
  over that loses its CRC and is dropped silently (NaviCore CLAUDE.md rule 2; config_tool/index.html:5501-5507). Longer
  JSON goes as fragments, which is what the config tool's sendJSON does (index.html:5645-5731). NaviCore joins at most
  FRAG_MAX_PARTS parts per session, in a pool of FRAG_POOL_SIZE sessions matched on (sid, sender), each reclaimed after
  FRAG_TIMEOUT_MS without a part (rc_telemetry.h:141-149, :192-227). An envelope with f < 1, of < 1, of > 192, f > of or
  sid 0 is dropped without a word, and so is one whose "of" disagrees with its session's (:2079-2091).
- A '?MAC,3,<octet>' typed on a WCB changes its receive filter at once while its radio address changes only at boot
  (WCB.ino:6515-6518; s18's _deaf_w1), so the board still transmits and hears nothing - and the octet is saved to NVS
  at once, so it is put back first thing, on the board's own console, and the board is never reset in between.
- NaviCore drops a mesh COMMAND whose sequence number it has already seen from that sender: a 32-wide window below the
  highest one heard (WCBClient WCB_Client.cpp:879-898, applied at :2891-2895). A probe restarts its sequence numbers at
  1 on every join, so a probe id already used since NaviCore's last boot can lose its first commands, ACKed and
  silent. A newer WCB_Client clears that window on the sender's boot announce (:2744-2769, sent at join :1819-1833),
  but only when both NaviCore's image and the probe's firmware carry it; probe_peer does not rely on it.

Credentials: bridged(), send_fragments() and the burn send no password. deaf() and probe_peer() read the mesh settings
from W1's config chain; nothing here notes or raises a line that could hold one (mesh_params keeps it in memory).
"""
import json
import math
import re
import secrets
import time
from collections import namedtuple
from contextlib import contextmanager

from .navicore import DBG_MAESTRO, NaviCore
from .wcb import WCB

NAVICORE_ID = 20              # NaviCore's WCB number on this bench (wcbNetwork.deviceId; the special-peer slot)
ENV_MAX_BYTES = 187           # FRAG_MAX_ENV_BYTES (config_tool/index.html:5507): an envelope's hard cap, UTF-8 bytes
ENV_TARGET_BYTES = 180        # FRAG_ENV_TARGET_BYTES (:5546): what the chunker aims at
ENV_OVERHEAD = 37             # FRAG_ENV_OVERHEAD (:5548): {"f":999,"of":999,"sid":99999,"s":""}
ENV_BUDGET = ENV_TARGET_BYTES - ENV_OVERHEAD     # 143 escaped bytes of slice per envelope (_fragChunks' budget)
MAX_PARTS = 192               # FRAG_MAX_PARTS (:5476) = rcTelemetry::FRAG_MAX_PARTS (rc_telemetry.h:141)
FRAG_POOL = 3                 # rcTelemetry::FRAG_POOL_SIZE (rc_telemetry.h:148)
FRAG_TIMEOUT_S = 5.0          # rcTelemetry::FRAG_TIMEOUT_MS (rc_telemetry.h:149), an idle timeout per session
PACE_FLOOR_S = 0.100          # FRAG_PACE_FLOOR_MS (index.html:5496)
PACE_LINK_MULT = 2            # FRAG_PACE_LINK_MULT (:5497)
LINK_BYTES_PER_MS = 11.52     # 115200 8N1 on the bridge WCB's UART0 (:5729)
RECV_MAX_PARTS = 512          # FRAG_MAX_PARTS_RECV (:5502): what the tool accepts from NaviCore
RELAY_WINDOW_S = 20.0         # W1 relays NaviCore's JSON to USB for this long after a ;W20,{...} (WCB.ino:7943)
REPLAY_WINDOW = 32            # NaviCore's per-sender COMMAND window (WCB_Client.cpp:890-896)

Reply = namedtuple("Reply", "match lines sys")
Reply.__doc__ = """bridged()'s answer: `match` the re.Match of the first W1 line matching the pattern (None when none
came in time), `lines` every W1 console line since the send, `sys` the {"sys":1,...} lines among them, parsed (a line
that does not parse is left out)."""


def compact(obj):
    """JSON text as the config tool's JSON.stringify writes it: no spaces, UTF-8 characters as they are."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _text(payload):
    text = payload if isinstance(payload, str) else compact(payload)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("the payload holds a lone surrogate: no UTF-8 text can carry it (JSON.stringify would escape "
                         "it to 6 bytes the tool's size estimate counts as 3)") from None
    return text


# ------------------------------------------------------------------ fragments (the config tool's sendJSON)
def esc_bytes(ch):
    """_fragEscBytes (index.html:5549-5555): what one code point costs once JSON-escaped, in UTF-8 bytes - 2 for '"' and
    backslash, 2 for the five short escapes (\\b \\t \\n \\f \\r), 6 for any other control character (\\u00XX), otherwise
    its UTF-8 length. For any text without a lone surrogate this is exactly what JSON.stringify and json.dumps write."""
    c = ord(ch)
    if c in (0x22, 0x5C) or c in (8, 9, 10, 12, 13):
        return 2
    if c < 0x20:
        return 6
    return 1 if c < 0x80 else 2 if c < 0x800 else 3 if c < 0x10000 else 4


def envelope(f, of, sid, s):
    """One fragment envelope, byte for byte what the tool sends: JSON.stringify({f, of, sid, s}) (index.html:5686). Not
    validated, so a test can build the malformed ones NaviCore must drop (f 0, f > of, sid 0, a changed "of")."""
    return compact({"f": f, "of": of, "sid": sid, "s": s})


def chunks(text):
    """_fragChunks (index.html:5556-5594), code point for code point: grow a slice while its escaped size stays within
    ENV_BUDGET (143), so the envelope around it stays within ENV_TARGET_BYTES (180) even with the worst-case header
    {"f":999,"of":999,"sid":99999}; then check the real envelope against the 187-byte cap and carry any code point that
    would pass it into the next slice. The slices join back into `text` exactly (iterating a str walks code points, as
    `for..of` does in JavaScript). Like the tool's, the last flush drops a carry it could not place; that happens only
    when the estimate undercounts, which it never does for text without a lone surrogate (fragments() checks)."""
    out, cur, esc = [], [], 0

    def flush():
        nonlocal cur, esc
        if not cur:
            return
        carry = []
        while cur and len(envelope(999, 999, 99999, "".join(cur)).encode("utf-8")) > ENV_MAX_BYTES:
            carry.insert(0, cur.pop())
        if cur:
            out.append("".join(cur))
        elif carry:                      # one code point alone over the cap: impossible (6 escaped + 37); never spin
            out.append(carry.pop(0))
        cur = carry
        esc = sum(esc_bytes(c) for c in cur)

    for ch in text:
        cost = esc_bytes(ch)
        if esc + cost > ENV_BUDGET and cur:
            flush()
        cur.append(ch)
        esc += cost
    flush()
    return out


def fragments(payload, sid, max_parts=MAX_PARTS):
    """The envelopes the config tool's sendJSON sends for `payload` (a dict or list, serialised as JSON.stringify does,
    or the exact text), as sid `sid` -> [envelope text], f = 1..n in order (index.html:5654-5690). The tool stamps
    "sys":1 first on everything it sends (:5617); pass it in the payload to match that. Each envelope is at most 187
    UTF-8 bytes. sid must be 1-65535: NaviCore drops sid 0 (its free-slot sentinel, rc_telemetry.h:2079) and the tool
    wraps 65535 back to 1 (index.html:5657). More than `max_parts` fragments is refused, as the tool refuses them
    (:5663): NaviCore's receive pool holds 192 parts a session (rc_telemetry.h:141); max_parts=None lifts the cap for a
    test that means to exceed it. Pace them with send_fragments()."""
    text = _text(payload)
    if not text:
        raise ValueError("an empty payload has no fragments (the tool sends nothing for one)")
    if not isinstance(sid, int) or not 1 <= sid <= 65535:
        raise ValueError(f"sid {sid!r}: the tool's sids run 1-65535, and NaviCore drops sid 0 as malformed")
    parts = chunks(text)
    if "".join(parts) != text:
        raise ValueError("the tool's chunker would drop part of this payload (a carry left after its last slice)")
    if max_parts is not None and len(parts) > max_parts:
        raise ValueError(f"{len(parts)} fragments: NaviCore reassembles at most {max_parts} per session "
                         f"(rc_telemetry.h:141), and the tool refuses such a send (index.html:5663)")
    envs = [envelope(i + 1, len(parts), sid, s) for i, s in enumerate(parts)]
    over = [i + 1 for i, e in enumerate(envs) if len(e.encode("utf-8")) > ENV_MAX_BYTES]
    if over:
        raise ValueError(f"envelope(s) {over[:5]} over {ENV_MAX_BYTES} bytes: the tool would abort this send (:5691)")
    return envs


def pace_s(env, prefix=f";w{NAVICORE_ID},"):
    """The tool's wait after sending envelope `env` (index.html:5728-5730): twice the line's wire time at 115200 8N1 on
    the bridge WCB's UART, but never under FRAG_PACE_FLOOR_MS (100 ms), which is what it always is for an envelope of
    187 bytes or less (a 193-byte line is 17 ms of wire). The floor keeps the bridge's 10-deep ETM pending table from
    saturating (:5710-5720)."""
    line_bytes = len(prefix) + len(env.encode("utf-8")) + 1
    wire_ms = math.ceil(line_bytes / LINK_BYTES_PER_MS)
    return max(wire_ms * PACE_LINK_MULT, PACE_FLOOR_S * 1000) / 1000


def send_fragments(w1, envelopes, order=None, target=NAVICORE_ID, gap_s=None, sleep=time.sleep):
    """Type each envelope on W1's console as ';W<target>,<envelope>', paced as the tool paces them (pace_s, nothing after
    the last) or `gap_s` apart -> W1's console mark taken before the first. `order` is a list of indexes into
    `envelopes`, repeats allowed, for out-of-order and duplicate delivery; None sends them in order. A fragment is not
    JSON NaviCore acts on, so it does not open W1's relay window: the reassembled message, or a bridged() send, does."""
    idx = list(range(len(envelopes))) if order is None else list(order)
    prefix = f";W{target},"
    m = w1.dev.mark()
    for n, i in enumerate(idx):
        env = envelopes[i]
        if len(env.encode("utf-8")) > ENV_MAX_BYTES:
            raise ValueError(f"envelope {i} is over {ENV_MAX_BYTES} bytes: W1 would send it with its CRC cut off")
        w1.send(prefix + env)
        if n < len(idx) - 1:
            sleep(pace_s(env, prefix) if gap_s is None else gap_s)
    return m


def reassemble(lines):
    """NaviCore's fragmented replies among `lines` (W1's console, or any text lines) -> [(sid, text)] of each session
    that completed, in completion order: the tool's receive side (index.html:9096-9134). An envelope with of outside
    1-512, f outside 1..of or a negative sid is ignored; a sid seen again with another "of" starts over; the first copy
    of a part wins; a session completes when every part is there."""
    sessions, done = {}, []
    for line in lines:
        if not line.startswith('{"f":'):
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        f, of, sid = msg.get("f"), msg.get("of"), msg.get("sid")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (f, of, sid)):
            continue
        if not 1 <= of <= RECV_MAX_PARTS or not 1 <= f <= of or sid < 0:
            continue
        sess = sessions.get(sid)
        if sess is None or sess["of"] != of:
            sess = sessions[sid] = {"of": of, "parts": {}}
        sess["parts"].setdefault(f, msg.get("s") or "")
        if len(sess["parts"]) >= of:
            done.append((sid, "".join(sess["parts"][k] for k in range(1, of + 1))))
            del sessions[sid]
    return done


# ------------------------------------------------------------------ bridged JSON through W1
def bridged(w1, obj, pattern=None, timeout=3.0, target=NAVICORE_ID):
    """Type ';W<target>,<json>' on W1's console (`w1` a hil.wcb.WCB) and wait up to `timeout` for a W1 line matching
    `pattern`, or, with no pattern, the whole `timeout` -> Reply(match, lines, sys). Returns rather than raises when
    nothing matched (match None), since several tests assert a reply never comes. `obj` is a dict (serialised compactly,
    key order kept - a bridged SET_CMDLIB cuts its data up to the message's last '}', rc_telemetry.h:1200-1210) or the
    exact text; it must be one line of at most 187 UTF-8 bytes (longer JSON goes as fragments()). The send also opens,
    or renews, W1's 20 s relay window. WCB.run() hides the {"sys":1 lines, so they are read off the console directly."""
    text = _text(obj)
    if "\n" in text or "\r" in text:
        raise ValueError("a bridged line must be one line: W1 ends a command at CR or LF")
    if len(text.encode("utf-8")) > ENV_MAX_BYTES:
        raise ValueError(f"{len(text.encode('utf-8'))} bytes of JSON: one mesh packet holds {ENV_MAX_BYTES} "
                         f"(NaviCore CLAUDE.md rule 2) - send it as fragments()")
    m = w1.dev.mark()
    w1.send(f";W{target},{text}")
    match = None
    if pattern is None:
        time.sleep(timeout)
    else:
        try:
            match = w1.dev.expect(pattern, timeout=timeout, since=m)
        except AssertionError:
            match = None
    lines = w1.dev.since(m)
    parsed = []
    for x in lines:
        if x.startswith('{"sys":1'):
            try:
                parsed.append(json.loads(x))
            except ValueError:
                pass
    return Reply(match, lines, parsed)


# ------------------------------------------------------------------ a WCB off the air
def _octet(bench, n):
    from .config import token
    t = token(bench.config_tokens(n), "?MAC,3,")
    if t is None:
        from .runner import Skip
        raise Skip(f"W{n}'s config chain has no ?MAC,3 token")
    return t[len("?MAC,3,"):].upper()


def _set_octet(w, octet):
    """'?MAC,3,<octet>' on WCB console `w` -> True when the board confirmed it (WCB.ino:6515-6518)."""
    out = w.run(f"?MAC,3,{octet}")
    return any(f"Updated 3rd MAC octet to 0x{octet}" in x for x in out)


@contextmanager
def deaf(bench, n):
    """W<n> hears nothing on the mesh for the block: its 3rd MAC octet flips (XOR 1) on its OWN USB console and flips
    back as the block ends, however it ends - a failure, a Skip or an abort. Its radio address is set at boot, so it
    still transmits; only reception stops (s18 _deaf_w1, s25). The octet is saved to NVS at once (WCB_Storage.cpp,
    saveMACPreferences), so W<n> must never restart inside the block, and a board with no USB console of its own is
    refused (Skip): a flip sent over the mesh would cut off the only way to send it back. Yields the board's hil.wcb.WCB.
    If the octet cannot be put back, the test fails with the command that restores it (never quoting a secret: the
    octet is not one)."""
    from .runner import Skip
    own = "wcb1" if n == bench.usb_wcb_number() else bench.usb_wcbs().get(n)
    if not own:
        raise Skip(f"W{n} has no USB console of its own: deafening it over the mesh would cut off the way back")
    w = WCB(bench.dev(own))
    orig = _octet(bench, n)
    other = "%02X" % (int(orig, 16) ^ 0x01)
    bench.note(f"W{n} deaf: ?MAC,3,{other} (if this run dies, type ?MAC,3,{orig} on W{n}'s own console)")
    failure, flipped = None, False
    try:
        if not _set_octet(w, other):
            raise AssertionError(f"W{n} did not take ?MAC,3,{other}")
        flipped = True
        yield w
    except BaseException as e:  # noqa: BLE001 - re-raised below, after the octet is back
        failure = e
    finally:
        problem = None
        for attempt in (1, 2):
            try:
                if _set_octet(w, orig):
                    problem = None
                    break
                problem = f"W{n} did not confirm ?MAC,3,{orig}"
            except AssertionError as e:        # a console that did not answer: try once more
                problem = f"W{n} did not answer ?MAC,3,{orig} ({str(e).splitlines()[0]})"
            time.sleep(0.5)
        if problem:
            bench.note(f"W{n} IS STILL DEAF - {problem}; type ?MAC,3,{orig} on W{n}'s own console")
        elif flipped:
            bench.note(f"W{n} hears again (?MAC,3,{orig})")
    if problem:
        msg = f"W{n} IS STILL DEAF - {problem}; type ?MAC,3,{orig} on W{n}'s own console"
        raise AssertionError((f"{failure}\n" if failure is not None else "") + msg)
    if failure is not None:
        raise failure


# ------------------------------------------------------------------ the probe as NaviCore's peer
def cmd_seq_dup(state, seq):
    """WCB_Client::_cmdSeqDup (WCB_Client.cpp:879-898) on `state`, a dict {'have', 'high', 'mask'} per sender -> True
    when the COMMAND with sequence number `seq` is dropped as a duplicate. The window's high is always a duplicate, a
    seen number up to 32 below it is one, an unseen one there is taken and marked, a newer one moves the window, and
    one more than 32 below is taken without a trace. Here for selftest.py's fake NaviCore, and as the rule
    burn_window() is built on."""
    if not state.get("have"):
        state.update(have=True, high=seq, mask=0)
        return False
    d = ((seq - state["high"] + 0x8000) & 0xFFFF) - 0x8000       # int16_t(seq - high)
    if d == 0:
        return True
    if d > 0:
        state["mask"] = 0 if d >= 32 else ((state["mask"] << d) | (1 << (d - 1))) & 0xFFFFFFFF
        state["high"] = seq
        return False
    back = -d
    if back <= 32:
        bit = 1 << (back - 1)
        if state["mask"] & bit:
            return True
        state["mask"] |= bit
    return False


def burn_window(nc, probe, device_id, target=NAVICORE_ID, window=REPLAY_WINDOW, min_sends=None, gap_s=0.05,
                settle_s=1.0, max_sends=None):
    """Send throwaway plain-text commands 'HILB<tag><k>' from the probe to NaviCore until its duplicate window for
    `device_id` can no longer drop the next one -> {'sent', 'dropped': [k...], 'tag'}.

    The window keeps the highest number H it heard from this id and the numbers up to 32 below it (cmd_seq_dup). The
    probe's k-th command after joining carries number k, and NaviCore always drops H itself when it comes round, so the
    last command NaviCore did not print (D) is H, or lies within 32 below it: once `window` + 1 more have been sent past
    D, the next number is above H, and every later one is new (+1 more, for a send the probe could not transmit, which
    still used a number: WCB_Client.cpp:2218-2219, :2306-2312). With nothing dropped, a window whose high was at most the
    number of burn sends would have shown itself (H is always dropped), and one far above lets the burn through as too
    old to remember (:897) and lets the test's commands through the same way. What stays unseen is a high just above the
    burn: an earlier session under this id whose last command NaviCore heard carried number min_sends+1 .. min_sends+32
    plus the test's own count, with nothing it heard in the burn's range. So the burn is 2 x (window + 1) = 66 sends, not
    the plan's 33: a session that sent 34-65 commands (W1 unicasts first, a broadcast last) is ordinary in this harness,
    one of 67+ is not. A command lost on the air looks like a drop, and only makes the burn longer.

    Seen on NaviCore's own console: with the DBG_MAESTRO bit, a plain-text mesh command prints '[WCB RX] from WCB<id>:
    <text>' (NaviCore.ino:3112-3113), and nothing else happens to it while no aux port has serialBcast out on (the
    bench's; probe_peer checks: queueSerialBroadcastOut, NaviCore.ino:2961-2971, called at :3100-3101). The flags go
    back to 0 afterwards (NaviCore.debug).
    AssertionError when NaviCore printed none of the first round (the probe does not reach it), or when the drops go on
    past `max_sends` (6 x (window + 1) by default)."""
    tag = "HILB" + secrets.token_hex(2).upper()
    floor = min_sends or 2 * (window + 1)
    cap = max_sends or 6 * (window + 1)
    rx = re.compile(rf"\[WCB RX\] from WCB{device_id}: {tag}(\d{{3}})\b")
    sent, goal, dropped = 0, floor, []
    with nc.debug(DBG_MAESTRO):
        m = nc.dev.mark()
        while True:
            while sent < goal:
                sent += 1
                probe.mesh_send(target, f"{tag}{sent:03d}")
                time.sleep(gap_s)
            time.sleep(settle_s)
            fm = nc.dev.mark()
            nc.dev.send("#L12")                      # releases a last line NaviCore holds back (HIL_TESTING.md §5)
            try:
                nc.dev.expect(r"Mode=\d+", timeout=3, since=fm)
            except AssertionError:
                pass
            seen = {int(g.group(1)) for x in nc.dev.since(m) for g in [rx.search(x)] if g}
            if not seen:
                raise AssertionError(f"NaviCore printed none of the {sent} burn commands from WCB{device_id}: the "
                                     f"probe does not reach it (not joined, or on another channel or password)")
            dropped = [k for k in range(1, sent + 1) if k not in seen]
            need = max(floor, (max(dropped) if dropped else 0) + window + 2)
            if sent >= need:
                return {"sent": sent, "dropped": dropped, "tag": tag}
            if need > cap:
                raise AssertionError(f"NaviCore's duplicate window for WCB{device_id} still drops the burn after "
                                     f"{sent} commands ({len(dropped)} dropped, last at {max(dropped)})")
            goal = need


@contextmanager
def probe_peer(bench, device_id, probe_name="probe1", target=NAVICORE_ID, **mesh):
    """probe_in_mesh (suites/common.py) plus burn_window() against NaviCore, so the test's first command to NaviCore
    is never dropped as a duplicate of an earlier session's -> the joined hil.probe.Probe, with `.burn` set to the burn's
    result. `mesh` overrides W1's mesh settings (password, checksum, channel...), as probe_in_mesh takes them.

    The quantity defaults to 20, not W1's ?WCBQ: WCB_Client registers the MACs of boards 1..quantity as ESP-NOW peers at
    begin() (WCB_Client.cpp:1643-1652) and transmits only to a registered one (:2349-2354), and the probe sets no
    special peer (wcb_probe/probe_main.cpp:1026-1031), so with the bench's quantity it could reach NaviCore only after
    learning it from two adverts - a peer it would then keep in its NVS for every later session. A test that passes
    another quantity must leave NaviCore inside it.

    An id NaviCore knows (GET_WCB_STATUS known[], which holds its learned peers even while they are silent) is refused:
    a temporary advert from a learned id makes WCB_Client forget it, an NVS write (WCB_Client.cpp:249-255, s21's
    navicore.probe_temp_peer_not_learned). So is a NaviCore with serialBcast out on for any aux port: it writes every
    plain-text mesh command out that port (NaviCore.ino:3100-3101), so the burn's 66 or more lines would reach whatever
    is wired there; a test that needs the flag sets it after the burn. The window burnt is NaviCore's only; a test that
    also commands W1 or W2 under this id burns their rings too (s22 _burn_ring)."""
    from .runner import Skip
    from suites.common import probe_in_mesh     # a lazy import: hil/ stays importable without the suites
    nc = NaviCore(bench.dev("navicore"))
    known = nc.wcb_status().get("known", [])
    if device_id <= len(known) and known[device_id - 1]:
        raise Skip(f"mesh id {device_id} is known to NaviCore (a learned peer, or a client heard in the last 180 s)")
    out = [p for p, v in (nc.config().get("serialBcast") or {}).items() if isinstance(v, dict) and v.get("out")]
    if out:
        raise Skip(f"NaviCore writes plain-text mesh commands out {', '.join(out)} (serialBcast out): the burn's lines "
                   f"would reach whatever is wired there")
    mesh = dict(mesh)
    mesh.setdefault("quantity", NAVICORE_ID)
    with probe_in_mesh(bench, probe_name, device_id, **mesh) as probe:
        time.sleep(1.0)                           # the join's own adverts and heartbeats first
        probe.burn = burn_window(nc, probe, device_id, target)
        bench.note(f"probe_peer WCB{device_id}: burnt NaviCore's window with {probe.burn['sent']} commands, "
                   f"{len(probe.burn['dropped'])} dropped")
        yield probe
