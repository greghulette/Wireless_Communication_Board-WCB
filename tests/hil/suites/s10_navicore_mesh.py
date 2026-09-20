"""NaviCore (WCB_Client, id 20) <-> WCB traffic in both directions."""
from hil.navicore import NaviCore
from hil.runner import test
from suites.common import marker, usb_wcb, wire


@test("navicore.cli_over_mesh", ";W20,?version runs on NaviCore and its output returns as [TERM:20]",
      needs=["wcb1", "navicore"])
def cli_over_mesh(bench):
    nc = NaviCore(bench.dev("navicore"))
    fw = nc.ping()
    w = usb_wcb(bench)
    m = w.send(";W20,?version")
    w.dev.expect(rf"^\[TERM:20\]Software Version: {fw}$", timeout=4, since=m)


@test("navicore.wcb_send", "NaviCore WCB_SEND unicasts reach W1 S1 and W2 S2 on the wire", links=["W1S1", "W2S2"],
      needs=["navicore", "probe1", "probe2"])
def wcb_send(bench):
    nc = NaviCore(bench.dev("navicore"))
    for wcb, port in ((1, "S1"), (2, "S2")):
        probe, ch = wire(bench, wcb, port)
        t = marker()
        m = probe.dev.mark()
        nc.json_cmd({"type": "WCB_SEND", "target": wcb, "cmd": f";{port}{t}"}, r'^\{"type":"ACK","ok":true')
        probe.expect_bytes(ch, t.encode() + b"\r", timeout=3, since=m)


@test("navicore.trigger", "A TRIGGER sent from W1 dispatches on NaviCore (rc_trig)",
      needs=["wcb1", "navicore"])
def trigger(bench):
    nc = NaviCore(bench.dev("navicore"))
    m = nc.dev.mark()
    usb_wcb(bench).send(';W20,{"type":"TRIGGER","mode":1,"btn":1,"tap":1}')
    nc.dev.expect(r'"type":"rc_trig".*"mode":1,"btn":1,"tap":1', timeout=4, since=m)
