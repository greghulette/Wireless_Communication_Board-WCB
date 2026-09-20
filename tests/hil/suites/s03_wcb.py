"""WCB console basics — config integrity and command parsing. Changes nothing."""
from hil.runner import test
from suites.common import config_guard, remote_wcbs, usb_wcb


@test("wcb.backup_crc", "?backup's configured-boards chain carries a valid CRC-32", needs=["wcb1"])
def backup_crc(bench):
    tokens, provided, calc = usb_wcb(bench).backup_chain()
    assert provided == calc, f"?backup chain says CHK {provided}, CRC-32 of the chain is {calc}"
    assert tokens[0].startswith("?HW,"), f"chain should start with ?HW, starts with {tokens[0]}"


@test("wcb.mgmt_pull", "?MGMT,PULL returns each remote WCB's config: valid CRC, same firmware, right id",
      needs=["wcb1"])
def mgmt_pull(bench):
    w = usb_wcb(bench)
    fw = w.version()
    for n in remote_wcbs(bench):
        ver, tokens, provided, calc = w.mgmt_pull(n)
        assert provided == calc, f"W{n} pull CHK {provided}, calculated {calc}"
        assert ver == fw, f"W{n} runs {ver}, W1 runs {fw}"
        assert f"?WCB,{n}" in tokens, f"W{n} config does not say ?WCB,{n}"


@test("wcb.unknown", "An unknown ? command is reported, not silently swallowed", needs=["wcb1"])
def unknown(bench):
    lines = usb_wcb(bench).run("?ZZHIL")
    assert any(t.startswith("Unknown command: ZZHIL") for t in lines), f"got {lines}"


@test("wcb.help_trap", "A ? command ending in '?' prints help instead of running (and changes nothing)",
      needs=["wcb1"])
def help_trap(bench):
    with config_guard(bench, 1):
        lines = usb_wcb(bench).run("?LABEL,S4,HILTRAP?")
        assert not any("label set to" in t for t in lines), f"label was set: {lines}"
