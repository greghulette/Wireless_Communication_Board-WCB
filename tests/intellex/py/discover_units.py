"""discover.identify_serial against a fake serial port, under Intellex's venv. No port is opened: pyserial's Serial is
replaced, in this process only, by a class that answers the PING from a script.

group "direct_pong_should" (intellex.identify_direct_pong, a (should) test for plan finding 3): identify_serial's own
docstring says ONLY A DIRECT PONG COUNTS (Intellex src/discover.py:153-157) - a USB-tethered doorway prints the mesh's
JSON, a relayed PONG included - and probe() tells the two apart (:347-359), but identify_serial takes any line whose type
is PONG (:217-218). Low impact while the launcher asks only Espressif native-USB ports (launcher.html:805), which a WCB
doorway is not. Two controls pass today: a direct PONG identifies the port, a busy port reads as busy, and a port
outside INTELLEX_SERIAL_ALLOW is never opened (H2, discover.py:165-166).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import case, check  # noqa: E402


class _FakeSerial:
    """What identify_serial uses of serial.Serial: set lines and settings, open, write, read, flush, close. The reply to
    the first write comes from `reply`; `fail_open` raises from open()."""
    reply = b""
    fail_open = None
    opened = []

    def __init__(self, *a, **k):
        self.port = self.baudrate = self.timeout = None
        self.dtr = self.rts = True
        self._out = b""

    def open(self):
        _FakeSerial.opened.append(self.port)
        if _FakeSerial.fail_open:
            raise _FakeSerial.fail_open

    def write(self, data):
        if b'"PING"' in data:
            self._out += _FakeSerial.reply
        return len(data)

    def read(self, n):
        out, self._out = self._out[:n], self._out[n:]
        return out

    def reset_input_buffer(self):
        pass

    def flush(self):
        pass

    def close(self):
        pass


def _identify(port, reply=b"", fail_open=None):
    import serial
    import discover
    real = serial.Serial
    _FakeSerial.reply, _FakeSerial.fail_open, _FakeSerial.opened = reply, fail_open, []
    serial.Serial = _FakeSerial
    try:
        return discover.identify_serial(port, timeout=0.6), list(_FakeSerial.opened)
    finally:
        serial.Serial = real


@case("control: a direct PONG on the port identifies a NaviCore", group="direct_pong_should")
def direct(ctx):
    os.environ["INTELLEX_SERIAL_ALLOW"] = "COMFAKE"
    got, opened = _identify("COMFAKE", b'{"type":"PONG","version":"v0.2.0_hil"}\n')
    check(opened == ["COMFAKE"], f"opened {opened}")
    check(got == {"version": "v0.2.0_hil", "busy": False}, f"a direct PONG: {got}")


@case("(should) a PONG relayed over the mesh (it carries sys and id) does not identify the port as a NaviCore",
      group="direct_pong_should")
def relayed(ctx):
    os.environ["INTELLEX_SERIAL_ALLOW"] = "COMFAKE"
    got, _ = _identify("COMFAKE", b'{"sys":1,"type":"rc_hb","id":20,"fw":"v0.2.0_hil"}\n'
                                  b'{"sys":1,"type":"PONG","id":20,"version":"v0.2.0_hil"}\n')
    check(got.get("version") is None, f"a mesh-relayed PONG was taken for a NaviCore on the port: {got} "
                                      f"(discover.py:217-218 accepts any PONG; its docstring, :153-157, says only a "
                                      f"direct one counts, as probe() does, :347-359)")


@case("a port another program holds reads as busy; one outside INTELLEX_SERIAL_ALLOW is never opened",
      group="direct_pong_should")
def busy_and_leash(ctx):
    os.environ["INTELLEX_SERIAL_ALLOW"] = "COMFAKE"
    got, _ = _identify("COMFAKE", fail_open=PermissionError(13, "Access is denied."))
    check(got == {"version": None, "busy": True}, f"a port held elsewhere: {got}")
    got, opened = _identify("COMOTHER", b'{"type":"PONG","version":"v0"}\n')
    check(opened == [] and got == {"version": None, "busy": False},
          f"a port outside INTELLEX_SERIAL_ALLOW: opened {opened}, answered {got}")


if __name__ == "__main__":
    sys.exit(_ixpy.main())
