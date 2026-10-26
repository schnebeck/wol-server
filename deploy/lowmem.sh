#!/bin/bash
# Stellt Kernel und Firmware eines Raspberry Pi Zero W (512 MB RAM) auf wenig Speicher ein.
# Mehrfach ausführbar; geänderte Boot-Dateien werden vorher gesichert.
#
#   sudo ./deploy/lowmem.sh          auf dem Pi, danach neu starten
#   ./deploy/lowmem.sh --boot DIR    nur config.txt und cmdline.txt in DIR (make-image.sh, ohne root)
#
# Rückgängig: Block "# >>> wol-server lowmem" aus config.txt entfernen, cmdline.txt aus dem Backup
# zurückspielen, die Dateien wol-server-lowmem* in /etc/modprobe.d, /etc/sysctl.d und
# /etc/rpi/swap.conf.d löschen und die genannten Timer wieder aktivieren.
set -euo pipefail

BOOT=""
BACKUP=1
case "${1:-}" in
    --boot) BOOT="${2:?Verzeichnis fehlt}"; BACKUP=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    "") ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
esac
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

if [ -z "$BOOT" ]; then
    [ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }
    BOOT=/boot/firmware
    [ -f "$BOOT/config.txt" ] || BOOT=/boot
fi
for f in config.txt cmdline.txt; do
    [ -f "$BOOT/$f" ] || { echo "$BOOT/$f nicht gefunden – ist das ein Raspberry Pi?" >&2; exit 1; }
done

# Neue Fassung erst fertig schreiben, dann austauschen: Ein Reset mitten im Schreiben hinterließ
# sonst eine leere Datei auf der FAT-Partition.
replace() {
    if cmp -s "$1" "$2"; then
        rm -f "$2"
        echo "    $(basename "$1") unverändert"
        return
    fi
    [ "$BACKUP" -eq 0 ] || cp -a "$1" "$1.bak-$(date +%Y%m%d-%H%M%S)"
    sync
    mv -f "$2" "$1"
    sync
    echo "    $(basename "$1") angepasst"
}

# ---------------------------------------------------------------- config.txt
# Der Grafikchip bekommt 16 statt 64 MB (mehr RAM für Linux). Ohne KMS-Treiber, Audio sowie
# Kamera- und Display-Erkennung lädt der Kernel deren Treiber gar nicht erst.
say "Firmware: Speicheraufteilung und Treiber"
NEW="$BOOT/config.txt.wol-neu"
{
    sed -e 's/^dtoverlay=vc4-kms-v3d/#&  # deaktiviert von wol-server lowmem/' \
        -e 's/^max_framebuffers=/#&/' \
        -e '/^# >>> wol-server lowmem/,/^# <<< wol-server lowmem/d' "$BOOT/config.txt"
    echo "# >>> wol-server lowmem"
    echo "[all]"
    echo "gpu_mem=16"
    echo "dtparam=audio=off"
    echo "camera_auto_detect=0"
    echo "display_auto_detect=0"
    echo "# <<< wol-server lowmem"
} > "$NEW"
replace "$BOOT/config.txt" "$NEW"

# ---------------------------------------------------------------- cmdline.txt
# cma=16M:      Die CMA-Reserve (Device Tree: 64 MB) dient Kamera, Codecs und Grafik, die hier fehlen.
# audit=0:      kein Kernel-Audit (AppArmor bleibt aktiv).
# console=tty1: keine Bildschirmkonsole; die serielle Konsole bleibt für Notfälle.
say "Kernel-Parameter"
NEW="$BOOT/cmdline.txt.wol-neu"
args=$(tr ' ' '\n' < "$BOOT/cmdline.txt" | grep -vE '^(console=tty1|cma=.*|audit=.*)?$' | tr '\n' ' ')
printf '%scma=16M audit=0\n' "$args" > "$NEW"
replace "$BOOT/cmdline.txt" "$NEW"

[ "$BACKUP" -eq 1 ] || exit 0

# ---------------------------------------------------------------- Treiber
say "Kamera-, Video- und Audiotreiber sperren"
cat > /etc/modprobe.d/wol-server-lowmem.conf <<'EOF'
# wol-server lowmem: headless, ohne Kamera, Video-Codecs und Audio
blacklist bcm2835_codec
blacklist bcm2835_isp
blacklist bcm2835_v4l2
blacklist bcm2835_unicam
blacklist bcm2835_mmal_vchiq
blacklist vc_sm_cma
blacklist snd_bcm2835
EOF

# ---------------------------------------------------------------- Swap
# Raspberry Pi OS lagert ruhende zram-Seiten standardmäßig in /var/swap auf der SD-Karte aus.
# Nur zram: kein Verschleiß der Karte, rpi-swap entfernt die Datei beim nächsten Start.
say "Swap nur komprimiert im RAM (zram)"
install -d /etc/rpi/swap.conf.d
cat > /etc/rpi/swap.conf.d/60-wol-server-lowmem.conf <<'EOF'
[Main]
Mechanism=zram
EOF
cat > /etc/sysctl.d/60-wol-server-lowmem.conf <<'EOF'
# zram ist schneller als das erneute Lesen verdrängter Dateien von der SD-Karte.
# page-cluster=0: zram braucht kein Vorauslesen beim Einlagern.
vm.swappiness = 100
vm.page-cluster = 0
EOF
sysctl -q -p /etc/sysctl.d/60-wol-server-lowmem.conf || true

# ---------------------------------------------------------------- Timer
# Ohne unattended-upgrades lädt apt-daily nur Paketlisten (auf dem Zero viel RAM und minutenlang
# CPU). Updates spielt install.sh bzw. apt von Hand ein.
say "Tägliche Wartungsläufe abschalten"
for unit in apt-daily.timer apt-daily-upgrade.timer man-db.timer e2scrub_all.timer \
            e2scrub_reap.service rpi-eeprom-update.service; do
    systemctl cat "$unit" >/dev/null 2>&1 || continue
    # --no-reload: Neuladen dauert auf dem Zero 12–20 s; der Neustart danach liest alles neu ein
    systemctl disable --no-reload --now "$unit" >/dev/null 2>&1 || true
    echo "    $unit deaktiviert"
done

echo
say "Fertig. Wirksam nach einem Neustart: sudo reboot"
