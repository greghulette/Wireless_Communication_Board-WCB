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
- A port that goes silent is reopened too, where that is safe (revive_silent: NaviCore). In full run
  20261006-122850 the handle on NaviCore's native-USB port stopped delivering bytes, with no error, for
  up to half an hour while NaviCore itself answered over WiFi; three tests failed in a row until a
  hand-off closed and reopened the port. An expect() that times out on a port which gave no byte at all
  since the last command now has the reader reopen it, and says so in the failure.
"""
import os
import re
import threading
import time

import serial


class ExpectTimeout(AssertionError):
    pass


class SerialDevice:
    REOPEN_EVERY_S = 0.5      # how often the reader retries a vanished port
    SEND_WAIT_S = 5.0         # how long send() waits for a reopen before failing the test
    SILENT_S = 2.5            # no byte at all for this long after a command: a silent port (revive_silent)
    REVIVE_WAIT_S = 5.0       # how long a timed-out expect() waits for the reader to reopen a silent port
    OPEN_TIMEOUT_S = 15.0     # an open that has not returned by then is a hung chip or driver (_open_port)

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
        self._hold_until = 0.0           # hold_reads(): the reader reads nothing until this monotonic time
        self.revive_silent = False       # reopen the port when it goes silent after a command (Bench.dev: NaviCore)
        self.last_tx_at = None           # monotonic time of the last write that went out
        self.last_rx_at = None           # ... and of the last byte read
        self.revived = 0                 # silent-port reopens so far
        self._revive_req = False         # set by expect(), served by the reader thread
        self._open_pending = None        # the helper thread of an open that never returned (_open_port)

    # ------------------------------------------------------------ lifecycle
    def _open_port(self):
        """_open_port_now, given OPEN_TIMEOUT_S. On 2026-10-08 (run 20261008-073116) the open of WCB1's CH343 port
        after a Wizard test never returned: its chip had hung, the WCH driver's open waited on it in the kernel, and the
        GUI froze for half an hour. The open runs on a helper thread instead; one that has not returned raises, with
        last_error_at set so the runner treats the board as lost (host_usb_loss, then its outage pause), and the stuck
        thread is left behind - nothing can cancel the call - to close the port if the open ever does return. While it
        is stuck, every further open of this device raises at once rather than leaving another thread behind it."""
        pend = self._open_pending
        if pend is not None and pend.is_alive():
            self.last_error_at = time.monotonic()
            raise serial.SerialException(f"{self.name}: an earlier open of {self.port} has still not returned - its "
                                         f"USB-serial chip or driver is hung; unplug and replug it")
        box, lock = {}, threading.Lock()

        def work():
            try:
                s = self._open_port_now()
            except Exception as e:  # noqa: BLE001 - handed to the caller below
                with lock:
                    box["err"] = e
                return
            with lock:
                if not box.get("abandoned"):
                    box["ser"] = s
                    return
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass

        t = threading.Thread(target=work, name=f"open-{self.name}", daemon=True)
        t.start()
        t.join(self.OPEN_TIMEOUT_S)
        with lock:
            if "ser" in box:
                return box["ser"]
            if "err" in box:
                raise box["err"]
            box["abandoned"] = True
        self._open_pending = t
        self.last_error_at = time.monotonic()
        raise serial.SerialException(f"{self.name}: opening {self.port} has not returned in {self.OPEN_TIMEOUT_S:.0f} s "
                                     f"- its USB-serial chip or driver is hung; unplug and replug it")

    def _open_port_now(self):
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = self.port, self.baud, 0.05
        # Set ONCE, here. Assigning write_timeout on an open port makes pyserial re-run _reconfigure_port()
        # (SetCommTimeouts + SetCommMask + a full SetCommState) under the reader thread's in-flight ReadFile; doing
        # that before every send stalled and lost input on the S3 native-USB ports (NaviCore's GET_CONFIG replies
        # arrived ~10 s late or not at all, 2026-09-22). 2 s covers ~23 KB at 115200 - far above any line sent.
        s.write_timeout = 2.0
        if os.name == "nt":
            s.dtr = False
            s.rts = False
            s.open()
            return s
        # macOS and Linux raise DTR and RTS on open, and pyserial then lowers DTR before RTS: RTS alone is the
        # auto-reset circuit's (and the S3 USB-Serial/JTAG's) reset, so every open rebooted the board - Find devices'
        # ?VERSION landed in its boot log (measured on a CP2102N WCB, 2026-10-05). RTS goes low at the open and DTR
        # after it: DTR alone only straps IO0, read at a reset that never comes. exclusive=True is Windows' one owner
        # per COM port: without it a second process (run.py beside the GUI) opens the port too and takes half its lines.
        s.exclusive = True
        s.rts = False
        s.open()
        s.dtr = False
        return s

    def open(self):
        """Open the port and start the reader. Also reopens a closed device (Bench.dev reuses one after a hand-off):
        the previous reader must be gone first - two on one port would split its lines - and a partial line from
        before the close must not prefix the first one after it."""
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise serial.SerialException(f"{self.name}: the previous reader of {self.port} has not stopped")
        self._ser = self._open_port()
        self._partial = bytearray()
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

    def hold_reads(self, seconds):
        """Read nothing from the port for `seconds`: a host that stalls, so the OS buffer and then the device's own TX
        buffer fill - for a test of what the device does then (NAVICORE.md D-NC75). Nothing is lost on this side: the
        bytes wait in the port until the reader resumes."""
        self._hold_until = time.monotonic() + seconds

    def _reader(self):
        while not self._stop:
            if self._revive_req:
                self._revive_req = False
                self._append(f"<<silent: no byte since the last command; reopening {self.port}>>")
                if not self._reopen():
                    return
                continue
            if self._hold_until > time.monotonic():
                time.sleep(0.01)
                continue
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
            self.last_rx_at = time.monotonic()
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
                        self.last_tx_at = time.monotonic()
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

    def send_paced(self, text, chunk=512, gap_s=0.004, eol="\n"):
        """send() for a line longer than a board's USB receive ring takes at once: `chunk` bytes at a time, `gap_s`
        apart, the way NaviCore's config tool writes a SET_CONFIG or SET_CMDLIB (sendLine, NaviCore
        config_tool/index.html: USB_CHUNK 512, 4 ms). A line of `chunk` bytes or less goes out as send() sends it. The
        write lock is held across the whole line, so no other send lands inside it. A write that fails part-way raises
        at once and is never resumed: the board already holds the front of the line, and the rest sent later would
        join a different line."""
        data = text.encode() + eol.encode()
        if len(data) <= chunk:
            return self.send(text, eol)
        if self.log:
            self.log(self.name, ">", text)
        deadline = time.monotonic() + self.SEND_WAIT_S
        while True:
            with self._wlock:
                ser = self._ser
                if ser is not None:
                    done = 0
                    try:
                        while done < len(data):
                            ser.write(data[done:done + chunk])
                            done = min(done + chunk, len(data))
                            time.sleep(gap_s)       # the board's loop() drains its RX ring between chunks
                        self.last_tx_at = time.monotonic()
                        return
                    except serial.SerialTimeoutException:
                        raise ExpectTimeout(f"{self.name}: write to {self.port} timed out {done} bytes into a "
                                            f"{len(data)}-byte paced line - the board is not draining its port")
                    except (serial.SerialException, OSError) as e:
                        self._append(f"<<write error: {e}>>")
                        raise ExpectTimeout(f"{self.name}: {self.port} failed {done} bytes into a {len(data)}-byte "
                                            f"paced line ({e})")
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
                    break
                self._cv.wait(left)
        # Outside the lock: the reader takes it to log its reopen.
        raise ExpectTimeout(f"{self.name}: no line matching /{pattern}/ within {timeout}s; "
                            f"last lines:\n    {tail}" + self._revive_if_silent())

    def silent_since_send(self):
        """revive_silent is set, a command went out SILENT_S or more ago, and the port has given no byte since."""
        tx, rx = self.last_tx_at, self.last_rx_at
        return (self.revive_silent and tx is not None and (rx is None or rx < tx)
                and time.monotonic() - tx >= self.SILENT_S)

    def _revive_if_silent(self):
        """After an expect() timed out: on a silent port (silent_since_send), have the reader reopen it, wait up to
        REVIVE_WAIT_S for that -> a note for the failure, '' when the port was not silent. The command's reply is
        lost either way; the reopen is for the commands after it."""
        if not self.silent_since_send():
            return ""
        quiet = time.monotonic() - self.last_tx_at
        m = self.mark()
        self._revive_req = True
        end = time.monotonic() + self.REVIVE_WAIT_S
        while time.monotonic() < end and not any(t.startswith("<<reopened") for t in self.since(m)):
            time.sleep(0.05)
        self.revived += 1
        done = any(t.startswith("<<reopened") for t in self.since(m))
        return (f"\n    ({self.port} gave no byte for {quiet:.1f} s after the command: "
                + ("reopened it" if done else f"asked the reader to reopen it, not done in {self.REVIVE_WAIT_S:.0f} s")
                + f"; silent-port reopen {self.revived} on this device)")

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


def usb_jtag_reset(dev, hold_s=0.2):
    """Reset an ESP32-S3 into its app through its native USB-Serial/JTAG port (NaviCore, the SBUS controller): RTS=1
    with DTR=0 holds the chip in reset, and DTR stays 0, so it boots the app, not download mode. On Windows,
    usbser.sys sends SET_CONTROL_LINE_STATE only when DTR is written, so an RTS change alone never reaches the board:
    DTR is re-written after every RTS change, as esptool's _setRTS does (esptool/reset.py:72-77). The port usually
    drops as the chip resets; the reader reopens it (<<reopened>>). docs/HIL_TESTING.md §2."""
    # A local handle: if the USB re-enumerates, the reader's _reopen() sets dev._ser = None before the release below.
    # A closed handle is harmless - pyserial skips the hardware call once is_open is false.
    s = dev._ser
    if s is None:
        raise ExpectTimeout(f"{dev.name}: {dev.port} is not open - no reset pulse sent")
    s.rts = True
    s.dtr = False
    time.sleep(hold_s)
    try:
        s.rts = False
        s.dtr = False
    except (serial.SerialException, OSError):
        pass    # the port went away with the reset; the reader reopens it
