import serial
import time
import sys

# Raspberry Pi UART port
# Note: UART 5 maps to GPIO 12 (TX) & 13 (RX). Requires 'dtoverlay=uart5' in /boot/firmware/config.txt
SERIAL_PORT = '/dev/ttyAMA1'  
BAUD_RATE = 115200

print("========================================")
print("  Raspberry Pi <-> ESP32 Serial Test")
print("========================================")

try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    time.sleep(2) # Give connection time to stabilize
    print(f"✅ Successfully opened {SERIAL_PORT} at {BAUD_RATE} baud.")
except Exception as e:
    print(f"❌ Failed to open serial port: {e}")
    sys.exit(1)

def send_ping():
    print("\nSending 'PING' to ESP32...")
    try:
        ser.write(b"PING\n")
        ser.flush()
        
        # Wait up to 2 seconds for a response
        start_time = time.time()
        while time.time() - start_time < 2.0:
            if ser.in_waiting:
                response = ser.readline().decode('utf-8', errors='ignore').strip()
                if response:
                    print(f"✅ Received response: {response}")
                    return True
        print("⚠️ No response received within 2 seconds.")
        return False
    except Exception as e:
        print(f"❌ Error during communication: {e}")
        return False

# Run a quick ping test on startup
send_ping()

print("\n--- Interactive Mode ---")
print("Type a message and press Enter to send it to the ESP32.")
print("Type 'exit' or 'quit' to stop.")

try:
    while True:
        # Check for incoming messages first
        while ser.in_waiting:
            incoming = ser.readline().decode('utf-8', errors='ignore').strip()
            if incoming:
                print(f"📩 Incoming from ESP32: {incoming}")

        msg = input("\nSend: ")
        if msg.lower() in ['exit', 'quit']:
            break
            
        ser.write((msg + "\n").encode('utf-8'))
        ser.flush()
        
        # Wait briefly to catch the immediate echo from the ESP32
        time.sleep(0.5)
        while ser.in_waiting:
            incoming = ser.readline().decode('utf-8', errors='ignore').strip()
            if incoming:
                print(f"📩 Reply: {incoming}")

except KeyboardInterrupt:
    print("\nTest stopped by user.")
finally:
    ser.close()
    print("Serial port closed.")
