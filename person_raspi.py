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


def calculate_distance(a, b):
    """Calculate Euclidean distance between two points."""
    return np.linalg.norm(np.array(a) - np.array(b))


def check_plank(shoulder, hip, ankle, threshold=0.1):
    """Check if body is in plank position by alignment of shoulder-hip-ankle."""
    shoulder_hip_distance = calculate_distance(shoulder, hip)
    hip_ankle_distance = calculate_distance(hip, ankle)
    return shoulder_hip_distance < threshold and hip_ankle_distance < threshold


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
        ("FAR",   "LEFT"):   ("FWD_LEFT",  f"↖"),
        ("FAR",   "CENTER"): ("FORWARD",   f"↑"),
        ("FAR",   "RIGHT"):  ("FWD_RIGHT", f"↗"),
        ("GOOD",  "LEFT"):   ("LEFT",      f"←"),
        ("GOOD",  "CENTER"): ("STOP",      f"●"),
        ("GOOD",  "RIGHT"):  ("RIGHT",     f"→"),
        ("CLOSE", "LEFT"):   ("BWD_LEFT",  f"↙"),
        ("CLOSE", "CENTER"): ("BACKWARD",  f"↓"),
        ("CLOSE", "RIGHT"):  ("BWD_RIGHT", f"↘"),
    }

    command, arrow = matrix.get((d_zone, h_zone), ("STOP", "●"))
    state = (f"{arrow} {command} "
             f"(x:{person_x:.2f} w:{shoulder_width:.3f})")

    return command, state, d_zone, h_zone


# ═══════════════════════════════════════════════════════
#  DRAWING
# ═══════════════════════════════════════════════════════

CMD_COLORS = {
    'FORWARD':   (0, 255, 0),
    'BACKWARD':  (0, 0, 255),
    'LEFT':      (0, 255, 255),
    'RIGHT':     (255, 0, 255),
    'FWD_LEFT':  (0, 255, 128),
    'FWD_RIGHT': (128, 255, 0),
    'BWD_LEFT':  (128, 0, 255),
    'BWD_RIGHT': (255, 0, 128),
    'STOP':      (128, 128, 128),
    'SEARCH':    (128, 128, 255),
}

ACCENT_GREEN  = (72, 245, 66)
ACCENT_RED    = (66, 66, 245)
ACCENT_ORANGE = (66, 165, 245)
ACCENT_CYAN   = (245, 210, 66)
TEXT_WHITE     = (230, 230, 230)
TEXT_DIM       = (160, 160, 160)
PANEL_BG      = (20, 20, 20)

SKELETON_LANDMARK = (200, 220, 245)
SKELETON_LINE     = (180, 180, 60)


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


def draw_direction_indicator(frame, command, cx, cy, size=40):
    """Draw compass rose direction indicator."""
    pad = size + 10
    draw_glass_panel(frame, cx - pad, cy - pad, pad * 2, pad * 2,
                     alpha=0.7, radius=pad)

    cv2.circle(frame, (cx, cy), size + 3, (60, 60, 60), 1)

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
        cv2.circle(frame, pos, 4, (50, 50, 50), -1)

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
        ["↖FL", "↑FW", "↗FR"],
        ["←L",  "●ST", "→R"],
        ["↙BL", "↓BW", "↘BR"],
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

            cv2.rectangle(frame, (x1, y1), (x2, y2), (50, 50, 50), 1)
            cv2.putText(frame, labels[row][col],
                        (x1 + 3, y1 + cell_h - 6),
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
    cv2.rectangle(ov, (0, 0), (zone_l, h), (255, 0, 0), -1)
    cv2.addWeighted(ov, 0.08, frame, 0.92, 0, frame)

    ov = frame.copy()
    cv2.rectangle(ov, (zone_r, 0), (w, h), (0, 0, 255), -1)
    cv2.addWeighted(ov, 0.08, frame, 0.92, 0, frame)

    cv2.line(frame, (zone_l, 0), (zone_l, h), (255, 255, 0), 1)
    cv2.line(frame, (zone_r, 0), (zone_r, h), (255, 255, 0), 1)

    # Person position dot
    if person_x is not None:
        px = int(person_x * w)
        cv2.circle(frame, (px, h // 2), 14, (0, 255, 255), 1)
        cv2.circle(frame, (px, h // 2), 8, (0, 255, 255), -1)
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
#  EXERCISE MODES
# ═══════════════════════════════════════════════════════

def run_bicep_curls():
    """Bicep curl counter with form detection for both arms."""
    def process_arm(shoulder, elbow, wrist, side_name, state_dict, image, img_w, img_h):
        if shoulder is None or elbow is None or wrist is None:
            return "N/A", ACCENT_RED, "---"

        angle = calculate_angle(shoulder, elbow, wrist)

        flare_error = abs(shoulder[0] - elbow[0]) > 0.1
        color = ACCENT_GREEN
        feedback = "GOOD"

        if flare_error:
            feedback = "TUCK ELBOW"
            color = ACCENT_ORANGE

        if angle > 150:
            state_dict['stage'] = "down"
        if angle < 30 and state_dict['stage'] == "down":
            state_dict['stage'] = "up"
            if not flare_error:
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

        return feedback, color, state_dict['stage'].upper()

    arms = {
        'left': {'counter': 0, 'stage': 'down'},
        'right': {'counter': 0, 'stage': 'down'}
    }

    cap = cv2.VideoCapture(CAMERA_INDEX)
    cv2.namedWindow('Bicep Curls', cv2.WINDOW_NORMAL)
    cv2.resizeWindow('Bicep Curls', 1280, 720)

    print("🏋️ Bicep Curl Counter Active! Press Q to quit.")

    landmarker = create_pose_landmarker(det_conf=0.7, track_conf=0.7)
    timestamp_ms = 0

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            frame = cv2.flip(frame, 1)
            img_h, img_w, _ = frame.shape
            image = frame

            results, timestamp_ms = detect_pose_frame(landmarker, image, timestamp_ms)

            if results.pose_landmarks and len(results.pose_landmarks) > 0:
                lm = results.pose_landmarks[0]

                l_s, l_e, l_w = get_pt(lm, 11), get_pt(lm, 13), get_pt(lm, 15)
                r_s, r_e, r_w = get_pt(lm, 12), get_pt(lm, 14), get_pt(lm, 16)

                l_fb, l_clr, l_stg = process_arm(l_s, l_e, l_w, "Left", arms['left'], image, img_w, img_h)
                r_fb, r_clr, r_stg = process_arm(r_s, r_e, r_w, "Right", arms['right'], image, img_w, img_h)

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

            cv2.imshow('Bicep Curls', image)
            if cv2.waitKey(10) & 0xFF == ord('q'):
                break
    finally:
        landmarker.close()

    cap.release()
    cv2.destroyAllWindows()
    print(f"\n✅ Bicep Curls Done! Left: {arms['left']['counter']} | Right: {arms['right']['counter']}")


def run_pushups():
    """Push-up counter using nose-to-shoulder distance."""
    def count_pushups(nose, shoulders, stage, counter):
        left, right = np.array(shoulders[0]), np.array(shoulders[1])
        shoulder_mid = (left + right) / 2.0
        nose = np.array(nose)
        nose_shoulder_dist = np.linalg.norm(nose - shoulder_mid)

        if nose_shoulder_dist < 0.2 and stage == 'down':
            stage = 'up'
        if nose_shoulder_dist > 0.4 and stage == 'up':
            stage = 'down'
            counter += 1
            print(f"  Push-ups Count: {counter}")

        return stage, counter

    pushup_counter = 0
    pushup_stage = 'up'

    cap = cv2.VideoCapture(CAMERA_INDEX)
    print("💪 Push-up Counter Active! Press Q to quit.")

    landmarker = create_pose_landmarker(det_conf=0.5, track_conf=0.5)
    timestamp_ms = 0

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            frame = cv2.flip(frame, 1)
            image = frame
            results, timestamp_ms = detect_pose_frame(landmarker, image, timestamp_ms)

            try:
                if results.pose_landmarks and len(results.pose_landmarks) > 0:
                    lm = results.pose_landmarks[0]
                    nose = get_pt(lm, 0)
                    left_shoulder = get_pt(lm, 11)
                    right_shoulder = get_pt(lm, 12)
                    if nose and left_shoulder and right_shoulder:
                        pushup_stage, pushup_counter = count_pushups(
                            nose, [left_shoulder, right_shoulder],
                            pushup_stage, pushup_counter
                        )
            except Exception:
                pass

            draw_skeleton(image, results)

            draw_glass_panel(image, 5, 5, 220, 90, alpha=0.65, radius=10)
            cv2.putText(image, "PUSH-UPS", (15, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_DIM, 1)
            stage_clr = ACCENT_GREEN if pushup_stage == 'up' else ACCENT_CYAN
            cv2.putText(image, str(pushup_counter), (15, 68),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, TEXT_WHITE, 2, cv2.LINE_AA)
            cv2.putText(image, pushup_stage.upper(), (100, 68),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, stage_clr, 2, cv2.LINE_AA)
            cv2.putText(image, "press Q to quit", (15, 88),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.3, TEXT_DIM, 1)

            cv2.imshow('Push-ups', image)

            if cv2.waitKey(10) & 0xFF == ord('q'):
                break
    finally:
        landmarker.close()

    cap.release()
    cv2.destroyAllWindows()
    print(f"\n✅ Push-ups Done! Total: {pushup_counter}")


def run_squats():
    """Squat counter using 3D hip angle."""
    counter = 0
    stage = "up"
    SQUAT_DOWN_ANGLE = 110
    SQUAT_UP_ANGLE = 160
    down_angle = SQUAT_DOWN_ANGLE

    cap = cv2.VideoCapture(CAMERA_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    print("🦵 Squat Counter Active! Press Q to quit.")

    landmarker = create_pose_landmarker(det_conf=0.5, track_conf=0.5)
    timestamp_ms = 0

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            image = frame
            results, timestamp_ms = detect_pose_frame(landmarker, image, timestamp_ms)

            current_hip_angle = 180
            posture_correct = False

            try:
                if results.pose_landmarks and len(results.pose_landmarks) > 0:
                    lm = results.pose_landmarks[0]

                    L_shoulder = get_pt(lm, 11, use_z=True)
                    L_hip = get_pt(lm, 23, use_z=True)
                    L_knee = get_pt(lm, 25, use_z=True)
                    R_shoulder = get_pt(lm, 12, use_z=True)
                    R_hip = get_pt(lm, 24, use_z=True)
                    R_knee = get_pt(lm, 26, use_z=True)

                    if all([L_shoulder, L_hip, L_knee, R_shoulder, R_hip, R_knee]):
                        L_hip_angle = calculate_angle_3d(L_shoulder, L_hip, L_knee)
                        R_hip_angle = calculate_angle_3d(R_shoulder, R_hip, R_knee)

                        current_hip_angle = min(L_hip_angle, R_hip_angle)

                        if current_hip_angle < SQUAT_DOWN_ANGLE:
                            stage = "down"
                            if down_angle > current_hip_angle:
                                down_angle = current_hip_angle

                        if current_hip_angle > SQUAT_UP_ANGLE and stage == 'down':
                            stage = "up"
                            if down_angle > 80:
                                counter += 1
                                print(f"  Rep {counter} completed! Angle: {int(down_angle)}")
                            down_angle = SQUAT_DOWN_ANGLE

                        posture_correct = stage == "up"

                    draw_skeleton(image, results)
            except Exception:
                pass

            img_h, img_w = image.shape[:2]

            # Main stats panel
            draw_glass_panel(image, 5, 5, 250, 85, alpha=0.65, radius=10)
            accent = ACCENT_GREEN if posture_correct else ACCENT_RED
            cv2.rectangle(image, (5, 5), (9, 90), accent, -1)
            cv2.putText(image, "SQUATS", (18, 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_DIM, 1, cv2.LINE_AA)
            cv2.putText(image, str(counter), (18, 65),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.2, TEXT_WHITE, 2, cv2.LINE_AA)
            cv2.putText(image, "reps", (80, 65),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, TEXT_DIM, 1, cv2.LINE_AA)
            cv2.putText(image, stage.upper(), (140, 65),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, accent, 2, cv2.LINE_AA)

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

            cv2.imshow('Squats', image)

            if cv2.waitKey(5) & 0xFF == ord('q'):
                break
    finally:
        landmarker.close()

    cap.release()
    cv2.destroyAllWindows()
    print(f"\n✅ Squats Done! Total Reps: {counter}")


def run_plank():
    """Plank timer measuring hold duration."""
    plank_timer = 0
    in_plank = False
    start_time = None

    def count_plank_time(shoulder, hip, ankle, in_plank_state, plank_start):
        if check_plank(shoulder, hip, ankle):
            if not in_plank_state:
                plank_start = time.time()
                in_plank_state = True
            else:
                elapsed_time = time.time() - plank_start
                return in_plank_state, plank_start, elapsed_time
        else:
            in_plank_state = False
            plank_start = None
        return in_plank_state, plank_start, 0

    cap = cv2.VideoCapture(CAMERA_INDEX)
    print("🧘 Plank Timer Active! Press Q to quit.")

    landmarker = create_pose_landmarker(det_conf=0.5, track_conf=0.5)
    timestamp_ms = 0

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            image = frame
            results, timestamp_ms = detect_pose_frame(landmarker, image, timestamp_ms)

            try:
                if results.pose_landmarks and len(results.pose_landmarks) > 0:
                    lm = results.pose_landmarks[0]
                    left_shoulder = get_pt(lm, 11)
                    left_hip = get_pt(lm, 23)
                    left_ankle = get_pt(lm, 27)
                    if left_shoulder and left_hip and left_ankle:
                        in_plank, start_time, elapsed_time = count_plank_time(
                            left_shoulder, left_hip, left_ankle, in_plank, start_time
                        )
                        plank_timer += elapsed_time
            except Exception:
                pass

            draw_skeleton(image, results)

            ring_cx, ring_cy, ring_r = 55, 60, 38
            draw_glass_panel(image, 5, 5, 230, ring_r * 2 + 20,
                             alpha=0.65, radius=10)

            state_color = ACCENT_GREEN if in_plank else ACCENT_ORANGE
            state_text = "HOLD" if in_plank else "GET IN POSITION"

            cv2.putText(image, "PLANK", (15, 24),
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

            cv2.putText(image, state_text, (ring_cx + ring_r + 12, 55),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, state_color, 1, cv2.LINE_AA)
            cv2.putText(image, f"Total: {plank_timer:.1f}s",
                        (ring_cx + ring_r + 12, 78),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, TEXT_WHITE, 1, cv2.LINE_AA)

            cv2.imshow('Plank', image)

            if cv2.waitKey(10) & 0xFF == ord('q'):
                break
    finally:
        landmarker.close()

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


if __name__ == "__main__":
    print("\n═══════════════════════════════════════════")
    print("  FITNESS PROJECT - Mode Selection")
    print("═══════════════════════════════════════════")
    print("  1. Person Following (Robot)")
    print("  2. Bicep Curls")
    print("  3. Push-ups")
    print("  4. Squats")
    print("  5. Plank")
    print("═══════════════════════════════════════════")
    choice = input("  Enter choice (1-5): ").strip()

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
    else:
        print("  Invalid choice. Exiting.")