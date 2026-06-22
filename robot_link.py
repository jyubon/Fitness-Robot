"""
robot_link.py
Bidirectional link between the Fitness Robot (Pi/PC) and the dashboard backend.

- Push live robot outputs (mode, reps, plank time, ESP commands, messages) so the web UI can display them in real time.
- Receive commands from the dashboard (switch mode, terminate, force stop).

Usage in your robot scripts (e.g. v.1.1.py or person_raspi.py):

    from robot_link import post_state, poll_commands, ack_command
    from datetime import datetime

    # While running an exercise, periodically:
    post_state(
        mode="BICEP_CURLS",
        reps=left + right,
        esp_cmd="STOP",
        esp_spd=0,
        message="Good form",                    # optional short message for the feed
        camera_url="http://192.168.1.50:8080/?action=stream"  # MJPEG stream URL
    )

    # In your main loop or a background thread, check for dashboard commands:
    for cmd in poll_commands():
        print("Dashboard command:", cmd)
        # handle SWITCH_MODE / TERMINATE / FORCE_STOP
        ack_command(cmd["id"], "done")

Environment variables (same pattern as report_session):
    FITBOT_API            Base URL of the backend (default: http://localhost:8000)
    FITBOT_USER_EMAIL     Default user email
"""

import os
import requests
from typing import Optional, List, Dict, Any

API_BASE = os.getenv("FITBOT_API", "http://localhost:8000")
DEFAULT_USER_EMAIL = os.getenv("FITBOT_USER_EMAIL", "athlete@local.dev")


def post_state(
    *,
    user_email: Optional[str] = None,
    mode: Optional[str] = None,
    reps: Optional[int] = None,
    plank_time: Optional[float] = None,
    esp_cmd: Optional[str] = None,
    esp_spd: Optional[int] = None,
    message: Optional[str] = None,
    camera_url: Optional[str] = None,   # NEW: live camera stream URL (MJPEG recommended)
) -> None:
    """
    Fire-and-forget push of live robot state.
    Call this frequently (e.g. every frame or every 500-1000ms) during an exercise.
    The dashboard polls /api/robot/state to show real-time outputs.
    """
    email = (user_email or DEFAULT_USER_EMAIL).lower()

    payload: Dict[str, Any] = {"userEmail": email}

    if mode is not None:
        payload["mode"] = str(mode).upper()
    if reps is not None:
        payload["reps"] = int(reps)
    if plank_time is not None:
        payload["plankTime"] = float(plank_time)
    if esp_cmd is not None:
        payload["espCmd"] = str(esp_cmd).upper()
    if esp_spd is not None:
        payload["espSpd"] = int(esp_spd)
    if message:
        payload["message"] = str(message)[:200]
    if camera_url:
        payload["cameraUrl"] = str(camera_url)

    try:
        requests.post(
            f"{API_BASE.rstrip('/')}/api/robot/state",
            json=payload,
            timeout=2.0,
        )
    except Exception:
        # Never break the robot if the dashboard is unreachable
        pass


def poll_commands(
    user_email: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Ask the backend for recent commands sent from the dashboard.
    Returns a list of command dicts (newest first), e.g.:
        [{"id": "...", "action": "SWITCH_MODE", "mode": "PUSHUPS", "status": "pending", ...}, ...]
    The robot should act on them and then ack them.
    """
    email = (user_email or DEFAULT_USER_EMAIL).lower()
    try:
        resp = requests.get(
            f"{API_BASE.rstrip('/')}/api/robot/commands",
            params={"userEmail": email, "limit": str(limit)},
            timeout=3.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def ack_command(cmd_id: str, status: str = "done") -> None:
    """
    Tell the backend we have handled (or ignored) a command.
    status: "claimed" | "done" | "ignored"
    """
    try:
        requests.post(
            f"{API_BASE.rstrip('/')}/api/robot/commands/{cmd_id}/ack",
            params={"status": status},
            timeout=2.0,
        )
    except Exception:
        pass


def process_dashboard_commands(
    *,
    user_email: Optional[str] = None,
    stop_event=None,
    command_queue=None,
    on_switch_mode=None,
    on_force_stop=None,
) -> List[Dict[str, Any]]:
    """
    Convenience helper: fetch pending commands and do the common actions.

    - If action == "FORCE_STOP" or "TERMINATE": set stop_event if provided.
    - If action == "SWITCH_MODE" and mode present:
        * put the mode into command_queue (if provided)
        * or call on_switch_mode(mode) if you passed a callback
    - Always acks the command.

    Returns the list of commands that were processed.

    Example in your main robot loop or voice thread:

        cmds = process_dashboard_commands(
            stop_event=stop_exercise,
            command_queue=command_queue,
        )
    """
    email = user_email or DEFAULT_USER_EMAIL
    cmds = poll_commands(email)
    acted = []

    for c in cmds:
        cid = c.get("id") or c.get("_id")
        action = (c.get("action") or "").upper()
        mode = c.get("mode")

        if action in ("FORCE_STOP", "TERMINATE"):
            if stop_event is not None:
                try:
                    stop_event.set()
                except Exception:
                    pass
            if on_force_stop:
                try:
                    on_force_stop()
                except Exception:
                    pass
            acted.append(c)

        elif action == "SWITCH_MODE" and mode:
            m = str(mode).upper()
            if command_queue is not None:
                try:
                    command_queue.put(m)
                except Exception:
                    pass
            if on_switch_mode:
                try:
                    on_switch_mode(m)
                except Exception:
                    pass
            acted.append(c)

        elif action == "START" and mode:
            m = str(mode).upper()
            if command_queue is not None:
                try:
                    command_queue.put(m)
                except Exception:
                    pass
            acted.append(c)

        # Ack so the dashboard knows we saw it (even if we chose to ignore)
        if cid:
            ack_command(cid, "done")

    return acted
