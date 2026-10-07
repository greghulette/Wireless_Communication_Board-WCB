"""NaviCore firmware images for the harness: build one, check it, flash it over NaviCore's own ?OTALOCAL USB protocol,
and recover a board that does not come back. docs/hil_plan/NAVICORE.md §3 INF4; how a test or a session uses it:
docs/HIL_TESTING.md §5, "Building, flashing and recovering NaviCore".

Nothing here runs `arduino-cli upload` or writes the bootloader (0x0) or the partition table (0x8000). A flash is
?OTALOCAL over the harness's own handle on NaviCore's USB port, brick-safe by construction: the board writes the
INACTIVE slot, esp_ota_end checks the image's checksum and SHA-256 before the boot pointer moves, and a failure, an
ABORT or 30 s without a written chunk leaves the running app as it was (navicore_ota.h:1-11, :104, :134-137, :206-224).
esptool is the recovery ladder's last rung, and runs only when the caller allows it (recover()).

The protocol, as NaviCore implements it (navicore_ota.h:244-310; routed by the '?OTALOCAL,' prefix, NaviCore.ino:3360,
so a bare '?OTALOCAL' is "Unknown command"):
  ?OTALOCAL,STATUS            Chip (family 1 on the S3), Firmware, Running, Next (OTA), Session (:226-238)
  ?OTALOCAL,BEGIN,<size>,1    '[OTA:BEGIN,START]' at once, then the blocking erase of the inactive slot, then
                              '[OTA:BEGIN,OK,0]', or '[OTA:BEGIN,ERR,0]' after an '[OTA] BEGIN rejected: ...' line
                              (:252-268). The family is checked against the board (:143-147), never the image's header.
  ?OTALOCAL,DATA,<off>,<b64>  at most 1024 decoded bytes (:106), written only at the write cursor: '[OTA:ACK,<cursor>]';
                              anything else '[OTA:NAK,<cursor>]', where the cursor reads 0 once the session is gone
                              (:119, :270-292). A line whose base64 does not decode gets NO marker at all (:278-281).
  ?OTALOCAL,END               '[OTA:END,OK]' and a restart 2 s later, or '[OTA:END,ERR]' (:294-305)
  ?OTALOCAL,ABORT             ends the session; prints only when one was active (:307, :129-132)
The session id is fixed at 1 (:105), and a BEGIN supersedes a stale session (:141).

The sender keeps one chunk in flight. The config tool pipelines eight (config_tool/index.html otaUpdateOverUsb), which
needs Go-Back-N bookkeeping for markers that arrive late; one at a time costs about a minute for a 1.17 MB image over
the harness's reader and leaves nothing ambiguous. An ACK must name exactly the chunk's end. A NAK naming the chunk's own
offset means the line arrived damaged and nothing was written, so the chunk goes again: the rewind. Anything else (no
marker within chunk_timeout, a NAK elsewhere, an ACK for another length) is settled by ?OTALOCAL,STATUS. The board
answers lines in order, so its 'Session: ACTIVE id=1 <written> / <size> B' comes after every marker of the lines before
it and no stale marker can follow it. The cursor is then the chunk's start (send it again), its end (the marker was
lost; go on) or anywhere else (a damaged line was written: this image cannot be completed, so the session is aborted).
While it waits, the sender writes a lone newline every nudge_s. An empty line does nothing on NaviCore
(NaviCore.ino:4277), but a packet from the host is what releases output HWCDC is holding back (the stall
kickUsbCdcTx fixes, NaviCore.ino:5354-5376), so an image without that fix still answers.
"""
import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from contextlib import contextmanager
from datetime import datetime

import serial

from .checkpoint import RunBusy, RunLock, atomic_write_text, redact_text
from .navicore import DOWNLOAD_MODE, NaviCore, parse_boot
from .wcb import dev_note

HERE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))      # tests/hil
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
RESULTS = os.path.join(HERE, "results")
BUILDS = os.path.join(RESULTS, "builds")
KNOWN_GOOD = "navicore"        # folder under BUILDS: the image on the board since 2026-09-22 (FLASHED.md)
# Folder under BUILDS: the image the bench NaviCore runs (docs/HIL_WEEK_DECISIONS.md D77, D80: NaviCore main 520b096,
# built as 6bd0ced before its rebase onto CI's auto-build, same source; the
# aux-port fixes of 2026-10-06 on D76's tree - RMT soft TX, the device-write gate, the console off UART0, D-NC57,
# D-NC59 - D-NC75's paced long replies and D-NC77's boot-panic fix (the GPIO ISR service installed first in setup()),
# with WCB_Client 1.17.2, App SHA256 b199a45cf155d8a5, with the NAVICORE_HIL_HOOKS hooks, built on the Mac; before it
# navicore-fix6 (0657025, 88ddbd392b06899e), navicore-fix5 (c454c66, 46640d0be405faf7), D76's navicore-fix4
# (57c83d3, 4dbe0b305ce57a22) and D45's navicore-hil1, 6925773). The tests that flash NaviCore flash only this image
# and end on it, verified by its App SHA256 (suites/s47_navicore_ota.py); ncota.image_identity fails when the board
# runs anything else. A new image changes this one line. KNOWN_GOOD above stays the rollback.
BENCH_IMAGE = "navicore-fix7"
FLASHED = "FLASHED.md"

# NaviCore/CLAUDE.md:113 and docs/BUILD_AND_RELEASE.md:60, verbatim; build() refuses once CLAUDE.md stops naming it.
# PSRAM=opi is load-bearing: without it ps_calloc returns null and the board halts on a red LED.
FQBN = "esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,PartitionScheme=custom,FlashSize=16M,PSRAM=opi"
CORE_ID, CORE_VERSION = "esp32:esp32", "3.3.4"     # pinned across NaviCore, WCB and SBUSController
ESPTOOL_VERSION = "5.1.0"                          # the core's own esptool (packages/esp32/tools/esptool_py)
# In the esp32 3.3.4 recipe (platform.txt:70, used at :152) and empty by default, so a build without the hooks omits it.
HOOKS_PROPERTY = "compiler.cpp.extra_flags=-DNAVICORE_HIL_HOOKS=1"
SKETCH, IMAGE, ELF = "NaviCore.ino", "NaviCore.ino.bin", "NaviCore.ino.elf"
MANIFEST, COMPILE_LOG = "BUILD.json", "compile.log"
LIBS = (("WCB_Client", "WCBClient"), ("WcbCmd", "WcbCmd"))       # sketchbook folder -> its repo
LIB_PARTS = ("src", "library.properties")                        # what a compile reads of a library
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
SOURCE_EXT = (".ino", ".h", ".hpp", ".c", ".cpp", ".s", ".csv")

# The app image (esp_image_header_t, then segments, a checksum byte and the appended SHA-256), and esp_app_desc_t at
# 0x20 inside the first segment, whose app_elf_sha256 field sits at 0xB0: elf2image writes the ELF's SHA-256 there
# (--elf-sha256-offset 0xb0, platform.txt:167).
IMAGE_MAGIC = 0xE9
CHIP_ID_ESP32S3 = 9
FAMILY_ESP32S3 = 1             # ?OTALOCAL,BEGIN's family for an S3 (navicore_ota.h:109-117)
APP_DESC_AT, APP_DESC_MAGIC = 0x20, 0xABCD5432
ELF_SHA_AT = 0xB0
# FW_VERSION: FW_VERSION_BASE "_" FW_VERSION_DTG, e.g. v0.2.0_102105QSEP26 (fw_version.h). The DTG changes only on a
# commit, so two images of one commit carry the same string: the ELF SHA-256 tells them apart.
VERSION_B = re.compile(rb"v\d+\.\d+\.\d+[A-Za-z0-9.+-]*_\d{6}[A-Z]{4}\d{2}")
VERSION_S = re.compile(VERSION_B.pattern.decode())

CHUNK = 1024                   # OTA_LOCAL_MAX_CHUNK (navicore_ota.h:106)
IDLE_S = 30                    # OTA_TIMEOUT_MS (navicore_ota.h:104): the board's idle reaper
M_BEGIN = re.compile(r"\[OTA:BEGIN,(START|OK|ERR)(?:,(\d+))?\]")
M_BEGIN_DONE = re.compile(r"\[OTA:BEGIN,(OK|ERR),(\d+)\]")
M_CURSOR = re.compile(r"\[OTA:(ACK|NAK),(\d+)\]")
M_END = re.compile(r"\[OTA:END,(OK|ERR)\]")
M_ABORTED = re.compile(r"\[OTA\] aborted: (.*)")
RESTARTED = re.compile(r"^<<serial error|Reset reason: ")    # the port dropped, or setup() began again

# Recovery writes only these two addresses, in one esptool connection: the known-good app into app0 and the core's
# boot_app0.bin into otadata, which selects app0 (partitions.csv:16-17). Never 0x0, never 0x8000.
APP0_AT, OTADATA_AT, OTADATA_LEN = 0x10000, 0xE000, 0x2000
ALLOWED_WRITES = frozenset((APP0_AT, OTADATA_AT))


class ImageError(AssertionError):
    """The image is not one NaviCore can take: nothing was sent."""


class BuildError(AssertionError):
    """The compile failed, or its inputs are not what the build claims to be."""


class FlashError(AssertionError):
    """A flash stopped. `stage` is where (ping, status, begin, data, end, boot, verify), `offset` how many bytes the board
    had written, `intact` True when STATUS afterwards showed the same app running (False: another app runs; None: it
    did not answer)."""

    def __init__(self, stage, offset, message, intact=None):
        super().__init__(message)
        self.stage, self.offset, self.intact = stage, offset, intact


class NotBack(FlashError):
    """END was accepted (the image verified and the boot slot moved) but NaviCore did not come back: recover()."""


class RecoveryFailed(AssertionError):
    """No rung of the recovery ladder brought NaviCore back, or the next rung needs allow_esptool."""


# ---------------------------------------------------------------------------- where things are
def github_root():
    """The folder holding the sibling repos (NaviCore, WCBClient, WcbCmd, Arduino-Code): $HIL_GITHUB_ROOT; else the
    nearest ancestor of this repo that has a NaviCore folder (a worktree sits deeper than the checkout); else this
    repo's parent, hil/intellex.py's convention."""
    env = os.environ.get("HIL_GITHUB_ROOT")
    if env:
        return env
    d = os.path.dirname(REPO)
    while True:
        if os.path.isdir(os.path.join(d, "NaviCore")):
            return d
        up = os.path.dirname(d)
        if up == d:
            return os.path.dirname(REPO)
        d = up


def cli_path():
    """The arduino-cli on PATH, else the one this PC installed (C:/Users/ghulette/tools/arduino-cli)."""
    found = shutil.which("arduino-cli")
    if found:
        return found
    fallback = os.path.join(os.path.expanduser("~"), "tools", "arduino-cli", "arduino-cli.exe")
    if os.path.isfile(fallback):
        return fallback
    raise BuildError("arduino-cli is not on PATH")


def _cli_config(key):
    try:
        r = subprocess.run([cli_path(), "config", "get", key], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError, BuildError):
        return None
    value = r.stdout.decode("utf-8", "replace").strip()
    return value if r.returncode == 0 and value else None


def sketchbook_dir(github=None):
    """arduino-cli's directories.user, whose libraries/ every local compile uses (the Arduino-Code repo on this PC)."""
    return _cli_config("directories.user") or os.path.join(github or github_root(), "Arduino-Code")


def default_arduino15(osname=os.name, platform=sys.platform, env=os.environ, home=os.path.expanduser("~")):
    """arduino-cli's own default data folder on this OS: %LOCALAPPDATA%\\Arduino15 on Windows, ~/Library/Arduino15 on a
    Mac, ~/.arduino15 elsewhere. ~/Arduino15, the old fallback off Windows, does not exist on the Mac bench, so
    ncota.recovery_esptool found no boot_app0.bin there (run 20261006-122850)."""
    if osname == "nt":
        return os.path.join(env.get("LOCALAPPDATA", home), "Arduino15")
    if platform == "darwin":
        return os.path.join(home, "Library", "Arduino15")
    return os.path.join(home, ".arduino15")


def arduino15():
    """The arduino-cli data folder that holds the esp32 core and its tools: HIL_ARDUINO15, arduino-cli's
    directories.data, else default_arduino15() - an IDE's older arduino-cli has no `config get` (0.35.3 on the Mac)."""
    return os.environ.get("HIL_ARDUINO15") or _cli_config("directories.data") or default_arduino15()


def esptool_path():
    """The core's esptool, ESPTOOL_VERSION when present."""
    root = os.path.join(arduino15(), "packages", "esp32", "tools", "esptool_py")
    exe = "esptool.exe" if os.name == "nt" else "esptool"
    versions = sorted(os.listdir(root)) if os.path.isdir(root) else []
    for v in ([ESPTOOL_VERSION] if ESPTOOL_VERSION in versions else []) + versions[::-1]:
        p = os.path.join(root, v, exe)
        if os.path.isfile(p):
            return p
    raise RecoveryFailed(f"no esptool under {root}")


def boot_app0_path():
    """The pinned core's boot_app0.bin: the otadata every Arduino upload writes at 0xe000 (platform.txt:180, :297)."""
    return os.path.join(arduino15(), "packages", "esp32", "hardware", "esp32", CORE_VERSION, "tools", "partitions",
                        "boot_app0.bin")


# ---------------------------------------------------------------------------- source trees
def _git(repo, *args, timeout=60):
    """stdout (bytes) of a read-only git command, None when it fails. --no-optional-locks: `status` must not refresh
    the index of a repo this harness only reads."""
    try:
        r = subprocess.run(["git", "-C", repo, "--no-optional-locks", *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def tree_state(repo):
    """{'repo', 'head', 'short', 'branch', 'subject', 'dirty', 'files', 'diff'} of a working tree: HEAD, the branch
    checked out (None when detached), whether anything is modified or untracked, those paths, and 'diff', the first 12
    hex of a SHA-256 over `git diff HEAD --binary` plus every untracked source file, so two builds of one dirty tree can
    be told apart. head None: not a git tree."""
    head = _git(repo, "rev-parse", "HEAD")
    if head is None:
        return {"repo": repo, "head": None, "short": None, "branch": None, "subject": None, "dirty": None, "files": [],
                "diff": None}
    head = head.decode().strip()
    branch = (_git(repo, "symbolic-ref", "--short", "-q", "HEAD") or b"").decode("utf-8", "replace").strip() or None
    subject = (_git(repo, "log", "-1", "--format=%s") or b"").decode("utf-8", "replace").strip()[:100]
    status = (_git(repo, "status", "--porcelain", "--untracked-files=all") or b"").decode("utf-8", "replace")
    rows = [x for x in status.splitlines() if x.strip()]
    files = [x[3:].strip().strip('"') for x in rows]
    h = hashlib.sha256(_git(repo, "diff", "HEAD", "--binary") or b"")
    for x in rows:
        path = x[3:].strip().strip('"')
        full = os.path.join(repo, path)
        if x.startswith("??") and path.lower().endswith(SOURCE_EXT) and os.path.isfile(full):
            h.update(path.encode("utf-8"))
            with open(full, "rb") as f:
                h.update(f.read())
    return {"repo": repo, "head": head, "short": head[:7], "branch": branch, "subject": subject, "dirty": bool(rows),
            "files": files, "diff": h.hexdigest()[:12] if rows else None}


def tree_line(state):
    """One line for a report: '`dda37f6` + 5 dirty files (diff 1a2b3c4d5e6f)', '`dda37f6` clean', or 'not recorded'; a
    branch other than main or master is named ('`0a66ce0` on hil-week clean'), so a row says when an image came from a
    local branch rather than the line of history everyone builds."""
    if not state or not state.get("head"):
        return "not recorded"
    where = f" on {state['branch']}" if state.get("branch") not in (None, "main", "master") else ""
    if not state.get("dirty"):
        return f"`{state['short']}`{where} clean"
    return f"`{state['short']}`{where} + {len(state['files'])} dirty files (diff {state['diff']})"


def _files(root, part):
    """{posix relative name: path} of `part` under root: a file, or every file of a folder."""
    full = os.path.join(root, part)
    if os.path.isfile(full):
        return {part: full}
    out = {}
    for dirpath, dirs, names in os.walk(full):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in names:
            p = os.path.join(dirpath, n)
            out[os.path.relpath(p, root).replace(os.sep, "/")] = p
    return out


def compare_tree(sketch_copy, repo, parts=LIB_PARTS):
    """The files a compile reads of one library, sketchbook copy against repo -> {'same', 'eol_only', 'differ',
    'only_sketchbook', 'only_repo'} (lists of relative names). A file that differs only in CRLF against LF compiles
    the same and is listed in eol_only; 'same' is False only for a real difference."""
    res = {"same": True, "eol_only": [], "differ": [], "only_sketchbook": [], "only_repo": []}
    for part in parts:
        fa, fb = _files(sketch_copy, part), _files(repo, part)
        for rel in sorted(set(fa) | set(fb)):
            if rel not in fb:
                res["only_sketchbook"].append(rel)
                continue
            if rel not in fa:
                res["only_repo"].append(rel)
                continue
            with open(fa[rel], "rb") as f:
                a = f.read()
            with open(fb[rel], "rb") as f:
                b = f.read()
            if a == b:
                continue
            if a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n"):
                res["eol_only"].append(rel)
            else:
                res["differ"].append(rel)
    res["same"] = not (res["differ"] or res["only_sketchbook"] or res["only_repo"])
    return res


def check_libs(sketchbook=None, github=None):
    """NaviCore's two shared libraries, the sketchbook copy a local compile uses (it shadows the real one, NaviCore
    BUILD_AND_RELEASE.md §3) against the repo CI clones -> {name: compare_tree(...) + 'sketchbook', 'repo', 'version',
    'repo_state'}; a missing folder gives 'same': False and 'missing'. Reads only; never copies anything."""
    github = github or github_root()
    libraries = os.path.join(sketchbook or sketchbook_dir(github), "libraries")
    out = {}
    for lib, repo_name in LIBS:
        a, b = os.path.join(libraries, lib), os.path.join(github, repo_name)
        if not os.path.isdir(a) or not os.path.isdir(b):
            out[lib] = {"same": False, "missing": [p for p in (a, b) if not os.path.isdir(p)], "sketchbook": a,
                        "repo": b, "repo_name": repo_name, "version": None, "repo_state": None}
            continue
        res = compare_tree(a, b)
        version = None
        props = os.path.join(b, "library.properties")
        if os.path.isfile(props):
            with open(props, encoding="utf-8", errors="replace") as f:
                version = next((x.split("=", 1)[1].strip() for x in f if x.startswith("version=")), None)
        res.update(sketchbook=a, repo=b, repo_name=repo_name, version=version, repo_state=tree_state(b))
        out[lib] = res
    return out


def libs_line(libs):
    """One line: 'WCB_Client = WCBClient 1.17.1 (`1a2b3c4` clean); WcbCmd = WcbCmd 0.9.1 (5 files in other line
    endings)', or which files differ."""
    parts = []
    for lib, r in libs.items():
        name = f"{r.get('repo_name', lib)} {r.get('version') or ''}".strip()
        if r.get("missing"):
            parts.append(f"{lib}: missing {', '.join(r['missing'])}")
            continue
        extra = tree_line(r.get("repo_state"))
        if r["same"]:
            eol = f", {len(r['eol_only'])} files in other line endings" if r["eol_only"] else ""
            parts.append(f"{lib} = {name} ({extra}{eol})")
        else:
            diff = r["differ"] + [f"{x} (sketchbook only)" for x in r["only_sketchbook"]] + \
                [f"{x} (repo only)" for x in r["only_repo"]]
            parts.append(f"{lib} DIFFERS from {name}: {', '.join(diff[:6])}{' ...' if len(diff) > 6 else ''}")
    return "; ".join(parts)


def fw_version_of(navicore):
    """FW_VERSION as NaviCore's fw_version.h composes it, or None."""
    try:
        with open(os.path.join(navicore, "fw_version.h"), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    base = re.search(r'#define\s+FW_VERSION_BASE\s+"([^"]+)"', text)
    dtg = re.search(r'#define\s+FW_VERSION_DTG\s+"([^"]+)"', text)
    return f"{base.group(1)}_{dtg.group(1)}" if base and dtg else None


def _hooks_in_source(navicore):
    for name in os.listdir(navicore):
        if name.lower().endswith((".ino", ".h", ".cpp")):
            with open(os.path.join(navicore, name), encoding="utf-8", errors="replace") as f:
                if "NAVICORE_HIL_HOOKS" in f.read():
                    return True
    return False


# What a compile reads of a sketch folder: its top-level code files, partitions.csv, bootloader.bin and build_opt.h
# (the esp32 3.3.4 platform.txt prebuild hooks 1, 4 and 5 copy them from {build.source.path}), sketch.yaml, and src/.
SKETCH_EXT = (".ino", ".pde", ".h", ".hh", ".hpp", ".c", ".cc", ".cpp", ".cxx", ".s")
SKETCH_FILES = ("partitions.csv", "bootloader.bin", "sketch.yaml", "sketch.yml")


def stage_sketch(source, parent):
    """Copy what a compile reads of the NaviCore sketch at `source` into <parent>/NaviCore -> that folder. arduino-cli
    refuses a sketch folder not named after its main .ino ('main file missing from sketch: <folder>/hil-week.ino' for
    a worktree at .../hil-week), so a git worktree of NaviCore, whose folder carries the worktree's name, is compiled
    from this copy. The top-level files SKETCH_EXT and SKETCH_FILES name, and src/ whole; nothing else of the repo."""
    dest = os.path.join(parent, "NaviCore")
    os.makedirs(dest)
    for name in sorted(os.listdir(source)):
        full = os.path.join(source, name)
        if os.path.isfile(full) and (name.lower().endswith(SKETCH_EXT) or name.lower() in SKETCH_FILES):
            shutil.copy2(full, os.path.join(dest, name))
    if os.path.isdir(os.path.join(source, "src")):
        shutil.copytree(os.path.join(source, "src"), os.path.join(dest, "src"))
    return dest


# ---------------------------------------------------------------------------- build
class _BuildLock(RunLock):
    """One compile at a time: two share arduino-cli's core cache (%LOCALAPPDATA%/arduino) and corrupt it. Not run.lock:
    the GUI reads a held run.lock in any results folder as a live run."""

    def __init__(self, path):
        self.path, self.fd = path, None


def _run_cli(argv, low_priority=True, timeout_s=3600):
    """Run arduino-cli -> (returncode, stdout, stderr, seconds). low_priority: the Windows BELOW_NORMAL priority class,
    which the compiler processes it starts inherit (a child takes a below-normal parent's class), so a build beside a
    HIL run leaves the harness's USB threads ahead of it; elsewhere `nice`."""
    kw = {}
    if low_priority and os.name == "nt":
        kw["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS
    elif low_priority and shutil.which("nice"):
        argv = ["nice", "-n", "10"] + list(argv)
    t0 = time.monotonic()
    r = subprocess.run(argv, capture_output=True, timeout=timeout_s, **kw)
    return (r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace"),
            time.monotonic() - t0)


def compile_argv(out_dir, sketch_dir, hooks=False, jobs=None, cli=None):
    """The compile command. Never -u/--upload and never -p: this only builds."""
    argv = [cli or cli_path(), "compile", "--json", "--fqbn", FQBN, "--build-path", out_dir]
    if jobs:
        argv += ["--jobs", str(jobs)]
    if hooks:
        argv += ["--build-property", HOOKS_PROPERTY]
    return argv + [sketch_dir]


def _core_version(cli):
    """The installed esp32 core's version, from `arduino-cli core list --json`; None when it cannot tell."""
    try:
        r = subprocess.run([cli, "core", "list", "--json"], capture_output=True, timeout=60)
        doc = json.loads(r.stdout.decode("utf-8", "replace"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    rows = doc.get("platforms", []) if isinstance(doc, dict) else doc
    for p in rows if isinstance(rows, list) else []:
        pid = p.get("id") or (p.get("metadata") or {}).get("id")
        if pid == CORE_ID:
            return p.get("installed_version") or p.get("installed")
    return None


def _error_lines(text, n=12):
    """The compiler's 'error' lines only, each cut to 200 characters: never the source lines gcc quotes under them,
    which could hold a compile-time default such as the mesh password."""
    rows = [x.strip()[:200] for x in text.splitlines() if re.search(r"\berror\b", x, re.I)]
    return rows[:n] or [x.strip()[:200] for x in text.strip().splitlines()[-3:]]


def build(tag, hooks=False, *, builds_root=None, allow_drift=False, rebuild=False, jobs=None, low_priority=True,
          timeout_s=3600, github=None, sketchbook=None, source=None):
    """Compile NaviCore's working tree into <builds_root>/navicore-<tag> (the build path is the folder itself, so the
    IDE's sketch cache is never touched) and return its manifest, also written there as BUILD.json beside the
    compiler's output (compile.log). hooks=True adds -DNAVICORE_HIL_HOOKS=1 (INF9). `source`: the NaviCore tree to
    compile, a checkout or a git worktree of one (default <github>/NaviCore); a folder not named NaviCore is compiled
    from a copy of its sketch files (stage_sketch; 'staged' in the manifest), while every check and the recorded tree
    state read the source itself. Before compiling: the FQBN must still be the one the source's CLAUDE.md names, the
    esp32 core must be 3.3.4, and the sketchbook's WCB_Client and WcbCmd must equal the WCBClient and WcbCmd repos
    (line endings aside); BuildError names what differs, unless allow_drift=True, which records it instead. The
    manifest records NaviCore's commit, whether its tree is dirty (and a fingerprint of the change), both libraries,
    and the image check. BuildError also when the tree changed during the compile, or when the folder already holds a
    build (rebuild=True recompiles it in place). One build at a time, at below-normal priority."""
    if not TAG.fullmatch(tag or ""):
        raise ValueError(f"tag {tag!r}: letters, digits, '_', '.', '-', at most 40, not starting with a symbol")
    github = github or github_root()
    navicore = os.path.normpath(os.path.abspath(source)) if source else os.path.join(github, "NaviCore")
    if not os.path.isfile(os.path.join(navicore, SKETCH)):
        raise BuildError(f"no {SKETCH} in {navicore}")
    try:
        with open(os.path.join(navicore, "CLAUDE.md"), encoding="utf-8", errors="replace") as f:
            claude_md = f.read()
    except OSError:
        claude_md = ""
    if FQBN not in claude_md:
        raise BuildError(f"NaviCore/CLAUDE.md does not name {FQBN}: its FQBN changed, so hil/ncflash.py FQBN must "
                         f"follow it before anything is built")
    builds_root = builds_root or BUILDS
    out = os.path.join(builds_root, f"navicore-{tag}")
    if os.path.exists(os.path.join(out, MANIFEST)) and not rebuild:
        raise BuildError(f"{out} already holds a build ({MANIFEST}); pick another tag or pass rebuild=True")
    cli = cli_path()
    libs = check_libs(sketchbook, github)
    drift = [lib for lib, r in libs.items() if not r["same"]]
    if drift and not allow_drift:
        raise BuildError(f"the sketchbook's copies differ from the repos CI builds against, so this build would not be "
                         f"what it claims: {libs_line(libs)}. Sync them (NaviCore BUILD_AND_RELEASE.md §3), or pass "
                         f"allow_drift=True to build and record it")
    core = _core_version(cli)
    if core is not None and core != CORE_VERSION:
        raise BuildError(f"the installed {CORE_ID} core is {core}; NaviCore is pinned to {CORE_VERSION}")
    os.makedirs(out, exist_ok=True)
    if os.path.exists(os.path.join(out, MANIFEST)):
        os.remove(os.path.join(out, MANIFEST))          # rebuild: the old record would contradict the new image
    jobs = jobs or max(1, (os.cpu_count() or 2) // 2)
    lock = _BuildLock(os.path.join(builds_root, ".ncflash-build.lock"))
    try:
        lock.acquire(wait_s=1.0)
    except RunBusy:
        raise BuildError("another NaviCore build is running (results/builds/.ncflash-build.lock)") from None
    stage = None
    try:
        before = tree_state(navicore)
        sketch_dir = navicore
        if os.path.basename(navicore) != "NaviCore":
            stage = tempfile.mkdtemp(prefix="ncflash-src-")
            sketch_dir = stage_sketch(navicore, stage)
        argv = compile_argv(out, sketch_dir, hooks, jobs, cli)
        started = datetime.now().astimezone()
        try:
            rc, stdout, stderr, secs = _run_cli(argv, low_priority, timeout_s)
        except subprocess.TimeoutExpired:
            raise BuildError(f"the compile of {tag} ran past {timeout_s} s and was stopped") from None
        after = tree_state(navicore)
    finally:
        if stage:
            shutil.rmtree(stage, ignore_errors=True)    # a plain copy (stage_sketch), never a link to the source
        lock.release()
    try:
        doc = json.loads(stdout)
    except ValueError:
        doc = {}
    compiler_out = doc.get("compiler_out", "" if doc else stdout) if isinstance(doc, dict) else stdout
    compiler_err = (doc.get("compiler_err", "") if isinstance(doc, dict) else "") + stderr
    atomic_write_text(os.path.join(out, COMPILE_LOG),
                      f"$ {' '.join(argv)}\n# exit {rc}, {secs:.0f} s\n\n{compiler_out}\n{compiler_err}\n")
    if rc != 0 or (isinstance(doc, dict) and doc.get("success") is False):
        raise BuildError(f"compile of {tag} failed (exit {rc}); see {os.path.join(out, COMPILE_LOG)}:\n    "
                         + "\n    ".join(_error_lines(compiler_err or compiler_out)))
    if (before["head"], before["diff"]) != (after["head"], after["diff"]):
        raise BuildError(f"NaviCore's tree changed during the compile ({tree_line(before)} -> {tree_line(after)}): "
                         f"what went into the image is unknown; build again")
    result = doc.get("builder_result", {}) if isinstance(doc, dict) else {}
    props = result.get("build_properties") or []
    warnings = []
    if props and hooks != (HOOKS_PROPERTY in props):
        raise BuildError(f"the build's properties {'lack' if hooks else 'carry'} {HOOKS_PROPERTY}")
    hooks_src = _hooks_in_source(navicore)
    if hooks and not hooks_src:
        warnings.append("hooks requested, but NaviCore's source has no NAVICORE_HIL_HOOKS yet (INF9): the define "
                        "changes nothing")
    platform = result.get("build_platform") or {}
    if platform.get("version") and platform.get("version") != CORE_VERSION:
        raise BuildError(f"arduino-cli built with {platform.get('id')} {platform.get('version')}, not {CORE_VERSION}")
    used = [{"name": u.get("name"), "version": u.get("version"), "dir": u.get("install_dir")}
            for u in result.get("used_libraries") or []]
    for u in used:
        lib = libs.get(u["name"])
        if lib and u["dir"] and os.path.normcase(os.path.normpath(u["dir"])) != \
                os.path.normcase(os.path.normpath(lib["sketchbook"])):
            raise BuildError(f"the compile took {u['name']} from {u['dir']}, not the sketchbook copy that was checked "
                             f"({lib['sketchbook']})")
    info = check_image(out)
    info.pop("data", None)
    source_version = fw_version_of(navicore)
    if source_version and info["version"] != source_version:
        raise BuildError(f"the image carries {info['version']}; NaviCore's fw_version.h says {source_version}")
    manifest = {
        "tag": tag, "folder": out, "built": started.isoformat(timespec="seconds"), "secs": round(secs),
        "fqbn": FQBN, "hooks": bool(hooks), "hooks_in_source": hooks_src, "core": f"{CORE_ID}@{CORE_VERSION}",
        "arduino_cli": cli, "jobs": jobs, "navicore": before, "staged": bool(stage),
        "libraries": {lib: {k: r.get(k) for k in ("same", "eol_only", "differ", "only_sketchbook", "only_repo",
                                                  "missing", "sketchbook", "repo", "version", "repo_state")}
                      for lib, r in libs.items()},
        "libraries_line": libs_line(libs), "drift_allowed": bool(drift), "used_libraries": used,
        "image": {k: info[k] for k in ("size", "elf_sha", "version", "slot", "slot_source", "segments")},
        "warnings": warnings,
    }
    atomic_write_text(os.path.join(out, MANIFEST), json.dumps(manifest, indent=2) + "\n")
    return manifest


def build_line(manifest):
    """What is in a build, one line, for FLASHED.md."""
    if not manifest:
        return f"no {MANIFEST} (not built by hil/ncflash.py)"
    bits = [f"NaviCore {tree_line(manifest.get('navicore'))}", manifest.get("libraries_line", "")]
    bits.append("NAVICORE_HIL_HOOKS" + ("" if manifest.get("hooks_in_source") else " (no hooks in the source)")
                if manifest.get("hooks") else "no hooks")
    if manifest.get("drift_allowed"):
        bits.append("built with library drift")
    return "; ".join(b for b in bits if b)


def read_manifest(folder):
    try:
        with open(os.path.join(folder, MANIFEST), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------- the image
def _num(text):
    text = text.strip()
    mult = 1
    if text[-1:] in ("K", "k"):
        text, mult = text[:-1], 1024
    elif text[-1:] in ("M", "m"):
        text, mult = text[:-1], 1024 * 1024
    return int(text, 0) * mult if text else None


def partitions(text):
    """A partitions.csv -> {name: {'type', 'subtype', 'offset', 'size'}} (offsets and sizes as ints; K/M suffixes)."""
    out = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("#", 1)[0].split(",")]
        if len(cells) < 5 or not cells[0]:
            continue
        out[cells[0]] = {"type": cells[1], "subtype": cells[2], "offset": _num(cells[3]), "size": _num(cells[4])}
    return out


def _partition_csv(folder):
    """(text, where) of the partitions.csv for an image: the build folder's copy, else NaviCore's own."""
    for p in (os.path.join(folder, "partitions.csv"), os.path.join(github_root(), "NaviCore", "partitions.csv")):
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read(), p
    return None, None


def ota_slot(parts):
    """The smaller OTA app slot, in bytes (both app0 and app1 must hold the image: OTA alternates)."""
    sizes = [p["size"] for p in parts.values() if p["type"] == "app" and p["subtype"] in ("ota_0", "ota_1")]
    return min(sizes) if sizes else None


def parse_image(data):
    """(info, problems) for an ESP32-S3 app image, checked the way the board will: the magic esp_ota_write needs
    first, the chip id, every segment inside the file, the checksum byte and the appended SHA-256 esp_ota_end verifies
    before it moves the boot pointer, and nothing after them; then the app descriptor at 0x20 (so 0xB0 really is its
    app_elf_sha256), a non-zero ELF SHA-256, and exactly one FW_VERSION string."""
    info = {"size": len(data), "chip_id": None, "segments": 0, "elf_sha": None, "version": None, "versions": []}
    problems = []
    if len(data) < ELF_SHA_AT + 32:
        return info, [f"{len(data)} bytes is too short for an app image"]
    if data[0] != IMAGE_MAGIC:
        problems.append(f"magic byte 0x{data[0]:02X}, not 0xE9: esp_ota_write refuses it")
    nseg = data[1]
    info["chip_id"] = struct.unpack_from("<H", data, 12)[0]
    if info["chip_id"] != CHIP_ID_ESP32S3:
        problems.append(f"chip id {info['chip_id']}, not {CHIP_ID_ESP32S3} (ESP32-S3)")
    hash_appended = data[23] == 1
    if not 1 <= nseg <= 16:
        problems.append(f"{nseg} segments in the header")
    else:
        off, csum = 24, 0xEF
        for i in range(nseg):
            if off + 8 > len(data):
                problems.append(f"segment {i}'s header runs past the end of the file")
                break
            _, length = struct.unpack_from("<II", data, off)
            if off + 8 + length > len(data):
                problems.append(f"segment {i} ({length} B at 0x{off:X}) runs past the end of the file")
                break
            for b in data[off + 8:off + 8 + length]:
                csum ^= b
            off += 8 + length
        else:
            info["segments"] = nseg
            at = off + 15 - off % 16                        # the checksum byte ends a 16-byte block
            end = at + 1 + (32 if hash_appended else 0)
            if at >= len(data):
                problems.append("the file ends before the checksum byte")
            elif data[at] != csum:
                problems.append(f"checksum byte 0x{data[at]:02X}, computed 0x{csum:02X}: the image is corrupt")
            elif hash_appended and data[at + 1:end] != hashlib.sha256(data[:at + 1]).digest():
                problems.append("the appended SHA-256 does not match the image: it is corrupt")
            elif end != len(data):
                problems.append(f"{len(data) - end:+d} bytes after the image's end (0x{end:X})")
            if not hash_appended:
                problems.append("no SHA-256 appended (hash_appended is 0)")
    if struct.unpack_from("<I", data, APP_DESC_AT)[0] != APP_DESC_MAGIC:
        problems.append("no app descriptor (magic 0xABCD5432) at 0x20, so 0xB0 is not the ELF SHA-256")
    sha = data[ELF_SHA_AT:ELF_SHA_AT + 32]
    info["elf_sha"] = sha.hex()
    if not any(sha):
        problems.append("the ELF SHA-256 at 0xB0 is all zero: elf2image did not stamp it (--elf-sha256-offset 0xb0)")
    versions = sorted({v.decode() for v in VERSION_B.findall(data)})
    info["versions"] = versions
    if len(versions) == 1:
        info["version"] = versions[0]
    else:
        problems.append("no FW_VERSION string" if not versions else f"{len(versions)} version strings: {versions}")
    return info, problems


def _image_paths(path):
    """(image, elf or None, folder) for a build folder or an image file."""
    image = os.path.normpath(os.path.abspath(os.path.join(path, IMAGE) if os.path.isdir(path) else path))
    folder = os.path.dirname(image)
    stem = image[:-4] if image.endswith(".bin") else image
    elf = stem + ".elf"
    return image, (elf if os.path.isfile(elf) else None), folder


def check_image(path, slot=None):
    """A build folder (or its NaviCore.ino.bin) -> info {'path', 'folder', 'size', 'chip_id', 'segments', 'elf_sha',
    'elf', 'elf_checked', 'version', 'slot', 'slot_source', 'manifest', 'data'}; ImageError listing every problem.
    Beyond parse_image: the ELF beside the image must hash to the SHA-256 at 0xB0 (the ELF that decodes this image's
    crashes), the image must fit the OTA slot (partitions.csv's smaller app slot, or `slot`), and a BUILD.json in the
    folder must describe this image."""
    image, elf, folder = _image_paths(path)
    try:
        with open(image, "rb") as f:
            data = f.read()
    except OSError as e:
        raise ImageError(f"{image}: {e.strerror or e}") from None
    info, problems = parse_image(data)
    info.update(path=image, folder=folder, elf=elf, elf_checked=False, data=data)
    if elf:
        h = hashlib.sha256()
        with open(elf, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        info["elf_checked"] = True
        if info["elf_sha"] and h.hexdigest() != info["elf_sha"]:
            problems.append(f"{os.path.basename(elf)} hashes to {h.hexdigest()[:16]}, the image names "
                            f"{info['elf_sha'][:16]}: they are not one build")
    if slot is None:
        text, where = _partition_csv(folder)
        slot = ota_slot(partitions(text)) if text else None
        info["slot_source"] = where
    else:
        info["slot_source"] = "given"
    info["slot"] = slot
    if slot is None:
        problems.append("no partitions.csv to size the OTA slot against")
    elif len(data) > slot:
        problems.append(f"{len(data)} B does not fit the {slot} B OTA slot")
    manifest = read_manifest(folder)
    info["manifest"] = manifest
    if manifest:
        want = manifest.get("image", {})
        if want.get("elf_sha") and want.get("elf_sha") != info["elf_sha"]:
            problems.append(f"{MANIFEST} records ELF {str(want.get('elf_sha'))[:16]}; the image carries "
                            f"{str(info['elf_sha'])[:16]}")
    if problems:
        raise ImageError(f"{image}: " + "; ".join(problems))
    return info


def image_sha16(path):
    """The first 16 hex digits of the ELF SHA-256 an image (a build folder or its .bin) carries at 0xB0, which is what
    the board prints as 'App SHA256:' (navicore_ota.h otaAppSha16: 8 bytes of esp_app_desc_t.app_elf_sha256), or None
    when there is no such file or it is too short. Reads 32 bytes; checks nothing else (check_image does)."""
    image, _, _ = _image_paths(path)
    try:
        with open(image, "rb") as f:
            f.seek(ELF_SHA_AT)
            sha = f.read(32)
    except OSError:
        return None
    return sha[:8].hex() if len(sha) == 32 else None


def builds_with_sha(app_sha, builds_root=None):
    """The build folders under builds_root (default results/builds) whose NaviCore.ino.bin carries an ELF SHA-256 that
    starts with `app_sha` (the board's 8 to 64 hex digits, any case), sorted by name. Two folders can match: a copy of
    one build. [] when the board prints no SHA or none matches."""
    root = builds_root or BUILDS
    want = (app_sha or "").lower()
    if not re.fullmatch(r"[0-9a-f]{8,64}", want):
        return []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for name in names:
        folder = os.path.join(root, name)
        if not os.path.isfile(os.path.join(folder, IMAGE)):
            continue
        try:
            with open(os.path.join(folder, IMAGE), "rb") as f:
                f.seek(ELF_SHA_AT)
                sha = f.read(32).hex()
        except OSError:
            continue
        if len(sha) == 64 and sha.startswith(want[:64]):
            out.append(folder)
    return out


# ---------------------------------------------------------------------------- the board
def parse_ota_status(lines):
    """The ?OTALOCAL,STATUS block (navicore_ota.h otaPrintStatus :226-238) -> {'chip', 'family', 'firmware', 'running',
    'next', 'active', 'session', 'app_sha'}. running/next: {'label', 'addr', 'size'} (next None: no spare slot);
    active: False for 'Session: idle', True for 'ACTIVE' with session {'id', 'written', 'size'}; app_sha: the hex of an
    'App SHA256:' line (INF9 a), else None. The last block in `lines` counts:
        ---------- OTA Status ----------
        Chip:        ESP32-S3 (family 1)
        Firmware:    v0.2.0_102105QSEP26
        Running:     'app0' @0x010000 (1966080 B)
        Next (OTA):  'app1' @0x1f0000 (1966080 B)
        Session:     idle                        or   Session:     ACTIVE id=1  4096 / 1170096 B
        --------------------------------"""
    start = max((i for i, x in enumerate(lines) if "---------- OTA Status ----------" in x), default=0)
    out = {"chip": None, "family": None, "firmware": None, "running": None, "next": None, "active": None,
           "session": None, "app_sha": None}

    def slot(m):
        return {"label": m.group(1), "addr": int(m.group(2), 16), "size": int(m.group(3))}
    for x in lines[start:]:
        m = re.search(r"Chip:\s+(.*?) \(family (\d+)\)", x)
        if m:
            out["chip"], out["family"] = m.group(1), int(m.group(2))
        m = re.search(r"Firmware:\s+(\S+)", x)
        if m:
            out["firmware"] = m.group(1)
        m = re.search(r"Running:\s+'([^']*)' @0x([0-9A-Fa-f]+) \((\d+) B\)", x)
        if m:
            out["running"] = slot(m)
        m = re.search(r"Next \(OTA\):\s+'([^']*)' @0x([0-9A-Fa-f]+) \((\d+) B\)", x)
        if m:
            out["next"] = slot(m)
        m = re.search(r"Session:\s+ACTIVE id=(\d+)\s+(\d+) / (\d+) B", x)
        if m:
            out["active"] = True
            out["session"] = {"id": int(m.group(1)), "written": int(m.group(2)), "size": int(m.group(3))}
        elif re.search(r"Session:\s+idle", x):
            out["active"], out["session"] = False, None
        m = re.search(r"App SHA256:\s+([0-9A-Fa-f]{8,64})", x)
        if m:
            out["app_sha"] = m.group(1).lower()
        if x.startswith("--------------------------------") and out["running"]:
            break
    return out


def ota_status(nc, timeout=4.0, tries=2):
    """?OTALOCAL,STATUS -> parse_ota_status(), with the '#L12' flush after it (NaviCore.cli). AssertionError when no
    whole block (Running and Session) arrives in `tries`."""
    for _ in range(tries):
        try:
            st = parse_ota_status(nc.cli("?OTALOCAL,STATUS", timeout=timeout))
        except AssertionError:
            continue
        if st["running"] and st["active"] is not None:
            return st
    raise AssertionError(f"{nc.dev.name}: ?OTALOCAL,STATUS printed no whole status block in {tries} tries")


def _slot_name(s):
    return f"'{s['label']}' @0x{s['addr']:06x}" if s else "none"


def _nudge(dev, count=None):
    """A lone newline: nothing on NaviCore, but a host packet, which releases output HWCDC is holding."""
    if getattr(dev, "connected", True) is False:
        return
    try:
        dev.send("")
    except AssertionError:
        return
    if count is not None:
        count["nudged"] = count.get("nudged", 0) + 1


def _await(dev, since, rx, timeout, nudge_s=0.0, stop=None, count=None):
    """The first match of rx in the lines after mark `since`, or None after `timeout` s; a line matching `stop` ends
    the wait early (None). A newline every nudge_s while it waits (counted in count['nudged'])."""
    deadline = time.monotonic() + timeout
    next_nudge = time.monotonic() + nudge_s if nudge_s else None
    i = since
    while True:
        for x in dev.since(i):
            i += 1
            m = rx.search(x)
            if m:
                return m
            if stop is not None and stop.search(x):
                return None
        now = time.monotonic()
        if now >= deadline:
            return None
        if next_nudge is not None and now >= next_nudge:
            _nudge(dev, count)
            next_nudge = now + nudge_s
        time.sleep(0.003)


def _last(dev, since, rx):
    """The last match of rx after mark `since`, or None."""
    hits = [m for x in dev.since(since) for m in [rx.search(x)] if m]
    return hits[-1] if hits else None


def _first(dev, since, rx):
    """The first match of rx after mark `since`, or None."""
    return next((m for x in dev.since(since) for m in [rx.search(x)] if m), None)


@contextmanager
def _quiet_data(dev):
    """session.log gets '?OTALOCAL,DATA,<off>,<1368 base64 chars>' instead of 1.6 MB of base64, and an empty send is
    labelled; every received line is logged as always."""
    log = getattr(dev, "log", None)
    if not log:
        yield
        return

    def filtered(name, direction, text):
        if direction == ">" and text.startswith("?OTALOCAL,DATA,"):
            head, _, b64 = text.rpartition(",")
            text = f"{head},<{len(b64)} base64 chars>"
        elif direction == ">" and text == "":
            text = "(empty line: output nudge)"
        log(name, direction, text)
    dev.log = filtered
    try:
        yield
    finally:
        dev.log = log


def ota_begin(nc, size, family=FAMILY_ESP32S3, timeout=40.0, nudge_s=1.0, count=None):
    """?OTALOCAL,BEGIN -> the mark taken before it. START comes before the erase and OK or ERR after it
    (navicore_ota.h:257-266); each gets `timeout` (the config tool allows 40 s, a cold erase of a full slot being
    slow). FlashError('begin') on ERR, with the board's reason, or on silence."""
    dev = nc.dev
    m = dev.mark()
    dev.send(f"?OTALOCAL,BEGIN,{size},{family}")
    got = _await(dev, m, M_BEGIN, timeout, nudge_s, count=count)
    if got and got.group(1) == "START":
        got = _await(dev, m, M_BEGIN_DONE, timeout, nudge_s, count=count)
    if got is None:
        raise FlashError("begin", 0, f"no [OTA:BEGIN,OK/ERR] within {timeout:g} s of BEGIN")
    if got.group(1) == "ERR":
        why = _last(dev, m, re.compile(r"\[OTA\] ((?:BEGIN rejected|esp_ota_begin failed).*)"))
        raise FlashError("begin", 0, f"BEGIN refused: {why.group(1) if why else '[OTA:BEGIN,ERR]'}")
    return m


def ota_abort(nc):
    """?OTALOCAL,ABORT: ends a session without switching anything; silent when none is active."""
    nc.dev.send("?OTALOCAL,ABORT")


def ota_stream(nc, data, *, family=FAMILY_ESP32S3, chunk_timeout=3.0, begin_timeout=40.0, end_timeout=20.0,
               nudge_s=0.25, max_fails=6, progress=None, state=None):
    """BEGIN, every chunk, END (module docstring) -> stats {'chunks', 'naks', 'resyncs', 'nudged', 'end',
    'end_mark', 'begin_mark', 'secs'}. 'end' is 'OK', or None when no verdict came but the board restarted (then only
    the slot it comes back on tells). FlashError at the first thing that goes wrong; `state` (a dict) is kept current
    with 'stage', 'offset' and 'began', so a caller can clean up. The per-chunk wait and max_fails keep the longest
    stall (max_fails x (chunk_timeout + a STATUS)) under the board's 30 s idle reaper."""
    if max_fails * chunk_timeout >= IDLE_S:
        raise ValueError(f"max_fails x chunk_timeout ({max_fails} x {chunk_timeout:g} s) would outlast the board's "
                         f"{IDLE_S} s idle reaper, which ends the session")
    dev = nc.dev
    size = len(data)
    state = state if state is not None else {}
    state.update(stage="begin", offset=0, began=True)
    t0 = time.monotonic()
    stats = {"chunks": 0, "naks": 0, "resyncs": 0, "nudged": 0, "end": None}
    with _quiet_data(dev):
        try:
            _stream(nc, data, stats, state, chunk_timeout, begin_timeout, end_timeout, nudge_s, max_fails, progress,
                    family)
        except FlashError:
            raise
        except AssertionError as e:               # the port itself failed (SerialDevice.send's ExpectTimeout)
            raise FlashError(state["stage"], state["offset"], str(e).splitlines()[0][:200]) from None
    stats["secs"] = round(time.monotonic() - t0, 1)
    return stats


def _stream(nc, data, stats, state, chunk_timeout, begin_timeout, end_timeout, nudge_s, max_fails, progress, family):
    dev = nc.dev
    size = len(data)
    m0 = ota_begin(nc, size, family, begin_timeout, max(nudge_s, 1.0), count=stats)
    stats["begin_mark"] = m0
    state["stage"] = "data"
    pos, fails, step = 0, 0, max(size // 10, 1)
    next_report = step
    while pos < size:
        piece = data[pos:pos + CHUNK]
        end = pos + len(piece)
        m = dev.mark()
        nc.send_paced(f"?OTALOCAL,DATA,{pos},{base64.b64encode(piece).decode('ascii')}")
        stats["chunks"] += 1
        got = _await(dev, m, M_CURSOR, chunk_timeout, nudge_s, count=stats)
        if got and got.group(1) == "ACK" and int(got.group(2)) == end:
            pos, fails = end, 0
            state["offset"] = pos
            if pos >= next_report or pos == size:
                next_report = pos + step
                dev_note(dev, f"ncflash: {pos} / {size} B ({pos * 100 // size}%)")
                if progress:
                    progress(pos, size)
            continue
        fails += 1
        if fails > max_fails:
            raise FlashError("data", pos, f"no progress past {pos} of {size} B after {max_fails} tries")
        if got and got.group(1) == "NAK" and int(got.group(2)) == pos and pos > 0:
            stats["naks"] += 1              # the board is where we are: the line was damaged, nothing written
            continue
        stats["resyncs"] += 1
        try:
            st = ota_status(nc)
        except AssertionError:
            raise FlashError("data", pos, "?OTALOCAL,STATUS stopped answering during the transfer"
                             + ("; NaviCore restarted" if _last(dev, m0, RESTARTED) else "")) from None
        if not st["active"]:
            why = _last(dev, m0, M_ABORTED)
            raise FlashError("data", pos, "the board ended the session: " + (
                why.group(1) if why else "NaviCore restarted" if _last(dev, m0, RESTARTED) else "no reason printed"))
        session = st["session"]
        if session["size"] != size:
            raise FlashError("data", pos, f"the board's session is for {session['size']} B, not {size}")
        if session["written"] == end:
            pos, fails = end, 0                 # written; its marker was lost
            state["offset"] = pos
            continue
        if session["written"] != pos:
            raise FlashError("data", session["written"],
                             f"the board's write cursor is {session['written']}, but the chunk sent covered "
                             f"{pos}-{end}: a damaged line was written, so this image cannot be completed")
    state["stage"] = "end"
    m_end = dev.mark()
    stats["end_mark"] = m_end
    dev.send("?OTALOCAL,END")
    got = _await(dev, m_end, M_END, end_timeout, max(nudge_s, 0.5), stop=RESTARTED, count=stats)
    if got is None and not _last(dev, m_end, RESTARTED):
        # The END line or its answer was lost. A second END is safe: after a good one the board sits in its 2 s
        # restart delay and never reads it (navicore_ota.h:297-300); after a failed one it answers "no matching
        # active session" (:207).
        m_end2 = dev.mark()
        dev.send("?OTALOCAL,END")
        got = _await(dev, m_end2, M_END, end_timeout, max(nudge_s, 0.5), stop=RESTARTED, count=stats)
    if got is None and not _last(dev, m_end, RESTARTED):
        raise FlashError("end", size, f"no [OTA:END,OK/ERR] within {end_timeout:g} s, twice")
    if got is not None and got.group(1) == "ERR":
        why = _first(dev, m_end, re.compile(r"\[OTA\] (END .*)"))      # the first END's reason, not a resend's
        raise FlashError("end", size, f"END refused: {why.group(1) if why else '[OTA:END,ERR]'}")
    stats["end"] = got.group(1) if got else None
    state["stage"] = "boot"


def _ping(nc, tries=3):
    """PONG's version, or None after `tries` PINGs (a newline between them, in case a reply is held)."""
    for _ in range(tries):
        try:
            return nc.ping()
        except AssertionError:
            _nudge(nc.dev)
    return None


def _cell(text):
    return str(text).replace("|", "/").replace("\r", " ").replace("\n", " ").strip()


FLASH_LOG_HEADING = "## NaviCore flashes (hil/ncflash.py)"
FLASH_LOG_INTRO = ("Appended by `hil/ncflash.py` for every flash that reached the board: an `?OTALOCAL` transfer that "
                   "sent BEGIN, or the recovery ladder's esptool write. The ELF SHA-256 is the image's own identity "
                   "(its bytes at 0xB0); the version string changes only on a NaviCore commit.")
FLASH_LOG_HEAD = ("| When | Folder | ELF SHA-256 | NaviCore tree | How | Result | What is in it |\n"
                  "|---|---|---|---|---|---|---|")


def record_flash(builds_root, *, folder, elf_sha, tree, how, result, what):
    """One row in <builds_root>/FLASHED.md's 'NaviCore flashes' table, which is created at the end of the file the
    first time. The rest of the file, hand-kept, is left as it is."""
    builds_root = builds_root or BUILDS
    os.makedirs(builds_root, exist_ok=True)
    path = os.path.join(builds_root, FLASHED)
    try:
        rel = os.path.relpath(folder, builds_root)
        shown = f"`{rel.replace(os.sep, '/')}/`" if not rel.startswith("..") else f"`{folder}`"
    except ValueError:
        shown = f"`{folder}`"
    row = "| " + " | ".join(_cell(x) for x in (datetime.now().strftime("%Y-%m-%d %H:%M"), shown,
                                                 f"`{(elf_sha or '?')[:16]}`", tree, how, result, what)) + " |"
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = "# Bench images\n"
    lines = text.split("\n")
    if FLASH_LOG_HEADING in lines:
        i = lines.index(FLASH_LOG_HEADING)
        j = i
        for k in range(i + 1, len(lines)):
            if lines[k].startswith("|"):
                j = k
            elif j > i and lines[k].strip():
                break
        lines.insert(j + 1, row)
        text = "\n".join(lines)
    else:
        text = text.rstrip("\n") + f"\n\n{FLASH_LOG_HEADING}\n\n{FLASH_LOG_INTRO}\n\n{FLASH_LOG_HEAD}\n{row}\n"
    atomic_write_text(path, text)
    return row


def flash_rows(builds_root=None):
    """The rows record_flash wrote to <builds_root>/FLASHED.md, oldest first -> [{'when', 'folder', 'sha', 'tree', 'how',
    'result', 'what'}]: 'folder' without its backticks and trailing slash ('navicore-hil1'), 'sha' the 16 hex digits.
    [] when the file or the table is missing. A cell never holds a '|' (_cell), so splitting on it is exact."""
    path = os.path.join(builds_root or BUILDS, FLASHED)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
    except OSError:
        return []
    if FLASH_LOG_HEADING not in lines:
        return []
    out = []
    for line in lines[lines.index(FLASH_LOG_HEADING) + 1:]:
        if line.startswith("#"):
            break                                  # a later heading ends the table
        if not line.startswith("| ") or line.startswith(("| When ", "|---")):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 7:
            continue
        out.append({"when": cells[0], "folder": cells[1].strip("`").rstrip("/"), "sha": cells[2].strip("`"),
                    "tree": cells[3], "how": cells[4], "result": cells[5], "what": cells[6]})
    return out


def last_written(builds_root=None):
    """The newest FLASHED.md row after which the board runs the image it names -> the row (flash_rows) or None: an
    ?OTALOCAL flash that came back verified ('OK: ...', flash()), or the ladder's esptool write ('written (recovery)',
    recover()). A FAILED, VERIFY FAILED or NOT BACK row leaves the board on what ran before it, so it is passed over."""
    rows = [r for r in flash_rows(builds_root) if r["result"].startswith("OK") or r["result"] == "written (recovery)"]
    return rows[-1] if rows else None


def put_back(folder=None):
    """The commands, from tests/hil, that put NaviCore back on results/builds/<folder> (default BENCH_IMAGE), for a
    failure message that has to leave the bench to the next reader: status first, then the ?OTALOCAL flash, then the
    ladder with esptool."""
    folder = folder or BENCH_IMAGE
    return (f"from tests/hil, with no run holding the bench: `python -m hil.ncflash status`; if it does not show App "
            f"SHA256 {image_sha16(os.path.join(BUILDS, folder)) or '<the image at 0xB0>'}, `python -m hil.ncflash "
            f"flash results/builds/{folder} --what \"put back\"`; if NaviCore does not answer, `python -m "
            f"hil.ncflash recover --allow-esptool --known-good results/builds/{folder}`")


def _failure(nc, before, state, exc):
    """After a failed transfer: ABORT (harmless when idle), then STATUS -> the FlashError to raise, saying where it
    stopped and what runs now."""
    began = state.get("began")
    if began and state.get("stage") != "boot":
        try:
            ota_abort(nc)
        except AssertionError:
            pass
    try:
        st = ota_status(nc)
    except AssertionError:
        st = None
    stage, offset = exc.stage, exc.offset
    where = f"{stage.upper()}" + (f" at {offset} B" if stage in ("data", "end") else "")
    if st is None:
        after, intact = "STATUS did not answer afterwards: run ncflash.recover()", None
    elif before and st["running"] and st["running"]["addr"] == before["running"]["addr"]:
        after, intact = (f"the running app is intact: STATUS shows {_slot_name(st['running'])} running, session "
                         f"{'idle' if not st['active'] else 'still ACTIVE'}"), True
    else:
        after, intact = f"STATUS shows {_slot_name(st['running'] if st else None)} running", False
    return FlashError(stage, offset, f"NaviCore flash stopped at {where}: {exc}. {after}.", intact), st


def flash(nc, image, what="", *, builds_root=None, chunk_timeout=3.0, begin_timeout=40.0, end_timeout=20.0,
          boot_timeout=45.0, nudge_s=0.25, max_fails=6, progress=None):
    """Flash a build folder (or image) into NaviCore's inactive slot over ?OTALOCAL and see it run -> a result dict
    ('before', 'after' STATUS, 'version', 'boot' parse_boot of the restart, 'stats', 'secs', 'row').
    First check_image (ImageError: nothing sent), then PING (a NaviCore version, or nothing is sent) and STATUS
    (family 1, a Next slot the image fits, no other session streaming). Then ota_stream; after END,OK the board
    restarts: wait_boot (the banner, or the port reopening and a PONG), PING, and STATUS must show the old Next slot
    running, the image's version, and, when the board prints one (INF9 a), an App SHA256 that is the image's ELF
    SHA-256. FlashError on anything else; a failure before END leaves the running app, and the message says what
    STATUS showed afterwards. NotBack after END,OK when the board does not come back: recover(). Every attempt that sent
    BEGIN appends a row to FLASHED.md."""
    t0 = time.monotonic()
    info = check_image(image)
    data = info.pop("data")
    dev = nc.dev
    builds_root = builds_root or BUILDS
    manifest = info.get("manifest")
    row = dict(folder=info["folder"], elf_sha=info["elf_sha"], tree=tree_line((manifest or {}).get("navicore")),
               how="?OTALOCAL", what="; ".join(x for x in (what, build_line(manifest)) if x))
    pong = _ping(nc)
    if pong is None:
        raise FlashError("ping", 0, f"NaviCore on {dev.port} does not answer PING; nothing was sent. "
                         f"ncflash.recover() is the ladder")
    if not VERSION_S.fullmatch(pong):
        raise FlashError("ping", 0, f"the board on {dev.port} answers PONG {pong!r}, not a NaviCore version; nothing "
                         f"was sent")
    try:
        before = ota_status(nc)
    except AssertionError as e:
        raise FlashError("status", 0, f"{e}; nothing was sent") from None
    if before["family"] != FAMILY_ESP32S3:
        raise FlashError("status", 0, f"the board reports chip family {before['family']}, not {FAMILY_ESP32S3} "
                         f"(ESP32-S3); nothing was sent")
    if not before["next"]:
        raise FlashError("status", 0, "the board has no spare OTA slot (Next: none); nothing was sent")
    if len(data) > before["next"]["size"]:
        raise FlashError("status", 0, f"{len(data)} B does not fit the board's {_slot_name(before['next'])} "
                         f"({before['next']['size']} B); nothing was sent")
    if before["active"]:
        time.sleep(2.0)
        try:
            again = ota_status(nc)
        except AssertionError as e:
            raise FlashError("status", 0, f"{e}; nothing was sent") from None
        if again["active"] and again["session"]["written"] != before["session"]["written"]:
            raise FlashError("status", 0, "another OTA session is streaming into NaviCore (its write cursor moved "
                             "within 2 s); nothing was sent")
        dev_note(dev, f"ncflash: a stale session ({before['session']['written']} / {before['session']['size']} B) "
                 f"is superseded by BEGIN (navicore_ota.h:141)")
    dev_note(dev, f"ncflash: flashing {info['path']} ({len(data)} B, ELF {info['elf_sha'][:16]}, {info['version']}) "
             f"from {_slot_name(before['running'])} into {_slot_name(before['next'])}")
    state = {}
    try:
        stats = ota_stream(nc, data, chunk_timeout=chunk_timeout, begin_timeout=begin_timeout, end_timeout=end_timeout,
                           nudge_s=nudge_s, max_fails=max_fails, progress=progress, state=state)
    except FlashError as e:
        err, st = _failure(nc, before, state, e)
        rows = record_flash(builds_root, result=f"FAILED at {e.stage.upper()} {e.offset}/{len(data)} B: "
                            f"{str(e)[:160]}; " + ("running app intact" if err.intact else
                                                   "running app NOT confirmed"), **row) if state.get("began") else None
        err.row = rows
        raise err from None
    dev_note(dev, f"ncflash: END {'OK' if stats['end'] else '(no verdict; the board restarted)'} after "
             f"{stats['chunks']} chunks, {stats['naks']} NAK, {stats['resyncs']} STATUS resyncs")
    try:
        boot_lines = nc.wait_boot(since=stats["end_mark"], timeout=boot_timeout)
        version = nc.ping()
        after = ota_status(nc)
    except AssertionError as e:
        msg = (f"NaviCore took the image (END {'OK' if stats['end'] else 'unconfirmed'}, boot slot "
               f"{_slot_name(before['next'])}) but did not come back: {str(e)[:200]}. Run ncflash.recover(nc); its "
               f"esptool rung needs allow_esptool=True (known good: results/builds/{KNOWN_GOOD})")
        record_flash(builds_root, result=f"NOT BACK after END: {str(e)[:120]}", **row)
        raise NotBack("boot", len(data), msg, intact=False) from None
    boot = parse_boot(boot_lines)
    running = after["running"]
    problems = []
    if running["addr"] == before["running"]["addr"]:
        problems.append(f"it came back on the old slot {_slot_name(running)}: "
                        + ("the bootloader refused the new image" if stats["end"] else "END never took effect"))
    elif running["addr"] != before["next"]["addr"]:
        problems.append(f"it runs {_slot_name(running)}, neither the old slot nor the one written")
    for name, got in (("PONG", version), ("STATUS Firmware", after["firmware"])):
        if got != info["version"]:
            problems.append(f"{name} reports {got}, the image carries {info['version']}")
    app_sha = after["app_sha"] or next((m.group(1).lower() for x in boot_lines
                                        for m in [re.search(r"App SHA256:\s+([0-9A-Fa-f]{8,64})", x)] if m), None)
    if app_sha and not info["elf_sha"].startswith(app_sha):
        problems.append(f"the board's App SHA256 {app_sha} is not the image's {info['elf_sha'][:len(app_sha)]}")
    secs = round(time.monotonic() - t0, 1)
    if problems:
        record_flash(builds_root, result="VERIFY FAILED after the restart: " + "; ".join(problems)[:160], **row)
        raise FlashError("verify", len(data), "NaviCore restarted after END,OK but " + "; ".join(problems),
                         intact=running["addr"] == before["running"]["addr"])
    result_text = (f"OK: {_slot_name(before['running'])} -> {_slot_name(running)}, PONG {version}, {secs:.0f} s, "
                   f"{stats['chunks']} chunks, {stats['naks']} NAK, {stats['resyncs']} resync"
                   + ("" if app_sha else ", no App SHA256 line on the board"))
    written = record_flash(builds_root, result=result_text, **row)
    dev_note(dev, f"ncflash: {result_text}")
    return {"image": info["path"], "elf_sha": info["elf_sha"], "version": version, "before": before, "after": after,
            "boot": boot, "stats": stats, "secs": secs, "app_sha": app_sha, "row": written}


# ---------------------------------------------------------------------------- recovery
def kick_argv(port):
    """esptool, read-only: connect only if the chip already sits in the ROM bootloader (--before no-reset), read its
    id, and leave by the RTC watchdog, which boots the app without the DTR/RTS lines. Exit 0 means it WAS in download
    mode. Greg's flasher does exactly this after every S3 flash (ESP-Flasher-Companion esp_flasher_companion.py
    _exit_download_mode :918-953)."""
    return ["--chip", "esp32s3", "-p", port, "--before", "no-reset", "--after", "watchdog-reset",
            "--connect-attempts", "2", "chip-id"]


def write_argv(port, app, otadata):
    """esptool: the known-good app into app0 and boot_app0.bin into otadata, one connection. --after watchdog-reset,
    not hard-reset: on these native-USB S3 devkits the DTR/RTS reset after esptool can land back in download mode
    (esp_flasher_companion.py :1005-1008). The flash parameters are kept, as the Arduino upload keeps them
    (platform.txt:297)."""
    return ["--chip", "esp32s3", "-p", port, "--before", "usb-reset", "--after", "watchdog-reset", "write-flash", "-z",
            "--flash-mode", "keep", "--flash-freq", "keep", "--flash-size", "keep",
            f"0x{APP0_AT:x}", app, f"0x{OTADATA_AT:x}", otadata]


def guard_writes(argv):
    """ValueError unless argv writes only app0 (0x10000) and otadata (0xe000) and erases nothing: the bootloader at 0x0,
    the partition table at 0x8000, NVS, the config and the clips stay as they are."""
    lowered = [a.lower() for a in argv]
    for bad in ("erase-flash", "erase_flash", "erase-region", "erase_region", "--erase-all", "-e", "write-mem"):
        if bad in lowered:
            raise ValueError(f"esptool argv carries {bad}: the recovery never erases")
    if "write-flash" not in lowered:
        return argv
    rest = argv[lowered.index("write-flash") + 1:]
    addrs, i = [], 0
    while i < len(rest):
        a = rest[i]
        if a.startswith("-"):
            i += 1 if a in ("-z", "--compress", "-u", "--no-compress", "--no-progress", "--verify") else 2
            continue
        addrs.append(int(a, 0))
        i += 2
    if not addrs or any(x not in ALLOWED_WRITES for x in addrs):
        raise ValueError(f"esptool would write {[hex(x) for x in addrs]}; the recovery writes only "
                         f"{sorted(hex(x) for x in ALLOWED_WRITES)}")
    return argv


def check_otadata(data):
    """ValueError unless `data` is an 8 KB otadata from which the bootloader picks app0, the way the core's
    boot_app0.bin does. One entry per 4 KB sector: ota_seq at +0, ota_state at +24, crc at +28. An entry counts when its
    sequence is not 0xFFFFFFFF, its state is not INVALID (3) or ABORTED (4), and its CRC is esp_rom_crc32_le(UINT32_MAX)
    over the sequence, which is zlib.crc32(seq, 0xFFFFFFFF); the counted entry with the higher sequence wins and boots
    slot (seq - 1) % 2 (IDF bootloader_common_get_active_otadata). boot_app0.bin: sequence 1 in the first sector and 0 in
    the second, whose CRC 0xFFFFFFFF is right for four zero bytes, so it counts and loses."""
    if len(data) != OTADATA_LEN:
        raise ValueError(f"otadata image is {len(data)} B, not {OTADATA_LEN}")
    counted = []
    for off in (0, OTADATA_LEN // 2):
        seq, state, crc = (struct.unpack_from("<I", data, off + k)[0] for k in (0, 24, 28))
        if seq != 0xFFFFFFFF and state not in (3, 4) and crc == zlib.crc32(data[off:off + 4], 0xFFFFFFFF):
            counted.append(seq)
    if not counted or max(counted) < 1 or (max(counted) - 1) % 2 != 0:
        raise ValueError(f"otadata image does not select app0 (sequences counted: {counted})")


def check_layout(text):
    """ValueError unless the partition table puts app0 at 0x10000 and otadata at 0xe000 (0x2000): the addresses the
    recovery writes."""
    parts = partitions(text)
    app0 = next((p for p in parts.values() if p["type"] == "app" and p["subtype"] == "ota_0"), None)
    ota = next((p for p in parts.values() if p["type"] == "data" and p["subtype"] == "ota"), None)
    if not app0 or app0["offset"] != APP0_AT or not ota or (ota["offset"], ota["size"]) != (OTADATA_AT, OTADATA_LEN):
        raise ValueError("NaviCore's partitions.csv no longer has app0 at 0x10000 and otadata at 0xe000/0x2000")


def run_esptool(argv, timeout=240):
    """The core's esptool with argv -> (returncode, output)."""
    try:
        r = subprocess.run([esptool_path()] + list(argv), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"esptool timed out after {timeout} s"
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def _command_line(argv):
    return " ".join(f'"{a}"' if " " in a else a for a in ["esptool"] + list(argv))


def _release_port(dev):
    dev.close()


def _reclaim_port(dev, timeout=25.0):
    """Reopen the device after esptool (the USB re-enumerates as the chip resets) -> True when open."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            dev.open()
            return True
        except (serial.SerialException, OSError):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)


def _wait_alive(nc, since, timeout):
    """PONG's version once NaviCore answers, None at the timeout or on the ROM's download-mode banner."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(DOWNLOAD_MODE in x for x in nc.dev.since(since)):
            return None
        try:
            return nc.ping()
        except AssertionError:
            time.sleep(0.5)
    return None


def recover(nc, known_good=None, allow_esptool=False, *, builds_root=None, esptool=None, boot_timeout=30.0,
            ping_tries=3, what="recovery"):
    """The INF4 ladder, each rung only when the one before did not bring NaviCore back -> {'rung', 'version',
    'tried', 'status'}:
      1. 'ping': PING, `ping_tries` times.
      2. 'hard_reset': the USB-Serial/JTAG reset (NaviCore.hard_reset), then wait_boot and PING.
      3. 'kick' (esptool): if the chip sits in ROM download mode, leave it by the RTC watchdog (kick_argv); nothing is
         written.
      4. 'esptool': write the known-good app (default results/builds/navicore) into app0 and boot_app0.bin into
         otadata, one connection (write_argv; guard_writes: never 0x0 or 0x8000), then PING. A FLASHED.md row.
    Rungs 3 and 4 run only with allow_esptool=True; otherwise RecoveryFailed names what was tried and the two esptool
    command lines. The port is closed while esptool holds it and reopened after. RecoveryFailed when nothing works:
    then USB itself may be dead, and NaviCore work stops until someone is at the bench (NAVICORE.md §6.2)."""
    dev = nc.dev
    builds_root = builds_root or BUILDS
    tried = []
    version = _ping(nc, ping_tries)
    if version:
        return {"rung": "ping", "version": version, "tried": ["PING answered"], "status": None}
    tried.append(f"PING: no PONG in {ping_tries} tries")
    deadline = time.monotonic() + 10
    while getattr(dev, "connected", True) is False and time.monotonic() < deadline:
        time.sleep(0.25)
    if getattr(dev, "connected", True) is False:
        tried.append(f"hard_reset: not sent, {dev.port} is gone")
    else:
        m = dev.mark()
        try:
            nc.hard_reset()
            nc.wait_boot(since=m, timeout=boot_timeout)
            version = nc.ping()
        except AssertionError as e:
            # wait_boot pings only after the banner or a reopened port; one more PING covers a reset whose USB never
            # dropped and whose banner was missed, unless the ROM said it is in download mode.
            version = None if any(DOWNLOAD_MODE in x for x in dev.since(m)) else _ping(nc, 1)
            if not version:
                tried.append(f"hard_reset: {str(e).splitlines()[0][:160]}")
        if version:
            dev_note(dev, "ncflash: recovered by hard_reset")
            return {"rung": "hard_reset", "version": version, "tried": tried + ["hard_reset: back"], "status": None}
    good = known_good or os.path.join(builds_root, KNOWN_GOOD)
    image, _, _ = _image_paths(good)
    port = dev.port
    commands = [_command_line(kick_argv(port)), _command_line(write_argv(port, image, boot_app0_path()))]
    if not allow_esptool:
        raise RecoveryFailed(f"NaviCore on {port} is not back ({'; '.join(tried)}). The next rungs use esptool and "
                             f"need recover(..., allow_esptool=True): first, read-only, `{commands[0]}`; then "
                             f"`{commands[1]}`")
    try:
        info = check_image(good)
    except ImageError as e:
        raise RecoveryFailed(f"the known-good image does not pass check_image, so it is not written: {e}") from None
    info.pop("data", None)
    otadata = boot_app0_path()
    try:
        with open(otadata, "rb") as f:
            check_otadata(f.read())
        csv, _ = _partition_csv(os.path.join(github_root(), "NaviCore"))
        if csv:
            check_layout(csv)
    except (OSError, ValueError) as e:
        raise RecoveryFailed(f"not writing: {e}") from None
    run = esptool or run_esptool

    def esp(argv):
        guard_writes(argv)
        dev_note(dev, f"ncflash: {_command_line(argv)}")
        _release_port(dev)
        try:
            rc, out = run(argv)
        finally:
            reopened = _reclaim_port(dev)
        for x in out.splitlines()[-8:]:
            dev_note(dev, f"ncflash: esptool | {x}")
        return rc, out, reopened
    m = dev.mark()
    rc, out, reopened = esp(kick_argv(port))
    if rc == 0:
        tried.append("esptool: the chip was in ROM download mode; left it by the RTC watchdog")
        version = _wait_alive(nc, m, boot_timeout) if reopened else None
        if version:
            return {"rung": "kick", "version": version, "tried": tried, "status": _status_or_none(nc)}
        tried.append("still no PONG after leaving download mode")
    else:
        tried.append("esptool: not in download mode (no ROM bootloader answered)")
    m = dev.mark()
    argv = write_argv(port, info["path"], otadata)
    rc, out, reopened = esp(argv)
    last = (out.strip().splitlines() or ["no output"])[-1][:160]
    if rc != 0:
        raise RecoveryFailed(f"esptool could not write the known-good image (exit {rc}: {last}); tried: "
                             f"{'; '.join(tried)}. If USB itself is dead, stop NaviCore work and wait for someone at "
                             f"the bench")
    record_flash(builds_root, folder=info["folder"], elf_sha=info["elf_sha"],
                 tree=tree_line((info.get("manifest") or {}).get("navicore")), how="esptool app0 + otadata",
                 result="written (recovery)", what="; ".join(x for x in (what, build_line(info.get("manifest"))) if x))
    version = _wait_alive(nc, m, boot_timeout) if reopened else None
    if not version:
        raise RecoveryFailed(f"esptool wrote {info['path']} into app0, but NaviCore still does not answer; tried: "
                             f"{'; '.join(tried)}. Stop NaviCore work and wait for someone at the bench")
    return {"rung": "esptool", "version": version, "tried": tried + ["esptool: known-good image written, back"],
            "status": _status_or_none(nc)}


def _status_or_none(nc):
    try:
        return ota_status(nc)
    except AssertionError:
        return None


# ---------------------------------------------------------------------------- command line
_SSID = re.compile(r'(SoftAP ")((?!<redacted:)[^"]*)(")')     # redact_text now hashes it first; never hash a hash


def _log_to(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a", encoding="utf-8")
    t0 = time.monotonic()

    def log(name, direction, text):
        text = _SSID.sub(lambda m: m.group(1) + "<redacted:" + hashlib.sha256(m.group(2).encode()).hexdigest()[:12]
                         + ">" + m.group(3), redact_text(text))
        f.write(f"{time.monotonic() - t0:9.3f} {name:>9} {direction} {text}\n")
        f.flush()
    return log, f


def _live_run(results):
    """The name of a HIL run holding its run.lock, else None: that run owns the COM ports."""
    try:
        names = sorted(os.listdir(results), reverse=True)
    except OSError:
        return None
    return next((n for n in names if os.path.isdir(os.path.join(results, n)) and RunLock.held(os.path.join(results, n))),
                None)


def main(argv=None):
    """python -m hil.ncflash (from tests/hil): build | check | libs | status | reset | flash | recover. The board commands
    open bench.json's navicore port with DTR/RTS low, refuse while a HIL run holds its run.lock, and log every line to
    results/builds/ncflash-logs/ (passwords and the SoftAP name hashed)."""
    import argparse
    from .serialdev import SerialDevice
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="python -m hil.ncflash", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="compile NaviCore's working tree (or --source's) into "
                                     "results/builds/navicore-<tag>")
    b.add_argument("tag")
    b.add_argument("--hooks", action="store_true", help="-DNAVICORE_HIL_HOOKS=1 (INF9)")
    b.add_argument("--allow-drift", action="store_true", help="build although the sketchbook's libraries differ")
    b.add_argument("--rebuild", action="store_true", help="recompile a folder that already holds a build")
    b.add_argument("--source", default=None, help="the NaviCore tree to compile, a checkout or a git worktree of one "
                   "(default: the NaviCore folder beside this repo)")
    sub.add_parser("check", help="check an image without a board").add_argument("image")
    sub.add_parser("libs", help="the sketchbook's WCB_Client and WcbCmd against their repos")
    st = sub.add_parser("status", help="PING and ?OTALOCAL,STATUS (read-only)")
    rs = sub.add_parser("reset", help="the ladder's rung 2 alone: the USB-Serial/JTAG reset, the boot, PING and STATUS")
    fl = sub.add_parser("flash", help="flash a build folder over ?OTALOCAL")
    fl.add_argument("image")
    fl.add_argument("--what", default="", help="what is in it, for FLASHED.md")
    rc = sub.add_parser("recover", help="the recovery ladder")
    rc.add_argument("--known-good", default=None)
    rc.add_argument("--allow-esptool", action="store_true", help="let the ladder use esptool (rungs 3 and 4)")
    for p in (st, rs, fl, rc):
        p.add_argument("--bench", default=os.path.join(HERE, "bench.json"))
        p.add_argument("--port", default=None, help="override bench.json's navicore port")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "build":
            print(json.dumps(build(args.tag, args.hooks, allow_drift=args.allow_drift, rebuild=args.rebuild,
                                   source=args.source), indent=2))
            return 0
        if args.cmd == "check":
            info = check_image(args.image)
            info.pop("data", None)
            info.pop("manifest", None)
            print(json.dumps(info, indent=2))
            return 0
        if args.cmd == "libs":
            libs = check_libs()
            print(libs_line(libs))
            return 0 if all(r["same"] for r in libs.values()) else 1
        live = _live_run(RESULTS)
        if live:
            print(f"refused: HIL run {live} holds the bench (one owner per COM port)")
            return 2
        with open(args.bench, encoding="utf-8") as f:
            port = args.port or json.load(f)["devices"]["navicore"]["port"]
        log, fh = _log_to(os.path.join(BUILDS, "ncflash-logs",
                                       f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{args.cmd}.log"))
        dev = SerialDevice("navicore", port, 115200, log=log).open()
        try:
            time.sleep(0.2)
            nc = NaviCore(dev)
            if args.cmd == "status":
                print(json.dumps({"pong": _ping(nc), "status": ota_status(nc)}, indent=2))
            elif args.cmd == "reset":
                lines = nc.wait_boot(since=nc.hard_reset(), timeout=45)
                print(json.dumps({"boot": parse_boot(lines), "pong": _ping(nc), "status": ota_status(nc)}, indent=2))
            elif args.cmd == "flash":
                out = flash(nc, args.image, args.what, progress=lambda w, s: print(f"  {w} / {s} B", flush=True))
                print(json.dumps({k: out[k] for k in ("version", "secs", "stats", "app_sha", "row")}, indent=2,
                                 default=str))
            else:
                print(json.dumps(recover(nc, args.known_good, args.allow_esptool), indent=2, default=str))
        finally:
            dev.close()
            fh.close()
        return 0
    except (AssertionError, ValueError, OSError) as e:
        print(f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
