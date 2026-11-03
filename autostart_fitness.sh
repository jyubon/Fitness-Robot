#!/bin/bash
# FITNESS_ROBOT_AUTOSTART_V4
# If --install prints "Autostart Synced", the Pi still has the old script.
# -------------------------------------------------------------------------
# Clone path: /home/fitness/Documents/FitBot/Fitness-Robot
#
#   ./autostart_fitness.sh --install   # SSH-safe: register only
#   reboot / log in to desktop         # starts the UI
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

# SSH (tty) defaults to install. Desktop autostart (no tty) defaults to run.
if [ -n "${1:-}" ]; then
    MODE="$1"
elif [ -t 0 ]; then
    MODE="install"
else
    MODE="run"
fi

LOG_FILE="${REPO_DIR}/autostart.log"
LOCK_FILE="/tmp/fitness_robot.lock"
DESKTOP_NAME="fitness_robot.desktop"
VENV_DIR="/home/fitness/Documents/project_env"
LAUNCHER="${REPO_DIR}/autostart_fitness.sh"

# Tkinter / OpenCV windows on Raspberry Pi need X11 (Xwayland), not Wayland.
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-${HOME}/.Xauthority}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export GDK_BACKEND=x11
export QT_QPA_PLATFORM=xcb
unset WAYLAND_DISPLAY || true
export PYTHONUNBUFFERED=1

install_autostart() {
    local autostart_dir="${HOME}/.config/autostart"
    local desktop_file="${autostart_dir}/${DESKTOP_NAME}"

    mkdir -p "${autostart_dir}"
    chmod +x "${LAUNCHER}" 2>/dev/null || true

    # No quotes in Exec — some Pi session helpers break on them.
    cat > "${desktop_file}" <<EOF
[Desktop Entry]
Type=Application
Name=Fitness Robot
Comment=Start Fitness Robot UI after desktop login
Exec=/bin/bash ${LAUNCHER} --run
Path=${REPO_DIR}
StartupNotify=false
Terminal=false
Hidden=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=5
EOF
    chmod +x "${desktop_file}"

    # labwc (current Raspberry Pi OS desktop)
    local labwc_dir="${HOME}/.config/labwc"
    local labwc_file="${labwc_dir}/autostart"
    mkdir -p "${labwc_dir}"
    if [ ! -s "${labwc_file}" ]; then
        if [ -f /etc/xdg/labwc/autostart ]; then
            cp /etc/xdg/labwc/autostart "${labwc_file}"
        else
            touch "${labwc_file}"
        fi
    fi
    if grep -q "autostart_fitness.sh" "${labwc_file}" 2>/dev/null; then
        grep -v "autostart_fitness.sh" "${labwc_file}" > "${labwc_file}.tmp"
        mv "${labwc_file}.tmp" "${labwc_file}"
    fi
    printf '\n# Fitness Robot\n/bin/bash %s --run &\n' "${LAUNCHER}" >> "${labwc_file}"
    chmod +x "${labwc_file}"

    # wayfire (older Bookworm)
    local wayfire_ini="${HOME}/.config/wayfire.ini"
    if [ -f "${wayfire_ini}" ]; then
        grep -v "autostart_fitness.sh" "${wayfire_ini}" > "${wayfire_ini}.tmp" || true
        mv "${wayfire_ini}.tmp" "${wayfire_ini}"
        if ! grep -q "^\[autostart\]" "${wayfire_ini}" 2>/dev/null; then
            printf '\n[autostart]\n' >> "${wayfire_ini}"
        fi
        printf 'fitness_robot = /bin/bash %s --run\n' "${LAUNCHER}" >> "${wayfire_ini}"
    fi

    # LXDE-pi (legacy)
    local lx_dir="${HOME}/.config/lxsession/LXDE-pi"
    local lx_file="${lx_dir}/autostart"
    mkdir -p "${lx_dir}"
    touch "${lx_file}"
    if grep -q "autostart_fitness.sh" "${lx_file}" 2>/dev/null; then
        grep -v "autostart_fitness.sh" "${lx_file}" > "${lx_file}.tmp"
        mv "${lx_file}.tmp" "${lx_file}"
    fi
    printf '@/bin/bash %s --run\n' "${LAUNCHER}" >> "${lx_file}"

    echo "==========================================="
    echo "  Fitness Robot - Autostart Installed (v4)"
    echo "==========================================="
    echo "Repo:    ${REPO_DIR}"
    echo "Desktop: ${desktop_file}"
    echo "Labwc:   ${labwc_file}"
    echo "Python:  ${VENV_DIR}/bin/python3"
    echo "Log:     ${LOG_FILE}"
    echo
    echo "Install does not start the GUI (safe over SSH)."
    echo "Reboot the Pi, or log out and back in on the screen."
    echo "If the window still does not appear, run:"
    echo "  tail -n 50 ${LOG_FILE}"
}

wait_for_session() {
    local i
    for i in $(seq 1 20); do
        if [ -S "${XDG_RUNTIME_DIR}/wayland-0" ] || [ -S "${XDG_RUNTIME_DIR}/wayland-1" ]; then
            return 0
        fi
        if [ -S "/tmp/.X11-unix/X0" ]; then
            return 0
        fi
        if command -v xset >/dev/null 2>&1 && xset q >/dev/null 2>&1; then
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
    echo "$(date -Iseconds) autostart v4 --run uid=$(id -u) cwd=$(pwd)"
    echo "DISPLAY=${DISPLAY:-} XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-} GDK_BACKEND=${GDK_BACKEND:-}"

    exec 9>"${LOCK_FILE}"
    if ! flock -n 9; then
        echo "$(date -Iseconds) already running (lock ${LOCK_FILE}); exiting."
        exit 0
    fi

    wait_for_session
    sleep 2

    activate_venv
    echo "$(date -Iseconds) starting ${PYTHON_BIN} ${REPO_DIR}/person_raspi.py"
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
