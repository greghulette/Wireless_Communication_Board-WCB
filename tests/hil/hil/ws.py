"""A minimal RFC 6455 client for a WCB's ws://<ip>/ws endpoint (WCB_WS.cpp): text frames carry command lines in, and
the board's console output comes back as text frames. Standard library only, like the rest of the harness (pyserial is
its one dependency, docs/HIL_TESTING.md). Client frames are masked, as the RFC requires; the board's are not.
WsEndpoint is the other end: a stand-in board endpoint on a loopback address that can vanish the way an access point
does when its board restarts.

The token (docs/WIFI_DESIGN.md §7; WCB_WS.cpp wsAdmit): a board with one set says [AUTH:REQUIRED] when a session opens,
and runs nothing and mirrors nothing to it until it sends ?AUTH,<token> and is answered [AUTH:OK]; a wrong one is
answered [AUTH:FAIL] and a CLOSE (1008). WsClient(token=...) signs in when the board asks, and only then: a board that
does not ask is never sent the token. The token never goes into a message."""
import base64
import hashlib
import os
import socket
import sys
import struct
import threading
import time

AUTH_REQUIRED, AUTH_OK, AUTH_FAIL = "[AUTH:REQUIRED]", "[AUTH:OK]", "[AUTH:FAIL]"
AUTH_LINES = (AUTH_REQUIRED, AUTH_OK, AUTH_FAIL)
CLOSE_POLICY = b"\x03\xf0"          # the CLOSE status a failed sign-in is sent: 1008, policy violation (RFC 6455 7.4.1)


class AuthFailed(ConnectionError):
    """The board answered ?AUTH with [AUTH:FAIL]: a wrong token, or any ?AUTH within 1 s of a failed one."""


def frame(payload: bytes, opcode=0x1, mask=None):
    """One masked frame (FIN set). mask: 4 bytes, random when None."""
    mask = mask or os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def parse(buf: bytes):
    """(opcode, payload, bytes consumed) for the frame at the start of buf, or None when buf holds no whole frame."""
    if len(buf) < 2:
        return None
    op, n, pos = buf[0] & 0x0F, buf[1] & 0x7F, 2
    if n == 126:
        if len(buf) < 4:
            return None
        n, pos = struct.unpack(">H", buf[2:4])[0], 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n, pos = struct.unpack(">Q", buf[2:10])[0], 10
    masked = bool(buf[1] & 0x80)
    if masked:
        if len(buf) < pos + 4:
            return None
        mask, pos = buf[pos:pos + 4], pos + 4
    if len(buf) < pos + n:
        return None
    payload = buf[pos:pos + n]
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return op, payload, pos + n


class WsClient:
    """origin: an Origin header to send (Intellex's /_link refuses a foreign one and allows none). Binary frames -
    Intellex relays every byte from the board as one - are kept whole in self.raw and decoded into self.text too.
    rcvbuf: a receive buffer size set before connecting, so a client that stops reading fills its window within a few
    KB (hil/ncws.py pause(); Windows otherwise grows the window far beyond what a test streams).
    token: sign in with it if the board asks (sign_in_if_asked, after the handshake); AuthFailed if it is refused."""
    def __init__(self, host, port=80, path="/ws", timeout=5.0, origin=None, rcvbuf=None, token=None, auth_wait=1.0):
        if rcvbuf:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, int(rcvbuf))
            self.sock.settimeout(timeout)
            try:
                self.sock.connect((host, port))
            except OSError:
                self.sock.close()
                raise
        else:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        extra = f"Origin: {origin}\r\n" if origin else ""
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{extra}\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("the connection closed during the WebSocket handshake")
            resp += chunk
        head, _, self.buf = resp.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status:
            raise ConnectionError(f"WebSocket handshake refused: {status}")
        self.text = ""
        self.raw = b""
        self.text_frames = []      # each text frame's payload as sent (self.text decodes with errors="replace")
        self.pongs = []            # each PONG's payload, in order
        self.close_frames = []     # each CLOSE frame's payload the peer sent
        self.auth_lines = []       # the [AUTH:...] lines sign_in took out of self.text, in order
        if token is not None:
            try:
                self.sign_in_if_asked(token, wait=auth_wait, timeout=timeout)
            except OSError:
                self.sock.close()
                raise

    def send_text(self, s: str):
        self.sock.sendall(frame(s.encode()))

    def send_ping(self, payload: bytes = b""):
        self.sock.sendall(frame(payload, opcode=0x9))

    def _frame(self):
        while True:
            got = parse(self.buf)
            if got:
                op, payload, used = got
                self.buf = self.buf[used:]
                return op, payload
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("the connection closed")
            self.buf += chunk

    def _read_one(self, timeout):
        """Read one frame into the fields read_until fills -> False when none came in `timeout` s or it was a CLOSE."""
        self.sock.settimeout(max(0.05, timeout))
        try:
            op, payload = self._frame()
        except socket.timeout:
            return False
        if op == 0x1:
            self.text_frames.append(payload)
            self.text += payload.decode(errors="replace")
        elif op == 0x2:
            self.raw += payload
            self.text += payload.decode(errors="replace")
        elif op == 0x9:
            self.sock.sendall(frame(payload, opcode=0xA))
        elif op == 0xA:
            self.pongs.append(payload)
        elif op == 0x8:
            self.close_frames.append(payload)
            return False
        return True

    def read_until(self, needle: str, timeout=5.0):
        """Collect text frames into self.text until `needle` is in it or the time is up; returns the text so far."""
        end = time.monotonic() + timeout
        while needle not in self.text and time.monotonic() < end:
            if not self._read_one(max(0.1, end - time.monotonic())):
                break
        return self.text

    def _take_auth(self):
        """Move every whole [AUTH:...] line out of self.text (and its frame out of text_frames) into auth_lines."""
        keep = []
        for line in self.text.splitlines(keepends=True):
            if line.strip() in AUTH_LINES and line.endswith(("\n", "\r")):
                self.auth_lines.append(line.strip())
            else:
                keep.append(line)
        self.text = "".join(keep)
        self.text_frames = [f for f in self.text_frames if f.decode(errors="replace").strip() not in AUTH_LINES]

    def sign_in(self, token, timeout=5.0):
        """Send ?AUTH,<token> and read until it is answered: returns on [AUTH:OK], raises AuthFailed on [AUTH:FAIL] and
        ConnectionError when nothing answers. A board with no token answers [AUTH:OK] to any ?AUTH. The [AUTH:...] lines
        go to auth_lines; anything else read meanwhile stays in text."""
        start = len(self.text)
        self.send_text(f"?AUTH,{token}\n")
        end = time.monotonic() + timeout
        while True:
            new = self.text[start:]
            if AUTH_FAIL in new:
                self._take_auth()
                raise AuthFailed("the board answered ?AUTH with [AUTH:FAIL]")
            if AUTH_OK in new:
                self._take_auth()
                return
            left = end - time.monotonic()
            if left <= 0 or self.close_frames:
                raise ConnectionError(f"?AUTH was not answered within {timeout:g} s")
            self._read_one(min(0.3, left))

    def sign_in_if_asked(self, token, wait=1.0, timeout=5.0):
        """Sign in if the board says [AUTH:REQUIRED] in its first frame, read within `wait` s of the open -> True when
        it did. A board that sends anything else first, or nothing, is not asking: it is never sent the token."""
        end = time.monotonic() + wait
        while AUTH_REQUIRED not in self.text and time.monotonic() < end:
            before = len(self.text)
            if not self._read_one(end - time.monotonic()):
                break
            if len(self.text) > before and AUTH_REQUIRED not in self.text:
                return False                    # console output: a board that is not asking
        if AUTH_REQUIRED not in self.text:
            return False
        self.sign_in(token, timeout=timeout)
        return True

    def close(self):
        try:
            self.sock.sendall(frame(b"", opcode=0x8))
            self.sock.settimeout(1.0)
            self.sock.recv(4096)
        except OSError:
            pass
        finally:
            self.sock.close()


def server_frame(payload: bytes, opcode=0x1):
    """One unmasked frame (FIN set), as a server sends it."""
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


def accept_key(key: str) -> str:
    """Sec-WebSocket-Accept for a client's Sec-WebSocket-Key (RFC 6455 section 4.2.2)."""
    return base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()


class WsEndpoint:
    """A stand-in for a board's ws://<host>/ws endpoint on one loopback address (suites/s32_intellex.py
    intellex.link_drop_no_stall). It completes the upgrade, answers a ping with a pong, a close with a close, and a text
    '{"type":"PING"}' line with `pong` - and it can vanish the way an access point whose board restarts does:
    go_silent() closes the listening socket, so a new connect is refused (what Intellex's liveness probe sees of a dead
    AP), and every open connection stays open with nothing read or sent - no FIN, no close frame, nothing a peer could
    notice but silence. close() ends it all. Standard library; a thread per connection.

    For intellex.reconnect_idle_gate it can also stall the way an access point does while its board erases flash for an
    OTA: close_listener() refuses new connects (the probe fails) while the open connections are still served, and
    trickle() sends a byte frame on each every `period` s - traffic, the proof of life Intellex's idle gate trusts.

    token: speak the WCB's sign-in (module docstring; WCB_WS.cpp wsAdmit) - [AUTH:REQUIRED] on the open and for each line
    sent before ?AUTH,<token>, [AUTH:OK], or [AUTH:FAIL] and a 1008 CLOSE; with none, ?AUTH answers [AUTH:OK]. Every
    text line a connection sends is kept in `received`, so a check can see what was (never) sent."""

    def __init__(self, host="127.0.0.2", port=80, pong='{"type":"PONG","version":"v-endpoint"}', token=None):
        self.host, self.port, self.pong, self.token = host, port, pong, token
        self.received = []
        self.accepted = 0
        self.upgraded = 0
        self._conns = []
        self._live = []                         # (conn, its send lock): every upgraded connection still served
        self.last_sent = None                   # time.monotonic() of the latest trickle frame
        self.trickled = 0
        self._trickle = None                    # (thread, stop event) while trickle() runs
        self._lock = threading.Lock()
        self._silent = threading.Event()
        self._stopped = threading.Event()
        self.wildcard = False                   # listening on every address, serving only `host` (below)
        self._lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            try:
                self._lsock.bind((host, port))  # OSError when the address is taken or cannot be used here
            except PermissionError:
                # macOS (10.14 on) lets an unprivileged process take a port below 1024 only on the wildcard address:
                # 127.0.0.2:80 itself is EACCES there. So listen on all of them, and _accept() closes, unserved, every
                # connection not addressed to `host`: nothing on the Mac's other addresses reaches the stand-in.
                if sys.platform != "darwin" or not 0 < port < 1024:
                    raise
                self._lsock.close()
                self._lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._lsock.bind(("0.0.0.0", port))
                self.wildcard = True
            self._lsock.listen(4)
            self.port = self._lsock.getsockname()[1]     # the one bound, for port 0
        except OSError:
            self._lsock.close()
            raise
        threading.Thread(target=self._accept, name=f"ws-endpoint:{host}:{port}", daemon=True).start()

    def _accept(self):
        while not self._stopped.is_set():
            try:
                conn, _ = self._lsock.accept()
            except OSError:
                return                          # the listener closed: go_silent() or close()
            if self.wildcard:
                try:
                    local = conn.getsockname()[0]
                except OSError:
                    local = ""
                if local != self.host:          # another of the Mac's addresses: not the stand-in's
                    conn.close()
                    continue
            with self._lock:
                self._conns.append(conn)
                self.accepted += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(0.25)
            req = b""
            while b"\r\n\r\n" not in req:
                if self._stopped.is_set() or self._silent.is_set():
                    return
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                req += chunk
            head, _, buf = req.partition(b"\r\n\r\n")
            key = next((line.split(b":", 1)[1].strip().decode() for line in head.split(b"\r\n")[1:]
                        if line.split(b":", 1)[0].strip().lower() == b"sec-websocket-key"), "")
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n").encode())
            slock = threading.Lock()            # replies here and trickle() frames share the socket
            with self._lock:
                self.upgraded += 1
                self._live.append((conn, slock))
            authed = not self.token

            def say(line):
                with slock:
                    conn.sendall(server_frame((line + "\r\n").encode()))
            if not authed:
                say(AUTH_REQUIRED)
            while not self._stopped.is_set():
                if self._silent.is_set():
                    time.sleep(0.1)             # hold it open; read nothing, answer nothing
                    continue
                got = parse(buf)
                if not got:
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        return
                    buf += chunk
                    continue
                op, payload, used = got
                buf = buf[used:]
                if op == 0x9:
                    with slock:
                        conn.sendall(server_frame(payload, opcode=0xA))
                elif op == 0x8:
                    with slock:
                        conn.sendall(server_frame(payload[:2], opcode=0x8))
                    return
                elif op == 0x1:
                    for raw in payload.decode(errors="replace").replace("\r", "\n").split("\n"):
                        line = raw.strip()
                        if not line:
                            continue
                        self.received.append(line)
                        if line.upper() == "?AUTH" or line.upper().startswith("?AUTH,"):
                            if authed or line[6:] == self.token:
                                authed = True
                                say(AUTH_OK)
                            else:
                                say(AUTH_FAIL)
                                with slock:
                                    conn.sendall(server_frame(CLOSE_POLICY, opcode=0x8))
                                return
                        elif not authed:
                            say(AUTH_REQUIRED)
                        elif line == '{"type":"PING"}':
                            with slock:
                                conn.sendall(server_frame((self.pong + "\n").encode()))
        except OSError:
            return
        finally:
            with self._lock:
                self._live = [x for x in self._live if x[0] is not conn]

    def close_listener(self):
        """Refuse new connects - a TCP probe of the port fails at once - while every open connection is still served."""
        try:
            self._lsock.close()
        except OSError:
            pass

    def trickle(self, period=2.0, payload=b"."):
        """Send `payload` as one TEXT frame on every open connection now and every `period` s after, until
        stop_trickle() (or go_silent / close); last_sent is when the latest went out."""
        self.stop_trickle()
        stop = threading.Event()

        def run():
            while not stop.is_set() and not self._stopped.is_set() and not self._silent.is_set():
                with self._lock:
                    live = list(self._live)
                sent = False
                for conn, slock in live:
                    try:
                        with slock:
                            conn.sendall(server_frame(payload))
                        sent = True
                    except OSError:
                        pass
                if sent:
                    self.last_sent = time.monotonic()
                    self.trickled += 1
                stop.wait(period)
        t = threading.Thread(target=run, name=f"ws-endpoint-trickle:{self.host}", daemon=True)
        self._trickle = (t, stop)
        t.start()

    def stop_trickle(self):
        """Stop trickle() and wait for its thread -> last_sent: when the final frame went out (None if none did)."""
        if self._trickle is not None:
            t, stop = self._trickle
            stop.set()
            t.join(5)
            self._trickle = None
        return self.last_sent

    def go_silent(self):
        """Vanish without a word: refuse new connects, and fall silent on the open ones (kept open)."""
        self._silent.set()
        try:
            self._lsock.close()
        except OSError:
            pass

    def close(self):
        self._stopped.set()
        self._silent.set()
        self.stop_trickle()
        try:
            self._lsock.close()
        except OSError:
            pass
        with self._lock:
            conns, self._conns = list(self._conns), []
        for c in conns:
            try:
                c.close()
            except OSError:
                pass
