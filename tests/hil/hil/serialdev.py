"""Line-oriented serial transport for bench devices.

Opens a port WITHOUT asserting DTR/RTS, so attaching to a board never auto-resets it (the
CP210x/CH9102 auto-reset circuits and the S3's USB-Serial/JTAG both treat those lines as
EN/GPIO0). A reader thread timestamps every received line; tests mark a position, act, and
expect() a regex after the mark, so output that arrives before the command can't satisfy it.
"""
import re
import threading
import time

import serial


class ExpectTimeout(AssertionError):
    pass


class SerialDevice:
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

    # ------------------------------------------------------------ lifecycle
    def open(self):
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = self.port, self.baud, 0.05
        s.dtr = False
        s.rts = False
        s.open()
        self._ser = s
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
            self._thread.join(timeout=1)
        if self._ser:
            self._ser.close()
            self._ser = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _reader(self):
        while not self._stop:
            try:
                chunk = self._ser.read(4096)
            except serial.SerialException as e:
                self._append(f"<<serial error: {e}>>")
                return
            if not chunk:
                continue
            self._partial += chunk
            while b"\n" in self._partial:
                raw, _, rest = self._partial.partition(b"\n")
                self._partial = bytearray(rest)
                self._append(raw.rstrip(b"\r").decode("utf-8", errors="replace"))

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
        self._ser.write(text.encode() + eol.encode())
        self._ser.flush()

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
