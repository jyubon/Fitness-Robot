#!/bin/bash
# -------------------------------------------------------------------------
# Autostart launcher for Fitness Robot on Raspberry Pi.
#
# Clone path: /home/fitness/Documents/FitBot/Fitness-Robot
#
# One-time install (enables launch on desktop login):
#   chmod +x autostart_fitness.sh
#   ./autostart_fitness.sh --install
#
# Boot / login runs this script with --run.
# -------------------------------------------------------------------------

set -u

DEFAULT_REPO="/home/fitness/Documents/FitBot/Fitness-Robot"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "${SCRIPT_DIR}/person_raspi.py" ]; then
    REPO_DIR="${SCRIPT_DIR}"
elif [ -f "${DEFAULT_REPO}/person_raspi.py" ]; then
    REPO_DIR="${DEFAULT_REPO}"
else
    echo "Fitness Robot repo not found. Expected person_raspi.py in:"
    echo "  ${SCRIPT_DIR}"
    echo "  ${DEFAULT_REPO}"
    exit 1
fi

cd "${REPO_DIR}"

# Default is install-only so SSH does not try to open the Tk window.
MODE="${1:-install}"
LOG_FILE="${REPO_DIR}/autostart.log"
LOCK_FILE="/tmp/fitness_robot.lock"
DESKTOP_NAME="fitness_robot.desktop"
VENV_DIR="/home/fitness/Documents/project_env"

export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-${HOME}/.Xauthority}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PYTHONUNBUFFERED=1

install_autostart() {
    local autostart_dir="${HOME}/.config/autostart"
    local desktop_file="${autostart_dir}/${DESKTOP_NAME}"
    local launcher="${REPO_DIR}/autostart_fitness.sh"

    mkdir -p "${autostart_dir}"
    chmod +x "${launcher}" 2>/dev/null || true

    cat > "${desktop_file}" <<EOF
[Desktop Entry]
Type=Application
Name=Fitness Robot
Comment=Start Fitness Robot UI after desktop login
Exec=/bin/bash "${launcher}" --run
Path=${REPO_DIR}
WorkingDirectory=${REPO_DIR}
StartupNotify=false
Terminal=false
Hidden=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=8
EOF
    chmod +x "${desktop_file}"

    # Raspberry Pi OS Bookworm+ (labwc)
    local labwc_dir="${HOME}/.config/labwc"
    local labwc_file="${labwc_dir}/autostart"
    mkdir -p "${labwc_dir}"
    touch "${labwc_file}"
    chmod +x "${labwc_file}"
    if ! grep -q "autostart_fitness.sh" "${labwc_file}" 2>/dev/null; then
        printf '\n# Fitness Robot\n/bin/bash "%s" --run &\n' "${launcher}" >> "${labwc_file}"
    fi

    # Older Bookworm (wayfire)
    local wayfire_ini="${HOME}/.config/wayfire.ini"
    if [ -f "${wayfire_ini}" ]; then
        if ! grep -q "autostart_fitness.sh" "${wayfire_ini}" 2>/dev/null; then
            if ! grep -q "^\[autostart\]" "${wayfire_ini}" 2>/dev/null; then
                printf '\n[autostart]\n' >> "${wayfire_ini}"
            fi
            printf 'fitness_robot = /bin/bash "%s" --run\n' "${launcher}" >> "${wayfire_ini}"
        fi
    fi

    echo "==========================================="
    echo "  Fitness Robot - Autostart Installed"
    echo "==========================================="
    echo "Repo:    ${REPO_DIR}"
    echo "Desktop: ${desktop_file}"
    echo "Labwc:   ${labwc_file}"
    echo "Python:  ${VENV_DIR}/bin/python3"
    echo "Log:     ${LOG_FILE}"
    echo
    echo "Install does not start the GUI (safe over SSH)."
    echo "Reboot, or log out and back in, to start the app."
}

wait_for_session() {
    local i
    for i in $(seq 1 60); do
        if [ -S "${XDG_RUNTIME_DIR}/wayland-0" ] || [ -S "${XDG_RUNTIME_DIR}/wayland-1" ]; then
            [ -z "${WAYLAND_DISPLAY:-}" ] && export WAYLAND_DISPLAY=wayland-0
            return 0
        fi
        if command -v xset >/dev/null 2>&1 && xset q >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 0
}

wait_for_camera() {
    local i
    for i in $(seq 1 20); do
        if [ -e /dev/video0 ] || [ -e /dev/video1 ]; then
            return 0
        fi
        sleep 1
    done
    return 0
}

activate_venv() {
    local venv_path py
    for venv_path in \
        "${VENV_DIR}" \
        "${REPO_DIR}/venv" \
        "${REPO_DIR}/project_env" \
        "${REPO_DIR}/../project_env" \
        "${REPO_DIR}/../dp311_env"
    do
        for py in "${venv_path}/bin/python3" "${venv_path}/bin/python"; do
            if [ -x "${py}" ]; then
                echo "Using Python: ${py}"
                PYTHON_BIN="${py}"
                return 0
            fi
        done
    done
    echo "WARNING: No virtual environment found at ${VENV_DIR}. Using system python3."
    PYTHON_BIN="python3"
}

run_app() {
    exec 9>"${LOCK_FILE}"
    if ! flock -n 9; then
        echo "$(date -Iseconds) Fitness Robot already running; exiting."
        exit 0
    fi

    wait_for_session
    sleep 3
    wait_for_camera

    activate_venv

    echo "$(date -Iseconds) Starting person_raspi.py from ${REPO_DIR}"
    echo "DISPLAY=${DISPLAY} WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-} XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR}"

    cd "${REPO_DIR}"
    exec "${PYTHON_BIN}" "${REPO_DIR}/person_raspi.py"
}

case "${MODE}" in
    --install|install)
        install_autostart
        ;;
    --run|run)
        mkdir -p "$(dirname "${LOG_FILE}")"
        exec >>"${LOG_FILE}" 2>&1
        run_app
        ;;
    --all|all)
        install_autostart
        mkdir -p "$(dirname "${LOG_FILE}")"
        exec >>"${LOG_FILE}" 2>&1
        run_app
        ;;
    *)
        echo "Usage: $0 [--install|--run]"
        exit 1
        ;;
esac
