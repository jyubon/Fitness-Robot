# Fitness Robot (FitBot)

Pose-tracking fitness robot + voice commands + beautiful MongoDB-backed dashboard.

The robot (Raspberry Pi + camera + ESP32 motors) counts reps, measures form, times holds, follows you, and reports every completed session to a FastAPI + MongoDB backend. The dashboard shows live telemetry, controls, weekly analytics, form accuracy, and session history.

## Architecture

Robot (v.1.1.py / person_raspi.py)
  → report_session.py (HTTP POST)
  → FastAPI (server/main.py)
  → MongoDB (exercise_sessions collection)
Dashboard (fitness_robot_dashboard.html)
  → fetches /api/sessions + /api/analytics/summary
  → graceful fallback to mock data if backend is unreachable

## Prerequisites

- Python 3.9 – 3.12
- MongoDB (local or Atlas)
- (Optional) Raspberry Pi + camera + ESP32 for the physical robot

## Quick Start (Windows)

1. Clone / open this folder.

2. Install MongoDB (local) or use a free Atlas cluster.  
   Local default connection string: `mongodb://localhost:27017`

3. Backend (API + DB):

   ```powershell
   cd server

   # Create venv (recommended)
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1

   pip install -r requirements.txt

   # Configure Mongo
   copy .env.example .env
   # Edit .env and set MONGO_URI if not using localhost:27017
   # DB_NAME=fitness_robot

   # Run the API
   uvicorn server.main:app --reload --port 8000
   ```

   The API will be at `http://localhost:8000`.  
   Opening `http://localhost:8000` in a browser will serve the dashboard (or just open the `.html` file directly).

4. Seed demo data (so charts and numbers appear immediately):

   ```powershell
   cd server
   python seed_demo.py
   # Optional: python seed_demo.py your@email.com
   ```

   Refresh the Analytics page in the dashboard.

5. Robot / exercise side (on the machine with the camera):

   ```powershell
   # From the project root
   python -m pip install -r requirements.txt

   # Optional: tell the robot which user to report for
   $env:FITBOT_USER_EMAIL = "athlete@local.dev"
   $env:FITBOT_API = "http://localhost:8000"

   python v.1.1.py
   ```

   When you complete a Bicep Curls / Push-ups / Squats / Plank / Person Tracking session, it will POST to the backend and appear on the dashboard within a few seconds (the Live page polls for new sessions).

## Dashboard Pages

- **Live Telemetry** — Current mode, rep count, plank timer, session clock, wristband-style sensors, encouragement feed. Pulls the most recent saved session from Mongo and shows a “Last: …” summary line.
- **Remote Control** — Switch modes (simulated), terminate, force stop. Status panel describes the architecture.
- **Analytics** — Four summary cards (Total Reps, Plank Time, Sessions, Gym Time), daily reps bar chart, form accuracy bars, session time trend, and session history table. Fully driven by real data when the backend is reachable; falls back to mock data otherwise. Charts rebuild when you have real analytics.

The dashboard is a single static HTML file (`fitness_robot_dashboard.html`) that talks to the API via fetch. No build step required.

## Reporting Sessions from Custom Code

```python
from datetime import datetime, timezone
from report_session import report_session

start = datetime.now(timezone.utc)
# ... run your exercise ...
end = datetime.now(timezone.utc)

report_session(
    user_email="you@example.com",
    exercise_type="BICEP_CURLS",   # or PUSHUPS, SQUATS, PLANK, PERSON_TRACKING
    start_time=start,
    end_time=end,
    duration_sec=int((end - start).total_seconds()),
    reps=28,
    left_reps=14,
    right_reps=14,
    form_good_pct=89.0,
    form_bad_pct=11.0,
    notes="Solid session",
)
```

- Never throws — logs warnings if the backend is down.
- Controlled by `FITBOT_API` and `FITBOT_USER_EMAIL` environment variables.

## Environment Variables

Backend (server/.env or process env):

- `MONGO_URI` — MongoDB connection string (default: mongodb://localhost:27017)
- `DB_NAME` — Database name (default: fitness_robot)

Robot side:

- `FITBOT_API` — Base URL of the FastAPI backend (default: http://localhost:8000)
- `FITBOT_USER_EMAIL` — Default user email for session reports

Robot scripts may also need `api_keys.GEMINI_API_KEY` for voice features (see api_keys.py).

## Project Layout

```
.
├── fitness_robot_dashboard.html   # The beautiful single-file dashboard
├── v.1.1.py                       # Main exercise robot (bicep curls, push-ups, squats, plank, person tracking)
├── person_raspi.py                # Raspberry Pi variant / person follower
├── report_session.py              # Helper to POST completed sessions to the backend
├── requirements.txt               # Robot-side Python deps (MediaPipe, etc.)
├── README.md
└── server/
    ├── main.py                    # FastAPI app (auth, sessions, analytics)
    ├── models.py                  # Pydantic models
    ├── db.py                      # Motor + lifespan + indexes
    ├── requirements.txt           # Backend deps (FastAPI, motor, etc.)
    ├── .env.example
    └── seed_demo.py               # One-shot script to insert realistic demo sessions
```

## Seeding & Resetting Demo Data

```powershell
cd server
python seed_demo.py                # uses athlete@local.dev
python seed_demo.py you@your.com   # custom user
```

To start fresh, drop the collection in MongoDB (or delete documents for a specific email).

## Windows Tips

- Use PowerShell. Activate venv with `.\.venv\Scripts\Activate.ps1`.
- If you get execution policy errors for the venv activation script, run once:
  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
  ```
- MongoDB local install: use the official MSI or Chocolatey (`choco install mongodb`).
- Firewall: allow the FastAPI port (8000) if accessing the dashboard from another machine on the LAN.
- Paths with spaces: quote them when running commands.

## Running Without MongoDB / Backend

- The robot scripts still work; `report_session` will just print warnings and continue.
- The dashboard detects a missing backend and falls back to beautiful mock data so the UI is always usable for demos or development.

## License / Notes

Educational / personal project. MediaPipe, FastAPI, Motor, Chart.js, etc. retain their own licenses.

Enjoy your gains. 🤖💪
