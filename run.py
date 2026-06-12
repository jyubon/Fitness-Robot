import os
import sys
import subprocess
import urllib.request

# URLs for MediaPipe task files if needed
POSE_LANDMARKER_URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task"
HAND_LANDMARKER_URL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"

def download_file(url, filename):
    print(f"Downloading {filename}...")
    try:
        urllib.request.urlretrieve(url, filename)
        print(f"Downloaded {filename} successfully.")
    except Exception as e:
        print(f"Failed to download {filename}: {e}")

def main():
    # Ensure MediaPipe model files are present
    if not os.path.exists("pose_landmarker.task"):
        download_file(POSE_LANDMARKER_URL, "pose_landmarker.task")
        
    if not os.path.exists("hand_landmarker.task"):
        download_file(HAND_LANDMARKER_URL, "hand_landmarker.task")

    print("Starting Fitness Robot Application...")
    
    # Run person_raspi.py using the specified virtual environment's python interpreter
    venv_python = r"D:\University\Semester 01\CM 1900\Project\Projects_2nd _round\Fitness Robot\dp311_env\Scripts\python.exe"
    python_exe = venv_python if os.path.exists(venv_python) else sys.executable
    cmd = [python_exe, "person_raspi.py"]
    try:
        subprocess.run(cmd, check=True)
    except KeyboardInterrupt:
        print("\nFitness Robot stopped.")
    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] Application exited with error code {e.returncode}")

if __name__ == "__main__":
    main()
