"""Reading a WCB's saved config as comparable tokens — ?backup for the USB board, ?MGMT,PULL for the rest."""
import time

from .wcb import WCB, PullRefused, comparable


def read_config(bench, wcb):
    """A WCB with its own USB connection is read with ?backup on that port; any other through a
    ?MGMT,PULL,<n>,P from the primary console (wcb1), in one line or in parts (WCB.mgmt_pull).

    A timeout, a CRC mismatch or a retryable refusal (NOMEM, CHANGED, NOPARTS, an empty reply) is
    tried three times; a permanent one (TOOBIG) fails at once, because asking again gets the
    same answer - and config_guard, which reads every guarded board twice, would otherwise turn
    one refusal into six pulls of 10+ s each."""
    own = bench.usb_wcbs().get(wcb)
    if own:
        tokens, provided, calc = WCB(bench.dev(own)).backup_chain()
    elif wcb == bench.usb_wcb_number():
        tokens, provided, calc = WCB(bench.dev("wcb1")).backup_chain()
    else:
        w = WCB(bench.dev("wcb1"))
        last = None
        for _ in range(3):
            try:
                _, tokens, provided, calc = w.mgmt_pull(wcb)
                break
            except PullRefused as e:
                if not e.retryable:
                    raise
                last = e
                time.sleep(1)
            except AssertionError as e:
                last = e
                time.sleep(1)
        else:
            raise last
    assert provided == calc, f"W{wcb} config chain CRC mismatch ({provided} vs {calc})"
    return comparable(tokens)


def token(tokens, prefix):
    """The first token starting with `prefix` (case-insensitive), or None."""
    for t in tokens:
        if t.upper().startswith(prefix.upper()):
            return t
    return None


def wifi_mode_token(tokens):
    """The chain's ?WIFI mode line (?WIFI,AP,... / ?WIFI,JOIN,... / ?WIFI,OFF), or None. Not token(tokens, "?WIFI,"):
    a board with a WebSocket token emits ?WIFI,TOKEN,<token> just ahead of it (collectConfigCommands, WCB.ino;
    docs/WIFI_DESIGN.md §7)."""
    for t in tokens:
        u = t.upper()
        if u.startswith("?WIFI,") and not u.startswith("?WIFI,TOKEN,"):
            return t
    return None


def ws_token(tokens):
    """The WebSocket token a chain carries (?WIFI,TOKEN,<token>), or None. It goes to a board or a WsClient, never into
    a message."""
    t = token(tokens, "?WIFI,TOKEN,")
    return t.split(",", 2)[2] if t else None
