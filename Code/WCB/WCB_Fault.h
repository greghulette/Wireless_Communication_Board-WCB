#pragma once
// ════════════════════════════════════════════════════════════════════════════
//  ?DEBUG,FAULT,<NAME> - test knobs (the dispatcher and help: WCB.ino debugFaultCommand, ?DEBUG?)
//
//  Each knob drives ONE failure branch that no bench board reaches on its own (a heap copy that fails, a store that
//  will not read, an NVS that will not open), so the HIL harness can assert what the firmware does there. RAM only,
//  never saved, not in ?backup: a knob fires once and then disarms, or lapses unfired after WCB_FAULT_ARM_MS.
//  ?DEBUG,FAULT,OFF and a reboot clear every one. Unarmed, a knob costs one load and a compare at its site.
// ════════════════════════════════════════════════════════════════════════════
#include <Arduino.h>

#define WCB_FAULT_ARM_MS 60000UL

struct WcbFault {
  volatile uint32_t armedMs = 0;   // millis() | 1 when armed, 0 when off
  void arm(bool on) { armedMs = on ? (millis() | 1) : 0; }
  // True once - the site fails this time - if armed within WCB_FAULT_ARM_MS. Disarms either way.
  bool take() {
    const uint32_t t = armedMs;
    if (!t) return false;
    armedMs = 0;
    return millis() - t < WCB_FAULT_ARM_MS;
  }
  // Armed and not lapsed, without disarming (a fault that has to hold for a while, e.g. JOINNOIP).
  bool active() const {
    const uint32_t t = armedMs;
    return t && millis() - t < WCB_FAULT_ARM_MS;
  }
};

// Defined in WCB.ino, beside the dispatcher. Each comment names the site the knob fails.
extern WcbFault faultTimerMem;    // WCB.ino enqueuePendingTimerChain: a long chain's heap copy
extern WcbFault faultSeqRead;     // WCB_SeqStore.cpp seqStoreKeyList / seqStoreForEachKey / seqStoreGet: the store reads as unreadable
extern WcbFault faultSeqList;     // WCB_SeqStore.cpp nvsRemove: the key-list rewrite after the value went
extern WcbFault faultVarNvs;      // WCB_Variables.cpp saveVarsToNVS: the NVS open
extern WcbFault faultEspNowDown;  // WCB_PWM.cpp canSendESPNow: ESP-NOW reads as not running
extern WcbFault faultJoinChan;    // WCB_WiFi.cpp JOIN: the associated AP reads as off the mesh channel
extern WcbFault faultJoinNoIp;    // WCB_WiFi.cpp JOIN: an association holds no address
extern WcbFault faultChanDrift;   // WCB_WiFi.cpp drift check: the radio is moved off the mesh channel for it
