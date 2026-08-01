#include <AsyncTCP.h>
#include <ESPAsyncWebServer.h>
#include <ESPmDNS.h>
#include <HardwareSerial.h>
#include <WiFi.h>

#define ELEGANTOTA_USE_ASYNC_WEBSERVER 1
#include "arduino_secrets.h"
#include <ElegantOTA.h>

// ============================================================
// WiFi & ElegantOTA Configuration
// ============================================================
#define WIFI_SSID SECRET_SSID
#define WIFI_PASSWORD SECRET_PASS
#define USE_AP_MODE false // Set to false to use STA Mode (connecting to router)

AsyncWebServer server(80);

// ============================================================
// UART1 - Raspberry Pi Communication (GPIO 48=RX, 47=TX)
// ============================================================
#define PI_UART_RX_PIN 44
#define PI_UART_TX_PIN 43
HardwareSerial piSerial(1);

// ============================================================
// Ultrasonic Sensor Pins (HC-SR04)
// ============================================================
#define TRIG_FRONT 15
#define ECHO_FRONT 42
#define TRIG_LEFT 20
#define ECHO_LEFT 41
#define TRIG_RIGHT 19
#define ECHO_RIGHT 1
#define TRIG_REAR 13
#define ECHO_REAR 2

// ============================================================
// Voltage Sensor & RGB LED Pins
// ============================================================
#define VOLTAGE_PIN 3

#define RGB_RED_PIN 36
#define RGB_GREEN_PIN 37
#define RGB_BLUE_PIN 38

// Voltage Divider Ratio (Default 5.0 for standard 25V divider module: 30k/7.5k)
#define VOLTAGE_DIVIDER_RATIO 5.0f
#define ADC_REF_VOLTAGE 3.3f
#define ADC_RESOLUTION 4095.0f

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
#define FL_PWM_CH 0
#define FR_PWM_CH 1
#define RL_PWM_CH 2
#define RR_PWM_CH 3

#define RGB_RED_CH 4
#define RGB_GREEN_CH 5
#define RGB_BLUE_CH 6

#define PWM_FREQ 1000
#define PWM_RES 8

// Motor Speed (0-255)
// Set default speed to 255
#define MOTOR_SPEED 255
#define MOTOR_SPEED_HALF 128

// Obstacle Detection Threshold
#define OBSTACLE_THRESHOLD_CM 50

// Command timeout - stop motors if no command received
#define CMD_TIMEOUT_MS 2000

// ============================================================
// Sensor Data
// ============================================================
float distances[4] = {999, 999, 999, 999};
const char *sensorNames[] = {"FRONT", "LEFT", "RIGHT", "REAR"};
int trigPins[] = {TRIG_FRONT, TRIG_LEFT, TRIG_RIGHT, TRIG_REAR};
int echoPins[] = {ECHO_FRONT, ECHO_LEFT, ECHO_RIGHT, ECHO_REAR};

float currentVoltage = 0.0f;

// ============================================================
// State Variables & Declarations
// ============================================================
String currentCommand = "";
String lastCommand = "";
unsigned long lastCmdTime = 0;

void setRGBColor(uint8_t red, uint8_t green, uint8_t blue);
float readVoltage();
void updateVoltageLED(float v);

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(115200);
  delay(100);

  // --- Pi UART ---
  piSerial.begin(115200, SERIAL_8N1, PI_UART_RX_PIN, PI_UART_TX_PIN);
  piSerial.setTimeout(50);

  // --- WiFi & ElegantOTA ---
  if (USE_AP_MODE) {
    Serial.println("[WiFi] Starting Access Point...");
    WiFi.softAP(WIFI_SSID, WIFI_PASSWORD);
    Serial.print("[WiFi] AP IP Address: ");
    Serial.println(WiFi.softAPIP());
  } else {
    Serial.print("[WiFi] Connecting to ");
    Serial.println(WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    int retries = 0;
    while (WiFi.status() != WL_CONNECTED && retries < 20) {
      delay(500);
      Serial.print(".");
      retries++;
    }
    if (WiFi.status() == WL_CONNECTED) {
      Serial.println("\n[WiFi] Connected!");
      Serial.print("[WiFi] IP Address: ");
      Serial.println(WiFi.localIP());
    } else {
      Serial.println("\n[WiFi] Connection Failed. Falling back to AP Mode...");
      WiFi.softAP(WIFI_SSID, WIFI_PASSWORD);
      Serial.print("[WiFi] AP IP Address: ");
      Serial.println(WiFi.softAPIP());
    }
  }

  // --- mDNS responder ---
  if (MDNS.begin("fitness-robot")) {
    Serial.println(
        "[mDNS] Responder started. Access via http://fitness-robot.local");
  }

  server.on("/", HTTP_GET, [](AsyncWebServerRequest *request) {
    request->send(200, "text/plain",
                  "Fitness Robot ESP32 Ready. Go to "
                  "http://fitness-robot.local/update to upload firmware OTA.");
  });

  ElegantOTA.begin(&server);
  server.begin();
  Serial.println("[OTA] HTTP server started, ElegantOTA ready");

  // --- Ultrasonic Pins ---
  for (int i = 0; i < 4; i++) {
    pinMode(trigPins[i], OUTPUT);
    pinMode(echoPins[i], INPUT);
    digitalWrite(trigPins[i], LOW);
  }

  // --- Voltage Sensor & RGB LED Pins ---
  pinMode(VOLTAGE_PIN, INPUT);
  analogReadResolution(12);

  pinMode(RGB_RED_PIN, OUTPUT);
  pinMode(RGB_GREEN_PIN, OUTPUT);
  pinMode(RGB_BLUE_PIN, OUTPUT);

  // --- Motor Pins ---
  pinMode(FL_IN1, OUTPUT);
  pinMode(FL_IN2, OUTPUT);
  pinMode(FR_IN1, OUTPUT);
  pinMode(FR_IN2, OUTPUT);
  pinMode(RL_IN1, OUTPUT);
  pinMode(RL_IN2, OUTPUT);
  pinMode(RR_IN1, OUTPUT);
  pinMode(RR_IN2, OUTPUT);

  // --- LEDC PWM ---
  initPWM();

  // --- Stop Motors & Turn Off LED ---
  stopAllMotors();
  setRGBColor(0, 0, 0);

  Serial.println("================================");
  Serial.println("ESP32-S3 Mecanum Robot Ready (OTA Enabled)");
  Serial.println("================================");
}

// ============================================================
// PWM INIT
// ============================================================
void initPWM() {
  // Motor PWM Setup
  ledcSetup(FL_PWM_CH, PWM_FREQ, PWM_RES);
  ledcAttachPin(FL_ENA, FL_PWM_CH);

  ledcSetup(FR_PWM_CH, PWM_FREQ, PWM_RES);
  ledcAttachPin(FR_ENA, FR_PWM_CH);

  ledcSetup(RL_PWM_CH, PWM_FREQ, PWM_RES);
  ledcAttachPin(RL_ENA, RL_PWM_CH);

  ledcSetup(RR_PWM_CH, PWM_FREQ, PWM_RES);
  ledcAttachPin(RR_ENA, RR_PWM_CH);

  // RGB LED PWM Setup
  ledcSetup(RGB_RED_CH, PWM_FREQ, PWM_RES);
  ledcAttachPin(RGB_RED_PIN, RGB_RED_CH);

  ledcSetup(RGB_GREEN_CH, PWM_FREQ, PWM_RES);
  ledcAttachPin(RGB_GREEN_PIN, RGB_GREEN_CH);

  ledcSetup(RGB_BLUE_CH, PWM_FREQ, PWM_RES);
  ledcAttachPin(RGB_BLUE_PIN, RGB_BLUE_CH);
}

// ============================================================
// MOTOR CONTROL
// ============================================================
void stopAllMotors() {
  digitalWrite(FL_IN1, LOW);
  digitalWrite(FL_IN2, LOW);
  ledcWrite(FL_PWM_CH, 0);
  digitalWrite(FR_IN1, LOW);
  digitalWrite(FR_IN2, LOW);
  ledcWrite(FR_PWM_CH, 0);
  digitalWrite(RL_IN1, LOW);
  digitalWrite(RL_IN2, LOW);
  ledcWrite(RL_PWM_CH, 0);
  digitalWrite(RR_IN1, LOW);
  digitalWrite(RR_IN2, LOW);
  ledcWrite(RR_PWM_CH, 0);
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
  setMotor(FL_IN1, FL_IN2, FL_PWM_CH, MOTOR_SPEED);
  setMotor(FR_IN1, FR_IN2, FR_PWM_CH, MOTOR_SPEED);
  setMotor(RL_IN1, RL_IN2, RL_PWM_CH, MOTOR_SPEED);
  setMotor(RR_IN1, RR_IN2, RR_PWM_CH, MOTOR_SPEED);
}

void moveBackward() {
  setMotor(FL_IN1, FL_IN2, FL_PWM_CH, -MOTOR_SPEED);
  setMotor(FR_IN1, FR_IN2, FR_PWM_CH, -MOTOR_SPEED);
  setMotor(RL_IN1, RL_IN2, RL_PWM_CH, -MOTOR_SPEED);
  setMotor(RR_IN1, RR_IN2, RR_PWM_CH, -MOTOR_SPEED);
}

void strafeLeft() {
  setMotor(FL_IN1, FL_IN2, FL_PWM_CH, -MOTOR_SPEED);
  setMotor(FR_IN1, FR_IN2, FR_PWM_CH, MOTOR_SPEED);
  setMotor(RL_IN1, RL_IN2, RL_PWM_CH, MOTOR_SPEED);
  setMotor(RR_IN1, RR_IN2, RR_PWM_CH, -MOTOR_SPEED);
}

void strafeRight() {
  setMotor(FL_IN1, FL_IN2, FL_PWM_CH, MOTOR_SPEED);
  setMotor(FR_IN1, FR_IN2, FR_PWM_CH, -MOTOR_SPEED);
  setMotor(RL_IN1, RL_IN2, RL_PWM_CH, -MOTOR_SPEED);
  setMotor(RR_IN1, RR_IN2, RR_PWM_CH, MOTOR_SPEED);
}

void veerLeft() {
  setMotor(FL_IN1, FL_IN2, FL_PWM_CH, -MOTOR_SPEED);
  setMotor(FR_IN1, FR_IN2, FR_PWM_CH, MOTOR_SPEED);
  setMotor(RL_IN1, RL_IN2, RL_PWM_CH, -MOTOR_SPEED);
  setMotor(RR_IN1, RR_IN2, RR_PWM_CH, MOTOR_SPEED);
}

void veerRight() {
  setMotor(FL_IN1, FL_IN2, FL_PWM_CH, MOTOR_SPEED);
  setMotor(FR_IN1, FR_IN2, FR_PWM_CH, -MOTOR_SPEED);
  setMotor(RL_IN1, RL_IN2, RL_PWM_CH, MOTOR_SPEED);
  setMotor(RR_IN1, RR_IN2, RR_PWM_CH, -MOTOR_SPEED);
}

// ============================================================
// EXECUTE MOVEMENT
// ============================================================
void executeMovement(String cmd) {
  Serial.print("[MOTOR] Executing: ");
  Serial.println(cmd);

  if (cmd == "FWD")
    moveForward();
  else if (cmd == "BACK")
    moveBackward();
  else if (cmd == "LEFT")
    strafeLeft();
  else if (cmd == "RIGHT")
    strafeRight();
  else if (cmd == "VEER_LEFT")
    veerLeft();
  else if (cmd == "VEER_RIGHT")
    veerRight();
  else
    stopAllMotors();
}

// ============================================================
// VOLTAGE SENSOR & RGB LED CONTROL
// ============================================================
void setRGBColor(uint8_t red, uint8_t green, uint8_t blue) {
  ledcWrite(RGB_RED_CH, red);
  ledcWrite(RGB_GREEN_CH, green);
  ledcWrite(RGB_BLUE_CH, blue);
}

float readVoltage() {
  int sum = 0;
  const int samples = 10;
  for (int i = 0; i < samples; i++) {
    sum += analogRead(VOLTAGE_PIN);
    delayMicroseconds(50);
  }
  float rawAdc = (float)sum / (float)samples;
  float pinVoltage = (rawAdc / ADC_RESOLUTION) * ADC_REF_VOLTAGE;
  currentVoltage = pinVoltage * VOLTAGE_DIVIDER_RATIO;
  return currentVoltage;
}

void updateVoltageLED(float v) {
  if (v < 10.0f) {
    // Red when V < 10V
    setRGBColor(255, 0, 0);
  } else if (v >= 10.0f && v < 11.0f) {
    // Orange when 10V <= V < 11V
    setRGBColor(255, 64, 0);
  } else {
    // Green when V >= 11V
    setRGBColor(0, 255, 0);
  }
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
  if (duration == 0)
    return 999;

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
  bool frontBlocked =
      (distances[0] < OBSTACLE_THRESHOLD_CM && distances[0] > 0);
  bool leftBlocked = (distances[1] < OBSTACLE_THRESHOLD_CM && distances[1] > 0);
  bool rightBlocked =
      (distances[2] < OBSTACLE_THRESHOLD_CM && distances[2] > 0);
  bool rearBlocked = (distances[3] < OBSTACLE_THRESHOLD_CM && distances[3] > 0);

  if (cmd == "STOP")
    return true;
  if (cmd == "FWD")
    return !frontBlocked;
  if (cmd == "BACK")
    return !rearBlocked;
  if (cmd == "LEFT")
    return !leftBlocked;
  if (cmd == "RIGHT")
    return !rightBlocked;
  if (cmd == "VEER_LEFT")
    return !leftBlocked;
  if (cmd == "VEER_RIGHT")
    return !rightBlocked;

  return false; // Unknown command - deny
}

// ============================================================
// COMMAND VALIDATION WHITELIST
// ============================================================
const char *VALID_COMMANDS[] = {"FWD",       "BACK",       "LEFT", "RIGHT",
                                "VEER_LEFT", "VEER_RIGHT", "STOP"};
const int NUM_COMMANDS = 7;

bool isValidCommand(const String &cmd) {
  for (int i = 0; i < NUM_COMMANDS; i++) {
    if (cmd == VALID_COMMANDS[i])
      return true;
  }
  return false;
}

// ============================================================
// SEND SENSOR DATA TO PI
// ============================================================
void sendDistances() {
  piSerial.print("DIST:");
  for (int i = 0; i < 4; i++) {
    if (i > 0)
      piSerial.print(",");
    piSerial.print(sensorNames[i]);
    piSerial.print(":");
    piSerial.print(distances[i]);
  }
  piSerial.print(",VOLT:");
  piSerial.print(currentVoltage, 2);
  piSerial.println();
}

// ============================================================
// HANDLE INCOMING COMMAND
// ============================================================
void handleNewCommand(String cmd) {
  // Same command already running - refresh timeout
  if (cmd == currentCommand && cmd != "STOP") {
    lastCmdTime = millis(); // Keep the failsafe timer alive
    return;
  }

  Serial.print("[CMD] Received: ");
  Serial.println(cmd);

  if (cmd == "STOP") {
    currentCommand = "STOP";
    stopAllMotors();
    lastCmdTime = millis();
    return;
  }

  // Safety check before moving
  if (!isSafeToMove(cmd)) {
    Serial.print("[OBSTACLE] Blocked direction for: ");
    Serial.println(cmd);
    stopAllMotors();
    currentCommand = "STOP";
    piSerial.println("ALERT:OBSTACLE");
    return;
  }

  // Execute immediately
  executeMovement(cmd);
  currentCommand = cmd;
  lastCmdTime = millis();
}

// ============================================================
// LOOP
// ============================================================
void loop() {
  unsigned long now = millis();

  // --- Read Ultrasonic Sensors ---
  readAllSensors();

  // --- Read Voltage Sensor & Update RGB LED ---
  readVoltage();
  updateVoltageLED(currentVoltage);

  // --- Check Obstacle Safety during movement ---
  if (currentCommand != "STOP" && !isSafeToMove(currentCommand)) {
    Serial.print("[OBSTACLE] Sudden block detected! Stopping motors for: ");
    Serial.println(currentCommand);
    stopAllMotors();
    currentCommand = "STOP";
    piSerial.println("ALERT:OBSTACLE");
  }

  // --- Send Data to Pi ---
  sendDistances();

  // --- Check Command Timeout (failsafe) ---
  if ((now - lastCmdTime) > CMD_TIMEOUT_MS && currentCommand != "STOP") {
    Serial.println("[FAILSAFE] No command received - stopping motors");
    stopAllMotors();
    currentCommand = "STOP";
  }

  // --- Read Commands from Pi ---
  if (piSerial.available() > 0) {
    String line = piSerial.readStringUntil('\n');
    if (line.length() == 0)
      goto skip_cmd;

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
    } else if (line.equalsIgnoreCase("PING")) {
      piSerial.println("PONG");
      Serial.println("[CMD] Received PING - Responded PONG");
    }
  }

skip_cmd:

  // --- Handle ElegantOTA Updates ---
  ElegantOTA.loop();

  delay(30);
}