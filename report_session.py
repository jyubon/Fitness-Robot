"""
report_session.py
Helper module for the Fitness Robot to persist exercise sessions to MongoDB
via the FastAPI backend.

Usage (in your exercise scripts):
    from report_session import report_session
    from datetime import datetime, timezone

    start = datetime.now(timezone.utc)
    # ... run exercise ...
    end = datetime.now(timezone.utc)

    report_session(
        user_email="you@example.com",
        exercise_type="BICEP_CURLS",
        start_time=start,
        end_time=end,
        duration_sec=int((end - start).total_seconds()),
        reps=42,
        left_reps=21,
        right_reps=21,
        form_good_pct=87.0,
        form_bad_pct=13.0,
        notes="Solid session"
    )

Environment variables:
    FITBOT_API            Base URL of the backend (default: http://localhost:8000)
    FITBOT_USER_EMAIL     Default user email if you don't pass one (optional)
"""

import os
import requests
from datetime import datetime
from typing import Optional

API_BASE = os.getenv("FITBOT_API", "http://localhost:8000")
DEFAULT_USER_EMAIL = os.getenv("FITBOT_USER_EMAIL", "athlete@local.dev")


def report_session(
    *,
    user_email: Optional[str] = None,
    exercise_type: str,
    start_time: datetime,
    end_time: datetime,
    duration_sec: int,
    reps: Optional[int] = None,
    left_reps: Optional[int] = None,
    right_reps: Optional[int] = None,
    hold_time_sec: Optional[float] = None,
    form_good_pct: Optional[float] = None,
    form_bad_pct: Optional[float] = None,
    notes: Optional[str] = None,
) -> None:
    """
    Fire-and-forget POST of a completed exercise session to the backend.

    Never raises — logs to console only. This keeps the robot running even
    if the dashboard backend is temporarily unavailable.
    """
    email = (user_email or DEFAULT_USER_EMAIL).lower()

    payload = {
        "userEmail": email,
        "exerciseType": exercise_type,
        "startTime": start_time.isoformat(),
        "endTime": end_time.isoformat(),
        "durationSec": max(0, int(duration_sec)),
    }

    if reps is not None:
        payload["reps"] = int(reps)
    if left_reps is not None:
        payload["leftReps"] = int(left_reps)
    if right_reps is not None:
        payload["rightReps"] = int(right_reps)
    if hold_time_sec is not None:
        payload["holdTimeSec"] = float(hold_time_sec)
    if form_good_pct is not None:
        payload["formGoodPct"] = float(form_good_pct)
    if form_bad_pct is not None:
        payload["formBadPct"] = float(form_bad_pct)
    if notes:
        payload["notes"] = str(notes)[:500]

    try:
        resp = requests.post(
            f"{API_BASE.rstrip('/')}/api/sessions",
            json=payload,
            timeout=6,
        )
        if resp.status_code == 201:
            sid = resp.json().get("id") or resp.json().get("_id") or "ok"
            print(f"[REPORT] ✅ Session saved to MongoDB (id={sid})")
        else:
            print(f"[REPORT] ⚠️  Backend returned {resp.status_code}: {resp.text[:180]}")
    except Exception as e:
        print(f"[REPORT] ⚠️  Could not reach backend at {API_BASE}: {e}")


# Quick self-test (optional)
if __name__ == "__main__":
    from datetime import timedelta, timezone as tz
    now = datetime.now(tz.utc)
    report_session(
        exercise_type="BICEP_CURLS",
        start_time=now - timedelta(minutes=12),
        end_time=now,
        duration_sec=12 * 60,
        reps=28,
        left_reps=14,
        right_reps=14,
        form_good_pct=89,
        form_bad_pct=11,
        notes="Test report from report_session.py",
    )
    print("Test report sent (check your backend / Mongo).")