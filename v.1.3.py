from google import genai
from google.genai import types
import serial
import json
import time

# --- 1. IMPORT THE SQUAT GENERATOR ---
from squats import detect_squats 

# Initialize the client (USE A NEW KEY, NOT THE COMPROMISED ONE)
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

# --- TOOLS DEFINITIONS ---

def send_motor_command(keyword: str, speed: int):
    """Sends a command keyword and a speed value (0-255) to the ESP32-S3."""
    if not ser or not ser.is_open:
        print("[ERROR] Serial port is not open.")
        return

    safe_speed = max(0, min(255, int(speed)))
    safe_keyword = keyword.strip().upper()

    payload = {
        "cmd": safe_keyword,
        "spd": safe_speed
    }

    message = json.dumps(payload) + '\n'

    try:
        ser.write(message.encode('utf-8'))
        ser.flush()
        print(f"[TX] Sent command: {message.strip()}")
    except Exception as e:
        print(f"[TX ERROR] Failed to send data: {e}")

def trigger_robot_movement(animation_name: str):
    print(f"\n[HARDWARE COMMAND] Sending '{animation_name}' to ESP32-S3...")

def send_watch_alert(alert_type: str):
    print(f"\n[HARDWARE COMMAND] Sending '{alert_type}' to ESP32-C3...")


# --- 2. SQUAT WORKOUT TOOL ---

def start_squat_workout(target_reps: int):
    """Starts the camera to track the user's squats. Requires the number of target reps."""
    print(f"\n📷 [CAMERA] Opening camera to track {target_reps} squats...")
    
    # Iterate over the generator we built in squats.py
    for current_rep in detect_squats():
        print(f"💪 [WORKOUT] Rep {current_rep}/{target_reps} completed.")
        
        # Check for halfway point
        if current_rep == (target_reps // 2):
            print("\n🤖 GEMINI SAYS: Keep going, you're halfway there!\n")
            
        # Check for completion
        elif current_rep >= target_reps:
            print("\n🤖 GEMINI SAYS: Great job, you've finished your set!\n")
            break # This breaks the loop, safely stopping the camera
            
    return f"Successfully tracked and completed {target_reps} squats."


#