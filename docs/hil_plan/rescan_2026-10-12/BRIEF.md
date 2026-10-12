# Coverage audit brief (2026-10-12)

Goal: list every user-facing behaviour in your slice and match it to the tests that assert it, so the gaps can be built.
The user (Greg) wants "all features of every system testable"; NaviCore had this audit on 2026-10-11 and every gap was built.

## Rules
- Read-only. Never open a serial port, never run tests, run.py, gui.py or Playwright: a bench run may be in progress.
- Never print, copy or write a credential: bench.json, results/ and fixtures hold WiFi/mesh passwords (?WIFI, ?EPASS,
  "password" JSON keys). Do not read tests/hil/results/ at all.
- A behaviour counts as TESTED only if a test's assertion would fail when that behaviour breaks: read the test body, not
  the title. PARTIAL when some forms, paths or edge rules are unasserted. Say exactly which.
- Group the forms of one command as one behaviour with the forms noted; do not list every error string separately,
  but do list each distinct rule (a clamp, a refusal, a persistence, a mesh path, a boot load) as its own row.
- Known-blocked items in docs/hil_plan/WCB.md "WCB-WP59" (line ~688) and docs/hil_plan/INTELLEX.md "4.2 Not testable"
  get status "blocked" with blocked_by "known: <ref>"; do not re-derive them.
- docs/HIL_TESTING.md section 7 "Coverage" (line ~1131) and docs/hil_plan/*.md are orientation only; the test bodies decide.
- Test registry: `grep -n '^@test(' tests/hil/suites/*.py` (ids like area.name); Wizard specs in tests/wizard/specs and
  tests/wizard/unit; Intellex specs in tests/intellex/specs. Harness primitives in tests/hil/hil/ and suites/common.py.
- The wiki (../Wireless_Communication_Board-WCB.wiki/*.md) describes released firmware; the WIFI branch code is the truth.

## Output (both files, in the audit folder named in your prompt)
1. `<slice>.json`: a JSON array, one object per behaviour:
   {"id": "area.short_slug", "area": "...", "behaviour": "one sentence", "where": "file:line[-line]",
    "status": "tested|partial|untested|blocked", "tested_by": ["test ids"], "gap": "what is unasserted (partial/untested)",
    "testable": "unattended|opt-in|attended|blocked", "bench_needs": "probe tap / W2 / NaviCore / Intellex host / none",
    "risk": "H|M|L", "how": "how a test would drive it and what it would observe (1-2 sentences)",
    "blocked_by": "only for blocked"}
   Risk: H = silent loss or misrouting of data, a boot/persistence fault, a safety issue (servos, erase); M = a wrong
   reply, format or limit on a path users hit; L = cosmetic, rare, or diagnostic text.
2. `<slice>.md`: counts per status, then the gaps (untested + partial) ordered H, M, L as a table: id | behaviour | where |
   tested_by | gap | how. Keep it factual, no filler.

Final report to the caller (short): the counts, the output paths, the ten highest-risk gaps in one line each, and anything
you could not resolve from the code.
