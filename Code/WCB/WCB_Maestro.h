#ifndef WCB_MAESTRO_H
#define WCB_MAESTRO_H
#include <Arduino.h>

// Maestro configuration structure
struct MaestroConfig {
  uint8_t maestroID;
  uint8_t serialPort;
  uint8_t remoteWCB;
  bool configured;
  uint32_t baudRate;    // ADD THIS
};

#define MAX_MAESTROS_PER_WCB 9  // Config-slot capacity. Maestro device IDs are 1-8;
                                // id 9 (all local) and id 0 (all Maestros) are RESERVED
                                // routing targets, never stored as a device.

extern MaestroConfig maestroConfigs[MAX_MAESTROS_PER_WCB];
extern bool maestroEnabled;
extern bool lastReceivedViaESPNOW;

// Core Maestro functions
void sendMaestroCommand(uint8_t maestroID, uint8_t scriptNumber);
// Native Pololu servo/query verb: build the device#-addressed frame from a ";M" verb
// body (e.g. "2,setTarget,0,6000") via the shared WcbMaestro translator and route it by
// device# the SAME way as ;M<id><seq> (sendMaestroCommand) — to the configured local
// Maestro port, or forwarded to the configured remote WCB. See processMaestroCommand
// (verb-form branch) and the definition for the full routing/dedup notes.
void sendMaestroServoVerb(const char *verbBody);

// Get-query request/response (getMovingState/getPosition/getErrors). handleMaestroGet
// reads a local Maestro's reply into a RAM variable (m<dev>moving / m<dev>pos<ch> /
// m<dev>err) that IF logic tests, or forwards the query to the hosting board; the two
// helpers service the cross-board wire forms ;MG<dev>,<replyTo>,<verb> and ;M!<name>=<val>.
// While a query reads, the background Maestro-RX readers must skip maestroQueryPort.
extern volatile int maestroQueryPort;
void handleMaestroGet(int dev, const String &verb, const String &ch, int replyToWCB);
void handleMaestroGetForwarded(const String &body);   // ;MG body (excludes "MG")
void handleMaestroResult(const String &body);         // ;M! body (excludes "M!")
// Rewrite a standalone inbound ;M<dev>,getX read (received over the mesh) to ;MG<dev>,<sender>,getX
// so the reply routes back to the SENDER (:MQR to a controller, ;M! to a WCB). No-op otherwise.
void maestroRewriteInboundGet(String &cmd, int sender);

// Configuration functions
void configureMaestro(const String &message);
void clearMaestroByID(const String &message);
void clearAllMaestroConfigs();
// Auto-add (or baud-refresh) a REMOTE Maestro proxy learned from a WDP advert:
// slot = {id, serialPort:0, remoteWCB:hostWCB, baud}. First-host-wins + idempotent
// + persisted. Returns true if a slot was added or its baud changed. Called from
// the WDP advert handler (loop task → NVS-safe). See WCB_WDP.cpp.
bool maestroAutoAddRemote(uint8_t maestroID, uint8_t hostWCB, uint32_t baud);

// Helper functions
int8_t findSlotByMaestroID(uint8_t maestroID);
int8_t findSlotByMaestroIDAndTarget(uint8_t maestroID, uint8_t remoteWCB);
// Slot identity = (maestroID, serialPort, remoteWCB). Baud is a mutable
// attribute, NOT part of identity: re-configuring the same Maestro on the
// same port with a different baud updates the existing slot instead of
// adding a duplicate. Same ID on a different port (or a different target)
// still gets its own slot. Used by BOTH the ?MAESTRO and ?KYBER config
// paths so they can never disagree and create duplicate slots.
int8_t findSlotByMaestroIDPortTarget(uint8_t maestroID, uint8_t serialPort, uint8_t remoteWCB);
int8_t findEmptySlot();
bool isMaestroConfigured(uint8_t maestroID);
// defSep/defFunc: factory-chain separator + func identifier (see WCB_HCR.h).
void printMaestroBackup(String &chainedConfig, String &chainedConfigDefault,
                        char delimiter, bool printToSerial = false,
                        const String &defSep = "^", const String &defFunc = "?");
#endif