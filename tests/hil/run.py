"""Hardware-in-the-loop tests — command line. For the GUI: python tests/hil/gui.py

    python tests/hil/run.py                 # every test
    python tests/hil/run.py "maestro.*"     # ids matching globs (several allowed)
    python tests/hil/run.py --discover      # re-detect which probe header is wired to which WCB port
    python tests/hil/run.py --list | --plan | --links

Close the Arduino IDE serial monitor and any Wizard/NaviCore tabs holding the bench ports
first — a COM port has one owner. Results land in tests/hil/results/<timestamp>/.
"""
import argparse
import importlib
import os
import pkgutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from hil import runner  # noqa: E402
import suites  # noqa: E402

for mod in pkgutil.iter_modules(suites.__path__):
    importlib.import_module(f"suites.{mod.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("selectors", nargs="*", help="test id globs, e.g. 'mesh.*'")
    ap.add_argument("--bench", default=os.path.join(HERE, "bench.json"))
    ap.add_argument("--discover", action="store_true", help="re-detect wires before running")
    ap.add_argument("--list", action="store_true", help="list tests and the wires each needs")
    ap.add_argument("--plan", action="store_true", help="print what to wire")
    ap.add_argument("--links", action="store_true", help="print the wires found last time")
    args = ap.parse_args()
    results_root = os.path.join(HERE, "results")

    if args.list:
        for t in runner.REGISTRY:
            wires = ",".join(runner.links_of(t)) or "-"
            print(f"{t['id']:<26} {wires:<16} {t['title']}")
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
    if args.selectors == [] and not args.discover:
        pass
    out_dir, results = runner.run(args.bench, args.selectors, results_root, discover=args.discover)
    bad = sum(1 for _, s, _, _ in results if s in ("FAIL", "ERROR"))
    print(f"\n{len(results)} run, {bad} failed — report: {os.path.join(out_dir, 'report.md')}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
