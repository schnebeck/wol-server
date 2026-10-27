#!/bin/bash
# WOL-Server - deploy/openvpn-setup.sh
# Sets up the Pi as an OpenVPN client, reachable over the VPN.
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
# Sets up the Pi as an OpenVPN client so the WOL GUI is reachable via the VPN.
#
#   sudo ./deploy/openvpn-setup.sh <client.ovpn> [--name wol] [--allow-redirect-gateway]
#                                  [--auth credentials.txt] [--no-wait]
#
# - installs openvpn, stores the configuration as /etc/openvpn/client/<name>.conf
# - asks for user/password if the configuration uses "auth-user-pass" without a file
# - ignores "redirect-gateway" by default: the Pi should only be *reachable* via the VPN,
#   not send all its traffic through the tunnel (saves CPU on the Zero)
# - reconnects automatically after Wi-Fi or server outages
# --auth: credentials from a file (line 1: user, line 2: password) instead of a prompt
# --no-wait: do not wait for the tunnel (for setup from the web GUI)
set -euo pipefail

NAME=wol
ALLOW_REDIRECT=0
OVPN=""
AUTH_FILE=""
WAIT=1
while [ $# -gt 0 ]; do
    case "$1" in
        --name) NAME="$2"; shift 2 ;;
        --allow-redirect-gateway) ALLOW_REDIRECT=1; shift ;;
        --auth) AUTH_FILE="$2"; shift 2 ;;
        --no-wait) WAIT=0; shift ;;
        -h|--help) sed -n '17,29p' "$0"; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; exit 1 ;;
        *) OVPN="$1"; shift ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo." >&2; exit 1; }
if [ -z "$OVPN" ] || [ ! -f "$OVPN" ]; then
    echo "Usage: sudo $0 <client.ovpn> [--name wol]" >&2; exit 1
fi
[[ "$NAME" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "Invalid name: $NAME" >&2; exit 1; }
[ -z "$AUTH_FILE" ] || [ -f "$AUTH_FILE" ] || { echo "--auth: file not found: $AUTH_FILE" >&2; exit 1; }

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
DIR=/etc/openvpn/client
CONF="$DIR/$NAME.conf"
AUTH="$DIR/$NAME.auth"
UNIT="openvpn-client@$NAME.service"

if ! command -v openvpn >/dev/null; then
    say "Installing openvpn"
    apt-get update -q
    apt-get install -y --no-install-recommends openvpn
fi

say "Configuration to $CONF"
install -d -m 0755 "$DIR"
# Strip Windows line endings, drop the previous wol-server block
tr -d '\r' < "$OVPN" | sed '/^# >>> wol-server/,/^# <<< wol-server/d' > "$CONF.new"

# auth-user-pass without a file would prompt interactively - impossible headless
if grep -Eq '^[[:space:]]*auth-user-pass[[:space:]]*$' "$CONF.new"; then
    if [ -n "$AUTH_FILE" ]; then
        (umask 077; tr -d '\r' < "$AUTH_FILE" > "$AUTH")
    elif [ -t 0 ]; then
        say "The VPN account needs a user name and password"
        read -r -p "    User name: " vpn_user
        read -r -s -p "    Password: " vpn_pass; echo
        umask 077
        printf '%s\n%s\n' "$vpn_user" "$vpn_pass" > "$AUTH"
        unset vpn_user vpn_pass
    elif [ ! -f "$AUTH" ]; then
        echo "auth-user-pass requires $AUTH (line 1: user, line 2: password)" >&2
        rm -f "$CONF.new"; exit 1
    fi
    sed -i -E "s|^[[:space:]]*auth-user-pass[[:space:]]*$|auth-user-pass $AUTH|" "$CONF.new"
    chmod 0600 "$AUTH"
fi

has_keepalive=0
grep -Eq '^[[:space:]]*(keepalive|ping-restart)[[:space:]]' "$CONF.new" && has_keepalive=1
{
    echo "# >>> wol-server"
    echo "# Reconnect indefinitely after connection losses (Wi-Fi, server)"
    echo "resolv-retry infinite"
    echo "connect-retry 5 120"
    echo "persist-key"
    if [ "$has_keepalive" -eq 0 ]; then
        echo "ping 15"
        echo "ping-restart 90"
    fi
    if [ "$ALLOW_REDIRECT" -eq 0 ]; then
        echo "# Only be reachable, do not route Internet traffic through the tunnel"
        echo "pull-filter ignore \"redirect-gateway\""
        echo "pull-filter ignore \"dhcp-option DNS\""
    fi
    echo "# <<< wol-server"
} >> "$CONF.new"
mv "$CONF.new" "$CONF"
chmod 0600 "$CONF"

# Referenced separate files (instead of inline <ca> etc.) must be next to the configuration
# shellcheck disable=SC2013  # file names without spaces
for ref in $(awk '$1 ~ /^(ca|cert|key|tls-auth|tls-crypt|tls-crypt-v2|pkcs12)$/ && NF >= 2 {print $2}' "$CONF"); do
    case "$ref" in /*) path="$ref" ;; *) path="$DIR/$ref" ;; esac
    [ -f "$path" ] && continue
    src="$(dirname "$OVPN")/$ref"
    if [ -f "$src" ]; then
        install -m 0600 "$src" "$path"
        echo "    $ref copied"
    else
        echo "WARNING: $ref is referenced but missing ($path)" >&2
    fi
done

say "Service $UNIT"
install -d "/etc/systemd/system/$UNIT.d"
cat > "/etc/systemd/system/$UNIT.d/60-wol-server.conf" <<'EOF'
[Unit]
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=20
Nice=5
EOF
systemctl enable "$UNIT" >/dev/null   # reloads by itself, including the drop-in
systemctl restart "$UNIT"
if [ "$WAIT" -eq 0 ]; then
    say "Service $UNIT started"
    exit 0
fi

say "Waiting for the tunnel (max. 45 s)"
vpn_ip=""
for _ in $(seq 45); do
    vpn_ip=$(ip -4 -o addr show 2>/dev/null | awk '$2 ~ /^(tun|tap)/ {split($4, a, "/"); print a[1]; exit}')
    [ -n "$vpn_ip" ] && break
    sleep 1
done

if [ -z "$vpn_ip" ]; then
    echo "No tunnel yet. Logs: journalctl -u $UNIT -e" >&2
    exit 1
fi

port=$(python3 -c 'import json;print(json.load(open("/etc/wol-server/config.json")).get("port",80))' 2>/dev/null || echo 80)
suffix=""; [ "$port" != "80" ] && suffix=":$port"
if ! (cd /opt/wol-server 2>/dev/null && python3 - "$vpn_ip" <<'PY'
import ipaddress, sys
from wolserver.config import load_config
from wolserver.server import AccessList
# "auto" allows the tunnel network anyway; fixed lists must contain it
sys.exit(0 if AccessList(load_config()["allowed_networks"]).allows(ipaddress.ip_address(sys.argv[1])) else 1)
PY
); then
    echo "WARNING: $vpn_ip is not in allowed_networks - add it in /etc/wol-server/config.json"
    echo "         (\"auto\" or the VPN subnet, e.g. \"10.8.0.0/24\") and: sudo systemctl restart wol-server"
fi
say "VPN connected: $vpn_ip"
echo "  WOL GUI via VPN: http://$vpn_ip$suffix/"
echo "  Tip: on the VPN server give the Pi a fixed address (client-config-dir / ifconfig-push)"
echo "       and allow client-to-client or routing so other VPN clients can reach it."
