#include <HardwareSerial.h>
#include <DFRobotDFPlayerMini.h>

// ============================================================
// UART1 - Raspberry Pi Communication (GPIO 44=RX, 43=TX)
// ============================================================
#define PI_UART_RX_PIN 44
#define PI_UART_TX_PIN 43
HardwareSerial piSerial(1);

// ============================================================
// UART2 - DFPlayer Mini Communication (GPIO 39=RX, 40=TX)
// ============================================================
#define DF_UART_RX_PIN 39
#define DF_UART_TX_PIN 40
HardwareSerial dfSerial(2);
DFRobotDFPlayerMini dfPlayer;
bool dfPlayerReady = false;

// ============================================================
// DFPlayer Audio Track Mapping (SD Card folder /01/)
// ============================================================
// 001.mp3 = "Align horizontally GET IN POSITION"
// 002.mp3 = "Don't go too deep in squats"
// 003.mp3 = "Equalize both hands"
// 004.mp3 = "Extend arms straight and directly in front of you"
// 005.mp3 = "Form a straight line with body"
// 006.mp3 = "Keep feet at shoulder distance"
// 007.mp3 = "Look in front of you"
// 008.mp3 = "Slow down! Curling too fast"
// 009.mp3 = "Stand at shoulder width and keep knees in front of body"
// 010.mp3 = "Tuck elbow closer to your body"
// 011.mp3 = "Do not swing arm / Keep hands in straight position without swinging"

unsigned long lastHeartbeatTime = 0;
const unsigned long heartbeatInterval = 3000; // Send heartbeat every 3 seconds

void setup() {
  // Initialize default Serial (USB debugging console to PC)
  Serial.begin(115200);
  delay(1000);

  Serial.println("========================================");
  Serial.println("  ESP32-S3 Serial Test Utility Initialized");
  Serial.println("========================================");
  Serial.printf("Configuring UART1 (Pi Link) on RX:%d, TX:%d at 115200 baud\n", PI_UART_RX_PIN, PI_UART_TX_PIN);

  // Initialize HardwareSerial 1 for Raspberry Pi communication
  piSerial.begin(115200, SERIAL_8N1, PI_UART_RX_PIN, PI_UART_TX_PIN);
  piSerial.setTimeout(50);

  // Initialize DFPlayer Mini
  dfSerial.begin(9600, SERIAL_8N1, DF_UART_RX_PIN, DF_UART_TX_PIN);
  Serial.println("[DFPlayer] Initializing...");
  delay(1500); // Allow DFPlayer to boot

  if (dfPlayer.begin(dfSerial, true, true)) {
    dfPlayerReady = true;
    dfPlayer.setTimeOut(500);
    dfPlayer.volume(25); // Volume: 0-30
    dfPlayer.EQ(DFPLAYER_EQ_NORMAL);
    dfPlayer.outputDevice(DFPLAYER_DEVICE_SD);
    Serial.println("✅ [DFPlayer] Ready - Volume: 25/30");
  } else {
    dfPlayerReady = false;
    Serial.println("❌ [DFPlayer] FAILED - Check wiring on GPIO 39/40");
    Serial.println("[DFPlayer] Continuing without audio...");
  }

  Serial.println("✅ Ready! Listening for data from Pi...");
  Serial.println("----------------------------------------");
}

void loop() {
  unsigned long now = millis();

  // 1. Read from Raspberry Pi and handle it
  if (piSerial.available() > 0) {
    String incoming = piSerial.readStringUntil('\n');
    incoming.trim();
    
    if (incoming.length() > 0) {
      // Print to USB console for local PC debugging
      Serial.print("📩 Received from Pi: ");
      Serial.println(incoming);

      // Handle specific commands
      if (incoming.equalsIgnoreCase("PING")) {
        Serial.println("📤 Replying: PONG");
        piSerial.println("PONG");
      } else if (incoming.startsWith("AUDIO:")) {
        String trackStr = incoming.substring(6);
        trackStr.trim();
        int track = trackStr.toInt();
        if (track > 0) {
          if (dfPlayerReady) {
            Serial.print("🔊 Playing custom track: ");
            Serial.println(track);
            dfPlayer.playFolder(1, track);
            piSerial.print("PLAYING:");
            piSerial.println(track);
          } else {
            Serial.println("❌ AUDIO command ignored (DFPlayer not ready)");
            piSerial.println("ERROR:DFPLAYER_NOT_READY");
          }
        }
      } else {
        // Echo back generic messages to Pi
        String reply = "ECHO:" + incoming;
        Serial.print("📤 Replying: ");
        Serial.println(reply);
        piSerial.println(reply);
      }
    }
  }

  // 2. Read from PC Serial (USB Monitor) and send to Raspberry Pi
  if (Serial.available() > 0) {
    String pcInput = Serial.readStringUntil('\n');
    pcInput.trim();
    if (pcInput.length() > 0) {
      Serial.print("📤 Sending to Pi: ");
      Serial.println(pcInput);
      piSerial.println(pcInput);
    }
  }

  // 3. Periodic Heartbeat to verify connection
  if (now - lastHeartbeatTime >= heartbeatInterval) {
    lastHeartbeatTime = now;
    String hbMessage = "HEARTBEAT (millis=" + String(now) + ")";
    
    // Send to both Pi and local PC serial
    piSerial.println(hbMessage);
    Serial.println("📡 Sent Heartbeat to Pi");
  }

  // Keep DFPlayer communication alive
  if (dfPlayerReady) {
    dfPlayer.readType();
  }

  delay(10);
}
