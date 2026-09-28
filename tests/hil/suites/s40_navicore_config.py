"""NaviCore's config surface and persistence (docs/hil_plan/NAVICORE.md NC-WP1, ids nccfg.*).

Every test here that writes NaviCore's config does it inside hil/nc_guard.py's nc_guard (decision D-NC2), which
snapshots NaviCore first, restores it afterwards and proves its config byte-identical. nccfg.guard_selftest comes
first: the others trust the guard it proves. The bench NaviCore is the droid's own dome controller, so a test changes
only what has no live effect (tool metadata, a mapping on a matrix slot no SBUS value decodes to), and never its mesh
identity (D-NC14).
"""
import copy
import os
import re

from hil.nc_guard import nc_guard, secrets_of
from hil.runner import REDACT_KINDS, Skip, test
from suites.common import marker

LOG_LINE = re.compile(r"^\s*[0-9.]+\s+(\S+) ([<>#]) (.*)$")      # Bench.log's layout: time, name, direction, text


def _inert_mapping_key(cfg):
    """A mapping key (mode * 100 + slot) with no mapping, on a matrix slot whose band is 0/0: pwmToButton never decodes
    it from SBUS, so only a TRIGGER could fire it (NaviCore.ino:568-579). 136 first (NAVICORE.md §1.4)."""
    maps, bands = cfg.get("mappings") or {}, cfg.get("thresholds") or []
    for btn in range(len(bands), 0, -1):
        b = bands[btn - 1]
        if not isinstance(b, dict) or (b.get("minPwm"), b.get("maxPwm")) != (0, 0):
            continue
        for mode in (1, 2, 3):
            if str(mode * 100 + btn) not in maps:
                return str(mode * 100 + btn)
    raise Skip("no free mapping key on an inert (0/0) matrix slot")


def _slot_without_channels(cfg):
    """(slot, True) for a Maestro slot that prints no "channels" key, slot 3 first (a remote slot nobody hosts on this
    bench, NAVICORE.md §1.2); (3, False) when every slot has some, so that trap is not exercised."""
    maes = cfg.get("maestros") or []
    for slot in [3] + [s for s in range(1, len(maes) + 1) if s != 3]:
        if slot <= len(maes) and "channels" not in maes[slot - 1]:
            return slot, True
    return 3, False


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
