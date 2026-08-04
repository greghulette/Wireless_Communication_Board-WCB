#include "WCB_RemoteTerm.h"  // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_Maestro.h"
#include "WCB_Storage.h"
#include "WCB_Variables.h"   // setVariableRAM — get-query replies land in RAM-only variables
#include <WcbCmd.h>          // shared ;M/;A/;L/;H → native-byte translators (WcbMaestro::buildSubroutineFrame
                            // is byte-identical to the inline {0xAA,id,0x27,seq}; same lib NaviCore compiles)

// While a get-query is reading its Maestro's reply, the three background readers that
// drain Maestro RX (forwardMaestroDataToLocalKyber/RemoteKyber + processIncomingSerial)
// must SKIP that port so they don't steal the reply bytes. 0 = no query active.
volatile int maestroQueryPort = 0;
static const unsigned long MAESTRO_QUERY_TIMEOUT_MS = 25;   // reply is a few bytes @57600 — usually <2ms

extern bool maestroEnabled;
extern int WCB_Number;
extern int Default_WCB_Quantity;
extern bool lastReceivedViaESPNOW;
extern bool debugEnabled;
extern bool debugMaestro;
extern uint8_t WCB_SPECIAL_PEER_ID;   // controller/NaviCore device id (default 20) — gets :MQR replies
extern char LocalFunctionIdentifier;
extern char CommandCharacter;
extern unsigned long baudRates[5];
extern void sendESPNowMessage(uint8_t target, const char *message, bool useETM = true);
extern Stream &getSerialStream(int port);
extern void updateBaudRate(int port, int baud);

MaestroConfig maestroConfigs[MAX_MAESTROS_PER_WCB];

// ==================== Helper Functions ====================

int8_t findSlotByMaestroID(uint8_t maestroID) {
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (maestroConfigs[i].configured && maestroConfigs[i].maestroID == maestroID) {
      return i;
    }
  }
  return -1;  // Not found
}

// Find a slot matching BOTH maestroID and target (remoteWCB == 0 means local).
// Used during configuration so that two entries with the same ID but different
// targets (e.g. M2:W2 local and M2:W3 remote) each get their own slot rather
// than the second overwriting the first.
int8_t findSlotByMaestroIDAndTarget(uint8_t maestroID, uint8_t remoteWCB) {
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (maestroConfigs[i].configured &&
        maestroConfigs[i].maestroID == maestroID &&
        maestroConfigs[i].remoteWCB == remoteWCB) {
      return i;
    }
  }
  return -1;
}

// Slot identity = (maestroID, serialPort, remoteWCB). Baud is intentionally
// excluded: configuring the same Maestro on the same port with only a new
// baud rate must UPDATE the existing slot, not create a duplicate. Same ID
// on a different port, or the same ID targeting a different WCB, still gets
// its own slot. For local maestros serialPort is the S-port and remoteWCB
// is 0; for remote maestros serialPort is 0 and remoteWCB is the target.
int8_t findSlotByMaestroIDPortTarget(uint8_t maestroID, uint8_t serialPort, uint8_t remoteWCB) {
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (maestroConfigs[i].configured &&
        maestroConfigs[i].maestroID  == maestroID &&
        maestroConfigs[i].serialPort == serialPort &&
        maestroConfigs[i].remoteWCB  == remoteWCB) {
      return i;
    }
  }
  return -1;
}

int8_t findEmptySlot() {
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (!maestroConfigs[i].configured) {
      return i;
    }
  }
  return -1;  // No empty slots
}

bool isMaestroConfigured(uint8_t maestroID) {
  return findSlotByMaestroID(maestroID) >= 0;
}

// ==================== Command Sending ====================

void sendMaestroCommand(uint8_t maestroID, uint8_t scriptNumber) {

  // Iterate ALL slots with this ID — the same maestroID can appear on multiple
  // targets (e.g. M2 local on this board AND M2 remote on WCB3), and every
  // matching slot must receive the command.
  bool handled = false;

  // Dedup by RESOLVED physical destination so overlapping config slots can't
  // double-fire one device. Every matching slot here shares this same maestroID
  // (it's the match key), so a repeated destination IS the same physical Maestro:
  //   • local  → dedup by serial port  (same id + same port = same device on the wire)
  //   • remote → dedup by target WCB   (same id + same board = same device over there)
  // Distinct ports / distinct boards still each fire — this only drops true repeats.
  // NOTE: the broadcast (id 0) path below is deliberately NOT deduped this way,
  // because there each write carries a DIFFERENT device id and multiple Maestros
  // can be daisy-chained on one serial line.
  uint8_t  sentLocalPorts = 0;   // bit p set → wrote serial port p (1..5)
  uint32_t sentRemoteWCBs = 0;   // bit w set → unicast WCB w      (1..20)

  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (!maestroConfigs[i].configured || maestroConfigs[i].maestroID != maestroID) continue;
    MaestroConfig &config = maestroConfigs[i];

    // LOCAL Maestro
    if (config.serialPort > 0) {
      uint8_t portBit = (config.serialPort <= 7) ? (uint8_t)(1u << config.serialPort) : 0;
      if (portBit && !(sentLocalPorts & portBit)) {   // one write per physical port
        sentLocalPorts |= portBit;
        uint8_t command[4]; WcbMaestro::buildSubroutineFrame(maestroID, scriptNumber, command);
        Stream &targetSerial = getSerialStream(config.serialPort);
        // write() queues bytes into the hardware UART TX buffer (256 bytes).
        // flush() would block ~347–694 µs waiting for the 4 bytes to transmit —
        // unnecessary and a direct source of jitter. The UART drains asynchronously.
        targetSerial.write(command, sizeof(command));
        Serial.printf("→ Maestro %d: Local S%d, Script %d\n",
                      maestroID, config.serialPort, scriptNumber);
      }
      handled = true;
    }

    // REMOTE Maestro — use ETM so the receiving board ACKs the command.
    // ;m commands are processed commands, not raw passthrough, so delivery
    // confirmation matters.  Raw Kyber Maestro port bytes bypass this path entirely.
    //
    // EVERY ;M send in this file goes out ETM — unicast, broadcast and the
    // unconfigured fallbacks alike. Two reasons, and the second is not optional:
    //   1. They are all one-shot commands (a subroutine trigger, a verb, a get
    //      request), not a servo stream, so the ACK traffic is negligible and the
    //      retry is worth having. Receivers dedup by (sender, seq), so a retry
    //      cannot double-fire a subroutine.
    //   2. A non-ETM send builds the 249-byte legacy espnow_struct_message instead
    //      of the 252-byte ETM one, and WCB_Client drops anything that is not
    //      exactly 252 bytes. So a non-ETM ;M is INVISIBLE to every client device
    //      on the mesh — including a NaviCore hosting Maestros of its own.
    if (config.remoteWCB > 0 && !lastReceivedViaESPNOW) {
      uint32_t wcbBit = (config.remoteWCB <= 31) ? (1u << config.remoteWCB) : 0;
      if (wcbBit && !(sentRemoteWCBs & wcbBit)) {     // one unicast per target board
        sentRemoteWCBs |= wcbBit;
        String espnowMsg = String(CommandCharacter) + "M" + String(maestroID) + String(scriptNumber);
        sendESPNowMessage(config.remoteWCB, espnowMsg.c_str());
        if (debugEnabled) {
          Serial.printf("→ Maestro %d: Unicast to WCB%d, Script %d\n",
                        maestroID, config.remoteWCB, scriptNumber);
        }
      }
      handled = true;
    }
  }

  if (handled) return;

  // Broadcast (maestroID == 0)
  if (maestroID == 0) {
    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
      if (maestroConfigs[i].configured && maestroConfigs[i].serialPort > 0) {
        uint8_t command[4]; WcbMaestro::buildSubroutineFrame(maestroConfigs[i].maestroID, scriptNumber, command);
        Stream &targetSerial = getSerialStream(maestroConfigs[i].serialPort);
        targetSerial.write(command, sizeof(command)); // no flush — UART drains async
      }
    }
    
    if (!isMaestroConfigured(WCB_Number)) {
      uint8_t command[4]; WcbMaestro::buildSubroutineFrame(WCB_Number, scriptNumber, command);
      Serial1.write(command, sizeof(command)); // no flush — UART drains async
    }
    
    if (!lastReceivedViaESPNOW) {
      String espnowMsg = String(CommandCharacter) + "M0" + String(scriptNumber);
      sendESPNowMessage(0, espnowMsg.c_str());   // ETM — see the note on the remote branch above
    }
    
    if (debugEnabled) {
      Serial.printf("→ Maestro Broadcast: Script %d\n", scriptNumber);
    }
    return;
  }
  
  // Target 9 = "the local Maestro(s) on THIS board." Resolve through the
  // configured Maestro mapping so each local Maestro is addressed with its OWN
  // configured Pololu device ID and port. No longer assumes the device ID
  // equals the WCB number, nor that the Maestro lives on S1.
  if (maestroID == 9) {
    bool wroteLocal = false;
    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
      if (maestroConfigs[i].configured && maestroConfigs[i].remoteWCB == 0 &&
          maestroConfigs[i].serialPort > 0) {
        uint8_t command[4]; WcbMaestro::buildSubroutineFrame(maestroConfigs[i].maestroID, scriptNumber, command);
        getSerialStream(maestroConfigs[i].serialPort).write(command, sizeof(command)); // no flush — UART drains async
        Serial.printf("→ Maestro %d (local, target 9): S%d, Script %d\n",
                      maestroConfigs[i].maestroID, maestroConfigs[i].serialPort, scriptNumber);
        wroteLocal = true;
      }
    }
    // Backward-compat: no local Maestro configured yet → legacy S1 + WCB number
    // so boards that haven't been reconfigured still respond to target 9.
    if (!wroteLocal) {
      uint8_t command[4]; WcbMaestro::buildSubroutineFrame(WCB_Number, scriptNumber, command);
      Serial1.write(command, sizeof(command)); // no flush — UART drains async
      Serial.printf("→ Maestro (local, target 9): legacy S1 fallback, dev %d, Script %d\n",
                    WCB_Number, scriptNumber);
    }
    return;
  }

  // Legacy: targeting this board's own WCB number with nothing configured for
  // that ID — keep the old hardcoded S1 write for backward compatibility.
  if (maestroID == WCB_Number) {
    uint8_t command[4]; WcbMaestro::buildSubroutineFrame(maestroID, scriptNumber, command);
    Serial1.write(command, sizeof(command)); // no flush — UART drains async
    Serial.printf("→ Maestro %d: Legacy S1, Script %d\n", maestroID, scriptNumber);
    return;
  }
  
  // Unconfigured fallback
  if (maestroID >= 1 && maestroID <= Default_WCB_Quantity) {
    if (!lastReceivedViaESPNOW) {
      String espnowMsg = String(CommandCharacter) + "M" + String(maestroID) + String(scriptNumber);
      sendESPNowMessage(maestroID, espnowMsg.c_str());
      if (debugEnabled) {
        Serial.printf("→ Maestro %d: Fallback unicast to WCB%d, Script %d\n",
                      maestroID, maestroID, scriptNumber);
      }
    }
  } else {
    if (!lastReceivedViaESPNOW) {
      String espnowMsg = String(CommandCharacter) + "M" + String(maestroID) + String(scriptNumber);
      sendESPNowMessage(0, espnowMsg.c_str());
      Serial.printf("→ Maestro %d: Fallback broadcast, Script %d\n",
                    maestroID, scriptNumber);
    }
  }
}

// ── Native Pololu servo/query verb routing ──────────────────────────────────────
// Build the frame from a ";M" verb body (e.g. "2,setTarget,0,6000") and route it by
// device# EXACTLY like the ;M<id><seq> subroutine trigger — a full parallel of
// sendMaestroCommand: config-matched LOCAL write / REMOTE forward, and the SAME dev-0
// broadcast, dev-9 "all local Maestros", dev==WCB_Number legacy-S1, and unconfigured
// fallback. Broadcast / target-9 re-address the verb to each Maestro's OWN id (the
// payload after the device# is location-independent), so "all Maestros goHome" works.
//
// Query verbs (getPosition/…) write only the REQUEST frame; the Maestro's reply rides the
// existing Maestro→Kyber return path. WcbMaestro::build validates every arg (returns 0 on
// a bad verb/range), so a malformed verb never reaches the servo wire.

// Re-address the verb payload to Maestro id `tid` and write that frame to `out`.
static void writeVerbFrameTo(Stream &out, int tid, const String &payload) {
  String  cmd = String(tid) + "," + payload;              // "<tid>,goHome" / "<tid>,5" / …
  uint8_t f[WcbMaestro::MAX_FRAME];
  size_t  nn = WcbMaestro::build(cmd.c_str(), f, sizeof(f));
  if (nn) out.write(f, nn);                               // no flush — UART drains async
}

// The ";M…" text to forward to another board. A numeric single-digit payload keeps the
// legacy no-comma spelling (";M15"); everything else uses the unambiguous comma form
// (";M1,goHome", ";M12,5"), which the receiver re-parses identically.
static String maestroVerbForwardText(int dev, const String &payload) {
  bool numeric = payload.length() > 0;
  for (unsigned i = 0; i < payload.length(); i++)
    if (payload[i] < '0' || payload[i] > '9') { numeric = false; break; }
  String pfx = String(CommandCharacter) + "M" + String(dev);
  return (numeric && dev >= 0 && dev <= 9) ? (pfx + payload) : (pfx + "," + payload);
}

void sendMaestroServoVerb(const char *verbBody) {
  // verbBody = "<dev>,<payload>" — payload is a subroutine number (numeric comma form) or a
  // verb ("goHome" / "setTarget,0,6000"). Validate + build THIS device's frame once.
  uint8_t frame[WcbMaestro::MAX_FRAME];
  size_t  n = WcbMaestro::build(verbBody, frame, sizeof(frame));
  if (n == 0) {
    if (debugEnabled) Serial.printf("[MAESTRO] bad verb, ignored: %s\n", verbBody);
    return;
  }
  String body    = verbBody;
  int    dev     = body.toInt();                          // routing key = leading digits
  int    ci      = body.indexOf(',');
  String payload = (ci >= 0) ? body.substring(ci + 1) : String();   // after the first comma

  // Get-queries (getMovingState/getPosition/getErrors) are request→reply, not fire-and-
  // forget: hand them to the query engine, which reads the Maestro's answer and stores it
  // in a RAM variable. This is a LOCALLY-originated query, so the reply lands here.
  {
    int    pc = payload.indexOf(',');
    String v0 = (pc < 0) ? payload : payload.substring(0, pc);
    String v0l = v0; v0l.toLowerCase();
    if (v0l.startsWith("get")) {
      handleMaestroGet(dev, v0, (pc < 0) ? String() : payload.substring(pc + 1), WCB_Number);
      return;
    }
  }

  // Config-matched routing (dedup: one write per port, one unicast per board).
  bool     handled        = false;
  uint8_t  sentLocalPorts = 0;
  uint32_t sentRemoteWCBs = 0;
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (!maestroConfigs[i].configured || maestroConfigs[i].maestroID != dev) continue;
    MaestroConfig &config = maestroConfigs[i];
    if (config.serialPort > 0) {                          // LOCAL — write this dev's frame
      uint8_t portBit = (config.serialPort <= 7) ? (uint8_t)(1u << config.serialPort) : 0;
      if (portBit && !(sentLocalPorts & portBit)) {
        sentLocalPorts |= portBit;
        getSerialStream(config.serialPort).write(frame, n);
        if (debugMaestro) Serial.printf("→ Maestro %d verb '%s': Local S%d\n", dev, verbBody, config.serialPort);
      }
      handled = true;
    }
    if (config.remoteWCB > 0 && !lastReceivedViaESPNOW) { // REMOTE — forward the verb TEXT
      uint32_t wcbBit = (config.remoteWCB <= 31) ? (1u << config.remoteWCB) : 0;
      if (wcbBit && !(sentRemoteWCBs & wcbBit)) {
        sentRemoteWCBs |= wcbBit;
        sendESPNowMessage(config.remoteWCB, maestroVerbForwardText(dev, payload).c_str());
        if (debugMaestro) Serial.printf("→ Maestro %d verb '%s': Unicast WCB%d\n", dev, verbBody, config.remoteWCB);
      }
      handled = true;
    }
  }
  if (handled) return;

  // Broadcast (dev 0) — every LOCAL Maestro runs the verb re-addressed to its own id, and
  // the verb text goes out to the mesh so remote boards do the same. Mirrors the legacy
  // sendMaestroCommand broadcast, including its default-Maestro-on-S1 fallback so an
  // un-mapped board still actuates its S1 Maestro (Pololu id = WCB number).
  if (dev == 0) {
    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++)
      if (maestroConfigs[i].configured && maestroConfigs[i].serialPort > 0)
        writeVerbFrameTo(getSerialStream(maestroConfigs[i].serialPort), maestroConfigs[i].maestroID, payload);
    if (!isMaestroConfigured(WCB_Number))
      writeVerbFrameTo(Serial1, WCB_Number, payload);
    if (!lastReceivedViaESPNOW)
      sendESPNowMessage(0, maestroVerbForwardText(0, payload).c_str());
    if (debugMaestro) Serial.printf("→ Maestro Broadcast verb '%s'\n", verbBody);
    return;
  }

  // Target 9 — "the local Maestro(s) on THIS board", each addressed by its own id.
  if (dev == 9) {
    bool wroteLocal = false;
    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++)
      if (maestroConfigs[i].configured && maestroConfigs[i].remoteWCB == 0 && maestroConfigs[i].serialPort > 0) {
        writeVerbFrameTo(getSerialStream(maestroConfigs[i].serialPort), maestroConfigs[i].maestroID, payload);
        wroteLocal = true;
      }
    if (!wroteLocal) writeVerbFrameTo(Serial1, WCB_Number, payload);   // legacy S1 fallback
    if (debugMaestro) Serial.printf("→ Maestro (local, target 9) verb '%s'\n", verbBody);
    return;
  }

  // dev == this board's own number, nothing configured → legacy local S1 write. This is the
  // case that must NOT self-forward over ESP-NOW (a board can't ESP-NOW its own MAC).
  if (dev == WCB_Number) {
    Serial1.write(frame, n);   // no flush — UART drains async
    if (debugMaestro) Serial.printf("→ Maestro %d verb '%s': Legacy S1\n", dev, verbBody);
    return;
  }

  // Unconfigured fallback — forward the verb text so whichever board owns `dev` handles it.
  // Guarded so a verb we received over ESP-NOW is never bounced (dev==WCB_Number handled above).
  if (!lastReceivedViaESPNOW) {
    uint8_t target = (dev >= 1 && dev <= Default_WCB_Quantity) ? (uint8_t)dev : 0;  // else discovery-broadcast
    sendESPNowMessage(target, maestroVerbForwardText(dev, payload).c_str());
    if (debugMaestro) Serial.printf("→ Maestro %d verb '%s': Fallback %s\n",
                                    dev, verbBody, target ? "unicast" : "broadcast");
  }
}

// ==================== Get-query request/response ====================
// A ;M<dev>,get* verb is a REQUEST/RESPONSE, not fire-and-forget: send the Pololu query,
// read the Maestro's reply, and drop the value into a RAM-only variable that IF logic can
// test (e.g. IF,m2moving=0). Because the reply is asynchronous the query and the IF must
// be SEPARATE invocations with a ;t gap. Variable naming:
//   getMovingState -> m<dev>moving (0/1)   getPosition,<ch> -> m<dev>pos<ch>   getErrors -> m<dev>err
// Cross-board: the originator forwards ;MG<dev>,<replyTo>,<verb>[,<ch>] to the hosting
// board, which reads its local Maestro and pushes ;M!<name>=<value> back to <replyTo>.

// Map a get verb to its reply length + the RAM variable it writes. Returns false if not a get.
static bool maestroGetInfo(int dev, const String &verb, const String &ch,
                           int &expectedBytes, String &varName) {
  if (verb.equalsIgnoreCase("getMovingState")) { expectedBytes = 1; varName = "m" + String(dev) + "moving";     return true; }
  if (verb.equalsIgnoreCase("getErrors"))      { expectedBytes = 2; varName = "m" + String(dev) + "err";        return true; }
  if (verb.equalsIgnoreCase("getPosition"))    { expectedBytes = 2; varName = "m" + String(dev) + "pos" + ch;   return true; }
  return false;
}

// Service a get-query for device `dev`. `replyToWCB` is the board whose RAM state should
// receive the result (its IF logic reads it). Devices 1-8 only (0/9 are fan-out targets;
// a query needs a single answer).
void handleMaestroGet(int dev, const String &verb, const String &ch, int replyToWCB) {
  if (dev < 1 || dev > 8) { if (debugEnabled) Serial.printf("[MAESTRO] get: bad device %d (1-8 only)\n", dev); return; }

  int expectedBytes; String varName;
  if (!maestroGetInfo(dev, verb, ch, expectedBytes, varName)) {
    if (debugEnabled) Serial.printf("[MAESTRO] get: unknown query '%s'\n", verb.c_str());
    return;
  }
  if (varName.length() > 15) { if (debugEnabled) Serial.printf("[MAESTRO] get: var name too long: %s\n", varName.c_str()); return; }

  // Build the Pololu request frame from the reconstructed verb body.
  String  verbBody = String(dev) + "," + verb + (ch.length() ? ("," + ch) : "");
  uint8_t frame[WcbMaestro::MAX_FRAME];
  size_t  n = WcbMaestro::build(verbBody.c_str(), frame, sizeof(frame));
  if (n == 0) { if (debugEnabled) Serial.printf("[MAESTRO] get: bad query %s\n", verbBody.c_str()); return; }

  // Where does dev live? (local serial port, or a remote host WCB)
  int localPort = 0, hostWCB = 0;
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (!maestroConfigs[i].configured || maestroConfigs[i].maestroID != dev) continue;
    if (maestroConfigs[i].remoteWCB == 0 && maestroConfigs[i].serialPort > 0) localPort = maestroConfigs[i].serialPort;
    else if (maestroConfigs[i].remoteWCB > 0)                                 hostWCB   = maestroConfigs[i].remoteWCB;
  }

  // Legacy default (no ?MAESTRO config): this board's OWN Maestro is device == WCB_Number on
  // S1 — mirror the ;M<id><seq> / ;M<dev>,goHome fallback so a get on it reads S1 locally
  // instead of broadcasting an unanswerable ;MG into the mesh.
  if (localPort == 0 && hostWCB == 0 && dev == WCB_Number) localPort = 1;

  // REMOTE: forward the query to the hosting board, carrying the originator so it replies
  // home. Never bounce a query we ourselves received over ESP-NOW (a ;MG we can't service).
  if (localPort == 0) {
    if (lastReceivedViaESPNOW) return;
    String fwd = String(CommandCharacter) + "MG" + String(dev) + "," + String(replyToWCB) + "," + verb
                 + (ch.length() ? ("," + ch) : "");
    if (hostWCB > 0) sendESPNowMessage((uint8_t)hostWCB, fwd.c_str());       // unicast to the host
    else             sendESPNowMessage(0, fwd.c_str());                     // unconfigured → let the host find it
    if (debugMaestro) Serial.printf("→ Maestro %d get '%s' -> WCB%d (reply to %d)\n", dev, verb.c_str(), hostWCB, replyToWCB);
    return;
  }

  // LOCAL: gated timed read of the reply on this Maestro's port.
  Stream &s = getSerialStream(localPort);
  maestroQueryPort = localPort;                 // background readers skip this port now
  while (s.available() > 0) s.read();           // discard any stale bytes
  s.write(frame, n);                            // send the request
  uint8_t reply[2]; int got = 0;
  unsigned long t0 = millis();
  while (got < expectedBytes && (millis() - t0) < MAESTRO_QUERY_TIMEOUT_MS) {
    if (s.available() > 0) reply[got++] = (uint8_t)s.read();
    else delay(1);                               // yield ~1ms rather than spin — don't starve loop()/tasks
  }
  maestroQueryPort = 0;                          // release the port

  if (got < expectedBytes) {
    if (debugMaestro) Serial.printf("[MAESTRO] get %d '%s': timeout (%d/%d bytes) — value kept\n",
                                    dev, verb.c_str(), got, expectedBytes);
    return;                                      // leave the previous value in place
  }
  int32_t value = (expectedBytes == 1) ? reply[0] : (int32_t)(reply[0] | (reply[1] << 8));

  setVariableRAM(varName, value);                // THIS board's IF can now read it
  if (debugMaestro) Serial.printf("[MAESTRO] get %d '%s' -> %s=%ld\n", dev, verb.c_str(), varName.c_str(), (long)value);

  // Deliver the result to whoever asked. The controller/special peer (NaviCore = device
  // WCB_SPECIAL_PEER_ID) speaks the ":MQR,<id>,<chan>,<KIND>,<value>" reply format it parses
  // in onCommand (KIND = POS|MOV|ERR); any other WCB gets the ";M!<var>=<value>" form that
  // feeds its RAM/IF state. The asker sends a PLAIN ;M<dev>,getX; the WCB receive path rewrites
  // inbound mesh reads to ;MG<dev>,<sender>,verb (maestroRewriteInboundGet) so replyTo is set.
  if (replyToWCB > 0 && replyToWCB != WCB_Number) {
    String res;
    if (replyToWCB == WCB_SPECIAL_PEER_ID) {
      const char *kind = verb.equalsIgnoreCase("getPosition")    ? "POS"
                       : verb.equalsIgnoreCase("getMovingState") ? "MOV"
                       : verb.equalsIgnoreCase("getErrors")      ? "ERR" : "";
      res = String(":MQR,") + String(dev) + "," + (ch.length() ? ch : String("0"))
          + "," + kind + "," + String((long)value);
    } else {
      res = String(CommandCharacter) + "M!" + varName + "=" + String((long)value);
    }
    sendESPNowMessage((uint8_t)replyToWCB, res.c_str());
    if (debugMaestro) Serial.printf("[MAESTRO] get reply -> WCB%d: %s\n", replyToWCB, res.c_str());
  }
}

// ;MG<dev>,<replyTo>,<verb>[,<ch>] — a get forwarded to us (the host). body excludes "MG".
void handleMaestroGetForwarded(const String &body) {
  int c1 = body.indexOf(',');            if (c1 < 0) return;
  int c2 = body.indexOf(',', c1 + 1);    if (c2 < 0) return;
  int    dev     = body.substring(0, c1).toInt();
  int    replyTo = body.substring(c1 + 1, c2).toInt();
  if (replyTo < 1 || replyTo > MAX_WCB_COUNT) return;   // malformed/injected reply-to → drop, don't misroute
  String rest    = body.substring(c2 + 1);          // "getMovingState" | "getPosition,0"
  int c3 = rest.indexOf(',');
  String verb = (c3 < 0) ? rest : rest.substring(0, c3);
  String ch   = (c3 < 0) ? String() : rest.substring(c3 + 1);
  handleMaestroGet(dev, verb, ch, replyTo);
}

// ;M!<name>=<value> — a get-result pushed home from the host. Store RAM-only. body excludes "M!".
void handleMaestroResult(const String &body) {
  int eq = body.indexOf('=');
  if (eq <= 0) return;
  String name = body.substring(0, eq); name.trim();
  // Only accept our own telemetry namespace ("m" + device 1-8): a mesh peer must not be
  // able to set (or, via the RAM path, silently demote/clobber) arbitrary WCB variables.
  if (name.length() < 2 || name[0] != 'm' || name[1] < '1' || name[1] > '8') return;
  int32_t value = (int32_t)body.substring(eq + 1).toInt();
  setVariableRAM(name, value);
  if (debugMaestro) Serial.printf("[MAESTRO] result %s=%ld (from mesh)\n", name.c_str(), (long)value);
}

// A native Maestro READ (;M<dev>,getX[,ch]) received OVER THE MESH must reply to the SENDER,
// not this board — NaviCore (the controller) sends a plain ;M<dev>,getMovingState and reads
// the reply back; a peer WCB likewise. Rewrite a standalone inbound get in place to the
// ;MG<dev>,<sender>,getX[,ch] forwarded-get form so the existing get-query path routes the
// reply home (:MQR to the special peer/controller, ;M! to a WCB). No-op for actuation verbs
// (they need no reply), chains, or anything malformed. Called at the ESP-NOW receive sites
// where senderWCB is known — see the ETM + non-ETM command dispatch in WCB.ino.
void maestroRewriteInboundGet(String &cmd, int sender) {
  if (sender < 1 || sender > MAX_WCB_COUNT) return;
  if (cmd.indexOf('^') >= 0) return;                          // standalone command only
  int p = (cmd.length() && cmd[0] == CommandCharacter) ? 1 : 0;
  if (p + 1 >= (int)cmd.length() || (cmd[p] != 'M' && cmd[p] != 'm')) return;
  int comma = cmd.indexOf(',', p + 1);
  if (comma <= p + 1) return;                                 // need <dev> then a comma
  for (int i = p + 1; i < comma; i++) if (cmd[i] < '0' || cmd[i] > '9') return;
  String after = cmd.substring(comma + 1);                    // "getMovingState" | "getPosition,0" | "goHome"
  String low = after; low.toLowerCase();
  if (!low.startsWith("get")) return;                         // only get* reads carry a reply
  String dev = cmd.substring(p + 1, comma);
  cmd = String(CommandCharacter) + "MG" + dev + "," + String(sender) + "," + after;
}

// ==================== Configuration Functions ====================
void configureMaestro(const String &message) {
  // Format: ?MAESTRO,M<maestroID>:W<wcb>S<port>:<baud>
  // Example: ?MAESTRO,M2:W2S1:57600
  // Chained: ?MAESTRO,M1:W1S2:115200,M2:W2S1:57600,M3:W3S3:115200
  
String remaining = message;
  remaining.trim();
  if (remaining.isEmpty() || !remaining.startsWith("M") && !remaining.startsWith("m")) {
    Serial.printf("Invalid format. Use: %cMAESTRO,M<maestroID>:W<wcb>S<port>:<baud>\n", LocalFunctionIdentifier);
    Serial.printf("Example: %cMAESTRO,M2:W2S1:57600\n", LocalFunctionIdentifier);
    return;
  }
  
  // Process each Maestro config (split by comma)
  int startIdx = 0;
  while (startIdx < remaining.length()) {
    int nextComma = remaining.indexOf(',', startIdx);
    if (nextComma == -1) nextComma = remaining.length();
    
    String singleConfig = remaining.substring(startIdx, nextComma);
    singleConfig.trim();
    singleConfig.toUpperCase();
    
    // Must start with 'M'
    if (!singleConfig.startsWith("M")) {
      Serial.printf("Invalid Maestro config: %s (must start with M)\n", singleConfig.c_str());
      Serial.printf("Format should be: M<maestroID>:W<wcb>S<port>:<baud>\n");
      startIdx = nextComma + 1;
      continue;
    }
    
    // Parse: M2:W2S1:57600
    int firstColon = singleConfig.indexOf(':');
    int secondColon = singleConfig.indexOf(':', firstColon + 1);
    
    if (firstColon == -1 || secondColon == -1) {
      Serial.printf("Invalid Maestro config: %s\n", singleConfig.c_str());
      Serial.printf("Format should be: M<maestroID>:W<wcb>S<port>:<baud>\n");
      startIdx = nextComma + 1;
      continue;
    }
    
    int maestroID = singleConfig.substring(1, firstColon).toInt();  // Skip 'M'
    String destStr = singleConfig.substring(firstColon + 1, secondColon);
    int baudRate = singleConfig.substring(secondColon + 1).toInt();
    
    // Validate Maestro ID
    if (maestroID < 1 || maestroID > 8) {
      Serial.println("Invalid Maestro ID. Must be 1-8 (9=all local, 0=all Maestros are reserved)");
      startIdx = nextComma + 1;
      continue;
    }
    
    // Parse W<wcb>S<port> format
    int wIndex = destStr.indexOf('W');
    int sIndex = destStr.indexOf('S');
    
    if (wIndex == -1 || sIndex == -1 || sIndex <= wIndex) {
      Serial.println("Destination must be in format W<wcb>S<port> (e.g., W2S1)");
      startIdx = nextComma + 1;
      continue;
    }
    
    int targetWCB = destStr.substring(wIndex + 1, sIndex).toInt();
    int serialPort = destStr.substring(sIndex + 1).toInt();
    
    if (targetWCB < 1 || targetWCB > MAX_WCB_COUNT) {
      Serial.printf("Invalid WCB number. Must be W1-W%d\n", MAX_WCB_COUNT);
      startIdx = nextComma + 1;
      continue;
    }
    
    if (serialPort < 1 || serialPort > 5) {
      Serial.println("Invalid serial port. Must be S1-S5");
      startIdx = nextComma + 1;
      continue;
    }
    
    // Validate baud rate
    if (!(baudRate == 0 || baudRate == 110 || baudRate == 300 || 
          baudRate == 600 || baudRate == 1200 || baudRate == 2400 || 
          baudRate == 9600 || baudRate == 14400 || baudRate == 19200 || 
          baudRate == 38400 || baudRate == 57600 || baudRate == 115200 || 
          baudRate == 128000 || baudRate == 256000)) {
      Serial.println("Invalid baud rate");
      startIdx = nextComma + 1;
      continue;
    }
    
    // Find or create slot — keyed on (maestroID, serialPort, target). A repeat
    // config of the same Maestro on the same port (e.g. only the baud rate
    // changed) updates the existing slot instead of adding a duplicate. Same
    // ID on a different port, or targeting a different WCB, still gets its own
    // slot. The slot stores serialPort=0 for remote maestros, so the key uses
    // 0 for the port in the remote case to match how the slot is written below.
    uint8_t effectiveRemoteWCB = (targetWCB == WCB_Number) ? 0 : targetWCB;
    uint8_t keyPort            = (targetWCB == WCB_Number) ? (uint8_t)serialPort : 0;
    int8_t slot = findSlotByMaestroIDPortTarget(maestroID, keyPort, effectiveRemoteWCB);
    if (slot < 0) {
      slot = findEmptySlot();
      if (slot < 0) {
        Serial.printf("No available slots. Maximum %d Maestros.\n", MAX_MAESTROS_PER_WCB);
        startIdx = nextComma + 1;
        continue;
      }
    }
    
    // DETERMINE IF LOCAL OR REMOTE
    if (targetWCB == WCB_Number) {
      // LOCAL CONFIGURATION
      
      // Software serial warning
      if (serialPort >= 3 && serialPort <= 5 && baudRate > 57600) {
        Serial.println("\n⚠️  =============== WARNING ===============");
        Serial.printf("S%d is SOFTWARE SERIAL\n", serialPort);
        Serial.printf("Baud rate (%d) is TOO HIGH for software serial\n", baudRate);
        Serial.println("❌ CONFIGURATION BLOCKED!");
        Serial.println("Options:");
        Serial.printf("  1. Use hardware serial: %cMAESTRO,M%d:W%dS1:%d\n", 
                      LocalFunctionIdentifier, maestroID, WCB_Number, baudRate);
        Serial.printf("  2. Lower baud rate: %cMAESTRO,M%d:W%dS%d:57600\n", 
                      LocalFunctionIdentifier, maestroID, WCB_Number, serialPort);
        Serial.println("========================================\n");
        startIdx = nextComma + 1;
        continue;
      }
      
      // Update baud rate if needed
      if (baudRate != baudRates[serialPort - 1]) {
        updateBaudRate(serialPort, baudRate);
      }

      // **DISABLE BROADCAST OUTPUT ON THIS PORT**
      if (serialBroadcastEnabled[serialPort - 1]) {
        serialBroadcastEnabled[serialPort - 1] = false;
        saveBroadcastSettingsToPreferences();
        Serial.printf("⚠️  Disabled broadcast output on S%d (Maestro port)\n", serialPort);
      }

      // **DISABLE BROADCAST INPUT ON THIS PORT**
      if (!blockBroadcastFrom[serialPort - 1]) {
        blockBroadcastFrom[serialPort - 1] = true;
        saveBroadcastBlockSettings();
        Serial.printf("⚠️  Disabled broadcast input on S%d (Maestro port)\n", serialPort);
      }
      
      maestroConfigs[slot].maestroID = maestroID;
      maestroConfigs[slot].serialPort = serialPort;
      maestroConfigs[slot].remoteWCB = 0;
      maestroConfigs[slot].configured = true;
      maestroConfigs[slot].baudRate = baudRate;
      
      saveMaestroSettings();
      Serial.printf("✓ Maestro %d: Local S%d at %d baud (slot %d)\n", 
                    maestroID, serialPort, baudRate, slot + 1);
                    
    } else {
      // REMOTE CONFIGURATION
      maestroConfigs[slot].maestroID = maestroID;
      maestroConfigs[slot].serialPort = 0;
      maestroConfigs[slot].remoteWCB = targetWCB;
      maestroConfigs[slot].configured = true;
      maestroConfigs[slot].baudRate = baudRate;
      
      saveMaestroSettings();
      Serial.printf("✓ Maestro %d: Remote on WCB%d (unicast, slot %d)\n", 
                    maestroID, targetWCB, slot + 1);
    }
    
    startIdx = nextComma + 1;
  }
}

// Auto-add (or baud-refresh) a REMOTE Maestro proxy learned from a WDP advert.
// Mirrors the REMOTE branch of configureMaestro: slot = {id, serialPort:0,
// remoteWCB:hostWCB, baud}. Behavior:
//   • existing proxy to THIS host  -> refresh baud only if it changed
//   • no proxy to this host yet     -> claim an empty slot, EVEN IF this id is
//                                      already hosted locally or proxied to a
//                                      DIFFERENT host. Duplicate Maestro ids are a
//                                      supported mesh arrangement (the same id can
//                                      live on this board AND on one or more others),
//                                      and sendMaestroCommand() fans ;M<id> out to
//                                      every matching slot — so each advertising host
//                                      needs its OWN proxy. This is per-host, NOT
//                                      first-host-wins, and a proxy alongside a local
//                                      Maestro of the same id is intended (multicast).
//   • no free slot                 -> skip (logged; 9-slot cap)
// Never re-homes or evicts on its own: a proxy to a host that goes offline, or whose
// Maestro is physically moved, stays until cleared (?MAESTRO,CLEAR / clear-by-id).
// Persists via saveMaestroSettings() and is safe to call every advert. Returns
// true iff a slot was added or its baud changed (so the caller could log it).
bool maestroAutoAddRemote(uint8_t maestroID, uint8_t hostWCB, uint32_t baud) {
  if (maestroID < 1 || maestroID > 8)      return false;
  if (hostWCB == 0 || hostWCB == WCB_Number) return false;   // not a remote host

  int8_t slot = findSlotByMaestroIDPortTarget(maestroID, 0, hostWCB);
  if (slot >= 0) {                         // already proxied to this exact host
    if (baud != 0 && maestroConfigs[slot].baudRate != baud) {
      maestroConfigs[slot].baudRate = baud;
      saveMaestroSettings();
      Serial.printf("[WDP] Maestro %d @ WCB%d baud updated to %lu\n",
                    maestroID, hostWCB, (unsigned long)baud);
      return true;
    }
    return false;                          // unchanged — nothing to do
  }
  // Intentionally NO "already configured elsewhere" guard: duplicate Maestro ids are legal
  // (same id local AND/OR on multiple hosts), and sendMaestroCommand fans ;M<id> to every
  // matching slot, so each host gets its OWN proxy. The exact (id,host) duplicate was already
  // handled above; fall through to claim a fresh slot for this new host.
  slot = findEmptySlot();
  if (slot < 0) {
    if (debugEnabled)                       // rare (9 slots); debug-gated to avoid steady-state spam
      Serial.printf("[WDP] Maestro %d @ WCB%d heard but no free slot (max %d) — clear one to auto-add\n",
                    maestroID, hostWCB, MAX_MAESTROS_PER_WCB);
    return false;
  }
  maestroConfigs[slot].maestroID  = maestroID;
  maestroConfigs[slot].serialPort = 0;      // remote proxy
  maestroConfigs[slot].remoteWCB  = hostWCB;
  maestroConfigs[slot].configured = true;
  maestroConfigs[slot].baudRate   = baud;
  saveMaestroSettings();
  Serial.printf("[WDP] auto-added remote Maestro %d on WCB%d @ %lu baud (slot %d)\n",
                maestroID, hostWCB, (unsigned long)baud, slot + 1);
  return true;
}

// Clear one slot and do the port housekeeping (broadcast re-enable, baud
// reset). Does NOT call saveMaestroSettings() — the caller saves once after
// clearing one or more slots.
static void _clearMaestroSlot(int8_t slot) {
  if (slot < 0) return;

  uint8_t freedPort = maestroConfigs[slot].serialPort;

  maestroConfigs[slot].configured = false;
  maestroConfigs[slot].maestroID  = 0;
  maestroConfigs[slot].serialPort = 0;
  maestroConfigs[slot].remoteWCB  = 0;
  maestroConfigs[slot].baudRate   = 0;

  // Auto re-enable broadcast on freed port (only if no OTHER slot still uses it)
  if (freedPort > 0) {
    bool portStillUsed = false;
    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
      if (maestroConfigs[i].configured && maestroConfigs[i].serialPort == freedPort) {
        portStillUsed = true;
        break;
      }
    }
    if (!portStillUsed) {
      if (!serialBroadcastEnabled[freedPort - 1]) {
        serialBroadcastEnabled[freedPort - 1] = true;
        saveBroadcastSettingsToPreferences();
        Serial.printf("✓ Re-enabled broadcast output on S%d\n", freedPort);
      }
      if (blockBroadcastFrom[freedPort - 1]) {
        blockBroadcastFrom[freedPort - 1] = false;
        saveBroadcastBlockSettings();
        Serial.printf("✓ Re-enabled broadcast input on S%d\n", freedPort);
      }
      updateBaudRate(freedPort, 9600);
      Serial.printf("✓ Reset S%d baud rate to 9600\n", freedPort);
    }
  }
}

void clearMaestroByID(const String &message) {
  // Caller strips "CLEAR," and passes the remainder. Accepts:
  //   "M5" / "5"             → clear ALL slots with that Maestro ID
  //   "M1:W1S2" / "1:W1S2"   → clear ONLY that specific (id, port, target)
  //                            slot. Required now that the same ID can occupy
  //                            multiple slots (e.g. M1 moved S2→S1); a bare-ID
  //                            clear could otherwise nuke the wrong one.

  String s = message;
  s.trim();
  if (s.startsWith("M") || s.startsWith("m")) s = s.substring(1);

  int colon = s.indexOf(':');

  // ---- Bare ID: clear every slot with this ID ----
  if (colon < 0) {
    int maestroID = s.toInt();
    if (maestroID < 1 || maestroID > 8) {
      Serial.printf("Invalid Maestro ID. Must be M1-M8 (9=all local, 0=all are reserved)\n");
      return;
    }
    int cleared = 0;
    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
      if (maestroConfigs[i].configured && maestroConfigs[i].maestroID == maestroID) {
        _clearMaestroSlot(i);
        cleared++;
      }
    }
    if (cleared == 0) {
      Serial.printf("Maestro ID %d is not configured\n", maestroID);
    } else {
      saveMaestroSettings();
      Serial.printf("Cleared Maestro ID %d (%d slot%s)\n",
                    maestroID, cleared, cleared == 1 ? "" : "s");
    }
    return;
  }

  // ---- Targeted: M<id>:W<wcb>S<port> (clear exactly one slot) ----
  int maestroID = s.substring(0, colon).toInt();
  String tgt = s.substring(colon + 1);
  tgt.toUpperCase();
  int wi = tgt.indexOf('W');
  int si = tgt.indexOf('S');
  if (maestroID < 1 || maestroID > 8 || wi < 0 || si < 0 || si <= wi) {
    Serial.printf("Invalid CLEAR target '%s'. Use M<id>:W<wcb>S<port> or M<id>\n",
                  message.c_str());
    return;
  }
  int wcb  = tgt.substring(wi + 1, si).toInt();
  int port = tgt.substring(si + 1).toInt();   // toInt() stops at any trailing ':baud'

  // Mirror how configureMaestro keys slots: local → (id, port, 0);
  // remote → (id, 0, wcb).
  uint8_t keyRemote = (wcb == WCB_Number) ? 0 : (uint8_t)wcb;
  uint8_t keyPort   = (wcb == WCB_Number) ? (uint8_t)port : 0;
  int8_t slot = findSlotByMaestroIDPortTarget(maestroID, keyPort, keyRemote);

  if (slot < 0) {
    Serial.printf("Maestro M%d:W%dS%d is not configured\n", maestroID, wcb, port);
    return;
  }
  _clearMaestroSlot(slot);
  saveMaestroSettings();
  Serial.printf("Cleared Maestro M%d:W%dS%d (freed slot %d)\n",
                maestroID, wcb, port, slot + 1);
}


void clearAllMaestroConfigs() {
  // Format: ?MAESTRO_DEFAULT
  
  // Track which ports need broadcast re-enabling
  bool portsUsed[5] = {false, false, false, false, false};
  
  // Identify which ports were being used
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    if (maestroConfigs[i].configured && maestroConfigs[i].serialPort > 0) {
      portsUsed[maestroConfigs[i].serialPort - 1] = true;
    }
  }
  
  // Clear all configs
  for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
    maestroConfigs[i].configured = false;
    maestroConfigs[i].maestroID = 0;
    maestroConfigs[i].serialPort = 0;
    maestroConfigs[i].remoteWCB = 0;
  }
  
  saveMaestroSettings();
  Serial.println("All Maestro configurations cleared");
  Serial.println("Reverted to legacy routing (Maestro ID = WCB Number on S1)");
  
  // **AUTO RE-ENABLE BROADCAST ON ALL FREED PORTS**
  Serial.println("\nRe-enabling broadcasts on freed ports:");
  for (int i = 0; i < 5; i++) {
    if (portsUsed[i]) {
      // Re-enable output
      if (!serialBroadcastEnabled[i]) {
        serialBroadcastEnabled[i] = true;
        Serial.printf("  ✓ S%d output enabled\n", i + 1);
      }
      
      // Re-enable input
      if (blockBroadcastFrom[i]) {
        blockBroadcastFrom[i] = false;
        Serial.printf("  ✓ S%d input enabled\n", i + 1);
      }
    }
  }
  // **RESET BAUD RATES ON ALL FREED PORTS** (once — was nested in the loop above)
  Serial.println("Resetting baud rates on freed ports:");
  for (int i = 0; i < 5; i++) {
    if (portsUsed[i]) {
      updateBaudRate(i + 1, 9600);
      Serial.printf("  ✓ S%d baud rate reset to 9600\n", i + 1);
    }
  }
  
  saveBroadcastSettingsToPreferences();
  saveBroadcastBlockSettings();
}

void printMaestroBackup(String &chainedConfig, String &chainedConfigDefault,
                        char delimiter, bool printToSerial,
                        const String &defSep, const String &defFunc) {
    bool anyActive = false;
    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
        if (maestroConfigs[i].configured) {
            anyActive = true;
            break;
        }
    }
    if (!anyActive) return;

    for (int i = 0; i < MAX_MAESTROS_PER_WCB; i++) {
        if (!maestroConfigs[i].configured) continue;

        // Build the suffix WITHOUT a hardcoded '?' so both chains can apply
        // the correct prefix (live funcChar vs factory-chain func id).
        String suffix = "MAESTRO,M" + String(maestroConfigs[i].maestroID);

        // Decide local-vs-remote by remoteWCB (the slot's true identity key —
        // same rule configure/clear use), NOT by serialPort. Keying on serialPort
        // let a malformed remote-to-self slot emit as a local-looking line that
        // CLEAR could never match. (normalizeMaestroSelfSlots() also purges those
        // at boot; this keeps emit and CLEAR in agreement regardless.)
        if (maestroConfigs[i].remoteWCB == 0) {
            suffix += ":W" + String(WCB_Number) +
                      "S" + String(maestroConfigs[i].serialPort) +
                      ":" + String(maestroConfigs[i].baudRate);
        } else {
            // Remote — look up actual port from kyberTargets
            int targetPort = 1;  // fallback
            for (int j = 0; j < MAX_KYBER_TARGETS; j++) {
                if (kyberTargets[j].enabled &&
                    kyberTargets[j].maestroID == maestroConfigs[i].maestroID) {
                    targetPort = kyberTargets[j].targetPort;
                    break;
                }
            }
            suffix += ":W" + String(maestroConfigs[i].remoteWCB) +
                      "S" + String(targetPort) +
                      ":" + String(maestroConfigs[i].baudRate);
        }

        String cmd = String(LocalFunctionIdentifier) + suffix;
        if (printToSerial) Serial.println(cmd);
        chainedConfig += String(delimiter) + cmd;
        chainedConfigDefault += defSep + defFunc + suffix;
    }
}




