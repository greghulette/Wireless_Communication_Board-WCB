"""The opt-in registry: every bench.json "opt_in" key a test can be gated on (docs/HIL_TESTING.md §2).

A test declares its gate on @test(opt_in="<key>"), and the runner skips it before it starts unless bench.json "opt_in"
names the key, with the Skip message skip_reason() builds. The GUI draws one checkbox per entry, in this order, and
run.py --list tags each gated test. An @test naming a key that is not here fails at import.

Each entry:
    title       the checkbox label
    what        one line: what the tests do to the bench, and why that makes them opt-in
    why         the reason in the Skip message ('opt-in: add "<key>" to bench.json "opt_in" (<why>)'); a test can
                override it with @test(opt_in_why=...), and the reports keep whichever text the test gives
    estimate_s  seconds per test, used until a real result exists (hil/durations.py), or None
    minutes_key a bench.json key whose value, in minutes, sets the length (w1s4_soak: soak_minutes); the estimate then
                always follows it, because a past run's length only reflects the value that run had
"""

OPT_INS = {
    "nvs_wear": {
        "title": "Persistent variable cap",
        "what": "Fills W1's 100-variable table with persistent variables: about 100 whole-blob NVS writes (flash wear).",
        "why": "about 100 whole-blob NVS writes",
        "estimate_s": 20,
    },
    "seq_wipe": {
        "title": "Wipe all sequences",
        "what": "Clears every W1 sequence with ?SEQ,CLEAR,ALL and ?CCLEAR, then replays them; a failed replay loses them.",
        "why": "wipes W1's sequences and replays them",
        "estimate_s": 10,
    },
    "ota_erase": {
        "title": "OTA sessions",
        "what": "Local and relayed OTA sessions on W1 and W2; every accepted BEGIN erases at least 4 KB of the inactive "
                "app slot.",
        "why": "every accepted BEGIN erases at least 4 KB of the inactive app slot, which nothing can restore",
        "estimate_s": 15,
    },
    "ota_full": {
        "title": "Full-image OTA on W1",
        "what": "Streams W1's whole image over USB: a corrupt copy, then two real re-flashes that switch its boot slot "
                "and reboot it.",
        "why": "erases the whole inactive app slot",
        "estimate_s": 300,
    },
    "ota_full_wcb2": {
        "title": "Full-image relay OTA on W2",
        "what": "Re-flashes W2 twice through W1's relay (~7000 frames a pass at 115200), switching its boot slot each "
                "time.",
        "why": "erases and rewrites W2's inactive app slot and switches its boot slot twice",
        "estimate_s": 1500,
    },
    "ota_wrong_chip": {
        "title": "Wrong-chip image (attended)",
        "what": "Streams an ESP32-S3 image to W1; only IDF verify at END keeps it out of the boot slot. Stay at the "
                "bench.",
        "why": "streams an S3 image to W1; only IDF verify stands between it and the boot slot",
        "estimate_s": 200,
    },
    "navicore_clip": {
        "title": "NaviCore clip playback (attended)",
        "what": "Replays a saved NaviCore clip of up to 30 s: servo motion and its recorded actions.",
        "why": "replays a saved clip: servo motion and recorded actions",
        "estimate_s": 30,
    },
    "sbus_reset": {
        "title": "SBUS controller reset",
        "what": "Reboots the SBUS controller to check NaviCore's signal-loss handling.",
        "why": "reboots the SBUS controller",
        "estimate_s": 15,
    },
    "softrx_erratum": {
        "title": "Soft-RX erratum measurement (#78)",
        "what": "About 15 minutes of two- and three-port input into W1 S3-S5 at a sub-bit skew; needs wcb_probe 4.",
        "why": "about 15 minutes of soft-port input into W1 S3-S5; needs wcb_probe 4",
        "estimate_s": 16 * 60,
    },
    "wifi_modes": {
        "title": "WiFi mode changes on W1",
        "what": "W1 turns its access point off and back on, then joins W2's access point and returns to its own: four W1 "
                "reboots, and W1's WiFi is unreachable meanwhile.",
        "why": "changes W1's WiFi mode and reboots it four times",
        "estimate_s": 60,
    },
    "wifi_pc": {
        "title": "PC joins W1's access point (attended)",
        "what": "A WiFi adapter on the PC joins W1's access point for about 30 s, opens the WebSocket endpoint, then returns "
                "to its network. The spare adapter is used when there is one; with a single adapter the PC is offline "
                "meanwhile. The AP password sits in a temporary Windows profile that is deleted after.",
        "why": "a WiFi adapter on this PC leaves its network for about 30 s; run it with someone at the keyboard",
        "estimate_s": 90,
    },
    "mesh_password": {
        "title": "Mesh password on W1 (attended)",
        "what": "Gives W1 a throwaway ESP-NOW password for a few seconds, then its own back from its chain: W1 is off the "
                "mesh meanwhile, and stays off if the run dies before the restore.",
        "why": "takes W1 off the mesh for a few seconds with a throwaway password",
        "estimate_s": 30,
    },
    "nvs_erase": {
        "title": "Erase W1's NVS (attended)",
        "what": "Erases all of W1's settings, checks the factory defaults, then restores every setting from W1's own "
                "chain and its learned peers: three reboots per test, two tests.",
        "why": "erases all of W1's settings and restores them from its chain",
        "estimate_s": 120,
    },
    "nvs_fill": {
        "title": "Fill W1's NVS with sequences",
        "what": "Saves throwaway sequences on W1 until its settings storage refuses one, checks nothing is left "
                "half-saved or listed empty, then removes them. Other NVS writes on W1 may fail for those seconds.",
        "why": "fills W1's settings storage with throwaway sequences, then removes them",
        "estimate_s": 60,
    },
    "w1s4_soak": {
        "title": "W1S4 corruption soak (#78)",
        "what": "Loads W1's S2/S4 fan-out for bench.json soak_minutes (default 20), alternating W1S4's receiver.",
        "why": "loads W1's S2/S4 fan-out for soak_minutes, default 20",
        "estimate_s": None,
        "minutes_key": "soak_minutes",
        "default_minutes": 20,
        "overhead_s": 60,
    },
}


def skip_reason(key, why=None):
    """The Skip detail a gated test gets when its key is off - the same text the tests' own checks always raised."""
    return f'opt-in: add "{key}" to bench.json "opt_in" ({why or OPT_INS[key]["why"]})'


def enabled(cfg):
    """The opt-in keys bench.json names (a malformed value counts as none)."""
    v = (cfg or {}).get("opt_in", [])
    return [k for k in v if isinstance(k, str)] if isinstance(v, list) else []


def gate(cfg, t):
    """The Skip detail for registry entry `t` under bench config `cfg`, or None when it may run."""
    key = t.get("opt_in")
    if key and key not in enabled(cfg):
        return skip_reason(key, t.get("opt_in_why"))
    return None


def estimate_s(key, cfg=None):
    """Seconds per test for `key` with no history, or None. A minutes_key entry follows bench.json."""
    o = OPT_INS[key]
    if o.get("minutes_key"):
        try:
            minutes = float((cfg or {}).get(o["minutes_key"], o["default_minutes"]))
        except (TypeError, ValueError):
            minutes = float(o["default_minutes"])
        return max(0.0, minutes) * 60 + o.get("overhead_s", 0)
    return o.get("estimate_s")


def set_enabled(cfg, key, on):
    """Tick or untick `key` in cfg["opt_in"] in place. Other entries keep their order; a new key goes at the end. The
    key stays in the file when the list empties (an empty list and a missing one mean the same)."""
    if key not in OPT_INS:
        raise KeyError(key)
    cur = cfg.get("opt_in")
    cur = list(cur) if isinstance(cur, list) else []
    if on and key not in cur:
        cur.append(key)
    elif not on:
        cur = [k for k in cur if k != key]
    cfg["opt_in"] = cur
    return cur
