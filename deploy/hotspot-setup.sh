#!/bin/bash
# WOL-Server - deploy/hotspot-setup.sh
# Sets up Wi-Fi management with the setup hotspot and the root services.
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
# Sets up Wi-Fi management with the setup hotspot (called by install.sh).
#
#   sudo ./deploy/hotspot-setup.sh [--country DE]
#
# If the Pi finds no known Wi-Fi, it opens the hotspot "WOL-Setup-XXXX". Connect with a phone,
# the setup page opens (otherwise http://10.42.0.1/), choose the Wi-Fi there.
# Requires NetworkManager (default since Raspberry Pi OS Bookworm).
set -euo pipefail

COUNTRY=DE
while [ $# -gt 0 ]; do
    case "$1" in
        --country) COUNTRY="$2"; shift 2 ;;
        -h|--help) sed -n '17,23p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo." >&2; exit 1; }
[[ "$COUNTRY" =~ ^[A-Z]{2}$ ]] || { echo "--country: country code such as DE, AT, CH" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

if ! command -v nmcli >/dev/null || ! nmcli -t -f RUNNING general 2>/dev/null | grep -q running; then
    echo "NetworkManager is not running - setup hotspot will not be set up." >&2
    echo "Raspberry Pi OS uses NetworkManager by default since Bookworm." >&2
    exit 1
fi
[ -d /opt/wol-server/wolserver ] || { echo "Please run install.sh first." >&2; exit 1; }

say "Packages for hotspot operation"
missing=()
dpkg -s dnsmasq-base >/dev/null 2>&1 || missing+=(dnsmasq-base)   # DHCP/DNS in the hotspot
command -v iw >/dev/null || missing+=(iw)
if ! command -v nft >/dev/null && ! command -v iptables >/dev/null; then
    missing+=(nftables)   # NetworkManager uses it to set up the hotspot network
fi
if [ ${#missing[@]} -gt 0 ]; then
    apt-get update -q
    apt-get install -y --no-install-recommends "${missing[@]}"
fi

say "Setting Wi-Fi country $COUNTRY (otherwise Wi-Fi stays blocked on fresh systems)"
if command -v raspi-config >/dev/null; then
    raspi-config nonint do_wifi_country "$COUNTRY" || true
else
    iw reg set "$COUNTRY" 2>/dev/null || true
fi
if command -v rfkill >/dev/null; then rfkill unblock wifi 2>/dev/null || true; fi
nmcli radio wifi on || true

say "Captive portal: resolve all names to the Pi in the hotspot"
install -d /etc/NetworkManager/dnsmasq-shared.d
cat > /etc/NetworkManager/dnsmasq-shared.d/60-wol-server-captive.conf <<'EOF'
# Only active while the setup hotspot is running (NetworkManager "shared" mode).
# Phones then open the setup page by themselves.
address=/#/10.42.0.1
EOF

say "Providing DHCP/DNS (dnsmasq) - it is switched on in the web GUI under Network"
# The dnsmasq-base package creates the user dnsmasq; fall back to nobody just in case
DNS_USER=dnsmasq
id -u "$DNS_USER" >/dev/null 2>&1 || DNS_USER=nobody
install -d -m 0755 /etc/wol-dns
install -d -m 0755 -o "$DNS_USER" /var/lib/wol-dns
install -d -m 0700 /var/lib/wol-wifi /var/lib/wol-wifi/vpn /var/lib/wol-wifi/dns /var/lib/wol-wifi/dns/requests
install -m 0644 "$SRC/deploy/wol-dns.service" "$SRC/deploy/wol-dns-apply.path" "$SRC/deploy/wol-dns-apply.service" \
    /etc/systemd/system/
# Vendor names for MAC addresses (optional, needs Internet)
if [ ! -f /usr/share/ieee-data/oui.csv ]; then
    timeout 300 apt-get install -y -q --no-install-recommends ieee-data >/dev/null 2>&1 \
        || echo "    ieee-data not installed - vendors are not shown without the list"
fi

say "Service wol-wifi (Wi-Fi, VPN and network from the web GUI)"
install -m 0644 "$SRC/deploy/wol-wifi.service" /etc/systemd/system/wol-wifi.service
install -m 0644 "$SRC/deploy/wol-vpn.path" "$SRC/deploy/wol-vpn.service" /etc/systemd/system/
# One enable for all: reloads once (see install.sh)
systemctl enable wol-wifi.service wol-vpn.path wol-dns-apply.path >/dev/null
systemctl restart wol-wifi.service
systemctl start wol-vpn.path wol-dns-apply.path
sleep 2
if systemctl is-active --quiet wol-wifi.service; then
    say "Wi-Fi management is running"
else
    echo "wol-wifi could not be started - see: journalctl -u wol-wifi -e" >&2
    exit 1
fi
