#!/usr/bin/env python3
"""
Robot Control for Fitness Robot.
Handles serial communication with ESP32 and 8-direction tracking logic.
"""

import time
import serial
import threading

from config import (
    SERIAL_PORT,
    BAUD_RATE,
    DEBUG,
    CENTER_ZONE_LEFT,
    CENTER_ZONE_RIGHT,
    TOO_CLOSE,
    TOO_FAR,
    DIRECTION_MATRIX,
)
import local_stream_server
from session_manager import publish_event, publish_form_error
from audio_manager import play_audio_track

# ─── Serial Connection ───
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
    print(f"✅ Connected to ESP32 on UART serial: {SERIAL_PORT}")
except Exception as e:
    ser = None
    print(f"⚠️ Serial initialization failed: {e}")

current_command = "STOP"
last_command_time = 0
last_person_side = None
lost_person_since = None


def _serial_read_worker():
    """Background thread to read serial inputs from ESP32."""
    global ser
    if ser is None:
        return
    print("📡 Started background serial input reader...")
    while True:
        try:
            if ser.in_waiting:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if "ALERT:OBSTACLE" in line:
                    print("⚠️ ALERT: OBSTACLE DETECTED!")
                    try:
                        publish_form_error("Sudden block detected! Obstacle in path.")
                    except Exception:
                        pass
                    # Voice command play_audio_track(16) removed for person following

                # Parse Battery Voltage
                if ",VOLT:" in line or "VOLT:" in line:
                    try:
                        parts = line.split(",")
                        for part in parts:
                            if part.startswith("VOLT:"):
                                voltage = float(part.split(":")[1])
                                local_stream_server.broadcast_telemetry("battery_voltage", {"voltage": voltage})

                                now_ts = time.time()
                                if not hasattr(_serial_read_worker, "last_volt_ts"):
                                    _serial_read_worker.last_volt_ts = 0
                                if now_ts - _serial_read_worker.last_volt_ts > 10.0:
                                    publish_event("battery_voltage", {"voltage": voltage})
                                    _serial_read_worker.last_volt_ts = now_ts
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(0.1)

_serial_read_thread = threading.Thread(target=_serial_read_worker, daemon=True)
_serial_read_thread.start()


_manual_motion_override = False


def set_manual_motion_override(active):
    """Enable/disable manual voice motion override so tracking loops don't interrupt timed voice movements."""
    global _manual_motion_override
    _manual_motion_override = bool(active)


def is_manual_motion_override():
    """Return True if a voice-controlled timed movement is currently active."""
    return _manual_motion_override


def send_command(command, manual=False):
    """Send command to ESP32 over serial only when changed or refreshed."""
    global current_command, last_command_time
    if _manual_motion_override and not manual:
        return
    now = time.time()

    cmd_mapping = {
        "FORWARD": "FWD",
        "BACKWARD": "BACK",
        "LEFT": "LEFT",
        "MOVE_LEFT": "LEFT",
        "RIGHT": "RIGHT",
        "MOVE_RIGHT": "RIGHT",
        "ROTATE": "VEER_RIGHT",
        "ROTATE_LEFT": "VEER_LEFT",
        "ROTATE_RIGHT": "VEER_RIGHT",
        "FWD_LEFT": "VEER_LEFT",
        "FWD_RIGHT": "VEER_RIGHT",
        "BWD_LEFT": "VEER_LEFT",
        "BWD_RIGHT": "VEER_RIGHT",
        "STOP": "STOP",
        "VEER_LEFT": "VEER_LEFT",
        "VEER_RIGHT": "VEER_RIGHT",
    }

    mapped_command = cmd_mapping.get(command, command)

    if mapped_command in ["FWD", "BACK", "LEFT", "RIGHT", "VEER_LEFT", "VEER_RIGHT", "STOP"]:
        send_str = f"MOTOR:{mapped_command}"
    else:
        send_str = mapped_command

    if command != current_command or now - last_command_time > 1.0:
        if ser is not None:
            try:
                ser.write((send_str + "\n").encode('utf-8'))
                ser.flush()
                if DEBUG and command != current_command:
                    print(f"  📡 → ESP32 Serial: {send_str}")
            except Exception:
                pass

        current_command = command
        last_command_time = now
        publish_event("robot_command", {"command": command})


def calculate_command(person_x, body_height):
    """Calculate 8-directional movement command based on body height."""
    # Horizontal zone
    if person_x < CENTER_ZONE_LEFT:
        h_zone = "LEFT"
    elif person_x > CENTER_ZONE_RIGHT:
        h_zone = "RIGHT"
    else:
        h_zone = "CENTER"

    # Distance zone
    if body_height > TOO_CLOSE:
        d_zone = "CLOSE"
    elif body_height < TOO_FAR:
        d_zone = "FAR"
    else:
        d_zone = "GOOD"

    command = DIRECTION_MATRIX.get((d_zone, h_zone), "STOP")
    state = f"{command} (x:{person_x:.2f} h:{body_height:.3f})"
    return command, state, d_zone, h_zone


def reset_tracking_state():
    """Reset person-lost timer and side tracking when starting a new session."""
    global last_person_side, lost_person_since
    last_person_side = None
    lost_person_since = None


def adjust_robot_position(exercise_name, results, in_exercise_mode=False, get_pt_fn=None, get_body_height_fn=None):
    """
    Adjust robot orientation and distance to center user in frame.
    Returns status: 'TRACKING', 'SEARCHING', or 'LOST_TIMEOUT'.
    """
    global last_person_side, lost_person_since

    # 1. Person not detected
    if not results or not results.pose_landmarks or len(results.pose_landmarks) == 0:
        if in_exercise_mode:
            send_command("STOP")
            return "TRACKING"

        now = time.time()
        if lost_person_since is None:
            lost_person_since = now

        elapsed_lost = now - lost_person_since
        if elapsed_lost > 5.0:
            search_elapsed = elapsed_lost - 5.0
            if search_elapsed > 8.0:
                send_command("STOP")
                lost_person_since = None
                return "LOST_TIMEOUT"
            else:
                dir1 = "VEER_RIGHT" if last_person_side == "RIGHT" else "VEER_LEFT"
                dir2 = "VEER_RIGHT" if dir1 == "VEER_LEFT" else "VEER_LEFT"

                if search_elapsed < 2.0:
                    send_command(dir1)
                elif search_elapsed < 6.0:
                    send_command(dir2)
                else:
                    send_command(dir1)
                return "SEARCHING"
        else:
            send_command("STOP")
            return "TRACKING"

    # Reset lost person timer
    lost_person_since = None
    lm = results.pose_landmarks[0]

    # Calculate horizontal centering (h_zone)
    if get_pt_fn is not None:
        l_shoulder = get_pt_fn(lm, 11)
        r_shoulder = get_pt_fn(lm, 12)
        l_hip = get_pt_fn(lm, 23)
        r_hip = get_pt_fn(lm, 24)
        nose = get_pt_fn(lm, 0)
    else:
        l_shoulder = (lm[11].x, lm[11].y) if len(lm) > 11 else None
        r_shoulder = (lm[12].x, lm[12].y) if len(lm) > 12 else None
        l_hip = (lm[23].x, lm[23].y) if len(lm) > 23 else None
        r_hip = (lm[24].x, lm[24].y) if len(lm) > 24 else None
        nose = (lm[0].x, lm[0].y) if len(lm) > 0 else None

    if l_shoulder and r_shoulder:
        person_center = (l_shoulder[0] + r_shoulder[0]) / 2
    elif l_hip and r_hip:
        person_center = (l_hip[0] + r_hip[0]) / 2
    elif nose:
        person_center = nose[0]
    else:
        person_center = lm[0].x

    if person_center < CENTER_ZONE_LEFT:
        h_zone = "LEFT"
        last_person_side = "LEFT"
    elif person_center > CENTER_ZONE_RIGHT:
        h_zone = "RIGHT"
        last_person_side = "RIGHT"
    else:
        h_zone = "CENTER"
        last_person_side = None

    if get_body_height_fn is not None:
        body_height = get_body_height_fn(lm)
    else:
        body_height = 0.60

    if body_height > TOO_CLOSE:
        d_zone = "CLOSE"
    elif body_height < TOO_FAR:
        d_zone = "FAR"
    else:
        d_zone = "GOOD"

    command = DIRECTION_MATRIX.get((d_zone, h_zone), "STOP")

    if in_exercise_mode:
        send_command("STOP")
    else:
        send_command(command)

    return "TRACKING"
