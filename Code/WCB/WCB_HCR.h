#ifndef WCB_HCR_H
#define WCB_HCR_H

#include <Arduino.h>

// -----------------------------------------------------------------------
// Human-Cyborg Relations (HCR) Vocalizer integration for the WCB
//
//   One HCR per WCB board, on a reserved local UART. Send + status parsing
//   are handled by the HumanCyborgRelationsAPI (HCRVocalizer) library.
//
//   Configure : ?HCR,PORT,S<port>:<baud>   ?HCR,POLL,<sec|OFF>   ?HCR,CLEAR
//   Query     : ?HCR,LIST   ?HCR,STATUS   ?HCR,GET,<field>   ?HCR,REFRESH
//   Operate   : ;H,STIM,..  ;H,PLAY,..  ;H,VOL,..  ;H,FN,..  ;H,RAW,<str>
//   Volume/fade (WCB-synthesized, non-blocking, per channel):
//               ;H,VOLUP|VOLDN,<V|A|B>[,step]   ;H,FADEIN,<A|B>,<sec>
//               ;H,FADEOUT,<A|B>,<sec>   ;H,PLAY,<A|B>,<file>,FADEIN,<sec>
//     Fades use a WCB volume shadow as the capture/restore point (poll
//     status can be stale). FADEOUT ramps->0, StopWAV, restores volume.
//     Crossfade = FADEOUT one channel while PLAY..FADEIN the other.
//
//   Convention mirrors Maestro/MP3:
//     ?HCR,...  configures & queries (LocalFunctionIdentifier, NVS-persisted)
//     ;H,...    runtime actions      (CommandCharacter, routed/broadcastable)
//
//   RX on the reserved port is owned exclusively by the HCRVocalizer library
//   (it parses status). TX accepts ;H verbs, ;H,RAW, and raw bytes forwarded
//   from the RC-Controller — all just serialized writes, no RX collision.
// -----------------------------------------------------------------------

struct HCRConfig {
  uint8_t  serialPort;   // 1-5 (local UART port); 0 = not configured
  bool     configured;
  uint32_t baudRate;     // default 9600
  uint16_t pollSec;      // auto-poll interval in seconds; 0 = off; default 10
};

extern HCRConfig hcrConfig;

// ---- Runtime (;H,...) ---------------------------------------------------
void processHCRRuntimeCommand(const String &message);   // dispatch ;H,... actions

// ---- Configuration / query (?HCR,...) ----------------------------------
void configureHCR(const String &args);
void clearHCRConfig();
void printHCRSettings();     // ?HCR,LIST
void printHCRStatus();       // ?HCR,STATUS  ->  [HCR:...]
// defSep/defFunc: separator + function-identifier used for the FACTORY chain
// (chainedConfigDefault). printBackupConfig passes its live defaultSep/
// defaultFunc, which flip after the chain replays ?DELIM/?FUNCCHAR — never
// hardcode '^'/'?' inside an emitter or custom-delimiter restores break.
void printHCRBackup(String &chainedConfig, String &chainedConfigDefault,
                    char delimiter, bool printToSerial = false,
                    const String &defSep = "^", const String &defFunc = "?");

// ---- Loop tick — non-blocking update + auto-poll; call once per loop ----
void processHCRTick();

// ---- Port-conflict query (serial task & broadcast skip) ----------------
bool isSerialPortUsedForHCR(int port);

// ---- Lifecycle / NVS ----------------------------------------------------
void beginHCR();             // (re)create the HCRVocalizer on the configured port
void saveHCRSettings();
void loadHCRSettings();

#endif
