#!/bin/bash
# WOL-Server - deploy/uninstall.sh
# Removes WOL-Server, optionally including data and configuration.
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
# Removes WOL-Server. With --purge also data (devices, schedules, SSH keys) and configuration.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo." >&2; exit 1; }

systemctl disable --now wol-server.service wol-bootlog.timer wol-cert.timer 2>/dev/null || true
rm -f /etc/systemd/system/wol-server.service /etc/systemd/system/wol-bootlog.service /etc/systemd/system/wol-bootlog.timer \
      /etc/systemd/system/wol-cert.service /etc/systemd/system/wol-cert.timer /usr/local/bin/wol-server /etc/sysctl.d/60-wol-server.conf
systemctl disable --now wol-wlan-watchdog.timer 2>/dev/null || true
rm -f /etc/systemd/system/wol-wlan-watchdog.service /etc/systemd/system/wol-wlan-watchdog.timer \
      /usr/local/sbin/wol-wlan-watchdog /etc/default/wol-wlan-watchdog
if systemctl is-active --quiet wol-dns.service; then
    echo "Note: WOL-Server DHCP/DNS is being stopped now - switch the old DHCP server back on first!" >&2
fi
systemctl disable --now wol-wifi.service wol-vpn.path wol-dns-apply.path wol-dns.service 2>/dev/null || true
rm -f /etc/systemd/system/wol-wifi.service /etc/systemd/system/wol-vpn.path /etc/systemd/system/wol-vpn.service \
      /etc/systemd/system/wol-dns.service /etc/systemd/system/wol-dns-apply.path /etc/systemd/system/wol-dns-apply.service /etc/NetworkManager/dnsmasq-shared.d/60-wol-server-captive.conf
nmcli connection delete wol-hotspot >/dev/null 2>&1 || true
# Saved Wi-Fi networks, Wi-Fi powersave off, hardware watchdog and OpenVPN client are kept on purpose:
# they are useful for a headless Pi on Wi-Fi even without WOL-Server.
systemctl daemon-reload
rm -rf /opt/wol-server

if [ "${1:-}" = "--purge" ]; then
    rm -rf /var/lib/wol-server /var/lib/wol-wifi /var/lib/wol-dns /etc/wol-server /etc/wol-dns
    userdel wol 2>/dev/null || true
    echo "WOL-Server and its data removed."
else
    echo "WOL-Server removed. Data in /var/lib/wol-server and /etc/wol-server is kept (--purge deletes it)."
fi
