# The away week (2026-09-27 to about 2026-10-04): decisions and full HIL runs

Greg, leaving on 2026-09-27: *"Can you make sure you're always working towards this goal this week and make any
decisions needed. Just track them so I can review when I return."* The goal: *"a complete hil tool that tests the wcbs,
navicore and intellex fully. All features of every system should be testable. Use playwright testing to test webpages
and functionality of that."*

Every decision that would normally have been his is recorded here, newest last, with how to undo it, and every
full HIL run of the week is logged below with a short summary (Greg: *"keep track of all the full hil tests you run
through the week as well with a brief summary of them"*). The work itself is tracked in
[HIL_TEST_AUDIT.md](HIL_TEST_AUDIT.md) §6 (plan and progress notes) and the commits it names.

## What Greg decided before leaving

- Servos may move all week. The bench GUI may be stopped. Firmware may be changed and flashed, NaviCore included.
- WCB work is pushed to `WIFI`, never to WCB `main` (which publishes a release). NaviCore, Intellex, WcbCmd and
  WCBClient may be pushed to `main` (NaviCore `main` republishes the production config tool).
- The dev mesh password in the WCBClient MgmtRelay example stays (F17 closed).
- Scheduled check-ins keep the work going; they live in the Claude session in VS Code, which must stay open.

## Decisions

| # | Date | Decision | Alternatives considered | Why | How to undo | Where |
|---|---|---|---|---|---|---|
| D1 | 2026-09-25 | WcbCmd shipped as **0.9.1**, not a re-used 0.9.0, and **not tagged** (so the Arduino Library Manager still offers 0.7.0). | Keep 0.9.0; tag 0.9.1. | 0.9.0 was already public with both bugs; a tag publishes a Library Manager release, which Greg had not asked for. | `git tag 0.9.1 0ab4af5 && git push origin 0.9.1` to publish. | WcbCmd `0ab4af5` |
| D2 | 2026-09-25 | WcbCmd #2's 200 ms stale-partial timeout shipped as bench-verified, with its limit documented (a WCB `loop()` stall over 200 ms mid-frame drops that frame), rather than redesigned. | Rescan for `7E` on a tail failure, which would remove both the timeout and the limit. | The shipped version had passed on the bench; a redesign could not be HIL-verified that day. | Revisit with the rescan approach. | WcbCmd `docs/WCB-INTEGRATION.md` Phase 5 |
| D3 | 2026-09-27 | All HIL tests for all three systems (WCB, NaviCore, Intellex, and their Playwright specs) live in this repo's `tests/`, run by one `run.py` and one GUI. | Put NaviCore/Intellex tests in their own repos. | One bench, one runner, one report: the goal is one complete tool. | Move a suite later; the runner loads suites by path. | `tests/` |
| D4 | 2026-09-27 | Check-ins every 3 hours (session cron), work chained continuously in between; usage paced so the weekly limit does not stop the week. | Once or twice a day. | A usage limit then costs hours, not half a day. | `CronDelete` the job in the session. | session job `c6b3f811` |
| D5 | 2026-09-27 | F23 fixed as recommended: every PWM output pulse (local passthrough and `;P`) is one RMT symbol on a per-port channel, so preemption cannot stretch it; with no channel free the port keeps the bit-bang and says so once. | Re-pin PWMTask to core 1 (a mitigation only: a flash write stalls both cores). | The 1830-for-1000 us pulse was the one a digital servo holds. | Revert the `pwmPulse()` commit; the fallback image is `results/builds/wcb-esp32-meshq-f21`. | tracker #94 |
| D6 | 2026-09-27 | A full HIL run (everything, servos on) at least once a day, overnight when the bench is idle, and after each phase; each one logged below. | Only targeted runs. | Catches cross-suite regressions the targeted runs miss; the bench is otherwise idle at night. | Stop scheduling them. | the run log |
| D7 | 2026-09-27 | The nightly full runs use `bench.json`'s opt-ins exactly as Greg ticked them, attended ones included (`mesh_password` - never run before - `nvs_erase`, `wifi_pc`, the relay OTA re-flashes, `nvs_wear`, `seq_wipe`, the soak). | Leave the attended ones out while nobody is at the bench. | Greg ticked them and wants everything tested; each is recoverable over USB without hands: W1's mesh password from W2's chain (the same value), an erased NVS from W1's chain, an OTA by a USB flash; `wifi_pc` uses only the second adapter, not the one carrying the internet. | Untick them in `bench.json`. | `tests/hil/bench.json` |
| D8 | 2026-09-27 | The Intellex test plan's 14 recommendations (DX1-DX14) adopted as written: tests in `tests/intellex/` run by `suites/s32_intellex.py`; a staged copy of Intellex per test with `LOCALAPPDATA` pointed into the stage; three env hooks added to Intellex (`INTELLEX_OFFLINE`, `INTELLEX_SERIAL_ALLOW`, `INTELLEX_DISCOVER_HOSTS`); flashing only W2, with its own bench image; new Intellex opt-ins start unticked. Pushing Intellex `main` also published Greg's unpushed local commit `d615344` ("Wizard: reconnect the link at its own slot, not slot 1"). | Test through Playwright route interception only, with no Intellex change. | Background loops and Python-level tests need the same leash; each hook is a few lines and inert when unset. | Revert Intellex `e9f95f2`. | Intellex `e9f95f2` |
| D9 | 2026-09-27 | The NaviCore plan's decisions D-NC1 to D-NC41 are taken as recommended (`hil_plan/NAVICORE.md` §7): its config may be written only inside a guard that proves byte-identity afterwards; it is flashed by `?OTALOCAL`, esptool only for recovery; WCBClient then NaviCore `main` are pushed after a bench pass; the SBUS controller gets RAM-only test verbs, flashed locally and not pushed; credentials are filtered from the logs. Except D-NC37 (wire a third probe to NaviCore's pins), which needs Greg's hands and waits for him. | Decide each as the work reaches it. | They are well-argued, each with its undo, and the week has no one to ask. | Per row, in the plan. | `hil_plan/NAVICORE.md` |
| D10 | 2026-09-27 | Every real firmware defect the WCB re-scan confirmed (26, `hil_plan/WCB.md` §3) is fixed this week, high first, each proven by its test on the bench before it is pushed; the 12 Wizard defects are fixed with their specs. First: the PWM mappings loading before the board number, `?HW` accepting the other chip's pin map, and the race that drops a typed broadcast from the mesh. | File them for Greg. | He asked for decisions to be made, and a known defect with a proving test is cheaper to fix now than to rediscover. | Revert per commit. | `hil_plan/WCB.md` §3 |
| D11 | 2026-09-27 | Intellex's specs live in `tests/intellex`, beside `tests/wizard`; the NaviCore config tool's specs go in `tests/wizard` under `navicore/` (D-NC10), served from the same origin as the Wizard. | One folder for every browser test. | Intellex's pages are served by Intellex, not by `serve.js`, and need no Web Serial grant, so they do not share the Wizard folder's Chrome profiles or server. | Move the folder. | `tests/intellex` |

## Full HIL runs

Every full run of the week, newest last. "Full" means `run.py` with no selectors (everything registered, opt-ins as
ticked in `bench.json`), unless the scope column says otherwise. Reports are in `tests/hil/results/<run>/report.md`
(gitignored: on the bench PC only).

| Run | When | Image(s) | Scope | Result | Time | Summary |
|---|---|---|---|---|---|---|
| `20260925-092255` | 2026-09-25 09:22 (Greg, before the week; the baseline) | W1/W2 `meshq` F20/F21 image | everything, servos on | 493 pass, 2 fail, 4 skip | 2:35:02 | `pwm.passthrough_local`: a stretched PWM pulse, a firmware defect (F23, fixed as D5). `etm.reboot_defer_cap`: a harness cleanup read the `;S0` sentinel glued to a WiFi-task line (fixed in `b170342`). Skips: three WDP-DA label checks (W1 S3/S5 carry labels), and `ota.local_wrong_chip_image` (no S3 image of the running version). |
