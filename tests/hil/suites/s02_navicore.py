"""NaviCore as a WCB_Client mesh member — read-only checks, nothing moves."""
import time

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
    """NaviCore learns a WCB from its WDP advert, which a WCB sends about once a minute, so a NaviCore that has just
    booted has no rows yet (run 20260925-091104: up about 27 s, count=0, both WCBs learned 4 and 10 s after the test).
    A missing row gets one ?WDP,POLL from W1 - W1 advertises and solicits the rest; WCB_Client answers a SOLICIT and
    does not mistake it for an advert since 1.15.1 - and up to 15 s to arrive. The note says when that was needed."""
    w1 = WCB(bench.dev("wcb1"))
    fw = w1.version()
    want = sorted(_bench_wcb_ids(bench))
    rows = {int(r["N"]): r for r in _nc(bench).wdp_dump()}
    missing = [n for n in want if n not in rows]
    if missing:
        t0 = time.monotonic()
        w1.run("?WDP,POLL")
        while any(n not in rows for n in want) and time.monotonic() - t0 < 15:
            time.sleep(1.0)
            rows = {int(r["N"]): r for r in _nc(bench).wdp_dump()}
        bench.note(f"NaviCore's WDP table lacked WCB {missing} (a fresh NaviCore?); after ?WDP,POLL it had "
                   f"{sorted(rows)} in {time.monotonic() - t0:.1f} s")
    for wcb_id in want:
        assert wcb_id in rows, f"WCB{wcb_id} missing from NaviCore ?WDP,DUMP (has {sorted(rows)})"
        row = rows[wcb_id]
        assert row["CLIENT"] == "0", f"WCB{wcb_id} advertised as a client: {row}"
        assert row["FW"] == fw, f"WCB{wcb_id} advertises FW={row['FW']}, WCB1 runs {fw}"


@test("navicore.bench_health", "NaviCore sees the SBUS controller at full rate and its local Maestro answers ?MAE,GET: "
      "a dead input or Maestro FAILS here instead of turning every test that needs it into a skip (read only)",
      needs=["navicore", "sbus"])
def bench_health(bench):
    """NAVICORE.md D-NC15. The SBUS and Maestro tests skip when NaviCore sees no SBUS stream or its local Maestro does
    not answer (s21 _sbus_setup, NaviCore.usable_slot), which is right for them and let a dead Maestro read as skips in
    every run until 2026-09-22. This one fails instead. #L09 is NaviCore's own view of the stream (dumpSbusState,
    NaviCore.ino:2885-2903); ?MAE,GET reads a local slot synchronously off Serial2 (maestroLocalQuery, :713-733) and
    moves nothing. The Maestro part runs only when bench.json lists a Maestro on NaviCore ("maestros")."""
    nc = _nc(bench)
    problems = []
    st = nc.sbus_dump()
    if st["fps"] < 100 or st["lost"] != "no":
        problems.append(f"NaviCore's SBUS input: {st['fps']} fps, lost={st['lost']}, variant {st['variant']}")
    wants_maestro = any(m.get("where") == "navicore" for m in bench.cfg.get("maestros", []))
    local = nc.local_slots(nc.config()) if wants_maestro else []
    if wants_maestro and not local:
        problems.append("bench.json lists a Maestro on NaviCore, and NaviCore's config has no local Maestro slot")
    for slot, dev in local:
        pos = nc.mae_get(slot, 0)
        if not isinstance(pos, int):
            problems.append(f"local Maestro slot {slot} (device {dev}) answers ?MAE,GET,{slot},0 with {pos!r}")
    bench.note(f"SBUS {st['fps']} fps ({st['variant']}); local Maestro slots {[s for s, _ in local] or 'not checked'}")
    assert not problems, "; ".join(problems)
