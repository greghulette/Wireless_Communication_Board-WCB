"""A WCB's app written with esptool, the way the Wizard's and Intellex's Update write it: the image into app0 (0x10000)
and the core's boot_app0.bin into otadata (0xe000), which selects app0 - nothing else. The bootloader, the partition
table, NVS and the sequence store stay as they are, which is the whole point for suites/s54_upgrade.py: a board moved
from the released firmware to this one keeps everything a user's board would.

The port is handed to esptool and taken back with hil/intellex.py handed_over, which proves the board answers ?VERSION
afterwards. ncflash.guard_writes refuses any argv that would write elsewhere or erase.
"""
import os
import tempfile

from . import ncflash

APP0_AT, OTADATA_AT = ncflash.APP0_AT, ncflash.OTADATA_AT


def write_argv(port, app, otadata):
    """esptool: app0 and otadata, one connection, through the classic auto-reset circuit (DTR/RTS) - a WCB is not the
    S3 native USB that NaviCore's write_argv is for. --chip auto: the WCBs are ESP32 and ESP32-S3."""
    return ["--chip", "auto", "-p", port, "--before", "default-reset", "--after", "hard-reset", "write-flash", "-z",
            "--flash-mode", "keep", "--flash-freq", "keep", "--flash-size", "keep",
            f"0x{APP0_AT:x}", app, f"0x{OTADATA_AT:x}", otadata]


def write_app(bench, device, image, timeout=300):
    """Write `image` (bytes) as `device`'s app -> the tail of esptool's output. AssertionError when esptool fails; the
    board is then still on whatever it ran (esptool verifies before it resets)."""
    from .intellex import handed_over
    fd, path = tempfile.mkstemp(prefix="wcb-app-", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(image)
        with handed_over(bench, device) as port:
            argv = ncflash.guard_writes(write_argv(port, path, ncflash.boot_app0_path()))
            rc, out = ncflash.run_esptool(argv, timeout=timeout)
        tail = "\n".join(out.strip().splitlines()[-4:])
        if rc != 0:
            raise AssertionError(f"esptool could not write {device}'s app (exit {rc}): {tail}")
        return tail
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
