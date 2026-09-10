#include "WCB_RemoteTerm.h"   // Must be first — redirects Serial -> WCBDebugSerial
#include "WCB_WS.h"
#include "WCB_WiFi.h"

#include <esp_http_server.h>
#include <WiFi.h>
#include <lwip/sockets.h>     // close() — the session-close hook owns the fd

// Provided by WCB.ino. Note the NON-CONST reference — the dispatcher may rewrite
// the line in place, so it cannot be handed a temporary.
extern void processSerialCommandHelper(String &data, int sourceID);
extern bool lastReceivedViaESPNOW;
extern bool inSequenceBody;

// ---- Sizing ---------------------------------------------------------------
// Keep in step with httpd_config_t::max_open_sockets in wcbWsBegin(). More
// clients than the server will accept is just dead slots; fewer means a
// connected client the sink never writes to — a silently deaf client.
static const uint8_t  WS_MAX_CLIENTS = 3;
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
static int            wsFds[WS_MAX_CLIENTS] = { -1, -1, -1 };

// ---- Output sink ----------------------------------------------------------
static char     sinkBuf[WS_SINK_BUF];
static size_t   sinkLen      = 0;
static bool     sinkDropping = false;
static uint32_t sinkDrops    = 0;     // whole lines dropped; reported from loop()
static uint32_t cmdDrops     = 0;     // commands refused at the queue
// The loop task, captured the first time wcbWsService() runs. wcbWsSinkWrite() may
// only flush inline while running on THIS task — see the note there.
static TaskHandle_t sinkLoopTask = nullptr;
// Guards sinkBuf/sinkLen/sinkDropping. The tee runs on whatever task is printing
// and sinkPump() runs on loop — see the locking note above sinkPump().
static portMUX_TYPE sinkMux = portMUX_INITIALIZER_UNLOCKED;

bool wcbWsSinkLive() {
  for (int i = 0; i < WS_MAX_CLIENTS; i++) if (wsFds[i] >= 0) return true;
  return false;
}

bool wcbWsRunning() { return wsServer != nullptr; }

int wcbWsClientCount() {
  int n = 0;
  for (int i = 0; i < WS_MAX_CLIENTS; i++) if (wsFds[i] >= 0) n++;
  return n;
}

static void sinkAdd(int fd) {
  for (int i = 0; i < WS_MAX_CLIENTS; i++) if (wsFds[i] == fd) return;
  // First client after a quiet period: start from a clean sheet. sinkDropping is
  // set when a send fails, which is exactly what happens as the last client
  // leaves mid-line — without this reset it survives the disconnect and eats the
  // first whole line the NEXT client is sent.
  if (!wcbWsSinkLive()) { sinkLen = 0; sinkDropping = false; }
  for (int i = 0; i < WS_MAX_CLIENTS; i++) if (wsFds[i] < 0) { wsFds[i] = fd; return; }
  wsFds[0] = fd;   // full: evict the oldest rather than refuse the newcomer
}

static void sinkDrop(int fd) {
  for (int i = 0; i < WS_MAX_CLIENTS; i++) if (wsFds[i] == fd) wsFds[i] = -1;
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

// Flush the staging buffer to every connected client. Called ONLY from loop().
//
// THE LOCKING RULE. sinkBuf/sinkLen are written from whatever task is printing —
// including the ESP-NOW receive callback on the WiFi task — while this function
// rewrites them on the loop task. Unsynchronised, a print landing during the
// memmove below has its byte relocated out from under it and sinkLen then
// clobbered: torn output a host parser cannot tell from a real reply.
//
// The critical sections are deliberately tiny and never span the TCP send. That
// is safe because appends only ever write at sinkBuf[sinkLen] and sinkLen only
// grows, so the region [0, send) handed to httpd cannot be touched by a
// concurrent writer — only the tail moves, and the tail is re-read under the lock
// afterwards. Holding a spinlock across httpd_ws_send_frame_async() would disable
// interrupts on this core for the whole of a network write, which is exactly the
// stall this file exists to prevent.
static bool sinkPump() {
  if (!wsServer) return wcbWsSinkLive();

  size_t send;
  taskENTER_CRITICAL(&sinkMux);
  send = sinkLen ? utf8SafeLen(sinkBuf, sinkLen) : 0;
  if (!send && sinkLen) send = sinkLen;      // cannot improve it — send rather than stall
  taskEXIT_CRITICAL(&sinkMux);
  if (!send) return wcbWsSinkLive();

  httpd_ws_frame_t f = {};
  f.final   = true;
  f.type    = HTTPD_WS_TYPE_TEXT;
  f.payload = (uint8_t *)sinkBuf;
  f.len     = send;
  for (int i = 0; i < WS_MAX_CLIENTS; i++) {
    if (wsFds[i] < 0) continue;
    if (httpd_ws_send_frame_async(wsServer, wsFds[i], &f) != ESP_OK) wsFds[i] = -1;
  }

  // Re-read sinkLen: a writer may have appended while the send was in flight.
  taskENTER_CRITICAL(&sinkMux);
  const size_t left = (sinkLen > send) ? (sinkLen - send) : 0;
  if (left) memmove(sinkBuf, sinkBuf + send, left);
  sinkLen = left;
  taskEXIT_CRITICAL(&sinkMux);
  return wcbWsSinkLive();
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
  // So flush inline, but ONLY on the loop task. That is the whole reason this
  // check exists: on any other task — above all the ESP-NOW receive callback — a
  // TCP send here would block the WiFi task, which is the failure this file is
  // built to avoid. Off the loop task we still drop, because dropping a telemetry
  // line is survivable and stalling the radio is not.
  //
  // sinkPump() takes the lock itself, so it must be called OUTSIDE the critical
  // section — taskENTER_CRITICAL does not nest across a network write.
  if (xTaskGetCurrentTaskHandle() == sinkLoopTask) {
    sinkPump();
    taskENTER_CRITICAL(&sinkMux);
    if (sinkLen < WS_SINK_BUF) {
      sinkBuf[sinkLen++] = (char)c;
      taskEXIT_CRITICAL(&sinkMux);
      return;
    }
    taskEXIT_CRITICAL(&sinkMux);
  }

  taskENTER_CRITICAL(&sinkMux);
  sinkDropping = true;
  sinkDrops++;
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
  if (frame.type == HTTPD_WS_TYPE_CLOSE) { accRelease(fd); sinkDrop(fd); return ESP_OK; }
  if (frame.len == 0) return ESP_OK;

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

  frame.payload = rxBuf;
  err = httpd_ws_recv_frame(req, &frame, sizeof(rxBuf));
  if (err != ESP_OK) return err;
  if (frame.type != HTTPD_WS_TYPE_TEXT) return ESP_OK;

  sinkAdd(fd);                            // re-arm on traffic
  WsAcc *a = accFor(fd);
  for (size_t i = 0; i < frame.len; i++) accFeed(a, fd, (char)rxBuf[i]);
  return ESP_OK;
}

// Session close: release the accumulator and the sink slot. Without this a
// departed client holds both until some later send happens to fail on it, and
// the unowned tail of a half-accumulated line eats the next client's first command.
static void wsClose(httpd_handle_t /*hd*/, int sockfd) {
  accRelease(sockfd);
  sinkDrop(sockfd);
  close(sockfd);
}

// ---- Lifecycle ------------------------------------------------------------
bool wcbWsBegin() {
  if (!wcbWifiReady()) return false;      // no interface — nothing to listen on
  if (wsServer) return true;              // already running

  wsQueue = xQueueCreate(WS_QUEUE_DEPTH, sizeof(WsCmd));
  if (!wsQueue) {
    Serial.println("[WS] command queue alloc FAILED — WebSocket disabled");
    return false;
  }

  httpd_config_t cfg   = HTTPD_DEFAULT_CONFIG();
  cfg.max_open_sockets = WS_MAX_CLIENTS;
  cfg.close_fn         = wsClose;
  cfg.lru_purge_enable = true;
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

  static bool startTried = false;
  if (!wsServer) {
    if (startTried || !wcbWifiReady()) return;
    startTried = true;
    if (!wcbWsBegin()) return;
  }

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
  // print would block the wrong task. Once per occurrence, not per line.
  if (sinkDrops) { uint32_t n = sinkDrops; sinkDrops = 0;
                   Serial.printf("[WS] dropped %u output line(s) — client too slow\n", (unsigned)n); }
  if (cmdDrops)  { uint32_t n = cmdDrops;  cmdDrops  = 0;
                   Serial.printf("[WS] dropped %u inbound command(s) — queue full or oversized\n", (unsigned)n); }

  sinkPump();
}
