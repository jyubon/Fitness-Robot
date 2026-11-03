#!/usr/bin/env python3
"""
Base Exercise Runner for Fitness Robot.
Encapsulates camera handling, gesture recognition, pause/resume, rest timers,
session tracking, and display lifecycle shared across all workout routines.
"""

import cv2
import time
import local_stream_server

from config import (
    CAMERA_INDEX,
    CAMERA_WIDTH,
    CAMERA_HEIGHT,
    DISPLAY_WIDTH,
    DISPLAY_HEIGHT,
    FRAME_SKIP,
    HAND_FRAME_SKIP,
    PALM_STOP_REQUIRED_FRAMES,
    ACCENT_CYAN,
)
from audio_manager import play_audio_track
from session_manager import (
    publish_robot_state,
    publish_session_start,
    publish_session_end,
    publish_set_complete,
    publish_pause,
    publish_resume,
    add_pause_time,
    clear_session_errors,
    print_session_error_summary,
    save_session_to_firestore,
    force_stop,
    start_exercise,
)
from robot_control import send_command, adjust_robot_position
from pose_utils import (
    create_pose_landmarker,
    create_hand_landmarker,
    detect_pose_frame,
    detect_hand_frame,
    update_palm_stop_frames,
    is_thumbs_up,
    get_pt,
    get_body_height,
)
from ui_overlays import (
    draw_skeleton,
    draw_follow_phase_overlay,
    draw_paused_overlay,
    draw_hand_controls_status,
    show_palm_stop_message,
    draw_home_button,
)


class ExerciseRunner:
    """
    Unified base loop for exercises.
    Eliminates redundant camera capture, gesture polling, and window management.
    """

    def __init__(self, exercise_title, exercise_key, view_position="FRONT", ui=None):
        self.exercise_title = exercise_title
        self.exercise_key = exercise_key
        self.view_position = view_position
        self.ui = ui

        # Home click tracking
        self.home_clicked = False

        # Loop and gesture states
        self.paused = False
        self.pause_start_time = 0.0
        self.thumbs_up_frames = 0
        self.thumbs_up_cooldown_active = False
        self.palm_stop_frames = 0

        # Timing & Counters
        self.frame_counter = 0
        self.timestamp_ms = 0
        self.hand_timestamp_ms = 0
        self.last_results = None
        self.last_hand_results = None

        # Workout Set & Rest Tracking
        self.current_set = 1
        self.reps_in_set = 0
        self.is_mid_workout_rest = False
        self.rest_start_time = None
        self.lost_start_time = None
        self.set_incremented_while_lost = False
        self.waiting_for_start = True

        self.last_session_summary = None

    def on_mouse_click(self, event, x, y, flags, param):
        """Handle click on OpenCV HOME button (390-470, 5-45)."""
        if event == cv2.EVENT_LBUTTONDOWN:
            if 390 <= x <= 470 and 5 <= y <= 45:
                self.home_clicked = True

    def trigger_mid_workout_rest(self):
        """Trigger standard 30-second mid-workout rest break."""
        play_audio_track(13)
        self.is_mid_workout_rest = True
        self.rest_start_time = time.time()
        self.current_set = 3
        self.reps_in_set = 0

        if self.ui is not None:
            self.ui.is_resting = True
            if hasattr(self.ui, 'set_count_warning_label'):
                self.ui.set_count_warning_label.configure(text="Rest for a While.", fg="#96D75F")

    def _check_rest_timer(self):
        """Update and check countdown for mid-workout rest."""
        if not self.is_mid_workout_rest or self.rest_start_time is None:
            return

        elapsed_rest = time.time() - self.rest_start_time
        if self.ui is not None and hasattr(self.ui, 'set_count_warning_label'):
            remaining = max(0, int(30.0 - elapsed_rest))
            self.ui.set_count_warning_label.configure(
                text=f"Rest for a While. Resuming in {remaining}s.",
                fg="#96D75F"
            )

        if elapsed_rest > 30.0:
            play_audio_track(15)
            self.is_mid_workout_rest = False
            if self.ui is not None:
                self.ui.is_resting = False
                if hasattr(self.ui, 'set_count_warning_label'):
                    self.ui.set_count_warning_label.configure(text="")
            print(f"  [REST OVER] Resuming {self.exercise_title}.")

    def _handle_person_lost(self):
        """Handle person-lost timeout (60s exit, 20s set increment)."""
        now = time.time()
        if self.lost_start_time is None:
            self.lost_start_time = now

        elapsed_lost = now - self.lost_start_time
        if elapsed_lost > 60.0:
            print(f"  [TIMEOUT] Person lost for >1m during {self.exercise_title}.")
            return False

        if elapsed_lost > 20.0 and not self.set_incremented_while_lost:
            if not self.is_mid_workout_rest and self.reps_in_set > 6:
                try:
                    publish_set_complete(self.exercise_key, self.current_set, self.reps_in_set)
                except Exception:
                    pass
                self.current_set += 1
                self.reps_in_set = 0
                self.set_incremented_while_lost = True
                self.on_set_increment_while_lost()

        return True

    def on_set_increment_while_lost(self):
        """Hook for exercise subclasses when set increments while person lost."""
        pass

    def run(self):
        """Execute exercise session."""
        clear_session_errors()
        self.home_clicked = False

        cap = cv2.VideoCapture(CAMERA_INDEX)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)

        window_name = self.exercise_title
        if self.ui is None:
            cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(window_name, DISPLAY_WIDTH, DISPLAY_HEIGHT)
            cv2.setMouseCallback(window_name, self.on_mouse_click)

        print(f"🏋️ {self.exercise_title} Active! Press Q or Palm-Stop to exit.")
        publish_robot_state("LOADING", exercise=self.exercise_key)

        landmarker = create_pose_landmarker(det_conf=0.7, track_conf=0.7)
        hand_landmarker = create_hand_landmarker()

        session_started = False

        try:
            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break

                if force_stop.is_set():
                    print("  [REMOTE] Force Stop received from web UI")
                    send_command("STOP")
                    break

                self.frame_counter += 1
                frame = cv2.flip(frame, 1)
                image = cv2.resize(frame, (DISPLAY_WIDTH, DISPLAY_HEIGHT))
                img_h, img_w, _ = image.shape

                # 1. Rest countdown check
                self._check_rest_timer()

                # 2. Pose detection (skip frames for performance)
                if self.frame_counter % FRAME_SKIP == 0 or self.last_results is None:
                    results, self.timestamp_ms = detect_pose_frame(landmarker, frame, self.timestamp_ms)
                    self.last_results = results
                else:
                    results = self.last_results

                # 3. Waiting for start phase (following user)
                if self.waiting_for_start:
                    if self.home_clicked or (self.ui is not None and getattr(self.ui, 'home_clicked', False)):
                        break
                    if start_exercise.is_set():
                        play_audio_track(14)
                        send_command("STOP")
                        self.waiting_for_start = False
                        start_exercise.clear()
                        if not session_started:
                            publish_session_start(self.exercise_key)
                            session_started = True
                        publish_robot_state("ACTIVE", exercise=self.exercise_key)
                    else:
                        if results and results.pose_landmarks:
                            draw_skeleton(image, results)
                            robot_status = adjust_robot_position(
                                self.exercise_key.upper(), results,
                                in_exercise_mode=False,
                                get_pt_fn=get_pt,
                                get_body_height_fn=get_body_height
                            )
                        else:
                            robot_status = adjust_robot_position(
                                self.exercise_key.upper(), None,
                                in_exercise_mode=False,
                                get_pt_fn=get_pt,
                                get_body_height_fn=get_body_height
                            )

                        if robot_status == "LOST_TIMEOUT":
                            break

                        draw_follow_phase_overlay(image, self.view_position)
                        local_stream_server.update_frame(image)

                        if self.ui is not None:
                            self.ui.update_feedback("🤖 ROBOT FOLLOWING...\nGet in Position and Press the Start", ACCENT_CYAN)
                            self.ui.update()
                        else:
                            img_copy = image.copy()
                            draw_home_button(img_copy)
                            cv2.imshow(window_name, img_copy)
                            if cv2.waitKey(10) & 0xFF == ord('q') or self.home_clicked:
                                break
                        continue

                # 4. Hand detection (gestures: thumbs-up pause, open palm stop)
                if hand_landmarker is not None:
                    if self.frame_counter % HAND_FRAME_SKIP == 0 or self.last_hand_results is None:
                        hand_results, self.hand_timestamp_ms = detect_hand_frame(
                            hand_landmarker, frame, self.hand_timestamp_ms
                        )
                        self.last_hand_results = hand_results

                        if is_thumbs_up(hand_results):
                            if not self.thumbs_up_cooldown_active:
                                self.thumbs_up_frames += HAND_FRAME_SKIP
                                if self.thumbs_up_frames >= PALM_STOP_REQUIRED_FRAMES:
                                    self.paused = not self.paused
                                    self.thumbs_up_frames = 0
                                    self.thumbs_up_cooldown_active = True
                                    if self.paused:
                                        self.pause_start_time = time.time()
                                        publish_pause()
                                    else:
                                        p_dur = time.time() - self.pause_start_time
                                        add_pause_time(p_dur)
                                        publish_resume()
                                        self.on_resume_adjust_timing(p_dur)
                        else:
                            self.thumbs_up_frames = 0
                            self.thumbs_up_cooldown_active = False

                        self.palm_stop_frames = update_palm_stop_frames(
                            hand_results, self.palm_stop_frames, increment=HAND_FRAME_SKIP
                        )
                    else:
                        hand_results = self.last_hand_results
                else:
                    hand_results = None

                # 5. Paused Overlay
                if self.paused:
                    send_command("STOP")
                    if results and results.pose_landmarks:
                        draw_skeleton(image, results)
                    draw_paused_overlay(image, self.thumbs_up_frames)
                    if hand_landmarker is not None:
                        draw_hand_controls_status(image, self.thumbs_up_frames, self.palm_stop_frames, is_paused=True)
                    local_stream_server.update_frame(image)

                    if self.ui is not None:
                        self.ui.update_feedback("⏸️ WORKOUT PAUSED\nShow Thumbs-Up to Resume", ACCENT_CYAN)
                        self.ui.update()
                        if getattr(self.ui, 'home_clicked', False):
                            break
                    else:
                        if hand_landmarker is not None and self.palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                            show_palm_stop_message(window_name, image)
                            break
                        img_copy = image.copy()
                        draw_home_button(img_copy)
                        cv2.imshow(window_name, img_copy)
                        if cv2.waitKey(10) & 0xFF == ord('q') or self.home_clicked:
                            break
                    continue

                # 6. Check person visibility
                has_landmarks = results and results.pose_landmarks and len(results.pose_landmarks) > 0
                if has_landmarks:
                    self.lost_start_time = None
                    self.set_incremented_while_lost = False
                    lm = results.pose_landmarks[0]
                    # Execute subclass-specific posture & counting logic
                    self.process_exercise_frame(image, results, lm, img_w, img_h)
                else:
                    if not self._handle_person_lost():
                        break
                    self.process_missing_frame(image, img_w, img_h)

                # 7. Render controls & handle frame exit
                if hand_landmarker is not None:
                    draw_hand_controls_status(image, self.thumbs_up_frames, self.palm_stop_frames, is_paused=False)

                local_stream_server.update_frame(image)

                if self.ui is None:
                    if hand_landmarker is not None and self.palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                        show_palm_stop_message(window_name, image)
                        break
                    img_copy = image.copy()
                    draw_home_button(img_copy)
                    cv2.imshow(window_name, img_copy)
                    if cv2.waitKey(10) & 0xFF == ord('q') or self.home_clicked:
                        break
                else:
                    if hand_landmarker is not None and self.palm_stop_frames >= PALM_STOP_REQUIRED_FRAMES:
                        break
                    if self.home_clicked or getattr(self.ui, 'home_clicked', False):
                        break
                    self.ui.update()

        finally:
            send_command("STOP")
            if landmarker is not None:
                try: landmarker.close()
                except Exception: pass
            if hand_landmarker is not None:
                try: hand_landmarker.close()
                except Exception: pass

            summary = self.build_session_summary()
            self.last_session_summary = summary
            print_session_error_summary()
            save_session_to_firestore(summary)
            try:
                publish_session_end(summary)
            except Exception:
                pass

            cap.release()
            if self.ui is None:
                cv2.destroyAllWindows()

            self.on_finished()

    # Subclass hooks
    def on_resume_adjust_timing(self, pause_duration):
        """Override to adjust internal timers when unpaused."""
        pass

    def process_exercise_frame(self, image, results, lm, img_w, img_h):
        """Must be implemented by exercise subclass."""
        raise NotImplementedError

    def process_missing_frame(self, image, img_w, img_h):
        """Optional hook when person is not in frame."""
        pass

    def build_session_summary(self):
        """Build dict for last_session_summary."""
        raise NotImplementedError

    def on_finished(self):
        """Called upon loop exit."""
        pass
