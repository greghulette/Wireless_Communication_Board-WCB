#pragma once
// ════════════════════════════════════════════════════════════════════════════
//  WCB Sequence Store — where stored sequences (?SEQ,SAVE) live
//
//  ONE FILE, not NVS. The sequences are the lines of /seqs ("<key>,<value>\n", in
//  save order) in a LittleFS on the 128 KB "spiffs" data partition that the
//  min_spiffs table carries and nothing else on a WCB uses. In NVS they shared the
//  20 KB settings store with every other setting (about 50 typical sequences fitted).
//  The file may grow to SEQ_FILE_MAX: a few hundred sequences.
//
//  ONE FILE on purpose. A save of a NEW key appends one line (LittleFS commits a
//  write only at close, so a reset mid-append leaves the file as it was). Replacing
//  or removing a sequence writes the whole file to /seqs.tmp and renames it over
//  /seqs, so a reset leaves the old store or the new one, never half of each. That
//  copy needs room for a second file - one reason SEQ_FILE_MAX is under half the
//  partition: a full store can still remove a sequence. A recall, a ?backup walk and
//  the inventory hash are each one sequential read. The key list keeps the old
//  key_list format ("k1,k2,...,") and order, so the inventory hash peers compare is
//  byte-identical across the move from NVS.
//
//  MOUNTED ONLY WHILE USED. A mount holds ~1.9 KB of heap and an open file ~0.7 KB
//  more: on a classic ESP32 hosting its access point that is a tenth of what is free
//  (CLAUDE.md rule 14). Each call mounts on demand (~12 ms); seqStoreService() in
//  loop() unmounts after SEQ_IDLE_UNMOUNT_MS unused. Below SEQ_MIN_FREE_HEAP free the
//  store refuses to mount (SEQ_E_NOMEM): the radio's buffers come first (rule 16).
//  Call it from task context only - never the ESP-NOW receive callback (rule 11).
//
//  THE SETTINGS STORE STILL DECIDES WHEN SEQUENCES ARE ERASED. A factory reset that
//  blanks only NVS (the Wizard's) takes stored_cmds/seq_mig_done with it, and a boot
//  that finds the file but not that marker clears the file too. ?ERASE,NVS and
//  ?SEQ,CLEAR,ALL clear both.
//
//  NVS FALLBACK. If the partition will not mount at boot (no "spiffs" partition, the
//  last boot crashed while mounting it, or ?DEBUG,SEQNVS asked for it), the store
//  runs on the old NVS layout ("stored_cmds": key_list + one string per key) for that
//  boot. Every boot that mounts moves whatever NVS still lists into the file (NVS
//  wins a key both hold: it only holds one while the file exists when a fallback
//  boot or an older firmware saved it, which is newer), and removes it from NVS once
//  read back from the file.
// ════════════════════════════════════════════════════════════════════════════
#include <Arduino.h>
#include <functional>

enum : int {
  SEQ_OK        = 0,
  SEQ_NOTFOUND  = 1,
  SEQ_E_FULL    = -1,   // no room: the file would pass SEQ_FILE_MAX or the partition is full, or NVS refused it
  SEQ_E_IO      = -2,   // a read or write failed, or the store will not mount
  SEQ_E_NOMEM   = -3,   // the heap could not hold the mount or a value
  SEQ_E_TOOBIG  = -4,   // a value over SEQ_VALUE_MAX
  SEQ_E_BADVAL  = -5,   // an empty value, or a line break in the key or value (the file is one sequence per line)
  SEQ_E_LIST    = -6,   // NVS fallback only: the value was removed but the key list could not be rewritten
};

// What NVS held (4000 bytes with the NUL). Kept, so nothing downstream - a recall's queue reserve, a relayed
// SEQVAL, the Wizard - ever sees a longer sequence than it did.
#define SEQ_VALUE_MAX 3999
// The most the file may hold. Two limits meet here. Replacing or removing a sequence writes a second copy of the file,
// so it must stay under half the 128 KB partition. And a config pull carries at most 16 parts of 2880 bytes (46 KB,
// WCB_ConfigParts.h), with every sequence riding it as a ?SEQ,SAVE token beside every other setting: at 32 KB a full
// store still pulls, where 48 KB answered ERROR TOOBIG. A few hundred typical sequences - NVS held about 50.
#define SEQ_FILE_MAX  (32 * 1024)

void   seqStoreBegin();          // setup(), before migrateOldStoredCommands(): mount, move NVS sequences in, report
bool   seqStoreOnFile();         // false when NVS backs the store this boot
int    seqStoreKeyList(String &list);                        // "k1,k2,...," in save order
int    seqStoreGet(const String &key, String &value);        // SEQ_OK / SEQ_NOTFOUND / SEQ_E_*
int    seqStorePut(const String &key, const String &value);  // a new key goes last; an existing one keeps its place
int    seqStoreRemove(const String &key);                    // SEQ_OK / SEQ_NOTFOUND / SEQ_E_*
int    seqStoreClear();          // every sequence, in the file and in NVS
// fn(key, value) for every sequence in save order; return false to stop. value is empty when it could not be held in
// memory (a stored value never is): that one is lost to the caller. Returns SEQ_OK when the walk reached the end (or
// fn stopped it), else SEQ_E_* - and what fn did not see is lost too. fn must not change the store.
int    seqStoreForEach(const std::function<bool(const char *key, const String &value)> &fn);
// FNV-1a 32 over the key list, then each value, each followed by a 0xFF separator (sequenceInventoryHash).
int    seqStoreHash(uint32_t &hash);
bool   seqListHas(const String &list, const String &key);   // is key an entry of a "k1,k2,...," list
void   seqStoreService();        // loop(): unmount after SEQ_IDLE_UNMOUNT_MS unused
void   seqStoreReport();         // ?NVS: one "Sequences: ..." line
void   seqStoreForceNvsNextBoot();          // ?DEBUG,SEQNVS: the next boot runs on the NVS fallback (a test knob)
const char *seqStoreError(int code);        // a short reason for a SEQ_E_* code, for messages
