"""
Pydantic models for Fitness Robot API.

These define the shape of data we store and exchange.
"""

from datetime import datetime
from typing import Optional, Literal, Any
from pydantic import BaseModel, Field, EmailStr


# ─── Exercise Types (must match the robot) ────────────────────────────────────
ExerciseType = Literal[
    "BICEP_CURLS",
    "PUSHUPS",
    "SQUATS",
    "PLANK",
    "PERSON_TRACKING",
]


# ─── Users ────────────────────────────────────────────────────────────────────
class UserBase(BaseModel):
    email: EmailStr
    name: str = "Athlete"
    initials: Optional[str] = None


class UserCreate(UserBase):
    pass


class User(UserBase):
    id: str = Field(alias="_id")
    createdAt: datetime

    class Config:
        populate_by_name = True


# ─── Exercise Sessions ────────────────────────────────────────────────────────
class ExerciseSessionBase(BaseModel):
    userEmail: EmailStr
    exerciseType: ExerciseType
    startTime: datetime
    endTime: datetime
    durationSec: int = Field(ge=0)

    # Rep-based exercises
    reps: Optional[int] = None
    leftReps: Optional[int] = None
    rightReps: Optional[int] = None

    # Time-based (plank)
    holdTimeSec: Optional[float] = None

    # Form quality (0-100). We store what the robot can provide.
    formGoodPct: Optional[float] = Field(default=None, ge=0, le=100)
    formBadPct: Optional[float] = Field(default=None, ge=0, le=100)

    notes: Optional[str] = None
    raw: Optional[dict[str, Any]] = None   # future-proof for extra details


class ExerciseSessionCreate(ExerciseSessionBase):
    """What the robot (or manual logger) sends when a session completes."""
    pass


class ExerciseSession(ExerciseSessionBase):
    """What we return from the database (includes Mongo _id)."""
    id: str = Field(alias="_id")

    class Config:
        populate_by_name = True


# ─── Auth / User ensure response ──────────────────────────────────────────────
class AuthResponse(BaseModel):
    user: User


# ─── Analytics summary (computed) ─────────────────────────────────────────────
class AnalyticsSummary(BaseModel):
    totalReps: int
    totalPlankSeconds: float
    totalSessions: int
    totalGymMinutes: float
    repsByExercise: dict[str, list[int]]   # daily series matching weekLabels
    formAccuracy: dict[str, dict[str, float]]
    recentSessions: list[dict[str, Any]]
    weekLabels: list[str]

    # Optional daily series for nice charts (seconds per day for plank, minutes per day for total session)
    plankSeries: Optional[list[float]] = None
    sessionMinsSeries: Optional[list[float]] = None


# ─── Live Robot State (for dashboard telemetry while exercise is running) ─────
class RobotStateIn(BaseModel):
    """What the robot pushes periodically or on key events."""
    userEmail: EmailStr
    mode: Optional[str] = None                 # e.g. "BICEP_CURLS", "IDLE", "STOPPED"
    reps: Optional[int] = None
    plankTime: Optional[float] = None          # seconds for current plank hold
    espCmd: Optional[str] = None               # last motor command sent (STOP, FORWARD, ...)
    espSpd: Optional[int] = None
    message: Optional[str] = None              # optional short message for feed (e.g. form hint)

    # Camera streaming
    cameraUrl: Optional[str] = None            # Full URL the browser can load for live camera (MJPEG or HLS)
    # Example: "http://192.168.1.50:8080/stream" or "http://robot-ip:8080/mjpeg"


class RobotState(RobotStateIn):
    """Stored last-known state with timestamp."""
    timestamp: datetime

    class Config:
        populate_by_name = True


# ─── Commands from dashboard to robot ─────────────────────────────────────────
class RobotCommandIn(BaseModel):
    userEmail: EmailStr
    action: Literal["SWITCH_MODE", "TERMINATE", "FORCE_STOP", "START"]
    mode: Optional[str] = None   # required for SWITCH_MODE / START


class RobotCommand(BaseModel):
    id: str = Field(alias="_id")
    userEmail: EmailStr
    action: str
    mode: Optional[str] = None
    createdAt: datetime
    status: str = "pending"      # pending | claimed | done | ignored

    class Config:
        populate_by_name = True
