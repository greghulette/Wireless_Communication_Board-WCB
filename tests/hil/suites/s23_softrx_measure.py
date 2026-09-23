"""Soft-port RX measurements for tracker #78 — long, opt-in runs that answer a question — and a short guard for the fix.

- softrx.erratum_pairs asks whether a classic-ESP32 WCB loses soft-port input when two or three of its S3-S5 pins
  take edges at nearly the same moment. ESP32 erratum GPIO-3.14 (all revisions, no fix): pins 0-31 share one GPIO
  interrupt status register, and the handler's W1TC clear for one pin's edge can swallow another pin's edge in the
  same clock. EspSoftwareSerial receives on CHANGE interrupts, and W1's S3-S5 RX pins (25/21/23 on HW 1.0) are all
  in that group. The probes showed the loss on their own soft channels (#66/#78); nobody has measured it on a WCB.
  It CONFIRMED the loss on W1 (run 20260923-113109: 227 of 10125 multi-port lines, every single exact). The fix, a
  vendored EspSoftwareSerial whose RX ISR runs on a level interrupt it flips after every edge (Code/WCB/src/
  EspSoftwareSerial, classic ESP32 only), is now this test's subject: it must PASS on the fixed image.
- softrx.level_irq_stuck_line guards that fix's one new failure mode. A level interrupt armed for the level the line
  is already at re-fires forever; on a soft port that is an interrupt storm on core 1 and an interrupt-watchdog reset.
  Holding W1's S4 RX low for 2 s must leave W1 answering and not rebooting, and S4 must read text exactly afterwards.
  Short (~6 s) and not opt-in.
- soak.w1s4_wire hunts the intermittent W1S4 episode of run 20260923-012123 (~3.5 % of lines lost with only one probe
  edge pin receiving, so NOT the erratum) and says which of W1's output, the wire or crosstalk from S2 it was.

The two measurements are opt-in (bench.json "opt_in": "softrx_erratum", "w1s4_soak") and write a CSV into the run's
results folder; softrx.erratum_pairs needs wcb_probe 4 (TXSKEW). All three run under config_guard.
"""
import csv
import difflib
import os
import random
import re
import time

from hil.links import SW_MAX_BAUD
from hil.probe import HW_CHANNELS, HW_ONLY_HEADERS, PROBE_TXSKEW_VERSION
from hil.runner import Skip, test
from suites.common import Watch, config_guard, link, nonce, padded, prime, require_tokens, token, usb_wcb

# W1 console lines the measurement reads. A *** comment line only prints "Ignored chain command: <line>"
# (parseCommandsAndEnqueue, WCB.ino); one that lost its *** is plain text, which ?BCAST,IN,<port>,OFF stops at
# "Broadcast blocked from Serial<n>" (processBroadcastCommand). (.*) not (\S*): a mangled byte can be a space or a
# control character, and the diff is what says whether an edge was lost.
IGNORED = re.compile(r"^Ignored chain command: (\*\*\*.*)$")
BLOCKED = re.compile(r"^Broadcast blocked from Serial(\d)")
HEAP = re.compile(r"^Heap: (\d+) free, largest block (\d+), min free since boot (\d+)")   # ?STATS (WCB.ino)
ETM_ROW = re.compile(r"^WCB(\d+)(?: \(special\))?: Sent: (\d+), ACKd: (\d+), Retries: (\d+), Failed: (\d+)")
CLASSIC_HW = ("1", "21", "23", "24")   # ?HW,<n>: classic ESP32 boards, exposed to GPIO-3.14
S3_HW = ("31", "32")                   # ESP32-S3 boards: no GPIO-3.14 equivalent in the S3 errata


class _Csv:
    """Rows appended to <run results folder>/<name>; a no-op when the run has no results folder."""

    def __init__(self, bench, name, fields):
        self.path = os.path.join(bench.out_dir, name) if bench.out_dir else None
        self.fields = fields
        if self.path:
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fields).writeheader()

    def add(self, rows):
        if self.path and rows:
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, self.fields, extrasaction="ignore").writerows(rows)


def _w1_lines(dev, since):
    """(set of the *** lines W1 printed as ignored comments, {port: broadcast-blocked line count}) since the mark."""
    got, blocked = set(), {}
    for t in dev.since(since):
        m = IGNORED.match(t)
        if m:
            got.add(m.group(1))
            continue
        m = BLOCKED.match(t)
        if m:
            blocked[m.group(1)] = blocked.get(m.group(1), 0) + 1
    return got, blocked


def _rebooted(w, w_mark, probe_dev, p_mark):
    out = []
    if w.rebooted_since(w_mark):
        out.append("W1 rebooted (brownout? a maxed USB hub browns a board out on radio TX spikes)")
    if any("BOOT wcb_probe" in t for t in probe_dev.since(p_mark)):   # not ^-anchored: the ROM banner lands in front
        out.append(f"{probe_dev.name} rebooted")
    return out


def _stats(w):
    """((free, largest block, min free since boot) or None, {wcb: (sent, ackd, retries, failed)}) from one ?STATS."""
    lines = w.run("?STATS")
    heap = next((tuple(int(g) for g in m.groups()) for m in map(HEAP.match, lines) if m), None)
    etm = {int(m.group(1)): tuple(int(g) for g in m.groups()[1:])
           for m in map(ETM_ROW.match, lines) if m and "Unguaranteed" not in m.string}
    return heap, etm


def _not_text(tokens, ports):
    """W1 config tokens that keep any of `ports` off the text path, or put one more edge interrupt beside them.
    processIncomingSerial (WCB.ino, "Skip ports reserved for ...") leaves a port to an MP3 Trigger, DFPlayer or HCR
    on it, to a Kyber bridge draining a local Maestro there, and the Kyber local port; a raw ?MAP,SERIAL mapping
    matches on its input port (WCB_Storage.cpp). A WLED port, or a Maestro no bridge drains, still reads text. Its
    other two skips (a Maestro get-query, a live baud change) are momentary and not in the config. Token shapes as in
    s21_navicore_sbus.py. With none of these, a line that does not arrive is RX broken, not a busy port."""
    tokens = [t.upper() for t in tokens]
    kyber = next((t for t in tokens if t.startswith("?KYBER,LOCAL")), None)
    kport = None if kyber is None else "S" + (re.match(r"^\?KYBER,LOCAL(?:,S(\d))?", kyber).group(1) or "2")
    bridged = kyber is not None or "?MAESTRO,REMOTE" in tokens
    busy = [t for t in tokens if t.startswith("?MAP,PWM")]
    for p in ports:
        on_p = lambda t: re.search(rf"(?:\b|W1){p}\b", t)
        busy += [t for t in tokens
                 if t.startswith(f"?MAP,SERIAL,{p}")
                 or (t.startswith(("?MP3,", "?DFP,", "?HCR,PORT")) and on_p(t))
                 or (t.startswith("?MAESTRO,M") and on_p(t) and bridged)]
        if p == kport:
            busy.append(kyber)
    return sorted(set(busy))


# ============================================================ lost-edge signature
def _frame_bits(b):
    """The 8N1 line levels of one byte: start 0, data LSB first, stop 1."""
    return [0] + [(b >> i) & 1 for i in range(8)] + [1]


def _runs(bits):
    """Maximal equal-level runs of `bits`, as sets of bit indexes."""
    out, start = [], 0
    for i in range(1, len(bits) + 1):
        if i == len(bits) or bits[i] != bits[start]:
            out.append(set(range(start, i)))
            start = i
    return out


def _lost_edge(sent: bytes, got: bytes):
    """How `got` differs from `sent`, when it is one byte: '1 run' / '2 runs' when the flipped bits are exactly one or
    two whole bit runs of the sent frame - what a lost edge pair does in EspSoftwareSerial (the decoder fills each
    interval with the PREVIOUS recorded level, so the run between two unseen edges takes its neighbours' level; the
    probe's 0x35->0x37 and 0x34->0xF4 were this). 'other' for any other one-byte difference, None otherwise.
    Recorded, never asserted."""
    if len(sent) != len(got):
        return None
    diffs = [i for i in range(len(sent)) if sent[i] != got[i]]
    if len(diffs) != 1:
        return None
    fs, fg = _frame_bits(sent[diffs[0]]), _frame_bits(got[diffs[0]])
    flipped = {k for k in range(10) if fs[k] != fg[k]}
    whole = [r for r in _runs(fs) if r <= flipped]
    if whole and set().union(*whole) == flipped and len(whole) <= 2:
        return f"{len(whole)} run" + ("s" if len(whole) > 1 else "")
    return "other"


def _closest(line, candidates, max_diff=3):
    """The candidate of the same length with the fewest differing characters (at most max_diff), or None."""
    best, best_d = None, max_diff + 1
    for c in candidates:
        if len(c) == len(line):
            d = sum(1 for a, b in zip(c, line) if a != b)
            if d < best_d:
                best, best_d = c, d
    return best


# ============================================================ A. erratum GPIO-3.14 on W1's soft ports
LINE_LEN = 26          # ***<kind><port><nonce4><idx5>, padded with U (0x55: an edge on every bit boundary)
MULTI_ROUNDS = 4500    # pair and triple rounds; ~15 min at 9600
FINE_NS, COARSE_NS = 15000, 52000
COVERAGE_PAIRS = 2500  # pairs within the fine band a PASS needs, so a starved sweep is never reported as "refuted"
CONFIRM_LOSSES = 6     # multi-port lines lost with every single-port line exact: P = 0.5^6 = 1.6 % under equal rates


def _skew_ns(rng, fine, coarse):
    """~5 % exactly simultaneous, ~57 % across the fine band, ~38 % across half a bit either way."""
    r = rng.random()
    if r < 0.05:
        return 0
    if r < 0.62:
        return int(rng.uniform(-fine, fine))
    return int(rng.uniform(-coarse, coarse))


@test("softrx.erratum_pairs", "W1's soft ports lose an edge when two receive at once (ESP32 erratum GPIO-3.14)? Two- and three-port lines at a swept sub-bit skew against single-port controls (opt-in, ~15 min, wcb_probe 4)", needs=["wcb1"], opt_in="softrx_erratum")
def erratum_pairs(bench):
    """Measures whether W1 loses soft-port input to GPIO-3.14 (tracker #78). It cannot be done with the probe's
    hardware channels: each UART starts a frame on its own baud tick, so the skew between two of them is whatever
    their phases are, and one re-bind gives one skew for a whole batch. TXSKEW (wcb_probe 4) instead bit-bangs two or
    three lines onto W1's S3-S5 RX pins from one IRAM loop on the cycle counter, with the second and third line's
    start set to the nanosecond, so every round draws a fresh, independent skew.

    Rounds rotate the pairs (S3,S4), (S4,S5), (S3,S5), and every 4th round is a triple. Each multi-port round is
    followed by one single-port line on each port it used: the control, measured under the same conditions, so W1's
    own one-port RX (see soak.w1s4_wire) cannot be mistaken for the erratum. Lines of one round differ only in the
    port digit, so their edges coincide on nearly every bit boundary, within the drawn skew: seeded (noted), 60 %
    across +-15 us, 40 % across +-52 us (half a bit at 9600), ~5 % exactly zero. Each line is framed CR..CR: a lost
    edge that eats a terminator then spoils only its own line, never the single that follows.

    W1 transmits nothing: every line starts *** and only prints "Ignored chain command", and ?BCAST,IN is OFF on
    S3-S5 so a line that loses its *** stops at "Broadcast blocked". Producing a command would take corruption across
    more than one byte (the payload is U, digits, hex, P/T/S). The observation is W1's USB console, so no probe
    channel is read and the channel hand-off guard cannot misattribute anything.

    Verdict, pooled: any single-port line lost -> FAIL "inconclusive" (W1's one-port RX is not exact); >= 6 multi-port
    lines lost with every single exact -> FAIL "confirmed"; none lost with >= 2500 pairs inside +-15 us -> PASS, with
    the rule-of-three bound; anything else -> FAIL "inconclusive, rerun". ?HW 31/32 (ESP32-S3) should never lose."""
    s3, s4, s5 = link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    wires = (s3, s4, s5)
    if len({l.probe_name for l in wires}) != 1:
        raise Skip(f"W1 S3-S5 are not all on one probe ({'; '.join(map(str, wires))}): TXSKEW times the lines on one "
                   f"probe's clock")
    if any(l.tap for l in wires):
        raise Skip("a W1 S3-S5 wire is listen-only, so nothing can be sent into it")
    probe = s3.probe
    probe.hello()
    if not probe.version.isdigit() or int(probe.version) < PROBE_TXSKEW_VERSION:
        raise Skip(f"{s3.probe_name} runs wcb_probe v{probe.version}; TXSKEW needs v{PROBE_TXSKEW_VERSION} - flash "
                   f"tests/hil/wcb_probe")
    if probe.mesh_id:
        raise Skip(f"{s3.probe_name} is in mesh mode, where TXSKEW is refused (it masks interrupts)")
    tokens = bench.config_tokens(1, refresh=True)
    bauds = {bench.port_baud(1, l.port) for l in wires}
    if len(bauds) != 1 or max(bauds) > SW_MAX_BAUD:
        raise Skip(f"W1 S3-S5 run at {sorted(bauds)} baud: they must match (edges coincide only at one baud) and stay "
                   f"<= {SW_MAX_BAUD}, where one-port soft RX is exact")
    baud = bauds.pop()
    if (LINE_LEN + 2 + 1) * 10 / baud > 0.060:   # a CR..CR line plus a bit of skew must fit TXSKEW's 60 ms cap
        raise Skip(f"W1 S3-S5 run at {baud} baud: a {LINE_LEN + 2}-byte line takes longer than TXSKEW's 60 ms cap "
                   f"(it needs >= 4800 baud)")
    busy = _not_text(tokens, ("S3", "S4", "S5"))
    if busy:
        raise Skip(f"W1 has {busy}: a serial mapping or device reader takes the port off the text path, and a PWM "
                   f"input is one more edge interrupt")
    hw = (token(tokens, "?HW,") or "?HW,?").split(",")[1]
    chip = ("classic ESP32, exposed if the erratum bites" if hw in CLASSIC_HW else
            "ESP32-S3, expect no loss" if hw in S3_HW else "unknown chip")

    w = usb_wcb(bench)
    drive = {l.key: ("RX" if l.swap else "TX") for l in wires}   # the probe pin facing W1's RX (Link.pwm_out)
    half_bit_ns = 1e9 / baud / 2
    fine, coarse = min(FINE_NS, half_bit_ns), min(COARSE_NS, half_bit_ns)
    frame_s = (LINE_LEN + 2) * 10 / baud
    spacing = max(0.060, 2 * frame_s + 0.004)   # start to start; W1 has read and printed the last line well before
    seed = random.SystemRandom().randrange(1 << 31)
    rng = random.Random(seed)
    tag = nonce()[:4]

    def text(kind, l, idx):
        return (f"***{kind}{l.port[1]}{tag}{idx:05d}" + "U" * LINE_LEN)[:LINE_LEN]

    calls, pair_i = [], 0   # (round, kind, [(link, skew_ns, text)])
    for r in range(MULTI_ROUNDS):
        if r % 4 == 3:
            group, kind = wires, "T"
        else:
            group, kind = ((s3, s4), (s4, s5), (s3, s5))[pair_i % 3], "P"
            pair_i += 1
        skews = [0] + [_skew_ns(rng, fine, coarse) for _ in group[1:]]
        calls.append((r, kind, [(l, s, text(kind, l, r)) for l, s in zip(group, skews)]))
        for l in group:
            calls.append((r, "S", [(l, 0, text("S", l, r))]))

    def play(entries):
        probe.tx_skew(baud, [(l.header, drive[l.key], s, b"\r" + t.encode() + b"\r") for l, s, t in entries])

    held = []
    with config_guard(bench, 1):
        saved = [token(tokens, f"?BCAST,IN,S{n},") for n in "345"]
        try:
            for n in "345":
                w.run(f"?BCAST,IN,S{n},OFF")
            # Nothing on this probe may be bound while TXSKEW masks its interrupts (up to ~30 ms a call), and the
            # pins it drives must be free for LEVEL.
            for l in bench.links.all():
                if l.probe_name == s3.probe_name:
                    bench.links.release(l)
            for l in wires:
                if l.pwm_active:
                    l.pwm_stop()
                probe.level(l.header, drive[l.key], 1)
                held.append(l)
            time.sleep(0.05)
            for l in wires:   # the hand-over can glitch a partial byte into W1's line buffer: end that line
                probe.tx_skew(baud, [(l.header, drive[l.key], 0, b"\r")])
            time.sleep(0.3)
            for l in wires:
                t = f"***R{l.port[1]}{nonce()}"
                m = w.dev.mark()
                probe.tx_skew(baud, [(l.header, drive[l.key], 0, t.encode() + b"\r")])
                try:
                    w.dev.expect(rf"^Ignored chain command: {re.escape(t)}$", timeout=1.5, since=m)
                except AssertionError:
                    # Not a Skip: _not_text() already cleared the config, and one-port soft RX at <= SW_MAX_BAUD is
                    # exact, so this is the fix's regression (level-triggered RX not decoding) - it must not hide.
                    raise AssertionError(f"W1 {l.port} did not read one line as text on its own, before any "
                                         f"multi-port line (level-triggered RX broken, or its arm never flips?)")

            w_mark, p_mark = w.dev.mark(), probe.dev.mark()
            t_next = time.monotonic()
            for i, (_, _, entries) in enumerate(calls):
                wait = t_next - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                t_next = time.monotonic() + spacing
                play(entries)
                if i % 500 == 499:
                    boots = _rebooted(w, w_mark, probe.dev, p_mark)
                    if boots:
                        raise AssertionError(f"not a result - {', '.join(boots)} {i + 1} lines into the run")
            time.sleep(1.5)
            boots = _rebooted(w, w_mark, probe.dev, p_mark)
            if boots:
                raise AssertionError(f"not a result - {', '.join(boots)} during the run")
            got, blocked = _w1_lines(w.dev, w_mark)
        finally:
            for l in held:
                try:
                    probe.level(l.header, drive[l.key], "Z")
                except AssertionError:
                    pass
            try:
                prime(*wires)   # before the restore: a partial line must not become a broadcast
                time.sleep(0.5)
            finally:
                for t in saved:
                    if t:
                        w.run(t)
                for l in wires:
                    l.release()

    sent_all = {t for _, _, entries in calls for _, _, t in entries}
    corrupt = [g for g in got if g not in sent_all]
    rows, samples, lost_deltas = [], [], []
    multi = {"S3+S4": [0, 0], "S4+S5": [0, 0], "S3+S5": [0, 0], "S3+S4+S5": [0, 0]}   # [lines, lost]
    single = {l.port: [0, 0] for l in wires}
    coverage = 0
    for r, kind, entries in calls:
        skews = [s for _, s, _ in entries]
        if kind != "S":
            coverage += sum(1 for a in range(len(skews)) for b in range(a + 1, len(skews))
                            if abs(skews[b] - skews[a]) <= fine)
        for j, (l, s, t) in enumerate(entries):
            ok = t in got
            near = None
            if kind != "S":
                near = min((skews[k] - s for k in range(len(entries)) if k != j), key=abs)
                bucket = multi["+".join(x.port for x, _, _ in entries)]
            else:
                bucket = single[l.port]
            bucket[0] += 1
            bucket[1] += not ok
            mangled = sig = None
            if not ok:
                mangled = _closest(t, corrupt)
                if mangled:   # a byte >= 0x80 reaches us as U+FFFD (the console is decoded as UTF-8): not recoverable
                    sig = (_lost_edge(t.encode(), mangled.encode("latin-1")) if all(ord(c) < 0x80 for c in mangled)
                           else "byte >= 0x80, hidden by the console decode")
                if kind != "S":
                    lost_deltas.append(near)
                if len(samples) < 3 and mangled:
                    samples.append(f"{kind} {l.port} sent {t!r} got {mangled!r} nearest skew "
                                   f"{'-' if near is None else f'{near / 1000:+.2f} us'} lost edge {sig}")
            rows.append({"seed": seed, "round": r, "kind": kind, "port": l.port, "skew_ns": s,
                         "nearest_delta_ns": "" if near is None else near, "exact": int(ok),
                         "lost_edge": sig or "", "got": mangled or ""})
    _Csv(bench, "softrx_pairs.csv", ["seed", "round", "kind", "port", "skew_ns", "nearest_delta_ns", "exact",
                                     "lost_edge", "got"]).add(rows)

    multi_lines, multi_lost = sum(v[0] for v in multi.values()), sum(v[1] for v in multi.values())
    single_lines, single_lost = sum(v[0] for v in single.values()), sum(v[1] for v in single.values())
    summary = (f"W1 ?HW,{hw} ({chip}), {baud} baud, seed {seed}: multi-port lines exact "
               + ", ".join(f"{k} {v[0] - v[1]}/{v[0]}" for k, v in multi.items())
               + "; single lines exact " + ", ".join(f"{k} {v[0] - v[1]}/{v[0]}" for k, v in single.items())
               + f"; pairs within +-{fine / 1000:.0f} us: {coverage}; broadcast-blocked lines {blocked or 0}")
    bench.note("softrx.erratum_pairs: " + summary)
    for s in samples:
        bench.note("softrx.erratum_pairs sample: " + s)

    if single_lost:
        raise AssertionError(f"inconclusive: W1's one-port soft RX is not exact ({single_lost} of {single_lines} single "
                             f"lines lost), so pair losses cannot be pinned on the erratum - see soak.w1s4_wire. "
                             + summary)
    if multi_lost >= CONFIRM_LOSSES:
        hist = {}
        for d in lost_deltas:
            b = round(d / 500) * 0.5
            hist[b] = hist.get(b, 0) + 1
        what = ("GPIO-3.14 confirmed on a classic-ESP32 WCB" if hw not in S3_HW else
                "multi-port loss on an ESP32-S3 WCB, which has no GPIO-3.14 - not the erratum")
        raise AssertionError(f"{what}: {multi_lost} of {multi_lines} multi-port lines lost, every single exact; lost "
                             f"lines by nearest skew (0.5 us bins): "
                             + ", ".join(f"{k:+.1f}:{v}" for k, v in sorted(hist.items())) + ". " + summary)
    if multi_lost == 0 and coverage >= COVERAGE_PAIRS:
        bench.note(f"softrx.erratum_pairs: no loss in {multi_lines} multi-port lines; 95 % upper bound "
                   f"{3 / multi_lines:.3%} per line (the probe's own soft channels lost ~0.2-0.7 %)")
        return
    raise AssertionError(f"inconclusive: {multi_lost} multi-port lines lost, {coverage} pairs within the fine band "
                         f"(a PASS needs 0 and >= {COVERAGE_PAIRS}; confirmed needs >= {CONFIRM_LOSSES}); rerun. "
                         + summary)


@test("softrx.level_irq_stuck_line", "A W1 soft RX line held low for 2 s causes no interrupt storm (W1 keeps answering, no reboot) and S4 reads text exactly once released - the level-triggered RX of tracker #78", needs=["wcb1"])
def level_irq_stuck_line(bench):
    """Guards the one failure the level-triggered soft RX (tracker #78) can add. On a classic ESP32 the vendored
    EspSoftwareSerial arms each S3-S5 RX pin for the level the line is NOT at and flips it in the ISR; armed for the
    level the line already has, a level interrupt re-fires as fast as the core can take it, starving loop() until the
    interrupt watchdog resets the board. A line held low (a device resetting, a cable pulled, a break) is where a wrong
    arm would show. The probe pin facing W1's S4 RX is held low for 2 s; meanwhile ?VERSION must answer within 1 s,
    repeatedly, and no boot line may appear. Released, then a CR, then one *** line on S4 must print exactly as
    "Ignored chain command" - RX still decodes after the stuck period. ?BCAST,IN,S4 is OFF throughout, so the NUL a held
    line decodes to (dropped by the reader, #79) or a partial line can never become a broadcast.
    On an ESP32-S3 (?HW 31/32) RX stays edge-triggered; the test runs there too and must pass trivially."""
    s4 = link(bench, 1, "S4")
    if s4.tap:
        raise Skip(f"{s4} is listen-only, so W1's S4 RX cannot be driven")
    tokens = bench.config_tokens(1, refresh=True)
    busy = _not_text(tokens, ("S4",))
    if busy:
        raise Skip(f"W1 S4 is not a text port ({busy}): S4 must be read as text")
    baud = bench.port_baud(1, "S4")
    if baud > SW_MAX_BAUD:
        raise Skip(f"W1 S4 runs at {baud} baud, above the {SW_MAX_BAUD} where one-port soft RX is exact")
    hw = (token(tokens, "?HW,") or "?HW,?").split(",")[1]
    w = usb_wcb(bench)
    probe = s4.probe
    drive = "RX" if s4.swap else "TX"   # the probe pin facing W1's RX (as in softrx.erratum_pairs)

    def read_exactly(what):
        prime(s4)
        time.sleep(0.3)
        line = f"***{padded('SL', 20)}"
        m = w.dev.mark()
        s4.send(line.encode() + b"\r")
        try:
            w.dev.expect(rf"^Ignored chain command: {re.escape(line)}$", timeout=1.5, since=m)
            return None
        except AssertionError:
            return f"{what}: W1 S4 did not read {line!r} exactly (got {sorted(_w1_lines(w.dev, m)[0])})"

    # w_mark / stalled are read in the finally, which a Skip or an early failure reaches before the hold sets them.
    held, w_mark, stalled, body_exc = False, None, False, None
    with config_guard(bench, 1):
        saved = token(tokens, "?BCAST,IN,S4,")
        try:
            w.run("?BCAST,IN,S4,OFF")
            # _not_text() cleared the config and one-port soft RX is exact at this baud, so a miss here is the fix's
            # own regression (the new RX not decoding at all) - a FAIL, never a Skip.
            problem = read_exactly("before")
            assert problem is None, problem + (" - S4 RX does not decode even before the hold (level-triggered RX "
                                               "broken, or its arm never flips?)")

            s4.release()   # LEVEL takes the header pin itself
            w_mark = w.dev.mark()
            probe.level(s4.header, drive, 0)
            held = True
            t_end, answers = time.monotonic() + 2.0, 0
            while time.monotonic() < t_end:
                t0 = time.monotonic()
                try:
                    out = w.run("?VERSION", timeout=1.0)
                except AssertionError:
                    stalled = True
                    # The ROM reset banner / panic text print within ms of a reset; BOOT_LINE only ~2.7 s later
                    # (after setup()'s two 1 s delays), so rebooted_since() would still be False here.
                    reset = any(re.search(r"^rst:0x|Guru Meditation|Interrupt wdt", t) for t in w.dev.since(w_mark))
                    raise AssertionError(f"W1 (?HW,{hw}) stopped answering with its S4 RX held low - an interrupt "
                                         f"storm on core 1 (a level interrupt armed for the level the line is at?)"
                                         + (", and it reset" if reset else ""))
                if not any(x.startswith("Software Version:") for x in out):
                    raise AssertionError(f"?VERSION with S4 held low printed {out}")
                answers += 1
                time.sleep(max(0.0, 0.25 - (time.monotonic() - t0)))
            probe.level(s4.header, drive, "Z")
            held = False
            assert not w.rebooted_since(w_mark), f"W1 (?HW,{hw}) rebooted with its S4 RX held low"

            problem = read_exactly("after the stuck-low period")
            assert problem is None, problem + " - RX lost its interrupt arm?"
            assert not w.rebooted_since(w_mark), f"W1 (?HW,{hw}) rebooted after its S4 RX was released"
            bench.note(f"softrx.level_irq_stuck_line: W1 ?HW,{hw} answered ?VERSION {answers} times with S4 RX held "
                       f"low for 2 s; S4 read text exactly afterwards")
        except BaseException as e:
            body_exc = e
            raise
        finally:
            if held:
                try:
                    probe.level(s4.header, drive, "Z")
                except AssertionError:
                    pass
            restore_err = None
            try:
                if stalled and w_mark is not None:
                    # A W1 that reset is still in setup(): it flushes USB input ~1.5 s after the reset (WCB.ino
                    # setup(), delay + flush) and prints BOOT_LINE ~2.7 s after, so rebooted_since() is still False
                    # now and a restore sent now is dropped. Do not gate this on rebooted_since(). If W1 never reset,
                    # wait_boot times out and that is appended to the storm message below, not put in its place.
                    w.wait_boot(w_mark)
                prime(s4)   # before the restore: a partial line must not become a broadcast
                time.sleep(0.3)
                if saved:
                    w.run(saved)
            except AssertionError as e:
                restore_err = e
            finally:
                s4.release()
            if restore_err is not None:
                # The runner reports only str(e) (no chained context): keep the body's diagnosis in front.
                if body_exc is not None and not isinstance(body_exc, Skip):
                    msg = f"{body_exc}\n(and the restore afterwards failed: {restore_err})"
                    raise AssertionError(msg) from restore_err
                raise restore_err


# ============================================================ B. the W1S4 episode
SOAK_ROTATION = ("F", "4", "F", "2")   # F: S3 fan-out (S2 + S4 at once), 4: ;S4 alone, 2: ;S2 alone
SOAK_EXPECT = {"S2": ("F", "2"), "S4": ("F", "4"), "S5": ("F",)}
BLOCK_S, ZOOM_BLOCK_S, ZOOM_S, STATS_EVERY_S = 30, 10, 300, 300
SOAK_FIELDS = ["t_s", "block", "mode", "block_s", "S2_rx", "S4_rx", "S2_F", "S2_2", "S4_F", "S4_4", "S5_F",
               "near_misses", "spurious", "spurious_in_A2", "rxerr_S2", "rxerr_S4", "rxerr_S5"]


def _stream(l, since):
    """(bytes, [(offset, probe ms)]) the wire received since the mark."""
    data, starts = b"", []
    for ms, chunk in l.bursts(since):
        starts.append((len(data), ms))
        data += chunk
    return data, starts


def _ms_at(starts, off):
    return next((ms for o, ms in reversed(starts) if o <= off), None)


def _soak_block(watch, ports, sent):
    """Score one block. ports = {"S2": link, "S4": link, "S5": link}; sent = [(slot kind, line)].
    Returns {exact, total: {(port, kind): n}, near: [(port, kind, line, got)], spurious: [(port, bytes, probe ms)],
    a2_ms: [probe ms of each A2 line on S2], rxerr: {port: [lines]}}."""
    exact, total, near, spurious, a2_ms, rxerr = {}, {}, [], [], [], {}
    for port, l in ports.items():
        want = {line.encode(): kind for kind, line in sent if kind in SOAK_EXPECT[port]}
        data, starts = _stream(l, watch.marks[l.key])
        seen, off = set(), 0
        for tok in data.split(b"\r"):
            at = off
            off += len(tok) + 1
            if not tok:
                continue
            if tok in want and tok not in seen:
                seen.add(tok)
                if port == "S2" and want[tok] == "2":
                    a2_ms.append(_ms_at(starts, at))
                continue
            whole = next((x for x in want if x not in seen and tok.endswith(x)), None)
            if whole:   # the line arrived intact, behind other bytes: an earlier line that lost its CR, or spurious
                seen.add(whole)
                tok = tok[:-len(whole)]
            miss = max(((difflib.SequenceMatcher(None, tok, x).ratio(), x) for x in want if x not in seen),
                       default=(0, None))
            if miss[0] >= 0.85:
                near.append((port, want[miss[1]], miss[1], tok))
            else:
                spurious.append((port, tok, _ms_at(starts, at)))
        for x, kind in want.items():
            total[(port, kind)] = total.get((port, kind), 0) + 1
            exact[(port, kind)] = exact.get((port, kind), 0) + (x in seen)
        rxerr[port] = l.errors(watch.marks[l.key])
    return dict(exact=exact, total=total, near=near, spurious=spurious, a2_ms=a2_ms, rxerr=rxerr)


@test("soak.w1s4_wire", "W1S4 corruption soak: hardware vs soft receiver, S4 alone vs with S2, crosstalk into idle S4 (opt-in, default 20 min)", needs=["wcb1"], opt_in="w1s4_soak")
def w1s4_soak(bench):
    """Hunts the episode of run 20260923-012123: W1S4 lost ~3.5 % of lines for a few minutes with only one probe edge
    pin receiving, which rules out GPIO-3.14, and only while S2 and S4 transmitted together; minutes later all was
    clean (tracker #78). The soak repeats that load for soak_minutes (bench.json, default 20) and records enough to
    say where the corruption was made when it comes back.

    W1S3 is injected from a probe hardware channel. Blocks of 30 s alternate the receivers: H puts W1S4 on a probe
    HARDWARE UART (W1S2 soft), S puts W1S2 on hardware and W1S4 soft - the arrangement that caught the episode. W1S5
    is soft throughout. At most one probe soft pin receives at a time: S2 and S4 overlap, and S5 is written after S4
    (the fan-out loop, WcbSoftSerial::write blocks), so the probe's own erratum stays out of it. Slots every 120 ms
    rotate F (a plain S3 line: W1 writes S2, then S4 over RMT at once, then S5, then the mesh), A4 (;S4 alone) and
    A2 (;S2 alone, when W1S4 and W1S5 must stay silent: any byte there is induced).

    Per block: exact/total per (wire, receiver, slot), near-misses, spurious bytes, probe RXERR lines, boot lines on
    W1 and the probe (a reboot aborts: that is the power trap, not a wire). A CSV row per block (soak_w1s4.csv) and a
    note per minute. W1 ?STATS heap every 5 min and after any block with an error; any error on W1S2 or W1S4 switches
    to 10 s blocks for the next 5 min. ?BCAST,IN is OFF on S2/S4/S5 meanwhile: a re-bind can glitch a partial line
    into W1's buffers, and it must never become a broadcast. PASS = every slot exact, nothing spurious, no reboot;
    a FAIL names the likely place: W1's content, W1's S4 waveform or the lead, sub-bit glitches, S2 coupling."""
    s2, s3, s4, s5 = link(bench, 1, "S2"), link(bench, 1, "S3"), link(bench, 1, "S4"), link(bench, 1, "S5")
    require_tokens(bench, 1, "?BCAST,IN,S3,ON", "?BCAST,OUT,S2,ON", "?BCAST,OUT,S4,ON", "?BCAST,OUT,S5,ON",
                   "?BCAST,OUT,S0,OFF")
    tokens = bench.config_tokens(1)
    if token(tokens, "?MAP,SERIAL,S3"):
        raise Skip("W1 S3 has a serial mapping, which replaces its broadcast")
    outs = {"S2": s2, "S4": s4, "S5": s5}
    if s3.tap or len({l.probe_name for l in outs.values()}) != 1 or any(l.header in HW_ONLY_HEADERS for l in outs.values()):
        raise Skip(f"W1 S2, S4 and S5 must be soft-capable headers of one probe and W1 S3 must transmit "
                   f"({'; '.join(map(str, (s2, s3, s4, s5)))})")
    bauds = {bench.port_baud(1, p) for p in outs}
    if len(bauds) != 1 or max(bauds) > SW_MAX_BAUD or bench.port_baud(1, "S3") > SW_MAX_BAUD:
        raise Skip(f"W1 S2/S4/S5 must share one baud and S2-S5 stay <= {SW_MAX_BAUD} (got S2/S4/S5 {sorted(bauds)}, "
                   f"S3 {bench.port_baud(1, 'S3')}): only then does S5 never overlap S2 on the probe")
    baud = bauds.pop()
    minutes = float(bench.cfg.get("soak_minutes", 20))
    if minutes <= 0:   # no block would run, and an empty soak must not read as a PASS
        raise Skip(f"bench.json soak_minutes is {minutes}: nothing to soak")
    slot_s = max(0.12, 5 * 21 * 10 / baud)
    w = usb_wcb(bench)
    probe_dev = s2.probe.dev

    def arrange(mode):
        s2.release()
        s4.release()
        (s4 if mode == "H" else s2).listen(hw=True)
        (s2 if mode == "H" else s4).listen(hw=False)
        s5.listen(hw=False)

    blocks, heap = [], []
    table = _Csv(bench, "soak_w1s4.csv", SOAK_FIELDS)
    start = time.monotonic()
    with config_guard(bench, 1):
        saved = [token(tokens, f"?BCAST,IN,S{n},") for n in "245"]
        try:
            for n in "245":
                w.run(f"?BCAST,IN,S{n},OFF")
            # Free the probes' channels explicitly (W1's S1 wire holds A on a HW-only header), rather than leaning on
            # LRU eviction, which could take a channel from the wire being scored.
            for l in bench.links.all():
                if l not in (s2, s3, s4, s5) and l.probe_name in (s2.probe_name, s3.probe_name):
                    bench.links.release(l)
            s3.listen(hw=True)
            deadline, zoom_until, next_stats, seq, bi = start + minutes * 60, 0.0, start, 0, 0
            minute, acc = 0, []
            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_stats:
                    heap.append((round(now - start), *_stats(w)))
                    next_stats = now + STATS_EVERY_S
                mode = "HS"[bi % 2]
                block_s = ZOOM_BLOCK_S if now < zoom_until else BLOCK_S
                arrange(mode)
                prime(s2, s4, s5)
                time.sleep(0.3)
                watch = Watch(s2, s4, s5)   # marks AFTER the re-bind, so no read crosses a channel hand-off
                w_mark, p_mark = w.dev.mark(), probe_dev.mark()
                sent, t0, k = [], time.monotonic(), 0
                while time.monotonic() - t0 < block_s:
                    wait = t0 + k * slot_s - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                    kind = SOAK_ROTATION[k % 4]
                    line = padded(f"K{kind}{seq:05d}", 20)
                    if kind == "F":
                        s3.send(line.encode() + b"\r")
                    elif kind == "4":
                        w.send(";S4" + line)
                    else:
                        w.send(";S2" + line)
                    sent.append((kind, line))
                    seq, k = seq + 1, k + 1
                time.sleep(1.0)   # the last slot's lines, and the mesh copies, before the next re-bind
                res = _soak_block(watch, outs, sent)
                res.update(t_s=round(time.monotonic() - start), block=bi, mode=mode, block_s=block_s,
                           rx={p: ("hw" if l.channel in HW_CHANNELS else "soft") for p, l in outs.items()})
                a2 = [m for m in res["a2_ms"] if m is not None]
                res["in_a2"] = [(p, b, m) for p, b, m in res["spurious"]
                                if p in ("S4", "S5") and m is not None and any(-20 <= m - t <= slot_s * 1000 for t in a2)]
                blocks.append(res)
                errs = {p: sum(res["total"].get((p, kk), 0) - res["exact"].get((p, kk), 0) for kk in SOAK_EXPECT[p])
                        + sum(1 for sp in res["spurious"] if sp[0] == p) for p in outs}
                table.add([{
                    "t_s": res["t_s"], "block": bi, "mode": mode, "block_s": block_s,
                    "S2_rx": res["rx"]["S2"], "S4_rx": res["rx"]["S4"],
                    **{f"{p}_{kk}": f"{res['exact'].get((p, kk), 0)}/{res['total'].get((p, kk), 0)}"
                       for p in outs for kk in SOAK_EXPECT[p]},
                    "near_misses": len(res["near"]), "spurious": len(res["spurious"]), "spurious_in_A2": len(res["in_a2"]),
                    **{f"rxerr_{p}": len(res["rxerr"][p]) for p in outs}}])
                boots = _rebooted(w, w_mark, probe_dev, p_mark)
                if boots:
                    raise AssertionError(f"soak aborted {res['t_s']} s in: {', '.join(boots)}")
                if errs["S2"] or errs["S4"]:
                    zoom_until = time.monotonic() + ZOOM_S
                    heap.append((round(time.monotonic() - start), *_stats(w)))
                acc.append((res, errs))
                if int(res["t_s"] // 60) != minute or time.monotonic() >= deadline:
                    bench.note(f"soak.w1s4_wire minute {minute}, errors per block: " + ", ".join(
                        f"{r['mode']} " + " ".join(f"{p}:{e}" for p, e in er.items()) for r, er in acc))
                    minute, acc = int(res["t_s"] // 60), []
                bi += 1
        finally:
            try:
                prime(s2, s4, s5)   # before the restore: a partial line must not become a broadcast
                time.sleep(0.5)
            finally:
                for t in saved:
                    if t:
                        w.run(t)
                for l in (s2, s3, s4, s5):
                    l.release()

    def lost(port, kind, rx=None):
        """(lines lost, lines sent) for one wire and slot kind, on one receiver kind or both."""
        mine = [b for b in blocks if rx is None or b["rx"][port] == rx]
        tot = sum(b["total"].get((port, kind), 0) for b in mine)
        return tot - sum(b["exact"].get((port, kind), 0) for b in mine), tot

    s4f_hw, s4f_sw, s4_4, s2_f = lost("S4", "F", "hw"), lost("S4", "F", "soft"), lost("S4", "4"), lost("S2", "F")
    spurious = [sp for b in blocks for sp in b["spurious"]]
    in_a2 = [sp for b in blocks for sp in b["in_a2"]]
    frame_hw = [e for b in blocks if b["rx"]["S4"] == "hw" for e in b["rxerr"]["S4"] if " FRAME " in e]
    rxerrs = {p: sum(len(b["rxerr"][p]) for b in blocks) for p in outs}
    totals = (f"{len(blocks)} blocks in {round(time.monotonic() - start)} s at {baud} baud; lost/total S4 F on hw "
              f"{s4f_hw[0]}/{s4f_hw[1]}, on soft {s4f_sw[0]}/{s4f_sw[1]}; S4 A4 {s4_4[0]}/{s4_4[1]}; S2 F "
              f"{s2_f[0]}/{s2_f[1]}; spurious {len(spurious)} ({len(in_a2)} during A2); RXERR lines {rxerrs}")
    heaps = [(t, h) for t, h, _ in heap if h]
    heap_trend = (f"W1 heap free {heaps[0][1][0]} -> {heaps[-1][1][0]}, min since boot {heaps[-1][1][2]}"
                  if heaps else "W1 heap not read")
    bench.note(f"soak.w1s4_wire: {totals}; {heap_trend}")
    errors = sum(b["total"][key] - b["exact"][key] for b in blocks for key in b["total"]) + len(spurious)
    if not errors:
        return

    cls = []
    by_line = {}
    for b in blocks:
        for port, kind, line, got in b["near"]:
            if kind == "F":
                by_line.setdefault(line, {})[port] = got
    if any(v.get("S2") is not None and v.get("S2") == v.get("S4") for v in by_line.values()):
        cls.append("content: W1 formed it wrong (firmware) - the S2 and S4 copies of one line carry the same corruption")
    hw_rate = s4f_hw[0] / s4f_hw[1] if s4f_hw[1] else 0
    sw_rate = s4f_sw[0] / s4f_sw[1] if s4f_sw[1] else 0
    if (s4f_hw[0] and hw_rate >= sw_rate / 3) or frame_hw:
        cls.append(f"waveform: W1 S4 output (GPIO4 on HW 1.0) or the lead; scope both ends (S4 F lost on the hardware "
                   f"receiver {s4f_hw[0]}/{s4f_hw[1]}, FRAME errors there {len(frame_hw)})")
    elif s4f_sw[0] and not s4f_hw[0]:
        cls.append("sub-bit glitches: crosstalk or ground, not W1's bytes (only the soft receiver saw W1S4 errors)")
    if s4_4[1] and not s4_4[0] and (s4f_hw[0] or s4f_sw[0]):
        cls.append("needs S2 activity (S2 TX = GPIO26 on HW 1.0): ;S4 alone stayed exact while the S2+S4 fan-out did not")
    if in_a2:
        cls.append(f"S2 induces bytes on idle W1S4/W1S5: crosstalk proven ({len(in_a2)} bursts during ;S2-only slots, "
                   f"e.g. {in_a2[0][0]} {in_a2[0][1]!r})")
    if not cls:
        cls.append("unclassified - see soak_w1s4.csv and the per-minute notes")
    raise AssertionError("; ".join(cls) + f" || {totals}; {heap_trend}")
