#!/bin/bash
# Builds a ready-to-use SD card image: Raspberry Pi OS Lite with WOL-Server, which sets itself
# up on the first boot. Then write the image to the card with any program, boot the Pi,
# done - without SSH and without the Imager's OS customisation.
#
#   ./deploy/make-image.sh [-c wol-setup.txt] [--ovpn client.ovpn] [--ovpn-auth credentials.txt]
#                          [--ssh-key ~/.ssh/id_ed25519.pub] [--image raspios.img.xz]
#                          [-o wol-server.img] [--xz]
#
# Without --image the current Raspberry Pi OS Lite (32-bit) is downloaded and verified.
# Runs without root on any Linux PC; requires mtools, xz-utils, fdisk and curl.
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
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

SRC="$(cd "$(dirname "$0")/.." && pwd)"
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { echo "Error: $*" >&2; exit 1; }

missing=()
for tool in mcopy:mtools xz:xz-utils sfdisk:fdisk curl:curl; do
    command -v "${tool%%:*}" >/dev/null || missing+=("${tool#*:}")
done
[ ${#missing[@]} -eq 0 ] || die "please install: sudo apt install ${missing[*]}"
for f in "$SETUP" "$OVPN" "$OVPN_AUTH" "$SSH_KEY" "$IMAGE"; do
    [ -z "$f" ] || [ -f "$f" ] || die "file not found: $f"
done
[ -n "$SETUP" ] || SETUP="$SRC/deploy/firstboot/wol-setup.txt"
[[ "$OUT" == *.img ]] || die "-o must end in .img (with --xz an additional .img.xz is created)"

# ---------------------------------------------------------------- Check settings
value() { sed -n "s/^$1=//p" "$SETUP" | tail -1 | tr -d '\r'; }
gui_pw=$(value GUI_PASSWORD)
if [ -z "$gui_pw" ]; then
    echo "Note: no GUI_PASSWORD set - on the first boot the GUI is unprotected via the open hotspot." >&2
elif [ ${#gui_pw} -lt 8 ]; then
    die "GUI_PASSWORD in $SETUP must have at least 8 characters"
fi
unset gui_pw
admin=$(value ADMIN_USER | tr -d ' ')
# Raspberry Pi OS always creates the first user; with ADMIN_USER=root it is called "admin"
[[ "$admin" =~ ^[a-z][a-z0-9-]{0,31}$ ]] && [ "$admin" != root ] || admin="admin"

# ---------------------------------------------------------------- Get Raspberry Pi OS
if [ -z "$IMAGE" ]; then
    cache="${XDG_CACHE_HOME:-$HOME/.cache}/wol-server"
    mkdir -p "$cache"
    say "Looking up current Raspberry Pi OS Lite (32-bit)"
    url=$(curl -fsSLI -o /dev/null -w '%{url_effective}' https://downloads.raspberrypi.com/raspios_lite_armhf_latest)
    IMAGE="$cache/$(basename "$url")"
    if [ ! -f "$IMAGE" ]; then
        say "Downloading $(basename "$url")"
        curl -fL --progress-bar -o "$IMAGE.part" "$url"
        expected=$(curl -fsSL "$url.sha256" | awk '{print $1}')
        actual=$(sha256sum "$IMAGE.part" | awk '{print $1}')
        if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
            rm -f "$IMAGE.part"
            die "checksum mismatch"
        fi
        mv "$IMAGE.part" "$IMAGE"
    else
        say "Using $(basename "$IMAGE") from $cache"
    fi
fi

# ---------------------------------------------------------------- Write image
say "Writing $OUT"
case "$IMAGE" in
    *.xz) xz -dc -T0 "$IMAGE" > "$OUT" ;;
    *.img) cp --sparse=always "$IMAGE" "$OUT" ;;
    *) die "--image: .img or .img.xz expected" ;;
esac
start=$(sfdisk --dump "$OUT" | awk -F'[=,]' '$1 ~ /1 : start/ {gsub(/ /, "", $2); print $2; exit}')
[ -n "$start" ] || die "boot partition not found in $OUT"
part="$OUT@@$((start * 512))"
export MTOOLS_SKIP_CHECK=1
mdir -i "$part" ::/cmdline.txt >/dev/null 2>&1 || die "$OUT does not look like Raspberry Pi OS"

# ---------------------------------------------------------------- Files for the first boot
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

mkdir "$stage/wol-server"
cp -r "$SRC/wolserver" "$SRC/deploy" "$SRC/README.md" "$stage/wol-server/"
find "$stage" -name '__pycache__' -prune -exec rm -rf {} +
cp "$SRC/deploy/firstboot/user-data" "$stage/user-data"
printf '%s:*\n' "$admin" > "$stage/userconf.txt"   # user without password login, no dialog at boot
cp "$SETUP" "$stage/wol-setup.txt"
[ -z "$OVPN" ] || cp "$OVPN" "$stage/openvpn.ovpn"
[ -z "$OVPN_AUTH" ] || cp "$OVPN_AUTH" "$stage/openvpn.auth"
[ -z "$SSH_KEY" ] || cp "$SSH_KEY" "$stage/ssh_authorized_keys"

say "Copying WOL-Server to the boot partition"
mcopy -i "$part" -s -D o "$stage"/* ::/
mdir -i "$part" ::/wol-server/deploy/firstboot.sh >/dev/null || die "copying failed"

if [ "$COMPRESS" -eq 1 ]; then
    say "Compressing to $OUT.xz"
    xz -T0 -6 -f "$OUT"
    OUT="$OUT.xz"
fi

echo
echo "  Done: $OUT"
echo
echo "  1. Write it to the SD card, e.g. with Raspberry Pi Imager (\"Use custom\";"
echo "     answer NO when asked about OS customisation), balenaEtcher or"
echo "     xzcat/dd. Caution: the image may contain passwords and VPN keys."
echo "  2. Card into the Pi, power on. The first boot takes about 5-10 minutes."
if [ -z "$(value WIFI_SSID)" ]; then
    echo "  3. Connect to the Wi-Fi \"WOL-Setup-XXXX\" and choose your home Wi-Fi."
else
    echo "  3. The GUI is then reachable at http://$(value HOSTNAME | tr -d ' ' | sed 's/^$/wol/').local/."
fi
echo "  Setup log: wol-setup.log on the boot partition"
