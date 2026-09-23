"""Hardware-in-the-loop tests — command line. For the GUI: python tests/hil/gui.py

    python tests/hil/run.py                 # every test
    python tests/hil/run.py "maestro.*"     # ids matching globs (several allowed)
    python tests/hil/run.py --discover      # re-detect which probe header is wired to which WCB port
    python tests/hil/run.py --list | --plan | --links
    python tests/hil/run.py --resume [RUN]  # continue a paused or interrupted run (the newest, or RUN)
    python tests/hil/run.py --paused        # list the runs that can be resumed

Ctrl+C once pauses after the current test (exit 3, safe to disconnect); twice aborts at once and cuts that test off
(exit 130; it runs again first on --resume). Creating results/<run>/PAUSE, or results/PAUSE, pauses a run started
elsewhere. Close the Arduino IDE serial monitor and any Wizard/NaviCore tabs holding the bench ports
first — a COM port has one owner. Results land in tests/hil/results/<timestamp>/.
"""
import argparse
import importlib
import json
import os
import pkgutil
import signal
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from hil import checkpoint, runner  # noqa: E402
import suites  # noqa: E402

for mod in pkgutil.iter_modules(suites.__path__):
    importlib.import_module(f"suites.{mod.name}")
checkpoint.freeze_harness(HERE)   # the sources this process runs, for the resume's "test code changed" check


def make_ask(force):
    """The resume checks' question: print the differences; --force accepts, a terminal asks, anything else declines
    (or takes the question's default, e.g. the reboot after a cut-off test). ask.unanswered records that a question
    went to no one, so the exit hint can name --force."""
    def ask(title, text, default=False):
        print(f"\n{title}:\n  " + text.replace("\n", "\n  "))
        if force:
            print("  --force: accepted")
            return True
        if sys.stdin.isatty():
            try:
                a = input("  Continue? [Y/n] " if default else "  Continue? [y/N] ").strip().lower()
            except KeyboardInterrupt:     # during a question Ctrl+C means No
                print()
                return False
            except EOFError:
                # stdin is NUL or closed (the PowerShell tool, Task Scheduler, < NUL): Windows reports NUL as a
                # character device, so isatty() is True with nobody behind it. Fall through to the no-terminal rule.
                print()
            else:
                return default if not a else a.startswith("y")
        ask.unanswered = True
        if not default:
            print("  not a terminal - rerun with --force to accept")
        return default
    ask.unanswered = False
    return ask


def install_pause_handler(control):
    """First Ctrl+C: pause after the current test. Second: abort now (default_int_handler raises KeyboardInterrupt).
    Installed only once the resume checks are over, so a Ctrl+C at a question means No."""
    def handler(signum, frame):
        if control.pausing:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            raise KeyboardInterrupt
        control.request_pause("ctrl_c")
        # A raw, unlocked write: the handler runs on the main thread, and a buffered print landing inside the main
        # thread's own stdout flush raises "reentrant call inside BufferedWriter".
        try:
            os.write(sys.stderr.fileno(), b"\nPausing after the current test... (Ctrl+C again aborts NOW and cuts that "
                                          b"test off)\n")
        except (OSError, ValueError, AttributeError):
            pass
    signal.signal(signal.SIGINT, handler)


def main():
    # Redirected to a file on Windows, stdout is cp1252 and the first '→' in a failure message kills
    # the whole run with UnicodeEncodeError (2026-09-21). Emit UTF-8, and never die on a character.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("selectors", nargs="*", help="test id globs, e.g. 'mesh.*'")
    ap.add_argument("--bench", default=os.path.join(HERE, "bench.json"))
    ap.add_argument("--discover", action="store_true", help="re-detect wires before running")
    ap.add_argument("--list", action="store_true",
                    help="list tests: the wires each needs, its expected time and its opt-in")
    ap.add_argument("--plan", action="store_true", help="print what to wire")
    ap.add_argument("--links", action="store_true", help="print the wires found last time")
    ap.add_argument("--resume", nargs="?", const="latest", default=None, metavar="RUN",
                    help="continue a paused or interrupted run: the newest, or the run folder named")
    ap.add_argument("--paused", action="store_true", help="list the runs that can be resumed")
    ap.add_argument("--force", action="store_true", help="--resume: accept every difference without asking")
    args = ap.parse_args()
    results_root = os.path.join(HERE, "results")

    if args.list:
        # Read-only: bench.json for which opt-ins are on, and the durations cache without rewriting it.
        from hil import durations
        try:
            with open(args.bench, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        history = durations.load(results_root, write_cache=False)
        for line in runner.list_lines(runner.REGISTRY, cfg, history):
            print(line)
        return 0
    if args.paused:
        runs = checkpoint.find_resumable(results_root, include_stopped=True)
        for s in runs:
            print(checkpoint.summary_text(s) + ("   (resume by name only)" if s["state"] == "stopped" else ""))
        if not runs:
            print("No paused, interrupted or stopped run.")
        return 0
    if args.plan or args.links:
        from hil import wiring
        bench = runner.Bench(args.bench, results_root)
        if args.links:
            for link in bench.links.all():
                print(f"{link}  verified={link.verified}")
        if args.plan:
            for row in wiring.plan(bench):
                print(f"{row['key']:<6} [{row['status']}] {row['how']}")
                if row["unlocks"]:
                    print(f"       unlocks: {', '.join(row['unlocks'])}")
        return 0
    if args.resume is not None and (args.selectors or args.discover):
        ap.print_usage(sys.stderr)
        print("run.py: error: --resume continues a run's own selection - it takes no test globs and no --discover",
              file=sys.stderr)
        return 2

    from hil.resume import ResumeAborted, ResumeBlocked
    control = runner.RunControl()
    ckpt = None
    ask = make_ask(args.force)
    try:
        if args.resume is not None:
            out_dir, ckpt = runner.resume(args.bench, results_root, args.resume, control=control,
                                          ask=ask, on_checks_done=lambda: install_pause_handler(control))
        else:
            out_dir, ckpt = runner.run(args.bench, args.selectors, results_root, discover=args.discover,
                                       control=control, on_checks_done=lambda: install_pause_handler(control))
    except checkpoint.CheckpointError as e:
        print(f"run.py: {e}", file=sys.stderr)
        return 2
    except checkpoint.RunBusy as e:
        print(f"run.py: {e}", file=sys.stderr)
        return 3
    except ResumeAborted as e:
        print(f"\nResume not done ({e}) - the run is still paused. python tests/hil/run.py --resume"
              + (" --force" if "declined" in str(e) and ask.unanswered else ""))
        return 3
    except ResumeBlocked as e:
        print("\nResume blocked - the run is still paused. Fix this, then run --resume again:\n  "
              + str(e).replace("\n", "\n  "))
        return 3
    except KeyboardInterrupt:
        name = ckpt.name if ckpt else _newest_name(results_root)
        print(f"\nAborted. The run is paused; a test that was cut off runs again first. Continue with:\n"
              f"  python tests/hil/run.py --resume{(' ' + name) if name else ''}")
        return 130
    if ckpt.state == "paused":
        print(f"\n{ckpt.done_count} of {ckpt.total} done — report: {os.path.join(out_dir, 'report.md')}")
        print(ckpt.pause_message())
        return 3
    results = ckpt.data["results"]
    bad = sum(1 for r in results if r["status"] in ("FAIL", "ERROR"))
    print(f"\n{len(results)} run, {bad} failed — report: {os.path.join(out_dir, 'report.md')}")
    return 1 if bad else 0


def _newest_name(results_root):
    runs = checkpoint.find_resumable(results_root)
    return runs[0]["name"] if runs else None


if __name__ == "__main__":
    sys.exit(main())
