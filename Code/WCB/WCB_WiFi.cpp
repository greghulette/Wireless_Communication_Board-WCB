#include "WCB_RemoteTerm.h"   // Must be first — redirects Serial -> WCBDebugSerial
#include "WCB_WiFi.h"
#include "WCB_WS.h"           // wcbWsClientCount() for the status block
#include "WCB_Storage.h"
#include "WCB_Fault.h"         // ?DEBUG,FAULT,JOINCHAN / JOINNOIP / CHANDRIFT test knobs

#include <WiFi.h>
#include <esp_heap_caps.h>
#include <esp_wifi.h>
#include <esp_netif.h>                 // esp_netif_dhcps_option / _stop / _start
#include "dhcpserver/dhcpserver.h"     // dhcps_offer_t, OFFER_ROUTER
#include "WCB_ChanSync.h"              // JOIN follows its network across channels (docs/CHANNEL_SYNC.md §5)

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
char        wcbWifiToken[WCB_WIFI_TOKEN_MAX + 1] = "";

// ---- Runtime state --------------------------------------------------------
static bool          wifiUp          = false;   // an interface is up
static bool          joinSettled     = false;   // JOIN reached a terminal state
static unsigned long joinNextAttempt = 0;
static uint16_t      joinAttempts    = 0;
static bool          joinScanning    = false;   // a scan of the mesh channel for the SSID is running
static unsigned long joinAssocMs     = 0;       // when an association still waiting for its address began (0: none)
static unsigned long lastChannelWarn = 0;

// Retry cadence for JOIN. Deliberately unbounded in total: on a shared power
// switch this board is ready long before a NaviCore raises its SoftAP (that
// board mounts LittleFS, loads config and allocates a multi-megabyte PSRAM
// buffer first), so a bounded window would lose the race on every cold boot.
// Retrying costs nothing — see the note on the mesh-channel scan above wcbWifiJoinScan().
static const unsigned long JOIN_RETRY_MS   = 5000;
// How often to re-check that the radio is still on the mesh channel, and how
// often we are willing to complain about it.
static const unsigned long CHAN_CHECK_MS   = 30000;
// An association that has not been given an address in this long is dropped and
// the join starts over, rather than sitting associated and unreachable for good.
static const unsigned long JOIN_ADDR_MS    = 20000;

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
    wcbWifiToken[0] = '\0';                                        // a missing key leaves the buffer untouched
    if (preferences.isKey("token")) preferences.getString("token", wcbWifiToken, sizeof(wcbWifiToken));
    preferences.end();
    if (wcbWifiMode > WCB_WIFI_JOIN) wcbWifiMode = WCB_WIFI_OFF;   // corrupt/never-set
    wcbWsSetToken(wcbWifiToken);
}

// This boot refused a JOIN for want of a token (wcbWifiStart): ?WIFI,TOKEN then says a reboot starts it.
static bool joinNoToken = false;

// Only ?WIFI,TOKEN writes the key, alone: a save of the other settings never touches it. False when NVS refused it.
static bool saveWifiToken() {
    preferences.begin("wifi_cfg", false);
    const bool ok = wcbWifiToken[0] ? preferences.putString("token", wcbWifiToken) == strlen(wcbWifiToken)
                                    : (!preferences.isKey("token") || preferences.remove("token"));
    preferences.end();
    return ok;
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
//  Channel sync: JOIN follows its network (state; the functions are at the end)
// ════════════════════════════════════════════════════════════════════════════
// With channel sync on, a JOIN board that cannot hear its network on the mesh channel also looks on every channel:
// at once (right after the mesh-channel scan misses it), then 1, 2, 4, 8, 16 and 30 minutes after the network went
// missing, then every 30 minutes. A network found elsewhere takes the mesh with it (chanSyncJoinFollow). The
// mesh-channel scan every 5 s is unchanged, and still the only thing that ever calls WiFi.begin without a move.
static volatile bool csFullScanning = false;   // a full-band scan is running: the radio is off the mesh channel
static uint8_t       csFullIdx      = 0;       // full-band scans since the network went missing
static unsigned long csFullEpochMs  = 0;       // when it went missing (boot, or the association lost)
static unsigned long csFullLastMs   = 0;
static bool          joinScanMissed = false;   // the mesh-channel scan just finished without hearing it
static unsigned long joinBeginMs    = 0;       // when WiFi.begin last went out (a connect in progress); 0 = none
static uint8_t       csFollowCh     = 0;       // a MOVE to the network's channel is under way; join there once on it
static uint8_t       csFollowBssid[6];
static uint16_t      csStayNote     = 0;       // the "staying" line last printed (reason << 8 | channel): say it once
static void csJoinLost(unsigned long now);
static bool csFullScanDue(unsigned long now);
static void csFullScanStart(unsigned long now);
static void csFullScanDone(unsigned long now);
static bool csFollow(unsigned long now);

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

    // CHANNEL IS THE THIRD PARAMETER AND DEFAULTS TO 1. Pass meshChannel
    // explicitly. Once an AP owns the radio, nothing later moves it — the
    // esp_wifi_set_channel() call in setup() cannot override an AP — so a
    // defaulted channel against a mesh on any other channel is a silent, total
    // blackout. max_connection 4: this is a config channel, not a hotspot.
    if (WiFi.softAP(ssid.c_str(), wcbWifiApPass.c_str(), meshChannel, /*hidden=*/0, /*max_conn=*/4)) {
        wifiUp = true;

        // NO DEFAULT GATEWAY. This AP is a local management link with no upstream,
        // so it must not advertise itself as the router (DHCP option 3). If it does,
        // a laptop with a second adapter gets two competing default routes and may
        // send internet traffic here to die, and a phone may decide the network is
        // broken. Without it, clients reach 192.168.4.1 on-link and keep their real
        // default route for everything else.
        //
        // The option is only settable while the DHCP server is STOPPED, so this is
        // stop -> set -> start: the same sequence the core's own
        // APClass::enableDhcpCaptivePortal() uses once the AP is up.
        //
        // THE RESTART IS UNCONDITIONAL. A stopped DHCP server is precisely the dead
        // AP that softAPConfig() produced — clients associate, get no lease, and land
        // on 169.254.x with nothing pointing at the cause. So once stopped, it is
        // restarted whatever the set returned. And if the current mask cannot even be
        // read, the server is never touched at all: stock behaviour beats a guess.
        {
            esp_netif_t  *apNetif = WiFi.AP.netif();
            dhcps_offer_t offer   = 0;
            if (!apNetif || !WiFi.AP.started()) {
                Serial.println("[WIFI] AP netif not ready — leaving the DHCP gateway option at its default.");
            } else if (esp_netif_dhcps_option(apNetif, ESP_NETIF_OP_GET, ESP_NETIF_ROUTER_SOLICITATION_ADDRESS,
                                              &offer, sizeof(offer)) != ESP_OK) {
                Serial.println("[WIFI] could not read the DHCP offer mask — leaving the gateway option at its default.");
            } else {
                offer &= (dhcps_offer_t)~OFFER_ROUTER;   // clear ONLY the router bit
                esp_netif_dhcps_stop(apNetif);           // ALREADY_STOPPED is harmless
                const esp_err_t setErr   = esp_netif_dhcps_option(apNetif, ESP_NETIF_OP_SET,
                                               ESP_NETIF_ROUTER_SOLICITATION_ADDRESS, &offer, sizeof(offer));
                const esp_err_t startErr = esp_netif_dhcps_start(apNetif);
                if (startErr != ESP_OK && startErr != ESP_ERR_ESP_NETIF_DHCP_ALREADY_STARTED)
                    Serial.printf("[WIFI] *** DHCP server FAILED to restart (0x%x) — clients will get NO address. ***\n",
                                  (unsigned)startErr);
                else if (setErr != ESP_OK)
                    Serial.printf("[WIFI] could not clear the DHCP gateway option (0x%x) — clients will be offered a router.\n",
                                  (unsigned)setErr);
            }
        }

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
        // ws://…/ws, not http://…/ — the only URI ever registered is the WebSocket
        // one (wcbWsBegin). Printing http:// sent operators to a 404 and reads as
        // "the AP is broken" when it is working perfectly.
        Serial.printf("[WIFI] SoftAP \"%s\" up on channel %u — ws://%s/ws\n",
                      ssid.c_str(), meshChannel, WiFi.softAPIP().toString().c_str());
        Serial.println("[WIFI] ESP-NOW shares this channel (WIFI_AP_STA).");
    } else {
        Serial.printf("[WIFI] SoftAP \"%s\" FAILED to start on channel %u.\n",
                      ssid.c_str(), meshChannel);
    }
}

// One association attempt. Non-blocking — the result is picked up by
// wcbWifiService().
// STAY ON THE MESH CHANNEL. A connect scans for its AP, and for the length of a
// sweep this board is off the mesh — which does not present as "WiFi failed", it
// presents as the board randomly dropping mesh traffic. WiFi.begin's channel does
// NOT confine that scan: it is only where the scan starts ("Channel hint ... scan
// starting from the specified channel", wifi_sta_config_t.channel in
// esp_wifi_types_generic.h), so with the AP absent every attempt swept the band
// (tracker #103: ?WIFI caught the radio on channels 12 and 14 while it looked, and
// unicasts to a peer needed 26 retries). So each attempt first scans the mesh
// channel ALONE for the SSID, without blocking, and only an AP heard there gets a
// WiFi.begin — with its BSSID, so the driver's own scan finds it on the first
// channel it tries. Auto-reconnect stays OFF so a vanished AP cannot start sweeps
// of its own behind our back.
static void wcbWifiJoinScan() {
    joinAttempts++;
    // async, hidden SSIDs too (the probe names it), active, 120 ms, the mesh channel, this SSID.
    // A scan refused while a connect is still in progress is simply the next attempt's job.
    joinScanning = WiFi.scanNetworks(true, true, false, 120, meshChannel, wcbWifiJoinSsid.c_str())
                   == WIFI_SCAN_RUNNING;
}

// The scan's answer -> WiFi.begin on the mesh channel if the SSID was heard there.
// Returns true while the scan is still running.
static bool wcbWifiJoinScanDone() {
    const int16_t n = WiFi.scanComplete();
    if (n == WIFI_SCAN_RUNNING) return true;
    joinScanning = false;
    uint8_t bssid[6];
    bool heard = false;
    for (int16_t i = 0; i < n && !heard; i++) {
        if (WiFi.channel(i) == meshChannel && WiFi.SSID(i) == wcbWifiJoinSsid) {
            memcpy(bssid, WiFi.BSSID(i), sizeof(bssid));
            heard = true;
        }
    }
    WiFi.scanDelete();
    if (heard) {
        WiFi.setAutoReconnect(false);
        WiFi.begin(wcbWifiJoinSsid.c_str(), wcbWifiJoinPass.c_str(), meshChannel, bssid);
        joinBeginMs = millis() | 1;      // a connect in progress: no search hop meanwhile (wcbWifiCanHop)
    }
    joinScanMissed = !heard;             // channel sync: a full-band scan may be due (csFullScanDue)
    return false;
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
    // NO TOKEN, NO JOIN (docs/WIFI_DESIGN.md §7). On someone else's network the WebSocket endpoint is a command channel
    // to the whole mesh for every device on that network; an AP of our own at least has its WPA2 password. JOIN shipped
    // in 7.0.1 without a token, so a board updated from it lands here: every boot says so, with the fix.
    if (!wcbWifiToken[0]) {
        Serial.println("[WIFI] ****************************************************************");
        Serial.println("[WIFI] JOIN REFUSED: no WebSocket token is set, so WiFi stays OFF.");
        Serial.println("[WIFI] Joining a network opens this board's command channel to every");
        Serial.println("[WIFI] device on it. 7.0.1 joined without a token; this firmware will not.");
        Serial.println("[WIFI] Fix:  ?WIFI,TOKEN,<token>   then reboot");
        Serial.println("[WIFI]       (8-32 characters, none of: space , ^ ; ? \" \\)");
        Serial.println("[WIFI] Clients then sign in with ?AUTH,<token> (Intellex asks for it).");
        Serial.println("[WIFI] ****************************************************************");
        joinSettled = true;
        joinNoToken = true;
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
    // joinSettled means "stop trying FOREVER", and only the off-mesh case sets it:
    // that one needs an operator to fix one end, so retrying would just thrash.
    // A NORMAL disconnect must NOT settle — the AP rebooting, or the droid driving
    // out of range, has to heal itself. Auto-reconnect is off (it would sweep the
    // band), so if this loop stops retrying nothing else will, and the board is off
    // the network until someone power-cycles it.
    if (wcbWifiMode == WCB_WIFI_JOIN && !joinSettled) {
        // ASK THE DRIVER, NOT WiFi.status(). The core keeps that status from its
        // event task, and it can be wrong both ways: its WIFI_REASON_AUTH_EXPIRE
        // branch leaves WL_CONNECTED standing after the AP has gone, and on the
        // first disconnect after every boot it reconnects once whatever
        // setAutoReconnect says (STA.cpp, first_connect) — a connect scan that
        // sweeps the band. Reading the RADIO's channel mid-sweep, the guard below
        // took a lost AP for an association on channel 2 and settled for good: no
        // "lost" line and no rejoin (tracker #103). esp_wifi_sta_get_ap_info
        // answers from the driver, and its record names the AP's own channel.
        wifi_ap_record_t ap;
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
            // VERIFY WHERE THE ASSOCIATION ACTUALLY LEFT US. An AP owns the
            // channel once associated. A NaviCore raises its SoftAP on its own
            // mesh channel, so joining it is a no-op channel-wise — but if we
            // land anywhere else we are off the mesh, and being deaf is worse
            // than having no WiFi. Hang up rather than sit there silently.
            // ?DEBUG,FAULT,JOINCHAN: this check reads the AP one channel off the mesh's, once (WCB_Fault.h).
            const uint8_t ch = faultJoinChan.take() ? (uint8_t)(meshChannel % 11 + 1) : ap.primary;
            if (ch != meshChannel) {
                Serial.printf("[WIFI] joined \"%s\" but its AP is on channel %u, not %u — "
                              "that is OFF-MESH. Disconnecting.\n",
                              wcbWifiJoinSsid.c_str(), ch, meshChannel);
                // NOT disconnect(true). The bool is `wifioff`, and with the radio in
                // STA-only mode it runs STA.end() -> WIFI_MODE_NULL, which stops WiFi
                // outright and takes ESP-NOW with it: no peers, no heartbeats, no WDP,
                // until a power cycle. This guard exists to keep the board ON the mesh;
                // passing true made it the one thing that reliably killed it.
                WiFi.disconnect();
                wifiUp      = false;
                if (chanSyncEnabled() && etmEnabled) {
                    // Channel sync: the full-band scan finds the network where it went and takes the mesh there.
                    Serial.println("[WIFI] Looking for it on every channel - channel sync moves the mesh to it.");
                    csJoinLost(now);
                } else {
                    Serial.println("[WIFI] Move that AP to the mesh channel, or change ?WCBCH to match it.");
                    joinSettled = true;  // do not thrash: the operator must fix one end
                }
                return;
            }
            if (!wifiUp) {
                // ?DEBUG,FAULT,JOINNOIP hides the address until the arm below has fired.
                if (WiFi.STA.hasIP() && !faultJoinNoIp.active()) {  // edge — announce once, not every pass
                    wifiUp = true;
                    joinAssocMs = 0;
                    Serial.printf("[WIFI] joined \"%s\" on channel %u after %u attempt(s) — ws://%s/ws\n",
                                  wcbWifiJoinSsid.c_str(), ch, joinAttempts,
                                  WiFi.localIP().toString().c_str());
                } else if (!joinAssocMs) {
                    joinAssocMs = now | 1;   // associated; the address is on its way
                } else if (now - joinAssocMs >= JOIN_ADDR_MS) {
                    Serial.printf("[WIFI] associated with \"%s\" but no address after %lu s — starting over\n",
                                  wcbWifiJoinSsid.c_str(), (unsigned long)(JOIN_ADDR_MS / 1000));
                    WiFi.disconnect();
                    joinAssocMs = 0;
                    faultJoinNoIp.arm(false);   // the knob has done its job: the next association is real
                }
            }
            return;                      // associated: nothing to retry
        }
        joinAssocMs = 0;

        // Not associated. If we WERE up, the association just dropped: say so,
        // clear the status so ?WIFI stops claiming "up" with no address, cancel
        // whatever reconnect the core started on its own (its connect scan sweeps
        // the band; ours below stays on the mesh channel), and fall through to the
        // retry below rather than latching. WiFi.disconnect(), NOT disconnect(true):
        // see the off-mesh guard above.
        if (wifiUp) {
            wifiUp = false;
            WiFi.disconnect();
            Serial.printf("[WIFI] lost \"%s\" — retrying every %lu s\n",
                          wcbWifiJoinSsid.c_str(), (unsigned long)(JOIN_RETRY_MS / 1000));
            csJoinLost(now);   // channel sync: the network may have changed channel - look on every one again
        }
        if (csFullScanning) { csFullScanDone(now); return; }   // channel sync: a full-band scan is running
        if (joinScanning && wcbWifiJoinScanDone()) return;   // still scanning the mesh channel
        if (csFollow(now)) return;          // channel sync: the mesh is moving to the network's channel, or just did
        if (chanSyncBusy()) return;         // a search or a move has the radio: no scan of ours meanwhile
        if (joinScanMissed) {               // the mesh-channel scan did not hear it: a full-band scan, if one is due
            joinScanMissed = false;
            if (csFullScanDue(now)) { csFullScanStart(now); return; }
        }
        if (now >= joinNextAttempt) {
            wcbWifiJoinScan();
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
    // ?DEBUG,FAULT,CHANDRIFT (WCB_Fault.h): the radio really moves off the mesh channel for this one check, and back
    // right after it, so the check below runs on a drifted radio. Only reached with an interface up outside a pending
    // JOIN (the JOIN branch above returns first): in practice, AP mode.
    bool driftFault = false;
    if (wifiUp && faultChanDrift.take()) {
        const uint8_t off = (uint8_t)(meshChannel % 11 + 1);
        driftFault = esp_wifi_set_channel(off, WIFI_SECOND_CHAN_NONE) == ESP_OK;
        Serial.printf("[FAULT] CHANDRIFT: %s channel %u for one drift check\n",
                      driftFault ? "radio moved to" : "the radio refused to move to", off);
        if (driftFault) lastChannelWarn = now - CHAN_CHECK_MS;
    }
    // Not while channel sync has the radio: a search visits the other channels on purpose (WCB_ChanSync.h).
    if (wifiUp && !chanSyncBusy() && (now - lastChannelWarn) >= CHAN_CHECK_MS) {
        lastChannelWarn = now;
        const uint8_t ch = wcbWifiRadioChannel();
        if (ch != 0 && ch != meshChannel)
            Serial.printf("[WIFI] WARNING: radio is on channel %u but the mesh is on %u — "
                          "this board cannot reach the mesh.\n", ch, meshChannel);
    }
    if (driftFault) {
        esp_wifi_set_channel(meshChannel, WIFI_SECOND_CHAN_NONE);
        Serial.printf("[FAULT] CHANDRIFT: radio back on channel %u\n", wcbWifiRadioChannel());
    }
}

// ?DEBUG,FAULT,JOINNOIP (WCB.ino debugFaultCommand): a joined JOIN board drops its association now, so the knob bites
// the very next one. WiFi.disconnect(), not disconnect(true): see the off-mesh guard.
void wcbWifiFaultRejoin() {
    if (wcbWifiMode != WCB_WIFI_JOIN || joinSettled) return;
    WiFi.disconnect();
    wifiUp      = false;
    joinAssocMs = 0;
}

// ════════════════════════════════════════════════════════════════════════════
//  Channel sync (WCB_ChanSync.h, docs/CHANNEL_SYNC.md)
// ════════════════════════════════════════════════════════════════════════════
// Full-band scans: minutes after the network went missing, then every 30 minutes (docs/CHANNEL_SYNC.md §5).
static const uint8_t       CS_FULL_SCAN_MIN[] = { 0, 1, 2, 4, 8, 16, 30 };
static const uint8_t       CS_FULL_SCAN_N     = sizeof(CS_FULL_SCAN_MIN) / sizeof(CS_FULL_SCAN_MIN[0]);
static const unsigned long CS_FULL_SCAN_EVERY = 30UL * 60000UL;

// The network went missing (boot is the first time): the full-band schedule starts over, from a scan at once.
static void csJoinLost(unsigned long now) {
    csFullEpochMs = now;
    csFullIdx     = 0;
    csStayNote    = 0;
}

static bool csFullScanDue(unsigned long now) {
    if (!chanSyncEnabled() || !etmEnabled || csFollowCh) return false;   // sync off: the mesh channel only, as before
    const unsigned long due = csFullIdx < CS_FULL_SCAN_N
        ? csFullEpochMs + (unsigned long)CS_FULL_SCAN_MIN[csFullIdx] * 60000UL
        : csFullLastMs + CS_FULL_SCAN_EVERY;
    return (long)(now - due) >= 0;
}

// Every channel the region allows, 120 ms each and the same SSID filter as the mesh-channel scan: the board is off
// the mesh for about 1.5 s, which chanSyncRadioAway() reports so the receive path and loop() hold off meanwhile.
static void csFullScanStart(unsigned long now) {
    csFullScanning = true;               // first: the radio leaves the moment the scan starts
    csFullScanning = WiFi.scanNetworks(true, true, false, 120, 0, wcbWifiJoinSsid.c_str()) == WIFI_SCAN_RUNNING;
    if (!csFullScanning) return;         // refused (a connect still settling): the next miss tries again
    csFullLastMs = now;
    if (csFullIdx < 255) csFullIdx++;
    Serial.printf("[WIFI] \"%s\" is not on channel %u - looking on every channel\n",
                  wcbWifiJoinSsid.c_str(), meshChannel);
}

static void csFullScanDone(unsigned long now) {
    const int16_t n = WiFi.scanComplete();
    if (n == WIFI_SCAN_RUNNING) return;
    // The mesh-channel scan never leaves the mesh channel; this one does, so the radio is put back explicitly before
    // anything else is sent or heard, rather than trusting the driver to return an unassociated station there. The
    // home channel, not meshChannel: a ?WCBCH typed since boot is saved for the next one, not applied (WCB_ChanSync.h).
    esp_wifi_set_channel(chanSyncHomeChannel(), WIFI_SECOND_CHAN_NONE);
    csFullScanning = false;
    int16_t best = -1;
    for (int16_t i = 0; i < n; i++) {
        if (WiFi.SSID(i) != wcbWifiJoinSsid) continue;
        if (WiFi.channel(i) == meshChannel) { best = i; break; }   // on the mesh channel after all: just join it
        if (best < 0 || WiFi.RSSI(i) > WiFi.RSSI(best)) best = i;
    }
    uint8_t ch = 0, bssid[6];
    if (best >= 0) {
        ch = (uint8_t)WiFi.channel(best);
        memcpy(bssid, WiFi.BSSID(best), sizeof(bssid));
    }
    WiFi.scanDelete();
    if (best < 0) return;                // nowhere: the 5 s mesh-channel scans go on, and the next full scan
    if (ch == meshChannel) {
        WiFi.setAutoReconnect(false);
        WiFi.begin(wcbWifiJoinSsid.c_str(), wcbWifiJoinPass.c_str(), meshChannel, bssid);
        joinBeginMs = now | 1;
        return;
    }
    uint8_t by = 0;
    const ChanSyncJoinVerdict v = chanSyncJoinFollow(ch, &by);
    if (v == CSJ_GO) {
        csFollowCh = ch;
        memcpy(csFollowBssid, bssid, sizeof(csFollowBssid));
        csStayNote = 0;
        Serial.printf("[WIFI] found \"%s\" on channel %u - the mesh moves there, then this board joins it\n",
                      wcbWifiJoinSsid.c_str(), ch);
        return;
    }
    if (v == CSJ_BUSY) {                 // a search or a move: decide again after the next miss
        if (csFullIdx) csFullIdx--;
        return;
    }
    const uint16_t note = (uint16_t)(((uint16_t)v << 8) | ch);
    if (note == csStayNote) return;      // said once already
    csStayNote = note;
    if (v == CSJ_PINNED_OUTRANKED)
        Serial.printf("[WIFI] found \"%s\" on channel %u, but WCB%u is joined to a network on this channel and "
                      "outranks this board - staying on %u. Two networks on two channels cannot both be kept.\n",
                      wcbWifiJoinSsid.c_str(), ch, by, meshChannel);
    else if (v == CSJ_OUT_OF_REGION)
        Serial.printf("[WIFI] found \"%s\" on channel %u, outside this board's region (channels 1-%u) - staying on "
                      "%u. ?REGION,<cc> on every board allows it (next boot), or move the access point.\n",
                      wcbWifiJoinSsid.c_str(), ch, chanSyncMaxChannel(), meshChannel);
    else
        Serial.printf("[WIFI] found \"%s\" on channel %u - channel sync is off, staying on %u\n",
                      wcbWifiJoinSsid.c_str(), ch, meshChannel);
}

// A move to the network's channel (chanSyncJoinFollow) is counting down; once the radio is there, join with the
// BSSID the full scan found, so the driver's connect scan finds it on the first channel it tries (tracker #103).
// Returns true while it has the JOIN state machine.
static bool csFollow(unsigned long now) {
    if (!csFollowCh) return false;
    if (chanSyncBusy()) return true;
    if (meshChannel == csFollowCh) {
        WiFi.setAutoReconnect(false);
        WiFi.begin(wcbWifiJoinSsid.c_str(), wcbWifiJoinPass.c_str(), csFollowCh, csFollowBssid);
        joinBeginMs     = now | 1;
        joinNextAttempt = now + JOIN_RETRY_MS;
        Serial.printf("[WIFI] the mesh is on channel %u - joining \"%s\" there\n", csFollowCh, wcbWifiJoinSsid.c_str());
    }                                    // else the move did not happen: back to looking
    csFollowCh = 0;
    return true;
}

bool wcbWifiJoinAssociated() {
    if (wcbWifiMode != WCB_WIFI_JOIN) return false;
    wifi_ap_record_t ap;                 // the driver's answer, not WiFi.status() (tracker #103)
    return esp_wifi_sta_get_ap_info(&ap) == ESP_OK;
}

bool wcbWifiInterfaceUp() {
    return (wcbWifiMode == WCB_WIFI_AP && wifiUp) || wcbWifiJoinAssociated();
}

bool wcbWifiJoinScanning()    { return joinScanning || csFullScanning; }
bool wcbWifiFullScanRunning() { return csFullScanning; }

bool wcbWifiCanHop(const char **why) {
    const char *w = nullptr;
    if (wcbWifiMode == WCB_WIFI_AP && wifiUp) {
        // IDF: esp_wifi_set_channel must not be called while the soft-AP has stations connected.
        wifi_sta_list_t sta;
        if (esp_wifi_ap_get_sta_list(&sta) != ESP_OK || sta.num > 0)
            w = "a station is connected to this board's access point";
    } else if (wcbWifiMode == WCB_WIFI_JOIN && !joinSettled) {
        if (wcbWifiJoinAssociated())
            w = "this board is joined to its access point";
        else if (joinScanning || csFullScanning)
            w = "a WiFi scan is running";
        else if (joinBeginMs && millis() - joinBeginMs < JOIN_RETRY_MS)
            w = "a WiFi connect is in progress";
        else if (csFollowCh)
            w = "the mesh is moving to this board's network";
    }
    if (why) *why = w;
    return w == nullptr;
}

void wcbWifiJoinCancelConnect() {
    if (wcbWifiMode != WCB_WIFI_JOIN || wcbWifiJoinAssociated()) return;
    if (joinBeginMs) {
        WiFi.disconnect();               // not disconnect(true): see the off-mesh guard in wcbWifiService
        joinBeginMs = 0;
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
    if (WiFi.STA.hasIP()) return WiFi.localIP().toString();   // not WiFi.status(): it can stay stale (#103)
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
        wifi_ap_record_t ap;             // the driver's answer, not WiFi.status() (tracker #103)
        Serial.printf("Association   : %s (%u attempt(s))\n",
                      esp_wifi_sta_get_ap_info(&ap) == ESP_OK ? "connected" : "not connected",
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
    // Whether a token is set, never what it is. "set" / "none" lead the value: the line is read by tools.
    Serial.printf("Token         : %s\n", wcbWifiToken[0] ? "set" :
                  wcbWifiMode == WCB_WIFI_JOIN ? "none - JOIN will not start (?WIFI,TOKEN,<token>)" : "none");
    Serial.printf("Free heap     : %u bytes (min since boot %u)\n",
                  (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),       // byte-addressable only: see ?STATS
                  (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT));
    Serial.println("------------------------------------------------------");
}

static void wcbWifiTokenCommand(const String &arg);   // ?WIFI,TOKEN, below

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

    if (verb == "TOKEN") { wcbWifiTokenCommand((c1 < 0) ? String("") : args.substring(c1 + 1)); return; }

    if (verb != "AP" && verb != "JOIN") {
        Serial.println("Usage: ?WIFI | ?WIFI,OFF | ?WIFI,AP,<ssid>,<pass> | ?WIFI,JOIN,<ssid>,<pass>");
        Serial.println("       ?WIFI,TOKEN,<token> | ?WIFI,TOKEN,CLEAR");
        return;
    }

    const String rest = (c1 < 0) ? String("") : args.substring(c1 + 1);
    const int    c2   = rest.indexOf(',');
    String ssid = (c2 < 0) ? rest : rest.substring(0, c2);
    String pass = (c2 < 0) ? String("") : rest.substring(c2 + 1);
    ssid.trim();
    // Not trimmed here, so a passphrase keeps a LEADING space. It cannot END in one: every line reader trims the
    // whole line before this (the serial readers in WCB.ino, WCB_WS.cpp), and exempting ?WIFI there would make a
    // terminal's stray trailing space part of the credential instead (WCB coverage re-scan #31).

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
    // Saved anyway, so a config push or a restore that sets the token after this line still ends up whole; the boot
    // is what refuses (wcbWifiStart).
    if (wcbWifiMode == WCB_WIFI_JOIN && !wcbWifiToken[0]) {
        Serial.println("WARNING: no WebSocket token is set, and JOIN will not start at the next boot");
        Serial.println("without one: joining a network opens this board's command channel to every");
        Serial.println("device on it. Set one before rebooting:  ?WIFI,TOKEN,<token>");
    }
}

// ---- ?WIFI,TOKEN ---------------------------------------------------------
// The characters the config chain and the line grammar give meaning to cannot be in a token: it travels as
// ?WIFI,TOKEN,<token> in ?backup and a config pull (split at ',' and '^') and as ?AUTH,<token> on the socket.
static const char *wcbWifiTokenProblem(const String &t) {
    if (t.length() < WCB_WIFI_TOKEN_MIN || t.length() > WCB_WIFI_TOKEN_MAX) return "must be 8-32 characters";
    for (size_t i = 0; i < t.length(); i++) {
        const char ch = t[i];
        if (ch < 0x21 || ch > 0x7E) return "takes printable ASCII only, and no space";
        if (strchr(",^;?\"\\", ch)) return "cannot contain any of , ^ ; ? \" \\";
    }
    return nullptr;
}

// Takes effect for sessions opened from now on: one already signed in, the one sending this over the socket
// included, stays signed in (wcbWsSetToken). The value is never printed, here or in ?WIFI.
static void wcbWifiTokenCommand(const String &arg) {
    if (arg.length() == 0) {
        Serial.println("Usage: ?WIFI,TOKEN,<token> | ?WIFI,TOKEN,CLEAR");
        return;
    }
    char was[sizeof(wcbWifiToken)];
    memcpy(was, wcbWifiToken, sizeof(was));
    if (arg.equalsIgnoreCase("CLEAR")) {
        wcbWifiToken[0] = '\0';
    } else {
        const char *why = wcbWifiTokenProblem(arg);
        if (why) {
            Serial.printf("Token refused: a token %s. Nothing changed.\n", why);
            return;
        }
        strlcpy(wcbWifiToken, arg.c_str(), sizeof(wcbWifiToken));
    }
    if (!saveWifiToken()) {
        memcpy(wcbWifiToken, was, sizeof(wcbWifiToken));   // RAM keeps what NVS still holds
        Serial.println("Token NOT saved: NVS refused it (full?). Nothing changed. See ?NVS.");
        return;
    }
    wcbWsSetToken(wcbWifiToken);
    if (wcbWifiToken[0]) {
        Serial.println("WebSocket token set. Sessions opened from now on must send ?AUTH,<token>");
        Serial.println("before anything they send runs; sessions already signed in stay signed in.");
        if (joinNoToken && wcbWifiMode == WCB_WIFI_JOIN)
            Serial.println("JOIN was refused at this boot for want of a token: reboot to start it.");
    } else {
        Serial.println("WebSocket token cleared. Sessions opened from now on need no sign-in.");
        if (wcbWifiMode == WCB_WIFI_JOIN)
            Serial.println("WARNING: this board is set to JOIN, which will not start at the next boot without a token.");
    }
}
