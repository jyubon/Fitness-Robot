#!/usr/bin/env python3
"""
Configuration and constants for Fitness Robot.
Raspberry Pi 4 + MediaPipe + ESP32 via UART/Serial
"""

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

# Load .env file
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_DIR / ".env")
except ImportError:
    env_path = PROJECT_DIR / ".env"
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        os.environ[parts[0].strip()] = parts[1].strip()

# Suppress TF/GLOG logs
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['GLOG_minloglevel'] = '3'

# ─── ESP32 (UART5 Serial Connection) ───
SERIAL_PORT = "/dev/ttyAMA5"   # Standard Raspberry Pi UART5 Serial Port
BAUD_RATE   = 115200

# ─── Camera (C270) ───
CAMERA_INDEX    = 0
CAMERA_WIDTH    = 480            # Lower = faster on RPi4
CAMERA_HEIGHT   = 320
FRAME_SKIP      = 2              # Process pose landmarks every N frames (optimized for RPi4)
HAND_FRAME_SKIP = 6              # Process hand landmarks every N frames (optimized for RPi4)

# ─── MediaPipe ───
MODEL_PATH                = "pose_landmarker.task"
HAND_MODEL_PATH           = "hand_landmarker.task"
PALM_STOP_REQUIRED_FRAMES = 12
PALM_STOP_MIN_HAND_SIZE   = 0.12

# ─── Following & Distance Thresholds ───
CENTER_ZONE_LEFT  = 0.25
CENTER_ZONE_RIGHT = 0.75
TOO_CLOSE         = 0.75
TOO_FAR           = 0.50

# ─── Timing ───
COMMAND_COOLDOWN    = 0.75    # Min seconds between sending same command
LOST_PERSON_TIMEOUT = 1.5     # Seconds before searching

# ─── Display ───
SHOW_WINDOW    = True
DISPLAY_WIDTH  = 480          # Fixed display width for 480x320 screen
DISPLAY_HEIGHT = 320          # Fixed display height for 480x320 screen

# ─── Debug ───
DEBUG    = True
SHOW_FPS = True

# ─── Backend publishing ───
BACKEND_BASE_URL    = os.getenv("BACKEND_BASE_URL", "http://localhost:8000").rstrip("/")
BACKEND_TOKEN       = os.getenv("BACKEND_TOKEN", "").strip()
FIREBASE_USER_ID    = os.getenv("FIREBASE_USER_ID", "").strip()
FIREBASE_USER_EMAIL = os.getenv("FIREBASE_USER_EMAIL", "").strip()

# ─── Visual Colors (BGR) ───
ACCENT_GREEN  = (150, 215, 95)
ACCENT_RED    = (95, 95, 210)
ACCENT_ORANGE = (80, 175, 225)
ACCENT_CYAN   = (200, 185, 100)
TEXT_WHITE    = (235, 235, 240)
TEXT_DIM      = (155, 155, 165)
PANEL_BG      = (25, 25, 30)

SKELETON_LANDMARK = (190, 210, 235)
SKELETON_LINE     = (170, 170, 90)

# ─── Command Colors (BGR) ───
CMD_COLORS = {
    'FORWARD':   (180, 220, 80),
    'BACKWARD':  (100, 100, 220),
    'LEFT':      (210, 190, 80),
    'RIGHT':     (200, 120, 200),
    'FWD_LEFT':  (180, 210, 100),
    'FWD_RIGHT': (160, 200, 120),
    'BWD_LEFT':  (180, 100, 200),
    'BWD_RIGHT': (140, 100, 210),
    'STOP':      (120, 120, 130),
    'SEARCHING': (80, 180, 220),
}

# ─── 8-Direction Matrix ───
DIRECTION_MATRIX = {
    ("FAR",   "LEFT"):   "FWD_LEFT",
    ("FAR",   "CENTER"): "FORWARD",
    ("FAR",   "RIGHT"):  "FWD_RIGHT",
    ("GOOD",  "LEFT"):   "LEFT",
    ("GOOD",  "CENTER"): "STOP",
    ("GOOD",  "RIGHT"):  "RIGHT",
    ("CLOSE", "LEFT"):   "BWD_LEFT",
    ("CLOSE", "CENTER"): "BACKWARD",
    ("CLOSE", "RIGHT"):  "BWD_RIGHT",
}

# ─── Vosk Offline Voice Recognition ───
VOSK_MODEL_DIR_NAME = "vosk-model-small-en-us-0.15"
_env_model_path = os.getenv("VOSK_MODEL_PATH")
if _env_model_path and Path(_env_model_path).exists():
    VOSK_MODEL_PATH = str(Path(_env_model_path).resolve())
else:
    _candidates = [
        PROJECT_DIR / VOSK_MODEL_DIR_NAME,
        PROJECT_DIR / "model",
        PROJECT_DIR.parent / "Voice Commands" / "Vosk" / VOSK_MODEL_DIR_NAME,
        PROJECT_DIR.parent.parent / "Voice Commands" / "Vosk" / VOSK_MODEL_DIR_NAME,
    ]
    VOSK_MODEL_PATH = str(PROJECT_DIR / VOSK_MODEL_DIR_NAME)
    for _c in _candidates:
        if _c.exists():
            VOSK_MODEL_PATH = str(_c.resolve())
            break

VOICE_COMMANDS_ENABLED = True
VOICE_SAMPLE_RATE = 16000
VOICE_PROCESSING_DELAY = 3.0  # Seconds to wait when someone is speaking before processing
VOICE_MIC_DEVICE = None       # Default microphone

