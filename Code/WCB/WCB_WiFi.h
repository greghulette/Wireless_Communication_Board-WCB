#pragma once

// ════════════════════════════════════════════════════════════════════════════
//  WCB WiFi — host an access point, or join one, on the mesh channel
// ════════════════════════════════════════════════════════════════════════════
//
//  Gives a WCB an IP presence so a phone or desktop app can manage the mesh with
//  no USB cable and no extra hardware. Off by default; USB and ESP-NOW are
//  unchanged when it is off.
//
//  WHY THIS EXISTS AT ALL. A phone cannot open a serial port — the Web Serial API
//  is desktop-only — so a mobile client has no way to reach the mesh except over
//  the network. The alternatives were a dedicated relay board (an extra board in
//  every droid) or requiring a NaviCore (most WCB users do not have one), so the
//  boards have to be able to carry it themselves.
//
//  ── THE ONE RULE: THE RADIO HAS ONE CHANNEL ────────────────────────────────
//  The ESP32 has a single radio, so the AP we host — or the AP we associate
//  with — shares a channel with ESP-NOW. That channel is ALWAYS `meshChannel`
//  (?WCBCH) and is deliberately NOT separately configurable: a WiFi channel that
//  disagrees with the mesh is a total, silent blackout — every ESP-NOW packet
//  dropped, nothing logged on the other boards, no fault indication anywhere.
//  Every entry point below therefore passes meshChannel explicitly, and JOIN
//  verifies the channel after associating rather than trusting it. Channel sync
//  (WCB_ChanSync.h) is the one thing that moves meshChannel at runtime, and it
//  moves the whole mesh with it.
//
//  ── MODES ──────────────────────────────────────────────────────────────────
//    OFF   ESP-NOW only. The default, and byte-for-byte the previous behaviour.
//    AP    Host a SoftAP. Self-contained — no infrastructure, and nothing
//          external can move the radio off the mesh channel. This is the mode
//          for a droid in the field.
//    JOIN  Associate with an existing AP (a NaviCore's, typically) so one
//          network reaches everything and the phone keeps its internet. There is
//          deliberately NO automatic fallback to hosting: a board that quietly
//          became an AP because it could not find its network is exactly the
//          rogue-AP surprise this design is meant to avoid. It retries forever
//          and says so.
//
//  ── WHY SETTINGS APPLY AT THE NEXT BOOT ────────────────────────────────────
//  Same discipline as the mesh channel (saveMeshChannelToPreferences). Bringing
//  an interface up or down re-enters the WiFi driver underneath a running
//  esp_now, and a config push arrives as a STREAM of commands — losing the radio
//  midway would strand the rest of the push. Persist, then reboot deliberately.
// ════════════════════════════════════════════════════════════════════════════

#include <Arduino.h>

// Persisted mode. Values are stored in NVS — do not renumber.
enum WcbWifiMode : uint8_t {
  WCB_WIFI_OFF  = 0,
  WCB_WIFI_AP   = 1,
  WCB_WIFI_JOIN = 2,
};

// ---- Settings (loaded from NVS at boot, edited via ?WIFI) ------------------
extern WcbWifiMode wcbWifiMode;
extern String      wcbWifiApSsid;     // "" = derive "WCB-<n>" / the board alias
extern String      wcbWifiApPass;     // >= 8 chars or the AP refuses to start
extern String      wcbWifiJoinSsid;
extern String      wcbWifiJoinPass;

// ---- WebSocket token (docs/WIFI_DESIGN.md §7) ------------------------------
// What a WebSocket session sends as ?AUTH,<token> before anything it sends runs or any console output reaches it.
// "" = none. JOIN does not start without one. A char array, not a String: a setter whose String allocation failed
// would store "" - a cleared token - and say nothing (CLAUDE.md rule 14).
static const size_t WCB_WIFI_TOKEN_MIN = 8;
static const size_t WCB_WIFI_TOKEN_MAX = 32;
extern char        wcbWifiToken[WCB_WIFI_TOKEN_MAX + 1];

// ---- Lifecycle ------------------------------------------------------------
// Load settings from NVS. Call BEFORE wcbWifiStart().
void loadWifiSettings();
void saveWifiSettings();

// Bring the interface up. MUST be called from setup() BEFORE esp_now_init():
// in AP mode the SoftAP has to own the radio channel before ESP-NOW pins it,
// which is the same ordering WCB_Client discovered on NaviCore. Safe to call
// when the mode is OFF — it does nothing and leaves WIFI_STA exactly as before.
void wcbWifiStart();

// Serviced every loop() pass. Non-blocking. Carries a pending JOIN association
// forward and re-checks that the radio has not drifted off the mesh channel.
void wcbWifiService();
// ?DEBUG,FAULT,JOINNOIP: a joined JOIN board drops its association so the knob bites the next one (a test knob).
void wcbWifiFaultRejoin();

// ---- Status ---------------------------------------------------------------
// True when an interface is up AND on the mesh channel (i.e. actually usable).
bool wcbWifiReady();
// Dotted-quad of this board on whichever interface is up, or "" when none is.
String wcbWifiIP();

// ---- Command / query ------------------------------------------------------
//   ?WIFI                       status (mode, SSID, IP, channel, free heap)
//   ?WIFI,OFF                   disable (next boot)
//   ?WIFI,AP,<ssid>,<pass>      host an AP  (next boot)
//   ?WIFI,JOIN,<ssid>,<pass>    join an AP  (next boot; needs a token)
//   ?WIFI,TOKEN,<token>         the WebSocket token (sessions opened from then on)
//   ?WIFI,TOKEN,CLEAR           no token
void processWifiCommand(const String &args);

// ---- Channel sync (WCB_ChanSync.cpp, docs/CHANNEL_SYNC.md) -----------------
// The mesh channel moves at runtime now: a MOVE switches it live, or restarts a board whose interface is up. JOIN
// follows its network to another channel by moving the mesh there first (§5).
// These read what RUNS (the driver, the AP this boot started), never wcbWifiMode, which ?WIFI changes at once.
bool wcbWifiJoinAssociated();            // the station is associated with an AP now: the AP owns the channel (F_PINNED)
bool wcbWifiApUp();                      // this boot brought an access point up
bool wcbWifiInterfaceUp();               // an AP is up, or the station is associated: a channel move needs a restart
bool wcbWifiBootedOff();                 // this boot started no WiFi interface (?WIFI,OFF): ESP-NOW is all the driver runs
bool wcbWifiCanHop(const char **why);    // may a search take the radio to other channels now? why = the reason not
bool wcbWifiJoinScanning();              // a JOIN scan is running (the driver refuses a channel switch meanwhile)
bool wcbWifiFullScanRunning();           // a JOIN full-band scan is running: the radio is off the mesh channel
void wcbWifiJoinCancelConnect();         // JOIN, not associated: cancel a connect in progress before a live move
