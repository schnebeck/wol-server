#!/bin/bash
# WOL-Server - deploy/install.sh
# Installs or updates WOL-Server on Raspberry Pi OS.
#
#   Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
#   Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
#   Written by Anthropic Claude Opus 5.5 - AI generated content.
#
#   Free software under the GNU General Public License, version 3 or later.
#   There is no warranty, to the extent permitted by law. The full text is in
#   LICENSES/GPL-3.0-or-later.txt.
#
# SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
# SPDX-FileContributor: Anthropic Claude Opus 5.5 (AI generated content)
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Installs or updates WOL-Server on Raspberry Pi OS (Lite).
# Run from the project directory:
#   sudo ./deploy/install.sh [--port 80] [--no-password] [--openvpn client.ovpn] [--country DE]
#                            [--no-wlan-setup] [--no-hotspot]
set -euo pipefail

PORT=80
ASK_PASSWORD=1
OVPN=""
WLAN_SETUP=1
HOTSPOT=1
COUNTRY=DE
while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --no-password) ASK_PASSWORD=0; shift ;;
        --openvpn) OVPN="$(realpath "$2")"; shift 2 ;;
        --no-wlan-setup) WLAN_SETUP=0; shift ;;
        --no-hotspot) HOTSPOT=0; shift ;;
        --country) COUNTRY="$2"; shift 2 ;;
        -h|--help) sed -n '17,20p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo." >&2; exit 1; }

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR=/opt/wol-server
DATA_DIR=/var/lib/wol-server
CONF_DIR=/etc/wol-server
UNIT=/etc/systemd/system/wol-server.service

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

say "Checking dependencies"
missing=()
command -v python3 >/dev/null || missing+=(python3)
command -v ssh >/dev/null || missing+=(openssh-client)
if [ ${#missing[@]} -gt 0 ]; then
    apt-get update -q
    apt-get install -y --no-install-recommends "${missing[@]}"
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
    || { echo "Python >= 3.9 required" >&2; exit 1; }

say "System user 'wol'"
if ! id wol >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin wol
fi
install -d -o wol -g wol -m 0750 "$DATA_DIR"

say "Copying program to $APP_DIR"
install -d -m 0755 "$APP_DIR"
if [ "$SRC" != "$APP_DIR" ]; then
    rm -rf "$APP_DIR/wolserver" "$APP_DIR/deploy"
    cp -r "$SRC/wolserver" "$SRC/deploy" "$APP_DIR/"   # deploy/: scripts for later changes
    cp "$SRC/README.md" "$APP_DIR/" 2>/dev/null || true
fi
find "$APP_DIR" -name '__pycache__' -prune -exec rm -rf {} +
python3 -m compileall -q "$APP_DIR/wolserver"   # precompile: faster startup on the Zero
chown -R root:root "$APP_DIR"

say "Configuration"
install -d -m 0755 "$CONF_DIR"
if [ ! -s "$CONF_DIR/config.json" ]; then   # -s: also replace a file left empty by a crash
    sed "s/\"port\": 80/\"port\": $PORT/" "$SRC/deploy/config.json" > "$CONF_DIR/config.json"
    chmod 0644 "$CONF_DIR/config.json"
else
    echo "    $CONF_DIR/config.json already exists - left unchanged"
fi

say "Allowing unprivileged ICMP (for the online indicator)"
echo 'net.ipv4.ping_group_range = 0 2147483647' > /etc/sysctl.d/60-wol-server.conf
sysctl -q -p /etc/sysctl.d/60-wol-server.conf || true

say "CLI-Wrapper /usr/local/bin/wol-server"
cat > /usr/local/bin/wol-server <<'EOF'
#!/bin/sh
# wol-server passwd | wol-server wake AA:BB:CC:DD:EE:FF
cd /opt/wol-server || exit 1
if [ "$(id -u)" -eq 0 ]; then
    exec runuser -u wol -- python3 -m wolserver --config /etc/wol-server/config.json "$@"
fi
exec python3 -m wolserver --config /etc/wol-server/config.json "$@"
EOF
chmod 0755 /usr/local/bin/wol-server

say "HTTPS: directory for the certificate"
# wol-cert.timer fetches cert.pem/key.pem via SSH from the web server (cert_source in config.json)
install -d -o root -g wol -m 0750 "$CONF_DIR/tls"
if [ ! -f "$CONF_DIR/tls/fetch_key" ]; then
    ssh-keygen -q -t ed25519 -N "" -C "wol-cert@$(hostname)" -f "$CONF_DIR/tls/fetch_key"
fi

say "SSH key for putting devices to sleep"
if [ ! -f "$DATA_DIR/.ssh/id_ed25519" ]; then
    install -d -o wol -g wol -m 0700 "$DATA_DIR/.ssh"
    runuser -u wol -- ssh-keygen -q -t ed25519 -N "" -C "wol-server@$(hostname)" -f "$DATA_DIR/.ssh/id_ed25519"
fi

if [ "$ASK_PASSWORD" -eq 1 ] && [ -t 0 ] && ! grep -q '"password_hash"' "$DATA_DIR/auth.json" 2>/dev/null; then
    say "Set the web GUI password (leave empty = set it later in the GUI)"
    read -r -s -p "Password: " pw1; echo
    if [ -n "$pw1" ]; then
        read -r -s -p "Repeat: " pw2; echo
        if [ "$pw1" = "$pw2" ]; then
            printf '%s\n' "$pw1" | /usr/local/bin/wol-server passwd --stdin
        else
            echo "    Passwords do not match - skipped (later: sudo wol-server passwd)"
        fi
    fi
    unset pw1 pw2
fi

say "systemd service"
install -m 0644 "$SRC/deploy/wol-server.service" "$UNIT"
# Boot report on the boot partition (wol-boot.log), readable with any card reader
install -m 0644 "$SRC/deploy/wol-bootlog.service" "$SRC/deploy/wol-bootlog.timer" /etc/systemd/system/
# Fetch the HTTPS certificate daily (waits until tls/known_hosts is set up)
install -m 0644 "$SRC/deploy/wol-cert.service" "$SRC/deploy/wol-cert.timer" /etc/systemd/system/
# enable reloads the unit files by itself. No extra daemon-reload: each one takes 12-20 s on the
# Zero, during which systemd blocks (and does not serve the hardware watchdog).
systemctl enable wol-server.service wol-bootlog.timer wol-cert.timer >/dev/null
systemctl restart wol-server.service
sleep 2
if systemctl is-active --quiet wol-server.service; then
    say "WOL-Server is running"
else
    echo "Service could not be started - see: journalctl -u wol-server -e" >&2
    exit 1
fi

if [ "$WLAN_SETUP" -eq 1 ] && [ -d /sys/class/net/wlan0 ]; then
    "$SRC/deploy/wlan-setup.sh" --iface wlan0
fi

if [ "$HOTSPOT" -eq 1 ] && [ -d /sys/class/net/wlan0 ]; then
    "$SRC/deploy/hotspot-setup.sh" --country "$COUNTRY" \
        || echo "Setup hotspot not active - retry later: sudo ./deploy/hotspot-setup.sh" >&2
fi

if [ -n "$OVPN" ]; then
    "$SRC/deploy/openvpn-setup.sh" "$OVPN" \
        || echo "OpenVPN not connected yet - retry later: sudo ./deploy/openvpn-setup.sh <client.ovpn>" >&2
fi

suffix=""
[ "$PORT" != "80" ] && suffix=":$PORT"
echo
echo "  Web GUI:  http://$(hostname).local$suffix/"
for ip in $(hostname -I); do
    case "$ip" in *:*) ;; *) echo "            http://$ip$suffix/" ;; esac
done
echo "  Logs:     journalctl -u wol-server -f"
echo "  Save memory and power (optional, once): sudo ./deploy/lowmem.sh && sudo ./deploy/lowpower.sh"
[ -z "$OVPN" ] && echo "  OpenVPN client:  sudo ./deploy/openvpn-setup.sh <client.ovpn>"
exit 0
