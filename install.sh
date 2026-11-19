#!/usr/bin/env bash
# Interactive installer for the tapo-watcher systemd user service.
#
#   ./install.sh install     # prompt for settings, write tapo.env, install + start the service
#   ./install.sh uninstall   # stop + remove the service (optionally tapo.env / learned state)
#   ./install.sh             # menu
#
# Runs as your normal user (no sudo), except the optional "loginctl enable-linger".

set -euo pipefail

SERVICE=tapo-watcher
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$PROJECT_DIR/tapo.env"
STATE_FILE="$PROJECT_DIR/tapo-watcher.state.json"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/$SERVICE.service"

# ---------------------------------------------------------------- helpers

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '\033[34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mWarning:\033[0m %s\n' "$*" >&2; }
die() {
    printf '\033[31mError:\033[0m %s\n' "$*" >&2
    exit 1
}

# ask VAR "Question" [default] -> sets VAR, re-prompts while empty and no default
ask() {
    local __var=$1 __prompt=$2 __default=${3-} __reply
    while true; do
        if [[ -n $__default ]]; then
            read -r -p "$__prompt [$__default]: " __reply
            __reply=${__reply:-$__default}
        else
            read -r -p "$__prompt: " __reply
        fi
        [[ -n $__reply ]] && break
        echo "  A value is required."
    done
    printf -v "$__var" '%s' "$__reply"
}

# confirm "Question" [y|n] -> exit status 0 for yes
confirm() {
    local prompt=$1 default=${2:-n} reply hint
    [[ $default == y ]] && hint="Y/n" || hint="y/N"
    while true; do
        read -r -p "$prompt [$hint]: " reply
        reply=${reply:-$default}
        case ${reply,,} in
            y | yes | o | oui) return 0 ;;
            n | no | non) return 1 ;;
        esac
    done
}

# current value of KEY in tapo.env (quotes stripped), empty if missing
current() {
    [[ -f $ENV_FILE ]] || return 0
    local line
    line=$(grep -E "^[[:space:]]*$1=" "$ENV_FILE" | tail -n1 || true)
    line=${line#*=}
    line=${line%\"}
    line=${line#\"}
    line=${line%\'}
    line=${line#\'}
    printf '%s' "$line"
}

valid_ip() {
    local ip=$1 o
    [[ $ip =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    for o in "${BASH_REMATCH[@]:1}"; do ((o <= 255)) || return 1; done
}

valid_mac() { [[ $1 =~ ^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$ ]]; }

valid_uint() { [[ $1 =~ ^[0-9]+$ ]] && (($1 > 0)); }

valid_action() {
    [[ $1 =~ ^(preset:.+|move:-?[0-9]+,-?[0-9]+|privacy:(on|off)|none)$ ]]
}

# ask_valid VAR "Question" default validator "error message"
ask_valid() {
    local __var=$1 __prompt=$2 __default=$3 __check=$4 __msg=$5 __val
    while true; do
        ask __val "$__prompt" "$__default"
        "$__check" "$__val" && break
        echo "  $__msg"
    done
    printf -v "$__var" '%s' "$__val"
}

find_uv() {
    local uv
    uv=$(command -v uv 2>/dev/null || true)
    [[ -z $uv && -x $HOME/.local/bin/uv ]] && uv=$HOME/.local/bin/uv
    [[ -z $uv && -x $HOME/.cargo/bin/uv ]] && uv=$HOME/.cargo/bin/uv
    printf '%s' "$uv"
}

# ---------------------------------------------------------------- prompts

prompt_phones() {
    local existing ip mac entry n=1
    PHONES=""
    existing=$(current PHONES)
    if [[ -n $existing ]]; then
        echo "Current phones: $existing"
        if confirm "Keep these phones?" y; then
            PHONES=$existing
            # count the kept phones so the next one is numbered after them
            n=$(($(tr -cd , <<<"$existing" | wc -c) + 2))
            confirm "Add another phone?" n || return 0
        fi
    fi
    echo "Add the phones to watch (identified by MAC, the IP is an optional hint)."
    echo "iPhone: Settings > Wi-Fi > (i) > Private Wi-Fi Address = Off or Fixed."
    while true; do
        bold "Phone $n"
        while true; do
            read -r -p "  MAC address (aa:bb:cc:dd:ee:ff): " mac
            mac=${mac,,}
            mac=${mac//-/:}
            valid_mac "$mac" && break
            echo "  Invalid MAC address."
        done
        while true; do
            read -r -p "  IP address (optional, Enter to skip): " ip
            [[ -z $ip ]] || valid_ip "$ip" && break
            echo "  Invalid IP address."
        done
        entry="$ip=$mac"
        PHONES=${PHONES:+$PHONES,}$entry
        n=$((n + 1))
        confirm "Add another phone?" n || break
    done
}

# prompt_password VAR KEY "Label" -> sets VAR; KEY = tapo.env key holding the current value
prompt_password() {
    local __var=$1 key=$2 label=$3 p1 p2 existing
    existing=$(current "$key")
    while true; do
        if [[ -n $existing && $existing != change-me ]]; then
            read -r -s -p "$label (Enter to keep the current one): " p1
            echo
            if [[ -z $p1 ]]; then
                p1=$existing
                break
            fi
        else
            read -r -s -p "$label: " p1
            echo
            [[ -z $p1 ]] && {
                echo "  A value is required."
                continue
            }
        fi
        if [[ $p1 == *"'"* ]]; then
            echo "  The password cannot contain a single quote (')."
            continue
        fi
        read -r -s -p "Confirm password: " p2
        echo
        [[ $p1 == "$p2" ]] && break
        echo "  Passwords do not match."
    done
    printf -v "$__var" '%s' "$p1"
}

# Camera name -> tapo.env infix, like tapo_watcher.env_name ("living-room" -> "LIVING_ROOM")
env_name() {
    local n=${1^^}
    printf '%s' "${n//[^A-Z0-9]/_}"
}

valid_name() { [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; }

# Fills CAM_NAMES / CAM_IPS / CAM_USERS / CAM_PASSWORDS / CAM_HOME / CAM_AWAY
# and SHARED_ACCOUNT (1 = one account for every camera).
prompt_cameras() {
    local existing count i name key d user home away
    existing=$(current CAMERAS)
    local -a old_names=()
    [[ -n $existing ]] && IFS=', ' read -r -a old_names <<<"${existing//,/ }"
    count=${#old_names[@]}
    ((count > 0)) || count=1
    ask_valid count "Number of cameras" "$count" valid_uint "Positive integer expected."

    CAM_NAMES=() CAM_IPS=() CAM_USERS=() CAM_PASSWORDS=() CAM_HOME=() CAM_AWAY=()
    SHARED_ACCOUNT=1
    echo "Use the camera account from the Tapo app (Camera > Settings > Advanced > Camera Account)."
    echo "Actions: preset:<name|id>  move:<x>,<y>  privacy:on|off  none"

    if ((count == 1)) && [[ -z $existing ]]; then
        # Single camera: plain CAMERA_IP / CAMERA_USER / ... format
        CAM_NAMES=(camera)
        ask_valid d "Camera IP address" "$(current CAMERA_IP)" valid_ip "Invalid IP address."
        CAM_IPS=("$d")
        user=$(current CAMERA_USER)
        ask SHARED_USER "Camera account username" "${user:-admin}"
        prompt_password SHARED_PASSWORD CAMERA_PASSWORD "Camera password"
        home=$(current ACTION_HOME)
        away=$(current ACTION_AWAY)
        ask_valid d "Action when a phone is home" "${home:-preset:Home}" valid_action "Invalid action."
        CAM_HOME=("$d")
        ask_valid d "Action when all phones are away" "${away:-preset:Away}" valid_action "Invalid action."
        CAM_AWAY=("$d")
        return
    fi

    if confirm "Same camera account (username + password) for every camera?" y; then
        user=$(current CAMERA_USER)
        ask SHARED_USER "Camera account username" "${user:-admin}"
        prompt_password SHARED_PASSWORD CAMERA_PASSWORD "Camera password"
    else
        SHARED_ACCOUNT=0
    fi

    for ((i = 0; i < count; i++)); do
        echo
        bold "Camera $((i + 1))"
        while true; do
            ask_valid name "  Name (e.g. salon, entrance)" "${old_names[i]-}" valid_name \
                "Letters, digits, '-' and '_' only."
            key=$(env_name "$name")
            local dup=0 n
            for n in "${CAM_NAMES[@]}"; do [[ $(env_name "$n") == "$key" ]] && dup=1; done
            ((dup == 0)) && break
            echo "  This name is already used."
        done
        CAM_NAMES+=("$name")
        ask_valid d "  IP address" "$(current "CAMERA_${key}_IP")" valid_ip "Invalid IP address."
        CAM_IPS+=("$d")
        if ((SHARED_ACCOUNT == 0)); then
            user=$(current "CAMERA_${key}_USER")
            ask d "  Camera account username" "${user:-admin}"
            CAM_USERS+=("$d")
            prompt_password d "CAMERA_${key}_PASSWORD" "  Camera password"
            CAM_PASSWORDS+=("$d")
        fi
        home=$(current "CAMERA_${key}_ACTION_HOME")
        away=$(current "CAMERA_${key}_ACTION_AWAY")
        [[ -n $home ]] || home=$(current ACTION_HOME)
        [[ -n $away ]] || away=$(current ACTION_AWAY)
        ask_valid d "  Action when a phone is home" "${home:-preset:Home}" valid_action "Invalid action."
        CAM_HOME+=("$d")
        ask_valid d "  Action when all phones are away" "${away:-preset:Away}" valid_action "Invalid action."
        CAM_AWAY+=("$d")
    done
}

prompt_config() {
    bold "Cameras"
    prompt_cameras
    echo

    bold "Phones"
    prompt_phones
    echo

    bold "Timing"
    local v
    v=$(current INTERVAL)
    ask_valid INTERVAL "Seconds between two checks" "${v:-30}" valid_uint "Positive integer expected."
    v=$(current AWAY_AFTER_MISSES)
    ask_valid AWAY_AFTER_MISSES "Failed checks before away (until the delay is learned)" "${v:-6}" valid_uint "Positive integer expected."
    v=$(current ADAPTIVE_DELAY)
    if confirm "Learn the away delay per phone (adaptive delay)?" "$([[ ${v:-1} == 0 ]] && echo n || echo y)"; then
        ADAPTIVE_DELAY=1
    else
        ADAPTIVE_DELAY=0
    fi
}

# ---------------------------------------------------------------- writers

# Keep advanced settings already present in tapo.env (or use the defaults).
keep() {
    local v
    v=$(current "$1")
    printf '%s' "${v:-$2}"
}

# tapo.env lines for the cameras
camera_block() {
    local i key
    if [[ ${CAM_NAMES[0]} == camera && ${#CAM_NAMES[@]} -eq 1 ]]; then
        printf '%s\n' "CAMERA_IP=${CAM_IPS[0]}" "CAMERA_USER=$SHARED_USER" \
            "CAMERA_PASSWORD='$SHARED_PASSWORD'" "" \
            "ACTION_HOME=${CAM_HOME[0]}" "ACTION_AWAY=${CAM_AWAY[0]}"
        return
    fi
    local IFS=,
    echo "CAMERAS=${CAM_NAMES[*]}"
    unset IFS
    if ((SHARED_ACCOUNT)); then
        printf '%s\n' "CAMERA_USER=$SHARED_USER" "CAMERA_PASSWORD='$SHARED_PASSWORD'"
    fi
    for i in "${!CAM_NAMES[@]}"; do
        key=$(env_name "${CAM_NAMES[i]}")
        echo
        echo "CAMERA_${key}_IP=${CAM_IPS[i]}"
        if ((!SHARED_ACCOUNT)); then
            echo "CAMERA_${key}_USER=${CAM_USERS[i]}"
            echo "CAMERA_${key}_PASSWORD='${CAM_PASSWORDS[i]}'"
        fi
        echo "CAMERA_${key}_ACTION_HOME=${CAM_HOME[i]}"
        echo "CAMERA_${key}_ACTION_AWAY=${CAM_AWAY[i]}"
    done
}

write_env() {
    local tmp
    tmp=$(mktemp "$PROJECT_DIR/.tapo.env.XXXXXX")
    chmod 600 "$tmp"
    cat >"$tmp" <<EOF
# Generated by install.sh — keep this file at mode 600 (it contains camera passwords).
# See tapo.env.example for the description of each setting.

PHONES=$PHONES

$(camera_block)

INTERVAL=$INTERVAL
AWAY_AFTER_MISSES=$AWAY_AFTER_MISSES

ADAPTIVE_DELAY=$ADAPTIVE_DELAY
AWAY_MIN=$(keep AWAY_MIN 60)
AWAY_MAX=$(keep AWAY_MAX 1200)
ADAPTIVE_MARGIN=$(keep ADAPTIVE_MARGIN 1.2)
ADAPTIVE_EXTRA_CHECKS=$(keep ADAPTIVE_EXTRA_CHECKS 2)
ADAPTIVE_MIN_SAMPLES=$(keep ADAPTIVE_MIN_SAMPLES 5)
EOF
    local state_file
    state_file=$(current STATE_FILE)
    [[ -n $state_file ]] && echo "STATE_FILE=$state_file" >>"$tmp"
    if [[ -f $ENV_FILE ]]; then
        cp -p "$ENV_FILE" "$ENV_FILE.bak"
        chmod 600 "$ENV_FILE.bak"
        info "Previous config saved to tapo.env.bak"
    fi
    mv "$tmp" "$ENV_FILE"
    info "Wrote $ENV_FILE (mode 600)"
}

write_unit() {
    local uv=$1
    mkdir -p "$UNIT_DIR"
    cat >"$UNIT_FILE" <<EOF
# Generated by $PROJECT_DIR/install.sh
[Unit]
Description=Move Tapo C210 cameras depending on whether phones are on the Wi-Fi
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$ENV_FILE
# uv syncs .venv from uv.lock (--frozen: never update the lockfile) then runs the script
ExecStart=$uv run --frozen tapo_watcher.py
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
EOF
    info "Wrote $UNIT_FILE"
}

# ---------------------------------------------------------------- commands

do_install() {
    bold "tapo-watcher — install"
    echo "Project: $PROJECT_DIR"
    echo

    command -v systemctl >/dev/null || die "systemctl not found (systemd is required)."
    command -v ping >/dev/null || warn "ping not found: detection will not work."
    command -v ip >/dev/null || warn "ip (iproute2) not found: detection will not work."

    local uv
    uv=$(find_uv)
    [[ -n $uv ]] || die "uv not found. Install it: https://docs.astral.sh/uv/"
    info "Using uv: $uv"

    if [[ -f $ENV_FILE ]] && confirm "tapo.env already exists. Keep it as is (skip the questions)?" y; then
        :
    else
        echo
        prompt_config
        echo
        bold "Summary"
        camera_block | sed -E "s/^(.*PASSWORD=).*/\\1********/; /^$/d; s/^/  /"
        echo "  PHONES=$PHONES"
        echo "  INTERVAL=$INTERVAL  AWAY_AFTER_MISSES=$AWAY_AFTER_MISSES  ADAPTIVE_DELAY=$ADAPTIVE_DELAY"
        echo
        confirm "Write this configuration?" y || die "Aborted, nothing changed."
        write_env
    fi
    chmod 600 "$ENV_FILE"

    info "Installing dependencies (uv sync --frozen)…"
    (cd "$PROJECT_DIR" && "$uv" sync --frozen)

    echo
    info "Detection test (no camera access):"
    (cd "$PROJECT_DIR" && "$uv" run --frozen tapo_watcher.py --check) ||
        warn "Detection test failed or no phone found right now."

    if confirm "Test the camera connection by listing the presets (read-only)?" y; then
        (cd "$PROJECT_DIR" && "$uv" run --frozen tapo_watcher.py --presets) ||
            warn "Could not reach a camera: check its IP, username and password."
    fi

    echo
    write_unit "$uv"
    systemctl --user daemon-reload
    if confirm "Enable and start the service now? (the camera will move to the current state)" y; then
        systemctl --user enable --now "$SERVICE"
        systemctl --user restart "$SERVICE"
        info "Service started."
    else
        systemctl --user enable "$SERVICE"
        info "Service enabled, start it with: systemctl --user start $SERVICE"
    fi

    if [[ $(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true) != yes ]]; then
        echo
        echo "Without 'linger', the service stops when you log out."
        if confirm "Keep it running when logged out (sudo loginctl enable-linger $USER)?" y; then
            sudo loginctl enable-linger "$USER" && info "Linger enabled."
        fi
    fi

    echo
    bold "Done."
    echo "  Logs:    journalctl --user -u $SERVICE -f"
    echo "  Status:  systemctl --user status $SERVICE"
    echo "  Stats:   $uv run tapo_watcher.py --stats"
}

do_uninstall() {
    bold "tapo-watcher — uninstall"
    if [[ -f $UNIT_FILE ]]; then
        systemctl --user disable --now "$SERVICE" 2>/dev/null || true
        rm -f "$UNIT_FILE"
        systemctl --user daemon-reload
        systemctl --user reset-failed "$SERVICE" 2>/dev/null || true
        info "Service stopped and removed."
    else
        info "Service not installed."
    fi

    if [[ -f $ENV_FILE ]] && confirm "Delete tapo.env (camera credentials and phones)?" n; then
        rm -f "$ENV_FILE" "$ENV_FILE.bak"
        info "tapo.env deleted."
    fi

    local state
    state=$(current STATE_FILE)
    state=${state:-$STATE_FILE}
    if [[ -f $state ]] && confirm "Delete the learned away delays ($(basename "$state"))?" n; then
        rm -f "$state"
        info "Learned state deleted."
    fi

    if [[ -d $PROJECT_DIR/.venv ]] && confirm "Delete the virtualenv (.venv)?" n; then
        rm -rf "$PROJECT_DIR/.venv"
        info ".venv deleted."
    fi

    if [[ $(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true) == yes ]]; then
        echo "Linger is still enabled for $USER (other user services may rely on it)."
        if confirm "Disable it (sudo loginctl disable-linger $USER)?" n; then
            sudo loginctl disable-linger "$USER" && info "Linger disabled."
        fi
    fi
    bold "Done."
}

[[ $EUID -ne 0 ]] || die "Run this script as your normal user, not root (it installs a systemd user service)."

case ${1-} in
    install) do_install ;;
    uninstall | remove) do_uninstall ;;
    "")
        echo "1) Install / reconfigure"
        echo "2) Uninstall"
        read -r -p "Choice [1]: " choice
        case ${choice:-1} in
            1) do_install ;;
            2) do_uninstall ;;
            *) die "Invalid choice." ;;
        esac
        ;;
    -h | --help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) die "Usage: $0 [install|uninstall]" ;;
esac
