"""NaviCore USB driver. NaviCore only acts on lines starting with '?', '#' or '{' — anything else
is silently ignored (NaviCore/NaviCore.ino processInputLine), so a bare PING never answers."""
import json
import re


class NaviCore:
    def __init__(self, dev):
        self.dev = dev

    def json_cmd(self, obj, reply_pattern, timeout=3.0):
        m = self.dev.mark()
        self.dev.send(json.dumps(obj, separators=(",", ":")))
        return self.dev.expect(reply_pattern, timeout=timeout, since=m)

    def ping(self):
        got = self.json_cmd({"type": "PING"}, r'^\{"type":"PONG","version":"([^"]+)"')
        return got.group(1)

    def wcb_status(self):
        got = self.json_cmd({"type": "GET_WCB_STATUS"}, r'^\{"type":"WCB_STATUS".*\}$')
        return json.loads(got.group(0))

    def online_ids(self):
        st = self.wcb_status()
        return {i + 1 for i, v in enumerate(st["online"]) if v} | {st["self"]}

    def wdp_dump(self, timeout=5.0):
        m = self.dev.mark()
        self.dev.send("?WDP,DUMP")
        self.dev.expect(r"^\[WDP:END,count=\d+\]", timeout=timeout, since=m)
        rows = []
        for text in self.dev.since(m):
            hit = re.match(r"^\[WDP:(N=.*)\]$", text)
            if hit:
                rows.append(dict(kv.split("=", 1) for kv in hit.group(1).split(",") if "=" in kv))
        return rows

    def sbus_state(self, timeout=3.0):
        """#L09 -> {'fps': int, 'lost': str, 'channels': [24 ints]} (no sentinel; ends at CH17-24)."""
        m = self.dev.mark()
        self.dev.send("#L09")
        self.dev.expect(r"^\s*CH17-24:", timeout=timeout, since=m)
        text = self.dev.since(m)
        joined = "\n".join(text)
        fps = re.search(r"fps=(\d+)", joined)
        lost = re.search(r"lost=(\S+)", joined)
        channels = []
        for t in text:
            hit = re.match(r"^\s*CH\d+-\d+:\s+(.*)$", t)
            if hit:
                channels += [int(v) for v in hit.group(1).split()]
        return {"fps": int(fps.group(1)) if fps else 0, "lost": lost.group(1) if lost else "?",
                "channels": channels}

    def set_debug_flags(self, flags):
        self.json_cmd({"type": "SET_DEBUG_FLAGS", "flags": flags}, r'^\{"type":"ACK","ok":true')
