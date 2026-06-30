"""
MongoDB connection and collection helpers using Motor (async).
All data is real — no dummy seeding.
"""

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING
import os
from typing import Optional

# MongoDB connection string (override via env)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGO_DB_NAME", "fitness_robot")

_client: Optional[AsyncIOMotorClient] = None
_db = None


def get_db():
    """Return the database instance. Initialize lazily."""
    global _client, _db
    if _db is None:
        _client = AsyncIOMotorClient(MONGO_URI)
        _db = _client[DB_NAME]
    return _db


async def init_indexes():
    """Create required indexes (idempotent)."""
    db = get_db()

    # Users: unique email
    await db.users.create_index([("email", ASCENDING)], unique=True)

    # Sessions: query by user + time
    await db.sessions.create_index([("user_id", ASCENDING), ("started_at", ASCENDING)])

    # Events: query by session and time
    await db.events.create_index([("session_id", ASCENDING), ("ts", ASCENDING)])

    # Daily aggregates: unique per user+date
    await db.daily_aggregates.create_index(
        [("user_id", ASCENDING), ("date", ASCENDING)], unique=True
    )

    # Watch data: unique per user+date
    await db.watch_data.create_index(
        [("user_id", ASCENDING), ("date", ASCENDING)], unique=True
    )


async def close_db():
    """Close MongoDB connection."""
    global _client, _db
    if _client:
        _client.close()
    _client = None
    _db = None
