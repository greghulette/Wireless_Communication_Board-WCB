#ifndef WCB_WLED_H
#define WCB_WLED_H

#include <Arduino.h>

// -----------------------------------------------------------------------
// WLED serial control for the WCB
//
//   Drive a stock WLED node over a reserved local UART using WLED's JSON
//   state API (no custom WLED firmware). Author the looks in WLED's web UI;
//   the WCB fires presets / power / brightness from sequences + the mesh.
//   One WLED per WCB port; reach a WLED on another board by targeting its
//   WCB the usual way (e.g. ;W3,;L,PS,2).
//
//   Wiring: WCB TX -> WLED RX, common ground. WLED listens for JSON on UART0
//   (GPIO1 TX / GPIO3 RX) at 115200 by default — use a build whose LED data
//   pin is NOT on GPIO1/GPIO3 (GPIO2/16 is fine). Fire-and-forget: the WCB
//   does not read WLED back, so only TX needs to be wired.
//
//   Configure : ?WLED,PORT,S<port>:<baud>   ?WLED,CLEAR   ?WLED,LIST|STATUS
//   Operate   : ;L,ON  ;L,OFF  ;L,TOGGLE
//               ;L,BRI,<0-255>            -> master brightness
//               ;L,PS,<n>                 -> recall preset n   (primary workflow)
//               ;L,COL,<RRGGBB[WW]>       -> solid colour (segment 0)
//               ;L,FX,<n>[,<speed>,<int>] -> effect by index  (+ optional sx/ix)
//               ;L,PAL,<n>                -> palette by index
//               ;L,JSON,{...}             -> raw /json/state passthrough (escape hatch)
//
//   Convention mirrors Maestro/MP3/HCR:
//     ?WLED,...  configures & queries (LocalFunctionIdentifier, NVS-persisted)
//     ;L,...     runtime actions      (CommandCharacter, routed/broadcastable)
// -----------------------------------------------------------------------

struct WLEDConfig {
  uint8_t  serialPort;   // 1-5 (local UART port); 0 = not configured
  bool     configured;
  uint32_t baudRate;     // default 115200 (WLED serial default)
};

extern WLEDConfig wledConfig;

// ---- Runtime (;L,...) ---------------------------------------------------
void processWLEDRuntimeCommand(const String &message);   // dispatch ;L,... actions

// ---- Configuration / query (?WLED,...) ---------------------------------
void configureWLED(const String &args);
void clearWLEDConfig();
void printWLEDSettings();    // ?WLED,LIST
void printWLEDStatus();      // ?WLED,STATUS  ->  [WLED:...]
// defSep/defFunc mirror printHCRBackup: the FACTORY chain uses these; the live
// chain uses the running delimiter/func so a custom-funcChar restore still works.
void printWLEDBackup(String &chainedConfig, String &chainedConfigDefault,
                     char delimiter, bool printToSerial = false,
                     const String &defSep = "^", const String &defFunc = "?");

// ---- Port-conflict query (serial task & sibling modules) ---------------
bool isSerialPortUsedForWLED(int port);

// ---- Lifecycle / NVS ----------------------------------------------------
void beginWLED();            // bind the write stream to the configured port
void saveWLEDSettings();
void loadWLEDSettings();

#endif
