# Fitness-Robot

Raspberry Pi fitness and robot-following app using MediaPipe pose detection and ESP32 Serial control.

## Features

1. **Person Following** — 8-direction robot control via ESP32 (forward, backward, left, right, and diagonals)
2. **Bicep Curls** — Rep counter with form feedback for both arms
3. **Push-ups** — Rep counter with form detection
4. **Squats** — Rep counter with depth tracking
5. **Plank** — Hold timer with alignment check

## Hardware

| Component | Required for |
|---|---|
| Raspberry Pi 4 | Primary target platform |
| USB webcam  | All modes |
| ESP32 c3 on same WiFi network | ESP32 with Serial control | Mode 1 (Person Following) only |

Exercise modes (2–5) also work on a PC or laptop with a webcam.

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/jyubon/Fitness-Robot  
cd Fitness-Robot
```

### 2. Create a virtual environment and install dependencies

```bash
python -m venv venv
source venv/bin/activate   # Linux / Raspberry Pi
# venv\Scripts\activate    # Windows

pip install -r requirements.txt
```

Requires **Python 3.9–3.12**. On Raspberry Pi OS, `pip` will use [piwheels](https://www.piwheels.org/) for ARM-compatible packages.

### 3. Download the pose model (required)

The MediaPipe model file is not included in this repo. Download it into the project root and save it as `pose_landmarker.task`:

**Linux / Raspberry Pi** (lite model recommended for speed on Pi):

```bash
wget -O pose_landmarker.task \
  https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task
```

**Windows (PowerShell):**

```powershell
Invoke-WebRequest -Uri "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task" -OutFile "pose_landmarker.task"
```

The script looks for `pose_landmarker.task` in the **directory you run the command from**. For other model sizes (full, heavy), see the [MediaPipe Pose Landmarker documentation](https://ai.google.dev/edge/mediapipe/solutions/vision/pose_landmarker).

### 4. Configure

Edit the constants at the top of `person_raspi.py`:

| Setting | Purpose |
|---|---|
| `ESP32_IP` / `ESP32_PORT` | ESP32 address for robot mode (mode 1) |
| `CAMERA_INDEX` | Webcam device index (default `0`) |
| `CENTER_ZONE_LEFT` / `CENTER_ZONE_RIGHT` | Horizontal follow zones |
| `TOO_CLOSE` / `TOO_FAR` | Distance thresholds (shoulder width) |

## Usage

```bash
python person_raspi.py
```

Choose a mode from the menu (1–5). Press **Q** to quit in any mode.

## Project structure

```
FitnessProject/
├── person_raspi.py       # Main application
├── requirements.txt
├── README.md
├── .gitignore
└── pose_landmarker.task  # Download separately (gitignored)
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `FileNotFoundError: pose_landmarker.task` | Run the model download step above |
| Camera not opening | Change `CAMERA_INDEX` in `person_raspi.py` |
| Robot not moving | Update `ESP32_IP`; ensure Pi and ESP32 are on the same WiFi |
| Slow performance on Pi | Use the lite model; lower `CAMERA_WIDTH` and `CAMERA_HEIGHT` |

## License

See repository license file if provided.
