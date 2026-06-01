import sounddevice as sd
from scipy.io.wavfile import write
from google import genai
from google.genai import types
import serial
import json
import time

# 1. Initialize the client (USE A NEW KEY, NOT THE COMPROMISED ONE)
client = genai.Client(api_key="AIzaSyC_omTzQj25mB84B9ay1ENgNnoq1xo4Pro")

# Initialize serial connection (Fixed the missing '/' for Linux/Pi)
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


# --- AUDIO RECORDING FUNCTION ---

def record_audio(filename="voice_command.wav", duration=4):
    """Records audio from the default microphone for a set duration."""
    sample_rate = 44100 # Standard audio quality
    print(f"\n🎤 Recording for {duration} seconds... Speak your command now!")
    
    # Start recording
    audio_data = sd.rec(int(duration * sample_rate), samplerate=sample_rate, channels=1, dtype='int16')
    sd.wait() # Wait for the duration to finish
    
    # Save the file
    write(filename, sample_rate, audio_data)
    print("✅ Audio captured.")
    return filename


# --- CONFIGURATION & CHAT ---

config = types.GenerateContentConfig(
    system_instruction="You are a fitness robot. Map user voice or text input to hardware tools. You can perform several exercises and provide feedback. Keep verbal responses short and motivating.",
    temperature=0.7,
    tools=[trigger_robot_movement, send_watch_alert, send_motor_command],
)

chat = client.chats.create(
    model="gemini-2.5-flash-lite", 
    config=config
)


# --- TERMINAL LOOP ---

print("=" * 60)
print("Fitness Robot Voice Controller Activated.")
print("Press ENTER to speak a command (or type 'exit' to quit).")
print("=" * 60)

while True:
    user_input = input("\nPress ENTER to speak... ")
    
    if user_input.strip().lower() == 'exit':
        print("Shutting down...")
        break
        
    try:
        # 1. Record the user's voice
        wav_file = record_audio(duration=4) # Adjust duration if you need more time to speak
        
        print("⏳ Uploading audio to Gemini...")
        # 2. Upload the audio file to Google's servers
        gemini_audio_file = client.files.upload(file=wav_file)
        
        print("🧠 Processing voice intent...")
        # 3. Send the audio file PLUS a hidden system prompt to the chat
        response = chat.send_message([
            gemini_audio_file, 
            "Listen to this voice command and trigger the appropriate hardware tool. Reply with a short, motivational confirmation of what you just did."
        ])
        
        # 4. Print the AI's verbal response
        if response.text:
            print(f"\nRobot Says: {response.text}")
            
    except Exception as e:
        print(f"\n[ERROR]: An error occurred: {e}")
        print("Waiting a few seconds before continuing...")
        time.sleep(3)