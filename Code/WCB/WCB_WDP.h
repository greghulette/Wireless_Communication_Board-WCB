#ifndef WCB_WDP_H
#define WCB_WDP_H

#include <Arduino.h>

// -----------------------------------------------------------------------
// WDP — Wireless Discovery Protocol.  Full design: docs/WDP_DESIGN.md
//
// "CDP/LLDP for WCBs." Every board periodically broadcasts a compact TLV
// advert of its OWN identity + capabilities on the existing ESP-NOW mesh;
// every board decodes adverts into a RAM neighbor table. On top of that
// discovery, WDP now also drives ACTIONABLE auto-config: controller-peer
// auto-adopt, Maestro-remote auto-enable (source-aware, see
// wdpEvaluateMaestroRemote), and dynamic persistent PEER MEMBERSHIP —
// boards heard on the mesh are auto-joined and remembered (see WCB.ino
// learned_peers / addActivePeer).
//
//   Transport : rides the existing 252-byte espnow_struct_message_etm with a
//               new PACKET_TYPE_WDP=12; the TLV payload packs into
//               structCommand[200]. Because it rides the ETM struct + gate,
//               WDP requires ETM enabled.
//   Cadence   : 3x boot burst + a ~60 s periodic backstop.
//   Table     : RAM-only neighbor table, sized MAX_WCB_COUNT, indexed by
//               (WCB number - 1). The WDP on/off + auto-join flags persist to
//               NVS (wdp_cfg); learned-peer MEMBERSHIP persists separately
//               (learned_peers), MAC-octet fingerprinted.
//
//   Commands  : ?WDP,LIST | ?WDP,<n> | ?WDP,DETAIL,<n> | ?WDP,STATUS |
//               ?WDP,DUMP | ?WDP,DA | ?WDP,POLL | ?WDP,ON | ?WDP,OFF |
//               ?WDP,AUTOJOIN[,ON|,OFF] | ?WDP,ADD,<id> | ?WDP,FORGET,<id> |
//               ?WDP,CLEAR
// -----------------------------------------------------------------------

#define WDP_PROTO_VERSION 0x01
#define WDP_MAX_MAESTRO   9      // per-board local Maestro IDs advertised
#define WDP_MAX_WLED      9      // per-board local WLED IDs advertised
#define WDP_MAX_PWMTARGET 10     // remote PWM output (WCB,port) targets advertised

// Capability bitmap (WDP_TLV_CAPFLAGS, uint16 little-endian on the wire).
#define WDP_CAP_HCR         0x0001
#define WDP_CAP_MP3         0x0002
#define WDP_CAP_WLED        0x0004
#define WDP_CAP_KYBER_LOCAL 0x0008
#define WDP_CAP_MAESTRO_REM 0x0010
#define WDP_CAP_PWM         0x0020
#define WDP_CAP_CONTROLLER  0x0040
#define WDP_CAP_MAESTRO_LOC 0x0080

// One learned neighbor. A board is the sole authority for its own facts, so an
// advert REPLACES that board's entry wholesale (dropped facts vanish naturally).
struct WdpNeighbor {
  bool          valid;         // have we ever heard this board?
  bool          confirmed;     // heard a fresh advert within the TTL (else stale)
  uint8_t       wcbNumber;
  char          alias[25];     // <=24 chars + NUL (matches wcb_alias clamp)
  char          fwVer[28];     // firmware version string
  uint8_t       hwVer;         // 0/1/21/23/24/31/32
  uint16_t      capFlags;      // WDP_CAP_* bitmap
  uint8_t       ctrlId;        // controller (special-peer) ID this board links to; 0=none/unknown
  uint8_t       maestroIds[WDP_MAX_MAESTRO];  // this board's local Maestro IDs
  uint8_t       maestroBaudCode[WDP_MAX_MAESTRO]; // baud code per id (WDP_TLV_MAESTRO_CFG); 0xFF = unknown (old advert)
  uint8_t       maestroCount;
  uint8_t       wledIds[WDP_MAX_WLED];        // this board's local WLED IDs
  uint8_t       wledBaudCode[WDP_MAX_WLED];   // baud code per id (WDP_TLV_WLED_CFG); 0xFF = unknown
  uint8_t       wledCount;
  char          portLabels[5][25];            // advertised serial-port (interface) labels; "" = unlabeled
  // ---- Receiver-side PWM auto-config ---------------------------------------
  // Ports on THIS board (1-5) that this neighbor drives as a remote PWM output
  // (decoded from its WDP_TLV_PWMTARGET). We self-configure them as output ports.
  uint8_t       pwmSelfPorts[5];
  uint8_t       pwmSelfCount;
  // ---- Client-device fields (WCB_Client / WDP-DA identity) ----------------
  // A WCB_Client device advertises WDP_TLV_DEVTYPE instead of the WCB fields.
  // When isClient is set, `alias` holds the device's canonical type name and the
  // WCB-only fields (hwVer/capFlags/maestro/portLabels) are unused.
  bool          isClient;      // true = a WCB_Client device, not a WCB
  bool          temporary;     // advertised the WDP "temporary" flag (WDP_ADVFLAG_TEMPORARY)
                               // — adopt as a TEMPORARY peer (live but not persisted), never
                               // as a permanent learned peer. See addTemporaryPeer (WCB.ino).
  char          hwRev[16];     // client hardware revision string ("" = none)
  char          capTags[49];   // client capability tags, space-separated ("" = none)
  unsigned long lastAdvertMs;
};

extern bool wdpEnabled;
extern bool wdpAutoJoin;   // learn regular WCBs from adverts as permanent peers (default ON)

// ---- Lifecycle -----------------------------------------------------------
void wdpBegin();   // load NVS flag, clear the table, arm the advert cadence
void wdpTick();    // loop(): send adverts on schedule + age the table

// ---- Receive (called from the loop-drained WDP queue in WCB.ino) ---------
void wdpOnAdvertReceived(int senderWCB, const uint8_t *structCommand);
void wdpForgetNeighbor(uint8_t id);   // drop a neighbor row so ?WDP,DUMP stops emitting it (temporary eviction)

// ---- Command / query -----------------------------------------------------
void processWdpCommand(const String &args);

// ---- Alias resolution (used by the ;w<alias> command router) --------------
// Returns the WCB number for a UNIQUE alias match in the neighbor table,
// 0 if no board advertises that alias, or -1 if it's ambiguous (>1 match).
// The local board is NOT considered here (the caller matches wcb_alias first).
int wdpResolveAlias(const char *alias);

// ---- Capability routing ---------------------------------------------------
// Returns the WCB number that OWNS a capability bit: the lowest-numbered ONLINE
// board advertising it, self included. 0 if nobody (not even this board) owns it.
// This is generic over any WDP_CAP_* bit, but only the single-owner, ID-LESS
// capabilities route through it today — HCR (;H) and MP3 (;A) — as the LIVE
// fallback when no host is persisted (or a pinned host is offline). WLED and
// Maestro are NOT elected here: they are ID-addressed (;L<id> / ;M<id>) and route
// per-id to the board that advertised that id. See routeStoredOrCap() in WCB.ino.
int wdpCapOwner(uint16_t capBit);

// ---- NVS -----------------------------------------------------------------
void loadWdpSettings();
void saveWdpSettings();

// ---- WDP-DA: serial-attached device announces (@WDP1) --------------------
// A device wired to a WCB serial port periodically sends "@WDP1 {json}" to
// self-identify (see docs/WDP_DEVICE_ANNOUNCE.md). We capture its identity per
// port; it fills this board's advertised port label (when unlabeled) and lists
// under ?WDP,DA. RAM-only, TTL-aged; never persisted.
struct WdpDaDevice {
  bool          present;       // heard an announce within the TTL
  char          type[25];      // device type (from the shared vocabulary)
  char          fw[28];        // device firmware version ("" = none)
  char          hwRev[16];     // hardware revision ("" = none)
  char          capTags[49];   // space-separated capability tags ("" = none)
  unsigned long lastSeenMs;
};

// Handle a serial line that begins with "@WDP" on port (1-5): validate the
// @WDP1 marker, parse the JSON identity, update the per-port record.
void wdpDaHandleLine(int port, const char *line);
// Age out serial devices not heard within the TTL. Call each loop().
void wdpDaTick();
// ?WDP,DA — print this board's detected serial-attached devices.
void wdpDaPrint();
// Detected device type for port (1-5), or "" if none — used to fill the port's
// advertised label when the user hasn't set one.
const char *wdpDaType(int port);

#endif
