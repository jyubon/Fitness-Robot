#!/usr/bin/env python3
"""
Custom Tkinter UI widgets and the main touch interface for Fitness Robot.
"""

import os
import subprocess
import tkinter as tk
from tkinter import messagebox, ttk

from audio_manager import terminate_current_audio
from session_manager import (
    publish_robot_state,
    force_stop,
    remote_mode_queue,
)
from robot_control import send_command, ser
from ui_overlays import bgr_to_hex
import local_stream_server

# Global state for session summary
last_session_summary = None


class RoundedButton(tk.Canvas):
    def __init__(self, parent, text, command, bg_color, fg_color="#1E1E24", radius=15, font=("Helvetica", 12, "bold"), height=50, width=None, outline_color=None, outline_width=3, **kwargs):
        super().__init__(parent, borderwidth=0, highlightthickness=0, bg=parent["bg"], height=height, **kwargs)
        if width:
            self.configure(width=width)
        self.command = command
        self.bg_color = bg_color
        self.fg_color = fg_color
        self.radius = radius
        self.text = text
        self.font = font
        self.pressed = False
        self.outline_color = outline_color
        self.outline_width = outline_width

        self.bind("<Configure>", self._draw)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _draw(self, event=None):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        r = min(self.radius, w // 2, h // 2)
        bw = self.outline_width if self.outline_color else 0

        if w <= 0 or h <= 0:
            return

        color = self.bg_color
        if self.pressed:
            color = self._darken_color(self.bg_color)

        self.create_oval(bw, bw, r*2+bw, r*2+bw, fill=color, outline=color)
        self.create_oval(w - r*2 - bw, bw, w - bw, r*2+bw, fill=color, outline=color)
        self.create_oval(bw, h - r*2 - bw, r*2+bw, h - bw, fill=color, outline=color)
        self.create_oval(w - r*2 - bw, h - r*2 - bw, w - bw, h - bw, fill=color, outline=color)

        self.create_rectangle(r+bw, bw, w - r - bw, h - bw, fill=color, outline=color)
        self.create_rectangle(bw, r+bw, w - bw, h - r - bw, fill=color, outline=color)

        if self.outline_color and bw > 0:
            self.create_arc(bw, bw, r*2+bw, r*2+bw, start=90, extent=90, style="arc", outline=self.outline_color, width=bw)
            self.create_arc(w - r*2 - bw, bw, w - bw, r*2+bw, start=0, extent=90, style="arc", outline=self.outline_color, width=bw)
            self.create_arc(bw, h - r*2 - bw, r*2+bw, h - bw, start=180, extent=90, style="arc", outline=self.outline_color, width=bw)
            self.create_arc(w - r*2 - bw, h - r*2 - bw, w - bw, h - bw, start=270, extent=90, style="arc", outline=self.outline_color, width=bw)

            self.create_line(r+bw, bw, w - r - bw, bw, fill=self.outline_color, width=bw)
            self.create_line(r+bw, h - bw, w - r - bw, h - bw, fill=self.outline_color, width=bw)
            self.create_line(bw, r+bw, bw, h - r - bw, fill=self.outline_color, width=bw)
            self.create_line(w - bw, r+bw, w - bw, h - r - bw, fill=self.outline_color, width=bw)

        self.create_text(w // 2, h // 2, text=self.text, fill=self.fg_color, font=self.font)

    def _darken_color(self, hex_color):
        try:
            if hex_color.startswith("#"):
                r = int(hex_color[1:3], 16)
                g = int(hex_color[3:5], 16)
                b = int(hex_color[5:7], 16)
                return f"#{max(0, r-30):02x}{max(0, g-30):02x}{max(0, b-30):02x}"
        except Exception:
            pass
        return hex_color

    def _on_press(self, event):
        self.pressed = True
        self._draw()

    def _on_release(self, event):
        self.pressed = False
        self._draw()
        if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            if self.command:
                self.command()

    def _on_enter(self, event):
        self.configure(cursor="hand2")

    def _on_leave(self, event):
        self.configure(cursor="")
        if self.pressed:
            self.pressed = False
            self._draw()


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

    def __init__(self, exercise_callbacks=None):
        super().__init__()
        self.title("Fitness Robot UI")

        self.attributes("-fullscreen", True)
        self.bind("<Escape>", lambda e: self.quit_app())
        self.configure(bg="#1E1E24")

        self.style = ttk.Style(self)
        self.style.theme_use('clam')
        self.style.configure('.', background='#1E1E24', foreground='#EBEBEF')

        self.main_container = tk.Frame(self, bg="#1E1E24")
        self.main_container.pack(fill="both", expand=True, padx=5, pady=5)

        self.exercise_active = False
        self.next_remote_mode = None
        self.home_clicked = False
        self.is_resting = False
        self.stat_cards = {}
        self.exercise_callbacks = exercise_callbacks or {}

        self.show_home_screen()
        self.after(200, self.check_remote_commands)

    def show_home_screen(self):
        try:
            publish_robot_state("READY")
        except Exception:
            pass
        self.exercise_active = False
        self.home_clicked = False

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

        title_canvas = OutlinedLabel(
            self.main_container,
            text="FITNESS ROBOT",
            font=("Helvetica", 24, "bold"),
            fg="#96D75F",
            outline_color="#000000",
            height=40
        )
        title_canvas.pack(pady=(5, 10))

        grid_frame = tk.Frame(self.main_container, bg="#1E1E24")
        grid_frame.pack(fill="both", expand=True)

        exercises = [
            ("Bicep Curls", self.launch_bicep_curls, "#96D75F"),
            ("Push-ups", self.launch_pushups, "#96D75F"),
            ("Squats", self.launch_squats, "#96D75F"),
            ("Plank", self.launch_plank, "#96D75F"),
        ]

        for i, (name, callback, color) in enumerate(exercises):
            btn = RoundedButton(
                grid_frame,
                text=name,
                command=callback,
                bg_color="#2B2B36",
                fg_color=color,
                outline_color=color,
                outline_width=2,
                radius=10,
                font=("Helvetica", 12, "bold"),
                height=45
            )
            row = i // 2
            col = i % 2
            btn.grid(row=row, column=col, sticky="nsew", padx=8, pady=8)

        grid_frame.columnconfigure(0, weight=1)
        grid_frame.columnconfigure(1, weight=1)
        grid_frame.rowconfigure(0, weight=1)
        grid_frame.rowconfigure(1, weight=1)

        sys_frame = tk.Frame(self.main_container, bg="#1E1E24")
        sys_frame.pack(fill="x", pady=(10, 5))
        for i in range(3):
            sys_frame.columnconfigure(i, weight=1)

        sys_buttons = [
            ("REBOOT", self.confirm_reboot, "#E5B05E"),
            ("SHUTDOWN", self.confirm_shutdown, "#5F5FDF"),
            ("EXIT APP", self.quit_app, "#5F5FDF"),
        ]

        for i, (name, callback, color) in enumerate(sys_buttons):
            btn = RoundedButton(
                sys_frame,
                text=name,
                command=callback,
                bg_color="#2B2B36",
                fg_color=color,
                outline_color=color,
                outline_width=2,
                radius=8,
                font=("Helvetica", 10, "bold"),
                height=35
            )
            btn.grid(row=0, column=i, sticky="ew", padx=5)

    def run_exercise(self, exercise_name, func):
        self.exercise_active = True
        self.home_clicked = False
        force_stop.clear()
        try:
            self.show_exercise_screen(exercise_name)
            self.update()
            func(self)
        except tk.TclError:
            return
        finally:
            self.exercise_active = False
            try:
                local_stream_server.update_frame(None)
            except Exception:
                pass
            try:
                exists = self.winfo_exists()
            except tk.TclError:
                exists = False
            if exists:
                if self.next_remote_mode:
                    mode = self.next_remote_mode
                    self.next_remote_mode = None
                    force_stop.clear()
                    self.launch_by_name(mode)
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
                cmd_type, cmd_val = remote_mode_queue.get()
                if cmd_type == "switch":
                    print(f"[GUI] Remote mode switch received: {cmd_val}")
                    if self.exercise_active:
                        print("[GUI] Ignored remote switch mode because an exercise is active.")
                    else:
                        self.launch_by_name(cmd_val)
            self.after(200, self.check_remote_commands)
        except (tk.TclError, Exception):
            pass

    def launch_by_name(self, name):
        name = str(name).upper().replace("-", "_").replace(" ", "_")
        if name in ("BICEP_CURLS", "BICEP_CURL"):
            self.launch_bicep_curls()
        elif name in ("PUSHUPS", "PUSH_UPS"):
            self.launch_pushups()
        elif name in ("SQUATS", "SQUAT"):
            self.launch_squats()
        elif name == "PLANK":
            self.launch_plank()
        elif name in ("PERSON_FOLLOW", "PERSON_TRACKING", "FOLLOWING", "FOLLOW"):
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
        for widget in self.main_container.winfo_children():
            widget.destroy()

        self.is_resting = False
        self.ex_title_label = tk.Label(
            self.main_container,
            text=exercise_name,
            font=("Helvetica", 20, "bold"),
            fg="#96D75F",
            bg="#1E1E24"
        )
        self.ex_title_label.pack(anchor="w", pady=(5, 10), padx=5)

        self.stats_row_frame = tk.Frame(self.main_container, bg="#1E1E24")
        self.stats_row_frame.pack(anchor="w", fill="x", pady=5, padx=5)
        self.stat_cards = {}

        self.set_count_warning_label = tk.Label(
            self.main_container,
            text="",
            font=("Helvetica", 14, "bold"),
            fg="#FF3B30",
            bg="#1E1E24",
            wraplength=450,
            justify="left",
            anchor="w"
        )
        self.set_count_warning_label.pack(anchor="w", fill="x", pady=(5, 0), padx=5)

        self.feedback_label = tk.Label(
            self.main_container,
            text="Aligning and tracking...",
            font=("Helvetica", 14, "bold"),
            fg="#EBEBEF",
            bg="#1E1E24",
            wraplength=450,
            justify="left",
            anchor="w"
        )
        self.feedback_label.pack(anchor="w", fill="x", pady=10, padx=5)

        self.bottom_bar = tk.Frame(self.main_container, bg="#1E1E24")
        self.bottom_bar.pack(fill="x", side="bottom", pady=10)

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

    def update_stat_card(self, label, value, color="#96D75F"):
        try:
            if not self.winfo_exists():
                return
            color = bgr_to_hex(color)
            if label not in self.stat_cards:
                card = RoundedFrame(
                    self.stats_row_frame,
                    bg_color="#2B2B36",
                    border_color=color,
                    border_width=2,
                    radius=10,
                    width=110,
                    height=75
                )
                card.pack(side="left", padx=3)
                card.pack_propagate(False)

                lbl = tk.Label(card, text=f"{label}:", font=("Helvetica", 10, "bold"), fg=color, bg="#2B2B36")
                lbl.pack(anchor="w", padx=10, pady=(10, 2))

                val_lbl = tk.Label(card, text=str(value), font=("Helvetica", 18, "bold"), fg="#FFFFFF", bg="#2B2B36")
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

        self.summary_timer_id = self.after(60000, self.show_home_screen)

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
            bg="#1E1E24"
        )
        title_label.pack(pady=(5, 10))

        card_container = tk.Frame(self.main_container, bg="#2B2B36", height=150)
        card_container.pack(fill="x", padx=10, pady=5)
        card_container.pack_propagate(False)

        canvas = tk.Canvas(card_container, bg="#2B2B36", highlightthickness=0)
        scrollbar = tk.Scrollbar(card_container, orient="vertical", command=canvas.yview)
        card_frame = tk.Frame(canvas, bg="#2B2B36", padx=10, pady=10)

        card_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=card_frame, anchor="nw", width=420)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        tk.Label(card_frame, text="Performance Metrics", font=("Helvetica", 12, "bold"), fg="#50AFDF", bg="#2B2B36").pack(anchor="w", pady=(0, 5))

        for metric, val in summary['metrics']:
            lbl = tk.Label(card_frame, text=f"• {metric}: {val}", font=("Helvetica", 10), fg="#EBEBEF", bg="#2B2B36")
            lbl.pack(anchor="w", pady=1)

        if summary.get('errors'):
            tk.Label(card_frame, text="Form Warnings / Mistakes", font=("Helvetica", 12, "bold"), fg="#E5B05E", bg="#2B2B36").pack(anchor="w", pady=(10, 5))
            for err in set(summary['errors']):
                lbl = tk.Label(card_frame, text=f"⚠️ {err}", font=("Helvetica", 10), fg="#E5B05E", bg="#2B2B36", wraplength=380, justify="left")
                lbl.pack(anchor="w", pady=1)
        else:
            tk.Label(card_frame, text="✅ Perfect posture! No mistakes detected.", font=("Helvetica", 10, "italic"), fg="#96D75F", bg="#2B2B36").pack(anchor="w", pady=(10, 5))

        btn = RoundedButton(
            self.main_container,
            text="BACK TO MENU",
            command=self.show_home_screen,
            bg_color="#2B2B36",
            fg_color="#96D75F",
            outline_color="#96D75F",
            outline_width=2,
            radius=10,
            font=("Helvetica", 12, "bold"),
            width=180,
            height=35
        )
        btn.pack(pady=10)

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
        try:
            terminate_current_audio()
        except Exception:
            pass
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        self.destroy()
        os._exit(0)
