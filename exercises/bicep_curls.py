#!/usr/bin/env python3
"""
Bicep Curls counter and posture tracker.
Evaluates both arms, detecting flaring elbows, body swinging, and fast rep cadence.
"""

import cv2
import time
import numpy as np

from config import (
    ACCENT_GREEN,
    ACCENT_RED,
    ACCENT_ORANGE,
    TEXT_WHITE,
    TEXT_DIM,
)
from exercise_base import ExerciseRunner
from pose_utils import calculate_angle, get_pt
from ui_overlays import draw_glass_panel, draw_skeleton
from session_manager import publish_rep, publish_set_complete, log_session_error, session_errors


class BicepCurlsRunner(ExerciseRunner):
    """Bicep curls runner subclass."""

    def __init__(self, ui=None):
        super().__init__(
            exercise_title="Bicep Curls",
            exercise_key="bicep_curls",
            view_position="FRONT",
            ui=ui
        )
        self.arms = {
            'left': {'counter': 0, 'stage': 'down'},
            'right': {'counter': 0, 'stage': 'down'}
        }
        self.curl_timestamps = []
        self.warning_active_until = 0.0
        self.last_console_warning_time = 0.0
        self.swing_warning_active_until = 0.0
        self.last_swing_warning_time = 0.0

        self.workout_state = "DOING_SET"
        self.last_rep_time = None
        self.left_reps_at_set_start = 0
        self.right_reps_at_set_start = 0
        self.last_uneven_warning_time = 0.0
        self.set_finished = False

    def on_resume_adjust_timing(self, pause_duration):
        if self.last_rep_time is not None:
            self.last_rep_time += pause_duration
        if self.last_uneven_warning_time is not None:
            self.last_uneven_warning_time += pause_duration
        self.warning_active_until += pause_duration
        self.swing_warning_active_until += pause_duration

    def on_set_increment_while_lost(self):
        self.left_reps_at_set_start = self.arms['left']['counter']
        self.right_reps_at_set_start = self.arms['right']['counter']
        self.set_finished = False

    def process_arm(self, shoulder, elbow, wrist, side_name, state_dict, image, img_w, img_h):
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

        draw_glass_panel(image, bar_x - 3, bar_y_top - 18, 20, bar_h + 30, alpha=0.6, radius=6)
        cv2.putText(image, side_name[0], (bar_x, bar_y_top - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, TEXT_DIM, 1)
        cv2.rectangle(image, (bar_x, bar_y_top), (bar_x + 14, bar_y_top + bar_h), (40, 40, 40), -1)
        cv2.rectangle(image, (bar_x, bar_y_top + bar_h - fill_h), (bar_x + 14, bar_y_top + bar_h), color, -1)
        cv2.rectangle(image, (bar_x, bar_y_top), (bar_x + 14, bar_y_top + bar_h), (60, 60, 60), 1)

        return feedback, color, state_dict['stage'].upper(), swing_error

    def process_exercise_frame(self, image, results, lm, img_w, img_h):
        now = time.time()
        cur_l = self.arms['left']['counter'] - self.left_reps_at_set_start
        cur_r = self.arms['right']['counter'] - self.right_reps_at_set_start
        self.reps_in_set = min(cur_l, cur_r)

        if self.workout_state == "DOING_SET":
            if self.reps_in_set >= 8 and not self.set_finished:
                print(f"\n✅ Set {self.current_set} Finished! Please rest.")
                try:
                    publish_set_complete("bicep_curls", self.current_set, self.reps_in_set)
                except Exception:
                    pass
                self.set_finished = True
                if self.current_set == 2:
                    self.trigger_mid_workout_rest()
                    self.left_reps_at_set_start = self.arms['left']['counter']
                    self.right_reps_at_set_start = self.arms['right']['counter']
                    self.set_finished = False

        is_uneven = (cur_l != cur_r) and (max(cur_l, cur_r) > 0)
        if is_uneven and self.workout_state == "DOING_SET":
            if now - self.last_uneven_warning_time > 5.0:
                log_session_error(f"Equalize both hands (Left: {cur_l} | Right: {cur_r})")
                self.last_uneven_warning_time = now

        # MediaPipe swapped for camera mirroring
        l_s, l_e, l_w = get_pt(lm, 12), get_pt(lm, 14), get_pt(lm, 16)
        r_s, r_e, r_w = get_pt(lm, 11), get_pt(lm, 13), get_pt(lm, 15)

        prev_l = self.arms['left']['counter']
        prev_r = self.arms['right']['counter']
        prev_min = min(prev_l, prev_r)

        if self.workout_state != "DOING_SET" or self.is_mid_workout_rest:
            l_fb, l_clr, l_stg, l_swing = "REST", ACCENT_GREEN, "REST", False
            r_fb, r_clr, r_stg, r_swing = "REST", ACCENT_GREEN, "REST", False
        else:
            l_fb, l_clr, l_stg, l_swing = self.process_arm(l_s, l_e, l_w, "Left", self.arms['left'], image, img_w, img_h)
            r_fb, r_clr, r_stg, r_swing = self.process_arm(r_s, r_e, r_w, "Right", self.arms['right'], image, img_w, img_h)

        if l_swing or r_swing:
            self.swing_warning_active_until = now + 0.5
            if now - self.last_swing_warning_time > 3.0:
                log_session_error("Keep hands in straight position without swinging")
                self.last_swing_warning_time = now

        if not self.is_mid_workout_rest and ((self.arms['left']['counter'] > prev_l) or (self.arms['right']['counter'] > prev_r)):
            self.curl_timestamps.append(now)
            if self.set_finished:
                self.current_set += 1
                self.left_reps_at_set_start = prev_l
                self.right_reps_at_set_start = prev_r
                self.set_finished = False
            cur_l = max(0, self.arms['left']['counter'] - self.left_reps_at_set_start)
            cur_r = max(0, self.arms['right']['counter'] - self.right_reps_at_set_start)
            self.reps_in_set = min(cur_l, cur_r)
            self.last_rep_time = now
            total_curls = min(self.arms['left']['counter'], self.arms['right']['counter'])
            is_full = total_curls > prev_min
            try:
                publish_rep(
                    "bicep_curls",
                    set_num=self.current_set,
                    left=self.arms['left']['counter'],
                    right=self.arms['right']['counter'],
                    count=total_curls,
                    left_in_set=cur_l,
                    right_in_set=cur_r,
                    in_set=self.reps_in_set,
                    is_full_rep=is_full
                )
            except Exception:
                pass

        self.curl_timestamps = [t for t in self.curl_timestamps if now - t <= 2.0]
        if len(self.curl_timestamps) >= 4:
            self.warning_active_until = now + 2.0
            if now - self.last_console_warning_time > 2.0:
                log_session_error("Slow down! Curling too fast")
                self.last_console_warning_time = now

        draw_skeleton(image, results)

        # Update stats & feedback on the UI
        if self.ui is not None:
            self.ui.update_stat_card("Set", self.current_set)
            self.ui.update_stat_card("Reps", self.reps_in_set)
            self.ui.update_stat_card("L Reps", self.arms['left']['counter'])
            self.ui.update_stat_card("R Reps", self.arms['right']['counter'])

            feedback_msgs = []
            feedback_color = ACCENT_GREEN

            if time.time() < self.warning_active_until:
                feedback_msgs.append("⚠️ WARNING: SLOW DOWN! Curling too fast.")
                feedback_color = ACCENT_RED
            if time.time() < self.swing_warning_active_until:
                feedback_msgs.append("⚠️ WARNING: DON'T SWING! Keep hands straight.")
                feedback_color = ACCENT_ORANGE
            if is_uneven and not self.set_finished:
                feedback_msgs.append(f"⚠️ Equalize both hands (Left: {cur_l} | Right: {cur_r})")
                feedback_color = ACCENT_ORANGE

            if l_fb != "GOOD" and l_fb != "---":
                feedback_msgs.append(f"⚠️ Left Arm: {l_fb}")
                feedback_color = ACCENT_ORANGE
            if r_fb != "GOOD" and r_fb != "---":
                feedback_msgs.append(f"⚠️ Right Arm: {r_fb}")
                feedback_color = ACCENT_ORANGE

            if not feedback_msgs:
                if self.set_finished:
                    feedback_msgs.append("✅ Set finished! Please rest.")
                else:
                    feedback_msgs.append("✅ Form is Good. Keep it up!")

            self.ui.update_feedback("\n".join(feedback_msgs), feedback_color)

    def process_missing_frame(self, image, img_w, img_h):
        log_session_error("Align horizontally GET IN POSITION.")
        if self.ui is not None:
            self.ui.update_feedback("⚠️ Positioning... Stand in front of camera.", ACCENT_ORANGE)

    def build_session_summary(self):
        return {
            'exercise': 'Bicep Curls',
            'total_reps': min(self.arms['left']['counter'], self.arms['right']['counter']),
            'metrics': [
                ('Left Reps', self.arms['left']['counter']),
                ('Right Reps', self.arms['right']['counter']),
                ('Total Sets', self.current_set)
            ],
            'errors': list(session_errors)
        }

    def on_finished(self):
        print(f"\n✅ Bicep Curls Done! Left: {self.arms['left']['counter']} | Right: {self.arms['right']['counter']}")


def run_bicep_curls(ui=None):
    """Convenience runner matching original function signature."""
    runner = BicepCurlsRunner(ui=ui)
    runner.run()
    return runner.last_session_summary
