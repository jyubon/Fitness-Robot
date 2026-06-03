import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['GLOG_minloglevel'] = '3'

import cv2
import mediapipe as mp
import numpy as np
from google import genai
from google.genai import types
import serial
import json
import time

from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision

# 1. Initialize the new client with your real API key
client = genai.Client(api_key="AIzaSyC_omTzQj25mB84B9ay1ENgNnoq1xo4Pro")

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

            if cv2.waitKey(10) & 0xFF == ord('q'):
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

            if cv2.waitKey(5) & 0xFF == ord('q'):
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

            if cv2.waitKey(10) & 0xFF == ord('q'):
                break
    finally:
        landmarker.close()

    cap.release()
    cv2.destroyAllWindows()
    summary = f"Plank completed. Total time: {plank_timer:.2f} seconds."
    print(f"\n✅ {summary}")
    return summary

# Configuration
config = types.GenerateContentConfig(
    system_instruction=(
        "You are the fitness robot. Map user input to hardware tools. "
        "You can perform several exercises (bicep curls, pushups, squats, plank) and provide feedback. "
        "Use the tools to trigger movements, start exercise sessions, or send alerts based on user commands."
    ),
    temperature=0.7,
    tools=[
        trigger_robot_movement,
        send_watch_alert,
        send_motor_command,
        run_bicep_curls,
        run_pushups,
        run_squats,
        run_plank
    ],
)

# Start the chat using the new client
chat = client.chats.create(
    model="gemini-2.5-flash-lite", # Updated to the latest fast model
    config=config
)

# Terminal loop
print("=" * 50)
print("Fitness Robot Terminal Controller Activated.")
print("=" * 50)

while True:
    user_command = input("\nYou: ")
    if user_command.strip().lower() == 'exit':
        break
    if not user_command.strip():
        continue
        
    # Send the message
    response = chat.send_message(user_command)
    
    # In the new SDK, function calls are handled differently, 
    # but the chat.send_message still processes the text return.
    if response.text:
        print(f"Robot: {response.text}")