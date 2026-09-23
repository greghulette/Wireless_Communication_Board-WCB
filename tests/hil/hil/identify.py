"""What is on a COM port, and which bench device it is. Shared by the GUI's Find devices / Add and the resume checks.

identify_port() is the one safe sequence for a port nobody has confirmed yet: ?VERSION at 115200 first, then HELLO at
921600. Nothing kind-specific goes to an unconfirmed port - a JSON PING into a port that turned out to be a WCB would
be broadcast to the whole mesh.

A device's identity on a resume is its board number (WCB), MAC (probe) or kind (NaviCore, SBUS) PLUS the USB serial
number the checkpoint recorded at the start (usb_fingerprint): a second board answering as WCB1 - Greg's droid WCB1
plugged in at home - is never adopted as the bench's without asking.
"""
import re
import time

from serial.tools import list_ports

from .serialdev import ExpectTimeout, SerialDevice

ESP_VIDS = {0x10C4, 0x1A86, 0x303A, 0x0403, 0x067B}   # CP210x, CH34x/CH9102, Espressif native, FTDI, PL2303


# WCB.ino handleSingleCommand answers this fixed line whatever the LFI and command character are.
CONFIG_PULL = "WCB_WEBTOOL_CONFIG_PULL"


def identify_port(port, wcb_home=False):
    """What is on this COM port? -> dict(kind=..., ...) or None. Never resets a board (DTR/RTS low).

    115200 first: a WCB and NaviCore both answer ?VERSION (NaviCore's version starts with 'v');
    the SBUS controller treats '?' as its status key and prints '[SBUS] Mode:'. Only then 921600,
    where a probe answers HELLO. No free text is sent anywhere: an unprefixed line on a WCB would
    be broadcast to the whole mesh.

    A WCB answer carries its board number, command character ('cmdchar', from ?config's "Command Character:" line),
    command delimiter ('delim', "Delimiter Character:") and 'lfi' '?' - ?VERSION answering proves it. ?config is read
    up to those lines, never through WCB.run(): run()'s ';S0,HILEND' sentinel is itself a broadcast when a cut-off
    chars.* test left the command character or delimiter changed. A WCB whose board number does not print comes back as
    kind 'wcb' with wcb None and a note, not as an open error.

    wcb_home=True is for a port the recorded USB serial already proves is a bench WCB (the resume's re-identify). It
    sends the fixed WCB_WEBTOOL_CONFIG_PULL first, which answers whatever the LFI is, and reads the LFI, the board
    number, the command character and the delimiter from the backup lines. When any of them is not the default it
    returns without sending ?VERSION - an LFI of '!' would make '?VERSION' an unprefixed line, broadcast to the mesh.
    No answer returns None (still booting), and 921600 is not tried."""
    try:
        with SerialDevice("scan", port, 115200) as d:
            time.sleep(0.3)
            if wcb_home:
                m = d.mark()
                d.send(CONFIG_PULL)
                try:
                    num = int(d.expect(r"^\SWCB,(\d+)\s*$", timeout=5.0, since=m).group(1))
                    chars = d.expect(r"^(\S)CMDCHAR,(\S)\s*$", timeout=5.0, since=m)
                except ExpectTimeout:
                    return None
                lfi, cc = chars.group(1), chars.group(2)
                # ?DELIM,<x> is emitted only when the delimiter is not '^', just before CMDCHAR (collectConfigCommands,
                # WCB.ino), so it is already read. A ',' delimiter splits run()'s ';S0,HILEND' sentinel into a broadcast.
                dm = next((x for x in (re.match(r"^\SDELIM,(.)\s*$", ln) for ln in d.since(m)) if x), None)
                delim = dm.group(1) if dm else "^"
                if lfi != "?" or cc != ";" or delim != "^":
                    return {"kind": "wcb", "version": None, "wcb": num, "lfi": lfi, "cmdchar": cc, "delim": delim}
            m = d.mark()
            d.send("?VERSION")
            try:
                got = d.expect(r"Software Version: (\S+)|\[SBUS\] (Mode:|Ready)", timeout=2.0, since=m)
            except ExpectTimeout:
                got = None
            if got:
                if got.group(0).startswith("[SBUS]"):
                    return {"kind": "sbus"}
                ver = got.group(1)
                if ver.startswith("v"):
                    return {"kind": "navicore", "version": ver}
                m = d.mark()
                d.send("?config")
                try:
                    num = int(d.expect(r"Configuration: Wireless Communication Board (\d+)", timeout=5.0,
                                       since=m).group(1))
                except ExpectTimeout:
                    return {"kind": "wcb", "version": ver, "wcb": None, "note": "?config did not print its board"}
                try:
                    delim = d.expect(r"^Delimiter Character:\s+(\S)", timeout=5.0, since=m).group(1)
                except ExpectTimeout:
                    delim = None
                try:
                    cc = d.expect(r"^Command Character:\s+(\S)", timeout=5.0, since=m).group(1)
                except ExpectTimeout:
                    cc = None
                return {"kind": "wcb", "version": ver, "wcb": num, "lfi": "?", "cmdchar": cc, "delim": delim}
            if wcb_home:
                return None
        with SerialDevice("scan", port, 921600) as d:
            time.sleep(0.2)
            # The ?VERSION sent at 115200 reached a probe as garbage with no line end, which then prefixed HELLO
            # ("ERR unknown verb <garbage>HELLO"): a bare line end closes it first. A probe ignores an empty line.
            d.send("")
            time.sleep(0.1)
            m = d.mark()
            d.send("HELLO")
            try:
                got = d.expect(r"^HELLO wcb_probe (\S+) mac=(\S+)", timeout=2.0, since=m)
                return {"kind": "probe", "version": got.group(1), "mac": got.group(2)}
            except ExpectTimeout:
                return None
    except Exception as e:   # port busy, vanished, access denied
        return {"kind": "error", "error": str(e).splitlines()[0]}


def usb_ports():
    """{COM name: ListPortInfo} for every ESP32-type USB serial port. Enumerates only - opens nothing."""
    return {p.device: p for p in list_ports.comports() if p.vid in ESP_VIDS}


def usb_fingerprint(port, ports=None):
    """{serial, vid, pid, location} of a COM port from its USB descriptor, all None when it is not there."""
    if ports is None:
        ports = {p.device: p for p in list_ports.comports()}
    info = next((i for dev, i in ports.items() if port and dev.upper() == str(port).upper()), None)
    if info is None:
        return {"serial": None, "vid": None, "pid": None, "location": None}
    return {"serial": info.serial_number or None, "vid": info.vid, "pid": info.pid,
            "location": getattr(info, "location", None)}


def identity_of(dev_cfg):
    """The identity bench.json expects: ('wcb', n), ('probe', MAC) or (kind,)."""
    kind = dev_cfg.get("kind")
    if kind == "wcb":
        return ("wcb", dev_cfg.get("wcb"))
    if kind == "probe":
        mac = dev_cfg.get("mac")
        return ("probe", mac.upper()) if mac else ("probe", None)
    return (kind,)


def identity_from(info):
    """The identity an identify_port() answer shows, or None (nothing answered, or the port would not open)."""
    if not info or info.get("kind") == "error":
        return None
    kind = info["kind"]
    if kind == "wcb":
        return ("wcb", info.get("wcb"))
    if kind == "probe":
        return ("probe", (info.get("mac") or "").upper())
    return (kind,)


def describe(ident):
    if not ident:
        return "nothing"
    if ident[0] == "wcb":
        return f"WCB{ident[1]}" if ident[1] is not None else "a WCB (board number unknown)"
    if ident[0] == "probe":
        return f"probe {ident[1]}"
    return {"navicore": "NaviCore", "sbus": "the SBUS controller"}.get(ident[0], ident[0])
