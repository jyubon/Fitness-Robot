#!/usr/bin/env python3
"""
Vosk Offline Voice Control for Fitness Robot.
Handles continuous standby listening, speech detection, a 3-second delay
window to capture and process full utterances, and executes motion and workout commands.
"""

import os
import sys
import json
import time
import queue
import re
import threading
from pathlib import Path

from config import (
    VOSK_MODEL_PATH,
    VOICE_COMMANDS_ENABLED,
    VOICE_SAMPLE_RATE,
    VOICE_PROCESSING_DELAY,
    VOICE_MIC_DEVICE,
    DEBUG,
)
from robot_control import send_command
from session_manager import (
    force_stop,
    remote_mode_queue,
    start_exercise,
    publish_event,
    publish_pause,
    publish_resume,
)
import local_stream_server

# Check for Vosk and SoundDevice availability
try:
    import sounddevice as sd
    from vosk import Model, KaldiRecognizer, SetLogLevel
    VOSK_AVAILABLE = True
except ImportError:
    sd = None
    Model = None
    KaldiRecognizer = None
    SetLogLevel = None
    VOSK_AVAILABLE = False


# ─── State Definitions ───
class VoiceState:
    STANDBY = "STANDBY"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    EXECUTING = "EXECUTING"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


# Global state tracking
_current_state = VoiceState.STANDBY
_state_message = "Initializing..."
_last_recognized_text = ""
_last_executed_command = ""
_state_lock = threading.Lock()

_audio_queue = queue.Queue(maxsize=120)
_listener_thread = None
_stop_listener_event = threading.Event()

# Timed movement management (for directional commands)
_motion_stop_event = threading.Event()
_motion_thread = None


def get_voice_state():
    """Return tuple of (state, status_message, last_text, last_cmd)."""
    with _state_lock:
        return _current_state, _state_message, _last_recognized_text, _last_executed_command


def _set_voice_state(state, message=None, last_text=None, last_cmd=None):
    """Update voice assistant state and broadcast telemetry."""
    global _current_state, _state_message, _last_recognized_text, _last_executed_command
    with _state_lock:
        _current_state = state
        if message is not None:
            _state_message = message
        if last_text is not None:
            _last_recognized_text = last_text
        if last_cmd is not None:
            _last_executed_command = last_cmd

    try:
        local_stream_server.broadcast_telemetry("voice_status", {
            "state": state,
            "message": _state_message,
            "last_text": _last_recognized_text,
            "last_command": _last_executed_command,
        })
    except Exception:
        pass


def _drain_audio_queue():
    """Discard all queued audio chunks to prevent residual audio processing."""
    while True:
        try:
            _audio_queue.get_nowait()
        except queue.Empty:
            break


def _audio_callback(indata, frames, time_info, status):
    """Callback for sounddevice RawInputStream."""
    if status and DEBUG:
        print(f"[VOICE AUDIO] Warning: {status}", file=sys.stderr)
    try:
        _audio_queue.put_nowait(bytes(indata))
    except queue.Full:
        pass


# ─── Timed Autonomous Movement Helper ───
def _execute_timed_motion(direction, duration=2.5):
    """
    Execute robot motion in background for `duration` seconds,
    then automatically issue STOP, unless interrupted.
    """
    global _motion_thread
    _motion_stop_event.set()
    if _motion_thread and _motion_thread.is_alive():
        _motion_thread.join(timeout=0.3)
    _motion_stop_event.clear()

    def _worker():
        send_command(direction)
        start_ts = time.time()
        while time.time() - start_ts < duration:
            if _motion_stop_event.is_set():
                break
            time.sleep(0.4)
            # Send heartbeat command to avoid ESP32 failsafe timeout (2.0s)
            send_command(direction)
        send_command("STOP")
        print(f"🤖 [ROBOT] Completed {direction} movement.")

    _motion_thread = threading.Thread(target=_worker, daemon=True)
    _motion_thread.start()


def _execute_stop_motion():
    """Immediately halt any active timed movement and issue STOP to ESP32."""
    _motion_stop_event.set()
    send_command("STOP")


# ─── Command Parsing & Execution ───
COMMAND_PATTERNS = [
    # 1. STOP has highest priority
    ("STOP", [
        r"\bstop\b", r"\bhalt\b", r"\bfreeze\b", r"\bbrake\b",
        r"\bcancel\b", r"\bwait\b"
    ]),

    # 2. Workout selection modes
    ("BICEP_CURLS", [r"\bbicep\b", r"\bcurls?\b"]),
    ("PUSHUPS", [r"\bpush\s*ups?\b", r"\bpushup\b"]),
    ("SQUATS", [r"\bsquats?\b"]),
    ("PLANK", [r"\bplanks?\b"]),
    ("PERSON_FOLLOW", [r"\bfollow(\s+me)?\b", r"\btrack(ing)?\b", r"\btrack\s+me\b"]),

    # 3. Directional movement
    ("FORWARD", [r"\bforward\b", r"\bahead\b", r"\bfront\b", r"\bgo\s+ahead\b"]),
    ("BACKWARD", [r"\bback(ward)?\b", r"\breverse\b", r"\bretreat\b"]),
    ("LEFT", [r"\bturn\s+left\b", r"\bgo\s+left\b", r"\bmove\s+left\b", r"\bleft\b"]),
    ("RIGHT", [r"\bturn\s+right\b", r"\bgo\s+right\b", r"\bmove\s+right\b", r"\bright\b"]),

    # 4. Session & UI controls
    ("PAUSE", [r"\bpause\b", r"\brest\b", r"\btake\s+a\s+break\b"]),
    ("RESUME", [r"\bresume\b", r"\bcontinue\b"]),
    ("HOME", [r"\bhome\b", r"\bmenu\b", r"\bmain\s+menu\b", r"\bexit\b", r"\bquit\b"]),
    ("START", [r"\bstart(\s+workout|\s+exercise)?\b", r"\bbegin\b"]),
]


def parse_voice_command(text):
    """Match transcribed text to a canonical command keyword."""
    if not text:
        return None
    cleaned = text.lower().strip()
    for cmd_key, regex_list in COMMAND_PATTERNS:
        for pattern in regex_list:
            if re.search(pattern, cleaned):
                return cmd_key
    return None


def execute_voice_command(recognized_text):
    """Dispatches the matched command to the robot and application state."""
    command = parse_voice_command(recognized_text)
    if not command:
        print(f"⚠️ [VOICE] Unrecognized command phrase: \"{recognized_text}\"")
        _set_voice_state(
            VoiceState.STANDBY,
            f"Unrecognized: \"{recognized_text}\"",
            last_text=recognized_text
        )
        return False

    print(f"🚀 [VOICE] Executing command: {command} (Heard: \"{recognized_text}\")")
    _set_voice_state(
        VoiceState.EXECUTING,
        f"Executing: {command}",
        last_text=recognized_text,
        last_cmd=command
    )

    try:
        # 1. Stop command
        if command == "STOP":
            _execute_stop_motion()
            try:
                force_stop.set()
            except Exception:
                pass

        # 2. Motion commands
        elif command == "FORWARD":
            _execute_timed_motion("FORWARD", duration=2.5)
        elif command == "BACKWARD":
            _execute_timed_motion("BACKWARD", duration=2.5)
        elif command == "LEFT":
            _execute_timed_motion("LEFT", duration=1.5)
        elif command == "RIGHT":
            _execute_timed_motion("RIGHT", duration=1.5)

        # 3. Exercise modes
        elif command in ("BICEP_CURLS", "PUSHUPS", "SQUATS", "PLANK", "PERSON_FOLLOW"):
            print(f"🔄 [VOICE] Queueing mode switch to {command}")
            remote_mode_queue.put(("switch", command))

        # 4. Session & UI controls
        elif command == "START":
            start_exercise.set()
        elif command == "PAUSE":
            publish_pause()
        elif command == "RESUME":
            publish_resume()
        elif command == "HOME":
            _execute_stop_motion()
            force_stop.set()

        # Telemetry broadcast
        publish_event("voice_command", {
            "command": command,
            "raw_text": recognized_text,
            "status": "executed",
            "timestamp": time.time(),
        })
        return True

    except Exception as e:
        print(f"⚠️ [VOICE ERROR] Failed to execute {command}: {e}", file=sys.stderr)
        return False


# ─── Background Voice Listener Loop ───
def _voice_listener_worker():
    """
    Main background thread loop:
    1. Standby: Wait for incoming audio and detect when someone is speaking.
    2. Speaking Detected: When speech begins, wait for a 3-second delay to
       allow the speaker to finish their utterance without interruption.
    3. Processing: Transcribe the final complete utterance with Vosk.
    4. Execution: Parse and execute the robot control command.
    5. Return to standby.
    """
    if not VOSK_AVAILABLE:
        print("⚠️ [VOICE] Vosk or sounddevice not installed. Voice control disabled.")
        _set_voice_state(VoiceState.DISABLED, "Vosk/sounddevice not installed")
        return

    model_path = Path(VOSK_MODEL_PATH)
    if not model_path.is_dir():
        print(f"⚠️ [VOICE] Vosk model directory not found at: {model_path}")
        _set_voice_state(VoiceState.DISABLED, f"Model not found: {model_path.name}")
        return

    try:
        SetLogLevel(-1)
        print(f"🎙️ [VOICE] Loading Vosk model from: {model_path} ...")
        model = Model(str(model_path))

        # Determine audio device & sample rate
        try:
            device_info = sd.query_devices(VOICE_MIC_DEVICE, "input")
            sample_rate = int(device_info.get("default_samplerate", VOICE_SAMPLE_RATE))
            mic_name = device_info.get("name", "Default Microphone")
            print(f"🎤 [VOICE] Audio input: {mic_name} ({sample_rate} Hz)")
        except Exception as dev_err:
            print(f"⚠️ [VOICE] Could not query input device ({dev_err}). Using default {VOICE_SAMPLE_RATE} Hz.")
            sample_rate = VOICE_SAMPLE_RATE

        recognizer = KaldiRecognizer(model, sample_rate)
    except Exception as init_err:
        print(f"❌ [VOICE] Failed to initialize Vosk: {init_err}", file=sys.stderr)
        _set_voice_state(VoiceState.ERROR, str(init_err))
        return

    print("==================================================")
    print("  🎤 VOSK VOICE CONTROL ACTIVE")
    print("  • Robot is in STANDBY awaiting speech")
    print(f"  • When speech is detected, a {VOICE_PROCESSING_DELAY}s delay")
    print("    accumulates the full utterance before execution.")
    print("==================================================")

    _set_voice_state(VoiceState.STANDBY, "Standby - waiting for voice command")

    try:
        with sd.RawInputStream(
            device=VOICE_MIC_DEVICE,
            samplerate=sample_rate,
            blocksize=4000,
            dtype="int16",
            channels=1,
            callback=_audio_callback,
        ):
            accumulated_text = []
            speaking_detected = False
            speech_start_time = 0.0

            while not _stop_listener_event.is_set():
                try:
                    chunk = _audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if not speaking_detected:
                    # ─── STANDBY PHASE ───
                    has_accepted = recognizer.AcceptWaveform(chunk)
                    if has_accepted:
                        res = json.loads(recognizer.Result() or "{}").get("text", "").strip()
                        if res:
                            speaking_detected = True
                            speech_start_time = time.time()
                            accumulated_text.append(res)
                            _set_voice_state(
                                VoiceState.LISTENING,
                                f"Speech detected: '{res}'. Waiting {VOICE_PROCESSING_DELAY:.0f}s delay..."
                            )
                            print(f"\n🎙️ [VOICE] Speech detected: '{res}'! Waiting {VOICE_PROCESSING_DELAY:.0f}s delay to process command...")
                    else:
                        partial = json.loads(recognizer.PartialResult() or "{}").get("partial", "").strip()
                        if partial:
                            speaking_detected = True
                            speech_start_time = time.time()
                            _set_voice_state(
                                VoiceState.LISTENING,
                                f"Listening: '{partial}'. Waiting {VOICE_PROCESSING_DELAY:.0f}s delay..."
                            )
                            print(f"\n🎙️ [VOICE] Speech detected: '{partial}'! Waiting {VOICE_PROCESSING_DELAY:.0f}s delay to process command...")

                else:
                    # ─── SPEAKING DETECTED: 3-SECOND DELAY PHASE ───
                    time_elapsed = time.time() - speech_start_time
                    remaining = max(0.0, VOICE_PROCESSING_DELAY - time_elapsed)

                    # Feed chunks to recognizer during the 3-second delay
                    if recognizer.AcceptWaveform(chunk):
                        res = json.loads(recognizer.Result() or "{}").get("text", "").strip()
                        if res:
                            accumulated_text.append(res)
                    else:
                        partial = json.loads(recognizer.PartialResult() or "{}").get("partial", "").strip()
                        if partial and DEBUG:
                            print(f"\r  [VOICE] Hearing: '{partial}' ({remaining:.1f}s delay remaining)...", end="", flush=True)

                    # Check if 3-second delay has completed
                    if time_elapsed >= VOICE_PROCESSING_DELAY:
                        if DEBUG:
                            print()  # newline after carriage return
                        _set_voice_state(VoiceState.PROCESSING, "3s delay elapsed. Processing command...")
                        print(f"⏱️ [VOICE] {VOICE_PROCESSING_DELAY:.0f}-second delay complete. Finalizing utterance...")

                        # Finalize remaining decoded speech from the recognizer
                        final_res = json.loads(recognizer.FinalResult() or "{}").get("text", "").strip()
                        if final_res:
                            accumulated_text.append(final_res)

                        full_command = " ".join(accumulated_text).strip()

                        if full_command:
                            print(f"🧠 [VOICE] Command Received: \"{full_command}\"")
                            execute_voice_command(full_command)
                        else:
                            print("⚠️ [VOICE] No distinct command speech captured. Returning to standby.")
                            _set_voice_state(VoiceState.STANDBY, "Standby - waiting for voice command")

                        # Reset recognizer state and audio buffer for next standby cycle
                        recognizer.Reset()
                        accumulated_text = []
                        speaking_detected = False
                        _drain_audio_queue()
                        time.sleep(0.3)
                        _set_voice_state(VoiceState.STANDBY, "Standby - waiting for voice command")
                        print("🎤 [VOICE] Robot in standby - ready for next voice command.\n")

    except Exception as e:
        print(f"❌ [VOICE STREAM ERROR] {e}", file=sys.stderr)
        _set_voice_state(VoiceState.ERROR, f"Audio error: {e}")


# ─── Public API ───
def start_voice_listener():
    """Start the Vosk voice listener thread in background."""
    global _listener_thread
    if not VOICE_COMMANDS_ENABLED:
        print("ℹ️ Voice commands disabled in config.")
        return False

    if _listener_thread is not None and _listener_thread.is_alive():
        print("ℹ️ Voice listener is already running.")
        return True

    _stop_listener_event.clear()
    _listener_thread = threading.Thread(
        target=_voice_listener_worker,
        name="VoskVoiceListener",
        daemon=True
    )
    _listener_thread.start()
    return True


def stop_voice_listener():
    """Signal voice listener to terminate cleanly."""
    _stop_listener_event.set()
    _execute_stop_motion()
    if _listener_thread and _listener_thread.is_alive():
        _listener_thread.join(timeout=1.0)
    print("🛑 Voice listener stopped.")


if __name__ == "__main__":
    # Standalone test runner
    print("Testing Vosk Voice Controller directly...")
    start_voice_listener()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nExiting...")
        stop_voice_listener()

