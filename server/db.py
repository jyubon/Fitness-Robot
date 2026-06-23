"""
MongoDB connection and collection helpers for Fitness Robot dashboard.

Uses motor (async MongoDB driver) + FastAPI lifespan.
"""

import os
from pathlib import Path
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
# pyrefly: ignore [missing-import]
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING
from contextlib import asynccontextmanager
# pyrefly: ignore [missing-import]
from fastapi import FastAPI

# Load environment variables from the server/.env file
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

# ─── Configuration ────────────────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "fitness_robot")

_client: AsyncIOMotorClient | None = None
_db = None


async def get_db():
    """Get the database instance (creates client on first use if needed)."""
    global _client, _db
    if _client is None:
        _client = AsyncIOMotorClient(MONGO_URI)
        _db = _client[DB_NAME]
        await _ensure_indexes()
    return _db


async def _ensure_indexes():
    """Create useful indexes on first connection."""
    db = _client[DB_NAME]

    # Users: fast lookup by email
    await db.users.create_index([("email", ASCENDING)], unique=True)

    # Sessions: common queries
    await db.exercise_sessions.create_index([("userEmail", ASCENDING), ("startTime", DESCENDING)])
    await db.exercise_sessions.create_index([("exerciseType", ASCENDING)])
    await db.exercise_sessions.create_index([("startTime", DESCENDING)])

    # Live robot state (latest per user)
    await db.robot_states.create_index([("userEmail", ASCENDING), ("timestamp", DESCENDING)])

    # Commands for robot (polling queue)
    await db.robot_commands.create_index([("userEmail", ASCENDING), ("createdAt", DESCENDING)])
    await db.robot_commands.create_index([("userEmail", ASCENDING), ("status", ASCENDING), ("createdAt", ASCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan: connect to Mongo on startup, clean up on shutdown.
    The app will still start even if Mongo is down (endpoints will fail gracefully).
    """
    global _client, _db
    try:
        _client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        # Force a quick ping so we know early if Mongo is unreachable
        await _client.admin.command("ping")
        _db = _client[DB_NAME]
        await _ensure_indexes()
        print(f"[MongoDB] Connected to {DB_NAME} at {MONGO_URI}")
    except Exception as e:
        print(f"[MongoDB] WARNING: Could not connect at startup: {e}")
        print("[MongoDB] The server will still run. Endpoints will return errors until Mongo is available.")
        _client = None
        _db = None

    yield

    if _client:
        _client.close()
        print("[MongoDB] Connection closed.")


async def close_db():
    """Explicit close helper (used in lifespan)."""
    global _client
    if _client:
        _client.close()
        _client = None
