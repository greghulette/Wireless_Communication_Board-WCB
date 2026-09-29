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
    "navicore_clip_write": {
        "title": "NaviCore clip writes",
        "what": "Records, uploads, saves, renames and deletes HIL<nonce> clips on NaviCore's 12 MB clips partition (a "
                "few hundred bytes each, removed again by the test) and replays them; one take runs 60 s to the "
                "recorder's backstop. Greg's own clips are never written: each test fails unless ?REC,LS ends as it "
                "began (docs/hil_plan/NAVICORE.md NC-WP12).",
        "why": "writes and removes HIL* clips on NaviCore's clips partition",
        "estimate_s": 30,
    },
    "navicore_reboot": {
        "title": "NaviCore restarts",
        "what": "Restarts NaviCore in software (REBOOT): its USB port re-enumerates, the mesh loses WCB 20 and SBUS OUT "
                "stops for about 5 s. A software restart never re-samples the boot strap, so it cannot leave the S3 in "
                "ROM download mode (docs/hil_plan/NAVICORE.md §1.3).",
        "why": "restarts NaviCore: the mesh and SBUS OUT lose it for about 5 s",
        "estimate_s": 40,
    },
    "navicore_ota_erase": {
        "title": "NaviCore OTA sessions",
        "what": "Opens 4 KB ?OTALOCAL sessions on NaviCore and ends them by ABORT and by the 30 s idle reaper: each "
                "accepted BEGIN erases the first 4 KB of its inactive app slot, the head of the image there (the "
                "rollback copy); the boot slot never moves.",
        "why": "every accepted BEGIN erases 4 KB of NaviCore's inactive app slot, which nothing restores",
        "estimate_s": 45,
    },
    "navicore_ota_full": {
        "title": "Full-image OTA on NaviCore",
        "what": "Re-flashes NaviCore over USB (?OTALOCAL, hil/ncflash.py) with the bench image it runs, twice: two "
                "~80 s transfers that each switch its boot slot and restart it, ending on the slot it started on.",
        "why": "rewrites NaviCore's inactive app slot and switches its boot slot twice",
        "estimate_s": 240,
    },
    "navicore_ota_relay_full": {
        "title": "Full relay OTA to NaviCore (run by hand)",
        "what": "Re-flashes NaviCore through W1's ?OTA relay with the bench image it runs, twice: about 6100 frames "
                "of 192 bytes a pass, some 12 minutes each, and two restarts.",
        "why": "rewrites NaviCore's inactive app slot through W1's relay and switches its boot slot twice (~25 min)",
        "estimate_s": 1500,
    },
    "navicore_esptool": {
        "title": "NaviCore esptool recovery (watched)",
        "what": "Runs the recovery ladder's esptool rungs on the healthy NaviCore: esptool resets it into ROM download "
                "mode, writes the bench image into app0 and boot_app0.bin into otadata, and boots it. Run it with "
                "someone watching the log: a chip left in download mode needs the ladder's own kick rung.",
        "why": "resets NaviCore into ROM download mode and writes its app0 and otadata with esptool; watched runs only",
        "estimate_s": 90,
    },
    "navicore_identity": {
        "title": "NaviCore mesh identity (attended)",
        "what": "Saves an invalid NaviCore deviceId and restarts it: NaviCore is off the mesh until the test writes "
                "the real one back over USB and restarts it again.",
        "why": "takes NaviCore off the mesh with a saved invalid deviceId until it is restored over USB; attended only",
        "estimate_s": 60,
    },
    "navicore_nvs": {
        "title": "NaviCore learned peers (NVS)",
        "what": "Has NaviCore learn a permanent probe client as a mesh peer, forget it with FORGET_PEER, learn it again "
                "and drop it when it comes back temporary: four writes of NaviCore's learned-peer mask to its NVS (W1 "
                "and W2 learn and forget the probe too). Every board forgets the probe afterwards.",
        "why": "writes NaviCore's learned-peer list to its NVS four times",
        "estimate_s": 60,
    },
    "navicore_aux_tx": {
        "title": "NaviCore device bytes on its own ports",
        "what": "Routes an HCR, MP3 Trigger, DFPlayer or WLED to NaviCore's own S3-S5 for a few seconds and fires their "
                "actions, and runs #L20/#L21: device-protocol bytes go out J4-J6, where nothing is recorded as attached "
                "(docs/hil_plan/NAVICORE.md D-NC14, NC-WP7). A NAVICORE_HIL_HOOKS image's DBG_WIRE log is what reads "
                "them back: no probe is wired to NaviCore.",
        "why": "sends HCR, MP3 Trigger, DFPlayer and WLED bytes out NaviCore's own serial ports, where nothing records "
               "what is attached",
        "estimate_s": 30,
    },
    "navicore_fault": {
        "title": "NaviCore fault hooks (hook build)",
        "what": "Uses the NAVICORE_HIL_HOOKS fault verbs (#L91-#L93: a corrupted /config.json, an overflowing "
                "GET_CONFIG, a failed save) and restores what they break. On any other image the tests skip.",
        "why": "injects faults into NaviCore's config storage (a NAVICORE_HIL_HOOKS build only)",
        "estimate_s": 40,
    },
    "navicore_wifi": {
        "title": "PC joins NaviCore's access point",
        "what": "For each of fifteen tests the PC's spare WiFi adapter joins NaviCore's access point for about 30 s and "
                "uses its WebSocket endpoint (the ping soak streams for 5 minutes), then returns to its network. "
                "NaviCore's SSID and password are read from its config and sit only in a temporary Windows profile that "
                "is deleted after. A PC whose only WiFi adapter carries its internet skips them (D-NC14). With "
                "navicore_reboot ticked too, two restart NaviCore (one saves a 3-character AP password for a boot) and "
                "one may, if a stalled socket crashes it (D-NC62).",
        "why": "the PC's spare WiFi adapter joins NaviCore's access point for about 30 s",
        "estimate_s": 90,
    },
    "navicore_webserial": {
        "title": "NaviCore config tool over real Web Serial (attended)",
        "what": "Hands NaviCore's own port to Chrome and runs its config tool over real Web Serial (a one-time grant in "
                "tests/wizard/.profiles/navicore: the first run asks someone to pick NaviCore's COM port in Chrome's "
                "dialog). Chrome's open restarts NaviCore, and the flash test runs the tool's Full Wipe & Flash with "
                "esptool-js: the custom bootloader, the partition table and the bench image NaviCore already runs go into "
                "0x0, 0x8000 and app0, its NVS (the learned peers, which W1 then re-advertises) and otadata are erased, "
                "and it boots app0; the config and clips partitions are never written. Stay at the bench: a flash that "
                "stops half-way leaves NaviCore in its ROM bootloader until `python -m hil.ncflash recover "
                "--allow-esptool` (from tests/hil) writes the bench image back.",
        "why": "NaviCore's port goes to Chrome over real Web Serial, which restarts it, and one test rewrites its "
               "bootloader, partition table and app and erases its NVS with esptool-js; attended only",
        "estimate_s": 240,
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
        "what": "W1 turns its access point off and back on, joins W2's access point and returns to its own, brings its "
                "access point up under its derived name, loses W2's access point and rejoins it, and looks for an absent "
                "network: up to ten W1 and two W2 reboots across its tests, and W1's WiFi is unreachable meanwhile.",
        "why": "changes W1's WiFi mode and reboots it four times",
        "estimate_s": 60,
    },
    "wifi_pc": {
        "title": "PC joins W1's access point (attended)",
        "what": "For each of six tests a WiFi adapter on the PC joins W1's access point for about 30 s, uses the WebSocket "
                "endpoint (line framing, ?backup, three client slots, a small OTA session with ota_erase, DHCP with no "
                "gateway), then returns to its network. The spare adapter is used when there is one; with a single "
                "adapter the PC is offline meanwhile. The AP password sits in a temporary Windows profile that is "
                "deleted after.",
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
                "chain and its learned peers: three reboots per test (four for the LED pin test), three tests.",
        "why": "erases all of W1's settings and restores them from its chain",
        "estimate_s": 200,
    },
    "nvs_fill": {
        "title": "Fill W1's NVS with sequences",
        "what": "Boots W1 once with its stored sequences in NVS (?DEBUG,SEQNVS, the sequence store's fallback), "
                "saves throwaway sequences until its settings storage refuses one, checks nothing is left half-saved "
                "or listed empty, then removes them and boots back. Other NVS writes on W1 may fail for those seconds.",
        "why": "fills W1's settings storage with throwaway sequences, then removes them (two extra reboots per test)",
        "estimate_s": 90,
    },
    "etm_seq_wrap": {
        "title": "ETM sequence wrap (~7 min flood)",
        "what": "Drives W1's 16-bit ETM sequence counter past 65,535 with about 65,000 untracked JSON broadcasts typed "
                "on its console (its port broadcasts off meanwhile), then checks that a unicast sent right after the "
                "wrap still runs on W2: the mesh carries W1's flood for several minutes, and W2 reboots once.",
        "why": "floods the mesh with about 65,000 JSON broadcasts from W1 for several minutes and reboots W2",
        "estimate_s": 420,
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
    "intellex_reboot": {
        "title": "NaviCore restart through Intellex",
        "what": "Sends NaviCore a REBOOT through Intellex's own serial transport, to see how the transport meets a device "
                "that goes away: NaviCore's USB port re-enumerates, the mesh loses WCB 20 and SBUS OUT stops for about "
                "5 s. Runs inside nc_guard, like every NaviCore restart.",
        "why": "restarts NaviCore through Intellex's transport: the mesh and SBUS OUT lose it for about 5 s",
        "estimate_s": 60,
    },
    "intellex_nc_save": {
        "title": "NaviCore config save through Intellex",
        "what": "Saves NaviCore's config twice from the config tool running inside Intellex: the telemetry rate (chRateHz) "
                "one step away and back, two rewrites of its ~14 KB /config.json on LittleFS. Nothing moves; runs inside "
                "nc_guard, which proves the config byte-identical afterwards.",
        "why": "rewrites NaviCore's /config.json twice (chRateHz one step away and back)",
        "estimate_s": 90,
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
