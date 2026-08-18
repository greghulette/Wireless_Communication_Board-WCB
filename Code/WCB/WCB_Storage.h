#include <sys/_types.h>
#ifndef WCB_STORAGE_H
#define WCB_STORAGE_H

#include <Arduino.h>
#include <Preferences.h>
#include "command_timer_queue.h"

// =============== WCB System Constants ===============
// Guard allows WCB.ino to also define these without conflict
#ifndef MAX_WCB_COUNT
#define MAX_WCB_COUNT        20
#define WCB_TARGET_BROADCAST  0
#define WCB_TARGET_RAW_SERIAL 97
#define WCB_TARGET_KYBER      98
#endif

// ESP-NOW mesh channel (1–11). The whole mesh AND every WCB_Client device must
// share this channel — the ESP32 has one radio, so a board on a different channel
// is silently unreachable. 1–11 only: 12–13 need a WiFi-country override the firmware
// doesn't set (and aren't US-legal), so esp_wifi_set_channel would silently reject them.
// Default 1; runtime-settable via ?WCBCH / the Wizard and persisted in NVS (wcb_config).
// Own guard so both WCB.ino and WCB_Storage.cpp see it.
#ifndef WCB_MESH_CHANNEL_DEFAULT
#define WCB_MESH_CHANNEL_DEFAULT 1
#endif

// =============== Global Variables ===============
extern Preferences preferences;

// These are declared elsewhere in the main code, but we need them here as externs
extern uint8_t umac_oct2;
extern uint8_t umac_oct3;
extern int WCB_Number;
extern uint8_t meshChannel;       // ESP-NOW mesh channel (1–11); loaded from NVS, default WCB_MESH_CHANNEL_DEFAULT
extern String wcb_alias;          // Per-WCB friendly name (e.g. "Body"); ≤24 chars; "" = unset
extern int Default_WCB_Quantity;
extern bool specialPeerEnabled;
extern uint8_t WCB_SPECIAL_PEER_ID;   // special peer ID (NaviCore), default 20, runtime-configurable
extern char espnowPassword[40];
extern bool debugEnabled;
extern bool serialBroadcastEnabled[5];
extern bool broadcastToS0;   // echo broadcast output to S0/USB (opt-in, persisted)
extern unsigned long baudRates[5];
extern char CommandCharacter;
extern char commandDelimiter;
extern char LocalFunctionIdentifier;
extern int wcb_hw_version;
extern bool Maestro_Remote;
extern bool Kyber_Local;
extern int  kyberLocalPort;   // serial port Kyber is physically on (0 = not configured)
extern String Kyber_Location;
extern String serialPortLabels[5];
String getSerialLabel(int port);
extern bool blockBroadcastFrom[5];
extern bool serialMonitorEnabled[5];
extern bool etmChecksumEnabled;

// ETM Settings
extern bool etmEnabled;
extern int etmBootHeartbeatSec;
extern int etmHeartbeatSec;
extern int etmMissedHeartbeats;
extern int etmTimeoutMs;
extern int etmCharMessageCount;
extern int etmCharDelayMs;

// For stored commands
#define MAX_STORED_COMMANDS 80
#define MAX_SERIAL_MONITOR_MAPPINGS 5  // One per input port

extern String storedCommands[MAX_STORED_COMMANDS];

extern void enqueueCommand(const String &cmd, int sourceID);  // Declare it as an external function
extern void parseCommandsAndEnqueue(const String &data, int sourceID);

struct SerialMonitorOutput {
    uint8_t wcbNumber;    // 0 = local, 1-9 = remote WCB
    uint8_t serialPort;   // 0 = USB, 1-5 = Serial ports
};

// struct SerialMonitorMapping {
//     bool active;
//     uint8_t inputPort;    // 1-5 (which local serial port to monitor)
//     uint8_t outputCount;
//     SerialMonitorOutput outputs[10];  // Up to 10 output destinations
// };
struct SerialMonitorMapping {
    bool active;
    uint8_t inputPort;
    uint8_t outputCount;
    SerialMonitorOutput outputs[10];
    bool rawMode;  // ← ADD THIS
};

// Kyber target structure
struct KyberTarget {
  uint8_t maestroID;    // Which Maestro (for reference/documentation)
  uint8_t targetWCB;    // Which WCB to send to (0 = broadcast to all)
  uint8_t targetPort;   // Which serial port on that WCB (0 = S1 default)
  bool enabled;         // Is this target active
};

#define MAX_KYBER_TARGETS 9  // Can target up to 9 Maestros

extern KyberTarget kyberTargets[MAX_KYBER_TARGETS];
extern bool kyberUseTargeting;  // True = use targets, False = broadcast to all S1

extern SerialMonitorMapping serialMonitorMappings[MAX_SERIAL_MONITOR_MAPPINGS];

// =============== Function Declarations ===============
void saveHWversion(int wcb_hw_version_f);
void loadHWversion();
void printHWversion();
void saveStatusLEDPin(int pin);
void loadStatusLEDPin();
void saveMACPreferences();
void loadMACPreferences();

void loadWCBNumberFromPreferences();
void saveWCBNumberToPreferences(int wcb_number_f);
void loadWCBAlias();
void saveWCBAlias(const String &alias);
void loadWCBQuantitiesFromPreferences();
void saveWCBQuantityPreferences(int quantity);
void loadMeshChannelFromPreferences();
void saveMeshChannelToPreferences(uint8_t channel);
void loadSpecialPeerPreferences();
void saveSpecialPeerPreferences(bool enabled);
void loadSpecialPeerIDFromPreferences();
void saveSpecialPeerIDToPreferences(uint8_t id);

void updateBaudRate(int port, int baud);
void loadBaudRatesFromPreferences();
void resetBroadcastSettingsNamespace();
void printBaudRates();
bool isTimerCommand(const String &input);

void recallCommandSlot(const String &key, int sourceID);
// void loadStoredCommandsFromPreferences();
void saveStoredCommandsToPreferences(const String &message);
void listStoredCommands();

// ── Stored-sequence INVENTORY (names only, no values) ──────────────────────
// The full ?SEQ,LIST print and the config-pull chain both carry sequence VALUES,
// which blows past the 16-chunk (2912 char) relay ceiling on a board with a real
// sequence set. A consumer that only needs to know WHAT exists (NaviCore's command
// picker, a web UI) wants names alone — ~16 bytes per entry instead of hundreds.
//
// buildSequenceNamesString() is the single source of truth for that inventory and
// is used by BOTH ?SEQ,NAMES (console) and the SEQ_REQ/SEQ_FRAG mesh request, so
// the two can never disagree. Format:
//
//     <hash8hex>,<count>[,<key1>,<key2>,...]
//
// Comma separation is safe: saveStoredCommandsToPreferences() takes the key as
// everything BEFORE the first comma, so a stored key can never contain one.
// The empty case is "811C9DC5,0" — the FNV offset basis and a zero count, so a
// parser needs no special case for "board has no sequences".
String   buildSequenceNamesString();

// FNV-1a over the NVS key_list AND every stored value. Cheap content fingerprint of
// "what sequences this board has" — NOT security, and order-sensitive (key_list
// preserves save order), which is what we want: it answers "did MY inventory
// change", not "do two boards match".
//
// It covers VALUES, not just names, because editing a sequence in place never
// touches key_list — a keys-only hash would leave every peer holding a stale copy
// while believing it current. That is the whole point of the fingerprint.
//
// Advertised as WDP_TLV_SEQHASH, so a peer re-pulls only when something actually
// changed. CACHED: the WDP dirty-check rebuilds the advert twice a second, and
// hashing values uncached would mean N NVS reads at 2 Hz forever. Every write path
// must call invalidateSequenceInventoryHash().
uint32_t sequenceInventoryHash();
void     invalidateSequenceInventoryHash();
void eraseStoredCommandByName(const String &name);

void clearAllStoredCommands();
void migrateOldStoredCommands();

void storeKyberSettings(const String &message);
void loadKyberSettings();
void printKyberSettings();
void saveKyberTargets();
void loadKyberTargets();
// Add-only reconcile: give every configured Maestro that lacks one an enabled
// kyberTargets[] entry, WITHOUT disturbing existing entries (or their documented
// remote ports). Used when WDP auto-learns a remote Maestro so a Kyber-LOCAL host
// begins forwarding to it with no manual ?KYBER,LOCAL re-issue. Returns # added.
int reconcileKyberTargetsFromMaestroConfigs();
void printKyberList();


void saveBroadcastSettingsToPreferences();
void loadBroadcastSettingsFromPreferences();

void loadLocalFunctionIdentifierAndCommandCharacter();
void saveLocalFunctionIdentifierAndCommandCharacter();

void loadESPNowPasswordFromPreferences();
void setESPNowPassword(const char *password);

void setCommandDelimiter(char delimiter);
void loadCommandDelimiter();

void eraseNVSFlash();

extern bool isSerialPortUsedForPWMInput(int port);
extern bool isSerialPortPWMOutput(int port);
extern bool isSerialPortUsedForMP3(int port);

void loadSerialLabelsFromPreferences();
void saveSerialLabelToPreferences(int port, const String &label);
void clearSerialLabel(int port);

// Serial monitoring
extern bool serialMonitorEnabled[5];
extern bool mirrorToUSB;
extern bool mirrorToKyber;

// Broadcast blocking
extern bool blockBroadcastFrom[5];

void loadSerialMonitorSettings();
void saveSerialMonitorSettings();
void loadBroadcastBlockSettings();
void saveBroadcastBlockSettings();

void addSerialMonitorMapping(const String &message);
void removeSerialMonitorMapping(const String &portStr);
void clearAllSerialMonitorMappings();
void listSerialMonitorMappings();
void saveSerialMonitorMappings();
void loadSerialMonitorMappings();
bool isSerialPortMonitored(int port); 

bool isSerialPortRawMapped(int port);
void setSerialMappingRawMode(int inputPort, bool raw);

// Maestro configuration storage
void saveMaestroSettings();
void loadMaestroSettings();
void normalizeMaestroSelfSlots();   // repair legacy remote-to-self slots (call after WCB_Number is loaded)
void printMaestroSettings();
void saveETMSettings();
void loadETMSettings();

#endif