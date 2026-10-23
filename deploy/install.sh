#!/bin/bash
# Installiert bzw. aktualisiert den WOL-Server auf Raspberry Pi OS (Lite).
# Aufruf aus dem Projektverzeichnis:
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
        -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
        *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR=/opt/wol-server
DATA_DIR=/var/lib/wol-server
CONF_DIR=/etc/wol-server
UNIT=/etc/systemd/system/wol-server.service

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

say "Prüfe Abhängigkeiten"
missing=()
command -v python3 >/dev/null || missing+=(python3)
command -v ssh >/dev/null || missing+=(openssh-client)
if [ ${#missing[@]} -gt 0 ]; then
    apt-get update -q
    apt-get install -y --no-install-recommends "${missing[@]}"
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
    || { echo "Python >= 3.9 benötigt" >&2; exit 1; }

say "Systembenutzer 'wol'"
if ! id wol >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin wol
fi
install -d -o wol -g wol -m 0750 "$DATA_DIR"

say "Programm nach $APP_DIR kopieren"
install -d -m 0755 "$APP_DIR"
rm -rf "$APP_DIR/wolserver"
cp -r "$SRC/wolserver" "$APP_DIR/"
cp "$SRC/README.md" "$APP_DIR/" 2>/dev/null || true
find "$APP_DIR" -name '__pycache__' -prune -exec rm -rf {} +
python3 -m compileall -q "$APP_DIR/wolserver"   # vorkompilieren: schnellerer Start auf dem Zero
chown -R root:root "$APP_DIR"

say "Konfiguration"
install -d -m 0755 "$CONF_DIR"
if [ ! -f "$CONF_DIR/config.json" ]; then
    sed "s/\"port\": 80/\"port\": $PORT/" "$SRC/deploy/config.json" > "$CONF_DIR/config.json"
    chmod 0644 "$CONF_DIR/config.json"
else
    echo "    $CONF_DIR/config.json existiert bereits – unverändert gelassen"
fi

say "Unprivilegiertes ICMP erlauben (für die Online-Anzeige)"
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

say "SSH-Schlüssel für das Schlafenlegen"
if [ ! -f "$DATA_DIR/.ssh/id_ed25519" ]; then
    install -d -o wol -g wol -m 0700 "$DATA_DIR/.ssh"
    runuser -u wol -- ssh-keygen -q -t ed25519 -N "" -C "wol-server@$(hostname)" -f "$DATA_DIR/.ssh/id_ed25519"
fi

if [ "$ASK_PASSWORD" -eq 1 ] && [ -t 0 ] && ! grep -q '"password_hash"' "$DATA_DIR/auth.json" 2>/dev/null; then
    say "Passwort für die Web-GUI festlegen (leer lassen = später in der GUI)"
    read -r -s -p "Passwort: " pw1; echo
    if [ -n "$pw1" ]; then
        read -r -s -p "Wiederholen: " pw2; echo
        if [ "$pw1" = "$pw2" ]; then
            printf '%s\n' "$pw1" | /usr/local/bin/wol-server passwd --stdin
        else
            echo "    Passwörter stimmen nicht überein – übersprungen (später: sudo wol-server passwd)"
        fi
    fi
    unset pw1 pw2
fi

say "systemd-Dienst"
install -m 0644 "$SRC/deploy/wol-server.service" "$UNIT"
systemctl daemon-reload
systemctl enable wol-server.service >/dev/null
systemctl restart wol-server.service
sleep 2
if systemctl is-active --quiet wol-server.service; then
    say "WOL-Server läuft"
else
    echo "Dienst konnte nicht gestartet werden – siehe: journalctl -u wol-server -e" >&2
    exit 1
fi

if [ "$WLAN_SETUP" -eq 1 ] && [ -d /sys/class/net/wlan0 ]; then
    "$SRC/deploy/wlan-setup.sh" --iface wlan0
fi

if [ "$HOTSPOT" -eq 1 ] && [ -d /sys/class/net/wlan0 ]; then
    "$SRC/deploy/hotspot-setup.sh" --country "$COUNTRY" \
        || echo "Einrichtungs-Hotspot nicht aktiv – später erneut: sudo ./deploy/hotspot-setup.sh" >&2
fi

if [ -n "$OVPN" ]; then
    "$SRC/deploy/openvpn-setup.sh" "$OVPN" \
        || echo "OpenVPN noch nicht verbunden – später erneut: sudo ./deploy/openvpn-setup.sh <client.ovpn>" >&2
fi

suffix=""
[ "$PORT" != "80" ] && suffix=":$PORT"
echo
echo "  Web-GUI:  http://$(hostname).local$suffix/"
for ip in $(hostname -I); do
    case "$ip" in *:*) ;; *) echo "            http://$ip$suffix/" ;; esac
done
echo "  Logs:     journalctl -u wol-server -f"
echo "  Stromsparen (optional, einmalig): sudo ./deploy/lowpower.sh"
[ -z "$OVPN" ] && echo "  OpenVPN-Client:  sudo ./deploy/openvpn-setup.sh <client.ovpn>"
exit 0
