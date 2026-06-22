"""
seed_demo.py
One-shot script to populate the MongoDB with realistic demo exercise sessions
so the dashboard shows charts, numbers, and history immediately.

Run from the server/ directory (or project root):

    cd server
    python seed_demo.py

It targets the same DB/collection as the FastAPI backend.
Default user email matches the robot's default (athlete@local.dev).

You can override the user with an argument:

    python seed_demo.py you@yourdomain.com
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pymongo import MongoClient

# --- Config (matches backend defaults) ---
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "fitness_robot")
COLLECTION = "exercise_sessions"

# Default user that the robot also uses unless FITBOT_USER_EMAIL is set
DEFAULT_USER = "athlete@local.dev"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def main(user_email: str = DEFAULT_USER):
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client[DB_NAME]
    coll = db[COLLECTION]

    now = _now_utc()
    inserted = []

    # Spread sessions over the last 7 days (including today)
    # Mix of exercise types, reps, form quality, and one plank + one tracking session.
    demo_sessions = [
        # Day -6 (older)
        {
            "exerciseType": "BICEP_CURLS",
            "startTime": now - timedelta(days=6, hours=9, minutes=15),
            "durationSec": 19 * 60 + 30,
            "reps": 24,
            "leftReps": 12,
            "rightReps": 12,
            "formGoodPct": 91.0,
            "formBadPct": 9.0,
        },
        {
            "exerciseType": "SQUATS",
            "startTime": now - timedelta(days=6, hours=9, minutes=45),
            "durationSec": 14 * 60 + 10,
            "reps": 18,
            "formGoodPct": 88.0,
            "formBadPct": 12.0,
        },

        # Day -5
        {
            "exerciseType": "PUSHUPS",
            "startTime": now - timedelta(days=5, hours=18, minutes=5),
            "durationSec": 11 * 60 + 45,
            "reps": 15,
            "formGoodPct": 73.0,
            "formBadPct": 27.0,
        },
        {
            "exerciseType": "PLANK",
            "startTime": now - timedelta(days=5, hours=18, minutes=25),
            "durationSec": 3 * 60 + 50,
            "holdTimeSec": 95.0,
        },

        # Day -3
        {
            "exerciseType": "BICEP_CURLS",
            "startTime": now - timedelta(days=3, hours=7, minutes=50),
            "durationSec": 22 * 60 + 15,
            "reps": 32,
            "leftReps": 16,
            "rightReps": 16,
            "formGoodPct": 84.0,
            "formBadPct": 16.0,
        },
        {
            "exerciseType": "SQUATS",
            "startTime": now - timedelta(days=3, hours=8, minutes=20),
            "durationSec": 17 * 60,
            "reps": 25,
            "formGoodPct": 94.0,
            "formBadPct": 6.0,
        },

        # Day -2
        {
            "exerciseType": "PUSHUPS",
            "startTime": now - timedelta(days=2, hours=19, minutes=10),
            "durationSec": 13 * 60 + 20,
            "reps": 20,
            "formGoodPct": 79.0,
            "formBadPct": 21.0,
        },
        {
            "exerciseType": "PERSON_TRACKING",
            "startTime": now - timedelta(days=2, hours=19, minutes=40),
            "durationSec": 27 * 60 + 5,
        },

        # Day -1 (yesterday)
        {
            "exerciseType": "BICEP_CURLS",
            "startTime": now - timedelta(days=1, hours=6, minutes=30),
            "durationSec": 16 * 60 + 40,
            "reps": 28,
            "leftReps": 14,
            "rightReps": 14,
            "formGoodPct": 89.0,
            "formBadPct": 11.0,
        },
        {
            "exerciseType": "PLANK",
            "startTime": now - timedelta(days=1, hours=6, minutes=55),
            "durationSec": 4 * 60 + 15,
            "holdTimeSec": 118.0,
        },

        # Today
        {
            "exerciseType": "SQUATS",
            "startTime": now - timedelta(hours=2, minutes=10),
            "durationSec": 15 * 60 + 55,
            "reps": 22,
            "formGoodPct": 85.0,
            "formBadPct": 15.0,
        },
        {
            "exerciseType": "PUSHUPS",
            "startTime": now - timedelta(hours=1, minutes=40),
            "durationSec": 9 * 60 + 30,
            "reps": 12,
            "formGoodPct": 67.0,
            "formBadPct": 33.0,
        },
    ]

    for s in demo_sessions:
        start = s["startTime"]
        end = start + timedelta(seconds=s["durationSec"])

        doc = {
            "userEmail": user_email.lower(),
            "exerciseType": s["exerciseType"],
            "startTime": start,
            "endTime": end,
            "durationSec": s["durationSec"],
        }

        # Rep-based fields
        if "reps" in s:
            doc["reps"] = s["reps"]
        if "leftReps" in s:
            doc["leftReps"] = s["leftReps"]
        if "rightReps" in s:
            doc["rightReps"] = s["rightReps"]

        # Plank
        if "holdTimeSec" in s:
            doc["holdTimeSec"] = float(s["holdTimeSec"])

        # Form (only meaningful for rep exercises)
        if "formGoodPct" in s:
            doc["formGoodPct"] = float(s["formGoodPct"])
        if "formBadPct" in s:
            doc["formBadPct"] = float(s["formBadPct"])

        # Keep raw for future flexibility (optional)
        doc["raw"] = {"source": "seed_demo.py"}

        result = coll.insert_one(doc)
        inserted.append((s["exerciseType"], start, result.inserted_id))

    # Print friendly summary
    print(f"[seed] Inserted {len(inserted)} demo sessions for {user_email} into {DB_NAME}.{COLLECTION}")
    for ex, ts, _id in inserted:
        print(f"  - {ex:15s} @ {ts.strftime('%Y-%m-%d %H:%M')}  (_id={_id})")

    client.close()
    print("[seed] Done. Refresh the dashboard Analytics page to see the data.")


if __name__ == "__main__":
    email = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_USER
    main(email)
