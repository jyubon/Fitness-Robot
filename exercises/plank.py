#!/usr/bin/env python3
"""
Plank hold duration timer and posture tracker.
Monitors horizontal position, shoulder-hip-ankle line straightness, and head alignment.
"""

import cv2
import time

from config import (
    ACCENT_GREEN,
    ACCENT_RED,
    ACCENT_ORANGE,
    TEXT_WHITE,
    TEXT_DIM,
)
from exercise_base import ExerciseRunner
from pose_utils import (
    calculate_distance,
    check_plank,
    get_pt,
)
from ui_overlays import draw_glass_panel, draw_skeleton
from session_manager import (
    publish_plank_time,
    publish_set_complete,
    log_session_error,
    session_errors,
)


class PlankRunner(ExerciseRunner):
    """Plank runner subclass."""

    def __init__(self, ui=None):
        super().__init__(
            exercise_title="Plank",
            exercise_key="plank",
            view_position="SIDE",
            ui=ui
        )
        self.plank_timer = 0.0
        self.in_plank = False
        self.start_time = None
        self.current_set_time = 0.0

    def on_resume_adjust_timing(self, pause_duration):
        if self.start_time is not None:
            self.start_time += pause_duration

    def on_set_increment_while_lost(self):
        self.current_set_time = 0.0

    def count_plank_time(self, shoulder, hip, ankle):
        if check_plank(shoulder, hip, ankle):
            now = time.time()
            if not self.in_plank:
                self.start_time = now
                self.in_plank = True
                return 0.0
            else:
                elapsed_time = now - self.start_time
                self.start_time = now
                return elapsed_time
        else:
            self.in_plank = False
            self.start_time = None
            return 0.0

    def process_exercise_frame(self, image, results, lm, img_w, img_h):
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

        is_horizontal = False
        is_looking_around = False

        if shoulder and hip and ankle:
            neck = [(left_shoulder[0] + right_shoulder[0]) / 2, (left_shoulder[1] + right_shoulder[1]) / 2] if (left_shoulder and right_shoulder) else shoulder
            is_horizontal = abs(shoulder[0] - ankle[0]) > abs(shoulder[1] - ankle[1])

            left_ear = get_pt(lm, 7)
            right_ear = get_pt(lm, 8)
            if left_ear and right_ear and left_shoulder and right_shoulder:
                ear_dist = calculate_distance(left_ear, right_ear)
                sh_width = calculate_distance(left_shoulder, right_shoulder)
                if sh_width > 0 and (ear_dist / sh_width) > 0.35:
                    is_looking_around = True

            if is_horizontal:
                elapsed_time = self.count_plank_time(neck, hip, ankle)
                if not self.is_mid_workout_rest:
                    self.plank_timer += elapsed_time
                    if self.in_plank:
                        self.current_set_time += elapsed_time
                        if self.current_set_time >= 30.0:
                            try:
                                publish_set_complete("plank", self.current_set, 30)
                            except Exception:
                                pass
                            self.current_set += 1
                            self.current_set_time = 0.0
                            print(f"\n✅ Plank Set completed! Total sets: {self.current_set - 1}")
                            if self.current_set == 3:
                                self.trigger_mid_workout_rest()

                try:
                    publish_plank_time(self.plank_timer)
                except Exception:
                    pass

                if not self.in_plank:
                    log_session_error("Form a straight line with body")
                if is_looking_around:
                    log_session_error("Look in front of you")
            else:
                self.in_plank = False
                self.start_time = None
                log_session_error("Align horizontally GET IN POSITION.")

        draw_skeleton(image, results)

        # UI Stats & Feedback
        if self.ui is not None:
            mins = int(self.plank_timer) // 60
            secs = self.plank_timer % 60
            time_str = f"{mins}:{secs:05.2f}" if mins > 0 else f"{secs:.1f}s"

            self.ui.update_stat_card("Hold Time", time_str)
            self.ui.update_stat_card("Set", self.current_set)

            feedback_msgs = []
            feedback_color = ACCENT_GREEN

            if not is_horizontal:
                feedback_msgs.append("⚠️ Align horizontally! Lie down in profile view.")
                feedback_color = ACCENT_ORANGE
            elif not self.in_plank:
                feedback_msgs.append("⚠️ Form a straight line with body!")
                feedback_color = ACCENT_RED
            elif is_looking_around:
                feedback_msgs.append("⚠️ Look in front of you!")
                feedback_color = ACCENT_ORANGE
            else:
                feedback_msgs.append("✅ Form is Good. HOLD!")
                feedback_color = ACCENT_GREEN

            self.ui.update_feedback("\n".join(feedback_msgs), feedback_color)

    def process_missing_frame(self, image, img_w, img_h):
        log_session_error("Align horizontally GET IN POSITION.")
        if self.ui is not None:
            self.ui.update_feedback("⚠️ Positioning... Lie down in front of camera.", ACCENT_ORANGE)

    def build_session_summary(self):
        return {
            'exercise': 'Plank',
            'total_plank_time_s': round(self.plank_timer, 2),
            'metrics': [
                ('Total Hold Time', f"{self.plank_timer:.1f}s"),
                ('Total Sets', max(0, self.current_set - 1))
            ],
            'errors': list(session_errors)
        }

    def on_finished(self):
        print(f"\n✅ Plank Done! Total Hold Time: {self.plank_timer:.1f}s")


def run_plank(ui=None):
    """Convenience runner matching original function signature."""
    runner = PlankRunner(ui=ui)
    runner.run()
    return runner.last_session_summary
