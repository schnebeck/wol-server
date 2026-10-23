#!/bin/bash
# Baut ein fertiges SD-Karten-Image: Raspberry Pi OS Lite mit WOL-Server, der sich beim ersten
# Start selbst einrichtet. Danach: Image mit beliebigem Programm auf die Karte schreiben, Pi
# starten, fertig – ohne SSH und ohne die OS-Anpassungen des Imagers.
#
#   ./deploy/make-image.sh [-c wol-setup.txt] [--ovpn client.ovpn] [--ovpn-auth zugang.txt]
#                          [--ssh-key ~/.ssh/id_ed25519.pub] [--image raspios.img.xz]
#                          [-o wol-server.img] [--xz]
#
# Ohne --image wird das aktuelle Raspberry Pi OS Lite (32-bit) geladen und geprüft.
# Läuft ohne root auf jedem Linux-PC; benötigt mtools, xz-utils, fdisk und curl.
set -euo pipefail

SETUP=""
OVPN=""
OVPN_AUTH=""
SSH_KEY=""
IMAGE=""
OUT="wol-server.img"
COMPRESS=0
while [ $# -gt 0 ]; do
    case "$1" in
        -c|--config) SETUP="$2"; shift 2 ;;
        --ovpn) OVPN="$2"; shift 2 ;;
        --ovpn-auth) OVPN_AUTH="$2"; shift 2 ;;
        --ssh-key) SSH_KEY="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        -o|--output) OUT="$2"; shift 2 ;;
        --xz) COMPRESS=1; shift ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
    esac
done

SRC="$(cd "$(dirname "$0")/.." && pwd)"
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { echo "Fehler: $*" >&2; exit 1; }

missing=()
for tool in mcopy:mtools xz:xz-utils sfdisk:fdisk curl:curl; do
    command -v "${tool%%:*}" >/dev/null || missing+=("${tool#*:}")
done
[ ${#missing[@]} -eq 0 ] || die "bitte installieren: sudo apt install ${missing[*]}"
for f in "$SETUP" "$OVPN" "$OVPN_AUTH" "$SSH_KEY" "$IMAGE"; do
    [ -z "$f" ] || [ -f "$f" ] || die "Datei nicht gefunden: $f"
done
[ -n "$SETUP" ] || SETUP="$SRC/deploy/firstboot/wol-setup.txt"
[[ "$OUT" == *.img ]] || die "-o muss auf .img enden (mit --xz entsteht zusätzlich .img.xz)"

# ---------------------------------------------------------------- Einstellungen prüfen
value() { sed -n "s/^$1=//p" "$SETUP" | tail -1 | tr -d '\r'; }
gui_pw=$(value GUI_PASSWORD)
if [ -z "$gui_pw" ]; then
    echo "Hinweis: Kein GUI_PASSWORD gesetzt – beim ersten Start ist die GUI über den offenen Hotspot ungeschützt." >&2
elif [ ${#gui_pw} -lt 8 ]; then
    die "GUI_PASSWORD in $SETUP muss mindestens 8 Zeichen haben"
fi
unset gui_pw
admin=$(value ADMIN_USER | tr -d ' ')
[[ "$admin" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || admin="admin"

# ---------------------------------------------------------------- Raspberry Pi OS besorgen
if [ -z "$IMAGE" ]; then
    cache="${XDG_CACHE_HOME:-$HOME/.cache}/wol-server"
    mkdir -p "$cache"
    say "Suche aktuelles Raspberry Pi OS Lite (32-bit)"
    url=$(curl -fsSLI -o /dev/null -w '%{url_effective}' https://downloads.raspberrypi.com/raspios_lite_armhf_latest)
    IMAGE="$cache/$(basename "$url")"
    if [ ! -f "$IMAGE" ]; then
        say "Lade $(basename "$url")"
        curl -fL --progress-bar -o "$IMAGE.part" "$url"
        expected=$(curl -fsSL "$url.sha256" | awk '{print $1}')
        actual=$(sha256sum "$IMAGE.part" | awk '{print $1}')
        if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
            rm -f "$IMAGE.part"
            die "Prüfsumme stimmt nicht"
        fi
        mv "$IMAGE.part" "$IMAGE"
    else
        say "Verwende $(basename "$IMAGE") aus $cache"
    fi
fi

# ---------------------------------------------------------------- Image schreiben
say "Schreibe $OUT"
case "$IMAGE" in
    *.xz) xz -dc -T0 "$IMAGE" > "$OUT" ;;
    *.img) cp --sparse=always "$IMAGE" "$OUT" ;;
    *) die "--image: .img oder .img.xz erwartet" ;;
esac
start=$(sfdisk --dump "$OUT" | awk -F'[=,]' '$1 ~ /1 : start/ {gsub(/ /, "", $2); print $2; exit}')
[ -n "$start" ] || die "Boot-Partition in $OUT nicht gefunden"
part="$OUT@@$((start * 512))"
export MTOOLS_SKIP_CHECK=1
mdir -i "$part" ::/cmdline.txt >/dev/null 2>&1 || die "$OUT sieht nicht nach Raspberry Pi OS aus"

# ---------------------------------------------------------------- Dateien für den ersten Start
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
mkdir "$stage/wol-server"
cp -r "$SRC/wolserver" "$SRC/deploy" "$SRC/README.md" "$stage/wol-server/"
find "$stage" -name '__pycache__' -prune -exec rm -rf {} +
cp "$SRC/deploy/firstboot/user-data" "$stage/user-data"
printf '%s:*\n' "$admin" > "$stage/userconf.txt"   # Benutzer ohne Passwort-Login, kein Dialog beim Start
cp "$SETUP" "$stage/wol-setup.txt"
[ -z "$OVPN" ] || cp "$OVPN" "$stage/openvpn.ovpn"
[ -z "$OVPN_AUTH" ] || cp "$OVPN_AUTH" "$stage/openvpn.auth"
[ -z "$SSH_KEY" ] || cp "$SSH_KEY" "$stage/ssh_authorized_keys"

say "Kopiere WOL-Server auf die Boot-Partition"
mcopy -i "$part" -s -D o "$stage"/* ::/
mdir -i "$part" ::/wol-server/deploy/firstboot.sh >/dev/null || die "Kopieren fehlgeschlagen"

if [ "$COMPRESS" -eq 1 ]; then
    say "Komprimiere zu $OUT.xz"
    xz -T0 -6 -f "$OUT"
    OUT="$OUT.xz"
fi

echo
echo "  Fertig: $OUT"
echo
echo "  1. Auf die SD-Karte schreiben, z. B. mit dem Raspberry Pi Imager (\"Eigenes Image\";"
echo "     die Frage nach OS-Anpassungen mit NEIN beantworten), balenaEtcher oder"
echo "     xzcat/dd. Achtung: Das Image enthält ggf. Passwörter und VPN-Schlüssel."
echo "  2. Karte in den Pi, Strom an. Der erste Start dauert ca. 5–10 Minuten."
if [ -z "$(value WIFI_SSID)" ]; then
    echo "  3. Mit dem WLAN \"WOL-Setup-XXXX\" verbinden und das Heim-WLAN wählen."
else
    echo "  3. Die GUI ist danach unter http://$(value HOSTNAME | tr -d ' ' | sed 's/^$/wol/').local/ erreichbar."
fi
echo "  Protokoll der Einrichtung: wol-setup.log auf der Boot-Partition"
