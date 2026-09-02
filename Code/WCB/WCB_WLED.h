#ifndef WCB_WLED_H
#define WCB_WLED_H

#include <Arduino.h>

// -----------------------------------------------------------------------
// WLED serial control for the WCB — ID-ADDRESSED (mirrors the Maestro model)
//
//   Drive stock WLED nodes over reserved local UARTs using WLED's JSON state
//   API (no custom WLED firmware). Author the looks in WLED's web UI; the WCB
//   fires presets / power / brightness from sequences + the mesh.
//
//   Every WLED in the system has a unique ID (1-9). A WCB may host several WLED
//   nodes (one per serial port) and/or address WLED nodes physically wired to
//   OTHER boards — a ;L<id> command routes to whichever board hosts that ID over
//   ESP-NOW, exactly like ;M<id> for Maestros. wledConfigs[] holds both LOCAL
//   entries (serialPort 1-5, remoteWCB 0) and REMOTE proxies (serialPort 0,
//   remoteWCB = host). Remote proxies are auto-added from WDP adverts.
//
//   Wiring: WCB TX -> WLED RX, common ground. WLED listens for JSON on its UART
//   at 115200 by default — use a build whose LED data pin is NOT on the UART
//   pins. Fire-and-forget: the WCB does not read WLED back, so only TX is wired.
//
//   Configure : ?WLED,<id>:W<wcb>S<port>:<baud>   (e.g. ?WLED,1:W2S1:115200)
//               ?WLED,PORT,S<port>:<baud>         (legacy — becomes WLED 1, local)
//               ?WLED,CLEAR          clear ALL     ?WLED,CLEAR,<id>  clear one
//               ?WLED,LIST | ?WLED,STATUS
//   Operate   : ;L<id>,VERB           -> the WLED with that ID (local or routed)
//               ;L,VERB               -> this board's own (lowest-id local) WLED
//               VERB = ON | OFF | TOGGLE
//                      BRI,<0-255>              -> master brightness
//                      PS,<n>                   -> recall preset n  (primary workflow)
//                      COL,<RRGGBB[WW]>         -> solid colour (segment 0)
//                      FX,<n>[,<speed>,<int>]   -> effect by index  (+ optional sx/ix)
//                      PAL,<n>                  -> palette by index
//                      JSON,{...}               -> raw /json/state passthrough (escape hatch)
//
//   Convention mirrors Maestro/MP3/HCR:
//     ?WLED,...  configures & queries (LocalFunctionIdentifier, NVS-persisted)
//     ;L,...     runtime actions      (CommandCharacter, routed/broadcastable)
// -----------------------------------------------------------------------

struct WLEDConfig {
  uint8_t  wledID;       // 1-9, system-wide WLED identifier
  uint8_t  serialPort;   // 1-5 local UART; 0 = remote proxy
  uint8_t  remoteWCB;    // 0 = local (this board); else the WCB that hosts this WLED
  bool     configured;
  uint32_t baudRate;     // default 115200 (WLED serial default)
};

#define MAX_WLED_PER_WCB 9   // track all WLED in the system (local + remote proxies)

extern WLEDConfig wledConfigs[MAX_WLED_PER_WCB];

// ---- Runtime (;L[id],...) ----------------------------------------------
void processWLEDRuntimeCommand(const String &message);   // dispatch ;L[id],... actions

// ---- Configuration / query (?WLED,...) ---------------------------------
void configureWLED(const String &args);
void clearWLEDConfig();                    // ?WLED,CLEAR — clear ALL
void clearWLEDByID(const String &idArg);   // ?WLED,CLEAR,<id> — clear one
void printWLEDSettings();    // ?WLED,LIST
void printWLEDStatus();      // ?WLED,STATUS  ->  [WLED:...]
// defSep/defFunc mirror printHCRBackup: the FACTORY chain uses these; the live
// chain uses the running delimiter/func so a custom-funcChar restore still works.
void printWLEDBackup(String &chainedConfig, String &chainedConfigDefault,
                     char delimiter, bool printToSerial = false,
                     const String &defSep = "^", const String &defFunc = "?");

// ---- Slot helpers ------------------------------------------------------
// A WLED ID is ONE physical device: exactly one slot per wledID (unlike Maestro,
// where the same ID may fan out to several ports). Config/auto-add key on the ID.
int8_t findWLEDSlotByID(uint8_t wledID);
int8_t findEmptyWLEDSlot();
bool   isWLEDConfigured(uint8_t wledID);

// ---- WDP auto-config: adopt a REMOTE WLED heard on an advert ------------
// slot = {id, serialPort:0, remoteWCB:hostWCB, baud}. First-host-wins +
// idempotent + persisted. Returns true if a slot was added or its baud changed.
bool wledAutoAddRemote(uint8_t wledID, uint8_t hostWCB, uint32_t baud);

// ---- Port-conflict query (serial task & sibling modules) ---------------
bool isSerialPortUsedForWLED(int port);

// ---- Lifecycle / NVS ----------------------------------------------------
void beginWLED();            // announce configured LOCAL WLED ports
void saveWLEDSettings();
void loadWLEDSettings();     // loads the array + migrates a legacy singular config

#endif
