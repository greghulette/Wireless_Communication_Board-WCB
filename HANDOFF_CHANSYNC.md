# Handoff: channel sync, WebSocket token, windowed relay OTA

Temporary note for moving this work between machines. **Delete this file before the branch is merged.**
Written 2026-10-22 on the Windows bench PC.

## What was built

| Repo | Branch | Head | What |
|---|---|---|---|
| Wireless_Communication_Board-WCB | `chansync` (from `seq-builder` f733e1a) | see `git log` | Firmware: channel sync (`WCB_ChanSync.{h,cpp}`, wire + pure decisions `WCB_ChanSyncWire.h`), `?REGION` for 12-13, JOIN follows its network, WebSocket token (`?WIFI,TOKEN`, `?AUTH`, JOIN needs one), commands held while the radio is away, JOIN keyed on the booted mode. Wizard: windowed relay OTA (`parser.js` `otaRelayStream`), Region / Channel Sync / Access Token fields, mesh-panel Channel column. HIL suite `s55_chsync.py` + token tests in `s28_wifi.py`. Host test `tests/chansync_wire_test.cpp`. |
| WCBClient | `chansync` (from master 0c5803e) | 9df1df1 | WCB_Client 1.19.0: same channel sync (byte-identical `src/WCB_ChanSyncWire.h`), `setRegion`, `onChannelMove`, MgmtRelay token + rank 255. |
| NaviCore | `chansync` (from local main, which includes 7fefcaa, a commit not yet on origin/main) | 3c2d37f | Leads at rank 0, saves a move and restarts to move its AP, region + chanSync + `wifiToken` config fields and tool UI, token on its WebSocket (15 s sign-in limit). Needs WCB_Client 1.19.0. |
| Intellex | `chansync` (from main c9644ac) | f9f8cd9 | Signs in with `?AUTH` when a droid asks, tokens kept per board MAC in settings.json, "Locked droid" + token entry. |
| WCB / NaviCore / Intellex wikis | `chansync` | 2271c3e / 40deab5 / 6af51de | User pages. The WCB wiki's main checkout on the Windows PC has another session's uncommitted edits to Command-Reference, Config-Tool and ETM; merge with care. |

Design docs: `docs/CHANNEL_SYNC.md`, `docs/WIFI_DESIGN.md` §7, `docs/WCB_OTA_TECHNICAL.md` §4. CLAUDE.md rule 18.

## Verified

- Compiles: WCB both targets (ESP32 75%, S3 73%); WCB_Client examples on esp32/esp32s3; NaviCore (16M, PSRAM=opi) against WCB_Client 1.19.0 via `--library`.
- Host tests: `wdp_wire_test`, `config_parts_test`, `chansync_wire_test` pass. HIL `selftest.py` 134/147, the 13 failures pre-date this work.
- Wizard: unit tests (24 new) and the CI no-board Playwright specs pass. Intellex: `tools/smoke_ws_auth.py` + existing smokes pass.
- Bench (Windows PC): W1, W2, W3 flashed with `7.0.1_220448ROCT2026` (app0 + otadata only, configs intact; record in `tests/hil/results/builds/wcb-esp32-meshq/FLASHED.md`, backups in `tests/hil/results/backups/20261022-071749-*`). Targeted HIL 9/9: `chsync.status_line`, `.operator_move_and_back`, `.stranded_found`, `.region_refuses_12`, `wifi.token_setter`, `wifi.join_needs_token`, `ws.token_*`.
- Regression pass (run 20261021-212944, 404 tests, previous build): 190 pass, 10 fail, 204 skip (199 Wizard tests skipped: `tests/wizard` had no node_modules in the worktree). Fixed since: commands held while away (`ota.relay_navicore_brick_guard`, `ws.client_slots`). Re-run on the current build (run 20261022-072416, Windows PC) PAUSED cleanly at 49 of 245 before the PC was unplugged; resume there with `python tests/hil/run.py --resume 20261022-072416`. So far 43 pass, 5 fail: four `ws.*` (`client_slots`, `stalled_client_costs_nothing`, `ota_chunk`, `token_wrong_closes`) all because the PC's spare adapter got no DHCP lease from W1's AP in 60 s, and `wizard.parser` on a UI_VERSION stamping unit test (Windows temp-folder issue seen before). After the pause all three boards were checked: new build, channel 1, region 01, no token, W1/W2 AP mode.
- W1's stale learned peers 6 and 9 were forgotten (Greg's OK); W1's peers are 2, 3, 20.

## Open

- **W3 ESP-NOW transmit wedge** during `etm.char_legacy` (driver refuses every frame until a reboot). Same failure recorded in `WCB_EspNow.cpp` from run 20261006-122850, before channel sync: looks pre-existing. Does the re-run reproduce it?
- **`ws.ping_pong_and_close`** "W1 did not echo the CLOSE frame": first ever run of that test; re-check on the current build.
- **Slow DHCP leases** on W1's AP (35-55 s, sometimes none in 62 s) during the `ws.*` tests; cause unknown.
- **NaviCore not flashed** on the bench yet. Plan: compile with `--library <WCBClient chansync>` and `--build-property compiler.cpp.extra_flags=-DNAVICORE_HIL_HOOKS=1` into `tests/hil/results/builds/navicore-chansync`, flash with `hil/ncflash.flash()` over `?OTALOCAL`. Then update the WCB HIL NaviCore tests the NaviCore agent listed: `s40_navicore_config.py` TOP_KEYS (+`wifiToken`, `chanSync`), `nccfg.clamps_mesh_channel` (13 is now stored), the IDENTITY tuples in s40/s43, s43's bridged-strip test (region + token stripped), s49 `cloud_hidden_wifi`.
- **Remote sequence-name pull** caps at 2,912 chars (16 x 182, `sendResultFrags`), but the file store holds a few hundred sequences: ~180+ names cannot be listed remotely ("Result too large to relay"). `docs/SEQUENCE_INVENTORY.md` §2 sizing (80 keys) is stale. Proposed: page the names reply when it does not fit. Not started.
- Push/merge order when releasing: WCBClient 1.19.0 to master BEFORE NaviCore (its `#error` needs 1.19.0; NaviCore's branch CI fails until then). The Arduino-Code sketchbook copy of WCB_Client was NOT synced (it shadows every local compile).
- `tests/hil/bench.json` on Windows was edited locally (COM30/COM7/COM29 for probe2/probe4/wcb3, `chsync` opt-in) and not committed. On the Mac the bench uses `/dev/cu.*` paths, and `chsync` must be added to its `opt_in` to run `s55`.
- Wiki Changelog entry left for release time. The "about 3 minutes" relay-OTA figure in the wiki is from the simulator, not measured.

## Resume on another machine

```bash
for r in Wireless_Communication_Board-WCB WCBClient NaviCore Intellex; do git -C $r fetch origin chansync; done
git -C Wireless_Communication_Board-WCB worktree add ../_chansync/WCB chansync   # or check the branch out
# NaviCore against the new library: arduino-cli compile ... --library <path to WCBClient chansync> (confirm "Using library WCB_Client at version 1.19.0")
```
