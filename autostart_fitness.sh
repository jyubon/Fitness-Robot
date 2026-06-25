#!/bin/bash
# -------------------------------------------------------------------------
# Autostart Script for Fitness Robot Application on Raspberry Pi
# -------------------------------------------------------------------------

# Navigate to the project directory (Update this path to your actual repository path on the Raspberry Pi)
cd "$(dirname "$0")"

# Path to your Raspberry Pi virtual environment directory (update if needed)
VENV_PATH="../dp311_env"

if [ -d "$VENV_PATH" ]; then
    echo "Activating virtual environment..."
    source "$VENV_PATH/bin/activate"
elif [ -d "./venv" ]; then
    source "./venv/bin/activate"
fi

# Run the python application
python3 person_raspi.py
