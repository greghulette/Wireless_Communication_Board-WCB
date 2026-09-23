"""Line-oriented serial transport for bench devices.

Opens a port WITHOUT asserting DTR/RTS, so attaching to a board never auto-resets it (the
CP210x/CH9102 auto-reset circuits and the S3's USB-Serial/JTAG both treat those lines as
EN/GPIO0). A reader thread timestamps every received line; tests mark a position, act, and
expect() a regex after the mark, so output that arrives before the command can't satisfy it.

Two failure modes are handled here rather than in every test (both cost a whole run on 2026-09-21/22):
- A write never blocks forever. A native-USB board that is not draining its endpoint (booting, busy)
  never accepts the bytes, and an unbounded write()+flush() froze a GUI run for ten minutes while it
  still held the port. Writes now time out and raise a test failure.
- A port that vanishes is reopened. When a board's USB drops or re-enumerates, the reader used to log
  "<<serial error>>" and exit for good, so every later test on that device errored. It now reopens the
  same port (DTR/RTS still deasserted) until it comes back or the device is closed.
"""
import re
import threading
import time

import serial


class ExpectTimeout(AssertionError):
    pass


class SerialDevice:
    REOPEN_EVERY_S = 0.5      # how often the reader retries a vanished port
    SEND_WAIT_S = 5.0         # how long send() waits for a reopen before failing the test

    def __init__(self, name, port, baud=115200, log=None):
        self.name = name
        self.port = port
        self.baud = baud
        self.log = log            # callable(name, direction, text) or None
        self.lines = []           # [(monotonic_time, text)]
        self._partial = bytearray()
        self._cv = threading.Condition()
        self._ser = None
        self._stop = False
        self._thread = None
        self._wlock = threading.Lock()   # one writer at a time; also held while the handle is swapped
        self.last_error_at = None        # monotonic time the port last failed - runner.host_usb_loss() compares these

    # ------------------------------------------------------------ lifecycle
    def _open_port(self):
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = self.port, self.baud, 0.05
        # Set ONCE, here. Assigning write_timeout on an open port makes pyserial re-run _reconfigure_port()
        # (SetCommTimeouts + SetCommMask + a full SetCommState) under the reader thread's in-flight ReadFile; doing
        # that before every send stalled and lost input on the S3 native-USB ports (NaviCore's GET_CONFIG replies
        # arrived ~10 s late or not at all, 2026-09-22). 2 s covers ~23 KB at 115200 - far above any line sent.
        s.write_timeout = 2.0
        s.dtr = False
        s.rts = False
        s.open()
        return s

    def open(self):
        self._ser = self._open_port()
        self._stop = False
        self._thread = threading.Thread(target=self._reader, name=f"rx-{self.name}", daemon=True)
        self._thread.start()
        return self

    def set_baud(self, baud):
        """Change the rate of the open port in place, for ?OTALOCAL,BAUD. pyserial reconfigures the open handle and
        keeps the DTR/RTS state it was opened with, so the board is not reset; bytes in flight are lost."""
        self.baud = baud
        if self._ser:
            self._ser.baudrate = baud

    def close(self):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=2)     # the reopen loop checks _stop every REOPEN_EVERY_S
        with self._wlock:
            if self._ser:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _reader(self):
        while not self._stop:
            try:
                chunk = self._ser.read(4096)
            except (serial.SerialException, OSError, AttributeError) as e:
                if self._stop:
                    return
                self.last_error_at = time.monotonic()
                self._append(f"<<serial error: {e}>>")
                if not self._reopen():
                    return
                continue
            if not chunk:
                continue
            self._partial += chunk
            while b"\n" in self._partial:
                raw, _, rest = self._partial.partition(b"\n")
                self._partial = bytearray(rest)
                self._append(raw.rstrip(b"\r").decode("utf-8", errors="replace"))

    def _reopen(self):
        """The port vanished (USB dropped / re-enumerated). Drop the dead handle and reopen the same port until it
        answers or the device is closed. Returns False only when close() asked the reader to stop."""
        with self._wlock:
            try:
                if self._ser:
                    self._ser.close()
            except Exception:
                pass
            self._ser = None
        self._partial = bytearray()
        while not self._stop:
            time.sleep(self.REOPEN_EVERY_S)
            try:
                s = self._open_port()
            except (serial.SerialException, OSError):
                continue
            with self._wlock:
                self._ser = s
            self._append(f"<<reopened {self.port}>>")
            return True
        return False

    @property
    def active(self):
        """Opened and not closed (a port handed to Chrome for a Wizard test is closed, not active)."""
        return self._thread is not None and not self._stop

    @property
    def connected(self):
        """Active and holding a live handle - False while the reader is waiting to reopen a vanished port."""
        return self.active and self._ser is not None

    def _append(self, text):
        with self._cv:
            self.lines.append((time.monotonic(), text))
            self._cv.notify_all()
        if self.log:
            self.log(self.name, "<", text)

    # ------------------------------------------------------------ I/O
    def send(self, text, eol="\n"):
        if self.log:
            self.log(self.name, ">", text)
        data = text.encode() + eol.encode()
        deadline = time.monotonic() + self.SEND_WAIT_S
        while True:
            with self._wlock:
                ser = self._ser
                if ser is not None:
                    try:
                        ser.write(data)          # bounded by the write_timeout set in _open_port()
                        return
                    except serial.SerialTimeoutException:
                        raise ExpectTimeout(f"{self.name}: write to {self.port} timed out - the board is not "
                                            f"draining its port (booting, hung, or USB stalled)")
                    except (serial.SerialException, OSError) as e:
                        # The reader thread notices too and reopens; wait for it below.
                        self._append(f"<<write error: {e}>>")
            if time.monotonic() > deadline:
                raise ExpectTimeout(f"{self.name}: {self.port} is gone and did not come back within "
                                    f"{self.SEND_WAIT_S:.0f}s")
            time.sleep(0.1)

    def mark(self):
        with self._cv:
            return len(self.lines)

    def since(self, mark):
        with self._cv:
            return [t for _, t in self.lines[mark:]]

    def expect(self, pattern, timeout=3.0, since=None):
        """Wait for a line matching `pattern` at or after `since`; return the re.Match."""
        rx = re.compile(pattern)
        start = self.mark() if since is None else since
        deadline = time.monotonic() + timeout
        i = start
        with self._cv:
            while True:
                while i < len(self.lines):
                    m = rx.search(self.lines[i][1])
                    if m:
                        return m
                    i += 1
                left = deadline - time.monotonic()
                if left <= 0:
                    tail = "\n    ".join(t for _, t in self.lines[max(start, len(self.lines) - 15):])
                    raise ExpectTimeout(f"{self.name}: no line matching /{pattern}/ within {timeout}s; "
                                        f"last lines:\n    {tail}")
                self._cv.wait(left)

    def expect_none(self, pattern, window=2.0, since=None):
        """Assert that no line matching `pattern` appears within `window` seconds."""
        rx = re.compile(pattern)
        start = self.mark() if since is None else since
        time.sleep(window)
        for text in self.since(start):
            if rx.search(text):
                raise AssertionError(f"{self.name}: unexpected line matching /{pattern}/: {text}")

    def collect(self, window, since=None):
        start = self.mark() if since is None else since
        time.sleep(window)
        return self.since(start)
