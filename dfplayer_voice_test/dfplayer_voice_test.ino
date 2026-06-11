#include <Arduino.h>
#include <DFRobotDFPlayerMini.h>
#include <HardwareSerial.h>

// ============================================================
// UART2 - DFPlayer Mini Communication
// Same pins as defined in esp_commands.ino:
// GPIO 39 = RX, GPIO 40 = TX
// ============================================================
#define DF_UART_RX_PIN 40
#define DF_UART_TX_PIN 39
HardwareSerial dfSerial(2);
DFRobotDFPlayerMini dfPlayer;

bool dfPlayerReady = false;
int currentVolume = 25; // Default volume (0-30)

// Audio Track Mapping for Folder 01 on SD Card
const char* trackNames[] = {
  "Align horizontally GET IN POSITION", // Track 1
  "Don't go too deep in squats",        // Track 2
  "Equalize both hands",                // Track 3
  "Extend arms straight and directly in front of you", // Track 4
  "Form a straight line with body",     // Track 5
  "Keep feet at shoulder distance",     // Track 6
  "Look in front of you",               // Track 7
  "Slow down! Curling too fast",        // Track 8
  "Stand at shoulder width and keep knees in front of body", // Track 9
  "Tuck elbow closer to your body",     // Track 10
  "Do not swing arm / Keep hands in straight position without swinging" // Track 11
};
const int TOTAL_TRACKS = 11;

void printHelp() {
  Serial.println("\n--- DFPlayer Mini Voice Test Menu ---");
  Serial.println("Commands:");
  Serial.println("  p <track_num> : Play track (1-11) from folder 01");
  Serial.println("  v <volume>    : Set volume (0-30)");
  Serial.println("  s             : Stop playback");
  Serial.println("  pause         : Pause playback");
  Serial.println("  resume        : Resume playback");
  Serial.println("  r             : Reset DFPlayer");
  Serial.println("  h             : Show this help menu");
  Serial.println("\nTrack List (Folder 01):");
  for (int i = 0; i < TOTAL_TRACKS; i++) {
    Serial.print("  ");
    Serial.print(i + 1);
    Serial.print(": ");
    Serial.println(trackNames[i]);
  }
  Serial.println("-------------------------------------");
}

void initDFPlayer() {
  Serial.println("[DFPlayer] Initializing...");
  delay(1500); // Allow DFPlayer to boot

  if (dfPlayer.begin(dfSerial, true, true)) {
    dfPlayerReady = true;
    dfPlayer.setTimeOut(500);
    dfPlayer.volume(currentVolume);
    dfPlayer.EQ(DFPLAYER_EQ_NORMAL);
    dfPlayer.outputDevice(DFPLAYER_DEVICE_SD);
    Serial.print("[DFPlayer] Ready - Volume: ");
    Serial.print(currentVolume);
    Serial.println("/30");
  } else {
    dfPlayerReady = false;
    Serial.println("[DFPlayer] FAILED - Check wiring on ESP32 GPIO 39/40");
  }
}

void setup() {
  Serial.begin(115200);
  while (!Serial) {
    ; // Wait for serial port to connect (needed for native USB port only)
  }
  delay(100);

  // Initialize HardwareSerial for DFPlayer Mini (9600 baud)
  dfSerial.begin(9600, SERIAL_8N1, DF_UART_RX_PIN, DF_UART_TX_PIN);

  initDFPlayer();
  printHelp();
}

void loop() {
  // Keep DFPlayer communication alive and read status
  if (dfPlayerReady) {
    if (dfPlayer.available()) {
      printDetail(dfPlayer.readType(), dfPlayer.read());
    }
  }

  // Handle Serial commands
  if (Serial.available() > 0) {
    String input = Serial.readStringUntil('\n');
    input.trim();
    if (input.length() == 0) return;

    Serial.print("> Command received: ");
    Serial.println(input);

    if (input.equalsIgnoreCase("h")) {
      printHelp();
    } 
    else if (input.equalsIgnoreCase("s")) {
      if (dfPlayerReady) {
        dfPlayer.stop();
        Serial.println("[Audio] Stopped playback");
      }
    } 
    else if (input.equalsIgnoreCase("pause")) {
      if (dfPlayerReady) {
        dfPlayer.pause();
        Serial.println("[Audio] Paused playback");
      }
    } 
    else if (input.equalsIgnoreCase("resume")) {
      if (dfPlayerReady) {
        dfPlayer.start();
        Serial.println("[Audio] Resumed playback");
      }
    } 
    else if (input.equalsIgnoreCase("r")) {
      initDFPlayer();
    } 
    else if (input.startsWith("p ")) {
      String valStr = input.substring(2);
      valStr.trim();
      int trackNum = valStr.toInt();
      if (trackNum >= 1 && trackNum <= TOTAL_TRACKS) {
        if (dfPlayerReady) {
          Serial.print("[Audio] Playing Track ");
          Serial.print(trackNum);
          Serial.print(": ");
          Serial.println(trackNames[trackNum - 1]);
          dfPlayer.playFolder(1, trackNum);
        } else {
          Serial.println("[Error] DFPlayer is not ready!");
        }
      } else {
        Serial.print("[Error] Invalid track number. Enter a number between 1 and ");
        Serial.println(TOTAL_TRACKS);
      }
    } 
    else if (input.startsWith("v ")) {
      String valStr = input.substring(2);
      valStr.trim();
      int vol = valStr.toInt();
      if (vol >= 0 && vol <= 30) {
        if (dfPlayerReady) {
          currentVolume = vol;
          dfPlayer.volume(currentVolume);
          Serial.print("[Audio] Volume set to ");
          Serial.println(currentVolume);
        } else {
          Serial.println("[Error] DFPlayer is not ready!");
        }
      } else {
        Serial.println("[Error] Invalid volume level. Enter a number between 0 and 30.");
      }
    } 
    else {
      Serial.println("[Error] Unknown command. Type 'h' to see the help menu.");
    }
  }
}

// Print DFPlayer diagnostic messages
void printDetail(uint8_t type, int value) {
  switch (type) {
    case TimeOut:
      Serial.println(F("[DFPlayer Detail] Time Out!"));
      break;
    case WrongStack:
      Serial.println(F("[DFPlayer Detail] Stack Wrong!"));
      break;
    case DFPlayerCardInserted:
      Serial.println(F("[DFPlayer Detail] Card Inserted!"));
      break;
    case DFPlayerCardRemoved:
      Serial.println(F("[DFPlayer Detail] Card Removed!"));
      break;
    case DFPlayerCardOnline:
      Serial.println(F("[DFPlayer Detail] Card Online!"));
      break;
    case DFPlayerPlayFinished:
      Serial.print(F("[DFPlayer Detail] Play Finished! Track: "));
      Serial.println(value);
      break;
    case DFPlayerError:
      Serial.print(F("[DFPlayer Detail] Error: "));
      switch (value) {
        case Busy:
          Serial.println(F("Card Busy"));
          break;
        case Sleeping:
          Serial.println(F("Sleeping"));
          break;
        case SerialWrongStack:
          Serial.println(F("Wrong Stack"));
          break;
        case CheckSumNotMatch:
          Serial.println(F("CheckSum Not Match"));
          break;
        case FileIndexOut:
          Serial.println(F("File Index Out of Bound"));
          break;
        case FileMismatch:
          Serial.println(F("Cannot Find File"));
          break;
        case Advertise:
          Serial.println(F("In Advertise"));
          break;
        default:
          Serial.println(value);
          break;
      }
      break;
    default:
      break;
  }
}
