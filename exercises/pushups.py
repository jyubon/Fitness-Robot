#!/usr/bin/env python3
"""
Push-up counter and posture tracker.
Monitors elbow flexion depth, horizontal posture, and torso-leg alignment.
"""

import time

from config import (
    ACCENT_GREEN,
    ACCENT_ORANGE,
)
from exercise_base import ExerciseRunner
from pose_utils import calculate_angle, get_pt
from ui_overlays import draw_skeleton
from session_manager import publish_rep, publish_set_complete, log_session_error, session_errors


class PushupsRunner(ExerciseRunner):
    """Push-up runner subclass."""

    def __init__(self, ui=None):
        super().__init__(
            exercise_title="Push-ups",
            exercise_key="pushups",
            view_position="SIDE",
            ui=ui
        )
        self.pushup_counter = 0
        self.pushup_stage = "up"
        self.set_finished = False
        self.workout_state = "DOING_SET"

    def count_pushups(self, shoulder, elbow, wrist, neck, hip, ankle):
        elbow_angle = calculate_angle(shoulder, elbow, wrist)
        hip_angle = calculate_angle(neck, hip, ankle)
        is_body_straight = hip_angle >= 165.0

        if is_body_straight:
            if elbow_angle < 110 and self.pushup_stage == 'up':
                self.pushup_stage = 'down'
            elif elbow_angle > 150 and self.pushup_stage == 'down':
                self.pushup_stage = 'up'
                self.pushup_counter += 1
                print(f"  Push-ups Count: {self.pushup_counter}")

        return is_body_straight, elbow_angle

    def process_exercise_frame(self, image, results, lm, img_w, img_h):
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

        is_horizontal = False
        is_body_straight = True

        if shoulder and hip and ankle and elbow and wrist:
            neck = [(left_shoulder[0] + right_shoulder[0]) / 2, (left_shoulder[1] + right_shoulder[1]) / 2] if (left_shoulder and right_shoulder) else shoulder
            is_horizontal = abs(shoulder[0] - ankle[0]) > abs(shoulder[1] - ankle[1])

            if is_horizontal:
                if self.workout_state == "DOING_SET" and not self.is_mid_workout_rest:
                    prev_counter = self.pushup_counter
                    is_body_straight, _ = self.count_pushups(shoulder, elbow, wrist, neck, hip, ankle)

                    if self.pushup_counter > prev_counter:
                        if self.set_finished:
                            self.current_set += 1
                            self.reps_in_set = 0
                            self.set_finished = False
                        self.reps_in_set += 1
                        try:
                            publish_rep("pushups", set_num=self.current_set, count=self.pushup_counter, in_set=self.reps_in_set)
                        except Exception:
                            pass

                        if self.reps_in_set >= 8:
                            print(f"\n✅ Push-up Set {self.current_set} Finished! Please rest.")
                            try:
                                publish_set_complete("pushups", self.current_set, self.reps_in_set)
                            except Exception:
                                pass
                            self.set_finished = True
                            if self.current_set == 2:
                                self.trigger_mid_workout_rest()
                                self.set_finished = False

                    if not is_body_straight:
                        log_session_error("Form a straight line with body")
            else:
                if self.reps_in_set > 0 and not self.set_finished:
                    self.set_finished = True
                    print(f"\n✅ Push-up Set {self.current_set} Finished! Completed {self.reps_in_set} reps.")
                    try:
                        publish_set_complete("pushups", self.current_set, self.reps_in_set)
                    except Exception:
                        pass
                    if self.current_set == 2:
                        self.trigger_mid_workout_rest()
                        self.set_finished = False

        draw_skeleton(image, results)

        if self.ui is not None:
            self.ui.update_stat_card("Set", self.current_set)
            self.ui.update_stat_card("Reps", self.reps_in_set)
            self.ui.update_stat_card("Total", self.pushup_counter)

            feedback_msgs = []
            feedback_color = ACCENT_GREEN

            if not is_horizontal:
                feedback_msgs.append("⚠️ Get into horizontal push-up position")
                feedback_color = ACCENT_ORANGE
            elif not is_body_straight:
                feedback_msgs.append("⚠️ Form a straight line with your body")
                feedback_color = ACCENT_ORANGE
            else:
                feedback_msgs.append("✅ Form is Good. Keep it up!")

            self.ui.update_feedback("\n".join(feedback_msgs), feedback_color)

    def process_missing_frame(self, image, img_w, img_h):
        log_session_error("Align horizontally GET IN POSITION.")
        if self.ui is not None:
            self.ui.update_feedback("⚠️ Positioning... Align horizontally in frame.", ACCENT_ORANGE)

    def build_session_summary(self):
        return {
            'exercise': 'Push-ups',
            'total_reps': self.pushup_counter,
            'metrics': [
                ('Total Push-ups', self.pushup_counter),
                ('Total Sets', self.current_set)
            ],
            'errors': list(session_errors)
        }

    def on_finished(self):
        print(f"\n✅ Push-ups Done! Total: {self.pushup_counter}")


def run_pushups(ui=None):
    """Convenience runner matching original function signature."""
    runner = PushupsRunner(ui=ui)
    runner.run()
    return runner.last_session_summary
