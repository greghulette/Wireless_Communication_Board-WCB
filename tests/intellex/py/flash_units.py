"""Intellex's flash rules as pure functions, under its venv, with no board, no network and no esptool process.

group "rules" (intellex.flash_rules_unit, IX-WP3): wcb_flash._pick_app, the ESP32-S3 bootloader choice, the full-flash
refusals, both write lists, NaviCore's both-or-neither pair, and detect() parsing esptool 5.3.1's flash-id output. The
images come from a firmware cache seeded into the staged src/firmware for short test branches (u-*), read offline
(INTELLEX_OFFLINE); detect() gets tests/intellex/fixtures/esptool/*.txt through a patched proc.run. Those fixtures are
built from esptool 5.3.1's own print statements (esptool/__init__.py:478, :559-569; cmds.py:2050-2080), not captured
from a board: nothing on this bench has recorded one.

group "partition_should" (intellex.flash_update_partition_escalates, a (should) test, plan finding 5): an app-only update
onto a board whose partition table differs from the build's must escalate once to a full, NVS-preserving flash, as the
Wizard's own flasher does (Wizard/flasher.js:245-260 comparePartitionTable, :465-511). wcb_flash.write_list(app_only=True)
writes the app alone and never reads the table (Intellex wcb_flash.py:302-307), while its header claims parity with
flasher.js (:11-14). The fake esptool answers a read of 0x8000 with the table the case chooses; a fix that reads the
table some other way than `esptool read-flash` may need this case taught its spelling.

group "partition_edges" (intellex.flash_update_partition_edges): the rest of Update FW's partition check
(wcb_flash.py:372-393 update_needs_full, :523-535 flash). A table that cannot be read keeps the update app-only while
the app fits the legacy layout's 0x140000 slot, and is refused ('Use Flash FW instead') one byte past it; a build with
no _part.bin skips the check without reading the board; a differing table that needs a bootloader the build does not
have is refused. Every refusal writes nothing and restarts the board out of its ROM loader (boot_app).

group "reachable" (intellex.reachable_unit): flash.reachable (Intellex src/flash.py:50-156) with socket.getaddrinfo and
socket.socket stood in for, so nothing is resolved or connected: each address DNS returns gets its own connect attempt
bounded by `timeout` (a dead IPv6 address does not spend the budget), the whole answer by `budget` (which outlasts a slow
DNS answer), answers are kept per host for REACH_TTL_S (20 s), and INTELLEX_OFFLINE answers False at once.

group "nc_fetch_store" (intellex.nc_fetch_store_unit): flash.fetch_images online (:266-371), flash.reachable and
flash._get stood in for GitHub: the listing is stored as GitHub sent it and each image as it is downloaded, under
src/firmware/navicore/<branch> (a '/' in the branch flattened), with the manifest; an empty image is refused and never
stored; offline the same set comes back from the cache.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import FIXTURES, SkipCase, case, check, seed_cache  # noqa: E402,F401

TAG = "6.2.1_250646RSEP2026_hil"
NC_TAG = "0.2.0_280000RSEP2026"


def wcb(kind):
    return f"WCB_{TAG}_{kind}"


def blob(tag, n=64):
    """Distinct, recognisable bytes for one image."""
    return (tag.encode() * (n // max(1, len(tag)) + 1))[:n]


def raises(fn, words):
    """Call fn(); it must raise FlashError whose text contains `words` -> the message."""
    import flash
    try:
        fn()
    except flash.FlashError as e:
        check(words in str(e), f"refused, but not for the right reason: expected {words!r} in {str(e)[:200]!r}")
        return str(e)
    raise AssertionError(f"accepted; expected a refusal naming {words!r}")


# ------------------------------------------------------------------ group "rules"
@case("_pick_app takes exactly one anchored app image and refuses two", group="rules")
def pick_app(ctx):
    import wcb_flash
    files = [{"name": f"WCB_{TAG}_ESP32S3.bin"}, {"name": f"WCB_{TAG}_ESP32.bin"},
             {"name": f"WCB_{TAG}_ESP32_part.bin"}, {"name": f"XWCB_{TAG}_ESP32.bin"}, {"name": "WCB_x_ESP32.bin.bak"}]
    check(wcb_flash._pick_app(files, "ESP32")["name"] == f"WCB_{TAG}_ESP32.bin", "ESP32 did not get its one image")
    check(wcb_flash._pick_app(files, "ESP32S3")["name"] == f"WCB_{TAG}_ESP32S3.bin", "ESP32S3 did not get its image")
    two = files + [{"name": "WCB_6.2.1_010101RJAN2026_main_ESP32.bin"}]
    raises(lambda: wcb_flash._pick_app(two, "ESP32"), "Refusing to guess")
    raises(lambda: wcb_flash._pick_app([{"name": f"WCB_{TAG}_ESP32S3.bin"}], "ESP32"), "no ESP32 app image")


@case("the ESP32-S3 bootloader is the 8 or 16 MB build; the unsized one stands in for 16 MB only", group="rules")
def s3_bootloader(ctx):
    import wcb_flash
    both = {wcb("ESP32S3.bin"): blob("app"), wcb("ESP32S3_part.bin"): blob("part"),
            wcb("ESP32S3_boot_8MB.bin"): blob("boot8"), wcb("ESP32S3_boot_16MB.bin"): blob("boot16"),
            wcb("ESP32S3_boot.bin"): blob("boot-unsized")}
    seed_cache(ctx, "wcb", "u-s3", both)
    for mb, name in ((8, "ESP32S3_boot_8MB.bin"), (16, "ESP32S3_boot_16MB.bin")):
        fw = wcb_flash.fetch_images("ESP32S3", mb, "u-s3")
        check(fw["boot"] and fw["boot"]["name"] == wcb(name) and fw["boot"]["address"] == 0x0,
              f"{mb} MB board: bootloader {fw['boot'] and fw['boot']['name']}, expected {wcb(name)} at 0x0")
        check(fw["boot"]["data"] == both[wcb(name)], f"{mb} MB: the bootloader bytes are not the cached file")
        check(not fw["bootBlocked"], f"{mb} MB: blocked ({fw['bootBlocked']})")
        check(fw["version"] == TAG, f"version {fw['version']!r}, expected {TAG!r}")
        check(fw["app"]["address"] == 0x10000 and fw["part"]["address"] == 0x8000, "app or table at a wrong address")
    seed_cache(ctx, "wcb", "u-s3u", {k: v for k, v in both.items() if "MB" not in k})
    fw = wcb_flash.fetch_images("ESP32S3", 16, "u-s3u")
    check(fw["boot"] and fw["boot"]["name"] == wcb("ESP32S3_boot.bin"),
          "a 16 MB board did not fall back to the unsized (16 MB) bootloader")
    fw = wcb_flash.fetch_images("ESP32S3", 8, "u-s3u")
    check(fw["boot"] is None and "no 8 MB ESP32-S3 bootloader" in fw["bootBlocked"],
          f"an 8 MB board was given {fw['boot'] and fw['boot']['name']} (blocked: {fw['bootBlocked']!r}): the unsized "
          f"file is the 16 MB build (CLAUDE.md rule 7)")
    fw = wcb_flash.fetch_images("ESP32S3", None, "u-s3")
    check(fw["boot"] is None and "could not determine the flash size" in fw["bootBlocked"],
          f"unknown flash size: boot {fw['boot'] and fw['boot']['name']}, blocked {fw['bootBlocked']!r}")
    fw = wcb_flash.fetch_images("ESP32S3", 4, "u-s3")
    check(fw["boot"] is None and "unsupported flash size 4 MB" in fw["bootBlocked"],
          f"4 MB: boot {fw['boot'] and fw['boot']['name']}, blocked {fw['bootBlocked']!r}")
    seed_cache(ctx, "wcb", "u-32", {wcb("ESP32.bin"): blob("a32"), wcb("ESP32_part.bin"): blob("p32"),
                                    wcb("ESP32_boot.bin"): blob("b32")})
    fw = wcb_flash.fetch_images("ESP32", 4, "u-32")
    check(fw["boot"] and fw["boot"]["address"] == 0x1000 and not fw["bootBlocked"],
          f"ESP32: boot {fw['boot'] and hex(fw['boot']['address'])}, blocked {fw['bootBlocked']!r}")
    seed_cache(ctx, "wcb", "u-32nb", {wcb("ESP32.bin"): blob("a32"), wcb("ESP32_part.bin"): blob("p32")})
    fw = wcb_flash.fetch_images("ESP32", 4, "u-32nb")
    check(fw["boot"] is None and "no bootloader" in fw["bootBlocked"], f"ESP32 without a bootloader: {fw['bootBlocked']!r}")


def _fw(boot=True, part=True, blocked=""):
    return {"version": TAG,
            "app": {"address": 0x10000, "data": blob("app"), "name": wcb("ESP32.bin")},
            "part": {"address": 0x8000, "data": blob("part"), "name": wcb("ESP32_part.bin")} if part else None,
            "boot": {"address": 0x1000, "data": blob("boot"), "name": wcb("ESP32_boot.bin")} if boot else None,
            "bootBlocked": blocked}


@case("a full WCB flash is refused without a bootloader or a partition table; Update FW is not", group="rules")
def full_flash_refusals(ctx):
    import wcb_flash
    raises(lambda: wcb_flash.write_list(_fw(boot=False, blocked="no bootloader in this build"), False, False),
           "Cannot do a full flash")
    raises(lambda: wcb_flash.write_list(_fw(part=False), False, False), "no partition table")
    got = [e["address"] for e in wcb_flash.write_list(_fw(boot=False, blocked="x"), True, False)]
    check(got == [0xE000, 0x10000], f"Update FW with no bootloader wrote {[hex(a) for a in got]}")


@case("write lists are sorted, otadata is always erased, NVS only on a factory reset or full wipe", group="rules")
def write_lists(ctx):
    import flash
    import wcb_flash

    def addrs(entries):
        return [e["address"] for e in entries]

    def blank(entries, addr, size):
        e = next((e for e in entries if e["address"] == addr), None)
        check(e is not None and e["data"] == b"\xff" * size, f"{hex(addr)} is not {size} bytes of 0xFF")

    cases = ((True, False, [0xE000, 0x10000]), (True, True, [0x9000, 0xE000, 0x10000]),
             (False, False, [0x1000, 0x8000, 0xE000, 0x10000]), (False, True, [0x1000, 0x8000, 0x9000, 0xE000, 0x10000]))
    for app_only, erase, want in cases:
        got = wcb_flash.write_list(_fw(), app_only, erase)
        check(addrs(got) == want, f"WCB app_only={app_only} erase_nvs={erase}: {[hex(a) for a in addrs(got)]}, "
                                  f"expected {[hex(a) for a in want]}")
        blank(got, 0xE000, 0x2000)
        if erase:
            blank(got, 0x9000, 0x5000)
    images = [{"address": 0x0, "data": blob("b"), "name": "boot"}, {"address": 0x8000, "data": blob("p"), "name": "p"},
              {"address": 0x10000, "data": blob("a"), "name": "a"}]
    for erase, want in ((False, [0x0, 0x8000, 0xE000, 0x10000]), (True, [0x0, 0x8000, 0x9000, 0xE000, 0x10000])):
        got = flash.write_list(list(images), erase)
        check(addrs(got) == want, f"NaviCore erase_nvs={erase}: {[hex(a) for a in addrs(got)]}")
        blank(got, 0xE000, 0x2000)


@case("NaviCore's custom bootloader and partition table are both or neither; the app is anchored", group="rules")
def navicore_pair(ctx):
    import flash
    app, part, boot = f"NaviCore_{NC_TAG}_ESP32S3.bin", f"NaviCore_{NC_TAG}_ESP32S3_part.bin", flash.BOOTLOADER_NAME
    other = "RC-Controller_1.0.0_ESP32S3.bin"        # another product's image in the shared firmware/ (flash.py:194-199)
    seed_cache(ctx, "navicore", "u-nc", {other: blob("rc"), app: blob("nca"), part: blob("ncp"),
                                                      boot: blob("ncb")})
    got = flash.fetch_images("u-nc")
    check([(e["address"], e["name"]) for e in got] == [(0x0, boot), (0x8000, part), (0x10000, app)],
          f"full set: {[(hex(e['address']), e['name']) for e in got]}")
    seed_cache(ctx, "navicore", "u-nca", {app: blob("nca")})
    got = flash.fetch_images("u-nca")
    check([e["address"] for e in got] == [0x10000], f"app-only set: {[hex(e['address']) for e in got]}")
    seed_cache(ctx, "navicore", "u-ncnp", {app: blob("nca"), boot: blob("ncb")})
    raises(lambda: flash.fetch_images("u-ncnp"), "Incomplete firmware")
    seed_cache(ctx, "navicore", "u-ncnb", {app: blob("nca"), part: blob("ncp")})
    raises(lambda: flash.fetch_images("u-ncnb"), "Incomplete firmware")


class _Run:
    """proc.run for detect(): the recorded flash-id output, and every argv it was given."""

    def __init__(self, fixture=None, rc=0):
        self.fixture, self.rc, self.calls = fixture, rc, []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        out = ""
        if self.fixture:
            with open(os.path.join(FIXTURES, "esptool", self.fixture), encoding="utf-8") as f:
                out = f.read()
        return subprocess.CompletedProcess(cmd, self.rc, stdout=out, stderr="")


@case("detect() reads esptool 5.3.1 flash-id: ESP32, ESP32-S3 8 and 16 MB; refuses a C3 and a failed run", group="rules")
def detect_parses(ctx):
    import proc
    import wcb_flash
    real = proc.run
    try:
        for fixture, want in (("flash_id_esp32.txt", ("ESP32", 4)), ("flash_id_esp32s3_8mb.txt", ("ESP32S3", 8)),
                              ("flash_id_esp32s3_16mb.txt", ("ESP32S3", 16)),
                              ("flash_id_unknown_size.txt", ("ESP32S3", None))):
            run = _Run(fixture)
            proc.run = run
            got = wcb_flash.detect("COMFAKE")
            check(got == want, f"{fixture}: detect() -> {got}, expected {want}")
            argv = run.calls[0]
            for a, b in (("--chip", "auto"), ("--port", "COMFAKE"), ("--before", "default-reset"),
                         ("--after", "no-reset")):
                check(a in argv and argv[argv.index(a) + 1] == b, f"detect() argv lacks {a} {b}: {argv[2:]}")
            check("flash-id" in argv, f"detect() did not ask for flash-id: {argv[2:]}")
        proc.run = _Run("flash_id_esp32c3.txt")
        raises(lambda: wcb_flash.detect("COMFAKE"), "is not a WCB target")
        proc.run = _Run("flash_id_esp32.txt", rc=2)
        raises(lambda: wcb_flash.detect("COMFAKE"), "could not talk to the board")
    finally:
        proc.run = real


# ------------------------------------------------------------------ group "partition_should" (plan finding 5)
BUILD_TABLE = bytes.fromhex("aa50011200000900000005000000") + b"nvs".ljust(16, b"\0") + b"\xff" * 0x100
BOARD_DEFAULT_TABLE = bytes.fromhex("aa50011200000900000005000000") + b"nvs".ljust(16, b"\0") + \
    bytes.fromhex("aa500100000001000000140000") + b"app0".ljust(16, b"\0") + b"\xff" * 0x100


class _Esptool:
    """proc.run and proc.popen for a whole wcb_flash.flash(): flash-id answers as a 4 MB ESP32, a read of 0x8000 returns
    `table`, and every write is recorded as its sorted addresses. Nothing is started."""

    class _P:
        def __init__(self):
            self.stdout = iter(["Hash of data verified.\n"])

        def wait(self):
            return 0

    def __init__(self, table):
        self.table, self.writes, self.reads = table, [], []

    def run(self, cmd, **kw):
        if "flash-id" in cmd or "flash_id" in cmd:
            with open(os.path.join(FIXTURES, "esptool", "flash_id_esp32.txt"), encoding="utf-8") as f:
                return subprocess.CompletedProcess(cmd, 0, stdout=f.read(), stderr="")
        verb = next((c for c in cmd if c in ("read-flash", "read_flash")), None)
        if verb:
            tail = [a for a in cmd[cmd.index(verb) + 1:] if not a.startswith("-")]
            addr, size, out = int(tail[0], 0), int(tail[1], 0), tail[2]
            self.reads.append((addr, size))
            with open(out, "wb") as f:
                f.write((self.table + b"\xff" * size)[:size])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def popen(self, cmd, **kw):
        if "write-flash" in cmd or "write_flash" in cmd:
            verb = "write-flash" if "write-flash" in cmd else "write_flash"
            tail = cmd[cmd.index(verb) + 1:]
            self.writes.append(sorted(int(t, 0) for i, t in enumerate(tail)
                                      if t.startswith("0x") and not tail[i - 1].startswith("--flash")))
        return self._P()


@case("(should) Update FW onto a board whose partition table differs escalates to a full, NVS-preserving flash; a "
      "matching table stays app-only", group="partition_should")
def update_escalates(ctx):
    import proc
    import wcb_flash
    seed_cache(ctx, "wcb", "u-part", {wcb("ESP32.bin"): blob("app", 256), wcb("ESP32_part.bin"): BUILD_TABLE,
                                             wcb("ESP32_boot.bin"): blob("boot", 128)})
    run0, popen0 = proc.run, proc.popen
    problems = []
    try:
        for board, want_full in ((BUILD_TABLE, False), (BOARD_DEFAULT_TABLE, True)):
            e = _Esptool(board)
            proc.run, proc.popen = e.run, e.popen
            wcb_flash.flash("COMFAKE", app_only=True, erase_nvs=False, log=lambda _m: None, branch="u-part")
            wrote = e.writes[-1] if e.writes else []
            what = "a different" if want_full else "the same"
            if 0x9000 in wrote:
                problems.append(f"board with {what} table: an Update wrote NVS (0x9000)")
            if want_full and not {0x1000, 0x8000, 0x10000} <= set(wrote):
                problems.append(f"board with {what} partition table: Update FW wrote {[hex(a) for a in wrote]} "
                                f"(read {[(hex(a), n) for a, n in e.reads] or 'nothing'}); the Wizard's own flasher "
                                f"escalates once to bootloader + table + app, NVS untouched (Wizard/flasher.js:465-511). "
                                f"wcb_flash.write_list(app_only=True) never reads the table (wcb_flash.py:302-307)")
            if not want_full and wrote != [0xE000, 0x10000]:
                problems.append(f"board with {what} table: Update FW wrote {[hex(a) for a in wrote]}, expected the "
                                f"app alone (0xe000 otadata, 0x10000)")
    finally:
        proc.run, proc.popen = run0, popen0
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "partition_edges"
class _EsptoolEdge(_Esptool):
    """_Esptool with every esptool run kept as its verb and flags, and a read of 0x8000 that can fail the way a board
    that will not talk does: exit 2, esptool's own last line."""

    def __init__(self, table, read_rc=0):
        super().__init__(table)
        self.read_rc, self.runs = read_rc, []

    def run(self, cmd, **kw):
        verb = next((c for c in cmd if c in ("flash-id", "flash_id", "read-flash", "read_flash")), "?")
        self.runs.append((verb, cmd[cmd.index("--after") + 1] if "--after" in cmd else None))
        if verb in ("read-flash", "read_flash") and self.read_rc:
            self.reads.append(None)
            return subprocess.CompletedProcess(cmd, self.read_rc, stdout="",
                                               stderr="A fatal error occurred: Failed to connect to ESP32: No serial "
                                                      "data received.")
        return super().run(cmd, **kw)


def _update(ctx, branch, files, table, read_rc=0):
    """wcb_flash.flash(app_only=True) on a fake 4 MB ESP32 against a cache seeded for `branch` with `files` -> (esptool,
    the log lines, the FlashError text or None)."""
    import flash
    import proc
    import wcb_flash
    seed_cache(ctx, "wcb", branch, files)
    e = _EsptoolEdge(table, read_rc)
    run0, popen0 = proc.run, proc.popen
    lines, err = [], None
    proc.run, proc.popen = e.run, e.popen
    try:
        wcb_flash.flash("COMFAKE", app_only=True, erase_nvs=False, log=lines.append, branch=branch)
    except flash.FlashError as x:
        err = str(x)
    finally:
        proc.run, proc.popen = run0, popen0
    return e, lines, err


def _restarted(e):
    """Whether boot_app restarted the board after a refusal: a flash-id run ended by --after hard-reset."""
    return any(v in ("flash-id", "flash_id") and after == "hard-reset" for v, after in e.runs)


@case("an Update whose partition-table read fails stays app-only while the app fits the legacy 0x140000 slot",
      group="partition_edges")
def unreadable_table_app_fits(ctx):
    import wcb_flash
    slot = wcb_flash.LEGACY_APP_SLOT
    e, log, err = _update(ctx, "u-unread-fit", {wcb("ESP32.bin"): blob("app", slot), wcb("ESP32_part.bin"): BUILD_TABLE,
                                                wcb("ESP32_boot.bin"): blob("boot", 128)}, BUILD_TABLE, read_rc=2)
    check(err is None, f"a {slot}-byte app (the slot exactly) was refused: {err}")
    check(e.reads == [None], f"the table was read {len(e.reads)} time(s), expected one failed read")
    check(e.writes == [[0xE000, 0x10000]], f"Update wrote {[[hex(a) for a in w] for w in e.writes]}, expected the app "
                                           f"alone (otadata, 0x10000)")
    check(any("Could not read the partition table -- migration check skipped, app only." in x for x in log),
          "the log does not say the check was skipped")
    check(any("could not read it:" in x and "Failed to connect" in x for x in log),
          "the log does not carry esptool's own reason the read failed")


@case("an Update whose partition-table read fails is refused, nothing written and the board restarted, once the app "
      "is larger than the legacy slot: 'Use Flash FW instead'", group="partition_edges")
def unreadable_table_app_too_big(ctx):
    import wcb_flash
    size = wcb_flash.LEGACY_APP_SLOT + 1
    e, log, err = _update(ctx, "u-unread-big", {wcb("ESP32.bin"): blob("app", size), wcb("ESP32_part.bin"): BUILD_TABLE,
                                                wcb("ESP32_boot.bin"): blob("boot", 128)}, BUILD_TABLE, read_rc=2)
    check(err is not None and "Use Flash FW instead" in err and "Could not read the board's partition table" in err,
          f"a {size}-byte app onto an unreadable table: {'accepted' if err is None else err[:200]!r}, expected the "
          f"'Use Flash FW instead' refusal (wcb_flash.py:387-391)")
    check(e.writes == [], f"a refused Update still wrote {[[hex(a) for a in w] for w in e.writes]}")
    check(_restarted(e), f"the refusal left the board in its ROM loader: esptool runs {e.runs} (boot_app)")


@case("a build with no partition table skips the check without reading the board, and updates the app alone",
      group="partition_edges")
def no_table_in_build(ctx):
    e, log, err = _update(ctx, "u-nopart", {wcb("ESP32.bin"): blob("app", 256), wcb("ESP32_boot.bin"): blob("boot", 128)},
                          BOARD_DEFAULT_TABLE)
    check(err is None, f"an Update from a build with no table was refused: {err}")
    check(e.reads == [] and not any(v in ("read-flash", "read_flash") for v, _ in e.runs),
          f"the board's table was read although the build has none to compare: {e.runs}")
    check(e.writes == [[0xE000, 0x10000]], f"Update wrote {[[hex(a) for a in w] for w in e.writes]}")
    check(any("No partition table in this build -- migration check skipped, app only." in x for x in log),
          "the log does not say the check was skipped for want of a table")


@case("an Update onto a board whose table differs is refused, nothing written and the board restarted, when the build "
      "has no bootloader to escalate with", group="partition_edges")
def escalation_without_bootloader(ctx):
    e, log, err = _update(ctx, "u-noboot", {wcb("ESP32.bin"): blob("app", 256), wcb("ESP32_part.bin"): BUILD_TABLE},
                          BOARD_DEFAULT_TABLE)
    check(err is not None and "has to write the bootloader and table as well" in err
          and f"no bootloader ({wcb('ESP32_boot.bin')}) in this build" in err,
          f"a differing table with no bootloader in the build: {'accepted' if err is None else err[:240]!r}")
    check(e.reads and e.reads[0] == (0x8000, 0x200), f"the table was not read before the refusal: {e.reads}")
    check(e.writes == [], f"a refused escalation still wrote {[[hex(a) for a in w] for w in e.writes]}")
    check(_restarted(e), f"the refusal left the board in its ROM loader: esptool runs {e.runs} (boot_app)")


# ------------------------------------------------------------------ group "reachable"
class _Net:
    """socket.getaddrinfo and socket.socket for flash.reachable: per host a list of (family, addr, behaviour) where
    behaviour is 'accept', 'refuse' or 'hang' (waits out the socket's timeout), or an OSError to raise from getaddrinfo;
    dns_delay holds getaddrinfo back. Every lookup and connect is recorded."""

    def __init__(self, hosts, dns_delay=0.0):
        import socket
        self.hosts, self.dns_delay, self.lookups, self.connects = hosts, dns_delay, [], []
        net = self

        class Sock:
            def __init__(self, family, socktype, proto=0):
                self.timeout = None

            def settimeout(self, t):
                self.timeout = t

            def connect(self, addr):
                how = net.how.get(addr[0], "refuse")
                net.connects.append((addr[0], round(time.monotonic() - net.t0, 2)))
                if how == "hang":
                    time.sleep(self.timeout or 0)
                    raise socket.timeout("timed out")
                if how == "refuse":
                    raise ConnectionRefusedError(61, "Connection refused")

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *e):
                self.close()
        self.Sock = Sock
        self.how = {}
        self.t0 = time.monotonic()

    def getaddrinfo(self, host, port, *a, **k):
        import socket
        self.lookups.append(host)
        time.sleep(self.dns_delay)
        rows = self.hosts.get(host)
        if isinstance(rows, BaseException):
            raise rows
        out = []
        for family, addr, how in rows or []:
            self.how[addr] = how
            fam = socket.AF_INET6 if family == 6 else socket.AF_INET
            out.append((fam, socket.SOCK_STREAM, 6, "", (addr, port) if family == 4 else (addr, port, 0, 0)))
        return out


class _Patched:
    def __init__(self, net):
        self.net = net

    def __enter__(self):
        import socket
        self.saved = (socket.getaddrinfo, socket.socket, os.environ.pop("INTELLEX_OFFLINE", None))
        socket.getaddrinfo, socket.socket = self.net.getaddrinfo, self.net.Sock
        self.net.t0 = time.monotonic()
        return self.net

    def __exit__(self, *exc):
        import socket
        socket.getaddrinfo, socket.socket, off = self.saved
        if off is not None:
            os.environ["INTELLEX_OFFLINE"] = off


@case("flash.reachable: each address gets its own attempt (a dead IPv6 first does not spend the budget), the budget "
      "outlasts a slow DNS answer and bounds a hung one, answers are kept per host for 20 s, INTELLEX_OFFLINE answers "
      "False at once", group="reachable")
def reachable_rules(ctx):
    import socket
    import flash
    problems = []
    flash._reach.clear()
    check(flash.REACH_TTL_S == 20.0, f"REACH_TTL_S is {flash.REACH_TTL_S}; this case is written for 20 s")
    v6dead = {"hil-a.example": [(6, "2001:db8::1", "hang"), (4, "192.0.2.10", "accept")]}
    with _Patched(_Net(v6dead)) as net:
        t0 = time.monotonic()
        ok = flash.reachable("hil-a.example", timeout=0.6, budget=3.0)
        took = time.monotonic() - t0
    if ok is not True or [c[0] for c in net.connects] != ["2001:db8::1", "192.0.2.10"]:
        problems.append(f"a dead IPv6 address then a live IPv4 one: {ok}, tried {net.connects}")
    if took > 1.5:
        problems.append(f"the live address answered {took:.1f} s in: the dead one spent more than its 0.6 s timeout")

    with _Patched(_Net({"hil-slowdns.example": [(4, "192.0.2.11", "accept")]}, dns_delay=1.2)) as net:
        ok = flash.reachable("hil-slowdns.example", timeout=0.5, budget=3.0)
    if ok is not True:
        problems.append(f"a DNS answer slower than one connect's timeout (1.2 s > 0.5 s) but inside the budget: {ok}, "
                        f"reason {flash.unreachable_reason('hil-slowdns.example')!r} - the budget must cover the lookup")

    # The lookup outlives the budget; when it finally answers, it fails - the probe thread that reachable() stopped
    # waiting for must not go on to a connect after the stand-ins are put back (that would be a real one).
    with _Patched(_Net({"hil-hung.example": socket.gaierror(8, "HIL: answered too late")}, dns_delay=2.5)) as net:
        t0 = time.monotonic()
        ok = flash.reachable("hil-hung.example", timeout=0.5, budget=0.8)
        took = time.monotonic() - t0
    why = flash.unreachable_reason("hil-hung.example")
    if ok is not False or took > 1.5 or "no answer within" not in why:
        problems.append(f"DNS hanging past the 0.8 s budget: {ok} after {took:.1f} s, reason {why!r}")

    with _Patched(_Net({"hil-refused.example": [(6, "2001:db8::2", "refuse"), (4, "192.0.2.13", "refuse")]})) as net:
        ok = flash.reachable("hil-refused.example", timeout=0.5, budget=2.0)
    why = flash.unreachable_reason("hil-refused.example")
    if ok is not False or "no address accepted a connection" not in why or "2001:db8::2" not in why \
            or "192.0.2.13" not in why:
        problems.append(f"every address refusing: {ok}, reason {why!r}")

    with _Patched(_Net({"hil-nodns.example": socket.gaierror(8, "nodename nor servname provided")})) as net:
        ok = flash.reachable("hil-nodns.example", timeout=0.5, budget=2.0)
    if ok is not False or not flash.unreachable_reason("hil-nodns.example").startswith("DNS lookup failed"):
        problems.append(f"a DNS failure: {ok}, reason {flash.unreachable_reason('hil-nodns.example')!r}")

    # Per host, and kept 20 s: hil-a.example is still up although hil-nodns.example is down, without asking again.
    with _Patched(_Net({"hil-a.example": [(4, "192.0.2.10", "refuse")]})) as net:
        ok = flash.reachable("hil-a.example", timeout=0.5, budget=2.0)
        if ok is not True or net.lookups:
            problems.append(f"a second ask inside 20 s: {ok}, looked up {net.lookups} - the answer is not kept per "
                            f"host for REACH_TTL_S")
        if flash.unreachable_reason("hil-a.example") != "":
            problems.append(f"hil-a.example's reason is {flash.unreachable_reason('hil-a.example')!r} after another "
                            f"host failed: the answers are not kept apart")
        when, up, why = flash._reach["hil-a.example"]
        flash._reach["hil-a.example"] = (when - 20.5, up, why)          # 20.5 s ago
        ok = flash.reachable("hil-a.example", timeout=0.5, budget=2.0)
        if ok is not False or net.lookups != ["hil-a.example"]:
            problems.append(f"an answer 20.5 s old: {ok}, looked up {net.lookups} - expected a fresh probe")

    with _Patched(_Net({"hil-off.example": [(4, "192.0.2.14", "accept")]})) as net:
        os.environ["INTELLEX_OFFLINE"] = "1"
        try:
            t0 = time.monotonic()
            ok = flash.reachable("hil-off.example")
            took = time.monotonic() - t0
        finally:
            os.environ.pop("INTELLEX_OFFLINE", None)
    if ok is not False or net.lookups or took > 0.2 or \
            flash.unreachable_reason("hil-off.example") != "INTELLEX_OFFLINE is set":
        problems.append(f"INTELLEX_OFFLINE: {ok} after {took:.2f} s, looked up {net.lookups}, reason "
                        f"{flash.unreachable_reason('hil-off.example')!r}")
    flash._reach.clear()
    check(not problems, "; ".join(problems))


# ------------------------------------------------------------------ group "nc_fetch_store"
@case("NaviCore's fetch_images online stores the listing as GitHub sent it and each image it downloads under "
      "firmware/navicore/<branch> with the manifest; an empty image is refused and not stored; offline the stored set "
      "comes back", group="nc_fetch_store")
def nc_fetch_store(ctx):
    import json
    import flash
    app, part, boot = f"NaviCore_{NC_TAG}_ESP32S3.bin", f"NaviCore_{NC_TAG}_ESP32S3_part.bin", flash.BOOTLOADER_NAME
    other = "RC-Controller_1.0.0_ESP32S3.bin"
    data = {app: blob("app-online", 256), part: blob("part-online", 96), boot: blob("boot-online", 128),
            other: blob("rc-online", 64)}
    problems = []
    for branch, key in (("hil-store", "hil-store"), ("feature/hil-store", "feature%2Fhil-store")):   # fwcache._key percent-encodes since 2026-10-13
        base = f"https://raw.githubusercontent.com/{flash.GITHUB_OWNER}/{flash.GITHUB_REPO}/{branch}/firmware/"
        listing = json.dumps([{"name": n, "type": "file", "size": len(b), "download_url": base + n}
                              for n, b in data.items()]).encode()
        asked = []

        def get(url, _log=None, **_k):
            asked.append(url)
            if "api.github.com" in url:
                return listing
            return data[url.rsplit("/", 1)[1]]
        r0, g0 = flash.reachable, flash._get
        flash.reachable, flash._get = (lambda *a, **k: True), get
        log = []
        try:
            got = flash.fetch_images(branch, log.append)
        finally:
            flash.reachable, flash._get = r0, g0
        d = os.path.join(ctx.src, "firmware", "navicore", key)
        if [(e["address"], e["name"], e["data"]) for e in got] != [(0x0, boot, data[boot]), (0x8000, part, data[part]),
                                                                   (0x10000, app, data[app])]:
            problems.append(f"{branch}: fetch_images returned {[(hex(e['address']), e['name']) for e in got]}")
        try:
            stored_listing = open(os.path.join(d, "listing.json"), "rb").read()
        except OSError:
            stored_listing = None
        if stored_listing != listing:
            problems.append(f"{branch}: the listing was not stored as GitHub sent it under firmware/navicore/{key}")
        for n in (app, part, boot):
            try:
                ok = open(os.path.join(d, n), "rb").read() == data[n]
            except OSError:
                ok = False
            if not ok:
                problems.append(f"{branch}: {n} was not stored under firmware/navicore/{key}")
        if os.path.exists(os.path.join(d, other)) or any(other in u for u in asked):
            problems.append(f"{branch}: another product's image was downloaded or stored")
        if any(f.endswith(".part") for f in os.listdir(d)):
            problems.append(f"{branch}: a .part file was left behind: {sorted(os.listdir(d))}")
        try:
            m = json.load(open(os.path.join(ctx.src, "firmware", "manifest.json"), encoding="utf-8"))
        except (OSError, ValueError):
            m = {}
        if sorted((m.get(f"navicore/{key}") or {})) != sorted([app, part, boot]):
            problems.append(f"{branch}: the manifest's navicore/{key} names {sorted(m.get(f'navicore/{key}') or {})}")
        if branch != "main" and not any("WARNING firmware source branch" in x for x in log):
            problems.append(f"{branch}: no non-main branch warning in the flash log")
        # Offline: the same set, from the cache.
        r0 = flash.reachable
        flash.reachable = lambda *a, **k: False
        try:
            again = [(e["name"], e["data"]) for e in flash.fetch_images(branch)]
        except flash.FlashError as e:
            again = f"refused: {e}"
        finally:
            flash.reachable = r0
        if again != [(e["name"], e["data"]) for e in got]:
            problems.append(f"{branch}: offline the cached set is not what was downloaded "
                            f"({again if isinstance(again, str) else [n for n, _ in again]})")

    # GitHub hands back an empty image: refused, and nothing stored under its name.
    branch = "hil-empty"
    base = f"https://raw.githubusercontent.com/{flash.GITHUB_OWNER}/{flash.GITHUB_REPO}/{branch}/firmware/"
    listing = json.dumps([{"name": app, "type": "file", "size": 0, "download_url": base + app}]).encode()
    r0, g0 = flash.reachable, flash._get
    flash.reachable = lambda *a, **k: True
    flash._get = lambda url, _log=None, **_k: listing if "api.github.com" in url else b""
    try:
        raises(lambda: flash.fetch_images(branch), "is empty")
    except AssertionError as e:
        problems.append(f"an empty image from GitHub: {e}")
    finally:
        flash.reachable, flash._get = r0, g0
    if os.path.exists(os.path.join(ctx.src, "firmware", "navicore", branch, app)):
        problems.append("an empty image was stored under its build's name")
    check(not problems, "; ".join(problems))



# ------------------------------------------------------------------ group "progress" (intellex.flash_progress_unit)
def _wline(addr, pct, done=0, total=1):
    """One esptool 5 progress line (esptool logger.py's bar), as the fake esptool prints it."""
    return f"Writing at {addr:#010x} [{'=' * int(pct // 10):<10}] {pct:5.1f}% {done}/{total} bytes..."


@case("flash.write_progress weights each region by its size and finds it by the address its line names: a 2 KB "
      "bootloader written to 100 % is about 0.1 % of a 2 MB write, not 99; a region's last line, naming its end where "
      "the next begins, stays with it; a line naming no address passes its percent through; /_api/flash-status's "
      "percent is monotonic and held at 99 until done; its log keeps the last 400 lines", group="progress")
def flash_progress(ctx):
    import contextlib
    import io
    import flash
    import host
    problems = []
    boot = {"address": 0x1000, "data": b"\0" * 2048}
    app = {"address": 0x10000, "data": b"\0" * (2 * 1024 * 1024)}
    total = 2048 + 2 * 1024 * 1024
    got = []
    on_line = flash.write_progress([app, boot], got.append)          # unsorted on purpose: it sorts by address
    for line in (_wline(0x1000, 0.0), _wline(0x1400, 50.0), _wline(0x1800, 100.0)):
        on_line(line)
    if got != [0, 0, int(2048 * 100.0 / total)]:
        problems.append(f"the 2 KB bootloader at 0, 50 and 100 %: reported {got}, expected [0, 0, "
                        f"{int(2048 * 100.0 / total)}] (its 2 KB of {total} bytes)")
    del got[:]
    for line in (_wline(0x10000, 0.0), _wline(0x110000, 50.0), _wline(0x210000, 100.0)):
        on_line(line)
    want = [int(2048 * 100.0 / total), int((2048 + 1024 * 1024) * 100.0 / total), 100]
    if got != want:
        problems.append(f"the 2 MB app at 0, 50 and 100 %: reported {got}, expected {want}")
    # WCB's otadata (0xE000, 8 KB) ends at 0x10000 where the app begins: its 100 % line names 0x10000.
    ota = {"address": 0xE000, "data": b"\0" * 0x2000}
    got2 = []
    on2 = flash.write_progress([ota, app], got2.append)
    on2(_wline(0xE000, 0.0))
    on2(_wline(0x10000, 100.0))
    if got2 != [0, int(0x2000 * 100.0 / (0x2000 + 2 * 1024 * 1024))]:
        problems.append(f"otadata's last line (at 0x10000, the app's start): reported {got2}, expected it kept with "
                        f"otadata - [0, {int(0x2000 * 100.0 / (0x2000 + 2 * 1024 * 1024))}], not the app's 100")
    got3 = []
    on3 = flash.write_progress([boot, app], got3.append)
    on3("Erasing flash... (37 %)")                                      # esptool 4's form, no address
    on3("Connecting....")
    if got3 != [37]:
        problems.append(f"a percent with no 'Writing at' address: reported {got3}, expected [37] as it stands")
    if flash.write_progress([boot], None)(_wline(0x1000, 50.0)) is not None:
        problems.append("progress None: the handler returned something")
    # Through the host's state: monotonic, and 99 at most until the job marks it done.
    with host._flash_lock:
        host._flash_state.update(percent=0, log=[])
    on4 = flash.write_progress([boot, app], host._flash_progress)
    seen = []
    for line in (_wline(0x1000, 100.0), _wline(0x10000, 0.0), _wline(0x110000, 50.0), _wline(0x1000, 10.0),
                 _wline(0x210000, 100.0)):
        on4(line)
        seen.append(host._flash_state["percent"])
    if seen[0] > 1 or seen[2] != int((2048 + 1024 * 1024) * 100.0 / total) or seen[3] < seen[2] or seen[4] != 99:
        problems.append(f"/_api/flash-status's percent through a bootloader at 100 %, the app at 0 and 50 %, a stray "
                        f"bootloader 10 % line and the app at 100 %: {seen}, expected about 0, then "
                        f"{int((2048 + 1024 * 1024) * 100.0 / total)}, never backwards, and 99 at the end")
    with contextlib.redirect_stdout(io.StringIO()):
        for i in range(500):
            host._flash_log(f"HILFLASH-{i:03d}")
    log = list(host._flash_state["log"])
    if len(log) != 400 or log[0] != "HILFLASH-100" or log[-1] != "HILFLASH-499":
        problems.append(f"after 500 lines the flash log holds {len(log)}, {log[:1]} .. {log[-1:]}, expected the last "
                        f"400 (HILFLASH-100 .. HILFLASH-499)")
    with host._flash_lock:
        host._flash_state.update(percent=0, log=[])
    check(not problems, "; ".join(problems))


if __name__ == "__main__":
    sys.exit(_ixpy.main())
