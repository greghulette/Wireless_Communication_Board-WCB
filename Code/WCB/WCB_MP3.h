#ifndef WCB_MP3_H
#define WCB_MP3_H

#include <Arduino.h>

// -----------------------------------------------------------------------
// MP3 Trigger v2.x serial-control driver for WCB
//
//  One MP3 Trigger per WCB board.
//  Configure with : ?MP3,S<port>:<baud>:V<vol>   (e.g. ?MP3,S2:9600:V25)
//  Operate  with  : ;A,PLAY,<n>  ;A,STOP  ;A,VOL,<n>  etc.
//  Callbacks      : ;A,PLAY,5,ONFIN,key  or  ;A,PLAY,5,key  (per-play)
//                   ?MP3,ONERR,<key>  — run stored-cmd on error (global)
// -----------------------------------------------------------------------

struct MP3Config {
  uint8_t  serialPort;    // 1-5 (local UART port); 0 = not configured
  bool     configured;    // true = this board HOSTS the MP3 Trigger locally
  uint32_t baudRate;      // 9600 or 38400
  uint8_t  volume;        // default volume (0=loudest, 64=inaudible); sent before every play
  char     onErrCmd[24];  // stored-command key to run when error ('E')
  uint8_t  remoteWCB;     // 0 = none; else the WCB that HOSTS the MP3 (this board routes
                          // ;A there). Persisted + backed up (mirrors HCR/WLED/Maestro).
};

extern MP3Config mp3Config;

// Auto-learn the MP3 host from a WDP advert (first-host-wins, persisted). Returns
// true if a host was newly stored.
bool mp3AutoAddRemote(uint8_t hostWCB);
extern uint8_t   mp3Volume;  // current tracked volume (0=loudest, 64=inaudible ceiling)

// ---- Core ---------------------------------------------------------------
void sendMP3Raw(uint8_t byte1, int byte2 = -1);   // send 1 or 2-byte protocol msg (byte2 0-255, -1=none)
void processMP3AudioCommand(const String &message);   // dispatch ;A,... commands

// ---- Configuration (?MP3,...) -------------------------------------------
void configureMP3(const String &args);
void clearMP3Config();
void printMP3Settings();
// defSep/defFunc: factory-chain separator + func identifier (see WCB_HCR.h).
void printMP3Backup(String &chainedConfig, String &chainedConfigDefault,
                    char delimiter, bool printToSerial = false,
                    const String &defSep = "^", const String &defFunc = "?");

// ---- Response reader — call once per loop() tick ------------------------
void processMP3Responses();

// ---- Port-conflict query (used in serialCommandTask & broadcast) --------
bool isSerialPortUsedForMP3(int port);

// ---- NVS storage --------------------------------------------------------
void saveMP3Settings();
void loadMP3Settings();

#endif
