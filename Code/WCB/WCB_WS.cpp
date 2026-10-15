#include "WCB_RemoteTerm.h"   // Must be first — redirects Serial -> WCBDebugSerial
#include "WCB_WS.h"
#include "WCB_WiFi.h"

#include <esp_http_server.h>
#include <WiFi.h>
#include <errno.h>
#include <esp_heap_caps.h>
#include <freertos/semphr.h>
#include <lwip/sockets.h>     // close() — the session-close hook owns the fd; send(MSG_DONTWAIT) — the sender

// Provided by WCB.ino. Note the NON-CONST reference — the dispatcher may rewrite
// the line in place, so it cannot be handed a temporary.
extern void processSerialCommandHelper(String &data, int sourceID);
extern bool lastReceivedViaESPNOW;
extern bool inSequenceBody;

// ---- Sizing ---------------------------------------------------------------
// Keep in step with httpd_config_t::max_open_sockets in wcbWsBegin(). More
// clients than the server will accept is just dead slots; fewer means a
// connected client the sink never writes to — a silently deaf client.
// Two, not three (Greg, 2026-10-07; HIL tracker #111): with the access point up a classic
// ESP32 has about 17 KB of heap, and three busy sessions took it to within 48 bytes of
// empty. A third client still connects: httpd closes the least recently used session
// to take it (lru_purge_enable in wcbWsBegin()).
static const uint8_t  WS_MAX_CLIENTS = 2;
// See the header: sized for ?OTALOCAL,DATA with a 1024-byte chunk.
static const size_t   WS_LINE_MAX    = 1536;
// drain() runs ONE command per loop() pass. Depth only has to absorb a burst;
// an OTA is ACK-paced, so it never has more than a couple in flight.
static const uint8_t  WS_QUEUE_DEPTH = 6;
// How long the handler waits for room rather than discarding. This is the httpd
// task, so blocking costs latency on other sockets and nothing on the mesh. It
// turns a silent drop into ordinary backpressure, which is what TCP is for.
static const uint32_t WS_ENQUEUE_WAIT_MS = 50;
// Output staging. One flush is roughly one TCP segment.
static const size_t   WS_SINK_BUF    = 2048;
// THE SENDER NEVER BLOCKS (2026-10-14, HIL row ws.burst_closes_live_client). Output goes out with send(MSG_DONTWAIT):
// what a socket takes now is written, the rest waits for a later loop() pass. A client that is slow to take a burst
// loses whole lines (counted, reported like any other tee drop) instead of being closed, and loop() never waits on
// a network write, where the old blocking send waited up to send_wait_timeout per client per flush and closed the
// client at the first failure (a 10-16 KB burst on W1's access point closed a healthy client and stalled loop()
// 3.5 s; a grace window made it 9.4 s). Three limits replace the timeout:
//   WS_PASS_WAIT_MS - inside a print on the loop task, a full sink may wait this long in one loop() pass, in all, for
//     a client to take it before lines drop. A healthy client needs none of it for a ~6 KB ?backup (lwIP queues
//     ~5.7 KB per socket at once); it lets a bigger reply through to a client that is ACKing.
//   WS_FRESH_MS - that wait is only for a client that took bytes this recently: one that has stopped costs nothing.
//   WS_STUCK_MS - a client that owes output and has taken none of it for this long is closed: #113's dead peer when
//     no station-left event says so (JOIN mode, or a station still associated but gone quiet).
static const uint32_t WS_PASS_WAIT_MS = 600;
static const uint32_t WS_FRESH_MS     = 300;
static const uint32_t WS_STUCK_MS     = 10000;
// Below this much free heap the sender hands lwIP no more console output; it waits (or the tee drops lines) until
// ACKs give memory back. Output handed over sits in lwIP's send buffer (up to ~5.7 KB a socket) and the WiFi driver's
// dynamic TX buffers until the client ACKs it - and the driver takes a dynamic RX buffer from the same heap for every
// packet it RECEIVES. A client that stopped reading took W1 to 1.4 KB free (run 20261014-025548): the window update it
// sent when it read again could not be received, it never got another byte and was closed as stuck, and W1's heap
// stayed near 9 KB long enough that the PC could not join the access point for the next test. With the access point
// up and a PC on it W1 has 14-18 KB; the floor leaves the radio and the rest of the firmware about 8 KB of it. PONG and
// CLOSE frames are a few bytes and pass it.
static const size_t   WS_HEAP_FLOOR   = 8192;

// Serial0 is the source id the USB reader uses. Reusing it means a WebSocket
// command takes byte-for-byte the same path as a typed one — same routing, same
// echo behaviour, same debug gating — instead of being a second dialect.
static const int      WS_SOURCE_ID   = 0;

struct WsCmd {
  int  fd;
  char line[WS_LINE_MAX];
};

static QueueHandle_t  wsQueue   = nullptr;
static httpd_handle_t wsServer  = nullptr;

// ---- Output sink ----------------------------------------------------------
static char     sinkBuf[WS_SINK_BUF];
static size_t   sinkLen      = 0;
static bool     sinkDropping = false;
static uint32_t sinkDrops    = 0;     // whole lines dropped; reported from loop()
static uint32_t cmdDrops     = 0;     // commands refused at the queue
static uint32_t stuckCloses  = 0;     // clients closed by WS_STUCK_MS; reported from loop()
// The loop task, captured the first time wcbWsService() runs. wcbWsSinkWrite() may
// only flush inline while running on THIS task — see the note there.
static TaskHandle_t sinkLoopTask = nullptr;
// Guards sinkBuf/sinkLen/sinkDropping. The tee runs on whatever task is printing
// and sinkPump() runs on loop — see the locking note above sinkPump().
static portMUX_TYPE sinkMux = portMUX_INITIALIZER_UNLOCKED;
// What the tee has waited, inline, since wcbWsService() last ran (WS_PASS_WAIT_MS).
static uint32_t passWaitMs = 0;
// Set on the loop task while wcbWsService() prints its drop reports. A report the full sink cannot take is not itself
// counted as a dropped line: with a stalled client it would be, and each report would cause the next, one a pass, for
// as long as the client stayed stalled (HIL run 20261014-021636: twelve '[WS] dropped 1 output line(s)' in 50 ms).
static bool sinkReporting = false;

// ---- The clients and what each still owes --------------------------------
// Every client is sent the same frame: frameHdr, then sinkBuf[0..frameLen). Each keeps its own place in it, because
// a non-blocking send takes what the socket has room for and a client may be part-way through when the next pass
// comes. The frame's bytes stay put until the slowest client has sent them (sinkPump compacts only what all have).
// A PONG is queued per client and sent only BETWEEN data frames: a control frame written into the middle of a data
// frame breaks the stream. That is also why the handler takes the control frames itself (handle_ws_control_frames):
// httpd's own PING reply is a blocking send from its task, which could land inside a frame this sender has half
// written - from now on only this file writes to a WebSocket once its handshake is done.
struct WsTx {
  int      fd       = -1;
  bool     inFrame  = false;   // owes the frame in flight
  size_t   hdrSent  = 0;       // of frameHdrLen
  size_t   paySent  = 0;       // of sinkBuf[0..frameLen), moved back as sinkPump compacts
  uint32_t lastTook = 0;       // millis() of the last byte the socket took, or of when it began to owe
  uint8_t  ctl[2 + 125];       // a PONG owed: the whole frame, header included
  size_t   ctlLen   = 0;
  size_t   ctlSent  = 0;
  bool     closing  = false;   // its close could not be queued yet: retried every pass (txClose), nothing sent to it
};
static WsTx     tx[WS_MAX_CLIENTS];
static uint8_t  frameHdr[4];
static size_t   frameHdrLen = 0;
static size_t   frameLen    = 0;     // 0 = no frame in flight
// tx[] membership and every write to a WebSocket: the loop's sender against the httpd task (handshake, PONG queueing,
// a CLOSE's echo, the session-close hook). Held only around non-blocking work, never across a wait.
static SemaphoreHandle_t txLock = nullptr;

static void txReset(WsTx &c, int fd) {
  c.fd = fd; c.inFrame = false; c.hdrSent = c.paySent = 0; c.ctlLen = c.ctlSent = 0; c.lastTook = millis();
  c.closing = false;
}

static bool txLive(const WsTx &c) { return c.fd >= 0 && !c.closing; }

static bool txOwes(const WsTx &c) { return txLive(c) && (c.inFrame || c.ctlLen > c.ctlSent); }

static bool txMidFrame(const WsTx &c) {
  return (c.inFrame && (c.hdrSent || c.paySent)) || c.ctlSent;
}

static WsTx *txFor(int fd) {
  for (int i = 0; i < WS_MAX_CLIENTS; i++) if (tx[i].fd == fd) return &tx[i];
  return nullptr;
}

bool wcbWsSinkLive() {
  for (int i = 0; i < WS_MAX_CLIENTS; i++) if (txLive(tx[i])) return true;
  return false;
}

bool wcbWsRunning() { return wsServer != nullptr; }

const char *wcbWsLockHolderName() {
  if (!txLock) return "no WS";
  TaskHandle_t h = xSemaphoreGetMutexHolder(txLock);
  return h ? pcTaskGetName(h) : "none";
}

int wcbWsClientCount() {
  int n = 0;
  for (int i = 0; i < WS_MAX_CLIENTS; i++) if (txLive(tx[i])) n++;
  return n;
}

// httpd task (handshake, traffic).
static void sinkAdd(int fd) {
  if (!txLock) return;
  xSemaphoreTake(txLock, portMAX_DELAY);
  if (!txFor(fd)) {
    // First client after a quiet period: start from a clean sheet. sinkDropping is
    // set when the sink overflows mid-line; without this reset it survives the last
    // client leaving and eats the first whole line the NEXT client is sent.
    if (!wcbWsSinkLive()) {
      taskENTER_CRITICAL(&sinkMux);
      sinkLen = 0; sinkDropping = false;
      taskEXIT_CRITICAL(&sinkMux);
      frameLen = 0;
    }
    WsTx *slot = txFor(-1);
    if (!slot) {                       // full: evict the oldest rather than refuse the newcomer
      slot = &tx[0];
      if (wsServer) httpd_sess_trigger_close(wsServer, slot->fd);   // its stream stops mid-frame: close it
    }
    txReset(*slot, fd);                // joins at the next frame, never part-way into this one
  }
  xSemaphoreGive(txLock);
}

// httpd task (CLOSE frame, session-close hook). Frees the slot before the fd can be closed and reused.
static void sinkDrop(int fd) {
  if (!txLock) return;
  xSemaphoreTake(txLock, portMAX_DELAY);
  WsTx *c = txFor(fd);
  if (c) txReset(*c, -1);
  xSemaphoreGive(txLock);
}

// httpd task: a PING's PONG (RFC 6455 5.5.3, the same payload), sent by sinkPump at the next frame boundary. A PONG
// already part-sent finishes first; a newer PING replaces one not yet begun - answering only the latest is allowed.
static void queuePong(int fd, const uint8_t *payload, size_t len) {
  if (!txLock || len > 125) return;
  xSemaphoreTake(txLock, portMAX_DELAY);
  WsTx *c = txFor(fd);
  if (c && !c->closing && !c->ctlSent) {
    if (!txOwes(*c)) c->lastTook = millis();
    c->ctl[0] = 0x8A;                  // FIN + PONG
    c->ctl[1] = (uint8_t)len;
    if (len) memcpy(c->ctl + 2, payload, len);
    c->ctlLen = 2 + len;
  }
  xSemaphoreGive(txLock);
}

// httpd task: the client sent CLOSE. Echo an empty CLOSE if no frame is part-way out (best effort, never waits), then
// leave the tee; httpd closes the session when the handler returns.
static void closeEcho(int fd) {
  if (!txLock) return;
  xSemaphoreTake(txLock, portMAX_DELAY);
  WsTx *c = txFor(fd);
  if (c) {
    if (!c->closing && !txMidFrame(*c)) {
      static const uint8_t bye[2] = { 0x88, 0x00 };
      send(fd, bye, sizeof(bye), MSG_DONTWAIT);
    }
    txReset(*c, -1);
  }
  xSemaphoreGive(txLock);
}

// Longest prefix of b[0..n) that does not end part-way through a UTF-8 sequence.
// A TEXT frame must be valid UTF-8 on its own (RFC 6455 8.1); a multi-byte
// character straddling the buffer boundary otherwise ends a frame in a bare
// continuation byte, and a conforming client fails the connection with 1007 and
// then replays the same bytes at the same alignment forever.
static size_t utf8SafeLen(const char *b, size_t n) {
  if (!n) return 0;
  size_t back = 0;
  while (back < 3 && back < n) {
    unsigned char c = (unsigned char)b[n - 1 - back];
    if ((c & 0xC0) == 0x80) { back++; continue; }
    size_t need = (c & 0x80) == 0x00 ? 1 :
                  (c & 0xE0) == 0xC0 ? 2 :
                  (c & 0xF0) == 0xE0 ? 3 :
                  (c & 0xF8) == 0xF0 ? 4 : 1;
    return (back + 1 >= need) ? n : n - (back + 1);
  }
  return n;
}

// Writes what the socket takes NOW of buf[sent..len). False only when the session is dead. "No room" and "no
// memory" are not dead: with the access point up W1's heap runs within a few hundred bytes of empty (#111), and lwIP
// then refuses a segment (ENOMEM) that it takes a moment later - the old blocking send closed a live client on it.
static bool txSome(WsTx &c, const uint8_t *buf, size_t len, size_t &sent, uint32_t now, bool gated = true) {
  while (sent < len) {
    if (gated && heap_caps_get_free_size(MALLOC_CAP_8BIT) < WS_HEAP_FLOOR) return true;   // later (WS_HEAP_FLOOR)
    const int r = send(c.fd, buf + sent, len - sent, MSG_DONTWAIT);
    if (r > 0) { sent += (size_t)r; c.lastTook = now; continue; }
    if (r == 0) return true;
    const int e = errno;
    return e == EAGAIN || e == EWOULDBLOCK || e == ENOMEM || e == ENOBUFS || e == EINTR;
  }
  return true;
}

// Ask httpd to close the session; free the slot once it has the request or the session is gone. The request is a
// message on httpd's control socket, and with the heap exhausted that send fails (httpd_queue_work: "failed to queue
// work"): the slot was freed anyway, the session stayed open with its whole unsent buffer while TCP retried a client
// that had left, and W1's heap stayed ~9 KB short for the next test (run 20261014-025548). So a slot whose request did
// not go stays 'closing' - out of the tee, nothing sent to it - and sinkPump asks again every pass.
static void txTryClose(WsTx &c) {
  const esp_err_t e = wsServer ? httpd_sess_trigger_close(wsServer, c.fd) : ESP_ERR_NOT_FOUND;
  if (e == ESP_OK || e == ESP_ERR_NOT_FOUND) txReset(c, -1);   // httpd will close it (wsClose finds no slot), or did
}

// Leave the tee and close the session (loop task, txLock held).
static void txClose(WsTx &c) {
  c.inFrame = false; c.hdrSent = c.paySent = 0; c.ctlLen = c.ctlSent = 0;
  c.closing = true;
  txTryClose(c);
}

// Moves output from the sink to the clients without ever waiting. Called ONLY from loop() — wcbWsService() and the
// tee's inline flush on the loop task. Returns true when a client is live.
//
// THE LOCKING RULE. sinkBuf/sinkLen are written from whatever task is printing —
// including the ESP-NOW receive callback on the WiFi task — while this function
// rewrites them on the loop task. Unsynchronised, a print landing during the
// memmove below has its byte relocated out from under it and sinkLen then
// clobbered: torn output a host parser cannot tell from a real reply.
//
// The spinlock sections are deliberately tiny and never span a send. That is safe
// because appends only ever write at sinkBuf[sinkLen] and sinkLen only grows, so the
// frame [0, frameLen) the sender reads cannot be touched by a concurrent writer —
// only the tail moves, and the tail is re-read under the lock. Holding a spinlock
// across send() would disable interrupts on this core for the whole of a network
// call. txLock (a mutex, not a spinlock) is what keeps the httpd task's writes and
// slot changes out of the middle of a pass.
// True on the task that holds txLock: a print from inside a send (none today - IDF's own log lines do not pass
// through WCBSerial) must not take the mutex again on the same task.
static bool txHeldHere() {
  return txLock && xSemaphoreGetMutexHolder(txLock) == xTaskGetCurrentTaskHandle();
}

static bool sinkPump() {
  if (!wsServer || !txLock || txHeldHere()) return wcbWsSinkLive();
  xSemaphoreTake(txLock, portMAX_DELAY);
  const uint32_t now = millis();

  for (int round = 0; round < 4; round++) {
    // Open the next frame: what the sink holds now, cut back to a whole UTF-8 character. With nothing to send the
    // pass still runs the clients below: a PONG owed goes out on a quiet console too.
    size_t n = 0;
    if (!frameLen) {
      taskENTER_CRITICAL(&sinkMux);
      n = sinkLen ? utf8SafeLen(sinkBuf, sinkLen) : 0;
      if (!n && sinkLen) n = sinkLen;      // cannot improve it — send rather than stall
      taskEXIT_CRITICAL(&sinkMux);
    }
    if (n && wcbWsSinkLive()) {
      frameLen = n;
      frameHdr[0] = 0x81;                  // FIN + TEXT
      if (n < 126) { frameHdr[1] = (uint8_t)n; frameHdrLen = 2; }
      else { frameHdr[1] = 126; frameHdr[2] = (uint8_t)(n >> 8); frameHdr[3] = (uint8_t)n; frameHdrLen = 4; }
      for (int i = 0; i < WS_MAX_CLIENTS; i++) {
        WsTx &c = tx[i];
        if (!txLive(c)) continue;
        if (!txOwes(c)) c.lastTook = now;  // its no-progress clock starts when it begins to owe
        c.inFrame = true; c.hdrSent = 0; c.paySent = 0;
      }
    }

    for (int i = 0; i < WS_MAX_CLIENTS; i++) {
      WsTx &c = tx[i];
      if (c.fd < 0) continue;
      if (c.closing) { txTryClose(c); continue; }
      bool alive = true;
      if (c.ctlLen && !(c.inFrame && (c.hdrSent || c.paySent))) {     // a PONG, between frames only
        alive = txSome(c, c.ctl, c.ctlLen, c.ctlSent, now, false);
        if (alive && c.ctlSent == c.ctlLen) c.ctlLen = c.ctlSent = 0;
      }
      if (alive && c.inFrame && !c.ctlLen) {
        alive = txSome(c, frameHdr, frameHdrLen, c.hdrSent, now);
        if (alive && c.hdrSent == frameHdrLen) alive = txSome(c, (const uint8_t *)sinkBuf, frameLen, c.paySent, now);
        if (alive && c.hdrSent == frameHdrLen && c.paySent == frameLen) c.inFrame = false;
      }
      // Close the session, not just leave the tee: a session left open holds its unsent output in lwIP (up to a
      // whole TCP send buffer, ~5.7 KB) for as long as TCP retries a peer that has gone, and two or three such
      // sessions emptied W1's AP-mode heap until no new client could connect (HIL tracker #113). wsClose resets it.
      if (!alive) txClose(c);
      else if (txOwes(c) && now - c.lastTook >= WS_STUCK_MS) { stuckCloses++; txClose(c); }
    }

    // Free what every client still in the frame has sent: the slowest one sets the pace. When none is left in it
    // (all done, or all gone) the whole frame goes and the next one can open.
    if (!frameLen) break;                  // nothing in flight: the sink was empty this round
    size_t done = frameLen;
    for (int i = 0; i < WS_MAX_CLIENTS; i++) {
      const WsTx &c = tx[i];
      if (!txLive(c) || !c.inFrame) continue;
      const size_t at = (c.hdrSent < frameHdrLen) ? 0 : c.paySent;
      if (at < done) done = at;
    }
    if (done) {
      taskENTER_CRITICAL(&sinkMux);
      const size_t left = (sinkLen > done) ? (sinkLen - done) : 0;
      if (left) memmove(sinkBuf, sinkBuf + done, left);
      sinkLen = left;
      taskEXIT_CRITICAL(&sinkMux);
      frameLen -= done;
      for (int i = 0; i < WS_MAX_CLIENTS; i++)
        if (txLive(tx[i]) && tx[i].inFrame) tx[i].paySent -= done;
    }
    if (frameLen) break;                   // someone is still part-way through: the rest waits for a later pass
  }

  const bool live = wcbWsSinkLive();
  xSemaphoreGive(txLock);
  return live;
}

// A client the tee may wait for: it owes output and took some within WS_FRESH_MS. Read without the lock - a
// stale answer costs one wait of at most a tick or one line dropped early.
static bool sinkClientFresh() {
  const uint32_t now = millis();
  for (int i = 0; i < WS_MAX_CLIENTS; i++)
    if (txOwes(tx[i]) && now - tx[i].lastTook < WS_FRESH_MS) return true;
  return false;
}

// THE TEE. Runs inside arbitrary Serial.printf calls, on whatever task is
// printing — including the ESP-NOW receive callback. So: no prints (that would
// recurse straight back into here), no allocation, no TCP send, no blocking.
// Append only. Overflow drops WHOLE LINES rather than truncating, because half a
// JSON object breaks a host tool's parser while a lost line is just a lost line.
void wcbWsSinkWrite(uint8_t c) {
  if (!wcbWsSinkLive()) return;

  // Fast path under the lock: the common case is a byte into free space, and that
  // costs one short critical section. Only the FULL case falls through to the
  // slower handling below, which must not run with interrupts disabled.
  taskENTER_CRITICAL(&sinkMux);
  if (sinkDropping) {
    if (c == '\n') sinkDropping = false;
    taskEXIT_CRITICAL(&sinkMux);
    return;
  }
  if (sinkLen < WS_SINK_BUF) {
    sinkBuf[sinkLen++] = (char)c;
    taskEXIT_CRITICAL(&sinkMux);
    return;
  }
  taskEXIT_CRITICAL(&sinkMux);

  // FULL. A bulk reply — a config pull is ~3 KB — is emitted as a tight run of
  // Serial.println() calls with no loop() iteration between them, so waiting for
  // the next wcbWsService() means discarding most of it. Measured: pulling a
  // config over the socket lost a line mid-dump.
  //
  // So flush inline, but ONLY on the loop task. On any other task — above all the
  // ESP-NOW receive callback — touching the sockets here would hold up the WiFi
  // task, which is the failure this file is built to avoid. Off the loop task we
  // still drop, because dropping a telemetry line is survivable and stalling the
  // radio is not. On the loop task the sends never block; when a client cannot take
  // more yet, the tee waits a tick and tries again - only while some client is
  // still taking bytes (WS_FRESH_MS), and for at most WS_PASS_WAIT_MS per loop()
  // pass in all - then drops.
  //
  // sinkPump() takes the locks itself, so it must be called OUTSIDE the critical
  // section — taskENTER_CRITICAL does not nest across a network call.
  if (xTaskGetCurrentTaskHandle() == sinkLoopTask && !txHeldHere()) {
    const uint32_t t0 = millis();
    for (;;) {
      sinkPump();
      taskENTER_CRITICAL(&sinkMux);
      if (sinkLen < WS_SINK_BUF) {
        sinkBuf[sinkLen++] = (char)c;
        taskEXIT_CRITICAL(&sinkMux);
        passWaitMs += millis() - t0;
        return;
      }
      taskEXIT_CRITICAL(&sinkMux);
      if (passWaitMs + (millis() - t0) >= WS_PASS_WAIT_MS || !sinkClientFresh()) break;
      vTaskDelay(1);
    }
    passWaitMs += millis() - t0;
  }

  const bool report = sinkReporting && xTaskGetCurrentTaskHandle() == sinkLoopTask;
  taskENTER_CRITICAL(&sinkMux);
  sinkDropping = true;
  if (!report) sinkDrops++;
  taskEXIT_CRITICAL(&sinkMux);
}

// ---- Per-socket line accumulator ------------------------------------------
// ONE PER SOCKET, never a shared static. A long line legitimately spans several
// frames, so with a single buffer a second client's traffic landing in that
// window fuses two clients' bytes into one garbage line.
struct WsAcc {
  int    fd      = -1;
  size_t len     = 0;
  bool   overrun = false;
  char   buf[WS_LINE_MAX];
};
static WsAcc accs[WS_MAX_CLIENTS];

static WsAcc *accFor(int fd) {
  for (int i = 0; i < WS_MAX_CLIENTS; i++) if (accs[i].fd == fd) return &accs[i];
  for (int i = 0; i < WS_MAX_CLIENTS; i++)
    if (accs[i].fd < 0) { accs[i].fd = fd; accs[i].len = 0; accs[i].overrun = false; return &accs[i]; }
  accs[0].fd = fd; accs[0].len = 0; accs[0].overrun = false;
  return &accs[0];
}

static void accRelease(int fd) {
  for (int i = 0; i < WS_MAX_CLIENTS; i++)
    if (accs[i].fd == fd) { accs[i].fd = -1; accs[i].len = 0; accs[i].overrun = false; }
}

static void accFeed(WsAcc *a, int fd, char c) {
  if (c == '\n' || c == '\r') {
    if (a->overrun) { a->overrun = false; a->len = 0; return; }   // drop the WHOLE line
    if (!a->len) return;
    a->buf[a->len] = '\0';
    WsCmd cmd;
    cmd.fd = fd;
    memcpy(cmd.line, a->buf, a->len + 1);
    a->len = 0;
    if (!wsQueue) return;
    if (xQueueSend(wsQueue, &cmd, pdMS_TO_TICKS(WS_ENQUEUE_WAIT_MS)) != pdTRUE) cmdDrops++;
    return;
  }
  if (a->overrun) return;
  if (a->len < WS_LINE_MAX - 1) { a->buf[a->len++] = c; return; }
  // Overrun. Suppress to end-of-line rather than restarting accumulation: zeroing
  // len mid-line would treat the TAIL as a fresh command, and an unprefixed tail
  // is a BROADCAST — putting an arbitrary mid-line fragment on every board.
  a->overrun = true;
  a->len     = 0;
}

// ---- httpd handler (Core 0 — copy, enqueue, return; never print) ----------
static esp_err_t wsHandler(httpd_req_t *req) {
  const int fd = httpd_req_to_sockfd(req);
  if (req->method == HTTP_GET) {          // handshake complete
    sinkAdd(fd);
    accFor(fd);
    return ESP_OK;
  }

  httpd_ws_frame_t frame = {};
  // static, not a stack array: 3 KB would blow the httpd task stack. Safe because
  // esp_http_server services every socket from ONE task, so two invocations of
  // this handler can never overlap. Declared before the length probe so the
  // oversize check below can size itself against the real buffer.
  static uint8_t rxBuf[WS_LINE_MAX * 2];

  frame.type = HTTPD_WS_TYPE_TEXT;
  esp_err_t err = httpd_ws_recv_frame(req, &frame, 0);   // length probe
  if (err != ESP_OK) return err;
  // Control frames come here too (handle_ws_control_frames, wcbWsBegin): httpd no longer answers them itself.
  if (frame.type == HTTPD_WS_TYPE_CLOSE) { accRelease(fd); closeEcho(fd); return ESP_OK; }   // httpd then closes it
  if (frame.type == HTTPD_WS_TYPE_PING && frame.len > 125) return ESP_FAIL;                 // RFC 6455 5.5

  // OVERSIZED: we cannot read it, and we must not leave it unread. The call above
  // is a LENGTH PROBE — esp_http_server has parsed the header but the payload is
  // still sitting in the socket, and httpd_ws_recv_frame() refuses to read into a
  // buffer smaller than the frame (ESP_ERR_INVALID_SIZE) rather than draining part
  // of it. Returning ESP_OK here leaves those bytes to be parsed as the NEXT frame
  // header: garbage opcode, garbage length, and a session that is desynchronised
  // for good while the log calls it a benign dropped command.
  //
  // So fail the request instead. httpd closes the socket, wsClose() releases the
  // slot, and the client sees a clean disconnect it can retry — a bounded, visible
  // failure rather than a silently corrupted stream.
  if (frame.len > sizeof(rxBuf)) {
    cmdDrops++;
    return ESP_FAIL;
  }

  // Every frame's payload is read, a PING's and a PONG's too: one left in the socket is parsed as the next header.
  if (frame.len) {
    frame.payload = rxBuf;
    err = httpd_ws_recv_frame(req, &frame, sizeof(rxBuf));
    if (err != ESP_OK) return err;
  }
  if (frame.type == HTTPD_WS_TYPE_PING) { queuePong(fd, rxBuf, frame.len); return ESP_OK; }
  if (frame.type != HTTPD_WS_TYPE_TEXT || !frame.len) return ESP_OK;

  sinkAdd(fd);                            // re-arm on traffic
  WsAcc *a = accFor(fd);
  for (size_t i = 0; i < frame.len; i++) accFeed(a, fd, (char)rxBuf[i]);
  return ESP_OK;
}

// Session close: release the accumulator and the sink slot. Without this a
// departed client holds both until some later send happens to fail on it, and
// the unowned tail of a half-accumulated line eats the next client's first command.
//
// Closed with a reset (SO_LINGER 0), not a FIN: a graceful close keeps the socket's unsent data queued while TCP
// retries a peer that may be gone - a phone out of range, a PC moved to another network - and that is the heap the
// next client needs (#113). A client that is still there sees its connection reset, which it treats as closed.
static void wsClose(httpd_handle_t /*hd*/, int sockfd) {
  accRelease(sockfd);
  sinkDrop(sockfd);
  struct linger lg = { 1, 0 };
  setsockopt(sockfd, SOL_SOCKET, SO_LINGER, &lg, sizeof(lg));
  close(sockfd);
}

// ---- A client whose station left the access point -----------------------
// The AP sees a station leave (WIFI_EVENT_AP_STADISCONNECTED) long before TCP gives up on its sockets. When the
// last station has gone, every session from the AP's own subnet is dead: close them at once rather than wait for a
// send to fail or for WS_STUCK_MS to pass on each. With stations still there a dead session closes at its first hard
// send error, or after WS_STUCK_MS without taking a byte it owes (sinkPump). The flag is set on the Arduino event
// task and read on loop().
static volatile bool apStationLeft = false;

static void onApStationLeft(arduino_event_id_t, arduino_event_info_t) { apStationLeft = true; }

static void closeSessionsOfLeftStations() {
  if (!apStationLeft) return;
  apStationLeft = false;
  if (!wsServer || !txLock || WiFi.getMode() == WIFI_STA || WiFi.softAPgetStationNum() > 0) return;
  IPAddress ap = WiFi.softAPIP();
  xSemaphoreTake(txLock, portMAX_DELAY);
  for (int i = 0; i < WS_MAX_CLIENTS; i++) {
    if (!txLive(tx[i])) continue;
    struct sockaddr_in6 peer = {};
    socklen_t len = sizeof(peer);
    if (getpeername(tx[i].fd, (struct sockaddr *)&peer, &len) != 0) continue;
    uint32_t v4;
    if (peer.sin6_family == AF_INET) v4 = ((struct sockaddr_in *)&peer)->sin_addr.s_addr;
    else if (peer.sin6_family == AF_INET6 && peer.sin6_addr.un.u32_addr[0] == 0 && peer.sin6_addr.un.u32_addr[1] == 0 &&
             peer.sin6_addr.un.u32_addr[2] == PP_HTONL(0x0000FFFFUL)) v4 = peer.sin6_addr.un.u32_addr[3];   // v4-mapped
    else continue;
    IPAddress ip(v4);
    if (ip[0] == ap[0] && ip[1] == ap[1] && ip[2] == ap[2])     // the AP's /24: a client that came through it
      txClose(tx[i]);
  }
  xSemaphoreGive(txLock);
}

// ---- Lifecycle ------------------------------------------------------------
bool wcbWsBegin() {
  if (!wcbWifiReady()) return false;      // no interface — nothing to listen on
  if (wsServer) return true;              // already running

  wsQueue = xQueueCreate(WS_QUEUE_DEPTH, sizeof(WsCmd));
  if (!txLock) txLock = xSemaphoreCreateMutex();
  if (!wsQueue || !txLock) {
    Serial.println("[WS] command queue alloc FAILED — WebSocket disabled");
    if (wsQueue) { vQueueDelete(wsQueue); wsQueue = nullptr; }
    return false;
  }

  httpd_config_t cfg   = HTTPD_DEFAULT_CONFIG();
  cfg.max_open_sockets = WS_MAX_CLIENTS;
  cfg.close_fn         = wsClose;
  cfg.lru_purge_enable = true;
  // Only httpd's own sends use it now - the handshake reply and HTTP errors, on its task. The console goes out
  // through sinkPump, which never blocks (WS_STUCK_MS). Before that, a send to a client that had gone blocked loop()
  // for this long each time: 5 s by default, seen as five-second stalls of W1's console (#113).
  cfg.send_wait_timeout = 2;
  // The default 4096 is not enough once our handler's frame work is on it.
  cfg.stack_size       = 6144;
  if (httpd_start(&wsServer, &cfg) != ESP_OK) {
    Serial.println("[WS] httpd_start FAILED — WebSocket disabled");
    vQueueDelete(wsQueue); wsQueue = nullptr; wsServer = nullptr;
    return false;
  }

  httpd_uri_t uri  = {};
  uri.uri          = "/ws";
  uri.method       = HTTP_GET;
  uri.handler      = wsHandler;
  uri.user_ctx     = nullptr;
  uri.is_websocket = true;
  // PING and CLOSE come to wsHandler instead of being answered by httpd with a blocking send on its own task, which
  // could land inside a frame sinkPump has half written: only this file writes to a WebSocket after the handshake.
  uri.handle_ws_control_frames = true;
  if (httpd_register_uri_handler(wsServer, &uri) != ESP_OK) {
    Serial.println("[WS] URI register FAILED — WebSocket disabled");
    httpd_stop(wsServer); wsServer = nullptr;
    vQueueDelete(wsQueue); wsQueue = nullptr;
    return false;
  }

  // NaviLink watches for this exact line to know the endpoint is live.
  Serial.printf("[WS] command endpoint ready — ws://%s/ws\n", wcbWifiIP().c_str());
  return true;
}

void wcbWsService() {
  // Start on the first pass where an interface exists. Not from setup(): in JOIN
  // mode the association completes long after setup() has returned, so there is
  // no single point in boot where "the network is up" is true for both modes.
  // Latched — a failed start is not retried every pass, which would spam the log
  // forever on a board where httpd genuinely cannot come up.
  // We are on the loop task here, by definition. Capture it so the output tee can
  // tell "safe to flush inline" from "this is the WiFi task, drop instead".
  if (!sinkLoopTask) sinkLoopTask = xTaskGetCurrentTaskHandle();
  passWaitMs = 0;                   // a new loop() pass: the tee may wait WS_PASS_WAIT_MS again

  static bool startTried = false;
  if (!wsServer) {
    if (startTried || !wcbWifiReady()) return;
    startTried = true;
    if (!wcbWsBegin()) return;
    WiFi.onEvent(onApStationLeft, ARDUINO_EVENT_WIFI_AP_STADISCONNECTED);
  }
  closeSessionsOfLeftStations();

  // Run at most ONE queued command per pass. processSerialCommandHelper() can
  // block on ETM retries, and a burst back-to-back would stall the rest of loop().
  if (wsQueue) {
    WsCmd cmd;
    if (xQueueReceive(wsQueue, &cmd, 0) == pdTRUE) {
      // Reset the per-command flags at this SOURCE, exactly as the Serial0 reader
      // does. They are snapshotted per queue item, so a value left over from a
      // prior mesh recall's body drain would otherwise latch and suppress this
      // command's fan-out.
      lastReceivedViaESPNOW = false;
      inSequenceBody        = false;
      String line(cmd.line);        // named: the dispatcher takes a non-const ref
      // TRIM, exactly as the Serial0 reader does (processIncomingSerial, WCB.ino).
      // processSerialCommandHelper() does NOT trim, and a line with a leading space
      // fails the startsWith(LocalFunctionIdentifier) test — so it is not recognised
      // as a local command and falls through to the UNPREFIXED path, which is a
      // mesh-wide BROADCAST. One stray space from a paste would push raw text to
      // every serial port on every board.
      line.trim();
      if (line.length()) processSerialCommandHelper(line, WS_SOURCE_ID);
    }
  }

  // Report drops from HERE, never from the handler or the tee — both run where a
  // print would block the wrong task. Summed, at most once a second: while a client
  // is stalled nearly every pass that prints drops something for it.
  static uint32_t lastReportMs = 0;
  if ((sinkDrops || cmdDrops || stuckCloses) && millis() - lastReportMs >= 1000) {
    lastReportMs = millis();
    sinkReporting = true;
    if (sinkDrops) { uint32_t n = sinkDrops; sinkDrops = 0;
                     Serial.printf("[WS] dropped %u output line(s) — client too slow\n", (unsigned)n); }
    if (cmdDrops)  { uint32_t n = cmdDrops;  cmdDrops  = 0;
                     Serial.printf("[WS] dropped %u inbound command(s) — queue full or oversized\n", (unsigned)n); }
    if (stuckCloses) { uint32_t n = stuckCloses; stuckCloses = 0;
                       Serial.printf("[WS] closed %u client(s) that took no output for %u s\n", (unsigned)n,
                                     (unsigned)(WS_STUCK_MS / 1000)); }
    sinkReporting = false;
  }

  sinkPump();
}
