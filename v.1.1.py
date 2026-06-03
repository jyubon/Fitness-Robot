import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['GLOG_minloglevel'] = '3'

import cv2
import mediapipe as mp
import numpy as np
import sounddevice as sd
from scipy.io.wavfile import write
from google import genai
from google.genai import types
import serial
import json
import time
import threading
import queue

from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision
import api_keys

# 1. Initialize the new client with the API key from api_keys.py
client = genai.Client(api_key=api_keys.GEMINI_API_KEY)

# Initialize serial connection
SERIAL_PORT = '/dev/ttyS0'
BAUD_RATE = 115200

try:
    # Initialize the serial port
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    time.sleep(2) # Give the connection a moment to stabilize
    print(f"Successfully connected to {SERIAL_PORT}")
except Exception as e:
    print(f"Failed to connect to Serial Port: {e}")
    ser = None

# ─── Shared State for Voice-Driven Exercise Switching ───
stop_exercise = threading.Event()
command_queue = queue.Queue()

# ─── Configuration & Constants for Exercises ───
CAMERA_INDEX = 0
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(SCRIPT_DIR, "pose_landmarker.task")

ACCENT_GREEN  = (150, 215, 95)
ACCENT_RED    = (95, 95, 210)
ACCENT_ORANGE = (80, 175, 225)
ACCENT_CYAN   = (200, 185, 100)
TEXT_WHITE    = (235, 235, 240)
TEXT_DIM      = (155, 155, 165)

SKELETON_LANDMARK = (190, 210, 235)
SKELETON_LINE     = (170, 170, 90)

# Helper Functions for Exercises
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

# Tools Definitions

# The Hardware Command Function
def send_motor_command(keyword: str, speed: int):
    """
    Sends a command keyword and a speed value (0-255) to the ESP32-S3.
    """
    if not ser or not ser.is_open:
        print("[ERROR] Serial port is not open.")
        return

    # 1. Enforce the 0-255 limit
    # This prevents errors if the API accidentally passes a speed of 300 or -50
    safe_speed = max(0, min(255, int(speed)))
    
    # 2. Format the keyword to ensure it's always uppercase (FWD, BWD, etc.)
    safe_keyword = keyword.strip().upper()

    # 3. Create a JSON payload
    payload = {
        "cmd": safe_keyword,
        "spd": safe_speed
    }

    # 4. Convert to string and add the CRITICAL newline character (\n)
    # The \n tells the ESP32 that the message is complete
    message = json.dumps(payload) + '\n'

    # 5. Send it over the RX/TX wires
    try:
        ser.write(message.encode('utf-8'))
        ser.flush() # Ensure all data is pushed out of the buffer
        print(f"[TX] Sent command: {message.strip()}")
    except Exception as e:
        print(f"[TX ERROR] Failed to send data: {e}")

def trigger_robot_movement(animation_name: str):
    print(f"\n[HARDWARE COMMAND] Sending '{animation_name}' to ESP32-S3...")

def send_watch_alert(alert_type: str):
    print(f"\n[HARDWARE COMMAND] Sending '{alert_type}' to ESP32-C3...")

def run_bicep_curls() -> str:
    """
    Start the bicep curl counter session using the camera. 
    It tracks curls for both left and right arms, monitors form (e.g. elbow tucking),
    and displays a visual feedback screen. Returns a summary of reps completed.
    """
    def process_arm(shoulder, elbow, wrist, side_name, state_dict, image, img_w, img_h):
        if shoulder is None or elbow is None or wrist is None:
            return "N/A", ACCENT_RED, "---", False

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
                return feedback, color, state_dict['stage'].upper(), False
            else:
                return feedback, color, state_dict['stage'].upper(), True

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

        return feedback, color, state_dict['stage'].upper(), False

    arms = {
        'left': {'counter': 0, 'stage': 'down'},
        'right': {'counter': 0, 'stage': 'down'}
    }
    tuck_count = 0              # shared counter across both arms
    form_warning_until = 0      # timestamp until which to show warning on screen

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

                l_fb, l_clr, l_stg, l_bad = process_arm(l_s, l_e, l_w, "Left", arms['left'], image, img_w, img_h)
                r_fb, r_clr, r_stg, r_bad = process_arm(r_s, r_e, r_w, "Right", arms['right'], image, img_w, img_h)

                # Increment shared tuck counter if EITHER arm had a bad-form rep
                if l_bad or r_bad:
                    tuck_count += 1
                elif l_fb == "GOOD" and r_fb == "GOOD":
                    tuck_count = 0   # reset on a clean frame

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

                # After 3 bad-form reps from either arm, show warning
                if tuck_count >= 3:
                    form_warning_until = time.time() + 4
                    tuck_count = 0
                    print("\n⚠️  Gemini: Drop your weight and focus on your form!")

            # Show warning banner on screen
            if time.time() < form_warning_until:
                warn_msg = "Drop your weight and focus on your form!"
                draw_glass_panel(image, 5, img_h - 55, img_w - 10, 50, alpha=0.8, radius=10)
                cv2.putText(image, warn_msg, (15, img_h - 25),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, ACCENT_RED, 2, cv2.LINE_AA)

            cv2.imshow('Bicep Curls', image)
            if cv2.waitKey(10) & 0xFF == ord('q') or stop_exercise.is_set():
                break
    finally:
        landmarker.close()

    cap.release()
    cv2.destroyAllWindows()
    summary = f"Bicep curls completed. Final count - Left: {arms['left']['counter']} | Right: {arms['right']['counter']}"
    print(f"\n✅ {summary}")
    return summary

def run_pushups() -> str:
    """
    Start the push-up counter session using the camera.
    It tracks push-up repetitions based on nose-to-shoulder distance and displays feedback.
    Returns a summary of repetitions completed.
    """
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

            if cv2.waitKey(10) & 0xFF == ord('q') or stop_exercise.is_set():
                break
    finally:
        landmarker.close()

    cap.release()
    cv2.destroyAllWindows()
    summary = f"Push-ups completed. Total count: {pushup_counter}"
    print(f"\n✅ {summary}")
    return summary

def run_squats() -> str:
    """
    Start the squat counter session using the camera.
    It measures hip angle in 3D to count squats and check posture/depth.
    Returns a summary of repetitions completed.
    """
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

            if cv2.waitKey(5) & 0xFF == ord('q') or stop_exercise.is_set():
                break
    finally:
        landmarker.close()

    cap.release()
    cv2.destroyAllWindows()
    summary = f"Squats completed. Total reps: {counter}"
    print(f"\n✅ {summary}")
    return summary

def run_plank() -> str:
    """
    Start the plank timer session using the camera.
    It checks body alignment and measures hold duration for the plank exercise.
    Returns a summary of the hold time.
    """
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

            ring_cx, ring_cy, ring_r = 55, 88, 38
            draw_glass_panel(image, 5, 5, 230, ring_r * 2 + 50,
                             alpha=0.65, radius=10)

            state_color = ACCENT_GREEN if in_plank else ACCENT_ORANGE
            state_text = "HOLD" if in_plank else "GET IN POSITION"

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

            cv2.imshow('Plank', image)

            if cv2.waitKey(10) & 0xFF == ord('q') or stop_exercise.is_set():
                break
    finally:
        landmarker.close()

    cap.release()
    cv2.destroyAllWindows()
    summary = f"Plank completed. Total time: {plank_timer:.2f} seconds."
    print(f"\n✅ {summary}")
    return summary

# ─── Person Following / Tracking Helpers & Tool ───

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

def draw_frame(frame, command, state, person_x, shoulder_width, fps, d_zone, h_zone):
    """Draw all overlay elements."""
    h, w = frame.shape[:2]

    # Zone overlays
    CENTER_ZONE_LEFT  = 0.42
    CENTER_ZONE_RIGHT = 0.58
    TOO_CLOSE = 0.43
    TOO_FAR   = 0.18

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

def run_person_tracking() -> str:
    """
    Start the person tracking and following session using the camera.
    The robot will detect the user and send movement commands (FORWARD, BACKWARD, LEFT, RIGHT, etc.)
    to follow the user, keeping them at a calibrated distance.
    Returns a summary when the tracking session ends.
    """
    import socket
    
    # ─── ESP32 ───
    ESP32_IP   = "10.249.231.70"
    ESP32_PORT = 8888

    # ─── Camera ───
    CAMERA_WIDTH  = 320
    CAMERA_HEIGHT = 240
    FRAME_SKIP    = 1

    # ─── Following Thresholds ───
    CENTER_ZONE_LEFT  = 0.42
    CENTER_ZONE_RIGHT = 0.58
    TOO_CLOSE = 0.43
    TOO_FAR   = 0.18

    # ─── Timing ───
    LOST_PERSON_TIMEOUT = 1.5

    # ─── Display & Debug ───
    SHOW_WINDOW  = True
    WINDOW_SCALE = 2.0
    DEBUG    = True
    SHOW_FPS = True

    # Initialize UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    
    state_dict = {
        'current_command': "STOP",
        'last_command_time': 0.0
    }

    def send_command(command):
        now = time.time()
        if (command != state_dict['current_command'] or
                now - state_dict['last_command_time'] > 1.0):
            try:
                sock.sendto(
                    command.encode('utf-8'),
                    (ESP32_IP, ESP32_PORT)
                )
                if DEBUG and command != state_dict['current_command']:
                    print(f"  📡 → ESP32: {command}")
            except Exception:
                pass
            state_dict['current_command'] = command
            state_dict['last_command_time'] = now

    def calculate_command(person_x, shoulder_width):
        if person_x < CENTER_ZONE_LEFT:
            h_zone = "LEFT"
        elif person_x > CENTER_ZONE_RIGHT:
            h_zone = "RIGHT"
        else:
            h_zone = "CENTER"

        if shoulder_width > TOO_CLOSE:
            d_zone = "CLOSE"
        elif shoulder_width < TOO_FAR:
            d_zone = "FAR"
        else:
            d_zone = "GOOD"

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
        state = f"{command} (x:{person_x:.2f} w:{shoulder_width:.3f})"
        return command, state, d_zone, h_zone

    # ─── MediaPipe Tasks API Setup ───
    landmarker = create_pose_landmarker(det_conf=0.5, track_conf=0.5)
    timestamp_ms = 0
    print("✅ MediaPipe loaded")

    # ─── Camera ───
    print("\nOpening camera...")
    cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap = cv2.VideoCapture(CAMERA_INDEX)

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    for _ in range(5):
        cap.read()

    aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"✅ Camera: {aw}x{ah}")

    print()
    print("🚀 8-Direction Person Following Active!")
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

    try:
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

                if results.pose_landmarks and len(results.pose_landmarks) > 0:
                    lm        = results.pose_landmarks[0]
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
                        shoulder_center = (l_shoulder.x + r_shoulder.x) / 2
                        shoulder_width = abs(l_shoulder.x - r_shoulder.x)

                        if hip_ok:
                            hip_center = (l_hip.x + r_hip.x) / 2
                            person_x = shoulder_center * 0.6 + hip_center * 0.4
                        else:
                            person_x = shoulder_center

                        lost_since = None
                        command, state, d_zone, h_zone = calculate_command(person_x, shoulder_width)
                        send_command(command)
                else:
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

                if (DEBUG and SHOW_FPS and
                        frame_counter % (30 * FRAME_SKIP) == 0):
                    det = "👤" if person_x is not None else "❌"
                    print(f"  {det} FPS:{fps:.0f} | CMD:{command:10s} | {state}")

            if last_results:
                draw_skeleton(frame, last_results)

            draw_frame(frame, command, state, person_x, shoulder_width, fps, d_zone, h_zone)

            if SHOW_WINDOW:
                scale_w = int(CAMERA_WIDTH  * WINDOW_SCALE)
                scale_h = int(CAMERA_HEIGHT * WINDOW_SCALE)
                display = cv2.resize(frame, (scale_w, scale_h))
                cv2.imshow('Person Follower', display)

                key = cv2.waitKey(1) & 0xFF
                if key == ord('q') or stop_exercise.is_set():
                    break

            fps_counter += 1
            if time.time() - fps_time >= 1.0:
                fps         = fps_counter
                fps_counter = 0
                fps_time    = time.time()
    finally:
        send_command("STOP")
        time.sleep(0.2)
        cap.release()
        cv2.destroyAllWindows()
        landmarker.close()
        sock.close()

    print("\n✅ Done!")
    return "Person tracking session completed."

def calibrate_mic(sample_rate=16000, duration=1.0):
    """
    Record ambient noise for `duration` seconds and return the average RMS.
    This is used to auto-set the speech detection threshold.
    """
    print("🔇 Calibrating microphone... Please stay quiet for 1 second.")
    audio = sd.rec(int(duration * sample_rate), samplerate=sample_rate, channels=1, dtype='float32')
    sd.wait()
    ambient_rms = float(np.sqrt(np.mean(audio**2)))
    print(f"   Ambient noise RMS: {ambient_rms:.5f}")
    return ambient_rms


def record_voice_command(filename="voice_command.wav", sample_rate=16000,
                         threshold=0.03, silence_seconds=1.8, max_duration=6.0):
    """
    Listens for speech on the default microphone.
    - Waits until audio RMS exceeds `threshold` (speech start).
    - Records until `silence_seconds` of quiet OR `max_duration` is reached.
    - Saves the recording to `filename` and returns the path.
    """
    print("\n🎧 Listening... Speak your command now.")

    chunk_size = 1024
    chunks_per_sec = sample_rate / chunk_size
    max_silent_chunks = int(silence_seconds * chunks_per_sec)
    max_total_chunks  = int(max_duration * chunks_per_sec)

    audio_buffer = []
    recording = False
    silent_chunks = 0
    total_chunks  = 0

    with sd.InputStream(samplerate=sample_rate, channels=1,
                        blocksize=chunk_size, dtype='float32') as stream:
        while True:
            data, _ = stream.read(chunk_size)
            rms = float(np.sqrt(np.mean(data**2)))

            if not recording:
                # Waiting for speech to start
                if rms > threshold:
                    print("🎙️  Speech detected! Recording...")
                    recording = True
                    audio_buffer.extend(data.flatten())
                    silent_chunks = 0
                    total_chunks = 1
            else:
                audio_buffer.extend(data.flatten())
                total_chunks += 1

                if rms < threshold:
                    silent_chunks += 1
                else:
                    silent_chunks = 0

                # Stop conditions
                if silent_chunks >= max_silent_chunks:
                    print("✅ End of speech detected.")
                    break
                if total_chunks >= max_total_chunks:
                    print(f"✅ Max recording time ({max_duration}s) reached.")
                    break

    # Convert float32 buffer → 16-bit WAV
    audio_np = np.array(audio_buffer, dtype='float32')
    audio_int16 = (audio_np * 32767).astype(np.int16)
    write(filename, sample_rate, audio_int16)
    duration_sec = len(audio_np) / sample_rate
    print(f"   Recorded {duration_sec:.1f}s of audio.")
    return filename


# ═══════════════════════════════════════════════════════
#  EXERCISE DISPATCH MAP
# ═══════════════════════════════════════════════════════

EXERCISE_MAP = {
    "BICEP_CURLS":     run_bicep_curls,
    "PUSHUPS":         run_pushups,
    "SQUATS":          run_squats,
    "PLANK":           run_plank,
    "PERSON_TRACKING": run_person_tracking,
}

EXERCISE_KEYWORDS = ", ".join(EXERCISE_MAP.keys())


# ═══════════════════════════════════════════════════════
#  GEMINI CONFIGURATION
# ═══════════════════════════════════════════════════════

config = types.GenerateContentConfig(
    system_instruction=(
        "You are the fitness robot voice controller. "
        "When the user speaks, determine what they want to do. "
        f"If they want to start or switch to an exercise, reply with EXACTLY one of these keywords on its own line: {EXERCISE_KEYWORDS}. "
        "If they want to stop or exit, reply with STOP. "
        "You may add a short motivational sentence AFTER the keyword. "
        "For hardware commands (motor, movement, watch alerts), use the provided tools. "
        "Keep replies very short."
    ),
    temperature=0.3,
    tools=[
        trigger_robot_movement,
        send_watch_alert,
        send_motor_command,
    ],
)

chat = client.chats.create(
    model="gemini-2.5-flash-lite",
    config=config
)


# ═══════════════════════════════════════════════════════
#  VOICE LISTENER (Background Thread)
# ═══════════════════════════════════════════════════════

def voice_listener_thread(speech_threshold):
    """
    Runs in a daemon thread. Waits for the user to press Enter,
    then records a voice command, sends it to Gemini, and dispatches.
    """
    while True:
        try:
            input("\n🎤 Press ENTER to give a voice command...")

            wav_file = record_voice_command(
                threshold=speech_threshold,
                silence_seconds=1.8,
                max_duration=6.0
            )

            print("⏳ Uploading voice to Gemini...")
            gemini_audio_file = client.files.upload(file=wav_file)

            print("🧠 Processing voice command...")
            response = chat.send_message([
                gemini_audio_file,
                "Listen to this voice command. If they want an exercise or tracking, "
                f"respond with the keyword ({EXERCISE_KEYWORDS} or STOP). "
                "Add a short motivational line after."
            ])

            # Cleanup temp audio
            if os.path.exists(wav_file):
                os.remove(wav_file)

            if not response.text:
                continue

            reply = response.text.strip()
            print(f"\n🤖 Robot: {reply}")

            # Check if Gemini returned an exercise keyword
            reply_upper = reply.upper()
            for keyword in EXERCISE_MAP:
                if keyword in reply_upper:
                    print(f"\n🔄 Switching to: {keyword}")
                    stop_exercise.set()          # signal current exercise to stop
                    # Drain any old commands
                    while not command_queue.empty():
                        try:
                            command_queue.get_nowait()
                        except queue.Empty:
                            break
                    command_queue.put(keyword)    # queue the new exercise
                    break

            # Check for STOP command
            if "STOP" in reply_upper and not any(k in reply_upper for k in EXERCISE_MAP):
                print("\n⏹️  Stop requested.")
                stop_exercise.set()
                command_queue.put("STOP")

        except Exception as e:
            print(f"\n[VOICE ERROR]: {e}")
            time.sleep(2)


# ═══════════════════════════════════════════════════════
#  MAIN (Calibrate → Start Listener → Dispatch Exercises)
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    # Calibrate microphone
    ambient_rms = calibrate_mic()
    SPEECH_THRESHOLD = max(ambient_rms * 2.5, 0.01)
    print(f"✅ Speech threshold set to: {SPEECH_THRESHOLD:.5f}")

    # Start voice listener as a daemon background thread
    listener = threading.Thread(
        target=voice_listener_thread,
        args=(SPEECH_THRESHOLD,),
        daemon=True
    )
    listener.start()
    print("✅ Voice listener started (background thread)")

    print()
    print("=" * 60)
    print("  Fitness Robot — Voice Controller")
    print("  Press ENTER to speak a command (even mid-exercise).")
    print("  Say 'stop' to idle, press Ctrl+C to exit.")
    print("=" * 60)

    try:
        while True:
            print("\n⏸️  Idle — waiting for voice command...")
            # Block until the voice listener queues a command
            cmd = command_queue.get()

            if cmd == "STOP":
                continue

            if cmd in EXERCISE_MAP:
                stop_exercise.clear()  # reset the stop signal
                exercise_fn = EXERCISE_MAP[cmd]
                print(f"\n▶️  Starting {cmd}...")
                try:
                    result = exercise_fn()
                    print(f"📊 {result}")
                except Exception as e:
                    print(f"[EXERCISE ERROR]: {e}")

    except KeyboardInterrupt:
        stop_exercise.set()
        print("\n\n🛑 Shutting down. Goodbye!")