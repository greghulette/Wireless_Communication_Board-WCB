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
"""
import os
import subprocess
import sys

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


if __name__ == "__main__":
    sys.exit(_ixpy.main())
