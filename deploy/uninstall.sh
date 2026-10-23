#!/bin/bash
# Entfernt den WOL-Server. Mit --purge auch Daten (Geräte, Zeitpläne, SSH-Schlüssel) und Konfiguration.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }

systemctl disable --now wol-server.service 2>/dev/null || true
rm -f /etc/systemd/system/wol-server.service /usr/local/bin/wol-server /etc/sysctl.d/60-wol-server.conf
systemctl daemon-reload
rm -rf /opt/wol-server

if [ "${1:-}" = "--purge" ]; then
    rm -rf /var/lib/wol-server /etc/wol-server
    userdel wol 2>/dev/null || true
    echo "WOL-Server inkl. Daten entfernt."
else
    echo "WOL-Server entfernt. Daten in /var/lib/wol-server und /etc/wol-server bleiben erhalten (--purge löscht sie)."
fi
