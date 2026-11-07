#!/usr/bin/env python3
"""
Audio Manager for Fitness Robot.
Handles non-blocking background MP3 playback for exercise feedback, state alerts,
and ElevenLabs voice command responses mapped to Vosk Speech-to-Text commands.
"""

import os
import time
import subprocess
import threading
from pathlib import Path
from config import PROJECT_DIR

# ─── Exercise Form Feedback MP3 Map (in voice/) ───
VOICE_FILE_MAP = {
    "align horizontally":      "001_align_horizontally_GET_IN_POSITION.mp3",
    "too deep":                "002_don't_go_too_deep_in_squats.mp3",
    "equalize both hands":     "003_equalize_both_hands.mp3",
    "extend arms straight":    "004_extendarms_straight_and_directly_in_front_of_you.mp3",
    "straight line":           "005_form_a_straight_line_with_body!.mp3",
    "feet at shoulder":        "006_keep_feet_at_shoulder_distance.mp3",
    "look in front":           "007_look_in_front_of_you!.mp3",
    "curling too fast":        "008_slow_down!_curling_too_fast.mp3",
    "stand at shoulder width": "009_stand_at_shoulder_width_and_keep_knees_in_front_of_body.mp3",
    "tuck elbow":              "010_tuck_elbow_closer_to_your_body.mp3",
    "swing":                   "011_do_not_swing_arm,keep_hands_in_straight_position_without_swinging.mp3",
}

# ─── Vosk Speech-to-Text Command -> ElevenLabs MP3 Map (in voice/) ───
COMMAND_VOICE_FILE_MAP = {
    # Conversational replies
    "HELLO":        "017_hello_fitbot.mp3",
    "HOW_ARE_YOU":  "018_how_are_you.mp3",
    "WHO_ARE_YOU":  "019_who_are_you.mp3",
    "THANK_YOU":    "020_thank_you.mp3",
    "GOODBYE":      "021_goodbye.mp3",

    # Session & workout state commands (reuse existing 013-015 or dedicated clips)
    "PAUSE":        "013_please_rest_for_a_while.mp3",
    "START":        "014_lets_start_the_workout.mp3",
    "RESUME":       "015_lets_resume_the_workout.mp3",

    # Mode & movement command confirmations (played automatically when present in voice/)
    "STOP":         "022_stopping.mp3",
    # ── EXHIBITION STATIONARY MODE: Moving voice commands commented out for reuse later ──
    # "FOLLOW_ME":    "023_following_you.mp3",
    "BICEP_CURLS":  "024_starting_bicep_curls.mp3",
    "PUSHUPS":      "025_starting_pushups.mp3",
    "SQUATS":       "026_starting_squats.mp3",
    "PLANK":        "027_starting_plank.mp3",
    # "ROTATE":       "028_rotating.mp3",
    # "ROTATE_LEFT":  "029_turning_left.mp3",
    # "ROTATE_RIGHT": "030_turning_right.mp3",
    # "MOVE_LEFT":    "031_moving_left.mp3",
    # "MOVE_RIGHT":   "032_moving_right.mp3",
    # "FORWARD":      "033_moving_forward.mp3",
    # "BACKWARD":     "034_moving_backward.mp3",
}

# ─── Reply Text Substring -> ElevenLabs MP3 Map (fallback when called via speak_text(text)) ───
REPLY_TEXT_FILE_MAP = {
    "hello! i am fitbot":        "017_hello_fitbot.mp3",
    "doing great and ready":     "018_how_are_you.mp3",
    "smart fitness robot":       "019_who_are_you.mp3",
    "you are welcome":           "020_thank_you.mp3",
    "goodbye! stay healthy":     "021_goodbye.mp3",
}

_current_audio_process = None
_process_lock = threading.Lock()
_audio_lock = threading.Semaphore(1)
_tts_speaking = threading.Event()
_cancel_audio_sequence = threading.Event()
_feedback_muted = False


def set_feedback_muted(muted: bool):
    """Enable or disable robot audio/voice feedback."""
    global _feedback_muted
    _feedback_muted = bool(muted)
    if _feedback_muted:
        terminate_current_audio()
        stop_background_music_process()
    if os.name != "nt":
        try:
            if _feedback_muted:
                subprocess.run(["amixer", "set", "Master", "mute"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["amixer", "-c", "0", "set", "Master", "mute"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.run(["amixer", "set", "Master", "unmute"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["amixer", "-c", "0", "set", "Master", "unmute"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    print(f"🔇 [AUDIO] Feedback muted: {_feedback_muted}")


def is_feedback_muted() -> bool:
    """Return True if robot voice feedback is currently muted."""
    return _feedback_muted


def is_tts_speaking():
    """Return True while FitBot is playing a voice response (mutes mic self-triggering)."""
    return _tts_speaking.is_set()


def _resolve_voice_file(filename):
    """Locate an MP3 file in `voice/` (supporting minor apostrophe/spacing filename variants)."""
    if not filename:
        return None
    voice_dir = PROJECT_DIR / "voice"
    direct = voice_dir / filename
    if direct.exists():
        return direct

    # Check prefix number (e.g. "017_", "014_") in voice/ directory if user named the rest slightly differently
    prefix = filename.split("_", 1)[0]
    if prefix.isdigit() and len(prefix) == 3 and voice_dir.exists():
        matches = sorted(voice_dir.glob(f"{prefix}_*.mp3"))
        if matches:
            return matches[0]
    return None


def play_command_audio(command_name, audio_file=None, force=True):
    """
    Play the pre-generated ElevenLabs MP3 corresponding to a command identified
    by the Vosk Speech-to-Text model.
    Returns True if a matching MP3 file exists in `voice/` and playback started.
    """
    if _feedback_muted:
        return False
    filename = audio_file or COMMAND_VOICE_FILE_MAP.get(str(command_name).upper())
    file_path = _resolve_voice_file(filename)
    if file_path is None or not file_path.exists():
        return False

    def _run():
        _tts_speaking.set()
        try:
            if force:
                terminate_current_audio()
                _audio_lock.acquire(blocking=True)
            else:
                if not _audio_lock.acquire(blocking=False):
                    return
            try:
                if os.name == "nt":
                    _play_mp3_windows(str(file_path))
                else:
                    _play_mp3_linux(str(file_path))
            finally:
                _audio_lock.release()
        finally:
            time.sleep(0.15)
            _tts_speaking.clear()

    threading.Thread(target=_run, daemon=True).start()
    return True


def speak_text(text, command_name=None, audio_file=None):
    """
    Play the pre-generated ElevenLabs MP3 for a spoken response.
    Looks up by `audio_file`, `command_name` (in COMMAND_VOICE_FILE_MAP),
    or `text` (in REPLY_TEXT_FILE_MAP).
    """
    if _feedback_muted:
        return
    if command_name or audio_file:
        if play_command_audio(command_name, audio_file=audio_file, force=True):
            return

    if not text or not str(text).strip():
        return
    clean_lower = str(text).strip().lower()

    for key, fname in REPLY_TEXT_FILE_MAP.items():
        if key in clean_lower:
            if play_command_audio(None, audio_file=fname, force=True):
                return

    print(f"ℹ️ [AUDIO] No ElevenLabs MP3 file in voice/ for: \"{text}\"")


def terminate_current_audio():
    """Terminate the currently active audio playback process immediately."""
    global _current_audio_process
    _cancel_audio_sequence.set()
    with _process_lock:
        if _current_audio_process is not None:
            try:
                _current_audio_process.terminate()
                _current_audio_process.wait(timeout=1)
            except Exception:
                pass
            _current_audio_process = None


def _play_mp3_windows(file_path_str):
    """Play MP3 on Windows using PowerShell Media.MediaPlayer."""
    global _current_audio_process
    if _feedback_muted:
        return False
    uri = "file:///" + file_path_str.replace("\\", "/").replace(" ", "%20").replace("'", "''")
    ps_cmd = (
        "Add-Type -AssemblyName PresentationCore;"
        "$m=New-Object System.Windows.Media.MediaPlayer;"
        f"$m.Open([System.Uri]'{uri}');"
        "$sw=[Diagnostics.Stopwatch]::StartNew();"
        "while(-not $m.NaturalDuration.HasTimeSpan -and $sw.ElapsedMilliseconds -lt 2000){Start-Sleep -Milliseconds 50};"
        "$m.Play();"
        "$dur=if($m.NaturalDuration.HasTimeSpan){[int][Math]::Ceiling($m.NaturalDuration.TimeSpan.TotalMilliseconds)+250}else{5000};"
        "Start-Sleep -Milliseconds $dur;"
        "$m.Close()"
    )
    try:
        terminate_current_audio()
        with _process_lock:
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-c", ps_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE
            )
            _current_audio_process = proc

        try:
            stdout, stderr = proc.communicate(timeout=25)
            if proc.returncode != 0 and proc.returncode is not None:
                err = stderr.decode(errors="replace").strip()
                print(f"⚠️ PowerShell audio failed (rc={proc.returncode}): {err[:200]}")
                return False
        except subprocess.TimeoutExpired:
            terminate_current_audio()
            return False

        with _process_lock:
            if _current_audio_process == proc:
                _current_audio_process = None
        return True
    except Exception as exc:
        print(f"⚠️ _play_mp3_windows exception: {exc}")
        return False


def _play_mp3_linux(file_path_str):
    """Play MP3 on Linux / Raspberry Pi using mpg123, mpg321, or play."""
    global _current_audio_process
    if _feedback_muted:
        return False
    for cmd in [
        ["mpg123", "-q", file_path_str],
        ["mpg321", "-q", file_path_str],
        ["play",   "-q", file_path_str],
    ]:
        try:
            terminate_current_audio()
            with _process_lock:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE
                )
                _current_audio_process = proc

            try:
                stdout, stderr = proc.communicate(timeout=25)
                if proc.returncode == 0:
                    with _process_lock:
                        if _current_audio_process == proc:
                            _current_audio_process = None
                    return True
                err_text = stderr.decode(errors="replace").strip()
                if err_text:
                    print(f"⚠️ {cmd[0]} returned {proc.returncode}: {err_text[:120]}")
            except subprocess.TimeoutExpired:
                terminate_current_audio()
            except FileNotFoundError:
                continue
        except Exception as exc:
            print(f"⚠️ {cmd[0]} error: {exc}")
            continue
    print("⚠️ No working MP3 player found. On RPi run: sudo apt-get install -y mpg123")
    return False


def _play_mp3_background(file_path_str, force=False):
    """Play audio file in non-blocking background thread."""
    if _feedback_muted:
        return

    def run():
        if _feedback_muted:
            return
        if force:
            terminate_current_audio()
            _audio_lock.acquire(blocking=True)
        else:
            if not _audio_lock.acquire(blocking=False):
                return
        try:
            if _feedback_muted:
                return
            if os.name == 'nt':
                _play_mp3_windows(file_path_str)
            else:
                _play_mp3_linux(file_path_str)
        except Exception as exc:
            print(f"⚠️ Audio playback error: {exc}")
        finally:
            _audio_lock.release()

    t = threading.Thread(target=run, daemon=False)
    t.start()


def play_audio_sequence(items, force=True):
    """
    Play a list of audio files or track numbers sequentially in a non-blocking background thread.
    Each item can be an MP3 filename string or an integer track number (e.g. 14).
    """
    if _feedback_muted or not items:
        return False

    def _run():
        _tts_speaking.set()
        try:
            if force:
                terminate_current_audio()
                _audio_lock.acquire(blocking=True)
                _cancel_audio_sequence.clear()
            else:
                if not _audio_lock.acquire(blocking=False):
                    return
            try:
                track_files = {
                    13: "013_please_rest_for_a_while.mp3",
                    14: "014_lets_start_the_workout.mp3",
                    15: "015_lets_resume_the_workout.mp3",
                    16: "016_an_obstacle_is_detected.mp3",
                }
                for item in items:
                    if _cancel_audio_sequence.is_set() or _feedback_muted:
                        break
                    if isinstance(item, int):
                        filename = track_files.get(item)
                    else:
                        filename = str(item)

                    file_path = _resolve_voice_file(filename)
                    if file_path and file_path.exists():
                        if os.name == "nt":
                            _play_mp3_windows(str(file_path))
                        else:
                            _play_mp3_linux(str(file_path))
                        time.sleep(0.15)
                    else:
                        print(f"⚠️ Voice file in sequence not found: {filename}")
            finally:
                _audio_lock.release()
        finally:
            time.sleep(0.15)
            _tts_speaking.clear()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return True


def play_audio_track(track_number):
    """Play state transition audio tracks (13=rest, 14=start, 15=resume, 16=obstacle)."""
    if _feedback_muted:
        return
    track_files = {
        13: "013_please_rest_for_a_while.mp3",
        14: "014_lets_start_the_workout.mp3",
        15: "015_lets_resume_the_workout.mp3",
        16: "016_an_obstacle_is_detected.mp3"
    }
    filename = track_files.get(track_number)
    file_path = _resolve_voice_file(filename)
    if file_path and file_path.exists():
        _play_mp3_background(str(file_path), force=True)
    else:
        print(f"⚠️ Voice file not found: {PROJECT_DIR / 'voice' / str(filename)}")


def play_voice_command(err_msg, is_resting=False, person_missing=False, lost_person_since=None):
    """Play matching MP3 feedback based on error message."""
    if _feedback_muted or is_resting or person_missing:
        return
    if lost_person_since is not None and (time.time() - lost_person_since) > 2.0:
        return

    msg = err_msg.lower()
    matched_file = None
    for key, filename in VOICE_FILE_MAP.items():
        if key in msg:
            matched_file = filename
            break

    if not matched_file:
        return

    file_path = _resolve_voice_file(matched_file)
    if file_path and file_path.exists():
        _play_mp3_background(str(file_path))
    else:
        print(f"⚠️ Voice file not found: {PROJECT_DIR / 'voice' / matched_file}")


# ─── Background Music Player (while Idle / No Exercise) ───
MUSIC_DIR = PROJECT_DIR / "music"
_bg_music_process = None
_bg_music_lock = threading.Lock()
_bg_music_stop_event = threading.Event()
_bg_music_thread = None
_music_volume = 0.35  # Ambient background volume (~35%)


def stop_background_music_process():
    """Immediately stops the currently playing background music process."""
    global _bg_music_process
    with _bg_music_lock:
        if _bg_music_process is not None:
            try:
                _bg_music_process.terminate()
                _bg_music_process.wait(timeout=0.5)
            except Exception:
                try:
                    _bg_music_process.kill()
                except Exception:
                    pass
            _bg_music_process = None


def _play_music_file(file_path_str):
    """Play background music track at ambient volume without blocking."""
    global _bg_music_process
    stop_background_music_process()
    if _feedback_muted:
        return
    if os.name == 'nt':
        uri = "file:///" + file_path_str.replace("\\", "/").replace(" ", "%20").replace("'", "''")
        ps_cmd = (
            "Add-Type -AssemblyName PresentationCore;"
            "$m=New-Object System.Windows.Media.MediaPlayer;"
            f"$m.Volume={_music_volume};"
            f"$m.Open([System.Uri]'{uri}');"
            "$sw=[Diagnostics.Stopwatch]::StartNew();"
            "while(-not $m.NaturalDuration.HasTimeSpan -and $sw.ElapsedMilliseconds -lt 2000){Start-Sleep -Milliseconds 50};"
            "$m.Play();"
            "$dur=if($m.NaturalDuration.HasTimeSpan){[int][Math]::Ceiling($m.NaturalDuration.TimeSpan.TotalMilliseconds)+250}else{600000};"
            "Start-Sleep -Milliseconds $dur;"
            "$m.Close()"
        )
        with _bg_music_lock:
            try:
                _bg_music_process = subprocess.Popen(
                    ["powershell", "-NoProfile", "-NonInteractive", "-c", ps_cmd],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            except Exception as e:
                print(f"⚠️ Music playback exception: {e}")
    else:
        scale_val = int(32768 * _music_volume)
        cmd = ["mpg123", "-f", str(scale_val), "-q", file_path_str]
        with _bg_music_lock:
            try:
                _bg_music_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            except FileNotFoundError:
                for alt_cmd in [["mpg321", "-q", file_path_str], ["play", "-q", "-v", str(_music_volume), file_path_str]]:
                    try:
                        _bg_music_process = subprocess.Popen(alt_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        break
                    except FileNotFoundError:
                        continue
            except Exception as e:
                print(f"⚠️ Music playback exception: {e}")


def _background_music_worker():
    """Loops through MP3 tracks in music/ while no exercise is active and no voice is speaking."""
    import random
    from session_manager import exercise_executing

    while not _bg_music_stop_event.is_set():
        if _feedback_muted or exercise_executing.is_set() or is_tts_speaking():
            stop_background_music_process()
            time.sleep(0.4)
            continue

        if not MUSIC_DIR.exists():
            MUSIC_DIR.mkdir(parents=True, exist_ok=True)

        tracks = sorted(MUSIC_DIR.glob("*.mp3")) + sorted(MUSIC_DIR.glob("*.wav"))
        if not tracks:
            time.sleep(2.0)
            continue

        shuffled = list(tracks)
        random.shuffle(shuffled)

        for track in shuffled:
            if _feedback_muted or exercise_executing.is_set() or _bg_music_stop_event.is_set():
                stop_background_music_process()
                break

            if is_tts_speaking():
                stop_background_music_process()
                time.sleep(0.4)
                break

            if _feedback_muted:
                stop_background_music_process()
                break

            _play_music_file(str(track))

            while _bg_music_process is not None and _bg_music_process.poll() is None:
                if _feedback_muted or exercise_executing.is_set() or _bg_music_stop_event.is_set() or is_tts_speaking():
                    stop_background_music_process()
                    break
                time.sleep(0.2)

            time.sleep(0.5)


def start_background_music():
    """Start background music service if not already active."""
    global _bg_music_thread
    _bg_music_stop_event.clear()
    if _bg_music_thread is None or not _bg_music_thread.is_alive():
        _bg_music_thread = threading.Thread(target=_background_music_worker, daemon=True)
        _bg_music_thread.start()
        print("🎵 Background music service started (active while idle/no exercise).")


def stop_background_music():
    """Stop background music service completely."""
    _bg_music_stop_event.set()
    stop_background_music_process()
