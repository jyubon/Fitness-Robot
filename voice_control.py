#!/usr/bin/env python3
"""
Vosk Offline Voice Control for Fitness Robot.

Flow:
  1. STANDBY    – passively listens to the microphone (background noise
                  level is learned continuously).
  2. LISTENING  – as soon as words are heard, the whole utterance is
                  captured until the speaker has been silent for
                  VOICE_SILENCE_TIMEOUT (1 s by default).
  3. PROCESSING – the full sentence is passed to the intent extractor
                  (local LLM via Ollama if available, otherwise the local
                  semantic matcher) which picks the intended command.
  4. EXECUTING  – the command's action (defined in voice_commands.py) runs.

Commands are defined in `voice_commands.py` – edit that file to add or
change commands (it is hot-reloaded, no restart required).
"""

import sys
import json
import time
import queue
import threading
from pathlib import Path

import numpy as np

from config import (
    VOSK_MODEL_PATH,
    VOICE_COMMANDS_ENABLED,
    VOICE_SAMPLE_RATE,
    VOICE_MIC_DEVICE,
    VOICE_SILENCE_TIMEOUT,
    VOICE_MAX_UTTERANCE,
    VOICE_BLOCK_DURATION,
    VOICE_MIN_ENERGY,
    VOICE_ENERGY_RATIO,
    DEBUG,
)
from robot_control import send_command
from session_manager import (
    force_stop,
    remote_mode_queue,
    start_exercise,
    pause_request,
    resume_request,
    publish_event,
)
from intent_parser import extract_intent, get_registry, llm_available
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

_audio_queue = queue.Queue(maxsize=200)
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


def _rms(chunk):
    """Root-mean-square loudness of an int16 audio chunk."""
    samples = np.frombuffer(chunk, dtype=np.int16)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


# ─── Timed Autonomous Movement Helper ───
def _execute_timed_motion(direction, duration=2.5):
    """
    Execute robot motion in background for `duration` seconds,
    then automatically issue STOP, unless interrupted.
    """
    global _motion_thread
    _motion_stop_event.set()
    if _motion_thread and _motion_thread.is_alive():
        _motion_thread.join(timeout=0.5)
    _motion_stop_event.clear()

    def _worker():
        send_command(direction)
        start_ts = time.time()
        while time.time() - start_ts < duration:
            if _motion_stop_event.wait(0.4):
                break
            # Heartbeat to avoid ESP32 failsafe timeout (2.0s)
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
def parse_voice_command(text):
    """Return the command name extracted from a sentence, or None."""
    intent = extract_intent(text)
    return intent.name if intent else None


def _run_action(intent, text):
    """Perform the action attached to a command in voice_commands.py."""
    action = intent.command.get("action", {}) or {}
    a_type = str(action.get("type", "")).lower()
    params = intent.params

    if a_type == "stop":
        _execute_stop_motion()
        force_stop.set()

    elif a_type == "motion":
        reg = get_registry()
        max_dur = float(getattr(reg, "MAX_MOTION_DURATION", 10.0))
        duration = float(params.get("duration", action.get("duration", 2.0)))
        duration *= float(params.get("scale", 1.0))
        duration = max(0.3, min(duration, max_dur))
        _execute_timed_motion(action.get("direction", "STOP"), duration=duration)

    elif a_type == "mode":
        mode = action.get("mode")
        print(f"🔄 [VOICE] Queueing mode switch to {mode}")
        remote_mode_queue.put(("switch", mode))

    elif a_type == "start":
        start_exercise.set()

    elif a_type == "pause":
        resume_request.clear()
        pause_request.set()

    elif a_type == "resume":
        pause_request.clear()
        resume_request.set()

    elif a_type == "home":
        _execute_stop_motion()
        force_stop.set()

    elif a_type == "serial":
        send_command(str(action.get("command", "STOP")))

    elif a_type == "python":
        func = action.get("function")
        if callable(func):
            func(params, text)
        else:
            raise ValueError(f"Command {intent.name}: 'function' is not callable")

    else:
        raise ValueError(f"Command {intent.name}: unknown action type '{a_type}'")


def execute_voice_command(recognized_text):
    """Extract the intended command from a sentence and execute it."""
    intent = extract_intent(recognized_text)
    if intent is None:
        print(f"⚠️ [VOICE] No command understood in: \"{recognized_text}\"")
        _set_voice_state(
            VoiceState.STANDBY,
            f"Not a command: \"{recognized_text}\"",
            last_text=recognized_text,
        )
        return False

    print(f"🚀 [VOICE] Executing {intent.name} "
          f"(conf {intent.confidence:.2f}, via {intent.source}, params {intent.params}) "
          f"<- \"{recognized_text}\"")
    _set_voice_state(
        VoiceState.EXECUTING,
        f"Executing: {intent.name}",
        last_text=recognized_text,
        last_cmd=intent.name,
    )

    try:
        _run_action(intent, recognized_text)
        publish_event("voice_command", {
            "command": intent.name,
            "raw_text": recognized_text,
            "confidence": round(intent.confidence, 3),
            "source": intent.source,
            "params": intent.params,
            "status": "executed",
            "timestamp": time.time(),
        })
        return True
    except Exception as e:
        print(f"⚠️ [VOICE ERROR] Failed to execute {intent.name}: {e}", file=sys.stderr)
        return False


# ─── Background Voice Listener Loop ───
def _voice_listener_worker():
    """
    Passive listening loop with end-of-speech detection.

    Speech activity = Vosk producing new words OR audio louder than the
    adaptive noise threshold. The utterance ends once there has been no
    speech activity for VOICE_SILENCE_TIMEOUT seconds.
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

    backend = "local LLM (Ollama)" if llm_available() else "local semantic matcher"
    print("==================================================")
    print("  🎤 VOSK VOICE CONTROL ACTIVE")
    print("  • Passive listening (STANDBY)")
    print(f"  • Utterance ends after {VOICE_SILENCE_TIMEOUT:.1f}s of silence")
    print(f"  • Command extraction: {backend}")
    print("  • Commands defined in voice_commands.py")
    print("==================================================")

    _set_voice_state(VoiceState.STANDBY, "Standby - listening passively")

    blocksize = max(400, int(sample_rate * VOICE_BLOCK_DURATION))

    try:
        with sd.RawInputStream(
            device=VOICE_MIC_DEVICE,
            samplerate=sample_rate,
            blocksize=blocksize,
            dtype="int16",
            channels=1,
            callback=_audio_callback,
        ):
            noise_floor = None
            listening = False
            segments = []
            last_partial = ""
            last_voice_time = 0.0
            utterance_start = 0.0

            def finalize(reason):
                nonlocal listening, segments, last_partial
                final_text = json.loads(recognizer.FinalResult() or "{}").get("text", "").strip()
                if final_text:
                    segments.append(final_text)
                sentence = " ".join(s for s in segments if s).strip()

                if DEBUG:
                    print()
                print(f"⏱️ [VOICE] Speech ended ({reason}).")
                if sentence:
                    print(f"🧠 [VOICE] Heard: \"{sentence}\"")
                    _set_voice_state(VoiceState.PROCESSING, f"Understanding: \"{sentence}\"",
                                     last_text=sentence)
                    execute_voice_command(sentence)
                else:
                    print("⚠️ [VOICE] No words captured.")

                recognizer.Reset()
                listening = False
                segments = []
                last_partial = ""
                _drain_audio_queue()
                _set_voice_state(VoiceState.STANDBY, "Standby - listening passively")
                print("🎤 [VOICE] Standby - listening passively.\n")

            while not _stop_listener_event.is_set():
                try:
                    chunk = _audio_queue.get(timeout=0.1)
                except queue.Empty:
                    if listening and time.time() - last_voice_time >= VOICE_SILENCE_TIMEOUT:
                        finalize(f"{VOICE_SILENCE_TIMEOUT:.1f}s silence")
                    continue

                now = time.time()
                level = _rms(chunk)
                floor = noise_floor if noise_floor is not None else VOICE_MIN_ENERGY / VOICE_ENERGY_RATIO
                loud = level > max(VOICE_MIN_ENERGY, floor * VOICE_ENERGY_RATIO)

                if recognizer.AcceptWaveform(chunk):
                    text = json.loads(recognizer.Result() or "{}").get("text", "").strip()
                    partial = ""
                else:
                    text = ""
                    partial = json.loads(recognizer.PartialResult() or "{}").get("partial", "").strip()

                # ─── STANDBY: wait for words ───
                if not listening:
                    if text or partial:
                        listening = True
                        utterance_start = now
                        last_voice_time = now
                        segments = [text] if text else []
                        last_partial = partial
                        heard = text or partial
                        _set_voice_state(VoiceState.LISTENING, f"Listening: \"{heard}\"")
                        print(f"\n🎙️ [VOICE] Speech detected: \"{heard}\" ...")
                    else:
                        # Learn background noise level while nobody is talking
                        if noise_floor is None:
                            noise_floor = level
                        else:
                            alpha = 0.01 if loud else 0.05
                            noise_floor = (1 - alpha) * noise_floor + alpha * level
                    continue

                # ─── LISTENING: capture until 1 s of silence ───
                if text:
                    segments.append(text)
                    # Vosk finalises a segment *after* a pause; only count it as
                    # new speech if it did not come from a partial we already saw.
                    if not last_partial:
                        last_voice_time = now
                    last_partial = ""
                elif partial and partial != last_partial:
                    last_partial = partial
                    last_voice_time = now
                    heard = " ".join(segments + [partial]).strip()
                    _set_voice_state(VoiceState.LISTENING, f"Listening: \"{heard}\"")
                    if DEBUG:
                        print(f"\r  [VOICE] Hearing: \"{heard}\"", end="", flush=True)

                if loud:
                    last_voice_time = now

                if now - last_voice_time >= VOICE_SILENCE_TIMEOUT:
                    finalize(f"{VOICE_SILENCE_TIMEOUT:.1f}s silence")
                elif now - utterance_start >= VOICE_MAX_UTTERANCE:
                    finalize(f"max {VOICE_MAX_UTTERANCE:.0f}s reached")

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
