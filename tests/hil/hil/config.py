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
