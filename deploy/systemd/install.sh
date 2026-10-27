#!/usr/bin/env bash
# Installs a FastQ IOC host or control host as a systemd service, running from this repo checkout.
#
#   sudo deploy/systemd/install.sh ioc-host ioc_host/supervisor_config.yaml [options]
#   sudo deploy/systemd/install.sh control-host control/lab.yaml [options]
#   deploy/systemd/install.sh ioc-host ioc_host/supervisor_config.yaml --dry-run      (prints, changes nothing)
#
# options: --user NAME      run as this account (default: $SUDO_USER); it needs read access to the repo
#          --venv DIR       default <repo>/.venv (shared by both roles on one PC)
#          --python EXE     interpreter for a new venv (default python3; 3.10 - 3.14)
#          --open-firewall  ufw rules for Channel Access (ioc-host) or the control services (control-host)
#          --no-start       install and enable, but do not start now
#          --dry-run        print every command instead of running it
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROLE="${1:-}"; CONFIG="${2:-}"
[[ "$ROLE" == ioc-host || "$ROLE" == control-host ]] && [[ -n "$CONFIG" ]] || {
    sed -n '2,14p' "$0"; exit 2; }
shift 2
RUN_USER="${SUDO_USER:-$(id -un)}"; VENV="$REPO/.venv"; PYTHON=python3
FIREWALL=0; START=1; DRY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --user) RUN_USER="$2"; shift ;;
        --venv) VENV="$2"; shift ;;
        --python) PYTHON="$2"; shift ;;
        --open-firewall) FIREWALL=1 ;;
        --no-start) START=0 ;;
        --dry-run) DRY=1 ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
    shift
done

run() { if (( DRY )); then echo "+ $*"; else "$@"; fi; }
info() { echo "==> $*"; }

[[ "$CONFIG" = /* ]] || CONFIG="$REPO/$CONFIG"
[[ -f "$CONFIG" ]] || { echo "not found: $CONFIG" >&2; exit 1; }
NAME="$(basename "$CONFIG" .yaml)"
if (( ! DRY )) && [[ $EUID -ne 0 ]]; then echo "run with sudo (or add --dry-run)" >&2; exit 1; fi
id "$RUN_USER" >/dev/null 2>&1 || { echo "no such user: $RUN_USER (--user)" >&2; exit 1; }

# ---- Python + venv (created as the service user, so it owns its packages)
if [[ ! -x "$VENV/bin/python" ]]; then
    ver="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    "$PYTHON" -c 'import sys; sys.exit(not ((3, 10) <= sys.version_info[:2] <= (3, 14)))' \
        || { echo "Python $ver not supported (need 3.10 - 3.14; 3.12 for a Nanotec rotator host)" >&2; exit 1; }
    info "creating $VENV with $PYTHON ($ver)"
    run sudo -u "$RUN_USER" "$PYTHON" -m venv "$VENV" \
        || { echo "creating the venv failed - on Ubuntu: sudo apt install python3-venv" >&2; exit 1; }
fi
REQS=(-r "$REPO/requirements-ioc-host.txt")
[[ "$ROLE" == control-host ]] && REQS=(-r "$REPO/requirements-control.txt")
run sudo -u "$RUN_USER" "$VENV/bin/python" -m pip install --upgrade pip
run sudo -u "$RUN_USER" "$VENV/bin/python" -m pip install "${REQS[@]}"

# ---- unit
UNIT="fastq-$ROLE.service"
info "installing /etc/systemd/system/$UNIT (user $RUN_USER, config $CONFIG)"
rendered="$(sed -e "s|@REPO@|$REPO|g" -e "s|@VENV@|$VENV|g" -e "s|@USER@|$RUN_USER|g" \
                -e "s|@CONFIG@|$CONFIG|g" -e "s|@NAME@|$NAME|g" "$REPO/deploy/systemd/$UNIT")"
if (( DRY )); then
    echo "+ write /etc/systemd/system/$UNIT:"; echo "$rendered" | sed 's/^/    /'
else
    echo "$rendered" > "/etc/systemd/system/$UNIT"
fi
[[ -f /etc/fastq/fastq.env ]] || {
    run mkdir -p /etc/fastq
    if (( DRY )); then echo "+ write /etc/fastq/fastq.env (commented template)"; else cat > /etc/fastq/fastq.env <<'EOF'
# FastQ site settings for the systemd services (KEY=value, no quotes needed)
# Channel Access to IOC hosts in other subnets: their broadcast address or IP, space separated
#EPICS_CA_ADDR_LIST=192.168.1.255
#EPICS_CA_AUTO_ADDR_LIST=YES
#FASTQ_TILED_API_KEY=
#FASTQ_AI_USER=
#FASTQ_AI_PASSWORD=
#FASTQ_ELOG_PASSWORD=
EOF
    chmod 640 /etc/fastq/fastq.env; fi
}
run systemctl daemon-reload
if (( START )); then run systemctl enable --now "$UNIT"; else run systemctl enable "$UNIT"; fi

# ---- firewall (ufw, if present)
if (( FIREWALL )); then
    if command -v ufw >/dev/null 2>&1 || (( DRY )); then
        if [[ "$ROLE" == ioc-host ]]; then
            # CA: UDP 5064 search + 5065 beacons; TCP 5064 for the first IOC, then caproto's random 49152-65535
            run ufw allow 5064:5065/udp comment 'FastQ Channel Access'
            run ufw allow 5064/tcp comment 'FastQ Channel Access'
            run ufw allow 49152:65535/tcp comment 'FastQ Channel Access (IOC servers)'
        else
            run ufw allow 8088/tcp comment 'FastQ dashboards'
            run ufw allow 8000/tcp comment 'FastQ Tiled'
            run ufw allow 60615/tcp comment 'FastQ queue server'
            run ufw allow 60625/tcp comment 'FastQ queue server console'
        fi
    else
        echo "no ufw - open the ports with your firewall (see deploy/README.md)"
    fi
fi

info "done: systemctl status $UNIT ; journalctl -u $UNIT -f"
[[ "$ROLE" == ioc-host ]] && info "IOC logs: /var/log/fastq/$NAME/" || true
