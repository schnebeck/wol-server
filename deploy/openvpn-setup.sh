#!/bin/bash
# Richtet den Pi als OpenVPN-Client ein, damit die WOL-GUI über das VPN erreichbar ist.
#
#   sudo ./deploy/openvpn-setup.sh <client.ovpn> [--name wol] [--allow-redirect-gateway]
#                                  [--auth zugang.txt] [--no-wait]
#
# - installiert openvpn, legt die Konfiguration als /etc/openvpn/client/<name>.conf ab
# - fragt Benutzer/Passwort ab, falls die Konfiguration "auth-user-pass" ohne Datei nutzt
# - ignoriert standardmäßig "redirect-gateway": Der Pi soll nur über das VPN *erreichbar*
#   sein, nicht seinen ganzen Verkehr durch den Tunnel schicken (spart CPU auf dem Zero)
# - verbindet sich nach WLAN- oder Server-Ausfällen automatisch neu
# --auth: Zugangsdaten aus Datei (Zeile 1: Benutzer, Zeile 2: Passwort) statt Abfrage
# --no-wait: nicht auf den Tunnel warten (für die Einrichtung aus der Web-GUI)
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
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        -*) echo "Unbekannte Option: $1" >&2; exit 1 ;;
        *) OVPN="$1"; shift ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }
if [ -z "$OVPN" ] || [ ! -f "$OVPN" ]; then
    echo "Aufruf: sudo $0 <client.ovpn> [--name wol]" >&2; exit 1
fi
[[ "$NAME" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "Ungültiger Name: $NAME" >&2; exit 1; }
[ -z "$AUTH_FILE" ] || [ -f "$AUTH_FILE" ] || { echo "--auth: Datei nicht gefunden: $AUTH_FILE" >&2; exit 1; }

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
DIR=/etc/openvpn/client
CONF="$DIR/$NAME.conf"
AUTH="$DIR/$NAME.auth"
UNIT="openvpn-client@$NAME.service"

if ! command -v openvpn >/dev/null; then
    say "Installiere openvpn"
    apt-get update -q
    apt-get install -y --no-install-recommends openvpn
fi

say "Konfiguration nach $CONF"
install -d -m 0755 "$DIR"
# Windows-Zeilenenden entfernen, bisherigen wol-server-Block verwerfen
tr -d '\r' < "$OVPN" | sed '/^# >>> wol-server/,/^# <<< wol-server/d' > "$CONF.new"

# auth-user-pass ohne Datei würde interaktiv fragen – headless unmöglich
if grep -Eq '^[[:space:]]*auth-user-pass[[:space:]]*$' "$CONF.new"; then
    if [ -n "$AUTH_FILE" ]; then
        (umask 077; tr -d '\r' < "$AUTH_FILE" > "$AUTH")
    elif [ -t 0 ]; then
        say "Der VPN-Zugang braucht Benutzername und Passwort"
        read -r -p "    Benutzername: " vpn_user
        read -r -s -p "    Passwort: " vpn_pass; echo
        umask 077
        printf '%s\n%s\n' "$vpn_user" "$vpn_pass" > "$AUTH"
        unset vpn_user vpn_pass
    elif [ ! -f "$AUTH" ]; then
        echo "auth-user-pass benötigt $AUTH (Zeile 1: Benutzer, Zeile 2: Passwort)" >&2
        rm -f "$CONF.new"; exit 1
    fi
    sed -i -E "s|^[[:space:]]*auth-user-pass[[:space:]]*$|auth-user-pass $AUTH|" "$CONF.new"
    chmod 0600 "$AUTH"
fi

has_keepalive=0
grep -Eq '^[[:space:]]*(keepalive|ping-restart)[[:space:]]' "$CONF.new" && has_keepalive=1
{
    echo "# >>> wol-server"
    echo "# Nach Verbindungsabbrüchen (WLAN, Server) unbegrenzt neu verbinden"
    echo "resolv-retry infinite"
    echo "connect-retry 5 120"
    echo "persist-key"
    if [ "$has_keepalive" -eq 0 ]; then
        echo "ping 15"
        echo "ping-restart 90"
    fi
    if [ "$ALLOW_REDIRECT" -eq 0 ]; then
        echo "# Nur erreichbar sein, keinen Internetverkehr durch den Tunnel leiten"
        echo "pull-filter ignore \"redirect-gateway\""
        echo "pull-filter ignore \"dhcp-option DNS\""
    fi
    echo "# <<< wol-server"
} >> "$CONF.new"
mv "$CONF.new" "$CONF"
chmod 0600 "$CONF"

# Verweise auf separate Dateien (statt Inline-<ca> usw.) müssen neben der Konfiguration liegen
# shellcheck disable=SC2013  # Dateinamen ohne Leerzeichen
for ref in $(awk '$1 ~ /^(ca|cert|key|tls-auth|tls-crypt|tls-crypt-v2|pkcs12)$/ && NF >= 2 {print $2}' "$CONF"); do
    case "$ref" in /*) path="$ref" ;; *) path="$DIR/$ref" ;; esac
    [ -f "$path" ] && continue
    src="$(dirname "$OVPN")/$ref"
    if [ -f "$src" ]; then
        install -m 0600 "$src" "$path"
        echo "    $ref kopiert"
    else
        echo "WARNUNG: $ref wird referenziert, fehlt aber ($path)" >&2
    fi
done

say "Dienst $UNIT"
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
systemctl enable "$UNIT" >/dev/null   # lädt selbst neu, auch das Drop-in
systemctl restart "$UNIT"
if [ "$WAIT" -eq 0 ]; then
    say "Dienst $UNIT gestartet"
    exit 0
fi

say "Warte auf den Tunnel (max. 45 s)"
vpn_ip=""
for _ in $(seq 45); do
    vpn_ip=$(ip -4 -o addr show 2>/dev/null | awk '$2 ~ /^(tun|tap)/ {split($4, a, "/"); print a[1]; exit}')
    [ -n "$vpn_ip" ] && break
    sleep 1
done

if [ -z "$vpn_ip" ]; then
    echo "Noch kein Tunnel. Logs: journalctl -u $UNIT -e" >&2
    exit 1
fi

port=$(python3 -c 'import json;print(json.load(open("/etc/wol-server/config.json")).get("port",80))' 2>/dev/null || echo 80)
suffix=""; [ "$port" != "80" ] && suffix=":$port"
if ! (cd /opt/wol-server 2>/dev/null && python3 - "$vpn_ip" <<'PY'
import ipaddress, sys
from wolserver.config import load_config
from wolserver.server import AccessList
# "auto" erlaubt das Netz des Tunnels ohnehin; feste Listen müssen es enthalten
sys.exit(0 if AccessList(load_config()["allowed_networks"]).allows(ipaddress.ip_address(sys.argv[1])) else 1)
PY
); then
    echo "WARNUNG: $vpn_ip ist nicht in allowed_networks – in /etc/wol-server/config.json ergänzen"
    echo "         (\"auto\" oder das VPN-Subnetz, z. B. \"10.8.0.0/24\") und: sudo systemctl restart wol-server"
fi
say "VPN verbunden: $vpn_ip"
echo "  WOL-GUI über VPN: http://$vpn_ip$suffix/"
echo "  Tipp: Auf dem VPN-Server dem Pi eine feste Adresse geben (client-config-dir / ifconfig-push)"
echo "        und client-to-client bzw. Routing erlauben, damit andere VPN-Clients ihn erreichen."
