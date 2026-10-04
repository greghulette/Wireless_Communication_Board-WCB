# Intellex under the HIL harness: feature map and test plan

> Written 2026-09-27 by planning agents that read the code (and, for WCB, a verified coverage re-scan), for the
> away week's goal: a complete HIL tool for the WCBs, NaviCore and Intellex. It is a plan, not a spec: line numbers
> drift, and any claim is to be confirmed against the code before acting on it. Progress is tracked in
> [../HIL_TEST_AUDIT.md](../HIL_TEST_AUDIT.md) §6 and decisions in [../HIL_WEEK_DECISIONS.md](../HIL_WEEK_DECISIONS.md).

Written 2026-09-27 for phase 3 of the away-week plan (`docs/HIL_TEST_AUDIT.md:672-673`: "Intellex: map its
features, run it headless, Playwright against its UI over both transports, and its flashing path"). Read-only
research: nothing was run, no port was opened, and no repo was changed.

**What was read:**

- **Intellex**, working tree `C:/Users/ghulette/Documents/GitHub/Intellex` at `d615344` (local `main`, one commit
  ahead of `origin/main`): `CLAUDE.md`, `README.md`, `docs/*`, every `src/*.py`, `src/*.html`,
  `src/intellex_shim.js`, `tools/*`, `scripts/*`, `Intellex.spec`, `requirements.txt` and `Intellex.bat`.
- **NaviCore:** the parts of `config_tool/` and `NaviCore.ino` that Intellex depends on.
- **WCB:** `Wizard/`; `tests/wizard/` (config, `lib/`, `specs/`); `tests/hil/hil/{wizard,bridge,ws,runner,optin,
  navicore,serialdev}.py`; `suites/s28_wifi.py`; `suites/s30_wizard.py`; `bench.json`; `docs/HIL_TESTING.md`;
  `docs/HIL_WEEK_DECISIONS.md`; `results/builds/**/FLASHED.md`.

A bare `file:line` means `Intellex/src/...`; every other repo is named. "The harness" means `tests/hil` in the WCB
repo.

---

## 0. The short version

- **What Intellex is.** One aiohttp process (`host.py`) that serves:
  - the two browser tools, unmodified;
  - its own launcher, shell and docs viewer.

  It runs on `127.0.0.1:<port>`. Each page's `/_link` WebSocket is bridged to **one** transport: a COM port (pyserial,
  opened with DTR and RTS low) or a droid's `ws://<ip>/ws`. `app.py` only adds a pywebview window on top.
- **It already runs headless.** `python src/host.py --port P --no-auto-bounce [--serial COMx | --ws IP]` serves with
  no window and no browser (`host.py:1840-1898`).
- **What a bench needs from it:**
  - *Isolation.* Today it writes Greg's real settings, logs, bundles and firmware cache. No code change is needed:
    stage a copy of `src/` and `tools/` for each test and point `LOCALAPPDATA` into the stage.
  - *A leash.* Its launcher opens every Espressif COM port and probes 192.168.4.1. This needs three small env hooks
    in Intellex (§2.4, H1-H3).
- **No Web Serial.** Intellex's pages talk to the host, so its Playwright specs need no Chrome profile, no port grant
  and no one at the keyboard. They run headless against a host the harness starts, which makes most of this plan
  unattended.
- **Where the tests go.** 14 work packages, IX-WP1 to IX-WP14 (§3.3). Bench-bound specs live in `tests/intellex/` in
  the WCB repo (decision D3). They are launched by `tests/hil/suites/s32_intellex.py` through a new `hil/intellex.py`,
  the same way `s30_wizard.py` launches `tests/wizard`.
- **What reading the code found.** 11 items (§1.6). Three are real bugs, and one is a likely hazard:
  - the shim cannot update the Wizard's `latestFirmwareVersion`;
  - two Wizard images are never bundled;
  - Update FW skips the Wizard's partition-table migration check;
  - a USB-cabled WCB doorway probably leaves the NaviCore tool out of Via WCB mode, which is the rule-10 OTA hazard.

---

## 1. Feature inventory

### 1.1 The shape

```
 page(s): Playwright or WebView2          Intellex host (one process, 127.0.0.1:P)                  far end
 ───────────────────────────────          ────────────────────────────────────────                  ───────
 /            NaviCore config tool ─┐      aiohttp app, host.py:1760-1837
 /wcb/Wizard/ WCB Wizard           ─┼─ /_link (WS) ─► Bridge: a set of pages, one FIFO pump (host.py:124-382)
 /_launcher  /_shell  /wiki/*      ─┘                   └─ one Transport ─┬─ SerialTransport ──► COMx (DTR/RTS low)
   every tool page carries the shim                                      └─ WebSocketTransport ─► ws://192.168.4.1/ws
   (intellex_shim.js: navigator.serial -> /_link)   /_api/* = HTTP JSON control plane
                                                     reconnect_loop host.py:1518-1702 (1 s poll, TCP probe, re-identify, bounce)
```

### 1.2 The page <-> host WebSocket protocol (`/_link`)

| Direction | Frame | Content and rule | Where |
|---|---|---|---|
| page -> host | TEXT | One complete line, **terminator included**. The shim splits on `\r`, `\n` or `\r\n` (CRLF counts as one terminator). An unterminated tail stays buffered. One `TextDecoder` lives as long as the port. | `intellex_shim.js:166-215` (split at `:202-212`); flushed on close `:216-218` |
| page -> host | BINARY | Raw bytes, written as they are. The shim never sends these; a native client may. | `host.py:1292-1293` |
| page -> host | TEXT (host side) | Encoded as UTF-8, then written to the transport. | `host.py:1294-1299` |
| host -> page | BINARY | Every transport chunk goes to **every** page, whole and in arrival order: one `asyncio.Queue`, one `_pump` task. A send failure is suppressed per page. | `host.py:322-372` |
| host -> page | TEXT | `{"type":"ERROR","msg":"link write failed: ..."}\n` when the transport write raises (nothing attached, or device lost). The page sees it as a line in its stream. | `host.py:1300-1306` |
| host -> page | close | Every page socket is closed when the transport drops or is re-attached. Each tool then re-handshakes, which is how it sees a fresh PONG version after an OTA. | `host.py:249-287`; `attach` `:210-238` |
| host <-> page | ping/pong | aiohttp heartbeat every 30 s. | `host.py:1287` |
| upgrade | 403 | A cross-origin `Origin` is refused. **No** `Origin` at all (a native client) is allowed. | `host.py:1261-1286` |
| shim | stream error | When the socket closes or errors, the ReadableStream errors with `NetworkError` rather than closing gracefully (a graceful close pegged a CPU core). Writing while not OPEN throws `NetworkError`. | shim `:100-147`, `:170-174` |
| shim | DTR/RTS | `port.setSignals()` becomes a real `POST /_api/signals`. It is a no-op on a WS transport. | shim `:248-260`; `host.py:1234-1257` |
| frame <-> frame | postMessage | `{intellex:'open-chooser'}` and `{intellex:'close-chooser'}`, same origin only. | shim `:1492-1500`; `launcher.html:617-620`; `shell.html:439-444` |

**Host -> droid** (`ws_transport.py`):

- **Endpoint:** `ws://<host>:80/ws` (`:24-25`, `:37`).
- **Timing:** 5 s open timeout; a ping every 5 s with a 10 s pong deadline (`:77-80`).
- **Sending:** each write is decoded strictly as UTF-8, then sent as one TEXT message (`:106-124`).
- **Receiving:** frames are concatenated as bytes, with no awareness of lines (`:130-159`).

### 1.3 Control API (`build_app`, `host.py:1760-1837`)

Every POST passes through `_guard_origin` (`host.py:1733-1757`). A foreign `Origin` gets a 403 and a log line.

| Route | Handler | What it does, and its side effects |
|---|---|---|
| `GET /` | `index` `:1366-1392` | Serves the NaviCore tool. The shim tag is injected before the first `<script>` at serve time (`_inject_shim` `:1345-1363`). Sent `no-store`; a 503 explanation when the bundle is missing. |
| `GET /wcb/Wizard/`, `/wcb/Wizard/index.html` | `wcb_index` `:1395-1416` | The same for the Wizard. `/wcb/Wizard` without the slash redirects (`:1813-1815`). |
| static `/wcb/`, `/_assets/`, `/` | `:1830-1836` | Registration order is load-bearing: `/` matches every path. |
| `GET /_intellex.js` | `shim_js` `:1317-1342` | The shim, with `window.__intellexBranches = {...}` prepended. |
| `GET /_launcher`, `GET /_shell` | `:842-854` | The chooser, and the two-tool window. |
| `GET /wiki/`, `/wiki/{product}/{name}` | `:730-772` | Rendered wikis. Pages get a nonce CSP (`:675-712`); assets are sandboxed (`:681`, `:755-762`). |
| `GET /_api/status` | `:1112-1128` | Returns `attached`, `target`, `lastError`, `reconnecting`, `wantsLink`, `kind`, `role`, `relayId`. |
| `POST /_api/attach` | `:1131-1199` | Body `{kind:serial,port}` or `{kind:ws,host,role?,ssid?,relayId?}`. **The target is set before the attach is tried**, so a failed attach is retried every second until `/_api/detach`. |
| `POST /_api/detach` | `:1229-1231` | Forgets the target and closes the transport. |
| `POST /_api/identify` | `:1202-1226` | **Opens the COM port** and sends it a PING (`discover.identify_serial`). |
| `POST /_api/signals` | `:1234-1257` | Sets DTR/RTS on the attached serial port. |
| `GET /_api/ports` | `:386-390` | `list_serial_ports()`. |
| `GET /_api/discover` | `:393-402` | WS-probes 192.168.4.1 and .19 (`?hosts=` overrides that list). Sends PING, `?RELAY,WIFI` and `?WDP,DUMP`. |
| `POST /_api/wifi-bounce` | `:405-414` | Runs `netsh wlan disconnect`, then `connect`, on the adapter that carries the SSID. |
| `GET /_api/webui-version` | `:502-536` | Bundled vs published stamp for both tools (network). |
| `POST /_api/update-webui?tool=` | `:897-950` | Runs `tools/fetch_webui.py` (network). Rewrites the bundle and keeps `.prev`. |
| `GET /_api/firmware`, `POST /_api/update-firmware` | `:616-654` | Cache summary; runs `tools/fetch_firmware.py`. |
| `GET /_api/branches`, `GET /_api/branch-list`, `POST /_api/branch` | `:573-613` | Branch per product (`settings.py`); GitHub branch list (`branchlist.py`). |
| `GET /_api/gh/contents`, `GET /_api/gh/raw` | `:775-805` | The tools' own GitHub firmware calls, answered from GitHub or from the cache (`ghproxy.py`). |
| `POST /_api/flash`, `POST /_api/flash-wcb`, `GET /_api/flash-status` | `:1055-1109` | Native esptool flash: one at a time, USB only, with a detach before and a reattach after. |
| `GET /_api/wiki`, `POST /_api/update-wiki` | `:808-839` | Wiki inventory; runs `tools/fetch_wiki.py`. |
| `GET /_api/log`, `GET /_api/version` | `:539-570` | Log path plus its last 400 lines; `version.info()`. |
| `POST /_api/open-window` | `:865-894` | Local paths only (400 otherwise). Returns 501 when there is no pywebview hook, which is always the case under `host.py`. |

### 1.4 Every feature: how a test drives and observes it, and its risk

**Risk:**

- **H:** a regression silently costs a board, a config or the droid link.
- **M:** the breakage is visible.
- **L:** cosmetic, or narrow.

| # | Feature | Where | Drive | Observe | Risk |
|---|---|---|---|---|---|
| A1 | Headless host | `host.py:1840-1898` | start with `--port P --no-auto-bounce` | `GET /_api/status` answers 200; process stdout | M |
| A2 | App window | `app.py:141-387`. The port is bound on the main thread with no `SO_REUSEADDR` (`:47-71`). A failure shows a MessageBox (`:89-112`, `:219-226`). pywebview runs with `private_mode=False` and a `storage_path` (`:350-374`). `--inspect` sets `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` (`:209-212`). | `app.py --inspect 9333` | Playwright `connectOverCDP` into the real WebView2 | M |
| A3 | Frozen-build sentinels | `app.py:148-170`: `--run-esptool`, `--run-fetch-webui`, `--run-fetch-firmware`, `--run-fetch-wiki` | `dist/Intellex.exe --run-esptool version` | exit code, stdout | M |
| A4 | Launcher (chooser) | `launcher.html`, see the note below this table | Playwright, with `/_api/ports`, `/_api/identify` and `/_api/discover` fulfilled from fixtures | DOM text and classes, captured request bodies, navigation | M |
| A5 | Shell | `shell.html`, see the note below this table | Playwright | the iframes; `style.zoom` and `width`; `localStorage`; popups; a frame's `timeOrigin` unchanged across the overlay | M |
| B1 | Serial transport | `serial_transport.py`, see the note below this table | Python on COM5 and COM6 | no boot banner on the stream; data arrives as `bytes`; writes raise | H |
| B2 | WebSocket transport | `ws_transport.py` (see §1.2) | Python, through Wi-Fi 2 on NaviCore's AP | PONG; a 13 KB `GET_CONFIG` reassembled; raises on loss | H |
| B3 | Bridge fan-out | `host.py:124-382`: a set of pages, one FIFO pump, suppression per page, a `_gen` counter so a newer attach wins, and the lock never held across I/O | two `/_link` clients | identical byte streams (compared by hash) under load | H |
| B4 | Reconnect loop | `host.py:1518-1702`, see the note below this table | drop the far end (a reboot), or hop APs | `/_api/status`; the order of log lines | H |
| C1 | Tool hosting | `host.py:1313-1416`, `:1817-1836` (see §1.3) | HTTP GETs | body equals the file on disk plus one shim tag; headers | M |
| C2 | Shim core | serial shim `:56-299`; reload keys `:375-380`; the "still trying" banner and `watchLink` `:1541-1596` | Playwright | `navigator.serial`; `[Intellex]` console lines; the banner | H |
| C3 | NaviCore-tool glue | `autoConnect`, which forces Via WCB for role `wcb` or `relay` (`:396-448`); native flash buttons (`:539-607`, `:670-738`); OTA relabel (`:630-666`); transport label (`:1414-1464`) | Playwright on COM5, or over Wi-Fi | status text; `viaWcbActive`; button titles and labels | H |
| C4 | Wizard glue | intellex_shim.js, see the note below this table | Playwright on COM6, or through NaviCore | `boardBaselines`; `remoteRelayForBoard`; chip text | H |
| C5 | GitHub fetch rewrite | shim `:472-495` redirects to `/_api/gh/contents` | Playwright, opening a tool's Firmware tab | the request URL; the `X-Intellex-Source` header | H (offline OTA) |
| D1 | Firmware cache | `fwcache.py`, see the note below this table | seed a test branch | `/_api/firmware`; the files | M |
| D2 | NaviCore flash | `flash.py`, see the note below this table | the Update Firmware button, on COM5 | the `/_api/flash-status` log; PONG afterwards | H |
| D3 | WCB flash | `wcb_flash.py`, see the note below this table | Wizard Update FW, on W2 | the log; W2 `?VERSION`; config unchanged | H |
| D4 | Flash orchestration | One claim covers both tools (`host.py:979-1013`); USB only (`:1002-1009`); detach, run esptool, always reattach (`:1016-1052`); progress never goes backwards (`:972-976`) | two panes | the 409s; status JSON | H |
| E1 | Branch per product | `settings.py`: whitelist `[A-Za-z0-9._/-]`, at most 100 characters (`:37`, `:61-86`); read at call time; `tool_base()` for `/dev/<branch>/` (`:104-110`) | `POST /_api/branch` | the 400s; `settings.json`; the `/_intellex.js` prelude; the tools' `localStorage` | M |
| E2 | Branch list | `branchlist.py`: a 6 h TTL, but refetched once per run (`:45-55`); `gh-pages` dropped (`:118-122`); the current branch and `main` always present (`:147-155`); `missing` (`:144-145`) | `/_api/branch-list`, with or without `?refresh=1` | JSON | L |
| E3 | GitHub proxy | `ghproxy.py`, see the note below this table | `/_api/gh/*` | body; headers; the 502s | H |
| E4 | Tool update | `tools/fetch_webui.py`, see the note below this table | `/_api/update-webui`, in a staged copy | files byte-identical to Pages | M |
| E5 | Firmware pre-fetch | `tools/fetch_firmware.py`: both WCB families and both S3 sizes (`:52-70`) | `/_api/update-firmware`, in a staged copy | cache summary | M |
| F1 | Wiki viewer | `wikidocs.py`, `host.py:668-772` and `wiki.html`, see the note below this table | a seeded wiki | rendered HTML; the CSP; no script runs from wiki content | M (security) |
| F2 | Wiki download | `tools/fetch_wiki.py`: images capped at 1 MB (`:90`); one swap per wiki | `/_api/update-wiki`, in a staged copy | `/_api/wiki` | L |
| G1 | Paths | `paths.py`, see the note below this table | unit test with `FROZEN` patched; `LOCALAPPDATA` | which directory is chosen | M |
| G2 | Log | `applog.py`: one file per run, teed; keeps 20 runs across both name prefixes, sorted by timestamp (`:33`, `:91-106`); the header records argv (`:131-137`) | start hosts | `/_api/log` | M (prunes Greg's real logs if not isolated) |
| G3 | Version | `version.py`: the build stamp first, then git with `+dirty` (`:65-67`) | `/_api/version` | JSON | L |
| H1 | Discovery | `discover.py`, see the note below this table | Python through Wi-Fi 2; the launcher | the returned dicts | M |
| I1 | Window size and icon | `winsize.py` (work area, DPI-normalised, `:49-84`); `appicon.py` | unit test with the work area patched; window mode | geometry | L |
| J1 | Certificates | `certs.py`: the system store, else certifi, never unverified; `is_cert_error` walks the cause chain (`:51-66`) | unit test | contexts; messages | L here (M on macOS) |
| K1 | Packaging | `Intellex.spec`, `scripts/build-windows.bat`, `scripts/install-windows.ps1` | a build (heavy side effects) | `dist/Intellex.exe` runs | M |
| L1 | Security surface | Origin guards (`host.py:1261-1286`, `:1733-1757`); loopback bind (`:119-121`); wiki CSP and escaped titles (`:685-712`); open-window takes local paths only (`:878-884`); the proxy is not open (`ghproxy.py:57-61`) | crafted requests | 403, 400 or 502; no script runs | H |

Notes for the longer rows:

- **A4, Launcher** (`launcher.html`):
  - *Scanning.* `scan()` `:771-866` lists ports, then sends **`/_api/identify` to every 303A port** (`:805`,
    `:830-855`). `scanWifi()` is at `:681-760`, and the watch while a droid boots at `:675-769`.
  - *Selecting.* Auto-select runs once (`:419-423`). Selection panel `:458-489`; filter tabs `:495-511`.
  - *Settings.* Theme `:521-543`. Views, with `wcb` as the default, `:553-606`.
  - *Connecting.* Connect `:647-657`; opening the tools, including `sep`, `:628-645`.
  - *Maintenance.* Summary lines `:348-394`; firmware `:907-941`; branches `:966-1090`; docs `:1093-1141`; version
    `:1146-1154`; log `:1156-1173`; update `:1176-1191`; back and offline `:1196-1232`.
- **A5, Shell** (`shell.html`):
  - Frames are never destroyed (`:143-163`), and **both panes are preloaded after 4 s** (`:182-188`).
  - Views and the URL: `:130-217`. Split view with zoom-to-fit (`DESIGN_W` 1180, minimum zoom 0.55): `:219-293`.
    Drag: `:300-323`.
  - New window moves the tool rather than copying it (`:348-375`). Transport label: `:380-396`. Chooser overlay:
    `:397-444`.
- **B1, Serial transport** (`serial_transport.py`):
  - DTR and RTS are set low **before** `open()` (`:43-66`).
  - 115200 baud, a 50 ms read timeout, reads of up to 4 KB (`:19-24`).
  - Writes take a lock, have a 2 s write timeout, and **raise** on loss (`:91-106`).
  - The reader closes the port before reporting a loss (`:122-137`).
  - `set_signals` writes DTR before RTS (`:108-119`).
- **B4, Reconnect loop** (`host.py:1518-1702`):
  - Polls every second.
  - A TCP probe, gated on idle: 6 s of silence and 3 failures (`:1554-1593`).
  - An adapter bounce only when the route is wrong: gives up after `BOUNCE_GIVE_UP` (3) failures, 10 s cooldown
    (`:1633-1696`).
  - When the SSID has changed, it re-identifies the board **before** reattaching (`:1447-1515`, `:1605-1627`).
- **C4, Wizard glue** (`intellex_shim.js`):
  - Disables port sharing (`:784-794`), retires the branch toast (`:812-832`), dismisses the splash (`:856-877`) and
    sets `rc_config_tool_url='/'` (`:360-366`).
  - Auto-connects by **port, not slot** (`:906-993`).
  - Routes the mesh through a relay (`:1019-1106`), or through a plain WCB (`:1146-1296`).
  - Replaces `flashFirmware` (`:1318-1395`); draws the chip (`:1502-1528`).
- **D1, Firmware cache** (`fwcache.py`):
  - One directory per (product, branch), with `/` in a branch name flattened (`:81-89`).
  - Atomic write through a `.part` file and a rename (`:108-125`).
  - A manifest and a cached listing (`:180-204`); the summary covers the configured branch (`:143-168`).
- **D2, NaviCore flash** (`flash.py`):
  - A reachability probe per host (`:49-147`) that fails fast when offline (`:150-175`). GitHub retries honour
    `Retry-After` (`:214-254`).
  - The app name is anchored; the bootloader and partition table are both-or-neither (`:297-362`).
  - otadata is always erased, NVS only on Full Wipe (`:365-388`).
  - esptool at 921600, with the flash parameters left at `keep` (`:408-455`).
- **D3, WCB flash** (`wcb_flash.py`):
  - `detect()` runs `flash-id` before anything else (`:246-286`).
  - Exactly one app image, or it refuses (`:112-134`). App, part and boot come from one build tag.
  - The S3 bootloader must match the chip's 8 or 16 MB (`:190-228`).
  - A full flash is refused without a bootloader or partition table (`:309-323`). NVS is erased only on Factory
    Reset (`:326-339`).
- **E3, GitHub proxy** (`ghproxy.py`):
  - Only the two firmware repos and their bin directories (`:47-61`, `:82-84`).
  - A listing is stored only after it parses as one (`:112-116`), and `download_url` is rewritten to point at the
    host (`:118-124`).
  - Filenames must be bare (`:132-133`).
- **E4, Tool update** (`tools/fetch_webui.py`):
  - Probes reachability once (`:451-455`).
  - Uses the branch's tool, or main's with a note (`:81-102`).
  - Explicit file lists (`:113`, `:125-150`, `:171`).
  - An atomic swap that keeps `.prev` (`:327-342`); a `--base` override (`:438-439`).
- **F1, Wiki viewer** (`wikidocs.py`, `host.py:668-772`, `wiki.html`):
  - Links are rewritten in the markdown renderer's rules.
  - Raw `<img>` tags are rewritten (`:170-206`), and path traversal is refused (`:71-85`).
  - The page carries one nonce'd script.
- **G1, Paths** (`paths.py`):
  - The user data dir is `%LOCALAPPDATA%\Intellex` (`:81-98`). A one-shot move from the old `NaviLink` name
    (`:44-78`).
  - Frozen builds prefer the user copy when it is non-empty (`:101-117`). Wikis decide per subdirectory (`:120-139`).
  - Frozen builds write updates to the user dir (`:142-144`).
- **H1, Discovery** (`discover.py`):
  - `probe()` separates a direct PONG, a mesh PONG and the SELF row (`:259-409`). `scan()`: `:412-440`.
    `identify_serial()`: `:142-224`.
  - `ssid_for_host()` makes read-only netsh calls (`:461-518`). `wifi_bounce()` is scoped to one adapter
    (`:521-594`).
  - The macOS path (`:597-750`); hints (`:753-781`).

### 1.5 Where Intellex reaches into the two tools (the drift surface)

Intellex depends on names inside files it does not own. Every one of them is present today.

**Wizard** (`Wireless_Communication_Board-WCB/Wizard`):

- *State (`app.js`):* `boardConnections`, `boardConfigs`, `boardBaselines` (`:42-44`); `remoteRelayForBoard`
  (`:58`); `_pullingBoards` (`:60`); `UI_VERSION` (`:116`); `latestFirmwareVersion` (`:128`, a top-level `let`);
  `_meshBoards` (`:402`); `_meshClients` (`:407`).
- *Functions (`app.js`):* `splashGoConfig` (`:314`); `relayManageOne` (`:534`); `relayRouteAll` (`:548`);
  `renderRelayCard` (`:590`); `establishConnection` (`:5946`); `_modalDoConnect` (`:6240`); `setRemoteConnected`
  (`:6271`); `boardGo` (`:7365`); `remoteBoardPull` (`:8445`); `renderWdpMesh` (`:13170`); `_wdpMeshConn`
  (`:13273`). `flashFirmware` lives in `flasher.js:302`.
- *Other hooks (`app.js`):* the branch-toast text (`:344`); `rc_config_tool_url` (`:12895`); the WDP `client` flag
  (`:13095`).
- *DOM (`index.html`):* `#relay-cards` (`:193`); `#toast-container` (`:411`); `#section-board-*` (`:415`);
  `#splash-overlay` (`:1001`).

**NaviCore tool** (`NaviCore/config_tool`):

- *Functions and state (`index.html`):* `connectDirect` (`:4639`); `onViaWcbToggle` (`:5402`); `viaWcbActive`
  (`:5357`); the PONG handler, which accepts any PONG (`:9515`).
- *DOM (`index.html`):* the flash buttons and progress ids (`:3245-3277`); `#status-text` and `#btn-connect`
  (`:1010-1011`); `#footer-dtg` (`:2322`).
- *Firmware (`flasher.js`):* `rc_fw_branch` (`:51`); the Contents URL (`:68`).

A runtime contract spec (IX-WP4) that asserts every one of these catches a cross-repo break before a user does.

### 1.6 Found while reading (verify, then fix or file)

1. **The shim cannot label a flashed board.**
   - `window.latestFirmwareVersion = st.version` (`intellex_shim.js:1390`) only creates a property on `window`.
   - The Wizard's `latestFirmwareVersion` is a top-level `let` (`Wizard/app.js:128`), and the shim's own comment
     (`:1176-1181`) says assigning on `window` cannot reach one of those.
   - `boardGo` therefore labels the card with the page's GitHub "latest", not with the build the host actually wrote
     (`app.js:7586-7587` on 2026-09-29). Confirmed in the page: `intellex.ui_latest_fw_version` (`(should)`).
2. **Two Wizard images are never bundled.**
   - `../Images/LabelOnly.jpg` (`Wizard/app.js:10691`, the identity step) and `../Images/PololuLogo.png` (`:10952`, the
     Maestro step) are missing from `WCB_IMAGES` (`tools/fetch_webui.py:150`).
   - Inside Intellex, the Setup Wizard's hardware-version and Maestro steps show broken images. Confirmed in the page:
     `intellex.ui_wizard_setup_images` (`(should)`).
3. **`identify_serial()` accepts any PONG** (`discover.py:221-222` at `e9f95f2`). Its own docstring says only a direct
   PONG counts (`:154-158`), and `probe()` does tell the two apart (`:351-363`). Low impact today, because the launcher
   only asks 303A ports (`launcher.html:805`). `intellex.identify_direct_pong` is its `(should)` test (DX11).
4. **A USB-cabled WCB doorway probably leaves the NaviCore tool out of Via WCB mode.**
   - Intellex records a role only for a `ws` attach (`host.py:1138-1182`). A serial attach reports `role: ""`, so the
     shim never forces Via WCB (`intellex_shim.js:440-444`).
   - The NaviCore tool accepts any PONG (`NaviCore/config_tool/index.html:9515`). A WCB broadcasts an unprefixed JSON
     PING to the mesh and mirrors NaviCore's mesh PONG back (`discover.py:26-33`, `docs/HIL_TESTING.md:277`).
   - The shell preloads the NaviCore pane even when only the Wizard was chosen (`shell.html:182-188`); the launcher's
     default view is `wcb` (`launcher.html:568-573`).
   - So picking W1's COM port likely yields a NaviCore tool that believes it is wired straight to the droid. That is
     rule 10's hazard over USB: `?OTALOCAL` lands on the WCB. Unverified, so IX-WP7 writes it as a `(should)` test.
   - Narrowed while writing it (2026-09-29): over USB, W1 prints NaviCore's reply to that PING - unicast back to W1,
     `rc_telemetry.h:2186-2192` - only inside its 20 s relay window, which a `;W20,{...}` line from USB opens
     (`WCB.ino:8019-8021`, gate at `:5513`); a bare JSON line never does. So a cold connect falls back to Via WCB as it
     should, and the hazard is a connect while the window is open: a reload (Intellex's F5), or the shim's reconnect,
     within 20 s of the tool's last Via-WCB line - its keep-alive is every 10 s. `intellex.nc_via_usb_doorway` makes
     both connects; in the dry run against a simulated W1 the cold one went Via WCB and the reload came up direct, with
     "Update over USB (OTA)" enabled.
5. **Update FW does not fully match the Wizard's.**
   - `wcb_flash.write_list(app_only=True)` writes the app alone (`wcb_flash.py:302-307`).
   - `flasher.js` first reads the board's partition table. When it differs, it escalates once to a full,
     NVS-preserving flash (`Wizard/flasher.js:465-511`, `:245-260`).
   - A board still on the default table would take a min_spiffs-sized app (1.38 MB) into a 1.25 MB slot. The module
     header claims parity with `flasher.js` (`wcb_flash.py:11-14`).
   - The bench boards are min_spiffs, so only a unit test can show this: `intellex.flash_update_partition_escalates`
     (`(should)`, confirmed: an Update onto a board holding a different table writes 0xe000 and 0x10000 only).
6. **Stale doc about the clone directory.**
   - Intellex `CLAUDE.md:19-21` says the Windows clone is still named `NaviLink`. It is `Intellex`.
   - The venv was created at `...\NaviLink\.venv` (`.venv/pyvenv.cfg`, `command =`). So `.venv/Scripts/esptool.exe`
     and `pip.exe` start with `#!C:\...\NaviLink\.venv\Scripts\python.exe`, which no longer exists.
   - `.venv/Scripts/python.exe -m ...` still works, and Intellex itself calls `sys.executable -m esptool`
     (`flash.py:402`).
7. **Revision rows inside a table.** In `docs/WCB_WIZARD.md:381-390`, three 2026-09-07 revision rows (`:383-385`)
   are spliced into the "Flashing a WCB" flash-map table instead of the Revision log.
8. **Another project's file in a public repo.** `src/5G Monitoring Icon Design.zip` is tracked. It was added in
   `258cf8e`, a revision-log commit, and is on `origin/main`.
9. **Stale status text.** `README.md:23` says flashing is "Written, not yet run against a board". `README.md:179-180`
   and `transport.py:104-118` still describe the transports as unwritten.
10. **The build stamp shadows git.** A build leaves `src/build_stamp.py` behind (gitignored), and `version.info()`
    prefers it (`version.py:65-67`). A run from source therefore reports the build's commit (`d615344`,
    2026-09-11), not the working tree's.
11. **A commit that is not pushed yet.** Local `main` holds `d615344` ("Wizard: reconnect the link at its own slot"),
    and its doc row says "Not yet verified on hardware" (`docs/WCB_WIZARD.md:546`). The first Intellex push
    (IX-WP1) publishes it too.

Found while writing IX-WP3 to IX-WP6 (2026-09-29, Intellex `e9f95f2`). Each has a `(should)` test that fails until it
is fixed; none can cost a board.

12. **The branch whitelist lets `..` through.** `settings.py:33-37` says the whitelist keeps URL operators such as
    `/../` out, but `^[A-Za-z0-9._/-]+$` accepts `../x` (`valid_branch`, `:61-62`), and `tool_base()` puts the branch in
    a URL path (`:104-110`), so the tool update would fetch from another of Greg's Pages sites. Only the launcher (same
    origin) can set it. `intellex.settings_branch_dotdot`.
13. **Flash progress never moves under esptool 5.** Intellex installs esptool 5.3.1 (`requirements.txt:17` asks for
    `>=4.7`), which prints `Writing at 0x00010000 [=====>    ]  25.0% ...` (esptool `logger.py:223-248`). `_PCT_RE`
    (`flash.py:414`, used at `:459` and by `wcb_flash.py:38-39`, `:390`) matches esptool 4's `(25 %)` only, so `/_api/flash-status`
    reads 0 % for the whole write and then 100. `intellex.flash_esptool5_output`.
14. **The NaviCore flash still spells esptool 4's options.** `flash.py:434-438` passes `write_flash`, `--flash_mode`,
    `default_reset` and `hard_reset`, and esptool 5 answers each with a `Deprecated:` warning in the user's flash log
    (esptool `cli_util.py:35-44`, `:350-383`), the thing `wcb_flash.py:352-354` says it avoids. Same test as 13.
15. **`[[wiki links]]` are rewritten inside code.** `_wikilinks_to_md` runs a regex over the whole markdown source
    before parsing (`wikidocs.py:88-101`, called at `:215` and `:227`), so `[[Target]]` in a fenced block or in
    backticks is shown as `[Target](Target)`. The module header says its rewriting is done in the renderer so that code
    and examples are left alone (`:9-16`). Nothing becomes a live link. `intellex.wiki_code_verbatim`.

Harness notes from the same work (not Intellex defects):

- From a git worktree under `.claude/worktrees/<name>`, `hil/intellex.py` looked for Intellex and NaviCore beside
  `.claude/worktrees`, so every Intellex test skipped there. Fixed: `github_dir()` resolves the main checkout's parent
  (`selftest.py` `intellex_github_dir`).
- A staged host's `/_api/version` names this WCB repo's commit: the stage has no build stamp and no `.git`, and
  `version.py:43-67`'s git fallback walks up into the WCB repo. Nothing compares it; `stage()` logs Intellex's real HEAD.
- Stage paths come close to Windows' 260-character limit. From a worktree (42 characters longer than the main
  checkout) the `include_data` stages of `intellex.smoke_repo_probe_identity` and `_reconnect_identity` fail to copy
  Greg's downloaded wiki images and firmware cache. The venv scripts keep their own branch and file names short.

Found while writing IX-WP7 and IX-WP8 (2026-09-29, Intellex `e9f95f2`, the Wizard and config tool of the day). Each
has a `(should)` test; "confirmed in the page" means against the real shim, host and tool page, with the board
simulated (the IX-WP7/8 status notes).

16. **Through Intellex the Wizard holds W1's GPIO0 low.** Every Wizard connect asserts DTR alone
    (`Wizard/app.js:5364`, RTS left alone on purpose). Chrome's Web Serial `open()` asserts both lines, which on a
    CH9102 or CP210x auto-reset circuit leaves EN and GPIO0 high. Intellex opens with both low
    (`serial_transport.py:65-68`) and passes the page's `setSignals` through (`intellex_shim.js:248-260`,
    `host.py:1239-1260`), so the circuit (DTR drives GPIO0, `docs/HIL_TESTING.md` §2) holds GPIO0 low for the whole
    session. A restart samples the strapping pins; if it samples GPIO0 low, W1 boots the ROM loader ("waiting for
    download") and stays there until an EN reset - `?reboot`, a push that ends in a restart, an OTA's final restart.
    Confirmed: the DTR assert reaches the host (the dry run's `wizard_pull_w1` note: `{"dataTerminalReady":true}->200`).
    On the bench (`20260929-110148`) W1 restarted with `?reboot` under the Wizard's DTR booted its app:
    `intellex.wizard_reboot_w1_boots_app` (`(should)`) passes, so the hazard is not reproduced here. The test stays,
    for a board or bridge that samples GPIO0 on a software restart; when it fails, the harness resets W1 into its
    app (`hil/intellex.py reset_into_app`).
17. **The shim files mesh boards under a string relay slot.** `routeMeshThroughBoard` passes `_wdpMeshConn().slot`,
    a key of `Object.entries(boardConnections)` (`Wizard/app.js:13420-13428`), to `setRemoteConnected` and
    `remoteBoardPull` (`intellex_shim.js:1264`, `:1279`), so `remoteRelayForBoard[2]` is `"1"`. The Wizard's own
    entry points pass numbers (`app.js:538` relayManageOne, `:6364` modalRemoteConnect; `:2022` re-passes the stored
    value), and its relay-slot tests are `===` against numbers: the `[TERM:n]` demux (`:5790`), the boards re-armed
    after a relay drop (`:5939-5942`) and `clearRemoteBoardsForRelay` (`:6432-6436`). A board the shim routed is
    therefore neither cleared nor re-armed when W1's link drops. Confirmed in the page:
    `intellex.wizard_mesh_relay_slot_number` (`(should)`).

Found while writing IX-WP9 and IX-WP10 (2026-09-29, Intellex `e9f95f2`):

18. **A WCB flash Intellex refuses after detecting the chip leaves the board in its ROM loader.**
    - `wcb_flash.detect()` runs `esptool --chip auto --before default-reset --after no-reset flash-id`
      (`wcb_flash.py:254-256`): esptool resets the chip into its ROM loader, loads its stub and leaves it there
      (`Staying in bootloader.`). That is right on the way to a write, whose `--after hard-reset` boots the app.
    - Every refusal after detection raises out of `wcb_flash.flash` (`:398-415`) with nothing written: no cached image,
      two app images (`_pick_app` `:128-133`), a full flash of a build with no bootloader or table (`write_list`
      `:309-323`). `host.py` `_start_flash_job` (`:1021-1057`) then reports the error and reattaches the port, opened
      with DTR and RTS low (`serial_transport.py:65-68`), and nothing resets the chip. The board answers nothing until
      someone presses EN or replugs it; the Wizard shows "Flash failed" and then a board that never becomes ready.
    - The Wizard's own flasher gets out of this by closing and reopening its port, whose DTR/RTS toggle resets the
      board (`Wizard/flasher.js:736-761`, the comment on the reset).
    - Confirmed at the host in a dry run (the real host, a fake esptool and port: only `flash-id --after no-reset` ran,
      nothing was written, the port was reattached). `intellex.flash_refused_board_runs` (`(should)`) makes the
      refusal on W2 and then asks it for `?VERSION` over a port opened with both lines low; the harness resets a W2 it
      finds in the loader. It costs no flash write either way: a refusal that did not hold would write the image W2
      already runs.
    - Confirmed on the bench (run `20260929-203453`): after the refused flash W2 answered nothing on its own port until
      the harness's EN pulse (RTS, DTR low).

Found on the bench in IX-WP9 (run `20260929-202852`, Intellex `e9f95f2`):

19. **Dropping a dead WebSocket link stalls the whole host for 10 s.**
    - The reconnect loop's liveness probe declares a silent link dead - "<host> unreachable after <n>s idle — link is
      dead" - and calls `bridge._drop()` on the event loop itself (`host.py:1583-1596`). `_drop()` closes the transport
      (`:249-256`), and `WebSocketTransport.close()` (`ws_transport.py:89-99`) runs websockets' closing handshake, which
      waits `close_timeout` - 10 s by default (websockets 17.1, `sync/connection.py:1024-1040`) - for a close frame that
      a vanished access point never sends. Nothing else runs meanwhile: `/_api/status`, every page's `/_link`, the flash
      status. The loop's own comment on `open()` (`host.py:1604-1609`) names the same failure for the open: "while a
      droid was unreachable the whole server stopped answering: /_api/status timed out, and the page went dead".
    - On the bench, `intellex.wifi_link_loss`: NaviCore's REBOOT through the link at 0 s, the probe's verdict at 11.8 s,
      then "keepalive ping failed ... timed out while closing connection" 10.04 s later, and not one status poll
      answered in between: the lost link showed at 21.9 s, against the ~15 s Intellex's own comments promise
      (`ws_transport.py:73-74`). The probe's "~9 s" (`host.py:1439-1440`) is ~11-12 s in practice: each failed probe
      spends its 1 s timeout beside the loop's 1 s sleep.
    - Related, and why that run could not say more: a failed reattach is silent. `except TransportError` (`host.py:1638`)
      counts the failure and, with the bounce off or not due (`:1655-1671`), neither prints it nor keeps it in
      `last_error`, so `/_api/status` said "probe failed (droid AP down?)" for the whole minute the reattach kept
      failing, and the log cannot say why.
    - Reproduced with no board: `intellex.link_drop_no_stall` (`(should)`, s32) attaches a staged host to a stand-in
      endpoint on 127.0.0.2:80 (`hil/ws.py` `WsEndpoint`) that then vanishes - its listener closed, the open socket kept
      open and unanswered - and polls `/_api/status` every 0.25 s: 10.3 s unanswered and the loss shown 10.0 s after the
      verdict, with the same verdict line and traceback as the bench.
    - Fix shape, for Intellex: drop the link off the event loop (`await asyncio.to_thread(bridge._drop)`), and close a
      link already judged dead with a short `close_timeout`.

---

## 2. Running Intellex under test

### 2.1 What exists today

| Knob | Where | Effect |
|---|---|---|
| `host.py --port N` | `host.py:1842`; default 8765 at `:118` | Listens on `127.0.0.1:N` (loopback always, `:121`). |
| `host.py --serial COMx` or `--ws IP [--ssid S]` | `:1843-1846`, `:1851-1867` | Attaches at startup. The two are mutually exclusive. **`--ws` cannot set `role` or `relayId`**: only `POST /_api/attach` can. |
| `host.py --no-auto-bounce` | `:1847-1848`, `:1858-1859` | Turns the WLAN adapter bounce off. **Mandatory for any test host** (Intellex `CLAUDE.md:444-465`). |
| no window, no browser | `host.py:857-862` (never imports webview), `:1877` | `host.py` is already the headless mode. `/_api/open-window` answers 501, and the pages fall back to `window.open()`. |
| `app.py --port/--serial/--ws/--ssid/--no-auto-bounce/--dev/--inspect [9333]/--browser` | `app.py:182-195` | Window mode. There is **no headless flag**: it always opens a pywebview window or the default browser (`:258-317`), and shows a modal MessageBox when it fails (`:89-112`). |
| `app.py --run-esptool` and the `--run-fetch-*` flags | `app.py:148-170` | Frozen-build sentinels. They also work from source. |
| `LOCALAPPDATA` (env) | `paths.py:89-91` | Moves the whole user data dir on Windows: `settings.json` (`settings.py:40-41`), logs (`applog.py:118`), `branches_cache.json` (`branchlist.py:63-64`) and webview storage (`app.py:363`). In frozen builds it also holds the updated webui, webui_wcb, firmware and wiki. |
| the bundle dir | `paths.py:40-41`, `:113-117`, `:142-144`; `fwcache.py:65-78` | Run from source, the bundles and the firmware cache are read **and written** in the directory `paths.py` sits in (`src/`). |
| `settings.json` | `settings.py:84-86` | `{"branch":{"navicore":..,"wcb":..}}`. Seed the file, or use `POST /_api/branch`. |

**Startup side effects of `host.py`:**

- `applog.start()` creates a log and prunes old ones down to 20 (`:1874`).
- `_published_dtg()` probes github.io and reads the head of `index.html` (`:1878-1890`), which can take up to about
  9 s plus a 10 s read.
- `build_app` creates `webui/` and `webui_wcb/` under the bundle dir (`:1825-1826`).
- The reconnect loop starts (`:1721-1722`).

**What does not happen on its own.** Nothing opens a COM port except an attach or `/_api/identify`. Nothing touches
WiFi except:

- `/_api/discover`;
- a `ws` attach, through `ssid_for_host`, which runs read-only netsh (`:1170`);
- the bounce.

**What an unisolated test would clobber.** Greg's real state right now:

- `%LOCALAPPDATA%\Intellex\settings.json`: WCB on `WIFI`, NaviCore on `main`.
- 21 log files, which a 20-run prune would start deleting.
- `src/webui_wcb`, whose Wizard is `UI_VERSION 04.13:06.R.SEP.2026`. The WCB working tree is
  `25.06:46.R.SEP.2026` (`Wizard/app.js:116`), so the bundled Wizard predates the F13 pull-in-parts protocol.
- `src/firmware`: `navicore/main` and `wcb/WIFI`.

**Ports already taken on this PC:**

- 8765: Intellex's default.
- 8777 and 8778: the Wizard static servers.
- 8799: a 5G-monitor GUI is listening there now.

Pick a free ephemeral port for every test.

### 2.2 The recipe (no Intellex change needed)

1. **Stage** `results/<run>/intellex/<test_id>/`.
   - Copy `Intellex/src`, without `webui*`, `wiki*`, `firmware`, `__pycache__`, `build_stamp.py` or `*.zip`. Copy
     `Intellex/tools` as well.
   - `host.py` finds `tools/` as `parent.parent/tools` (`host.py:637`, `:820`, `:930`), and `paths.BUNDLE_DIR`
     becomes the staged `src/`. So nothing in Greg's checkout or user data can be written.
2. **Seed the bundles.** Mode `worktree` is the default:
   - `webui/`: NaviCore `config_tool/{index.html, flasher.js, serial-hub.js}` and `cmdlib/{droidnet,navicore}/**`
     (27 files), plus `webui/Images/{qr-code.png, r2logo.png}` from `NaviCore/Images`. This is the set
     `fetch_webui.py` builds (`:113`, `:171`, cmdlib `:238-301`): 32 files, the same count as Intellex's bundle.
   - `webui_wcb/Wizard/`: the 12 files `fetch_webui.py:125-146` lists, taken from WCB `Wizard/` (all present), plus
     `webui_wcb/Images/{r2logo, navicore-icon, kyberLogo, qr-code}.png` (`:150`).

   Mode `shipped` instead copies Intellex's own `src/webui` and `src/webui_wcb`.
3. **Seed the firmware cache** (flash and OTA tests only). See IX-WP10.
4. **Seed** `appdata/Intellex/settings.json` with the branches the test wants.
5. **Launch** `<Intellex>/.venv/Scripts/python.exe <stage>/src/host.py --port <free> --no-auto-bounce`.
   - Environment:
     - `LOCALAPPDATA=<stage>/appdata`.
     - `PYTHONUTF8=1`. The host prints `—` and `→`, and a cp1252 pipe would raise on them; `host.py:1864` prints
       before the log tee starts.
     - `INTELLEX_OFFLINE=1` (H1).
     - `INTELLEX_SERIAL_ALLOW=<the one COM port>` (H2).
     - `INTELLEX_DISCOVER_HOSTS=` (H3). Leave it empty unless the test is about discovery.
   - Start it with `CREATE_NEW_PROCESS_GROUP`, and send its stdout to `session.log` tagged `intellex`.
6. **Wait until ready:** poll `GET /_api/status` for up to 30 s (about 2 s with H1).
7. **Attach with `POST /_api/attach`,** not `--serial`. Errors then come back as JSON. Follow a failed attach with
   `POST /_api/detach`, because attach records the target first (`host.py:1186-1189`) and would keep retrying.
8. **Tear down** in `finally`, always:
   - `POST /_api/detach`;
   - kill the process tree (`taskkill /T /F`, like `hil/wizard.py:41-61`);
   - check that the harness can reopen the COM port;
   - copy `appdata/Intellex/logs/*.log` next to the report;
   - delete the stage, unless the test failed.

```
C:\Users\ghulette\Documents\GitHub\Intellex\.venv\Scripts\python.exe <stage>\src\host.py --port 51873 --no-auto-bounce
    env  LOCALAPPDATA=<stage>\appdata  PYTHONUTF8=1  INTELLEX_OFFLINE=1  INTELLEX_SERIAL_ALLOW=COM6  INTELLEX_DISCOVER_HOSTS=
POST http://127.0.0.1:51873/_api/attach   {"kind":"serial","port":"COM6"}
```

Never run `.venv\Scripts\esptool.exe` or `pip.exe` (finding 6). Always use `python.exe -m`.

### 2.3 Rules for any test host

- **Always pass `--no-auto-bounce`.** Never POST `/_api/wifi-bounce`, except in the one attended bounce test.
- **Guard the launcher.** Never load `/_launcher`, or the shell's chooser overlay, unless H2 and H3 are set or the
  routes are fulfilled. On load, `scan()`:
  - POSTs `/_api/identify` to every 303A port. That includes the SBUS controller on COM4, which may reset when its
    port opens (`docs/HIL_TESTING.md:169`), and NaviCore on COM5.
  - calls `/_api/discover`, which WS-probes 192.168.4.1 through Wi-Fi 2.
- **Never point a test at Greg's `src/` or his real `LOCALAPPDATA`.**
- **Never leave a host running.** It reopens a wanted target every second, forever (`host.py:1595-1632`). The COM11
  incident was exactly this: `Intellex.exe` held probe2's port for hours (`docs/HIL_TEST_AUDIT.md:702-704`).
- **Preflight: Greg's own Intellex must not be running.**
  - The preflight looks for `Intellex.exe`, and for a Python process running `src/app.py` or `src/host.py`. If one is
    running, it **skips** with a message and never kills anything.
  - This matters beyond the COM port: an Intellex session attached to NaviCore's AP made NaviCore re-arm `?RTERM` on
    W1 once a second, which blocked W1's deferred reboots (F21, `docs/HIL_TEST_AUDIT.md:506-512`).
- **Specs never receive, log or compare a credential.**
  - NaviCore's `GET_CONFIG` carries `wifiPassword` in clear (`NaviCore/rc_config.h:1224-1226`). Config pulls carry
    the mesh password.
  - Follow the same discipline as `tests/wizard/specs/remote_pull.spec.js:1-7`: the harness hands a spec lengths,
    versions and keys, never tokens.

### 2.4 Testability hooks to add to Intellex (pushed to its `main`)

Each hook is a few lines. Each comes with a no-hardware smoke tool, `tools/smoke_test_hooks.py`, and a mention in
Intellex `CLAUDE.md` § Verifying, in the same commit (`CLAUDE.md:544-548`).

Before editing, follow Intellex's own rules:

- `git fetch --all --prune` and check every branch (`CLAUDE.md:51-67`);
- commit to `main`, with no feature branch;
- set the exec bit through the index on any new `.command` or `.sh` file.

**H1. `INTELLEX_OFFLINE=1` makes GitHub unreachable, deterministically.**

- **Change:** `flash.reachable()` (`flash.py:64-147`) first checks the variable, then records
  `(now, False, "INTELLEX_OFFLINE is set")` in `_reach` and returns `False`. `flash.py` needs an `import os`.
- **Why it reaches everything:** every network consumer already asks this one probe:
  - the host's version check (`host.py:448-450`, `:484-486`);
  - the branch list (`branchlist.py:111`, `:135-137`);
  - the proxy (`ghproxy.py:93-104`, `:138-150`);
  - both flashers (`flash.py:267-275`, `:324-332`; `wcb_flash.py:94-102`, `:169-177`);
  - `fetch_webui` (`:451-455`), `fetch_wiki` (`:334-336`) and `fetch_firmware`.
- **What it buys:**
  - the con-floor workflow, which is Intellex's core design point, becomes testable;
  - a 2 s startup;
  - no GitHub rate limit (60 API calls an hour, unauthenticated);
  - the fetchers run as subprocesses, so they inherit it.
- **Why an env var rather than monkeypatching:** `wcb_flash` binds `reachable` at import
  (`from flash import reachable`, `wcb_flash.py:38`), so patching `flash.reachable` in-process misses it.

**H2. `INTELLEX_SERIAL_ALLOW=COM5,COM6` restricts serial ports (a comma list; case-insensitive; unset means no
restriction).**

- **Change:** one helper, `transport.serial_allowed(path)`, used in four places:
  1. `list_serial_ports()` filters its result (`transport.py:83-101`).
  2. `SerialTransport.open()` raises `TransportError("<port> is not in INTELLEX_SERIAL_ALLOW")` before touching the
     port (`serial_transport.py:39-66`). This one guard covers `/_api/attach`, `--serial`, the reconnect loop and the
     reattach after a flash.
  3. `identify_serial()` returns `{"version": None, "busy": False}` without opening (`discover.py:142-188`).
  4. `_claim_port_for_flash()` answers 409 (`host.py:997-1013`), because esptool opens the port itself and bypasses
     the transport.
- **What it buys:** the launcher, the shell overlay and any stray reconnect can no longer touch COM4, COM11, COM15 or
  COM16.
- **Why not only Playwright route interception:** Python-level tests and background loops need the same protection.

**H3. `INTELLEX_DISCOVER_HOSTS=192.168.4.1` sets the discovery candidates (a comma list; set but empty means probe
nothing).**

- **Change:** in `discover.scan()` (`discover.py:412-415`), an explicit `?hosts=` still wins; otherwise the variable
  replaces `DEFAULT_CANDIDATES` (`:69-71`).
- **What it buys:** the launcher's WS probes send PING, `?RELAY,WIFI` and `?WDP,DUMP`. With this, they reach a droid
  only when a test wants them to.

**H4 (optional). `INTELLEX_DATA_DIR`.** `paths.user_data_dir()` returns it when set and skips the legacy migration
(`paths.py:81-98`). `LOCALAPPDATA` already does this on Windows, so this hook is only for clarity and for macOS.

**H5 (optional). `app.py --headless`.**

- **Change:** after `_wait_until_up()` succeeds (`app.py:241-250`), print `serving <url>` and wait on an Event until
  interrupted, then call `bridge.detach()`. Also make `_tell_user()` return early (`:89-112`).
- **What it buys:** an unattended test of the frozen `dist/Intellex.exe`, with no window and no modal. Source tests
  do not need it.

---

## 3. Test plan

### 3.1 Where it lives (decision D3: every HIL test is in the WCB repo)

```
Wireless_Communication_Board-WCB/
  tests/intellex/
    package.json           @playwright/test pinned to 1.63.0, as in tests/wizard, so the installed chromium-1243 is shared
    playwright.config.js   no webServer; baseURL = process.env.INTELLEX_URL; headless; workers 1; timeout 300 s; reporter line
    global-setup.js        standalone runs only: when INTELLEX_URL is unset, stage and start a no-board host
                           (INTELLEX_DIR defaults to ../../../Intellex)
    lib/fixtures.js        page fixture: records pageerror, [Intellex] console lines and responses >= 400;
                           guard() fulfils /_api/identify|discover|wifi-bounce|update-* unless a spec opts in;
                           hilCtx is borrowed from ../../wizard/lib/hil.js
    lib/intellex.js        openTool(), frame('nc'|'wcb'), rawLink(), waitAttached(), stats()
                           rawLink(): a read-only /_link client (Node 24 global WebSocket), the oracle for
                           "no boot banner" and for byte counts
    fixtures/              a seeded wiki, fake ports and discover payloads, recorded esptool 5.3.1 output,
                           a fake esptool package
    specs/                 ui_*.spec.js  wizard_*.spec.js  nc_*.spec.js  wifi_*.spec.js  flash_*.spec.js  window_*.spec.js
    py/                    run under Intellex's venv:
                           transports, discover, the flash units, the fake-esptool pipeline, paths/applog/winsize/certs
  tests/hil/hil/intellex.py   stage(), class IntellexHost (start, attach, detach, stop), run_intellex_test(), run_intellex_py()
  tests/hil/hil/ws.py         + read BINARY frames (opcode 0x2; read_until drops them today, :85-100) and send an optional Origin header
                              + WsEndpoint: a stand-in board endpoint that vanishes like a restarting AP (finding 19)
  tests/hil/suites/s32_intellex.py         no board (DX15)
  tests/hil/suites/s33_intellex_bench.py   the transports and the bridge on the boards (IX-WP5, IX-WP6)
  tests/hil/suites/s34_intellex_tools.py   the tools on the boards (IX-WP7, IX-WP8; DX26)
  tests/hil/suites/s35_intellex_wifi.py    over WiFi, through NaviCore's AP (IX-WP9; DX34)
  tests/hil/suites/s36_intellex_flash.py   flashing W2 and NaviCore's app (IX-WP10)
  tests/hil/hil/optin.py      + the intellex_* keys (§3.4)
  tests/hil/bench.json        + "intellex_dir" (default: an Intellex checkout beside this repo), optional "intellex_python"
```

**The harness stays stdlib plus pyserial** (`docs/HIL_TESTING.md:8`):

- *HTTP and WS checks* run in the harness itself (urllib and `hil/ws.py`) against a staged host subprocess.
- *Checks that import Intellex modules* (transports, `discover`, flash logic, in-process fakes) run as scripts under
  `<intellex_dir>/.venv/Scripts/python.exe`. That venv has aiohttp 3.14.3, websockets 17.1, esptool 5.3.1,
  pyserial 3.5, markdown-it-py 4.2.0 and pywebview 6.2.1. The scripts write a JSON results file, which the harness
  parses the way `_outcomes` parses a Playwright report (`hil/wizard.py:71-90`).

### 3.2 How `run.py` launches them

`run.py` imports every module in `suites/` (`run.py:28-31`), so `s32_intellex.py` needs no registration. Its tests
are `intellex.*`, and the GUI groups them as their own area.

`run_intellex_test(bench, test_id, attach=None, device=None, tools="worktree", settings=None, allow_ports=None,
discover_hosts=(), offline=True, env=None, args=None, timeout=300, wiki=False, link_check=None, recover=None)` follows
`run_wizard_test` (`hil/wizard.py:175-245`, `docs/HIL_TESTING.md:563-575`) step for step:

1. Stage and seed the instance (§2.2). Record Intellex's `git rev-parse HEAD`, with `+dirty` when the tree is dirty,
   in `session.log`.
2. If `device` is set, read its VID/PID and release its port with `bench.close_device(device)`.
3. Start the host with the environment from §2.2, and wait for `/_api/status`. When `attach` is given, `POST` it; on
   failure, `POST /_api/detach`, then fail with the error.
4. Serve a `Bridge` (`hil/bridge.py`, unchanged). Its `/context` adds `{intellex: {url, stage, target}}`.
5. Run `node tests/intellex/node_modules/@playwright/test/cli.js test --reporter=line,json --grep <id>`. Use its own
   process group. Set `INTELLEX_URL`, `HIL_BRIDGE` and `PLAYWRIGHT_JSON_OUTPUT_NAME`, and log its output as `wizard`
   lines do. A watchdog kills the process tree (`_wait_node`, `hil/wizard.py:116-152`).
6. In `finally`:
   - `POST /_api/detach`;
   - kill the host's process tree;
   - reacquire the device and wait for it to answer: a WCB `?VERSION` or a NaviCore PING, as `_reacquire` does
     (`hil/wizard.py:93-113`). With `recover`, a device that does not answer is handed to it (`reset_into_app` for a
     WCB left in its ROM loader), and the test fails naming both;
   - copy the Intellex log next to the report.
7. Turn the JSON report into PASS, FAIL or SKIP (reuse `_outcomes`). With `link_check`, a raw `/_link` client of the
   harness's own (the rawLink) has read every byte the host fanned out since the attach, and `link_check(bytes)`'s
   problems fail the test beside the spec's own (`boot_check`: the board printed a boot line). The bytes are never
   logged.

`run_intellex_py(bench, test_id, script, device=None, args=None)` does steps 1-2 and 6-7, and runs
`<venv python> tests/intellex/py/<script>.py --stage <dir> --args <json> --out <json>` instead of Playwright.

Sketch of the suite (the same shape as `s30_wizard.py:65-86`):

```python
@test("intellex.wizard_pull_w1", "The Wizard served by Intellex auto-connects to W1 over COM6 (no port picker) and shows its saved bauds and labels", needs=["wcb1"])
def wizard_pull_w1(bench):
    with config_guard(bench, 1) as before:
        run_intellex_test(bench, "intellex.wizard_pull_w1", device="wcb1",
                          attach={"kind": "serial", "port": bench.cfg["devices"]["wcb1"]["port"]},
                          args={"tokens": before[1]})

@test("intellex.flash_w2_update", "Intellex re-flashes W2 with the bench image through the Wizard's Update FW, offline from its cache; W2 comes back identical", needs=["wcb2"], opt_in="intellex_flash")
def flash_w2_update(bench): ...
```

### 3.3 Work packages

**Effort** is agent hours to write and debug. **Bench** is the time one run takes. **Mode** is one of: *unattended*
(runs in the nightly full run); *opt-in* (a `bench.json` key, can run unattended once it has been proven); or
*attended* (someone at the bench, or it changes the PC's own WiFi).

#### IX-WP1: Testability hooks in Intellex

| | |
|---|---|
| Goal | H1, H2 and H3 from §2.4 (H4 and H5 are deferred). |
| Tests | `tools/smoke_test_hooks.py`, no hardware: offline makes `reachable()` false with its reason, and `/_api/webui-version` says `offline`; the allow-list filters `/_api/ports`, and refuses `open`, `identify` and the flash claim for any other port; the discover hosts variable empties `scan()`. The harness wraps it as `intellex.smoke_hooks`. |
| Also | Fix the doc drift in findings 6, 7 and 9 in the same push, and record the `d615344` publication (finding 11) in `HIL_WEEK_DECISIONS.md`. |
| Prereqs | The Intellex checkout. `git fetch --all`, then check every branch. |
| Effort | 3 h. |
| Mode | No hardware. |

#### IX-WP2: Harness plumbing

| | |
|---|---|
| Goal | `hil/intellex.py`; `hil/ws.py` learns binary frames and an Origin header; `s32_intellex.py`; the opt-in keys; the `tests/intellex` Playwright project with `lib/` and `fixtures/`; the `bench.json` keys; the preflight. |
| Tests | `intellex.stage_selftest`: stage, start, check `/_api/status`, stop. Asserts that nothing was written outside the stage, and that no COM port or netsh call was touched (read the log). A `selftest.py` case covers the stage-file filter. |
| Effort | 8 h. |
| Mode | Unattended. |

#### IX-WP3: No-board host, API and security (harness side, plus the Intellex venv)

| Test id | Checks |
|---|---|
| `intellex.smoke_repo_*` | Intellex's own suite, one harness test per tool. Python: `smoke_fanout`, `smoke_probe_identity`, `smoke_reconnect_identity`, `smoke_ghproxy` (on a seeded cache), `smoke_wiki` (on a seeded wiki). Node: `smoke_mesh_route`, `smoke_doorway_via_wcb`, `smoke_ota_label`, `smoke_wizard_reconnect`. Plus `py_compile` of `src`/`tools`, `node --check` of the shim, and `jscheck.js` on `shell.html` and `launcher.html` (Intellex `CLAUDE.md:467-539`). |
| `intellex.routes` | Every route answers with the status and content type in §1.3. `/`, `/wcb/Wizard/` and `/wcb/Wizard/index.html` each carry the shim tag exactly once, before the first `<script>`. The body minus the tag equals the file on disk (no fork). The headers are `no-store` where `host.py` says so. `/wcb/Wizard` gets a 301. Both 503 texts appear with empty bundles. `/_intellex.js` starts with `window.__intellexBranches` matching `settings.json`. |
| `intellex.origin_guard` | Every POST route in `build_app`, sent with `Origin: http://evil.example`, gets a 403. The `/_link` upgrade gets a 403 for a foreign Origin, and a 101 for `127.0.0.1`, for `localhost` and for no Origin. |
| `intellex.wiki_security` | `/wiki/wcb/</title><script>…` is escaped and does not run (verified in the page in WP4). The CSP carries a fresh nonce on every response. An asset gets the `sandbox` CSP. `..%2F` traversal is refused. An unknown product gets a 404. A missing page gets the "not in the downloaded copy" body. |
| `intellex.attach_validation` | Bad JSON gives 400; a bad `kind` gives 400; serial without a port gives 400. A `relayId` outside 1-20 is dropped. `role` is whitelisted. With H2, a port that is not allowed is refused with `wantsLink` false afterwards. `/_api/open-window` gives 400 for a non-local URL and 501 with no hook. |
| `intellex.link_error_frame` | A `/_link` write with nothing attached comes back as the TEXT `{"type":"ERROR",...}\n`. The `/_api/status` keys match §1.3. |
| `intellex.offline_con_floor` | With H1: `webui-version` reason `offline - no route to github.io...`. `branch-list` returns its error with the current branch and `main` present. `gh/contents` comes from the cache with `X-Intellex-Source: cache`, and every `download_url` begins `/_api/gh/raw`. `gh/raw` bytes equal the cached file. Other repos, paths and filenames get 502. `update-firmware` gives `ok:false` with the cache summary. `update-webui` gives 502. `update-wiki` gives `ok:false`. |
| `intellex.settings_branch` | `POST /_api/branch` accepts `WIFI` and `feature/x`. It refuses `../x`, `a b`, `x?y`, a 101-character name and an unknown product. The branch shows up in the shim prelude and in `/_api/firmware`, and `settings.json` is written only inside the stage. |
| `intellex.flash_rules_unit` (venv) | Pure functions: `_pick_app` refuses two images; the S3 bootloader must be the 8 or 16 MB file, and the unsized fallback is accepted only for 16 MB; a full flash is refused without a bootloader or partition table; write lists are sorted, otadata is always erased and NVS only on factory/wipe; NaviCore boot and part are both-or-neither. `detect()` parses recorded esptool 5.3.1 output for ESP32, ESP32-S3 8 MB and 16 MB, and refuses C3. A `(should)` case for finding 5 (a partition-table mismatch escalates to a full NVS-preserving flash), which fails until it is ported. |
| `intellex.flash_pipeline_fake` (venv, in-process) | `build_app()` on a local site with a FakeTransport attached as `{kind:serial, port:COMFAKE}`, a fake `esptool` package on `PYTHONPATH`, and H1 with a seeded cache. `/_api/flash-wcb` goes: claim (a second POST gets 409), detach, fake detect, write, progress that never goes backwards, `version`, reattach (`rebuild` patched). The esptool argv is checked: chip; sorted addresses; `keep/keep/keep`; `--compress`; 921600. A fake esptool exit 2 surfaces as `ok:false` with the tail of its output. A ws spec gets the USB 409. |
| `intellex.ghget_retry_unit` (venv) | `flash._get` against a local HTTP server: 429 with `Retry-After: 1`, then 200, is retried; a 404 is not retried; connection refused fails fast (`no_network`). |
| `intellex.paths_logs_unit` (venv) | With `FROZEN` patched: `data_dir` prefers the user copy only when it is non-empty; wikis decide per subdirectory; `write_dir` goes to the user dir. The legacy `NaviLink` directory is moved once and never merged. The applog prune spans both prefixes and orders by timestamp. `winsize.fit` clamping and centring with a patched work area. `certs.is_cert_error` on a nested `SSLCertVerificationError`. |

| | |
|---|---|
| Prereqs | IX-WP1 and IX-WP2. |
| Effort | 12 h. |
| Mode | Unattended; no board. |

**Status (2026-09-29): done, run standalone (no bench).** In `suites/s32_intellex.py`; the venv checks are scripts in
`tests/intellex/py` run by `hil/intellex.py run_intellex_py` (a shared runner, `_ixpy.py`, and a JSON results file).

- New tests: `intellex.routes_api`, `.wiki_security`, `.offline_gh_proxy`, `.flash_rules_unit`,
  `.flash_pipeline_fake`, `.ghget_retry_unit`, `.paths_logs_unit`, and the `(should)` tests
  `.settings_branch_dotdot` (finding 12), `.flash_update_partition_escalates` (finding 5),
  `.flash_esptool5_output` (findings 13 and 14), `.wiki_code_verbatim` (finding 15) and `.identify_direct_pong`
  (finding 3, DX11). All normal tests pass; each `(should)` test fails on its finding.
- Where the plan and the code disagree, the tests follow the code:
  - `intellex.attach_validation`: a port outside `INTELLEX_SERIAL_ALLOW` is refused, but `wantsLink` stays true until
    `/_api/detach`, because `api_attach` records the target before trying (`host.py:1191-1194`). The plan said false.
  - `intellex.settings_branch`: `../x` is accepted (`settings.py:37`, `:61-62`). The refusal the plan expected is the
    `(should)` test `intellex.settings_branch_dotdot`.
  - `intellex.flash_pipeline_fake`: "progress never goes backwards" holds only because, with esptool 5.3.1, there is
    no progress at all (finding 13); `intellex.flash_esptool5_output` asserts that it moves.
- Split from the plan's rows: the proxy half of `intellex.offline_con_floor` is `intellex.offline_gh_proxy` (its own
  seeded cache), which also covers `settings_branch`'s `/_api/firmware` check; `intellex.routes_api` covers the GET
  routes `intellex.routes` does not; finding 5's case is its own test id, so `flash_rules_unit` can pass.
- Fixtures: `detect()` reads flash-id output built from esptool 5.3.1's own print statements (no board on this bench has
  recorded one); the pipeline runs a fake esptool (`tests/intellex/fixtures/fake_esptool`), checked by its signature
  before any flash route is called, against a fake transport on a port name that exists nowhere (`COMFAKE`).

#### IX-WP4: No-board Playwright (UI with nothing attached, dangerous routes guarded)

| Test id | Checks |
|---|---|
| `intellex.ui_tools_load` | `/` and `/wcb/Wizard/`: no `pageerror` and no response of 400 or more. The console shows `navigator.serial is backed by`, `tool: WCB Wizard`, `cross-tab port sharing disabled` and `flashFirmware() now runs on the host`. The splash is closed within 2 s. `requestPort()` returns the shim port, with `getInfo()` 303A:1001. `wcb_fw_branch` is set only when the branch is not `main`. `rc_config_tool_url === '/'`. The chip reads `Intellex · not attached ▾`. For the NaviCore tool: flash buttons enabled with the native titles, the OTA label untouched, and `host has no transport attached`. |
| `intellex.ui_wizard_setup_images` | Opens the Wizard's guided setup and walks to the hardware-version and Maestro steps. No image 404s. Fails today (finding 2). |
| `intellex.ui_contract_wizard`, `intellex.ui_contract_navicore` | Asserts the type of every symbol and DOM id in §1.5, in the page. Runs twice: against the `worktree` bundles (a Wizard change that breaks the shim fails here) and against the `shipped` bundles (reports what users run today). |
| `intellex.ui_latest_fw_version` | `(should)`, finding 1. With `/_api/flash-wcb` and `/_api/flash-status` fulfilled, the replacement `flashFirmware()` is called. The bare `latestFirmwareVersion` must then equal the flashed version. |
| `intellex.ui_launcher` | Fulfilled ports (303A, 1A86, 10C4, a Bluetooth port, `/dev/tty.*`) give the chip labels, the skips, and "asking…" upgraded in place by the identify fixtures (version, busy, none). Fulfilled discover results (navicore, wcb, relay, routable-but-silent) give the rows, the subtitles, `relayId` in the attach body, and the "watching…" state. Also covered: the filter counts; auto-select exactly once; the theme and view persist, with `wcb` the default; the maintenance lines and attention count, orange for a non-main branch or an uncached set; the branch dropdown including `Other…`, `Set` and `Refresh`; the Log toggle; the Docs popup; Connect POSTs `/_api/attach` then goes to `/_shell?view=`; `sep` makes a popup; "Open the tool anyway"; Back when attached; F5. |
| `intellex.ui_shell` | Tab `aria-pressed` states and `?view=` in the URL. Both panes exist after 4 s. The split zoom arithmetic: a 700 px pane gives `zoom` 0.593 and an iframe width of `round(w/z)` px. A drag persists `intellex_split`. New window removes the frame and opens the popup. The chooser overlay opens from `#target` and from `postMessage`, and closes, **without reloading the tool frames** (`performance.timeOrigin` unchanged). The label strings. |
| `intellex.ui_wiki` | On a seeded wiki: tabs, sidebar and filter; `[[Links]]` and `(Page-Name)` rewritten to `/wiki/<p>/`; a missing image becomes an "offimg" link; raw `<img>` rewritten; a `<script>` in the markdown and the XSS URL do not run (a `window.__pwned` sentinel stays unset); no request leaves `127.0.0.1` except `https` images. |

| | |
|---|---|
| Prereqs | IX-WP1 and IX-WP2. |
| Effort | 12 h. |
| Mode | Unattended; no board. Later CI-able (decision DX12). |

**Status (2026-09-29): done, run standalone (no bench).** Specs in `tests/intellex/specs`, one harness test each in
`suites/s32_intellex.py`. `lib/fixtures.js` gained `hostGuard` (answers every route that reaches past the host for the
whole browser context, frames and popups included), `hilContext` and `hostPost`.

- New tests: `intellex.ui_tools_load_shipped` (DX3), `.ui_tools_glue`, `.ui_contract_wizard`, `.ui_contract_navicore`,
  `.ui_contract_wizard_shipped`, `.ui_contract_navicore_shipped`, `.ui_launcher`, `.ui_shell`, `.ui_wiki`, and the
  `(should)` tests `.ui_wizard_setup_images` (finding 2) and `.ui_latest_fw_version` (finding 1). All normal tests pass,
  the shipped bundles included; both `(should)` tests fail on their findings.
- `intellex.ui_tools_glue` is the rest of the plan's `ui_tools_load` row. `ui_contract_*` also checks, functionally,
  that each tool's own GitHub constants, fetched through the shim, land on a repository Intellex's proxy serves.
- Code over plan: finding 2's images are at `Wizard/app.js:10691` (the identity step) and `:10952` (the Maestro step),
  not `:10550`/`:10811`. `launcher.html:343-347` says a problem "opens the section"; `paintMaint` (`:363-385`) never
  does, and `:350-352` says it shows without opening. `intellex.ui_launcher` asserts what the code does.
- Not covered: the Docs popup with docs stored, the identify "connected right now" state, and the attached label
  strings (they need an attached host, IX-WP7/WP8). `ui_wiki` has no `https` image, so no request may leave 127.0.0.1.

#### IX-WP5: Transports against bench boards (Intellex venv; the harness hands the port over)

| Test id | Needs | Checks |
|---|---|---|
| `intellex.serial_no_reset_w1` | wcb1, wcb2 | Opens and closes `SerialTransport(COM6)` 10 times. Oracles: no `Raw Serial Forwarding Task Created` on the stream, and no W1 boot announce on W2's console, which the harness keeps holding. |
| `intellex.serial_no_reset_navicore` | navicore, wcb1 | The same on COM5. Oracles: no `Reset reason` or boot banner, and W1 shows no NaviCore reboot announce. |
| `intellex.serial_contract_w1` | wcb1 | `on_data` receives only `bytes`. `;S0,HIL<n>` followed by a multi-byte UTF-8 marker comes back byte-exact. A write after `close()` raises `TransportError`. `set_signals(dtr=False, rts=False)` does not reset. |
| `intellex.serial_signals_reset_w1` | wcb1 | Characterisation: `set_signals(rts=True)`, then `rts=False`, resets W1 (boot line seen). Records the DTR-then-RTS write order for native-USB boards (`serial_transport.py:113-117`). |
| `intellex.serial_device_loss_navicore` | navicore | Opt-in `intellex_reboot`. `{"type":"REBOOT"}` (`NaviCore.ino:26`, `:4006-4026`). Records whether the COM handle survives the reboot. If it does not: `write()` raises and `on_lost` fires. If it does: the boot banner arrives and the transport stays open. |
| `intellex.ws_transport_navicore` | navicore, Wi-Fi 2 on NaviCore's AP | PING gets a PONG. A `GET_CONFIG` of at least 10 KB is reassembled from about 1400-byte frames and parses (field names only). A non-UTF-8 write raises. A write after close raises. |

| | |
|---|---|
| Effort | 6 h. |
| Bench | About 5 minutes. |
| Mode | Unattended, except where marked. |

**Status (2026-09-29): written and bench-verified** (`20260929-043806`: every IX-WP5 test passes but `ws_transport_navicore`, which skips until the PC's second adapter is on NaviCore's AP (IX-WP9), and `serial_device_loss_navicore`, behind `intellex_reboot`). In `suites/s33_intellex_bench.py`, with the venv side in
`tests/intellex/py/transport_serial.py` and `transport_ws.py`. The harness releases the one port a test names and takes
it back afterwards (`hil/intellex.py handed_over`); `INTELLEX_SERIAL_ALLOW` names that port alone. Dry-run against a
simulated port and a faked WebSocket (every group's control flow); never against a board.

- All six tests are written. `serial_device_loss_navicore` is behind the new opt-in `intellex_reboot`, runs inside
  `nc_guard`, skips on `restart_blocker()` (D33) and is listed in `hil/servos.py`. `serial_signals_reset_w1` resets W1
  once, unattended, like the other reboot tests, and ends with `_peers_online`.
- No-reset oracles beyond the stream: W2 prints `[ETM] WCB1 came ONLINE (boot)` for each boot announce it hears, W1
  does the same for NaviCore (WCB 20), and NaviCore's `GET_MESH_STATS` `upMs` must keep counting.
- Changed from the plan: `ws_transport_navicore`'s script does not read `netsh` or compare an SSID (credential rule).
  It needs an address on 192.168.4.x and a DIRECT PONG (no `sys`, no `id`) from the host there, carrying the version
  NaviCore gives over USB, which a WCB doorway at the same address cannot give. Since IX-WP9 the harness joins
  NaviCore's AP for it first (`navicore_wifi`, DX34), and with the harness joined a missing address or PONG fails
  rather than skips; the script itself never joins, bounces or changes WiFi. `serial_contract_w1` adds a check the
  plan lacked: a second open of a held port raises.

#### IX-WP6: The bridge end to end on bench boards (harness side, raw `/_link` clients)

| Test id | Needs | Checks |
|---|---|---|
| `intellex.bridge_wire_w1` | wcb1, probe1 wire W1S1 | The host is attached to COM6. Client A sends `;S1HIL<n>\r`, and the probe sees it exactly once. Clients A and B receive byte streams with the same SHA-256 over 30 s of mesh traffic. |
| `intellex.bridge_terminators_w1` | wcb1 | `?VERSION\r`, `?VERSION\n` and `?VERSION\r\n` are each answered exactly once (count the `End of Version` lines). |
| `intellex.bridge_load_navicore` | navicore | Two clients. PING gets a PONG, and `GET_CONFIG` arrives whole. During 20 s of `START_MONITOR`, both clients' streams are identical and made of whole lines. Cleanup: `STOP_MONITOR`, and debug flags back to the harness baseline (they are RAM-only, `NaviCore.ino:489-494`). |
| `intellex.bridge_release_w1` | wcb1 | After detach, the harness reopens COM6 within 2 s. Killing an attached host also frees the port. |
| `intellex.bridge_failed_attach_w1` | wcb1 | While the harness holds COM6, `attach` gets 502 with `wantsLink` true; `detach` then clears `wantsLink`. The harness keeps the port afterwards: no steal once it is released (the COM11 precedent). |
| `intellex.bridge_reboot_w1` | wcb1 | `?reboot\r` through `/_link`. The CH9102 stays enumerated, so the link must not drop and the pages must not be closed; the boot banner arrives through the link. |

| | |
|---|---|
| Effort | 6 h. |
| Bench | About 5 minutes. |
| Mode | Unattended. |

**Status (2026-09-29): written and bench-verified** (`20260929-043806`: all six IX-WP6 tests pass). In `suites/s33_intellex_bench.py`. A staged, leashed host
attached to the board's COM port (`hil/intellex.py attached_host`), and raw `/_link` clients (`LinkTap`) standing in
for the tool pages. Streams are compared by length and SHA-256, never quoted (through NaviCore they carry GET_CONFIG).
`bridge_terminators_w1`, `bridge_reboot_w1` and `bridge_load_navicore` were dry-run end to end through a real staged
host whose pyserial was replaced by simulated boards on fake port names; the other three need real port ownership.

- `bridge_wire_w1` also needs W2: the "mesh traffic" is W2 sending `;W1,;S0,<marker>` over ETM, beside console lines
  from client A. Both clients must hold identical bytes between a sync line and an end line; every console line must
  arrive, in order; at least one mesh line must.
- Two clients compare only from a line both saw: the host drops a chunk while no page is bound (`host.py:329-331`)
  and binds a page just after its handshake (`:1292-1294`). NaviCore's PONG is the same every time, so
  `bridge_load_navicore` compares between the 2nd and 3rd PONG (`line_spans`).
- `bridge_load_navicore`: a JSON line cut short identically on both clients is noted, not failed: it happened upstream
  of the fan-out. Afterwards the harness sends STOP_MONITOR and SET_DEBUG_FLAGS 0 (the baseline `nc_guard` restores).
- `bridge_failed_attach_w1` adds: while the harness holds the port, the reconnect loop's retries (one a second) never
  attach it; after detach and release, the host does not take the port.

#### IX-WP7: The Wizard through Intellex, on W1 (and W2 over the mesh)

Ports of the `wizard.*` board tests. Intellex replaces the port picker with its own auto-connect, so the specs need
no Chrome profile.

| Test id | Needs | Checks |
|---|---|---|
| `intellex.wizard_pull_w1` | wcb1 | With no click, `boardBaselines[1].wcbNumber === 1` within 45 s. The bauds and labels match the harness tokens (the same as `board.spec.js:10-28`). The chip reads `Intellex · USB COM6 ▾`. No `pageerror`. Per `rawLink`, W1 was not reset. |
| `intellex.wizard_push_label_w1` | wcb1 | `config_guard(1)`. A port of `wizard.push_label` (`s30_wizard.py:71-80`). |
| `intellex.wizard_terminal_wire` | wcb1, probe1 | A port of `wizard.terminal_wire`: the `\r` framing through the shim, proved on the wire. |
| `intellex.wizard_mesh_autopull_w2` | wcb1, wcb2 | `config_guard(1,2)`, and `?RTERM,STOP` on W2 in `finally` (as at `s30_wizard.py:113`). `routeMeshThroughBoard` pulls W2 through W1: `remoteRelayForBoard[2]` is W1's slot, and a baseline for 2 appears. Client 20 (NaviCore) and probe clients are never pulled. Each board is pulled once. The slot's type is `intellex.wizard_mesh_relay_slot_number` (`(should)`, finding 17). |
| `intellex.wizard_remote_pull_parts` | wcb1, wcb2 | A port of `wizard.remote_pull_parts` (`s30_wizard.py:89-124`). Long `[MGMT:CFGPART` lines cross the Intellex link. |
| `intellex.wizard_reload_no_reset_w1` | wcb1 | Three reloads. No boot line; the link reconnects each time; the slot is the same. |
| `intellex.shell_split_w1` | wcb1 | `/_shell?view=split`. Both tools connect: the Wizard pulls W1, and the NaviCore pane shows its status. |
| `intellex.nc_via_usb_doorway` | wcb1, navicore | `(should)`, finding 4. The NaviCore tool reached through W1 over USB ends up with `viaWcbActive === true`, on a cold connect and on a reload right after it (inside W1's relay window). It is expected to fail until Intellex identifies a serial WCB (§4.3 DX9). Its companion check records whether the mesh PONG actually arrived. |
| `intellex.wizard_mesh_relay_slot_number` | wcb1, wcb2 | `(should)`, finding 17. A board the shim routes through W1 is filed under relay slot `1`, a number, as the Wizard's own callers file it. |
| `intellex.wizard_reboot_w1_boots_app` | wcb1 | `(should)`, finding 16. W1 restarted with `?reboot` from the Wizard's terminal while the Wizard holds it (its DTR asserted) boots its app, not the ROM loader. One W1 restart; the harness resets W1 into its app if not, and ends with every peer online. |

| | |
|---|---|
| Effort | 12 h. |
| Bench | About 10 minutes. |
| Mode | Unattended: `config_guard`, the same class as the `wizard.*` tests. |

**Status (2026-09-29): done and bench-verified (`20260929-110148`: every test passes but the two findings' `(should)` tests, 4 and 17, which fail as designed; finding 16's passes).** In `suites/s34_intellex_tools.py`, with
`tests/intellex/specs/wizard_board.spec.js` and `tests/intellex/lib/board.js`. Every row above is written, the last two
(findings 16 and 17) added while writing. Each test stages the working-tree tools, attaches a leashed host to W1's COM
port and lets the shim connect the Wizard; `run_intellex_test` gained `link_check` (the rawLink: a raw `/_link`
client of the harness's own, whose bytes `boot_check` searches for a boot line of W1's and for any board W1 hears
booting) and `recover` (`reset_into_app`, for a W1 left in the ROM loader). Dry-run end to end - real staged hosts,
the real Wizard, shim and config tool under Playwright - against simulated boards on fake port names (W1 with W2 and
NaviCore behind it, modelling W1's relay window); never against a board. `wizard_terminal_wire` needs a probe and
was not dry-run.

- Plan-vs-code: the shim pulls every WCB that W1's WDP sweep lists, whatever the test (`routeMeshThroughBoard`), and
  arms its remote terminal (`setRemoteConnected` -> `startRemoteTermSession`, `Wizard/app.js:6368-6429`). So every
  Wizard test here guards all the bench's WCBs, not only W1, and stops every other WCB's `?RTERM` afterwards.
- `wizard_remote_pull_parts` observes the shim's own pull of W2 instead of starting one (a second pull would race it
  on W1's stream); `remoteBoardPull` is wrapped to record each call, its caller and its one `onComplete`.
- `wizard_mesh_autopull_w2` compares the relay slot as text: the shim files it as `"1"` (finding 17), and the plan's
  `remoteRelayForBoard[2]===1` is that `(should)` test.
- `wizard_push_label_w1` and `wizard_terminal_wire` wait for the shim's pull of W2 first, so W1's stream carries only
  the test's own traffic.
- `nc_via_usb_doorway` needs NaviCore as well as W1 (the PONG it may mirror is NaviCore's), and waits 21 s with nothing
  attached first, so its first connect is cold: a test just before it can leave W1's relay window open, and in the
  dry run the "cold" connect then came up direct too. `shell_split_w1` records the NaviCore pane's transport and leaves
  the judgement to it.
- Nothing here moves a servo (`hil/servos.py` is unchanged): the Wizard pulls, pushes one label and types a `;S1` line.

#### IX-WP8: The NaviCore tool through Intellex on COM5

| Test id | Checks |
|---|---|
| `intellex.nc_autoconnect` | Status `Connected ✓`; `#intellex-transport` reads `· USB COM5 ▾`; `#fw-current-version` equals the harness's PING version; not in Via WCB; flash buttons wired, with the native titles; the OTA button reads `Update over USB (OTA)`; no `pageerror`. |
| `intellex.nc_reload_no_reset` | **The reason Intellex exists** (Intellex `CLAUDE.md:97-105`). Three F5 reloads, with no `Reset reason` or boot banner on `rawLink` and the PONG version stable. Afterwards, W1 shows that NaviCore stayed on the mesh without rebooting. |
| `intellex.nc_config_matches` | Named, non-secret fields of the tool's loaded config equal the harness's `GET_CONFIG`. Passwords are never compared or printed. |
| `intellex.nc_setsignals` | The page's `setSignals({dataTerminalReady:false, requestToSend:false})` gets 200 and causes no reset. |
| `intellex.nc_save_unchanged` | Opt-in `intellex_nc_save`, in `nc_guard`. Save with no edits sends nothing. `chRateHz` saved one step away and back: each Save one `SET_CONFIG` carrying that branch alone, ACKed; the config ends byte-identical. It re-dispatches no mode knob, so it is not in `hil/servos.py`. |

Every test ends by restoring `STOP_MONITOR` and the harness's debug flags.

| | |
|---|---|
| Effort | 8 h. |
| Bench | About 5 minutes. |
| Mode | Unattended, except the save. |

**Status (2026-09-29): done and bench-verified (`20260929-110148`: all five pass, `nc_save_unchanged` under the opt-in, ticked, D67).** In `suites/s34_intellex_tools.py` with
`tests/intellex/specs/nc_board.spec.js`; dry-run against the in-Node NaviCore emulator
(`tests/wizard/lib/navicore/emulator.js`) behind a fake port, all five passing there.

- The tool's connect writes nothing to NaviCore's flash: it compares the command library (`GET_CMDLIB_META`,
  `config_tool/index.html:14487-14496`) and only reports a different one (`:9139-9155`); `GET_CMDLIB` is the "Load from
  NaviCore" button (`:14500-14505`) and `SET_CMDLIB` its Save (`:14693-14733`). So the four read-only tests run without
  `nc_guard`: each spec fails on any JSON type the tool sends that is not a read (`NC_READ_ONLY`), and the harness puts
  back the RAM state the connect leaves (`STOP_MONITOR`, debug flags 0). They skip while NaviCore's recorder holds
  anything (D33), since a regression here would restart it.
- No-reset oracles: the rawLink (no ROM line, banner or reset reason), NaviCore's uptime across the test, and no
  `[ETM] WCB20 came ONLINE (boot)` on W1.
- `nc_config_matches` compares the whole CONFIG line byte for byte on the rawLink bytes, in the harness; the spec gets
  only named non-secret scalars (`NC_FIELDS`). Mapping and Maestro-slot counts are left out: the tool fills what
  GET_CONFIG omits (`rc_config.h:1262`), so they are the tool's numbers, not the board's.
- Plan-vs-code, `nc_save_unchanged`: a Save with no edits sends nothing (`saveConfigToBoard`'s no-diff return,
  `index.html:16911-16925`), so the spec checks that, then makes the SET_CONFIG round trip with `chRateHz` one step away
  and back, each save ACKed and carrying that branch alone. Still behind the new opt-in `intellex_nc_save` (unticked),
  in `nc_guard`. It moves nothing: an unchanged baud re-opens no port and easing is re-applied only where it changed
  (`NaviCore.ino:3236-3272`, `:3321-3345`), so it is not in `hil/servos.py`.
- `#push-budget` does not show a `chRateHz` edit (`_pushBudgetInfo` leaves the numeric fields out,
  `index.html:16665-16676`); only Save reads the slider.

#### IX-WP9: Over WiFi, through NaviCore's AP on the PC's second adapter

**Bench facts:**

- NaviCore has `wifiEnabled:true` and hosts its own access point (bench session log `20260927-174702`); the SSID is
  `wifiSsid`, or derived as `NaviCore-<deviceId>` when that is empty (`NaviCore.ino:4711-4726`).
- "Wi-Fi 2" (the TP-Link) is the spare adapter, and its own network IS NaviCore's AP; "Wi-Fi" carries the internet.
- The harness puts Wi-Fi 2 on NaviCore's AP for each test and back afterwards (`suites/s45_navicore_wifi.py` `_on_ap`,
  `hil/wlan.py` `pc_on_ap(..., spare_only=True)`): a temporary `HIL-` profile holding the SSID and password it reads from
  NaviCore's GET_CONFIG, a wait for the 192.168.4.x lease, the profile deleted afterwards. A PC whose only adapter carries
  its internet skips (D-NC14). The tests are behind `navicore_wifi`, the key s45 uses for the same act (DX34).

| Test id | Mode | Checks |
|---|---|---|
| `intellex.wifi_discover` | opt-in `navicore_wifi` | `discover.scan(['192.168.4.1'])`: kind `navicore`, a version equal to the PONG, and `via` on 192.168.4.x. `ssid_for_host()` names NaviCore's network (compared by SHA-256). `/_api/discover?hosts=192.168.4.1` gives the same, and a bare `/_api/discover` under the leash's empty host list probes nothing. |
| `intellex.wifi_nc_tool` | opt-in `navicore_wifi` | Attach `{kind:ws, host:192.168.4.1, role:navicore}`. The label reads `· WiFi 192.168.4.1 ▾`. The OTA button reads `Update over WiFi (OTA)` with the WiFi title. The flash buttons are disabled with the not-USB message. `POST /_api/flash` gets the USB 409. |
| `intellex.wifi_wizard_via_navicore` | opt-in `navicore_wifi`; `config_guard` on every WCB; `?RTERM,STOP` on every WCB in `finally` | The Wizard through NaviCore as its doorway: a relay card at 20, or the plain-board route, and baselines for W1 and W2. This answers "still to verify on hardware" (`docs/WCB_WIZARD.md:321-323`). |
| `intellex.wifi_rterm_rate` | opt-in `navicore_wifi` | The open F21 question: why Intellex re-arms `?RTERM` every second (`docs/HIL_TEST_AUDIT.md:506-512`). Once the Wizard has pulled every WCB through NaviCore, count the `?RTERM,START,<NaviCore>` session starts on each WCB's own USB console for 60 s, beside the re-arms the page wrote. Record the rate, and fail any WCB above 6 (DX10, DX42). |
| `intellex.wifi_link_loss` | opt-in `intellex_reboot` | NaviCore `REBOOT` through the link. The host notices: `/_api/status` shows the loss within 30 s (its own 15 s plus finding 19's 10 s stall, which `intellex.link_drop_no_stall` pins). The log has **no** `re-associating` line (`--no-auto-bounce`). Once the PC's link carries a connect to 192.168.4.1:80 again (`hil/wlan.py` `rejoin(reach=)`), it reattaches within 30 s and a page sees a PONG again. |
| `intellex.wifi_ap_hop_reidentify` | attended, opt-in `intellex_wifi_join` | While attached, move Wi-Fi 2 from NaviCore's AP to W1's (a temporary `HIL-` profile, as in `s28_wifi.py:254-351`). `/_api/status` flips from role `navicore` to `wcb` with `relayId` 1, and the log shows that **before** the attach (rule 10). The NaviCore tool goes to Via WCB. Then move back. |
| `intellex.wifi_bounce_scoped` | attended, opt-in `intellex_wifi_join` | `discover.wifi_bounce(<NaviCore's SSID>)` (the name in the script's environment only) bounces only Wi-Fi 2. "Wi-Fi" stays connected in every sample the harness takes while it runs, and its default route stays. Wi-Fi 2 ends back on NaviCore's network with a lease. |

| | |
|---|---|
| Effort | 12 h. |
| Bench | About 10 minutes, plus the attended part. |

**Status (2026-09-29): bench-run in `20260929-202852`; `wifi_link_loss` reworked since, not yet re-run.** In
`suites/s35_intellex_wifi.py`, with `tests/intellex/specs/wifi_board.spec.js` and the venv script
`tests/intellex/py/wifi_units.py`; every row above is written, and `intellex.ws_transport_navicore` (s33, IX-WP5) now
joins the same way (DX34), which is what left it skipped in `20260929-122112`. A leashed host allowed no COM port is
attached to `ws://192.168.4.1/ws` with role `navicore`, as Intellex's chooser attaches an identified droid. On the
bench `ws_transport_navicore`, `wifi_discover`, `wifi_nc_tool`, `wifi_wizard_via_navicore` and `wifi_rterm_rate` pass,
the two attended tests skip (`intellex_wifi_join` unticked), and `wifi_link_loss` failed there: finding 19, and the
shape it has now (below).

- The credential rule (DX36): a host attached over WiFi records the SSID it asks Windows for (`host.py` api_attach,
  `discover.ssid_for_host`) and names both networks when the adapter moves (`_reidentify_if_moved`: `network changed
  "<a>" -> "<b>"`, and the same in `/_api/status` `lastError` while it holds). So `IntellexHost(hide=...)` takes the
  names out of every line it keeps and logs, `copy_logs` out of the stage's and the copied log files, and
  `status_view` out of `lastError` before a hook hands the status to a spec. `wifi_units.py` gets a SHA-256 of the
  SSID (discover) or the SSID in its environment (the bounce), never in argv or its results.
- Mid-spec bench actions are bridge hooks (DX35): `run_intellex_test(hooks=...)`, the bridge's `/hook` route and
  `tests/intellex/lib/board.js` `hil.hook`. The rate test marks and counts every WCB's own USB console
  (`rterm_mark`, `rterm_count`); the AP hop moves the adapter (`hop`: s28 `_pc_on_w1_ap` nested inside `_on_ap`;
  `hop_back` leaves it, back to NaviCore's temporary profile) and waits for the host to reattach with the role it
  re-identified.
- Plan-vs-code: `/_api/discover` answers `{"candidates": [...]}` (`host.py:393-402`), not a bare list; with the
  leash's empty `INTELLEX_DISCOVER_HOSTS` a call without `?hosts=` probes nothing and answers none. The dry run caught
  the harness reading it as a list.
- `wifi_rterm_rate` counts, after every board is pulled and 10 s more, the `[RTERM] Session started -> relay WCB20`
  lines each WCB prints in 60 s, beside the `?MGMT,FRAG,...,?RTERM,START` frames the page wrote (`summarise`'s new
  `inner`), and bounds each board at DX10's 6. Reading the current Wizard, only setRemoteConnected, a pull that
  succeeded, an ETM came-ONLINE edge through the relay and relayRouteAll's re-arm of a board already managed send it,
  three frames a call (sendMgmtReliable); relayRouteAll runs again from the shim's 2 s poll only while the relay card
  shows a board unmanaged. F21 was seen with the bundle Intellex ships, which predates that code; the bench run
  measures the working-tree Wizard (DX3). On the bench: no `?RTERM,START` on either WCB in the minute after every
  board was pulled (the relay card 4 s after the page opened, both boards at 6 s), and none written by the page: F21's
  once-a-second re-arm does not reproduce with the working-tree Wizard.
- `wifi_link_loss` is behind `intellex_reboot` and checks `navicore_wifi` in its body; it runs harness-side with raw
  `/_link` clients and `nc_guard`. Two measurements from `20260929-202852` shape it:
  - The loss showed at 21.9 s: the probe's verdict at 11.8 s, then 10 s in which the host answered nothing while it
    closed the dead WebSocket (finding 19). The stall is `intellex.link_drop_no_stall`'s `(should)`, board-free
    (DX45); here the loss need only show within 30 s (`NOTICE_LIMIT_S`: the 15 s, the 10 s stall, 5 s), and time over
    15 s is noted.
  - The host did not reattach in the 60 s after the harness saw Wi-Fi 2 connected with its 192.168.4.2 lease. That
    adapter was never back. REBOOT deauthenticates nobody (`navicore_ota.h:184-204`), and Windows kept the old
    association: its WLAN AutoConfig log has no event at all from the join (20:32:20) to the harness's own disconnect
    (20:33:50), while the restarted access point dropped the station's frames. So `hil/wlan.py` `rejoin(reach=)` wants
    a TCP connect to 192.168.4.1:80 within 10 s and otherwise re-associates the adapter - netsh disconnect, then the
    temporary profile connected again, what Intellex's own bounce would do with its leash off - and the host gets 30 s
    from that proof (`REATTACH_S`: its loop retries about every second, 5 s per open) (DX46).
  - Intellex's bounce would not have fired either: it acts only on a wrong route (`host.py:1655-1671`), and a stale
    association keeps the lease and the route. NaviCore's own measurement had Windows re-attach after ~11 s of dead
    air (`navicore_ota.h:184-199`); on this bench's spare adapter (a TP-Link USB adapter) under the manual temporary
    profile it did not in 90 s.
- `wifi_ap_hop_reidentify` asserts the tool's state after a reload at each end (a connect made there is what rule 10
  guards) and notes whether the shim's `watchLink` switched it without one.
- Dry runs (DX44): a real staged host, the real tools under Playwright and the harness bodies themselves, against a
  fake NaviCore endpoint on 127.0.0.1:80 (the repo's NaviCore emulator behind a minimal WebSocket server that also
  answers the WCB_Mgmt relay surface with W1 and W2 behind it, and turns into a W1 doorway on request), with the staged
  `discover.ssid_for_host` reading a scratch file instead of netsh and `_on_ap` replaced: `wifi_discover`,
  `wifi_nc_tool`, `wifi_wizard_via_navicore`, `wifi_rterm_rate`, `wifi_link_loss` and `wifi_ap_hop_reidentify` pass
  there, and no network name reached session.log, the stage's log or the copied log. `wifi_bounce_scoped` ran only its
  script, with `wifi_bounce` stubbed (its netsh is the test). Nothing joined, bounced or read the PC's WiFi. The fake's
  REBOOT closed its sockets and refused connects for 4 s, which a restarting access point does not do - it goes
  silent, no FIN and no deauthentication - so the dry run passed `wifi_link_loss`; `hil/ws.py` `WsEndpoint` vanishes
  the real way.

#### IX-WP10: Flashing

**Which board. W2.**

- It is a classic ESP32 (HW 2.4, CP210x) running the bench image
  `tests/hil/results/builds/wcb-esp32-meshq` (`WCB.ino.bin`, `.bootloader.bin` and `.partitions.bin`), per that
  folder's `FLASHED.md`. Its version moves with every bench image (`6.2.1_292004RSEP2026` since `8e5d24f`), so a test
  reads it from W2 and skips unless the image carries it (DX39).
- Seeded as a test branch, that image makes the flash **identity-preserving**: W2 ends the test running the image it
  started with. Its bootloader and partition table are the same files `arduino-cli upload` wrote.
- Not a probe:
  - probes are the harness's instruments;
  - probe2's auto-reset sometimes misses download mode (`0x17`);
  - a failed probe flash breaks the wire tests until it is fixed.
- Not NaviCore unattended: its native USB may need the BOOT button to recover. It gets its own app-only opt-in.

**The seed** (`<stage>/src/firmware`), used with H1:

- `wcb/hil-bench/WCB_<version>_hilbench_ESP32.bin`, with `_part.bin` and `_boot.bin` beside it, and a
  `listing.json` of `{name, type:"file", download_url}` entries.
- `navicore/hil-bench/NaviCore_<version>_ESP32S3.bin` from `results/builds/navicore-hil1` (`hil/ncflash.py`
  `BENCH_IMAGE`). This is the app **only**, so `flash.py` takes its app-only path (`:356-358`) and leaves NaviCore's
  bootloader alone.
- `settings.json` sets the flashed product's branch to `hil-bench` and the other's to `hil-none`, which nothing is
  cached for, so no flash of the other product could find an image.

Even without H1, a missing branch gets a 404 from GitHub, and both the flashers and the proxy then fall back to the
cached listing. H1 just makes that deterministic.

| Test id | Mode | Checks |
|---|---|---|
| `intellex.flash_w2_update` | opt-in `intellex_flash`; `config_guard` on every WCB | The Wizard auto-connects W2. `boardGo(<its slot>, {mode:'update'})` (`Wizard/app.js:7472`) goes to the shim's `flashFirmware`, then to `/_api/flash-wcb {appOnly:true}`. The log shows `ESP32`, `offline - using the cached hil-bench copy`, and `Hash of data verified` for 0xE000 and 0x10000. `/_api/flash-status` has `ok:true` and version `<version>_hilbench`. W2 reattaches, and the Wizard pulls it again. After the test: W2 `?VERSION` is unchanged, it runs app0, W1 heard it boot, and the guard passes. |
| `intellex.flash_w2_full` | opt-in `intellex_flash` | `mode:'flash'`: bootloader at 0x1000, partitions at 0x8000, app, and otadata erased. The same checks. |
| `intellex.flash_one_at_a_time` | opt-in `intellex_flash` | While the Wizard is flashing, a `POST /_api/flash` (the NaviCore tool's route) and a second `/_api/flash-wcb` from the page get 409 `a flash is already running`. |
| `intellex.flash_refused_board_runs` | opt-in `intellex_flash`; `(should)`, finding 18 | A full flash the host refuses after detecting the chip (a seeded build with no bootloader) writes nothing and leaves W2 running its app: it answers `?VERSION` on its own port with no reset from anyone. |
| `intellex.flash_w2_factory` | attended, opt-in `intellex_flash_factory` | `mode:'factory'` erases W2's NVS. Restore it from its pre-test chain and learned peers, with `s31_password_erase.py`'s restore generalised to W2. The guard must pass. |
| `intellex.flash_navicore_app` | opt-in `intellex_flash_navicore` | The NaviCore tool's Update Firmware button is intercepted and goes to `/_api/flash`. The log says the app is written alone. Afterwards: the PONG version is unchanged, `GET_CONFIG` is unchanged, and NaviCore is back on the mesh. |

**Recovery**, from `results/builds/FLASHED.md`:

- **If a flash fails:** run the same Intellex flash again. The ESP32 ROM loader always answers on auto-reset. The W2
  tests do it themselves (DX38): an EN reset into the app first (`reset_into_app`), then the image flashed again in
  full through the host's own `/_api/flash-wcb` (`s36 reflash_w2`).
- **W2, last resort:** `arduino-cli upload --fqbn esp32:esp32:esp32:PartitionScheme=min_spiffs --input-dir
  tests/hil/results/builds/wcb-esp32-meshq -p COM15`.
- **NaviCore:** its ladder, `python -m hil.ncflash recover --allow-esptool --known-good
  results/builds/navicore-hil1` from `tests/hil` (the test runs it without the esptool rungs unless
  `navicore_esptool` is ticked); last, `arduino-cli upload --fqbn
  esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,PartitionScheme=custom,FlashSize=16M,PSRAM=opi --input-dir
  tests/hil/results/builds/navicore-hil1 -p COM5`.
- **Permission:** a Claude session in auto mode is refused bench uploads, so Greg must run these, or the session must
  be in bypass mode.

| | |
|---|---|
| Effort | 12 h. |
| Bench | About 3 minutes per flash. |

**Status (2026-09-29): bench-run in `20260929-203453`: `flash_w2_update`, `flash_w2_full`, `flash_one_at_a_time` and
`flash_navicore_app` pass, `flash_refused_board_runs` fails as designed (finding 18), and the attended Factory Reset
has not run.** In `suites/s36_intellex_flash.py`, with `tests/intellex/specs/flash_board.spec.js`. Every row above is
written, plus the `(should)` test `intellex.flash_refused_board_runs` for finding 18. The four W2 flashes
(`flash_w2_update`, `flash_w2_full`, `flash_one_at_a_time`, `flash_refused_board_runs`) are behind `intellex_flash`,
the NaviCore app behind `intellex_flash_navicore`, both ticked since D74; the Factory Reset behind the attended
`intellex_flash_factory`, unticked.

- Identity (DX39): each W2 test reads W2's version and skips unless `results/builds/wcb-esp32-meshq`'s app carries it,
  checks the three files (the app's magic, version and size; the bootloader's magic; a table with app0 at 0x10000 and
  otadata at 0xE000) and seeds them as `WCB_<version>_hilbench_ESP32*.bin`. Afterwards W2 answers the same version,
  runs app0 (Intellex writes the app there and erases otadata), W1 heard it boot, and `config_guard` on every WCB
  passes (the shim routes W1 through W2 and arms W1's remote terminal there, so W1 is guarded and its `?RTERM`
  stopped, DX30).
- The flash goes through the Wizard as a user runs it: its auto-connect, the shim's mesh routing through W2, then
  `boardGo(2, {mode})`. The spec follows `/_api/flash-status` from Node (every percent it read is noted: finding 13),
  waits for boardGo, the reconnect and a fresh pull of W2, then asks the harness (hook `flash_done`) to judge the host's
  flash log (`flash_log_problems`): the regions esptool wrote, each verified, the offline cache, the mode's own lines,
  no esptool 4 spelling on the WCB path.
- Plan-vs-code: outside the guided setup an Update re-pulls the board and pushes nothing (`app.js` boardGo, the isUpdate
  branch); a Flash or Factory Reset pushes the whole pre-flash config back unless `pushConfig:false`. The W2 Flash and
  Factory Reset pass `pushConfig:false` (DX37); the Factory Reset's restore is the harness's own (s31's `_erase_cycle`
  generalised to W2 over its USB).
- `flash_one_at_a_time` asks for the second flash from the page (DX40): `/_api/flash` - the NaviCore tool's route - and
  `/_api/flash-wcb` again, once the first shows running; both must answer 409 `a flash is already running`, and the
  host must have started exactly one.
- The NaviCore flash clicks Update Firmware on the Hardware Setup dialog's Firmware tab, where the button lives
  (`#btn-hwsetup`, `data-tab="firmware"`), follows the host's flash, and waits for the shim's `watchLink` to reconnect
  the tool. NaviCore ends on app0 whatever slot it ran (the image is the same); a FLASHED.md row records the flash,
  `OK` only when `?OTALOCAL,STATUS` shows the image's App SHA256 running from app0 (DX41).
- Dry runs (DX44): all five specs against a real staged host whose pyserial was a fake wired to a simulated W2 console
  (and to the repo's NaviCore emulator) and whose esptool was the repo's fake: each passes, the esptool calls wrote
  exactly the mode's regions (Update 0xE000 and the 1,394,864-byte bench app at 0x10000; Flash 0x1000, 0x8000, 0xE000,
  0x10000; Factory Reset 0x9000 too; NaviCore 0xE000 and 0x10000 with six `Deprecated:` lines, finding 14), and the
  progress read 0 throughout (finding 13). The harness bodies of `flash_w2_update`, `flash_refused_board_runs` and
  `flash_navicore_app` ran with the bench mocked; the refused flash ran `flash-id --after no-reset` alone, wrote
  nothing, and the host reattached the port with no reset (finding 18). No port was opened and nothing was flashed.
- On the bench (`20260929-203453`): each W2 flash took 22-27 s and wrote exactly its mode's regions, every one
  verified (Update 0xE000 and 0x10000; Flash 0x1000, 0x8000, 0xE000 and 0x10000), W2 running app0 before and after;
  NaviCore answered PING again 19 s after the click, moved from app1 to app0 with the same App SHA256 (its FLASHED.md
  row). Every progress reading was 0 (finding 13) and NaviCore's flash printed six `Deprecated` lines (finding 14):
  both confirmed on the bench.

#### IX-WP11: OTA through Intellex (opt-in `intellex_ota`)

| Test id | Checks |
|---|---|
| `intellex.ota_navicore_usb` | "Update over USB (OTA)" on COM5. The page fetches the image through the shim's rewrite, which the proxy serves from the seeded `hil-bench` cache. NaviCore reboots into the same image. The reconnect loop recovers the link, and the page re-PONGs the same version. |
| `intellex.ota_navicore_wifi` | The same over NaviCore's AP, with the precondition from IX-WP9. Greg verified this by hand on 2026-09-10. |
| `intellex.ota_w2_via_w1` | Not in the first pass: relay OTA takes about 25 minutes, and s20 already covers it at the firmware level. |

| | |
|---|---|
| Effort | 8 h. |
| Bench | 10-30 minutes. |

#### IX-WP12: Online features (network-dependent; unattended; skip when offline)

| Test id | Checks |
|---|---|
| `intellex.online_update_tools` | `/_api/update-webui` in the stage updates both tools. The files hash identical to a fresh fetch from Pages, and `.prev` is kept. |
| `intellex.online_branch_list` | `refresh=1` lists `main` and `WIFI` for the WCB, has no `gh-pages`, and reports `cached:false`. |
| `intellex.online_fetch_firmware` | The cache fills for NaviCore on `main` and the WCB on `WIFI`, with a listing stored. |
| `intellex.online_wiki` | `fetch_wiki --wiki intellex` into the stage, then `smoke_wiki` passes. |
| `intellex.online_version` | `/_api/webui-version` has `checked:true` for both tools. |

About 6 GitHub API calls per run, well under the limit.

| | |
|---|---|
| Effort | 4 h. |

#### IX-WP13: Window mode and the frozen exe (attended, opt-in `intellex_window`)

| Test id | Checks |
|---|---|
| `intellex.window_cdp` | `app.py --inspect <p> --port <q> --no-auto-bounce`, staged, with `LOCALAPPDATA` isolated. Playwright `connectOverCDP` into WebView2. The launcher renders. `intellex_view` survives a relaunch (the `private_mode=False` fix, `app.py:350-374`). `/_api/open-window` answers 200 and a second CDP target appears. The window lies inside the work area. |
| `intellex.exe_sentinels` | `dist\Intellex.exe --run-esptool version` exits 0. `--run-fetch-firmware --check` with H1 reports and exits 0. |
| `intellex.exe_headless` | Only with H5: the exe serves the no-board routes of IX-WP3. |

| | |
|---|---|
| Effort | 6 h. |
| Mode | Attended: windows open on Greg's desktop. |

#### IX-WP14: Docs and integration

- `docs/HIL_TESTING.md` gets a new §10 "Intellex tests", a coverage-table row and a revision-log row.
- `HIL_WEEK_DECISIONS.md` gets the rows for the decisions taken.
- `HIL_TEST_AUDIT.md` §6 gets the progress.
- Intellex `CLAUDE.md` § Verifying gets the hooks and `tools/smoke_test_hooks.py`.
- Filed as fixes or tracker items: findings 1-5, 8 and 10.

| | |
|---|---|
| Effort | 3 h. |

### 3.4 Opt-in keys to add to `hil/optin.py` (all start unticked)

| Key | What it does to the bench | Estimate |
|---|---|---|
| `intellex_flash` | Re-flashes W2, through Intellex's esptool path, with the bench image: app-only, full, app-only with a second flash refused, and a full flash the host refuses. W2 is off the mesh about 1 minute each time. Registered, unticked. | 240 s |
| `intellex_flash_factory` | Attended. Erases W2's NVS through Intellex, then restores it from its chain. Registered, unticked. | 300 s |
| `intellex_flash_navicore` | Re-flashes NaviCore's app with its bench build (`navicore-hil1`). NaviCore is off the mesh about 1 minute. Registered, unticked. | 180 s |
| `intellex_ota` | OTA of NaviCore through Intellex. Erases and rewrites NaviCore's inactive slot, then reboots it. | 600 s |
| `intellex_reboot` | Reboots NaviCore through the link, to exercise device loss and reconnect. | 60 s |
| `intellex_wifi_join` | Attended. Moves Wi-Fi 2 between APs through temporary `HIL-` profiles, and bounces it once. Registered, unticked. The unattended WiFi tests use `navicore_wifi` instead (DX34). | 180 s |
| `intellex_window` | Attended. Opens Intellex app windows on the desktop. | 120 s |
| `intellex_nc_save` | Saves NaviCore's config from the tool twice, `chRateHz` one step away and back: two rewrites of its `/config.json` (LittleFS), inside `nc_guard`. Registered in `hil/optin.py`, unticked (DX29). | 90 s |

### 3.5 Order

1. IX-WP1 and IX-WP2 first (the hooks and the plumbing).
2. Then IX-WP3 and IX-WP4, which need no board and whose results are known immediately.
3. Then IX-WP5 to IX-WP8 on the bench. W1 goes before NaviCore, so that finding 4's `(should)` test is known early.
4. Then IX-WP9 (WiFi).
5. Then IX-WP10 and IX-WP11: one attended pass first, then opt-in.
6. Then IX-WP12 and IX-WP13, and IX-WP14 throughout.

The suite is `s32`, so the full-run order puts it after the firmware suites and before `s99`. Every Intellex test
cleans up after itself (`?RTERM,STOP`, `STOP_MONITOR`, debug flags, and the host killed), so the timing-sensitive
suites that follow are not affected (the F21 precedent).

---

## 4. Risks, what cannot be tested here, and decisions

### 4.1 Risks and mitigations

| Risk | Mitigation |
|---|---|
| **A test host captures a COM port.** A wanted target is reopened every second, forever (`host.py:1595-1632`); the COM11 incident cost a whole run. | H2 allow-list; detach, then kill the tree, in `finally`; `_reacquire` must succeed; the preflight skips while Greg's Intellex runs; the attach-failure test (IX-WP6). |
| **The launcher opens ports and probes WiFi.** It opens every 303A port (COM4 may reset) and probes through Wi-Fi 2. | H2 and H3, plus `guard()` route fulfilment in every spec. |
| **The user's WiFi gets bounced.** | `--no-auto-bounce` always. `/_api/wifi-bounce` is fulfilled with a 403 unless the attended test opts in. `intellex.wifi_link_loss` asserts that no bounce happened. |
| **Mesh side effects.** The Wizard through Intellex arms `?RTERM` sessions and pulls every board, and its NaviCore pane starts `START_MONITOR`. | `config_guard`; `?RTERM,STOP`, `STOP_MONITOR` and debug flags restored; the suite runs late; `intellex.wifi_rterm_rate` measures the effect. |
| **A flash writes the wrong image.** The CI release shares the version string with the bench build and would silently drop uncommitted fixes (`results/builds/wcb-esp32-meshq/FLASHED.md`). | Seeded `hil-bench` branch with H1. Assert that the cached tag and `Hash of data verified` are in the log. Never flash without the seed. |
| **A flash is interrupted.** | Re-run the Intellex flash (the ROM loader always answers on ESP32); `arduino-cli upload` from `results/builds` as a last resort (needs Greg's permission mode). |
| **Update FW skips the partition-migration check** (finding 5). An un-migrated board could fail to boot. | Unit `(should)` test now. Port the `flasher.js` check (DX11). The bench boards are already migrated. |
| **Credentials.** NaviCore's config holds the AP password; pulls hold the mesh password. | The harness hands specs lengths, versions and keys only. The temporary WiFi profile is deleted afterwards (the `s28` pattern). The Intellex log holds argv and no secrets. |
| **Greg's real Intellex state gets polluted:** his branch setting (`WIFI`), logs pruned to 20, his bundles and his cache. | Stage plus `LOCALAPPDATA`. The self-test (IX-WP2) fails if anything outside the stage changes. |
| **The bundled Wizard is stale** (`04.13:06.R.SEP.2026`): it predates F13, and WCB rule 15 names it as an old Wizard. | Test `worktree` by default. Run `shipped` for the contract and load checks only, and report staleness without failing on it (DX3). |
| **Headless Chromium is not WebView2** (zoom, storage, window API). | IX-WP13 drives the real WebView2 over CDP. |
| **GitHub rate limit or network flakiness.** | H1 everywhere except IX-WP12, which skips when offline. |
| **Port collisions:** 8765, 8777, 8778, and 8799, which is in use now. | A free ephemeral port for every test. |
| **Pushing Intellex publishes `d615344`** (finding 11). | Allowed (`HIL_WEEK_DECISIONS.md:16-17`). Log it as a decision; `intellex.wizard_*` and `intellex.wifi_wizard_via_navicore` then verify it on hardware. |

### 4.2 Not testable on this bench

- **macOS as a whole.** `Intellex.command`, `scripts/*macos*`, WKWebView, the empty CA store of python.org's macOS
  Python (the path `certs.py` exists for), the `/dev/cu.*` and `/dev/tty.*` filtering, and `_wifi_bounce_macos`,
  which is confirmed broken and diagnosed but not fixed (`README.md:138-142`).
- **A real MgmtRelay doorway** (role `relay`, `?RELAY,WIFI` answering at .19). The old relay board is now probe1.
  NaviCore-as-relay is covered (IX-WP9); a MgmtRelay is not.
- **An ESP32-S3 WCB.** That means the 8 MB vs 16 MB bootloader selection on real silicon, and the rule-10 hazard
  where an S3 WCB accepts a NaviCore image. Unit tests only.
  - The SBUS controller is S3 silicon on WCB hardware, but `detect()` leaves a chip in download mode
    (`--after no-reset`, `wcb_flash.py:254-256`), and it is the bench's controller. Not recommended.
- **A physical USB unplug** (contract 2 on a device that really vanished). This needs a person or a switchable hub.
  IX-WP5 covers whatever a NaviCore reboot does.
- **Windows WiFi pathologies:** a half-dead association after a real AP reboot, or an adapter that comes up with DHCP
  off (Intellex `docs/WIP_NOTES.md:219`). Attended observation only.
- **Other displays:** multi-monitor, and DPI other than this PC's (`winsize.py`).
- **The one-file build's temp extraction** (`paths.FROZEN` with `_MEIPASS`). Only through `dist/Intellex.exe`
  (IX-WP13). Building the exe has heavy side effects: it writes `dist/` and `src/build_stamp.py`, and fetches into
  `src/`. Not part of HIL.
- **Real GitHub throttling (403 or 429 from GitHub itself).** Simulated with a local server instead
  (`intellex.ghget_retry_unit`).

### 4.3 Decisions to take (each with the recommendation, to be logged in `HIL_WEEK_DECISIONS.md`)

| # | Decision | Recommendation |
|---|---|---|
| DX1 | Where the tests live. | Bench-bound and cross-repo tests go in WCB `tests/intellex/` plus `suites/s32_intellex.py` (D3). Intellex-only smoke tools stay in `Intellex/tools/`, and the harness wraps them. |
| DX2 | Which Intellex is under test. | A fresh staged copy of the working tree for each test, recording HEAD and whether it is dirty. `dist/Intellex.exe` only in IX-WP13. |
| DX3 | Which tool bundles. | `worktree` by default: NaviCore `config_tool/` and WCB `Wizard/`, so a Wizard change is tested inside Intellex before it ships. Run `shipped` for `ui_contract_*` and `ui_tools_load` only, reporting staleness as a note. |
| DX4 | Hooks in Intellex vs Playwright interception only. | Add H1-H3 now (pushed to Intellex `main`). Defer H4, since `LOCALAPPDATA` already works, and H5, since it is only needed for the exe. |
| DX5 | How to isolate. | Stage `src/` and `tools/`, and set `LOCALAPPDATA`. No `INTELLEX_DATA_DIR` needed. |
| DX6 | Which interpreter runs the Python tests. | Intellex's venv, as subprocess scripts. The harness stays stdlib plus pyserial, and `hil/ws.py` gains binary frames and an Origin header. Do not rebuild the renamed venv; call `python.exe -m`. |
| DX7 | Which board to flash. | W2 with the seeded bench image: app-only, then full (`intellex_flash`), attended once and then unattended. NaviCore app-only under its own key. Factory reset attended only. Never a probe. |
| DX8 | How to get WiFi. | Use Wi-Fi 2 as it is when it is already on NaviCore's AP (read-only check, unattended), and skip otherwise. AP hops, joins and the bounce stay attended (`intellex_wifi_join`). |
| DX9 | The serial-doorway gap (finding 4). | Write `intellex.nc_via_usb_doorway` as a `(should)` test now. Fix it in Intellex once it is confirmed on the bench: have the host identify a serial target as a WCB (for example the `?WDP,DUMP` SELF row that `probe()` already reads) and report `role:"wcb"` to the shim. File it as an Intellex item. |
| DX10 | The F21 re-arm rate. | Measure it first (`intellex.wifi_rterm_rate`). Bound it at 6 a minute, which is at least 10 s apart. Report the root cause to Intellex, or to the Wizard if it is `setRemoteConnected` re-arming. |
| DX11 | Findings 1, 2, 3 and 5. | Fix them in Intellex test-first, each with its failing test. Finding 5 is the only one that can cost a board, so it goes first. |
| DX12 | CI. | Later (phase 4): a separate job in the WCB repo, path-filtered to `Wizard/**`, that clones the public Intellex and runs `ui_contract_wizard` and `ui_tools_load` without a board. Non-blocking at first. |
| DX13 | Nightly full runs. | Include every unattended `intellex.*` test (no board, serial, WiFi when on the AP, online). Leave the new opt-ins unticked (D7 runs only what Greg ticked). Run each opt-in once in a targeted run and log it. |
| DX14 | The stray zip and the stale docs (findings 6-10). | Fix the docs in the IX-WP1 push. Remove the zip from the Intellex tree in the same push; it stays in history, which is acceptable for a non-secret design asset. |

### 4.4 Decisions taken writing IX-WP3 to IX-WP6 (2026-09-29)

| # | Decision | Why |
|---|---|---|
| DX15 | The board tests go in a new `suites/s33_intellex_bench.py`; `s32_intellex.py` stays board-free. | `s32` runs while a bench run holds every port; one file per kind keeps that true and the needs visible. |
| DX16 | Each `(should)` case is its own test id, not a case inside a normal test. | A normal test must be able to pass while a finding is open; each finding is then one visible failure. |
| DX17 | Venv checks are scripts under `tests/intellex/py` with one runner (`_ixpy.py`): cases grouped, a JSON results file, judged by `hil/intellex.py judge_py`. | The harness stays stdlib plus pyserial (DX6); one script serves several test ids through its groups. |
| DX18 | No real esptool in IX-WP3. `detect()` reads flash-id output built from esptool 5.3.1's own print statements; the pipeline runs a fake esptool, checked by its signature first, against a fake transport on `COMFAKE`. | Nothing may reach a port; no board has recorded esptool 5 output here. |
| DX19 | `ws_transport_navicore` proves it is on NaviCore's AP by a direct PONG carrying the USB version, never by reading or comparing an SSID. | The credential rule; and the PONG is the stronger proof (a WCB AP at the same address mirrors a mesh PONG). |
| DX20 | "Nothing reset the board" is proved off the stream too: W2's `(boot)` edge for W1, W1's for WCB 20, NaviCore's uptime. | A reset while the port is closed never reaches the stream. |
| DX21 | `serial_signals_reset_w1` and `bridge_reboot_w1` reset W1 unattended and end with `_peers_online`; `serial_device_loss_navicore` is behind the new opt-in `intellex_reboot` (unticked), in `nc_guard`, and in `hil/servos.py`. | The same rules every W1 and NaviCore restart already follows (`docs/HIL_TESTING.md` §5). |
| DX22 | `bridge_wire_w1` needs W2 as well as W1 and probe1: W2 sends the mesh traffic. | "30 s of mesh traffic" needs a sender; W2's `;W1,;S0` lines are ETM-delivered and land on W1's USB. |
| DX23 | The wiki tests use a crafted wiki (`tests/intellex/fixtures/wiki`), never Greg's downloaded wikis. | The pages must carry the links, images and script under test, and nothing in a test should depend on real docs. |
| DX24 | `hil/intellex.py` finds the sibling repos from a git worktree (`github_dir()`). | Every Intellex test skipped from a worktree, which is where the week's agents write tests. |
| DX25 | Specs set a 15 s default action timeout; `ui_launcher` runs at 1280x1000. | A hidden control fails in 15 s, not at the 300 s test timeout; the app window is 1280x880, and at Playwright's 720 px the folded maintenance bar covers the device list. |

### 4.5 Decisions taken writing IX-WP7 and IX-WP8 (2026-09-29)

| # | Decision | Why |
|---|---|---|
| DX26 | The tools-through-Intellex tests go in a new `suites/s34_intellex_tools.py`; `s33` keeps the transports and the bridge. | One file per kind, as DX15; and a new file keeps this work's merges apart from the other agents'. |
| DX27 | "Nothing reset the board" is judged in the harness, on a raw `/_link` client of its own (`run_intellex_test(link_check=...)`, `boot_check`), beside the off-stream witnesses (DX20), not in the page. | The harness sees every byte the host fans out, including what a tool never displays, and never logs it; a page's view goes through the tool's own parser. |
| DX28 | The four read-only NaviCore tests run without `nc_guard`. Each spec fails on any JSON the tool sends that is not a read (`NC_READ_ONLY`), and the harness resets the monitor and debug flags afterwards. | The tool's connect writes nothing (its command library is compared, never synced); a full snapshot and restore (config, library, clips, peers, mode) twice would add about 20 s a test for nothing. |
| DX29 | `nc_save_unchanged` stays behind `intellex_nc_save`, unticked, although `nc_guard` exists now; its round trip saves `chRateHz` one step away and back. | The plan registers the opt-in and new opt-ins start unticked; `nccfg.*` already saves under `nc_guard` unattended, so ticking it is Greg's call. A Save with no edits sends nothing, so the round trip needs one field, and `chRateHz` moves nothing. |
| DX30 | Every Wizard test guards all the bench's WCBs and stops every other WCB's `?RTERM` afterwards, whatever the test is about. | The shim pulls and arms each board W1 hears within about 20 s of the page loading (`routeMeshThroughBoard`). |
| DX31 | `wizard_remote_pull_parts` observes the shim's own pull of W2 instead of calling `remoteBoardPull` itself. | Two pulls of one board through one relay race on its stream; the shim's pull is what runs for a user. |
| DX32 | Findings 16 and 17 get their own `(should)` tests, and every W1 test hands a W1 that does not answer afterwards to `reset_into_app` (an EN pulse with GPIO0 high), failing the test with it. | DX16; and a W1 left in the ROM loader would fail every test after it. |
| DX33 | `nc_via_usb_doorway` waits 21 s with nothing attached to W1 before its first connect. | A relay window left open by the test before (a `;W20,{...}` line) made the cold connect come up direct too in the dry run, and the companion data could not tell the two cases apart. |

### 4.6 Decisions taken writing IX-WP9 and IX-WP10 (2026-09-29)

| # | Decision | Why |
|---|---|---|
| DX34 | The WiFi tests put the PC's spare adapter on NaviCore's AP themselves (`s45 _on_ap`: `hil/wlan.py` `pc_on_ap`, spare adapter only) behind `navicore_wifi`, the key s45 uses for the same act, and `ws_transport_navicore` moves onto it. Replaces DX8's "use Wi-Fi 2 as it is, skip otherwise" and DX19's "never joins"; the attended hop and bounce keep `intellex_wifi_join`. | Waiting for the adapter to be there left `ws_transport_navicore` skipped in `20260929-122112`; `pc_on_ap` is the join s45's fifteen tests run nightly; the effect on the bench is the same as theirs, so the gate is the same (ticked, D70). DX19's direct-PONG proof stays. |
| DX35 | A step the spec must sequence with the bench goes through a bridge hook: `run_intellex_test(hooks={name: fn(host, body)})`, the bridge's `/hook`, `board.js` `hil.hook`. | The harness's test thread is parked while Playwright runs; moving the adapter, counting a console over a window the page defines and judging the host's flash log each need the harness at a moment only the spec knows. Timers or files would race. |
| DX36 | No network name reaches a spec, session.log or a log file: `IntellexHost(hide=...)` scrubs the host's lines, `copy_logs` the stage's and the copied logs, `status_view` the status's `lastError`; a venv script gets a SHA-256 of the SSID, or the SSID in its environment for the bounce. | A host attached over WiFi records the SSID and names both networks when the adapter moves (`host.py` `_reidentify_if_moved`); the rule every WiFi test here keeps (s28, D63). |
| DX37 | The W2 Flash and Factory Reset pass the Wizard's `pushConfig:false`, so it re-pulls W2 instead of pushing its whole pre-flash config back; the Factory Reset's restore is the harness's s31 procedure over W2's USB. | A Flash keeps NVS, and the Factory Reset's config comes back through a restore the harness has proved (`nvs_erase`); a full push is the Wizard's own feature, never run on the bench, and a defect in it would leak into W2's config inside a flash test. An Update outside the guided setup only re-pulls anyway. |
| DX38 | A W2 that does not answer after a flash gets an EN reset (`reset_into_app`), then the same image flashed again in full through the host's own `/_api/flash-wcb` (`reflash_w2`); only then the `arduino-cli upload` last resort. | The plan's first recovery ("run the same Intellex flash again"; the ESP32 ROM loader answers every auto-reset), and one bad flash must not fail every W2 test after it in an unattended run. |
| DX39 | Identity: a W2 test reads W2's version at run time, skips unless `results/builds/wcb-esp32-meshq`'s app carries it, checks the three files, and seeds them as `WCB_<version>_hilbench_ESP32*.bin`; the other product's branch is `hil-none`. Afterwards: the same version, app0 running, a boot heard by W1, `config_guard`. | W2 moved to `6.2.1_291138RSEP2026` after the plan was written, and the CI release carries the same version string: the bytes come from the bench folder only (§4.1, "never flash without the seed"). Nothing cached for the other product means no stray flash of it can find an image. |
| DX40 | `flash_one_at_a_time` asks for the second flash from the Wizard's page (the NaviCore tool's route and the Wizard's again), not from a shell's NaviCore pane. | The host's claim does not know panes; a NaviCore pane attached through W2 would put its handshake on the mesh during a flash. |
| DX41 | The NaviCore flash records a FLASHED.md row (`ncflash.record_flash`), `OK` only when `?OTALOCAL,STATUS` shows the image's App SHA256 on app0; it leaves NaviCore on app0 whatever slot it ran; its recovery is `ncflash.recover`, with the esptool rungs only when `navicore_esptool` is ticked. | Every flash that reaches NaviCore is recorded (INF4), and `last_written()` takes an OK row as what NaviCore runs. Intellex writes app0 and erases otadata, so the slot is not the test's to choose. |
| DX42 | `wifi_rterm_rate` counts each WCB's own USB console (the harness's hooks) and the re-arms the page wrote, and bounds each board at DX10's 6 a minute. A normal test, not `(should)`. | The console count is the effect, the page's frames the cause. Reading the current Wizard and shim finds no periodic re-arm, so a failure is a measurement to triage, not a known defect. |
| DX43 | Finding 18's test drives the host's API with no page: a full flash of a seed with no bootloader, then `?VERSION` on W2's own port opened with both lines low; a stranded W2 is reset into its app. | The defect is the host's; the page adds nothing. Nothing is written either way: a refusal that did not hold would write the image W2 already runs. |
| DX44 | The dry runs run the real staged host, the real tools under Playwright and the harness bodies against fakes: a fake pyserial for the host (a simulated W2 console, the NaviCore emulator), the repo's fake esptool, and a fake NaviCore endpoint on 127.0.0.1:80 that can turn into a W1 doorway; the staged `discover.ssid_for_host` reads a scratch file. None of it is committed. | No bench, no port, no netsh; the scaffolding stands in only for what the bench provides, so what runs is the code the bench run will run. |
| DX45 | Finding 19's `(should)` is a board-free s32 test, `intellex.link_drop_no_stall`: a staged host attached to `hil/ws.py` `WsEndpoint` on 127.0.0.2:80, which vanishes (listener closed, the open socket kept open and silent). `wifi_link_loss` keeps only a 30 s notice limit and notes time over 15 s. | The stall is the host's, and needs no NaviCore restart, no WiFi and no servo to show: the stand-in reproduces the bench's verdict line, traceback and 10 s exactly, every run. 127.0.0.2 because `attach_validation` counts on 127.0.0.1:80 being closed. A normal bench test that fails on a known defect would hide a regression in the reattach it exists for. |
| DX46 | `hil/wlan.py` `rejoin(reach=)`: connected with a lease is not back. The link must carry a TCP connect to the access point within 10 s, or the harness re-associates the adapter (netsh disconnect, the temporary profile connected again) and wants the connect then. `wifi_link_loss` times the reattach from that proof, 30 s. | Run `20260929-202852`: a REBOOT deauthenticates nobody, and Windows kept the stale association for 90 s with no WLAN event. Intellex's own remedy, the bounce, is leashed off here (and fires only on a wrong route), so the harness stands in for Windows; the reattach is then Intellex's alone. |

---

## Revision log

| Date | Change |
|---|---|
| 2026-09-27 | Created. Map of Intellex at `d615344`, how to run it under test, IX-WP1-14, risks and decisions. Research only: nothing run, nothing changed. |
| 2026-09-29 | IX-WP3 and IX-WP4 finished, IX-WP5 and IX-WP6 written (35 tests; `s32` additions, `s33_intellex_bench.py`, `tests/intellex/py`, `fixtures`, five specs). Findings 12-15 and three harness notes (§1.6); status notes on IX-WP3 to IX-WP6; decisions DX15-DX25 (§4.4). The no-board tests ran standalone against Intellex `e9f95f2`; the board tests are not yet run on the bench. Commit `_(pending)_`. |
| 2026-09-29 | IX-WP3 to IX-WP6 bench-verified (`20260929-043806`, 54 `intellex.*`): 45 pass; the seven `(should)` tests fail as designed (findings 1-3, 5, 12-15); `ws_transport_navicore` skips until the PC's second adapter is on NaviCore's AP (IX-WP9) and `serial_device_loss_navicore` ran behind `intellex_reboot`, now ticked (D59). |
| 2026-09-29 | IX-WP7 and IX-WP8 written: 15 tests in `suites/s34_intellex_tools.py` (the 13 plan rows, plus `(should)` tests for the new findings 16 and 17), specs `wizard_board.spec.js` and `nc_board.spec.js`, `tests/intellex/lib/board.js`. `run_intellex_test` gains `link_check` (the rawLink) and `recover`; `boot_check` and `reset_into_app`; opt-in `intellex_nc_save` registered, unticked. Finding 4 narrowed to W1's relay window. Status notes on IX-WP7 and IX-WP8; decisions DX26-DX33 (§4.5). Dry-run against simulated boards; not yet run on the bench. Commit `_(pending)_`. |
| 2026-09-29 | IX-WP9 and IX-WP10 written: 13 tests. `suites/s35_intellex_wifi.py` (seven: discovery, the config tool and the Wizard over WiFi through NaviCore, the RTERM re-arm rate, the link lost to a NaviCore restart, and the attended AP hop and bounce) and `suites/s36_intellex_flash.py` (six: W2's Update, Flash and one-at-a-time, the `(should)` test for the new finding 18, the attended Factory Reset, NaviCore's app); `intellex.ws_transport_navicore` now joins NaviCore's AP itself. Specs `wifi_board.spec.js` and `flash_board.spec.js`, venv script `wifi_units.py`. `run_intellex_test` gains `seed`, `hide` and `hooks` (the bridge's `/hook`); `IntellexHost` and `copy_logs` take network names out; `hil/wlan.py` `rejoin`. Opt-ins `intellex_flash`, `intellex_flash_factory`, `intellex_flash_navicore` and `intellex_wifi_join` registered, unticked; the unattended WiFi tests use `navicore_wifi`. Finding 18; status notes on IX-WP9 and IX-WP10; decisions DX34-DX44 (§4.6). Dry-run against a fake endpoint, a simulated W2 and the fake esptool; not yet run on the bench. Commit `_(pending)_`. |
| 2026-09-29 | IX-WP9 and IX-WP10 on the bench (`20260929-202852`, `20260929-203453`): 5 WiFi and 4 flash tests pass, the two attended WiFi tests skip, `flash_refused_board_runs` fails as designed. Findings 13, 14 and 18 confirmed on the bench; finding 19 (dropping a dead WebSocket link stalls the host 10 s) found by `wifi_link_loss`, with a board-free `(should)` `intellex.link_drop_no_stall` (`hil/ws.py` `WsEndpoint`). `wifi_link_loss` reworked: a 30 s notice limit, `wlan.rejoin(reach=)` re-associating a stale association, the reattach timed from a proven connect (DX45, DX46). Commit `_(pending)_`. |
