#!/bin/bash
# install.sh — voicekey: venv, dependencies, models, systemd unit, DMS widget. Idempotent.
#
# System prerequisites (Fedora names): pipewire-utils (pw-record), wtype,
# wl-clipboard, libnotify (notify-send), gcc (evdev builds from source), uv.
# The optional polish pass needs llama.cpp, which --download fetches.
# Config: ~/.config/voicekey/config.toml — created from config.example.toml if
# absent; symlink your own (per-machine) file there instead if you keep one.
# The venv lives outside the repo so binary wheels never travel with it.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="$HOME/.local/share/voicekey/venv"
CONFIG="$HOME/.config/voicekey/config.toml"
UNIT="$HOME/.config/systemd/user/voicekey.service"
WIDGET="${XDG_CONFIG_HOME:-$HOME/.config}/DankMaterialShell/plugins/Voicekey"

install_widget() {
    if ! command -v dms >/dev/null; then
        echo "  DMS: not installed; skipping the optional widget (rerun install.sh after installing DMS)."
        return
    fi
    if [[ -e "$WIDGET" || -L "$WIDGET" ]]; then
        if [[ ! -L "$WIDGET" || "$(readlink -f "$WIDGET")" != "$HERE/contrib/dms/Voicekey" ]]; then
            echo "  DMS: leaving existing $WIDGET untouched; see README.md for manual widget setup."
            return
        fi
    else
        if ! mkdir -p "$(dirname "$WIDGET")" || ! ln -s "$HERE/contrib/dms/Voicekey" "$WIDGET"; then
            echo "  DMS: could not link widget; see README.md for manual setup."
            return
        fi
    fi
    echo "  DMS: $WIDGET -> $HERE/contrib/dms/Voicekey"
    local reply attempt
    # IPC can succeed as a command while reporting an application-level error.
    if reply=$(timeout 3s dms ipc call plugin-scan scan 2>/dev/null) && [[ "$reply" == *SCAN_TRIGGERED:* ]]; then
        for ((attempt = 0; attempt < 10; attempt++)); do
            if ! reply=$(timeout 3s dms ipc call plugins status voicekey 2>/dev/null); then
                break
            fi
            case "$reply" in
                loaded) break ;;
                disabled)
                    if reply=$(timeout 3s dms ipc call plugins enable voicekey 2>/dev/null) && [[ "$reply" == *PLUGIN_ENABLE_SUCCESS:* ]]; then
                        reply=loaded
                    fi
                    break ;;
            esac
            sleep 0.5
        done
        if [[ "$reply" == loaded ]]; then
            echo "  DMS: Voicekey enabled. Add it in DMS Settings → DankBar → Widgets."
            return
        fi
    fi
    echo "  DMS: widget linked, but automatic enabling was unavailable. With DMS running, use:"
    echo "       dms ipc call plugin-scan scan"
    echo "       # Once scanning finishes:"
    echo "       dms ipc call plugins enable voicekey"
    echo "       Then add Voicekey in DMS Settings → DankBar → Widgets."
}

if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "  Would create $VENV (python 3.12 via uv), install $HERE, download models,"
    echo "  link $UNIT -> $HERE/voicekey.service, enable voicekey.service and run --check"
    echo "  If dms is installed, link $WIDGET and enable the widget when DMS is running."
    exit 0
fi

command -v uv >/dev/null || {
    echo "  uv not found — install it (dnf install uv, or https://astral.sh/uv)"; exit 1; }

if [[ ! -e "$CONFIG" ]]; then
    echo "  CONFIG: $CONFIG <- config.example.toml (edit the keys if needed)"
    mkdir -p "$(dirname "$CONFIG")"
    cp "$HERE/config.example.toml" "$CONFIG"
fi

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "  VENV: $VENV"
    uv python install 3.12
    uv venv --python 3.12 "$VENV"
fi

extra=""
if "$VENV/bin/python" - "$CONFIG" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as f:
    sys.exit(0 if tomllib.load(f).get("backend", {}).get("type") == "faster-whisper" else 1)
PY
then extra="[whisper]"; fi
echo "  INSTALL: voicekey$extra"
uv pip install --quiet --python "$VENV/bin/python" -e "$HERE$extra"

"$VENV/bin/python" -m voicekey --download

echo "  UNIT: $UNIT -> $HERE/voicekey.service"
mkdir -p "$(dirname "$UNIT")"
ln -sfnT "$HERE/voicekey.service" "$UNIT"
systemctl --user daemon-reload
systemctl --user enable voicekey.service

set +e
"$VENV/bin/python" -m voicekey --check
status=$?
set -e
case "$status" in
    0|3) ;;
    2) echo "  NOTE: no keyboard is readable. Run:  sudo usermod -aG input $USER"
       echo "        then log out and back in; the service starts with the next session." ;;
    *) echo "  ERROR: voicekey --check failed"; exit "$status" ;;
esac
if [[ "$status" != 2 ]] && systemctl --user is-active --quiet graphical-session.target; then
    systemctl --user restart voicekey.service
    echo "  voicekey.service restarted"
fi
install_widget
