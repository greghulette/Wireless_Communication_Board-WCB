#include "WCB_RemoteTerm.h"   // Must be first — redirects Serial -> WCBDebugSerial
#include "WCB_WiFi.h"
#include "WCB_WS.h"           // wcbWsClientCount() for the status block
#include "WCB_Storage.h"

#include <WiFi.h>
#include <esp_wifi.h>

// Defined in WCB.ino.
extern uint8_t meshChannel;
extern int     WCB_Number;
extern String  wcb_alias;

// ---- Settings -------------------------------------------------------------
WcbWifiMode wcbWifiMode = WCB_WIFI_OFF;
String      wcbWifiApSsid;
String      wcbWifiApPass;
String      wcbWifiJoinSsid;
String      wcbWifiJoinPass;

// ---- Runtime state --------------------------------------------------------
static bool          wifiUp          = false;   // an interface is up
static bool          joinSettled     = false;   // JOIN reached a terminal state
static unsigned long joinNextAttempt = 0;
static uint16_t      joinAttempts    = 0;
static unsigned long lastChannelWarn = 0;

// Retry cadence for JOIN. Deliberately unbounded in total: on a shared power
// switch this board is ready long before a NaviCore raises its SoftAP (that
// board mounts LittleFS, loads config and allocates a multi-megabyte PSRAM
// buffer first), so a bounded window would lose the race on every cold boot.
// Retrying costs nothing — see the note on channel pinning in wcbWifiJoinTry().
static const unsigned long JOIN_RETRY_MS   = 5000;
// How often to re-check that the radio is still on the mesh channel, and how
// often we are willing to complain about it.
static const unsigned long CHAN_CHECK_MS   = 30000;

// ════════════════════════════════════════════════════════════════════════════
//  NVS
// ════════════════════════════════════════════════════════════════════════════
void loadWifiSettings() {
    preferences.begin("wifi_cfg", true);
    wcbWifiMode     = (WcbWifiMode)preferences.getUChar("mode", WCB_WIFI_OFF);
    wcbWifiApSsid   = preferences.getString("ap_ssid",   "");
    wcbWifiApPass   = preferences.getString("ap_pass",   "");
    wcbWifiJoinSsid = preferences.getString("jn_ssid",   "");
    wcbWifiJoinPass = preferences.getString("jn_pass",   "");
    preferences.end();
    if (wcbWifiMode > WCB_WIFI_JOIN) wcbWifiMode = WCB_WIFI_OFF;   // corrupt/never-set
}

void saveWifiSettings() {
    preferences.begin("wifi_cfg", false);
    preferences.putUChar("mode", (uint8_t)wcbWifiMode);
    preferences.putString("ap_ssid",   wcbWifiApSsid);
    preferences.putString("ap_pass",   wcbWifiApPass);
    preferences.putString("jn_ssid",   wcbWifiJoinSsid);
    preferences.putString("jn_pass",   wcbWifiJoinPass);
    preferences.end();
}

// ════════════════════════════════════════════════════════════════════════════
//  Helpers
// ════════════════════════════════════════════════════════════════════════════
// Default SSID. Uses the alias when the operator has set one, so two droids in
// the same room do not both advertise an identical name — which is a genuinely
// confusing afternoon at a convention. Falls back to the board number.
static String wcbWifiDefaultSsid() {
    String s = "WCB-";
    if (wcb_alias.length() > 0) s += wcb_alias;
    else                        s += String(WCB_Number);
    // SSIDs are capped at 32 bytes.
    if (s.length() > 32) s = s.substring(0, 32);
    return s;
}

// Read the radio's actual primary channel. This is the question that matters —
// what we asked for and what the radio is on can differ after an association.
static uint8_t wcbWifiRadioChannel() {
    uint8_t primary = 0;
    wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
    if (esp_wifi_get_channel(&primary, &second) != ESP_OK) return 0;
    return primary;
}

// ════════════════════════════════════════════════════════════════════════════
//  Bring-up
// ════════════════════════════════════════════════════════════════════════════
// Host the AP. Called from wcbWifiStart(), i.e. BEFORE esp_now_init().
static void wcbWifiStartAP() {
    // FAIL CLOSED ON THE PASSWORD. WiFi.softAP() with an empty passphrase
    // cheerfully creates an OPEN network, and this interface is an
    // unauthenticated command channel to every board on the mesh — a bare
    // "?RESTART" carries no credential of its own. An open AP would hand the
    // whole droid to anyone in range, so refusing has to be explicit and loud.
    if (wcbWifiApPass.length() < 8) {
        Serial.printf("[WIFI] REFUSED: AP password is %u character(s); WPA2 needs 8.\n",
                      (unsigned)wcbWifiApPass.length());
        Serial.println("[WIFI] Not starting an OPEN access point — this interface accepts");
        Serial.println("[WIFI] commands for the whole mesh with no credential of its own.");
        Serial.println("[WIFI] Set one with:  ?WIFI,AP,<ssid>,<password>");
        return;
    }

    const String ssid = wcbWifiApSsid.length() ? wcbWifiApSsid : wcbWifiDefaultSsid();

    // AP_STA, not AP: ESP-NOW rides the STA interface, and tearing that down
    // would take the mesh with it.
    WiFi.mode(WIFI_AP_STA);

    // NOTE: the address is set AFTER softAP() below, not here. See the comment
    // there — configuring first leaves the DHCP server stopped.

    // CHANNEL IS THE THIRD PARAMETER AND DEFAULTS TO 1. Pass meshChannel
    // explicitly. Once an AP owns the radio, nothing later moves it — the
    // esp_wifi_set_channel() call in setup() cannot override an AP — so a
    // defaulted channel against a mesh on any other channel is a silent, total
    // blackout. max_connection 4: this is a config channel, not a hotspot.
    if (WiFi.softAP(ssid.c_str(), wcbWifiApPass.c_str(), meshChannel, /*hidden=*/0, /*max_conn=*/4)) {
        wifiUp = true;

        // WE DELIBERATELY DO NOT CALL softAPConfig(). The board stays on the stock
        // 192.168.4.1.
        //
        // The idea was to sit at 192.168.4.<board number> so a host could tell a WCB
        // from a NaviCore by address, since every ESP32 SoftAP defaults to .1. It was
        // tried both before and after softAP() and BREAKS DHCP either way: the AP
        // comes up, clients associate, ?WIFI reports the right IP — and no lease ever
        // arrives, so every client lands on 169.254.x and cannot reach the board at
        // all. Measured repeatedly on hardware ("unable to contact your DHCP server"),
        // and not a startup race — it still failed on a board that had been up for
        // minutes. It appeared to work once, which makes it worse than useless: an
        // intermittent DHCP failure is far more expensive to own than a duplicate
        // default address.
        //
        // Identity belongs in the protocol, not the address. A host should ASK — one
        // command over the socket names the board — rather than infer from an IP.
        Serial.printf("[WIFI] SoftAP \"%s\" up on channel %u — http://%s/\n",
                      ssid.c_str(), meshChannel, WiFi.softAPIP().toString().c_str());
        Serial.println("[WIFI] ESP-NOW shares this channel (WIFI_AP_STA).");
    } else {
        Serial.printf("[WIFI] SoftAP \"%s\" FAILED to start on channel %u.\n",
                      ssid.c_str(), meshChannel);
    }
}

// One association attempt. Non-blocking — the result is picked up by
// wcbWifiService().
static void wcbWifiJoinTry() {
    // PIN THE CHANNEL. A WiFi.begin() with no channel probes EVERY channel
    // looking for the SSID, and for the length of each sweep this board is off
    // the mesh — which does not present as "WiFi failed", it presents as the
    // board randomly dropping mesh traffic. Passing meshChannel confines the
    // probe to the channel we are already on, which is the only reason retrying
    // forever is affordable. Auto-reconnect stays OFF so a vanished AP cannot
    // start sweeps of its own behind our back.
    WiFi.setAutoReconnect(false);
    WiFi.begin(wcbWifiJoinSsid.c_str(), wcbWifiJoinPass.c_str(), meshChannel);
    joinAttempts++;
}

void wcbWifiStart() {
    if (wcbWifiMode == WCB_WIFI_OFF) return;   // leave WIFI_STA exactly as it was

    if (wcbWifiMode == WCB_WIFI_AP) {
        wcbWifiStartAP();
        return;
    }

    // JOIN. The association itself is started from wcbWifiService() rather than
    // here: setup() must not block waiting for an AP that may still be booting,
    // and ESP-NOW has to come up regardless of whether the network ever appears.
    if (wcbWifiJoinSsid.length() == 0) {
        Serial.println("[WIFI] JOIN mode selected but no SSID is set — staying off.");
        Serial.println("[WIFI] Set one with:  ?WIFI,JOIN,<ssid>,<password>");
        joinSettled = true;
        return;
    }
    Serial.printf("[WIFI] will join \"%s\" on channel %u (retrying in the background)\n",
                  wcbWifiJoinSsid.c_str(), meshChannel);
}

// ════════════════════════════════════════════════════════════════════════════
//  Service — called every loop() pass, never blocks
// ════════════════════════════════════════════════════════════════════════════
void wcbWifiService() {
    if (wcbWifiMode == WCB_WIFI_OFF) return;
    const unsigned long now = millis();

    // ---- JOIN state machine ------------------------------------------------
    if (wcbWifiMode == WCB_WIFI_JOIN && !joinSettled) {
        if (WiFi.status() == WL_CONNECTED) {
            // VERIFY WHERE THE ASSOCIATION ACTUALLY LEFT US. An AP owns the
            // channel once associated. A NaviCore raises its SoftAP on its own
            // mesh channel, so joining it is a no-op channel-wise — but if we
            // land anywhere else we are off the mesh, and being deaf is worse
            // than having no WiFi. Hang up rather than sit there silently.
            const uint8_t ch = wcbWifiRadioChannel();
            if (ch != meshChannel) {
                Serial.printf("[WIFI] joined \"%s\" but the radio is on channel %u, not %u — "
                              "that is OFF-MESH. Disconnecting.\n",
                              wcbWifiJoinSsid.c_str(), ch, meshChannel);
                Serial.println("[WIFI] Move that AP to the mesh channel, or change ?WCBCH to match it.");
                WiFi.disconnect(true);
                joinSettled = true;      // do not thrash: the operator must fix one end
                return;
            }
            wifiUp      = true;
            joinSettled = true;
            Serial.printf("[WIFI] joined \"%s\" on channel %u after %u attempt(s) — http://%s/\n",
                          wcbWifiJoinSsid.c_str(), ch, joinAttempts,
                          WiFi.localIP().toString().c_str());
            return;
        }
        if (now >= joinNextAttempt) {
            wcbWifiJoinTry();
            joinNextAttempt = now + JOIN_RETRY_MS;
            // Only say so occasionally — this can run for a long time if the
            // other board is simply switched off, and that is not an error.
            if (joinAttempts == 1 || (joinAttempts % 12) == 0)
                Serial.printf("[WIFI] still looking for \"%s\" (attempt %u)\n",
                              wcbWifiJoinSsid.c_str(), joinAttempts);
        }
        return;
    }

    // ---- Drift check -------------------------------------------------------
    // Cheap insurance. Nothing here should move the channel, but a channel that
    // has drifted means every ESP-NOW packet is being dropped with no other
    // symptom, so it is worth naming out loud rather than leaving to be
    // discovered as "the mesh went weird".
    if (wifiUp && (now - lastChannelWarn) >= CHAN_CHECK_MS) {
        lastChannelWarn = now;
        const uint8_t ch = wcbWifiRadioChannel();
        if (ch != 0 && ch != meshChannel)
            Serial.printf("[WIFI] WARNING: radio is on channel %u but the mesh is on %u — "
                          "this board cannot reach the mesh.\n", ch, meshChannel);
    }
}

// ════════════════════════════════════════════════════════════════════════════
//  Status
// ════════════════════════════════════════════════════════════════════════════
bool wcbWifiReady() {
    if (!wifiUp) return false;
    const uint8_t ch = wcbWifiRadioChannel();
    return (ch == 0 || ch == meshChannel);
}

String wcbWifiIP() {
    if (!wifiUp) return String("");
    if (wcbWifiMode == WCB_WIFI_AP)   return WiFi.softAPIP().toString();
    if (WiFi.status() == WL_CONNECTED) return WiFi.localIP().toString();
    return String("");
}

// ════════════════════════════════════════════════════════════════════════════
//  ?WIFI command
// ════════════════════════════════════════════════════════════════════════════
static const char *wcbWifiModeName(WcbWifiMode m) {
    switch (m) {
        case WCB_WIFI_AP:   return "AP";
        case WCB_WIFI_JOIN: return "JOIN";
        default:            return "OFF";
    }
}

static void wcbWifiPrintStatus() {
    Serial.println("------ WiFi ------------------------------------------");
    Serial.printf("Mode          : %s%s\n", wcbWifiModeName(wcbWifiMode),
                  wcbWifiMode == WCB_WIFI_OFF ? " (ESP-NOW only)" : "");
    if (wcbWifiMode == WCB_WIFI_AP) {
        Serial.printf("AP SSID       : %s\n",
                      wcbWifiApSsid.length() ? wcbWifiApSsid.c_str()
                                             : (wcbWifiDefaultSsid() + " (derived)").c_str());
        Serial.printf("AP password   : %s\n",
                      wcbWifiApPass.length() >= 8 ? "set" : "NOT SET — AP will not start");
        if (wifiUp) Serial.printf("Clients       : %d\n", WiFi.softAPgetStationNum());
    } else if (wcbWifiMode == WCB_WIFI_JOIN) {
        Serial.printf("Join SSID     : %s\n",
                      wcbWifiJoinSsid.length() ? wcbWifiJoinSsid.c_str() : "NOT SET");
        Serial.printf("Association   : %s (%u attempt(s))\n",
                      WiFi.status() == WL_CONNECTED ? "connected" : "not connected",
                      joinAttempts);
    }
    Serial.printf("Interface     : %s\n", wifiUp ? "up" : "down");
    const String ip = wcbWifiIP();
    if (ip.length()) Serial.printf("IP address    : %s\n", ip.c_str());
    // The channel is the single most useful number here: a mismatch means the
    // mesh is silently dead, and nothing else reports it.
    const uint8_t ch = wcbWifiRadioChannel();
    Serial.printf("Radio channel : %u  (mesh channel %u)%s\n", ch, meshChannel,
                  (ch && ch != meshChannel) ? "   *** MISMATCH — OFF-MESH ***" : "");
    // Free heap is the number that decides whether a web server can live here at
    // all, so make it observable rather than something you have to guess at.
    // "up with nobody connected" and "never started" both show 0 clients but want
    // completely different fixes, so say which one this is.
    if (wcbWsRunning())
        Serial.printf("WS endpoint   : ws://%s/ws  (%d client(s) connected)\n",
                      ip.c_str(), wcbWsClientCount());
    else
        Serial.println("WS endpoint   : NOT RUNNING");
    Serial.printf("Free heap     : %u bytes (min since boot %u)\n",
                  (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getMinFreeHeap());
    Serial.println("------------------------------------------------------");
}

void processWifiCommand(const String &args) {
    if (args.length() == 0) { wcbWifiPrintStatus(); return; }

    // Split on commas, but only the first two: a password may legitimately
    // contain a comma, so everything after the SSID field is the password.
    const int c1 = args.indexOf(',');
    String verb = (c1 < 0) ? args : args.substring(0, c1);
    verb.trim();
    verb.toUpperCase();

    if (verb == "OFF") {
        wcbWifiMode = WCB_WIFI_OFF;
        saveWifiSettings();
        Serial.println("WiFi disabled — reboot to apply.");
        return;
    }

    if (verb != "AP" && verb != "JOIN") {
        Serial.println("Usage: ?WIFI | ?WIFI,OFF | ?WIFI,AP,<ssid>,<pass> | ?WIFI,JOIN,<ssid>,<pass>");
        return;
    }

    const String rest = (c1 < 0) ? String("") : args.substring(c1 + 1);
    const int    c2   = rest.indexOf(',');
    String ssid = (c2 < 0) ? rest : rest.substring(0, c2);
    String pass = (c2 < 0) ? String("") : rest.substring(c2 + 1);
    ssid.trim();
    // Deliberately NOT trimmed: a trailing space is legal in a WPA2 passphrase,
    // and silently eating it would lock the operator out of their own AP.

    if (ssid.length() == 0 && verb == "JOIN") {
        Serial.println("A network name is required: ?WIFI,JOIN,<ssid>,<pass>");
        return;
    }
    if (ssid.length() > 32) {
        Serial.printf("SSID is %u characters; the maximum is 32.\n", (unsigned)ssid.length());
        return;
    }
    // Refuse a too-short AP password HERE as well as at bring-up, so the
    // operator finds out while they are still typing rather than after a reboot
    // that silently comes up with no AP.
    if (verb == "AP" && pass.length() < 8) {
        Serial.printf("AP password is %u character(s); WPA2 requires at least 8.\n",
                      (unsigned)pass.length());
        Serial.println("Refusing to configure an OPEN access point — this interface accepts");
        Serial.println("commands for the whole mesh with no credential of its own.");
        return;
    }

    if (verb == "AP") {
        wcbWifiMode   = WCB_WIFI_AP;
        wcbWifiApSsid = ssid;
        wcbWifiApPass = pass;
    } else {
        wcbWifiMode     = WCB_WIFI_JOIN;
        wcbWifiJoinSsid = ssid;
        wcbWifiJoinPass = pass;
    }
    saveWifiSettings();

    Serial.printf("WiFi mode set to %s", wcbWifiModeName(wcbWifiMode));
    if (wcbWifiMode == WCB_WIFI_AP)
        Serial.printf(" — SSID \"%s\"", ssid.length() ? ssid.c_str() : wcbWifiDefaultSsid().c_str());
    else
        Serial.printf(" — joining \"%s\"", ssid.c_str());
    Serial.printf(" on channel %u (the mesh channel). Reboot to apply.\n", meshChannel);
    // Say this every time. The channel coupling is the single thing most likely
    // to be misunderstood, and it is invisible until the mesh stops working.
    Serial.println("Note: the AP and ESP-NOW share one radio, so the WiFi channel always");
    Serial.println("follows ?WCBCH. It cannot be set independently.");
}
