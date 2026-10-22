#pragma once
// ════════════════════════════════════════════════════════════════════════════
//  WCB_ChanSync — channel sync: the whole mesh on one radio channel.
//  Design: docs/CHANNEL_SYNC.md. Wire format and the pure decisions (leader
//  ordering, judgeMove, shouldSearch, judgeSearch): WCB_ChanSyncWire.h, which
//  WCB_Client ships byte-identical. This file is the board's side of it: its
//  key, who it hears, the MOVE countdown, the search, the replies, ?CHSYNC and
//  ?REGION.
//
//  ── THREADS ────────────────────────────────────────────────────────────────
//  chanSyncOnFrame() runs on the WiFi task (espNowReceiveCallback): it copies
//  the frame into a queue and returns. Everything else — every decision, print,
//  send, channel switch and NVS write — runs in loop(), in chanSyncTick()
//  (CLAUDE.md rule 11). chanSyncRadioAway() is the one flag the receive path
//  reads, a volatile bool.
//
//  ── THE RADIO AWAY FROM HOME ───────────────────────────────────────────────
//  A search visits the other channels, ~150 ms each, and a JOIN board's
//  full-band scan does the same (WCB_WiFi.cpp). While the radio is away,
//  chanSyncRadioAway() is true: the receive path drops everything but the
//  channel-sync frames (a heartbeat heard there would mark a missing board
//  online for 55 s and clear the very condition that started the search), and
//  loop() holds the heartbeat, the WDP advert and ETM retries, which would go
//  out on a channel where nobody they are for can hear them.
// ════════════════════════════════════════════════════════════════════════════

#include <Arduino.h>
#include "WCB_ChanSyncWire.h"

// ---- Boot (setup(), in this order) -----------------------------------------
void    chanSyncLoadSettings();        // NVS wcb_config: chsync (on/off), region. With the other loads.
// After WiFi.mode(WIFI_STA), BEFORE esp_wifi_set_channel(meshChannel) and wcbWifiStart(): applies the saved
// region (no country call at all for "01", the driver default) and reads its top channel. A saved meshChannel
// above it falls back to WCB_MESH_CHANNEL_DEFAULT for this boot, loudly, and is NOT overwritten in NVS: fixing
// the region alone brings the saved channel back at the next boot. Returns the region's top channel.
uint8_t chanSyncApplyRegionAtBoot();
void    chanSyncBegin();               // after esp_now_init(): the home channel, the frame queue

// ---- loop() ------------------------------------------------------------------
void    chanSyncTick();

// ---- Receive path (WiFi task) ----------------------------------------------------
// The radio is visiting another channel (a search hop, a pull, or a JOIN full-band scan).
bool    chanSyncRadioAway();
// A MOVE / PROBE / PROBE_REPLY frame (chansync::PT_*) from senderId, already password-, id- and MAC-checked.
// Copies the payload and returns: never decides, prints or sends.
void    chanSyncOnFrame(uint8_t packetType, int senderId, const uint8_t *payload);

// ---- WDP (WCB_WDP.cpp) -------------------------------------------------------------
int     chanSyncPutTlv(uint8_t *buf, int o, int max);   // TLV 0x14 CHAN into this board's advert
void    chanSyncSelfFacts(chansync::ChanFacts &f);      // this board's own CHAN facts ([WDPCH:...] self row)

// ---- State ---------------------------------------------------------------------------
bool    chanSyncEnabled();             // ?CHSYNC,ON (the default)
bool    chanSyncBusy();                // a search or a move is in progress: no JOIN scan, no drift warning
uint8_t chanSyncHomeChannel();         // the channel the radio is on when it is home
uint8_t chanSyncMaxChannel();          // the applied region's top channel (<= 13)
// ?WCBCH's upper bound: the applied region's top channel, or 13 when the SAVED region differs from the applied
// one (*regionPending = true): the next boot applies that region and checks the channel against it.
uint8_t chanSyncWcbchLimit(bool *regionPending);
const char *chanSyncSavedRegion();     // the region ?REGION saved (what a config pull / ?backup carries)

// ---- WiFi JOIN follows its network (WCB_WiFi.cpp, docs/CHANNEL_SYNC.md §5) ---------
enum ChanSyncJoinVerdict : uint8_t {
  CSJ_GO = 0,            // a MOVE to the channel is announced; the JOIN board associates there once it lands
  CSJ_SYNC_OFF,          // channel sync is off here (or ETM is): stay, as before
  CSJ_OUT_OF_REGION,     // the channel is outside this board's region
  CSJ_PINNED_OUTRANKED,  // a pinned device this board hears outranks it: two networks cannot both be kept
  CSJ_BUSY,              // a search or another move is in progress: decide again at the next scan
};
// The JOIN board found its network on channel ch, not the mesh's. *outrankedBy names the pinned device for
// CSJ_PINNED_OUTRANKED.
ChanSyncJoinVerdict chanSyncJoinFollow(uint8_t ch, uint8_t *outrankedBy);

// ---- Commands ------------------------------------------------------------------------
//   ?CHSYNC                  status + [CHSYNC:EN=,CH=,MAXCH=,CC=,RANK=,PIN=,LEAD=,LRANK=,LPIN=,STATE=]
//   ?CHSYNC,ON | ?CHSYNC,OFF take part or not (NVS, live)
//   ?CHSYNC,SEARCH           one search now, if this board can hop
//   ?CHSYNC,MOVE,<ch>        operator move of the whole mesh
//   ?REGION[,<cc>]           show, or set for the next boot
void    processChanSyncCommand(const String &args);
void    processRegionCommand(const String &args);
