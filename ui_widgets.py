#!/usr/bin/env python3
"""
Custom Tkinter UI widgets and the main touch interface for Fitness Robot.
"""

import os
import subprocess
import tkinter as tk
from tkinter import messagebox, ttk

import math
import random
import threading
import time

try:
    import cv2
except ImportError:
    cv2 = None

from config import CAMERA_INDEX
from audio_manager import terminate_current_audio, start_background_music, stop_background_music_process
from session_manager import (
    publish_robot_state,
    force_stop,
    remote_mode_queue,
    start_exercise,
    exercise_executing,
)
from robot_control import send_command, ser
from ui_overlays import bgr_to_hex
import local_stream_server

try:
    from voice_control import get_voice_state, stop_voice_listener, VoiceState
except ImportError:
    get_voice_state = None
    stop_voice_listener = None
    VoiceState = None

# Global state for session summary
last_session_summary = None



class GooglyEyesWidget(tk.Canvas):
    """
    Animated Interactive Googly Eyes Widget for Touchscreen UI.
    Simulates physical googly eyes with:
      - Spring-damper physics with inertia and bouncing boundaries
      - Specular glassy highlights for authentic 3D plastic craft look
      - Smooth blinking and expressive eyelids/brows
      - Real-time tracking of person (x, y) coordinates
      - Interactive touch/drag tracking on touchscreen
      - Autonomous lifelike saccades and curiosity when idle
      - Emotional states: IDLE, FOLLOWING, SEARCHING, LOST, SURPRISED, INTERACTIVE
    """
    def __init__(
        self,
        parent,
        width=480,
        height=320,
        eye_radius=None,
        pupil_radius=None,
        bg="#000000",
        invert_x=True,
        interactive=True,
        on_click=None,
        **kwargs
    ):
        super().__init__(parent, width=width, height=height, bg=bg, highlightthickness=0, borderwidth=0, **kwargs)
        self.w = width
        self.h = height
        self.invert_x = invert_x
        self.interactive = interactive
        self.on_click = on_click

        # Eye geometries
        self.custom_eye_radius = eye_radius
        self.custom_pupil_radius = pupil_radius

        # Physics parameters
        # Normalized target [-1.0, 1.0]
        self.target_x = 0.0
        self.target_y = 0.0
        self.current_x = 0.0
        self.current_y = 0.0
        self.vel_x = 0.0
        self.vel_y = 0.0

        # Physical constants
        self.spring_k = 0.22      # Spring responsiveness
        self.damping = 0.72       # Inertial damping (lower = more bouncy/wobbly)
        self.restitution = 0.45   # Bounce off wall

        # State & Emotion
        self.state = "IDLE"       # IDLE, FOLLOWING, SEARCHING, LOST, SURPRISED, INTERACTIVE
        self.last_target_update = time.time()
        self.last_saccade_time = time.time()
        self.saccade_interval = random.uniform(1.8, 3.5)

        # Blinking state
        self.blink_progress = 0.0 # 0.0 (open) to 1.0 (closed)
        self.is_blinking = False
        self.blink_speed = 0.22   # Blink step
        self.blink_closing = True
        self.last_blink_time = time.time()
        self.next_blink_delay = random.uniform(3.0, 6.0)

        # Bind touch/mouse events for touchscreen interactivity
        if self.interactive:
            self.bind("<Button-1>", self._on_touch)
            self.bind("<B1-Motion>", self._on_touch)
            self.bind("<ButtonRelease-1>", self._on_release)
            self.bind("<Motion>", self._on_hover)

        self.bind("<Configure>", self._on_resize)
        self.running = True
        self._animate()

    def _on_resize(self, event):
        if event.width > 10 and event.height > 10:
            self.w = event.width
            self.h = event.height
            self._redraw()

    def set_target(self, norm_x, norm_y, mode="FOLLOWING"):
        """Set normalized target coordinate in [-1.0, 1.0]."""
        self.state = mode
        self.last_target_update = time.time()
        tx = -norm_x if self.invert_x else norm_x
        self.target_x = max(-1.0, min(1.0, tx))
        self.target_y = max(-1.0, min(1.0, norm_y))

    def set_mode(self, mode):
        """Set emotion/tracking mode (IDLE, FOLLOWING, SEARCHING, LOST, SURPRISED)."""
        self.state = mode
        self.last_target_update = time.time()

    def trigger_blink(self):
        """Force a blink animation."""
        self.is_blinking = True
        self.blink_closing = True

    def _on_touch(self, event):
        """Touchscreen interaction: look directly at the finger."""
        norm_x = (event.x / max(1, self.w) - 0.5) * 2.2
        norm_y = (event.y / max(1, self.h) - 0.5) * 2.2
        self.target_x = max(-1.0, min(1.0, norm_x))
        self.target_y = max(-1.0, min(1.0, norm_y))
        self.state = "INTERACTIVE"
        self.last_target_update = time.time()
        if callable(self.on_click):
            try:
                self.on_click()
            except Exception:
                pass

    def _on_hover(self, event):
        if self.state == "IDLE":
            norm_x = (event.x / max(1, self.w) - 0.5) * 1.6
            norm_y = (event.y / max(1, self.h) - 0.5) * 1.6
            self.target_x = max(-0.8, min(0.8, norm_x))
            self.target_y = max(-0.8, min(0.8, norm_y))
            self.last_target_update = time.time()

    def _on_release(self, event):
        # Give a fun little wobble on release
        self.vel_x += random.uniform(-0.18, 0.18)
        self.vel_y += random.uniform(-0.18, 0.18)

    def _update_physics(self):
        now = time.time()

        # Handle autonomous behaviors when idle or searching
        if self.state == "SEARCHING":
            # Rapid scanning search sweep
            sweep = math.sin(now * 5.0) * 0.95
            self.target_x = sweep
            self.target_y = math.sin(now * 2.5) * 0.25
        elif self.state == "LOST":
            # Look up and around quizzically
            self.target_x = math.sin(now * 2.0) * 0.6
            self.target_y = -0.7 + math.cos(now * 3.0) * 0.2
        elif self.state in ("IDLE", "INTERACTIVE"):
            # If no target update for 2.5 seconds, resume natural saccades
            if now - self.last_target_update > 2.5:
                self.state = "IDLE"
            if self.state == "IDLE":
                if now - self.last_saccade_time > self.saccade_interval:
                    self.last_saccade_time = now
                    self.saccade_interval = random.uniform(1.8, 4.0)
                    if random.random() < 0.45:
                        self.target_x = random.uniform(-0.65, 0.65)
                        self.target_y = random.uniform(-0.45, 0.45)
                    else:
                        self.target_x = random.uniform(-0.15, 0.15)
                        self.target_y = random.uniform(-0.1, 0.1)

        # Handle Blinking
        if not self.is_blinking and (now - self.last_blink_time > self.next_blink_delay):
            self.is_blinking = True
            self.blink_closing = True
            self.last_blink_time = now
            self.next_blink_delay = random.uniform(3.0, 6.0)

        if self.is_blinking:
            if self.blink_closing:
                self.blink_progress += self.blink_speed
                if self.blink_progress >= 1.0:
                    self.blink_progress = 1.0
                    self.blink_closing = False
            else:
                self.blink_progress -= self.blink_speed
                if self.blink_progress <= 0.0:
                    self.blink_progress = 0.0
                    self.is_blinking = False

        # Spring-damper physics
        acc_x = (self.target_x - self.current_x) * self.spring_k
        acc_y = (self.target_y - self.current_y) * self.spring_k

        self.vel_x = (self.vel_x + acc_x) * self.damping
        self.vel_y = (self.vel_y + acc_y) * self.damping

        self.current_x += self.vel_x
        self.current_y += self.vel_y

        # Bounding inside circular/elliptical eye dome
        dist = math.hypot(self.current_x, self.current_y)
        if dist > 1.0:
            nx = self.current_x / dist
            ny = self.current_y / dist
            self.current_x = nx
            self.current_y = ny
            # Restitution bounce
            dot = self.vel_x * nx + self.vel_y * ny
            if dot > 0:
                self.vel_x -= (1.0 + self.restitution) * dot * nx
                self.vel_y -= (1.0 + self.restitution) * dot * ny

    def _redraw(self):
        self.delete("all")
        w, h = self.w, self.h
        if w < 20 or h < 20:
            return

        cx = w / 2
        cy = h / 2

        # Scale based on canvas size, fitted for 480x320 screen resolution
        if h > 100:
            # Full screen mode: calibrated to 480x320 screen resolution
            outer_span = min(w * 0.90, h * 1.38)
            eye_w = outer_span / 2.50
            spacing = eye_w * 1.50
            eye_h = eye_w / 2.15
        else:
            # Compact banner on dashboard header
            eye_w = min(w * 0.32, h * 0.65 * 2.36)
            eye_h = eye_w / 2.36
            spacing = eye_w * 1.58

        left_cx = cx - spacing / 2
        right_cx = cx + spacing / 2

        pr = eye_h * 0.256
        glint_r = pr * 0.262
        max_travel_x = eye_w * 0.18
        max_travel_y = eye_h * 0.14

        # Parametric curves matching user reference image
        top_table = [0.040, 0.062, 0.086, 0.112, 0.139, 0.168, 0.198, 0.230, 0.263, 0.298, 0.335, 0.373, 0.412, 0.454, 0.496, 0.541, 0.587, 0.634, 0.683, 0.734, 0.786]
        bot_table = [0.061, 0.316, 0.520, 0.663, 0.765, 0.837, 0.898, 0.939, 0.969, 0.990, 1.000, 1.000, 1.000, 0.990, 0.969, 0.949, 0.918, 0.888, 0.857, 0.816, 0.786]

        def get_tbl_y(tbl, t):
            idx = t * 20.0
            i = min(19, int(idx))
            frac = idx - i
            return tbl[i] + frac * (tbl[i+1] - tbl[i])

        for ecx, eye_sign in ((left_cx, -1), (right_cx, 1)):
            pts = []
            steps = 40
            ex = ecx - eye_w / 2
            ey = cy - eye_h / 2

            # Top edge: slants down toward center from outer tip to inner tip
            for i in range(steps + 1):
                t = i / steps
                lx = t * eye_w
                ly = get_tbl_y(top_table, t) * eye_h
                if eye_sign == 1:
                    lx = eye_w - lx
                pts.extend([ex + lx, ey + ly])

            # Bottom edge: curved arc from inner tip back to outer tip
            for i in range(steps, -1, -1):
                t = i / steps
                lx = t * eye_w
                ly = get_tbl_y(bot_table, t) * eye_h
                if eye_sign == 1:
                    lx = eye_w - lx
                pts.extend([ex + lx, ey + ly])

            # 1. Solid pure white sclera
            self.create_polygon(pts, fill="#FFFFFF", outline="#FFFFFF", width=1, smooth=True)

            # 2. Black circular pupil cutting into the top edge
            base_px = ex + (0.516 if eye_sign == -1 else (1.0 - 0.516)) * eye_w
            base_py = ey + 0.498 * eye_h

            px = base_px + self.current_x * max_travel_x
            py = base_py + self.current_y * max_travel_y

            self.create_oval(
                px - pr, py - pr,
                px + pr, py + pr,
                fill="#000000", outline="#000000", width=1
            )

            # 3. Crisp white center circular pupil glint
            self.create_oval(
                px - glint_r, py - glint_r,
                px + glint_r, py + glint_r,
                fill="#FFFFFF", outline="#FFFFFF", width=1
            )

            # 4. Smooth black eyelid for blinking
            if self.blink_progress > 0.02:
                lid_drop = eye_h * 1.5 * self.blink_progress
                b_pts = [
                    ex - eye_w * 0.1, ey - eye_h * 0.5,
                    ex + eye_w * 1.1, ey - eye_h * 0.5,
                    ex + eye_w * 1.1, ey + lid_drop,
                    ex - eye_w * 0.1, ey + lid_drop
                ]
                self.create_polygon(b_pts, fill="#000000", outline="#000000", width=1)

    def _animate(self):
        if not self.running:
            return
        self._update_physics()
        self._redraw()
        self.after(25, self._animate)

    def destroy(self):
        self.running = False
        super().destroy()


class HomeMotionTracker:
    """
    Lightweight background camera motion tracker for Home Screen ("just powered on").
    Detects person motion in front of the camera and guides Googly Eyes.
    Cleanly releases camera whenever an exercise or follower mode starts.
    """
    def __init__(self, camera_index=CAMERA_INDEX, on_motion_callback=None):
        self.camera_index = camera_index
        self.on_motion = on_motion_callback
        self.running = False
        self.thread = None
        self._stop_event = threading.Event()

    def start(self):
        if self.running or cv2 is None:
            return
        self.running = True
        self._stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name="HomeMotionTracker")
        self.thread.start()

    def stop(self, timeout=0.8):
        if not self.running:
            return
        self.running = False
        self._stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=timeout)
        self.thread = None

    def _run(self):
        if cv2 is None:
            return
        cap = None
        try:
            cap = cv2.VideoCapture(self.camera_index)
            if not cap.isOpened():
                return
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)

            last_gray = None
            while not self._stop_event.is_set():
                ret, frame = cap.read()
                if not ret or frame is None:
                    time.sleep(0.1)
                    continue

                small = cv2.resize(frame, (160, 120))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                gray = cv2.GaussianBlur(gray, (15, 15), 0)

                if last_gray is None:
                    last_gray = gray
                    time.sleep(0.08)
                    continue

                diff = cv2.absdiff(last_gray, gray)
                thresh = cv2.threshold(diff, 20, 255, cv2.THRESH_BINARY)[1]
                thresh = cv2.dilate(thresh, None, iterations=2)

                M = cv2.moments(thresh)
                if M["m00"] > 350:
                    cx = M["m10"] / M["m00"]
                    cy = M["m01"] / M["m00"]
                    norm_x = (cx / 160.0 - 0.5) * 2.0
                    norm_y = (cy / 120.0 - 0.5) * 2.0
                    if self.on_motion:
                        self.on_motion(norm_x, norm_y)

                last_gray = gray
                time.sleep(0.06)
        except Exception:
            pass
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass


class RoundedButton(tk.Canvas):
    def __init__(self, parent, text, command, bg_color, fg_color="#1E1E24", radius=15, font=("Helvetica", 12, "bold"), height=50, width=None, outline_color=None, outline_width=3, gradient_color=None, **kwargs):
        super().__init__(parent, borderwidth=0, highlightthickness=0, bg=parent["bg"], height=height, **kwargs)
        if width:
            self.configure(width=width)
        self.command = command
        self.bg_color = bg_color
        self.gradient_color = gradient_color
        self.fg_color = fg_color
        self.radius = radius
        self.text = text
        self.font = font
        self.pressed = False
        self.hovered = False
        self.outline_color = outline_color
        self.outline_width = outline_width

        self.bind("<Configure>", self._draw)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _hex_to_rgb(self, h):
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

    def _brighten_color(self, hex_color, amount=22):
        try:
            if hex_color.startswith("#"):
                r = int(hex_color[1:3], 16)
                g = int(hex_color[3:5], 16)
                b = int(hex_color[5:7], 16)
                return f"#{min(255, r+amount):02x}{min(255, g+amount):02x}{min(255, b+amount):02x}"
        except Exception:
            pass
        return hex_color

    def _darken_color(self, hex_color, amount=30):
        try:
            if hex_color.startswith("#"):
                r = int(hex_color[1:3], 16)
                g = int(hex_color[3:5], 16)
                b = int(hex_color[5:7], 16)
                return f"#{max(0, r-amount):02x}{max(0, g-amount):02x}{max(0, b-amount):02x}"
        except Exception:
            pass
        return hex_color

    def _draw(self, event=None):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        r = min(self.radius, w // 2, h // 2)
        bw = self.outline_width if (self.outline_color and self.outline_width > 0) else 0

        if w <= 0 or h <= 0:
            return

        if self.gradient_color:
            c1 = self.bg_color
            c2 = self.gradient_color
            if self.pressed:
                c1 = self._darken_color(c1, 40)
                c2 = self._darken_color(c2, 40)
            elif self.hovered:
                c1 = self._brighten_color(c1, 20)
                c2 = self._brighten_color(c2, 20)

            rgb1 = self._hex_to_rgb(c1)
            rgb2 = self._hex_to_rgb(c2)

            for y in range(h):
                t = y / max(1, h - 1)
                cr = int(rgb1[0] + (rgb2[0] - rgb1[0]) * t)
                cg = int(rgb1[1] + (rgb2[1] - rgb1[1]) * t)
                cb = int(rgb1[2] + (rgb2[2] - rgb1[2]) * t)
                line_color = f"#{cr:02x}{cg:02x}{cb:02x}"
                if y < r:
                    dx = r - math.sqrt(max(0, r * r - (r - y) ** 2))
                elif y > h - r:
                    dy = y - (h - r)
                    dx = r - math.sqrt(max(0, r * r - dy * dy))
                else:
                    dx = 0
                self.create_line(int(dx), y, int(w - dx), y, fill=line_color)
        else:
            color = self.bg_color
            if self.pressed:
                color = self._darken_color(self.bg_color)
            elif self.hovered:
                color = self._brighten_color(self.bg_color)

            self.create_oval(bw, bw, r * 2 + bw, r * 2 + bw, fill=color, outline=color)
            self.create_oval(w - r * 2 - bw, bw, w - bw, r * 2 + bw, fill=color, outline=color)
            self.create_oval(bw, h - r * 2 - bw, r * 2 + bw, h - bw, fill=color, outline=color)
            self.create_oval(w - r * 2 - bw, h - r * 2 - bw, w - bw, h - bw, fill=color, outline=color)

            self.create_rectangle(r + bw, bw, w - r - bw, h - bw, fill=color, outline=color)
            self.create_rectangle(bw, r + bw, w - bw, h - r - bw, fill=color, outline=color)

        if self.outline_color and bw > 0:
            self.create_arc(bw, bw, r * 2 + bw, r * 2 + bw, start=90, extent=90, style="arc", outline=self.outline_color, width=bw)
            self.create_arc(w - r * 2 - bw, bw, w - bw, r * 2 + bw, start=0, extent=90, style="arc", outline=self.outline_color, width=bw)
            self.create_arc(bw, h - r * 2 - bw, r * 2 + bw, h - bw, start=180, extent=90, style="arc", outline=self.outline_color, width=bw)
            self.create_arc(w - r * 2 - bw, h - r * 2 - bw, w - bw, h - bw, start=270, extent=90, style="arc", outline=self.outline_color, width=bw)

            self.create_line(r + bw, bw, w - r - bw, bw, fill=self.outline_color, width=bw)
            self.create_line(r + bw, h - bw, w - r - bw, h - bw, fill=self.outline_color, width=bw)
            self.create_line(bw, r + bw, bw, h - r - bw, fill=self.outline_color, width=bw)
            self.create_line(w - bw, r + bw, w - bw, h - r - bw, fill=self.outline_color, width=bw)

        self.create_text(w // 2, h // 2, text=self.text, fill=self.fg_color, font=self.font)

    def _on_enter(self, event):
        self.hovered = True
        self.configure(cursor="hand2")
        self._draw()

    def _on_leave(self, event):
        self.hovered = False
        self.configure(cursor="")
        if self.pressed:
            self.pressed = False
        self._draw()

    def _on_press(self, event):
        self.pressed = True
        self._draw()

    def _on_release(self, event):
        self.pressed = False
        self._draw()
        if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            if self.command:
                self.command()




class OutlinedLabel(tk.Canvas):
    def __init__(self, parent, text, font, fg, outline_color, bg_color="#1E1E24", **kwargs):
        super().__init__(parent, borderwidth=0, highlightthickness=0, bg=bg_color, **kwargs)
        self.text = text
        self.font = font
        self.fg = fg
        self.outline_color = outline_color
        self.bind("<Configure>", self._draw)

    def _draw(self, event=None):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 0 or h <= 0:
            return

        x = w // 2
        y = h // 2

        offsets = [(-2, -2), (-2, 0), (-2, 2), (0, -2), (0, 2), (2, -2), (2, 0), (2, 2)]
        for dx, dy in offsets:
            self.create_text(x + dx, y + dy, text=self.text, font=self.font, fill=self.outline_color, anchor="center")

        self.create_text(x, y, text=self.text, font=self.font, fill=self.fg, anchor="center")


class RoundedFrame(tk.Canvas):
    def __init__(self, parent, bg_color, border_color, border_width=3, radius=20, **kwargs):
        super().__init__(parent, borderwidth=0, highlightthickness=0, bg=parent["bg"], **kwargs)
        self.bg_color = bg_color
        self.border_color = border_color
        self.border_width = border_width
        self.radius = radius
        self.bind("<Configure>", self._draw)

    def _draw(self, event=None):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        r = self.radius
        bw = self.border_width

        if w <= 0 or h <= 0:
            return

        self.create_oval(bw, bw, r*2+bw, r*2+bw, fill=self.bg_color, outline=self.bg_color)
        self.create_oval(w - r*2 - bw, bw, w - bw, r*2+bw, fill=self.bg_color, outline=self.bg_color)
        self.create_oval(bw, h - r*2 - bw, r*2+bw, h - bw, fill=self.bg_color, outline=self.bg_color)
        self.create_oval(w - r*2 - bw, h - r*2 - bw, w - bw, h - bw, fill=self.bg_color, outline=self.bg_color)
        self.create_rectangle(r+bw, bw, w - r - bw, h - bw, fill=self.bg_color, outline=self.bg_color)
        self.create_rectangle(bw, r+bw, w - bw, h - r - bw, fill=self.bg_color, outline=self.bg_color)

        if self.border_color and bw > 0:
            self.create_arc(bw, bw, r*2+bw, r*2+bw, start=90, extent=90, style="arc", outline=self.border_color, width=bw)
            self.create_arc(w - r*2 - bw, bw, w - bw, r*2+bw, start=0, extent=90, style="arc", outline=self.border_color, width=bw)
            self.create_arc(bw, h - r*2 - bw, r*2+bw, h - bw, start=180, extent=90, style="arc", outline=self.border_color, width=bw)
            self.create_arc(w - r*2 - bw, h - r*2 - bw, w - bw, h - bw, start=270, extent=90, style="arc", outline=self.border_color, width=bw)

            self.create_line(r+bw, bw, w - r - bw, bw, fill=self.border_color, width=bw)
            self.create_line(r+bw, h - bw, w - r - bw, h - bw, fill=self.border_color, width=bw)
            self.create_line(bw, r+bw, bw, h - r - bw, fill=self.border_color, width=bw)
            self.create_line(w - bw, r+bw, w - bw, h - r - bw, fill=self.border_color, width=bw)


class FitnessRobotUI(tk.Tk):
    """Main Touch Interface for Fitness Robot on Raspberry Pi 4."""

    def __init__(self, exercise_callbacks=None, fullscreen=True):
        super().__init__()
        self.title("Fitness Robot UI")

        if fullscreen:
            self.attributes("-fullscreen", True)
        else:
            self.geometry("480x320")
            self.resizable(True, True)
        self.bind("<Escape>", lambda e: self.quit_app())
        self.configure(bg="#000000")

        self.style = ttk.Style(self)
        self.style.theme_use('clam')
        self.style.configure('.', background='#000000', foreground='#EBEBEF')

        self.main_container = tk.Frame(self, bg="#000000")
        self.main_container.pack(fill="both", expand=True, padx=5, pady=5)

        self.exercise_active = False
        self.next_remote_mode = None
        self.home_clicked = False
        self.is_resting = False
        self.stat_cards = {}
        self.exercise_callbacks = exercise_callbacks or {}
        self.home_motion_tracker = None
        self.home_eyes = None
        self.follower_eyes = None

        # Turn on screen: ONLY Googly Eyes displayed
        self.show_eyes_only_screen()
        start_background_music()
        self.after(200, self.check_remote_commands)

    def show_home_screen(self):
        """Standby screen: purely Googly Eyes full screen."""
        self.show_eyes_only_screen()

    def run_exercise(self, exercise_name, func):
        global last_session_summary
        self.current_exercise_name = exercise_name
        self.stop_home_camera()
        self.exercise_active = True
        exercise_executing.set()
        stop_background_music_process()
        self.home_clicked = False
        force_stop.clear()
        try:
            if exercise_name == "Person Following":
                self.show_follower_screen()
            else:
                self.show_exercise_screen(exercise_name)
            self.update()
            summary = func(self)
            if summary:
                last_session_summary = summary
        except tk.TclError:
            return
        finally:
            self.exercise_active = False
            self.current_exercise_name = ""
            exercise_executing.clear()
            start_exercise.clear()
            was_force_stop = force_stop.is_set()
            force_stop.clear()
            try:
                local_stream_server.update_frame(None)
            except Exception:
                pass
            try:
                exists = self.winfo_exists()
            except tk.TclError:
                exists = False
            if exists:
                if self.home_clicked or (was_force_stop and not self.next_remote_mode):
                    self.next_remote_mode = None
                    self.home_clicked = False
                    while not remote_mode_queue.empty():
                        try:
                            remote_mode_queue.get_nowait()
                        except Exception:
                            break
                    try:
                        self.show_summary_screen()
                    except tk.TclError:
                        pass
                elif self.next_remote_mode:
                    mode = self.next_remote_mode
                    self.next_remote_mode = None
                    self.launch_by_name(mode)
                elif exercise_name == "Person Following":
                    # When robot came to a stop after person following -> show exercise selection dashboard!
                    try:
                        self.show_exercise_dashboard()
                    except tk.TclError:
                        pass
                else:
                    try:
                        self.show_summary_screen()
                    except tk.TclError:
                        pass

    def check_remote_commands(self):
        try:
            if not self.winfo_exists():
                return
            if not remote_mode_queue.empty():
                cmd_type, cmd_val = remote_mode_queue.get_nowait()
                if cmd_type == "switch":
                    print(f"[GUI] Remote mode switch received: {cmd_val}")
                    target_name = str(cmd_val).upper().replace("-", "_").replace(" ", "_")
                    if self.exercise_active:
                        current_ex = getattr(self, 'current_exercise_name', '').upper().replace("-", "_").replace(" ", "_")
                        is_same = (
                            ("BICEP" in current_ex and "BICEP" in target_name) or
                            ("PUSH" in current_ex and "PUSH" in target_name) or
                            ("SQUAT" in current_ex and "SQUAT" in target_name) or
                            ("PLANK" in current_ex and "PLANK" in target_name) or
                            ("FOLLOW" in current_ex and "FOLLOW" in target_name)
                        )
                        if is_same:
                            print(f"[GUI] Already running {cmd_val}; ignoring duplicate switch command.")
                        else:
                            self.next_remote_mode = cmd_val
                            force_stop.set()
                    else:
                        self.launch_by_name(cmd_val)

            # Update voice status indicator on UI
            if get_voice_state is not None and hasattr(self, 'voice_status_label'):
                try:
                    if self.voice_status_label.winfo_exists():
                        st, msg, last_txt, last_cmd = get_voice_state()
                        if st == VoiceState.LISTENING:
                            self.voice_status_label.configure(text="🎙️ Listening... (pause 1s to send)", fg="#FF9500")
                        elif st == VoiceState.PROCESSING:
                            self.voice_status_label.configure(text="🧠 Understanding command...", fg="#00C7BE")
                        elif st == VoiceState.EXECUTING:
                            if msg and msg.startswith("🗣️"):
                                self.voice_status_label.configure(text=msg, fg="#34C759")
                            else:
                                self.voice_status_label.configure(text=f"✅ Voice: {last_cmd}", fg="#34C759")
                        elif st == VoiceState.DISABLED:
                            self.voice_status_label.configure(text="🎤 Voice: Disabled", fg="#8E8E93")
                        else:
                            if self.exercise_active:
                                self.voice_status_label.configure(text="🎤 Voice: Say \"FitBot <command>\"", fg="#96D75F")
                            else:
                                self.voice_status_label.configure(text="🎤 Voice: Standby", fg="#96D75F")
                except Exception:
                    pass
        except Exception as e:
            print(f"[GUI] Error in check_remote_commands: {e}")
        finally:
            try:
                if self.winfo_exists():
                    self.after(200, self.check_remote_commands)
            except Exception:
                pass

    def launch_by_name(self, name):
        if hasattr(self, 'summary_timer_id') and self.summary_timer_id is not None:
            try:
                self.after_cancel(self.summary_timer_id)
            except Exception:
                pass
            self.summary_timer_id = None

        force_stop.clear()
        name = str(name).upper().replace("-", "_").replace(" ", "_")
        if "BICEP" in name or "CURL" in name:
            self.launch_bicep_curls()
        elif "PUSH" in name:
            self.launch_pushups()
        elif "SQUAT" in name:
            self.launch_squats()
        elif "PLANK" in name:
            self.launch_plank()
        elif "FOLLOW" in name or "PERSON" in name:
            self.launch_person_follow()
        else:
            print(f"[GUI] Unknown remote mode: {name}")

    def launch_person_follow(self):
        cb = self.exercise_callbacks.get("person_follow")
        if cb:
            self.run_exercise("Person Following", cb)

    def launch_bicep_curls(self):
        cb = self.exercise_callbacks.get("bicep_curls")
        if cb:
            self.run_exercise("Bicep Curls", cb)

    def launch_pushups(self):
        cb = self.exercise_callbacks.get("pushups")
        if cb:
            self.run_exercise("Push-ups", cb)

    def launch_squats(self):
        cb = self.exercise_callbacks.get("squats")
        if cb:
            self.run_exercise("Squats", cb)

    def launch_plank(self):
        cb = self.exercise_callbacks.get("plank")
        if cb:
            self.run_exercise("Plank", cb)

    def show_exercise_screen(self, exercise_name):
        try:
            # ── EXHIBITION MODE: State ACTIVE immediately (positioning bypassed) ──
            # publish_robot_state("FOLLOWING")
            publish_robot_state("ACTIVE")
        except Exception:
            pass

        self.configure(bg="#000000")
        self.main_container.configure(bg="#000000")
        self.main_container.pack_configure(padx=8, pady=6)

        self.stop_home_camera()
        self.home_eyes = None

        for widget in self.main_container.winfo_children():
            widget.destroy()

        self.is_resting = False
        self.ex_title_label = tk.Label(
            self.main_container,
            text=exercise_name,
            font=("Helvetica", 20, "bold"),
            fg="#96D75F",
            bg="#000000"
        )
        self.ex_title_label.pack(anchor="w", pady=(5, 10), padx=5)

        self.stats_row_frame = tk.Frame(self.main_container, bg="#000000")
        self.stats_row_frame.pack(anchor="w", fill="x", pady=5, padx=5)
        self.stat_cards = {}

        self.set_count_warning_label = tk.Label(
            self.main_container,
            text="",
            font=("Helvetica", 14, "bold"),
            fg="#FF3B30",
            bg="#000000",
            wraplength=450,
            justify="left",
            anchor="w"
        )
        self.set_count_warning_label.pack(anchor="w", fill="x", pady=(5, 0), padx=5)

        self.feedback_label = tk.Label(
            self.main_container,
            text="",
            font=("Helvetica", 14, "bold"),
            fg="#EBEBEF",
            bg="#000000",
            wraplength=450,
            justify="left",
            anchor="w"
        )
        self.feedback_label.pack(anchor="w", fill="x", pady=10, padx=5)

        self.bottom_bar = tk.Frame(self.main_container, bg="#000000")
        self.bottom_bar.pack(fill="x", side="bottom", pady=10)

        # ── EXHIBITION MODE: Start button commented out for reuse later ──
        # self.start_btn = RoundedButton(
        #     self.bottom_bar,
        #     text="Start",
        #     command=self.set_start_clicked,
        #     bg_color="#96D75F",
        #     fg_color="#000000",
        #     radius=10,
        #     width=100,
        #     height=35,
        #     font=("Helvetica", 12, "bold")
        # )
        # self.start_btn.pack(side="left", padx=10)
        self.start_btn = None

        self.home_btn = RoundedButton(
            self.bottom_bar,
            text="Home",
            command=self.set_home_clicked,
            bg_color="#FF9500",
            fg_color="#FFFFFF",
            radius=10,
            width=100,
            height=35,
            font=("Helvetica", 12, "bold")
        )
        self.home_btn.pack(side="right", padx=10)

        self.voice_status_label = tk.Label(
            self.bottom_bar,
            text="🎤 Voice: Say \"FitBot <command>\"",
            font=("Helvetica", 9, "bold"),
            fg="#96D75F",
            bg="#000000",
            wraplength=230,
        )
        self.voice_status_label.pack(side="top", pady=5)


    def start_home_camera(self):
        """Start background camera motion tracker for home screen Googly Eyes."""
        if cv2 is None:
            return
        try:
            if self.home_motion_tracker is None:
                def _on_motion(nx, ny):
                    if hasattr(self, 'home_eyes') and self.home_eyes is not None and self.home_eyes.winfo_exists():
                        self.home_eyes.set_target(nx, ny, mode="FOLLOWING")
                self.home_motion_tracker = HomeMotionTracker(camera_index=CAMERA_INDEX, on_motion_callback=_on_motion)
            self.home_motion_tracker.start()
        except Exception:
            pass

    def stop_home_camera(self):
        """Stop background camera motion tracker so exercises get exclusive camera access."""
        if self.home_motion_tracker is not None:
            try:
                self.home_motion_tracker.stop(timeout=0.6)
            except Exception:
                pass

    def show_full_face_screen(self):
        """Display giant full-screen Googly Eyes on touchscreen."""
        for widget in self.main_container.winfo_children():
            widget.destroy()

        top_bar = tk.Frame(self.main_container, bg="#000000")
        top_bar.pack(fill="x", pady=2)
        tk.Label(top_bar, text="🤖 FITBOT FACE • Touch anywhere to exit", font=("Helvetica", 10, "bold"), fg="#50AFDF", bg="#1E1E24").pack(side="left", padx=10)
        RoundedButton(top_bar, text="HOME", command=self.show_home_screen, bg_color="#FF9500", fg_color="#FFFFFF", radius=8, width=70, height=26, font=("Helvetica", 9, "bold")).pack(side="right", padx=10)

        self.home_eyes = GooglyEyesWidget(
            self.main_container,
            width=460,
            height=240,
            bg="#000000",
            invert_x=True,
            interactive=True,
            on_click=self.show_home_screen
        )
        self.home_eyes.pack(fill="both", expand=True, padx=10, pady=5)
        self.start_home_camera()

    def show_eyes_only_screen(self):
        """Turn-on / Standby screen: ONLY Googly Eyes displayed full screen."""
        try:
            publish_robot_state("READY")
        except Exception:
            pass
        self.exercise_active = False
        self.home_clicked = False
        self.configure(bg="#000000")
        self.main_container.configure(bg="#000000")
        self.main_container.pack_configure(padx=0, pady=0)

        if hasattr(self, 'summary_timer_id') and self.summary_timer_id is not None:
            try:
                self.after_cancel(self.summary_timer_id)
            except Exception:
                pass
            self.summary_timer_id = None

        for widget in self.main_container.winfo_children():
            widget.destroy()

        try:
            send_command("STOP")
        except Exception:
            pass

        # ONLY THE EYES DISPLAYED FULL SCREEN WHEN TURNED ON
        self.home_eyes = GooglyEyesWidget(
            self.main_container,
            bg="#000000",
            invert_x=True,
            interactive=True,
            on_click=self.show_exercise_dashboard
        )
        self.home_eyes.pack(fill="both", expand=True)

        # Start camera motion tracking so eyes track people in the room
        self.start_home_camera()
        start_background_music()

    def show_home_screen(self):
        """Alias to show_eyes_only_screen."""
        self.show_eyes_only_screen()

    def show_follower_screen(self):
        """Person Following screen: Googly Eyes FULL SCREEN."""
        try:
            publish_robot_state("FOLLOWING")
        except Exception:
            pass
        self.configure(bg="#000000")
        self.main_container.configure(bg="#000000")
        self.main_container.pack_configure(padx=0, pady=0)

        for widget in self.main_container.winfo_children():
            widget.destroy()

        self.is_resting = False

        # ONLY GOOGLY EYES FULL SCREEN DURING PERSON FOLLOWING
        self.follower_eyes = GooglyEyesWidget(
            self.main_container,
            bg="#000000",
            invert_x=True,
            interactive=True,
            on_click=self.stop_following_and_show_dashboard
        )
        self.follower_eyes.pack(fill="both", expand=True)

    def stop_following_and_show_dashboard(self):
        """User tapped screen while following: stop following and display exercise dashboard."""
        self.home_clicked = True
        force_stop.set()
        try:
            send_command("STOP")
        except Exception:
            pass

    def show_exercise_dashboard(self):
        """Exercise selection dashboard displayed ONLY when robot comes to a stop after following."""
        try:
            publish_robot_state("READY")
        except Exception:
            pass
        self.exercise_active = False
        self.home_clicked = False
        self.configure(bg="#000000")
        self.main_container.configure(bg="#000000")
        self.main_container.pack_configure(padx=8, pady=6)

        for widget in self.main_container.winfo_children():
            widget.destroy()

        try:
            send_command("STOP")
        except Exception:
            pass

        self.stop_home_camera()
        self.home_eyes = None

        # Title Header in solid Lime #C6FF34 without outline
        title_label = tk.Label(
            self.main_container,
            text="CHOOSE YOUR WORKOUT",
            font=("Helvetica", 16, "bold"),
            fg="#C6FF34",
            bg="#000000"
        )
        title_label.pack(pady=(6, 6))

        # Exercise Grid - 4 Exercise Modes in 2x2 Grid (Border removed, #7F3AED to #1E0638 gradient, #C6FF34 text)
        grid_frame = tk.Frame(self.main_container, bg="#000000")
        grid_frame.pack(fill="both", expand=True, padx=4, pady=(0, 6))

        exercises = [
            ("Bicep Curls", self.launch_bicep_curls),
            ("Push-ups", self.launch_pushups),
            ("Squats", self.launch_squats),
            ("Plank", self.launch_plank),
        ]

        for i, (name, callback) in enumerate(exercises):
            btn = RoundedButton(
                grid_frame,
                text=name,
                command=callback,
                bg_color="#7F3AED",
                gradient_color="#1E0638",
                fg_color="#C6FF34",
                outline_color=None,
                outline_width=0,
                radius=14,
                font=("Helvetica", 14, "bold"),
                height=52
            )
            row = i // 2
            col = i % 2
            btn.grid(row=row, column=col, sticky="nsew", padx=6, pady=4)

        for col_idx in range(2):
            grid_frame.columnconfigure(col_idx, weight=1)
        grid_frame.rowconfigure(0, weight=1)
        grid_frame.rowconfigure(1, weight=1)

        # System Bar: Reboot, Shutdown, Exit
        sys_frame = tk.Frame(self.main_container, bg="#000000")
        sys_frame.pack(fill="x", pady=(2, 2))
        for i in range(3):
            sys_frame.columnconfigure(i, weight=1)

        sys_buttons = [
            ("REBOOT", self.confirm_reboot, "#7F3AED"),
            ("SHUTDOWN", self.confirm_shutdown, "#7F3AED"),
            ("EXIT APP", self.quit_app, "#7F3AED"),
        ]

        for i, (name, callback, color) in enumerate(sys_buttons):
            btn = RoundedButton(
                sys_frame,
                text=name,
                command=callback,
                bg_color="#18072B",
                gradient_color="#0A0214",
                fg_color=color,
                outline_color=None,
                outline_width=0,
                radius=8,
                font=("Helvetica", 9, "bold"),
                height=28
            )
            btn.grid(row=0, column=i, sticky="ew", padx=4)

        self.voice_status_label = tk.Label(
            self.main_container,
            text="🎤 Voice: Say 'FitBot start <exercise>' or 'FitBot follow me'",
            font=("Helvetica", 9, "bold"),
            fg="#C6FF34",
            bg="#000000"
        )
        self.voice_status_label.pack(side="bottom", pady=(2, 2))

    def update_follower_eyes(self, person_x=None, person_y=None, command="STOP", state="TRACKING", d_zone="GOOD", h_zone="CENTER"):
        """Update the Googly Eyes and UI labels during Person Following mode."""
        try:
            if not self.winfo_exists():
                return
            if hasattr(self, 'follower_eyes') and self.follower_eyes is not None and self.follower_eyes.winfo_exists():
                if command == "SEARCH" or "SEARCH" in str(state).upper():
                    self.follower_eyes.set_mode("SEARCHING")
                elif state == "LOST" or person_x is None:
                    self.follower_eyes.set_mode("LOST")
                else:
                    norm_x = (person_x - 0.5) * 2.0
                    norm_y = (person_y - 0.5) * 2.0 if person_y is not None else 0.0
                    self.follower_eyes.set_target(norm_x, norm_y, mode="FOLLOWING")
        except Exception:
            pass

    def update_stat_card(self, label, value, color="#96D75F"):
        try:
            if not self.winfo_exists():
                return
            color = bgr_to_hex(color)
            if label not in self.stat_cards:
                card = RoundedFrame(
                    self.stats_row_frame,
                    bg_color="#000000",
                    border_color=color,
                    border_width=2,
                    radius=10,
                    width=110,
                    height=75
                )
                card.pack(side="left", padx=3)
                card.pack_propagate(False)

                lbl = tk.Label(card, text=f"{label}:", font=("Helvetica", 10, "bold"), fg=color, bg="#000000")
                lbl.pack(anchor="w", padx=10, pady=(10, 2))

                val_lbl = tk.Label(card, text=str(value), font=("Helvetica", 18, "bold"), fg="#FFFFFF", bg="#000000")
                val_lbl.pack(anchor="w", padx=10)

                self.stat_cards[label] = (card, val_lbl, lbl)
            else:
                card, val_lbl, lbl = self.stat_cards[label]
                val_lbl.configure(text=str(value))
                card.border_color = color
                card._draw()
                lbl.configure(fg=color)

            if label == "Set":
                try:
                    val_int = int(value)
                    if hasattr(self, 'is_resting') and self.is_resting:
                        pass
                    elif val_int >= 4:
                        if hasattr(self, 'set_count_warning_label'):
                            self.set_count_warning_label.configure(text="Required Set Count is Over.", fg="#FF3B30")
                    else:
                        if hasattr(self, 'set_count_warning_label'):
                            self.set_count_warning_label.configure(text="", fg="#FF3B30")
                except (ValueError, TypeError):
                    pass
        except tk.TclError:
            pass

    def update_feedback(self, text, color="#EBEBEF"):
        try:
            if not self.winfo_exists():
                return
            color = bgr_to_hex(color)
            self.feedback_label.configure(text=text, fg=color)
        except tk.TclError:
            pass

    def set_home_clicked(self):
        self.home_clicked = True
        self.next_remote_mode = None
        start_exercise.clear()
        force_stop.clear()
        while not remote_mode_queue.empty():
            try:
                remote_mode_queue.get_nowait()
            except Exception:
                break

    def set_start_clicked(self):
        start_exercise.set()
        self.hide_start_button()

    def hide_start_button(self):
        if hasattr(self, 'start_btn') and self.start_btn is not None:
            try:
                if self.start_btn.winfo_exists():
                    self.start_btn.pack_forget()
            except tk.TclError:
                pass

    def show_summary_screen(self):
        global last_session_summary
        if not last_session_summary:
            self.show_home_screen()
            return

        try:
            publish_robot_state("READY")
        except Exception:
            pass

        summary = last_session_summary
        last_session_summary = None

        if hasattr(self, 'summary_timer_id') and self.summary_timer_id is not None:
            try:
                self.after_cancel(self.summary_timer_id)
            except Exception:
                pass
            self.summary_timer_id = None

        # Display summary for 5 seconds after exercise, then return to eyes screen
        self.summary_timer_id = self.after(5000, self.show_home_screen)

        for widget in self.main_container.winfo_children():
            widget.destroy()

        try:
            send_command("STOP")
        except Exception:
            pass

        title_label = tk.Label(
            self.main_container,
            text=f"SESSION SUMMARY: {summary['exercise'].upper()}",
            font=("Helvetica", 14, "bold"),
            fg="#96D75F",
            bg="#000000"
        )
        title_label.pack(pady=(6, 8))

        # Directly pack card_frame without scrollbar
        card_frame = tk.Frame(self.main_container, bg="#000000")
        card_frame.pack(fill="both", expand=True, padx=16, pady=(0, 4))

        tk.Label(
            card_frame,
            text="Performance Metrics",
            font=("Helvetica", 15, "bold"),
            fg="#50AFDF",
            bg="#000000"
        ).pack(anchor="w", pady=(2, 4))

        for metric, val in summary['metrics']:
            lbl = tk.Label(
                card_frame,
                text=f"• {metric}: {val}",
                font=("Helvetica", 13, "bold"),
                fg="#FFFFFF",
                bg="#000000"
            )
            lbl.pack(anchor="w", pady=2, padx=6)

        if summary.get('errors'):
            tk.Label(
                card_frame,
                text="Form Warnings / Mistakes",
                font=("Helvetica", 15, "bold"),
                fg="#E5B05E",
                bg="#000000"
            ).pack(anchor="w", pady=(8, 4))
            for err in set(summary['errors']):
                lbl = tk.Label(
                    card_frame,
                    text=f"⚠️ {err}",
                    font=("Helvetica", 13, "bold"),
                    fg="#E5B05E",
                    bg="#000000",
                    wraplength=440,
                    justify="left"
                )
                lbl.pack(anchor="w", pady=2, padx=6)
        else:
            tk.Label(
                card_frame,
                text="✓ Perfect posture! No mistakes detected.",
                font=("Helvetica", 14, "bold"),
                fg="#C6FF34",
                bg="#000000"
            ).pack(anchor="w", pady=(10, 6), padx=4)

        btn = RoundedButton(
            self.main_container,
            text="BACK TO EYES",
            command=self.show_home_screen,
            bg_color="#7F3AED",
            gradient_color="#1E0638",
            fg_color="#C6FF34",
            outline_color=None,
            outline_width=0,
            radius=12,
            font=("Helvetica", 13, "bold"),
            width=200,
            height=38
        )
        btn.pack(pady=(4, 8))

    def confirm_shutdown(self):
        if messagebox.askyesno("Shutdown", "Are you sure you want to shutdown the Raspberry Pi?"):
            self.destroy()
            if os.name == 'posix':
                subprocess.run(["sudo", "shutdown", "-h", "now"])
            else:
                subprocess.run(["shutdown", "/s", "/t", "0"])

    def confirm_reboot(self):
        if messagebox.askyesno("Reboot", "Are you sure you want to reboot the Raspberry Pi?"):
            self.destroy()
            if os.name == 'posix':
                subprocess.run(["sudo", "reboot"])
            else:
                subprocess.run(["shutdown", "/r", "/t", "0"])

    def quit_app(self):
        self.stop_home_camera()
        try:
            terminate_current_audio()
        except Exception:
            pass
        if stop_voice_listener is not None:
            try:
                stop_voice_listener()
            except Exception:
                pass
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        self.destroy()
        os._exit(0)


if __name__ == "__main__":
    import time

    def mock_exercise_runner(exercise_name):
        def _runner(ui):
            print(f"[TEST RUNNER] Starting simulated exercise: {exercise_name}")
            ui.update_stat_card("Reps", "10")
            ui.update_stat_card("Target", "15")
            ui.set_feedback("Great form! Keep going...")
            ui.update()
            time.sleep(2)
            return {
                'exercise': exercise_name,
                'metrics': [('Reps Completed', '10'), ('Duration', '32.5s'), ('Target Reps', '15')],
                'errors': ['Watch back posture on repetition 5']
            }
        return _runner

    test_callbacks = {
        "person_follow": mock_exercise_runner("Person Follower"),
        "bicep_curls": mock_exercise_runner("Bicep Curls"),
        "pushups": mock_exercise_runner("Push-ups"),
        "squats": mock_exercise_runner("Squats"),
        "plank": mock_exercise_runner("Plank"),
    }

    print("==================================================")
    print(" Fitness Robot Touchscreen Dashboard (Preview Mode)")
    print("==================================================")
    print("Running in windowed mode (480x320).")
    print("Click any exercise button to test exercise & summary screens.")
    print("Press ESC or click EXIT APP to quit.")
    print("==================================================")

    app = FitnessRobotUI(exercise_callbacks=test_callbacks, fullscreen=False)
    app.mainloop()
