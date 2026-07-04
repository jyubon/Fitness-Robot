"""
FastAPI entry point for the Fitness Robot Web Dashboard.
Professional, real-time, no dummy data.
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import os
from pathlib import Path
from datetime import datetime, timezone

# Load environment variables from backend/.env (if present)
try:
    from dotenv import load_dotenv
    # Explicitly load backend/.env because the project root is the working directory
    load_dotenv(dotenv_path="backend/.env", override=True)
except Exception:
    pass  # python-dotenv not installed or no .env file — will use real env vars

# Debug: show if JWT_SECRET was loaded (only first few chars for safety)
_jwt = os.getenv("JWT_SECRET", "")
if _jwt and _jwt != "REPLACE_WITH_A_LONG_RANDOM_SECRET_FOR_PRODUCTION":
    print(f"[Startup] JWT_SECRET loaded from env (first 8 chars): {_jwt[:8]}...")
else:
    print("[Startup] WARNING: JWT_SECRET not set properly! Using default (insecure).")

from .database import init_indexes, close_db, get_db
from .auth import register_user, login_user, get_current_user_id
from .models import (
    UserRegister, UserLogin, TokenResponse,
    SessionStart, SessionEnd, SessionDoc,
    EventIn, EventDoc,
    WatchStepsIn,
    ControlCommand,
)
from .ws_manager import ws_manager
from bson import ObjectId

frontend_path = Path(__file__).resolve().parent.parent / "frontend"

# --- Lifespan: DB init / cleanup ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_indexes()
    print("[Backend] MongoDB indexes ensured.")
    yield
    # Shutdown
    await close_db()
    print("[Backend] MongoDB connection closed.")


app = FastAPI(
    title="Fitness Robot Dashboard API",
    description="Real-time web backend for Raspberry Pi fitness robot. All data is live from hardware.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS (tighten in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: restrict to your frontend origin(s)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Health ---
@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


# --- Auth ---
@app.post("/auth/register", response_model=dict, status_code=201)
async def api_register(payload: UserRegister):
    try:
        user = await register_user(payload.email, payload.password)
        return user
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/auth/login", response_model=TokenResponse)
async def api_login(payload: UserLogin):
    token = await login_user(payload.email, payload.password)
    return token


# --- Sessions ---
@app.post("/api/sessions/start", response_model=dict, status_code=201)
async def start_session(payload: SessionStart, user_id: str = Depends(get_current_user_id)):
    db = get_db()
    now = datetime.now(timezone.utc)

    session_doc = {
        "user_id": ObjectId(user_id),
        "exercise_type": payload.exercise_type,
        "started_at": payload.started_at,
        "ended_at": None,
        "total_duration_s": None,
        "active_duration_s": None,
        "rest_duration_s": None,
        "summary": {},
        "robot_involved": payload.exercise_type == "person_follow",
    }
    result = await db.sessions.insert_one(session_doc)
    session_id = str(result.inserted_id)

    # Publish live event so connected dashboards see the new session
    await ws_manager.broadcast_to_user(user_id, {
        "type": "session_start",
        "session_id": session_id,
        "exercise_type": payload.exercise_type,
        "started_at": payload.started_at.isoformat(),
    })

    return {"session_id": session_id}


@app.post("/api/sessions/end", response_model=dict)
async def end_session(payload: SessionEnd, user_id: str = Depends(get_current_user_id)):
    db = get_db()
    now = datetime.now(timezone.utc)

    try:
        sid = ObjectId(payload.session_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid session_id")

    session = await db.sessions.find_one({"_id": sid, "user_id": ObjectId(user_id)})
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    rest_s = max(0.0, payload.total_duration_s - payload.active_duration_s)

    update = {
        "$set": {
            "ended_at": payload.ended_at,
            "total_duration_s": payload.total_duration_s,
            "active_duration_s": payload.active_duration_s,
            "rest_duration_s": rest_s,
            "summary": payload.summary,
        }
    }
    await db.sessions.update_one({"_id": sid}, update)

    # Broadcast end
    await ws_manager.broadcast_to_user(user_id, {
        "type": "session_end",
        "session_id": payload.session_id,
        "ended_at": payload.ended_at.isoformat(),
        "total_duration_s": payload.total_duration_s,
        "active_duration_s": payload.active_duration_s,
        "rest_duration_s": rest_s,
        "summary": payload.summary,
    })

    return {"ok": True, "session_id": payload.session_id}


# --- Events (telemetry / form / robot state) ---
@app.post("/api/events", status_code=201)
async def create_event(payload: EventIn, user_id: str = Depends(get_current_user_id)):
    db = get_db()
    ts = payload.ts or datetime.now(timezone.utc)

    # We need a session context for most events.
    # For simplicity we accept events with an optional session_id in payload.
    session_id = payload.payload.get("session_id") if payload.payload else None

    event_doc = {
        "user_id": ObjectId(user_id),
        "session_id": ObjectId(session_id) if session_id else None,
        "ts": ts,
        "type": payload.type,
        "payload": payload.payload,
    }
    result = await db.events.insert_one(event_doc)
    event_id = str(result.inserted_id)

    # Broadcast live to the user's connected dashboards
    await ws_manager.broadcast_to_user(user_id, {
        "type": payload.type,
        "event_id": event_id,
        "ts": ts.isoformat(),
        "payload": payload.payload,
    })

    # --- Update daily aggregates from live events + session end (for analytics) ---
    # - "rep" events: only increment form_good_count (heuristic for form accuracy)
    # - "form_error": increment form_flaw_count
    # - "session_end": authoritative source for:
    #     * active_time_s / total_time_s
    #     * plank_time_s (from summary)
    #     * reps.* (from summary: total_reps or left+right for curls)
    # Live "rep" events are NOT used for rep totals to avoid double-counting.
    try:
        p = payload.payload or {}
        event_date = (ts or datetime.now(timezone.utc)).date().isoformat()
        user_oid = ObjectId(user_id)

        if payload.type == "rep":
            # "rep" events are used ONLY for the form_good heuristic.
            # Rep totals (and final plank time) come from the session_end summary
            # so the daily numbers match exactly what the Pi computed for the full session.
            await db.daily_aggregates.update_one(
                {"user_id": user_oid, "date": event_date},
                {"$inc": {"form_good_count": 1}},
                upsert=True
            )

        elif payload.type == "form_error":
            await db.daily_aggregates.update_one(
                {"user_id": user_oid, "date": event_date},
                {"$inc": {"form_flaw_count": 1}},
                upsert=True
            )

        elif payload.type == "session_end":
            # Session duration rollup (for active vs total time charts)
            active = float(p.get("active_duration_s") or 0)
            total_dur = float(p.get("total_duration_s") or 0)
            if active or total_dur:
                await db.daily_aggregates.update_one(
                    {"user_id": user_oid, "date": event_date},
                    {"$inc": {
                        "active_time_s": round(active, 2),
                        "total_time_s": round(total_dur, 2)
                    }},
                    upsert=True
                )

            # Exercise-specific final numbers from the Pi-computed summary.
            # Live "rep" events are only used for the form_good heuristic.
            # Final authoritative counts (reps + plank time) come from session_end.
            summary = p.get("summary") or {}
            ex = summary.get("exercise") or p.get("exercise")

            if ex == "plank":
                pt = float(summary.get("total_plank_time_s") or summary.get("plank_time_s") or 0)
                if pt:
                    await db.daily_aggregates.update_one(
                        {"user_id": user_oid, "date": event_date},
                        {"$inc": {"plank_time_s": round(pt, 2)}},
                        upsert=True
                    )

            elif ex in ("pushups", "squats"):
                r = int(summary.get("total_reps") or 0)
                if r:
                    await db.daily_aggregates.update_one(
                        {"user_id": user_oid, "date": event_date},
                        {"$inc": {f"reps.{ex}": r}},
                        upsert=True
                    )

            elif ex == "bicep_curls":
                left = int(summary.get("left_reps") or 0)
                right = int(summary.get("right_reps") or 0)
                tot = left + right
                if tot:
                    await db.daily_aggregates.update_one(
                        {"user_id": user_oid, "date": event_date},
                        {"$inc": {"reps.bicep_curls": tot}},
                        upsert=True
                    )
    except Exception:
        # Never break event ingestion
        pass

    return {"event_id": event_id}


# --- Watch / Steps (from BLE web page or future listener) ---
@app.post("/api/watch/steps", status_code=200)
async def ingest_watch_steps(payload: WatchStepsIn, user_id: str = Depends(get_current_user_id)):
    db = get_db()
    now = datetime.now(timezone.utc)

    # Upsert daily aggregate + watch_data
    date_str = payload.date  # YYYY-MM-DD

    # watch_data (raw daily record)
    await db.watch_data.update_one(
        {"user_id": ObjectId(user_id), "date": date_str},
        {"$set": {
            "steps": payload.steps,
            "calories": payload.calories,
            "distance_km": payload.distance_km,
            "last_synced_at": now,
        }},
        upsert=True
    )

    # daily_aggregates (merge steps into today's doc)
    await db.daily_aggregates.update_one(
        {"user_id": ObjectId(user_id), "date": date_str},
        {"$set": {
            "steps": payload.steps,
            "last_watch_sync": now,
        }},
        upsert=True
    )

    # Notify live clients
    await ws_manager.broadcast_to_user(user_id, {
        "type": "watch_steps",
        "date": date_str,
        "steps": payload.steps,
        "calories": payload.calories,
        "distance_km": payload.distance_km,
        "synced_at": now.isoformat(),
    })

    return {"ok": True}


# --- Analytics (weekly) ---
@app.get("/api/analytics/weekly", response_model=dict)
async def get_weekly_analytics(user_id: str = Depends(get_current_user_id)):
    """
    Returns real aggregated data for the last 7 days (calendar days).
    No dummy data.
    """
    from datetime import timedelta, date as date_cls
    import calendar

    db = get_db()
    today = datetime.now(timezone.utc).date()
    from_date = today - timedelta(days=6)  # 7-day window including today

    # Build date range strings
    def dstr(d): return d.isoformat()

    daily_docs = await db.daily_aggregates.find(
        {"user_id": ObjectId(user_id), "date": {"$gte": dstr(from_date), "$lte": dstr(today)}}
    ).sort("date", 1).to_list(length=7)

    # Session history for the week
    sessions = await db.sessions.find({
        "user_id": ObjectId(user_id),
        "started_at": {"$gte": datetime.combine(from_date, datetime.min.time()).replace(tzinfo=timezone.utc)}
    }).sort("started_at", -1).to_list(length=100)

    # Build daily summary (daily_aggregates are populated from rep + form_error + session_end)
    daily = []
    for doc in daily_docs:
        daily.append({
            "date": doc["date"],
            "steps": doc.get("steps", 0),
            "reps": doc.get("reps", {}),
            "plank_time_s": doc.get("plank_time_s", 0),
            "active_time_s": doc.get("active_time_s", 0),
            "total_time_s": doc.get("total_time_s", 0),
            "form_good": doc.get("form_good_count", 0),
            "form_flawed": doc.get("form_flaw_count", 0),
        })

    # Form accuracy totals come from the aggregated daily counts (no more dummy "good=0")
    total_form_good = sum(d.get("form_good", 0) for d in daily)
    total_form_flawed = sum(d.get("form_flawed", 0) for d in daily)

    # Totals (real data from daily_aggregates, fed by events + session_end)
    totals = {
        "steps": sum(d.get("steps", 0) for d in daily),
        "reps": {},
        "plank_time_s": sum(d.get("plank_time_s", 0) for d in daily),
        "active_time_s": sum(d.get("active_time_s", 0) for d in daily),
        "total_time_s": sum(d.get("total_time_s", 0) for d in daily),
    }
    for d in daily:
        for ex, cnt in (d.get("reps") or {}).items():
            totals["reps"][ex] = totals["reps"].get(ex, 0) + cnt

    # Session history (light)
    session_history = []
    for s in sessions:
        session_history.append({
            "id": str(s["_id"]),
            "exercise_type": s["exercise_type"],
            "started_at": s["started_at"].isoformat(),
            "ended_at": s["ended_at"].isoformat() if s.get("ended_at") else None,
            "active_duration_s": s.get("active_duration_s"),
            "total_duration_s": s.get("total_duration_s"),
            "summary": s.get("summary", {}),
        })

    return {
        "user_id": user_id,
        "from_date": dstr(from_date),
        "to_date": dstr(today),
        "totals": totals,
        "daily": daily,
        "form_accuracy": {"good": total_form_good, "flawed": total_form_flawed},
        "session_history": session_history,
    }


# --- Control (UI → Pi) ---
@app.post("/api/control", status_code=202)
async def send_control(cmd: ControlCommand, user_id: str = Depends(get_current_user_id)):
    """
    Broadcast a control command to the user's connected Pi(s).
    The Pi must be subscribed to the control WS for this user.
    """
    await ws_manager.broadcast_control_to_user(user_id, {
        "action": cmd.action,
        "mode": cmd.mode,
        "from": "web",
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True, "action": cmd.action, "mode": cmd.mode}


# --- WebSocket endpoints ---
@app.websocket("/ws/telemetry")
async def ws_telemetry(websocket: WebSocket):
    """
    Pi publishes live telemetry here.
    Browsers also connect here to receive live updates.
    Query param: ?token=JWT
    """
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=1008)
        return

    try:
        user_id = await get_current_user_id(token)
    except HTTPException:
        await websocket.close(code=1008)
        return

    await ws_manager.connect_telemetry(user_id, websocket)
    try:
        while True:
            # Pi can also send messages (e.g. ack or commands)
            data = await websocket.receive_text()
            # For now we just ignore; Pi is mostly a producer.
            # You can extend to handle Pi → backend messages here.
    except WebSocketDisconnect:
        ws_manager.disconnect_telemetry(user_id, websocket)


@app.websocket("/ws/control")
async def ws_control(websocket: WebSocket):
    """
    Bidirectional control channel.
    - UI connects to send force_stop / switch_mode.
    - Pi connects to receive those commands.
    Query param: ?token=JWT
    """
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=1008)
        return

    try:
        user_id = await get_current_user_id(token)
    except HTTPException:
        await websocket.close(code=1008)
        return

    await ws_manager.connect_control(user_id, websocket)
    try:
        while True:
            # UI or Pi can send commands
            raw = await websocket.receive_text()
            # Broadcast to the other side (UI <-> Pi)
            await ws_manager.broadcast_control_to_user(user_id, {
                "raw": raw,
                "from": "peer",
                "ts": datetime.now(timezone.utc).isoformat(),
            })
    except WebSocketDisconnect:
        ws_manager.disconnect_control(user_id, websocket)


# Serve frontend static files if path exists
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")


# --- Run hint ---
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port, reload=True)
