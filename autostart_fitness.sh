#!/bin/bash
# -------------------------------------------------------------------------
# Autostart Script for Fitness Robot Application on Raspberry Pi
# -------------------------------------------------------------------------

# Navigate to the project directory (dynamically detected relative to the script)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "${SCRIPT_DIR}"

# 1. Generate/Sync the ~/.config/autostart/fitness_robot.desktop file
AUTOSTART_DIR="${HOME}/.config/autostart"
mkdir -p "${AUTOSTART_DIR}"
DESKTOP_FILE="${AUTOSTART_DIR}/fitness_robot.desktop"

# Write the .desktop file to execute this wrapper script itself upon boot
cat <<EOF > "${DESKTOP_FILE}"
[Desktop Entry]
Type=Application
Name=Fitness Robot
Exec=bash ${SCRIPT_DIR}/autostart_fitness.sh
WorkingDirectory=${SCRIPT_DIR}
StartupNotify=false
Terminal=false
EOF
chmod +x "${DESKTOP_FILE}"

echo "==========================================="
echo "  Fitness Robot - Autostart Synced"
echo "==========================================="
echo "Autostart desktop entry updated at: ${DESKTOP_FILE}"
echo "It is configured to run: bash ${SCRIPT_DIR}/autostart_fitness.sh"
echo "-------------------------------------------"

# 2. Path to your Raspberry Pi virtual environment directory (update if needed)
if [ -d "./project_env" ]; then
    echo "Activating virtual environment at ./project_env..."
    source "./project_env/bin/activate"
elif [ -d "../project_env" ]; then
    echo "Activating virtual environment at ../project_env..."
    source "../project_env/bin/activate"
elif [ -d "../dp311_env" ]; then
    echo "Activating virtual environment at ../dp311_env..."
    source "../dp311_env/bin/activate"
elif [ -d "./venv" ]; then
    echo "Activating virtual environment at ./venv..."
    source "./venv/bin/activate"
fi

# Run the python application
echo "Starting person_raspi.py..."
python3 person_raspi.py
