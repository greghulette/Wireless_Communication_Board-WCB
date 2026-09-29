"""Intellex's SerialTransport on a real bench port, under its venv (IX-WP5). The harness releases the port first and takes
it back after (hil/intellex.py handed_over); INTELLEX_SERIAL_ALLOW names that port alone, so SerialTransport refuses any
other (H2). Intellex src/transport.py:11-28 states the three contracts: raw bytes up, a write that RAISES on loss, and an
open that does not assert DTR/RTS (serial_transport.py:39-66, the whole reason Intellex is native).

args: port; kind ("wcb" | "navicore"); boot (the markers a starting board prints: hil/intellex.py WCB_BOOT_MARKERS /
NAVICORE_BOOT_MARKERS); probe / reply (a line that proves the link works, and what its answer contains).

group "no_reset"      (intellex.serial_no_reset_w1 / _navicore): open and close the port `cycles` times, `dwell` s each;
                      no boot marker on the stream, no loss reported, and the probe answered afterwards.
group "contract"      (intellex.serial_contract_w1): on_data gets only bytes; ';S0,<marker><UTF-8>' comes back byte-exact;
                      set_signals(dtr=False, rts=False) resets nothing; a second open of a held port raises; a write or
                      set_signals after close() raises TransportError; close() twice is safe.
group "signals_reset" (intellex.serial_signals_reset_w1, a characterisation): set_signals(rts=True) then (rts=False)
                      resets the board (its boot lines arrive), and the order the lines are written in is recorded:
                      DTR before RTS (serial_transport.py:108-119).
group "device_loss"   (intellex.serial_device_loss_navicore, opt-in intellex_reboot): a NaviCore REBOOT through the
                      transport. Either the COM handle dies - on_lost fires, is_open goes False and a write raises - or it
                      survives, the boot banner arrives and the same transport still gets a PONG. Records which.

Nothing a board prints is quoted here: counts, marker names and the test's own markers only.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import SkipCase, case, check  # noqa: E402,F401


class _Sink:
    """on_data / on_lost for one or more transports: every chunk, its type, and every loss."""

    def __init__(self):
        self.data = bytearray()
        self.types = set()
        self.lost = []
        self.chunks = 0

    def on_data(self, chunk):
        self.types.add(type(chunk).__name__)
        self.data += chunk
        self.chunks += 1

    def on_lost(self, why):
        self.lost.append(why)

    def wait_for(self, needle, timeout, start=0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if bytes(self.data).find(needle, start) >= 0:
                return True
            time.sleep(0.02)
        return False


def _boots(data, markers):
    return [m for m in markers if m.encode() in data]


def _transport(port, sink):
    from serial_transport import SerialTransport
    return SerialTransport(port, sink.on_data, sink.on_lost)


def _probe(ctx, sink, t):
    """Send the args' probe line through `t` and wait for its reply -> True when it came."""
    start = len(sink.data)
    t.write(ctx.args["probe"].encode())
    return sink.wait_for(ctx.args["reply"].encode(), 4.0, start)


@case("open and close the port repeatedly: nothing resets, nothing is lost, and the link still answers", group="no_reset")
def no_reset(ctx):
    port, cycles, dwell = ctx.args["port"], int(ctx.args.get("cycles", 10)), float(ctx.args.get("dwell", 1.5))
    sink = _Sink()
    per = []
    for i in range(cycles):
        before = len(sink.data)
        t = _transport(port, sink)
        t.open()
        check(t.is_open, f"cycle {i + 1}: open() returned but is_open is False")
        time.sleep(dwell)
        t.close()
        per.append(len(sink.data) - before)
    t = _transport(port, sink)
    t.open()
    try:
        answered = _probe(ctx, sink, t)
    finally:
        t.close()
    ctx.note(f"{cycles} open/close cycles of {dwell:g} s: bytes received per cycle {per}")
    check(sink.types <= {"bytes"}, f"on_data received {sorted(sink.types)}, not only bytes (contract 1)")
    check(not sink.lost, f"the transport reported a loss: {sink.lost[:2]}")
    boots = _boots(bytes(sink.data), ctx.args["boot"])
    check(not boots, f"the board printed boot lines ({boots}) while the port was opened and closed: an open reset it")
    check(answered, f"after the cycles the board did not answer {ctx.args['probe'].strip()!r} through the transport")


@case("the SerialTransport contract on a live WCB", group="contract")
def contract(ctx):
    from transport import TransportError
    port, marker = ctx.args["port"], ctx.args["marker"]
    text = marker + ctx.args.get("utf8", "")
    sink = _Sink()
    t = _transport(port, sink)
    t.open()
    try:
        t.write(f";S0,{text}\r".encode("utf-8"))
        got = sink.wait_for(text.encode("utf-8"), 4.0)
        if not got:
            head = bytes(sink.data).find(marker.encode())
            tail = bytes(sink.data)[head:head + len(text.encode()) + 8] if head >= 0 else b""
            raise AssertionError(f"';S0,{marker}...' did not come back byte-exact: the marker "
                                 f"{'arrived followed by ' + tail[len(marker):].hex() if head >= 0 else 'never arrived'}"
                                 f", expected {text.encode()[len(marker):].hex()}")
        other = _transport(port, _Sink())
        try:
            other.open()
            other.close()
            raise AssertionError("a second SerialTransport opened a port the first one holds")
        except TransportError:
            pass
        mark = len(sink.data)
        t.set_signals(dtr=False, rts=False)
        time.sleep(2.5)
        boots = _boots(bytes(sink.data)[mark:], ctx.args["boot"])
        check(not boots, f"set_signals(dtr=False, rts=False) reset the board ({boots})")
        check(_probe(ctx, sink, t), "the board stopped answering after set_signals(False, False)")
    finally:
        t.close()
    check(not t.is_open, "is_open is True after close()")
    for what, fn in (("write", lambda: t.write(b"?VERSION\r")),
                     ("set_signals", lambda: t.set_signals(dtr=False, rts=False))):
        try:
            fn()
            raise AssertionError(f"{what} after close() did not raise (contract 2: a failing write is the only "
                                 f"liveness proof the tools have)")
        except TransportError:
            pass
    t.close()
    check(sink.types <= {"bytes"}, f"on_data received {sorted(sink.types)}, not only bytes (contract 1)")
    check(not sink.lost, f"a loss was reported on a healthy port: {sink.lost[:2]}")


class _Order:
    """Stands in for the transport's pyserial handle and records DTR/RTS writes in order; everything else passes through."""

    def __init__(self, ser):
        object.__setattr__(self, "_s", ser)
        object.__setattr__(self, "order", [])

    def __getattr__(self, k):
        return getattr(object.__getattribute__(self, "_s"), k)

    def __setattr__(self, k, v):
        if k in ("dtr", "rts"):
            self.order.append((k, v))
        setattr(object.__getattribute__(self, "_s"), k, v)


@case("characterisation: set_signals(rts=True) then (rts=False) resets the board, DTR written before RTS",
      group="signals_reset")
def signals_reset(ctx):
    sink = _Sink()
    t = _transport(ctx.args["port"], sink)
    t.open()
    try:
        rec = _Order(t._ser)
        t._ser = rec
        mark = len(sink.data)
        t.set_signals(dtr=False, rts=True)
        time.sleep(0.25)
        t.set_signals(dtr=False, rts=False)
        done = sink.wait_for(ctx.args["ready"].encode(), 20.0, mark)
        boots = _boots(bytes(sink.data)[mark:], ctx.args["boot"])
    finally:
        t.close()
    ctx.note(f"control-line writes in order: {rec.order}; boot lines seen: {boots}; setup finished: {done}")
    check(rec.order[:2] == [("dtr", False), ("rts", True)],
          f"set_signals wrote {rec.order[:2]}: DTR is written before RTS (serial_transport.py:108-119)")
    check(boots, "RTS asserted and released through set_signals did not reset the board: the page's setSignals() "
                 "would not reach the auto-reset circuit")
    check(done, f"the board reset but never printed {ctx.args['ready']!r} within 20 s")


@case("a NaviCore REBOOT through the transport: the handle dies and the loss is reported, or it survives and the "
      "board comes back on it", group="device_loss")
def device_loss(ctx):
    from transport import TransportError
    sink = _Sink()
    t = _transport(ctx.args["port"], sink)
    t.open()
    try:
        check(_probe(ctx, sink, t), "NaviCore did not answer a PING before the REBOOT")
        mark = len(sink.data)
        t.write(b'{"type":"REBOOT"}\n')
        check(sink.wait_for(b'"type":"ACK"', 4.0, mark), "no ACK to REBOOT")
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline and not sink.lost and not _boots(bytes(sink.data)[mark:], ctx.args["boot"]):
            time.sleep(0.1)
        if sink.lost:
            ctx.note(f"the COM handle did not survive the REBOOT: on_lost fired ({len(sink.lost)} report(s))")
            check(not t.is_open, "on_lost fired but is_open is still True (the reader must close before reporting)")
            try:
                t.write(b'{"type":"PING"}\n')
                raise AssertionError("a write after the loss did not raise (contract 2)")
            except TransportError:
                pass
        else:
            boots = _boots(bytes(sink.data)[mark:], ctx.args["boot"])
            check(boots, "within 25 s of the REBOOT neither a loss nor a boot banner arrived")
            ctx.note(f"the COM handle survived the REBOOT: boot lines {boots} arrived on the same transport")
            check(t.is_open, "the handle survived, but is_open is False")
            came = False
            for _ in range(10):
                if _probe(ctx, sink, t):
                    came = True
                    break
                time.sleep(1.0)
            check(came, "the handle survived the REBOOT but NaviCore never answered a PING on it")
    finally:
        t.close()
    check(sink.types <= {"bytes"}, f"on_data received {sorted(sink.types)}, not only bytes (contract 1)")


if __name__ == "__main__":
    sys.exit(_ixpy.main())
