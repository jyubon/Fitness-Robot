#!/usr/bin/env python3
"""
UI Overlays and Visual Drawing for Fitness Robot.
Handles OpenCV glass panels, HUD stats, skeleton rendering, and pause/start overlays.
"""

import cv2
import numpy as np

from config import (
    DISPLAY_WIDTH,
    DISPLAY_HEIGHT,
    CENTER_ZONE_LEFT,
    CENTER_ZONE_RIGHT,
    TOO_CLOSE,
    TOO_FAR,
    PALM_STOP_REQUIRED_FRAMES,
    ACCENT_GREEN,
    ACCENT_RED,
    ACCENT_ORANGE,
    ACCENT_CYAN,
    TEXT_WHITE,
    TEXT_DIM,
    SKELETON_LANDMARK,
    SKELETON_LINE,
    CMD_COLORS,
)


def bgr_to_hex(color):
    """Convert a BGR tuple or hex string to a Tkinter-compatible hex string."""
    if isinstance(color, str):
        return color
    if isinstance(color, (tuple, list)) and len(color) >= 3:
        b, g, r = int(color[0]), int(color[1]), int(color[2])
        return f"#{r:02x}{g:02x}{b:02x}"
    return "#EBEBEF"


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


def draw_home_button(frame):
    """Draw a touch-friendly HOME button on OpenCV frame."""
    draw_glass_panel(frame, 390, 5, 80, 40, alpha=0.75, color=(30, 30, 80), radius=8)
    cv2.rectangle(frame, (390, 5), (470, 45), (100, 100, 200), 1, cv2.LINE_AA)
    cv2.putText(frame, "HOME", (406, 31), cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT_WHITE, 2, cv2.LINE_AA)


def _draw_mini_arrow(frame, cx, cy, command, color, size=5):
    """Draw small direction arrow or stop dot for zone grid cells."""
    if command == 'STOP':
        cv2.circle(frame, (cx, cy), 2, color, -1)
        return

    directions = {
        'FORWARD':   (0, -size),
        'BACKWARD':  (0,  size),
        'LEFT':      (-size, 0),
        'RIGHT':     ( size, 0),
        'FWD_LEFT':  (-size + 2, -size + 2),
        'FWD_RIGHT': ( size - 2, -size + 2),
        'BWD_LEFT':  (-size + 2,  size - 2),
        'BWD_RIGHT': ( size - 2,  size - 2),
    }

    dx, dy = directions.get(command, (0, 0))
    if dx == 0 and dy == 0:
        return

    end = (cx + dx, cy + dy)
    cv2.arrowedLine(frame, (cx, cy), end, color, 1, tipLength=0.5)


def draw_direction_indicator(frame, command, cx, cy, size=40):
    """Draw compass rose direction indicator."""
    pad = size + 10
    draw_glass_panel(frame, cx - pad, cy - pad, pad * 2, pad * 2, alpha=0.7, radius=pad)
    cv2.circle(frame, (cx, cy), size + 3, (65, 65, 70), 1)

    directions = {
        'FORWARD':   (0, -size),
        'BACKWARD':  (0,  size),
        'LEFT':      (-size, 0),
        'RIGHT':     ( size, 0),
        'FWD_LEFT':  (-size + 8, -size + 8),
        'FWD_RIGHT': ( size - 8, -size + 8),
        'BWD_LEFT':  (-size + 8,  size - 8),
        'BWD_RIGHT': ( size - 8,  size - 8),
        'STOP':      (0, 0),
    }

    for cmd, (dx, dy) in directions.items():
        pos = (cx + dx, cy + dy)
        cv2.circle(frame, pos, 4, (55, 55, 60), -1)

    if command in directions:
        dx, dy = directions[command]
        active = (cx + dx, cy + dy)
        color = CMD_COLORS.get(command, (255, 255, 255))
        cv2.circle(frame, active, 6, color, -1)
        cv2.circle(frame, active, 9, color, 1)
        if command != 'STOP':
            cv2.arrowedLine(frame, (cx, cy), active, color, 2, tipLength=0.3)


def draw_zone_grid(frame, d_zone, h_zone, gx, gy):
    """Draw 3x3 zone grid."""
    cell_w, cell_h = 38, 22
    total_w, total_h = cell_w * 3, cell_h * 3

    draw_glass_panel(frame, gx - 3, gy - 3, total_w + 6, total_h + 6, alpha=0.7, radius=6)

    grid = [
        [("FWD_LEFT", "FAR", "LEFT"),   ("FORWARD", "FAR", "CENTER"),   ("FWD_RIGHT", "FAR", "RIGHT")],
        [("LEFT", "GOOD", "LEFT"),       ("STOP", "GOOD", "CENTER"),     ("RIGHT", "GOOD", "RIGHT")],
        [("BWD_LEFT", "CLOSE", "LEFT"),  ("BACKWARD", "CLOSE", "CENTER"), ("BWD_RIGHT", "CLOSE", "RIGHT")],
    ]
    labels = [
        ["FL", "FW", "FR"],
        ["L",  "ST", "R"],
        ["BL", "BW", "BR"],
    ]

    for row in range(3):
        for col in range(3):
            x1 = gx + col * cell_w
            y1 = gy + row * cell_h
            x2 = x1 + cell_w - 1
            y2 = y1 + cell_h - 1

            cmd, dz, hz = grid[row][col]

            if dz == d_zone and hz == h_zone:
                color = CMD_COLORS.get(cmd, (255, 255, 255))
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, -1)
                txt_color = (0, 0, 0)
            else:
                txt_color = (100, 100, 100)

            cv2.rectangle(frame, (x1, y1), (x2, y2), (55, 55, 60), 1)
            cx = x1 + cell_w // 2
            cy = y1 + 8
            _draw_mini_arrow(frame, cx, cy, cmd, txt_color, size=4)
            cv2.putText(frame, labels[row][col], (x1 + 3, y1 + cell_h - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.28, txt_color, 1)


def draw_frame(frame, command, state, person_x, body_height, fps, d_zone, h_zone):
    """Draw complete follower status overlay for person_following mode."""
    h, w = frame.shape[:2]

    zone_l = int(CENTER_ZONE_LEFT * w)
    zone_r = int(CENTER_ZONE_RIGHT * w)

    ov = frame.copy()
    cv2.rectangle(ov, (0, 0), (zone_l, h), (200, 140, 60), -1)
    cv2.addWeighted(ov, 0.06, frame, 0.94, 0, frame)

    ov = frame.copy()
    cv2.rectangle(ov, (zone_r, 0), (w, h), (80, 80, 200), -1)
    cv2.addWeighted(ov, 0.06, frame, 0.94, 0, frame)

    cv2.line(frame, (zone_l, 0), (zone_l, h), (180, 180, 80), 1)
    cv2.line(frame, (zone_r, 0), (zone_r, h), (180, 180, 80), 1)

    if person_x is not None:
        px = int(person_x * w)
        cv2.circle(frame, (px, h // 2), 14, (130, 220, 220), 1)
        cv2.circle(frame, (px, h // 2), 8, (130, 220, 220), -1)
        cv2.circle(frame, (px, h // 2), 8, (0, 0, 0), 1)

    draw_glass_panel(frame, 0, 0, w, 50, alpha=0.7, radius=0)
    cmd_color = CMD_COLORS.get(command, TEXT_WHITE)
    cv2.putText(frame, command, (12, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.65, cmd_color, 2)
    cv2.putText(frame, f"FPS:{fps:.0f}", (12, 43), cv2.FONT_HERSHEY_SIMPLEX, 0.35, TEXT_DIM, 1)

    dot = ACCENT_GREEN if person_x is not None else ACCENT_RED
    cv2.circle(frame, (w - 15, 15), 12, dot, 1)
    cv2.circle(frame, (w - 15, 15), 7, dot, -1)

    if person_x is not None:
        cv2.putText(frame, state, (w // 2 - 40, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.3, TEXT_DIM, 1)

    draw_direction_indicator(frame, command, w - 30, 95, size=25)
    draw_zone_grid(frame, d_zone, h_zone, 5, h - 75)

    if body_height is not None:
        bar_start = 125
        bar_end = w - 5
        bar_y = h - 22
        bar_h = 14
        bar_max = TOO_CLOSE + 0.15

        bar_pct = min(body_height / bar_max, 1.0)
        bar_w = int(bar_pct * (bar_end - bar_start))

        if body_height > TOO_CLOSE:
            bc = ACCENT_RED
        elif body_height < TOO_FAR:
            bc = ACCENT_ORANGE
        else:
            bc = ACCENT_GREEN

        draw_glass_panel(frame, bar_start - 3, bar_y - 14, bar_end - bar_start + 6, bar_h + 17, alpha=0.6, radius=6)
        cv2.rectangle(frame, (bar_start, bar_y), (bar_end, bar_y + bar_h), (40, 40, 40), -1)
        cv2.rectangle(frame, (bar_start, bar_y), (bar_start + bar_w, bar_y + bar_h), bc, -1)

        fm = int((TOO_FAR / bar_max) * (bar_end - bar_start)) + bar_start
        cm = int((TOO_CLOSE / bar_max) * (bar_end - bar_start)) + bar_start
        cv2.line(frame, (fm, bar_y - 2), (fm, bar_y + bar_h + 2), ACCENT_ORANGE, 1)
        cv2.line(frame, (cm, bar_y - 2), (cm, bar_y + bar_h + 2), ACCENT_RED, 1)
        cv2.putText(frame, f"HEIGHT: {body_height:.3f}", (bar_start, bar_y - 3),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.3, TEXT_DIM, 1)


def draw_skeleton(frame, results):
    """Draw MediaPipe skeleton."""
    if not results or not results.pose_landmarks or len(results.pose_landmarks) == 0:
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
            cv2.circle(frame, (int(pt.x * w), int(pt.y * h)), 3, SKELETON_LANDMARK, -1)


def draw_follow_phase_overlay(image, position):
    """Draw glass overlay indicating robot is following user during workout setup."""
    h, w = image.shape[:2]
    overlay = image.copy()
    cv2.rectangle(overlay, (0, 0), (w, h), (10, 10, 15), -1)
    cv2.addWeighted(overlay, 0.45, image, 0.55, 0, image)

    panel_w = int(w * 0.8)
    panel_h = int(h * 0.4)
    px = (w - panel_w) // 2
    py = (h - panel_h) // 2
    draw_glass_panel(image, px, py, panel_w, panel_h, alpha=0.85, color=(15, 15, 20), radius=16)
    cv2.rectangle(image, (px, py), (px + panel_w, py + panel_h), (80, 100, 120), 1, cv2.LINE_AA)

    cx = w // 2
    cy = py + int(panel_h * 0.3)
    cv2.circle(image, (cx, cy), 12, ACCENT_CYAN, -1, cv2.LINE_AA)

    msg1 = "ROBOT FOLLOWING"
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw1, th1), _ = cv2.getTextSize(msg1, font, 0.55, 2)
    cv2.putText(image, msg1, (w // 2 - tw1 // 2, cy + 28), font, 0.55, TEXT_WHITE, 2, cv2.LINE_AA)

    msg2 = "Get in Position and Press the Start"
    (tw2, th2), _ = cv2.getTextSize(msg2, font, 0.38, 1)
    cv2.putText(image, msg2, (w // 2 - tw2 // 2, cy + 48), font, 0.38, TEXT_DIM, 1, cv2.LINE_AA)


def draw_positioning_overlay(image, position, elapsed_pct):
    """Draw overlay indicating robot alignment."""
    h, w = image.shape[:2]
    overlay = image.copy()
    cv2.rectangle(overlay, (0, 0), (w, h), (10, 10, 15), -1)
    cv2.addWeighted(overlay, 0.55, image, 0.45, 0, image)

    panel_w = int(w * 0.75)
    panel_h = int(h * 0.45)
    px = (w - panel_w) // 2
    py = (h - panel_h) // 2
    draw_glass_panel(image, px, py, panel_w, panel_h, alpha=0.85, color=(15, 15, 20), radius=16)
    cv2.rectangle(image, (px, py), (px + panel_w, py + panel_h), (80, 100, 120), 1, cv2.LINE_AA)

    cx = w // 2
    cy = py + int(panel_h * 0.28)
    r = 20
    cv2.circle(image, (cx, cy), r, (40, 40, 40), 2, cv2.LINE_AA)
    sweep = int(elapsed_pct * 360)
    cv2.ellipse(image, (cx, cy), (r, r), -90, 0, sweep, ACCENT_GREEN, 2, cv2.LINE_AA)

    msg1 = "ROBOT POSITIONING"
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw1, _), _ = cv2.getTextSize(msg1, font, 0.6, 2)
    cv2.putText(image, msg1, (w // 2 - tw1 // 2, cy + 32), font, 0.6, TEXT_WHITE, 2, cv2.LINE_AA)

    msg2 = f"Aligning to {position} view..."
    (tw2, _), _ = cv2.getTextSize(msg2, font, 0.4, 1)
    cv2.putText(image, msg2, (w // 2 - tw2 // 2, cy + 52), font, 0.4, TEXT_DIM, 1, cv2.LINE_AA)

    progress_w = int(panel_w * 0.7)
    progress_h = 6
    bx = w // 2 - progress_w // 2
    by = py + int(panel_h * 0.8)

    cv2.rectangle(image, (bx, by), (bx + progress_w, by + progress_h), (40, 40, 40), -1)
    cv2.rectangle(image, (bx, by), (bx + int(progress_w * elapsed_pct), by + progress_h), ACCENT_GREEN, -1)
    cv2.rectangle(image, (bx, by), (bx + progress_w, by + progress_h), (60, 60, 60), 1)


def draw_hand_controls_status(image, thumbs_up_frames, palm_stop_frames, required_frames=PALM_STOP_REQUIRED_FRAMES, is_paused=False):
    """Draw thumbs-up and palm-stop progress panels dynamically."""
    h, w = image.shape[:2]
    compact = h <= 240
    panel_w = 180 if compact else 205
    panel_h = 45 if compact else 53
    x = max(5, w - panel_w - 5)
    current_y = h - panel_h - 5

    # 1. Palm Stop Panel
    if palm_stop_frames > 0:
        draw_glass_panel(image, x, current_y, panel_w, panel_h, alpha=0.65, radius=10)
        progress = min(palm_stop_frames / required_frames, 1.0)
        color = ACCENT_GREEN if progress >= 1.0 else ACCENT_ORANGE
        cv2.putText(image, "PALM STOP", (x + 8, current_y + 17),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38 if compact else 0.45, color, 1, cv2.LINE_AA)
        label = "STOPPING" if progress >= 1.0 else "HOLD PALM"
        cv2.putText(image, label, (x + 8, current_y + 36),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38 if compact else 0.45, TEXT_WHITE, 1, cv2.LINE_AA)
        bar_x = x + (95 if compact else 108)
        bar_y = current_y + (29 if compact else 34)
        bar_w, bar_h = (70, 7) if compact else (82, 8)
        cv2.rectangle(image, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (40, 40, 40), -1)
        cv2.rectangle(image, (bar_x, bar_y), (bar_x + int(bar_w * progress), bar_y + bar_h), color, -1)
        cv2.rectangle(image, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (60, 60, 60), 1)
        current_y -= (panel_h + 7)

    # 2. Thumbs Up Panel
    if thumbs_up_frames > 0:
        draw_glass_panel(image, x, current_y, panel_w, panel_h, alpha=0.65, radius=10)
        progress = min(thumbs_up_frames / required_frames, 1.0)
        color = ACCENT_GREEN if progress >= 1.0 else ACCENT_CYAN
        cv2.putText(image, "THUMBS UP", (x + 8, current_y + 17),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38 if compact else 0.45, color, 1, cv2.LINE_AA)
        label = ("PAUSING..." if not is_paused else "RESUMING...") if progress >= 1.0 else "HOLD THUMB"
        cv2.putText(image, label, (x + 8, current_y + 36),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38 if compact else 0.45, TEXT_WHITE, 1, cv2.LINE_AA)
        bar_x = x + (95 if compact else 108)
        bar_y = current_y + (29 if compact else 34)
        bar_w, bar_h = (70, 7) if compact else (82, 8)
        cv2.rectangle(image, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (40, 40, 40), -1)
        cv2.rectangle(image, (bar_x, bar_y), (bar_x + int(bar_w * progress), bar_y + bar_h), color, -1)
        cv2.rectangle(image, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (60, 60, 60), 1)


def show_palm_stop_message(window_name, image):
    """Show brief confirmation before leaving window."""
    h, w = image.shape[:2]
    panel_w = min(220, w - 20)
    panel_h = 48 if h <= 240 else 60
    x = w // 2 - panel_w // 2
    y = h // 2 - panel_h // 2
    draw_glass_panel(image, x, y, panel_w, panel_h, alpha=0.78, radius=12)
    cv2.putText(image, "PALM STOP DETECTED", (x + 16, y + panel_h // 2 + 5),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42 if h <= 240 else 0.55, ACCENT_GREEN, 1 if h <= 240 else 2, cv2.LINE_AA)
    display = cv2.resize(image, (DISPLAY_WIDTH, DISPLAY_HEIGHT))
    cv2.imshow(window_name, display)
    cv2.waitKey(500)


def draw_paused_overlay(image, thumbs_up_frames=0, required_frames=PALM_STOP_REQUIRED_FRAMES):
    """Draw dark glass overlay when paused."""
    h, w = image.shape[:2]
    overlay = image.copy()
    cv2.rectangle(overlay, (0, 0), (w, h), (10, 10, 15), -1)
    cv2.addWeighted(overlay, 0.6, image, 0.4, 0, image)

    panel_w = int(w * 0.75)
    panel_h = int(h * 0.5)
    px = (w - panel_w) // 2
    py = (h - panel_h) // 2
    draw_glass_panel(image, px, py, panel_w, panel_h, alpha=0.85, color=(15, 15, 20), radius=16)
    cv2.rectangle(image, (px, py), (px + panel_w, py + panel_h), (80, 100, 120), 1, cv2.LINE_AA)
    cv2.rectangle(image, (px - 2, py - 2), (px + panel_w + 2, py + panel_h + 2), (40, 50, 60), 1, cv2.LINE_AA)

    icon_cx = w // 2
    icon_cy = py + int(panel_h * 0.28)
    bar_w, bar_h = 8, 24
    bar_gap = 8
    cv2.rectangle(image, (icon_cx - bar_w - bar_gap//2, icon_cy - bar_h//2),
                  (icon_cx - bar_gap//2, icon_cy + bar_h//2), ACCENT_ORANGE, -1)
    cv2.rectangle(image, (icon_cx + bar_gap//2, icon_cy - bar_h//2),
                  (icon_cx + bar_w + bar_gap//2, icon_cy + bar_h//2), ACCENT_ORANGE, -1)

    msg1 = "EXERCISE PAUSED"
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw1, _), _ = cv2.getTextSize(msg1, font, 0.65, 2)
    cv2.putText(image, msg1, (w // 2 - tw1 // 2, icon_cy + 30), font, 0.65, TEXT_WHITE, 2, cv2.LINE_AA)

    msg2 = "Show Thumbs-Up to Resume"
    (tw2, _), _ = cv2.getTextSize(msg2, font, 0.4, 1)
    cv2.putText(image, msg2, (w // 2 - tw2 // 2, icon_cy + 52), font, 0.4, TEXT_DIM, 1, cv2.LINE_AA)

    if thumbs_up_frames > 0:
        progress = min(thumbs_up_frames / required_frames, 1.0)
        progress_w = int(panel_w * 0.6)
        progress_h = 6
        bx = w // 2 - progress_w // 2
        by = py + int(panel_h * 0.8)

        cv2.rectangle(image, (bx, by), (bx + progress_w, by + progress_h), (40, 40, 40), -1)
        cv2.rectangle(image, (bx, by), (bx + int(progress_w * progress), by + progress_h), ACCENT_GREEN, -1)
        cv2.rectangle(image, (bx, by), (bx + progress_w, by + progress_h), (60, 60, 60), 1)
