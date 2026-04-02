static const int SERIAL1_TX_PIN = 8;
static const int SERIAL1_RX_PIN = 21;

String rxBuffer = "";

void setup() {
  Serial.begin(115200);
  while (!Serial) delay(10);

  Serial1.begin(57600, SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
  // pinMode(SERIAL1_RX_PIN, INPUT_PULLUP);

  Serial.println("Serial bridge ready (CR/LF terminated)");
}

void loop() {
  // USB -> Serial1
  while (Serial.available()) {
    Serial1.write(Serial.read());
  }

  // Serial1 -> USB
  while (Serial1.available()) {
    char c = Serial1.read();

    if (c == '\r' || c == '\n') {
      if (rxBuffer.length()) {
        Serial.print("[RX] ");
        Serial.println(rxBuffer);
        rxBuffer = "";
      }
    } else {
      rxBuffer += c;
    }
  }
}
