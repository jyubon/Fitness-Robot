#!/usr/bin/env python3
"""
Session Manager and Firebase Telemetry for Fitness Robot.
Handles non-blocking data synchronization with Firebase and local WebSocket clients.
"""

import os
import sys
import json

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import time
import queue
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone

from config import (
    PROJECT_DIR,
    BACKEND_BASE_URL,
    BACKEND_TOKEN,
    FIREBASE_USER_ID,
    FIREBASE_USER_EMAIL,
)
import local_stream_server
from audio_manager import play_voice_command

# ─── Firebase Initialization ───
try:
    import firebase_admin
    from firebase_admin import auth as firebase_auth, credentials, firestore

    cred = credentials.Certificate(str(PROJECT_DIR / "serviceAccountKey.json"))
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("✅ Firebase initialized successfully.")
except Exception as e:
    db = None
    print(f"⚠️ Firebase initialization failed: {e}")

_resolved_firebase_uid = FIREBASE_USER_ID

def set_active_user(user_id, user_email=None):
    """Set or update the active Firebase user ID from authenticated dashboard."""
    global _resolved_firebase_uid
    if not user_id:
        return
    _resolved_firebase_uid = user_id
    print(f"✅ Firebase active user synced from client: {user_id} ({user_email})")
    if db is not None:
        try:
            db.collection("robot_status").document("lock").set({
                "active_user_id": user_id,
                "active_user_email": user_email or "",
                "is_locked": True,
                "connected_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP
            }, merge=True)
            db.collection("robot_accounts").document(str(user_id)).set({
                "uid": str(user_id),
                "email": user_email or "",
                "is_online": True,
                "connected_to_robot": True,
                "last_seen_at": firestore.SERVER_TIMESTAMP
            }, merge=True)
        except Exception as e:
            print(f"⚠️ Could not set Firestore robot lock: {e}")

def clear_active_user():
    """Clear active user lock when disconnected so another account can connect."""
    global _resolved_firebase_uid
    prev_uid = _resolved_firebase_uid
    _resolved_firebase_uid = None
    print("🔓 Active user lock cleared from session manager.")
    if db is not None:
        try:
            db.collection("robot_status").document("lock").set({
                "active_user_id": None,
                "active_user_email": None,
                "is_locked": False,
                "disconnected_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP
            }, merge=True)
            if prev_uid:
                db.collection("robot_accounts").document(str(prev_uid)).set({
                    "connected_to_robot": False,
                    "last_seen_at": firestore.SERVER_TIMESTAMP
                }, merge=True)
            print("🔓 Firestore robot lock released.")
        except Exception as e:
            print(f"⚠️ Could not release Firestore robot lock: {e}")

def _resolve_firebase_user_id():
    """Resolve dashboard account while keeping explicit UID authoritative."""
    global _resolved_firebase_uid
    if db is None or _resolved_firebase_uid:
        return

    try:
        if FIREBASE_USER_EMAIL:
            _resolved_firebase_uid = firebase_auth.get_user_by_email(FIREBASE_USER_EMAIL).uid
            print(f"Firebase dashboard user resolved from {FIREBASE_USER_EMAIL}.")
            return

        users = list(firebase_auth.list_users(max_results=2).users)
        if len(users) == 1:
            _resolved_firebase_uid = users[0].uid
            print(f"Firebase dashboard user auto-selected: {users[0].email or _resolved_firebase_uid}")
        elif not users:
            print("WARNING: Firebase Auth has no dashboard users. Register in web app first.")
        else:
            print("WARNING: Multiple Firebase users found. Set FIREBASE_USER_ID or FIREBASE_USER_EMAIL in .env.")
    except Exception as exc:
        print(f"WARNING: Could not resolve Firebase dashboard user: {exc}")

_resolve_firebase_user_id()

# ─── Global Remote Control Events ───
force_stop = threading.Event()
remote_mode_queue = queue.Queue()
start_exercise = threading.Event()
pause_request = threading.Event()       # Set by voice "pause"; consumed by the exercise loop
resume_request = threading.Event()      # Set by voice "resume"; consumed by the exercise loop
exercise_executing = threading.Event()  # Set while an exercise is actively running (requires "FitBot" wake word)

# ─── Session State Tracking ───
_session_id = None
_session_exercise_type = None
_session_start_wall = None
_pause_accumulator = 0.0

# ─── Session Error Tracking ───
session_errors = set()
last_printed_errors = {}
error_occurrence_count = {}

# ─── Non-blocking Publish Worker ───
_publish_queue = queue.Queue()

def _publish_worker():
    """Background thread to process published events without blocking camera loop."""
    while True:
        try:
            task = _publish_queue.get(timeout=1)
            if task is None:
                break
            task()
        except queue.Empty:
            continue
        except Exception:
            pass

_publish_thread = threading.Thread(target=_publish_worker, daemon=True)
_publish_thread.start()

def _enqueue(fn):
    try:
        _publish_queue.put_nowait(fn)
    except queue.Full:
        pass

def _firebase_user_ref():
    if db is None or not _resolved_firebase_uid:
        return None
    return db.collection("users").document(_resolved_firebase_uid)

def _post_json(path, payload):
    """Fallback HTTP post to backend if configured."""
    if not BACKEND_TOKEN:
        return None
    url = f"{BACKEND_BASE_URL}{path}"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {BACKEND_TOKEN}"
            },
            method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=2.5)
        return json.loads(resp.read().decode("utf-8") or "{}")
    except Exception:
        return None

def publish_event(event_type, payload):
    """Publish generic telemetry event to local WebSocket and Firebase."""
    p = dict(payload or {})
    if _session_id and "session_id" not in p:
        p["session_id"] = _session_id
    now = datetime.now(timezone.utc)

    try:
        local_stream_server.broadcast_telemetry(event_type, p)
    except Exception:
        pass

    def _do():
        user_ref = _firebase_user_ref()
        if user_ref is not None:
            try:
                event_ref = user_ref.collection("events").document()
                event_ref.set({
                    "type": event_type,
                    "payload": p,
                    "ts": now
                })
                if event_type == "battery_voltage":
                    user_ref.collection("device_status").document("battery").set({
                        "voltage": p.get("voltage"),
                        "updated_at": now
                    }, merge=True)
                _update_daily_aggregate_from_event(user_ref, event_type, p, now)
                return
            except Exception:
                pass
        _post_json("/api/events", {
            "type": event_type,
            "payload": p,
            "ts": now.isoformat()
        })

    _enqueue(_do)

def _update_daily_aggregate_from_event(user_ref, event_type, payload, ts):
    try:
        date_str = datetime.now().astimezone().strftime("%Y-%m-%d")
        aggregate_ref = user_ref.collection("daily_aggregates").document(date_str)

        if event_type == "rep":
            exercise = payload.get("exercise")
            updates = {
                "date": date_str,
                "form_good_count": firestore.Increment(1),
                "updated_at": firestore.SERVER_TIMESTAMP
            }
            if exercise in ("pushups", "squats"):
                updates["reps"] = {exercise: firestore.Increment(1)}
            elif exercise == "bicep_curls":
                if payload.get("is_full_rep"):
                    updates["reps"] = {exercise: firestore.Increment(1)}
                elif payload.get("left") is not None and payload.get("right") is not None and payload.get("left") == payload.get("right"):
                    updates["reps"] = {exercise: firestore.Increment(1)}
                elif payload.get("left") is None and payload.get("right") is None:
                    updates["reps"] = {exercise: firestore.Increment(1)}
            aggregate_ref.set(updates, merge=True)

        elif event_type == "form_error":
            msg = str(payload.get("message") or "")
            if "GET IN POSITION" in msg.upper() or "POSITIONING" in msg.upper():
                return
            aggregate_ref.set({
                "date": date_str,
                "form_flaw_count": firestore.Increment(1),
                "updated_at": firestore.SERVER_TIMESTAMP
            }, merge=True)

        elif event_type == "session_end":
            updates = {"date": date_str, "updated_at": firestore.SERVER_TIMESTAMP}
            active = float(payload.get("active_duration_s") or 0)
            total_dur = float(payload.get("total_duration_s") or 0)
            if active:
                updates["active_time_s"] = firestore.Increment(round(active, 2))
            if total_dur:
                updates["total_time_s"] = firestore.Increment(round(total_dur, 2))

            summary = payload.get("summary") or {}
            exercise = payload.get("exercise_type") or summary.get("exercise") or payload.get("exercise")
            if exercise == "plank":
                plank_time = float(summary.get("total_plank_time_s") or summary.get("plank_time_s") or 0)
                if not plank_time:
                    for m_name, m_val in summary.get("metrics", []):
                        if "Hold Time" in str(m_name):
                            try:
                                plank_time = float(str(m_val).replace("s", "").strip())
                            except Exception:
                                pass
                if plank_time:
                    updates["plank_time_s"] = firestore.Increment(round(plank_time, 2))

            aggregate_ref.set(updates, merge=True)
    except Exception:
        pass

def publish_session_start(exercise_type):
    """Create a session and store id locally."""
    global _session_id, _session_exercise_type, _session_start_wall, _pause_accumulator
    _session_exercise_type = exercise_type
    now = datetime.now(timezone.utc)
    _session_start_wall = time.time()
    _pause_accumulator = 0.0

    sid = "local_" + str(int(time.time()))
    _session_id = sid

    publish_event("session_start", {
        "session_id": sid,
        "exercise_type": exercise_type
    })

    user_ref = _firebase_user_ref()
    if user_ref is not None:
        try:
            session_ref = user_ref.collection("sessions").document()
            _session_id = session_ref.id
            sid = _session_id

            publish_event("session_start", {
                "session_id": sid,
                "exercise_type": exercise_type
            })

            def _do():
                try:
                    session_ref.set({
                        "user_id": _resolved_firebase_uid,
                        "exercise_type": exercise_type,
                        "started_at": now,
                        "ended_at": None,
                        "total_duration_s": None,
                        "active_duration_s": None,
                        "rest_duration_s": None,
                        "summary": {},
                        "robot_involved": exercise_type == "person_follow"
                    })
                except Exception:
                    pass
            _enqueue(_do)
            return
        except Exception:
            pass

    if not BACKEND_TOKEN:
        return

    payload = {
        "exercise_type": exercise_type,
        "started_at": now.isoformat()
    }

    def _do_http():
        try:
            body = _post_json("/api/sessions/start", payload) or {}
            global _session_id
            if not _session_id:
                _session_id = body.get("session_id")
            publish_event("session_start", {"exercise_type": exercise_type})
        except Exception:
            pass
    _enqueue(_do_http)

def publish_session_end(summary=None):
    """End current session and flush stats."""
    global _session_id, _session_exercise_type, _session_start_wall, _pause_accumulator
    if not _session_id:
        _session_id = None
        _session_exercise_type = None
        _session_start_wall = None
        _pause_accumulator = 0.0
        return

    total = (time.time() - _session_start_wall) if _session_start_wall else 0.0
    active = max(0.0, total - _pause_accumulator)
    now = datetime.now(timezone.utc)
    sid = _session_id
    exercise_type = _session_exercise_type

    _session_id = None
    _session_exercise_type = None
    _session_start_wall = None
    _pause_accumulator = 0.0

    payload = {
        "session_id": sid,
        "exercise_type": exercise_type,
        "ended_at": now,
        "total_duration_s": round(total, 2),
        "active_duration_s": round(active, 2),
        "rest_duration_s": round(max(0.0, total - active), 2),
        "summary": summary or {}
    }

    publish_event("session_end", payload)

    user_ref = _firebase_user_ref()
    def _do():
        if user_ref is not None:
            try:
                user_ref.collection("sessions").document(sid).set(payload, merge=True)
                return
            except Exception:
                pass

        if not BACKEND_TOKEN:
            return
        try:
            legacy_payload = dict(payload)
            legacy_payload["ended_at"] = now.isoformat()
            _post_json("/api/sessions/end", legacy_payload)
        except Exception:
            pass

    _enqueue(_do)

def publish_rep(exercise, set_num=None, count=None, left=None, right=None, **extra):
    p = {"exercise": exercise}
    if set_num is not None: p["set"] = set_num
    if count is not None: p["count"] = count
    if left is not None: p["left"] = left
    if right is not None: p["right"] = right
    p.update(extra)
    publish_event("rep", p)

def publish_set_complete(exercise, set_num, reps):
    publish_event("set_complete", {"exercise": exercise, "set": set_num, "reps": reps})

def publish_form_error(message, side=None):
    p = {"message": message}
    if side: p["side"] = side
    publish_event("form_error", p)

def publish_robot_state(state, command=None, exercise=None):
    p = {"state": state}
    if command: p["command"] = command
    if exercise: p["exercise"] = exercise
    publish_event("robot_state", p)

def publish_plank_time(value):
    publish_event("plank_time", {"value": round(float(value), 2)})

def publish_pause():
    publish_event("pause", {})

def publish_resume():
    publish_event("resume", {})

def add_pause_time(seconds):
    global _pause_accumulator
    _pause_accumulator += max(0.0, seconds)

# ─── Error Logging & Summary Helpers ───
def clear_session_errors():
    """Clear session error tracking."""
    global session_errors, last_printed_errors, error_occurrence_count
    session_errors = set()
    last_printed_errors = {}
    error_occurrence_count = {}

def log_session_error(err_msg, cooldown=3.0, is_resting=False, person_missing=False, lost_person_since=None):
    """Log form error, throttle messages, play voice from 2nd occurrence, and publish to backend."""
    global session_errors, last_printed_errors, error_occurrence_count
    session_errors.add(err_msg)
    now = time.time()
    if err_msg not in last_printed_errors or now - last_printed_errors[err_msg] > cooldown:
        print(f"  [SESSION WARNING] {err_msg}")
        last_printed_errors[err_msg] = now
        error_occurrence_count[err_msg] = error_occurrence_count.get(err_msg, 0) + 1

        if error_occurrence_count[err_msg] >= 2:
            play_voice_command(err_msg, is_resting, person_missing, lost_person_since)

        try:
            publish_form_error(err_msg)
        except Exception:
            pass

def save_session_to_firestore(summary):
    """Save finalized session summary into Firestore user workout_sessions collection."""
    if db is None:
        print("⚠️ Cannot save to Firestore: Database not initialized.")
        return

    try:
        metrics_dict = {k: v for k, v in summary['metrics']}
        doc_data = {
            "exercise": summary['exercise'],
            "metrics": metrics_dict,
            "errors": summary.get('errors', []),
            "timestamp": datetime.now()
        }

        user_ref = _firebase_user_ref()
        if user_ref is not None:
            user_ref.collection("workout_sessions").add(doc_data)
        else:
            db.collection("workout_sessions").add(doc_data)
        print(f"✅ Session saved to Firestore: {summary['exercise']}")
    except Exception as e:
        print(f"⚠️ Failed to save session to Firestore: {e}")

def print_session_error_summary():
    """Print summary of all detected session errors to console."""
    if session_errors:
        print("\n" + "=" * 50)
        print("  SESSION ERROR SUMMARY (ALL DETECTED ERRORS)")
        print("=" * 50)
        for err in sorted(session_errors):
            print(f"  • {err}")
        print("=" * 50 + "\n")

# ─── Firestore Remote Control Listener Thread ───
def _control_listener():
    """Poll Firestore control commands for force_stop, switch_mode, start_exercise."""
    processed = set()
    while True:
        try:
            if db is None:
                time.sleep(1.0)
                continue

            # Query collection_group across all users, with fallback to user_ref
            commands = []
            try:
                commands = list(db.collection_group("control_commands").where("status", "==", "pending").stream())
            except Exception:
                user_ref = _firebase_user_ref()
                if user_ref is not None:
                    try:
                        commands = list(user_ref.collection("control_commands").where("status", "==", "pending").stream())
                    except Exception:
                        commands = []

            for doc in commands:
                if doc.id in processed:
                    continue

                data = doc.to_dict() or {}
                # Automatically track active user ID from Firestore document path if available
                try:
                    parent_doc = doc.reference.parent.parent
                    if parent_doc and parent_doc.id:
                        set_active_user(parent_doc.id)
                except Exception:
                    pass

                action = str(data.get("action") or "").lower()
                mode = data.get("mode") or data.get("exercise")

                if action == "force_stop":
                    start_exercise.clear()
                    force_stop.set()
                elif action in ("switch_mode", "start_exercise", "start", "select_exercise"):
                    force_stop.clear()
                    start_exercise.set()
                    if mode:
                        remote_mode_queue.put(("switch", str(mode).upper()))
                elif action.upper() in ("BICEP_CURLS", "BICEP_CURL", "PUSHUPS", "PUSH_UPS", "SQUATS", "SQUAT", "PLANK"):
                    force_stop.clear()
                    start_exercise.set()
                    remote_mode_queue.put(("switch", action.upper()))

                processed.add(doc.id)
                try:
                    doc.reference.set({
                        "status": "processed",
                        "processed_at": firestore.SERVER_TIMESTAMP,
                        "processed_by": "raspi"
                    }, merge=True)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(0.5)

_control_thread = threading.Thread(target=_control_listener, daemon=True)
_control_thread.start()
