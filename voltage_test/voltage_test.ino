// ============================================================
// Voltage & RGB LED Test Sketch
// ============================================================
// Use this simple sketch to calibrate the voltage sensor and
// verify the RGB LED works correctly.

#define VOLTAGE_SENSOR_PIN 3
#define LED_R_PIN 36
#define LED_G_PIN 37
#define LED_B_PIN 38

// Standard 25V voltage sensor module (30k/7.5k resistor divider -> 1/5)
// Adjust this multiplier value to calibrate the reading.
const float VOLTAGE_MULTIPLIER = 5.0;

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("--- Voltage & RGB LED Test Started ---");

  pinMode(VOLTAGE_SENSOR_PIN, INPUT);
  pinMode(LED_R_PIN, OUTPUT);
  pinMode(LED_G_PIN, OUTPUT);
  pinMode(LED_B_PIN, OUTPUT);
  
  // Initialize LED to off (Assuming Common Cathode: LOW = OFF)
  digitalWrite(LED_R_PIN, LOW);
  digitalWrite(LED_G_PIN, LOW);
  digitalWrite(LED_B_PIN, LOW);
}

void loop() {
  // Read analog value (0-4095)
  int adcValue = analogRead(VOLTAGE_SENSOR_PIN);
  
  // Calculate voltage (Assuming 3.3V reference on ESP32)
  float voltage = (adcValue / 4095.0) * 3.3 * VOLTAGE_MULTIPLIER;
  
  // Print values to Serial Monitor
  Serial.print("Analog ADC: ");
  Serial.print(adcValue);
  Serial.print(" | Calculated Voltage: ");
  Serial.print(voltage);
  Serial.println(" V");

  // RGB LED control logic (Assuming Common Cathode: HIGH = ON)
  if (voltage >= 11.0) {
    // Green
    digitalWrite(LED_R_PIN, LOW);
    digitalWrite(LED_G_PIN, HIGH);
    digitalWrite(LED_B_PIN, LOW);
    Serial.println("LED State: GREEN");
  } else if (voltage >= 10.0) {
    // Yellow (Red + Green)
    digitalWrite(LED_R_PIN, HIGH);
    digitalWrite(LED_G_PIN, HIGH);
    digitalWrite(LED_B_PIN, LOW);
    Serial.println("LED State: YELLOW");
  } else {
    // Red
    digitalWrite(LED_R_PIN, HIGH);
    digitalWrite(LED_G_PIN, LOW);
    digitalWrite(LED_B_PIN, LOW);
    Serial.println("LED State: RED");
  }

  Serial.println("----------------------------------------");
  delay(1000); // Check once per second
}
