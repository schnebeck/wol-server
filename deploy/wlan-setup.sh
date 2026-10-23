#!/bin/bash
# Macht einen reinen WLAN-Betrieb des Pi Zero W robust (wird von install.sh aufgerufen).
#
#   sudo ./deploy/wlan-setup.sh [--iface wlan0] [--reboot-after 30] [--no-hw-watchdog]
#
# - WLAN-Powersave aus: Mit Powersave verpasst der Zero eingehende Verbindungen
#   und Broadcasts. Das kostet nur wenige mA.
# - WLAN-Watchdog: Er prüft jede Minute das Gateway. Nach 3 Fehlschlägen wird das WLAN
#   neu verbunden, nach --reboot-after Minuten ohne Netz startet der Pi neu (0 = nie).
# - Hardware-Watchdog: Er startet den Pi neu, falls das System komplett hängt.
set -euo pipefail

IFACE=wlan0
REBOOT_AFTER=30
HW_WATCHDOG=1
while [ $# -gt 0 ]; do
    case "$1" in
        --iface) IFACE="$2"; shift 2 ;;
        --reboot-after) REBOOT_AFTER="$2"; shift 2 ;;
        --no-hw-watchdog) HW_WATCHDOG=0; shift ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
    esac
done
[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }
[[ "$IFACE" =~ ^[a-z0-9]+$ ]] || { echo "Ungültiges Interface: $IFACE" >&2; exit 1; }
[[ "$REBOOT_AFTER" =~ ^[0-9]+$ ]] || { echo "--reboot-after: Minuten als Zahl" >&2; exit 1; }
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- Powersave
say "WLAN-Powersave auf $IFACE abschalten"
if [ -d /etc/NetworkManager/conf.d ]; then
    cat > /etc/NetworkManager/conf.d/60-wol-server-wifi-powersave.conf <<'EOF'
[connection]
wifi.powersave = 2
EOF
fi
cat > /etc/systemd/system/wifi-powersave-off@.service <<'EOF'
[Unit]
Description=WLAN-Powersave auf %i abschalten
After=sys-subsystem-net-devices-%i.device network-online.target
BindsTo=sys-subsystem-net-devices-%i.device

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'command -v iw >/dev/null && iw dev %i set power_save off || true'

[Install]
WantedBy=sys-subsystem-net-devices-%i.device
EOF
# Ältere Einzel-Unit aus früheren Versionen von lowpower.sh entfernen
if [ -f /etc/systemd/system/wifi-powersave-off.service ]; then
    systemctl disable wifi-powersave-off.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/wifi-powersave-off.service
fi
systemctl daemon-reload
systemctl enable "wifi-powersave-off@$IFACE.service" >/dev/null
if command -v iw >/dev/null; then
    iw dev "$IFACE" set power_save off 2>/dev/null || true
fi

# ---------------------------------------------------------------- Watchdog
say "WLAN-Watchdog (Reconnect nach 3 min, Neustart nach $REBOOT_AFTER min ohne Netz)"
cat > /etc/default/wol-wlan-watchdog <<EOF
# Konfiguration des WLAN-Watchdogs (wol-server)
IFACE=$IFACE
# Optional fester Prüf-Host statt Gateway, z. B. CHECK_HOST=192.168.178.1
CHECK_HOST=
# Fehlversuche in Folge (1 pro Minute), nach denen das WLAN neu verbunden wird
RECONNECT_AFTER=3
# Minuten ohne Netz bis zum Neustart; 0 = nie. Neustart frühestens 60 min nach dem Boot.
REBOOT_AFTER=$REBOOT_AFTER
EOF

cat > /usr/local/sbin/wol-wlan-watchdog <<'EOF'
#!/bin/sh
# Prüft die WLAN-Verbindung; aufgerufen jede Minute von wol-wlan-watchdog.timer.
IFACE=wlan0; CHECK_HOST=; RECONNECT_AFTER=3; REBOOT_AFTER=30
[ -r /etc/default/wol-wlan-watchdog ] && . /etc/default/wol-wlan-watchdog
STATE=/run/wol-wlan-watchdog.fails

# Einrichtungs-Hotspot aktiv (Dienst wol-wifi): Es gibt kein Gateway, und ein Reconnect oder
# Neustart würde die Einrichtung unterbrechen. Der Hotspot ersetzt hier den Neustart.
if ip -4 addr show dev "$IFACE" 2>/dev/null | grep -q "inet 10\.42\.0\.1/"; then
    rm -f "$STATE"
    exit 0
fi

target=$CHECK_HOST
[ -n "$target" ] || target=$(ip -4 route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')

if [ -n "$target" ] && ping -n -q -c 2 -W 3 -I "$IFACE" "$target" >/dev/null 2>&1; then
    if [ -f "$STATE" ]; then
        logger -t wlan-watchdog "Verbindung wieder da ($target)"
        rm -f "$STATE"
    fi
    exit 0
fi

fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"
logger -t wlan-watchdog "Keine Verbindung über $IFACE zu ${target:-<kein Gateway>} ($fails. Versuch)"

if [ $((fails % RECONNECT_AFTER)) -eq 0 ]; then
    logger -t wlan-watchdog "Verbinde $IFACE neu"
    if command -v nmcli >/dev/null && nmcli -t -f RUNNING general 2>/dev/null | grep -q running; then
        nmcli radio wifi off; sleep 3; nmcli radio wifi on
    else
        ip link set "$IFACE" down; sleep 3; ip link set "$IFACE" up
    fi
fi

uptime_s=$(cut -d. -f1 /proc/uptime)
if [ "$REBOOT_AFTER" -gt 0 ] && [ "$fails" -ge "$REBOOT_AFTER" ] && [ "$uptime_s" -ge 3600 ]; then
    logger -t wlan-watchdog "Seit $fails min kein Netz – starte neu"
    systemctl reboot
fi
EOF
chmod 0755 /usr/local/sbin/wol-wlan-watchdog

cat > /etc/systemd/system/wol-wlan-watchdog.service <<'EOF'
[Unit]
Description=WLAN-Verbindung prüfen und ggf. wiederherstellen
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/wol-wlan-watchdog
Nice=10
EOF
cat > /etc/systemd/system/wol-wlan-watchdog.timer <<'EOF'
[Unit]
Description=WLAN-Watchdog jede Minute

[Timer]
OnBootSec=3min
OnUnitActiveSec=1min
AccuracySec=15s

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now wol-wlan-watchdog.timer >/dev/null

# ---------------------------------------------------------------- Hardware-Watchdog
if [ "$HW_WATCHDOG" -eq 1 ]; then
    say "Hardware-Watchdog aktivieren (Neustart bei Systemhänger)"
    install -d /etc/systemd/system.conf.d
    cat > /etc/systemd/system.conf.d/60-wol-server-watchdog.conf <<'EOF'
[Manager]
# BCM2835-Watchdog: maximal ca. 15 s
RuntimeWatchdogSec=14s
RebootWatchdogSec=2min
EOF
    if [ -n "${WOL_FIRSTBOOT:-}" ]; then
        echo "    wird beim nächsten Start aktiv"   # kein daemon-reexec mitten im ersten Boot
    else
        systemctl daemon-reexec
    fi
fi

say "WLAN-Absicherung eingerichtet"
