#!/usr/bin/env python3
"""
Audio Manager for Fitness Robot.
Handles non-blocking background MP3 playback for voice guidance and alerts.
"""

import os
import time
import subprocess
import threading
from pathlib import Path
from config import PROJECT_DIR

VOICE_FILE_MAP = {
    "align horizontally":    "001_align_horizontally_GET_IN_POSITION.mp3",
    "too deep":              "002_don't_go_too_deep_in_squats.mp3",
    "equalize both hands":   "003_equalize_both_hands.mp3",
    "extend arms straight":  "004_extendarms_straight_and_directly_in_front_of_you.mp3",
    "straight line":         "005_form_a_straight_line_with_body!.mp3",
    "feet at shoulder":      "006_keep_feet_at_shoulder_distance.mp3",
    "look in front":         "007_look_in_front_of_you!.mp3",
    "curling too fast":      "008_slow_down!_curling_too_fast.mp3",
    "stand at shoulder width": "009_stand_at_shoulder_width_and_keep_knees_in_front_of_body.mp3",
    "tuck elbow":            "010_tuck_elbow_closer_to_your_body.mp3",
    "swing":                 "011_do_not_swing_arm,keep_hands_in_straight_position_without_swinging.mp3",
}

_current_audio_process = None
_process_lock = threading.Lock()
_audio_lock = threading.Semaphore(1)


def terminate_current_audio():
    """Terminate the currently active audio playback process immediately."""
    global _current_audio_process
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
    uri = "file:///" + file_path_str.replace("\\", "/").replace(" ", "%20").replace("'", "''")
    ps_cmd = (
        "Add-Type -AssemblyName PresentationCore;"
        "$m=New-Object System.Windows.Media.MediaPlayer;"
        f"$m.Open([System.Uri]'{uri}');"
        "Start-Sleep -Seconds 2;"
        "$m.Play();"
        "Start-Sleep -Seconds 6;"
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
            stdout, stderr = proc.communicate(timeout=15)
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
                stdout, stderr = proc.communicate(timeout=20)
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
    def run():
        if force:
            terminate_current_audio()
            _audio_lock.acquire(blocking=True)
        else:
            if not _audio_lock.acquire(blocking=False):
                return
        try:
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


def play_audio_track(track_number):
    """Play state transition audio tracks (13=rest, 14=start, 15=resume, 16=obstacle)."""
    track_files = {
        13: "013_please_rest_for_a_while.mp3",
        14: "014_lets_start_the_workout.mp3",
        15: "015_lets_resume_the_workout.mp3",
        16: "016_an_obstacle_is_detected.mp3"
    }
    filename = track_files.get(track_number)
    if filename:
        file_path = PROJECT_DIR / "voice" / filename
        if not file_path.exists():
            # Try alternate naming
            if track_number == 14:
                alt = PROJECT_DIR / "voice" / "014_let's_start_the_workout.mp3"
                if alt.exists(): file_path = alt
            elif track_number == 15:
                alt = PROJECT_DIR / "voice" / "015_lets_resume_the_workout.mp3"
                if alt.exists(): file_path = alt

        if file_path.exists():
            _play_mp3_background(str(file_path), force=True)
        else:
            print(f"⚠️ Voice file not found: {file_path}")


def play_voice_command(err_msg, is_resting=False, person_missing=False, lost_person_since=None):
    """Play matching MP3 feedback based on error message."""
    if is_resting or person_missing:
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

    file_path = PROJECT_DIR / "voice" / matched_file
    if file_path.exists():
        _play_mp3_background(str(file_path))
    else:
        print(f"⚠️ Voice file not found: {file_path}")
