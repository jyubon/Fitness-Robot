#!/usr/bin/env python3
"""
MediaPipe Pose & Hand Utilities for Fitness Robot.
Handles landmarker initializations, geometric calculations, and gesture recognition.
"""

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision

from config import (
    MODEL_PATH,
    HAND_MODEL_PATH,
    PALM_STOP_MIN_HAND_SIZE,
)


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
    norm_ba = np.linalg.norm(ba)
    norm_bc = np.linalg.norm(bc)
    if norm_ba == 0 or norm_bc == 0:
        return 0.0
    cosine_angle = np.dot(ba, bc) / (norm_ba * norm_bc)
    angle = np.arccos(np.clip(cosine_angle, -1.0, 1.0))
    return float(np.degrees(angle))


def calculate_lateral_deviation(hip, knee, ankle):
    """Calculate the 2D lateral deviation angle of the knee relative to hip-ankle line."""
    v_ha = np.array([ankle[0] - hip[0], ankle[1] - hip[1]])
    v_hk = np.array([knee[0] - hip[0], knee[1] - hip[1]])
    norm_ha = np.linalg.norm(v_ha)
    norm_hk = np.linalg.norm(v_hk)
    if norm_ha == 0 or norm_hk == 0:
        return 0.0
    cos_angle = np.dot(v_ha, v_hk) / (norm_ha * norm_hk)
    angle = np.degrees(np.arccos(np.clip(cos_angle, -1.0, 1.0)))
    return float(angle)


def calculate_distance(a, b):
    """Calculate Euclidean distance between two points."""
    return float(np.linalg.norm(np.array(a) - np.array(b)))


def check_plank(shoulder, hip, ankle, min_angle=165.0):
    """Check if shoulder-hip-ankle is close to a straight line (165-180 deg)."""
    angle = calculate_angle(shoulder, hip, ankle)
    return angle >= min_angle


def create_pose_landmarker(det_conf=0.5, track_conf=0.5):
    """Create MediaPipe PoseLandmarker in VIDEO mode."""
    base_options = mp_tasks.BaseOptions(model_asset_path=MODEL_PATH)
    options = vision.PoseLandmarkerOptions(
        base_options=base_options,
        running_mode=vision.RunningMode.VIDEO,
        min_pose_detection_confidence=det_conf,
        min_pose_presence_confidence=det_conf,
        min_tracking_confidence=track_conf,
        num_poses=1
    )
    return vision.PoseLandmarker.create_from_options(options)


def create_hand_landmarker(det_conf=0.6, track_conf=0.6):
    """Create MediaPipe HandLandmarker in VIDEO mode."""
    try:
        base_options = mp_tasks.BaseOptions(model_asset_path=HAND_MODEL_PATH)
        options = vision.HandLandmarkerOptions(
            base_options=base_options,
            running_mode=vision.RunningMode.VIDEO,
            min_hand_detection_confidence=det_conf,
            min_hand_presence_confidence=det_conf,
            min_tracking_confidence=track_conf,
            num_hands=2
        )
        return vision.HandLandmarker.create_from_options(options)
    except Exception as e:
        print(f"⚠️ Warning: Could not initialize hand landmarker: {e}")
        print("   Palm stop feature will be disabled.")
        return None


def detect_pose_frame(landmarker, frame_bgr, timestamp_ms):
    """Run pose detection on BGR frame."""
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
    timestamp_ms += 33
    results = landmarker.detect_for_video(mp_image, timestamp_ms)
    return results, timestamp_ms


def detect_hand_frame(hand_landmarker, frame_bgr, timestamp_ms):
    """Run hand detection on BGR frame."""
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
    timestamp_ms += 33
    results = hand_landmarker.detect_for_video(mp_image, timestamp_ms)
    return results, timestamp_ms


def get_pt(lm, idx, min_vis=0.5, use_z=False):
    """Get landmark [x,y] or [x,y,z] if visible, else None."""
    p = lm[idx]
    if p.visibility <= min_vis:
        return None
    return [p.x, p.y, p.z] if use_z else [p.x, p.y]


def get_body_height(lm):
    """Calculate normalized vertical body height with scale normalization."""
    nose = get_pt(lm, 0)
    l_shoulder = get_pt(lm, 11)
    r_shoulder = get_pt(lm, 12)
    l_hip = get_pt(lm, 23)
    r_hip = get_pt(lm, 24)
    l_ankle = get_pt(lm, 27)
    r_ankle = get_pt(lm, 28)

    if nose:
        top_y = nose[1]
        top_type = "NOSE"
    elif l_shoulder and r_shoulder:
        top_y = (l_shoulder[1] + r_shoulder[1]) / 2
        top_type = "SHOULDER"
    else:
        top_y = lm[0].y
        top_type = "NOSE"

    if l_ankle and r_ankle:
        bottom_y = (l_ankle[1] + r_ankle[1]) / 2
        bottom_type = "ANKLE"
    elif l_hip and r_hip:
        bottom_y = (l_hip[1] + r_hip[1]) / 2
        bottom_type = "HIP"
    else:
        bottom_y = max(lm[27].y, lm[28].y) if len(lm) > 28 else lm[0].y
        bottom_type = "ANKLE"

    raw_height = max(0.0, bottom_y - top_y)

    if top_type == "NOSE" and bottom_type == "ANKLE":
        scale_factor = 1.0
    elif top_type == "NOSE" and bottom_type == "HIP":
        scale_factor = 2.0
    elif top_type == "SHOULDER" and bottom_type == "ANKLE":
        scale_factor = 1.15
    elif top_type == "SHOULDER" and bottom_type == "HIP":
        scale_factor = 2.76
    else:
        scale_factor = 1.0

    return raw_height * scale_factor


def _hand_point(hand_landmarks, idx):
    pt = hand_landmarks[idx]
    return np.array([pt.x, pt.y])


def is_open_palm_stop(hand_results, min_hand_size=PALM_STOP_MIN_HAND_SIZE):
    """Return True when MediaPipe Hands sees a clear open palm facing camera."""
    if not hand_results or not hand_results.hand_landmarks:
        return False

    for idx, hand_landmarks in enumerate(hand_results.hand_landmarks):
        points = [_hand_point(hand_landmarks, idx_pt) for idx_pt in range(21)]
        xs = [pt[0] for pt in points]
        ys = [pt[1] for pt in points]
        hand_size = max(max(xs) - min(xs), max(ys) - min(ys))
        if hand_size < min_hand_size:
            continue

        hand_label = "Right"
        try:
            if hand_results.handedness and idx < len(hand_results.handedness):
                hand_label = hand_results.handedness[idx][0].category_name
        except (AttributeError, IndexError):
            pass

        v1 = points[9] - points[0]
        v2 = points[17] - points[5]
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
            (5, 6, 8),
            (9, 10, 12),
            (13, 14, 16),
            (17, 18, 20),
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

        dist_thumb_index = calculate_distance(points[4], points[8])
        dist_index_middle = calculate_distance(points[8], points[12])
        dist_middle_ring = calculate_distance(points[12], points[16])
        dist_ring_pinky = calculate_distance(points[16], points[20])

        fingers_spread = (
            dist_index_middle > hand_size * 0.12 and
            dist_middle_ring > hand_size * 0.12 and
            dist_ring_pinky > hand_size * 0.12 and
            dist_thumb_index > hand_size * 0.50
        )

        if extended >= 4 and thumb_extended and is_palm_facing and fingers_spread:
            return True

    return False


def update_palm_stop_frames(hand_results, stop_frames, increment=1):
    """Debounce palm stop frames."""
    if is_open_palm_stop(hand_results):
        return stop_frames + increment
    return 0


def is_thumbs_up(hand_results, min_hand_size=PALM_STOP_MIN_HAND_SIZE):
    """Return True when MediaPipe Hands sees a clear thumbs-up gesture."""
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
            (5, 6, 8),
            (9, 10, 12),
            (13, 14, 16),
            (17, 18, 20),
        ]
        extended = 0
        for mcp_idx, pip_idx, tip_idx in fingers:
            mcp = points[mcp_idx]
            pip = points[pip_idx]
            tip = points[tip_idx]
            if (calculate_distance(tip, wrist) > calculate_distance(pip, wrist) * 1.08 and
                    calculate_distance(tip, mcp) > calculate_distance(pip, mcp)):
                extended += 1

        if extended > 1:
            continue

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
