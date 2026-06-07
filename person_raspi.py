#!/usr/bin/env python3
"""
8-Direction Person Following Robot
Raspberry Pi 4 + MediaPipe + ESP32 via WiFi UDP
Logitech C270 Camera
"""

import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['GLOG_minloglevel'] = '3'

import cv2
import mediapipe as mp
import time
import numpy as np
import socket
import signal
import sys


# ═══════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════

# ─── ESP32 ───
ESP32_IP   = "10.249.231.70"   # ← Your ESP32 IP
ESP32_PORT = 8888

# ─── Camera (C270) ───
CAMERA_INDEX  = 0
CAMERA_WIDTH  = 320            # Lower = faster on RPi4
CAMERA_HEIGHT = 240
FRAME_SKIP    = 1              # Process every frame on RPi4

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
WINDOW_SCALE = 2.0           # 320x240 → 640x480

# ─── Debug ───
DEBUG    = True
SHOW_FPS = True

from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision

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


# ═══════════════════════════════════════════════════════
#  ESP32 UDP SENDER
# ═══════════════════════════════════════════════════════
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setblocking(False)
current_command  = "STOP"
last_command_time = 0
print(f"✅ UDP → {ESP32_IP}:{ESP32_PORT}")


def send_command(command):
    """Send command to ESP32 only when changed."""
    global current_command, last_command_time
    now = time.time()

    if (command != current_command or
            now - last_command_time > 1.0):
        try:
            sock.sendto(
                command.encode('utf-8'),
                (ESP32_IP, ESP32_PORT)
            )
            if DEBUG and command != current_command:
                print(f"  📡 → ESP32: {command}")
        except Exception:
            pass

        current_command   = command
        last_command_time = now


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
    """Return True when MediaPipe Hands sees a clear open palm."""
    if not hand_results.hand_landmarks:
        return False

    for hand_landmarks in hand_results.hand_landmarks:
        points = [_hand_point(hand_landmarks, idx) for idx in range(21)]
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

        thumb_tip = points[4]
        thumb_ip = points[3]
        index_mcp = points[5]
        thumb_extended = (
            calculate_distance(thumb_tip, wrist) > calculate_distance(points[2], wrist) * 1.05 and
            calculate_distance(thumb_tip, index_mcp) > calculate_distance(thumb_ip, index_mcp)
        )

        fingertip_spread = max(points[idx][0] for idx in (4, 8, 12, 16, 20)) - min(
            points[idx][0] for idx in (4, 8, 12, 16, 20)
        )
        if extended >= 4 and thumb_extended and fingertip_spread > hand_size * 0.35:
            return True

    return False


def update_palm_stop_frames(hand_results, stop_frames):
    """Debounce palm stop so one accidental frame does not quit the exercise."""
    if is_open_palm_stop(hand_results):
        return stop_frames + 1
    return 0


def draw_palm_stop_status(image, stop_frames, required_frames=PALM_STOP_REQUIRED_FRAMES):
    """Draw palm-stop progress while an exercise is active."""
    h, w = image.shape[:2]
    compact = h <= 240
    panel_w = 180 if compact else 205
    panel_h = 45 if compact else 53
    x = max(5, w - panel_w - 5)
    y = h - panel_h - 5
    draw_glass_panel(image, x, y, panel_w, panel_h, alpha=0.65, radius=10)

    progress = min(stop_frames / required_frames, 1.0)
    color = ACCENT_GREEN if progress >= 1.0 else ACCENT_ORANGE if stop_frames else TEXT_DIM
    cv2.putText(image, "PALM STOP", (x + 8, y + 17),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38 if compact else 0.45,
                color, 1, cv2.LINE_AA)

    label = "STOPPING" if progress >= 1.0 else "HOLD PALM" if stop_frames else "SHOW PALM"
    cv2.putText(image, label, (x + 8, y + 36),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38 if compact else 0.45,
                TEXT_WHITE, 1, cv2.LINE_AA)

    bar_x = x + (95 if compact else 108)
    bar_y = y + (29 if compact else 34)
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
    scale_w = int(w * WINDOW_SCALE)
    scale_h = int(h * WINDOW_SCALE)
    display = cv2.resize(image, (scale_w, scale_h))
    cv2.imshow(window_name, display)
    cv2.waitKey(500)


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
    """Log an error to the session errors set and print it with a cooldown."""
    global session_errors, last_printed_errors
    session_errors.add(err_msg)
    now = time.time()
    if err_msg not in last_printed_errors or now - last_printed_errors[err_msg] > cooldown:
        print(f"  [SESSION WARNING] {err_msg}")
        last_printed_errors[err_msg] = now

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


# ═══════════════════════════════════════════════════════
#  EXERCISE MODES
# ═══════════════════════════════════════════════════════

def run_bicep_curls():
    """Bicep curl counter with form detection for both arms."""
    clear_session_errors()

    def process_arm(shoulder, elbow, wrist, side_name, state_dict, image, img_w, img_h):
        if shoulder is None or elbow is None or wrist is None:
            return "N/A", ACCENT_RED, "---", False

        angle = calculate_angle(shoulder, elbow, wrist)

        swing_error = False
        if angle < 135:
            if abs(wrist[0] - elbow[0]) > 0.08:
                swing_error = True

        flare_error = abs(shoulder[0] - elbow[0]) > 0.1
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
    cv2.namedWindow('Bicep Curls', cv2.WINDOW_NORMAL)
    cv2.resizeWindow('Bicep Curls', 1280, 720)

    print("🏋️ Bicep Curl Counter Active! Press Q to quit.")

    landmarker = create_pose_landmarker(det_conf=0.7, track_conf=0.7)
    hand_landmarker = create_hand_landmarker()
    timestamp_ms = 0
    hand_timestamp_ms = 0
    palm_stop_frames = 0

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            frame = cv2.flip(frame, 1)
            img_h, img_w, _ = frame.shape
            image = frame

            results, timestamp_ms = detect_pose_frame(landmarker, image, timestamp_ms)
            if hand_landmarker is not None:
                hand_results, hand_timestamp_ms = detect_hand_frame(
                    hand_landmarker, image, hand_timestamp_ms
                )
                palm_stop_frames = update_palm_stop_frames(hand_results, palm_stop_frames)
            else:
                palm_stop_frames = 0

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
                    swing_warning_active_until = now + 1.5
                    if now - last_swing_warning_time > 3.0:
                        log_session_error("Keep hands in straight position without swinging")
                        last_swing_warning_time = now

                if (arms['left']['counter'] > prev_l_cnt) or (arms['right']['counter'] > prev_r_cnt):
                    curl_timestamps.append(now)
                    if set_finished:
                        current_set += 1
                        left_reps_at_set_start = prev_l_cnt
                        right_reps_at_set_start = prev_r_cnt
                        set_finished = False
                    last_rep_time = now

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

            if hand_landmarker is not None:
                draw_palm_stop_status(image, palm_stop_frames)
                if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                    show_palm_stop_message('Bicep Curls', image)
                    break

            cv2.imshow('Bicep Curls', image)
            if cv2.waitKey(10) & 0xFF == ord('q'):
                break
    finally:
        if 'landmarker' in locals() and landmarker is not None:
            landmarker.close()
        if 'hand_landmarker' in locals() and hand_landmarker is not None:
            hand_landmarker.close()
        print_session_error_summary()

    cap.release()
    cv2.destroyAllWindows()
    print(f"\n✅ Bicep Curls Done! Left: {arms['left']['counter']} | Right: {arms['right']['counter']}")


def run_pushups():
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

        return stage, counter, is_body_straight, elbow_angle

    pushup_counter = 0
    pushup_stage = 'up'
    current_set = 1
    reps_in_set = 0
    set_ended = False

    cap = cv2.VideoCapture(CAMERA_INDEX)
    print("💪 Push-up Counter Active! Press Q to quit.")

    landmarker = create_pose_landmarker(det_conf=0.5, track_conf=0.5)
    hand_landmarker = create_hand_landmarker()
    timestamp_ms = 0
    hand_timestamp_ms = 0
    palm_stop_frames = 0

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            frame = cv2.flip(frame, 1)
            image = frame
            results, timestamp_ms = detect_pose_frame(landmarker, image, timestamp_ms)
            if hand_landmarker is not None:
                hand_results, hand_timestamp_ms = detect_hand_frame(
                    hand_landmarker, image, hand_timestamp_ms
                )
                palm_stop_frames = update_palm_stop_frames(hand_results, palm_stop_frames)
            else:
                palm_stop_frames = 0

            is_horizontal = False
            is_body_straight = True
            elbow_angle = 180.0

            try:
                if results.pose_landmarks and len(results.pose_landmarks) > 0:
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

            if hand_landmarker is not None:
                draw_palm_stop_status(image, palm_stop_frames)
                if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                    show_palm_stop_message('Push-ups', image)
                    break

            cv2.imshow('Push-ups', image)

            if cv2.waitKey(10) & 0xFF == ord('q'):
                break
    finally:
        if 'landmarker' in locals() and landmarker is not None:
            landmarker.close()
        if 'hand_landmarker' in locals() and hand_landmarker is not None:
            hand_landmarker.close()

    cap.release()
    cv2.destroyAllWindows()
    print(f"\n✅ Push-ups Done! Total: {pushup_counter}")


def run_squats():
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
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    print("🦵 Squat Counter Active! Press Q to quit.")

    landmarker = create_pose_landmarker(det_conf=0.5, track_conf=0.5)
    hand_landmarker = create_hand_landmarker()
    timestamp_ms = 0
    hand_timestamp_ms = 0
    palm_stop_frames = 0

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            image = frame
            results, timestamp_ms = detect_pose_frame(landmarker, image, timestamp_ms)
            if hand_landmarker is not None:
                hand_results, hand_timestamp_ms = detect_hand_frame(
                    hand_landmarker, image, hand_timestamp_ms
                )
                palm_stop_frames = update_palm_stop_frames(hand_results, palm_stop_frames)
            else:
                palm_stop_frames = 0

            current_hip_angle = 180
            posture_correct = True

            # Posture warnings
            feet_warning = None
            arms_warning = None
            depth_warning = None
            knees_warning = None

            try:
                if results.pose_landmarks and len(results.pose_landmarks) > 0:
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

                    draw_skeleton(image, results)
            except Exception:
                pass

            # --- Set transition / Countdown timer logic ---
            now_time = time.time()
            if set_started and last_rep_time is not None:
                time_elapsed = now_time - last_rep_time
                time_limit = 15.0 if reps_in_set >= 6 else 20.0

                if time_elapsed > time_limit:
                    print(f"\n✅ Set {current_set} Finished! Completed {reps_in_set} reps.")
                    current_set += 1
                    reps_in_set = 0
                    set_started = False
                    last_rep_time = None

            img_h, img_w = image.shape[:2]

            # Main stats panel
            draw_glass_panel(image, 5, 5, 280, 115, alpha=0.65, radius=10)
            accent = ACCENT_GREEN if posture_correct else ACCENT_RED
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

            if warnings:
                panel_h = 24 + len(warnings) * 20
                panel_y = img_h - panel_h - 15
                draw_glass_panel(image, 10, panel_y, img_w - 20, panel_h, alpha=0.75, color=(20, 20, 40), radius=8)
                cv2.rectangle(image, (10, panel_y), (img_w - 10, panel_y + panel_h), ACCENT_RED, 1)
                for idx, warn in enumerate(warnings):
                    cv2.putText(image, warn, (20, panel_y + 18 + idx * 20),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (80, 80, 240), 1, cv2.LINE_AA)

            if hand_landmarker is not None:
                draw_palm_stop_status(image, palm_stop_frames)
                if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                    show_palm_stop_message('Squats', image)
                    break

            cv2.imshow('Squats', image)

            if cv2.waitKey(5) & 0xFF == ord('q'):
                break
    finally:
        if 'landmarker' in locals() and landmarker is not None:
            landmarker.close()
        if 'hand_landmarker' in locals() and hand_landmarker is not None:
            hand_landmarker.close()
        print_session_error_summary()

    cap.release()
    cv2.destroyAllWindows()
    print(f"\n✅ Squats Done! Total Reps: {counter}")


def run_plank():
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
    print("🧘 Plank Timer Active! Press Q to quit.")

    landmarker = create_pose_landmarker(det_conf=0.5, track_conf=0.5)
    hand_landmarker = create_hand_landmarker()
    timestamp_ms = 0
    hand_timestamp_ms = 0
    palm_stop_frames = 0

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            image = frame
            results, timestamp_ms = detect_pose_frame(landmarker, image, timestamp_ms)
            if hand_landmarker is not None:
                hand_results, hand_timestamp_ms = detect_hand_frame(
                    hand_landmarker, image, hand_timestamp_ms
                )
                palm_stop_frames = update_palm_stop_frames(hand_results, palm_stop_frames)
            else:
                palm_stop_frames = 0

            is_horizontal = False
            is_looking_around = False

            try:
                if results.pose_landmarks and len(results.pose_landmarks) > 0:
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

                            if not in_plank:
                                log_session_error("Form a straight line with body")
                            if is_looking_around:
                                log_session_error("Look in front of you")
                        else:
                            in_plank = False
                            start_time = None
            except Exception:
                pass

            draw_skeleton(image, results)

            ring_cx, ring_cy, ring_r = 55, 88, 38
            draw_glass_panel(image, 5, 5, 230, ring_r * 2 + 50,
                             alpha=0.65, radius=10)

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

            if hand_landmarker is not None:
                draw_palm_stop_status(image, palm_stop_frames)
                if palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                    show_palm_stop_message('Plank', image)
                    break

            cv2.imshow('Plank', image)

            if cv2.waitKey(10) & 0xFF == ord('q'):
                break
    finally:
        if 'landmarker' in locals() and landmarker is not None:
            landmarker.close()
        if 'hand_landmarker' in locals() and hand_landmarker is not None:
            hand_landmarker.close()
        print_session_error_summary()

    cap.release()
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
        landmarker.close()
        sock.close()
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

    fps          = 0
    fps_counter  = 0
    fps_time     = time.time()
    frame_counter = 0

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

        # Draw
        if last_results:
            draw_skeleton(frame, last_results)

        draw_frame(frame, command, state, person_x,
                   shoulder_width, fps, d_zone, h_zone)

        if SHOW_WINDOW:
            scale_w = int(CAMERA_WIDTH  * WINDOW_SCALE)
            scale_h = int(CAMERA_HEIGHT * WINDOW_SCALE)
            display = cv2.resize(frame, (scale_w, scale_h))
            cv2.imshow('Person Follower', display)

            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break

        # FPS
        fps_counter += 1
        if time.time() - fps_time >= 1.0:
            fps         = fps_counter
            fps_counter = 0
            fps_time    = time.time()

    # Cleanup
    send_command("STOP")
    time.sleep(0.2)
    cap.release()
    cv2.destroyAllWindows()
    landmarker.close()
    sock.close()
    print("\n✅ Done!")


def show_mode_menu():
    print("\n═══════════════════════════════════════════")
    print("  FITNESS PROJECT - Mode Selection")
    print("═══════════════════════════════════════════")
    print("  1. Person Following (Robot)")
    print("  2. Bicep Curls")
    print("  3. Push-ups")
    print("  4. Squats")
    print("  5. Plank")
    print("  6. Exit / Q to Quit the session")
    print("═══════════════════════════════════════════")
    return input("  Enter choice (1-6, Q): ").strip().lower()


if __name__ == "__main__":
    while True:
        choice = show_mode_menu()

        if choice == "1":
            main()
        elif choice == "2":
            run_bicep_curls()
        elif choice == "3":
            run_pushups()
        elif choice == "4":
            run_squats()
        elif choice == "5":
            run_plank()
        elif choice in ("6", "q"):
            print("  Exiting.")
            break
        else:
            print("  Invalid choice. Please try again.")
