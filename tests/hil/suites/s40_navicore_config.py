"""NaviCore's config surface and persistence (docs/hil_plan/NAVICORE.md NC-WP1, ids nccfg.*).

Every test here that writes NaviCore's config does it inside hil/nc_guard.py's nc_guard (decision D-NC2), which
snapshots NaviCore first, restores it afterwards and proves its config byte-identical. nccfg.guard_selftest comes
first: the others trust the guard it proves. The bench NaviCore is the droid's own dome controller, so a test changes
only what has no live effect (tool metadata, a mapping on a matrix slot no SBUS value decodes to), and never its mesh
identity (D-NC14).

What the tests change, and why each change is inert:
- Mappings only on a matrix slot whose band is 0/0, which pwmToButton never decodes (NaviCore.ino:568-577), in a mode
  where the slot has none; nothing here TRIGGERs them. A band given to such a slot lies above 2047, where no SBUS value
  reaches.
- Maestro channel metadata only on a slot with none (a remote slot nobody hosts on this bench); the firmware never reads
  it (rc_config.h:496-500). A slot's type and device are never changed (D-NC40).
- A knob only when it is disabled (channel 0: processKnobs skips it, NaviCore.ino:2595-2596); a switch only when it has
  no tiers and drives neither the mode nor a knob's mode override; a smoothing profile only when it has no entries and
  nothing names it; mode and stats reports only with wcb 0, which sends nothing (rc_telemetry.h:1258, :1298).
- A baud only on a port already at the value the clamp falls back to, except where a re-open is the point
  (side_effects_live, boardtype_change_no_reboot: S4 for a few seconds, nothing recorded on J5).
- Nothing writes a credential or the mesh identity: wcbNetwork's password, deviceId and quantity and the wifi fields are
  never sent changed. The mesh probes send only values the clamp turns back into the current ones (channel 13 or 0 when
  the channel is already 1; an octet plus 256). wcbProfiles, the tool's inert saved identities, are rebuilt only with
  HIL entries that hold no password.
- RESET_DEFAULTS is RAM only over USB (NaviCore.ino:3989-3992): the guard's diff path puts the saved text back (the
  flash already holds it), or, in the navicore_reboot tests, a REBOOT does and nothing is written at all. Without a
  restart it is sent only when the defaults would decode nothing from the live SBUS input (_defaults_live_effects): a
  button they read as held would fire the restored mapping once the restore releases it.
Each SET_CONFIG rewrites /config.json (~14 KB of a 128 KB LittleFS); the suite does about 45 per run.

Failure messages name key paths through checkpoint.redacted_diff, so a credential only ever shows as <redacted:sha12>;
no message quotes a config line.
"""
import copy
import json
import os
import re
import secrets
import time

from hil import optin
from hil.checkpoint import redact_text, redact_token, redacted_diff
from hil.nc_guard import nc_guard, secrets_of
from hil.navicore import (DBG_DFP, DBG_HCR, DBG_MAESTRO, DBG_MP3, DBG_SERIAL, DBG_WCB, DBG_WLED, NaviCore,
                          parse_boot)
from hil.ncmesh import bridged
from hil.runner import REDACT_KINDS, Skip, test
from hil.navicore import SBUS_FULL_FPS
from hil.wcb import WCB
from suites.common import marker, nonce

LOG_LINE = re.compile(r"^\s*[0-9.]+\s+(\S+) ([<>#]) (.*)$")      # Bench.log's layout: time, name, direction, text

# rcConfigToJSON's top-level keys, in the order it writes them (rc_config.h:1221-1466); serialLabels only when a label
# is set (:1416-1426).
TOP_KEYS = ("txModel", "threeAxisGimbals", "sbusOutEnabled", "wifiEnabled", "wifiSsid", "wifiPassword", "maeGateMs",
            "boardType", "tapWindowMs", "holdMs", "switchSettleMs", "chRateHz", "matrixChannel", "matrixDebounceFrames",
            "funcBindings", "peerEvent", "thresholds", "mappings", "switches", "knobs", "hcrDest", "maestros",
            "wcbNetwork", "wcbProfiles", "mp3Dest", "dfpDest", "wledSlots", "auxBaud", "serialLabels", "serialBcast",
            "modeReport", "statsReport", "smoothProfiles")
SWITCHES = ("SA", "SB", "SC", "SD", "SE", "SF", "SG", "SH", "SI", "SJ")          # RC_SWITCH_LABELS (rc_config.h:209)
SWITCH_DEFAULT_CH = (8, 9, 10, 11, 12, 13, 14, 15, 0, 0)                         # (:210)
SWITCH_DEFAULT_POS = (3, 3, 3, 3, 3, 2, 3, 2, 2, 2)                              # (:211)
KNOBS = ("S1", "S2", "LS", "RS", "S3", "J1", "J2", "J3", "J4", "J5", "J6")      # RC_KNOB_LABELS (:342)
KNOB_DEFAULT_CH = (5, 6, 0, 0, 0, 1, 2, 3, 4, 0, 0)                              # (:343)
LABEL_KEYS = ("S3", "S4", "S5", "maestro")                                       # RC_SLBL_KEYS (:589)
# actionToJson (rc_config.h:970-1061): per JSON type, the keys it always writes after "type", then the ones it writes
# only when set, in its order; "note" last, only when non-empty.
ACTIONS = {
    "wcb_unicast": (("target", "cmd"), ("delay", "skipRunning", "note")),
    "wcb_broadcast": (("cmd",), ("delay", "skipRunning", "note")),
    "maestro_local": (("cmd",), ("delay", "note")),
    "maestro": (("target", "cmd"), ("delay", "skipRunning", "note")),
    "serial": (("port", "cmd"), ("delay", "note")),
    "hcr": (("fn", "chan", "track"), ("delay", "note")),
    "mp3": (("fn", "track"), ("delay", "note")),
    "dfplayer": (("fn", "chan", "track"), ("delay", "note")),
    "wled": (("cmd",), ("delay", "note")),
    "record": (("cmd",), ("delay", "note")),
    "play": (("cmd", "fn"), ("delay", "note")),
    "stop": ((), ("delay", "note")),
}
# rcConfigLoadDefaults (rc_config.h:801-968; wcb_config.h:16-20, :43). The mesh password default is never read here:
# it is compared with the saved one, in memory, and never printed.
DEFAULT_BANDS = [("B1", 1799, 1823), ("B2", 1758, 1782), ("B3", 1718, 1742), ("B4", 1676, 1700), ("B5", 1634, 1658),
                 ("B6", 1594, 1618), ("T4 Left", 1553, 1577), ("T4 Right", 1512, 1536), ("T5 Left", 1471, 1495),
                 ("T5 Right", 1430, 1454), ("T3 Up", 1389, 1413), ("T3 Down", 1348, 1372), ("T2 Up", 1308, 1332),
                 ("T2 Down", 1266, 1290), ("T6 Left", 1225, 1249), ("T6 Right", 1184, 1208), ("T1 Left", 1143, 1167),
                 ("T1 Right", 1103, 1127), ("L-Stick Click", 1062, 1086), ("R-Stick Click", 1021, 1045),
                 ("Unassigned", 0, 0)] + [(f"Logical {i}", 0, 0) for i in range(1, 16)]
DEFAULTS = {"txModel": 0, "threeAxisGimbals": False, "sbusOutEnabled": False, "wifiEnabled": False, "wifiSsid": "",
            "wifiPassword": "", "maeGateMs": 250, "boardType": 0, "tapWindowMs": 500, "holdMs": 750,
            "switchSettleMs": 80, "chRateHz": 5, "matrixChannel": 7, "matrixDebounceFrames": 1,
            "funcBindings": {"mode": 4}, "peerEvent": {"alert": True, "actions": []},
            "thresholds": [{"id": i + 1, "label": b[0], "minPwm": b[1], "maxPwm": b[2]} for i, b in enumerate(DEFAULT_BANDS)],
            "mappings": {},
            "switches": {s: {"channel": SWITCH_DEFAULT_CH[i], "positions": SWITCH_DEFAULT_POS[i]}
                         for i, s in enumerate(SWITCHES)},
            "knobs": {k: {"channel": KNOB_DEFAULT_CH[i], "function": 0, "reverse": False, "modeAware": False,
                          "modeSwitchOverride": -1, "outputs": []} for i, k in enumerate(KNOBS)},
            "hcrDest": {"transport": "off", "port": "S3"},
            "maestros": [{"type": 0, "device": i + 1} for i in range(8)],
            "wcbProfiles": [], "mp3Dest": {"transport": "off", "port": "2"}, "dfpDest": {"transport": "off", "port": "S3"},
            "wledSlots": [{"id": 0, "port": 0, "wcb": 0, "configured": False}] * 4,
            "auxBaud": {"S3": 9600, "S4": 9600, "S5": 9600, "maestro": 115200},
            "serialBcast": {p: {"out": False, "in": False} for p in ("S3", "S4", "S5")},
            "modeReport": {"enabled": False, "wcb": 0, "template": "", "cmds": ["", "", ""]},
            "statsReport": {"enabled": False, "wcb": 0},
            "smoothProfiles": [{"name": "Default", "entries": []}] + [{"name": "", "entries": []}] * 5}
DEFAULT_NET = {"macOct2": 0, "macOct3": 0, "quantity": 4, "deviceId": 20, "channel": 1}   # + the compile-time password
# What RESET_DEFAULTS keeps, on both transports (D-NC16, rcConfigResetKeepIdentity): the network identity.
IDENTITY = ("wcbNetwork", "wcbProfiles", "boardType", "wifiEnabled", "wifiSsid", "wifiPassword")

SAVED = re.compile(r"^RC config saved to LittleFS \((\d+) bytes\)\.")                  # rc_config.h:2079
REOPEN = re.compile(r"^\[(?:AUX\] S[345]|Serial2\] Local Maestro) re-open @")         # NaviCore.ino:3245-3270
SBUS_OUT = re.compile(r"^\[SBUS\] OUT (enabled|disabled)")                            # :3298, :3304
INFO_BOARD = '{"type":"INFO","msg":"boardType changed — reboot to apply the new pin profile"}'   # :3941
VERSION = re.compile(r"v\d+\.\d+\.\d+_\d{6}[A-Z]{4}\d{2}")     # FW_VERSION_BASE '_' DTG (fw_version.h:28-30)
HOOK_SKIP = "needs a NAVICORE_HIL_HOOKS build (NAVICORE.md INF9)"


def _nc(bench):
    return NaviCore(bench.dev("navicore"))


def _changed(a, b):
    """The top-level keys whose values differ between config dicts `a` and `b`, a key present in one only included."""
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def _mismatch(label, got, want):
    """None when `got` equals `want`; else one problem line with the key paths (credentials only as hashes). `label`
    is the config key, so a scalar goes through redacted_diff under it too, where checkpoint.SHOWN_AS_HASH turns a
    password or wifiSsid into its hash: formatted bare, nccfg.reset_defaults_ram quoted the bench's SoftAP name and
    password in its failure line (2026-10-04)."""
    if got == want:
        return None
    if isinstance(got, (dict, list)) and isinstance(want, (dict, list)):
        return f"{label}: {'; '.join(redacted_diff(want, got, limit=8))}"
    return "; ".join(redacted_diff({label: want}, {label: got}, limit=8))


def _save(nc, data, save_id=None, check=True):
    """SET_CONFIG `data` -> (its ACK, the console lines from the send to the ACK, right-stripped). The save's own lines
    - the LittleFS one, the INFO, the re-open and SBUS OUT lines - all print before its ACK (NaviCore.ino:3935-3954)."""
    m = nc.dev.mark()
    ack = nc.set_config(data, save_id=save_id, check=check)
    return ack, [x.rstrip() for x in nc.dev.since(m)]


def _flushed(nc, since, settle=0.3):
    """The lines since mark `since`, after `settle` seconds and a #L12 that releases a line NaviCore holds back."""
    time.sleep(settle)
    m = nc.dev.mark()
    nc.dev.send("#L12")
    try:
        nc.dev.expect(r"Mode=\d+", timeout=3, since=m)
    except AssertionError:
        pass
    return [x.rstrip() for x in nc.dev.since(since)]


def _inert_keys(cfg, n=1, modes=(1, 2, 3)):
    """`n` mapping keys (mode * 100 + slot) with no mapping, on matrix slots whose band is 0/0: pwmToButton never
    decodes one from SBUS, so only a TRIGGER could fire it (NaviCore.ino:568-577). Highest slot first, so 136 first
    (NAVICORE.md §1.4)."""
    maps, bands = cfg.get("mappings") or {}, cfg.get("thresholds") or []
    out = []
    for btn in range(len(bands), 0, -1):
        b = bands[btn - 1]
        if not isinstance(b, dict) or (b.get("minPwm"), b.get("maxPwm")) != (0, 0):
            continue
        for mode in modes:
            if str(mode * 100 + btn) not in maps:
                out.append(str(mode * 100 + btn))
                if len(out) == n:
                    return out
    raise Skip(f"fewer than {n} free mapping keys on inert (0/0) matrix slots")


def _inert_mapping_key(cfg):
    """A mapping key (mode * 100 + slot) with no mapping, on a matrix slot whose band is 0/0: pwmToButton never decodes
    it from SBUS, so only a TRIGGER could fire it (NaviCore.ino:568-579). 136 first (NAVICORE.md §1.4)."""
    return _inert_keys(cfg, 1)[0]


def _slot_without_channels(cfg):
    """(slot, True) for a Maestro slot that prints no "channels" key, slot 3 first (a remote slot nobody hosts on this
    bench, NAVICORE.md §1.2); (3, False) when every slot has some, so that trap is not exercised."""
    maes = cfg.get("maestros") or []
    for slot in [3] + [s for s in range(1, len(maes) + 1) if s != 3]:
        if slot <= len(maes) and "channels" not in maes[slot - 1]:
            return slot, True
    return 3, False


def _all_actions(cfg):
    """[(where, action)] for every action in `cfg`: mapping tiers t1-t4, switch tiers p0-p2, the new-peer actions."""
    out = []
    for key, m in (cfg.get("mappings") or {}).items():
        for t in range(1, 5):
            out += [(f"mappings.{key}.t{t}[{i}]", a) for i, a in enumerate(m.get(f"t{t}") or [])]
    for label, s in (cfg.get("switches") or {}).items():
        for p in range(3):
            out += [(f"switches.{label}.p{p}[{i}]", a) for i, a in enumerate(s.get(f"p{p}") or [])]
    out += [(f"peerEvent.actions[{i}]", a) for i, a in enumerate((cfg.get("peerEvent") or {}).get("actions") or [])]
    return out


def _free_profile(cfg):
    """A smoothing-profile index 1-5 with no entries that no knob uses and no action cmd names (',p<n>' - setEasing,
    applyProfile, restartScript's easing spec, NaviCore.ino:1324-1372), or None. Changing it changes no easing."""
    profs = cfg.get("smoothProfiles") or []
    used = {k.get("smoothProfile") for k in (cfg.get("knobs") or {}).values()}
    cmds = [str(a.get("cmd", "")).lower() for _, a in _all_actions(cfg)]
    for p in range(1, len(profs)):
        if profs[p].get("entries") or p in used or any(re.search(rf",p{p}(?!\d)", c) for c in cmds):
            continue
        return p
    return None


def _free_knob(cfg):
    """A knob label that is disabled and drives nothing (channel 0, function 0, no outputs, not mode-aware), or None."""
    for label in KNOBS:
        k = (cfg.get("knobs") or {}).get(label) or {}
        if k.get("channel") == 0 and k.get("function") == 0 and not k.get("outputs") and not k.get("modeAware"):
            return label
    return None


def _free_switch(cfg):
    """(index, label) of a switch with no tiers and no notes that is neither the mode switch nor any knob's mode
    override, or None: rebinding it fires nothing and moves no mode."""
    mode_sw = (cfg.get("funcBindings") or {}).get("mode")
    overrides = {k.get("modeSwitchOverride") for k in (cfg.get("knobs") or {}).values()}
    for i, label in enumerate(SWITCHES):
        s = (cfg.get("switches") or {}).get(label) or {}
        if i == mode_sw or i in overrides or any(re.fullmatch(r"p[0-2](note)?", k) for k in s):
            continue
        return i, label
    return None


def _log_tail(bench, start):
    """session.log from byte `start` on, as text (flushed first)."""
    bench.sync_log()
    path = os.path.join(bench.out_dir, "session.log")
    with open(path, "rb") as f:
        f.seek(start)
        return f.read().decode("utf-8", errors="replace")


def _quotes_secret(text, secret):
    """Does `text` carry `secret` as a value? Six characters or more: anywhere. Shorter: only where a credential sits
    (a JSON password field, ?EPASS), so a short value cannot match a timestamp."""
    if len(secret) >= 6:
        return secret in text
    return bool(re.search(r'[Pp]assword\\?"\s*:\s*\\?"' + re.escape(secret) + r'\\?"', text)
                or re.search(r"EPASS,?" + re.escape(secret) + r"(?:$|[\^\s'\"])", text))


def _hooks(nc):
    """True when NaviCore runs a NAVICORE_HIL_HOOKS build: '#L90,0' (stall loop() for 0 ms, INF9 b) is a known code
    there and 'Unknown #L code 90' on every other image (NaviCore.ino:3597-3604, :3689-3691)."""
    lines = nc.cli("#L90,0", until=r"Unknown #L code 90|Mode=\d+")
    return not any("Unknown #L code 90" in x for x in lines)


def _reboot_opted(bench):
    """Skip unless bench.json opts in to NaviCore restarts: for a test gated on another key that also reboots."""
    if "navicore_reboot" not in optin.enabled(bench.cfg):
        raise Skip(optin.skip_reason("navicore_reboot"))


@test("nccfg.guard_selftest", "nc_guard puts NaviCore's config back byte-identical after the three changes a re-sent "
      "snapshot cannot undo (a new mapping, channels on a Maestro slot that had none, a serial label), by the snapshot "
      "plus clears, never RESET_DEFAULTS; no credential reaches session.log", needs=["navicore"], links=[])
def guard_selftest(bench):
    """INF3's proof on the real board (NAVICORE.md NC-WP1: runs before any other nccfg test writes).

    One diff SET_CONFIG adds, all tagged HIL<nonce>: a mapping on an inert matrix slot (136 on this bench; its action,
    a ?-query to W1, never fires: nothing decodes that slot from SBUS and nothing here TRIGGERs it), a channel name on
    a Maestro slot that had none (tool metadata the firmware never reads, rc_config.h:496-500; slot 3 is a remote slot
    nobody hosts), and a serial label on S5 (advertised over WDP, so W1 and W2 show [WDPIF:N=20,S=3,DEV=HIL...] until
    NaviCore's next advert, which the restore's own SET_CONFIG sends at once, NaviCore.ino:3937). Then the snapshot
    alone is re-sent, and what it failed to undo is noted: the trap the guard's clears exist for (rc_config.h:1580-1603,
    :1705-1721, :1847-1864). The guard must then restore by its first path (snapshot plus clears), byte-identical, with
    no RESET_DEFAULTS sent, and every NaviCore, SBUS and runner line logged since the test began must be free of the
    snapshot's credentials, while the config lines that carried them are there, hashed. No servo moves: none of the
    three is easing, and no mode changes."""
    if not bench.out_dir:
        raise Skip("no session.log to check (not a run)")
    bench.sync_log()
    start = os.path.getsize(os.path.join(bench.out_dir, "session.log"))
    tag = marker()
    with nc_guard(bench) as g:
        nc, before = g.nc, g.before
        key = _inert_mapping_key(before)
        slot, slot_trap = _slot_without_channels(before)
        labels = dict(before.get("serialLabels") or {})
        label_key = next((k for k in ("S5", "S4", "S3", "maestro") if k not in labels), "S5")
        maes = copy.deepcopy(before["maestros"])
        chans = list(maes[slot - 1].get("channels") or [])
        free_ch = next(c for c in range(31, -1, -1) if all(x.get("ch") != c for x in chans))
        maes[slot - 1]["channels"] = chans + [{"ch": free_ch, "name": tag, "min": 4000, "max": 8000}]
        change = {"mappings": {key: {"exclusive": False, "t1": [{"type": "wcb_unicast", "target": "1",
                                                                  "cmd": "?HILNOOP"}], "t1note": tag}},
                  "maestros": maes, "serialLabels": dict(labels, **{label_key: tag})}
        traps = {"mapping": True, "channels": slot_trap, "label": "serialLabels" not in before}
        bench.note(f"changes: mapping {key}, a channel {free_ch} name on Maestro slot {slot}, serial label "
                   f"{label_key}; traps exercised: {', '.join(k for k, v in traps.items() if v) or 'none'}")

        def present(cfg):
            chs = (cfg.get("maestros") or [{}] * slot)[slot - 1].get("channels") or []
            return {"mapping": key in (cfg.get("mappings") or {}),
                    "channels": any(c.get("name") == tag for c in chs),
                    "label": (cfg.get("serialLabels") or {}).get(label_key) == tag}
        nc.set_config(change)
        landed = present(nc.config())
        assert all(landed.values()), f"the change did not land: {[k for k, v in landed.items() if not v]}"
        nc.set_config(g.before_text)                     # the snapshot alone, as a naive restore would send it
        kept = [k for k, v in present(nc.config()).items() if v]
        bench.note(f"the snapshot re-sent alone left {kept or 'nothing'} in place"
                   + ("" if kept else " - the trap is gone on this image; the change is made again so the guard "
                                      "still has to restore it"))
        if not kept:
            nc.set_config(change)
    assert g.path == "diff", (f"the guard restored by its {g.path!r} path, not the snapshot plus clears"
                              + (f"; problems: {g.problems}" if g.problems else ""))
    assert g.nc.config(raw=True) == g.before_text, "NaviCore's config is not byte-identical after the guard"
    names = {n for n, d in bench.cfg["devices"].items() if d.get("kind") in REDACT_KINDS} | {"runner"}
    secrets = secrets_of(before)
    leaked, config_lines, hashed, resets = [], 0, 0, 0
    for i, line in enumerate(_log_tail(bench, start).splitlines()):
        m = LOG_LINE.match(line)
        if not m or m.group(1) not in names:
            continue
        text = m.group(3)
        if any(_quotes_secret(text, s) for s in secrets):
            leaked.append(i + 1)
        if text.startswith(('{"type":"CONFIG","data":', '{"type":"SET_CONFIG"')):
            config_lines += 1
            hashed += "<redacted:" in text
        if m.group(2) == ">" and text.startswith('{"type":"RESET_DEFAULTS"'):
            resets += 1
    assert not leaked, (f"{len(leaked)} NaviCore/SBUS/runner line(s) logged since the test began quote one of the "
                        f"snapshot's {len(secrets)} credential(s) (lines {leaked[:5]} of the tail; not quoted here)")
    assert resets == 0, f"{resets} RESET_DEFAULTS sent: the diff path must never send one"
    assert config_lines >= 4, f"only {config_lines} GET_CONFIG/SET_CONFIG lines logged: the scan saw too little"
    assert not secrets or hashed >= 4, (f"{hashed} of {config_lines} config lines carry a <redacted:...> value, though "
                                        f"the config holds {len(secrets)} credential(s)")


# ============================================================ what GET_CONFIG prints
def _action_problems(where, a):
    """The ways action `a`, as GET_CONFIG printed it, departs from actionToJson (rc_config.h:970-1061)."""
    if not isinstance(a, dict) or a.get("type") not in ACTIONS:
        return [f"{where}: type {a.get('type') if isinstance(a, dict) else '?'!r} is not one actionToJson writes"]
    req, opt = ACTIONS[a["type"]]
    want = ["type"] + list(req) + [k for k in opt if k in a]
    out = [] if list(a) == want else [f"{where} ({a['type']}): keys {list(a)}, actionToJson writes {want}"]
    if "delay" in a and not (isinstance(a["delay"], int) and a["delay"] > 0):
        out.append(f"{where}: delay {a['delay']!r} printed (only a non-zero delay is)")
    if "skipRunning" in a and a["skipRunning"] is not True:
        out.append(f"{where}: skipRunning printed as {a['skipRunning']!r} (only true is)")
    if "note" in a and not a["note"]:
        out.append(f"{where}: an empty note printed")
    return out


def _shape_problems(cfg):
    """Everything in GET_CONFIG `cfg` that rcConfigToJSON (rc_config.h:1211-1482) would not have written: key sets and
    orders, array lengths, the sparse rules. Key paths and types only: no value that could be a credential."""
    p = []
    want = [k for k in TOP_KEYS if k != "serialLabels" or "serialLabels" in cfg]
    if list(cfg) != want:
        p.append(f"top-level keys {[k for k in cfg if k not in TOP_KEYS]} unexpected, "
                 f"{[k for k in want if k not in cfg]} missing, or out of order")
    if not isinstance(cfg.get("funcBindings"), dict) or list(cfg["funcBindings"]) != ["mode"]:
        p.append("funcBindings is not {mode}")
    pe = cfg.get("peerEvent") or {}
    if list(pe) != ["alert", "actions"] or len(pe.get("actions") or []) > 5:
        p.append("peerEvent is not {alert, actions[<=5]}")
    th = cfg.get("thresholds") or []
    if len(th) != 36 or any(list(t) != ["id", "label", "minPwm", "maxPwm"] for t in th):
        p.append(f"thresholds: {len(th)} entries (36 {{id, label, minPwm, maxPwm}} expected)")
    for key, m in (cfg.get("mappings") or {}).items():
        mode, btn = divmod(int(key), 100) if key.isdigit() else (0, 0)
        if not (1 <= mode <= 3 and 1 <= btn <= 36):
            p.append(f"mappings.{key}: not a mode*100+slot key")
        order = ["exclusive"] + [k for t in range(1, 5) for k in (f"t{t}", f"t{t}note") if k in m]
        if list(m) != order or not isinstance(m.get("exclusive"), bool):
            p.append(f"mappings.{key}: keys {list(m)}")
        if not m.get("exclusive") and len(m) == 1:
            p.append(f"mappings.{key}: an empty mapping printed (rc_config.h:1262 leaves them out)")
        for t in range(1, 5):
            if f"t{t}" in m and not 1 <= len(m[f"t{t}"]) <= 5:
                p.append(f"mappings.{key}.t{t}: {len(m[f't{t}'])} actions (1-5 printed)")
    sw = cfg.get("switches") or {}
    if list(sw) != list(SWITCHES):
        p.append(f"switches: keys {list(sw)}")
    for label, s in sw.items():
        order = ["channel", "positions"] + [k for q in range(3) for k in (f"p{q}", f"p{q}note") if k in s]
        if list(s) != order:
            p.append(f"switches.{label}: keys {list(s)}")
    kn = cfg.get("knobs") or {}
    if list(kn) != list(KNOBS):
        p.append(f"knobs: keys {list(kn)}")
    for label, k in kn.items():
        order = ["channel", "function", "reverse", "modeAware", "modeSwitchOverride"] + \
                [x for x in ("smoothProfile", "easeSwitchOverride") if x in k] + ["outputs"] + \
                (["outputs2", "outputs3"] if k.get("modeAware") else [])
        if list(k) != order:
            p.append(f"knobs.{label}: keys {list(k)}")
        if k.get("smoothProfile") == -1 or k.get("easeSwitchOverride") is False:
            p.append(f"knobs.{label}: a default smoothProfile/easeSwitchOverride printed (both are left out)")
        for key in ("outputs", "outputs2", "outputs3"):
            for i, o in enumerate(k.get(key) or []):
                oo = ["target", "maestroCh", "posMin", "posMax"] + [x for x in ("midClosed", "releaseIdleMs") if x in o]
                if list(o) != oo or o.get("midClosed", True) is not True or o.get("releaseIdleMs", 1) == 0:
                    p.append(f"knobs.{label}.{key}[{i}]: keys {list(o)}")
            if len(k.get(key) or []) > 10:
                p.append(f"knobs.{label}.{key}: more than 10 outputs")
    for key in ("hcrDest", "mp3Dest", "dfpDest"):
        d = cfg.get(key) or {}
        want_d = (["transport", "target", "wcbPort"] if key == "hcrDest" else ["transport", "target"]) \
            if d.get("transport") == "wcb" else ["transport", "port"]
        if d.get("transport") not in ("off", "wcb", "serial") or list(d) != want_d:
            p.append(f"{key}: transport {d.get('transport')!r}, keys {list(d)}")
    mae = cfg.get("maestros") or []
    if len(mae) != 8:
        p.append(f"maestros: {len(mae)} slots (8 expected)")
    for i, s in enumerate(mae):
        if list(s) not in (["type", "device"], ["type", "device", "channels"]):
            p.append(f"maestros[{i}]: keys {list(s)}")
        chs = s.get("channels")
        if chs is not None:
            if not chs or [c.get("ch") for c in chs] != sorted({c.get("ch") for c in chs}) or \
                    any(list(c) != ["ch", "name", "min", "max"] or not 0 <= c["ch"] <= 31 for c in chs):
                p.append(f"maestros[{i}].channels: not a sorted, non-empty list of {{ch 0-31, name, min, max}}")
    if list(cfg.get("wcbNetwork") or {}) != ["macOct2", "macOct3", "password", "quantity", "deviceId", "channel"]:
        p.append("wcbNetwork: keys are not macOct2, macOct3, password, quantity, deviceId, channel")
    prof = cfg.get("wcbProfiles")
    if not isinstance(prof, list) or len(prof) > 6 or any(
            list(x) != ["name", "macOct2", "macOct3", "password", "quantity", "deviceId", "channel"] for x in prof):
        p.append("wcbProfiles: not a list of at most 6 full profiles")
    ws = cfg.get("wledSlots") or []
    if len(ws) != 4 or any(list(w) != ["id", "port", "wcb", "configured"] for w in ws):
        p.append(f"wledSlots: {len(ws)} entries (4 {{id, port, wcb, configured}} expected)")
    if list(cfg.get("auxBaud") or {}) != ["S3", "S4", "S5", "maestro"]:
        p.append(f"auxBaud: keys {list(cfg.get('auxBaud') or {})}")
    if "serialLabels" in cfg:
        sl = cfg["serialLabels"]
        if not sl or any(k not in LABEL_KEYS or not v for k, v in sl.items()) or \
                list(sl) != [k for k in LABEL_KEYS if k in sl]:
            p.append(f"serialLabels: {list(sl)} (only set labels, keys {LABEL_KEYS} in that order)")
    sb = cfg.get("serialBcast") or {}
    if list(sb) != ["S3", "S4", "S5"] or any(list(v) != ["out", "in"] for v in sb.values()):
        p.append("serialBcast: not S3/S4/S5 x {out, in}")
    mr = cfg.get("modeReport") or {}
    if list(mr) != ["enabled", "wcb", "template", "cmds"] or len(mr.get("cmds") or []) != 3:
        p.append("modeReport: not {enabled, wcb, template, cmds[3]}")
    if list(cfg.get("statsReport") or {}) != ["enabled", "wcb"]:
        p.append("statsReport: not {enabled, wcb}")
    sp = cfg.get("smoothProfiles") or []
    if len(sp) != 6 or any(list(x) != ["name", "entries"] for x in sp):
        p.append(f"smoothProfiles: {len(sp)} entries (6 {{name, entries}} expected)")
    for i, x in enumerate(sp):
        for e in x.get("entries") or []:
            if list(e) != ["mid", "ch", "spd", "acc"] or not 1 <= e["mid"] <= 8 or not 0 <= e["ch"] <= 31 or \
                    not (e["spd"] or e["acc"]):
                p.append(f"smoothProfiles[{i}]: entry {e} (sparse: mid 1-8, ch 0-31, speed or accel set)")
                break
    for where, a in _all_actions(cfg):
        p += _action_problems(where, a)
    return p


@test("nccfg.get_config_shape", "GET_CONFIG's data has rcConfigToJSON's key set in its order, 36 thresholds, 8 Maestro "
      "slots, 4 WLED slots, 6 smoothing profiles, 3 mode-report commands, the switch and knob label keys, only the "
      "action types and keys actionToJson writes, and leaves out what is empty (read only)", needs=["navicore"], links=[])
def get_config_shape(bench):
    """A read of the running config against rcConfigToJSON (rc_config.h:1211-1482) and actionToJson (:970-1061): the
    config tool builds its whole model from this shape, and nc_guard's clears rely on the sparse rules (an empty mapping,
    a slot's empty channel list and empty labels are never printed: :1262, :1340, :1421). A field NaviCore gains without
    a matching entry here fails the test: the entry belongs in TOP_KEYS or ACTIONS, and the tool may need it too."""
    cfg = _nc(bench).config()
    problems = _shape_problems(cfg)
    bench.note(f"GET_CONFIG: {len(cfg.get('mappings') or {})} mappings, {len(_all_actions(cfg))} actions, "
               f"{sum(1 for s in cfg.get('maestros') or [] if 'channels' in s)} Maestro slots with channel names, "
               f"serialLabels {'set' if 'serialLabels' in cfg else 'absent'}")
    assert not problems, redact_text("; ".join(problems[:12]) + (f" ... and {len(problems) - 12} more"
                                                                   if len(problems) > 12 else ""))


HOLD_S = (0.3, 1.0)      # long_reply_host_stall: past HWCDC's 50 ms TX timeout, and well past it


@test("nccfg.long_reply_host_stall", "(should) GET_CONFIG's ~14 KB and GET_CMDLIB's ~19 KB reply lines arrive whole and "
      "byte-identical to an unstalled read when the host stops reading for 0.3 s and for 1 s just after asking: NaviCore "
      "waits for room in its USB TX ring instead of the USB core dropping the middle of the line (D-NC75; read only)",
      needs=["navicore"], links=[])
def long_reply_host_stall(bench):
    """NAVICORE.md D-NC75. NaviCore printed GET_CONFIG's line as one Serial.print of ~14 KB (and GET_CMDLIB's ~19 KB)
    into an 8 KB USB TX ring. The esp32 core's HWCDC::write waits for room in 1 ms steps; after its 50 ms TX timeout
    with no progress it marks the host gone and returns the rest unsent, and the next writes (the closing '}') take its
    not-connected path, which pops the OLDEST queued bytes to make room - so a host that stopped reading for 50 ms got
    the line with a hole in its middle (intellex.ws_transport_navicore on the Mac, 20261006-094314: '"cmd":";A,P' then
    'clusive":false'). The harness's reader holds its reads (SerialDevice.hold_reads) right after sending the request,
    so the OS buffer and then NaviCore's ring fill, then reads again: each reply must equal an unstalled read of the
    same request, byte for byte. Nothing quotes either line (the config holds the mesh and AP passwords): lengths only.
    Fixed in NaviCore 0657025 (printLong: the lines go out paced into the ring, waiting up to 1 s for a stalled host).
    On the Mac this passes before the fix as well (20261006-113323): macOS's USB driver keeps reading into its own
    buffer while the harness pauses, and a whole 19 KB reply fits there, so the stall never reaches NaviCore. It can
    catch the defect where the host buffers less - Windows, where a 9,575-character CONFIG was seen (20261004-231308)."""
    nc = _nc(bench)
    if not hasattr(nc.dev, "hold_reads"):
        raise Skip("this device driver cannot hold its reads")
    problems, notes = [], []
    for verb, pattern in (("GET_CONFIG", r'^\{"type":"CONFIG","data":'), ("GET_CMDLIB", r'^\{"type":"CMDLIB",')):
        ref = nc.json_cmd({"type": verb}, pattern, timeout=10.0).string
        try:
            json.loads(ref)
        except ValueError:
            raise AssertionError(f"{verb}: the unstalled reference read ({len(ref)} chars) is not whole JSON itself")
        for hold in HOLD_S:
            m = nc.dev.mark()
            nc.dev.send(json.dumps({"type": verb}, separators=(",", ":")))
            nc.dev.hold_reads(hold)
            try:
                got = nc.dev.expect(pattern, timeout=hold + 10.0, since=m).string
            except AssertionError:
                problems.append(f"{verb} with the host holding its reads {hold:g} s: no reply line")
                continue
            whole = got == ref
            notes.append(f"{verb} after a {hold:g} s hold: {len(got)} of {len(ref)} chars, "
                         f"{'byte-identical' if whole else 'different'}")
            if not whole:
                try:
                    json.loads(got)
                    how = "valid JSON, but not the same bytes"
                except ValueError:
                    how = "not JSON"
                problems.append(f"{verb} with the host holding its reads {hold:g} s: {len(got)} chars where an unstalled "
                                f"read gives {len(ref)} ({how})")
            time.sleep(0.5)
    bench.note("long_reply_host_stall: " + "; ".join(notes))
    assert not problems, "(should, D-NC75) a long reply lost bytes while the host stalled: " + "; ".join(problems)


# ============================================================ SET_CONFIG: replies and merge semantics
@test("nccfg.set_ack_shapes", "SET_CONFIG's replies: ok:true echoes the saveId (0 when absent) after 'RC config saved "
      "to LittleFS (N bytes)' with N the GET_CONFIG length; no data gets ok:false 'missing data' with the saveId; a "
      "document the filtered header parses but the full parse rejects gets ok:false 'parse failed' with no saveId; "
      "neither refusal saves", needs=["navicore"], links=[])
def set_ack_shapes(bench):
    """NaviCore.ino:3917-3958. The saves are no-ops (chRateHz sent as it is), so GET_CONFIG must stay byte-identical.
    'parse failed' needs a line whose filtered header parse (type only, NaviCore.ino:3809-3824) succeeds while the
    unfiltered parse fails: an invalid escape (\\q) inside "data" does it, because ArduinoJson only skips a string the
    filter drops (skipQuotedString) but validates every escape when it keeps one (parseQuotedString with
    EscapeSequence::unescapeChar; ArduinoJson 7.4.3 JsonDeserializer.hpp:396-445). The plan's 20000-element array does
    not: ArduinoJson 7's documents are elastic, so it parses, and a non-object data then SAVES and answers ok:true
    (nccfg.set_nonobject_data). The save-failure branch (:3954) needs the INF9 hook: nccfg.hook_save_fail."""
    problems = []
    with nc_guard(bench) as g:
        nc, before = g.nc, g.before
        rate, n = before["chRateHz"], len(g.before_text.encode("utf-8"))
        for obj, want, saves in (
                ({"type": "SET_CONFIG", "saveId": 424242, "data": {"chRateHz": rate}},
                 '{"type":"ACK","of":"SET_CONFIG","ok":true,"saveId":424242}', True),
                ({"type": "SET_CONFIG", "data": {"chRateHz": rate}},
                 '{"type":"ACK","of":"SET_CONFIG","ok":true,"saveId":0}', True),
                ({"type": "SET_CONFIG", "saveId": 7},
                 '{"type":"ACK","of":"SET_CONFIG","ok":false,"msg":"missing data","saveId":7}', False),
                ('{"type":"SET_CONFIG","saveId":8,"data":{"hil":"\\q"}}',
                 '{"type":"ACK","of":"SET_CONFIG","ok":false,"msg":"parse failed"}', False)):
            m = nc.dev.mark()
            if isinstance(obj, str):
                nc.dev.send(obj)
                got = nc.dev.expect(r'^\{"type":"ACK","of":"SET_CONFIG"', timeout=15, since=m).string.rstrip()
            else:
                got = nc.json_cmd(obj, r'^\{"type":"ACK","of":"SET_CONFIG"', timeout=15).string.rstrip()
            lines = [x.rstrip() for x in nc.dev.since(m)]
            saved = [int(s.group(1)) for x in lines for s in [SAVED.match(x)] if s]
            label = str(obj.get("saveId", "none") if isinstance(obj, dict) else 8)
            if got != want:
                problems.append(f"saveId {label}: ACK {got}")
            if saves and saved != [n]:
                problems.append(f"saveId {label}: saved-line sizes {saved}, GET_CONFIG data is {n} bytes")
            if not saves and saved:
                problems.append(f"saveId {label}: a refused SET_CONFIG still saved ({saved[0]} bytes)")
        after = nc.config(raw=True)
        if after != g.before_text:
            problems.append("GET_CONFIG changed: " + "; ".join(redacted_diff(before, json.loads(after), limit=6)))
    assert not problems, "; ".join(problems)


@test("nccfg.diff_toplevel", "SET_CONFIG with one top-level key changes only that key (chRateHz); tapWindowMs alone "
      "also moves holdMs through its clamp; a null reads through the key's default (chRateHz 5); an unknown key changes "
      "nothing", needs=["navicore"], links=[])
def diff_toplevel(bench):
    """Every top-level branch is containsKey-guarded (rc_config.h:1502-1899), which is what lets the tool save one
    branch at a time. But the holdMs clamp runs on every save, sent or not (:1527-1528): a tapWindowMs above
    holdMs - 100 raises holdMs to tapWindowMs + 250 (capped at 5000), so the tool's one-key save changes two keys. A
    null takes the `| default` (:1535-1539: chRateHz reads 5). chRateHz only paces the rc_ch stream while a tool holds a
    mesh subscription; tapWindowMs only times taps, and no button is pressed."""
    problems = []
    with nc_guard(bench) as g:
        nc, before = g.nc, g.before
        rate = 7 if before["chRateHz"] != 7 else 6
        _save(nc, {"chRateHz": rate})
        a = nc.config()
        if _changed(before, a) != ["chRateHz"] or a["chRateHz"] != rate:
            problems.append(f"chRateHz {rate}: changed {_changed(before, a)}, chRateHz {a['chRateHz']}")
        _save(nc, {"chRateHz": None})
        b = nc.config()
        if _changed(a, b) != ["chRateHz"] or b["chRateHz"] != 5:
            problems.append(f"chRateHz null: changed {_changed(a, b)}, chRateHz {b['chRateHz']} (the default 5)")
        hold, tap = b["holdMs"], b["tapWindowMs"]
        new_tap = hold - 50                          # above holdMs - 100, so the clamp must move holdMs
        want_hold = min(new_tap + 250, 5000)
        _save(nc, {"tapWindowMs": new_tap})
        c = nc.config()
        want = sorted(["tapWindowMs"] + (["holdMs"] if want_hold != hold else []))
        if _changed(b, c) != want or (c["tapWindowMs"], c["holdMs"]) != (new_tap, want_hold):
            problems.append(f"tapWindowMs {tap}->{new_tap} with holdMs {hold}: changed {_changed(b, c)}, now "
                            f"{c['tapWindowMs']}/{c['holdMs']} (expected {new_tap}/{want_hold})")
        _save(nc, {"hilUnknownKey": 1})
        d = nc.config()
        if _changed(c, d):
            problems.append(f"an unknown key changed {_changed(c, d)}")
        bench.note(f"holdMs {hold} became {c['holdMs']} when only tapWindowMs ({tap} -> {new_tap}) was sent")
    assert not problems, "; ".join(problems)


def _mapping_case(tag):
    """(what is sent for one inert key, what GET_CONFIG must print for it). Seven actions in t1: the retired 'smooth'
    type is dropped and the rest close up (actionFromJson returns false, the slot is reused: rc_config.h:1596-1600,
    :1151-1152), the sixth parseable one is cut (5 per tier, :1598); 'maestro_remote' is read as the unified 'maestro'
    (:1086-1095); a zero delay is not printed; exclusive defaults to false (:1590); a tier with only a note prints its
    note (:1276)."""
    a1 = {"type": "wcb_unicast", "target": "1", "cmd": f";S0,{tag}a", "delay": 1500, "skipRunning": True}
    a2 = {"type": "maestro", "target": "4", "cmd": "setTarget,5,6000", "skipRunning": True}
    a3 = {"type": "serial", "port": "S9", "cmd": f"{tag}s"}
    a4 = {"type": "hcr", "fn": 99, "chan": 0, "track": 0}
    a5 = {"type": "maestro_remote", "target": "4", "cmd": "goHome"}
    a6 = {"type": "stop"}
    a7 = {"type": "play", "cmd": f"{tag}p", "fn": 1, "delay": 0}
    sent = {"t1": [a1, {"type": "smooth", "cmd": "set,1,1"}, a2, a3, a4, a5, a6], "t1note": f"{tag}t1",
            "t2note": f"{tag}t2", "t4": [a7, {"type": "smooth", "cmd": "clear"}]}
    want = {"exclusive": False, "t1": [a1, a2, a3, a4, {"type": "maestro", "target": "4", "cmd": "goHome"}],
            "t1note": f"{tag}t1", "t2note": f"{tag}t2", "t4": [{"type": "play", "cmd": f"{tag}p", "fn": 1}]}
    return sent, want


@test("nccfg.mappings_per_key", "mappings replaces only the keys it names: an inert key gets 5 of 7 actions (the "
      "retired type dropped, the legacy maestro_remote read as maestro), an exclusive-only mapping prints, keys "
      "436/137/0 are ignored, every other mapping is untouched, and {} clears a key: GET_CONFIG ends byte-identical",
      needs=["navicore"], links=[])
def mappings_per_key(bench):
    """rc_config.h:1580-1603. Each named key is memset and rebuilt from t1-t4 and t1note-t4note; a key whose mode is
    not 1-3 or slot not 1-36 is skipped, so '436', '137' and '0' (toInt of a non-number) touch nothing. {} empties a
    mapping, and an empty mapping is not printed (:1262), which is how the tool deletes one (the trap nc_guard's clears
    exist for). Two keys on 0/0 matrix slots: no SBUS value decodes them and nothing here TRIGGERs them, so none of the
    actions can fire."""
    problems = []
    with nc_guard(bench) as g:
        nc, before = g.nc, g.before
        key, key2 = _inert_keys(before, 2)
        tag = marker()
        sent, want = _mapping_case(tag)
        _save(nc, {"mappings": {key: sent, key2: {"exclusive": True}, "436": sent, "137": sent, "0": sent}})
        maps = nc.config()["mappings"]
        problems.append(_mismatch(f"mappings.{key}", maps.get(key), want))
        problems.append(_mismatch(f"mappings.{key2}", maps.get(key2), {"exclusive": True}))
        others = {k: v for k, v in maps.items() if k not in (key, key2)}
        problems.append(_mismatch("the other mappings", others, before.get("mappings") or {}))
        _save(nc, {"mappings": {key: {}, key2: {}}})
        if nc.config(raw=True) != g.before_text:
            problems.append("after {} for both keys GET_CONFIG is not byte-identical to the snapshot")
        bench.note(f"keys {key} and {key2}: 5 of 7 t1 actions kept, the exclusive-only mapping printed, then both "
                   f"cleared by {{}}")
    problems = [p for p in problems if p]
    assert not problems, "; ".join(problems)


@test("nccfg.branch_semantics", "Each branch merges as its code says: thresholds overwrite from index 0 (a short array "
      "keeps the tail), maestros keep channels unless sent and drop channel numbers outside 0-31, wledSlots clear the "
      "slots a short array leaves out, serialLabels and wcbProfiles are replaced whole (unknown label keys clear every "
      "label, [] clears the profiles), smoothProfiles are rebuilt whole, knobs are replaced per label (mode-aware seeds "
      "outputs2/3), a switch's missing channel reads its default, and serialBcast, modeReport and statsReport change "
      "only the fields sent", needs=["navicore"], links=[])
def branch_semantics(bench):
    """rc_config.h: thresholds :1565-1577, maestros :1695-1724, wledSlots :1822-1837, serialLabels :1854-1864,
    wcbProfiles :1773-1789, smoothProfiles :1657-1677, knobs :1628-1652, switches :1605-1626, serialBcast :1868-1877,
    modeReport :1879-1893, statsReport :1895-1900. Two saves, then the guard. Each change is inert on this bench (the
    module docstring): threshold 0 keeps its band and gets a HIL label; the Maestro slot without channels gets tool
    metadata; the WLED slots used are unconfigured; the smoothing profile changed is one nothing uses; the knob is
    disabled (channel 0); the switch has no tiers; the reports go to wcb 0; S5's broadcast-out flag flips for about two
    seconds (a plain-text mesh broadcast in that window would go out J6, where nothing is recorded as attached). A part
    whose precondition the bench does not meet is noted as not exercised."""
    problems, skipped = [], []
    with nc_guard(bench) as g:
        nc, b = g.nc, copy.deepcopy(g.before)
        tag = marker()
        slot, has_room = _slot_without_channels(b)
        prof, knob, sw = _free_profile(b), _free_knob(b), _free_switch(b)
        wled_ok = all(not w["configured"] and w["id"] == 0 for w in b["wledSlots"][1:])
        th0 = b["thresholds"][0]
        change = {"thresholds": [{"label": f"{tag}th", "minPwm": th0["minPwm"], "maxPwm": th0["maxPwm"]}],
                  "serialLabels": {"S5": f"{tag}L", "bogus": "x", "1": "y"},
                  "wcbProfiles": [{"name": f"{tag}p", "macOct2": 1, "macOct3": 2, "password": "", "quantity": 2,
                                   "deviceId": 20, "channel": 1}],
                  "modeReport": {"wcb": 0, "cmds": [f"{tag}m1"]},
                  "statsReport": {"wcb": 0},
                  "serialBcast": {"S5": {"out": not b["serialBcast"]["S5"]["out"]}}}
        if has_room:
            change["maestros"] = [{"type": s["type"], "device": s["device"]} for s in b["maestros"]]
            change["maestros"][slot - 1]["channels"] = [{"ch": 5, "name": f"{tag}c", "min": 4000, "max": 8000},
                                                        {"ch": 40, "name": "HILbad"}, {"ch": -1, "name": "HILbad"},
                                                        {"name": "HILnoch"}]
        else:
            skipped.append("maestros (every slot already has channel names)")
        if wled_ok:
            change["wledSlots"] = [b["wledSlots"][0], {"id": 9, "port": 0, "wcb": 0, "configured": False},
                                   b["wledSlots"][2], b["wledSlots"][3]]
        else:
            skipped.append("wledSlots (a slot past the first is in use)")
        if prof is not None:
            sps = copy.deepcopy(b["smoothProfiles"])
            sps[prof] = {"name": f"{tag}sp", "entries": [{"mid": 4, "ch": 5, "spd": 100, "acc": 3},
                                                         {"mid": 9, "ch": 1, "spd": 1, "acc": 1},
                                                         {"mid": 4, "ch": 32, "spd": 1, "acc": 1}]}
            change["smoothProfiles"] = sps
        else:
            skipped.append("smoothProfiles (no unused empty profile)")
        out = {"target": 4, "maestroCh": 5, "posMin": 4000, "posMax": 8000}
        if knob:
            change["knobs"] = {knob: {"channel": 0, "function": 1, "modeAware": True, "outputs": [out]}}
        else:
            skipped.append("knobs (no disabled knob)")
        if sw:
            change["switches"] = {sw[1]: {"p1note": f"{tag}sw"}}
        else:
            skipped.append("switches (no switch without tiers that drives no mode)")
        _save(nc, change)
        a = nc.config()
        exp = copy.deepcopy(b)
        exp["thresholds"][0] = {"id": 1, "label": f"{tag}th", "minPwm": th0["minPwm"], "maxPwm": th0["maxPwm"]}
        exp["serialLabels"] = {"S5": f"{tag}L"}
        exp["wcbProfiles"] = change["wcbProfiles"]
        exp["modeReport"] = dict(b["modeReport"], wcb=0, cmds=[f"{tag}m1", "", ""])
        exp["statsReport"] = dict(b["statsReport"], wcb=0)
        exp["serialBcast"]["S5"]["out"] = not b["serialBcast"]["S5"]["out"]
        if has_room:
            exp["maestros"][slot - 1]["channels"] = [{"ch": 5, "name": f"{tag}c", "min": 4000, "max": 8000}]
        if wled_ok:
            exp["wledSlots"][1] = {"id": 9, "port": 0, "wcb": 0, "configured": False}
        if prof is not None:
            exp["smoothProfiles"][prof] = {"name": f"{tag}sp", "entries": [{"mid": 4, "ch": 5, "spd": 100, "acc": 3}]}
        if knob:
            exp["knobs"][knob] = {"channel": 0, "function": 1, "reverse": False, "modeAware": True,
                                  "modeSwitchOverride": -1, "outputs": [out], "outputs2": [out], "outputs3": [out]}
        if sw:
            i, label = sw
            exp["switches"][label] = {"channel": SWITCH_DEFAULT_CH[i], "positions": SWITCH_DEFAULT_POS[i],
                                      "p1note": f"{tag}sw"}
            if (b["switches"][label]["channel"], b["switches"][label]["positions"]) == \
                    (SWITCH_DEFAULT_CH[i], SWITCH_DEFAULT_POS[i]):
                skipped.append(f"the switch fallback ({label} already on its default channel)")
        exp = json.loads(json.dumps(exp))                  # key order as a dict round trip, for the comparison
        for k in TOP_KEYS:
            problems.append(_mismatch(k, a.get(k), exp.get(k)))
        second = {"serialLabels": {"1": "HILold"}, "wcbProfiles": []}
        if wled_ok:
            second["wledSlots"] = [b["wledSlots"][0]]
        if knob:
            second["knobs"] = {knob: {"channel": 0}}
        _save(nc, second)
        c = nc.config()
        if "serialLabels" in c:
            problems.append("serialLabels with only an unknown key did not clear every label")
        problems.append(_mismatch("wcbProfiles after []", c.get("wcbProfiles"), []))
        if wled_ok:
            problems.append(_mismatch("wledSlots after a 1-entry array", c.get("wledSlots"),
                                      [b["wledSlots"][0]] + [{"id": 0, "port": 0, "wcb": 0, "configured": False}] * 3))
        if knob:
            problems.append(_mismatch(f"knobs.{knob} after {{channel 0}}", c["knobs"][knob],
                                      {"channel": 0, "function": 0, "reverse": False, "modeAware": False,
                                       "modeSwitchOverride": -1, "outputs": []}))
    if skipped:
        bench.note("not exercised on this bench: " + "; ".join(skipped))
    problems = [p for p in problems if p]
    assert not problems, "; ".join(problems[:10])


@test("nccfg.string_truncation", "Strings are cut silently at their struct sizes: an action cmd at 95 characters, an "
      "action or tier note at 19, a target at 5, a threshold label at 23, a Maestro channel name at 23, a serial label "
      "at 24, a profile or smoothing-profile name at 23, a mode-report template or command at 47",
      needs=["navicore"], links=[])
def string_truncation(bench):
    """strlcpy into the fixed buffers of RcAction (rc_config.h:112-160: cmd[96], note[20], target[6]), RcTier.note[20],
    RcThreshold.label[24], RcMaestroChannel.name[24], serialLabels[25], RcWcbProfile.name[24], RcSmoothProfile.name[24]
    and RcModeReport.tmpl/cmds[48] - no error, no log (:1069-1153, :1573, :1592, :1666, :1717, :1779, :1860, :1884-1890).
    A truncated ;-chain then fires a different command than the one the tool shows. ASCII only here: a cut through a
    multi-byte character is nccfg.string_truncation_utf8. wifiSsid, wifiPassword and the mesh password are credentials
    or identity and are never written."""
    problems, skipped = [], []
    with nc_guard(bench) as g:
        nc, b = g.nc, g.before
        key = _inert_keys(b, 1)[0]
        tag = marker()
        slot, has_room = _slot_without_channels(b)
        prof = _free_profile(b)
        cmd, note, tnote = tag + "C" * 120, tag + "N" * 30, tag + "T" * 30
        ths = copy.deepcopy(b["thresholds"])
        ths[35]["label"] = tag + "L" * 30
        change = {"mappings": {key: {"t1": [{"type": "wcb_unicast", "target": "123456", "cmd": cmd, "note": note}],
                                     "t1note": tnote}},
                  "thresholds": ths,
                  "serialLabels": dict(b.get("serialLabels") or {}, S5=tag + "S" * 30),
                  "wcbProfiles": [{"name": tag + "P" * 30, "password": ""}],
                  "modeReport": {"wcb": 0, "template": tag + "R" * 60, "cmds": [tag + "U" * 60, "", ""]}}
        if has_room:
            maes = [{"type": s["type"], "device": s["device"]} for s in b["maestros"]]
            maes[slot - 1]["channels"] = [{"ch": 6, "name": tag + "M" * 30, "min": 1, "max": 2}]
            change["maestros"] = maes
        else:
            skipped.append("the Maestro channel name")
        if prof is not None:
            sps = copy.deepcopy(b["smoothProfiles"])
            sps[prof] = {"name": tag + "Q" * 30, "entries": []}
            change["smoothProfiles"] = sps
        else:
            skipped.append("the smoothing-profile name")
        _save(nc, change)
        a = nc.config()
        m = a["mappings"].get(key) or {}
        act = (m.get("t1") or [{}])[0]
        checks = [("action cmd", act.get("cmd"), cmd[:95]), ("action note", act.get("note"), note[:19]),
                  ("action target", act.get("target"), "12345"), ("tier note", m.get("t1note"), tnote[:19]),
                  ("threshold label", a["thresholds"][35]["label"], ths[35]["label"][:23]),
                  ("serial label", (a.get("serialLabels") or {}).get("S5"), (tag + "S" * 30)[:24]),
                  ("profile name", (a["wcbProfiles"] or [{}])[0].get("name"), (tag + "P" * 30)[:23]),
                  ("mode-report template", a["modeReport"]["template"], (tag + "R" * 60)[:47]),
                  ("mode-report command", a["modeReport"]["cmds"][0], (tag + "U" * 60)[:47])]
        if has_room:
            checks.append(("Maestro channel name", ((a["maestros"][slot - 1].get("channels") or [{}])[0]).get("name"),
                           (tag + "M" * 30)[:23]))
        if prof is not None:
            checks.append(("smoothing-profile name", a["smoothProfiles"][prof]["name"], (tag + "Q" * 30)[:23]))
        for label, got, want in checks:
            problems.append(_mismatch(label, got, want))
    if skipped:
        bench.note("not exercised on this bench: " + ", ".join(skipped))
    problems = [p for p in problems if p]
    assert not problems, "; ".join(problems)


@test("nccfg.string_truncation_utf8", "(should) A string cut at its struct size is never cut through a UTF-8 character: "
      "a tier note of 18 ASCII characters and an 'é' reads back valid, not with half a character", needs=["navicore"],
      links=[])
def string_truncation_utf8(bench):
    """Finding D-NC42 (this suite, 2026-09-28): every string field is cut with strlcpy at a byte count (rc_config.h:1592 for a
    tier note, 20 bytes), so a note of 18 ASCII characters and an 'é' (2 bytes) keeps 19 bytes - the 18 and the first
    byte of the 'é'. /config.json and GET_CONFIG then carry invalid UTF-8; the config tool's decoder shows U+FFFD and its
    next save writes that back, three bytes, so the name the user typed is corrupted on every round trip. The fix is a
    cut that backs up to a character boundary. Here: the note read back must be a prefix of the one sent (the harness
    decodes NaviCore's lines with errors='replace', so a split character arrives as U+FFFD)."""
    with nc_guard(bench) as g:
        key = _inert_keys(g.before, 1)[0]
        note = "HIL" + "x" * 15 + "é" + "tail"
        _save(g.nc, {"mappings": {key: {"t1note": note}}})
        got = (g.nc.config()["mappings"].get(key) or {}).get("t1note", "")
    bench.note(f"a 19-byte tier note cut: read back {len(got)} characters, {got.count(chr(0xFFFD))} replacement "
               f"character(s)")
    assert note.startswith(got), (f"the tier note was cut through the 'é': it reads back ending in "
                                  f"{got[-3:].encode('unicode_escape').decode()!r}, not a prefix of the note sent")


@test("nccfg.clamps_timing", "The timing clamps: tapWindowMs under 100 reads 500; holdMs under tapWindowMs + 100 reads "
      "tapWindowMs + 250 and over 5000 reads 5000; switchSettleMs is held to 0-1000; chRateHz under 1 reads 5 and over "
      "20 reads 20; matrixDebounceFrames is held to 1-4; nothing else moves", needs=["navicore"], links=[])
def clamps_timing(bench):
    """rc_config.h:1516-1545, two saves. Live but idle on this bench: the values only time taps, holds, switch settling,
    the rc_ch rate while a tool subscribes and the matrix debounce, and no button, switch or tool is in use."""
    problems = []
    keys = ("tapWindowMs", "holdMs", "switchSettleMs", "chRateHz", "matrixDebounceFrames")
    with nc_guard(bench) as g:
        nc, b = g.nc, g.before
        for sent, want in (((50, 9000, -5, 0, 0), (500, 5000, 0, 5, 1)),
                           ((0, 100, 5000, 99, 9), (500, 750, 1000, 20, 4))):
            _save(nc, dict(zip(keys, sent)))
            a = nc.config()
            got = tuple(a[k] for k in keys)
            if got != want:
                problems.append(f"sent {sent}: read {got}, expected {want}")
            extra = [k for k in _changed(b, a) if k not in keys]
            if extra:
                problems.append(f"sent {sent}: {extra} changed too")
    assert not problems, "; ".join(problems)


@test("nccfg.hold_exceeds_tap_window", "(should) holdMs stays at least tapWindowMs + 100 whatever tapWindowMs is: a "
      "tapWindowMs of 6000 must not leave holdMs at 5000, below it", needs=["navicore"], links=[])
def hold_exceeds_tap_window(bench):
    """Finding D-NC43 (this suite, 2026-09-28): the long press is recognised by holding past the deferred tap dispatch, so
    holdMs 'MUST stay > tapWindowMs' (rc_config.h:656-660, the RcConfig comment). rcConfigFromJSON raises a too-small
    holdMs to tapWindowMs + 250 and then caps it at 5000 (:1527-1528), but tapWindowMs has no upper bound (:1516-1520),
    so a tapWindowMs above 4900 leaves holdMs under it and the long press can never be seen; the single tap fires
    first. Either tapWindowMs gets an upper clamp (4900 at most) or the holdMs cap gives way to it. Live but idle: no
    button is pressed while the guard holds the value."""
    with nc_guard(bench) as g:
        _save(g.nc, {"tapWindowMs": 6000})
        a = g.nc.config()
    bench.note(f"tapWindowMs 6000 sent: read tapWindowMs {a['tapWindowMs']}, holdMs {a['holdMs']}")
    assert a["holdMs"] >= a["tapWindowMs"] + 100, (f"holdMs {a['holdMs']} is below tapWindowMs {a['tapWindowMs']} "
                                                   f"+ 100: the long press can never be recognised")


@test("nccfg.clamps_baud", "auxBaud outside 1200-115200 (0, 1199, 115201, 230400) reads 9600 on the way in, the port "
      "is not re-opened and NaviCore keeps answering", needs=["navicore"], links=[])
def clamps_baud(bench):
    """rcSanBaud on input (rc_config.h:1495-1497, :1839-1847) replaces an out-of-range baud with the port's default,
    9600 for S3-S5 and 115200 (LOCAL_MAESTRO_BAUD_RATE, wcb_config.h:43) for the Maestro - the DEFAULT, not the port's
    current rate. So only ports already at 9600 take the probes, which then change nothing: no re-open line
    (NaviCore.ino:3236-3273). A port at another rate, and the Maestro at anything but 115200 (57600 on this bench), would
    be re-opened at the default: those are left out and noted. The second clamp, sanBaud at apply time
    (NaviCore.ino:3229-3234), keeps an already corrupt stored config bootable; it is reachable only through a corrupt
    /config.json, which needs the INF9 hook (nccfg.hook_config_unreadable)."""
    problems = []
    with nc_guard(bench) as g:
        nc, b = g.nc, g.before
        ports = [p for p in ("S3", "S4", "S5") if b["auxBaud"][p] == 9600]
        if not ports:
            raise Skip("no aux port is at 9600, the rate an out-of-range baud falls back to")
        left = [p for p in ("S3", "S4", "S5", "maestro") if p not in ports]
        bad = [0, 230400, 1199, 115201]
        rounds = [bad[i:i + len(ports)] for i in range(0, len(bad), len(ports))]
        for vals in rounds:
            sent = dict(zip(ports, vals))
            _, lines = _save(nc, {"auxBaud": sent})
            got = {p: v for p, v in nc.config()["auxBaud"].items() if p in sent}
            if got != {p: 9600 for p in sent}:
                problems.append(f"sent {sent}: read {got}")
            if any(REOPEN.match(x) for x in lines):
                problems.append(f"sent {sent}: a port was re-opened ({[x for x in lines if REOPEN.match(x)]})")
            nc.ping()
        bench.note(f"probed {ports} with {bad}; left alone (not at the fallback rate): {left}")
    assert not problems, "; ".join(problems)


@test("nccfg.clamps_mesh_channel", "wcbNetwork.channel 13 and 0 read 1 and a MAC octet + 256 reads the octet (only while "
      "those equal the current values, so nothing changes); a profile's channel 13 or 0 reads 1, its octets are masked "
      "to a byte, a missing quantity/deviceId read 4/20, and a 7th profile is dropped", needs=["navicore"], links=[])
def clamps_mesh_channel(bench):
    """rc_config.h:1743-1768 (wcbNetwork: channel outside 1-11 falls back to 1, not 11, :1759-1767; octets
    presence-checked and masked, :1749-1750) and :1773-1789 (wcbProfiles, RC_MAX_WCB_PROFILES 6, :461). The wcbNetwork
    probes are built to leave the identity as it is: channel 13 and 0 only when the channel is already 1 (else a stored 1
    would move the droid off its mesh at the next boot, D-NC14), and each octet plus 256, which the mask brings back.
    The profiles are the tool's inert saved identities (the firmware never reads them, :456-460); the guard puts the
    real ones back. deviceId 21 fails at the next boot (ncboot.bad_device_id, opt-in navicore_identity, attended)."""
    problems = []
    with nc_guard(bench) as g:
        nc, b = g.nc, g.before
        net = b["wcbNetwork"]
        if net["channel"] == 1:
            for ch in (13, 0):
                _save(nc, {"wcbNetwork": {"channel": ch, "macOct2": net["macOct2"] + 256,
                                          "macOct3": net["macOct3"] + 512}})
                got = nc.config()["wcbNetwork"]
                if got != net:
                    problems.append(f"wcbNetwork channel {ch} / octets +256: {'; '.join(redacted_diff(net, got))}")
        else:
            bench.note(f"wcbNetwork.channel is {net['channel']}: the channel probes would store 1, so they are left out")
        profiles = [{"name": "HILq", "macOct2": 300, "macOct3": -1, "password": "", "quantity": 3, "deviceId": 7,
                     "channel": 13},
                    {"name": "HILr", "channel": 0}] + [{"name": f"HIL{i}", "password": ""} for i in range(5)]
        _save(nc, {"wcbProfiles": profiles})
        got = nc.config()["wcbProfiles"]
        want = [{"name": "HILq", "macOct2": 44, "macOct3": 255, "password": "", "quantity": 3, "deviceId": 7,
                 "channel": 1},
                {"name": "HILr", "macOct2": 0, "macOct3": 0, "password": "", "quantity": 4, "deviceId": 20,
                 "channel": 1}] + \
               [{"name": f"HIL{i}", "macOct2": 0, "macOct3": 0, "password": "", "quantity": 4, "deviceId": 20,
                 "channel": 1} for i in range(4)]
        problems.append(_mismatch("wcbProfiles", got, want))
    problems = [p for p in problems if p]
    assert not problems, "; ".join(problems)


# ============================================================ what a save does live
@test("nccfg.side_effects_live", "A save re-opens only a port whose baud changed: none for a no-op save, exactly one "
      "'[AUX] S4 re-open @ 19200 baud' for S4; a serial label on S5 reaches W1's ?WDP,DUMP as [WDPIF:N=20,S=3,DEV=...] "
      "within a few seconds", needs=["navicore", "wcb1"], links=[])
def side_effects_live(bench):
    """applyConfigSideEffects (NaviCore.ino:3321-3345) runs after every save: applySerialBauds re-opens a port only when
    its sanitised baud differs from the one applied (:3236-3273), applySbusOut acts only on a change (:3286), and
    rcAdvertiseSerialLabels (:4430-4461) hands the labels to WCB_Client, which re-advertises a changed one promptly; a
    NaviCore v2 S5 is WDP port 3 (rc_config.h:1980-1984). S4 runs at 19200 for a few seconds (nothing is recorded on
    J5); the guard's own save puts it back, with one more re-open."""
    problems = []
    w1 = WCB(bench.dev("wcb1"))
    with nc_guard(bench) as g:
        nc, b = g.nc, g.before
        _, lines = _save(nc, {"chRateHz": b["chRateHz"]})
        noisy = [x for x in lines if REOPEN.match(x) or SBUS_OUT.match(x) or x == INFO_BOARD]
        if noisy:
            problems.append(f"a no-op save printed {noisy}")
        baud = 19200 if b["auxBaud"]["S4"] != 19200 else 38400
        _, lines = _save(nc, {"auxBaud": {"S4": baud}})
        reopened = [x for x in lines if REOPEN.match(x)]
        if reopened != [f"[AUX] S4 re-open @ {baud} baud"]:
            problems.append(f"S4 -> {baud}: re-open lines {reopened}")
        label = marker()
        _save(nc, {"serialLabels": dict(b.get("serialLabels") or {}, S5=label)})
        want, seen, t0 = f"[WDPIF:N={b['wcbNetwork']['deviceId']},S=3,DEV={label}]", False, time.monotonic()
        while time.monotonic() - t0 < 8 and not seen:
            seen = want in [x.rstrip() for x in w1.run("?WDP,DUMP", timeout=8)]
            if not seen:
                time.sleep(1.0)
        if not seen:
            problems.append(f"W1's ?WDP,DUMP has no {want} after 8 s")
        else:
            bench.note(f"W1 listed NaviCore's new S5 label after {time.monotonic() - t0:.1f} s")
    assert not problems, "; ".join(problems)


@test("nccfg.sbus_out_toggle", "sbusOutEnabled applies live: saving it off prints '[SBUS] OUT disabled', saving it on "
      "again prints '[SBUS] OUT enabled — re-emit on GPIO<n>', each once (whatever is on J2 goes without SBUS in "
      "between)", needs=["navicore"], links=[])
def sbus_out_toggle(bench):
    """applySbusOut (NaviCore.ino:3284-3306), called from applyConfigSideEffects on every save, acts only when the flag
    changed (:3286). Off, the SBUS reader drops its passthrough sink; on, it tees each received byte to Serial1 TX again
    (v2 GPIO5). Nothing records what is wired to J2 (hil/servos.py), so a receiver-driven device there may go to its
    failsafe for the second between the two saves: listed in SERVO_TESTS."""
    with nc_guard(bench) as g:
        nc, b = g.nc, g.before
        first = not b["sbusOutEnabled"]
        t0 = time.monotonic()
        _, lines1 = _save(nc, {"sbusOutEnabled": first})
        _, lines2 = _save(nc, {"sbusOutEnabled": not first})
        gap = time.monotonic() - t0
    got1 = [m.group(1) for x in lines1 for m in [SBUS_OUT.match(x)] if m]
    got2 = [m.group(1) for x in lines2 for m in [SBUS_OUT.match(x)] if m]
    want = ["enabled" if first else "disabled", "disabled" if first else "enabled"]
    bench.note(f"SBUS OUT {want[0]} then {want[1]}, {gap:.1f} s apart")
    assert (got1, got2) == ([want[0]], [want[1]]), f"SBUS OUT lines {got1} then {got2}, expected {want}"
    on = next(x for x in lines1 + lines2 if x.startswith("[SBUS] OUT enabled"))
    assert re.match(r"^\[SBUS\] OUT enabled — re-emit on GPIO\d+ \(100k 8E2 inverted\)$", on), on


@test("nccfg.boardtype_change_no_reboot", "A boardType other than the booted profile answers the INFO line before the "
      "ACK and skips the live re-apply (a baud change re-opens nothing) until boardType matches again, when the deferred "
      "baud applies; nothing reboots", needs=["navicore"], links=[])
def boardtype_change_no_reboot(bench):
    """applyConfigSideEffects returns false while rcConfig.boardType differs from the profile applyBoardProfile applied
    at boot (NaviCore.ino:3185, :3205, :3321-3322), and the save prints {"type":"INFO","msg":"boardType changed —
    reboot to apply the new pin profile"} before its ACK (:3940-3941). NaviCore must NEVER restart in the flipped state:
    the SBUS and aux pins would move (the WCB HW 3.2 profile, :3187-3196). The test never reboots, and the guard puts
    boardType back within seconds."""
    problems = []
    with nc_guard(bench) as g:
        nc, b = g.nc, g.before
        cur, other = b["boardType"], 1 if b["boardType"] == 0 else 0
        orig = b["auxBaud"]["S4"]
        baud = 19200 if orig != 19200 else 38400
        for data, info, reopen in (({"boardType": other}, True, []),
                                   ({"boardType": other, "auxBaud": {"S4": baud}}, True, []),
                                   ({"boardType": cur}, False, [f"[AUX] S4 re-open @ {baud} baud"]),
                                   ({"auxBaud": {"S4": orig}}, False, [f"[AUX] S4 re-open @ {orig} baud"])):
            ack, lines = _save(nc, data)
            where = [i for i, x in enumerate(lines) if x == INFO_BOARD]
            ack_at = next((i for i, x in enumerate(lines) if x.startswith('{"type":"ACK","of":"SET_CONFIG"')), None)
            if info and not (where and ack_at is not None and where[0] < ack_at):
                problems.append(f"{data}: no INFO line before the ACK")
            if not info and where:
                problems.append(f"{data}: an INFO line, though boardType matches the booted profile")
            got = [x for x in lines if REOPEN.match(x)]
            if got != reopen:
                problems.append(f"{data}: re-open lines {got}, expected {reopen}")
    assert not problems, "; ".join(problems)


# ============================================================ the command library
@test("nccfg.cmdlib_roundtrip", "SET_CMDLIB stores the library opaquely: the ACK's size and hash are the host's FNV-1a "
      "of the bytes sent, GET_CMDLIB returns those bytes, CMDLIB_META agrees, and keys after data store data alone "
      "(skips when no library is stored: there is no delete verb, D-NC13)", needs=["navicore"], links=[])
def cmdlib_roundtrip(bench):
    """NaviCore.ino:3861-3915, rcCmdlibHash (rc_config.h:2159-2164). The data value is cut out by bracket matching, so
    '{"type":"SET_CMDLIB","data":{...},"x":1}' stores only the object (:3888-3910). The guard writes the stored library
    back byte for byte. With none stored the test skips: the smallest library that can be written is '{}', so 'nothing'
    could not be put back (NAVICORE.md D-NC13)."""
    nc = _nc(bench)
    size, _ = nc.cmdlib_meta()
    if not size:
        raise Skip("NaviCore stores no command library, and none written here could be removed (D-NC13)")
    problems = []
    with nc_guard(bench) as g:
        orig = g.nc.cmdlib().decode("utf-8")
        try:
            lib = json.loads(orig)
        except ValueError:
            raise Skip("the stored command library is not JSON") from None
        tag = marker()
        if isinstance(lib, dict):
            lib2 = dict(lib, hilRoundtrip=f"{tag} é中")
        elif isinstance(lib, list):
            lib2 = lib + [f"{tag} é中"]
        else:
            raise Skip("the stored command library is neither an object nor an array")
        text = json.dumps(lib2, separators=(",", ":"), ensure_ascii=False)
        g.nc.set_cmdlib(text)                             # raises unless ok:true with the host's size and FNV-1a hash
        raw = text.encode("utf-8")
        if g.nc.cmdlib() != raw:
            problems.append("GET_CMDLIB did not return the bytes SET_CMDLIB stored")
        if g.nc.cmdlib_meta() != (len(raw), g.nc.fnv1a32(raw)):
            problems.append(f"CMDLIB_META {g.nc.cmdlib_meta()} disagrees with ({len(raw)}, {g.nc.fnv1a32(raw)})")
        lib3 = json.dumps(dict(lib2, hilTail=tag) if isinstance(lib2, dict) else lib2 + [tag], separators=(",", ":"),
                          ensure_ascii=False)
        m = g.nc.dev.mark()
        g.nc.send_paced('{"type":"SET_CMDLIB","data":' + lib3 + ',"x":1}')
        ack = json.loads(g.nc.dev.expect(r'^\{"type":"ACK","of":"SET_CMDLIB"', timeout=10, since=m).string)
        raw3 = lib3.encode("utf-8")
        if (ack.get("ok"), ack.get("size"), ack.get("hash")) != (True, len(raw3), g.nc.fnv1a32(raw3)):
            problems.append(f"keys after data: ACK ok={ack.get('ok')} size {ack.get('size')}, expected {len(raw3)}")
        if g.nc.cmdlib() != raw3:
            problems.append("keys after data: the stored library is not the data object alone")
        bench.note(f"command library {size} bytes; round-tripped {len(raw)} and {len(raw3)} bytes, then restored")
    assert not problems, "; ".join(problems)


@test("nccfg.cmdlib_errors", "SET_CMDLIB with no data, or a scalar (5, a string, true, null), answers ok:false size 0 "
      "hash 0 and stores nothing; an unbalanced data never reaches it (the header parse fails first); with no library "
      "stored, GET_CMDLIB sends the default {\"boards\":[],\"enums\":{}} while CMDLIB_META says 0/0",
      needs=["navicore"], links=[])
def cmdlib_errors(bench):
    """NaviCore.ino:3879-3915: only a data value starting with '{' or '[' is cut out; anything else leaves ok false
    and 0/0. A line that is not JSON is refused by the filtered header parse before SET_CMDLIB is looked at
    (:3822-3832), so the 'unbalanced' case of the map is an ERROR line, not an ACK. The empty-store pair is
    :3866-3877. Inside nc_guard: a firmware that wrote any of these would be caught (and, with no library before,
    reported - there is no verb to delete one, D-NC13)."""
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        meta = nc.cmdlib_meta()
        no = '{"type":"ACK","of":"SET_CMDLIB","ok":false,"size":0,"hash":0}'
        for line in ('{"type":"SET_CMDLIB"}', '{"type":"SET_CMDLIB","data":5}', '{"type":"SET_CMDLIB","data":"HILx"}',
                     '{"type":"SET_CMDLIB","data":true}', '{"type":"SET_CMDLIB","data":null}'):
            m = nc.dev.mark()
            nc.dev.send(line)
            got = nc.dev.expect(r'^\{"type":"ACK","of":"SET_CMDLIB"', timeout=5, since=m).string.rstrip()
            if got != no:
                problems.append(f"{line}: {got}")
        m = nc.dev.mark()
        line = '{"type":"SET_CMDLIB","data":[1,2}'
        nc.dev.send(line)
        got = nc.dev.expect(r'^\{"type":"(ERROR|ACK)"', timeout=5, since=m).string.rstrip()
        if got != f'{{"type":"ERROR","msg":"JSON parse failed (InvalidInput)","rxLen":{len(line)}}}':
            problems.append(f"unbalanced data: {got}")
        if nc.cmdlib_meta() != meta:
            problems.append(f"CMDLIB_META moved from {meta} to {nc.cmdlib_meta()}")
        if meta == (0, 0):
            got = nc.json_cmd({"type": "GET_CMDLIB"}, r'^\{"type":"CMDLIB"').string.rstrip()
            default = '{"boards":[],"enums":{}}'
            if got != f'{{"type":"CMDLIB","size":{len(default)},"hash":{nc.fnv1a32(default)},"data":{default}}}':
                problems.append(f"empty store: GET_CMDLIB answered {got[:120]}")
    assert not problems, "; ".join(problems)


# ============================================================ the USB JSON surface
@test("nccfg.filter_whitelist", "Each field the header filter keeps reaches its handler: target and cmd (WCB_SEND), on "
      "(CALIB), flags (SET_DEBUG_FLAGS), mode/btn/tap (TRIGGER on an unmapped slot), id (FORGET_PEER 20), wcb and key "
      "(GET_WCB_SEQ/SEQVAL to W1)", needs=["navicore"], links=[])
def filter_whitelist(bench):
    """Every non-SET_CONFIG handler reads a header parsed through a whitelist filter (NaviCore.ino:3809-3824); a field
    missing from it is stripped and reads as its default, silently (NaviCore CLAUDE.md rule 4). One probe per field,
    each built so that a stripped field gives a different answer, and none sends anything anywhere: WCB_SEND to board
    21 is refused before sending, and a 300-character broadcast is refused by WCB_Client (it would otherwise go to every
    board, WCB_Client.cpp:392-402); TRIGGER hits an unmapped slot (rc_trig only); FORGET_PEER 20 names NaviCore itself,
    never a learned peer. 'all' is not probed: its only positive probe forgets every learned peer, an NVS write
    (ncmesh.wdp_learn_forget, opt-in navicore_nvs). The sequence pulls ask W1 over the mesh; any answer but
    'wcb out of range' / 'key required' proves the field arrived, a timeout included."""
    nc = _nc(bench)
    cfg = nc.config()
    problems = []
    nid = cfg["wcbNetwork"]["deviceId"]
    got = nc.ack_line({"type": "WCB_SEND", "target": 21, "cmd": "HILfilter" + "x" * 291})
    if got != '{"type":"ACK","ok":false,"msg":"target 21 out of range (0=broadcast, 1-20=unicast)"}':
        problems.append(f"target: {got}")
    m = nc.dev.mark()
    nc.ack_line({"type": "WCB_SEND", "target": 0, "cmd": "HILfilter" + "x" * 291})
    lines = _flushed(nc, m)
    long_ = [int(x.group(1)) for s in lines for x in [re.search(r"command too long \((\d+) > \d+ chars\)", s)] if x]
    if long_ != [300]:
        problems.append(f"cmd: WCB_Client's refusal saw lengths {long_} (300 sent)")
    try:
        on = nc.cli('{"type":"CALIB","on":true}', until=r"^\[CALIB\] action dispatch", flush=False)
        if "[CALIB] action dispatch SUPPRESSED (calibrating)" not in on:
            problems.append(f"on: {[x for x in on if x.startswith('[CALIB]')]}")
    finally:
        nc.calib(False)
    m = nc.dev.mark()
    nc.set_debug_flags(0x21)
    nc.set_debug_flags(0)
    if [x.rstrip() for x in nc.dev.since(m) if x.startswith("[DBG]")] != ["[DBG] flags=0x21", "[DBG] flags=0x00"]:
        problems.append(f"flags: {[x.rstrip() for x in nc.dev.since(m) if x.startswith('[DBG]')]}")
    key = _inert_keys(cfg, 1, modes=(2, 3, 1))[0]
    mode, btn = divmod(int(key), 100)
    m = nc.dev.mark()
    ack, trig = nc.trigger(mode, btn, 3)
    if not ack.get("ok") or f"[TRIGGER] mode={mode} btn={btn} tap=3" not in [x.rstrip() for x in nc.dev.since(m)]:
        problems.append(f"mode/btn/tap: ACK {ack}, TRIGGER line missing")
    got = nc.ack_line({"type": "FORGET_PEER", "id": nid})
    if got != f'{{"type":"ACK","of":"FORGET_PEER","ok":true,"id":{nid}}}':
        problems.append(f"id: {got}")
    for obj, kind in (({"type": "GET_WCB_SEQ", "wcb": 1}, "WCB_SEQ"),
                      ({"type": "GET_WCB_SEQVAL", "wcb": 1, "key": f"HILNK{nonce()}"}, "WCB_SEQVAL")):
        m = nc.dev.mark()
        nc.dev.send(json.dumps(obj, separators=(",", ":")))
        try:
            reply = json.loads(nc.dev.expect(rf'^\{{"sys":1,"type":"{kind}",', timeout=9, since=m).string)
        except AssertionError:
            problems.append(f"{kind}: no reply in 9 s (NaviCore gives up at 6 s, rc_telemetry.h:334)")
            continue
        if reply.get("msg") in ("wcb out of range", "key required") or reply.get("wcb") != 1:
            problems.append(f"{kind}: {reply.get('msg') or reply.get('wcb')}")
        if kind == "WCB_SEQVAL" and reply.get("ok") and reply.get("key") != obj["key"]:
            problems.append(f"key: echoed {reply.get('key')!r}")
        bench.note(f"{kind} from W1: ok {reply.get('ok')}" + (f", status {reply.get('status')}" if "status" in reply
                                                                else "") + (f", {reply.get('msg')}" if not reply.get("ok")
                                                                            else ""))
        time.sleep(0.5)
    assert not problems, "; ".join(problems)


@test("nccfg.json_errors", "Malformed input: a truncated line gets ERROR IncompleteInput with rxLen 14, '{bad}' "
      "InvalidInput with rxLen 5, a truncated SET_CONFIG an ERROR and never an ACK; {}, a numeric type, get_config, "
      "GET_WCB_META, SET_MODE and wcb_alias get 'unknown type'; 'ping' gets PONG; bare PING and hello get nothing; and "
      "the CLI's prefix quirks", needs=["navicore"], links=[])
def json_errors(bench):
    """processInputLine (NaviCore.ino:3773-3800): only '?', '{', '#' lines and WCB_WEBTOOL_CONFIG_PULL are acted on, so
    a bare PING is dropped without a word (the banner and PROTOCOLS.md:225 say otherwise: NAVICORE.md D-NC36). The
    filtered header parse reports ArduinoJson's code and the received length (:3822-3832), which is how the tool spots
    a line cut short on USB. SET_MODE, GET_WCB_META and wcb_alias exist only on the mesh (rc_telemetry.h:2201-2240), so
    USB answers 'unknown type' (:4254-4256). The CLI: ?OTALOCAL and ?OTA need their comma and are case-sensitive
    (:3360-3361); any '?REC...' matches the recorder (so ?RECORD prints its state, :3444-3595); ?FORGET matches on seven
    characters and a missing id prints the usage (:3371-3382); ?WDP,<not DUMP> is Unknown; ?MGMT,<anything> is consumed
    silently, a line of 400 or more characters with a notice (WCB_Mgmt.h:367-397); '#' lines other than #L<nn> print
    nothing (:3597-3693). Never #L2, #L02, #L20, #L21, ?FORGET,ALL or a ?REC verb that records or saves."""
    nc = _nc(bench)
    problems = []

    def one(line, pattern, timeout=3.0):
        m = nc.dev.mark()
        nc.dev.send(line)
        try:
            return nc.dev.expect(pattern, timeout=timeout, since=m).string.rstrip()
        except AssertionError:
            return None

    def silent(line, window=1.0):
        m = nc.dev.mark()
        nc.dev.send(line)
        time.sleep(window)
        rx = re.compile(r'^(\{"type":"(PONG|ERROR|ACK)"|Unknown command|Unknown #L code|\[mgmt\]|Mode=|\[REC\])')
        return [x for x in nc.dev.since(m) if rx.search(x)]

    for line, want in (('{"type":"PING"', '{"type":"ERROR","msg":"JSON parse failed (IncompleteInput)","rxLen":14}'),
                       ("{bad}", '{"type":"ERROR","msg":"JSON parse failed (InvalidInput)","rxLen":5}')):
        got = one(line, r'^\{"type":"ERROR"')
        if got != want:
            problems.append(f"{line}: {got}")
    trunc = '{"type":"SET_CONFIG","saveId":3,"data":{"chRateHz":5}'
    got = one(trunc, r'^\{"type":"(ERROR|ACK)"')
    if got != f'{{"type":"ERROR","msg":"JSON parse failed (IncompleteInput)","rxLen":{len(trunc)}}}':
        problems.append(f"truncated SET_CONFIG: {got}")
    for line in ("{}", '{"type":5}', '{"type":"get_config"}', '{"type":"GET_WCB_META"}',
                 '{"type":"SET_MODE","mode":1}', '{"type":"wcb_alias","id":1,"alias":"HIL"}'):
        got = one(line, r'^\{"type":"(ERROR|ACK|PONG|CONFIG)')
        if got != '{"type":"ERROR","msg":"unknown type"}':
            problems.append(f"{line}: {got}")
    if not (one('{"type":"ping"}', r'^\{"type":"PONG"') or "").startswith('{"type":"PONG","version":"'):
        problems.append("lowercase ping: no PONG")
    for line in ("PING", "hello", "#X", "#L", "?MGMT,FOO", "?MGMT,PULL,0"):
        noise = silent(line)
        if noise:
            problems.append(f"{line!r} answered {noise[:2]}")
    for line, want in (("?OTALOCAL", "Unknown command: ?OTALOCAL"), ("?otalocal,status", "Unknown command: ?otalocal,status"),
                       ("?WDP,FOO", "Unknown command: ?WDP,FOO"), ("?HILNOPE", "Unknown command: ?HILNOPE"),
                       ("?FORGET", "[WCB] usage: ?FORGET,<id 1-20>  or  ?FORGET,ALL"),
                       ("?forgetx", "[WCB] usage: ?FORGET,<id 1-20>  or  ?FORGET,ALL"),
                       ("#L99", "Unknown #L code 99. Valid: 1,2,9,10,11,12,13,20,21")):
        got = one(line, re.escape(want) + "$")
        if got != want:
            problems.append(f"{line}: no {want!r}")
    if not one("?RECORD", r"^\[REC\] state=\S+  events="):
        problems.append("?RECORD did not print the recorder's state")
    got = one("?MGMT," + "X" * 450, r"^\[mgmt\] line too long")
    if got != "[mgmt] line too long (450 B) - dropped":
        problems.append(f"a 450-character ?MGMT line: {got}")
    got = one("#L12", r"^Mode=")
    if not got or not re.fullmatch(r"Mode=[1-3]  matrixBtn=\d+  matrixVal=-?\d+", got):
        problems.append(f"#L12: {got!r}")
    assert not problems, "; ".join(problems)


@test("nccfg.line_framing_paced", "A ~20 KB SET_CONFIG written in 512-byte pieces 4 ms apart gets its ACK (and saves "
      "nothing new); a 100 KB '{' line is cut at 98304 characters and answered ERROR IncompleteInput rxLen 98304",
      needs=["navicore"], links=[])
def line_framing_paced(bench):
    """handleSerialInput (NaviCore.ino:4266-4294): the line buffer stops growing at 98304 characters and the rest of
    the line is read and dropped (:4291); the USB RX ring is 8 KB (:4500), so anything longer goes paced, as the tool
    writes it (config_tool/index.html:5313-5321, NaviCore.send_paced). The 20 KB line is the current chRateHz plus an
    unknown key rcConfigFromJSON never reads, so the save rewrites the same config. The 100 KB line's type is unknown
    and its string never closes inside the first 98304 characters, so the header parse ends IncompleteInput. The
    buffer keeps its ~98 KB capacity afterwards (PSRAM)."""
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        t0 = time.monotonic()
        ack, _ = _save(nc, {"chRateHz": g.before["chRateHz"], "hilPad": "x" * 20000}, check=False)
        took = time.monotonic() - t0
        if not ack.get("ok"):
            problems.append(f"the 20 KB SET_CONFIG: ACK {ack}")
        if nc.config(raw=True) != g.before_text:
            problems.append("the 20 KB SET_CONFIG changed the config")
        m = nc.dev.mark()
        t0 = time.monotonic()
        nc.send_paced('{"type":"HILX","pad":"' + "y" * 100000 + '"}')
        try:
            got = nc.dev.expect(r'^\{"type":"ERROR"', timeout=30, since=m).string.rstrip()
        except AssertionError:
            got = None
        if got != '{"type":"ERROR","msg":"JSON parse failed (IncompleteInput)","rxLen":98304}':
            problems.append(f"the 100 KB line: {got}")
        nc.ping()
        bench.note(f"20 KB SET_CONFIG ACKed in {took:.1f} s; the 100 KB line answered in {time.monotonic() - t0:.1f} s")
    assert not problems, "; ".join(problems)


@test("nccfg.monitor_stream", "START_MONITOR streams PWM_UPDATE at up to 20 Hz (about 40 in 2 s) with matrixCh, modeCh "
      "(the mode switch's channel), the mode and all 24 channels equal to #L09's; STOP_MONITOR ends it at once",
      needs=["navicore"], links=[])
def monitor_stream(bench):
    """sendPWMUpdate (NaviCore.ino:3119-3169), every WS_MONITOR_INTERVAL_MS (50 ms, :506) while the monitor is on; a
    frame the USB TX ring cannot take is dropped, not queued (:3159-3168), so a few may be missing. modeCh is
    switches[funcBindings.mode].channel (:3137-3140). STOP_MONITOR clears the flag (and a calibration, :3965-3970), so
    no frame follows its ACK. RAM only; the SBUS controller holds every channel still, so the last frame's channels
    equal #L09's."""
    nc = _nc(bench)
    cfg = nc.config()
    # #L09's fps is counted over a window, so a stall just before (nccfg.line_framing_paced keeps NaviCore reading a
    # 100 KB line for about a second) reads low for a moment: 73 fps in run 20260928-154700. Up to 4 s to recover.
    if nc.sbus_full_rate(4.0)["fps"] < SBUS_FULL_FPS:
        raise Skip("NaviCore sees no full-rate SBUS stream: the frames would carry no channels to compare")
    m = nc.dev.mark()
    nc.ack({"type": "START_MONITOR"})
    time.sleep(2.0)
    stop = nc.dev.mark()
    nc.ack({"type": "STOP_MONITOR"})
    time.sleep(0.3)
    lines = [x.rstrip() for x in nc.dev.since(m)]
    frames = [json.loads(x) for x in lines if x.startswith('{"type":"PWM_UPDATE"')]
    tail = [x.rstrip() for x in nc.dev.since(stop)]
    ack_at = next((i for i, x in enumerate(tail) if x == '{"type":"ACK","ok":true}'), len(tail))
    late = [x for x in tail[ack_at + 1:] if x.startswith('{"type":"PWM_UPDATE"')]
    dump, mode = nc.sbus_dump(), nc.mode()
    mode_ch = cfg["switches"][SWITCHES[cfg["funcBindings"]["mode"]]]["channel"] \
        if 0 <= cfg["funcBindings"]["mode"] < 10 else 0
    problems = []
    if not 25 <= len(frames) <= 42:
        problems.append(f"{len(frames)} frames in 2 s (about 40 expected)")
    for i, f in enumerate(frames):
        sb = f.get("sbus") or {}
        if (f.get("matrixCh"), f.get("modeCh"), f.get("mode")) != (cfg["matrixChannel"], mode_ch, mode):
            problems.append(f"frame {i}: matrixCh/modeCh/mode {f.get('matrixCh')}/{f.get('modeCh')}/{f.get('mode')}, "
                            f"expected {cfg['matrixChannel']}/{mode_ch}/{mode}")
            break
        if not sb.get("ok") or len(sb.get("channels") or []) != min(sb.get("chCount", 0), 24):
            problems.append(f"frame {i}: sbus ok {sb.get('ok')}, {len(sb.get('channels') or [])} channels of "
                            f"{sb.get('chCount')}")
            break
    if frames and frames[-1]["sbus"]["channels"] != dump["channels"]:
        diff = [i + 1 for i, (a, b) in enumerate(zip(frames[-1]["sbus"]["channels"], dump["channels"])) if a != b]
        problems.append(f"channels differ from #L09 on CH{diff}")
    if late:
        problems.append(f"{len(late)} PWM_UPDATE frame(s) after the STOP_MONITOR ACK")
    bench.note(f"{len(frames)} PWM_UPDATE frames in 2 s, {frames[-1]['sbus']['chCount'] if frames else 0} channels")
    assert not problems, "; ".join(problems)


@test("nccfg.version_surfaces", "One firmware version everywhere NaviCore reports it - USB PONG, ?version, "
      "?OTALOCAL,STATUS, its own WDP row, the mesh PONG, rc_hb and W1's WDP row - in the form v<x.y.z>_<DTG>",
      needs=["navicore", "wcb1"], links=[])
def version_surfaces(bench):
    """FW_VERSION (fw_version.h:28-30) reaches seven surfaces: the USB PONG (NaviCore.ino:3841-3843), ?version
    (WCB_Mgmt.h printVersion), ?OTALOCAL,STATUS 'Firmware:' (navicore_ota.h:231), the WDP identity (NaviCore.ino:4877;
    both ?WDP,DUMP rows), the mesh PONG and rc_hb (rc_telemetry.h:2186-2195, :1677-1686). One stale copy is how an
    image mix-up hides. The DTG changes only on a NaviCore commit, so equal strings do not prove the same image
    (INF9 a adds the ELF SHA line)."""
    vs = _nc(bench).version_surfaces(WCB(bench.dev("wcb1")))
    bench.note("version surfaces: " + ", ".join(f"{k} {v}" for k, v in vs.items()))
    silent = [k for k, v in vs.items() if v is None]
    assert not silent, f"no version from {silent}"
    assert len(set(vs.values())) == 1, f"the surfaces disagree: {vs}"
    v = next(iter(vs.values()))
    assert VERSION.fullmatch(v), f"version {v!r} is not v<x.y.z>_<ddhhmm><zone><MON><yy>"


def _backup_tokens(nc, line):
    """The ?TOKEN lines NaviCore prints for `line` (?backup, ?BACKUP, WCB_WEBTOOL_CONFIG_PULL), ?EPASS hashed."""
    m = nc.dev.mark()
    nc.dev.send(line)
    nc.dev.send("#L12")
    nc.dev.expect(r"^-+ End of Backup -+", timeout=4, since=m)
    return [redact_token(x.strip()) for x in nc.dev.since(m) if x.startswith("?")]


@test("nccfg.backup_block", "?backup, ?BACKUP and WCB_WEBTOOL_CONFIG_PULL print the same WCB-style block: ?HW,32, the "
      "MAC octets, ?WCB, ?RELAY,1, ?ALIAS,NaviCore, ?WCBQ, ?EPASS and ?CMDCHAR,; - equal to GET_CONFIG's wcbNetwork "
      "(the password compared by hash, never shown)", needs=["navicore"], links=[])
def backup_block(bench):
    """WcbMgmt::printBackup (WCBClient WCB_Mgmt.h:181-205) with the identity NaviCore gives it at boot
    (NaviCore.ino:4844-4857): the octets, id and quantity are boot copies, ?EPASS a pointer to the live password. The
    Wizard files a board by this block, and WCB_WEBTOOL_CONFIG_PULL is matched before the '?' branch (:3789-3792) with no
    function prefix. ?EPASS is compared through its hash (checkpoint.redact_token) and appears nowhere."""
    nc = _nc(bench)
    net = nc.config()["wcbNetwork"]
    want = ["?HW,32", f"?MAC,2,{net['macOct2']:02X}", f"?MAC,3,{net['macOct3']:02X}", f"?WCB,{net['deviceId']}",
            "?RELAY,1", "?ALIAS,NaviCore", f"?WCBQ,{net['quantity']}", redact_token("?EPASS," + net["password"]),
            "?CMDCHAR,;"]
    problems = []
    for line in ("?backup", "?BACKUP", "WCB_WEBTOOL_CONFIG_PULL"):
        got = _backup_tokens(nc, line)
        if got != want:
            diff = [f"{w or '-'} -> {x or '-'}" for w, x in zip(want + [None] * 9, got + [None] * 9) if w != x][:4]
            problems.append(f"{line}: {'; '.join(diff)}")
    assert not problems, "; ".join(problems)


# One action per SET_DEBUG_FLAGS bit (NaviCore.ino:495-501) whose only effect is its dispatch line: a Maestro verb no
# Maestro knows on remote slot 4 (executeMaestroCmd ignores it, :1295-1373), a WLED command that is not ;L (:1953-1956),
# an HCR/MP3/DFPlayer fn 99 that every transport refuses (:1592-1706, :1765-1802, :1890-1925), a serial port S9 that
# writes nothing (:2081-2089). DBG_WCB has no silent path: its action is a unicast to W1 of ';S0,<marker>', which W1
# echoes on its own USB console.
FLAG_ACTIONS = ((DBG_MAESTRO, "Maestro", {"type": "maestro", "target": "4", "cmd": "HILnoverb"}),
                (DBG_WCB, "WCB", {"type": "wcb_unicast", "target": "1", "cmd": ";S0,HILDBG"}),
                (DBG_WLED, "WLED", {"type": "wled", "cmd": "HILnotL"}),
                (DBG_HCR, "HCR", {"type": "hcr", "fn": 99, "chan": 0, "track": 0}),
                (DBG_MP3, "MP3", {"type": "mp3", "fn": 99, "track": 0}),
                (DBG_SERIAL, "Serial", {"type": "serial", "port": "S9", "cmd": "HILser"}),
                (DBG_DFP, "DF", {"type": "dfplayer", "fn": 99, "chan": 0, "track": 0}))


@test("nccfg.debug_flag_bits", "SET_DEBUG_FLAGS gates each [DISPATCH] family by its own bit: with only that bit the "
      "line prints, with every other bit it does not (Maestro, WCB, WLED, HCR, MP3, Serial, DFPlayer)",
      needs=["navicore"], links=[])
def debug_flag_bits(bench):
    """dlog(bit, ...) prints only when the bit is set in g_dbgFlags (NaviCore.ino:489-503), through the non-blocking
    writer. Each bit is exercised by a TEST_ACTION (FLAG_ACTIONS) that dispatches nothing, except DBG_WCB's, which sends
    W1 one ';S0,' echo. A skipped action answers ok:false with the skip line in msg whatever the flags (dskip,
    NaviCore.ino:545; D-NC20), DBG_WCB's ok:true. RAM only, and back to 0 at the end (the flags also clear at boot)."""
    nc = _nc(bench)
    problems = []
    try:
        for bit, family, action in FLAG_ACTIONS:
            for flags, want in ((bit, True), (0x7F & ~bit, False)):
                nc.set_debug_flags(flags)
                m = nc.dev.mark()
                ack = nc.test_action(action)
                lines = _flushed(nc, m, settle=0.2)
                hit = [x for x in lines if x.startswith(f"[DISPATCH] {family}")]
                if family == "WCB" and ack.get("ok") is not True:
                    problems.append(f"{family}: TEST_ACTION ACK {ack}")
                elif family != "WCB" and (ack.get("ok") is not False or not ack.get("msg")):
                    problems.append(f"{family}: a skipped action answered {ack}, not ok:false with its msg (D-NC20)")
                if bool(hit) != want:
                    problems.append(f"{family} with flags 0x{flags:02X}: {'no' if want else 'a'} [DISPATCH] {family} line")
    finally:
        nc.set_debug_flags(0)
    assert not problems, "; ".join(problems)


@test("nccfg.unvalidated_echo", "Fields stored as sent with no range check read back as sent: txModel 7, maeGateMs "
      "65000, a band 5000-6000 on an inert slot, modeReport.wcb and statsReport.wcb 99; a WLED id of 300 comes back "
      "masked to 44, not refused", needs=["navicore"], links=[])
def unvalidated_echo(bench):
    """rc_config.h:1503 (txModel), :1514 (maeGateMs), :1573-1575 (a band), :1881 and :1897 (the report targets, checked
    only when used: rc_telemetry.h:1258, :1298), :1831-1833 (the WLED fields, `& 0xFF`). Chosen because each is inert
    here: txModel only labels rc_hb and the mesh PONG, maeGateMs only times the skip-if-running gate of actions nothing
    fires, no SBUS value reaches a band above 2047, a report target of 99 sends nothing, and the WLED slot stays
    unconfigured. The others the map lists as unvalidated - matrixChannel, funcBindings.mode, knob and switch channels -
    index SBUS arrays live, so an out-of-range value is not written to the dome's controller; boardType is
    nccfg.boardtype_change_no_reboot and ncboot.boardtype2_mismatch (D-NC19)."""
    problems = []
    with nc_guard(bench) as g:
        nc, b = g.nc, g.before
        slot = next((i for i in range(35, -1, -1) if (b["thresholds"][i]["minPwm"], b["thresholds"][i]["maxPwm"]) ==
                     (0, 0)), None)
        change = {"txModel": 7, "maeGateMs": 65000, "modeReport": {"wcb": 99}, "statsReport": {"wcb": 99}}
        if slot is not None:
            ths = copy.deepcopy(b["thresholds"])
            ths[slot].update(minPwm=5000, maxPwm=6000)
            change["thresholds"] = ths
        wled_free = not b["wledSlots"][1]["configured"] and b["wledSlots"][1]["id"] == 0
        if wled_free:
            change["wledSlots"] = [b["wledSlots"][0], {"id": 300, "port": 3, "wcb": 0, "configured": False},
                                   b["wledSlots"][2], b["wledSlots"][3]]
        _save(nc, change)
        a = nc.config()
        checks = [("txModel", a["txModel"], 7), ("maeGateMs", a["maeGateMs"], 65000),
                  ("modeReport.wcb", a["modeReport"]["wcb"], 99), ("statsReport.wcb", a["statsReport"]["wcb"], 99),
                  ("modeReport.enabled", a["modeReport"]["enabled"], b["modeReport"]["enabled"])]
        if slot is not None:
            checks.append((f"thresholds[{slot}]", (a["thresholds"][slot]["minPwm"], a["thresholds"][slot]["maxPwm"]),
                           (5000, 6000)))
        if wled_free:
            checks.append(("wledSlots[1]", a["wledSlots"][1], {"id": 44, "port": 3, "wcb": 0, "configured": False}))
        for label, got, want in checks:
            problems.append(_mismatch(label, got, want))
    problems = [p for p in problems if p]
    assert not problems, "; ".join(problems)


@test("nccfg.set_nonobject_data", "A SET_CONFIG whose data is not an object: over USB it answers ok:true and saves "
      "the unchanged config (5, and a string); bridged through W1 it is dropped with a log line and no ACK",
      needs=["navicore", "wcb1"], links=[])
def set_nonobject_data(bench):
    """USB (NaviCore.ino:3924-3953): data.as<JsonObject>() is null, so every containsKey in rcConfigFromJSON is false
    and it returns true (rc_config.h:1904) - the config is saved as it is. The bridge (rc_telemetry.h:1120-1124) checks
    is<JsonObject>() and drops the message with '[RC] reassembled SET_CONFIG missing 'data' object' and no ACK, so the
    tool's save over the bridge would time out. The transports disagree; the tool never sends this. Both ends leave
    the config byte-identical."""
    problems = []
    w1 = WCB(bench.dev("wcb1"))
    with nc_guard(bench) as g:
        nc = g.nc
        n = len(g.before_text.encode("utf-8"))
        for data, sid in (("5", 5501), ('"HILstr"', 5502)):
            ack, lines = _save(nc, data, save_id=sid, check=False)
            saved = [int(s.group(1)) for x in lines for s in [SAVED.match(x)] if s]
            if (ack.get("ok"), ack.get("saveId")) != (True, sid) or saved != [n]:
                problems.append(f"USB data {data}: ACK {ack}, saved {saved}")
        m = nc.dev.mark()
        r = bridged(w1, f'{{"type":"SET_CONFIG","saveId":5503,"data":5}}', r'"of":"SET_CONFIG"', timeout=5)
        lines = _flushed(nc, m)
        if r.match:
            problems.append(f"bridged data 5 was ACKed: {r.match.string[:100]}")
        if "[RC] reassembled SET_CONFIG missing 'data' object" not in lines:
            problems.append("bridged data 5: no 'missing data object' line on NaviCore's console")
        if any(SAVED.match(x) for x in lines):
            problems.append("bridged data 5 saved the config")
        if nc.config(raw=True) != g.before_text:
            problems.append("the config changed")
    assert not problems, "; ".join(problems)


@test("nccfg.dest_null_hazard", "(should) An empty or null mp3Dest/dfpDest object reads as off, not as an enabled "
      "device (WCB 2 for MP3, local S3 for the DFPlayer)", needs=["navicore"], links=[])
def dest_null_hazard(bench):
    """NAVICORE.md D-NC22. A present-but-empty destination takes the `| default` of its transport: "wcb" for mp3Dest
    (rc_config.h:1793), "serial" for dfpDest (:1807) and hcrDest (:1729), so {} or null ENABLES the device - at WCB 2,
    or on NaviCore's own S3 - while the user meant nothing. The tool's _diffConfigBranches sends null for a baseline-only
    branch (config_tool/index.html:16567-16570). hcrDest is left out here: it drives the HCR-volume knobs S1/S2 live.
    Both are off on this bench, and for the second or two before the guard restores them a device would fire only if
    an action for it ran; none does."""
    with nc_guard(bench) as g:
        _save(g.nc, {"mp3Dest": {}, "dfpDest": None})
        a = g.nc.config()
    got = {k: a[k].get("transport") for k in ("mp3Dest", "dfpDest")}
    bench.note(f"after {{}} / null: mp3Dest {a['mp3Dest']}, dfpDest {a['dfpDest']}")
    assert got == {"mp3Dest": "off", "dfpDest": "off"}, f"an empty/null destination enabled the device: {got}"


def _mode_of(v):
    """The mode a bound mode-switch value decodes to (NaviCore.ino:2790)."""
    return 1 if v < 582 else 2 if v < 1401 else 3


def _defaults_live_effects(nc, cfg):
    """What rcConfigLoadDefaults would decode from the SBUS input NaviCore sees now (#L09), for a test that sends
    RESET_DEFAULTS and restores by SET_CONFIG rather than a restart: [] when nothing, else one line per effect.

    A button held on the default matrix channel 7, inside a default band (pwmToButton, NaviCore.ino:568-579): the
    press is parked while it is down (checkDeferredTap, :2392-2415), the defaults hold no long-press tier to consume it
    (rcHoldTierConfigured, :2347-2351), and the restore's SET_CONFIG resets only the matrix debounce (:3942-3952), not
    the parked tap. So when the restore moves the matrix back to a channel that reads neutral, the release re-times the
    tap (rcMatrixRelease, :2367-2390) and it fires the RESTORED mapping of that button - an action on the droid.
    A mode change: the default mode switch (SE on CH12) reads a value more than 5 away from the bound one, and the mode
    it decodes (or the one the restore re-derives from the bound switch) is not the current one (:2787-2798): the
    mode-aware knobs re-arm and the mode is broadcast. Both are RAM state a restart clears, so the navicore_reboot
    tests need no such check. The bench matches the defaults on both counts (matrix CH7, the default bands, SE on
    CH12) as of 2026-09-28. NAVICORE.md D-NC44."""
    ch = nc.sbus_dump()["channels"]
    out = []
    mx = DEFAULTS["matrixChannel"]
    v = ch[mx - 1]
    hit = next((b[0] for b in DEFAULT_BANDS if (b[1], b[2]) != (0, 0) and b[1] <= v <= b[2]), None)
    if hit:
        out.append(f"the default matrix channel CH{mx} reads {v}, inside the default band {hit!r}: a held button")
    mode = nc.mode()
    idx = (cfg.get("funcBindings") or {}).get("mode", -1)
    cur_ch = cfg["switches"][SWITCHES[idx]]["channel"] if 0 <= idx < len(SWITCHES) else 0
    cur_v = ch[cur_ch - 1] if 1 <= cur_ch <= 24 else None
    def_ch = SWITCH_DEFAULT_CH[DEFAULTS["funcBindings"]["mode"]]
    def_v = ch[def_ch - 1]
    if cur_v is None or abs(def_v - cur_v) > 5:
        modes = {_mode_of(def_v)} | ({_mode_of(cur_v)} if cur_v is not None else set())
        if modes != {mode}:
            out.append(f"the default mode switch (CH{def_ch}, {def_v}) or the restore would move the mode off {mode}")
    return out


@test("nccfg.mesh_creds_live_split", "(should) While NaviCore's mesh password in RAM differs from the one it booted "
      "with, the remote terminal keeps answering: ;W20,?version gets its [TERM:20] lines back as the ETM path still "
      "delivers the command (a throwaway mesh password, saved over USB and put back by the guard)",
      needs=["navicore", "wcb1"], links=[])
def mesh_creds_live_split(bench):
    """NAVICORE.md D-NC17. WCB_Client copies the mesh password at boot and the ETM stack keeps that copy; the RTERM
    reply, OTA auth and ACKs and WcbMgmt read rcConfig.wcbNetwork.password live, so after an unrebooted password change
    a relayed ;W20,?version still ran on NaviCore - its output appears on NaviCore's own USB, the capture tee - but the
    [TERM:20] reply carried the other password and W1 dropped it. NaviCore 0169cb9 gives those paths the boot copy
    (g_meshPasswordBoot). The split is made the way a user makes it: a throwaway password saved over USB, with no reboot
    (Greg's OK, 2026-10-07: this bench's rule was that no test changes a board's mesh password). nc_guard puts the
    saved config back with the bench's password (step 2 of its restore, no reboot), and it wrote the snapshot to the
    run folder first, so an aborted run is restored by the resume. The password is never printed (runner REDACT_KINDS).
    RESET_DEFAULTS made the split before D-NC16's fix, which keeps the network identity through a reset."""
    w1 = WCB(bench.dev("wcb1"))
    nc = _nc(bench)
    nid = nc.wcb_status()["self"]

    def relayed():
        wm, nm = w1.dev.mark(), nc.dev.mark()
        w1.send(f";W{nid},?version")
        try:
            w1.dev.expect(rf"^\[TERM:{nid}\]Software Version:", timeout=5, since=wm)
            term = True
        except AssertionError:
            term = False
        ran = any(x.startswith("Software Version:") for x in _flushed(nc, nm, settle=0.2))
        return term, ran

    term0, _ = relayed()
    if not term0:
        raise Skip(f";W{nid},?version gets no [TERM:{nid}] even before the change: the remote terminal is down")
    with nc_guard(bench) as g:
        throwaway = "HILmc" + secrets.token_hex(5)
        g.nc.set_config({"wcbNetwork": {"password": throwaway}})
        if g.nc.config()["wcbNetwork"]["password"] != throwaway:
            raise AssertionError("NaviCore did not take the throwaway mesh password over USB")
        term, ran = relayed()
    term1, _ = relayed()
    bench.note(f"with a throwaway mesh password in RAM and saved: ;W{nid},?version ran on NaviCore {ran}, "
               f"[TERM:{nid}] on W1 {term}; after the guard put the bench's back, [TERM:{nid}] {term1}")
    assert term1, f"after the guard restored the config, ;W{nid},?version still gets no [TERM:{nid}] on W1"
    assert ran, "NaviCore never ran the relayed ?version: the ETM path was down too, so no split was shown"
    assert term, (f"the relayed ?version ran on NaviCore but its [TERM:{nid}] reply never reached W1: the RTERM reply "
                  f"carries the RAM password while the ETM stack still uses the boot copy")


@test("nccfg.usb_no_late_reply", "800 back-to-back PINGs are each answered before the next is sent: no reply is held "
      "on NaviCore's USB until more output follows", needs=["navicore"], links=[])
def usb_no_late_reply(bench):
    """The HWCDC late-reply stall: a finished line could sit in the USB-Serial/JTAG TX FIFO until the next write
    released it (docs/HIL_TESTING.md §5), so a request/reply host waited the full timeout. kickUsbCdcTx() flushes every
    20 ms in the working-tree image (results/builds/FLASHED.md). Each PING waits up to 1 s for its PONG; the test stops
    after five late ones."""
    nc = _nc(bench)
    late, slow, t0 = [], 0.0, time.monotonic()
    for i in range(800):
        m = nc.dev.mark()
        t = time.monotonic()
        nc.dev.send('{"type":"PING"}')
        try:
            nc.dev.expect(r'^\{"type":"PONG"', timeout=1.0, since=m)
            slow = max(slow, time.monotonic() - t)
        except AssertionError:
            late.append(i)
            if len(late) >= 5:
                break
    bench.note(f"{i + 1} PINGs in {time.monotonic() - t0:.1f} s, slowest answer {slow * 1000:.0f} ms, late {late}")
    assert not late, f"PINGs {late} got no PONG within 1 s (held until the next write?)"


# ============================================================ restarts (opt-in navicore_reboot)
@test("nccfg.persist_reboot", "A save writes GET_CONFIG's exact bytes ('RC config saved to LittleFS (N bytes)', N the "
      "data's length) and a REBOOT brings back a byte-identical config, command library and clip list (NaviCore "
      "restarts)", needs=["navicore"], links=[], opt_in="navicore_reboot")
def persist_reboot(bench):
    """rcConfigSaveLFS writes rcConfigToJSON's output, the same bytes GET_CONFIG prints, to a temp file, checks its size
    on flash and renames it (rc_config.h:2039-2081); setup() loads defaults and then /config.json over them
    (rc_config.h:2085-2100, NaviCore.ino:4563-4586). Any difference after the restart is a load/save asymmetry. The save
    is a no-op (chRateHz as it is). REBOOT is ACKed, then ESP.restart() after 250 ms (NaviCore.ino:4006-4026): the USB
    port re-enumerates and the mesh loses WCB 20 for a few seconds; SBUS OUT stops meanwhile (SERVO_TESTS)."""
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        meta, clips = nc.cmdlib_meta(), sorted(n for n, _, _, _ in nc.rec_ls())
        _, lines = _save(nc, {"chRateHz": g.before["chRateHz"]})
        saved = [int(s.group(1)) for x in lines for s in [SAVED.match(x)] if s]
        if saved != [len(g.before_text.encode("utf-8"))]:
            problems.append(f"saved-line sizes {saved}, GET_CONFIG data is {len(g.before_text.encode('utf-8'))} bytes")
        boot = parse_boot(nc.reboot("json"))
        if nc.config(raw=True) != g.before_text:
            problems.append("after REBOOT GET_CONFIG differs: " + "; ".join(redacted_diff(g.before, nc.config(),
                                                                                           limit=8)))
        if nc.cmdlib_meta() != meta:
            problems.append(f"after REBOOT CMDLIB_META {nc.cmdlib_meta()}, was {meta}")
        if sorted(n for n, _, _, _ in nc.rec_ls()) != clips:
            problems.append("after REBOOT the clip list differs")
        bench.note(f"REBOOT: reset reason {boot['reset_code']} ({boot['reset']}), banner "
                   f"{'seen' if boot['complete'] else 'lost to the re-enumeration'}")
    assert not problems, "; ".join(problems)


def _defaults_problems(cfg, keep=None):
    """How GET_CONFIG `cfg` departs from rcConfigLoadDefaults (DEFAULTS, DEFAULT_NET); the password is not checked.
    keep: the config before a RESET_DEFAULTS, which keeps IDENTITY (D-NC16): those keys must read as in `keep`, every
    other key as its default. Without it (a boot on defaults) the identity is checked against the defaults too."""
    p = [x for x in (_mismatch(k, cfg.get(k), v) for k, v in DEFAULTS.items() if not (keep and k in IDENTITY)) if x]
    if "serialLabels" in cfg:
        p.append("serialLabels printed (defaults set none)")
    if keep is not None:
        return p + [x for x in (_mismatch(k, cfg.get(k), keep.get(k)) for k in IDENTITY) if x]
    net = {k: v for k, v in (cfg.get("wcbNetwork") or {}).items() if k != "password"}
    if net != DEFAULT_NET:
        p.append(_mismatch("wcbNetwork (password aside)", net, DEFAULT_NET))
    return p


@test("nccfg.reset_defaults_ram", "RESET_DEFAULTS over USB loads every factory default but the network identity into "
      "RAM and saves nothing: a REBOOT brings the saved config back byte-identical (NaviCore restarts)",
      needs=["navicore"], links=[], opt_in="navicore_reboot")
def reset_defaults_ram(bench):
    """rcConfigResetKeepIdentity (rc_config.h:1003-1030) and resetMaestroReleaseState, then ACK; no save. It keeps
    IDENTITY - wcbNetwork with its password, wcbProfiles, boardType, the wifi fields - and loads every other default
    (D-NC16), so that part is checked against the config before it. The side effects the USB path now runs (re-opened
    ports, SBUS OUT) are noted, not asserted. Until the restart the droid runs on defaults (no mappings, no knob
    outputs: nothing moves) with its own network identity. The
    REBOOT is the restore: nothing is written, and the guard then finds the config as it was. A button or mode the
    defaults decode from the live SBUS input meanwhile dies with the restart (tap and mode state are RAM), which is
    why these tests, unlike ncmesh's bridged reset (s43), need no _defaults_live_effects check."""
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        m = nc.dev.mark()
        ack = nc.reset_defaults(check=False)
        cfg = nc.config()
        lines = [x.rstrip() for x in nc.dev.since(m)]
        if ack != {"type": "ACK", "ok": True}:
            problems.append(f"RESET_DEFAULTS ACK {ack}")
        problems += _defaults_problems(cfg, keep=g.before)
        same_pw = cfg["wcbNetwork"]["password"] == g.before["wcbNetwork"]["password"]
        reopened = [x for x in lines if REOPEN.match(x) or SBUS_OUT.match(x)]
        if any(SAVED.match(x) for x in lines):
            problems.append("RESET_DEFAULTS saved the config")
        nc.reboot("json")
        if nc.config(raw=True) != g.before_text:
            problems.append("after REBOOT the saved config did not come back: " +
                            "; ".join(redacted_diff(g.before, nc.config(), limit=6)))
        bench.note(f"RESET_DEFAULTS: mesh password {'the same as' if same_pw else 'different from'} the saved one; "
                   f"re-open/SBUS OUT lines on USB {reopened or 'none'} (D-NC16)")
    assert not problems, "; ".join(problems[:10])


@test("nccfg.reset_defaults_keeps_identity", "(should) RESET_DEFAULTS keeps the network identity - wcbNetwork (password "
      "by hash), wcbProfiles, boardType and the wifi fields - so a later save cannot move the droid onto the default "
      "mesh credentials and pin profile (NaviCore restarts)", needs=["navicore"], links=[], opt_in="navicore_reboot")
def reset_defaults_keeps_identity(bench):
    """NAVICORE.md D-NC16. rcConfigLoadDefaults resets the whole block, the compile-time mesh password, deviceId 20,
    quantity 4, boardType 0 and WiFi off included (rc_config.h:801-968), and the next successful SET_CONFIG, for
    anything at all, persists it; the tool's Restore Defaults only re-pulls (index.html:17009-17041). The identity is
    what a user never means to reset with 'factory defaults'. Restored by REBOOT, which loads the untouched save."""
    keys = IDENTITY
    with nc_guard(bench) as g:
        g.nc.reset_defaults()
        cfg = g.nc.config()
        g.nc.reboot("json")
    lost = [redacted_diff({k: g.before.get(k)}, {k: cfg.get(k)}, limit=3) for k in keys if cfg.get(k) != g.before.get(k)]
    assert not lost, "RESET_DEFAULTS reset the identity: " + "; ".join(x for d in lost for x in d)


# ============================================================ INF9 hooks (a NAVICORE_HIL_HOOKS build, opt-in navicore_fault)
@test("nccfg.hook_save_fail", "With the next LittleFS save made to fail (#L93), SET_CONFIG applies to RAM and answers "
      "ok:false 'applied to RAM but could not be saved to flash (LittleFS write error)' with its saveId (hook build "
      "only)", needs=["navicore"], links=[], opt_in="navicore_fault")
def hook_save_fail(bench):
    """NaviCore.ino:3953-3954; the branch nccfg.set_ack_shapes cannot reach on a healthy flash. '#L93' is INF9's
    RAM-only fault verb (NAVICORE.md INF9 b), cleared at boot. The save is a no-op, so RAM and flash agree afterwards;
    the guard checks."""
    with nc_guard(bench) as g:
        if not _hooks(g.nc):
            raise Skip(HOOK_SKIP)
        g.nc.cli("#L93")
        ack, _ = _save(g.nc, {"chRateHz": g.before["chRateHz"]}, save_id=9301, check=False)
    assert ack == {"type": "ACK", "of": "SET_CONFIG", "ok": False,
                   "msg": "applied to RAM but could not be saved to flash (LittleFS write error)", "saveId": 9301}, ack


@test("nccfg.hook_get_config_overflow", "With the next GET_CONFIG made to overflow (#L92), the reply is a top-level "
      "ERROR, never a CONFIG a tool could baseline, and the next GET_CONFIG is whole again (hook build only)",
      needs=["navicore"], links=[], opt_in="navicore_fault")
def hook_get_config_overflow(bench):
    """rcConfigToJSON returns an ERROR envelope when its document overflowed (rc_config.h:1467-1475), and the USB handler
    passes it through at the top level instead of splicing it into "data" (NaviCore.ino:3845-3859): a truncated config
    must never become the tool's baseline. Unreachable without INF9's '#L92'. Nothing is written."""
    nc = _nc(bench)
    if not _hooks(nc):
        raise Skip(HOOK_SKIP)
    nc.cli("#L92")
    m = nc.dev.mark()
    nc.dev.send('{"type":"GET_CONFIG"}')
    got = nc.dev.expect(r'^\{"type":"(ERROR|CONFIG)"', timeout=10, since=m).string
    lines = _flushed(nc, m)
    assert got.startswith('{"type":"ERROR","msg":"GET_CONFIG overflow'), f"an overflowed GET_CONFIG answered {got[:60]}"
    assert "[CONFIG] ERROR: GET_CONFIG overflowed memory — config too large to serialize" in lines, "no overflow line"
    nc.config(raw=True)


@test("nccfg.hook_config_unreadable", "With /config.json corrupted (#L91, a copy kept), NaviCore boots on defaults, "
      "keeps the file and says so; the saved config is then written back and survives a second restart (hook build "
      "only; NaviCore restarts twice)", needs=["navicore"], links=[], opt_in="navicore_fault")
def hook_config_unreadable(bench):
    """rcConfigLoadLFS keeps an unparseable /config.json and runs on defaults for that boot (rc_config.h:2085-2100:
    '[CONFIG] LFS: /config.json parse failed (...) — keeping file, using defaults this boot'), so a user's config is
    never silently replaced. Needs INF9's '#L91' and navicore_reboot too. The restore is the guard's: the snapshot is
    sent back over USB (the diff path) and saved, and a second REBOOT proves it back on flash."""
    _reboot_opted(bench)
    problems = []
    with nc_guard(bench) as g:
        nc = g.nc
        if not _hooks(nc):
            raise Skip(HOOK_SKIP)
        nc.cli("#L91")
        lines = nc.reboot("json")
        if not any("/config.json parse failed" in x and "keeping file, using defaults this boot" in x for x in lines):
            bench.note("the load-failure line was not seen (lost to the USB re-enumeration?)")
        problems += _defaults_problems(nc.config())
        nc.set_config(g.before_text)
        nc.reboot("json")
        if nc.config(raw=True) != g.before_text:
            problems.append("the config written back did not survive the second restart")
    assert not problems, "; ".join(problems[:8])
