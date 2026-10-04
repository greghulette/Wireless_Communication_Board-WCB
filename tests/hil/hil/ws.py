"""A minimal RFC 6455 client for a WCB's ws://<ip>/ws endpoint (WCB_WS.cpp): text frames carry command lines in, and
the board's console output comes back as text frames. Standard library only, like the rest of the harness (pyserial is
its one dependency, docs/HIL_TESTING.md). Client frames are masked, as the RFC requires; the board's are not.
WsEndpoint is the other end: a stand-in board endpoint on a loopback address that can vanish the way an access point
does when its board restarts."""
import base64
import hashlib
import os
import socket
import struct
import threading
import time


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
    KB (hil/ncws.py pause(); Windows otherwise grows the window far beyond what a test streams)."""
    def __init__(self, host, port=80, path="/ws", timeout=5.0, origin=None, rcvbuf=None):
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

    def send_text(self, s: str):
        self.sock.sendall(frame(s.encode()))

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

    def read_until(self, needle: str, timeout=5.0):
        """Collect text frames into self.text until `needle` is in it or the time is up; returns the text so far."""
        end = time.monotonic() + timeout
        while needle not in self.text and time.monotonic() < end:
            self.sock.settimeout(max(0.1, end - time.monotonic()))
            try:
                op, payload = self._frame()
            except socket.timeout:
                break
            if op == 0x1:
                self.text_frames.append(payload)
                self.text += payload.decode(errors="replace")
            elif op == 0x2:
                self.raw += payload
                self.text += payload.decode(errors="replace")
            elif op == 0x9:
                self.sock.sendall(frame(payload, opcode=0xA))
            elif op == 0x8:
                break
        return self.text

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
    notice but silence. close() ends it all. Standard library; a thread per connection."""

    def __init__(self, host="127.0.0.2", port=80, pong='{"type":"PONG","version":"v-endpoint"}'):
        self.host, self.port, self.pong = host, port, pong
        self.accepted = 0
        self.upgraded = 0
        self._conns = []
        self._lock = threading.Lock()
        self._silent = threading.Event()
        self._stopped = threading.Event()
        self._lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._lsock.bind((host, port))      # OSError when the address is taken or cannot be used here
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
            with self._lock:
                self.upgraded += 1
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
                    conn.sendall(server_frame(payload, opcode=0xA))
                elif op == 0x8:
                    conn.sendall(server_frame(payload[:2], opcode=0x8))
                    return
                elif op == 0x1 and payload.strip() == b'{"type":"PING"}':
                    conn.sendall(server_frame((self.pong + "\n").encode()))
        except OSError:
            return

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
