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
  bool     configured;
  uint32_t baudRate;      // 9600 or 38400
  uint8_t  volume;        // default volume (0=loudest, 64=inaudible); sent before every play
  char     onErrCmd[24];  // stored-command key to run when error ('E')
};

extern MP3Config mp3Config;
extern uint8_t   mp3Volume;  // current tracked volume (0=loudest, 64=inaudible ceiling)

// ---- Core ---------------------------------------------------------------
void sendMP3Raw(uint8_t byte1, int8_t byte2 = -1);   // send 1 or 2-byte protocol msg
void processMP3AudioCommand(const String &message);   // dispatch ;A,... commands

// ---- Configuration (?MP3,...) -------------------------------------------
void configureMP3(const String &args);
void clearMP3Config();
void printMP3Settings();
void printMP3Backup(String &chainedConfig, String &chainedConfigDefault,
                    char delimiter, bool printToSerial = false);

// ---- Response reader — call once per loop() tick ------------------------
void processMP3Responses();

// ---- Port-conflict query (used in serialCommandTask & broadcast) --------
bool isSerialPortUsedForMP3(int port);

// ---- NVS storage --------------------------------------------------------
void saveMP3Settings();
void loadMP3Settings();

#endif
