"""The HIL tests that can move a REAL servo on Greg's bench, for a run with no moving servos: run.py --no-servos, the
GUI's "No moving servos" box, or bench.json "no_servos": true (docs/HIL_TESTING.md §2). The runner skips each one
before it starts with SKIP_REASON, the way it skips an opt-in that is off (hil/optin.py), and never calls its body.

The real servos, from bench.json, docs/HIL_TESTING.md §1/§6 and NaviCore's GET_CONFIG (session.log of run
20260924-190733):
- Maestro 2 on W2 S1 (bench.json "taps"). Reached by ;M2 (W1 and NaviCore hold a proxy for it), ;M0, raw bytes on W2 S1,
  and every Kyber broadcast: both WCBs are Maestro_Remote, so a byte >= 0x80 injected into W1 S1 is a command byte to it
  (s22 docstring), and NaviCore's remote slots 2-8 broadcast there, so knob J4 (SBUS CH4, the left stick's Y) moves it.
- NaviCore's local Maestro, device 1: the dome's pie and panel servos on ch 0-9, J2 on slot 1 ch 0, RS on ch 2-9. W1
  holds an M1:W20 proxy beside its local M1 on the W1 S1 probe, so every ;M1 subroutine or move verb typed on W1 also
  runs on it (maestro.fanout_remote), as does ;W20,;M<id>. NaviCore moves it itself on SET_MODE (mode-aware knobs
  re-dispatch; J2 moves), on a TRIGGER of a mapped slot, and on clip replay.
- Whatever NaviCore's SBUS OUT drives: GET_CONFIG has sbusOutEnabled true, so it re-emits every channel the controller
  moves, and nothing records what is wired there. If nothing is, the SBUS OUT block below can be deleted.

Read and left out, because they reach a real Maestro without moving it:
- get-queries (getPosition, getMovingState, getErrors, ;MG, ?MAE,GET/MOVING): one slot answers; nothing moves.
- kyber.local_s1_legacy_fallback_skips_kyber_port: its moves (;M12; ;M95 is W1-local anyway) follow ?MAESTRO,CLEAR,M1,
  which drops every M1 slot, NaviCore's proxy included (WCB_Maestro.cpp:994-1006), and the test aborts unless that
  clear confirms. What reaches a real Maestro is stopScript. (maestro.clear_all_legacy_routing, whose moves also follow
  a clear, is listed below: it carries on when the clear fails.)
- moves for an id no real Maestro answers (maestro.fallback_*, maestro.get_fallback_unicast_within_wcbq,
  maestro.stale_proxy_dropped, kyber.maestro_s2_single_reader): every ;M path builds a Pololu frame (0xAA, device), so
  the Maestro's device-number filter drops it even if the routing under test went wrong, and NaviCore actuates only its
  type-1 slots (device 1 here).
- kyber.* injections: bytes below 0x80 or a device-2 getErrors frame (the s22 rule); client_mesh.sendraw_exact_bytes'
  setTarget frame goes to W1 S2, a probe; navicore.maestro_skip_not_logged_as_dispatch targets channel 32, which no
  Maestro has (24 at most); navicore.trigger_bounds_usb_vs_mesh skips unless its slot is unmapped.
- ASCII only on a Maestro line or a NaviCore port: navicore.serial_route_dbg, navicore.test_action_*, the input.* and
  kyber.* text injections into W1 S1/S2 (bridged to Maestro 2). A Maestro moves only on a command byte (>= 0x80).
- WCB reboots (boot.*, etm.remote_reboot, every *_reboot test): the firmware writes nothing to a Maestro at boot
  (loadMaestroSettings only reads NVS, WCB_Storage.cpp:2495), and NaviCore's new-peer action list is empty
  (GET_CONFIG peerEvent.actions []), so a WCB appearing, or changing its number, fires nothing.

A new test that can move a servo is added here by id, with its reason. selftest.py fails on an id no suite registers.
"""

SKIP_REASON = "moves a servo (--no-servos)"

SERVO_TESTS = {
    # ---- ;M1 typed on W1 fans out to NaviCore's Maestro 1 through W1's M1:W20 proxy
    "maestro.sub": ";M11 runs subroutine 1 on NaviCore's Maestro 1 as well as on the W1 S1 probe",
    "maestro.comma_int": ";M1,7 runs subroutine 7 on NaviCore's Maestro 1",
    "maestro.verbs": ";M1,setTarget / goHome / sub move NaviCore's Maestro 1 (ch 0 is knob J2's servo)",
    "maestro.bad_verb": "its positive control ;M11 runs subroutine 1 on NaviCore's Maestro 1",
    "maestro.fanout_remote": "asserts that ;M11 runs subroutine 1 on NaviCore's Maestro 1",
    "pwm.p_refused_on_maestro_port": "sends ;M11 twice: subroutine 1 on NaviCore's Maestro 1",
    # Listed on doubt. clearAllMaestroConfigs drops every slot (WCB_Maestro.cpp:1050-1068), so on today's firmware its
    # moves reach only the W1 S1 probe; but the test only notes a failed clear ("LIST after CLEAR,ALL is not empty") and
    # sends them anyway, and a regression there is exactly what it exists to catch (cross-check, 2026-09-24).
    "maestro.clear_all_legacy_routing": "after ?MAESTRO,CLEAR,ALL it sends ;M12 and ;M1,goHome without stopping on a "
                                        "failed clear: a left-over M1:W20 proxy runs both on NaviCore's Maestro 1",
    "maestro.target9": ";M9,goHome is W1-local by design, but goHome moves and that routing is what the test checks",
    "kyber.target_id9_rejected": ";M95 and ;M9,goHome, W1-local by design; the same doubt as maestro.target9",
    "maestro.m0_broadcast": ";M0 reaches Maestro 2 and NaviCore's Maestro 1: stopScript, and with bench.json "
                            "maestro_safe_sub it runs that subroutine on both",
    # ---- Maestro 2 on W2 S1
    "maestro.remote_readback": ";M2,setTarget moves Maestro 2 ch 0 off its rest position and back",
    "client_mesh.sendraw_real_maestro_tap": "a raw setTarget frame moves Maestro 2 ch 0 to 1500 us and back",
    # ---- NaviCore moving its own servos
    "navicore.maestro_mesh_settarget_readback": ";W20,;M<D>,setTarget moves a NaviCore Maestro channel and back",
    "navicore.maestro_mesh_fanout_0_9": ";W20,;M9 and ;M0 setTarget ch 0 (J2's servo) on each local NaviCore Maestro",
    "navicore.set_mode": "SET_MODE re-dispatches the mode-aware knobs: J2 moves NaviCore's Maestro slot 1 ch 0",
    "navicore.trigger": "TRIGGER mode 1 button 1 fires whatever slot 101 is mapped to; the mapping is not checked",
    "navicore.trigger_dispatch_trace": "fires a real mapping's WCB actions without reading them (some are ;M11 here)",
    "navicore.rec_play_clip": "replays a saved clip: every Maestro channel in it moves",
    # getcfg on this controller: rx=CH1, ry=CH2, lx=CH3, ly=CH4, and its RC PWM outputs 1-4 follow CH1-4
    # (run 20260924-190733).
    "sbus.to_navicore": "full lx (CH3: knob J3, no outputs today) with no binding check, every other stick re-centred "
                        "(CH4 is J4 -> Maestro 2 ch 0); the controller's own RC PWM 3 mirrors CH3",
    "sbus.mode_sets_trigger_mode": "a mesh SET_MODE: J2 moves NaviCore's Maestro slot 1 ch 0",
    "sbus.trim_exact": "here it switches NaviCore's mode with SET_MODE (no trim is on a free channel): J2 moves",
    # ---- NaviCore re-emits every SBUS channel on SBUS OUT (sbusOutEnabled); what that drives is not recorded
    "sbus.switch_exact": "moves a controller switch channel, which NaviCore re-emits on SBUS OUT",
    "sbus.slider_exact": "moves a controller slider channel, which NaviCore re-emits on SBUS OUT",
    "sbus.btn_single_tap": "presses a matrix button: the matrix channel is re-emitted on SBUS OUT",
    "sbus.btn_double_triple_tap": "presses a matrix button: the matrix channel is re-emitted on SBUS OUT",
    "sbus.btn_hold_unconfigured_long": "holds a matrix button: the matrix channel is re-emitted on SBUS OUT",
    "sbus.signal_loss_controller_reset": "stops and restarts the SBUS stream that NaviCore's knobs and SBUS OUT follow",
}


def moves_servo(t):
    """True for a registry entry (or a test id) listed in SERVO_TESTS."""
    return (t if isinstance(t, str) else t["id"]) in SERVO_TESTS


def _on(v):
    """A "no_servos" value, failing safe: only a missing key, null, false, 0 or a string saying no leaves the servo
    tests on. A malformed "opt_in" gates its tests off, which is the safe side there; here the safe side is skipping,
    so a hand-typed "true" or 1 must not let a servo move."""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "no", "off", "0")
    return v is not None and v is not False and v != 0


def enabled(cfg, run=None):
    """True when servo tests are skipped: bench.json "no_servos" is set, or `run` - a checkpoint's data - was started or
    resumed with --no-servos. The checkpoint holds the CLI flag, so --resume and the GUI's Resume keep it; bench.json is
    read before each test, like "opt_in"."""
    return _on((cfg or {}).get("no_servos")) or _on((run or {}).get("no_servos"))


def gate(cfg, run, t):
    """SKIP_REASON for registry entry `t` when servo tests are skipped, or None when it may run."""
    return SKIP_REASON if moves_servo(t) and enabled(cfg, run) else None


def set_enabled(cfg, on):
    """Set bench.json "no_servos" in place (the GUI's box). Off removes the key, so bench.json is back to what git
    has."""
    if on:
        cfg["no_servos"] = True
    else:
        cfg.pop("no_servos", None)
