"""A board on the released firmware, updated to this one: does everything it held come through?

WIFI goes to main as 7.0 (Greg, 2026-10-15), and every user's board is on the release it replaces,
6.2.1_021242RSEP2026. Nothing tested that path: the HIL suites run this firmware on boards configured by this firmware.
Here W2 is made into such a user's board - the released app (git: the tag's Code/bin, so no download), a factory reset
on it (?ERASE,NVS), its own config replayed line by line onto the release, and a set of sequences of every shape the
release stores in NVS - and then updated the way the Wizard and Intellex update a board: the app only, over USB
(hil/wcbflash.py). The release's own ?backup before is the user's config; this firmware's ?backup after must hold all
of it, and the sequences must have moved into the sequence store whole.

Afterwards W2 is put back on this firmware with its own config, the way intellex.flash_w2_factory puts it back
(?HW and a boot, WDP off, the Maestro table cleared, every line of its chain, WDP on, a boot, its learned peers), inside
config_guard over every WCB. W2's chain carries the mesh password and WiFi credentials: it is read from W2 and given
back to W2 only, never noted.
"""
import re
import subprocess
import time

from hil import wcbflash
from hil.intellex import REPO
from hil.runner import Skip, test
from hil.wcb import WCB, group_tokens
from suites.common import config_guard, token
from suites.s27_identity_destructive import _learned_peers
from suites.s31_password_erase import _replay, _replayable

RELEASE_TAG = "v6.2.1_021242RSEP2026"
RELEASE_VERSION = RELEASE_TAG[1:]
BUILDS = {"esp32": "wcb-esp32-meshq", "esp32s3": "wcb-esp32s3-meshq"}   # results/builds/<dir>/WCB.ino.bin: the bench image

# Sequences of every shape a user has: many short ones, a chain of steps joined by '^', commas inside the value, a key
# of the full 15 characters - and a long one, stored separately (LONG_TRIES).
SEQS = ([(f"upg{i:02d}", f";S1UPG{i:02d}") for i in range(1, 21)]
        + [("upgchain", ";S1UPGA^;t500^;S1UPGB^;t500^;S2UPGC"),
           ("upgcomma", ";S1UPG,with,commas"),
           ("upgkey15charsxx", ";S1UPGKEY15")])
# The longest the release takes. Its ceiling is NVS's 4,000-byte string, but a string that long needs a whole empty NVS
# page, and on a board in use there often is none: 6.2.1 answered "Failed to store sequence 'upglong' (NVS write
# rejected)" for 3,999 characters on W2 (run 20261015-143515) - a real limit users meet, and one this firmware's file
# store does not have. So the longest of these the release accepts is the one carried across.
LONG_KEY = "upglong"
LONG_TRIES = (3999, 2000, 1000)


def _chip(tokens):
    """ESP32 or ESP32-S3 from the chain's ?HW (wcb_pin_map: hardware 3.x is the S3)."""
    hw = token(tokens, "?HW,")
    try:
        return "esp32s3" if int(hw.split(",")[1]) >= 30 else "esp32"
    except (AttributeError, IndexError, ValueError):
        raise Skip(f"W2's chain names no hardware version ({hw!r}): cannot tell which image it takes")


def _release_image(chip):
    name = f"Code/bin/WCB_{RELEASE_VERSION}_main_{'ESP32S3' if chip == 'esp32s3' else 'ESP32'}.bin"
    r = subprocess.run(["git", "-C", REPO, "show", f"{RELEASE_TAG}:{name}"], capture_output=True, timeout=60)
    if r.returncode != 0 or len(r.stdout) < 100_000:
        raise Skip(f"the released image {RELEASE_TAG}:{name} is not in this repo's history")
    return r.stdout


def _bench_image(chip, running):
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results", "builds", BUILDS[chip],
                        "WCB.ino.bin")
    try:
        data = open(path, "rb").read()
    except OSError:
        raise Skip(f"no bench image at {path}: build it (the WCB build into results/builds/{BUILDS[chip]})")
    if running.encode() not in data:
        raise Skip(f"results/builds/{BUILDS[chip]}/WCB.ino.bin is not the firmware W2 runs ({running}): rebuild it, "
                   f"or the test would leave W2 on another build")
    return data


def _seqs(tokens):
    """{key: value} of the ?SEQ,SAVE lines in a chain (its '^'-joined steps put back together)."""
    out = {}
    for t in group_tokens(tokens):
        m = re.match(r"^\?SEQ,SAVE,([^,]+),(.*)$", t, re.S | re.I)
        if m:
            out[m.group(1)] = m.group(2)
    return out


# The boot banner both firmwares print (WCB.ino setup). The harness's wait_boot waits for a later setup line this
# firmware prints and the release does not, and WCB.reboot for this firmware's deferred "Reboot queued": neither
# works on the release, which restarts 2 s after a legacy ?reboot and after its ?ERASE,NVS's quiet window.
BANNER = r"^Booting up the Wireless Communication Board"


def _wait_up(w, mark, timeout=45.0):
    """The board rebooted since `mark` and answers ?VERSION again (whichever firmware) -> its version."""
    w.dev.expect(BANNER, timeout=timeout, since=mark)
    end = time.monotonic() + 20
    while True:
        time.sleep(1.5)                       # the USB reader's start delay drops what comes sooner
        try:
            return w.version()
        except AssertionError:
            if time.monotonic() > end:
                raise


def _reboot_any(w):
    m = w.dev.mark()
    w.dev.send("?reboot")
    return _wait_up(w, m)


def _onto(w, tokens, hw):
    """A board fresh from a factory reset given `tokens`: the pin map and a boot first, WDP off and the Maestro table
    cleared while the lines go in, WDP back, a boot -> the lines refused (redacted)."""
    w.run(hw)
    _reboot_any(w)
    w.run("?WDP,OFF")
    w.run("?MAESTRO,CLEAR,ALL", timeout=8)
    refused = _replay(w, tokens)
    if "?WDP,OFF" not in tokens:
        w.run("?WDP,ON")
    _reboot_any(w)
    return refused


@test("upgrade.from_release", "A board on the released firmware (6.2.1_021242RSEP2026) updated to this one the way the "
      "Wizard does it (app only, over USB) keeps everything: W2 on the release, factory-reset, its own config replayed and "
      "24 sequences of every shape stored (20 short, a '^' chain, commas, a 15-character key, and the longest the release "
      "takes of 3,999 / 2,000 / 1,000 characters); "
      "after the update every line of the release's ?backup is in this firmware's, and every sequence is whole and in the "
      "sequence store. W2 is put back after (destructive: a factory reset and two flashes)", needs=["wcb2"])
def from_release(bench):
    w2 = WCB(bench.dev("wcb2"))
    running = w2.version()
    tokens = w2.backup_chain()[0]
    _replayable(tokens)
    hw, q = token(tokens, "?HW,"), token(tokens, "?WCBQ,")
    if hw is None or hw.upper() == "?HW,0" or q is None:
        raise Skip("W2's chain has no hardware version or ?WCBQ to restore")
    chip = _chip(tokens)
    new_image, rel_image = _bench_image(chip, running), _release_image(chip)
    floor = int(q.split(",")[1])
    learned = _learned_peers(w2, floor)
    problems, notes = [], []
    with config_guard(bench, *bench.wcb_numbers()):
        on_release = False
        try:
            # 1. A user's board: the released app, a factory reset, its config, its sequences.
            wcbflash.write_app(bench, "wcb2", rel_image)
            on_release = True
            w2 = WCB(bench.dev("wcb2"))
            v = w2.version()
            if v != RELEASE_VERSION:
                raise AssertionError(f"W2 runs {v} after writing the released app, not {RELEASE_VERSION}")
            m = w2.dev.mark()
            w2.dev.send("?ERASE,NVS")
            _wait_up(w2, m)
            refused = _onto(w2, tokens, hw)
            if refused:
                notes.append(f"{len(refused)} line(s) of W2's chain the release refused (this firmware's own settings)")
            for k, val in SEQS:
                w2.run(f"?SEQ,SAVE,{k},{val}", timeout=8)
            stored_long = 0
            for n in LONG_TRIES:
                out = w2.run(f"?SEQ,SAVE,{LONG_KEY},;S1" + "L" * (n - 3), timeout=8)
                if not any("Failed to store" in x for x in out):
                    stored_long = n
                    break
            notes.append(f"the release stored a {stored_long}-character sequence" if stored_long
                         else "the release stored no long sequence (all of " + str(LONG_TRIES) + " refused)")
            rel_chain = w2.backup_chain()[0]
            rel_seqs = _seqs(rel_chain)
            lost = [k for k, val in SEQS if rel_seqs.get(k) != val]
            if lost:
                raise AssertionError(f"the release itself did not store {lost} whole: the starting point is wrong")

            # 2. The update: this firmware's app, nothing else.
            wcbflash.write_app(bench, "wcb2", new_image)
            on_release = False
            w2 = WCB(bench.dev("wcb2"))
            v = w2.version()
            if v != running:
                problems.append(f"W2 runs {v} after the update, not {running}")
            new_chain = w2.backup_chain()[0]
            new_grouped = group_tokens(new_chain)
            missing = [t for t in group_tokens(rel_chain)
                       if not t.upper().startswith(("?CHK", "?PEERSLIVE")) and t not in new_grouped]
            if missing:
                problems.append(f"{len(missing)} line(s) of the release's config are not in this firmware's after the "
                                f"update: {[t.split(',')[0] + ',' + t.split(',')[1] if ',' in t else t for t in missing][:12]}")
            new_seqs = _seqs(new_chain)
            moved_wrong = [k for k, val in rel_seqs.items() if new_seqs.get(k) != val]
            if moved_wrong:
                problems.append(f"sequences not whole after the update: {moved_wrong[:10]}")
            store = next((x for x in w2.run("?NVS", timeout=8) if x.startswith("Sequences:")), "")
            mm = re.match(r"^Sequences: (\d+) in the sequence store", store)
            if not mm or int(mm.group(1)) != len(rel_seqs):
                problems.append(f"?NVS says {store!r}; expected all {len(rel_seqs)} sequences in the sequence store")
            bench.note(f"upgrade.from_release: {RELEASE_VERSION} -> {running}: {len(group_tokens(rel_chain))} config "
                       f"line(s), {len(rel_seqs)} sequence(s) on the release; after the update {len(missing)} line(s) "
                       f"missing, {len(moved_wrong)} sequence(s) not whole; {store}" + (f"; {'; '.join(notes)}" if notes
                                                                                         else ""))
        finally:
            # 3. W2 back: this firmware, its own config, its own sequences only, its learned peers.
            if on_release:
                wcbflash.write_app(bench, "wcb2", new_image)
            w2 = WCB(bench.dev("wcb2"))
            for k in [k for k, _ in SEQS] + [LONG_KEY]:
                w2.run(f"?SEQ,CLEAR,{k}", timeout=8)
            refused = _onto(w2, tokens, hw)
            if refused:
                problems.append("lines refused when W2 was put back: " + "; ".join(refused))
            for n in learned:
                w2.run(f"?WDP,ADD,{n}")
    now = _learned_peers(WCB(bench.dev("wcb2")), floor)
    if now != learned:
        problems.append(f"W2's learned peers after the restore: {now}, before: {learned}")
    assert not problems, "; ".join(problems)
