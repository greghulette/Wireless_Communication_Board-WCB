#ifndef WCB_HCR_H
#define WCB_HCR_H

#include <Arduino.h>
#include <functional>

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
//               ;H,VOLUP|VOLDN[,<V|A|B>][,step]  (omit channel = all V/A/B)   ;H,FADEIN,<A|B>,<sec>
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
  bool     configured;   // true = this board HOSTS the HCR locally
  uint32_t baudRate;     // default 9600
  uint16_t pollSec;      // auto-poll interval in seconds; 0 = off; default 10
  uint8_t  remoteWCB;    // 0 = none; else the WCB that HOSTS the HCR (this board is a
                         // client that routes ;H there). Persisted + backed up so the
                         // routing survives reboots and a config restore (see WCB_WDP).
};

extern HCRConfig hcrConfig;

// Auto-learn the HCR host from a WDP advert: if this board has no local HCR and no
// host yet, remember hostWCB (first-host-wins, persisted). Returns true if stored.
bool hcrAutoAddRemote(uint8_t hostWCB);

// ---- Runtime (;H,...) ---------------------------------------------------
void processHCRRuntimeCommand(const String &message);   // dispatch ;H,... actions

// ---- Configuration / query (?HCR,...) ----------------------------------
void configureHCR(const String &args);
void clearHCRConfig();
void printHCRSettings();     // ?HCR,LIST
void printHCRStatus();       // ?HCR,STATUS  ->  [HCR:...]
// Backup: yields each command BODY ("HCR,PORT,S2:9600" - no prefix, no separator) through
// `emit`. collectConfigCommands (WCB.ino) gives every output its own prefix and separator:
// the live chain's delimiter/funcChar, the factory chain's '^' and a func id that flips after
// ?FUNCCHAR. An emitter that hardcoded '^'/'?' broke custom-delimiter restores.
void emitHCRBackup(const std::function<void(const String &)> &emit);

// ---- Loop tick — non-blocking update + auto-poll; call once per loop ----
void processHCRTick();

// ---- Port-conflict query (serial task & broadcast skip) ----------------
bool isSerialPortUsedForHCR(int port);

// ---- Lifecycle / NVS ----------------------------------------------------
void beginHCR();             // (re)create the HCRVocalizer on the configured port
void saveHCRSettings();
void loadHCRSettings();

#endif
