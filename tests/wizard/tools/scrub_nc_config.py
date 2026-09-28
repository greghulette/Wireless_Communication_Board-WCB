"""Turn the bench NaviCore's real config into the committed fixture tests/wizard/fixtures/navicore/config.bench.json,
with every credential replaced (docs/hil_plan/NAVICORE.md §5.3, D-NC11).

    python tests/wizard/tools/scrub_nc_config.py --from tests/hil/results/<run>/navicore_snapshot.json
    python tests/wizard/tools/scrub_nc_config.py --from <a file holding GET_CONFIG's data object>

The source is what the harness already has: any run that ran a guarded NaviCore test (nc_guard, e.g.
nccfg.guard_selftest) left the exact config in <run>/navicore_snapshot.json. This opens no port.

Replaced: wcbNetwork.password, every wcbProfiles[].password, wifiPassword and wifiSsid (a non-empty value only), and
any other key ending in 'password' at any depth. It REFUSES to write when any original secret (or its JSON-escaped
form) is still anywhere in the output, and prints only counts and lengths — never a value. The fixture is written in
the key order NaviCore printed it (json keeps insertion order), indented, with a final newline.
"""
import argparse
import json
import os
import re
import sys

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures", "navicore", "config.bench.json")
SECRET_KEY = re.compile(r"password$", re.I)


def load(path):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if isinstance(doc, dict) and isinstance(doc.get("config"), str):     # a navicore_snapshot.json
        doc = json.loads(doc["config"])
    if isinstance(doc, dict) and doc.get("type") == "CONFIG" and isinstance(doc.get("data"), dict):
        doc = doc["data"]                                                # a whole CONFIG line
    if not isinstance(doc, dict) or "wcbNetwork" not in doc or "mappings" not in doc:
        raise SystemExit(f"{path}: not a NaviCore GET_CONFIG data object")
    return doc


def scrub(cfg):
    """-> (scrubbed copy, [original secret values]). One fixed placeholder per distinct VALUE, named after the first key
    that held it (the mesh password -> HILmeshPass, the AP's -> HILwifiPass, a profile's own -> HILprofile<n>Pass), so
    values that were equal stay equal: the tool matches a WCB profile to the live network by comparing them
    (_wcbProfilesEqual). A regenerated fixture diffs only where the real config changed."""
    placeholder = {}

    def name_for(value, hint):
        if value not in placeholder:
            taken = set(placeholder.values())
            p, n = hint, 2
            while p in taken:
                p, n = f"{hint[:-4]}{n}Pass", n + 1
            placeholder[value] = p
        return placeholder[value]

    out = json.loads(json.dumps(cfg))
    net = out.get("wcbNetwork") or {}
    if net.get("password"):
        net["password"] = name_for(net["password"], "HILmeshPass")
    if out.get("wifiPassword"):
        out["wifiPassword"] = name_for(out["wifiPassword"], "HILwifiPass")
    for i, p in enumerate(out.get("wcbProfiles") or [], 1):
        if isinstance(p, dict) and p.get("password"):
            p["password"] = name_for(p["password"], f"HILprofile{i}Pass")

    def walk(obj):                                     # any other *password key, at any depth
        if isinstance(obj, dict):
            for k, v in obj.items():
                if SECRET_KEY.search(str(k)) and isinstance(v, str) and v and not v.startswith("HIL"):
                    obj[k] = name_for(v, "HILsecretPass")
                else:
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(out)
    secrets = [v for v in placeholder]
    if out.get("wifiSsid"):
        secrets.append(out["wifiSsid"])
        out["wifiSsid"] = "HILssid"
    return out, sorted(set(secrets), key=len, reverse=True)


def leaks(text, secrets):
    """The secrets (by index) still findable in `text`, raw or JSON-escaped."""
    bad = []
    for i, s in enumerate(secrets):
        for form in {s, json.dumps(s)[1:-1]}:
            if form and form in text:
                bad.append(i)
                break
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="src", required=True, help="navicore_snapshot.json, or a GET_CONFIG data file")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)
    cfg = load(args.src)
    out, secrets = scrub(cfg)
    text = json.dumps(out, indent=1, ensure_ascii=False) + "\n"
    bad = leaks(text, secrets)
    if bad:
        print(f"REFUSED: {len(bad)} of {len(secrets)} original secret(s) still appear in the output "
              f"(lengths {[len(secrets[i]) for i in bad]}); nothing written", file=sys.stderr)
        return 2
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"wrote {os.path.relpath(args.out)}: {len(out.get('mappings') or {})} mappings, {len(secrets)} secret(s) "
          f"replaced (lengths {[len(s) for s in secrets]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
