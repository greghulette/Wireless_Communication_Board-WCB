"""NaviCore's WebSocket endpoint as a console device (docs/hil_plan/NAVICORE.md INF5). NcWs(ip) speaks RFC 6455 to
ws://<ip>/ws through hil/ws.py's WsClient and offers SerialDevice's line interface - name, port, lines [(t, text)],
mark(), since(), expect(), send(), log - so the INF1 driver runs over it as it runs over USB: NaviCore(NcWs(ip)) beside
NaviCore(bench.dev("navicore")), and hil/wcb.py's Pull reads a relay's console through it.

What the endpoint is (NaviCore navicore_wsserver.h, the hil-week tree):
- Inbound, each text frame is appended to that socket's own line buffer and every CR- or LF-ended line in it is queued
  for loop() (wsHandler :419-472): a line may span frames, a frame may hold several lines, an empty line is skipped.
  Unlike USB (handleSerialInput trims every line, NaviCore.ino:4299), a line runs exactly as it was sent (D-NC61).
- Outbound, the socket is a console mirror, not a reply channel: whatever the loop core prints while any client is
  connected is buffered (WsSink, 2 KB) and sent to EVERY connected client, cut at whole UTF-8 characters (:117-260);
  PWM_UPDATE, rc_trig and the dispatch trace reach it even when USB has no room (printlnDirect, writeDirect :296-325).
  So a NcWs also sees the replies to what is sent over USB or another socket, and NaviCore's USB console sees the
  replies to what is sent here: mark() before sending, as on USB, and where both transports are in play take a
  barrier first (s45 _sync).
- A line from a socket runs from loop() one per pass, in order (drain :512-558), so a '#L12' sent after a command still
  follows its reply; the driver's pokes are harmless here.
- There is no send_paced() on purpose: NaviCore.send_paced then sends a long line whole, in one frame, which is what the
  endpoint takes (a frame may be up to 98303 bytes, :406-408).

The socket is read on its own thread, as SerialDevice reads a port, so a test that is busy elsewhere never leaves the
board's sends blocked on a full TCP window; pause() stops the reading on purpose (s45's stalled client). The thread
answers the board's PING, records its PONGs (the server answers a client's PING itself, from the httpd task,
navicore_wsserver.h:181-195), counts every frame by opcode, and records a close. Sends and the reader's pongs are one
frame each, serialised by a lock. Sends block at most `timeout` s; reads wait in select(), so the two never share a
socket timeout.

Every line in and out is logged through `log` (Bench.log's signature) with checkpoint.redact_text applied here:
Bench.log redacts by device kind, and this is no bench device (INF3: the WebSocket path needs the same filter). An
ExpectTimeout's tail never quotes a CONFIG line, which carries the AP's SSID in clear (redact_text leaves wifiSsid
alone, D49).
"""
import os
import re
import select
import socket
import struct
import threading
import time

from .checkpoint import redact_text
from .serialdev import ExpectTimeout
from .ws import WsClient, frame, parse

CLOSED_MARK = "<<ws closed"     # the line appended when the socket ends, as SerialDevice appends '<<serial error'
LOG_CAP = 4096                  # a longer line is logged as its head and its length (the 98 KB framing frames)
TEXT, BINARY, CONT, CLOSE, PING, PONG = 0x1, 0x2, 0x0, 0x8, 0x9, 0xA


def shown(text, cap=160):
    """A received line for a failure message: a CONFIG line only as its length (it carries the AP's SSID, which
    redact_text leaves alone, and the passwords), anything else redacted and cut to `cap` characters."""
    if '"wifiSsid"' in text or text.startswith('{"type":"CONFIG"'):
        return f"<a CONFIG line of {len(text)} characters, not quoted>"
    t = redact_text(text)
    return t if len(t) <= cap else t[:cap] + f"... ({len(t)} characters)"


class NcWs:
    def __init__(self, ip, name="ncws", log=None, port=80, timeout=5.0, rcvbuf=None):
        self.name, self.ip = name, ip
        self.port = f"ws://{ip}/ws" if port == 80 else f"ws://{ip}:{port}/ws"
        self._raw_log = log
        self.log = self._redacted_log if log else None   # what hil/wcb.py dev_note calls too
        self.lines = []            # [(monotonic time, text)], as SerialDevice.lines
        self.frames = []           # [(monotonic time, payload bytes)]: every TEXT frame as it arrived
        self.pongs = []            # [(monotonic time, payload bytes)]: the board's answers to ping()
        self.opcodes = {}          # opcode -> how many frames of it arrived
        self.closed = None         # why the socket ended, once it has
        self.close_code = None     # the close frame's status code, when the board sent one
        self._partial = bytearray()
        self._cv = threading.Condition()
        self._wlock = threading.Lock()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self.ws = WsClient(ip, port=port, timeout=timeout, rcvbuf=rcvbuf)
        self.ws.sock.settimeout(timeout)
        self._thread = threading.Thread(target=self._run, name=f"rx-{name}", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ the reader
    def _run(self):
        sock, buf = self.ws.sock, bytes(self.ws.buf)      # ws.buf: what arrived behind the handshake
        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(0.02)
                continue
            got = parse(buf)
            if got:
                op, payload, used = got
                buf = buf[used:]
                if not self._on_frame(op, payload):
                    return
                continue
            try:
                ready, _, _ = select.select([sock], [], [], 0.2)
                if not ready:
                    continue
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            except (OSError, ValueError) as e:
                if not self._stop.is_set():
                    self._end(f"{type(e).__name__}: {e}")
                return
            if not chunk:
                if not self._stop.is_set():
                    self._end("the board closed the connection")
                return
            buf += chunk

    def _on_frame(self, op, payload):
        """One frame from the board -> False once the socket is over (a close frame)."""
        t = time.monotonic()
        self.opcodes[op] = self.opcodes.get(op, 0) + 1
        if op == TEXT:
            self.frames.append((t, payload))
        if op in (TEXT, BINARY, CONT):
            self._partial += payload
            while b"\n" in self._partial:
                raw, _, rest = self._partial.partition(b"\n")
                self._partial = bytearray(rest)
                self._append(raw.rstrip(b"\r").decode("utf-8", errors="replace"), t)
        elif op == PING:
            try:
                with self._wlock:
                    self.ws.sock.sendall(frame(payload, opcode=PONG))
            except OSError:
                pass
        elif op == PONG:
            self.pongs.append((t, payload))
        elif op == CLOSE:
            self.close_code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else None
            self._end("the board sent a close frame" + (f" (code {self.close_code})" if self.close_code else ""))
            return False
        return True

    def _append(self, text, t=None):
        with self._cv:
            self.lines.append((time.monotonic() if t is None else t, text))
            self._cv.notify_all()
        self._log("<", text)

    def _end(self, reason):
        with self._cv:
            if self.closed is not None:
                return
            self.closed = reason
        self._append(f"{CLOSED_MARK}: {reason}>>")

    def _redacted_log(self, name, direction, text):
        """Bench.log's signature, credentials hashed, THEN cut: cut first, a GET_CONFIG line's wifiPassword value (about
        characters 165-228) could lose the closing quote redact_text needs and reach session.log in part."""
        t = redact_text(text)
        self._raw_log(name, direction, t if len(t) <= LOG_CAP else f"{t[:200]}... ({len(text)} characters)")

    def _log(self, direction, text):
        if self.log:
            self.log(self.name, direction, text)

    # ------------------------------------------------------------ writes
    def _write(self, data):
        if self.closed is not None:
            raise ExpectTimeout(f"{self.name}: {self.port} is closed ({self.closed})")
        try:
            with self._wlock:
                self.ws.sock.sendall(data)
        except socket.timeout:
            raise ExpectTimeout(f"{self.name}: a write to {self.port} timed out: the board is not reading its socket")
        except OSError as e:
            self._end(f"{type(e).__name__}: {e}")
            raise ExpectTimeout(f"{self.name}: {self.port} failed a write ({e})")

    def send(self, text, eol="\n"):
        """One line, `eol` included, as one TEXT frame (SerialDevice.send's signature)."""
        self._log(">", text)
        self._write(frame((text + eol).encode("utf-8")))

    def send_frames(self, pieces, gap_s=0.0):
        """Each of `pieces` (str or bytes, nothing appended) as its own TEXT frame, `gap_s` apart: a line split over
        frames, several lines in one frame, a line with no end yet."""
        for k, p in enumerate(pieces):
            data = p.encode("utf-8") if isinstance(p, str) else bytes(p)
            self._log(">", f"[frame {k + 1}/{len(pieces)}] " + data.decode("utf-8", errors="replace"))
            self._write(frame(data))
            if gap_s and k + 1 < len(pieces):
                time.sleep(gap_s)

    def send_frame(self, payload, opcode=TEXT):
        """One frame of `payload` (bytes) and `opcode`, logged by its length only."""
        self._log(">", f"[a {len(payload)}-byte frame, opcode {opcode}]")
        self._write(frame(bytes(payload), opcode=opcode))

    def ping(self, payload=b""):
        """A PING frame; the board's PONG lands in self.pongs. -> the host time it was sent."""
        t = time.monotonic()
        self._write(frame(bytes(payload), opcode=PING))
        return t

    # ------------------------------------------------------------ reading, as SerialDevice
    def mark(self):
        with self._cv:
            return len(self.lines)

    def since(self, mark):
        with self._cv:
            return [t for _, t in self.lines[mark:]]

    def frame_mark(self):
        return len(self.frames)

    def frames_since(self, fmark):
        return [p for _, p in self.frames[fmark:]]

    def _tail(self, start):
        return "\n    ".join(shown(t) for _, t in self.lines[max(start, len(self.lines) - 15):])

    def expect(self, pattern, timeout=3.0, since=None):
        """Wait for a line matching `pattern` at or after `since` -> the re.Match. ExpectTimeout on a timeout, and at
        once when the socket has ended and no line matched (nothing more can arrive)."""
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
                if self.closed is not None:
                    raise ExpectTimeout(f"{self.name}: the socket ended ({self.closed}) with no line matching "
                                        f"/{pattern}/; last lines:\n    {self._tail(start)}")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ExpectTimeout(f"{self.name}: no line matching /{pattern}/ within {timeout}s; last lines:\n"
                                        f"    {self._tail(start)}")
                self._cv.wait(min(left, 0.5))

    def expect_none(self, pattern, window=2.0, since=None):
        rx = re.compile(pattern)
        start = self.mark() if since is None else since
        time.sleep(window)
        for text in self.since(start):
            if rx.search(text):
                raise AssertionError(f"{self.name}: unexpected line matching /{pattern}/: {shown(text)}")

    def collect(self, window, since=None):
        start = self.mark() if since is None else since
        time.sleep(window)
        return self.since(start)

    # ------------------------------------------------------------ state
    @property
    def connected(self):
        return self.closed is None and not self._stop.is_set()

    def wait_closed(self, timeout):
        """True once the socket has ended, within `timeout` s."""
        deadline = time.monotonic() + timeout
        while self.closed is None and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.closed is not None

    def pause(self):
        """Stop reading: the kernel's buffer, then the TCP window, fill, and the board's sends to this socket block."""
        self._paused.set()

    def resume(self):
        self._paused.clear()

    # ------------------------------------------------------------ the end
    def abort(self):
        """End the connection with a TCP reset and no close frame: a client that vanished mid-stream."""
        self._stop.set()
        try:        # struct linger: two u_shorts in Winsock, two ints elsewhere; on and 0 s is a reset on close
            self.ws.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                    struct.pack("HH" if os.name == "nt" else "ii", 1, 0))
        except OSError:
            pass
        try:
            self.ws.sock.close()
        except OSError:
            pass
        self._thread.join(2)
        with self._cv:
            if self.closed is None:
                self.closed = "aborted by the test"

    def close(self):
        """A close frame, then the socket; idempotent."""
        self._stop.set()
        self._thread.join(2)
        try:
            with self._wlock:
                self.ws.close()
        except OSError:
            pass
        with self._cv:
            if self.closed is None:
                self.closed = "closed by the test"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
