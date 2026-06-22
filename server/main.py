"""
Fitness Robot Dashboard Backend
FastAPI + MongoDB (motor)

Endpoints:
- POST   /api/auth/login          → ensure/create user, return user
- POST   /api/sessions            → record a completed exercise session (from robot)
- GET    /api/sessions            → list sessions for a user (newest first)
- GET    /api/analytics/summary   → weekly stats, charts data, form accuracy

Live bidirectional link (web ↔ robot):
- POST   /api/robot/state         → robot pushes live telemetry (mode, reps, plankTime, espCmd, espSpd, message)
- GET    /api/robot/state         → dashboard polls to show live robot outputs
- POST   /api/robot/command       → dashboard sends command (SWITCH_MODE, TERMINATE, FORCE_STOP, START)
- GET    /api/robot/commands      → robot polls for pending commands
- POST   /api/robot/commands/{id}/ack → robot acks after acting on a command

Run locally:
    uvicorn server.main:app --reload --port 8000

Environment:
    MONGO_URI=mongodb://localhost:27017
    DB_NAME=fitness_robot
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from datetime import datetime, timedelta, timezone
from typing import Optional, List
import os
from pathlib import Path

from bson import ObjectId

from .db import lifespan, get_db
from .models import (
    UserCreate,
    User,
    ExerciseSessionCreate,
    ExerciseSession,
    AuthResponse,
    AnalyticsSummary,
    RobotStateIn,
    RobotState,
    RobotCommandIn,
    RobotCommand,
)

# ─── App setup ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Fitness Robot API",
    description="Backend for FitBot dashboard — stores sessions from the Raspberry Pi / PC robot.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS: allow the local dashboard (file:// or localhost) and common dev origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:8000",
        "*",  # safe for local dev only — tighten in production
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the beautiful dashboard statically if someone opens it through the backend
# (optional but nice — user can also just open the .html file directly)
STATIC_DIR = Path(__file__).parent.parent
DASHBOARD_PATH = STATIC_DIR / "fitness_robot_dashboard.html"

if DASHBOARD_PATH.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_user_doc(user_in: UserCreate) -> dict:
    initials = user_in.initials or "".join(w[0].upper() for w in user_in.name.split()[:2])[:2] or "AT"
    return {
        "email": user_in.email.lower(),
        "name": user_in.name,
        "initials": initials,
        "createdAt": _now(),
    }


async def _ensure_user(db, email: str, name: Optional[str] = None) -> dict:
    """Create user if missing, return the user document."""
    email = email.lower()
    existing = await db.users.find_one({"email": email})
    if existing:
        return existing

    doc = {
        "email": email,
        "name": name or email.split("@")[0].replace(".", " ").title(),
        "initials": "".join(w[0].upper() for w in (name or email).split()[:2])[:2] or "AT",
        "createdAt": _now(),
    }
    await db.users.insert_one(doc)
    return doc


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """Simple health check."""
    return {"status": "ok", "service": "fitness-robot-api"}


@app.post("/api/auth/login", response_model=AuthResponse)
async def login(user_in: UserCreate):
    """
    Demo-friendly auth.
    Creates the user if they don't exist (email is the key).
    The frontend just needs to "log in" with an email.
    """
    db = await get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    doc = await _ensure_user(db, user_in.email, user_in.name)
    user = User(
        _id=str(doc["_id"]),
        email=doc["email"],
        name=doc["name"],
        initials=doc.get("initials"),
        createdAt=doc["createdAt"],
    )
    return AuthResponse(user=user)


@app.post("/api/sessions", response_model=ExerciseSession, status_code=201)
async def create_session(session_in: ExerciseSessionCreate):
    """
    Called by the robot (or a manual logger) when an exercise session completes.
    Stores the full session in MongoDB.
    """
    db = await get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    # Ensure the user exists (robot may report with any email)
    await _ensure_user(db, session_in.userEmail)

    doc = session_in.model_dump()
    # Convert Pydantic datetime (already timezone-aware if robot sends it correctly)
    result = await db.exercise_sessions.insert_one(doc)
    created = await db.exercise_sessions.find_one({"_id": result.inserted_id})

    return ExerciseSession(
        _id=str(created["_id"]),
        **{k: v for k, v in created.items() if k != "_id"},
    )


@app.get("/api/sessions", response_model=List[ExerciseSession])
async def list_sessions(
    userEmail: str = Query(..., description="User email to filter sessions"),
    limit: int = Query(100, ge=1, le=500),
):
    """Return recent sessions for a user (newest first)."""
    db = await get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    cursor = (
        db.exercise_sessions
        .find({"userEmail": userEmail.lower()})
        .sort("startTime", -1)
        .limit(limit)
    )
    sessions = []
    async for doc in cursor:
        sessions.append(
            ExerciseSession(
                _id=str(doc["_id"]),
                **{k: v for k, v in doc.items() if k != "_id"},
            )
        )
    return sessions


@app.get("/api/analytics/summary", response_model=AnalyticsSummary)
async def analytics_summary(
    userEmail: str = Query(..., description="User email"),
    days: int = Query(7, ge=1, le=90, description="How many days back to include"),
):
    """
    Compute weekly-ish analytics for the dashboard.
    The frontend already knows how to render the charts once it gets this shape.
    """
    db = await get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    email = userEmail.lower()
    since = _now() - timedelta(days=days)

    # Fetch sessions in the window
    cursor = db.exercise_sessions.find(
        {"userEmail": email, "startTime": {"$gte": since}}
    ).sort("startTime", 1)

    sessions = []
    async for doc in cursor:
        sessions.append(doc)

    # Prepare day labels (Mon, Tue, ...)
    day_labels = []
    day_buckets = {}  # date -> {"curls":, "pushups":, ... , "plankSec": , "totalMin": }
    for i in range(days):
        d = (since + timedelta(days=i)).date()
        label = d.strftime("%a")  # Mon, Tue, ...
        day_labels.append(label)
        day_buckets[d] = {
            "BICEP_CURLS": 0,
            "PUSHUPS": 0,
            "SQUATS": 0,
            "PLANK": 0,
            "PERSON_TRACKING": 0,
            "plankSec": 0,
            "totalMin": 0,
        }

    total_reps = 0
    total_plank = 0.0
    total_sessions = len(sessions)
    total_gym_minutes = 0.0

    form_totals = {
        "BICEP_CURLS": {"good": 0, "bad": 0, "count": 0},
        "PUSHUPS": {"good": 0, "bad": 0, "count": 0},
        "SQUATS": {"good": 0, "bad": 0, "count": 0},
    }

    for s in sessions:
        d = s["startTime"].date()
        if d not in day_buckets:
            continue

        ex = s["exerciseType"]
        dur_min = s.get("durationSec", 0) / 60.0
        day_buckets[d]["totalMin"] += dur_min
        total_gym_minutes += dur_min

        if ex in ("BICEP_CURLS", "PUSHUPS", "SQUATS"):
            reps = s.get("reps") or 0
            day_buckets[d][ex] += reps
            total_reps += reps

            if ex in form_totals and s.get("formGoodPct") is not None:
                form_totals[ex]["good"] += s.get("formGoodPct", 0)
                form_totals[ex]["bad"] += s.get("formBadPct", 0)
                form_totals[ex]["count"] += 1

        if ex == "PLANK":
            ht = s.get("holdTimeSec") or 0
            day_buckets[d]["plankSec"] += ht
            total_plank += ht

    # Build reps series in the order of day_labels
    reps_by_ex = {
        "BICEP_CURLS": [],
        "PUSHUPS": [],
        "SQUATS": [],
    }
    plank_series = []
    session_mins_series = []

    for label_date in [ (since + timedelta(days=i)).date() for i in range(days) ]:
        b = day_buckets.get(label_date, {})
        reps_by_ex["BICEP_CURLS"].append(b.get("BICEP_CURLS", 0))
        reps_by_ex["PUSHUPS"].append(b.get("PUSHUPS", 0))
        reps_by_ex["SQUATS"].append(b.get("SQUATS", 0))
        plank_series.append(b.get("plankSec", 0))
        session_mins_series.append(round(b.get("totalMin", 0), 1))

    # Form accuracy averages (only for exercises that had form data)
    form_accuracy = {}
    for ex, data in form_totals.items():
        if data["count"] > 0:
            good_avg = round(data["good"] / data["count"], 1)
            bad_avg = round(data["bad"] / data["count"], 1)
            label_map = {
                "BICEP_CURLS": "Bicep Curls — good form",
                "PUSHUPS": "Push-ups — good form",
                "SQUATS": "Squats — good form",
            }
            form_accuracy[ex.lower()] = {
                "good": good_avg,
                "bad": bad_avg,
                "label": label_map.get(ex, ex),
            }

    # Recent sessions (last 6) for the table
    recent = []
    for s in reversed(sessions[-6:]):
        recent.append({
            "date": s["startTime"].strftime("%a %b %d"),
            "exercise": s["exerciseType"],
            "workTime": f"{int(s.get('durationSec', 0)//60):02d}:{int(s.get('durationSec', 0)%60):02d}",
            "total": f"{int(s.get('durationSec', 0)//60):02d}:{int(s.get('durationSec', 0)%60):02d}",
        })

    return AnalyticsSummary(
        totalReps=total_reps,
        totalPlankSeconds=round(total_plank, 1),
        totalSessions=total_sessions,
        totalGymMinutes=round(total_gym_minutes, 1),
        repsByExercise=reps_by_ex,
        formAccuracy=form_accuracy,
        recentSessions=recent,
        weekLabels=day_labels,
        plankSeries=plank_series,
        sessionMinsSeries=session_mins_series,
    )


# ─── Live Robot Telemetry (dashboard ↔ robot) ────────────────────────────────
@app.post("/api/robot/state", status_code=204)
async def post_robot_state(state: RobotStateIn):
    """
    Robot pushes live state periodically or on events (reps, mode change, form hint, etc.).
    We keep only the latest per user for simple polling.
    """
    db = await get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    email = state.userEmail.lower()
    await _ensure_user(db, email)

    doc = state.model_dump()
    doc["userEmail"] = email
    doc["timestamp"] = _now()

    # Upsert latest state for this user
    await db.robot_states.update_one(
        {"userEmail": email},
        {"$set": doc},
        upsert=True
    )


@app.get("/api/robot/state", response_model=RobotState)
async def get_robot_state(userEmail: str = Query(..., description="User email")):
    """Dashboard polls this to show live robot telemetry."""
    db = await get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    doc = await db.robot_states.find_one({"userEmail": userEmail.lower()})
    if not doc:
        # Return a sensible default so the UI doesn't break
        now = _now()
        return RobotState(
            userEmail=userEmail.lower(),
            mode="IDLE",
            reps=0,
            plankTime=None,
            espCmd="STOP",
            espSpd=0,
            message=None,
            cameraUrl=None,
            timestamp=now,
        )

    return RobotState(
        _id=str(doc.get("_id")),
        userEmail=doc["userEmail"],
        mode=doc.get("mode"),
        reps=doc.get("reps"),
        plankTime=doc.get("plankTime"),
        espCmd=doc.get("espCmd"),
        espSpd=doc.get("espSpd"),
        message=doc.get("message"),
        cameraUrl=doc.get("cameraUrl"),
        timestamp=doc["timestamp"],
    )


@app.post("/api/robot/command", response_model=RobotCommand, status_code=201)
async def post_robot_command(cmd: RobotCommandIn):
    """
    Dashboard sends a command to the robot (switch mode, terminate, force stop, etc.).
    The robot should poll /api/robot/commands to pick these up.
    """
    db = await get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    email = cmd.userEmail.lower()
    await _ensure_user(db, email)

    doc = {
        "userEmail": email,
        "action": cmd.action,
        "mode": cmd.mode,
        "createdAt": _now(),
        "status": "pending",
    }
    result = await db.robot_commands.insert_one(doc)
    created = await db.robot_commands.find_one({"_id": result.inserted_id})

    return RobotCommand(
        _id=str(created["_id"]),
        userEmail=created["userEmail"],
        action=created["action"],
        mode=created.get("mode"),
        createdAt=created["createdAt"],
        status=created.get("status", "pending"),
    )


@app.get("/api/robot/commands", response_model=List[RobotCommand])
async def get_robot_commands(
    userEmail: str = Query(..., description="User email"),
    limit: int = Query(20, ge=1, le=100),
):
    """
    Robot polls this endpoint to receive pending commands.
    Returns newest first; robot should mark them as 'claimed' or 'done' after acting.
    """
    db = await get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    cursor = (
        db.robot_commands
        .find({"userEmail": userEmail.lower()})
        .sort("createdAt", -1)
        .limit(limit)
    )
    out = []
    async for doc in cursor:
        out.append(
            RobotCommand(
                _id=str(doc["_id"]),
                userEmail=doc["userEmail"],
                action=doc["action"],
                mode=doc.get("mode"),
                createdAt=doc["createdAt"],
                status=doc.get("status", "pending"),
            )
        )
    return out


@app.post("/api/robot/commands/{cmd_id}/ack", status_code=204)
async def ack_robot_command(cmd_id: str, status: str = Query("done", description="claimed | done | ignored")):
    """Robot calls this after it has processed (or decided to ignore) a command."""
    db = await get_db()
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    try:
        oid = ObjectId(cmd_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Bad command id")

    await db.robot_commands.update_one(
        {"_id": oid},
        {"$set": {"status": status}}
    )


# ─── Convenience: serve the dashboard at root (optional) ──────────────────────
@app.get("/")
async def root():
    if DASHBOARD_PATH.exists():
        return FileResponse(str(DASHBOARD_PATH))
    return {"message": "Fitness Robot API running. Open fitness_robot_dashboard.html directly or use /static."}


# ─── Dev entry ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server.main:app", host="0.0.0.0", port=8000, reload=True)
