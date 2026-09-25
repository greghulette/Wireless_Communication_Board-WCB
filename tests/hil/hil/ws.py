"""A minimal RFC 6455 client for a WCB's ws://<ip>/ws endpoint (WCB_WS.cpp): text frames carry command lines in, and
the board's console output comes back as text frames. Standard library only, like the rest of the harness (pyserial is
its one dependency, docs/HIL_TESTING.md). Client frames are masked, as the RFC requires; the board's are not."""
import base64
import os
import socket
import struct
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
    def __init__(self, host, port=80, path="/ws", timeout=5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
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
