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
def _resolve_asset_path(filename):
    for candidate in (
        PROJECT_DIR / filename,
        PROJECT_DIR.parent / filename,
        Path("/home/fitness/Documents") / filename,
        Path("/home/fitness/Desktop") / filename,
        Path.home() / "Documents" / filename,
        Path.home() / "Desktop" / filename,
    ):
        try:
            if candidate.exists():
                return str(candidate.resolve())
        except OSError:
            continue
    return str(PROJECT_DIR / filename)

MODEL_PATH                = _resolve_asset_path("pose_landmarker.task")
HAND_MODEL_PATH           = _resolve_asset_path("hand_landmarker.task")
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
VOICE_MIC_DEVICE = None       # Default microphone

# End-of-speech detection: the command is processed once the speaker has
# been silent for this many seconds.
VOICE_SILENCE_TIMEOUT = 1.0
VOICE_MAX_UTTERANCE = 12.0    # Safety cap: process after this many seconds of continuous speech
VOICE_BLOCK_DURATION = 0.1    # Audio chunk size (s) -> silence timing resolution
VOICE_MIN_ENERGY = 300        # Min RMS (int16) treated as voice; raise in noisy rooms
VOICE_ENERGY_RATIO = 2.5      # Voice must be this many times louder than background noise

# Command extraction with a local Large Language Model (Ollama).
#   "auto": use the LLM if Ollama is running with VOICE_LLM_MODEL, else local matcher
#   "on":   always try the LLM (falls back to local matcher on error)
#   "off":  local matcher only
# Setup on the Pi:  curl -fsSL https://ollama.com/install.sh | sh
#                   ollama pull qwen2.5:0.5b
VOICE_LLM_MODE = os.getenv("VOICE_LLM_MODE", "auto")
VOICE_LLM_URL = os.getenv("VOICE_LLM_URL", "http://localhost:11434")
VOICE_LLM_MODEL = os.getenv("VOICE_LLM_MODEL", "qwen2.5:0.5b")
VOICE_LLM_TIMEOUT = 8.0       # Seconds before falling back to the local matcher

# ─── ElevenLabs Text-to-Speech & Permanent SD Card Cache ───
ELEVENLABS_API_KEY          = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID         = os.getenv("ELEVENLABS_VOICE_ID", "6psAnGNeDguzLyTxKYvI").strip()
ELEVENLABS_MODEL_ID         = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()
ELEVENLABS_SPEED            = float(os.getenv("ELEVENLABS_SPEED", "0.9"))
ELEVENLABS_STABILITY        = float(os.getenv("ELEVENLABS_STABILITY", "0.50"))
ELEVENLABS_SIMILARITY_BOOST = float(os.getenv("ELEVENLABS_SIMILARITY_BOOST", "0.75"))
ELEVENLABS_STYLE            = float(os.getenv("ELEVENLABS_STYLE", "0.06"))
ELEVENLABS_SPEAKER_BOOST    = os.getenv("ELEVENLABS_SPEAKER_BOOST", "true").strip().lower() in (
    "1", "true", "yes", "on", "enabled"
)
TTS_CACHE_DIR               = PROJECT_DIR / "voice" / "tts_cache"

