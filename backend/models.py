"""
Pydantic models for the Fitness Robot Web App.
All fields represent real data coming from the Pi/ESP32/watch.
No dummy or default fabricated values.
"""

from pydantic import BaseModel, Field, EmailStr
from typing import Optional, Dict, Any, List
from datetime import datetime


# --- Auth ---

class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str


# --- Sessions ---

class SessionStart(BaseModel):
    exercise_type: str  # "bicep_curls" | "pushups" | "squats" | "plank" | "person_follow"
    started_at: datetime


class SessionEnd(BaseModel):
    session_id: str
    ended_at: datetime
    total_duration_s: float
    active_duration_s: float
    summary: Dict[str, Any] = Field(default_factory=dict)  # reps, sets, final_plank_time, etc.


class SessionDoc(BaseModel):
    id: str
    user_id: str
    exercise_type: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    total_duration_s: Optional[float] = None
    active_duration_s: Optional[float] = None
    rest_duration_s: Optional[float] = None
    summary: Dict[str, Any] = Field(default_factory=dict)
    robot_involved: bool = False


# --- Events (real-time telemetry & form) ---

class EventIn(BaseModel):
    type: str  # "rep", "set_complete", "form_error", "robot_command", "robot_state", "pause", "resume", "plank_time", "session_start", "session_end"
    payload: Dict[str, Any] = Field(default_factory=dict)
    ts: Optional[datetime] = None  # server will fill if missing


class EventDoc(BaseModel):
    id: str
    session_id: str
    user_id: str
    ts: datetime
    type: str
    payload: Dict[str, Any]


# --- Watch / Steps (real BLE data only) ---

class WatchStepsIn(BaseModel):
    date: str  # YYYY-MM-DD
    steps: int
    calories: Optional[float] = None
    distance_km: Optional[float] = None


# --- Analytics response models ---

class WeeklyAnalytics(BaseModel):
    user_id: str
    from_date: str
    to_date: str
    totals: Dict[str, Any]
    daily: List[Dict[str, Any]]
    form_accuracy: Dict[str, int]  # {"good": N, "flawed": M}
    session_history: List[Dict[str, Any]]


# --- Control commands (from UI to Pi) ---

class ControlCommand(BaseModel):
    action: str  # "force_stop" | "switch_mode" | "start_exercise" | "pause" | "resume" | "mute" | "unmute" | "set_mute"
    mode: Optional[str] = None
    exercise: Optional[str] = None
    muted: Optional[bool] = None
