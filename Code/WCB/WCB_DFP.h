#ifndef WCB_DFP_H
#define WCB_DFP_H

#include <Arduino.h>

// -----------------------------------------------------------------------
// DFPlayer Mini serial-control driver for WCB
//
//  One DFPlayer per WCB board — the alternate audio device to the MP3
//  Trigger (WCB_MP3). Both can be configured on one board; they are
//  independent verbs on independent ports.
//  Configure with : ?DFP,S<port>[:<baud>][:V<vol>]   (e.g. ?DFP,S2:9600:V20)
//  Operate  with  : ;D,PLAY,<n>  ;D,FOLDER,<f>,<t>  ;D,STOP  ;D,VOL,<n>  etc.
//  Callbacks      : ;D,PLAY,5,ONFIN,key  or  ;D,PLAY,5,key  (per-play)
//                   ?DFP,ONERR,<key>  — run stored-cmd on error (global)
//
//  TWO THINGS THAT DIFFER FROM THE MP3 TRIGGER and will bite if assumed:
//    1. Baud is FIXED at 9600 by the module. Any other value is rejected.
//    2. Volume is 0-30 where 0 = SILENT and 30 = LOUDEST — the INVERSE of
//       the MP3 Trigger's 0-64 (0 = loudest). Do not copy a volume value
//       from one device's config to the other.
//
//  Wire bytes come from the shared WcbCmd DfPlayerCodec — the SAME codec
//  NaviCore compiles — so a ;D command produces identical frames whether a
//  WCB parses it or a WCB_Client host does.
// -----------------------------------------------------------------------

struct DFPConfig {
  uint8_t  serialPort;    // 1-5 (local UART port); 0 = not configured
  bool     configured;    // true = this board HOSTS the DFPlayer locally
  uint32_t baudRate;      // always 9600 (the module offers nothing else)
  uint8_t  volume;        // default volume (0=silent .. 30=loudest); pushed to the codec shadow
  char     onErrCmd[24];  // stored-command key to run on a module error (0x40)
  uint8_t  remoteWCB;     // 0 = none; else the WCB that HOSTS the DFPlayer (this board
                          // routes ;D there). Persisted + backed up (mirrors HCR/MP3).
};

extern DFPConfig dfpConfig;
extern uint8_t   dfpVolume;   // current tracked volume (0=silent .. 30=loudest)

// Auto-learn the DFPlayer host from a WDP advert (first-host-wins, persisted).
// Returns true if a host was newly stored.
bool dfpAutoAddRemote(uint8_t hostWCB);

// ---- Core ---------------------------------------------------------------
void processDFPCommand(const String &message);   // dispatch ;D,... commands

// ---- Configuration (?DFP,...) -------------------------------------------
void configureDFP(const String &args);
void clearDFPConfig();
void printDFPSettings();
// defSep/defFunc: factory-chain separator + func identifier (see WCB_HCR.h).
void printDFPBackup(String &chainedConfig, String &chainedConfigDefault,
                    char delimiter, bool printToSerial = false,
                    const String &defSep = "^", const String &defFunc = "?");

// ---- Response reader — call once per loop() tick ------------------------
void processDFPResponses();

// ---- Port-conflict query (used in serialCommandTask & broadcast) --------
bool isSerialPortUsedForDFP(int port);

// ---- NVS storage --------------------------------------------------------
void saveDFPSettings();
void loadDFPSettings();

#endif
