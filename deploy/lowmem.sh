#!/bin/bash
# Stellt einen Raspberry Pi Zero W (512 MB RAM) auf wenig Speicherverbrauch ein.
# Mehrfach ausführbar; config.txt wird vorher gesichert.
#
#   sudo ./deploy/lowmem.sh    danach neu starten (firstboot.sh ruft es auf und startet neu)
#
# Die Kernel-Parameter (cmdline.txt) bleiben unverändert. Alles wirkt erst nach dem Neustart am Ende
# der Einrichtung; der erste Start läuft mit den Original-Boot-Dateien.
#
# Rückgängig: Block "# >>> wol-server lowmem" aus config.txt entfernen, die Dateien wol-server-lowmem*
# in /etc/modprobe.d, /etc/sysctl.d und /etc/rpi/swap.conf.d löschen, die genannten Timer aktivieren.
set -euo pipefail

case "${1:-}" in
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    "") ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
esac
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }
CONFIG=/boot/firmware/config.txt
[ -f "$CONFIG" ] || CONFIG=/boot/config.txt
[ -f "$CONFIG" ] || { echo "config.txt nicht gefunden – ist das ein Raspberry Pi?" >&2; exit 1; }

# ---------------------------------------------------------------- config.txt
# Ohne KMS-Treiber, Audio sowie Kamera- und Display-Erkennung lädt der Kernel deren Treiber gar nicht
# erst. gpu_mem bleibt beim Standard: Mit gpu_mem=16 läuft die Firmware über und überschreibt
# Speicher (zufällige Kernel-Oopses unter Last); Kamera- und Codec-Puffer liegen ohnehin im CMA.
# over_voltage=2 (nur Zero W): Manche Boards laufen mit den 6.x-Kerneln bei Standardspannung unter
# Speicher- und Kartenlast nicht stabil (raspberrypi/linux#5572). Ohne force_turbo bleibt die
# Garantie erhalten.
say "Firmware: Treiber und Spannung"
# Neue Fassung erst fertig schreiben, dann austauschen: Ein Reset mitten im Schreiben hinterließ
# sonst eine leere config.txt auf der FAT-Partition.
NEW="$CONFIG.wol-neu"
{
    sed -e 's/^dtoverlay=vc4-kms-v3d/#&  # deaktiviert von wol-server lowmem/' \
        -e 's/^max_framebuffers=/#&/' \
        -e '/^# >>> wol-server lowmem/,/^# <<< wol-server lowmem/d' "$CONFIG"
    echo "# >>> wol-server lowmem"
    echo "[all]"
    echo "dtparam=audio=off"
    echo "camera_auto_detect=0"
    echo "display_auto_detect=0"
    echo "[pi0w]"
    echo "over_voltage=2"
    echo "[all]"
    echo "# <<< wol-server lowmem"
} > "$NEW"
if cmp -s "$CONFIG" "$NEW"; then
    rm -f "$NEW"
else
    cp -a "$CONFIG" "$CONFIG.bak-$(date +%Y%m%d-%H%M%S)"
    sync
    mv -f "$NEW" "$CONFIG"
    sync
fi

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
