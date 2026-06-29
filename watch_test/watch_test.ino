#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Preferences.h>
#include <NimBLEDevice.h>
#include <time.h>
#include <sys/time.h>

// ==================== HARDWARE PINS ====================
#define SCREEN_WIDTH   128
#define SCREEN_HEIGHT   64
#define OLED_RESET      -1
#define SCREEN_ADDRESS  0x3C
#define SDA_PIN          8
#define SCL_PIN          9
#define LED_PIN         10

// ==================== DEVICE CONFIG ====================
#define DEVICE_NAME    "StepTracker" // ✅ Fixed missing macro definition

// ==================== NVS SETTINGS ====================
#define NVS_NAMESPACE  "stepcounter"
#define NVS_KEY        "stepcount"
#define NVS_KEY_DATE   "lastdate"
#define NVS_KEY_ACTIVE "activesec"

// ==================== BLE UUIDs ====================
#define SERVICE_UUID        "12345678-1234-1234-1234-123456789001"
#define STEP_COUNT_UUID     "12345678-1234-1234-1234-123456789002"
#define TIME_SYNC_UUID      "12345678-1234-1234-1234-123456789003"
#define DAILY_DATA_UUID     "12345678-1234-1234-1234-123456789004"
#define COMMAND_UUID        "12345678-1234-1234-1234-123456789005"

// ==================== PERSISTENCE ====================
const unsigned long SAVE_INTERVAL_MS = 30000;
const int           SAVE_MIN_CHANGE  = 5;
unsigned long lastSaveTime   = 0;
int           lastSavedCount = 0;

// ==================== STEP DETECTION ====================
int           stepCount      = 0;
bool          stepFlag       = false;
unsigned long lastStepTime   = 0;
float filteredAx = 0, filteredAy = 0, filteredAz = 0;
const float   alpha          = 0.3f;
float         stepThreshold  = 14.0f;
float         resetThreshold =  9.0f;
float         prevMagnitude  = 9.8f;
float         jerkRate       = 0;
const float   MAX_JERK_RATE  = 1500.0f;
const float   MIN_WALKING_DURATION = 60;

#define WINDOW_SIZE 30
float   magWindow[WINDOW_SIZE];
uint8_t wIndex  = 0;
bool    wFilled = false;
float   dynMid  = 0.0f;

unsigned long stepArmTime = 0;
bool          stepArmed   = false;

// ==================== DISPLAY / STATS ====================
const int   dailyGoal     = 10000;
const float calPerStep    = 0.04f;
const float metersPerStep = 0.75f;

unsigned long activeSeconds     = 0;
unsigned long lastActiveCheck   = 0;
bool          isActive          = false;
unsigned long lastDisplayUpdate = 0;
const int     displayInterval   = 100;
unsigned long lastStepDetected  = 0;
bool          showStepFlash     = false;

// ==================== TIME ====================
bool timeSynced    = false;
int  lastResetDay  = -1;
int  currentHour   = 0;
int  currentMinute = 0;

// ==================== BLE ====================
bool          bleConnected = false;
unsigned long lastBLENotify = 0;
const int     BLE_NOTIFY_INTERVAL = 2000;

// ==================== OBJECTS ====================
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);
Adafruit_MPU6050 mpu;
Preferences      prefs;

bool displayEnabled = false;
bool mpuEnabled     = false;

NimBLECharacteristic* pStepCountChar;
NimBLECharacteristic* pTimeSyncChar;
NimBLECharacteristic* pDailyDataChar;
NimBLECharacteristic* pCommandChar;

// Forward declarations
void notifyStepCount();
void notifyDailyData();
void checkMidnightReset();
void printHelpMenu();
void printStatus();
void handleSerialDebug();

// ==================== NVS ====================

void loadStepCount() {
  prefs.begin(NVS_NAMESPACE, false);
  stepCount      = prefs.getInt(NVS_KEY, 0);
  lastSavedCount = stepCount;
  lastResetDay   = prefs.getInt(NVS_KEY_DATE, -1);
  activeSeconds  = prefs.getULong(NVS_KEY_ACTIVE, 0);
  prefs.end();

  Serial.print(F("Loaded steps: "));
  Serial.println(stepCount);
}

void saveStepCount() {
  if (stepCount == lastSavedCount) return;

  prefs.begin(NVS_NAMESPACE, false);
  prefs.putInt(NVS_KEY, stepCount);
  prefs.putULong(NVS_KEY_ACTIVE, activeSeconds);
  prefs.end();

  lastSavedCount = stepCount;
  lastSaveTime   = millis();
}

void saveResetDay(int day) {
  prefs.begin(NVS_NAMESPACE, false);
  prefs.putInt(NVS_KEY_DATE, day);
  prefs.end();
}

// ==================== TIME ====================

void setTimeFromUnix(unsigned long unixTime) {
  struct timeval tv;
  tv.tv_sec  = unixTime;
  tv.tv_usec = 0;
  settimeofday(&tv, NULL);

  setenv("TZ", "IST-5:30", 1);
  tzset();

  timeSynced = true;

  struct tm timeinfo;
  if (getLocalTime(&timeinfo)) {
    Serial.print(F("Time set: "));
    Serial.print(timeinfo.tm_hour);
    Serial.print(F(":"));
    Serial.print(timeinfo.tm_min);
    Serial.print(F(":"));
    Serial.println(timeinfo.tm_sec);
  }
}

void updateCurrentTime() {
  if (!timeSynced) return;
  struct tm timeinfo;
  if (getLocalTime(&timeinfo)) {
    currentHour   = timeinfo.tm_hour;
    currentMinute = timeinfo.tm_min;
  }
}

void checkMidnightReset() {
  if (!timeSynced) return;

  struct tm timeinfo;
  if (!getLocalTime(&timeinfo)) return;

  int today = timeinfo.tm_mday;

  if (lastResetDay == -1) {
    lastResetDay = today;
    saveResetDay(today);
    return;
  }

  if (lastResetDay != today) {
    Serial.println(F("=== MIDNIGHT RESET ==="));
    Serial.print(F("Previous steps: "));
    Serial.println(stepCount);

    notifyDailyData();

    stepCount      = 0;
    lastSavedCount = 0;
    activeSeconds  = 0;
    lastResetDay   = today;
    saveStepCount();
    saveResetDay(today);

    if (displayEnabled) {
      display.clearDisplay();
      display.setTextSize(2);
      display.setCursor(10, 20);
      display.println("NEW DAY!");
      display.setTextSize(1);
      display.setCursor(10, 45);
      display.println("Steps reset to 0");
      display.display();
      delay(3000);
    }
  }
}

// ==================== BLE CALLBACKS ====================

class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* pServer, NimBLEConnInfo& connInfo) override {
    bleConnected = true;
    Serial.println(F("BLE: Client connected"));
  }

  void onDisconnect(NimBLEServer* pServer, NimBLEConnInfo& connInfo, int reason) override {
    bleConnected = false;
    Serial.print(F("BLE: Disconnected, reason: "));
    Serial.println(reason);
    NimBLEDevice::startAdvertising();
    Serial.println(F("Advertising restarted"));
  }
};

class TimeSyncCallback : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* pCharacteristic, NimBLEConnInfo& connInfo) override {
    std::string value = pCharacteristic->getValue();

    if (value.length() >= 4) {
      uint32_t unixTime = 0;
      memcpy(&unixTime, value.data(), 4);

      Serial.print(F("BLE: Time received: "));
      Serial.println(unixTime);

      setTimeFromUnix(unixTime);
      checkMidnightReset();
    }
  }
};

class CommandCallback : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* pCharacteristic, NimBLEConnInfo& connInfo) override {
    std::string value = pCharacteristic->getValue();
    String cmd = String(value.c_str());
    cmd.trim();

    Serial.print(F("BLE: Command: "));
    Serial.println(cmd);

    if (cmd == "RESET") {
      stepCount     = 0;
      activeSeconds = 0;
      saveStepCount();
      notifyStepCount();
      notifyDailyData();
      Serial.println(F("Steps reset"));
    }
    else if (cmd == "SYNC") {
      notifyStepCount();
      notifyDailyData();
      Serial.println(F("Data synced"));
    }
    else if (cmd == "PING") {
      pCommandChar->setValue("PONG");
      pCommandChar->notify();
    }
  }
};

// ==================== BLE NOTIFY ====================

void notifyStepCount() {
  if (!bleConnected) return;
  String stepStr = String(stepCount);
  pStepCountChar->setValue(stepStr.c_str());
  pStepCountChar->notify();
}

void notifyDailyData() {
  if (!bleConnected) return;

  float calories   = stepCount * calPerStep;
  float distanceKm = (stepCount * metersPerStep) / 1000.0f;
  int   activeMin  = activeSeconds / 60;
  int   pct        = (stepCount * 100) / dailyGoal;

  String json = "{";
  json += "\"steps\":" + String(stepCount) + ",";
  json += "\"cal\":"   + String(calories, 1) + ",";
  json += "\"km\":"    + String(distanceKm, 2) + ",";
  json += "\"min\":"   + String(activeMin) + ",";
  json += "\"goal\":"  + String(dailyGoal) + ",";
  json += "\"pct\":"   + String(pct);
  json += "}";

  pDailyDataChar->setValue(json.c_str());
  pDailyDataChar->notify();

  Serial.print(F("BLE sent: "));
  Serial.println(json);
}

// ==================== BLE SETUP ====================

void setupBLE() {
  Serial.println(F("Starting BLE..."));

  NimBLEDevice::init(DEVICE_NAME);
  NimBLEDevice::setPower(9);
  NimBLEDevice::setMTU(247);

  NimBLEServer* pServer = NimBLEDevice::createServer();
  pServer->setCallbacks(new ServerCallbacks());

  NimBLEService* pService = pServer->createService(SERVICE_UUID);

  pStepCountChar = pService->createCharacteristic(
    STEP_COUNT_UUID,
    NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY
  );
  pStepCountChar->setValue("0");

  pTimeSyncChar = pService->createCharacteristic(
    TIME_SYNC_UUID,
    NIMBLE_PROPERTY::WRITE
  );
  pTimeSyncChar->setCallbacks(new TimeSyncCallback());

  pDailyDataChar = pService->createCharacteristic(
    DAILY_DATA_UUID,
    NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY
  );
  pDailyDataChar->setValue("{}");

  pCommandChar = pService->createCharacteristic(
    COMMAND_UUID,
    NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY
  );
  pCommandChar->setCallbacks(new CommandCallback());

  pService->start();

  // ============ ADVERTISING ============
  NimBLEAdvertising* pAdvertising = NimBLEDevice::getAdvertising();

  NimBLEAdvertisementData advData;
  advData.setName(DEVICE_NAME);                    
  advData.setCompleteServices(NimBLEUUID(SERVICE_UUID));
  advData.setFlags(BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP);

  NimBLEAdvertisementData scanData;
  scanData.setName(DEVICE_NAME);                   

  pAdvertising->setAdvertisementData(advData);
  pAdvertising->setScanResponseData(scanData);

  pAdvertising->setMinInterval(0x20);
  pAdvertising->setMaxInterval(0x40);

  pAdvertising->start();

  Serial.print(F("BLE advertising as: "));
  Serial.println(DEVICE_NAME);
  Serial.print(F("MAC: "));
  Serial.println(NimBLEDevice::getAddress().toString().c_str());
}

// ==================== STEP DETECTION ====================

void detectStep(float magnitude, float threshold, float ax, float ay, float az) {
  unsigned long now    = millis();
  float         zRatio = abs(az) / magnitude;

  if (!stepFlag && magnitude > threshold) {
    bool isJerk = (jerkRate > MAX_JERK_RATE) && (zRatio < 0.2f);

    if (!isJerk) {
      if (!stepArmed) {
        stepArmed   = true;
        stepArmTime = now;
      } else if ((float)(now - stepArmTime) >= MIN_WALKING_DURATION) {
        stepCount++;
        lastStepTime     = now;
        lastStepDetected = now;
        stepFlag         = true;
        showStepFlash    = true;
        stepArmed        = false;
      }
    } else {
      stepArmed = false;
    }
  }

  if (stepFlag && magnitude < resetThreshold) {
    stepFlag  = false;
    stepArmed = false;
  }

  if (stepArmed && (float)(now - stepArmTime) > 250.0f) {
    stepArmed = false;
  }
}

void trackActiveTime(float magnitude) {
  isActive = (magnitude > 11.0f || magnitude < 8.5f);

  unsigned long now = millis();
  if (now - lastActiveCheck >= 1000) {
    if (isActive) activeSeconds++;
    lastActiveCheck = now;
  }
}

// ==================== DISPLAY ====================

void drawStepWatch() {
  display.clearDisplay();

  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print("STEPS");

  display.setCursor(40, 0);
  if (bleConnected) {
    display.print("[BLE]");
  } else {
    if ((millis() / 1000) % 2 == 0) {
      display.print(" ble ");
    }
  }

  if (timeSynced) {
    char timeBuf[6];
    sprintf(timeBuf, "%02d:%02d", currentHour, currentMinute);
    display.setCursor(98, 0);
    display.print(timeBuf);
  } else {
    display.setCursor(92, 0);
    display.print("--:--");
  }

  display.drawFastHLine(0, 10, 128, SSD1306_WHITE);
  display.drawFastHLine(0, 12, 128, SSD1306_WHITE);

  display.setTextSize(3);
  String stepStr   = String(stepCount);
  int    textWidth = stepStr.length() * 21;
  int    startX    = max(0, (SCREEN_WIDTH - textWidth) / 2);
  display.setCursor(startX, 16);
  display.print(stepCount);

  display.drawFastHLine(0, 40, 128, SSD1306_WHITE);

  int progressWidth = (int)map(
    (long)constrain(stepCount, 0, dailyGoal),
    0L, (long)dailyGoal, 0L, 124L);
  progressWidth = constrain(progressWidth, 0, 124);

  display.drawRect(2, 43, 124, 7, SSD1306_WHITE);
  if (progressWidth > 0) {
    display.fillRect(2, 43, progressWidth, 7, SSD1306_WHITE);
  }

  float calories   = stepCount * calPerStep;
  float distanceKm = (stepCount * metersPerStep) / 1000.0f;
  int   activeMin  = activeSeconds / 60;

  display.setTextSize(1);
  display.setCursor(0, 55);
  display.print(calories, 0);
  display.print("cal");

  display.setCursor(45, 55);
  display.print(distanceKm, 2);
  display.print("km");

  display.setCursor(95, 55);
  display.print(activeMin);
  display.print("min");

  if (stepCount >= dailyGoal) {
    if ((millis() / 500) % 2 == 0) {
      display.drawRect(0, 0, 128, 64, SSD1306_WHITE);
      display.drawRect(1, 1, 126, 62, SSD1306_WHITE);
    }
  }

  display.display();
}

// ==================== ERROR ====================

void blinkError() {
  pinMode(LED_PIN, OUTPUT);
  while (true) {
    digitalWrite(LED_PIN, HIGH);
    delay(100);
    digitalWrite(LED_PIN, LOW);
    delay(100);
  }
}

// ==================== DEBUG INTERFACE ====================

void printHelpMenu() {
  Serial.println(F("\n=== ESP32-S3 Watch Debug Menu ==="));
  Serial.println(F("  h or ?  : Show this help menu"));
  Serial.println(F("  s       : Simulate a single step (adds 1 step, adds 10s active time)"));
  Serial.println(F("  c <num> : Set step count to <num> (e.g., c 2500)"));
  Serial.println(F("  a <sec> : Set active time to <sec> seconds (e.g., a 300)"));
  Serial.println(F("  d       : Force send step count and daily data over BLE"));
  Serial.println(F("  r       : Reset step count and active time to 0"));
  Serial.println(F("  p       : Print current watch status and diagnostic info"));
  Serial.println(F("  l       : Toggle onboard LED (GPIO 10)"));
  Serial.println(F("=================================\n"));
}

void printStatus() {
  Serial.println(F("\n=== Watch Status ==="));
  Serial.print(F("  Display initialized: "));
  Serial.println(displayEnabled ? F("YES") : F("NO"));
  Serial.print(F("  MPU6050 initialized: "));
  Serial.println(mpuEnabled ? F("YES") : F("NO"));
  Serial.print(F("  BLE Connected: "));
  Serial.println(bleConnected ? F("YES") : F("NO"));
  Serial.print(F("  Time Synced: "));
  Serial.println(timeSynced ? F("YES") : F("NO"));
  if (timeSynced) {
    struct tm timeinfo;
    if (getLocalTime(&timeinfo)) {
      char timeBuf[30];
      strftime(timeBuf, sizeof(timeBuf), "%Y-%m-%d %H:%M:%S", &timeinfo);
      Serial.print(F("  Current Time: "));
      Serial.println(timeBuf);
    }
  }
  Serial.print(F("  Step Count: "));
  Serial.println(stepCount);
  Serial.print(F("  Active Time: "));
  Serial.print(activeSeconds);
  Serial.println(F(" seconds"));
  Serial.print(F("  BLE Advertising: "));
  Serial.println(NimBLEDevice::getAdvertising()->isAdvertising() ? F("YES") : F("NO"));
  Serial.println(F("====================\n"));
}

void handleSerialDebug() {
  if (!Serial.available()) return;

  String input = Serial.readStringUntil('\n');
  input.trim();
  if (input.length() == 0) return;

  char cmd = input.charAt(0);

  if (cmd == 'h' || cmd == '?') {
    printHelpMenu();
  } 
  else if (cmd == 's') {
    stepCount++;
    activeSeconds += 10;
    Serial.print(F("Debug: Step simulated. Step Count: "));
    Serial.println(stepCount);
    saveStepCount();
    notifyStepCount();
    notifyDailyData();
  }
  else if (cmd == 'c') {
    int spaceIndex = input.indexOf(' ');
    if (spaceIndex > 0) {
      int val = input.substring(spaceIndex + 1).toInt();
      if (val >= 0) {
        stepCount = val;
        Serial.print(F("Debug: Step Count set to: "));
        Serial.println(stepCount);
        saveStepCount();
        notifyStepCount();
        notifyDailyData();
      } else {
        Serial.println(F("Error: Step count must be >= 0"));
      }
    } else {
      Serial.println(F("Error: Format is 'c <steps>' (e.g., 'c 2500')"));
    }
  }
  else if (cmd == 'a') {
    int spaceIndex = input.indexOf(' ');
    if (spaceIndex > 0) {
      int val = input.substring(spaceIndex + 1).toInt();
      if (val >= 0) {
        activeSeconds = val;
        Serial.print(F("Debug: Active seconds set to: "));
        Serial.println(activeSeconds);
        saveStepCount();
        notifyStepCount();
        notifyDailyData();
      } else {
        Serial.println(F("Error: Active seconds must be >= 0"));
      }
    } else {
      Serial.println(F("Error: Format is 'a <seconds>' (e.g., 'a 300')"));
    }
  }
  else if (cmd == 'd') {
    Serial.println(F("Debug: Sending dummy daily data payload..."));
    notifyStepCount();
    notifyDailyData();
  }
  else if (cmd == 'r') {
    stepCount = 0;
    activeSeconds = 0;
    saveStepCount();
    notifyStepCount();
    notifyDailyData();
    Serial.println(F("Debug: Steps and active time reset to 0"));
  }
  else if (cmd == 'p') {
    printStatus();
  }
  else if (cmd == 'l') {
    static bool ledState = false;
    ledState = !ledState;
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, ledState ? HIGH : LOW);
    Serial.print(F("Debug: LED toggled to "));
    Serial.println(ledState ? F("HIGH") : F("LOW"));
  }
  else {
    Serial.print(F("Unknown command: "));
    Serial.println(cmd);
    printHelpMenu();
  }
}

// ==================== SETUP ====================

void setup() {
  Serial.begin(115200);
  unsigned long t = millis();
  while (!Serial && millis() - t < 3000) { delay(10); }
  delay(500);

  Serial.println(F("\n=== Step Tracker BLE ==="));

  loadStepCount();

  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(400000);

  if (display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    displayEnabled = true;
    Serial.println(F("OLED Display initialized successfully."));
  } else {
    Serial.println(F("WARNING: OLED Display not found. Continuing in headless mode."));
  }

  if (mpu.begin(0x68, &Wire)) {
    mpuEnabled = true;
    mpu.setAccelerometerRange(MPU6050_RANGE_8_G);
    mpu.setGyroRange(MPU6050_RANGE_500_DEG);
    mpu.setFilterBandwidth(MPU6050_BAND_5_HZ);
    Serial.println(F("MPU6050 IMU initialized successfully."));
  } else {
    Serial.println(F("WARNING: MPU6050 not found. Accelerometer step counting disabled."));
  }

  setupBLE();

  if (displayEnabled) {
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(10, 0);
    display.println("Step Tracker");
    display.drawFastHLine(0, 10, 128, SSD1306_WHITE);

    display.setCursor(0, 18);
    display.println("Saved steps:");
    display.setTextSize(2);
    display.setCursor(30, 30);
    display.print(stepCount);

    display.setTextSize(1);
    display.setCursor(0, 52);
    display.println("Waiting for phone...");
    display.display();
    delay(3000);
  }

  lastActiveCheck = millis();
  lastSaveTime    = millis();
  for (int i = 0; i < WINDOW_SIZE; i++) magWindow[i] = 9.8f;

  printHelpMenu();
}

// ==================== LOOP ====================

void loop() {
  handleSerialDebug();

  static unsigned long lastLoop = 0;
  if (millis() - lastLoop < 10) return;
  lastLoop = millis();

  if (mpuEnabled) {
    sensors_event_t a, g, temp;
    mpu.getEvent(&a, &g, &temp);

    filteredAx = alpha * a.acceleration.x + (1.0f - alpha) * filteredAx;
    filteredAy = alpha * a.acceleration.y + (1.0f - alpha) * filteredAy;
    filteredAz = alpha * a.acceleration.z + (1.0f - alpha) * filteredAz;

    float magnitude = sqrt(
      filteredAx * filteredAx +
      filteredAy * filteredAy +
      filteredAz * filteredAz);

    jerkRate      = abs(magnitude - prevMagnitude) / 0.03f;
    prevMagnitude = magnitude;

    magWindow[wIndex] = magnitude;
    wIndex = (wIndex + 1) % WINDOW_SIZE;
    if (wIndex == 0) wFilled = true;

    float cMax = 0.0f, cMin = 50.0f;
    for (int i = 0; i < WINDOW_SIZE; i++) {
      if (magWindow[i] > cMax) cMax = magWindow[i];
      if (magWindow[i] < cMin) cMin = magWindow[i];
    }
    dynMid = cMin + (cMax - cMin) * 0.6f;

    float dynamicPart        = dynMid + 3.0f;
    float effectiveThreshold = wFilled
      ? max(stepThreshold, dynamicPart)
      : stepThreshold;

    detectStep(magnitude, effectiveThreshold, filteredAx, filteredAy, filteredAz);
    trackActiveTime(magnitude);
  }

  unsigned long now = millis();

  if (now % 1000 < 15) {
    updateCurrentTime();
  }

  if (now - lastDisplayUpdate > displayInterval) {
    if (displayEnabled) {
      drawStepWatch();
    }
    lastDisplayUpdate = now;
  }

  if (showStepFlash && now - lastStepDetected > 200) {
    showStepFlash = false;
  }

  if (bleConnected && now - lastBLENotify > BLE_NOTIFY_INTERVAL) {
    lastBLENotify = now;
    notifyStepCount();

    static unsigned long lastFullSync = 0;
    if (now - lastFullSync > 10000) {
      lastFullSync = now;
      notifyDailyData();
    }
  }

  bool timeToSave  = (now - lastSaveTime >= SAVE_INTERVAL_MS);
  bool stepsToSave = (stepCount - lastSavedCount >= SAVE_MIN_CHANGE);
  if ((timeToSave && stepsToSave) || (now - lastSaveTime >= SAVE_INTERVAL_MS * 3)) {
    saveStepCount();
  }

  static unsigned long lastResetCheck = 0;
  if (now - lastResetCheck >= 60000) {
    lastResetCheck = now;
    checkMidnightReset();
  }
}