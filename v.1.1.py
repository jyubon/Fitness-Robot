from google import genai
from google.genai import types
import serial
import json
import time

# 1. Initialize the new client with your real API key
client = genai.Client(api_key="AIzaSyC_omTzQj25mB84B9ay1ENgNnoq1xo4Pro")

# Initialize serial connection
SERIAL_PORT = '/dev/ttyS0'
BAUD_RATE = 115200

try:
    # Initialize the serial port
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    time.sleep(2) # Give the connection a moment to stabilize
    print(f"Successfully connected to {SERIAL_PORT}")
except Exception as e:
    print(f"Failed to connect to Serial Port: {e}")
    ser = None

# Tools Definitions

# The Hardware Command Function
def send_motor_command(keyword: str, speed: int):
    """
    Sends a command keyword and a speed value (0-255) to the ESP32-S3.
    """
    if not ser or not ser.is_open:
        print("[ERROR] Serial port is not open.")
        return

    # 1. Enforce the 0-255 limit
    # This prevents errors if the API accidentally passes a speed of 300 or -50
    safe_speed = max(0, min(255, int(speed)))
    
    # 2. Format the keyword to ensure it's always uppercase (FWD, BWD, etc.)
    safe_keyword = keyword.strip().upper()

    # 3. Create a JSON payload
    payload = {
        "cmd": safe_keyword,
        "spd": safe_speed
    }

    # 4. Convert to string and add the CRITICAL newline character (\n)
    # The \n tells the ESP32 that the message is complete
    message = json.dumps(payload) + '\n'

    # 5. Send it over the RX/TX wires
    try:
        ser.write(message.encode('utf-8'))
        ser.flush() # Ensure all data is pushed out of the buffer
        print(f"[TX] Sent command: {message.strip()}")
    except Exception as e:
        print(f"[TX ERROR] Failed to send data: {e}")

def trigger_robot_movement(animation_name: str):
    print(f"\n[HARDWARE COMMAND] Sending '{animation_name}' to ESP32-S3...")

def send_watch_alert(alert_type: str):
    print(f"\n[HARDWARE COMMAND] Sending '{alert_type}' to ESP32-C3...")

# Configuration
config = types.GenerateContentConfig(
    system_instruction="You are the fitness robot. Map user input to hardware tools. You can perform several exercises and provide feedback. Use the tools to trigger movements or send alerts based on user commands.",
    temperature=0.7,
    tools=[trigger_robot_movement, send_watch_alert, send_motor_command],
)

# Start the chat using the new client
chat = client.chats.create(
    model="gemini-2.5-flash-lite", # Updated to the latest fast model
    config=config
)

# Terminal loop
print("=" * 50)
print("Fitness Robot Terminal Controller Activated.")
print("=" * 50)

while True:
    user_command = input("\nYou: ")
    if user_command.strip().lower() == 'exit':
        break
    if not user_command.strip():
        continue
        
    # Send the message
    response = chat.send_message(user_command)
    
    # In the new SDK, function calls are handled differently, 
    # but the chat.send_message still processes the text return.
    if response.text:
        print(f"Robot: {response.text}")