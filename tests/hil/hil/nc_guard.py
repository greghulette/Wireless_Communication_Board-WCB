"""nc_guard: NaviCore's config snapshot and restore, for every test that writes NaviCore (docs/hil_plan/NAVICORE.md
§3 INF3; decisions D-NC2, D-NC3, D-NC5).

    with nc_guard(bench) as g:       # snapshot taken and written to disk
        cfg = g.before               # the config as a dict (g.before_text: the exact text), to build a change from
        g.nc.set_config({...})       # any write, through the INF1 driver (g.nc)
    # here NaviCore's config is proven byte-identical to the snapshot, or the test FAILs with
    # 'NAVICORE CONFIG NOT RESTORED — <key paths>' (credentials only ever as <redacted:sha12>)

What is snapshotted: GET_CONFIG's data, as its exact text and as a dict; the command library's bytes when
GET_CMDLIB_META reports one (NaviCore.ino:3861-3877); the ?REC,LS clip names; the learned peers (?WDP,DUMP rows with
PEER=2) and the [WDPCFG:...,PEERS=n] count; the mode (#L12). The peer count matters because ?WDP,DUMP prints only the
neighbours NaviCore hears now (WCB_Client WCB_Mgmt.h:232-233), while PEERS counts the floor plus every learned peer,
silent ones included (:231-232).

Why the restore is a ladder, not "send the snapshot back": SET_CONFIG leaves alone whatever the message does not
name, and GET_CONFIG leaves out whatever is empty, so a snapshot re-sent verbatim cannot undo three kinds of change
(the map's nc.cfg.snapshot_restore_trap):
  - a mapping key added since: the merge replaces only the keys present (rc_config.h:1580-1603), and an empty mapping
    is never printed (:1262);
  - channels given to a Maestro slot that had none: a slot's channels are replaced only when its "channels" key is
    present (:1705-1721), and a slot with none prints no key (:1340-1341);
  - serialLabels set when there were none: replaced whole only when the key is present (:1847-1864), and printed only
    when a label is set (:1419-1426).
So the restore, in order:
  1. GET_CONFIG equals the snapshot byte for byte: nothing is written (every SET_CONFIG rewrites /config.json).
  2. SET_CONFIG with the snapshot plus explicit clears - {} for each mapping key that is new, "channels": [] for each
     slot that had none, "serialLabels": {} when there were none - then GET_CONFIG must equal the snapshot.
  3. Otherwise RESET_DEFAULTS, which empties all three (rc_config.h:869, :927, :944), then SET_CONFIG with the
     snapshot's exact text, and the same check.
  4. Still different: the test fails with the key paths, and the snapshot stays marked not restored for the next
     guard and for the resume.
Step 2 comes first because RESET_DEFAULTS reloads the compile-time mesh password into RAM (rc_config.h:957) without a
save or a re-apply (NaviCore.ino:3989-3992), so OTA, RTERM and WcbMgmt, which read the live value, are cut off from
the ETM stack, which keeps the boot copy, until the SET_CONFIG lands (D-NC16, D-NC17; the map's
nc.rx.reset_defaults_password_split). Step 2 re-sends the password unchanged.
Then the rest of what a test can leave: SET_CMDLIB with the snapshot's bytes if the size or hash differs (a library
written where none was stored cannot be removed: there is no delete verb, D-NC13); ?REC,RM of every HIL* clip the
snapshot lacked; two ?WDP,POLL from W1 when a learned peer was lost (NaviCore learns a peer on its second advert,
WCB_Client.cpp:2166-2168, and persists it at once, :1717-1718; a solicit brings each WCB's advert within 600 ms,
WCB WCB_WDP.cpp:366-372); PING, which clears CALIB (NaviCore.ino:3835-3838); a mesh SET_MODE from W1 back to the
snapshot's mode (SET_MODE is mesh-only, rc_telemetry.h:2236-2243, and holds until the SBUS mode switch moves,
NaviCore.ino:2786-2798). SET_DEBUG_FLAGS 0 and STOP_MONITOR go first of all, to quiet the console before the
14 KB transfers. A peer NaviCore learned during the test is reported, not forgotten: FORGET_PEER writes its NVS.

Persistence (D-NC3): the exact snapshot is written to <run>/navicore_snapshot.json (results/ is gitignored, like
session.log) before the test body runs, and again with its state after the restore; the checkpoint's 'navicore'
record holds a hash of the REDACTED config text and the state ('guarding', 'restored', 'not_restored'). The file is
written first and every write is numbered (seq) in both, so the newer of the two always tells the state, even when
the checkpoint's copy was lost (a GUI closed with No freezes the checkpoint while the test runs on; reconcile). A run
killed between the two is restored by the resume (hil/resume.py check_navicore). The first snapshot ever taken is
also kept as results/navicore_config_week_start.json, never overwritten. A guard that finds this run's snapshot still
'guarding' or 'not_restored' (a test aborted or failed before it) restores it before taking its own, and fails the
test without running it when it cannot.

Credentials: GET_CONFIG carries the mesh and AP passwords (rc_config.h:1226, :1354, :1367) and every restore sends them
back, so Bench.log hashes them on NaviCore's lines (runner.REDACT_KINDS). Nothing here notes or raises a config line or
a value: differences are key paths from checkpoint.redacted_diff, and every message passes through redact_text.

A second Ctrl+C (an abort) skips the restore: the snapshot stays 'guarding' and the resume restores it. Guards do not
nest.
"""
import copy
import hashlib
import json
import os
import time
from contextlib import contextmanager

from .checkpoint import SECRET_KEY, atomic_write_json, now_iso, redact_text, redacted_diff
from .navicore import NaviCore
from .wcb import WCB

FORMAT = 1
SNAPSHOT_FILE = "navicore_snapshot.json"                # <run>/, the exact snapshot (gitignored)
WEEK_START_FILE = "navicore_config_week_start.json"     # results/, the first snapshot ever taken, never overwritten
DIRTY = ("guarding", "not_restored")                    # checkpoint states that mean NaviCore may not be as found
HIL_CLIP_PREFIX = "HIL"                                 # the only clips a restore deletes (and only new ones)
POLL_GAP_S = 1.5              # between the two ?WDP,POLL and after the second
RELEARN_WAIT_S = 6.0          # for NaviCore's loop task to join a peer it heard twice
MODE_WAIT_S = 3.0             # for a mesh SET_MODE to show in #L12
NAVICORE_ID = 20              # when the snapshot's config names no wcbNetwork.deviceId

_active = set()               # id(bench) while a guard is open on it: guards do not nest


# ------------------------------------------------------------------ pure helpers
def redacted_sha(text):
    """sha256[:16] of the config text with every credential hashed (checkpoint.redact_text): what the checkpoint
    stores, so it can tell snapshots apart without holding a secret."""
    return hashlib.sha256(redact_text(text).encode("utf-8")).hexdigest()[:16]


def with_clears(target, current):
    """The SET_CONFIG data that turns `current` back into `target` (both GET_CONFIG dicts): `target` plus {} for each
    mapping key `current` has and `target` lacks, "channels": [] for each Maestro slot `target` gives no channels, and
    "serialLabels": {} when `target` has none (see the module docstring). `target` is not modified."""
    out = copy.deepcopy(target)
    new = [k for k in (current.get("mappings") or {}) if k not in (target.get("mappings") or {})]
    if new:
        maps = out.setdefault("mappings", {})
        for k in new:
            maps[k] = {}
    for slot in out.get("maestros") or []:
        if isinstance(slot, dict) and "channels" not in slot:
            slot["channels"] = []
    if "serialLabels" not in out:
        out["serialLabels"] = {}
    return out


def secrets_of(cfg):
    """The non-empty credential values in GET_CONFIG dict `cfg` (every key checkpoint.SECRET_KEY names, at any depth),
    for a test that checks none reached a log. Never print them."""
    out = []

    def walk(key, v):
        if key is not None and SECRET_KEY.match(str(key)):
            if isinstance(v, str) and v:
                out.append(v)
            return
        if isinstance(v, dict):
            for k, x in v.items():
                walk(k, x)
        elif isinstance(v, list):
            for x in v:
                walk(key, x)
    walk(None, cfg)
    return sorted(set(out))


def config_diff(before_text, after_text, limit=30):
    """Redacted key paths between two GET_CONFIG texts (checkpoint.redacted_diff); a text that does not parse is named
    as such."""
    try:
        a, b = json.loads(before_text), json.loads(after_text)
    except (TypeError, ValueError):
        return ["the config read back does not parse"]
    return redacted_diff(a, b, limit=limit)


def _peer_state(view):
    """parse_wdp's view -> ({board: PEER flag string}, PEERS count or None)."""
    rows = {int(r["N"]): r.get("PEER") for r in view.get("rows", []) if str(r.get("N", "")).isdigit()}
    peers = view.get("cfg", {}).get("PEERS")
    return rows, (int(peers) if str(peers or "").isdigit() else None)


def _lost_peers(snap, rows, peers):
    """(learned ids of the snapshot that NaviCore now shows as not learned, or that are absent while the peer count
    fell; whether the count fell)."""
    learned, before = snap.get("learned") or [], snap.get("peers")
    lost = [n for n in learned if n in rows and rows[n] != "2"]
    fell = before is not None and peers is not None and peers < before
    if fell:
        lost += [n for n in learned if n not in rows]
    return sorted(set(lost)), fell


# ------------------------------------------------------------------ reads
def read_config(nc, tries=2):
    """GET_CONFIG's exact data text, asked up to `tries` times (a line cut short in NaviCore's TX ring, or a late one,
    is asked again). AssertionError naming only the count."""
    last = None
    for _ in range(tries):
        try:
            return nc.config(raw=True)
        except AssertionError as e:
            last = e
            time.sleep(0.5)
    raise AssertionError(f"NaviCore's GET_CONFIG could not be read in {tries} tries ({redact_text(str(last))})")


def take_snapshot(nc, test=None):
    """Everything a restore puts back (module docstring) -> the snapshot dict, as navicore_snapshot.json holds it.
    Only the config is required: AssertionError when it cannot be read, so nothing is written unguarded. A part that
    cannot be read is None, and its restore is skipped."""
    text = read_config(nc)
    cfg = json.loads(text)
    snap = {"format": FORMAT, "test": test, "taken": now_iso(), "state": None, "sha": redacted_sha(text),
            "config": text, "wcb_id": (cfg.get("wcbNetwork") or {}).get("deviceId") or NAVICORE_ID,
            "cmdlib_meta": None, "cmdlib": None, "clips": None, "learned": None, "peers": None, "mode": None,
            "unread": []}
    try:
        size, h = nc.cmdlib_meta()
        snap["cmdlib_meta"] = [size, h]
        if size:
            snap["cmdlib"] = nc.cmdlib().decode("utf-8")
    except (AssertionError, ValueError) as e:
        snap["cmdlib_meta"], snap["cmdlib"] = None, None
        snap["unread"].append(f"command library ({redact_text(str(e)).splitlines()[0]})")
    try:
        snap["clips"] = [name for name, _, _, _ in nc.rec_ls()]
    except (AssertionError, ValueError) as e:
        snap["unread"].append(f"clips ({redact_text(str(e)).splitlines()[0]})")
    try:
        rows, peers = _peer_state(nc.wdp_view())
        snap["learned"], snap["peers"] = sorted(n for n, p in rows.items() if p == "2"), peers
    except (AssertionError, ValueError) as e:
        snap["unread"].append(f"learned peers ({redact_text(str(e)).splitlines()[0]})")
    try:
        snap["mode"] = nc.mode()
    except AssertionError as e:
        snap["unread"].append(f"mode ({redact_text(str(e)).splitlines()[0]})")
    return snap


# ------------------------------------------------------------------ the restore
def _quiet(nc):
    """SET_DEBUG_FLAGS 0 and STOP_MONITOR (which also ends a calibration, NaviCore.ino:3965-3970): RAM only."""
    nc.set_debug_flags(0)
    nc.ack({"type": "STOP_MONITOR"})


def restore_config(nc, snap, note):
    """The ladder (module docstring) -> (path, left): path 'unchanged', 'diff', 'reset' or 'failed'; left the
    redacted key paths still different ([] unless 'failed'). RESET_DEFAULTS is sent only after the snapshot plus
    clears was read back different."""
    want = snap["config"]
    target = json.loads(want)
    cur = read_config(nc)
    if cur == want:
        return "unchanged", []
    note("NaviCore's config differs from the snapshot (" + "; ".join(config_diff(want, cur, limit=8))
         + ") - sending the snapshot with its explicit clears")
    try:
        nc.set_config(json.dumps(with_clears(target, json.loads(cur)), separators=(",", ":"), ensure_ascii=False))
    except AssertionError as e:
        note(f"the snapshot-plus-clears SET_CONFIG failed ({redact_text(str(e)).splitlines()[0]})")
    try:
        cur = read_config(nc)
    except AssertionError as e:
        cur = None
        note(redact_text(str(e)))
    if cur == want:
        return "diff", []
    note("still different after the snapshot plus clears (" + ("; ".join(config_diff(want, cur, limit=8)) if cur else
         "unreadable") + ") - RESET_DEFAULTS, then the snapshot's exact text")
    try:
        nc.reset_defaults()
    except AssertionError as e:
        note(f"RESET_DEFAULTS failed ({redact_text(str(e)).splitlines()[0]}) - sending the snapshot anyway")
    for attempt in (1, 2):          # never leave NaviCore on the defaults in RAM
        try:
            nc.set_config(want)
            break
        except AssertionError as e:
            note(f"SET_CONFIG of the snapshot failed, attempt {attempt} ({redact_text(str(e)).splitlines()[0]})")
    try:
        cur = read_config(nc)
    except AssertionError as e:
        return "failed", [f"GET_CONFIG could not be read back ({redact_text(str(e)).splitlines()[0]})"]
    if cur == want:
        return "reset", []
    return "failed", config_diff(want, cur)


def _w1_of(w1):
    """`w1` as passed to restore(): a hil.wcb.WCB, None, or a callable returning one (opened only when needed)."""
    return w1() if callable(w1) else w1


def _restore_peers(nc, snap, w1, note):
    try:
        rows, peers = _peer_state(nc.wdp_view())
    except (AssertionError, ValueError) as e:
        return [f"learned peers not checked ({redact_text(str(e)).splitlines()[0]})"]
    problems = []
    before = snap.get("peers")
    new = sorted(n for n, p in rows.items() if p == "2" and n not in snap["learned"])
    if new and before is not None and peers is not None and peers > before:
        problems.append(f"NaviCore learned WCB {', '.join(map(str, new))} during the test and keeps it in NVS (not "
                        f"forgotten here: FORGET_PEER is itself an NVS write)")
    lost, fell = _lost_peers(snap, rows, peers)
    if not lost and not fell:
        return problems
    # A fallen count with no row lost is a learned peer that was silent when the snapshot was taken (?WDP,DUMP lists
    # only neighbours heard now): it may still be on the air - W2 between adverts - so it gets the polls too. Failing at
    # once flagged hook_config_unreadable in run 20260928-154700, whose defaults boot drops a learned peer.
    which = lost or f"(one not listed when the snapshot was taken: the count fell from {before} to {peers})"
    w1 = _w1_of(w1)
    if w1 is None:
        return problems + [f"learned peer(s) {which} lost, and there is no W1 to poll the mesh"]
    note(f"NaviCore lost learned peer(s) {which} - two ?WDP,POLL from W1, so it hears each WCB advertise twice")
    try:
        for _ in range(2):
            w1.run("?WDP,POLL", timeout=5)
            time.sleep(POLL_GAP_S)
    except AssertionError as e:
        why = redact_text(str(e)).splitlines()[0]
        return problems + [f"learned peer(s) {lost} lost; ?WDP,POLL on W1 failed ({why})"]
    deadline = time.monotonic() + RELEARN_WAIT_S
    while True:
        try:
            lost, fell = _lost_peers(snap, *_peer_state(nc.wdp_view()))
        except (AssertionError, ValueError):
            pass
        if not lost and not fell:
            note("NaviCore re-learned its lost peer(s)")
            return problems
        if time.monotonic() >= deadline:
            return problems + [f"learned peer(s) {lost or which} not re-learned after two ?WDP,POLL: not on the air now"]
        time.sleep(0.5)


def _restore_mode(nc, snap, w1, note):
    want = snap["mode"]
    try:
        now = nc.mode()
    except AssertionError as e:
        return [f"mode not read ({redact_text(str(e)).splitlines()[0]})"]
    if now == want:
        return []
    w1 = _w1_of(w1)
    if w1 is None:
        return [f"mode is {now}, was {want}: SET_MODE is mesh-only and there is no W1 to send it"]
    note(f"NaviCore's mode is {now}, was {want} - a mesh SET_MODE from W1 (mode-aware knobs re-dispatch)")
    try:
        w1.send(f';W{snap.get("wcb_id") or NAVICORE_ID},{{"type":"SET_MODE","mode":{want}}}')
    except AssertionError as e:
        return [f"mode is {now}, was {want}; the SET_MODE could not be sent ({redact_text(str(e)).splitlines()[0]})"]
    deadline = time.monotonic() + MODE_WAIT_S
    while True:
        time.sleep(0.2)
        try:
            now = nc.mode()
        except AssertionError:
            pass
        if now == want:
            return []
        if time.monotonic() >= deadline:
            return [f"mode is {now}, was {want}, after a mesh SET_MODE from W1"]


def restore_state(nc, snap, w1=None, note=None):
    """What a test can leave besides the config (module docstring), each part on its own so one failure never skips
    the rest -> the problems, redacted ([] when all is back)."""
    note = note or (lambda s: None)
    problems = []
    if snap.get("cmdlib_meta") is not None:
        try:
            meta = list(nc.cmdlib_meta())
            if meta != list(snap["cmdlib_meta"]):
                if snap.get("cmdlib"):
                    nc.set_cmdlib(snap["cmdlib"])
                    note(f"command library restored ({snap['cmdlib_meta'][0]} bytes)")
                else:
                    problems.append(f"a command library ({meta[0]} bytes) was written where none was stored: NaviCore "
                                    f"has no verb to delete one (NAVICORE.md D-NC13)")
        except (AssertionError, ValueError) as e:
            problems.append(f"command library not restored ({redact_text(str(e)).splitlines()[0]})")
    if snap.get("clips") is not None:
        try:
            for name, _, _, _ in nc.rec_ls():
                if name.startswith(HIL_CLIP_PREFIX) and name not in snap["clips"]:
                    try:
                        nc.rec_rm(name)
                        note(f"removed clip {name}")
                    except (AssertionError, ValueError) as e:
                        problems.append(f"clip {name} not removed ({redact_text(str(e)).splitlines()[0]})")
        except (AssertionError, ValueError) as e:
            problems.append(f"clips not listed ({redact_text(str(e)).splitlines()[0]})")
    if snap.get("learned") is not None:
        problems += _restore_peers(nc, snap, w1, note)
    try:
        nc.ping()                    # clears CALIB
    except AssertionError as e:
        problems.append(f"PING not answered ({redact_text(str(e)).splitlines()[0]})")
    if snap.get("mode") is not None:
        problems += _restore_mode(nc, snap, w1, note)
    return [redact_text(p) for p in problems]


def restore(nc, snap, w1=None, note=None):
    """The whole restore -> (path, left, problems): the console quieted, the config ladder, then restore_state. `w1` is
    W1's console (hil.wcb.WCB), None, or a callable returning one, called only if a peer or the mode needs W1."""
    note = note or (lambda s: None)
    problems = []
    try:
        _quiet(nc)
    except AssertionError as e:
        problems.append(f"debug flags or monitor not reset ({redact_text(str(e)).splitlines()[0]})")
    try:
        path, left = restore_config(nc, snap, note)
    except AssertionError as e:
        path, left = "failed", [f"the restore stopped: {redact_text(str(e)).splitlines()[0]}"]
    problems += restore_state(nc, snap, w1, note)
    return path, left, problems


# ------------------------------------------------------------------ persistence (D-NC3)
def snapshot_path(out_dir):
    return os.path.join(out_dir, SNAPSHOT_FILE)


def persist(out_dir, ckpt, snap, state, detail=None, restore_path=None, log=None):
    """Record `snap` with `state`: the exact snapshot to <out_dir>/navicore_snapshot.json first, then the redacted
    reference into the checkpoint, both numbered with the next seq, so a run killed between the two leaves a file at
    least as new as the checkpoint (reconcile). Either may be None (a guard outside a run). A failed file write is
    logged by atomic_write_json and never fails a test."""
    ref = (ckpt.data.get("navicore") if ckpt is not None else None) or {}
    snap["seq"] = max(ref.get("seq") or 0, snap.get("seq") or 0) + 1
    snap["state"] = state
    snap["updated"] = now_iso()
    if out_dir:
        snap["run"] = os.path.basename(os.path.normpath(out_dir))
        atomic_write_json(snapshot_path(out_dir), snap, log=log)
    if ckpt is not None:
        ckpt.set_navicore_ref({"seq": snap["seq"], "sha": snap["sha"], "state": state, "test": snap.get("test"),
                               "taken": snap.get("taken"), "at": now_iso(), "cmdlib": snap.get("cmdlib_meta"),
                               "path": restore_path, "detail": redact_text(detail) if detail else None})


def load_snapshot(out_dir):
    """<out_dir>/navicore_snapshot.json -> the snapshot dict. OSError when it is missing; ValueError when it does not
    parse, holds no config, or its config does not match its own redacted hash (edited or cut short)."""
    with open(snapshot_path(out_dir), encoding="utf-8") as f:
        snap = json.load(f)
    if not isinstance(snap, dict) or not isinstance(snap.get("config"), str):
        raise ValueError("it holds no config")
    json.loads(snap["config"])
    if redacted_sha(snap["config"]) != snap.get("sha"):
        raise ValueError("its config does not match its own hash")
    return snap


def reconcile(out_dir, ref):
    """Where this run's NaviCore record stands -> (snapshot, state, problem). state: 'guarding', 'restored',
    'not_restored', or None when nothing was ever recorded; snapshot: the one to restore from or compare with, None
    when there is none or it cannot be used; problem: why not, or None.
    The file (navicore_snapshot.json) is written before the checkpoint's record `ref`, and both carry the write's seq
    (persist). A file with a higher seq than `ref` is newer, and its state counts: the checkpoint's copy was lost (a
    GUI closed with No froze it while the test ran on, or the process died between the two writes). Otherwise the
    checkpoint's state counts, and the file is used only if it holds the config the checkpoint recorded (same redacted
    hash). A file from another run, or one that does not match its own hash, is never used; an unreadable one with no
    record behind it counts as 'guarding', the careful answer."""
    ref = ref or {}
    try:
        snap = load_snapshot(out_dir)
        if snap.get("run") not in (None, os.path.basename(os.path.normpath(out_dir))):
            raise ValueError(f"it was taken in run {snap['run']}")
    except FileNotFoundError:
        return None, ref.get("state"), (f"{SNAPSHOT_FILE} is missing" if ref else None)
    except (OSError, ValueError, TypeError) as e:
        return None, ref.get("state") or "guarding", f"{SNAPSHOT_FILE} cannot be used: {redact_text(str(e))}"
    if (snap.get("seq") or 0) > (ref.get("seq") or 0):
        return snap, snap.get("state"), None
    if snap.get("sha") == ref.get("sha"):
        return snap, ref.get("state"), None
    return None, ref.get("state"), f"{SNAPSHOT_FILE} is not the snapshot the checkpoint recorded"


def _keep_week_start(results_root, snap, note):
    path = os.path.join(results_root, WEEK_START_FILE)
    if os.path.exists(path):
        return
    ref = dict(snap, state="reference")
    if atomic_write_json(path, ref):
        note(f"the first NaviCore snapshot is kept as results/{WEEK_START_FILE} (never overwritten)")


# ------------------------------------------------------------------ the guard
class NcGuard:
    """One guarded block. enter() snapshots and persists; exit(failure) restores, persists, and raises (see
    nc_guard). After exit: path ('unchanged', 'diff', 'reset' or 'failed'), left and problems."""

    def __init__(self, bench, nc=None, w1=None):
        self.bench = bench
        self.nc = nc or NaviCore(bench.dev("navicore"))
        self._w1 = w1
        self.snap = self.before = self.before_text = None
        self.path, self.left, self.problems = None, [], []

    @property
    def w1(self):
        """W1's console, opened only when a peer or mode restore needs it (None without a wcb1)."""
        if self._w1 is None and self.bench.has("wcb1"):
            self._w1 = WCB(self.bench.dev("wcb1"))
        return self._w1

    def note(self, text):
        self.bench.note("nc_guard: " + redact_text(text))

    def _ckpt(self):
        return getattr(self.bench, "ckpt", None)

    def _pending(self):
        """This run's snapshot when its record says NaviCore was left changed ('guarding' or 'not_restored'), or None
        (reconcile). AssertionError when it says so and the snapshot cannot be used."""
        if not self.bench.out_dir:
            return None
        ck = self._ckpt()
        prev, state, problem = reconcile(self.bench.out_dir, ck.data.get("navicore") if ck is not None else None)
        if state not in DIRTY:
            return None
        if prev is None:
            raise AssertionError(f"NAVICORE CONFIG NOT RESTORED — this run's NaviCore record says {state}, and "
                                 f"{problem}; this test did not run")
        return prev

    def enter(self):
        if id(self.bench) in _active:
            raise RuntimeError("nc_guard does not nest: the outer guard already restores NaviCore")
        prev = self._pending()
        if prev is not None:
            by = prev.get("test") or "an earlier test"
            self.note(f"NaviCore may still hold {by}'s changes - restoring its snapshot before this test's own")
            path, left, problems = restore(self.nc, prev, lambda: self.w1, self.note)
            persist(self.bench.out_dir, self._ckpt(), prev, "not_restored" if left else "restored",
                    detail="; ".join(left + problems) or None, restore_path=path, log=self.note)
            if left:
                raise AssertionError(redact_text(f"NAVICORE CONFIG NOT RESTORED — still different from the snapshot "
                                                 f"taken before {by}: {'; '.join(left)}; this test did not run"))
            if problems:
                self.note(f"{by}'s config is back, but not all of its state: {'; '.join(problems)}")
        ck = self._ckpt()
        snap = take_snapshot(self.nc, test=ck.in_flight if ck is not None else None)
        for part in snap["unread"]:
            self.note(f"not snapshotted, so not restored: {part}")
        ref = (ck.data.get("navicore") if ck is not None else None) or {}
        if ref.get("sha") and ref["sha"] != snap["sha"] and prev is None:
            try:
                paths = config_diff(load_snapshot(self.bench.out_dir)["config"], snap["config"], limit=8)
            except (OSError, ValueError):
                paths = ["(the earlier snapshot is unreadable)"]
            self.note(f"NaviCore's config changed since {ref.get('test') or 'the last guarded test'} restored it, "
                      f"outside any guard: {'; '.join(paths)}")
        persist(self.bench.out_dir, ck, snap, "guarding", log=self.note)
        if self.bench.out_dir:
            root = getattr(self.bench, "results_root", None) or os.path.dirname(self.bench.out_dir)
            _keep_week_start(root, snap, self.note)
        self.snap, self.before_text, self.before = snap, snap["config"], json.loads(snap["config"])
        _active.add(id(self.bench))
        return self

    def exit(self, failure=None):
        """Restore, record, and raise: AssertionError 'NAVICORE CONFIG NOT RESTORED — ...' (the config) or 'NAVICORE
        STATE NOT RESTORED — ...' (the rest), after the body's own failure when it had one; else the body's failure,
        if any."""
        try:
            self.path, self.left, self.problems = restore(self.nc, self.snap, lambda: self.w1, self.note)
        finally:
            _active.discard(id(self.bench))
        state = "not_restored" if self.left else "restored"
        persist(self.bench.out_dir, self._ckpt(), self.snap, state, detail="; ".join(self.left + self.problems) or None,
                restore_path=self.path, log=self.note)
        if self.path in ("diff", "reset"):
            how = "the snapshot plus clears" if self.path == "diff" else "RESET_DEFAULTS, then the snapshot"
            self.note(f"NaviCore's config restored by {how}, byte-identical to the snapshot")
        msg = None
        if self.left:
            msg = (f"NAVICORE CONFIG NOT RESTORED — {'; '.join(self.left)}"
                   + (f"; also: {'; '.join(self.problems)}" if self.problems else "")
                   + f" (the exact snapshot is {SNAPSHOT_FILE} in this run's folder)")
        elif self.problems:
            msg = "NAVICORE STATE NOT RESTORED — " + "; ".join(self.problems)
        if msg:
            msg = redact_text(msg)
            self.bench.note("NAVICORE LEAK " + msg)
            raise AssertionError((f"{failure}\n" if failure else "") + msg)
        if failure:
            raise failure


@contextmanager
def nc_guard(bench, nc=None, w1=None):
    """Snapshot NaviCore, run the block, restore and prove it byte-identical (module docstring). A leak fails the test
    even when the block itself failed, with the block's failure first. KeyboardInterrupt (an abort) skips the restore
    and leaves the snapshot 'guarding' for the resume."""
    g = NcGuard(bench, nc=nc, w1=w1)
    g.enter()
    failure = None
    try:
        yield g
    except Exception as e:  # noqa: BLE001 - re-raised by exit() after the restore
        failure = e
    except BaseException:
        _active.discard(id(bench))
        raise
    g.exit(failure)
