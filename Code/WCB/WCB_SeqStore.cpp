#include "WCB_RemoteTerm.h"  // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_SeqStore.h"
#include "WCB_Storage.h"     // SEQ_KEY_MAX_LEN, seqKeyReserved
#include <Preferences.h>
#include <nvs.h>             // nvsRead: a string through the heap
#include <vector>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <sys/stat.h>
#include "esp_heap_caps.h"
#include "esp_partition.h"
#include "esp_system.h"      // esp_reset_reason
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_littlefs.h"

extern Preferences preferences;

// The partition, where it is mounted, and the two files. "spiffs" is the min_spiffs table's 128 KB data partition at
// 0x3D0000 (Code/bin/build.sh builds every target with PartitionScheme=min_spiffs). Driven through the IDF calls, not
// the Arduino LittleFS object: they say WHY a mount failed (no partition, no memory, not a file system yet), and a
// POSIX file needs no stdio buffer.
#define SEQ_PARTITION       "spiffs"
#define SEQ_BASE            "/seqfs"
#define SEQ_PATH            SEQ_BASE "/seqs"
#define SEQ_TMP_PATH        SEQ_BASE "/seqs.tmp"
#define SEQ_NVS_NS          "stored_cmds"
#define SEQ_IDLE_UNMOUNT_MS 3000     // unmount once unused this long; a config push keeps it mounted
#define SEQ_MIN_FREE_HEAP   6144     // refuse to mount below this much free heap

// RTC words, kept over every reset but a power-on (like the boot counter in WCB.ino). The core builds LittleFS with
// its asserts on, so a damaged file system can abort() inside a mount - and every later boot would mount it and crash
// the same way. seqMountGuard holds SEQ_GUARD_MOUNTING while a mount runs; a boot that finds it still set after a crash
// runs on the NVS fallback instead of mounting again. seqForceNvs is ?DEBUG,SEQNVS.
#define SEQ_GUARD_MOUNTING  0x5E0F11A7u
#define SEQ_FORCE_NVS       0x5E0F0A55u
RTC_NOINIT_ATTR static uint32_t seqMountGuard;
RTC_NOINIT_ATTR static uint32_t seqForceNvs;

static SemaphoreHandle_t seqMutex   = nullptr;
static bool        onFile           = false;   // the file backs the store this boot (else NVS)
static bool        mounted          = false;
static bool        partitionMissing = false;   // no "spiffs" partition: nothing to format or clear
static const char *fallbackWhy      = "the store was never started";
static uint32_t    lastUseMs        = 0;
static unsigned    walkDamaged      = 0;       // damaged lines the last walk skipped
static bool        walkEndedClean   = true;    // the last walk to the end found the file ending in a newline

static int errnoCode() {
  switch (errno) {
    case ENOSPC: return SEQ_E_FULL;
    case ENOMEM: return SEQ_E_NOMEM;
    default:     return SEQ_E_IO;
  }
}

namespace {

// Every entry point holds the store (recursively: a ForEach callback may read it again) and restarts the idle clock.
struct SeqLock {
  SeqLock()  { if (seqMutex) xSemaphoreTakeRecursive(seqMutex, portMAX_DELAY); }
  ~SeqLock() { lastUseMs = millis(); if (seqMutex) xSemaphoreGiveRecursive(seqMutex); }
};

// Reads /seqs a record at a time, with no Stream timeouts (Stream::readString waits a whole second at the end of a
// file). A damaged line - no comma, or an empty key or one longer than SEQ_KEY_MAX_LEN - is skipped and counted: no
// save writes one, and a record no key can address cannot be kept. A rewrite drops them.
class SeqReader {
public:
  explicit SeqReader(int fd) : fd_(fd) {}
  // The next record's key into key[SEQ_KEY_MAX_LEN + 1]. 1: a key, and value() or skipValue() must follow;
  // 0: the end of the file; -1: a read error.
  int nextKey(char *key) {
    size_t n = 0;
    bool bad = false;
    for (;;) {
      const int c = get();
      if (c == -2) return -1;
      if (c == -1) {                  // the end: a last line with no comma is damaged
        if (n || bad) damaged_++;
        return 0;
      }
      if (c == '\n') {                // a line with no comma
        damaged_++;
        n = 0;
        bad = false;
        continue;
      }
      if (c == ',') {
        if (n == 0 || bad) {          // an empty or over-long key: skip the line
          damaged_++;
          if (!skipValue()) return -1;
          n = 0;
          bad = false;
          continue;
        }
        key[n] = '\0';
        return 1;
      }
      if (n < SEQ_KEY_MAX_LEN) key[n++] = (char)c;
      else bad = true;
    }
  }
  // The current record's value, a piece at a time - fn(p, n) - up to the end of its line. false on a read error.
  template <typename F> bool value(F fn) {
    for (;;) {
      if (pos_ >= len_ && !fill()) return !err_;
      const char *p = buf_ + pos_;
      const size_t avail = len_ - pos_;
      const char *nl = (const char *)memchr(p, '\n', avail);
      const size_t take = nl ? (size_t)(nl - p) : avail;
      if (take) fn(p, take);
      pos_ += take;
      if (nl) {
        pos_++;
        return true;
      }
    }
  }
  bool skipValue() { return value([](const char *, size_t) {}); }
  unsigned damaged() const { return damaged_; }
  bool endsWithNewline() const { return last_ == 0 || last_ == '\n'; }   // valid once the end is reached

private:
  bool fill() {
    if (eof_) return false;
    const ssize_t r = ::read(fd_, buf_, sizeof(buf_));
    if (r <= 0) {
      eof_ = true;
      err_ = r < 0;
      return false;
    }
    len_ = (size_t)r;
    pos_ = 0;
    last_ = buf_[r - 1];
    return true;
  }
  int get() {
    if (pos_ >= len_ && !fill()) return err_ ? -2 : -1;
    return (uint8_t)buf_[pos_++];
  }
  int fd_;
  char buf_[256];
  size_t len_ = 0, pos_ = 0;
  bool eof_ = false, err_ = false;
  char last_ = 0;                     // the last byte read: at the end, the file's last byte (0 for an empty file)
  unsigned damaged_ = 0;
};

// Writes to an open file and keeps the first failure. LittleFS commits a file only at close, and not at all once a
// write to it failed, so a failed write leaves the file as it was.
struct SeqWriter {
  explicit SeqWriter(int fd) : fd_(fd) {}
  void put(const char *p, size_t n) {
    while (code == SEQ_OK && n) {
      const ssize_t w = ::write(fd_, p, n);
      if (w <= 0) {
        code = w < 0 ? errnoCode() : SEQ_E_IO;
        return;
      }
      p += w;
      n -= (size_t)w;
    }
  }
  void put(const char *s) { put(s, strlen(s)); }
  void put(const String &s) { put(s.c_str(), s.length()); }
  void ch(char c) { put(&c, 1); }
  int code = SEQ_OK;

private:
  int fd_;
};

}  // namespace

// ── Mounting ──────────────────────────────────────────────────────────────────
static esp_err_t registerFs() {
  esp_vfs_littlefs_conf_t conf = {};
  conf.base_path = SEQ_BASE;
  conf.partition_label = SEQ_PARTITION;
  conf.format_if_mount_failed = false;   // only seqStoreBegin() formats, and says so
  conf.grow_on_mount = true;
  seqMountGuard = SEQ_GUARD_MOUNTING;
  const esp_err_t err = esp_vfs_littlefs_register(&conf);
  seqMountGuard = 0;
  return err;
}

static int mountLocked() {
  if (mounted) return SEQ_OK;
  if (heap_caps_get_free_size(MALLOC_CAP_8BIT) < SEQ_MIN_FREE_HEAP) return SEQ_E_NOMEM;
  const esp_err_t err = registerFs();
  if (err == ESP_ERR_NO_MEM) return SEQ_E_NOMEM;
  if (err != ESP_OK) return SEQ_E_IO;
  mounted = true;
  return SEQ_OK;
}

static void unmountLocked() {
  if (!mounted) return;
  esp_vfs_littlefs_unregister(SEQ_PARTITION);
  mounted = false;
}

// A format erases the whole 128 KB partition first. The core's own LittleFS.format() holds core 0's task watchdog off
// for it, and so does this: every board formats once, at the first boot of this firmware (about a second).
static esp_err_t formatStore() {
  const bool wdt = disableCore0WDT();
  const esp_err_t err = esp_littlefs_format(SEQ_PARTITION);
  if (wdt) enableCore0WDT();
  return err;
}

// Does the partition hold a LittleFS at all? Its superblock carries "littlefs" 8 bytes into block 0 or 1. Only picks
// the words of the boot message when a mount fails: a first use, or a store that could not be read.
static bool partitionHasLittlefs() {
  const esp_partition_t *p = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, SEQ_PARTITION);
  if (!p) return false;
  char head[16];
  for (uint32_t off = 0; off <= 4096 && off + sizeof(head) <= p->size; off += 4096)
    if (esp_partition_read(p, off, head, sizeof(head)) == ESP_OK && memcmp(head + 8, "littlefs", 8) == 0) return true;
  return false;
}

// ── The file ──────────────────────────────────────────────────────────────────
// fn(key, reader) for each record of /seqs. fn consumes the value (value() or skipValue()) and returns 0 to go on,
// 1 to stop, or a SEQ_E_* code. No file is an empty store.
template <typename F> static int walkFile(F fn) {
  walkDamaged = 0;
  walkEndedClean = true;
  const int fd = ::open(SEQ_PATH, O_RDONLY);
  if (fd < 0) return errno == ENOENT ? SEQ_OK : errnoCode();
  SeqReader r(fd);
  char key[SEQ_KEY_MAX_LEN + 1];
  int rc = SEQ_OK;
  for (;;) {
    const int n = r.nextKey(key);
    if (n < 0) {
      rc = SEQ_E_IO;
      break;
    }
    if (n == 0) {
      walkEndedClean = r.endsWithNewline();
      break;
    }
    const int s = fn(key, r);
    if (s != 0) {
      if (s < 0) rc = s;
      break;
    }
  }
  walkDamaged = r.damaged();
  ::close(fd);
  return rc;
}

static size_t fileSize() {
  struct stat st;
  return ::stat(SEQ_PATH, &st) == 0 ? (size_t)st.st_size : 0;
}

static int fileGet(const char *key, String &value) {
  int result = SEQ_NOTFOUND;
  const int rc = walkFile([&](const char *k, SeqReader &r) -> int {
    if (strcmp(k, key) != 0) return r.skipValue() ? 0 : SEQ_E_IO;
    bool ok = true;
    if (!r.value([&](const char *p, size_t n) { if (ok) ok = value.concat(p, n); })) return SEQ_E_IO;
    result = ok ? SEQ_OK : SEQ_E_NOMEM;
    return 1;
  });
  if (rc != SEQ_OK) result = rc;
  if (result != SEQ_OK) value = String();
  return result;
}

// Where key is, how long its value is, and whether that value equals *compare.
struct SeqFound {
  bool found = false;
  bool same = false;
  size_t oldLen = 0;
};

static int fileFind(const char *key, const String *compare, SeqFound &f) {
  return walkFile([&](const char *k, SeqReader &r) -> int {
    if (strcmp(k, key) != 0) return r.skipValue() ? 0 : SEQ_E_IO;
    f.found = true;
    const char *cv = compare ? compare->c_str() : "";
    const size_t cl = compare ? compare->length() : 0;
    size_t pos = 0;
    bool eq = compare != nullptr;
    if (!r.value([&](const char *p, size_t n) {
          if (eq && (pos + n > cl || memcmp(cv + pos, p, n) != 0)) eq = false;
          pos += n;
        }))
      return SEQ_E_IO;
    f.oldLen = pos;
    f.same = eq && pos == cl;
    return 1;
  });
}

static int fileAppend(const String &key, const String &value, bool newlineFirst) {
  const int fd = ::open(SEQ_PATH, O_WRONLY | O_CREAT | O_APPEND, 0666);
  if (fd < 0) return errnoCode();
  SeqWriter w(fd);
  if (newlineFirst) w.ch('\n');       // a file that does not end in one (never written so, but never glue two lines)
  w.put(key);
  w.ch(',');
  w.put(value);
  w.ch('\n');
  const int c = ::close(fd);
  if (w.code != SEQ_OK) return w.code;
  return c == 0 ? SEQ_OK : errnoCode();
}

// Writes /seqs to /seqs.tmp - key's record replaced by newValue, or left out when newValue is null, and any damaged
// line or second record for key dropped - then renames it over /seqs.
static int fileRewrite(const char *key, const String *newValue) {
  const int in = ::open(SEQ_PATH, O_RDONLY);
  if (in < 0) return errno == ENOENT ? SEQ_NOTFOUND : errnoCode();
  const int out = ::open(SEQ_TMP_PATH, O_WRONLY | O_CREAT | O_TRUNC, 0666);
  if (out < 0) {
    const int e = errnoCode();
    ::close(in);
    return e;
  }
  SeqReader r(in);
  SeqWriter w(out);
  char k[SEQ_KEY_MAX_LEN + 1];
  bool hit = false;
  int rc = SEQ_OK;
  for (;;) {
    const int n = r.nextKey(k);
    if (n < 0) {
      rc = SEQ_E_IO;
      break;
    }
    if (n == 0) break;
    if (strcmp(k, key) == 0) {
      if (!r.skipValue()) {
        rc = SEQ_E_IO;
        break;
      }
      if (newValue && !hit) {
        w.put(k);
        w.ch(',');
        w.put(*newValue);
        w.ch('\n');
      }
      hit = true;
    } else {
      w.put(k);
      w.ch(',');
      if (!r.value([&](const char *p, size_t len) { w.put(p, len); })) {
        rc = SEQ_E_IO;
        break;
      }
      w.ch('\n');
    }
    if (w.code != SEQ_OK) break;
  }
  ::close(in);
  if (rc == SEQ_OK) rc = w.code;
  const int c = ::close(out);
  if (rc == SEQ_OK && c != 0) rc = errnoCode();
  if (rc == SEQ_OK && !hit) rc = SEQ_NOTFOUND;
  if (rc == SEQ_OK && ::rename(SEQ_TMP_PATH, SEQ_PATH) != 0) rc = errnoCode();
  if (rc != SEQ_OK) ::unlink(SEQ_TMP_PATH);
  return rc;
}

static int filePut(const String &key, const String &value) {
  SeqFound f;
  const int rc = fileFind(key.c_str(), &value, f);
  if (rc != SEQ_OK) return rc;
  if (f.found && f.same) return SEQ_OK;   // a re-pushed config: nothing to write
  const size_t size = fileSize();
  const size_t grown = f.found ? size - f.oldLen + value.length() : size + key.length() + value.length() + 2;
  if (grown > SEQ_FILE_MAX) return SEQ_E_FULL;
  if (f.found) return fileRewrite(key.c_str(), &value);
  return fileAppend(key, value, !walkEndedClean);   // the find walked to the end: walkEndedClean is this file's
}

static int fileClear() {
  ::unlink(SEQ_TMP_PATH);
  if (::unlink(SEQ_PATH) == 0 || errno == ENOENT) return SEQ_OK;
  // It would not go: start the store over.
  unmountLocked();
  return formatStore() == ESP_OK ? SEQ_OK : SEQ_E_IO;
}

static void fnv(uint32_t &h, const char *p, size_t n) {
  for (size_t i = 0; i < n; i++) {
    h ^= (uint8_t)p[i];
    h *= 16777619u;
  }
}
static void fnvSep(uint32_t &h) {   // record separator - "ab"+"c" != "a"+"bc"
  h ^= 0xFF;
  h *= 16777619u;
}

static int fileHash(uint32_t &h) {
  h = 2166136261u;
  const int rc = walkFile([&](const char *k, SeqReader &r) -> int {   // the key list, "k1,k2,...,", as NVS kept it
    fnv(h, k, strlen(k));
    fnv(h, ",", 1);
    return r.skipValue() ? 0 : SEQ_E_IO;
  });
  if (rc != SEQ_OK) return rc;
  fnvSep(h);
  return walkFile([&](const char *, SeqReader &r) -> int {            // then every value, in the same order
    if (!r.value([&](const char *p, size_t n) { fnv(h, p, n); })) return SEQ_E_IO;
    fnvSep(h);
    return 0;
  });
}

// ── The NVS fallback: the layout every firmware before the file store used ────
bool seqListHas(const String &list, const String &key) {
  if (key.length() == 0) return false;
  const String entry = key + ",";
  return list.startsWith(entry) || list.indexOf("," + entry) != -1;
}

// One stored_cmds string into out: SEQ_OK (an empty string is OK too), SEQ_NOTFOUND, SEQ_E_NOMEM or SEQ_E_IO. Read
// through the heap, not Preferences::getString: that copies a value through a stack array its own length (up to 4 KB),
// and the boot move reads these from inside a file walk, from setup() - an overflow there would boot-loop the board,
// and the mount guard would not see it.
static int nvsRead(const char *key, String &out) {
  out = String();
  nvs_handle_t h;
  if (nvs_open(SEQ_NVS_NS, NVS_READONLY, &h) != ESP_OK) return SEQ_NOTFOUND;   // no namespace yet
  size_t len = 0;
  int rc = SEQ_NOTFOUND;
  if (nvs_get_str(h, key, nullptr, &len) == ESP_OK) {
    rc = SEQ_OK;
    if (len > 1) {
      char *buf = (char *)malloc(len);
      if (!buf) rc = SEQ_E_NOMEM;
      else if (nvs_get_str(h, key, buf, &len) != ESP_OK) rc = SEQ_E_IO;
      else if (!out.concat(buf, strlen(buf))) rc = SEQ_E_NOMEM;
      free(buf);
      if (rc != SEQ_OK) out = String();
    }
  }
  nvs_close(h);
  return rc;
}

static int nvsKeyList(String &list) {
  const int rc = nvsRead("key_list", list);
  return rc == SEQ_NOTFOUND ? SEQ_OK : rc;         // no list: no sequences
}

static int nvsGet(const String &key, String &value) { return nvsRead(key.c_str(), value); }

// The value of the NVS entry for key, or an empty String when there is none or it could not be held.
static String nvsValue(const String &key) {
  String v;
  nvsRead(key.c_str(), v);
  return v;
}

static int nvsPut(const String &key, const String &value) {
  // The list first, so a list that cannot be read or grown changes nothing.
  String keys;
  const int lrc = nvsKeyList(keys);
  if (lrc != SEQ_OK) return lrc;
  const bool listed = seqListHas(keys, key);
  if (!listed && !(keys.concat(key) && keys.concat(','))) return SEQ_E_NOMEM;
  preferences.begin(SEQ_NVS_NS, false);
  if (!preferences.putString(key.c_str(), value)) {
    preferences.end();
    return SEQ_E_FULL;
  }
  // Checked (HIL_TEST_AUDIT.md F9): on a full NVS this write fails, and the value just stored would be a sequence no
  // list names - never shown by ?SEQ,NAMES or ?backup, never cleared, and holding NVS space for good. Take the value
  // back out (an erase needs no free space).
  if (!listed && preferences.putString("key_list", keys) != keys.length()) {
    preferences.remove(key.c_str());
    preferences.end();
    return SEQ_E_LIST;
  }
  preferences.end();
  return SEQ_OK;
}

static int nvsRemove(const String &name) {
  String existing;
  const int lrc = nvsKeyList(existing);
  preferences.begin(SEQ_NVS_NS, false);
  // Never by a key longer than SEQ_KEY_MAX_LEN: NVS compares only its first 15 characters, so the remove would delete
  // the shorter key's sequence. The key_list rewrite below still runs: firmware before d83042e listed an over-long key
  // it had failed to store, and erasing by that name is the only way to drop the phantom from the list.
  const bool removed = name.length() <= SEQ_KEY_MAX_LEN && preferences.remove(name.c_str());
  if (lrc != SEQ_OK) {                              // the list could not be read: the value alone went
    preferences.end();
    return removed ? SEQ_E_LIST : lrc;
  }
  existing.trim();
  String updated = "";
  int start = 0;
  while (start < (int)existing.length()) {
    const int comma = existing.indexOf(',', start);
    if (comma == -1) break;
    const String k = existing.substring(start, comma);
    if (k != name) {
      updated += k;
      updated += ',';
    }
    start = comma + 1;
  }
  // Checked (F9). The value goes first on purpose: on a full NVS, removing it is what frees the room this rewrite
  // needs. If the rewrite still fails, the name stays listed with no value (SEQ_E_LIST, said so by the caller).
  const bool listed = updated != existing;
  const bool listOk = !listed || (updated.length() ? preferences.putString("key_list", updated) == updated.length()
                                                   : preferences.remove("key_list"));
  preferences.end();
  if (!listOk) return SEQ_E_LIST;
  return removed ? SEQ_OK : SEQ_NOTFOUND;
}

// Every sequence NVS holds, and the key list with them. seq_mig_done is stamped again: clear() takes it too, and
// without it the legacy migration re-armed and re-imported CMD1..CMD80 from "stored_commands" on the next boot,
// resurrecting what was just deleted - and a boot with the file but no marker takes the file for a factory reset.
static int nvsClear() {
  preferences.begin(SEQ_NVS_NS, false);
  const bool ok = preferences.clear();
  preferences.putBool("seq_mig_done", true);
  preferences.end();
  return ok ? SEQ_OK : SEQ_E_IO;
}

static int nvsForEach(const std::function<bool(const char *key, const String &value)> &fn) {
  String keys;
  const int rc = nvsKeyList(keys);
  if (rc != SEQ_OK) return rc;
  int start = 0;
  while (start < (int)keys.length()) {
    int ci = keys.indexOf(',', start);
    if (ci == -1) ci = keys.length();
    String k = keys.substring(start, ci);
    start = ci + 1;
    if (k.c_str() == nullptr) {       // a key that could not be held: lost, as its value would be
      if (!fn("", String())) break;
      continue;
    }
    k.trim();
    if (k.length() == 0) continue;
    if (!fn(k.c_str(), nvsValue(k))) break;
  }
  return SEQ_OK;
}

static int nvsHash(uint32_t &h) {
  String keys;
  const int rc = nvsKeyList(keys);
  if (rc != SEQ_OK) return rc;
  h = 2166136261u;
  fnv(h, keys.c_str(), keys.length());
  fnvSep(h);
  int start = 0;
  while (start < (int)keys.length()) {
    int ci = keys.indexOf(',', start);
    if (ci == -1) ci = keys.length();
    String k = keys.substring(start, ci);
    start = ci + 1;
    if (k.c_str() == nullptr) return SEQ_E_NOMEM;
    k.trim();
    if (k.length() == 0) continue;
    String v;
    const int vrc = (k.length() <= SEQ_KEY_MAX_LEN) ? nvsRead(k.c_str(), v) : SEQ_NOTFOUND;
    if (vrc < 0) return vrc;          // never hash (and cache) a value that could not be read
    fnv(h, v.c_str(), v.length());
    fnvSep(h);
  }
  return SEQ_OK;
}

// ── Moving NVS sequences into the file (every boot that mounts) ───────────────
static bool valueFitsFile(const String &v) {
  return v.length() > 0 && v.length() <= SEQ_VALUE_MAX && v.indexOf('\n') < 0 && v.indexOf('\r') < 0;
}

// One rewrite of /seqs with every sequence NVS lists: a key the file has takes NVS's value in its place, the rest go
// last in NVS order. Each moved key is read back from the file before NVS lets go of it, so a reset anywhere in here
// loses nothing - the next boot moves again. A listed name with no value (a phantom F9 could leave) is dropped.
static void moveNvsSequences() {
  String keys;
  if (nvsKeyList(keys) != SEQ_OK || keys.length() == 0) return;

  struct Moving {
    String key;
    bool phantom = false;    // listed with no value, or a name no sequence can have: dropped from the list
    bool written = false;    // in the new file
    bool verified = false;   // read back from it
  };
  std::vector<Moving> mv;
  int start = 0;
  while (start < (int)keys.length()) {
    int ci = keys.indexOf(',', start);
    if (ci == -1) ci = keys.length();
    String k = keys.substring(start, ci);
    start = ci + 1;
    k.trim();
    if (k.length() == 0) continue;
    bool dup = false;
    for (const Moving &m : mv) dup |= (m.key == k);
    if (dup) continue;
    Moving m;
    m.key = k;
    m.phantom = k.length() > SEQ_KEY_MAX_LEN || seqKeyReserved(k) || k.indexOf('\n') >= 0 || k.indexOf('\r') >= 0;
    mv.push_back(m);
  }
  auto indexOf = [&mv](const char *k) -> int {
    for (size_t i = 0; i < mv.size(); i++)
      if (mv[i].key.equals(k)) return (int)i;
    return -1;
  };

  // The new file: the old one's records, then what NVS adds.
  const int in = ::open(SEQ_PATH, O_RDONLY);
  if (in < 0 && errno != ENOENT) {
    Serial.printf("[SEQ] Could not read the sequence store (%s): %u sequence(s) stay in the settings store, unused "
                  "this boot.\n", seqStoreError(errnoCode()), (unsigned)mv.size());
    return;
  }
  const int out = ::open(SEQ_TMP_PATH, O_WRONLY | O_CREAT | O_TRUNC, 0666);
  if (out < 0) {
    const int e = errnoCode();
    if (in >= 0) ::close(in);
    Serial.printf("[SEQ] Could not write the sequence store (%s): %u sequence(s) stay in the settings store, unused "
                  "this boot.\n", seqStoreError(e), (unsigned)mv.size());
    return;
  }
  SeqWriter w(out);
  size_t size = 0;
  int rc = SEQ_OK;
  if (in >= 0) {
    SeqReader r(in);
    char k[SEQ_KEY_MAX_LEN + 1];
    for (;;) {
      const int n = r.nextKey(k);
      if (n < 0) rc = SEQ_E_IO;
      if (n <= 0) break;
      const int i = indexOf(k);
      String nv;
      if (i >= 0 && !mv[i].written && !mv[i].phantom) {
        nv = nvsValue(mv[i].key);
        if (!valueFitsFile(nv)) nv = String();   // NVS's copy cannot go in: the file's stays (NVS's is dealt with below)
      }
      if (i >= 0 && mv[i].written) {             // a second record for a key already written: drop it
        if (!r.skipValue()) rc = SEQ_E_IO;
      } else if (nv.length()) {                  // NVS wins: its value in this record's place
        if (!r.skipValue()) rc = SEQ_E_IO;
        w.put(k);
        w.ch(',');
        w.put(nv);
        w.ch('\n');
        size += strlen(k) + nv.length() + 2;
        mv[i].written = true;
      } else {
        size_t len = 0;
        w.put(k);
        w.ch(',');
        if (!r.value([&](const char *p, size_t n2) {
              w.put(p, n2);
              len += n2;
            }))
          rc = SEQ_E_IO;
        w.ch('\n');
        size += strlen(k) + len + 2;
      }
      if (rc != SEQ_OK || w.code != SEQ_OK) break;
    }
    ::close(in);
  }
  unsigned dropped = 0, stayed = 0;
  for (Moving &m : mv) {
    if (rc != SEQ_OK || w.code != SEQ_OK) break;
    if (m.written || m.phantom) continue;
    const String nv = nvsValue(m.key);
    if (nv.length() == 0) {                      // listed with no value
      m.phantom = true;
      continue;
    }
    if (!valueFitsFile(nv)) {
      Serial.printf("[SEQ] '%s' cannot move into the sequence store (%s): it stays in the settings store, unused.\n",
                    m.key.c_str(), nv.length() > SEQ_VALUE_MAX ? "over 3999 characters" : "it holds a line break");
      stayed++;
      continue;
    }
    if (size + m.key.length() + nv.length() + 2 > SEQ_FILE_MAX) {
      Serial.printf("[SEQ] '%s' does not fit in the sequence store: it stays in the settings store, unused.\n",
                    m.key.c_str());
      stayed++;
      continue;
    }
    w.put(m.key);
    w.ch(',');
    w.put(nv);
    w.ch('\n');
    size += m.key.length() + nv.length() + 2;
    m.written = true;
  }
  if (rc == SEQ_OK) rc = w.code;
  const int c = ::close(out);
  if (rc == SEQ_OK && c != 0) rc = errnoCode();
  if (rc == SEQ_OK && ::rename(SEQ_TMP_PATH, SEQ_PATH) != 0) rc = errnoCode();
  if (rc != SEQ_OK) {
    ::unlink(SEQ_TMP_PATH);
    Serial.printf("[SEQ] Could not move the settings store's sequences into the sequence store (%s): they stay there, "
                  "unused this boot, and move at the next boot.\n", seqStoreError(rc));
    return;
  }

  // Read each one back before NVS lets go of it.
  walkFile([&](const char *k, SeqReader &r) -> int {
    const int i = indexOf(k);
    if (i < 0 || !mv[i].written || mv[i].verified) return r.skipValue() ? 0 : SEQ_E_IO;
    const String nv = nvsValue(mv[i].key);
    size_t pos = 0;
    bool eq = nv.length() > 0;
    if (!r.value([&](const char *p, size_t n) {
          if (eq && (pos + n > nv.length() || memcmp(nv.c_str() + pos, p, n) != 0)) eq = false;
          pos += n;
        }))
      return SEQ_E_IO;
    mv[i].verified = eq && pos == nv.length();
    return 0;
  });

  // NVS keeps what did not move (still listed); the rest goes.
  String keep = "";
  unsigned moved = 0;
  preferences.begin(SEQ_NVS_NS, false);
  for (const Moving &m : mv) {
    if (m.verified) {
      preferences.remove(m.key.c_str());
      moved++;
    } else if (m.phantom) {
      dropped++;
      Serial.printf("[SEQ] '%s' was listed with no sequence stored under it: dropped from the list.\n", m.key.c_str());
    } else {
      keep += m.key;
      keep += ',';
    }
  }
  if (keep.length()) preferences.putString("key_list", keep);
  else preferences.remove("key_list");
  preferences.end();
  const unsigned unverified = (unsigned)mv.size() - moved - dropped - stayed;
  Serial.printf("[SEQ] Moved %u sequence(s) from the settings store into the sequence store", moved);
  if (unverified) Serial.printf("; %u did not read back and stay in the settings store, unused", unverified);
  Serial.println(".");
}

// ── Public ────────────────────────────────────────────────────────────────────
static void useNvs(const char *why) {
  onFile = false;
  fallbackWhy = why;
  Serial.printf("[SEQ] Stored sequences are kept in the settings store this boot: %s.\n", why);
}

void seqStoreBegin() {
  if (!seqMutex) seqMutex = xSemaphoreCreateRecursiveMutex();
  SeqLock lock;
  const esp_reset_reason_t why = esp_reset_reason();
  const bool crashed = why == ESP_RST_PANIC || why == ESP_RST_INT_WDT || why == ESP_RST_TASK_WDT || why == ESP_RST_WDT;
  const bool diedMounting = crashed && seqMountGuard == SEQ_GUARD_MOUNTING;
  const bool forced = seqForceNvs == SEQ_FORCE_NVS;
  seqMountGuard = 0;
  seqForceNvs = 0;
  if (diedMounting) {
    useNvs("the board crashed while opening the sequence store last boot. ?SEQ,CLEAR,ALL starts it over (its "
           "sequences are lost; restore them from a backup)");
    return;
  }
  if (forced) {
    useNvs("?DEBUG,SEQNVS asked for it (a test knob); the next boot uses the sequence store again");
    return;
  }

  esp_err_t err = registerFs();
  if (err == ESP_FAIL) {                  // no file system there yet, or one that cannot be read
    if (partitionHasLittlefs())
      Serial.println("[SEQ] The sequence store could not be read - starting it over. Its sequences are lost.");
    else
      Serial.println("[SEQ] Setting up the sequence store (first use)...");
    if (formatStore() == ESP_OK) err = registerFs();
  }
  if (err != ESP_OK) {
    partitionMissing = (err == ESP_ERR_NOT_FOUND);
    useNvs(partitionMissing        ? "this firmware was built without a \"spiffs\" partition"
           : err == ESP_ERR_NO_MEM ? "out of memory"
                                   : "the sequence store would not open");
    return;
  }
  mounted = true;
  onFile = true;
  ::unlink(SEQ_TMP_PATH);                 // a replace or remove cut short by a reset

  // A factory reset that blanks only NVS (the Wizard's) takes seq_mig_done with it; sequences go with the settings.
  preferences.begin(SEQ_NVS_NS, true);
  const bool marker = preferences.isKey("seq_mig_done");
  preferences.end();
  struct stat st;
  if (!marker && ::stat(SEQ_PATH, &st) == 0) {
    ::unlink(SEQ_PATH);
    Serial.println("[SEQ] The settings store was erased since these sequences were saved (a factory reset): "
                   "cleared them too.");
  }
  moveNvsSequences();

  int count = 0;
  walkFile([&count](const char *, SeqReader &r) -> int {
    count++;
    return r.skipValue() ? 0 : SEQ_E_IO;
  });
  Serial.printf("[SEQ] %d stored sequence(s) in the sequence store (%u of %u bytes)\n", count, (unsigned)fileSize(),
                (unsigned)SEQ_FILE_MAX);
  unmountLocked();
}

bool seqStoreOnFile() { return onFile; }

int seqStoreKeyList(String &list) {
  list = "";
  SeqLock lock;
  if (!onFile) return nvsKeyList(list);
  int rc = mountLocked();
  if (rc != SEQ_OK) return rc;
  bool ok = true;
  rc = walkFile([&](const char *k, SeqReader &r) -> int {
    if (ok) ok = list.concat(k) && list.concat(',');
    return r.skipValue() ? 0 : SEQ_E_IO;
  });
  if (rc == SEQ_OK && !ok) rc = SEQ_E_NOMEM;
  if (rc != SEQ_OK) list = String();
  return rc;
}

int seqStoreGet(const String &key, String &value) {
  value = String();
  if (key.length() == 0 || key.length() > SEQ_KEY_MAX_LEN || seqKeyReserved(key)) return SEQ_NOTFOUND;
  SeqLock lock;
  if (!onFile) return nvsGet(key, value);
  const int rc = mountLocked();
  if (rc != SEQ_OK) return rc;
  return fileGet(key.c_str(), value);
}

int seqStorePut(const String &key, const String &value) {
  if (key.length() == 0 || key.length() > SEQ_KEY_MAX_LEN || seqKeyReserved(key) || key.indexOf(',') >= 0 ||
      key.indexOf('\n') >= 0 || key.indexOf('\r') >= 0)
    return SEQ_E_BADVAL;
  if (value.length() > SEQ_VALUE_MAX) return SEQ_E_TOOBIG;
  if (!valueFitsFile(value)) return SEQ_E_BADVAL;
  SeqLock lock;
  if (!onFile) return nvsPut(key, value);
  const int rc = mountLocked();
  if (rc != SEQ_OK) return rc;
  return filePut(key, value);
}

int seqStoreRemove(const String &key) {
  if (key.length() == 0 || seqKeyReserved(key)) return SEQ_NOTFOUND;
  SeqLock lock;
  if (!onFile) return nvsRemove(key);
  if (key.length() > SEQ_KEY_MAX_LEN) return SEQ_NOTFOUND;
  int rc = mountLocked();
  if (rc != SEQ_OK) return rc;
  SeqFound f;
  rc = fileFind(key.c_str(), nullptr, f);
  if (rc != SEQ_OK) return rc;
  if (!f.found) return SEQ_NOTFOUND;
  return fileRewrite(key.c_str(), nullptr);
}

int seqStoreClear() {
  SeqLock lock;
  int rc = SEQ_OK;
  if (onFile) {
    rc = mountLocked();
    if (rc == SEQ_OK) rc = fileClear();
  } else if (!partitionMissing) {
    // NVS backs the store this boot, and the file would be back at the next boot that mounts it: start it over too.
    // A format needs no mount, which is what may be failing.
    unmountLocked();
    if (formatStore() != ESP_OK) rc = SEQ_E_IO;
  }
  const int nrc = nvsClear();             // what a fallback boot or an older firmware left in NVS
  return rc != SEQ_OK ? rc : nrc;
}

int seqStoreForEach(const std::function<bool(const char *key, const String &value)> &fn) {
  SeqLock lock;
  if (!onFile) return nvsForEach(fn);
  const int rc = mountLocked();
  if (rc != SEQ_OK) return rc;
  return walkFile([&](const char *k, SeqReader &r) -> int {
    String v;
    bool ok = true;
    if (!r.value([&](const char *p, size_t n) { if (ok) ok = v.concat(p, n); })) return SEQ_E_IO;
    if (!ok) v = String();                // could not be held: fn sees it empty, which it counts as lost
    return fn(k, v) ? 0 : 1;
  });
}

int seqStoreHash(uint32_t &hash) {
  SeqLock lock;
  if (!onFile) return nvsHash(hash);
  const int rc = mountLocked();
  if (rc != SEQ_OK) return rc;
  return fileHash(hash);
}

void seqStoreService() {
  if (!mounted || !seqMutex || millis() - lastUseMs < SEQ_IDLE_UNMOUNT_MS) return;
  if (xSemaphoreTakeRecursive(seqMutex, 0) != pdTRUE) return;
  if (mounted && millis() - lastUseMs >= SEQ_IDLE_UNMOUNT_MS) unmountLocked();
  xSemaphoreGiveRecursive(seqMutex);
}

void seqStoreReport() {
  SeqLock lock;
  if (!onFile) {
    Serial.printf("Sequences: kept in the settings store this boot (%s)\n", fallbackWhy);
    return;
  }
  const int rc = mountLocked();
  if (rc != SEQ_OK) {
    Serial.printf("Sequences: the sequence store could not be opened (%s)\n", seqStoreError(rc));
    return;
  }
  int count = 0;
  const int wrc = walkFile([&count](const char *, SeqReader &r) -> int {
    count++;
    return r.skipValue() ? 0 : SEQ_E_IO;
  });
  size_t total = 0, used = 0;
  esp_littlefs_info(SEQ_PARTITION, &total, &used);
  Serial.printf("Sequences: %d in the sequence store, %u of %u bytes (partition %s: %u of %u bytes used)%s%s\n", count,
                (unsigned)fileSize(), (unsigned)SEQ_FILE_MAX, SEQ_PARTITION, (unsigned)used, (unsigned)total,
                wrc != SEQ_OK ? " - READ ERROR" : "", walkDamaged ? " - damaged lines skipped" : "");
}

void seqStoreForceNvsNextBoot() { seqForceNvs = SEQ_FORCE_NVS; }

const char *seqStoreError(int code) {
  switch (code) {
    case SEQ_E_FULL:   return onFile ? "the sequence store is full - see ?NVS" : "NVS write rejected";
    case SEQ_E_IO:     return onFile ? "the sequence store could not be read or written" : "NVS could not be read";
    case SEQ_E_NOMEM:  return "out of memory";
    case SEQ_E_TOOBIG: return "over 3999 characters";
    case SEQ_E_BADVAL: return "a sequence is one line, not empty, and a key has no comma";
    case SEQ_E_LIST:   return "NVS could not update the sequence list";
    default:           return "no error";
  }
}
