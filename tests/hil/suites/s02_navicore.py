"""NaviCore as a WCB_Client mesh member — read-only checks, nothing moves."""
from hil.navicore import NaviCore
from hil.runner import test
from hil.wcb import WCB


def _nc(bench):
    return NaviCore(bench.dev("navicore"))


def _bench_wcb_ids(bench):
    ids = {d["wcb"] for d in bench.cfg["devices"].values() if d["kind"] == "wcb"}
    return ids | set(bench.cfg.get("mesh_only_wcbs", []))


@test("navicore.ping", "NaviCore answers a JSON PING with its firmware version", needs=["navicore"])
def navicore_ping(bench):
    bench.note(f"NaviCore firmware {_nc(bench).ping()}")


@test("navicore.online", "NaviCore reports every bench WCB online", needs=["navicore"])
def navicore_online(bench):
    online = _nc(bench).online_ids()
    missing = _bench_wcb_ids(bench) - online
    assert not missing, f"NaviCore does not see WCB {sorted(missing)} online (online: {sorted(online)})"


@test("navicore.wdp", "NaviCore's WDP table lists every bench WCB, running WCB1's firmware",
      needs=["navicore", "wcb1"])
def navicore_wdp(bench):
    fw = WCB(bench.dev("wcb1")).version()
    rows = {int(r["N"]): r for r in _nc(bench).wdp_dump()}
    for wcb_id in sorted(_bench_wcb_ids(bench)):
        assert wcb_id in rows, f"WCB{wcb_id} missing from NaviCore ?WDP,DUMP (has {sorted(rows)})"
        row = rows[wcb_id]
        assert row["CLIENT"] == "0", f"WCB{wcb_id} advertised as a client: {row}"
        assert row["FW"] == fw, f"WCB{wcb_id} advertises FW={row['FW']}, WCB1 runs {fw}"
