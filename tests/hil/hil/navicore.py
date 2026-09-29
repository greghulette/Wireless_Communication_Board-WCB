"""NaviCore driver: everything the suites say to NaviCore over its USB console (the ESP32-S3's native USB-Serial/JTAG),
and the parsers for what it prints. docs/hil_plan/NAVICORE.md §3 INF1.

NaviCore acts only on a line starting '?' or '#' (the CLI, execCliLine NaviCore.ino:3359), '{' (the JSON protocol,
:3800-4256) or exactly WCB_WEBTOOL_CONFIG_PULL; anything else is dropped silently (processInputLine :3773-3800), so a
bare PING never answers. Three things every method here is shaped around:

- NaviCore can hold a finished line unsent until more output follows it (docs/HIL_TESTING.md §5): the [MAE:n] markers,
  the #L09 block and ?REC replies all end in a newline, yet sat on its USB for seconds. So a reply whose last line
  matters is followed by '#L12', a harmless read whose 'Mode=' line both releases it and ends it (cli()).
- Some output is best effort: a PWM_UPDATE frame or the USB copy of an rc_trig is DROPPED when the USB TX ring is short
  of room (NaviCore.ino:3150-3158, :2233-2266), so a missing one is not proof it never happened.
- GET_CONFIG carries the mesh password and the AP password (rc_config.h:1226, :1354, :1367), ?backup prints ?EPASS,
  and GET_CMDLIB and ?REC replies can be large. A message raised here quotes counts and short status lines only, never
  a config line or a secret (_await, backup()). In session.log, Bench.log hashes the credentials on every line NaviCore
  sends or receives (runner.REDACT_KINDS; NAVICORE.md INF3, D-NC5).

Methods return parsed data and raise AssertionError for what the board did or did not say (a test FAILs on it),
ValueError for a bad argument (a test bug), and runner.Skip only in the helpers that pick a Maestro channel to use.
"""
import json
import re
import secrets
import time
from contextlib import contextmanager

from .checkpoint import redact_tokens
from .serialdev import usb_jtag_reset
from .wcb import dev_note

# SET_DEBUG_FLAGS bits: each gates one family of [DISPATCH] lines (NaviCore.ino:495-501). 0 silences them all.
DBG_MAESTRO, DBG_WCB, DBG_WLED, DBG_HCR, DBG_MP3, DBG_SERIAL, DBG_DFP = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40

# The last line of setup(): Serial.print("[NaviCore] Firmware "), FW_VERSION, " — setup complete." (NaviCore.ino:4916-
# 4918). Three writes, so searched, not anchored: another task's line can land in front of it.
BOOT_DONE = re.compile(r"\[NaviCore\] Firmware (\S+) .*setup complete\.")
# The ESP32-S3 ROM prints this when a cold power-up strapped it into download mode (NAVICORE.md §1.3): no app runs.
DOWNLOAD_MODE = "waiting for download"
# navicore_record.h _clipPath (:522-533) keeps only [A-Za-z0-9_-] of a clip name and at most 32 of them, silently: '?REC,RM,
# HIL x' would delete clip 'HILx'. A name the driver sends must already be one the board will not rewrite.
CLIP_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
# Recorder event kinds (navicore_record.h:28): an action, a Maestro keyframe, an HCR volume keyframe.
REC_ACTION, REC_KF_MAESTRO, REC_KF_HCRVOL = 0, 1, 2


# ------------------------------------------------------------------ pure helpers (selftest.py feeds them fixtures)
def fnv1a32(data):
    """FNV-1a 32 over bytes (a str is UTF-8 encoded), exactly rcCmdlibHash (rc_config.h:2159-2164): offset basis
    2166136261, prime 16777619, one byte at a time. NaviCore signs the stored command library with it (GET_CMDLIB,
    CMDLIB_META, the SET_CMDLIB ACK), and the recorder's range fingerprint uses the same loop (navicore_record.h:176)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    h = 2166136261
    for b in data:
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def entries(obj):
    """A GET_CONFIG collection as a list: the values of a dict (knobs, switches keyed by index), a list as it is,
    anything else empty."""
    return list(obj.values()) if isinstance(obj, dict) else (obj if isinstance(obj, list) else [])


def _kv(text):
    """'N=20,CLIENT=0,...' -> {'N': '20', 'CLIENT': '0', ...}: every value a string, as printed."""
    return dict(kv.split("=", 1) for kv in text.split(",") if "=" in kv)


def _json_or_none(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def parse_sbus_dump(lines):
    """The #L09 block (NaviCore.ino dumpSbusState :2885-2903) -> {'fps', 'variant', 'frames', 'age', 'lost',
    'failsafe', 'channels', 'text'}:
        ---- SBUS STATE ----
          variant=SBUS-24 (24 ch, 36-byte frame)
          frames=444673  fps=111  ageMs=4  lost=no  failsafe=no
          CH1-8:   992  992 ...           (one row per 8 of the detected channels)
    fps and age are ints (0 when absent); frames, variant, lost and failsafe stay the printed strings (None when
    absent), so two reads compare as they always did; 'text' is every line joined. Other lines in `lines` (the #L12
    flush, a stray [DBG]) are ignored."""
    joined = "\n".join(lines)

    def grab(rx):
        m = re.search(rx, joined)
        return m.group(1) if m else None
    channels = [int(v) for t in lines for hit in [re.match(r"^\s*CH\d+-\d+:\s+(.*)$", t)] if hit
                for v in hit.group(1).split()]
    return {"fps": int(grab(r"fps=(\d+)") or 0), "variant": grab(r"variant=(\S+)"), "frames": grab(r"frames=(\d+)"),
            "age": int(grab(r"ageMs=(\d+)") or 0), "lost": grab(r"lost=(\S+)"), "failsafe": grab(r"failsafe=(\S+)"),
            "channels": channels, "text": joined}


def parse_sbus_raw(lines):
    """The #L13 hex dump (NaviCore.ino:3633-3660) -> the last frame NaviCore parsed, as bytes; b'' when it says '(no
    frame parsed yet)'. The header names the length ('---- SBUS RAW ---- (36 bytes, SBUS-24)'), then rows of eight
    ('  [ 8] F0 81 AF 15 AD 68 45 2B '). AssertionError when the rows do not add up to that length."""
    size, data = None, {}
    for x in lines:
        if x.startswith("---- SBUS RAW ---- (no frame"):
            return b""
        m = re.match(r"^---- SBUS RAW ---- \((\d+) bytes", x)
        if m:
            size, data = int(m.group(1)), {}
            continue
        m = re.match(r"^\s*\[\s*(\d+)\]((?:\s+[0-9A-Fa-f]{2})+)\s*$", x)
        if m and size is not None:
            for k, h in enumerate(m.group(2).split()):
                data[int(m.group(1)) + k] = int(h, 16)
    if size is None:
        raise AssertionError("#L13 printed no '---- SBUS RAW ----' header")
    if sorted(data) != list(range(size)):
        raise AssertionError(f"#L13's rows hold {len(data)} bytes; its header says {size}")
    return bytes(data[i] for i in range(size))


def parse_mae(text, slot, q, ch=None):
    """A [MAE:<slot>] query marker in `text` -> the value (int) or the error word ('timeout', 'disabled', 'remote'), or
    None when there is none. NaviCore prints one per ?MAE,GET / MOVING / ERR (maestroReportQuery NaviCore.ino:741-756):
        [MAE:1]{"q":"pos","ch":0,"val":6400}   [MAE:1]{"q":"mov","val":0}   [MAE:1]{"q":"err","val":0}
        [MAE:9]{"q":"pos","ch":0,"err":"disabled"}   [MAE:0]{"q":"mov","err":"disabled"}
    q is 'pos' (then ch is required), 'mov' or 'err'. A remote slot prints nothing at once: its answer comes back over
    the mesh and is printed later (maePumpRemoteEmits)."""
    if q == "pos":
        rx = rf'\[MAE:{slot}\]\{{"q":"pos","ch":{ch},(?:"val":(\d+)|"err":"(\w+)")\}}'
    else:
        rx = rf'\[MAE:{slot}\]\{{"q":"{q}",(?:"val":(\d+)|"err":"(\w+)")\}}'
    m = re.search(rx, text)
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else m.group(2)


def parse_clip_items(lines):
    """?REC,LS's [CLIPITEM]{"name":"...","bytes":N,"dur":ms,"n":events} lines -> [dict], in the order printed
    (navicore_record.h listClips :960-982). n is the file header's event count."""
    return [json.loads(x[len("[CLIPITEM]"):]) for x in lines if x.startswith("[CLIPITEM]")]


def parse_pwm_update(line):
    """One monitor frame (NaviCore.ino sendPWMUpdate :3118-3160) -> dict: matrixCh, modeCh, matrixVal, modeVal, btn,
    mode, and sbus {ok, fps, frames, ageMs, lost, failsafe, chCount, frameLen, channels}. ValueError for anything but
    one whole frame whose channel list is as long as the stream's channel count."""
    if not line.startswith('{"type":"PWM_UPDATE"'):
        raise ValueError("not a PWM_UPDATE line")
    obj = json.loads(line)
    sb = obj.get("sbus")
    if not isinstance(sb, dict) or not isinstance(sb.get("channels"), list) \
            or len(sb["channels"]) != min(sb.get("chCount", -1), 24):
        raise ValueError("a PWM_UPDATE frame without a whole sbus block")
    return obj


def merge_mesh_stats(pages):
    """MESH_STATS pages -> {'self', 'upMs', 'agg', 'peers', 'complete', 'pages'}. Page 0 carries the uptime and the
    aggregate {sent, ackd, rty, fail, ung, bcast, recv}; every page carries positional rows [id, sent, ackd, rty, fail,
    ung, recv], merged here by board id; only the last page carries "last":1 (rc_telemetry.h buildMeshStatsPage
    :1361-1432). Over USB the pages arrive in one burst (NaviCore.ino:4219-4252); bridged through W1 they carry "sys":1
    first. 'complete' is False without a last page: the config tool then keeps its previous snapshot."""
    first = next((p for p in pages if p.get("pg") == 0), pages[0] if pages else {})
    return {"self": first.get("self"), "upMs": first.get("upMs"), "agg": first.get("agg", {}),
            "peers": {r[0]: r for p in pages for r in p.get("peers", [])},
            "complete": any(p.get("last") == 1 for p in pages), "pages": pages}


def parse_wdp(lines):
    """?WDP,DUMP -> {'rows', 'ifaces', 'cfg', 'count'}, every value the printed string (WCB_Client's WCB_Mgmt.h
    printWdpDump :221-262): the SELF row first ([WDP:N=20,...,PEER=3]), then one [WDP:N=...] row per neighbour
    (PEER=2 learned, 1 configured, 0 neither), [WDPIF:N=..,S=..,DEV=..] port labels, [WDPCFG:EN=1,AUTOJOIN=..,
    PEERS=..], and [WDP:END,count=N], where N counts the neighbours without SELF."""
    rows, ifaces, cfg, count = [], [], {}, None
    for text in lines:
        hit = re.match(r"^\[WDP:(N=.*)\]$", text)
        if hit:
            rows.append(_kv(hit.group(1)))
            continue
        hit = re.match(r"^\[WDPIF:(.*)\]$", text)
        if hit:
            ifaces.append(_kv(hit.group(1)))
            continue
        hit = re.match(r"^\[WDPCFG:(.*)\]$", text)
        if hit and not cfg:
            cfg = _kv(hit.group(1))
            continue
        hit = re.match(r"^\[WDP:END,count=(\d+)\]", text)
        if hit:
            count = int(hit.group(1))
    return {"rows": rows, "ifaces": ifaces, "cfg": cfg, "count": count}


def parse_boot(lines):
    """The lines since a restart -> {'complete', 'version', 'reset_code', 'reset', 'rtc', 'attempts', 'device_id',
    'quantity', 'download_mode'}; a field the lines lack is None (False for the flags). From setup() (NaviCore.ino
    printBootTelemetry :4367-4415, :4899-4918); a reason's name may hold parentheses, so it ends at the two spaces:
        Reset reason: 3 - Software restart (incl. boot-guard retry)  (RTC codes core0=3 [SW system] core1=3 [...])
        Boot attempts since power applied: 1
        [WCB] Joined network as device ID 20 (quantity=1)
        [NaviCore] Firmware v0.2.0_102105QSEP26 — setup complete.
    The banner can be lost to the USB re-enumeration a restart causes (wait_boot), so no field is guaranteed."""
    out = {"complete": False, "version": None, "reset_code": None, "reset": None, "rtc": None, "attempts": None,
           "device_id": None, "quantity": None, "download_mode": False}
    for x in lines:
        m = BOOT_DONE.search(x)
        if m:
            out["complete"], out["version"] = True, m.group(1)
        m = re.search(r"Reset reason: (\d+) - (.*?)  \(RTC codes core0=(\d+) \[[^\]]*\] core1=(\d+)", x)
        if m:
            out["reset_code"], out["reset"] = int(m.group(1)), m.group(2)
            out["rtc"] = (int(m.group(3)), int(m.group(4)))
        m = re.search(r"Boot attempts since power applied: (\d+)", x)
        if m:
            out["attempts"] = int(m.group(1))
        m = re.search(r"\[WCB\] Joined network as device ID (\d+) \(quantity=(\d+)\)", x)
        if m:
            out["device_id"], out["quantity"] = int(m.group(1)), int(m.group(2))
        if DOWNLOAD_MODE in x:
            out["download_mode"] = True
    return out


def parse_clip_range(lines, name, first, n):
    """The reply to ?REC,EDITLOAD,<name>,<first>,<n>,B -> (header, tail, {index: event}) (navicore_record.h editStream
    :731-879; the tool's _clipRangeFeed). Lines of an earlier request are skipped: BEGIN and END echo from, n and nm
    (the clip the buffer holds), and only ours count. Events come as [CLIPDL:EV,<i>]{json} (actions, and keyframes
    without batching) or [CLIPDL:EVB,<first>]{"e":[[t,k,a,b,c],...]}, consecutive keyframes from <first>, rebuilt here
    into the per-event shape ({t,k:1,slot,ch,pos} or {t,k:2,chan,vol}) the upload takes back. A line that does not
    parse leaves its index missing, to be asked again. AssertionError: the clip is not there, the firmware has no
    ranged download, the buffer holds fewer events than the file (fc != count: loadClip truncates silently), or the
    buffer changed during the range (fp differs between BEGIN and END)."""
    hdr = tail = None
    events = {}

    def ours(o):
        return isinstance(o, dict) and o.get("from") == first and o.get("n") == n and o.get("nm", name) == name
    for x in lines:
        if x.startswith(f"[REC] clip '{name}' not found"):
            raise AssertionError(f"clip {name!r} is not on NaviCore's clips partition")
        if x.startswith("[CLIPDL:ERR]"):
            raise AssertionError(f"EDITLOAD {name}: {x[len('[CLIPDL:ERR]'):][:120]}")
        if x.startswith("[CLIPDL:BEGIN]"):
            h = _json_or_none(x[len("[CLIPDL:BEGIN]"):])
            if isinstance(h, dict) and "from" not in h:
                raise AssertionError("EDITLOAD answered without from/n: this NaviCore has no ranged download")
            if ours(h):
                hdr, tail, events = h, None, {}
            continue
        if hdr is None or tail is not None:
            continue                       # before our BEGIN, or after our END
        if x.startswith("[CLIPDL:END]"):
            t = _json_or_none(x[len("[CLIPDL:END]"):])
            if ours(t):
                tail = t
            continue
        mb = re.match(r"^\[CLIPDL:EVB,(\d+)\](.*)$", x)
        me = mb or re.match(r"^\[CLIPDL:EV,(\d+)\](.*)$", x)
        if not me:
            continue
        idx, body = int(me.group(1)), _json_or_none(me.group(2))
        if body is None:
            continue
        if mb:
            for r, q in enumerate(body.get("e", []) if isinstance(body, dict) else []):
                if first <= idx + r < first + n:
                    events[idx + r] = ({"t": q[0], "k": REC_KF_HCRVOL, "chan": q[2], "vol": q[3]} if q[1] == REC_KF_HCRVOL
                                       else {"t": q[0], "k": q[1], "slot": q[2], "ch": q[3], "pos": q[4]})
        elif first <= idx < first + n:
            events[idx] = body
    if hdr is None:
        raise AssertionError(f"EDITLOAD {name},{first},{n}: no [CLIPDL:BEGIN] for this range")
    if tail is None:
        raise AssertionError(f"EDITLOAD {name},{first},{n}: no [CLIPDL:END] for this range")
    if hdr.get("fc") != hdr.get("count"):
        raise AssertionError(f"clip {name!r} is truncated on the board: its file holds {hdr.get('fc')} events, the "
                             f"buffer {hdr.get('count')}")
    if tail.get("fp") != hdr.get("fp"):
        raise AssertionError(f"clip {name!r} changed on the board during events {first}-{first + n - 1} "
                             f"(fingerprint {hdr.get('fp')} -> {tail.get('fp')})")
    return hdr, tail, events


def _clip_name(name):
    if not CLIP_NAME.fullmatch(name or ""):
        raise ValueError(f"clip name {name!r}: NaviCore keeps only [A-Za-z0-9_-] and 32 characters of a name "
                         f"(navicore_record.h _clipPath), so this one would address a different clip")
    return name


# A healthy SBUS stream reads 99 to 111 frames a second in #L09 (fps is counted over a window, so a 100 Hz stream
# reads 99 or 100), and a stalled or failsafe one far below. Every "full rate" check uses this: a bare >= 100 skipped
# nccfg.monitor_stream on a healthy 99 (run 20260928-123843).
SBUS_FULL_FPS = 90


class NaviCore:
    def __init__(self, dev):
        self.dev = dev

    # ------------------------------------------------------------ transport
    def json_cmd(self, obj, reply_pattern, timeout=3.0):
        m = self.dev.mark()
        self.dev.send(json.dumps(obj, separators=(",", ":")))
        return self.dev.expect(reply_pattern, timeout=timeout, since=m)

    def send_paced(self, line, chunk=512, gap_s=0.004):
        """One line in `chunk`-byte writes `gap_s` apart, as the config tool writes anything over 512 bytes
        (index.html sendLine: USB_CHUNK 512, 4 ms). NaviCore's USB RX ring is 8 KB (NaviCore.ino:4500) and loop()
        drains it only between its other work, so a SET_CONFIG written at once overflows it: bytes vanish from the
        middle and the line fails to parse. A transport without paced writes (a WebSocket, INF5) sends it whole."""
        paced = getattr(self.dev, "send_paced", None)
        if paced is None:
            return self.dev.send(line)
        return paced(line, chunk=chunk, gap_s=gap_s)

    def cli(self, cmd, until=None, flush=True, timeout=3.0):
        """One console line ('?...' or '#...') -> the lines printed from it on.
        flush=True: '#L12' follows it, and the wait ends at `until` or, with none, at the flush's own 'Mode=' line,
        which also releases a reply NaviCore held back (module docstring). Lines as received.
        flush=False: the wait ends at `until` (required), then 0.3 s for the lines a multi-line reply prints after the
        one it matches (#L11, #L13); lines right-stripped, for whole-line comparisons."""
        if not flush and until is None:
            raise ValueError("cli(flush=False) needs `until`: nothing else ends the reply")
        m = self.dev.mark()
        self.dev.send(cmd)
        if flush:
            self.dev.send("#L12")
            self.dev.expect(until or r"Mode=\d+", timeout=timeout, since=m)
            return self.dev.since(m)
        self.dev.expect(until, timeout=timeout, since=m)
        time.sleep(0.3)
        return [x.rstrip() for x in self.dev.since(m)]

    def lines(self, since, pattern):
        """The console lines after mark `since` that match `pattern`."""
        rx = re.compile(pattern)
        return [x for x in self.dev.since(since) if rx.search(x)]

    def _await(self, since, test, timeout, what):
        """Poll the lines after `since` until test(line) returns something other than None -> that. The message names
        `what` and a line count, never a line: the lines may hold the config or ?EPASS."""
        deadline = time.monotonic() + timeout
        i = since
        while True:
            for x in self.dev.since(i):
                i += 1
                got = test(x)
                if got is not None:
                    return got
            if time.monotonic() >= deadline:
                raise AssertionError(f"{self.dev.name}: no {what} within {timeout:g} s "
                                     f"({i - since} lines since, not quoted)")
            time.sleep(0.02)

    def ack_line(self, obj, timeout=3.0):
        """The first {"type":"ACK"... line after sending `obj`, right-stripped, for the byte-exact comparisons the
        suites make ('{"type":"ACK","ok":true}')."""
        return self.json_cmd(obj, r'^\{"type":"ACK"', timeout=timeout).string.rstrip()

    def ack(self, obj, of=None, timeout=3.0):
        """Send `obj` -> its ACK as a dict. With `of`, only an ACK naming that command counts. Only SET_CONFIG,
        SET_CMDLIB, TEST_ACTION, FORGET_PEER and RESET_MESH_STATS name theirs; START/STOP_MONITOR, CALIB,
        RESET_DEFAULTS, TRIGGER, WCB_SEND, SET_DEBUG_FLAGS and REBOOT answer a bare {"type":"ACK","ok":...}
        (NaviCore.ino:3914-4217). ok:false is returned, not raised: several tests expect it."""
        pattern = rf'^\{{"type":"ACK","of":"{re.escape(of)}"' if of else r'^\{"type":"ACK"'
        got = self.json_cmd(obj, pattern, timeout=timeout)
        try:
            return json.loads(got.string)
        except ValueError:
            raise AssertionError(f"{obj.get('type')}: its ACK does not parse: {got.string[:160]!r}") from None

    def ping(self):
        got = self.json_cmd({"type": "PING"}, r'^\{"type":"PONG","version":"([^"]+)"')
        return got.group(1)

    # ------------------------------------------------------------ config
    def config(self, raw=False, timeout=10.0):
        """GET_CONFIG's data: a dict, or with raw=True the exact JSON text NaviCore printed, for byte comparisons (the
        INF3 guard). One line of tens of KB that holds the mesh and AP passwords: never quote it. rcConfigToJSON
        prints a top-level {"type":"ERROR"...} instead when it overflowed (NaviCore.ino:3845-3859); this then times
        out, as it always did. raw=True checks the text parses: the 8 KB TX ring drops the tail of a write the host
        does not drain in 50 ms (:4500-4530)."""
        got = self.json_cmd({"type": "GET_CONFIG"}, r'^\{"type":"CONFIG","data":', timeout=timeout)
        if not raw:
            return json.loads(got.string)["data"]
        text = got.string
        body = text[got.end():-1] if text.endswith("}") else None
        if body is None or _json_or_none(body) is None:
            raise AssertionError(f"GET_CONFIG: the CONFIG line ({len(text)} chars) is not whole - cut short in "
                                 f"NaviCore's USB TX ring?")
        return body

    def set_config(self, data, save_id=None, check=True, timeout=15.0):
        """SET_CONFIG with `data` (a dict, or the exact JSON text of one) -> the ACK as a dict, paced (send_paced).
        The ACK echoes the saveId (NaviCore.ino:3917-3958); one carrying another saveId answers an earlier save and is
        skipped, as the config tool's stale-ACK gate does. One without a saveId means the line did not parse, so it
        counts. check=True raises unless ok:true. Every SET_CONFIG rewrites /config.json (~14 KB of LittleFS), so
        tests send it only inside hil/nc_guard.py's nc_guard (INF3)."""
        text = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
        if "\n" in text or "\r" in text:
            raise ValueError("SET_CONFIG data must be one line: NaviCore ends a line at CR or LF")
        sid = save_id if save_id is not None else secrets.randbelow(2 ** 31 - 2) + 1

        def ours(x):
            if not x.startswith('{"type":"ACK","of":"SET_CONFIG"'):
                return None
            a = _json_or_none(x)
            return a if isinstance(a, dict) and a.get("saveId", sid) == sid else None
        m = self.dev.mark()
        self.send_paced(f'{{"type":"SET_CONFIG","saveId":{sid},"data":{text}}}')
        ack = self._await(m, ours, timeout, f"SET_CONFIG ACK for saveId {sid}")
        if check and not ack.get("ok"):
            raise AssertionError(f"SET_CONFIG (saveId {sid}) refused: {ack.get('msg')}")
        return ack

    def reset_defaults(self, check=True):
        """RESET_DEFAULTS -> its ACK. RAM only (rcConfigLoadDefaults saves nothing, rc_config.h:801) and, over USB,
        without applyConfigSideEffects (NaviCore.ino:3989-3992): the next save then persists every default, the
        compile-time mesh password (rc_config.h:957) included, and until a reboot the raw-packet paths (RTERM, OTA,
        WcbMgmt) use that password while the ETM stack keeps the boot copy (NAVICORE.md D-NC16, D-NC17)."""
        ack = self.ack({"type": "RESET_DEFAULTS"})
        if check and not ack.get("ok"):
            raise AssertionError(f"RESET_DEFAULTS refused: {ack}")
        return ack

    def cmdlib(self, timeout=10.0):
        """GET_CMDLIB -> the stored command library, exactly as stored (bytes), checked against the size and FNV-1a
        hash NaviCore sends with it; with none stored it sends {"boards":[],"enums":{}} (NaviCore.ino:3861-3870).
        A mismatch raises: the reply was cut short in the TX ring or re-encoded on the way."""
        got = self.json_cmd({"type": "GET_CMDLIB"}, r'^\{"type":"CMDLIB","size":(\d+),"hash":(\d+),"data":',
                            timeout=timeout)
        size, want = int(got.group(1)), int(got.group(2))
        text = got.string
        raw = text[got.end():-1].encode("utf-8") if text.endswith("}") else b""
        if len(raw) != size or fnv1a32(raw) != want:
            raise AssertionError(f"GET_CMDLIB: {len(raw)} bytes with hash {fnv1a32(raw)} arrived; NaviCore sent {size} "
                                 f"bytes with hash {want}")
        return raw

    def cmdlib_meta(self):
        """GET_CMDLIB_META -> (size, hash) of the stored library; (0, 0) when none is stored (NaviCore.ino:3872-3877)."""
        got = self.json_cmd({"type": "GET_CMDLIB_META"}, r'^\{"type":"CMDLIB_META","size":(\d+),"hash":(\d+)\}')
        return int(got.group(1)), int(got.group(2))

    def set_cmdlib(self, raw, check=True, timeout=10.0):
        """SET_CMDLIB with `raw` (bytes or str: one JSON object or array, on one line) -> the ACK as a dict, paced.
        NaviCore stores the bracket-matched value after "data": verbatim (NaviCore.ino:3879-3915), so `data` goes last
        and the stored bytes are `raw` stripped. check=True raises unless ok:true with the size and FNV-1a hash of
        those bytes. It rewrites /cmdlib.json: tests send it only inside hil/nc_guard.py's nc_guard (INF3)."""
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        body = text.strip()
        if "\n" in text or "\r" in text:
            raise ValueError("SET_CMDLIB data must be one line: NaviCore ends a line at CR or LF")
        if not body.startswith(("{", "[")) or _json_or_none(body) is None:
            raise ValueError("SET_CMDLIB data must be one JSON object or array")
        m = self.dev.mark()
        self.send_paced('{"type":"SET_CMDLIB","data":' + body + "}")
        ack = self._await(m, lambda x: _json_or_none(x) if x.startswith('{"type":"ACK","of":"SET_CMDLIB"') else None,
                          timeout, "SET_CMDLIB ACK")
        want = (len(body.encode("utf-8")), fnv1a32(body))
        if check and (not ack.get("ok") or (ack.get("size"), ack.get("hash")) != want):
            raise AssertionError(f"SET_CMDLIB: ACK ok={ack.get('ok')} size {ack.get('size')} hash {ack.get('hash')}; "
                                 f"sent {want[0]} bytes with hash {want[1]}")
        return ack

    fnv1a32 = staticmethod(fnv1a32)

    # ------------------------------------------------------------ live state
    def set_debug_flags(self, flags):
        self.json_cmd({"type": "SET_DEBUG_FLAGS", "flags": flags}, r'^\{"type":"ACK","ok":true')

    @contextmanager
    def debug(self, flags):
        """SET_DEBUG_FLAGS `flags` (DBG_* bits) for the block, then 0 whatever happened. RAM only, cleared at boot."""
        self.set_debug_flags(flags)
        try:
            yield
        finally:
            self.set_debug_flags(0)

    def mode(self):
        """The active trigger mode, 1-3, from #L12 ('Mode=<m>  matrixBtn=<b>  matrixVal=<v>', NaviCore.ino:3627)."""
        m = self.dev.mark()
        self.dev.send("#L12")
        return int(self.dev.expect(r"Mode=(\d+)", timeout=3, since=m).group(1))

    def calib(self, on):
        """CALIB on/off -> its ACK. On mutes every action dispatch and passthrough, and drops a recording in progress
        unsaved (NaviCore.ino:3972-3987); STOP_MONITOR and PING clear it again (:3835-3838, :3965-3970)."""
        return self.ack({"type": "CALIB", "on": bool(on)})

    def monitor(self, seconds, timeout=3.0):
        """START_MONITOR, `seconds` of frames, STOP_MONITOR -> [PWM_UPDATE dict] (parse_pwm_update), oldest first.
        A frame goes out every 50 ms (WS_MONITOR_INTERVAL_MS, NaviCore.ino:506) only when the USB TX ring has room
        for it, and is dropped otherwise, so there are at most seconds x 20. STOP_MONITOR also ends a calibration
        (:3965-3970). A frame that does not parse is left out and counted in session.log."""
        m = self.dev.mark()
        self.ack({"type": "START_MONITOR"}, timeout=timeout)
        time.sleep(seconds)
        self.ack({"type": "STOP_MONITOR"}, timeout=timeout)
        frames, bad = [], 0
        for x in self.dev.since(m):
            if x.startswith('{"type":"PWM_UPDATE"'):
                try:
                    frames.append(parse_pwm_update(x))
                except ValueError:
                    bad += 1
        if bad:
            dev_note(self.dev, f"monitor: {bad} PWM_UPDATE line(s) did not parse (cut short or interleaved), left out")
        return frames

    def trigger(self, mode, btn, tap, timeout=3.0):
        """USB TRIGGER -> (ACK dict, the rc_trig dict or None). NaviCore prints '[TRIGGER] ...', dispatches (rcDispatch
        emits the rc_trig first) and ACKs (NaviCore.ino:4028-4038); a bad mode, btn or tap gets ok:false and no rc_trig.
        The USB rc_trig is dropped when the TX ring is short of room (:2233-2266), so None alone proves nothing.
        A mapped slot runs its actions: tests trigger unmapped slots unless they mean to."""
        m = self.dev.mark()
        ack = self.ack({"type": "TRIGGER", "mode": mode, "btn": btn, "tap": tap}, timeout=timeout)
        trig = next((o for _, o in self.rc_events(m) if (o.get("mode"), o.get("btn"), o.get("tap")) == (mode, btn, tap)),
                    None)
        return ack, trig

    def test_action(self, action, timeout=3.0):
        """TEST_ACTION: fire one action object now, bypassing the calibration and replay gates -> its ACK
        (NaviCore.ino:3994-4004)."""
        return self.ack({"type": "TEST_ACTION", "action": action}, of="TEST_ACTION", timeout=timeout)

    def wcb_send(self, target, cmd, timeout=3.0):
        """WCB_SEND `cmd` to board `target` (0 = broadcast) -> its ACK; ok reflects whether WCB_Client sent it
        (NaviCore.ino:4040-4061). Never leave `target` out: it defaults to 0, a broadcast."""
        return self.ack({"type": "WCB_SEND", "target": target, "cmd": cmd}, timeout=timeout)

    def rc_events(self, since, kind="rc_trig", btn=None):
        """[(timestamp, object)] of the '"type":"<kind>"' JSON lines after mark `since` (rc_trig, rc_mode, ...), for
        rc_trig optionally one button slot; timestamps are the host's (SerialDevice.lines)."""
        return [(ts, json.loads(x)) for ts, x in list(self.dev.lines[since:])
                if f'"type":"{kind}"' in x and (btn is None or f'"btn":{btn},' in x)]

    def sbus_dump(self):
        """#L09 with the #L12 poke -> parse_sbus_dump's dict. NaviCore holds a finished block until more output
        follows it (module docstring), so the harmless #L12 releases it: without it the block sat unsent and the wait
        timed out while the block arrived the moment the NEXT test wrote. Measured at 3.07 s held (run
        20260916-135810, sbus.btn_hold_unconfigured_long: the 32.838 s block and the 35.843 s one both landed at
        35.905)."""
        m = self.dev.mark()
        self.dev.send("#L09")
        time.sleep(0.15)
        self.dev.send("#L12")
        self.dev.expect(r"^\s*CH17-24:", timeout=3, since=m)
        return parse_sbus_dump(self.dev.since(m))

    def sbus_state(self, timeout=3.0):
        """#L09 without the #L12 poke -> {'fps': int, 'lost': str, 'channels': [24 ints]}; ends at the CH17-24 row. It
        can time out on a held block, which sbus_dump() cannot; s11's sbus.to_navicore still reads it this way."""
        m = self.dev.mark()
        self.dev.send("#L09")
        self.dev.expect(r"^\s*CH17-24:", timeout=timeout, since=m)
        d = parse_sbus_dump(self.dev.since(m))
        return {"fps": d["fps"], "lost": d["lost"] if d["lost"] is not None else "?", "channels": d["channels"]}

    def sbus_raw(self):
        """#L13 -> the last SBUS frame NaviCore parsed (parse_sbus_raw); hil.sbus.decode() reads it."""
        return parse_sbus_raw(self.cli("#L13"))

    def wcb_status(self):
        got = self.json_cmd({"type": "GET_WCB_STATUS"}, r'^\{"type":"WCB_STATUS".*\}$')
        return json.loads(got.group(0))

    def online_ids(self):
        st = self.wcb_status()
        return {i + 1 for i, v in enumerate(st["online"]) if v} | {st["self"]}

    # ------------------------------------------------------------ Maestro (NaviCore.ino:3383-3439)
    @staticmethod
    def local_slots(cfg):
        """[(slot, device)] for NaviCore's type-1 (local) Maestro slots in GET_CONFIG `cfg`."""
        return [(i + 1, m.get("device")) for i, m in enumerate(cfg.get("maestros", [])) if m.get("type") == 1]

    @staticmethod
    def local_devices(cfg):
        """What GET_CONFIG `cfg` routes to NaviCore's OWN aux ports S3-S5, as the firmware derives it for the port labels
        and the broadcast fan-out (rcSerialLabelAuto, rc_config.h:1940-1950; auxPortHasDevice, NaviCore.ino:2946-2956):
        an HCR, MP3 Trigger or DFPlayer whose destination transport is "serial", and a configured WLED slot with a port
        3-5 and no WCB -> ['HCR on S3', 'WLED 2 on S5', ...]."""
        out = [f"{k[:-4].upper()} on {d.get('port')}" for k in ("hcrDest", "mp3Dest", "dfpDest")
               for d in [cfg.get(k) or {}] if d.get("transport") == "serial"]
        out += [f"WLED {w.get('id')} on S{w.get('port')}" for w in cfg.get("wledSlots") or []
                if w.get("configured") and w.get("wcb") == 0 and w.get("port") in (3, 4, 5)]
        return out

    def mae_get(self, slot, ch):
        """?MAE,GET,<slot>,<ch> (Get Position) -> int position (quarter-us), or the error word ('timeout',
        'disabled'). A local slot answers synchronously off Serial2 within 25 ms (maestroLocalQuery :713-733)."""
        text = "\n".join(self.cli(f"?MAE,GET,{slot},{ch}"))
        got = parse_mae(text, slot, "pos", ch)
        if got is None:
            raise AssertionError(f"?MAE,GET,{slot},{ch} printed {text!r}")
        return got

    def mae_moving(self, slot):
        """?MAE,MOVING,<slot> (Get Moving State) -> 0/1, or the error word."""
        text = "\n".join(self.cli(f"?MAE,MOVING,{slot}"))
        got = parse_mae(text, slot, "mov")
        if got is None:
            raise AssertionError(f"?MAE,MOVING,{slot} printed {text!r}")
        return got

    def mae_err(self, slot):
        """?MAE,ERR,<slot> (Get Errors) -> the error register, or the error word. Reading it CLEARS the Maestro's
        errors (NaviCore.ino:3393)."""
        text = "\n".join(self.cli(f"?MAE,ERR,{slot}"))
        got = parse_mae(text, slot, "err")
        if got is None:
            raise AssertionError(f"?MAE,ERR,{slot} printed {text!r}")
        return got

    def mae_set(self, slot, ch, pos):
        """?MAE,<slot>,<ch>,<pos>: setTarget, fire and forget, flushed -> the lines printed (only [DISPATCH] lines
        under DBG_MAESTRO). A local slot moves its servo; a remote one broadcasts the Pololu frame on the mesh."""
        return self.cli(f"?MAE,{slot},{ch},{pos}")

    def mae_free(self, slot, ch):
        """?MAE,FREE,<slot>,<ch>: speed 0 and accel 0 on that channel (the timeline preview), flushed -> the lines.
        It rewrites the channel's easing until the next config apply or reboot."""
        return self.cli(f"?MAE,FREE,{slot},{ch}")

    def usable_slot(self, cfg, channel=0):
        """(slot, device, position) of the first local Maestro that answers ?MAE,GET on `channel`, or Skip."""
        from .runner import Skip
        for slot, dev in self.local_slots(cfg):
            pos = self.mae_get(slot, channel)
            if isinstance(pos, int):
                return slot, dev, pos
        raise Skip("NaviCore has no local Maestro slot that answers ?MAE,GET")

    def undriven_channel(self, cfg):
        """(slot, device, channel, position) on a local Maestro slot for a channel NO Maestro-passthrough knob output
        drives, or Skip. A passthrough output re-dispatches on every global mode change, and one with releaseIdleMs > 0
        then arms NaviCore's idle auto-release for that channel (NaviCore.ino processKnobs / maestroIdleReleaseTick):
        any later mesh setTarget there goes limp releaseIdleMs after it, and the arm lasts in RAM until reboot or
        SET_CONFIG. On this bench J2 drives slot 1 ch 0 with a 1500 ms release, and a SET_MODE from sbus.trim_exact in
        an earlier run armed it (full run 20260922-215719). A driven channel can also be moved by the stick at any
        time."""
        from .runner import Skip
        for slot, dev in self.local_slots(cfg):
            driven = {o.get("maestroCh") for k in entries(cfg.get("knobs")) if k.get("function") == 1
                      for key in ("outputs", "outputs2", "outputs3") for o in (k.get(key) or [])
                      if o.get("target") == slot}
            for ch in sorted(set(range(0, 24)) - driven):
                pos = self.mae_get(slot, ch)   # not an int: timeout, or a channel not in servo mode
                if isinstance(pos, int):
                    return slot, dev, ch, pos
                break                          # the slot does not answer - try the next one
        raise Skip("NaviCore has no local Maestro channel, free of passthrough knobs, that answers ?MAE,GET")

    # ------------------------------------------------------------ record / replay (NaviCore.ino:3440-3596)
    def rec_info(self):
        """?REC,INFO -> (state, events, capacity, dur_ms, drops, buf), each the printed string ('idle', 'RECORDING',
        'REPLAYING' or 'EDITING'; buf 'ok' or 'OOM'; navicore_record.h info :1069-1076)."""
        m = self.dev.mark()
        self.dev.send("?REC,INFO")
        g = self.dev.expect(r"^\[REC\] state=(\S+)  events=(\d+)/(\d+)  dur=(\d+)ms  drops=(\d+)  buf=(\S+)", timeout=3,
                            since=m)
        return g.groups()

    def restart_blocker(self):
        """Why restarting NaviCore now would lose recorder state, or None. A restart ends a take being recorded, a replay
        or a clip upload (?REC,INFO state other than idle), and empties the buffer, which may hold an unsaved take
        (navicore_record.h keeps it in RAM only): the recorder buffer is bench state the harness leaves as found
        (docs/HIL_WEEK_DECISIONS.md D33). The tests that restart NaviCore skip on it."""
        state, events = self.rec_info()[:2]
        if state != "idle":
            return f"NaviCore's recorder is {state}: a restart would end it"
        if int(events):
            return (f"NaviCore's recorder buffer holds {events} events, perhaps an unsaved take, which a restart would "
                    f"lose (D33): save it (?REC,SAVE,<name>) or clear it (?REC,CLEAR) first")
        return None

    def clips(self):
        """?REC,LS -> (the lines printed, [CLIPITEM dict]). [CLIPFS]{"total","used"} and '[REC] clips:' come first
        when the clips partition is mounted (NaviCore.ino:3473-3480)."""
        m = self.dev.mark()
        self.dev.send("?REC,LS")
        self.dev.expect(r"^\[CLIPLIST:END\]", timeout=5, since=m)
        lines = self.dev.since(m)
        return lines, parse_clip_items(lines)

    def rec_ls(self):
        """[(name, bytes, dur_ms, events)] of the saved clips."""
        return [(c["name"], c["bytes"], c["dur"], c["n"]) for c in self.clips()[1]]

    def rec_rm(self, name):
        """?REC,RM,<name> -> True; AssertionError on 'delete failed' (no such clip, or no clips partition). Deletes
        from the clips partition: opt-in navicore_clip_write, and only HIL* names."""
        _clip_name(name)
        lines = self.cli(f"?REC,RM,{name}", until=r"^\[REC\] (deleted|delete failed)")
        if "[REC] deleted" not in [x.rstrip() for x in lines]:
            raise AssertionError(f"?REC,RM,{name}: delete failed (not on the clips partition?)")
        return True

    def _rec_range(self, name, first, n):
        m = self.dev.mark()
        self.dev.send(f"?REC,EDITLOAD,{name},{first},{n},B")
        self.dev.send("#L12")          # releases the END line if NaviCore holds it back

        def done(x):
            if x.startswith(("[CLIPDL:ERR]", f"[REC] clip '{name}' not found")):
                return x
            if x.startswith("[CLIPDL:END]"):
                t = _json_or_none(x[len("[CLIPDL:END]"):])
                if not isinstance(t, dict) or "from" not in t or (t.get("from"), t.get("n"), t.get("nm", name)) == \
                        (first, n, name):
                    return x
            return None
        # The tool's budget over USB: 15 s plus 10 ms an event (clipDownloadVerified).
        self._await(m, done, 15 + n * 0.01, f"[CLIPDL:END] for EDITLOAD {name},{first},{n}")
        return parse_clip_range(self.dev.since(m), name, first, n)

    def rec_download(self, name, chunk=400, tries=3):
        """Every event of saved clip <name> -> [event dict], in index order, or AssertionError; never a partial clip.
        Ranged ?REC,EDITLOAD,<name>,<from>,<count>,B as the config tool's clipDownloadVerified does it over USB: a
        (0,0) probe for count, fc and fp, then ranges of `chunk`, each asked up to `tries` times until every index in
        it arrived. Refused: fc != count (the file holds more than the buffer loaded), a fingerprint that changes
        between ranges (a Record or Play trigger replaced the buffer), a clip that is not there (parse_clip_range).
        Loads the clip into the recorder's buffer, replacing an unsaved take: tests check ?REC,INFO first."""
        _clip_name(name)
        hdr, _, _ = self._rec_range(name, 0, 0)
        total, fp = hdr["count"], hdr["fp"]
        got, first = {}, 0
        while first < total:
            n = min(chunk, total - first)
            for _ in range(tries):
                h, _, events = self._rec_range(name, first, n)
                if h["fp"] != fp:
                    raise AssertionError(f"clip {name!r} changed on the board mid-download (fingerprint {fp} -> "
                                         f"{h['fp']}): a Record or Play trigger replaced the buffer")
                got.update(events)
                if all(i in got for i in range(first, first + n)):
                    break
            else:
                missing = [i for i in range(first, first + n) if i not in got]
                raise AssertionError(f"clip {name!r}: {len(missing)} of events {first}-{first + n - 1} never arrived in "
                                     f"{tries} tries (first missing: {missing[0]})")
            first += n
        return [got[i] for i in range(total)]

    def _clipul(self, cmd, pattern, timeout):
        lines = self.cli(cmd, until=pattern, timeout=timeout)
        rx = re.compile(pattern)
        return next(x.rstrip() for x in lines if rx.search(x))

    def rec_upload(self, name, events, tries=3, timeout=4.0):
        """Save `events` (rec_download's shape) as clip <name> -> how many. ?REC,EDITBEGIN, then ?REC,EDITEV,<i>,<json>
        for each, whose ACK echoes the index, so a retry after a lost ACK rewrites the same slot instead of appending
        (up to `tries`), then ?REC,EDITEND,<name>, which re-sorts by time and saves (NaviCore.ino:3572-3592; the tool's
        clipRestoreOne). EDITEND OVERWRITES a clip of that name silently (saveClip truncates), and the clip keeps no
        mode of its own (NAVICORE.md D-NC33). Any failure sends ?REC,EDITCANCEL, so the board is never left in
        ST_EDITING. Writes the clips partition: opt-in navicore_clip_write, and only HIL* names."""
        _clip_name(name)
        if not events:
            raise ValueError("no events: EDITEND refuses an empty clip ('empty-clip')")
        begin = self._clipul("?REC,EDITBEGIN", r"^\[CLIPUL:BEGIN,", timeout)
        if begin != "[CLIPUL:BEGIN,OK]":
            raise AssertionError(f"EDITBEGIN refused ({begin}): recording or replaying?")
        try:
            for i, ev in enumerate(events):
                cmd, last = f"?REC,EDITEV,{i},{json.dumps(ev, separators=(',', ':'))}", None
                for _ in range(tries):
                    try:
                        last = self._clipul(cmd, rf"^\[CLIPUL:(ACK,{i}\]|NAK)", timeout)
                    except AssertionError:
                        last = "no answer"
                    if last == f"[CLIPUL:ACK,{i}]":
                        break
                else:
                    raise AssertionError(f"event {i + 1} of {len(events)} not ACKed in {tries} tries (last: {last})")
            end = self._clipul(f"?REC,EDITEND,{name}", r"^\[CLIPUL:END,", timeout)
            if end != "[CLIPUL:END,OK]":
                raise AssertionError(f"EDITEND,{name} refused: {end}")
        except BaseException:
            try:
                self.dev.send("?REC,EDITCANCEL")
            except Exception:  # noqa: BLE001 - a dead port must not hide why the upload failed
                pass
            raise
        return len(events)

    # ------------------------------------------------------------ mesh views
    def wdp_view(self, timeout=5.0):
        """?WDP,DUMP -> parse_wdp's dict."""
        m = self.dev.mark()
        self.dev.send("?WDP,DUMP")
        self.dev.expect(r"^\[WDP:END,count=\d+\]", timeout=timeout, since=m)
        return parse_wdp(self.dev.since(m))

    def wdp_dump(self, timeout=5.0):
        """?WDP,DUMP's [WDP:N=...] rows, SELF included, as dicts of strings."""
        return self.wdp_view(timeout)["rows"]

    def self_row(self):
        """NaviCore's own ?WDP,DUMP row (PEER=3). It hard-codes CAP=0000 and MAESTRO=- (WCB_Mgmt.h:226-227): what it
        hosts is in GET_CONFIG, and in the row a WCB keeps for it."""
        return next((r for r in self.wdp_dump() if r.get("PEER") == "3"), None)

    def wdpcfg(self):
        """?WDP,DUMP's [WDPCFG:...] summary: {'EN', 'AUTOJOIN', 'PEERS'}, strings; PEERS counts the configured floor
        plus the learned peers (WCB_Mgmt.h:231-260)."""
        return self.wdp_view()["cfg"]

    def mesh_stats(self, timeout=5.0):
        """GET_MESH_STATS -> merge_mesh_stats's dict. RAM counters, zeroed by a reboot or RESET_MESH_STATS; NaviCore
        adds its own periodic sends (NAVICORE.md §1.4, tracker #76)."""
        m = self.dev.mark()
        self.dev.send(json.dumps({"type": "GET_MESH_STATS"}, separators=(",", ":")))
        self.dev.expect(r'"last":1', timeout=timeout, since=m)
        return merge_mesh_stats([json.loads(x) for x in self.dev.since(m) if x.startswith('{"type":"MESH_STATS"')])

    def backup(self, timeout=3.0):
        """?backup -> its ?TOKEN lines, ?EPASS's value replaced by <redacted:sha256[:12]> (checkpoint.redact_token), so
        the list may be compared, noted and quoted. NaviCore's is WCB_Client's minimal backup (WCB_Mgmt.h printBackup
        :181-205): ?HW, ?MAC,2, ?MAC,3, ?WCB, ?RELAY,1 when it advertises a relay, ?ALIAS, ?WCBQ, ?EPASS, ?CMDCHAR,;
        then '--------- End of Backup ---------'."""
        m = self.dev.mark()
        self.dev.send("?backup")
        self.dev.send("#L12")
        self._await(m, lambda x: True if re.match(r"^-+ End of Backup -+", x) else None, timeout,
                    "'End of Backup' line from ?backup")
        return redact_tokens([x.strip() for x in self.dev.since(m) if x.startswith("?")])

    def _seq_pull(self, obj, kind, timeout):
        m = self.dev.mark()
        self.dev.send(json.dumps(obj, separators=(",", ":")))
        head = f'{{"sys":1,"type":"{kind}",'
        reply = self._await(m, lambda x: _json_or_none(x) if x.startswith(head) else None, timeout, f"{kind} reply")
        if not reply.get("ok"):
            raise AssertionError(f"{obj['type']} {obj.get('wcb')}: {reply.get('msg')}")
        return reply

    def seq(self, wcb, timeout=8.0):
        """GET_WCB_SEQ: board <wcb>'s stored-sequence names, pulled over the mesh -> {'hash': int, 'names': [..]}.
        Asynchronous, and one pull at a time (a second is refused 'busy'); NaviCore gives up after 6 s
        (WCB_SEQ_TIMEOUT_MS) and answers ok:false, which raises here (rc_telemetry.h:296-470)."""
        r = self._seq_pull({"type": "GET_WCB_SEQ", "wcb": wcb}, "WCB_SEQ", timeout)
        return {"hash": r.get("hash"), "names": r.get("names", [])}

    def seqval(self, wcb, key, timeout=8.0):
        """GET_WCB_SEQVAL: one stored sequence of board <wcb> -> {'key', 'status', 'value'}; status 0 OK, 1 NOTFOUND,
        2 TOOBIG are all answers, not errors (rc_telemetry.h:377-395)."""
        r = self._seq_pull({"type": "GET_WCB_SEQVAL", "wcb": wcb, "key": key}, "WCB_SEQVAL", timeout)
        return {"key": r.get("key"), "status": r.get("status"), "value": r.get("value")}

    def _cli_value(self, cmd, pattern, timeout=3.0):
        rx = re.compile(pattern)
        return next(m.group(1) for x in self.cli(cmd, until=pattern, timeout=timeout) for m in [rx.search(x)] if m)

    def version_surfaces(self, w1, timeout=6.0):
        """{surface: version} of every place NaviCore reports its firmware version, None for one that stayed silent
        (NAVICORE.md nccfg.version_surfaces): 'pong' (PING), 'cli' (?version, WCB_Mgmt.h printVersion), 'ota'
        (?OTALOCAL,STATUS 'Firmware:', navicore_ota.h:231), 'self_row' (its ?WDP,DUMP SELF row), and through W1 (a
        hil.wcb.WCB): 'mesh_pong' (the bridged PONG), 'rc_hb' (the heartbeat W1 relays) and 'w1_row' (W1's ?WDP,DUMP
        row for it). The bridged PING opens W1's 20 s relay window; rc_hb comes every 2 s inside it."""
        nid = self.wcb_status()["self"]
        out = {}

        def attempt(key, fn):
            try:
                out[key] = fn()
            except (AssertionError, StopIteration, KeyError, TypeError):
                out[key] = None
        attempt("pong", self.ping)
        attempt("cli", lambda: self._cli_value("?version", r"^Software Version: (\S+)"))
        attempt("ota", lambda: self._cli_value("?OTALOCAL,STATUS", r"^Firmware:\s+(\S+)"))
        attempt("self_row", lambda: self.self_row()["FW"])
        wm = w1.dev.mark()
        w1.send(f';W{nid},{{"type":"PING"}}')
        attempt("mesh_pong", lambda: w1.dev.expect(rf'^\{{"sys":1,"type":"PONG","id":{nid},"version":"([^"]+)"',
                                                   timeout=timeout, since=wm).group(1))
        attempt("rc_hb", lambda: w1.dev.expect(rf'^\{{"sys":1,"type":"rc_hb","id":{nid},"fw":"([^"]+)"',
                                               timeout=timeout, since=wm).group(1))
        attempt("w1_row", lambda: next(_kv(x[len("[WDP:"):-1])["FW"] for x in w1.run("?WDP,DUMP", timeout=8)
                                       if x.startswith(f"[WDP:N={nid},") and x.endswith("]")))
        return out

    # ------------------------------------------------------------ lifecycle (opt-in navicore_reboot)
    def reboot(self, how="json", timeout=20.0):
        """Restart NaviCore in software and wait for it -> the lines since (parse_boot reads them). how='json': REBOOT,
        ACKed, then a restart 250 ms later (NaviCore.ino:4006-4026); how='l02': #L02, ESP.restart() with no reply
        (:3611). Neither re-samples the boot strap, so this cannot land in download mode (NAVICORE.md §1.3). The mesh
        sees WCB 20 drop for a few seconds, and every RAM-only state (debug flags, CALIB, the monitor, the mode) is
        back at its boot value."""
        m = self.dev.mark()
        if how == "json":
            self.ack({"type": "REBOOT"})
        elif how == "l02":
            self.dev.send("#L02")
        else:
            raise ValueError(f"how must be 'json' or 'l02', not {how!r}")
        return self.wait_boot(since=m, timeout=timeout)

    def wait_boot(self, since=None, timeout=20.0):
        """Wait for NaviCore to come back from a restart begun after mark `since`, then PING it -> the lines since.
        Done when setup()'s last line arrives (BOOT_DONE), or when the port has reopened ('<<reopened', SerialDevice)
        and a PING answers: the restart re-enumerates the native USB port, and whatever NaviCore printed before the
        harness reopened it is lost, the banner with it. No harness run has seen a NaviCore restart yet (2026-09-27),
        so which of the two ends a real one is for the first bench check. The ROM's 'waiting for download' fails at
        once: no app runs until a reset into it (hard_reset) or a power cycle. setup() must finish inside the 15 s
        boot guard (NaviCore.ino:4310) or restarts itself."""
        since = self.dev.mark() if since is None else since
        deadline = time.monotonic() + timeout
        while True:
            lines = self.dev.since(since)
            if any(DOWNLOAD_MODE in x for x in lines):
                raise AssertionError(f"{self.dev.name}: NaviCore is in ROM download mode ('{DOWNLOAD_MODE}'); it needs "
                                     f"a reset into its app (hard_reset) or a power cycle")
            if any(BOOT_DONE.search(x) for x in lines):
                break
            if any(x.startswith("<<reopened") for x in lines):
                try:
                    self.ping()
                    break
                except AssertionError:
                    pass
            if time.monotonic() >= deadline:
                reopened = any(x.startswith("<<reopened") for x in lines)
                raise AssertionError(f"{self.dev.name}: NaviCore did not come back within {timeout:g} s (no 'setup "
                                     f"complete.' line; port {'reopened, no PONG' if reopened else 'never reopened'})")
            time.sleep(0.25)
        self.ping()
        return self.dev.since(since)

    def hard_reset(self, hold_s=0.2):
        """The USB-Serial/JTAG chip reset (serialdev.usb_jtag_reset) -> the mark taken before it; wait_boot(since=mark)
        waits for the app. INF4's recovery rung 2: a reset into the app, with the bootloader and the strap untouched,
        the same pulse sbus.signal_loss_controller_reset gives the controller (the same S3)."""
        m = self.dev.mark()
        usb_jtag_reset(self.dev, hold_s)
        return m

    boot_info = staticmethod(parse_boot)
