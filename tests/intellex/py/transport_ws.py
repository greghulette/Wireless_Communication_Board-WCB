"""Intellex's WebSocketTransport against NaviCore's own access point, under its venv (intellex.ws_transport_navicore,
IX-WP5). The harness puts the PC's spare WiFi adapter on that access point first (suites/s45_navicore_wifi.py _on_ap,
hil/wlan.py pc_on_ap); this script never changes the network itself: no netsh, no join, no bounce. It needs an address on
the host's subnet (discover.local_ip_for, a UDP connect that sends nothing) and a DIRECT PONG from the host - no "sys",
no "id" - carrying the version NaviCore gave the harness over USB. That is the proof the AP is NaviCore's: a WCB's AP at
the same 192.168.4.1 mirrors NaviCore's mesh PONG, which carries both. With args "joined" (the harness holds a lease on
NaviCore's AP) a missing address or PONG is a failure; without it, a skip (the network was whatever the PC was on).

Intellex src/ws_transport.py: frames are concatenated as bytes with no line awareness (:130-159), a write is sent as one
UTF-8 TEXT message and a payload that is not UTF-8 raises (:106-124), and a write after close() raises (contract 2).
GET_CONFIG, over 10 KB, arrives split across frames (NaviCore flushes its reply in ~1400-byte pieces) and must reassemble
into one line that parses. It holds the AP and mesh passwords: only its length, frame count and key count are reported.

args: host (default 192.168.4.1); version (NaviCore's PONG version, read by the harness over USB); joined.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ixpy  # noqa: E402
from _ixpy import SkipCase, case, check  # noqa: E402


class _Sink:
    def __init__(self):
        self.data = bytearray()
        self.frames = []
        self.lost = []

    def on_data(self, chunk):
        self.frames.append(len(chunk))
        self.data += chunk

    def on_lost(self, why):
        self.lost.append(why)

    def line(self, start, prefix, timeout):
        """The first whole line at or after offset `start` that starts with `prefix` -> (line bytes, the frame count so
        far) or (None, n)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = bytes(self.data)
            i = data.find(prefix, start)
            while i >= 0 and i > start and data[i - 1:i] != b"\n":
                i = data.find(prefix, i + 1)
            if i >= 0:
                j = data.find(b"\n", i)
                if j >= 0:
                    return data[i:j].rstrip(b"\r"), len(self.frames)
            time.sleep(0.05)
        return None, len(self.frames)


@case("PING, a GET_CONFIG over 10 KB reassembled from frames, a non-UTF-8 write and a write after close", group="navicore")
def navicore_ws(ctx):
    import discover
    from transport import TransportError
    from ws_transport import WebSocketTransport
    host = ctx.args.get("host") or "192.168.4.1"

    def need(msg):
        # The harness joined NaviCore's access point for this test (a lease is held), so a precondition that does not
        # hold is a failure; on whatever network the PC happened to be on, it is a skip.
        raise (AssertionError if ctx.args.get("joined") else SkipCase)(msg)
    via = discover.local_ip_for(host)
    if not via or via.rsplit(".", 1)[0] != host.rsplit(".", 1)[0]:
        need(f"this PC has no address on {host}'s network (it would leave by {via or 'nothing'}): the second WiFi "
             f"adapter is not on a droid's access point")
    sink = _Sink()
    t = WebSocketTransport(host, sink.on_data, sink.on_lost)
    try:
        t.open()
    except TransportError as e:
        need(f"nothing answers ws://{host}/ws from {via}: {str(e)[:160]}")
    try:
        mark = len(sink.data)
        t.write(b'{"type":"PING"}\n')
        pong = None
        deadline = time.monotonic() + 6.0
        while pong is None and time.monotonic() < deadline:
            whole = bytes(sink.data)[mark:].split(b"\n")[:-1]      # complete lines only
            for x in whole:
                x = x.strip()
                if x.startswith(b"{") and b'"PONG"' in x:
                    try:
                        obj = json.loads(x)
                    except ValueError:
                        continue
                    if obj.get("type") == "PONG":
                        pong = obj
                        break
            time.sleep(0.05)
        if pong is None:
            need(f"{host} answered no PONG within 6 s: not NaviCore's own access point")
        if "sys" in pong or "id" in pong:
            need(f"the PONG from {host} came over the mesh (it carries sys/id): this PC is on a doorway's access point, "
                 f"not NaviCore's")
        want = ctx.args.get("version")
        if want and pong.get("version") != want:
            need(f"{host} is a NaviCore on {pong.get('version')!r}, not the bench NaviCore ({want!r})")
        ctx.note(f"on NaviCore's access point via {via}: PONG {pong.get('version')}")

        mark = len(sink.data)
        f0 = len(sink.frames)
        t.write(b'{"type":"GET_CONFIG"}\n')
        line, frames = sink.line(mark, b'{"type":"CONFIG"', 12.0)
        check(line is not None, f"no whole CONFIG line within 12 s ({len(sink.data) - mark} bytes arrived)")
        try:
            cfg = json.loads(line)
        except ValueError as e:
            raise AssertionError(f"the {len(line)}-byte CONFIG line does not parse (reassembly lost or reordered "
                                 f"bytes): {type(e).__name__} at char {getattr(e, 'pos', '?')}") from None
        data = cfg.get("data")
        check(isinstance(data, dict), "CONFIG carries no data object")
        sizes = sink.frames[f0:frames]
        ctx.note(f"GET_CONFIG: {len(line)} bytes in {len(sizes)} frames (largest {max(sizes) if sizes else 0}), "
                 f"{len(data)} top-level keys")
        check(len(line) >= 10000, f"the CONFIG line is {len(line)} bytes; the plan expects over 10 KB")
        check(len(sizes) >= 2, f"the {len(line)}-byte CONFIG line came in {len(sizes)} frame(s): nothing to reassemble")

        try:
            t.write(b"\xff\xfe not utf-8\n")
            raise AssertionError("a write that is not UTF-8 was sent (ws_transport.py:113-116 must raise)")
        except TransportError:
            pass
        check(not sink.lost, f"the link was lost on a healthy AP: {sink.lost[:2]}")
    finally:
        t.close()
    check(not t.is_open, "is_open is True after close()")
    try:
        t.write(b'{"type":"PING"}\n')
        raise AssertionError("a write after close() did not raise (contract 2)")
    except TransportError:
        pass


if __name__ == "__main__":
    sys.exit(_ixpy.main())
