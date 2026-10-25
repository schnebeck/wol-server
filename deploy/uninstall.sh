#!/bin/bash
# Entfernt den WOL-Server. Mit --purge auch Daten (Geräte, Zeitpläne, SSH-Schlüssel) und Konfiguration.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }

systemctl disable --now wol-server.service 2>/dev/null || true
rm -f /etc/systemd/system/wol-server.service /usr/local/bin/wol-server /etc/sysctl.d/60-wol-server.conf
systemctl disable --now wol-wlan-watchdog.timer 2>/dev/null || true
rm -f /etc/systemd/system/wol-wlan-watchdog.service /etc/systemd/system/wol-wlan-watchdog.timer \
      /usr/local/sbin/wol-wlan-watchdog /etc/default/wol-wlan-watchdog
if systemctl is-active --quiet wol-dns.service; then
    echo "Hinweis: DHCP/DNS des WOL-Servers wird jetzt beendet – vorher den alten DHCP-Server wieder einschalten!" >&2
fi
systemctl disable --now wol-wifi.service wol-vpn.path wol-dns-apply.path wol-dns.service 2>/dev/null || true
rm -f /etc/systemd/system/wol-wifi.service /etc/systemd/system/wol-vpn.path /etc/systemd/system/wol-vpn.service \
      /etc/systemd/system/wol-dns.service /etc/systemd/system/wol-dns-apply.path /etc/systemd/system/wol-dns-apply.service /etc/NetworkManager/dnsmasq-shared.d/60-wol-server-captive.conf
nmcli connection delete wol-hotspot >/dev/null 2>&1 || true
# Gespeicherte WLANs, WLAN-Powersave-aus, Hardware-Watchdog und OpenVPN-Client bleiben bewusst bestehen:
# Sie sind für einen Headless-Pi im WLAN auch ohne WOL-Server sinnvoll.
systemctl daemon-reload
rm -rf /opt/wol-server

if [ "${1:-}" = "--purge" ]; then
    rm -rf /var/lib/wol-server /var/lib/wol-wifi /var/lib/wol-dns /etc/wol-server /etc/wol-dns
    userdel wol 2>/dev/null || true
    echo "WOL-Server inkl. Daten entfernt."
else
    echo "WOL-Server entfernt. Daten in /var/lib/wol-server und /etc/wol-server bleiben erhalten (--purge löscht sie)."
fi
