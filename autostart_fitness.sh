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

# 2. Activate virtual environment — check common locations
VENV_ACTIVATED=0

for VENV_PATH in "./project_env" "../project_env" "../dp311_env" "./venv"; do
    if [ -d "${VENV_PATH}" ]; then
        echo "Activating virtual environment at ${VENV_PATH}..."
        source "${VENV_PATH}/bin/activate"
        VENV_ACTIVATED=1
        break
    fi
done

if [ "${VENV_ACTIVATED}" -eq 0 ]; then
    echo "WARNING: No virtual environment found. Running with system Python."
fi

# 3. Run the python application
echo "Starting person_raspi.py..."
python3 "${SCRIPT_DIR}/person_raspi.py"
