#!/usr/bin/env python3
"""
8-Direction Person Following Robot
Raspberry Pi 4 + MediaPipe + ESP32 via WiFi UDP
Logitech C270 Camera
"""

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_DIR / ".env")
except ImportError:
    pass

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['GLOG_minloglevel'] = '3'

import cv2
import mediapipe as mp
import time
import numpy as np
import serial
import signal
import sys
import json
import urllib.request
import urllib.error
import threading
import queue
from datetime import datetime, timezone
import subprocess
import tkinter as tk
from tkinter import messagebox, ttk
import local_stream_server

# ─── Firebase Initialization ───
try:
    import firebase_admin
    from firebase_admin import auth as firebase_auth, credentials, firestore
    from datetime import datetime

    # Initialize Firebase using the service account key
    cred = credentials.Certificate(str(PROJECT_DIR / "serviceAccountKey.json"))
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("✅ Firebase initialized successfully.")
except Exception as e:
    db = None
    print(f"⚠️ Firebase initialization failed: {e}")


# Global session variables
home_clicked = False
last_session_summary = None
last_person_side = None
lost_person_since = None

def on_mouse_click(event, x, y, flags, param):
    global home_clicked
    if event == cv2.EVENT_LBUTTONDOWN:
        # Check if click is inside the HOME button area: x in [390, 470], y in [5, 45]
        if 390 <= x <= 470 and 5 <= y <= 45:
            home_clicked = True



# ═══════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════

# ─── ESP32 (UART5 Serial Connection) ───
SERIAL_PORT = "/dev/ttyAMA5"   # Standard Raspberry Pi UART5 Serial Port
BAUD_RATE   = 115200

# ─── Camera (C270) ───
CAMERA_INDEX  = 0
CAMERA_WIDTH  = 480            # Lower = faster on RPi4
CAMERA_HEIGHT = 320
FRAME_SKIP    = 2              # Process pose landmarks every N frames (optimized for RPi4)
HAND_FRAME_SKIP = 6            # Process hand landmarks every N frames (optimized for RPi4)

# ─── MediaPipe ───
MODEL_PATH = "pose_landmarker.task"
HAND_MODEL_PATH = "hand_landmarker.task"
PALM_STOP_REQUIRED_FRAMES = 12
PALM_STOP_MIN_HAND_SIZE = 0.12

# ─── Following Thresholds (YOUR CALIBRATED VALUES) ───
CENTER_ZONE_LEFT  = 0.42
CENTER_ZONE_RIGHT = 0.58
TOO_CLOSE = 0.43
TOO_FAR   = 0.18

# ─── Timing ───
COMMAND_COOLDOWN    = 0.1    # Min seconds between sending same command
LOST_PERSON_TIMEOUT = 1.5   # Seconds before searching

# ─── Display ───
SHOW_WINDOW  = True
DISPLAY_WIDTH  = 480         # Fixed display width for 480x320 screen
DISPLAY_HEIGHT = 320         # Fixed display height for 480x320 screen

# ─── Debug ───
DEBUG    = True
SHOW_FPS = True

# ─── Backend publishing (real data only) ───
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://localhost:8000").rstrip("/")
BACKEND_TOKEN = os.getenv("BACKEND_TOKEN", "").strip()
FIREBASE_USER_ID = os.getenv("FIREBASE_USER_ID", "").strip()
FIREBASE_USER_EMAIL = os.getenv("FIREBASE_USER_EMAIL", "").strip()


def _resolve_firebase_user_id():
    """Resolve the dashboard account while keeping an explicit UID authoritative."""
    global FIREBASE_USER_ID
    if db is None or FIREBASE_USER_ID:
        return

    try:
        if FIREBASE_USER_EMAIL:
            FIREBASE_USER_ID = firebase_auth.get_user_by_email(FIREBASE_USER_EMAIL).uid
            print(f"Firebase dashboard user resolved from {FIREBASE_USER_EMAIL}.")
            return

        users = list(firebase_auth.list_users(max_results=2).users)
        if len(users) == 1:
            FIREBASE_USER_ID = users[0].uid
            print(f"Firebase dashboard user auto-selected: {users[0].email or FIREBASE_USER_ID}")
        elif not users:
            print("WARNING: Firebase Auth has no dashboard users. Register in the web app first.")
        else:
            print("WARNING: Multiple Firebase users found. Set FIREBASE_USER_ID or FIREBASE_USER_EMAIL in .env.")
    except Exception as exc:
        print(f"WARNING: Could not resolve Firebase dashboard user: {exc}")


_resolve_firebase_user_id()

# Session / pause tracking for accurate active vs total time
_session_id = None
_session_start_wall = None
_pause_accumulator = 0.0

# Remote control from web UI
force_stop = threading.Event()
remote_mode_queue = queue.Queue()  # ("switch", "BICEP_CURLS") etc.

from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision


# ═══════════════════════════════════════════════════════
#  REAL-TIME PUBLISHING HELPERS (HTTP to backend)
#  All values come from the live exercise loops. No dummy data.
# ═══════════════════════════════════════════════════════

def _firebase_user_ref():
    """Return the Firestore user document used by this Pi."""
    if 'db' not in globals() or db is None or not FIREBASE_USER_ID:
        return None
    return db.collection("users").document(FIREBASE_USER_ID)


def _post_json(path, payload):
    """Legacy backend fallback. Never raises."""
    if not BACKEND_TOKEN:
        print("WARNING: Data was not published: Firebase user is unset and BACKEND_TOKEN is missing.")
        return None
    url = f"{BACKEND_BASE_URL}{path}"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {BACKEND_TOKEN}"
            },
            method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=2.5)
        return json.loads(resp.read().decode("utf-8") or "{}")
    except Exception:
        return None


# ─── Non-blocking publish worker ───
# All Firebase / HTTP calls are offloaded here so they never block the
# main exercise / camera loop.
_publish_queue = queue.Queue()

def _publish_worker():
    """Background thread: drain _publish_queue and execute each task."""
    while True:
        try:
            task = _publish_queue.get(timeout=1)
            if task is None:          # sentinel → exit
                break
            task()
        except queue.Empty:
            continue
        except Exception:
            pass

_publish_thread = threading.Thread(target=_publish_worker, daemon=True)
_publish_thread.start()


def _enqueue(fn):
    """Schedule fn() to run in the background publish thread."""
    try:
        _publish_queue.put_nowait(fn)
    except queue.Full:
        pass  # drop silently rather than block


def publish_event(event_type, payload):
    """Publish a generic event (non-blocking). Fire-and-forget."""
    p = dict(payload or {})
    if _session_id and "session_id" not in p:
        p["session_id"] = _session_id
    now = datetime.now(timezone.utc)

    # Broadcast to local WebSocket dashboard clients immediately
    try:
        local_stream_server.broadcast_telemetry(event_type, p)
    except Exception:
        pass

    def _do():
        user_ref = _firebase_user_ref()
        if user_ref is not None:
            try:
                event_ref = user_ref.collection("events").document()
                event_ref.set({
                    "type": event_type,
                    "payload": p,
                    "ts": now
                })
                _update_daily_aggregate_from_event(user_ref, event_type, p, now)
                return
            except Exception:
                pass
        _post_json("/api/events", {
            "type": event_type,
            "payload": p,
            "ts": now.isoformat()
        })

    _enqueue(_do)


def _update_daily_aggregate_from_event(user_ref, event_type, payload, ts):
    """Keep the same lightweight rollups the old backend maintained."""
    try:
        date_str = ts.date().isoformat()
        aggregate_ref = user_ref.collection("daily_aggregates").document(date_str)

        if event_type == "rep":
            aggregate_ref.set({
                "date": date_str,
                "form_good_count": firestore.Increment(1),
                "updated_at": firestore.SERVER_TIMESTAMP
            }, merge=True)

        elif event_type == "form_error":
            aggregate_ref.set({
                "date": date_str,
                "form_flaw_count": firestore.Increment(1),
                "updated_at": firestore.SERVER_TIMESTAMP
            }, merge=True)

        elif event_type == "session_end":
            updates = {"date": date_str, "updated_at": firestore.SERVER_TIMESTAMP}
            active = float(payload.get("active_duration_s") or 0)
            total_dur = float(payload.get("total_duration_s") or 0)
            if active:
                updates["active_time_s"] = firestore.Increment(round(active, 2))
            if total_dur:
                updates["total_time_s"] = firestore.Increment(round(total_dur, 2))

            summary = payload.get("summary") or {}
            exercise = summary.get("exercise") or payload.get("exercise")
            if exercise == "plank":
                plank_time = float(summary.get("total_plank_time_s") or summary.get("plank_time_s") or 0)
                if plank_time:
                    updates["plank_time_s"] = firestore.Increment(round(plank_time, 2))
            elif exercise in ("pushups", "squats"):
                reps = int(summary.get("total_reps") or 0)
                if reps:
                    updates[f"reps.{exercise}"] = firestore.Increment(reps)
            elif exercise == "bicep_curls":
                total_reps = int(summary.get("left_reps") or 0) + int(summary.get("right_reps") or 0)
                if total_reps:
                    updates["reps.bicep_curls"] = firestore.Increment(total_reps)

            aggregate_ref.set(updates, merge=True)
    except Exception:
        pass


def publish_session_start(exercise_type):
    """Create a session and store the id locally. Firestore/HTTP done in background."""
    global _session_id, _session_start_wall, _pause_accumulator
    now = datetime.now(timezone.utc)

    # Always stamp local timing immediately so the exercise loop is never blocked.
    _session_start_wall = time.time()
    _pause_accumulator = 0.0

    user_ref = _firebase_user_ref()
    if user_ref is not None:
        try:
            # Create the document reference synchronously (just a local object),
            # then write it in the background.
            session_ref = user_ref.collection("sessions").document()
            _session_id = session_ref.id
            sid = _session_id

            def _do():
                try:
                    session_ref.set({
                        "user_id": FIREBASE_USER_ID,
                        "exercise_type": exercise_type,
                        "started_at": now,
                        "ended_at": None,
                        "total_duration_s": None,
                        "active_duration_s": None,
                        "rest_duration_s": None,
                        "summary": {},
                        "robot_involved": exercise_type == "person_follow"
                    })
                    publish_event("session_start", {
                        "session_id": sid,
                        "exercise_type": exercise_type
                    })
                except Exception:
                    pass
            _enqueue(_do)
            return
        except Exception:
            _session_id = None

    if not BACKEND_TOKEN:
        _session_id = None
        return

    payload = {
        "exercise_type": exercise_type,
        "started_at": now.isoformat()
    }

    def _do_http():
        try:
            body = _post_json("/api/sessions/start", payload) or {}
            # session_id may arrive late; that's fine — events will still carry it.
            global _session_id
            if not _session_id:
                _session_id = body.get("session_id")
            publish_event("session_start", {"exercise_type": exercise_type})
        except Exception:
            pass
    _enqueue(_do_http)


def publish_session_end(summary=None):
    """End the current session. Firestore/HTTP writes done in background."""
    global _session_id, _session_start_wall, _pause_accumulator
    if not _session_id:
        _session_id = None
        _session_start_wall = None
        _pause_accumulator = 0.0
        return

    total = (time.time() - _session_start_wall) if _session_start_wall else 0.0
    active = max(0.0, total - _pause_accumulator)
    now = datetime.now(timezone.utc)
    sid = _session_id

    # Reset state immediately so the UI is never held up.
    _session_id = None
    _session_start_wall = None
    _pause_accumulator = 0.0

    payload = {
        "session_id": sid,
        "ended_at": now,
        "total_duration_s": round(total, 2),
        "active_duration_s": round(active, 2),
        "rest_duration_s": round(max(0.0, total - active), 2),
        "summary": summary or {}
    }

    user_ref = _firebase_user_ref()

    def _do():
        if user_ref is not None:
            try:
                user_ref.collection("sessions").document(sid).set(payload, merge=True)
                publish_event("session_end", payload)
                return
            except Exception:
                pass

        if not BACKEND_TOKEN:
            return
        try:
            legacy_payload = dict(payload)
            legacy_payload["ended_at"] = now.isoformat()
            _post_json("/api/sessions/end", legacy_payload)
            publish_event("session_end", legacy_payload)
        except Exception:
            pass

    _enqueue(_do)


def publish_rep(exercise, set_num=None, count=None, left=None, right=None, **extra):
    p = {"exercise": exercise}
    if set_num is not None: p["set"] = set_num
    if count is not None: p["count"] = count
    if left is not None: p["left"] = left
    if right is not None: p["right"] = right
    p.update(extra)
    publish_event("rep", p)


def publish_set_complete(exercise, set_num, reps):
    publish_event("set_complete", {"exercise": exercise, "set": set_num, "reps": reps})


def publish_form_error(message, side=None):
    p = {"message": message}
    if side: p["side"] = side
    publish_event("form_error", p)


def publish_robot_state(state, command=None):
    p = {"state": state}
    if command: p["command"] = command
    publish_event("robot_state", p)


def publish_plank_time(value):
    publish_event("plank_time", {"value": round(float(value), 2)})


def publish_pause():
    publish_event("pause", {})


def publish_resume():
    publish_event("resume", {})


# ─── Control listener (background thread) ───
def _control_listener():
    """Poll Firestore control commands and react to force_stop / switch_mode."""
    user_ref = _firebase_user_ref()
    if user_ref is None:
        return

    processed = set()
    while True:
        try:
            commands = user_ref.collection("control_commands").where("status", "==", "pending").stream()
            for doc in commands:
                if doc.id in processed:
                    continue

                data = doc.to_dict() or {}
                action = str(data.get("action") or "").lower()
                if action == "force_stop":
                    force_stop.set()
                elif action == "switch_mode":
                    mode = data.get("mode")
                    if mode:
                        remote_mode_queue.put(("switch", str(mode).upper()))

                processed.add(doc.id)
                doc.reference.set({
                    "status": "processed",
                    "processed_at": firestore.SERVER_TIMESTAMP,
                    "processed_by": "raspi"
                }, merge=True)
        except Exception:
            pass
        time.sleep(0.5)

# ═══════════════════════════════════════════════════════
#  EXERCISE HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════

def calculate_angle(a, b, c):
    """Calculate angle between three 2D points (a-b-c) in degrees."""
    a, b, c = np.array(a), np.array(b), np.array(c)
    radians = np.arctan2(c[1] - b[1], c[0] - b[0]) - np.arctan2(a[1] - b[1], a[0] - b[0])
    angle = np.abs(radians * 180.0 / np.pi)
    return 360 - angle if angle > 180.0 else angle


def calculate_angle_3d(a, b, c):
    """Calculate the true angle (in degrees) between three 3D joint landmarks (a-b-c)."""
    a = np.array(a)
    b = np.array(b)
    c = np.array(c)
    ba = a - b
    bc = c - b
    cosine_angle = np.dot(ba, bc) / (np.linalg.norm(ba) * np.linalg.norm(bc))
    angle = np.arccos(np.clip(cosine_angle, -1.0, 1.0))
    return np.degrees(angle)


def calculate_lateral_deviation(hip, knee, ankle):
    """Calculate the 2D lateral deviation angle of the knee relative to the hip-ankle line."""
    v_ha = np.array([ankle[0] - hip[0], ankle[1] - hip[1]])
    v_hk = np.array([knee[0] - hip[0], knee[1] - hip[1]])
    norm_ha = np.linalg.norm(v_ha)
    norm_hk = np.linalg.norm(v_hk)
    if norm_ha == 0 or norm_hk == 0:
        return 0.0
    cos_angle = np.dot(v_ha, v_hk) / (norm_ha * norm_hk)
    angle = np.degrees(np.arccos(np.clip(cos_angle, -1.0, 1.0)))
    return angle


def calculate_distance(a, b):
    """Calculate Euclidean distance between two points."""
    return np.linalg.norm(np.array(a) - np.array(b))


def check_plank(shoulder, hip, ankle, min_angle=165.0):
    """Check if body is in plank position by checking if the shoulder-hip-ankle angle is close to straight (165-180 degrees)."""
    angle = calculate_angle(shoulder, hip, ankle)
    return angle >= min_angle


def draw_positioning_overlay(image, position, elapsed_pct):
    """Draw a premium glass overlay indicating the robot is positioning itself."""
    h, w = image.shape[:2]
    
    # 1. Dark semi-transparent overlay over the entire frame
    overlay = image.copy()
    cv2.rectangle(overlay, (0, 0), (w, h), (10, 10, 15), -1)
    cv2.addWeighted(overlay, 0.55, image, 0.45, 0, image)
    
    # 2. Central glass panel
    panel_w = int(w * 0.75)
    panel_h = int(h * 0.45)
    px = (w - panel_w) // 2
    py = (h - panel_h) // 2
    draw_glass_panel(image, px, py, panel_w, panel_h, alpha=0.85, color=(15, 15, 20), radius=16)
    
    # Border
    cv2.rectangle(image, (px, py), (px + panel_w, py + panel_h), (80, 100, 120), 1, cv2.LINE_AA)
    cv2.rectangle(image, (px - 2, py - 2), (px + panel_w + 2, py + panel_h + 2), (40, 50, 60), 1, cv2.LINE_AA)

    # 3. Rotating spinner/progress indicator
    cx = w // 2
    cy = py + int(panel_h * 0.28)
    r = 20
    cv2.circle(image, (cx, cy), r, (40, 40, 40), 2, cv2.LINE_AA)
    sweep = int(elapsed_pct * 360)
    cv2.ellipse(image, (cx, cy), (r, r), -90, 0, sweep, ACCENT_GREEN, 2, cv2.LINE_AA)

    # 4. Text: "ROBOT POSITIONING"
    msg1 = "ROBOT POSITIONING"
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.6
    thickness = 2
    (tw1, th1), _ = cv2.getTextSize(msg1, font, font_scale, thickness)
    cv2.putText(image, msg1, (w // 2 - tw1 // 2, cy + 32), font, font_scale, TEXT_WHITE, thickness, cv2.LINE_AA)
    
    # 5. Text: "Aligning to FRONT view..." or "Aligning to SIDE view..."
    msg2 = f"Aligning to {position} view..."
    font_scale2 = 0.4
    thickness2 = 1
    (tw2, th2), _ = cv2.getTextSize(msg2, font, font_scale2, thickness2)
    cv2.putText(image, msg2, (w // 2 - tw2 // 2, cy + 52), font, font_scale2, TEXT_DIM, thickness2, cv2.LINE_AA)

    # 6. Progress bar
    progress_w = int(panel_w * 0.7)
    progress_h = 6
    bx = w // 2 - progress_w // 2
    by = py + int(panel_h * 0.8)
    
    cv2.rectangle(image, (bx, by), (bx + progress_w, by + progress_h), (40, 40, 40), -1)
    cv2.rectangle(image, (bx, by), (bx + int(progress_w * elapsed_pct), by + progress_h), ACCENT_GREEN, -1)
    cv2.rectangle(image, (bx, by), (bx + progress_w, by + progress_h), (60, 60, 60), 1)


# ═══════════════════════════════════════════════════════
#  ESP32 SERIAL SENDER (UART5)
# ═══════════════════════════════════════════════════════
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    print(f"✅ UART5 Serial Active → {SERIAL_PORT} @ {BAUD_RATE}")
except Exception as e:
    ser = None
    print(f"⚠️ Serial initialization failed: {e}")

current_command  = "STOP"
last_command_time = 0


def send_command(command):
    """Send command to ESP32 over serial only when changed."""
    global current_command, last_command_time
    now = time.time()

    # Map high-level directions to ESP32 movement commands
    cmd_mapping = {
        "FORWARD": "FWD",
        "BACKWARD": "BACK",
        "LEFT": "LEFT",
        "RIGHT": "RIGHT",
        "FWD_LEFT": "VEER_LEFT",
        "FWD_RIGHT": "VEER_RIGHT",
        "BWD_LEFT": "VEER_LEFT",
        "BWD_RIGHT": "VEER_RIGHT",
        "STOP": "STOP",
        "VEER_LEFT": "VEER_LEFT",
        "VEER_RIGHT": "VEER_RIGHT",
    }
    
    mapped_command = cmd_mapping.get(command, command)
    
    # Prepend MOTOR: prefix for valid movement commands
    if mapped_command in ["FWD", "BACK", "LEFT", "RIGHT", "VEER_LEFT", "VEER_RIGHT", "STOP"]:
        send_str = f"MOTOR:{mapped_command}"
    else:
        send_str = mapped_command

    if (command != current_command or
            now - last_command_time > 1.0):
        if ser is not None:
            try:
                ser.write((send_str + "\n").encode('utf-8'))
                ser.flush()
                if DEBUG and command != current_command:
                    print(f"  📡 → ESP32 Serial: {send_str}")
            except Exception:
                pass

        current_command   = command
        last_command_time = now

        # Publish robot command for live dashboard (real)
        publish_event("robot_command", {"command": command})


# ═══════════════════════════════════════════════════════
#  8 DIRECTION LOGIC
# ═══════════════════════════════════════════════════════

def calculate_command(person_x, shoulder_width):
    """Calculate 8-directional movement command."""

    # Horizontal zone
    if person_x < CENTER_ZONE_LEFT:
        h_zone = "LEFT"
    elif person_x > CENTER_ZONE_RIGHT:
        h_zone = "RIGHT"
    else:
        h_zone = "CENTER"

    # Distance zone
    if shoulder_width > TOO_CLOSE:
        d_zone = "CLOSE"
    elif shoulder_width < TOO_FAR:
        d_zone = "FAR"
    else:
        d_zone = "GOOD"

    # 8-direction matrix
    matrix = {
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

    command = matrix.get((d_zone, h_zone), "STOP")
    state = (f"{command} "
             f"(x:{person_x:.2f} w:{shoulder_width:.3f})")

    return command, state, d_zone, h_zone


# ═══════════════════════════════════════════════════════
#  DRAWING
# ═══════════════════════════════════════════════════════

CMD_COLORS = {
    'FORWARD':   (180, 220, 80),
    'BACKWARD':  (100, 100, 220),
    'LEFT':      (210, 190, 80),
    'RIGHT':     (200, 120, 200),
    'FWD_LEFT':  (180, 210, 100),
    'FWD_RIGHT': (160, 200, 120),
    'BWD_LEFT':  (180, 100, 200),
    'BWD_RIGHT': (140, 100, 210),
    'STOP':      (140, 140, 140),
    'SEARCH':    (100, 170, 230),
}

ACCENT_GREEN  = (150, 215, 95)
ACCENT_RED    = (95, 95, 210)
ACCENT_ORANGE = (80, 175, 225)
ACCENT_CYAN   = (200, 185, 100)
TEXT_WHITE     = (235, 235, 240)
TEXT_DIM       = (155, 155, 165)
PANEL_BG      = (25, 25, 30)

def bgr_to_hex(color):
    """Convert a BGR tuple or hex string color to a Tkinter-compatible hex string."""
    if isinstance(color, str):
        return color
    if isinstance(color, (tuple, list)) and len(color) >= 3:
        b, g, r = int(color[0]), int(color[1]), int(color[2])
        return f"#{r:02x}{g:02x}{b:02x}"
    return "#EBEBEF"

SKELETON_LANDMARK = (190, 210, 235)
SKELETON_LINE     = (170, 170, 90)


def draw_glass_panel(frame, x, y, w, h, alpha=0.65, color=(20, 20, 20), radius=12):
    """Draw a dark semi-transparent rounded rectangle panel."""
    x2, y2 = x + w, y + h
    fh, fw = frame.shape[:2]
    x, y = max(0, x), max(0, y)
    x2, y2 = min(fw, x2), min(fh, y2)
    w, h = x2 - x, y2 - y
    if w <= 0 or h <= 0:
        return
    radius = min(radius, w // 2, h // 2)
    sub = frame[y:y2, x:x2].copy()
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.rectangle(mask, (radius, 0), (w - radius, h), 255, -1)
    cv2.rectangle(mask, (0, radius), (w, h - radius), 255, -1)
    cv2.circle(mask, (radius, radius), radius, 255, -1)
    cv2.circle(mask, (w - radius, radius), radius, 255, -1)
    cv2.circle(mask, (radius, h - radius), radius, 255, -1)
    cv2.circle(mask, (w - radius, h - radius), radius, 255, -1)
    panel = np.full_like(sub, color, dtype=np.uint8)
    blended = cv2.addWeighted(panel, alpha, sub, 1 - alpha, 0)
    np.copyto(sub, blended, where=(mask[..., None] == 255))
    frame[y:y2, x:x2] = sub


def draw_home_button(frame):
    """Draw a premium touch-friendly HOME button on the OpenCV frame."""
    # Button is located at top right corner: x: [390, 470], y: [5, 45] for 480x320 display
    draw_glass_panel(frame, 390, 5, 80, 40, alpha=0.75, color=(30, 30, 80), radius=8)
    cv2.rectangle(frame, (390, 5), (470, 45), (100, 100, 200), 1, cv2.LINE_AA)
    cv2.putText(frame, "HOME", (406, 31), cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_WHITE, 2, cv2.LINE_AA)



def _draw_mini_arrow(frame, cx, cy, command, color, size=5):
    """Draw a small direction arrow or stop dot for zone grid cells."""
    if command == 'STOP':
        cv2.circle(frame, (cx, cy), 2, color, -1)
        return

    directions = {
        'FORWARD':   (0, -size),
        'BACKWARD':  (0,  size),
        'LEFT':      (-size, 0),
        'RIGHT':     ( size, 0),
        'FWD_LEFT':  (-size + 2, -size + 2),
        'FWD_RIGHT': ( size - 2, -size + 2),
        'BWD_LEFT':  (-size + 2,  size - 2),
        'BWD_RIGHT': ( size - 2,  size - 2),
    }

    dx, dy = directions.get(command, (0, 0))
    if dx == 0 and dy == 0:
        return

    end = (cx + dx, cy + dy)
    cv2.arrowedLine(frame, (cx, cy), end, color, 1, tipLength=0.5)


def draw_direction_indicator(frame, command, cx, cy, size=40):
    """Draw compass rose direction indicator."""
    pad = size + 10
    draw_glass_panel(frame, cx - pad, cy - pad, pad * 2, pad * 2,
                     alpha=0.7, radius=pad)

    cv2.circle(frame, (cx, cy), size + 3, (65, 65, 70), 1)

    directions = {
        'FORWARD':   (0, -size),
        'BACKWARD':  (0,  size),
        'LEFT':      (-size, 0),
        'RIGHT':     ( size, 0),
        'FWD_LEFT':  (-size + 8, -size + 8),
        'FWD_RIGHT': ( size - 8, -size + 8),
        'BWD_LEFT':  (-size + 8,  size - 8),
        'BWD_RIGHT': ( size - 8,  size - 8),
        'STOP':      (0, 0),
    }

    for cmd, (dx, dy) in directions.items():
        pos = (cx + dx, cy + dy)
        cv2.circle(frame, pos, 4, (55, 55, 60), -1)

    if command in directions:
        dx, dy = directions[command]
        active = (cx + dx, cy + dy)
        color  = CMD_COLORS.get(command, (255, 255, 255))
        cv2.circle(frame, active, 6, color, -1)
        cv2.circle(frame, active, 9, color, 1)
        if command != 'STOP':
            cv2.arrowedLine(frame, (cx, cy), active,
                            color, 2, tipLength=0.4)

    cv2.circle(frame, (cx, cy), 3, TEXT_WHITE, -1)


def draw_zone_grid(frame, d_zone, h_zone, gx, gy):
    """Draw 3x3 zone grid."""
    cell_w, cell_h = 38, 22
    total_w, total_h = cell_w * 3, cell_h * 3

    draw_glass_panel(frame, gx - 3, gy - 3, total_w + 6, total_h + 6,
                     alpha=0.7, radius=6)

    grid = [
        [("FWD_LEFT","FAR","LEFT"),   ("FORWARD","FAR","CENTER"),   ("FWD_RIGHT","FAR","RIGHT")],
        [("LEFT","GOOD","LEFT"),       ("STOP","GOOD","CENTER"),     ("RIGHT","GOOD","RIGHT")],
        [("BWD_LEFT","CLOSE","LEFT"),  ("BACKWARD","CLOSE","CENTER"),("BWD_RIGHT","CLOSE","RIGHT")],
    ]
    labels = [
        ["FL", "FW", "FR"],
        ["L",  "ST", "R"],
        ["BL", "BW", "BR"],
    ]

    for row in range(3):
        for col in range(3):
            x1 = gx + col * cell_w
            y1 = gy + row * cell_h
            x2 = x1 + cell_w - 1
            y2 = y1 + cell_h - 1

            cmd, dz, hz = grid[row][col]

            if dz == d_zone and hz == h_zone:
                color = CMD_COLORS.get(cmd, (255, 255, 255))
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, -1)
                txt_color = (0, 0, 0)
            else:
                txt_color = (100, 100, 100)

            cv2.rectangle(frame, (x1, y1), (x2, y2), (55, 55, 60), 1)
            cx = x1 + cell_w // 2
            cy = y1 + 8
            _draw_mini_arrow(frame, cx, cy, cmd, txt_color, size=4)
            cv2.putText(frame, labels[row][col],
                        (x1 + 3, y1 + cell_h - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.28,
                        txt_color, 1)


def draw_frame(frame, command, state, person_x,
               shoulder_width, fps, d_zone, h_zone):
    """Draw all overlay elements."""
    h, w = frame.shape[:2]

    # Zone overlays
    zone_l = int(CENTER_ZONE_LEFT * w)
    zone_r = int(CENTER_ZONE_RIGHT * w)

    ov = frame.copy()
    cv2.rectangle(ov, (0, 0), (zone_l, h), (200, 140, 60), -1)
    cv2.addWeighted(ov, 0.06, frame, 0.94, 0, frame)

    ov = frame.copy()
    cv2.rectangle(ov, (zone_r, 0), (w, h), (80, 80, 200), -1)
    cv2.addWeighted(ov, 0.06, frame, 0.94, 0, frame)

    cv2.line(frame, (zone_l, 0), (zone_l, h), (180, 180, 80), 1)
    cv2.line(frame, (zone_r, 0), (zone_r, h), (180, 180, 80), 1)

    # Person position dot
    if person_x is not None:
        px = int(person_x * w)
        cv2.circle(frame, (px, h // 2), 14, (130, 220, 220), 1)
        cv2.circle(frame, (px, h // 2), 8, (130, 220, 220), -1)
        cv2.circle(frame, (px, h // 2), 8, (0, 0, 0), 1)

    # Top bar
    draw_glass_panel(frame, 0, 0, w, 50, alpha=0.7, radius=0)
    cmd_color = CMD_COLORS.get(command, TEXT_WHITE)
    cv2.putText(frame, command, (12, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, cmd_color, 2)
    cv2.putText(frame, f"FPS:{fps:.0f}", (12, 43),
                cv2.FONT_HERSHEY_SIMPLEX, 0.35, TEXT_DIM, 1)

    # Detection indicator with glow
    dot = ACCENT_GREEN if person_x is not None else ACCENT_RED
    cv2.circle(frame, (w - 15, 15), 12, dot, 1)
    cv2.circle(frame, (w - 15, 15), 7, dot, -1)

    # State text
    if person_x is not None:
        cv2.putText(frame, state, (w // 2 - 40, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.3,
                    TEXT_DIM, 1)

    # Compass (top right)
    draw_direction_indicator(frame, command, w - 30, 95, size=25)

    # Zone grid (bottom left)
    draw_zone_grid(frame, d_zone, h_zone, 5, h - 75)

    # Distance bar (bottom)
    if shoulder_width is not None:
        bar_start = 125
        bar_end   = w - 5
        bar_y     = h - 22
        bar_h     = 14
        bar_max   = TOO_CLOSE + 0.15
        bar_pct   = min(shoulder_width / bar_max, 1.0)
        bar_w     = int(bar_pct * (bar_end - bar_start))

        if shoulder_width > TOO_CLOSE:
            bc = ACCENT_RED
        elif shoulder_width < TOO_FAR:
            bc = ACCENT_ORANGE
        else:
            bc = ACCENT_GREEN

        draw_glass_panel(frame, bar_start - 3, bar_y - 14,
                         bar_end - bar_start + 6, bar_h + 17,
                         alpha=0.6, radius=6)

        cv2.rectangle(frame, (bar_start, bar_y),
                      (bar_end, bar_y + bar_h), (40, 40, 40), -1)
        cv2.rectangle(frame, (bar_start, bar_y),
                      (bar_start + bar_w, bar_y + bar_h), bc, -1)

        fm = int((TOO_FAR / bar_max) * (bar_end - bar_start)) + bar_start
        cm = int((TOO_CLOSE / bar_max) * (bar_end - bar_start)) + bar_start
        cv2.line(frame, (fm, bar_y - 2), (fm, bar_y + bar_h + 2),
                 ACCENT_ORANGE, 1)
        cv2.line(frame, (cm, bar_y - 2), (cm, bar_y + bar_h + 2),
                 ACCENT_RED, 1)

        cv2.putText(frame, f"DIST: {shoulder_width:.3f}",
                    (bar_start, bar_y - 3),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.3,
                    TEXT_DIM, 1)


def draw_skeleton(frame, results):
    """Draw skeleton."""
    if not results.pose_landmarks or len(results.pose_landmarks) == 0:
        return

    lm = results.pose_landmarks[0]
    h, w = frame.shape[:2]

    connections = [
        (11, 12), (11, 13), (13, 15),
        (12, 14), (14, 16),
        (11, 23), (12, 24), (23, 24),
        (23, 25), (25, 27),
        (24, 26), (26, 28),
        (0, 11), (0, 12),
    ]

    for (i, j) in connections:
        if (i < len(lm) and j < len(lm) and
                lm[i].visibility > 0.5 and lm[j].visibility > 0.5):
            x1, y1 = int(lm[i].x * w), int(lm[i].y * h)
            x2, y2 = int(lm[j].x * w), int(lm[j].y * h)
            cv2.line(frame, (x1, y1), (x2, y2), SKELETON_LINE, 1)

    for pt in lm:
        if pt.visibility > 0.5:
            cv2.circle(frame, (int(pt.x * w), int(pt.y * h)),
                       3, SKELETON_LANDMARK, -1)


def create_pose_landmarker(det_conf=0.5, track_conf=0.5):
    """Create a PoseLandmarker using the MediaPipe Tasks API."""
    options = vision.PoseLandmarkerOptions(
        base_options=mp_tasks.BaseOptions(model_asset_path=MODEL_PATH),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=det_conf,
        min_pose_presence_confidence=det_conf,
        min_tracking_confidence=track_conf,
    )
    return vision.PoseLandmarker.create_from_options(options)


def create_hand_landmarker(det_conf=0.6, track_conf=0.6):
    """Create a HandLandmarker using the MediaPipe Tasks API. Returns None if task file is missing."""
    try:
        options = vision.HandLandmarkerOptions(
            base_options=mp_tasks.BaseOptions(model_asset_path=HAND_MODEL_PATH),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=det_conf,
            min_hand_presence_confidence=det_conf,
            min_tracking_confidence=track_conf,
        )
        return vision.HandLandmarker.create_from_options(options)
    except Exception as e:
        print(f"⚠️ Warning: Could not initialize hand landmarker: {e}")
        print("   Palm stop feature will be disabled.")
        return None


def detect_hand_frame(hand_landmarker, frame_bgr, timestamp_ms):
    """Run hand detection on a BGR frame; returns (results, new_timestamp_ms)."""
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
    timestamp_ms += 33
    results = hand_landmarker.detect_for_video(mp_image, timestamp_ms)
    return results, timestamp_ms


def _hand_point(hand_landmarks, idx):
    pt = hand_landmarks[idx]
    return np.array([pt.x, pt.y])


def is_open_palm_stop(hand_results, min_hand_size=PALM_STOP_MIN_HAND_SIZE):
    """Return True when MediaPipe Hands sees a clear open palm facing the camera with fingers spread."""
    if not hand_results.hand_landmarks:
        return False

    for idx, hand_landmarks in enumerate(hand_results.hand_landmarks):
        points = [_hand_point(hand_landmarks, idx_pt) for idx_pt in range(21)]
        xs = [pt[0] for pt in points]
        ys = [pt[1] for pt in points]
        hand_size = max(max(xs) - min(xs), max(ys) - min(ys))
        if hand_size < min_hand_size:
            continue

        # Get handedness label ("Left" or "Right")
        hand_label = "Right"
        try:
            if hand_results.handedness and idx < len(hand_results.handedness):
                hand_label = hand_results.handedness[idx][0].category_name
        except (AttributeError, IndexError):
            pass

        # Calculate cross product to distinguish palm from back of hand
        v1 = points[9] - points[0]   # Wrist to Middle Finger MCP
        v2 = points[17] - points[5]  # Index MCP to Pinky MCP
        cross_product_z = v1[0] * v2[1] - v1[1] * v2[0]

        is_palm_facing = False
        if hand_label == "Right":
            if cross_product_z < -0.05 * (hand_size ** 2):
                is_palm_facing = True
        elif hand_label == "Left":
            if cross_product_z > 0.05 * (hand_size ** 2):
                is_palm_facing = True

        wrist = points[0]
        fingers = [
            (5, 6, 8),    # index
            (9, 10, 12),  # middle
            (13, 14, 16), # ring
            (17, 18, 20), # pinky
        ]
        extended = 0
        for mcp_idx, pip_idx, tip_idx in fingers:
            mcp = points[mcp_idx]
            pip = points[pip_idx]
            tip = points[tip_idx]
            if (calculate_distance(tip, wrist) > calculate_distance(pip, wrist) * 1.08 and
                    calculate_distance(tip, mcp) > calculate_distance(pip, mcp)):
                extended += 1

        thumb_tip = points[4]
        thumb_ip = points[3]
        index_mcp = points[5]
        thumb_extended = (
            calculate_distance(thumb_tip, wrist) > calculate_distance(points[2], wrist) * 1.05 and
            calculate_distance(thumb_tip, index_mcp) > calculate_distance(thumb_ip, index_mcp)
        )

        # Spreading fingertip checks
        dist_thumb_index = calculate_distance(points[4], points[8])
        dist_index_middle = calculate_distance(points[8], points[12])
        dist_middle_ring = calculate_distance(points[12], points[16])
        dist_ring_pinky = calculate_distance(points[16], points[20])

        fingers_spread = (
            dist_index_middle > hand_size * 0.12 and
            dist_middle_ring > hand_size * 0.12 and
            dist_ring_pinky > hand_size * 0.12 and
            dist_thumb_index > hand_size * 0.20
        )

        if extended >= 4 and thumb_extended and is_palm_facing and fingers_spread:
            return True

    return False


def update_palm_stop_frames(hand_results, stop_frames, increment=1):
    """Debounce palm stop so one accidental frame does not quit the exercise."""
    if is_open_palm_stop(hand_results):
        return stop_frames + increment
    return 0


def draw_hand_controls_status(image, thumbs_up_frames, palm_stop_frames, required_frames=PALM_STOP_REQUIRED_FRAMES, is_paused=False):
    """Draw thumbs-up and palm-stop progress panels dynamically, hiding them when not in use."""
    h, w = image.shape[:2]
    compact = h <= 240
    panel_w = 180 if compact else 205
    panel_h = 45 if compact else 53
    x = max(5, w - panel_w - 5)
    
    current_y = h - panel_h - 5
    
    # 1. Palm Stop Panel (only draw if palm_stop_frames > 0)
    if palm_stop_frames > 0:
        draw_glass_panel(image, x, current_y, panel_w, panel_h, alpha=0.65, radius=10)
        progress = min(palm_stop_frames / required_frames, 1.0)
        color = ACCENT_GREEN if progress >= 1.0 else ACCENT_ORANGE
        cv2.putText(image, "PALM STOP", (x + 8, current_y + 17),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38 if compact else 0.45,
                    color, 1, cv2.LINE_AA)
        label = "STOPPING" if progress >= 1.0 else "HOLD PALM"
        cv2.putText(image, label, (x + 8, current_y + 36),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38 if compact else 0.45,
                    TEXT_WHITE, 1, cv2.LINE_AA)
        bar_x = x + (95 if compact else 108)
        bar_y = current_y + (29 if compact else 34)
        bar_w, bar_h = (70, 7) if compact else (82, 8)
        cv2.rectangle(image, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h),
                      (40, 40, 40), -1)
        cv2.rectangle(image, (bar_x, bar_y),
                      (bar_x + int(bar_w * progress), bar_y + bar_h),
                      color, -1)
        cv2.rectangle(image, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h),
                      (60, 60, 60), 1)
        
        current_y -= (panel_h + 7)

    # 2. Thumbs Up Panel (only draw if thumbs_up_frames > 0)
    if thumbs_up_frames > 0:
        draw_glass_panel(image, x, current_y, panel_w, panel_h, alpha=0.65, radius=10)
        progress = min(thumbs_up_frames / required_frames, 1.0)
        color = ACCENT_GREEN if progress >= 1.0 else ACCENT_CYAN
        cv2.putText(image, "THUMBS UP", (x + 8, current_y + 17),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38 if compact else 0.45,
                    color, 1, cv2.LINE_AA)
        label = "PAUSING..." if not is_paused else "RESUMING..."
        if progress < 1.0:
            label = "HOLD THUMB"
        cv2.putText(image, label, (x + 8, current_y + 36),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38 if compact else 0.45,
                    TEXT_WHITE, 1, cv2.LINE_AA)
        bar_x = x + (95 if compact else 108)
        bar_y = current_y + (29 if compact else 34)
        bar_w, bar_h = (70, 7) if compact else (82, 8)
        cv2.rectangle(image, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h),
                      (40, 40, 40), -1)
        cv2.rectangle(image, (bar_x, bar_y),
                      (bar_x + int(bar_w * progress), bar_y + bar_h),
                      color, -1)
        cv2.rectangle(image, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h),
                      (60, 60, 60), 1)


def show_palm_stop_message(window_name, image):
    """Show a brief confirmation before leaving the exercise window."""
    h, w = image.shape[:2]
    panel_w = min(220, w - 20)
    panel_h = 48 if h <= 240 else 60
    x = w // 2 - panel_w // 2
    y = h // 2 - panel_h // 2
    draw_glass_panel(image, x, y, panel_w, panel_h, alpha=0.78, radius=12)
    cv2.putText(image, "PALM STOP DETECTED", (x + 16, y + panel_h // 2 + 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42 if h <= 240 else 0.55,
                ACCENT_GREEN, 1 if h <= 240 else 2, cv2.LINE_AA)
    scale_w = DISPLAY_WIDTH
    scale_h = DISPLAY_HEIGHT
    display = cv2.resize(image, (scale_w, scale_h))
    cv2.imshow(window_name, display)
    cv2.waitKey(500)


def is_thumbs_up(hand_results, min_hand_size=PALM_STOP_MIN_HAND_SIZE):
    """Return True when MediaPipe Hands sees a clear thumbs-up gesture from any hand."""
    if not hand_results or not hand_results.hand_landmarks:
        return False

    for hand_landmarks in hand_results.hand_landmarks:
        points = [_hand_point(hand_landmarks, idx_pt) for idx_pt in range(21)]
        xs = [pt[0] for pt in points]
        ys = [pt[1] for pt in points]
        hand_size = max(max(xs) - min(xs), max(ys) - min(ys))
        if hand_size < min_hand_size:
            continue

        wrist = points[0]
        fingers = [
            (5, 6, 8),    # index
            (9, 10, 12),  # middle
            (13, 14, 16), # ring
            (17, 18, 20), # pinky
        ]
        extended = 0
        for mcp_idx, pip_idx, tip_idx in fingers:
            mcp = points[mcp_idx]
            pip = points[pip_idx]
            tip = points[tip_idx]
            if (calculate_distance(tip, wrist) > calculate_distance(pip, wrist) * 1.08 and
                    calculate_distance(tip, mcp) > calculate_distance(pip, mcp)):
                extended += 1

        # More responsive: at most 1 other finger can be misclassified as extended
        if extended > 1:
            continue

        # Check if thumb is extended and pointing up
        thumb_tip = points[4]
        thumb_ip = points[3]
        thumb_mcp = points[2]
        index_mcp = points[5]

        thumb_extended = (
            calculate_distance(thumb_tip, wrist) > calculate_distance(thumb_mcp, wrist) * 1.05 and
            calculate_distance(thumb_tip, index_mcp) > calculate_distance(thumb_ip, index_mcp)
        )

        thumb_pointing_up = (
            thumb_tip[1] < thumb_ip[1] and
            thumb_tip[1] < index_mcp[1] and
            thumb_tip[1] < wrist[1]
        )

        if thumb_extended and thumb_pointing_up:
            return True

    return False


def draw_paused_overlay(image, thumbs_up_frames=0, required_frames=PALM_STOP_REQUIRED_FRAMES):
    """Draw a premium dark glass overlay when the exercise is paused."""
    h, w = image.shape[:2]
    
    # 1. Dark semi-transparent overlay over the entire frame
    overlay = image.copy()
    cv2.rectangle(overlay, (0, 0), (w, h), (10, 10, 15), -1)
    cv2.addWeighted(overlay, 0.6, image, 0.4, 0, image)
    
    # 2. Large central glass panel
    panel_w = int(w * 0.75)
    panel_h = int(h * 0.5)
    px = (w - panel_w) // 2
    py = (h - panel_h) // 2
    draw_glass_panel(image, px, py, panel_w, panel_h, alpha=0.85, color=(15, 15, 20), radius=16)
    
    # Border
    cv2.rectangle(image, (px, py), (px + panel_w, py + panel_h), (80, 100, 120), 1, cv2.LINE_AA)
    cv2.rectangle(image, (px - 2, py - 2), (px + panel_w + 2, py + panel_h + 2), (40, 50, 60), 1, cv2.LINE_AA)

    # 3. Draw Pause Icon (||)
    icon_cx = w // 2
    icon_cy = py + int(panel_h * 0.28)
    bar_w, bar_h = 8, 24
    bar_gap = 8
    cv2.rectangle(image, (icon_cx - bar_w - bar_gap//2, icon_cy - bar_h//2), 
                  (icon_cx - bar_gap//2, icon_cy + bar_h//2), ACCENT_ORANGE, -1)
    cv2.rectangle(image, (icon_cx + bar_gap//2, icon_cy - bar_h//2), 
                  (icon_cx + bar_w + bar_gap//2, icon_cy + bar_h//2), ACCENT_ORANGE, -1)
    
    # 4. Text: "EXERCISE PAUSED"
    msg1 = "EXERCISE PAUSED"
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.65
    thickness = 2
    (tw1, th1), _ = cv2.getTextSize(msg1, font, font_scale, thickness)
    cv2.putText(image, msg1, (w // 2 - tw1 // 2, icon_cy + 30), font, font_scale, TEXT_WHITE, thickness, cv2.LINE_AA)
    
    # 5. Text: "Show Thumbs-Up to Resume"
    msg2 = "Show Thumbs-Up to Resume"
    font_scale2 = 0.4
    thickness2 = 1
    (tw2, th2), _ = cv2.getTextSize(msg2, font, font_scale2, thickness2)
    cv2.putText(image, msg2, (w // 2 - tw2 // 2, icon_cy + 52), font, font_scale2, TEXT_DIM, thickness2, cv2.LINE_AA)

    # 6. Progress bar for thumbs-up resume
    if thumbs_up_frames > 0:
        progress = min(thumbs_up_frames / required_frames, 1.0)
        progress_w = int(panel_w * 0.6)
        progress_h = 6
        bx = w // 2 - progress_w // 2
        by = py + int(panel_h * 0.8)
        
        cv2.rectangle(image, (bx, by), (bx + progress_w, by + progress_h), (40, 40, 40), -1)
        cv2.rectangle(image, (bx, by), (bx + int(progress_w * progress), by + progress_h), ACCENT_GREEN, -1)
        cv2.rectangle(image, (bx, by), (bx + progress_w, by + progress_h), (60, 60, 60), 1)


def detect_pose_frame(landmarker, frame_bgr, timestamp_ms):
    """Run pose detection on a BGR frame; returns (results, new_timestamp_ms)."""
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
    timestamp_ms += 33
    results = landmarker.detect_for_video(mp_image, timestamp_ms)
    return results, timestamp_ms


def get_pt(lm, idx, min_vis=0.5, use_z=False):
    """Get landmark [x,y] or [x,y,z] if visible, else None."""
    p = lm[idx]
    if p.visibility <= min_vis:
        return None
    return [p.x, p.y, p.z] if use_z else [p.x, p.y]


# ═══════════════════════════════════════════════════════
#  SESSION ERROR LOGGER
# ═══════════════════════════════════════════════════════
session_errors = set()
last_printed_errors = {}

def clear_session_errors():
    """Clear session error tracking."""
    global session_errors, last_printed_errors
    session_errors = set()
    last_printed_errors = {}

def log_session_error(err_msg, cooldown=3.0):
    """Log an error to the session errors set and print it with a cooldown.
    Also publishes the form feedback to the web backend (real data only).
    """
    global session_errors, last_printed_errors
    session_errors.add(err_msg)
    now = time.time()
    if err_msg not in last_printed_errors or now - last_printed_errors[err_msg] > cooldown:
        print(f"  [SESSION WARNING] {err_msg}")
        last_printed_errors[err_msg] = now
        # Publish to live dashboard (exact message shown on-robot / console)
        try:
            publish_form_error(err_msg)
        except Exception:
            pass


def save_session_to_firestore(summary):
    if 'db' not in globals() or db is None:
        print("⚠️ Cannot save to Firestore: Database not initialized.")
        return
        
    try:
        # Convert metrics list of tuples to a dictionary
        metrics_dict = {k: v for k, v in summary['metrics']}
        
        doc_data = {
            "exercise": summary['exercise'],
            "metrics": metrics_dict,
            "errors": summary.get('errors', []),
            "timestamp": datetime.now()
        }
        
        # Save to 'workout_sessions' collection
        user_ref = _firebase_user_ref()
        if user_ref is not None:
            user_ref.collection("workout_sessions").add(doc_data)
        else:
            db.collection("workout_sessions").add(doc_data)
        print(f"✅ Session saved to Firestore: {summary['exercise']}")
    except Exception as e:
        print(f"⚠️ Failed to save session to Firestore: {e}")

def print_session_error_summary():
    """Print a summary of all unique errors logged in the session."""
    global session_errors
    if session_errors:
        print("\n" + "=" * 50)
        print("  SESSION ERROR SUMMARY (ALL DETECTED ERRORS)")
        print("=" * 50)
        for err in sorted(session_errors):
            print(f"  • {err}")
        print("=" * 50 + "\n")


def adjust_robot_position(exercise_name, results):
    """
    Adjust the robot's physical position dynamically during exercise detection.
    Optimized for a camera height of 2.5 feet above the ground.
    """
    global last_person_side, lost_person_since

    # 1. If person is NOT detected (goes out of bounding box / camera frame)
    if not results or not results.pose_landmarks or len(results.pose_landmarks) == 0:
        now = time.time()
        if lost_person_since is None:
            lost_person_since = now
        
        elapsed_lost = now - lost_person_since
        if elapsed_lost > 2.0:
            search_elapsed = elapsed_lost - 2.0
            if search_elapsed > 8.0:
                send_command("STOP")
                return "LOST_TIMEOUT"
            else:
                dir1 = "VEER_LEFT" if last_person_side == "LEFT" else "VEER_RIGHT"
                dir2 = "VEER_RIGHT" if dir1 == "VEER_LEFT" else "VEER_LEFT"
                
                if search_elapsed < 2.0:
                    send_command(dir1)
                elif search_elapsed < 6.0:
                    send_command(dir2)
                else:
                    send_command(dir1)
                return "SEARCHING"
        else:
            if last_person_side == "LEFT":
                send_command("VEER_LEFT")
            elif last_person_side == "RIGHT":
                send_command("VEER_RIGHT")
            else:
                send_command("STOP")
            return "TRACKING"

    # Reset lost person timer
    lost_person_since = None

    # Reset lost person timer
    lost_person_since = None
    lm = results.pose_landmarks[0]

    # Thresholds tailored for a camera height of 2.5 feet above the ground:
    if exercise_name in ("BICEP_CURLS", "SQUATS"):
        # Frontal view (standing)
        l_shoulder = get_pt(lm, 11)
        r_shoulder = get_pt(lm, 12)

        if l_shoulder and r_shoulder:
            shoulder_width = abs(l_shoulder[0] - r_shoulder[0])
            shoulder_center = (l_shoulder[0] + r_shoulder[0]) / 2

            # Curls need shoulders & arms/wrists in frame
            if exercise_name == "BICEP_CURLS":
                too_close = 0.22  # Larger means closer
                too_far = 0.14
            # Squats need full body from shoulders down to ankles in frame
            else:  # SQUATS
                too_close = 0.20
                too_far = 0.12

            # Distance zone
            if shoulder_width > too_close:
                d_zone = "CLOSE"
            elif shoulder_width < too_far:
                d_zone = "FAR"
            else:
                d_zone = "GOOD"

            # Horizontal zone
            if shoulder_center < CENTER_ZONE_LEFT:
                h_zone = "LEFT"
                last_person_side = "LEFT"
            elif shoulder_center > CENTER_ZONE_RIGHT:
                h_zone = "RIGHT"
                last_person_side = "RIGHT"
            else:
                h_zone = "CENTER"
                last_person_side = None

            # 8-direction mapping
            matrix = {
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
            command = matrix.get((d_zone, h_zone), "STOP")
            send_command(command)
        else:
            send_command("STOP")

    elif exercise_name in ("PUSH_UPS", "PLANK"):
        # Side view (lying)
        # We need to detect best side profile shoulder and ankle
        left_shoulder = get_pt(lm, 11)
        left_ankle = get_pt(lm, 27)
        right_shoulder = get_pt(lm, 12)
        right_ankle = get_pt(lm, 28)

        shoulder, ankle = None, None
        left_ok = left_shoulder and left_ankle
        right_ok = right_shoulder and right_ankle

        if left_ok and right_ok:
            left_vis = lm[11].visibility + lm[27].visibility
            right_vis = lm[12].visibility + lm[28].visibility
            if left_vis > right_vis:
                shoulder, ankle = left_shoulder, left_ankle
            else:
                shoulder, ankle = right_shoulder, right_ankle
        elif left_ok:
            shoulder, ankle = left_shoulder, left_ankle
        elif right_ok:
            shoulder, ankle = right_shoulder, right_ankle

        if shoulder and ankle:
            # Horizontal span of the body in normalized x coordinates
            body_length = abs(shoulder[0] - ankle[0])
            body_center = (shoulder[0] + ankle[0]) / 2

            too_close = 0.60
            too_far = 0.40

            if body_length > too_close:
                d_zone = "CLOSE"
            elif body_length < too_far:
                d_zone = "FAR"
            else:
                d_zone = "GOOD"

            if body_center < CENTER_ZONE_LEFT:
                h_zone = "LEFT"
                last_person_side = "LEFT"
            elif body_center > CENTER_ZONE_RIGHT:
                h_zone = "RIGHT"
                last_person_side = "RIGHT"
            else:
                h_zone = "CENTER"
                last_person_side = None

            matrix = {
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
            command = matrix.get((d_zone, h_zone), "STOP")
            send_command(command)
        else:
            send_command("STOP")


# ═══════════════════════════════════════════════════════
#  EXERCISE MODES
# ═══════════════════════════════════════════════════════

def run_bicep_curls(ui=None):
    """Bicep curl counter with form detection for both arms."""
    clear_session_errors()

    def process_arm(shoulder, elbow, wrist, side_name, state_dict, image, img_w, img_h):
        if shoulder is None or elbow is None or wrist is None:
            return "N/A", ACCENT_RED, "---", False

        angle = calculate_angle(shoulder, elbow, wrist)

        swing_error = False
        if abs(wrist[0] - elbow[0]) > 0.08:
            swing_error = True

        flare_error = abs(shoulder[0] - elbow[0]) > 1.5
        color = ACCENT_GREEN
        feedback = "GOOD"

        if flare_error:
            feedback = "TUCK ELBOW"
            color = ACCENT_ORANGE
            log_session_error(f"{side_name} Arm: Tuck elbow closer to your body")
        elif swing_error:
            feedback = "DONT SWING"
            color = ACCENT_ORANGE
            log_session_error(f"{side_name} Arm: Do not swing arm")

        if angle > 150:
            state_dict['stage'] = "down"
        if angle < 30 and state_dict['stage'] == "down":
            state_dict['stage'] = "up"
            if not flare_error and not swing_error:
                state_dict['counter'] += 1

        bar_val = np.interp(angle, (30, 150), (100, 0))
        bar_x = img_w - 35 if side_name == "Left" else img_w - 18
        bar_y_top = 100
        bar_h = 120
        fill_h = int(bar_val * bar_h / 100)

        draw_glass_panel(image, bar_x - 3, bar_y_top - 18,
                         20, bar_h + 30, alpha=0.6, radius=6)
        cv2.putText(image, side_name[0], (bar_x, bar_y_top - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, TEXT_DIM, 1)
        cv2.rectangle(image, (bar_x, bar_y_top),
                      (bar_x + 14, bar_y_top + bar_h), (40, 40, 40), -1)
        cv2.rectangle(image, (bar_x, bar_y_top + bar_h - fill_h),
                      (bar_x + 14, bar_y_top + bar_h), color, -1)
        cv2.rectangle(image, (bar_x, bar_y_top),
                      (bar_x + 14, bar_y_top + bar_h), (60, 60, 60), 1)

        return feedback, color, state_dict['stage'].upper(), swing_error

    arms = {
        'left': {'counter': 0, 'stage': 'down'},
        'right': {'counter': 0, 'stage': 'down'}
    }
    curl_timestamps = []
    warning_active_until = 0.0
    last_console_warning_time = 0.0
    swing_warning_active_until = 0.0
    last_swing_warning_time = 0.0

    # Workout set/rep trackers
    current_set = 1
    current_set_reps = 0
    set_finished = False
    last_rep_time = None
    left_reps_at_set_start = 0
    right_reps_at_set_start = 0
    last_uneven_warning_time = 0.0

    cap = cv2.VideoCapture(CAMERA_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    if ui is None:
        cv2.namedWindow('Bicep Curls', cv2.WINDOW_NORMAL)
        cv2.resizeWindow('Bicep Curls', DISPLAY_WIDTH, DISPLAY_HEIGHT)
    global home_clicked
    home_clicked = False
    if ui is None:
        cv2.setMouseCallback('Bicep Curls', on_mouse_click)

    print("🏋️ Bicep Curl Counter Active! Press Q to quit.")

    publish_robot_state("LOADING")
    landmarker = create_pose_landmarker(det_conf=0.7, track_conf=0.7)
    hand_landmarker = create_hand_landmarker()
    timestamp_ms = 0
    hand_timestamp_ms = 0
    palm_stop_frames = 0
    frame_counter = 0
    last_results = None
    last_hand_results = None

    paused = False
    thumbs_up_frames = 0
    thumbs_up_cooldown_active = False
    pause_start_time = 0.0

    # Positioning state
    positioning_duration = 3.0
    positioning_start_time = time.time()
    positioning_completed = False
    send_command("FRONT")

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            # Remote Force Stop from web dashboard
            if force_stop.is_set():
                print("  [REMOTE] Force Stop received from web UI")
                send_command("STOP")
                break

            frame_counter += 1
            frame = cv2.flip(frame, 1)
            image = cv2.resize(frame, (DISPLAY_WIDTH, DISPLAY_HEIGHT))
            img_h, img_w, _ = image.shape

            # Run Pose detection every FRAME_SKIP frames
            if frame_counter % FRAME_SKIP == 0 or last_results is None:
                results, timestamp_ms = detect_pose_frame(landmarker, frame, timestamp_ms)
                last_results = results
            else:
                results = last_results

            # Check positioning phase
            if not positioning_completed:
                elapsed = time.time() - positioning_start_time
                if elapsed < positioning_duration:
                    pct = elapsed / positioning_duration
                    if results and results.pose_landmarks:
                        draw_skeleton(image, results)
                    draw_positioning_overlay(image, "FRONT", pct)
                    draw_home_button(image)
                    local_stream_server.update_frame(image)

                    if ui is not None:
                        ui.update_feedback(f"🤖 ROBOT POSITIONING...\nAligning to FRONT view... ({int(pct*100)}%)", ACCENT_CYAN)
                        ui.update()
                    else:
                        send_command("FRONT")
                        cv2.imshow('Bicep Curls', image)
                        if cv2.waitKey(10) & 0xFF == ord('q') or home_clicked:
                            break
                    send_command("FRONT")
                    continue
                else:
                    send_command("STOP")
                    positioning_completed = True
                    # Start real session for live dashboard (no dummy data)
                    if _session_id is None:
                        try:
                            publish_session_start("bicep_curls")
                            publish_robot_state("ACTIVE")
                        except Exception:
                            pass

            # Run Hand detection every HAND_FRAME_SKIP frames
            if hand_landmarker is not None:
                if frame_counter % HAND_FRAME_SKIP == 0 or last_hand_results is None:
                    hand_results, hand_timestamp_ms = detect_hand_frame(
                        hand_landmarker, frame, hand_timestamp_ms
                    )
                    last_hand_results = hand_results

                    # Thumbs-up pause/resume check
                    if is_thumbs_up(hand_results):
                        if not thumbs_up_cooldown_active:
                            thumbs_up_frames += HAND_FRAME_SKIP
                            if thumbs_up_frames >= PALM_STOP_REQUIRED_FRAMES:
                                paused = not paused
                                thumbs_up_frames = 0
                                thumbs_up_cooldown_active = True
                                if paused:
                                    pause_start_time = time.time()
                                    try:
                                        publish_pause()
                                    except Exception:
                                        pass
                                else:
                                    pause_duration = time.time() - pause_start_time
                                    try:
                                        _pause_accumulator += pause_duration
                                    except Exception:
                                        pass
                                    try:
                                        publish_resume()
                                    except Exception:
                                        pass
                                    # Adjust timestamps
                                    if last_rep_time is not None:
                                        last_rep_time += pause_duration
                                    if last_uneven_warning_time is not None:
                                        last_uneven_warning_time += pause_duration
                                    warning_active_until += pause_duration
                                    swing_warning_active_until += pause_duration
                    else:
                        thumbs_up_frames = 0
                        thumbs_up_cooldown_active = False

                    palm_stop_frames = update_palm_stop_frames(hand_results, palm_stop_frames, increment=HAND_FRAME_SKIP)
                else:
                    hand_results = last_hand_results
            else:
                palm_stop_frames = 0
                thumbs_up_frames = 0

            # If paused, render overlay and bypass main logic
            if paused:
                send_command("STOP")
                if results and results.pose_landmarks:
                    draw_skeleton(image, results)
                draw_paused_overlay(image, thumbs_up_frames)
                if hand_landmarker is not None:
                    draw_hand_controls_status(image, thumbs_up_frames, palm_stop_frames, is_paused=True)
                draw_home_button(image)
                local_stream_server.update_frame(image)

                if ui is not None:
                    ui.update_feedback("⏸️ EXERCISE PAUSED\nShow Thumbs-Up to Resume", ACCENT_ORANGE)
                    ui.update()
                    if hand_landmarker is not None:
                        if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                            break
                else:
                    if hand_landmarker is not None:
                        if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                            show_palm_stop_message('Bicep Curls', image)
                            break
                    cv2.imshow('Bicep Curls', image)
                    if cv2.waitKey(10) & 0xFF == ord('q') or home_clicked:
                        break
                continue

            # Calculate set stats
            now = time.time()
            current_set_left_reps = arms['left']['counter'] - left_reps_at_set_start
            current_set_right_reps = arms['right']['counter'] - right_reps_at_set_start
            current_set_reps = min(current_set_left_reps, current_set_right_reps)

            # End current set if >= 8 reps completed and no new reps done for 10 seconds
            if current_set_reps >= 8 and not set_finished:
                if last_rep_time is not None and now - last_rep_time > 10.0:
                    set_finished = True
                    print(f"\n✅ Set {current_set} Finished! Completed {current_set_reps} reps.")

            # Warn user if the hands have uneven reps in the current set
            is_uneven = (current_set_left_reps != current_set_right_reps) and (max(current_set_left_reps, current_set_right_reps) > 0)
            if is_uneven and not set_finished:
                if now - last_uneven_warning_time > 5.0:
                    log_session_error(f"Equalize both hands (Left: {current_set_left_reps} | Right: {current_set_right_reps})")
                    last_uneven_warning_time = now

            robot_status = "TRACKING"
            if positioning_completed and not paused:
                robot_status = robot_status = adjust_robot_position("BICEP_CURLS", results)
                if robot_status == "LOST_TIMEOUT":
                    break
                if robot_status == "LOST_TIMEOUT":
                    break

            if results.pose_landmarks and len(results.pose_landmarks) > 0:
                lm = results.pose_landmarks[0]

                # Swapped to account for camera mirroring: MediaPipe Right (12, 14, 16) -> Left arm on display, MediaPipe Left (11, 13, 15) -> Right arm on display
                l_s, l_e, l_w = get_pt(lm, 12), get_pt(lm, 14), get_pt(lm, 16)
                r_s, r_e, r_w = get_pt(lm, 11), get_pt(lm, 13), get_pt(lm, 15)

                prev_l_cnt = arms['left']['counter']
                prev_r_cnt = arms['right']['counter']

                l_fb, l_clr, l_stg, l_swing = process_arm(l_s, l_e, l_w, "Left", arms['left'], image, img_w, img_h)
                r_fb, r_clr, r_stg, r_swing = process_arm(r_s, r_e, r_w, "Right", arms['right'], image, img_w, img_h)

                if l_swing or r_swing:
                    swing_warning_active_until = now + 0.5
                    if now - last_swing_warning_time > 3.0:
                        log_session_error("Keep hands in straight position without swinging")
                        last_swing_warning_time = now

                if (arms['left']['counter'] > prev_l_cnt) or (arms['right']['counter'] > prev_r_cnt):
                    curl_timestamps.append(now)
                    if set_finished:
                        try:
                            publish_set_complete("bicep_curls", current_set, current_set_reps)
                        except Exception:
                            pass
                        current_set += 1
                        left_reps_at_set_start = prev_l_cnt
                        right_reps_at_set_start = prev_r_cnt
                        set_finished = False
                    last_rep_time = now
                    # Publish live rep update (real data from MediaPipe)
                    try:
                        publish_rep(
                            "bicep_curls",
                            set_num=current_set,
                            left=arms['left']['counter'],
                            right=arms['right']['counter']
                        )
                    except Exception:
                        pass

                curl_timestamps = [t for t in curl_timestamps if now - t <= 2.0]
                if len(curl_timestamps) >= 4:
                    warning_active_until = now + 2.0
                    if now - last_console_warning_time > 2.0:
                        log_session_error("Slow down! Curling too fast")
                        last_console_warning_time = now

                draw_skeleton(image, results)

                draw_glass_panel(image, 5, 5, 280, 120, alpha=0.65, radius=10)
                cv2.putText(image, "BICEP CURLS", (15, 28),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_DIM, 1)
                cv2.putText(image, f"L: {arms['left']['counter']}", (15, 58),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, l_clr, 2)
                cv2.putText(image, l_fb, (120, 58),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, l_clr, 1)
                cv2.putText(image, f"R: {arms['right']['counter']}", (15, 90),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, r_clr, 2)
                cv2.putText(image, r_fb, (120, 90),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, r_clr, 1)
                cv2.putText(image, f"{l_stg} | {r_stg}", (15, 115),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, TEXT_DIM, 1)
            else:
                if positioning_completed and not paused:
                    robot_status = adjust_robot_position("BICEP_CURLS", None)
                else:
                    send_command("STOP")

            # Show slow down warning banner on screen
            if time.time() < warning_active_until:
                flash = int(time.time() * 5) % 2 == 0
                bg_color = (20, 20, 100) if flash else (10, 10, 40)
                border_color = (0, 0, 255) if flash else (50, 50, 200)

                p_w = int(img_w * 0.7)
                p_h = 65
                p_x = (img_w - p_w) // 2
                p_y = int(img_h * 0.4)

                draw_glass_panel(image, p_x, p_y, p_w, p_h, alpha=0.85, color=bg_color, radius=12)
                cv2.rectangle(image, (p_x, p_y), (p_x + p_w, p_y + p_h), border_color, 2)

                warn_msg = "WARNING: SLOW DOWN!"
                font = cv2.FONT_HERSHEY_SIMPLEX
                font_scale = 0.7
                thickness = 2
                (text_w, text_h), _ = cv2.getTextSize(warn_msg, font, font_scale, thickness)
                tx = p_x + (p_w - text_w) // 2
                ty = p_y + (p_h + text_h) // 2

                cv2.putText(image, warn_msg, (tx, ty), font, font_scale,
                            (255, 255, 255) if flash else (200, 200, 255), thickness, cv2.LINE_AA)

            # Show swing warning banner on screen
            if time.time() < swing_warning_active_until:
                bg_color = (15, 45, 95)
                border_color = (80, 175, 225)

                p_w = int(img_w * 0.8)
                p_h = 55
                p_x = (img_w - p_w) // 2
                p_y = int(img_h * 0.75)

                draw_glass_panel(image, p_x, p_y, p_w, p_h, alpha=0.85, color=bg_color, radius=10)
                cv2.rectangle(image, (p_x, p_y), (p_x + p_w, p_y + p_h), border_color, 2)

                warn_msg = "DONT SWING: KEEP HANDS STRAIGHT"
                font = cv2.FONT_HERSHEY_SIMPLEX
                font_scale = 0.55
                thickness = 2
                (text_w, text_h), _ = cv2.getTextSize(warn_msg, font, font_scale, thickness)
                tx = p_x + (p_w - text_w) // 2
                ty = p_y + (p_h + text_h) // 2

                cv2.putText(image, warn_msg, (tx, ty), font, font_scale,
                            (255, 255, 255), thickness, cv2.LINE_AA)

            # Draw set stats panel (Set X, Reps Y)
            draw_glass_panel(image, 5, 130, 280, 110, alpha=0.65, radius=10)
            cv2.putText(image, "CURRENT SET STATS", (15, 150),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_DIM, 1)
            cv2.putText(image, f"SET: {current_set}", (15, 185),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, TEXT_WHITE, 2)
            cv2.putText(image, f"REPS: {current_set_reps}", (140, 185),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, TEXT_WHITE, 2)

            # Show individual arm counts in the current set
            cv2.putText(image, f"Set L: {current_set_left_reps}  R: {current_set_right_reps}", (15, 210),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, TEXT_DIM, 1)

            if set_finished:
                cv2.putText(image, "SET FINISHED (REST)", (15, 230),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, ACCENT_GREEN, 1)
            else:
                status_parts = []
                status_color = TEXT_DIM
                if is_uneven:
                    status_parts.append("EQUALIZE BOTH HANDS!")
                    status_color = ACCENT_ORANGE
                if current_set_reps >= 8 and last_rep_time is not None:
                    time_left = max(0.0, 10.0 - (time.time() - last_rep_time))
                    status_parts.append(f"Rest in: {time_left:.1f}s")
                    status_color = ACCENT_ORANGE

                if status_parts:
                    status_text = " | ".join(status_parts)
                    cv2.putText(image, status_text, (15, 230),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.4, status_color, 1, cv2.LINE_AA)

            # Update stats & feedback on the Tkinter UI
            if ui is not None:
                ui.update_stat_card("Set", current_set)
                ui.update_stat_card("Reps", current_set_reps)
                ui.update_stat_card("L Reps", arms['left']['counter'])
                ui.update_stat_card("R Reps", arms['right']['counter'])

                feedback_msgs = []
                feedback_color = ACCENT_GREEN

                if time.time() < warning_active_until:
                    feedback_msgs.append("⚠️ WARNING: SLOW DOWN! Curling too fast.")
                    feedback_color = ACCENT_RED
                if time.time() < swing_warning_active_until:
                    feedback_msgs.append("⚠️ WARNING: DON'T SWING! Keep hands straight.")
                    feedback_color = ACCENT_ORANGE
                if is_uneven and not set_finished:
                    feedback_msgs.append(f"⚠️ Equalize both hands (Left: {current_set_left_reps} | Right: {current_set_right_reps})")
                    feedback_color = ACCENT_ORANGE

                if results.pose_landmarks and len(results.pose_landmarks) > 0:
                    if l_fb != "GOOD" and l_fb != "---":
                        feedback_msgs.append(f"⚠️ Left Arm: {l_fb}")
                        feedback_color = ACCENT_ORANGE
                    if r_fb != "GOOD" and r_fb != "---":
                        feedback_msgs.append(f"⚠️ Right Arm: {r_fb}")
                        feedback_color = ACCENT_ORANGE
                else:
                    feedback_msgs.append("⚠️ Positioning... Stand in front of camera.")
                    feedback_color = ACCENT_ORANGE

                if not feedback_msgs:
                    if set_finished:
                        feedback_msgs.append("✅ Set finished! Please rest.")
                        feedback_color = ACCENT_GREEN
                    else:
                        feedback_msgs.append("✅ Form is Good. Keep it up!")
                        feedback_color = ACCENT_GREEN

                if robot_status == "SEARCHING":
                    ui.update_feedback("Searching...", ACCENT_ORANGE)
                else:
                    ui.update_feedback("\n".join(feedback_msgs), feedback_color)
                ui.update()

            if robot_status == "SEARCHING":
                cv2.putText(image, "Searching...", (image.shape[1]//2 - 120, image.shape[0]//2),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.2, ACCENT_ORANGE, 3, cv2.LINE_AA)

            if hand_landmarker is not None:
                draw_hand_controls_status(image, thumbs_up_frames, palm_stop_frames, is_paused=False)
            draw_home_button(image)
            local_stream_server.update_frame(image)

            if ui is None:
                if hand_landmarker is not None:
                    if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                        show_palm_stop_message('Bicep Curls', image)
                        break
                cv2.imshow('Bicep Curls', image)
                if cv2.waitKey(10) & 0xFF == ord('q') or home_clicked:
                    break
            else:
                if hand_landmarker is not None:
                    if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                        break
                if home_clicked:
                    break
    finally:
        send_command("STOP")
        if 'landmarker' in locals() and landmarker is not None:
            landmarker.close()
        if 'hand_landmarker' in locals() and hand_landmarker is not None:
            hand_landmarker.close()
        
        # Populate session summary
        global last_session_summary
        last_session_summary = {
            'exercise': 'Bicep Curls',
            'metrics': [
                ('Left Reps', arms['left']['counter']),
                ('Right Reps', arms['right']['counter']),
                ('Total Sets', current_set)
            ],
            'errors': list(session_errors)
        }
        print_session_error_summary()
        save_session_to_firestore(last_session_summary)

    cap.release()
    if ui is None:
        cv2.destroyAllWindows()
    print(f"\n✅ Bicep Curls Done! Left: {arms['left']['counter']} | Right: {arms['right']['counter']}")


def run_pushups(ui=None):
    """Push-up counter using elbow angles and body straightness check."""
    clear_session_errors()

    def count_pushups(shoulder, elbow, wrist, neck, hip, ankle, stage, counter):
        elbow_angle = calculate_angle(shoulder, elbow, wrist)
        hip_angle = calculate_angle(neck, hip, ankle)
        is_body_straight = hip_angle >= 165.0

        if is_body_straight:
            if elbow_angle < 110 and stage == 'up':
                stage = 'down'
            elif elbow_angle > 150 and stage == 'down':
                stage = 'up'
                counter += 1
                print(f"  Push-ups Count: {counter}")
                # Publish live rep (real data)
                robot_status = "TRACKING"
                try:
                    publish_rep("pushups", set_num=current_set, count=counter)
                except Exception:
                    pass

        return stage, counter, is_body_straight, elbow_angle

    pushup_counter = 0
    pushup_stage = 'up'
    current_set = 1
    reps_in_set = 0
    set_ended = False

    cap = cv2.VideoCapture(CAMERA_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    if ui is None:
        cv2.namedWindow('Push-ups', cv2.WINDOW_NORMAL)
        cv2.resizeWindow('Push-ups', DISPLAY_WIDTH, DISPLAY_HEIGHT)
    global home_clicked
    home_clicked = False
    if ui is None:
        cv2.setMouseCallback('Push-ups', on_mouse_click)
    print("💪 Push-up Counter Active! Press Q to quit.")

    publish_robot_state("LOADING")
    landmarker = create_pose_landmarker(det_conf=0.5, track_conf=0.5)
    hand_landmarker = create_hand_landmarker()
    timestamp_ms = 0
    hand_timestamp_ms = 0
    palm_stop_frames = 0
    frame_counter = 0
    last_results = None
    last_hand_results = None

    paused = False
    thumbs_up_frames = 0
    thumbs_up_cooldown_active = False
    pause_start_time = 0.0

    # Positioning state
    positioning_duration = 3.0
    positioning_start_time = time.time()
    positioning_completed = False
    send_command("SIDE")

    robot_status = "TRACKING"
    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            # Remote Force Stop from web dashboard
            if force_stop.is_set():
                print("  [REMOTE] Force Stop received from web UI")
                send_command("STOP")
                break

            frame_counter += 1
            frame = cv2.flip(frame, 1)
            image = cv2.resize(frame, (DISPLAY_WIDTH, DISPLAY_HEIGHT))
            # Run Pose detection every FRAME_SKIP frames
            if frame_counter % FRAME_SKIP == 0 or last_results is None:
                results, timestamp_ms = detect_pose_frame(landmarker, frame, timestamp_ms)
                last_results = results
            else:
                results = last_results

            # Check positioning phase
            if not positioning_completed:
                elapsed = time.time() - positioning_start_time
                if elapsed < positioning_duration:
                    pct = elapsed / positioning_duration
                    if results and results.pose_landmarks:
                        draw_skeleton(image, results)
                    draw_positioning_overlay(image, "SIDE", pct)
                    draw_home_button(image)
                    local_stream_server.update_frame(image)

                    if ui is not None:
                        ui.update_feedback(f"🤖 ROBOT POSITIONING...\nAligning to SIDE view... ({int(pct*100)}%)", ACCENT_CYAN)
                        ui.update()
                    else:
                        send_command("SIDE")
                        cv2.imshow('Push-ups', image)
                        if cv2.waitKey(10) & 0xFF == ord('q') or home_clicked:
                            break
                    send_command("SIDE")
                    continue
                else:
                    send_command("STOP")
                    positioning_completed = True
                    # Start real session for live dashboard (no dummy data)
                    if _session_id is None:
                        robot_status = "TRACKING"
                        try:
                            publish_session_start("pushups")
                            publish_robot_state("ACTIVE")
                        except Exception:
                            pass

            # Run Hand detection every HAND_FRAME_SKIP frames
            if hand_landmarker is not None:
                if frame_counter % HAND_FRAME_SKIP == 0 or last_hand_results is None:
                    hand_results, hand_timestamp_ms = detect_hand_frame(
                        hand_landmarker, frame, hand_timestamp_ms
                    )
                    last_hand_results = hand_results

                    # Thumbs-up check
                    if is_thumbs_up(hand_results):
                        if not thumbs_up_cooldown_active:
                            thumbs_up_frames += HAND_FRAME_SKIP
                            if thumbs_up_frames >= PALM_STOP_REQUIRED_FRAMES:
                                paused = not paused
                                thumbs_up_frames = 0
                                thumbs_up_cooldown_active = True
                                if paused:
                                    pause_start_time = time.time()
                                    robot_status = "TRACKING"
                                    try:
                                        publish_pause()
                                    except Exception:
                                        pass
                                else:
                                    if pause_start_time:
                                        robot_status = "TRACKING"
                                        try:
                                            _pause_accumulator += (time.time() - pause_start_time)
                                        except Exception:
                                            pass
                                    robot_status = "TRACKING"
                                    try:
                                        publish_resume()
                                    except Exception:
                                        pass
                                print(f"⏸️ Push-ups: {'PAUSED' if paused else 'RESUMED'}")
                    else:
                        thumbs_up_frames = 0
                        thumbs_up_cooldown_active = False

                    palm_stop_frames = update_palm_stop_frames(hand_results, palm_stop_frames, increment=HAND_FRAME_SKIP)
                else:
                    hand_results = last_hand_results
            else:
                palm_stop_frames = 0
                thumbs_up_frames = 0

            # If paused, render overlay and bypass main logic
            if paused:
                send_command("STOP")
                if results and results.pose_landmarks:
                    draw_skeleton(image, results)
                draw_paused_overlay(image, thumbs_up_frames)
                if hand_landmarker is not None:
                    draw_hand_controls_status(image, thumbs_up_frames, palm_stop_frames, is_paused=True)
                draw_home_button(image)
                local_stream_server.update_frame(image)

                if ui is not None:
                    ui.update_feedback("⏸️ EXERCISE PAUSED\nShow Thumbs-Up to Resume", ACCENT_ORANGE)
                    ui.update()
                    if hand_landmarker is not None:
                        if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                            break
                else:
                    if hand_landmarker is not None:
                        if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                            show_palm_stop_message('Push-ups', image)
                            break
                    cv2.imshow('Push-ups', image)
                    if cv2.waitKey(10) & 0xFF == ord('q') or home_clicked:
                        break
                continue

            is_horizontal = False
            is_body_straight = True
            elbow_angle = 180.0

            robot_status = "TRACKING"
            try:
                if results.pose_landmarks and len(results.pose_landmarks) > 0:
                    if positioning_completed and not paused:
                        robot_status = adjust_robot_position("PUSH_UPS", results)
                        if robot_status == "LOST_TIMEOUT":
                            break
                    lm = results.pose_landmarks[0]
                    left_shoulder = get_pt(lm, 11)
                    left_hip = get_pt(lm, 23)
                    left_ankle = get_pt(lm, 27)
                    left_elbow = get_pt(lm, 13)
                    left_wrist = get_pt(lm, 15)

                    right_shoulder = get_pt(lm, 12)
                    right_hip = get_pt(lm, 24)
                    right_ankle = get_pt(lm, 28)
                    right_elbow = get_pt(lm, 14)
                    right_wrist = get_pt(lm, 16)

                    left_ok = left_shoulder and left_hip and left_ankle and left_elbow and left_wrist
                    right_ok = right_shoulder and right_hip and right_ankle and right_elbow and right_wrist

                    shoulder, hip, ankle, elbow, wrist = None, None, None, None, None
                    if left_ok and right_ok:
                        left_vis = (lm[11].visibility + lm[23].visibility + lm[27].visibility + lm[13].visibility + lm[15].visibility) / 5.0
                        right_vis = (lm[12].visibility + lm[24].visibility + lm[28].visibility + lm[14].visibility + lm[16].visibility) / 5.0
                        if left_vis > right_vis:
                            shoulder, hip, ankle, elbow, wrist = left_shoulder, left_hip, left_ankle, left_elbow, left_wrist
                        else:
                            shoulder, hip, ankle, elbow, wrist = right_shoulder, right_hip, right_ankle, right_elbow, right_wrist
                    elif left_ok:
                        shoulder, hip, ankle, elbow, wrist = left_shoulder, left_hip, left_ankle, left_elbow, left_wrist
                    elif right_ok:
                        shoulder, hip, ankle, elbow, wrist = right_shoulder, right_hip, right_ankle, right_elbow, right_wrist

                    if shoulder and hip and ankle and elbow and wrist:
                        if left_shoulder and right_shoulder:
                            neck = [(left_shoulder[0] + right_shoulder[0]) / 2, (left_shoulder[1] + right_shoulder[1]) / 2]
                        else:
                            neck = shoulder

                        is_horizontal = abs(shoulder[0] - ankle[0]) > abs(shoulder[1] - ankle[1])

                        if is_horizontal:
                            prev_counter = pushup_counter
                            pushup_stage, pushup_counter, is_body_straight, elbow_angle = count_pushups(
                                shoulder, elbow, wrist, neck, hip, ankle, pushup_stage, pushup_counter
                            )

                            if pushup_counter > prev_counter:
                                if set_ended:
                                    current_set += 1
                                    reps_in_set = 1
                                    set_ended = False
                                else:
                                    reps_in_set += 1

                            if not is_body_straight:
                                log_session_error("Form a straight line with body")
                        else:
                            if reps_in_set > 0 and not set_ended:
                                set_ended = True
                                print(f"\n✅ Push-up Set {current_set} Finished! Completed {reps_in_set} reps.")
                                robot_status = "TRACKING"
                                try:
                                    publish_set_complete("pushups", current_set, reps_in_set)
                                except Exception:
                                    pass
                else:
                    if positioning_completed and not paused:
                        robot_status = adjust_robot_position("PUSH_UPS", None)
                    else:
                        send_command("STOP")
            except Exception:
                pass

            draw_skeleton(image, results)

            draw_glass_panel(image, 5, 5, 280, 115, alpha=0.65, radius=10)
            cv2.rectangle(image, (5, 5), (9, 120), ACCENT_GREEN if (is_horizontal and is_body_straight) else ACCENT_RED, -1)
            cv2.putText(image, "PUSH-UPS", (18, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_DIM, 1, cv2.LINE_AA)

            if is_horizontal:
                stage_clr = ACCENT_GREEN if pushup_stage == 'up' else ACCENT_CYAN
                cv2.putText(image, f"SET: {current_set}", (18, 55),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, TEXT_WHITE, 2, cv2.LINE_AA)
                cv2.putText(image, f"REPS: {reps_in_set}", (140, 55),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, TEXT_WHITE, 2, cv2.LINE_AA)
                cv2.putText(image, pushup_stage.upper(), (18, 88),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, stage_clr, 2, cv2.LINE_AA)
                cv2.putText(image, f"Total: {pushup_counter} | Elbow: {int(elbow_angle)} deg", (18, 108),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, TEXT_DIM, 1, cv2.LINE_AA)
            else:
                cv2.putText(image, f"SET: {current_set}", (18, 55),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, TEXT_WHITE, 2, cv2.LINE_AA)
                cv2.putText(image, f"REPS: {reps_in_set}", (140, 55),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, TEXT_WHITE, 2, cv2.LINE_AA)
                cv2.putText(image, "GET IN POSITION", (18, 88),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.65, ACCENT_ORANGE, 2, cv2.LINE_AA)
                cv2.putText(image, "Align horizontally", (18, 108),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, TEXT_DIM, 1, cv2.LINE_AA)

            if is_horizontal and not is_body_straight:
                warn_msg = "Form a straight line with body!"
                img_h, img_w = image.shape[:2]
                draw_glass_panel(image, 5, img_h - 55, img_w - 10, 50, alpha=0.8, color=(20, 20, 100), radius=10)
                cv2.putText(image, warn_msg, (15, img_h - 25),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, ACCENT_RED, 2, cv2.LINE_AA)

            # Update stats & feedback on the Tkinter UI
            if ui is not None:
                ui.update_stat_card("Set", current_set)
                ui.update_stat_card("Reps", reps_in_set)
                ui.update_stat_card("Total Reps", pushup_counter)

                feedback_msgs = []
                feedback_color = ACCENT_GREEN

                if results.pose_landmarks and len(results.pose_landmarks) > 0:
                    if is_horizontal:
                        if not is_body_straight:
                            feedback_msgs.append("⚠️ Form a straight line with body!")
                            feedback_color = ACCENT_RED
                    else:
                        feedback_msgs.append("⚠️ Align horizontally! Lie down in profile view.")
                        feedback_color = ACCENT_ORANGE
                else:
                    feedback_msgs.append("⚠️ Positioning... Lie down in front of camera.")
                    feedback_color = ACCENT_ORANGE

                if not feedback_msgs:
                    feedback_msgs.append("✅ Form is Good. Keep it up!")

                if robot_status == "SEARCHING":
                    ui.update_feedback("Searching...", ACCENT_ORANGE)
                else:
                    ui.update_feedback("\n".join(feedback_msgs), feedback_color)
                ui.update()

            if robot_status == "SEARCHING":
                cv2.putText(image, "Searching...", (image.shape[1]//2 - 120, image.shape[0]//2),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.2, ACCENT_ORANGE, 3, cv2.LINE_AA)

            if hand_landmarker is not None:
                draw_hand_controls_status(image, thumbs_up_frames, palm_stop_frames, is_paused=False)
            draw_home_button(image)
            local_stream_server.update_frame(image)

            if ui is None:
                if hand_landmarker is not None:
                    if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                        show_palm_stop_message('Push-ups', image)
                        break
                cv2.imshow('Push-ups', image)
                if cv2.waitKey(10) & 0xFF == ord('q') or home_clicked:
                    break
            else:
                if hand_landmarker is not None:
                    if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                        break
                if home_clicked:
                    break
    finally:
        send_command("STOP")
        if 'landmarker' in locals() and landmarker is not None:
            landmarker.close()
        if 'hand_landmarker' in locals() and hand_landmarker is not None:
            hand_landmarker.close()
        
        # Populate session summary
        global last_session_summary
        last_session_summary = {
            'exercise': 'Push-ups',
            'metrics': [
                ('Total Reps', pushup_counter),
                ('Total Sets', current_set)
            ],
            'errors': list(session_errors)
        }

    cap.release()
    if ui is None:
        cv2.destroyAllWindows()
    print(f"\n✅ Push-ups Done! Total: {pushup_counter}")


def run_squats(ui=None):
    """Squat counter using 3D hip angle."""
    clear_session_errors()
    counter = 0
    stage = "up"
    SQUAT_DOWN_ANGLE = 110
    SQUAT_UP_ANGLE = 160
    down_angle = SQUAT_DOWN_ANGLE

    # Exercise sets and reps state
    current_set = 1
    reps_in_set = 0
    last_rep_time = None
    set_started = False

    # Arm check grace period timer
    arms_incorrect_start_time = None

    # Standing reference coordinates for whole-body displacement verification
    up_shoulder_y = None
    up_hip_y = None
    up_wrist_y = None
    up_knee_y = None

    # Flags to track if the joints lowered during descent phase
    lowered_shoulders = False
    lowered_hips = False
    lowered_wrists = False
    lowered_knees = False

    cap = cv2.VideoCapture(CAMERA_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    if ui is None:
        cv2.namedWindow('Squats', cv2.WINDOW_NORMAL)
        cv2.resizeWindow('Squats', DISPLAY_WIDTH, DISPLAY_HEIGHT)
    global home_clicked
    home_clicked = False
    if ui is None:
        cv2.setMouseCallback('Squats', on_mouse_click)

    print("🦵 Squat Counter Active! Press Q to quit.")

    publish_robot_state("LOADING")
    landmarker = create_pose_landmarker(det_conf=0.5, track_conf=0.5)
    hand_landmarker = create_hand_landmarker()
    timestamp_ms = 0
    hand_timestamp_ms = 0
    palm_stop_frames = 0
    frame_counter = 0
    last_results = None
    last_hand_results = None

    paused = False
    thumbs_up_frames = 0
    thumbs_up_cooldown_active = False
    pause_start_time = 0.0

    # Positioning state
    positioning_duration = 3.0
    positioning_start_time = time.time()
    positioning_completed = False
    send_command("FRONT")

    robot_status = "TRACKING"
    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            # Remote Force Stop from web dashboard
            if force_stop.is_set():
                print("  [REMOTE] Force Stop received from web UI")
                send_command("STOP")
                break

            frame_counter += 1
            image = cv2.resize(frame, (DISPLAY_WIDTH, DISPLAY_HEIGHT))
            # Run Pose detection every FRAME_SKIP frames
            if frame_counter % FRAME_SKIP == 0 or last_results is None:
                results, timestamp_ms = detect_pose_frame(landmarker, frame, timestamp_ms)
                last_results = results
            else:
                results = last_results

            # Check positioning phase
            if not positioning_completed:
                elapsed = time.time() - positioning_start_time
                if elapsed < positioning_duration:
                    pct = elapsed / positioning_duration
                    if results and results.pose_landmarks:
                        draw_skeleton(image, results)
                    draw_positioning_overlay(image, "FRONT", pct)
                    draw_home_button(image)
                    local_stream_server.update_frame(image)

                    if ui is not None:
                        ui.update_feedback(f"🤖 ROBOT POSITIONING...\nAligning to FRONT view... ({int(pct*100)}%)", ACCENT_CYAN)
                        ui.update()
                    else:
                        send_command("FRONT")
                        cv2.imshow('Squats', image)
                        if cv2.waitKey(5) & 0xFF == ord('q') or home_clicked:
                            break
                    send_command("FRONT")
                    continue
                else:
                    send_command("STOP")
                    positioning_completed = True
                    # Start real session for live dashboard (no dummy data)
                    if _session_id is None:
                        robot_status = "TRACKING"
                        try:
                            publish_session_start("squats")
                            publish_robot_state("ACTIVE")
                        except Exception:
                            pass

            # Run Hand detection every HAND_FRAME_SKIP frames
            if hand_landmarker is not None:
                if frame_counter % HAND_FRAME_SKIP == 0 or last_hand_results is None:
                    hand_results, hand_timestamp_ms = detect_hand_frame(
                        hand_landmarker, frame, hand_timestamp_ms
                    )
                    last_hand_results = hand_results

                    # Thumbs-up check
                    if is_thumbs_up(hand_results):
                        if not thumbs_up_cooldown_active:
                            thumbs_up_frames += HAND_FRAME_SKIP
                            if thumbs_up_frames >= PALM_STOP_REQUIRED_FRAMES:
                                paused = not paused
                                thumbs_up_frames = 0
                                thumbs_up_cooldown_active = True
                                if paused:
                                    pause_start_time = time.time()
                                    robot_status = "TRACKING"
                                    try:
                                        publish_pause()
                                        # accumulate from previous resume if any
                                        # (we will add on resume)
                                    except Exception:
                                        pass
                                else:
                                    pause_duration = time.time() - pause_start_time
                                    robot_status = "TRACKING"
                                    try:
                                        _pause_accumulator += pause_duration
                                        publish_resume()
                                    except Exception:
                                        pass
                                    # Adjust timestamps
                                    if last_rep_time is not None:
                                        last_rep_time += pause_duration
                                    if arms_incorrect_start_time is not None:
                                        arms_incorrect_start_time += pause_duration
                                print(f"⏸️ Squats: {'PAUSED' if paused else 'RESUMED'}")
                    else:
                        thumbs_up_frames = 0
                        thumbs_up_cooldown_active = False

                    palm_stop_frames = update_palm_stop_frames(hand_results, palm_stop_frames, increment=HAND_FRAME_SKIP)
                else:
                    hand_results = last_hand_results
            else:
                palm_stop_frames = 0
                thumbs_up_frames = 0

            # If paused, render overlay and bypass main logic
            if paused:
                send_command("STOP")
                if results and results.pose_landmarks:
                    draw_skeleton(image, results)
                draw_paused_overlay(image, thumbs_up_frames)
                if hand_landmarker is not None:
                    draw_hand_controls_status(image, thumbs_up_frames, palm_stop_frames, is_paused=True)
                draw_home_button(image)
                local_stream_server.update_frame(image)

                if ui is not None:
                    ui.update_feedback("⏸️ EXERCISE PAUSED\nShow Thumbs-Up to Resume", ACCENT_ORANGE)
                    ui.update()
                    if hand_landmarker is not None:
                        if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                            break
                else:
                    if hand_landmarker is not None:
                        if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                            show_palm_stop_message('Squats', image)
                            break
                    cv2.imshow('Squats', image)
                    if cv2.waitKey(5) & 0xFF == ord('q') or home_clicked:
                        break
                continue

            current_hip_angle = 180
            posture_correct = True

            # Posture warnings
            feet_warning = None
            arms_warning = None
            depth_warning = None
            knees_warning = None

            robot_status = "TRACKING"
            try:
                if results.pose_landmarks and len(results.pose_landmarks) > 0:
                    if positioning_completed and not paused:
                        robot_status = adjust_robot_position("SQUATS", results)
                        if robot_status == "LOST_TIMEOUT":
                            break
                    if positioning_completed and not paused:
                        robot_status = adjust_robot_position("SQUATS", results)
                        if robot_status == "LOST_TIMEOUT":
                            break
                    lm = results.pose_landmarks[0]

                    L_shoulder = get_pt(lm, 11, use_z=True)
                    R_shoulder = get_pt(lm, 12, use_z=True)
                    L_elbow = get_pt(lm, 13, use_z=True)
                    R_elbow = get_pt(lm, 14, use_z=True)
                    L_wrist = get_pt(lm, 15, use_z=True)
                    R_wrist = get_pt(lm, 16, use_z=True)
                    L_hip = get_pt(lm, 23, use_z=True)
                    R_hip = get_pt(lm, 24, use_z=True)
                    L_knee = get_pt(lm, 25, use_z=True)
                    R_knee = get_pt(lm, 26, use_z=True)
                    L_ankle = get_pt(lm, 27, use_z=True)
                    R_ankle = get_pt(lm, 28, use_z=True)

                    # --- Form & Posture Checks ---

                    # 1. Feet at shoulder width check (Starting Position)
                    if all([L_shoulder, R_shoulder, L_ankle, R_ankle]):
                        shoulder_dist = abs(L_shoulder[0] - R_shoulder[0])
                        feet_dist = abs(L_ankle[0] - R_ankle[0])
                        if stage == "up":
                            if feet_dist < 0.9 * shoulder_dist or feet_dist > 1.4 * shoulder_dist:
                                feet_warning = "Keep feet at shoulder distance"
                                log_session_error("Keep feet at shoulder distance")

                    # 2. Arm extension check (10-second grace period)
                    arms_correct = True
                    if all([L_shoulder, R_shoulder, L_elbow, R_elbow, L_wrist, R_wrist]):
                        L_elbow_angle = calculate_angle_3d(L_shoulder, L_elbow, L_wrist)
                        R_elbow_angle = calculate_angle_3d(R_shoulder, R_elbow, R_wrist)

                        shoulder_dist = abs(L_shoulder[0] - R_shoulder[0])
                        wrist_dist = abs(L_wrist[0] - R_wrist[0])

                        elbows_straight = (L_elbow_angle > 140.0) and (R_elbow_angle > 140.0)
                        horizontal_level = (abs(L_wrist[1] - L_shoulder[1]) < 0.35 * shoulder_dist) and \
                                           (abs(R_wrist[1] - R_shoulder[1]) < 0.35 * shoulder_dist)
                        not_to_sides = (abs(L_wrist[0] - L_shoulder[0]) < 0.35 * shoulder_dist) and \
                                       (abs(R_wrist[0] - R_shoulder[0]) < 0.35 * shoulder_dist)
                        not_together = (wrist_dist >= 0.7 * shoulder_dist)

                        if not (elbows_straight and horizontal_level and not_to_sides and not_together):
                            arms_correct = False
                    else:
                        arms_correct = False

                    if not arms_correct:
                        if arms_incorrect_start_time is None:
                            arms_incorrect_start_time = time.time()
                        elif time.time() - arms_incorrect_start_time > 10.0:
                            arms_warning = "Extend arms straight and directly in front of you"
                            log_session_error("Extend arms straight and directly in front of you")
                    else:
                        arms_incorrect_start_time = None

                    # Check hip angle (for squat reps and stage classification)
                    if all([L_shoulder, L_hip, L_knee, R_shoulder, R_hip, R_knee]):
                        L_hip_angle = calculate_angle_3d(L_shoulder, L_hip, L_knee)
                        R_hip_angle = calculate_angle_3d(R_shoulder, R_hip, R_knee)

                        current_hip_angle = min(L_hip_angle, R_hip_angle)

                        # Standing references updated while standing upright
                        if current_hip_angle > SQUAT_UP_ANGLE:
                            up_shoulder_y = (L_shoulder[1] + R_shoulder[1]) / 2.0
                            up_hip_y = (L_hip[1] + R_hip[1]) / 2.0
                            up_wrist_y = (L_wrist[1] + R_wrist[1]) / 2.0
                            up_knee_y = (L_knee[1] + R_knee[1]) / 2.0

                        if current_hip_angle < SQUAT_DOWN_ANGLE:
                            stage = "down"
                            if down_angle > current_hip_angle:
                                down_angle = current_hip_angle

                            # Start set/countdown on first rep movement
                            if reps_in_set == 0 and not set_started:
                                set_started = True
                                last_rep_time = time.time()

                        # Track joint lowering during down phase
                        if stage == "down" and up_shoulder_y is not None:
                            curr_shoulder_y = (L_shoulder[1] + R_shoulder[1]) / 2.0
                            curr_hip_y = (L_hip[1] + R_hip[1]) / 2.0
                            curr_wrist_y = (L_wrist[1] + R_wrist[1]) / 2.0
                            curr_knee_y = (L_knee[1] + R_knee[1]) / 2.0

                            shoulder_dist = abs(L_shoulder[0] - R_shoulder[0])

                            if curr_shoulder_y > up_shoulder_y + 0.05 * shoulder_dist:
                                lowered_shoulders = True
                            if curr_hip_y > up_hip_y + 0.05 * shoulder_dist:
                                lowered_hips = True
                            if curr_wrist_y > up_wrist_y + 0.05 * shoulder_dist:
                                lowered_wrists = True
                            if curr_knee_y > up_knee_y + 0.02 * shoulder_dist:
                                lowered_knees = True

                        if current_hip_angle > SQUAT_UP_ANGLE and stage == 'down':
                            stage = "up"
                            # Enforce that hands, body (shoulders), knees, and hips all lowered during descent
                            if lowered_shoulders and lowered_hips and lowered_wrists and lowered_knees:
                                reps_in_set += 1
                                counter += 1
                                print(f"  Rep {reps_in_set} completed! Angle: {int(down_angle)}")
                                last_rep_time = time.time()
                                set_started = True
                                # Publish live squat rep (real data)
                                robot_status = "TRACKING"
                                try:
                                    publish_rep("squats", set_num=current_set, count=reps_in_set)
                                except Exception:
                                    pass
                            else:
                                print(f"  Rep ignored: Whole body did not lower. Shoulders: {lowered_shoulders}, Hips: {lowered_hips}, Wrists: {lowered_wrists}, Knees: {lowered_knees}")

                            # Reset descent tracking flags for the next rep
                            lowered_shoulders = False
                            lowered_hips = False
                            lowered_wrists = False
                            lowered_knees = False
                            down_angle = SQUAT_DOWN_ANGLE

                    # 3. Squat depth check (Down stage, angle knee-hip & knee-ankle < 80 deg)
                    if all([L_hip, L_knee, L_ankle, R_hip, R_knee, R_ankle]):
                        L_knee_angle = calculate_angle_3d(L_hip, L_knee, L_ankle)
                        R_knee_angle = calculate_angle_3d(R_hip, R_knee, R_ankle)

                        if stage == "down":
                            if L_knee_angle < 80.0 or R_knee_angle < 80.0:
                                depth_warning = "Don't go too deep in squats"
                                log_session_error("Don't go too deep in squats")

                    # 4. Knee direction check (Down stage, knees pointed sideways by > 5 degrees)
                    if all([L_hip, L_knee, L_ankle, R_hip, R_knee, R_ankle]):
                        L_dev = calculate_lateral_deviation(L_hip, L_knee, L_ankle)
                        R_dev = calculate_lateral_deviation(R_hip, R_knee, R_ankle)

                        if stage == "down" and (L_dev > 5.0 or R_dev > 5.0):
                            knees_warning = "Stand at shoulder width and keep knees in front of body"
                            log_session_error("Stand at shoulder width and keep knees in front of body")

                    # Posture is correct if no warnings are active
                    posture_correct = (feet_warning is None) and (arms_warning is None) and \
                                      (depth_warning is None) and (knees_warning is None)

                    if ui is None:
                        draw_skeleton(image, results)
                else:
                    if positioning_completed and not paused:
                        robot_status = adjust_robot_position("SQUATS", None)
                    else:
                        send_command("STOP")
            except Exception:
                pass

            # --- Set transition / Countdown timer logic ---
            now_time = time.time()
            set_finished = False
            if set_started and last_rep_time is not None:
                time_elapsed = now_time - last_rep_time
                time_limit = 15.0 if reps_in_set >= 6 else 20.0

                if time_elapsed > time_limit:
                    print(f"\n✅ Set {current_set} Finished! Completed {reps_in_set} reps.")
                    robot_status = "TRACKING"
                    try:
                        publish_set_complete("squats", current_set, reps_in_set)
                    except Exception:
                        pass
                    current_set += 1
                    reps_in_set = 0
                    set_started = False
                    last_rep_time = None
                    set_finished = True

            img_h, img_w = image.shape[:2]

            # Main stats panel
            accent = ACCENT_GREEN if posture_correct else ACCENT_RED
            if ui is None:
                draw_glass_panel(image, 5, 5, 280, 115, alpha=0.65, radius=10)
                cv2.rectangle(image, (5, 5), (9, 120), accent, -1)
                cv2.putText(image, "SQUATS", (18, 24),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_DIM, 1, cv2.LINE_AA)
                cv2.putText(image, f"SET: {current_set}", (18, 55),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, TEXT_WHITE, 2, cv2.LINE_AA)
                cv2.putText(image, f"REPS: {reps_in_set}", (140, 55),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, TEXT_WHITE, 2, cv2.LINE_AA)
                cv2.putText(image, stage.upper(), (18, 88),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, accent, 2, cv2.LINE_AA)

                if set_started and last_rep_time is not None:
                    time_limit = 15.0 if reps_in_set >= 6 else 20.0
                    time_elapsed = now_time - last_rep_time
                    countdown = max(0.0, time_limit - time_elapsed)
                    cv2.putText(image, f"Rest in: {countdown:.1f}s", (140, 88),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, ACCENT_ORANGE, 1, cv2.LINE_AA)

                # Arc gauge for hip angle
                gauge_cx, gauge_cy = img_w - 60, 60
                gauge_r = 40
                draw_glass_panel(image, gauge_cx - gauge_r - 8, 5,
                                 gauge_r * 2 + 16, gauge_r * 2 + 25,
                                 alpha=0.6, radius=10)
                cv2.ellipse(image, (gauge_cx, gauge_cy), (gauge_r, gauge_r),
                            0, 180, 360, (40, 40, 40), 3)
                angle_pct = np.clip((current_hip_angle - 60) / 120.0, 0, 1)
                sweep = int(angle_pct * 180)
                cv2.ellipse(image, (gauge_cx, gauge_cy), (gauge_r, gauge_r),
                            0, 360 - sweep, 360, accent, 3)
                cv2.putText(image, f"{int(current_hip_angle)}",
                            (gauge_cx - 15, gauge_cy + 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, TEXT_WHITE, 1, cv2.LINE_AA)
                cv2.putText(image, "angle",
                            (gauge_cx - 18, gauge_cy + gauge_r + 15),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, TEXT_DIM, 1, cv2.LINE_AA)

            # Compile and draw posture warnings panel
            warnings = []
            if results.pose_landmarks and len(results.pose_landmarks) > 0:
                if arms_warning:
                    warnings.append(arms_warning)
                if stage == "up" and feet_warning:
                    warnings.append(feet_warning)
                if stage == "down":
                    if depth_warning:
                        warnings.append(depth_warning)
                    if knees_warning:
                        warnings.append(knees_warning)

            if ui is None:
                if warnings:
                    panel_h = 24 + len(warnings) * 20
                    panel_y = img_h - panel_h - 15
                    draw_glass_panel(image, 10, panel_y, img_w - 20, panel_h, alpha=0.75, color=(20, 20, 40), radius=8)
                    cv2.rectangle(image, (10, panel_y), (img_w - 10, panel_y + panel_h), ACCENT_RED, 1)
                    for idx, warn in enumerate(warnings):
                        cv2.putText(image, warn, (20, panel_y + 18 + idx * 20),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (80, 80, 240), 1, cv2.LINE_AA)

            # Update stats & feedback on the Tkinter UI
            if ui is not None:
                ui.update_stat_card("Set", current_set)
                ui.update_stat_card("Reps", reps_in_set)
                ui.update_stat_card("Total Reps", counter)
                ui.update_stat_card("Hip Angle", f"{int(current_hip_angle)}°")

                feedback_msgs = []
                feedback_color = ACCENT_GREEN

                if results.pose_landmarks and len(results.pose_landmarks) > 0:
                    for w in warnings:
                        feedback_msgs.append(f"⚠️ {w}")
                    if warnings:
                        feedback_color = ACCENT_RED
                else:
                    feedback_msgs.append("⚠️ Positioning... Stand in front of camera.")
                    feedback_color = ACCENT_ORANGE

                if not feedback_msgs:
                    if set_finished:
                        feedback_msgs.append("✅ Set finished! Please rest.")
                        feedback_color = ACCENT_GREEN
                    elif set_started and last_rep_time is not None:
                        time_limit = 15.0 if reps_in_set >= 6 else 20.0
                        time_elapsed = now_time - last_rep_time
                        countdown = max(0.0, time_limit - time_elapsed)
                        feedback_msgs.append(f"✅ Form is Good. Rest in: {countdown:.1f}s")
                        feedback_color = ACCENT_GREEN
                    else:
                        feedback_msgs.append("✅ Form is Good. Keep it up!")
                        feedback_color = ACCENT_GREEN

                if robot_status == "SEARCHING":
                    ui.update_feedback("Searching...", ACCENT_ORANGE)
                else:
                    ui.update_feedback("\n".join(feedback_msgs), feedback_color)
                ui.update()

            if robot_status == "SEARCHING":
                cv2.putText(image, "Searching...", (image.shape[1]//2 - 120, image.shape[0]//2),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.2, ACCENT_ORANGE, 3, cv2.LINE_AA)

            if hand_landmarker is not None:
                draw_hand_controls_status(image, thumbs_up_frames, palm_stop_frames, is_paused=False)
            draw_home_button(image)
            local_stream_server.update_frame(image)

            if ui is None:
                if hand_landmarker is not None:
                    if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                        show_palm_stop_message('Squats', image)
                        break
                cv2.imshow('Squats', image)

                if cv2.waitKey(5) & 0xFF == ord('q') or home_clicked:
                    break
            else:
                if hand_landmarker is not None:
                    if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                        break
                if home_clicked:
                    break
    finally:
        send_command("STOP")
        if 'landmarker' in locals() and landmarker is not None:
            landmarker.close()
        if 'hand_landmarker' in locals() and hand_landmarker is not None:
            hand_landmarker.close()
        
        # Populate session summary
        global last_session_summary
        last_session_summary = {
            'exercise': 'Squats',
            'metrics': [
                ('Total Reps', counter),
                ('Total Sets', current_set)
            ],
            'errors': list(session_errors)
        }
        print_session_error_summary()
        save_session_to_firestore(last_session_summary)

    cap.release()
    cv2.destroyAllWindows()
    print(f"\n✅ Squats Done! Total Reps: {counter}")


def run_plank(ui=None):
    """Plank timer measuring hold duration."""
    clear_session_errors()
    plank_timer = 0
    in_plank = False
    start_time = None

    def count_plank_time(shoulder, hip, ankle, in_plank_state, plank_start):
        if check_plank(shoulder, hip, ankle):
            now = time.time()
            if not in_plank_state:
                plank_start = now
                in_plank_state = True
                return in_plank_state, plank_start, 0
            else:
                elapsed_time = now - plank_start
                plank_start = now
                return in_plank_state, plank_start, elapsed_time
        else:
            in_plank_state = False
            plank_start = None
        return in_plank_state, plank_start, 0

    cap = cv2.VideoCapture(CAMERA_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    if ui is None:
        cv2.namedWindow('Plank', cv2.WINDOW_NORMAL)
        cv2.resizeWindow('Plank', DISPLAY_WIDTH, DISPLAY_HEIGHT)
    global home_clicked
    home_clicked = False
    if ui is None:
        cv2.setMouseCallback('Plank', on_mouse_click)
    print("🧘 Plank Timer Active! Press Q to quit.")

    publish_robot_state("LOADING")
    landmarker = create_pose_landmarker(det_conf=0.5, track_conf=0.5)
    hand_landmarker = create_hand_landmarker()
    timestamp_ms = 0
    hand_timestamp_ms = 0
    palm_stop_frames = 0
    frame_counter = 0
    last_results = None
    last_hand_results = None

    paused = False
    thumbs_up_frames = 0
    thumbs_up_cooldown_active = False

    # Positioning state
    positioning_duration = 3.0
    positioning_start_time = time.time()
    positioning_completed = False
    send_command("SIDE")

    robot_status = "TRACKING"
    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            # Remote Force Stop from web dashboard
            if force_stop.is_set():
                print("  [REMOTE] Force Stop received from web UI")
                send_command("STOP")
                break

            frame_counter += 1
            image = cv2.resize(frame, (DISPLAY_WIDTH, DISPLAY_HEIGHT))
            # Run Pose detection every FRAME_SKIP frames
            if frame_counter % FRAME_SKIP == 0 or last_results is None:
                results, timestamp_ms = detect_pose_frame(landmarker, frame, timestamp_ms)
                last_results = results
            else:
                results = last_results

            # Check positioning phase
            if not positioning_completed:
                elapsed = time.time() - positioning_start_time
                if elapsed < positioning_duration:
                    pct = elapsed / positioning_duration
                    if results and results.pose_landmarks:
                        draw_skeleton(image, results)
                    draw_positioning_overlay(image, "SIDE", pct)
                    draw_home_button(image)
                    local_stream_server.update_frame(image)

                    if ui is not None:
                        ui.update_feedback(f"🤖 ROBOT POSITIONING...\nAligning to SIDE view... ({int(pct*100)}%)", ACCENT_CYAN)
                        ui.update()
                    else:
                        send_command("SIDE")
                        cv2.imshow('Plank', image)
                        if cv2.waitKey(10) & 0xFF == ord('q') or home_clicked:
                            break
                    send_command("SIDE")
                    continue
                else:
                    send_command("STOP")
                    positioning_completed = True
                    # Start real session for live dashboard (no dummy data)
                    if _session_id is None:
                        robot_status = "TRACKING"
                        try:
                            publish_session_start("plank")
                            publish_robot_state("ACTIVE")
                        except Exception:
                            pass

            # Run Hand detection every HAND_FRAME_SKIP frames
            if hand_landmarker is not None:
                if frame_counter % HAND_FRAME_SKIP == 0 or last_hand_results is None:
                    hand_results, hand_timestamp_ms = detect_hand_frame(
                        hand_landmarker, frame, hand_timestamp_ms
                    )
                    last_hand_results = hand_results

                    # Thumbs-up check
                    if is_thumbs_up(hand_results):
                        if not thumbs_up_cooldown_active:
                            thumbs_up_frames += HAND_FRAME_SKIP
                            if thumbs_up_frames >= PALM_STOP_REQUIRED_FRAMES:
                                paused = not paused
                                thumbs_up_frames = 0
                                thumbs_up_cooldown_active = True
                                if paused:
                                    pause_start_time = time.time()
                                    robot_status = "TRACKING"
                                    try:
                                        publish_pause()
                                    except Exception:
                                        pass
                                else:
                                    if 'pause_start_time' in dir() and pause_start_time:
                                        _pause_accumulator += (time.time() - pause_start_time)
                                    robot_status = "TRACKING"
                                    try:
                                        publish_resume()
                                    except Exception:
                                        pass
                                print(f"⏸️ Plank: {'PAUSED' if paused else 'RESUMED'}")
                    else:
                        thumbs_up_frames = 0
                        thumbs_up_cooldown_active = False

                    palm_stop_frames = update_palm_stop_frames(hand_results, palm_stop_frames, increment=HAND_FRAME_SKIP)
                else:
                    hand_results = last_hand_results
            else:
                palm_stop_frames = 0
                thumbs_up_frames = 0

            # If paused, render overlay and bypass main logic
            if paused:
                send_command("STOP")
                # Reset plank timer tracking variables so elapsed time is not added on resume
                in_plank = False
                start_time = None
                
                if results and results.pose_landmarks:
                    draw_skeleton(image, results)
                draw_paused_overlay(image, thumbs_up_frames)
                if hand_landmarker is not None:
                    draw_hand_controls_status(image, thumbs_up_frames, palm_stop_frames, is_paused=True)
                draw_home_button(image)
                local_stream_server.update_frame(image)

                if ui is not None:
                    ui.update_feedback("⏸️ EXERCISE PAUSED\nShow Thumbs-Up to Resume", ACCENT_ORANGE)
                    ui.update()
                    if hand_landmarker is not None:
                        if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                            break
                else:
                    if hand_landmarker is not None:
                        if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                            show_palm_stop_message('Plank', image)
                            break
                    cv2.imshow('Plank', image)
                    if cv2.waitKey(10) & 0xFF == ord('q') or home_clicked:
                        break
                continue

            is_horizontal = False
            is_looking_around = False

            robot_status = "TRACKING"
            try:
                if results.pose_landmarks and len(results.pose_landmarks) > 0:
                    if positioning_completed and not paused:
                        robot_status = adjust_robot_position("PLANK", results)
                        if robot_status == "LOST_TIMEOUT":
                            break
                    lm = results.pose_landmarks[0]
                    left_shoulder = get_pt(lm, 11)
                    left_hip = get_pt(lm, 23)
                    left_ankle = get_pt(lm, 27)
                    right_shoulder = get_pt(lm, 12)
                    right_hip = get_pt(lm, 24)
                    right_ankle = get_pt(lm, 28)

                    left_ok = left_shoulder and left_hip and left_ankle
                    right_ok = right_shoulder and right_hip and right_ankle

                    shoulder, hip, ankle = None, None, None
                    if left_ok and right_ok:
                        left_vis = (lm[11].visibility + lm[23].visibility + lm[27].visibility) / 3.0
                        right_vis = (lm[12].visibility + lm[24].visibility + lm[28].visibility) / 3.0
                        if left_vis > right_vis:
                            shoulder, hip, ankle = left_shoulder, left_hip, left_ankle
                        else:
                            shoulder, hip, ankle = right_shoulder, right_hip, right_ankle
                    elif left_ok:
                        shoulder, hip, ankle = left_shoulder, left_hip, left_ankle
                    elif right_ok:
                        shoulder, hip, ankle = right_shoulder, right_hip, right_ankle

                    if shoulder and hip and ankle:
                        if left_shoulder and right_shoulder:
                            neck = [(left_shoulder[0] + right_shoulder[0]) / 2, (left_shoulder[1] + right_shoulder[1]) / 2]
                        else:
                            neck = shoulder

                        is_horizontal = abs(shoulder[0] - ankle[0]) > abs(shoulder[1] - ankle[1])

                        left_ear = get_pt(lm, 7)
                        right_ear = get_pt(lm, 8)
                        if left_ear and right_ear and left_shoulder and right_shoulder:
                            ear_dist = calculate_distance(left_ear, right_ear)
                            sh_width = calculate_distance(left_shoulder, right_shoulder)
                            if sh_width > 0 and (ear_dist / sh_width) > 0.35:
                                is_looking_around = True

                        if is_horizontal:
                            in_plank, start_time, elapsed_time = count_plank_time(
                                neck, hip, ankle, in_plank, start_time
                            )
                            plank_timer += elapsed_time
                            robot_status = "TRACKING"
                            try:
                                publish_plank_time(plank_timer)
                            except Exception:
                                pass

                            if not in_plank:
                                log_session_error("Form a straight line with body")
                            if is_looking_around:
                                log_session_error("Look in front of you")
                        else:
                            in_plank = False
                            start_time = None
                else:
                    if positioning_completed and not paused:
                        robot_status = adjust_robot_position("PLANK", None)
                    else:
                        send_command("STOP")
            except Exception:
                pass

            if ui is None:
                draw_skeleton(image, results)

            ring_cx, ring_cy, ring_r = 55, 88, 38

            if is_horizontal:
                if in_plank:
                    if is_looking_around:
                        state_color = ACCENT_ORANGE
                        state_text = "LOOK FORWARD"
                    else:
                        state_color = ACCENT_GREEN
                        state_text = "HOLD"
                else:
                    state_color = ACCENT_RED
                    state_text = "ALIGN BODY"
            else:
                state_color = ACCENT_ORANGE
                state_text = "GET IN POSITION"

            if ui is None:
                draw_glass_panel(image, 5, 5, 230, ring_r * 2 + 50,
                                 alpha=0.65, radius=10)

                cv2.putText(image, "PLANK", (15, 28),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_DIM, 1, cv2.LINE_AA)

                cv2.ellipse(image, (ring_cx, ring_cy), (ring_r, ring_r),
                            0, 0, 360, (40, 40, 40), 3)
                sweep = int((plank_timer % 60) / 60.0 * 360)
                cv2.ellipse(image, (ring_cx, ring_cy), (ring_r, ring_r),
                            -90, 0, sweep, state_color, 3)

                mins = int(plank_timer) // 60
                secs = plank_timer % 60
                if mins > 0:
                    time_str = f"{mins}:{secs:05.2f}"
                else:
                    time_str = f"{secs:.1f}s"
                (tw, _), _ = cv2.getTextSize(time_str, cv2.FONT_HERSHEY_SIMPLEX,
                                             0.5, 1)
                cv2.putText(image, time_str,
                            (ring_cx - tw // 2, ring_cy + 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_WHITE, 1, cv2.LINE_AA)

                cv2.putText(image, state_text, (ring_cx + ring_r + 12, ring_cy - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, state_color, 1, cv2.LINE_AA)
                cv2.putText(image, f"Total: {plank_timer:.1f}s",
                            (ring_cx + ring_r + 12, ring_cy + 16),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, TEXT_WHITE, 1, cv2.LINE_AA)

                if is_horizontal:
                    if not in_plank:
                        warn_msg = "Form a straight line with body!"
                        img_h, img_w = image.shape[:2]
                        draw_glass_panel(image, 5, img_h - 55, img_w - 10, 50, alpha=0.8, color=(20, 20, 100), radius=10)
                        cv2.putText(image, warn_msg, (15, img_h - 25),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, ACCENT_RED, 2, cv2.LINE_AA)
                    elif is_looking_around:
                        warn_msg = "Look in front of you!"
                        img_h, img_w = image.shape[:2]
                        draw_glass_panel(image, 5, img_h - 55, img_w - 10, 50, alpha=0.8, color=(15, 45, 95), radius=10)
                        cv2.putText(image, warn_msg, (15, img_h - 25),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, ACCENT_ORANGE, 2, cv2.LINE_AA)

            # Update stats & feedback on the Tkinter UI
            if ui is not None:
                mins = int(plank_timer) // 60
                secs = plank_timer % 60
                if mins > 0:
                    time_str = f"{mins}:{secs:05.2f}"
                else:
                    time_str = f"{secs:.1f}s"

                ui.update_stat_card("Hold Time", time_str, state_color)
                ui.update_stat_card("Status", state_text, state_color)

                feedback_msgs = []
                feedback_color = ACCENT_GREEN

                if results.pose_landmarks and len(results.pose_landmarks) > 0:
                    if is_horizontal:
                        if not in_plank:
                            feedback_msgs.append("⚠️ Form a straight line with body!")
                            feedback_color = ACCENT_RED
                        elif is_looking_around:
                            feedback_msgs.append("⚠️ Look in front of you!")
                            feedback_color = ACCENT_ORANGE
                    else:
                        feedback_msgs.append("⚠️ Align horizontally! Lie down in profile view.")
                        feedback_color = ACCENT_ORANGE
                else:
                    feedback_msgs.append("⚠️ Positioning... Lie down in front of camera.")
                    feedback_color = ACCENT_ORANGE

                if not feedback_msgs:
                    if in_plank:
                        feedback_msgs.append("✅ Form is Good. HOLD!")
                        feedback_color = ACCENT_GREEN
                    else:
                        feedback_msgs.append("✅ Aligning body...")
                        feedback_color = ACCENT_ORANGE

                if robot_status == "SEARCHING":
                    ui.update_feedback("Searching...", ACCENT_ORANGE)
                else:
                    ui.update_feedback("\n".join(feedback_msgs), feedback_color)
                ui.update()

            if robot_status == "SEARCHING":
                cv2.putText(image, "Searching...", (image.shape[1]//2 - 120, image.shape[0]//2),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.2, ACCENT_ORANGE, 3, cv2.LINE_AA)

            if hand_landmarker is not None:
                draw_hand_controls_status(image, thumbs_up_frames, palm_stop_frames, is_paused=False)
            draw_home_button(image)
            local_stream_server.update_frame(image)

            if ui is None:
                if hand_landmarker is not None:
                    if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                        show_palm_stop_message('Plank', image)
                        break
                cv2.imshow('Plank', image)

                if cv2.waitKey(10) & 0xFF == ord('q') or home_clicked:
                    break
            else:
                if hand_landmarker is not None:
                    if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                        break
                if home_clicked:
                    break
    finally:
        send_command("STOP")
        if 'landmarker' in locals() and landmarker is not None:
            landmarker.close()
        if 'hand_landmarker' in locals() and hand_landmarker is not None:
            hand_landmarker.close()
        
        # Populate session summary
        global last_session_summary
        last_session_summary = {
            'exercise': 'Plank',
            'metrics': [
                ('Total Hold Time', f"{plank_timer:.1f}s")
            ],
            'errors': list(session_errors)
        }
        print_session_error_summary()
        save_session_to_firestore(last_session_summary)

    cap.release()
    if ui is None:
        cv2.destroyAllWindows()
    print(f"\n✅ Plank Done! Total Time: {plank_timer:.2f}s")


# ═══════════════════════════════════════════════════════
#  MAIN (Person Following)
# ═══════════════════════════════════════════════════════

def main():
    # ─── MediaPipe Tasks API Setup ───
    options = vision.PoseLandmarkerOptions(
        base_options=mp_tasks.BaseOptions(
            model_asset_path=MODEL_PATH
        ),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.5,
        min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.5
    )
    landmarker = vision.PoseLandmarker.create_from_options(options)
    timestamp_ms = 0
    print("✅ MediaPipe loaded")

    # ─── Camera ───
    print("\nOpening camera...")
    cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap = cv2.VideoCapture(CAMERA_INDEX)

    cap.set(cv2.CAP_PROP_FOURCC,
            cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    for _ in range(5):
        cap.read()

    aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"✅ Camera: {aw}x{ah}")

    # Signal handler
    def shutdown(sig, frame):
        print("\n🛑 Stopping...")
        send_command("STOP")
        time.sleep(0.2)
        cap.release()
        cv2.destroyAllWindows()
        robot_status = "TRACKING"
        try:
            landmarker.close()
        except:
            pass
        if ser is not None:
            ser.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)

    print()
    print("🚀 8-Direction Person Following Active!")
    print("─" * 45)
    print("  ↖ FWD_LEFT  │ ↑ FORWARD  │ ↗ FWD_RIGHT")
    print("  ← LEFT      │ ● STOP     │ → RIGHT")
    print("  ↙ BWD_LEFT  │ ↓ BACKWARD │ ↘ BWD_RIGHT")
    print("─" * 45)
    print("  Press Q to quit")
    print()

    if SHOW_WINDOW:
        cv2.namedWindow('Person Follower', cv2.WINDOW_NORMAL)
        cv2.resizeWindow('Person Follower', DISPLAY_WIDTH, DISPLAY_HEIGHT)
        global home_clicked
        home_clicked = False
        cv2.setMouseCallback('Person Follower', on_mouse_click)

    fps          = 0
    fps_counter  = 0
    fps_time     = time.time()
    frame_counter = 0
    start_time   = time.time()

    command        = "STOP"
    state          = "STARTING"
    person_x       = None
    shoulder_width = None
    d_zone         = "GOOD"
    h_zone         = "CENTER"
    last_results   = None
    lost_since     = None

    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        # Remote Force Stop from web dashboard
        if force_stop.is_set():
            print("  [REMOTE] Force Stop received from web UI")
            send_command("STOP")
            break

        frame_counter += 1

        if frame_counter % FRAME_SKIP == 0:
            timestamp_ms += 33
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image  = mp.Image(
                image_format=mp.ImageFormat.SRGB,
                data=frame_rgb
            )

            results      = landmarker.detect_for_video(mp_image, timestamp_ms)
            last_results = results

            person_x       = None
            shoulder_width = None

            if (results.pose_landmarks and
                    len(results.pose_landmarks) > 0):
                lm        = results.pose_landmarks[0]
                nose      = lm[0]
                l_shoulder = lm[11]
                r_shoulder = lm[12]
                l_hip      = lm[23]
                r_hip      = lm[24]

                shoulder_ok = (
                    l_shoulder.visibility > 0.2 and
                    r_shoulder.visibility > 0.2
                )

                hip_ok = (
                    l_hip.visibility > 0.2 and
                    r_hip.visibility > 0.2
                )

                if shoulder_ok:

                    shoulder_center = (
                        l_shoulder.x + r_shoulder.x
                    ) / 2

                    shoulder_width = abs(
                        l_shoulder.x - r_shoulder.x
                    )

                    # Full body available
                    if hip_ok:

                        hip_center = (
                            l_hip.x + r_hip.x
                        ) / 2

                        person_x = (
                            shoulder_center * 0.6 +
                            hip_center * 0.4
                        )

                    # Too close → hips missing
                    else:

                        person_x = shoulder_center

                    lost_since = None

                    command, state, d_zone, h_zone = calculate_command(
                        person_x,
                        shoulder_width
                    )

                    send_command(command)

                    # Publish high-level robot state for the web dashboard (real)
                    robot_status = "TRACKING"
                    try:
                        if person_x is not None and command not in ("STOP", "SEARCH"):
                            publish_robot_state("FOLLOWING", command)
                        elif command == "SEARCH":
                            publish_robot_state("SEARCHING", command)
                        else:
                            publish_robot_state("STOPPED", command)
                    except Exception:
                        pass
            else:
                # No person
                now = time.time()
                if lost_since is None:
                    lost_since = now
                    send_command("STOP")
                    command = "STOP"
                    state   = "LOST"
                elif now - lost_since > LOST_PERSON_TIMEOUT:
                    send_command("SEARCH")
                    command = "SEARCH"
                    state   = "SEARCHING..."

                d_zone = "GOOD"
                h_zone = "CENTER"

            # Terminal debug
            if (DEBUG and SHOW_FPS and
                    frame_counter % (30 * FRAME_SKIP) == 0):
                det = "👤" if person_x is not None else "❌"
                print(f"  {det} FPS:{fps:.0f} | "
                      f"CMD:{command:10s} | {state}")

        # Draw on display frame resized to target dimensions
        display = cv2.resize(frame, (DISPLAY_WIDTH, DISPLAY_HEIGHT))
        if last_results:
            draw_skeleton(display, last_results)

        draw_frame(display, command, state, person_x,
                   shoulder_width, fps, d_zone, h_zone)
        draw_home_button(display)

        if SHOW_WINDOW:
            local_stream_server.update_frame(display)
            local_stream_server.update_frame(display)
            cv2.imshow('Person Follower', display)

            key = cv2.waitKey(1) & 0xFF
            if key == ord('q') or home_clicked:
                break

        # FPS
        fps_counter += 1
        if time.time() - fps_time >= 1.0:
            fps         = fps_counter
            fps_counter = 0
            fps_time    = time.time()

    send_command("STOP")
    time.sleep(0.2)
    cap.release()
    cv2.destroyAllWindows()
    landmarker.close()
    if ser is not None:
        ser.close()
    
    # Populate session summary
    global last_session_summary
    duration = time.time() - start_time
    last_session_summary = {
        'exercise': 'Person Follower',
        'metrics': [
            ('Duration', f"{duration:.1f}s")
        ],
        'errors': []
    }


class RoundedButton(tk.Canvas):
    def __init__(self, parent, text, command, bg_color, fg_color="#1E1E24", radius=15, font=("Helvetica", 12, "bold"), height=50, width=None, outline_color=None, outline_width=3, **kwargs):
        super().__init__(parent, borderwidth=0, highlightthickness=0, bg=parent["bg"], height=height, **kwargs)
        if width:
            self.configure(width=width)
        self.command = command
        self.bg_color = bg_color
        self.fg_color = fg_color
        self.radius = radius
        self.text = text
        self.font = font
        self.pressed = False
        self.outline_color = outline_color
        self.outline_width = outline_width
        
        self.bind("<Configure>", self._draw)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        
    def _draw(self, event=None):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        r = min(self.radius, w//2, h//2)
        bw = self.outline_width if self.outline_color else 0
        
        if w <= 0 or h <= 0:
            return
            
        color = self.bg_color
        if self.pressed:
            color = self._darken_color(self.bg_color)
            
        # Draw rounded rectangle parts
        # Circles for corners
        self.create_oval(bw, bw, r*2+bw, r*2+bw, fill=color, outline=color)
        self.create_oval(w - r*2 - bw, bw, w - bw, r*2+bw, fill=color, outline=color)
        self.create_oval(bw, h - r*2 - bw, r*2+bw, h - bw, fill=color, outline=color)
        self.create_oval(w - r*2 - bw, h - r*2 - bw, w - bw, h - bw, fill=color, outline=color)
        
        # Rectangles to fill the center
        self.create_rectangle(r+bw, bw, w - r - bw, h - bw, fill=color, outline=color)
        self.create_rectangle(bw, r+bw, w - bw, h - r - bw, fill=color, outline=color)
        
        # Draw outline if color is specified
        if self.outline_color and bw > 0:
            self.create_arc(bw, bw, r*2+bw, r*2+bw, start=90, extent=90, style="arc", outline=self.outline_color, width=bw)
            self.create_arc(w - r*2 - bw, bw, w - bw, r*2+bw, start=0, extent=90, style="arc", outline=self.outline_color, width=bw)
            self.create_arc(bw, h - r*2 - bw, r*2+bw, h - bw, start=180, extent=90, style="arc", outline=self.outline_color, width=bw)
            self.create_arc(w - r*2 - bw, h - r*2 - bw, w - bw, h - bw, start=270, extent=90, style="arc", outline=self.outline_color, width=bw)
            
            self.create_line(r+bw, bw, w - r - bw, bw, fill=self.outline_color, width=bw)
            self.create_line(r+bw, h - bw, w - r - bw, h - bw, fill=self.outline_color, width=bw)
            self.create_line(bw, r+bw, bw, h - r - bw, fill=self.outline_color, width=bw)
            self.create_line(w - bw, r+bw, w - bw, h - r - bw, fill=self.outline_color, width=bw)

        # Text in the center
        self.create_text(w//2, h//2, text=self.text, fill=self.fg_color, font=self.font)
        
    def _darken_color(self, hex_color):
        robot_status = "TRACKING"
        try:
            if hex_color.startswith("#"):
                r = int(hex_color[1:3], 16)
                g = int(hex_color[3:5], 16)
                b = int(hex_color[5:7], 16)
                return f"#{max(0, r-30):02x}{max(0, g-30):02x}{max(0, b-30):02x}"
        except Exception:
            pass
        return hex_color

    def _on_press(self, event):
        self.pressed = True
        self._draw()
        
    def _on_release(self, event):
        self.pressed = False
        self._draw()
        if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            if self.command:
                self.command()
            
    def _on_enter(self, event):
        self.configure(cursor="hand2")
        
    def _on_leave(self, event):
        self.configure(cursor="")
        if self.pressed:
            self.pressed = False
            self._draw()


class OutlinedLabel(tk.Canvas):
    def __init__(self, parent, text, font, fg, outline_color, bg_color="#1E1E24", **kwargs):
        super().__init__(parent, borderwidth=0, highlightthickness=0, bg=bg_color, **kwargs)
        self.text = text
        self.font = font
        self.fg = fg
        self.outline_color = outline_color
        self.bind("<Configure>", self._draw)
        
    def _draw(self, event=None):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 0 or h <= 0:
            return
        
        x = w // 2
        y = h // 2
        
        # Draw outline border around text
        offsets = [(-2, -2), (-2, 0), (-2, 2), (0, -2), (0, 2), (2, -2), (2, 0), (2, 2)]
        for dx, dy in offsets:
            self.create_text(x + dx, y + dy, text=self.text, font=self.font, fill=self.outline_color, anchor="center")
            
        # Center main text
        self.create_text(x, y, text=self.text, font=self.font, fill=self.fg, anchor="center")


class RoundedFrame(tk.Canvas):
    def __init__(self, parent, bg_color, border_color, border_width=3, radius=20, **kwargs):
        super().__init__(parent, borderwidth=0, highlightthickness=0, bg=parent["bg"], **kwargs)
        self.bg_color = bg_color
        self.border_color = border_color
        self.border_width = border_width
        self.radius = radius
        self.bind("<Configure>", self._draw)
        
    def _draw(self, event=None):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        r = self.radius
        bw = self.border_width
        
        if w <= 0 or h <= 0:
            return
            
        # Draw background rounded rect
        self.create_oval(bw, bw, r*2+bw, r*2+bw, fill=self.bg_color, outline=self.bg_color)
        self.create_oval(w - r*2 - bw, bw, w - bw, r*2+bw, fill=self.bg_color, outline=self.bg_color)
        self.create_oval(bw, h - r*2 - bw, r*2+bw, h - bw, fill=self.bg_color, outline=self.bg_color)
        self.create_oval(w - r*2 - bw, h - r*2 - bw, w - bw, h - bw, fill=self.bg_color, outline=self.bg_color)
        self.create_rectangle(r+bw, bw, w - r - bw, h - bw, fill=self.bg_color, outline=self.bg_color)
        self.create_rectangle(bw, r+bw, w - bw, h - r - bw, fill=self.bg_color, outline=self.bg_color)
        
        # Draw outline border
        if self.border_color and bw > 0:
            self.create_arc(bw, bw, r*2+bw, r*2+bw, start=90, extent=90, style="arc", outline=self.border_color, width=bw)
            self.create_arc(w - r*2 - bw, bw, w - bw, r*2+bw, start=0, extent=90, style="arc", outline=self.border_color, width=bw)
            self.create_arc(bw, h - r*2 - bw, r*2+bw, h - bw, start=180, extent=90, style="arc", outline=self.border_color, width=bw)
            self.create_arc(w - r*2 - bw, h - r*2 - bw, w - bw, h - bw, start=270, extent=90, style="arc", outline=self.border_color, width=bw)
            
            self.create_line(r+bw, bw, w - r - bw, bw, fill=self.border_color, width=bw)
            self.create_line(r+bw, h - bw, w - r - bw, h - bw, fill=self.border_color, width=bw)
            self.create_line(bw, r+bw, bw, h - r - bw, fill=self.border_color, width=bw)
            self.create_line(w - bw, r+bw, w - bw, h - r - bw, fill=self.border_color, width=bw)


class FitnessRobotUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fitness Robot UI")
        
        # Open in fullscreen
        self.attributes("-fullscreen", True)
        self.bind("<Escape>", lambda e: self.quit_app())
        
        # Screen dimensions configuration
        self.configure(bg="#1E1E24")
        
        # Custom styles
        self.style = ttk.Style(self)
        self.style.theme_use('clam')
        self.style.configure('.', background='#1E1E24', foreground='#EBEBEF')
        
        self.main_container = tk.Frame(self, bg="#1E1E24")
        self.main_container.pack(fill="both", expand=True, padx=5, pady=5)
        
        self.exercise_active = False
        self.next_remote_mode = None
        self.show_home_screen()
        self.after(200, self.check_remote_commands)
        
    def show_home_screen(self):
        # Clear container
        for widget in self.main_container.winfo_children():
            widget.destroy()
            
        # Title banner with Outlined Label
        title_canvas = OutlinedLabel(
            self.main_container,
            text="FITNESS ROBOT",
            font=("Helvetica", 24, "bold"),
            fg="#96D75F",
            outline_color="#000000",
            height=40
        )
        title_canvas.pack(pady=(5, 10))
        
        # Grid frame for exercises
        grid_frame = tk.Frame(self.main_container, bg="#1E1E24")
        grid_frame.pack(fill="both", expand=True)
        
        exercises = [
            ("Bicep Curls", self.launch_bicep_curls, "#96D75F"),
            ("Push-ups", self.launch_pushups, "#96D75F"),
            ("Squats", self.launch_squats, "#96D75F"),
            ("Plank", self.launch_plank, "#96D75F"),
        ]
        
        # Layout in grid
        for i, (name, callback, color) in enumerate(exercises):
            # Create a premium rounded button card
            btn = RoundedButton(
                grid_frame,
                text=name,
                command=callback,
                bg_color="#2B2B36",
                fg_color=color,
                outline_color=color,
                outline_width=2,
                radius=10,
                font=("Helvetica", 12, "bold"),
                height=45
            )
            # 2 columns layout
            row = i // 2
            col = i % 2
            btn.grid(row=row, column=col, sticky="nsew", padx=8, pady=8)
            
        grid_frame.columnconfigure(0, weight=1)
        grid_frame.columnconfigure(1, weight=1)
        grid_frame.rowconfigure(0, weight=1)
        grid_frame.rowconfigure(1, weight=1)
        
        # Bottom system buttons
        sys_frame = tk.Frame(self.main_container, bg="#1E1E24")
        sys_frame.pack(fill="x", pady=(10, 5))
        for i in range(3):
            sys_frame.columnconfigure(i, weight=1)
        
        sys_buttons = [
            ("REBOOT", self.confirm_reboot, "#E5B05E"),
            ("SHUTDOWN", self.confirm_shutdown, "#5F5FDF"),
            ("EXIT APP", self.quit_app, "#5F5FDF"),
        ]
        
        for i, (name, callback, color) in enumerate(sys_buttons):
            btn = RoundedButton(
                sys_frame,
                text=name,
                command=callback,
                bg_color="#2B2B36",
                fg_color=color,
                outline_color=color,
                outline_width=2,
                radius=8,
                font=("Helvetica", 10, "bold"),
                height=35
            )
            btn.grid(row=0, column=i, sticky="ew", padx=5)

    def run_exercise(self, exercise_name, func):
        self.exercise_active = True
        force_stop.clear()
        try:
            self.show_exercise_screen(exercise_name)
            self.update()
            func(self)
        except tk.TclError:
            # GUI was destroyed during execution, exit loop gracefully
            return
        finally:
            self.exercise_active = False
            try:
                local_stream_server.update_frame(None)
            except Exception:
                pass
            try:
                exists = self.winfo_exists()
            except tk.TclError:
                exists = False
            if exists:
                if self.next_remote_mode:
                    mode = self.next_remote_mode
                    self.next_remote_mode = None
                    force_stop.clear()
                    self.launch_by_name(mode)
                else:
                    try:
                        self.show_summary_screen()
                    except tk.TclError:
                        pass

    def check_remote_commands(self):
        try:
            if not self.winfo_exists():
                return
            if not remote_mode_queue.empty():
                cmd_type, cmd_val = remote_mode_queue.get()
                if cmd_type == "switch":
                    print(f"[GUI] Remote mode switch received: {cmd_val}")
                    if self.exercise_active:
                        self.next_remote_mode = cmd_val
                        force_stop.set()
                    else:
                        self.launch_by_name(cmd_val)
            self.after(200, self.check_remote_commands)
        except (tk.TclError, Exception) as e:
            pass

    def launch_by_name(self, name):
        name = str(name).upper().replace("-", "_").replace(" ", "_")
        if name in ("BICEP_CURLS", "BICEP_CURL"):
            self.launch_bicep_curls()
        elif name in ("PUSHUPS", "PUSH_UPS"):
            self.launch_pushups()
        elif name in ("SQUATS", "SQUAT"):
            self.launch_squats()
        elif name == "PLANK":
            self.launch_plank()
        elif name in ("PERSON_FOLLOW", "PERSON_TRACKING", "FOLLOWING", "FOLLOW"):
            self.launch_person_follow()
        else:
            print(f"[GUI] Unknown remote mode: {name}")

    def launch_person_follow(self):
        self.run_exercise("Person Following", lambda ui: main())

    def launch_bicep_curls(self):
        self.run_exercise("Bicep Curls", run_bicep_curls)
        
    def launch_pushups(self):
        self.run_exercise("Push-ups", run_pushups)
        
    def launch_squats(self):
        self.run_exercise("Squats", run_squats)
        
    def launch_plank(self):
        self.run_exercise("Plank", run_plank)

    def show_exercise_screen(self, exercise_name):
        # Clear container
        for widget in self.main_container.winfo_children():
            widget.destroy()
            
        # Title Banner (Exercise Type)
        self.ex_title_label = tk.Label(
            self.main_container, 
            text=exercise_name, 
            font=("Helvetica", 20, "bold"), 
            fg="#96D75F", 
            bg="#1E1E24"
        )
        self.ex_title_label.pack(anchor="w", pady=(5, 10), padx=5)
        
        # Row Frame for Count Boxes/Stats
        self.stats_row_frame = tk.Frame(self.main_container, bg="#1E1E24")
        self.stats_row_frame.pack(anchor="w", fill="x", pady=5, padx=5)
        
        # Dictionary to hold the dynamically updated count/stat cards
        self.stat_cards = {}
        
        # Direct Label for comments/feedback (no framing/no border to match screenshot)
        self.feedback_label = tk.Label(
            self.main_container, 
            text="Aligning and tracking...", 
            font=("Helvetica", 14, "bold"), 
            fg="#EBEBEF", 
            bg="#1E1E24",
            wraplength=450,
            justify="left",
            anchor="w"
        )
        self.feedback_label.pack(anchor="w", fill="x", pady=10, padx=5)
        
        # Bottom bar for Home Button
        self.bottom_bar = tk.Frame(self.main_container, bg="#1E1E24")
        self.bottom_bar.pack(fill="x", side="bottom", pady=10)
        
        # Home button on bottom right corner (orange background, white text)
        self.home_btn = RoundedButton(
            self.bottom_bar,
            text="Home",
            command=self.set_home_clicked,
            bg_color="#FF9500",
            fg_color="#FFFFFF",
            radius=10,
            width=100,
            height=35,
            font=("Helvetica", 12, "bold")
        )
        self.home_btn.pack(side="right", padx=10)

    def update_stat_card(self, label, value, color="#96D75F"):
        try:
            if not self.winfo_exists():
                return
            color = bgr_to_hex(color)
            if label not in self.stat_cards:
                # Create a card frame using RoundedFrame
                card = RoundedFrame(
                    self.stats_row_frame, 
                    bg_color="#2B2B36", 
                    border_color=color, 
                    border_width=2, 
                    radius=10,
                    width=110,
                    height=75
                )
                card.pack(side="left", padx=3)
                card.pack_propagate(False)
                
                lbl = tk.Label(card, text=f"{label}:", font=("Helvetica", 10, "bold"), fg=color, bg="#2B2B36")
                lbl.pack(anchor="w", padx=10, pady=(10, 2))
                
                val_lbl = tk.Label(card, text=str(value), font=("Helvetica", 18, "bold"), fg="#FFFFFF", bg="#2B2B36")
                val_lbl.pack(anchor="w", padx=10)
                
                self.stat_cards[label] = (card, val_lbl, lbl)
            else:
                card, val_lbl, lbl = self.stat_cards[label]
                val_lbl.configure(text=str(value))
                card.border_color = color
                card._draw()
                lbl.configure(fg=color)
        except tk.TclError:
            pass

    def update_feedback(self, text, color="#EBEBEF"):
        try:
            if not self.winfo_exists():
                return
            color = bgr_to_hex(color)
            self.feedback_label.configure(text=text, fg=color)
        except tk.TclError:
            pass

    def set_home_clicked(self):
        global home_clicked
        home_clicked = True

    def show_summary_screen(self):
        global last_session_summary
        if not last_session_summary:
            self.show_home_screen()
            return
            
        summary = last_session_summary
        last_session_summary = None  # Reset
        
        # Clear container
        for widget in self.main_container.winfo_children():
            widget.destroy()
            
        title_label = tk.Label(self.main_container, text=f"SESSION SUMMARY: {summary['exercise'].upper()}", font=("Helvetica", 14, "bold"), fg="#96D75F", bg="#1E1E24")
        title_label.pack(pady=(5, 10))
        
        # Card style display for metrics
        card_container = tk.Frame(self.main_container, bg="#2B2B36", height=150)
        card_container.pack(fill="x", padx=10, pady=5)
        card_container.pack_propagate(False)
        
        canvas = tk.Canvas(card_container, bg="#2B2B36", highlightthickness=0)
        scrollbar = tk.Scrollbar(card_container, orient="vertical", command=canvas.yview)
        card_frame = tk.Frame(canvas, bg="#2B2B36", padx=10, pady=10)
        
        card_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=card_frame, anchor="nw", width=420)
        canvas.configure(yscrollcommand=scrollbar.set)
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        tk.Label(card_frame, text="Performance Metrics", font=("Helvetica", 12, "bold"), fg="#50AFDF", bg="#2B2B36").pack(anchor="w", pady=(0, 5))
        
        for metric, val in summary['metrics']:
            lbl = tk.Label(card_frame, text=f"• {metric}: {val}", font=("Helvetica", 10), fg="#EBEBEF", bg="#2B2B36")
            lbl.pack(anchor="w", pady=1)
            
        # Warnings summary
        if summary.get('errors'):
            tk.Label(card_frame, text="Form Warnings / Mistakes", font=("Helvetica", 12, "bold"), fg="#E5B05E", bg="#2B2B36").pack(anchor="w", pady=(10, 5))
            # Unique errors
            for err in set(summary['errors']):
                lbl = tk.Label(card_frame, text=f"⚠️ {err}", font=("Helvetica", 10), fg="#E5B05E", bg="#2B2B36", wraplength=380, justify="left")
                lbl.pack(anchor="w", pady=1)
        else:
            tk.Label(card_frame, text="✅ Perfect posture! No mistakes detected.", font=("Helvetica", 10, "italic"), fg="#96D75F", bg="#2B2B36").pack(anchor="w", pady=(10, 5))
            
        # Back home button
        btn = RoundedButton(
            self.main_container,
            text="BACK TO MENU",
            command=self.show_home_screen,
            bg_color="#2B2B36",
            fg_color="#96D75F",
            outline_color="#96D75F",
            outline_width=2,
            radius=10,
            font=("Helvetica", 12, "bold"),
            width=180,
            height=35
        )
        btn.pack(pady=10)

    def confirm_shutdown(self):
        if messagebox.askyesno("Shutdown", "Are you sure you want to shutdown the Raspberry Pi?"):
            self.destroy()
            if os.name == 'posix':
                subprocess.run(["sudo", "shutdown", "-h", "now"])
            else:
                subprocess.run(["shutdown", "/s", "/t", "0"])

    def confirm_reboot(self):
        if messagebox.askyesno("Reboot", "Are you sure you want to reboot the Raspberry Pi?"):
            self.destroy()
            if os.name == 'posix':
                subprocess.run(["sudo", "reboot"])
            else:
                subprocess.run(["shutdown", "/r", "/t", "0"])

    def quit_app(self):
        self.destroy()


if __name__ == "__main__":
    # Start local HTTP/WS servers for streaming & remote control
    try:
        local_stream_server.start_servers(force_stop_event=force_stop, remote_mode_queue=remote_mode_queue)
        print("Local HTTP/WS servers started successfully.")
    except Exception as e:
        print(f"Failed to start local servers: {e}")
        
    # Broadcast initial state
    publish_robot_state("READY")

    app = FitnessRobotUI()
    app.mainloop()
