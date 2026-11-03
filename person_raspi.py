#!/usr/bin/env python3
"""
8-Direction Person Following Robot
Raspberry Pi 4 + MediaPipe + ESP32 via UART Serial
Main Application Entry Point.
"""

import os
import sys
import time
import signal
import cv2
import mediapipe as mp

# ─── Configuration & Constants ───
from config import (
    PROJECT_DIR,
    SERIAL_PORT,
    BAUD_RATE,
    CAMERA_INDEX,
    CAMERA_WIDTH,
    CAMERA_HEIGHT,
    FRAME_SKIP,
    HAND_FRAME_SKIP,
    MODEL_PATH,
    HAND_MODEL_PATH,
    PALM_STOP_REQUIRED_FRAMES,
    PALM_STOP_MIN_HAND_SIZE,
    CENTER_ZONE_LEFT,
    CENTER_ZONE_RIGHT,
    TOO_CLOSE,
    TOO_FAR,
    COMMAND_COOLDOWN,
    LOST_PERSON_TIMEOUT,
    DISPLAY_WIDTH,
    DISPLAY_HEIGHT,
    SHOW_WINDOW,
    DEBUG,
    SHOW_FPS,
    BACKEND_BASE_URL,
    BACKEND_TOKEN,
    FIREBASE_USER_ID,
    FIREBASE_USER_EMAIL,
    ACCENT_GREEN,
    ACCENT_RED,
    ACCENT_ORANGE,
    ACCENT_CYAN,
    TEXT_WHITE,
    TEXT_DIM,
    PANEL_BG,
    SKELETON_LANDMARK,
    SKELETON_LINE,
    CMD_COLORS,
    DIRECTION_MATRIX,
)

# ─── Telemetry & Session Management ───
from session_manager import (
    db,
    force_stop,
    remote_mode_queue,
    start_exercise,
    exercise_executing,
    publish_event,
    publish_session_start,
    publish_session_end,
    publish_rep,
    publish_set_complete,
    publish_form_error,
    publish_robot_state,
    publish_plank_time,
    publish_pause,
    publish_resume,
    log_session_error,
    clear_session_errors,
    print_session_error_summary,
    save_session_to_firestore,
    session_errors,
)

# ─── Audio Management ───
from audio_manager import (
    play_audio_track,
    play_voice_command,
    terminate_current_audio,
)

# ─── Vosk Voice Control ───
from voice_control import (
    start_voice_listener,
    stop_voice_listener,
    get_voice_state,
)

# ─── Robot Control (ESP32 Serial) ───
from robot_control import (
    ser,
    send_command,
    calculate_command,
    adjust_robot_position,
    reset_tracking_state,
)

# ─── Pose & Hand Detection Utilities ───
from pose_utils import (
    create_pose_landmarker,
    create_hand_landmarker,
    detect_pose_frame,
    detect_hand_frame,
    get_pt,
    get_body_height,
    calculate_angle,
    calculate_angle_3d,
    calculate_lateral_deviation,
    calculate_distance,
    check_plank,
    is_open_palm_stop,
    update_palm_stop_frames,
    is_thumbs_up,
)

# ─── UI Overlays ───
from ui_overlays import (
    bgr_to_hex,
    draw_glass_panel,
    draw_home_button,
    draw_direction_indicator,
    draw_zone_grid,
    draw_frame,
    draw_skeleton,
    draw_follow_phase_overlay,
    draw_positioning_overlay,
    draw_hand_controls_status,
    show_palm_stop_message,
    draw_paused_overlay,
)

# ─── Exercise Base Runner ───
from exercise_base import ExerciseRunner

# ─── Workout Routines ───
from exercises import (
    BicepCurlsRunner, run_bicep_curls,
    PushupsRunner, run_pushups,
    SquatsRunner, run_squats,
    PlankRunner, run_plank,
)

# ─── GUI Widgets ───
from ui_widgets import (
    RoundedButton,
    OutlinedLabel,
    RoundedFrame,
    FitnessRobotUI,
)

import local_stream_server

# Global variables for backward compatibility
home_clicked = False
last_session_summary = None
last_person_side = None
lost_person_since = None
is_resting = False
person_missing = False


def on_mouse_click(event, x, y, flags, param):
    """Handle click on OpenCV HOME button (390-470, 5-45)."""
    global home_clicked
    if event == cv2.EVENT_LBUTTONDOWN:
        if 390 <= x <= 470 and 5 <= y <= 45:
            home_clicked = True


def main(ui=None):
    """8-Direction Person Following Routine."""
    global home_clicked, last_session_summary

    reset_tracking_state()
    exercise_executing.set()
    if ui is not None and hasattr(ui, 'hide_start_button'):
        try:
            ui.hide_start_button()
        except Exception:
            pass

    landmarker = create_pose_landmarker(det_conf=0.5, track_conf=0.5)
    timestamp_ms = 0
    print("✅ MediaPipe loaded")

    print("\nOpening camera...")
    if sys.platform.startswith("linux"):
        cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)
    else:
        cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        cap = cv2.VideoCapture(CAMERA_INDEX)

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    for _ in range(5):
        cap.read()

    aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"✅ Camera: {aw}x{ah}")

    def shutdown(sig, frame):
        print("\n🛑 Stopping...")
        stop_voice_listener()
        exercise_executing.clear()
        send_command("STOP")
        time.sleep(0.2)
        cap.release()
        cv2.destroyAllWindows()
        if landmarker is not None:
            try: landmarker.close()
            except Exception: pass
        if ser is not None:
            try: ser.close()
            except Exception: pass
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

    if SHOW_WINDOW and ui is None:
        cv2.namedWindow('Person Follower', cv2.WINDOW_NORMAL)
        cv2.resizeWindow('Person Follower', DISPLAY_WIDTH, DISPLAY_HEIGHT)
        home_clicked = False
        cv2.setMouseCallback('Person Follower', on_mouse_click)

    fps = 0
    fps_counter = 0
    fps_time = time.time()
    frame_counter = 0
    start_time = time.time()

    command = "STOP"
    state = "STARTING"
    person_x = None
    body_height = None
    d_zone = "GOOD"
    h_zone = "CENTER"
    last_results = None
    lost_since = None

    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        if force_stop.is_set() or (ui is not None and getattr(ui, 'home_clicked', False)):
            print("  [REMOTE/UI] Stop received")
            send_command("STOP")
            break

        frame_counter += 1

        if frame_counter % FRAME_SKIP == 0:
            results, timestamp_ms = detect_pose_frame(landmarker, frame, timestamp_ms)
            last_results = results

            person_x = None
            body_height = None

            if results.pose_landmarks and len(results.pose_landmarks) > 0:
                lm = results.pose_landmarks[0]
                l_shoulder = get_pt(lm, 11)
                r_shoulder = get_pt(lm, 12)
                l_hip = get_pt(lm, 23)
                r_hip = get_pt(lm, 24)
                nose = get_pt(lm, 0)

                if l_hip and r_hip:
                    person_x = (l_hip[0] + r_hip[0]) / 2
                elif l_shoulder and r_shoulder:
                    person_x = (l_shoulder[0] + r_shoulder[0]) / 2
                elif nose:
                    person_x = nose[0]
                else:
                    person_x = lm[0].x

                lost_since = None
                body_height = get_body_height(lm)
                command, state, d_zone, h_zone = calculate_command(person_x, body_height)
                send_command(command)

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
                now = time.time()
                if lost_since is None:
                    lost_since = now
                    send_command("STOP")
                    command = "STOP"
                    state = "LOST"
                elif now - lost_since > LOST_PERSON_TIMEOUT:
                    send_command("SEARCH")
                    command = "SEARCH"
                    state = "SEARCHING..."

                d_zone = "GOOD"
                h_zone = "CENTER"

            if DEBUG and SHOW_FPS and frame_counter % (30 * FRAME_SKIP) == 0:
                det = "👤" if person_x is not None else "❌"
                print(f"  {det} FPS:{fps:.0f} | CMD:{command:10s} | {state}")

        display = cv2.resize(frame, (DISPLAY_WIDTH, DISPLAY_HEIGHT))
        if last_results:
            draw_skeleton(display, last_results)

        draw_frame(display, command, state, person_x, body_height, fps, d_zone, h_zone)
        draw_home_button(display)

        local_stream_server.update_frame(display)
        if ui is not None:
            try:
                ui.update()
            except Exception:
                break
        elif SHOW_WINDOW:
            cv2.imshow('Person Follower', display)
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q') or home_clicked:
                break

        fps_counter += 1
        if time.time() - fps_time >= 1.0:
            fps = fps_counter
            fps_counter = 0
            fps_time = time.time()

    exercise_executing.clear()
    send_command("STOP")
    time.sleep(0.2)
    cap.release()
    if SHOW_WINDOW and ui is None:
        cv2.destroyAllWindows()
    if landmarker is not None:
        try: landmarker.close()
        except Exception: pass

    duration = time.time() - start_time
    last_session_summary = {
        'exercise': 'Person Follower',
        'metrics': [('Duration', f"{duration:.1f}s")],
        'errors': []
    }
    return last_session_summary


if __name__ == "__main__":
    try:
        local_stream_server.start_servers(
            force_stop_event=force_stop,
            remote_mode_queue=remote_mode_queue,
            start_exercise_event=start_exercise
        )
        print("Local HTTP/WS servers started successfully.")
    except Exception as e:
        print(f"Failed to start local servers: {e}")

    publish_robot_state("READY")

    # Start Vosk background voice listener
    try:
        start_voice_listener()
    except Exception as e:
        print(f"Failed to start voice listener: {e}")

    # Wire GUI callbacks
    callbacks = {
        "person_follow": main,
        "bicep_curls": run_bicep_curls,
        "pushups": run_pushups,
        "squats": run_squats,
        "plank": run_plank,
    }
    app = FitnessRobotUI(exercise_callbacks=callbacks)
    app.mainloop()