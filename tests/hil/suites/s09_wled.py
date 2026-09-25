"""WLED: ;L1,<verb> from W1 is routed to W2 (WLED 1's host) and written to W2 S2 as the exact
JSON WcbCmd's WcbWled::build emits, newline-terminated (WcbCmd/src/WcbWled.cpp)."""
import time

from hil.runner import test
from suites.common import usb_wcb, wire

NEEDS = ["wcb1", "probe2"]

CASES = [
    ("ON", b'{"on":true}\n'),
    ("OFF", b'{"on":false}\n'),
    ("TOGGLE", b'{"on":"t"}\n'),
    ("BRI,42", b'{"bri":42}\n'),
    ("BRI,300", b'{"bri":255}\n'),                      # clamped
    ("PS,12", b'{"ps":12}\n'),
    ("COL,#FF8000", b'{"seg":[{"col":[[255,128,0]]}]}\n'),
    ("COL,11223344", b'{"seg":[{"col":[[17,34,51,68]]}]}\n'),
    ("FX,3", b'{"seg":[{"fx":3}]}\n'),
    ("FX,9,128,200", b'{"seg":[{"fx":9,"sx":128,"ix":200}]}\n'),
    ("PAL,4", b'{"seg":[{"pal":4}]}\n'),
    ('JSON,{"on":true,"bri":10}', b'{"on":true,"bri":10}\n'),
]


@test("wled.verbs", "Every ;L1,<verb> lands on W2 S2 as its exact WLED JSON line", needs=NEEDS)
def verbs(bench):
    probe, ch = wire(bench, 2, "S2")
    w = usb_wcb(bench)
    bad = []
    for verb, expected in CASES:
        m = probe.dev.mark()
        w.send(f";L1,{verb}")
        try:
            probe.expect_bytes(ch, expected, timeout=3, since=m)
            time.sleep(0.3)
            got = probe.received(ch, m)
            if got != expected:
                bad.append(f"{verb}: extra bytes {got!r}")
        except AssertionError as e:
            bad.append(f"{verb}: {e}")
    assert not bad, "; ".join(bad)


@test("wled.bad_verb", "An unknown ;L verb writes nothing", needs=NEEDS)
def bad_verb(bench):
    probe, ch = wire(bench, 2, "S2")
    w = usb_wcb(bench)
    m = probe.dev.mark()
    w.send(";L1,ON")                    # positive control: the route to W2 S2 delivers before silence is asserted
    probe.expect_bytes(ch, b'{"on":true}\n', timeout=3, since=m)
    time.sleep(0.3)
    m = probe.dev.mark()
    w.send(";L1,BOGUS")
    probe.expect_silence(ch, window=2.0, since=m)
