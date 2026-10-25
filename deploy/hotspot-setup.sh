#!/bin/bash
# Richtet die WLAN-Verwaltung mit Einrichtungs-Hotspot ein (wird von install.sh aufgerufen).
#
#   sudo ./deploy/hotspot-setup.sh [--country DE]
#
# Findet der Pi kein bekanntes WLAN, öffnet er den Hotspot "WOL-Setup-XXXX". Mit dem Handy
# verbinden, die Einrichtungsseite öffnet sich (sonst http://10.42.0.1/), dort das WLAN wählen.
# Voraussetzung: NetworkManager (Standard ab Raspberry Pi OS Bookworm).
set -euo pipefail

COUNTRY=DE
while [ $# -gt 0 ]; do
    case "$1" in
        --country) COUNTRY="$2"; shift 2 ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
    esac
done
[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }
[[ "$COUNTRY" =~ ^[A-Z]{2}$ ]] || { echo "--country: Ländercode wie DE, AT, CH" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

if ! command -v nmcli >/dev/null || ! nmcli -t -f RUNNING general 2>/dev/null | grep -q running; then
    echo "NetworkManager läuft nicht – Einrichtungs-Hotspot wird nicht eingerichtet." >&2
    echo "Raspberry Pi OS ab Bookworm nutzt NetworkManager standardmäßig." >&2
    exit 1
fi
[ -d /opt/wol-server/wolserver ] || { echo "Bitte zuerst install.sh ausführen." >&2; exit 1; }

say "Pakete für den Hotspot-Betrieb"
missing=()
dpkg -s dnsmasq-base >/dev/null 2>&1 || missing+=(dnsmasq-base)   # DHCP/DNS im Hotspot
command -v iw >/dev/null || missing+=(iw)
if ! command -v nft >/dev/null && ! command -v iptables >/dev/null; then
    missing+=(nftables)   # NetworkManager richtet damit das Hotspot-Netz ein
fi
if [ ${#missing[@]} -gt 0 ]; then
    apt-get update -q
    apt-get install -y --no-install-recommends "${missing[@]}"
fi

say "WLAN-Land $COUNTRY setzen (sonst bleibt das WLAN auf frischen Systemen gesperrt)"
if command -v raspi-config >/dev/null; then
    raspi-config nonint do_wifi_country "$COUNTRY" || true
else
    iw reg set "$COUNTRY" 2>/dev/null || true
fi
if command -v rfkill >/dev/null; then rfkill unblock wifi 2>/dev/null || true; fi
nmcli radio wifi on || true

say "Captive Portal: Im Hotspot alle Namen auf den Pi auflösen"
install -d /etc/NetworkManager/dnsmasq-shared.d
cat > /etc/NetworkManager/dnsmasq-shared.d/60-wol-server-captive.conf <<'EOF'
# Nur aktiv, solange der Einrichtungs-Hotspot läuft (NetworkManager "shared"-Modus).
# Handys rufen dann die Einrichtungsseite von selbst auf.
address=/#/10.42.0.1
EOF

say "DHCP/DNS (dnsmasq) bereitstellen – eingeschaltet wird es in der Web-GUI unter Netzwerk"
# Benutzer dnsmasq legt das Paket dnsmasq-base an; zur Sicherheit auf nobody ausweichen
DNS_USER=dnsmasq
id -u "$DNS_USER" >/dev/null 2>&1 || DNS_USER=nobody
install -d -m 0755 /etc/wol-dns
install -d -m 0755 -o "$DNS_USER" /var/lib/wol-dns
install -d -m 0700 /var/lib/wol-wifi /var/lib/wol-wifi/vpn /var/lib/wol-wifi/dns /var/lib/wol-wifi/dns/requests
install -m 0644 "$SRC/deploy/wol-dns.service" "$SRC/deploy/wol-dns-apply.path" "$SRC/deploy/wol-dns-apply.service" \
    /etc/systemd/system/
# Herstellernamen zu MAC-Adressen (optional, braucht Internet)
if [ ! -f /usr/share/ieee-data/oui.csv ]; then
    timeout 300 apt-get install -y -q --no-install-recommends ieee-data >/dev/null 2>&1 \
        || echo "    ieee-data nicht installiert – Hersteller werden ohne Liste nicht angezeigt"
fi

say "Dienst wol-wifi (WLAN, VPN und Netzwerk aus der Web-GUI)"
install -m 0644 "$SRC/deploy/wol-wifi.service" /etc/systemd/system/wol-wifi.service
install -m 0644 "$SRC/deploy/wol-vpn.path" "$SRC/deploy/wol-vpn.service" /etc/systemd/system/
# Ein enable für alle: lädt einmal neu (siehe install.sh)
systemctl enable wol-wifi.service wol-vpn.path wol-dns-apply.path >/dev/null
systemctl restart wol-wifi.service
systemctl start wol-vpn.path wol-dns-apply.path
sleep 2
if systemctl is-active --quiet wol-wifi.service; then
    say "WLAN-Verwaltung läuft"
else
    echo "wol-wifi konnte nicht gestartet werden – siehe: journalctl -u wol-wifi -e" >&2
    exit 1
fi
