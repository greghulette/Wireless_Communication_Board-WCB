"""A stand-in for esptool 5.3.1, for Intellex's flash pipeline test (tests/intellex/py/flash_pipeline.py). That script
puts this directory's parent first on PYTHONPATH, so the `python -m esptool` Intellex runs (flash.py _esptool_argv) finds
this package ahead of the venv's real one. It NEVER opens a port. It prints what esptool 5.3.1 prints for the commands
Intellex runs (flash-id, write-flash, read-flash), in 5.3.1's own shapes (esptool/__init__.py:478, :559-569;
cmds.py:2050-2080, :1437-1444, :1536-1572; logger.py:223-248 with no terminal, as under a pipe), and appends one JSON
line per run to $FAKE_ESPTOOL_LOG: the argv, and for a write the address, length and SHA-256 of every file it names.

    FAKE_ESPTOOL_CHIP   esp32 (default) | esp32s3_8mb | esp32s3_16mb | esp32c3: the fixture flash-id prints
    FAKE_ESPTOOL_FAIL   flash-id | write: fail that command with esptool's own exit code 2
    FAKE_ESPTOOL_TABLE  a file whose bytes a read-flash returns (the board's partition table); 0xFF without it
    FAKE_ESPTOOL_DELAY  seconds between progress lines, so a poller can see a flash in progress
"""
import hashlib
import json
import os
import sys
import time

SIGNATURE = "FAKE-ESPTOOL-FOR-INTELLEX-HIL"
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "esptool")
CHIPS = {"esp32": ("flash_id_esp32.txt", "ESP32"), "esp32s3_8mb": ("flash_id_esp32s3_8mb.txt", "ESP32-S3"),
         "esp32s3_16mb": ("flash_id_esp32s3_16mb.txt", "ESP32-S3"), "esp32c3": ("flash_id_esp32c3.txt", "ESP32-C3")}
COMMANDS = ("flash-id", "flash_id", "write-flash", "write_flash", "read-flash", "read_flash", "version")
FLAGS = ("--compress", "-z", "--no-compress", "-u", "--no-progress", "-p", "--force", "--erase-all", "-e",
         "--no-stub", "--trace", "-t")


def progress(prefix, done, total, suffix):
    """esptool 5.3.1's progress_bar with no terminal (logger.py:223-248): a whole line per update."""
    width = 30
    filled = int(width * done // total)
    bar = "=" * width if filled == width else " " * width if filled == 0 else \
        f"{'=' * (filled - 1)}>{' ' * (width - filled)}"
    print(f"\r{prefix}[{bar}] {100 * (done / float(total)):>5.1f}%{suffix} ", flush=True)


def regions(tokens):
    """[(address, path)] from write-flash's arguments after the command: options take a value unless they are flags."""
    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("-"):
            i += 1 if t in FLAGS else 2
            continue
        out.append((int(t, 0), tokens[i + 1]))
        i += 2
    return out


def record(entry):
    path = os.environ.get("FAKE_ESPTOOL_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def deprecations(argv):
    """The warnings esptool 5.3.1 prints for esptool 4's underscore spellings (cli_util.py:35-44, :350-383)."""
    out = []
    for i, a in enumerate(argv):
        if a in ("write_flash", "flash_id", "read_flash"):
            out.append(f"Warning: Deprecated: Command '{a}' is deprecated. Use '{a.replace('_', '-')}' instead.")
        elif a.startswith("--") and "_" in a:
            out.append(f"Warning: Deprecated: Option '{a}' is deprecated. Use '{a.replace('_', '-')}' instead.")
        elif i and argv[i - 1] in ("--before", "--after") and "_" in a:
            out.append(f"Warning: Deprecated: Choice '{a}' for option '{argv[i - 1]}' is deprecated. Use "
                       f"'{a.replace('_', '-')}' instead.")
    return out


def connect(port, chip_name):
    print("esptool v5.3.1")
    print("Connecting....")
    print(f"Connected to {chip_name} on {port}:")


def main(argv):
    if argv[:1] == ["--fake-signature"]:
        print(SIGNATURE)
        return 0
    cmd = next((a for a in argv if a in COMMANDS), None)
    port = argv[argv.index("--port") + 1] if "--port" in argv else "?"
    fixture, chip_name = CHIPS.get(os.environ.get("FAKE_ESPTOOL_CHIP", "esp32"), CHIPS["esp32"])
    fail = os.environ.get("FAKE_ESPTOOL_FAIL", "")
    entry = {"argv": argv, "cmd": cmd}
    if cmd in ("write-flash", "write_flash"):
        files = []
        for addr, path in regions(argv[argv.index(cmd) + 1:]):
            with open(path, "rb") as f:
                data = f.read()
            files.append({"address": addr, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                          "all_ff": data == b"\xff" * len(data)})
        entry["files"] = files
    record(entry)
    for w in deprecations(argv):
        print(w, flush=True)

    if cmd in ("flash-id", "flash_id"):
        if fail == "flash-id":
            print("esptool v5.3.1")
            print("Connecting......................................")
            print("A fatal error occurred: Failed to connect to Espressif device: No serial data received.")
            return 2
        with open(os.path.join(FIXTURES, fixture), encoding="utf-8") as f:
            sys.stdout.write(f.read().replace("COMFAKE", port))
        return 0
    if cmd in ("read-flash", "read_flash"):
        tail = [a for a in argv[argv.index(cmd) + 1:] if not a.startswith("-")]
        addr, size, out = int(tail[0], 0), int(tail[1], 0), tail[2]
        table = os.environ.get("FAKE_ESPTOOL_TABLE")
        data = open(table, "rb").read() if table else b""
        data = (data + b"\xff" * size)[:size]
        with open(out, "wb") as f:
            f.write(data)
        connect(port, chip_name)
        print(f"Read {size} bytes from {addr:#010x} in 0.1 seconds.")
        return 0
    if cmd in ("write-flash", "write_flash"):
        connect(port, chip_name)
        print("Stub flasher running.")
        print("Changing baud rate to 921600...")
        print("Changed.")
        delay = float(os.environ.get("FAKE_ESPTOOL_DELAY") or 0)
        for n, f in enumerate(entry["files"]):
            size, addr = f["size"], f["address"]
            print(f"Compressed {size} bytes to {max(1, size // 3)}...")
            for step in range(0, 5):
                done = size * step // 4
                progress(f"Writing at {addr + done:#010x} ", done, size, f" {done // 3}/{max(1, size // 3)} bytes...")
                time.sleep(delay)
                if fail == "write" and n == 0 and step == 2:
                    print("A fatal error occurred: Packet content transfer stopped (received 8 bytes)", flush=True)
                    return 2
            print(f"Wrote {size} bytes ({max(1, size // 3)} compressed) at {addr:#010x} in 0.1 seconds.")
            print("Hash of data verified.")
        print("Hard resetting via RTS pin...")
        return 0
    if cmd == "version":
        print("5.3.1")
        return 0
    print(f"A fatal error occurred: the fake esptool does not do {argv}")
    return 2
