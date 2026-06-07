#include <HardwareSerial.h>
#include <DFRobotDFPlayerMini.h>

// ============================================================
// UART1 - Raspberry Pi Communication (GPIO 48=RX, 47=TX)
// ============================================================
#define PI_UART_RX_PIN 48
#define PI_UART_TX_PIN 47
HardwareSerial piSerial(1);

// ============================================================
// UART2 - DFPlayer Mini Communication (GPIO 39=RX, 40=TX)
// ============================================================
#define DF_UART_RX_PIN 39
#define DF_UART_TX_PIN 40
HardwareSerial dfSerial(2);
DFRobotDFPlayerMini dfPlayer;

// ============================================================
// Ultrasonic Sensor Pins (HC-SR04)
// ============================================================
#define TRIG_FRONT 12
#define ECHO_FRONT 1
#define TRIG_LEFT  13
#define ECHO_LEFT  2
#define TRIG_RIGHT 15
#define ECHO_RIGHT 42
#define TRIG_REAR  14
#define ECHO_REAR  41

// ============================================================
// Motor Driver Pins (L298N / TB6612)
// Mecanum Wheels in X-Configuration
// ============================================================
#define FL_IN1 16
#define FL_IN2 17
#define FL_ENA 5

#define FR_IN1 6
#define FR_IN2 7
#define FR_ENA 10

#define RL_IN1 18
#define RL_IN2 21
#define RL_ENA 4

#define RR_IN1 8
#define RR_IN2 9
#define RR_ENA 11

// ============================================================
// LEDC PWM Configuration
// ============================================================
#define FL_PWM_CH  0
#define FR_PWM_CH  1
#define RL_PWM_CH  2
#define RR_PWM_CH  3
#define PWM_FREQ   1000
#define PWM_RES    8

// Motor Speed (0-255)
#define MOTOR_SPEED      255
#define MOTOR_SPEED_HALF 128

// Obstacle Detection Threshold
#define OBSTACLE_THRESHOLD_CM 30

// Command timeout - stop motors if no command received
#define CMD_TIMEOUT_MS 500

// ============================================================
// DFPlayer Audio Track Mapping
// ============================================================
// SD Card folder /01/ must contain these tracks:
// 001.mp3 = "Moving Forward"
// 002.mp3 = "Moving Backward"
// 003.mp3 = "Strafing Left"
// 004.mp3 = "Strafing Right"
// 005.mp3 = "Moving Forward Left"
// 006.mp3 = "Moving Forward Right"
// 007.mp3 = "Moving Backward Left"
// 008.mp3 = "Moving Backward Right"
// 009.mp3 = "Stopping"
// 010.mp3 = "Obstacle Detected"
#define TRACK_FWD       1
#define TRACK_BACK      2
#define TRACK_LEFT      3
#define TRACK_RIGHT     4
#define TRACK_FWD_LEFT  5
#define TRACK_FWD_RIGHT 6
#define TRACK_BACK_LEFT 7
#define TRACK_BACK_RIGHT 8
#define TRACK_STOP      9
#define TRACK_OBSTACLE  10

// Audio announcement delay before movement (ms)
#define ANNOUNCE_DELAY_MS 800

// ============================================================
// Sensor Data
// ============================================================
float distances[4] = {999, 999, 999, 999};
const char* sensorNames[] = {"FRONT", "LEFT", "RIGHT", "REAR"};
int trigPins[] = {TRIG_FRONT, TRIG_LEFT, TRIG_RIGHT, TRIG_REAR};
int echoPins[] = {ECHO_FRONT, ECHO_LEFT, ECHO_RIGHT, ECHO_REAR};

// ============================================================
// State Variables
// ============================================================
String currentCommand    = "";
String lastCommand       = "";
unsigned long lastCmdTime = 0;
bool dfPlayerReady       = false;
bool isAnnouncing        = false;
unsigned long announceStartTime = 0;
String pendingCommand    = "";

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(115200);
  delay(100);

  // --- Pi UART ---
  piSerial.begin(115200, SERIAL_8N1, PI_UART_RX_PIN, PI_UART_TX_PIN);
  piSerial.setTimeout(50);

  // --- DFPlayer UART ---
  dfSerial.begin(9600, SERIAL_8N1, DF_UART_RX_PIN, DF_UART_TX_PIN);

  // --- Ultrasonic Pins ---
  for (int i = 0; i < 4; i++) {
    pinMode(trigPins[i], OUTPUT);
    pinMode(echoPins[i], INPUT);
    digitalWrite(trigPins[i], LOW);
  }

  // --- Motor Pins ---
  pinMode(FL_IN1, OUTPUT); pinMode(FL_IN2, OUTPUT);
  pinMode(FR_IN1, OUTPUT); pinMode(FR_IN2, OUTPUT);
  pinMode(RL_IN1, OUTPUT); pinMode(RL_IN2, OUTPUT);
  pinMode(RR_IN1, OUTPUT); pinMode(RR_IN2, OUTPUT);

  // --- LEDC PWM ---
  initPWM();

  // --- Stop Motors ---
  stopAllMotors();

  // --- DFPlayer Init ---
  initDFPlayer();

  Serial.println("================================");
  Serial.println("ESP32-S3 Mecanum Robot Ready");
  Serial.println("DFPlayer Audio + Obstacle Avoid");
  Serial.println("================================");
}

// ============================================================
// PWM INIT
// ============================================================
void initPWM() {
  ledcSetup(FL_PWM_CH, PWM_FREQ, PWM_RES);
  ledcAttachPin(FL_ENA, FL_PWM_CH);

  ledcSetup(FR_PWM_CH, PWM_FREQ, PWM_RES);
  ledcAttachPin(FR_ENA, FR_PWM_CH);

  ledcSetup(RL_PWM_CH, PWM_FREQ, PWM_RES);
  ledcAttachPin(RL_ENA, RL_PWM_CH);

  ledcSetup(RR_PWM_CH, PWM_FREQ, PWM_RES);
  ledcAttachPin(RR_ENA, RR_PWM_CH);
}

// ============================================================
// DFPLAYER INIT
// ============================================================
void initDFPlayer() {
  Serial.println("[DFPlayer] Initializing...");

  // Allow DFPlayer to boot
  delay(1500);

  if (dfPlayer.begin(dfSerial, true, true)) {
    dfPlayerReady = true;
    dfPlayer.setTimeOut(500);
    dfPlayer.volume(25);        // Volume: 0-30
    dfPlayer.EQ(DFPLAYER_EQ_NORMAL);
    dfPlayer.outputDevice(DFPLAYER_DEVICE_SD);
    Serial.println("[DFPlayer] Ready - Volume: 25/30");
  } else {
    dfPlayerReady = false;
    Serial.println("[DFPlayer] FAILED - Check wiring on GPIO 39/40");
    Serial.println("[DFPlayer] Continuing without audio...");
  }
}

// ============================================================
// AUDIO ANNOUNCEMENT
// ============================================================

// Map command string to track number
int commandToTrack(String cmd) {
  if (cmd == "FWD")        return TRACK_FWD;
  if (cmd == "BACK")       return TRACK_BACK;
  if (cmd == "LEFT")       return TRACK_LEFT;
  if (cmd == "RIGHT")      return TRACK_RIGHT;
  if (cmd == "FWD_LEFT")   return TRACK_FWD_LEFT;
  if (cmd == "FWD_RIGHT")  return TRACK_FWD_RIGHT;
  if (cmd == "BACK_LEFT")  return TRACK_BACK_LEFT;
  if (cmd == "BACK_RIGHT") return TRACK_BACK_RIGHT;
  if (cmd == "STOP")       return TRACK_STOP;
  return -1;  // Unknown
}

// Play audio announcement for a direction
void announceDirection(String cmd) {
  if (!dfPlayerReady) {
    Serial.println("[Audio] DFPlayer not ready - skipping announcement");
    return;
  }

  int track = commandToTrack(cmd);
  if (track < 0) {
    Serial.print("[Audio] No track for command: ");
    Serial.println(cmd);
    return;
  }

  Serial.print("[Audio] Playing track ");
  Serial.print(track);
  Serial.print(" for command: ");
  Serial.println(cmd);

  // Play from folder 01 on SD card
  dfPlayer.playFolder(1, track);
}

void announceObstacle() {
  if (!dfPlayerReady) return;
  dfPlayer.playFolder(1, TRACK_OBSTACLE);
  Serial.println("[Audio] Playing: Obstacle Detected");
}

// ============================================================
// MOTOR CONTROL
// ============================================================
void stopAllMotors() {
  digitalWrite(FL_IN1, LOW); digitalWrite(FL_IN2, LOW); ledcWrite(FL_PWM_CH, 0);
  digitalWrite(FR_IN1, LOW); digitalWrite(FR_IN2, LOW); ledcWrite(FR_PWM_CH, 0);
  digitalWrite(RL_IN1, LOW); digitalWrite(RL_IN2, LOW); ledcWrite(RL_PWM_CH, 0);
  digitalWrite(RR_IN1, LOW); digitalWrite(RR_IN2, LOW); ledcWrite(RR_PWM_CH, 0);
}

void setMotor(int in1, int in2, int pwmChannel, int speed) {
  speed = constrain(speed, -255, 255);

  if (speed == 0) {
    digitalWrite(in1, LOW);
    digitalWrite(in2, LOW);
    ledcWrite(pwmChannel, 0);
  } else if (speed > 0) {
    digitalWrite(in1, HIGH);
    digitalWrite(in2, LOW);
    ledcWrite(pwmChannel, (uint32_t)speed);
  } else {
    digitalWrite(in1, LOW);
    digitalWrite(in2, HIGH);
    ledcWrite(pwmChannel, (uint32_t)abs(speed));
  }
}

// ============================================================
// MECANUM MOVEMENT FUNCTIONS
// ============================================================
void moveForward() {
  setMotor(FL_IN1, FL_IN2, FL_PWM_CH,  MOTOR_SPEED);
  setMotor(FR_IN1, FR_IN2, FR_PWM_CH,  MOTOR_SPEED);
  setMotor(RL_IN1, RL_IN2, RL_PWM_CH,  MOTOR_SPEED);
  setMotor(RR_IN1, RR_IN2, RR_PWM_CH,  MOTOR_SPEED);
}

void moveBackward() {
  setMotor(FL_IN1, FL_IN2, FL_PWM_CH, -MOTOR_SPEED);
  setMotor(FR_IN1, FR_IN2, FR_PWM_CH, -MOTOR_SPEED);
  setMotor(RL_IN1, RL_IN2, RL_PWM_CH, -MOTOR_SPEED);
  setMotor(RR_IN1, RR_IN2, RR_PWM_CH, -MOTOR_SPEED);
}

void strafeLeft() {
  setMotor(FL_IN1, FL_IN2, FL_PWM_CH, -MOTOR_SPEED);
  setMotor(FR_IN1, FR_IN2, FR_PWM_CH,  MOTOR_SPEED);
  setMotor(RL_IN1, RL_IN2, RL_PWM_CH,  MOTOR_SPEED);
  setMotor(RR_IN1, RR_IN2, RR_PWM_CH, -MOTOR_SPEED);
}

void strafeRight() {
  setMotor(FL_IN1, FL_IN2, FL_PWM_CH,  MOTOR_SPEED);
  setMotor(FR_IN1, FR_IN2, FR_PWM_CH, -MOTOR_SPEED);
  setMotor(RL_IN1, RL_IN2, RL_PWM_CH, -MOTOR_SPEED);
  setMotor(RR_IN1, RR_IN2, RR_PWM_CH,  MOTOR_SPEED);
}

void moveForwardLeft() {
  setMotor(FL_IN1, FL_IN2, FL_PWM_CH,  MOTOR_SPEED_HALF);
  setMotor(FR_IN1, FR_IN2, FR_PWM_CH,  MOTOR_SPEED);
  setMotor(RL_IN1, RL_IN2, RL_PWM_CH,  MOTOR_SPEED_HALF);
  setMotor(RR_IN1, RR_IN2, RR_PWM_CH,  MOTOR_SPEED);
}

void moveForwardRight() {
  setMotor(FL_IN1, FL_IN2, FL_PWM_CH,  MOTOR_SPEED);
  setMotor(FR_IN1, FR_IN2, FR_PWM_CH,  MOTOR_SPEED_HALF);
  setMotor(RL_IN1, RL_IN2, RL_PWM_CH,  MOTOR_SPEED);
  setMotor(RR_IN1, RR_IN2, RR_PWM_CH,  MOTOR_SPEED_HALF);
}

void moveBackwardLeft() {
  setMotor(FL_IN1, FL_IN2, FL_PWM_CH, -MOTOR_SPEED_HALF);
  setMotor(FR_IN1, FR_IN2, FR_PWM_CH, -MOTOR_SPEED);
  setMotor(RL_IN1, RL_IN2, RL_PWM_CH, -MOTOR_SPEED_HALF);
  setMotor(RR_IN1, RR_IN2, RR_PWM_CH, -MOTOR_SPEED);
}

void moveBackwardRight() {
  setMotor(FL_IN1, FL_IN2, FL_PWM_CH, -MOTOR_SPEED);
  setMotor(FR_IN1, FR_IN2, FR_PWM_CH, -MOTOR_SPEED_HALF);
  setMotor(RL_IN1, RL_IN2, RL_PWM_CH, -MOTOR_SPEED);
  setMotor(RR_IN1, RR_IN2, RR_PWM_CH, -MOTOR_SPEED_HALF);
}

// ============================================================
// EXECUTE MOVEMENT (called after announcement delay)
// ============================================================
void executeMovement(String cmd) {
  Serial.print("[MOTOR] Executing: ");
  Serial.println(cmd);

  if      (cmd == "FWD")        moveForward();
  else if (cmd == "BACK")       moveBackward();
  else if (cmd == "LEFT")       strafeLeft();
  else if (cmd == "RIGHT")      strafeRight();
  else if (cmd == "FWD_LEFT")   moveForwardLeft();
  else if (cmd == "FWD_RIGHT")  moveForwardRight();
  else if (cmd == "BACK_LEFT")  moveBackwardLeft();
  else if (cmd == "BACK_RIGHT") moveBackwardRight();
  else                          stopAllMotors();
}

// ============================================================
// SENSOR READING
// ============================================================
float readDistance(int trigPin, int echoPin) {
  digitalWrite(trigPin, LOW);
  delayMicroseconds(2);
  digitalWrite(trigPin, HIGH);
  delayMicroseconds(10);
  digitalWrite(trigPin, LOW);

  long duration = pulseIn(echoPin, HIGH, 25000);
  if (duration == 0) return 999;

  float distance = duration * 0.034f / 2.0f;
  return (distance > 400) ? 999 : distance;
}

void readAllSensors() {
  for (int i = 0; i < 4; i++) {
    distances[i] = readDistance(trigPins[i], echoPins[i]);
    delay(10);
  }
}

// ============================================================
// DIRECTION-AWARE OBSTACLE CHECK
// ============================================================
bool isSafeToMove(String cmd) {
  bool frontBlocked = (distances[0] < OBSTACLE_THRESHOLD_CM && distances[0] > 0);
  bool leftBlocked  = (distances[1] < OBSTACLE_THRESHOLD_CM && distances[1] > 0);
  bool rightBlocked = (distances[2] < OBSTACLE_THRESHOLD_CM && distances[2] > 0);
  bool rearBlocked  = (distances[3] < OBSTACLE_THRESHOLD_CM && distances[3] > 0);

  if (cmd == "STOP")        return true;
  if (cmd == "FWD")         return !frontBlocked;
  if (cmd == "BACK")        return !rearBlocked;
  if (cmd == "LEFT")        return !leftBlocked;
  if (cmd == "RIGHT")       return !rightBlocked;
  if (cmd == "FWD_LEFT")    return !frontBlocked && !leftBlocked;
  if (cmd == "FWD_RIGHT")   return !frontBlocked && !rightBlocked;
  if (cmd == "BACK_LEFT")   return !rearBlocked  && !leftBlocked;
  if (cmd == "BACK_RIGHT")  return !rearBlocked  && !rightBlocked;

  return false;  // Unknown command - deny
}

// ============================================================
// COMMAND VALIDATION WHITELIST
// ============================================================
const char* VALID_COMMANDS[] = {
  "FWD", "BACK", "LEFT", "RIGHT",
  "FWD_LEFT", "FWD_RIGHT", "BACK_LEFT", "BACK_RIGHT", "STOP"
};
const int NUM_COMMANDS = 9;

bool isValidCommand(const String& cmd) {
  for (int i = 0; i < NUM_COMMANDS; i++) {
    if (cmd == VALID_COMMANDS[i]) return true;
  }
  return false;
}

// ============================================================
// SEND SENSOR DATA TO PI
// ============================================================
void sendDistances() {
  piSerial.print("DIST:");
  for (int i = 0; i < 4; i++) {
    if (i > 0) piSerial.print(",");
    piSerial.print(sensorNames[i]);
    piSerial.print(":");
    piSerial.print(distances[i]);
  }
  piSerial.println();
}

// ============================================================
// HANDLE INCOMING COMMAND WITH ANNOUNCE-THEN-MOVE LOGIC
// ============================================================
void handleNewCommand(String cmd) {
  // Same command already running - ignore
  if (cmd == currentCommand && cmd != "STOP") return;

  Serial.print("[CMD] Received: ");
  Serial.println(cmd);

  if (cmd == "STOP") {
    // Stop immediately - no announcement delay needed
    isAnnouncing  = false;
    pendingCommand = "";
    currentCommand = "STOP";
    stopAllMotors();
    announceDirection("STOP");
    lastCmdTime = millis();
    return;
  }

  // Safety check before announcing
  if (!isSafeToMove(cmd)) {
    Serial.print("[OBSTACLE] Blocked direction for: ");
    Serial.println(cmd);
    stopAllMotors();
    currentCommand = "STOP";

    // Only play obstacle alert if not already announcing it
    if (lastCommand != "OBSTACLE_ALERT") {
      announceObstacle();
      lastCommand = "OBSTACLE_ALERT";
    }

    piSerial.println("ALERT:OBSTACLE");
    return;
  }

  // New valid movement command - announce first, then move
  pendingCommand     = cmd;
  isAnnouncing       = true;
  announceStartTime  = millis();
  currentCommand     = cmd;
  lastCmdTime        = millis();

  // Stop motors during announcement
  stopAllMotors();

  // Play direction audio
  announceDirection(cmd);

  Serial.print("[Audio] Announcing: ");
  Serial.print(cmd);
  Serial.print(" - Moving in ");
  Serial.print(ANNOUNCE_DELAY_MS);
  Serial.println("ms");
}

// ============================================================
// LOOP
// ============================================================
void loop() {
  unsigned long now = millis();

  // --- Read Ultrasonic Sensors ---
  readAllSensors();

  // --- Send Data to Pi ---
  sendDistances();

  // --- Check Command Timeout (failsafe) ---
  if ((now - lastCmdTime) > CMD_TIMEOUT_MS && currentCommand != "STOP") {
    Serial.println("[FAILSAFE] No command received - stopping motors");
    stopAllMotors();
    currentCommand = "STOP";
    isAnnouncing   = false;
    pendingCommand  = "";
  }

  // --- Announce-then-Move State Machine ---
  if (isAnnouncing) {
    if ((now - announceStartTime) >= ANNOUNCE_DELAY_MS) {
      // Announcement delay elapsed - now execute movement
      isAnnouncing = false;

      // Re-check safety right before moving
      if (isSafeToMove(pendingCommand)) {
        executeMovement(pendingCommand);
        lastCommand = pendingCommand;
      } else {
        Serial.println("[OBSTACLE] Obstacle appeared during announcement - aborting move");
        stopAllMotors();
        currentCommand = "STOP";
        announceObstacle();
        piSerial.println("ALERT:OBSTACLE");
      }
      pendingCommand = "";
    }
    // While announcing - keep motors stopped (already done in handleNewCommand)
  }

  // --- Read Commands from Pi ---
  if (piSerial.available() > 0) {
    String line = piSerial.readStringUntil('\n');
    if (line.length() == 0) goto skip_cmd;

    line.trim();

    if (line.startsWith("MOTOR:")) {
      String cmd = line.substring(6);
      cmd.trim();

      if (!isValidCommand(cmd)) {
        Serial.print("[WARN] Invalid command rejected: ");
        Serial.println(cmd);
        goto skip_cmd;
      }

      handleNewCommand(cmd);
    }
  }

  skip_cmd:

  // --- Update DFPlayer state ---
  if (dfPlayerReady) {
    dfPlayer.readType();  // Keep DFPlayer communication alive
  }

  delay(30);
}