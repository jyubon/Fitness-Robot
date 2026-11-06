#!/usr/bin/env python3
"""
Squats counter and posture tracker.
Monitors feet stance width, arm forward extension, squat depth, and knee lateral deviation.
Includes session start positioning voice sequence, 4-rep positioning evaluation,
and 2-rep depth form checks.
"""

import time

from config import (
    ACCENT_GREEN,
    ACCENT_RED,
    ACCENT_ORANGE,
    ACCENT_CYAN,
)
from exercise_base import ExerciseRunner
from pose_utils import (
    calculate_angle_3d,
    calculate_lateral_deviation,
    get_pt,
)
from ui_overlays import draw_skeleton
from session_manager import publish_rep, publish_set_complete, log_session_error, session_errors
from audio_manager import play_audio_sequence, play_command_audio


class SquatsRunner(ExerciseRunner):
    """Squats runner subclass."""

    SQUAT_DOWN_ANGLE = 110
    SQUAT_UP_ANGLE = 160

    def __init__(self, ui=None):
        super().__init__(
            exercise_title="Squats",
            exercise_key="squats",
            view_position="FRONT",
            ui=ui
        )
        self.counter = 0
        self.stage = "up"
        self.down_angle = self.SQUAT_DOWN_ANGLE
        self.set_finished = False
        self.last_rep_time = None
        self.workout_state = "DOING_SET"
        self.set_started = False

        self.up_shoulder_y = None
        self.up_hip_y = None
        self.up_wrist_y = None
        self.up_knee_y = None

        self.lowered_shoulders = False
        self.lowered_hips = False
        self.lowered_wrists = False
        self.lowered_knees = False
        self.current_hip_angle = 180

        # Session Start Guidance State
        self.initial_instruction_active = False
        self.session_start_time = None

        # Requirement 1: Positioning Check after 4 reps
        self.rep_4_pos_checked = False
        self.current_stance_ratio = 1.0
        self.current_stance_ok = True
        self.stance_fault_count = 0
        self.stance_good_count = 0
        self.current_arms_ok = True
        self.arms_fault_count = 0
        self.arms_good_count = 0
        self.active_feet_warning = None
        self.active_feet_warning_until = 0.0
        self.active_arms_warning = None
        self.active_arms_warning_until = 0.0

        # Requirement 2: Depth check for each 2 reps
        self.depth_error_in_window = False
        self.min_knee_angle_in_window = 180.0
        self.last_depth_checked_rep = 0
        self.active_depth_warning = None
        self.active_depth_warning_until = 0.0

    def on_session_start(self):
        """
        Send posture instructions when starting squats session:
        1. Keep feet at shoulder distance (006)
        2. Extend arms directly in front of you (004)
        """
        print("📢 [SQUATS START] Sending setup voice instructions: Feet shoulder width + extend arms...")
        self.initial_instruction_active = True
        self.session_start_time = time.time()
        play_audio_sequence([
            "006_keep_feet_at_shoulder_distance.mp3",
            "004_extendarms_straight_and_directly_in_front_of_you.mp3"
        ], force=True)
        if self.ui is not None:
            self.ui.update_feedback("Keep feet at shoulder width & extend arms in front", ACCENT_CYAN)

    def _reset_set_checks(self):
        """Reset positioning and depth checks at the start of a set."""
        self.rep_4_pos_checked = False
        self.depth_error_in_window = False
        self.min_knee_angle_in_window = 180.0
        self.last_depth_checked_rep = 0
        self.stance_fault_count = 0
        self.stance_good_count = 0
        self.arms_fault_count = 0
        self.arms_good_count = 0
        self.active_feet_warning = None
        self.active_feet_warning_until = 0.0
        self.active_arms_warning = None
        self.active_arms_warning_until = 0.0
        self.active_depth_warning = None
        self.active_depth_warning_until = 0.0

    def on_resume_adjust_timing(self, pause_duration):
        if self.last_rep_time is not None:
            self.last_rep_time += pause_duration
        if self.session_start_time is not None:
            self.session_start_time += pause_duration
        if self.active_feet_warning_until > 0:
            self.active_feet_warning_until += pause_duration
        if self.active_arms_warning_until > 0:
            self.active_arms_warning_until += pause_duration
        if self.active_depth_warning_until > 0:
            self.active_depth_warning_until += pause_duration

    def on_set_increment_while_lost(self):
        self.set_finished = False
        self.set_started = False
        self.last_rep_time = None
        self._reset_set_checks()

    def _evaluate_rep_milestones(self):
        """Evaluate depth (every 2 reps) and positioning (after 4 reps)."""
        now = time.time()
        audio_to_play = []

        # 1. Depth check for each 2 reps (e.g. rep 2, 4, 6, 8)
        if self.reps_in_set > 0 and self.reps_in_set % 2 == 0:
            if self.last_depth_checked_rep != self.reps_in_set:
                self.last_depth_checked_rep = self.reps_in_set
                if self.depth_error_in_window:
                    print(f"⚠️ [DEPTH CHECK] Rep {self.reps_in_set}: Wrong form detected (min knee angle {self.min_knee_angle_in_window:.1f}° < 80°)")
                    self.active_depth_warning = "Don't go too deep in squats"
                    self.active_depth_warning_until = now + 4.5
                    log_session_error("Don't go too deep in squats")
                    audio_to_play.append("002_don't_go_too_deep_in_squats.mp3")
                else:
                    print(f"✅ [DEPTH CHECK] Rep {self.reps_in_set}: Good depth maintained ({self.min_knee_angle_in_window:.1f}°)")
                    self.active_depth_warning = None

                # Reset depth tracking for the next 2 reps window
                self.depth_error_in_window = False
                self.min_knee_angle_in_window = 180.0

        # 2. Positioning check after 4 reps
        if self.reps_in_set == 4 and not self.rep_4_pos_checked:
            self.rep_4_pos_checked = True
            print(f"🔍 [POSITION CHECK] Checking posture after 4 reps...")

            # Check feet stance width
            stance_wrong = (not self.current_stance_ok) or (self.stance_fault_count > self.stance_good_count)
            if stance_wrong:
                print(f"⚠️ [POSITION CHECK] Stance incorrect (ratio: {self.current_stance_ratio:.2f})")
                self.active_feet_warning = "Keep feet at shoulder distance"
                self.active_feet_warning_until = now + 4.5
                log_session_error("Keep feet at shoulder distance")
                audio_to_play.append("006_keep_feet_at_shoulder_distance.mp3")
            else:
                print("✅ [POSITION CHECK] Feet stance is correct.")
                self.active_feet_warning = None

            # Check arm forward extension
            arms_wrong = (not self.current_arms_ok) or (self.arms_fault_count > self.arms_good_count)
            if arms_wrong:
                print("⚠️ [POSITION CHECK] Arm extension incorrect (arms not straight/forward)")
                self.active_arms_warning = "Extend arms straight and directly in front of you"
                self.active_arms_warning_until = now + 4.5
                log_session_error("Extend arms straight and directly in front of you")
                audio_to_play.append("004_extendarms_straight_and_directly_in_front_of_you.mp3")
            else:
                print("✅ [POSITION CHECK] Arm extension is correct.")
                self.active_arms_warning = None

        # Play all required voice alerts in sequence
        if audio_to_play:
            print(f"📢 [VOICE FEEDBACK] Playing feedback sequence: {audio_to_play}")
            play_audio_sequence(audio_to_play, force=True)

    def process_exercise_frame(self, image, results, lm, img_w, img_h):
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

        knees_warning = None

        # 1. Track feet width (measured in stage 'up')
        if all([L_shoulder, R_shoulder, L_ankle, R_ankle]):
            shoulder_dist = abs(L_shoulder[0] - R_shoulder[0])
            feet_dist = abs(L_ankle[0] - R_ankle[0])
            if shoulder_dist > 1e-3:
                self.current_stance_ratio = feet_dist / shoulder_dist
                is_stance_good = (0.9 <= self.current_stance_ratio <= 1.4)
                self.current_stance_ok = is_stance_good
                if self.stage == "up":
                    if is_stance_good:
                        self.stance_good_count += 1
                    else:
                        self.stance_fault_count += 1

        # 2. Track arm extension
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

            is_arms_good = (elbows_straight and horizontal_level and not_to_sides and not_together)
            self.current_arms_ok = is_arms_good
            if is_arms_good:
                self.arms_good_count += 1
            else:
                self.arms_fault_count += 1
        else:
            self.current_arms_ok = False

        # 3. Squat reps and depth monitoring
        if all([L_shoulder, L_hip, L_knee, R_shoulder, R_hip, R_knee]):
            L_hip_angle = calculate_angle_3d(L_shoulder, L_hip, L_knee)
            R_hip_angle = calculate_angle_3d(R_shoulder, R_hip, R_knee)
            self.current_hip_angle = min(L_hip_angle, R_hip_angle)

            if self.current_hip_angle > self.SQUAT_UP_ANGLE:
                self.up_shoulder_y = (L_shoulder[1] + R_shoulder[1]) / 2.0
                self.up_hip_y = (L_hip[1] + R_hip[1]) / 2.0
                self.up_wrist_y = (L_wrist[1] + R_wrist[1]) / 2.0
                self.up_knee_y = (L_knee[1] + R_knee[1]) / 2.0

            if self.current_hip_angle < self.SQUAT_DOWN_ANGLE:
                self.stage = "down"
                if self.down_angle > self.current_hip_angle:
                    self.down_angle = self.current_hip_angle
                if self.reps_in_set == 0 and not self.set_started:
                    self.set_started = True
                    self.last_rep_time = time.time()

            if self.stage == "down" and self.up_shoulder_y is not None:
                curr_shoulder_y = (L_shoulder[1] + R_shoulder[1]) / 2.0
                curr_hip_y = (L_hip[1] + R_hip[1]) / 2.0
                curr_wrist_y = (L_wrist[1] + R_wrist[1]) / 2.0
                curr_knee_y = (L_knee[1] + R_knee[1]) / 2.0
                shoulder_dist = abs(L_shoulder[0] - R_shoulder[0])

                if curr_shoulder_y > self.up_shoulder_y + 0.05 * shoulder_dist:
                    self.lowered_shoulders = True
                if curr_hip_y > self.up_hip_y + 0.05 * shoulder_dist:
                    self.lowered_hips = True
                if curr_wrist_y > self.up_wrist_y + 0.05 * shoulder_dist:
                    self.lowered_wrists = True
                if curr_knee_y > self.up_knee_y + 0.02 * shoulder_dist:
                    self.lowered_knees = True

            # Rep completion check (transitioning up from down)
            if self.current_hip_angle > self.SQUAT_UP_ANGLE and self.stage == 'down':
                self.stage = "up"
                if self.lowered_shoulders and self.lowered_hips and self.lowered_wrists and self.lowered_knees:
                    if not self.is_mid_workout_rest:
                        if self.set_finished:
                            self.current_set += 1
                            self.reps_in_set = 0
                            self.set_finished = False
                            self._reset_set_checks()
                        self.reps_in_set += 1
                        self.counter += 1
                        print(f"  Rep {self.reps_in_set} completed! Angle: {int(self.down_angle)}")
                        self.last_rep_time = time.time()
                        self.set_started = True
                        try:
                            publish_rep("squats", set_num=self.current_set, count=self.counter, in_set=self.reps_in_set)
                        except Exception:
                            pass

                        # Evaluate 2-rep depth & 4-rep positioning milestones
                        self._evaluate_rep_milestones()

                        if self.reps_in_set >= 8:
                            print(f"\n✅ Set {self.current_set} Finished! Please rest.")
                            try:
                                publish_set_complete("squats", self.current_set, self.reps_in_set)
                            except Exception:
                                pass
                            self.set_finished = True
                            self.set_started = False
                            self.last_rep_time = None
                            self._reset_set_checks()
                            if self.current_set == 2:
                                self.trigger_mid_workout_rest()
                                self.set_finished = False

                self.lowered_shoulders = False
                self.lowered_hips = False
                self.lowered_wrists = False
                self.lowered_knees = False
                self.down_angle = self.SQUAT_DOWN_ANGLE

        # Depth tracking during descent
        if all([L_hip, L_knee, L_ankle, R_hip, R_knee, R_ankle]):
            L_knee_angle = calculate_angle_3d(L_hip, L_knee, L_ankle)
            R_knee_angle = calculate_angle_3d(R_hip, R_knee, R_ankle)
            if self.stage == "down":
                curr_min_knee = min(L_knee_angle, R_knee_angle)
                if curr_min_knee < self.min_knee_angle_in_window:
                    self.min_knee_angle_in_window = curr_min_knee
                if curr_min_knee < 80.0:
                    self.depth_error_in_window = True

        # Knee direction check
        if all([L_hip, L_knee, L_ankle, R_hip, R_knee, R_ankle]):
            L_dev = calculate_lateral_deviation(L_hip, L_knee, L_ankle)
            R_dev = calculate_lateral_deviation(R_hip, R_knee, R_ankle)
            if self.stage == "down" and (L_dev > 5.0 or R_dev > 5.0):
                knees_warning = "Stand at shoulder width and keep knees in front of body"
                log_session_error("Stand at shoulder width and keep knees in front of body")

        # Set transition countdown
        now_time = time.time()
        if self.set_started and self.last_rep_time is not None:
            time_elapsed = now_time - self.last_rep_time
            time_limit = 15.0 if self.reps_in_set >= 6 else 20.0
            if time_elapsed > time_limit:
                print(f"\n✅ Set {self.current_set} Finished! Completed {self.reps_in_set} reps.")
                try:
                    publish_set_complete("squats", self.current_set, self.reps_in_set)
                except Exception:
                    pass
                self.set_finished = True
                self.set_started = False
                self.last_rep_time = None
                self._reset_set_checks()
                if self.current_set == 2:
                    self.trigger_mid_workout_rest()
                    self.set_finished = False

        draw_skeleton(image, results)

        # Assemble UI display warnings
        warnings = []
        if self.active_feet_warning and now_time < self.active_feet_warning_until:
            warnings.append(self.active_feet_warning)
        if self.active_arms_warning and now_time < self.active_arms_warning_until:
            warnings.append(self.active_arms_warning)
        if self.active_depth_warning and now_time < self.active_depth_warning_until:
            warnings.append(self.active_depth_warning)
        if self.stage == "down" and knees_warning:
            warnings.append(knees_warning)

        if self.ui is not None:
            self.ui.update_stat_card("Set", self.current_set)
            self.ui.update_stat_card("Reps", self.reps_in_set)
            self.ui.update_stat_card("Total Reps", self.counter)
            self.ui.update_stat_card("Hip Angle", f"{int(self.current_hip_angle)}°")

            feedback_msgs = [f"⚠️ {w}" for w in warnings]
            feedback_color = ACCENT_RED if warnings else ACCENT_GREEN

            if not feedback_msgs:
                if self.initial_instruction_active and (self.session_start_time is not None) and (now_time - self.session_start_time < 5.0) and self.reps_in_set == 0:
                    feedback_msgs.append("Keep feet at shoulder width & extend arms in front")
                    feedback_color = ACCENT_CYAN
                elif self.set_finished:
                    feedback_msgs.append("✅ Set finished! Please rest.")
                elif self.set_started and self.last_rep_time is not None:
                    time_limit = 15.0 if self.reps_in_set >= 6 else 20.0
                    countdown = max(0.0, time_limit - (now_time - self.last_rep_time))
                    feedback_msgs.append(f"✅ Form is Good. Rest in: {countdown:.1f}s")
                else:
                    feedback_msgs.append("✅ Form is Good. Keep it up!")

            self.ui.update_feedback("\n".join(feedback_msgs), feedback_color)

    def process_missing_frame(self, image, img_w, img_h):
        if self.ui is not None:
            self.ui.update_feedback("⚠️ Stand in front of camera.", ACCENT_ORANGE)

    def build_session_summary(self):
        return {
            'exercise': 'Squats',
            'total_reps': self.counter,
            'metrics': [
                ('Total Reps', self.counter),
                ('Total Sets', self.current_set)
            ],
            'errors': list(session_errors)
        }

    def on_finished(self):
        print(f"\n✅ Squats Done! Total Reps: {self.counter}")


def run_squats(ui=None):
    """Convenience runner matching original function signature."""
    runner = SquatsRunner(ui=ui)
    runner.run()
    return runner.last_session_summary
