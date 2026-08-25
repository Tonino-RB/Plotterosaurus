#!/usr/bin/env bash
# Idempotent install script for Plotterosaurus.
# Run from the project root on the Raspberry Pi: ./install.sh
# Safe to re-run after `git pull` to update dependencies and restart the service.
#
# Unattended installs:
#   SUDO_PW='your-password'   — pipe into sudo -S
#   PLOTTER_MODEL=<1-8>       — set axidraw model for this installation
#   ENABLE_CAMERA=1           — set up plot recording via a Pi Camera Module 3 +
#                               MediaMTX (opt-in: skipped entirely otherwise)
#   ENABLE_DRAW_STREAM=1      — turn on the /draw-stream live "draw progress"
#                               page for an OBS Browser Source (opt-in: pure
#                               app code, no extra packages/services either way)
#   ENABLE_SELF_UPDATE=1      — install the in-app "Update now" path (root-owned
#                               wrapper + passwordless sudo rule that does
#                               `git fetch` + `git reset --hard` against
#                               REPO_URL below, then re-runs this installer).
#                               Off by default: safe for a stock checkout, but
#                               a `reset --hard` would wipe out any local fork
#                               changes, so it's opt-in rather than assumed.
#
# If port 80 is free the service binds there; otherwise it falls back to 8080.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="plotterosaurus"
UNIT_SRC="$PROJECT_DIR/systemd/$SERVICE_NAME.service"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME.service"
REPO_URL="https://github.com/Tonino-RB/Plotterosaurus.git"
# The service runs as a specific non-root user. Normally that's whoever invokes
# this script; the self-update path runs the script as root inside a transient
# unit and passes the user in via SERVICE_USER (falling back to SUDO_USER).
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-${USER:-$(whoami)}}}"
MIN_PY_MAJOR=3
MIN_PY_MINOR=11

run_sudo() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif [ -n "${SUDO_PW:-}" ]; then
        echo "$SUDO_PW" | sudo -S "$@"
    else
        sudo "$@"
    fi
}

# Run a command as the service user. When this script is invoked as root (the
# self-update path) the venv and pip must not be created root-owned, so drop
# privileges; otherwise run directly.
as_user() {
    if [ "$(id -u)" -eq 0 ] && [ "$SERVICE_USER" != "root" ]; then
        runuser -u "$SERVICE_USER" -- "$@"
    else
        "$@"
    fi
}

fail() { echo "!!! $*" >&2; exit 1; }

# Flip a boolean flag on in an existing config.json. app/config.py only
# applies an opt-in feature's ENABLE_* env var as the setting's default the
# very first time config.json is created; once that file exists (e.g. from an
# earlier install run without the feature), its persisted value permanently
# wins over the env var on every later boot, and there's no UI toggle to flip
# it back on (the feature's settings are themselves hidden while its flag is
# false) — so a re-run against an already-configured install needs a direct
# patch. No-ops if config.json doesn't exist yet (a fresh install already
# picks up the env var's default).
enable_config_flag() {
    local key="$1"
    [ -f "$PROJECT_DIR/config.json" ] || return 0
    echo "    enabling $key in existing config.json"
    local py; py="$(mktemp --suffix=.py)"
    cat > "$py" <<PYEOF
import json, sys

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
data["$key"] = True
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PYEOF
    chmod 644 "$py"
    as_user python3 "$py" "$PROJECT_DIR/config.json"
    rm -f "$py"
}

echo ">>> Checking prerequisites"

# Python version
if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 not found"
fi
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="$(echo "$PY_VER" | cut -d. -f1)"
PY_MINOR="$(echo "$PY_VER" | cut -d. -f2)"
if [ "$PY_MAJOR" -lt "$MIN_PY_MAJOR" ] || \
   { [ "$PY_MAJOR" -eq "$MIN_PY_MAJOR" ] && [ "$PY_MINOR" -lt "$MIN_PY_MINOR" ]; }; then
    fail "python $MIN_PY_MAJOR.$MIN_PY_MINOR+ required, found $PY_VER"
fi
echo "    python $PY_VER"

# dialout group (needed for /dev/ttyACM* access)
if ! id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx dialout; then
    fail "user '$SERVICE_USER' is not in the 'dialout' group.
    Fix:    sudo usermod -a -G dialout $SERVICE_USER
    Then log out and back in (or reboot) so the membership takes effect."
fi
echo "    '$SERVICE_USER' is in dialout group"

# video group (needed for camera access; only relevant with ENABLE_CAMERA=1)
if [ "${ENABLE_CAMERA:-}" = "1" ] && ! id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx video; then
    fail "user '$SERVICE_USER' is not in the 'video' group (needed for camera access).
    Fix:    sudo usermod -a -G video $SERVICE_USER
    Then log out and back in (or reboot) so the membership takes effect."
fi

# avahi (for plotterstudio.local)
if systemctl is-active --quiet avahi-daemon; then
    echo "    avahi-daemon active ($(hostname).local should resolve)"
else
    echo "    warning: avahi-daemon is not running; '.local' hostname resolution may not work"
fi

# systemd as the active init. This installer manages the service exclusively
# through systemctl; on a system that boots a different init as PID 1 those
# calls may silently no-op and the service never actually starts, or something
# else ends up answering on the local box. Raspberry Pi OS is systemd-booted.
if [ "$(ps -p 1 -o comm= 2>/dev/null)" != "systemd" ]; then
    echo "    warning: PID 1 is not systemd; this installer requires a systemd-booted system."
    echo "             The service is managed via 'systemctl' and will not start under another init."
    echo "             Plotterosaurus is only tested on Raspberry Pi OS."
fi

# If a previous install is already running, stop it so its own port-80
# binding doesn't look like a conflict during the port probe below.
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo "    $SERVICE_NAME already running; stopping for clean reinstall"
    run_sudo systemctl stop "$SERVICE_NAME"
fi

# Retire a pre-rename "plotterhub" unit left over from before this project
# was forked as Plotterosaurus. Only touched if it points at this exact repo
# checkout, so an unrelated same-named service elsewhere is never disturbed.
LEGACY_UNIT="/etc/systemd/system/plotterhub.service"
if [ -f "$LEGACY_UNIT" ] && grep -q "^WorkingDirectory=$PROJECT_DIR$" "$LEGACY_UNIT"; then
    echo ">>> Retiring legacy plotterhub.service (superseded by $SERVICE_NAME)"
    run_sudo systemctl disable --now plotterhub
    run_sudo rm -f "$LEGACY_UNIT"
    run_sudo systemctl daemon-reload
fi

# Pick a free port
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE '(^|:)80$'; then
    PORT=8080
    echo "    port 80 is taken; using $PORT instead"
else
    PORT=80
    echo "    port $PORT available"
fi

echo ">>> Installing system packages"
run_sudo apt-get update
run_sudo apt-get install -y python3 python3-venv python3-pip

echo ">>> Creating Python virtualenv"
if [ ! -d "$PROJECT_DIR/venv" ]; then
    as_user python3 -m venv "$PROJECT_DIR/venv"
fi

echo ">>> Installing Python dependencies"
as_user "$PROJECT_DIR/venv/bin/pip" install --upgrade pip wheel
as_user "$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

echo ">>> Installing systemd unit"
run_sudo cp "$UNIT_SRC" "$UNIT_DST"

# The repo's unit is a template; fill in the service user and the project
# path so it runs as the invoking user from wherever the repo was cloned.
run_sudo sed -i "s|__SERVICE_USER__|$SERVICE_USER|g" "$UNIT_DST"
run_sudo sed -i "s|__WORKDIR__|$PROJECT_DIR|g" "$UNIT_DST"

# Rewrite the port if we fell back.
if [ "$PORT" != "80" ]; then
    run_sudo sed -i "s|--port 80|--port $PORT|" "$UNIT_DST"
    # Low-port capability is only needed for ports <1024.
    run_sudo sed -i '/^AmbientCapabilities=/d' "$UNIT_DST"
fi

if [ -n "${PLOTTER_MODEL:-}" ]; then
    echo ">>> Setting PLOTTER_MODEL=$PLOTTER_MODEL in unit"
    run_sudo sed -i "s/^Environment=PLOTTER_MODEL=.*/Environment=PLOTTER_MODEL=$PLOTTER_MODEL/" "$UNIT_DST"
fi

# Camera recording (opt-in) -------------------------------------------------
# Sets up MediaMTX as a separate systemd service that reads a Pi Camera
# Module 3 directly (its native `rpiCamera` source — no rpicam-vid/ffmpeg
# piping needed for capture) and serves the live RTSP/HLS/WebRTC stream plus
# on-disk recording segments. Plotterosaurus only drives MediaMTX's local
# Control API (127.0.0.1:9997) and post-processes the segments with ffmpeg —
# see app/camera.py. Skipped entirely unless explicitly requested, since most
# installs have no camera attached.
if [ "${ENABLE_CAMERA:-}" = "1" ]; then
    echo ">>> Setting up camera recording (MediaMTX)"
    # No unversioned libcamera0 package: Raspberry Pi OS ships it as a
    # versioned package (e.g. libcamera0.7) that rpicam-apps already pulls in
    # as a dependency, so it doesn't need to be named explicitly here.
    run_sudo apt-get install -y rpicam-apps libfreetype6 ffmpeg

    MEDIAMTX_DIR="/opt/mediamtx"
    MEDIAMTX_BIN="$MEDIAMTX_DIR/mediamtx"
    if [ ! -x "$MEDIAMTX_BIN" ]; then
        echo "    downloading latest MediaMTX (arm64)"
        # No curl/jq assumed present (README's dependency list doesn't
        # guarantee them) — python3 already is, so resolve + download with it.
        MEDIAMTX_TGZ="$(mktemp --suffix=.tar.gz)"
        python3 - "$MEDIAMTX_TGZ" <<'PYEOF'
import json, sys, urllib.request

dest = sys.argv[1]
data = json.load(urllib.request.urlopen(
    "https://api.github.com/repos/bluenviron/mediamtx/releases/latest", timeout=15))
url = next((a["browser_download_url"] for a in data["assets"]
            if a["name"].endswith("_linux_arm64.tar.gz")), None)
if not url:
    sys.exit("could not find a linux_arm64 asset in the latest MediaMTX release")
urllib.request.urlretrieve(url, dest)
PYEOF
        run_sudo mkdir -p "$MEDIAMTX_DIR"
        run_sudo tar -xzf "$MEDIAMTX_TGZ" -C "$MEDIAMTX_DIR"
        rm -f "$MEDIAMTX_TGZ"
        run_sudo chmod +x "$MEDIAMTX_BIN"
    fi

    echo "    configuring $MEDIAMTX_DIR/mediamtx.yml"
    # The release tarball bundles MediaMTX's own default mediamtx.yml (every
    # section besides `paths:` is best left at its shipped default). `paths:`
    # is the file's last top-level section, so replace everything from that
    # marker onward with our single rpiCamera path rather than hand-writing
    # the ~100-key file from scratch.
    #
    # Written to a temp file rather than piped in via a heredoc: run_sudo's
    # SUDO_PW path pipes the password into the command's own stdin, which
    # would otherwise clobber a heredoc meant for python3.
    CONFIGURE_YML_PY="$(mktemp --suffix=.py)"
    cat > "$CONFIGURE_YML_PY" <<'PYEOF'
import re, sys

path = sys.argv[1]
text = open(path, encoding="utf-8").read()
text = re.split(r"^paths:", text, maxsplit=1, flags=re.MULTILINE)[0]
# MoQ (Media over QUIC) is on by default and, on first run, tries to write a
# self-signed TLS cert (auto.key/auto.crt) to its working directory. That
# directory is root-owned (created by this installer via sudo) while
# MediaMTX itself runs as the unprivileged service user, so the write fails
# and MediaMTX treats it as fatal, taking down RTSP/HLS/WebRTC with it even
# though Plotterosaurus never uses MoQ at all. Just turn it off.
text = re.sub(r"^moq: true", "moq: false", text, count=1, flags=re.MULTILINE)
# The Control API is off by default; app/camera.py needs it (127.0.0.1:9997)
# to drive recording/focus/settings. It has no authentication of its own, so
# bind it to loopback only rather than the default all-interfaces ":9997" —
# it's meant to be reachable from Plotterosaurus on the same box, not the LAN.
text = re.sub(r"^api: false", "api: true", text, count=1, flags=re.MULTILINE)
text = re.sub(r"^apiAddress: :9997", "apiAddress: 127.0.0.1:9997", text, count=1, flags=re.MULTILINE)
text += (
    "paths:\n"
    "  cam:\n"
    "    source: rpiCamera\n"
    "    rpiCameraWidth: 1920\n"
    "    rpiCameraHeight: 1080\n"
    "    rpiCameraFPS: 30\n"
)
open(path, "w", encoding="utf-8").write(text)
PYEOF
    run_sudo python3 "$CONFIGURE_YML_PY" "$MEDIAMTX_DIR/mediamtx.yml"
    rm -f "$CONFIGURE_YML_PY"

    echo "    installing mediamtx systemd unit"
    run_sudo cp "$PROJECT_DIR/systemd/mediamtx.service" /etc/systemd/system/mediamtx.service
    run_sudo sed -i "s|__SERVICE_USER__|$SERVICE_USER|g" /etc/systemd/system/mediamtx.service
    run_sudo systemctl daemon-reload
    run_sudo systemctl enable mediamtx
    run_sudo systemctl restart mediamtx

    echo ">>> Enabling camera recording in plotterosaurus.service"
    run_sudo sed -i "s/^Environment=ENABLE_CAMERA=.*/Environment=ENABLE_CAMERA=1/" "$UNIT_DST"
    enable_config_flag camera_enabled
fi

# Draw-stream OBS overlay (opt-in) ------------------------------------------
# Pure application code — no system packages, no separate service. Just flips
# the same kind of env-var-seeded default the camera uses (see app/config.py
# draw_stream_enabled), so the settings UI/page stay hidden until asked for.
if [ "${ENABLE_DRAW_STREAM:-}" = "1" ]; then
    echo ">>> Enabling draw-stream in plotterosaurus.service"
    run_sudo sed -i "s/^Environment=ENABLE_DRAW_STREAM=.*/Environment=ENABLE_DRAW_STREAM=1/" "$UNIT_DST"
    enable_config_flag draw_stream_enabled
fi

echo ">>> Installing sudoers rule for shutdown button"
SUDOERS_DST="/etc/sudoers.d/plotterosaurus-shutdown"
SUDOERS_TMP="$(mktemp)"
printf '%s ALL=(root) NOPASSWD: /sbin/shutdown\n' "$SERVICE_USER" > "$SUDOERS_TMP"
chmod 0440 "$SUDOERS_TMP"
run_sudo visudo -cf "$SUDOERS_TMP" >/dev/null
run_sudo install -m 0440 -o root -g root "$SUDOERS_TMP" "$SUDOERS_DST"
rm -f "$SUDOERS_TMP"

if [ "${ENABLE_SELF_UPDATE:-}" = "1" ]; then
    echo ">>> Installing self-update wrapper"
    # Root-owned wrapper at a fixed path (so the NOPASSWD grant can't be widened by
    # editing a repo file), with the project dir / user / repo baked in.
    WRAPPER_DST="/usr/local/sbin/plotterosaurus-update"
    WRAPPER_TMP="$(mktemp)"
    sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
        -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
        -e "s|__REPO_URL__|$REPO_URL|g" \
        "$PROJECT_DIR/scripts/plotterosaurus-update.in" > "$WRAPPER_TMP"
    run_sudo install -m 0755 -o root -g root "$WRAPPER_TMP" "$WRAPPER_DST"
    rm -f "$WRAPPER_TMP"

    echo ">>> Installing sudoers rule for self-update"
    UPD_SUDOERS_DST="/etc/sudoers.d/plotterosaurus-update"
    UPD_SUDOERS_TMP="$(mktemp)"
    printf '%s ALL=(root) NOPASSWD: %s "", %s --dry-run\n' \
        "$SERVICE_USER" "$WRAPPER_DST" "$WRAPPER_DST" > "$UPD_SUDOERS_TMP"
    chmod 0440 "$UPD_SUDOERS_TMP"
    run_sudo visudo -cf "$UPD_SUDOERS_TMP" >/dev/null
    run_sudo install -m 0440 -o root -g root "$UPD_SUDOERS_TMP" "$UPD_SUDOERS_DST"
    rm -f "$UPD_SUDOERS_TMP"
else
    # Remove any grant/wrapper from a previous install so re-running this
    # script (e.g. to pick up ENABLE_CAMERA) can't leave a stale passwordless
    # `git reset --hard` path lying around once self-update is opted out of.
    run_sudo rm -f /etc/sudoers.d/plotterosaurus-update /usr/local/sbin/plotterosaurus-update
fi

run_sudo systemctl daemon-reload
run_sudo systemctl enable "$SERVICE_NAME"
run_sudo systemctl restart "$SERVICE_NAME"

echo ">>> Waiting for service to come up"
sleep 3
if run_sudo systemctl is-active --quiet "$SERVICE_NAME"; then
    echo ">>> Service is running"

    # Confirm the listener is actually reachable from the LAN and not just from
    # the local box. A socket bound to 127.0.0.1/::1 answers the machine's own
    # browser but is invisible to every other host — the classic "loads locally,
    # never loads from another machine" report. We bind to 0.0.0.0 in the unit,
    # so anything else here means a stale/overridden unit or a hand-started copy.
    #
    # Poll rather than check once: systemd marks the Type=simple unit active the
    # moment the process forks, but uvicorn's heavy imports (numpy/scipy/vpype)
    # can delay the actual socket bind by 10s+ on a Pi, so a single check races
    # startup and cries wolf.
    LISTEN=""
    for _ in $(seq 1 15); do
        LISTEN="$(ss -ltn 2>/dev/null | awk -v p=":$PORT\$" '$4 ~ p {print $4}')"
        [ -n "$LISTEN" ] && break
        sleep 2
    done
    if [ -z "$LISTEN" ]; then
        echo "!!! warning: nothing is listening on port $PORT even though the service is active."
        echo "             Inspect with: journalctl -u $SERVICE_NAME -n 50"
    elif ! printf '%s\n' "$LISTEN" | grep -qE '^(0\.0\.0\.0|\*|\[::\]):'"$PORT"'$'; then
        echo "!!! warning: port $PORT is bound to loopback only ($LISTEN)."
        echo "             It will load on this machine but NOT from other machines on the network."
        echo "             Expected 0.0.0.0:$PORT — check for a stale or overridden systemd unit."
    fi

    URL="http://$(hostname).local"
    [ "$PORT" != "80" ] && URL="$URL:$PORT"
    echo ">>> Open $URL from any machine on the network"

    # Also print the raw LAN IP as a fallback: '.local' is the address to use
    # (mDNS follows the Pi across DHCP lease changes, the IP may not), but if a
    # client can't resolve '.local' the IP still gets them in — and printing it
    # makes a port-80 -> 8080 fallback obvious too.
    LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    if [ -n "$LAN_IP" ]; then
        IP_URL="http://$LAN_IP"
        [ "$PORT" != "80" ] && IP_URL="$IP_URL:$PORT"
        echo ">>> If a device can't resolve .local, use this Pi's IP instead: $IP_URL (may change on reboot)"
    fi
else
    echo "!!! Service failed to start; inspect with: journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi
