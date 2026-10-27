#!/bin/bash
# Configures a Raspberry Pi Zero W (512 MB RAM) for low memory usage.
# Can be run repeatedly; config.txt is backed up first.
#
#   sudo ./deploy/lowmem.sh    reboot afterwards (firstboot.sh calls it and reboots)
#
# The kernel parameters (cmdline.txt) stay unchanged. Everything takes effect only after the reboot
# at the end of the setup; the first boot runs with the original boot files.
#
# Undo: remove the block "# >>> wol-server lowmem" from config.txt, delete the files wol-server-lowmem*
# in /etc/modprobe.d, /etc/sysctl.d and /etc/rpi/swap.conf.d, enable the listed timers.
set -euo pipefail

case "${1:-}" in
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    "") ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
esac
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo." >&2; exit 1; }
CONFIG=/boot/firmware/config.txt
[ -f "$CONFIG" ] || CONFIG=/boot/config.txt
[ -f "$CONFIG" ] || { echo "config.txt not found - is this a Raspberry Pi?" >&2; exit 1; }

# ---------------------------------------------------------------- config.txt
# Without the KMS driver, audio and camera/display detection the kernel does not load their drivers
# at all. gpu_mem stays at the default: with gpu_mem=16 the firmware overflows and overwrites
# memory (random kernel oopses under load); camera and codec buffers live in CMA anyway.
# over_voltage=2 (Zero W only): some boards are not stable with the 6.x kernels at default voltage
# under memory and card load (raspberrypi/linux#5572). Without force_turbo the warranty is
# preserved.
say "Firmware: drivers and voltage"
# Write the new version completely first, then swap it in: otherwise a reset while writing would
# leave an empty config.txt on the FAT partition.
NEW="$CONFIG.wol-neu"
{
    sed -e 's/^dtoverlay=vc4-kms-v3d/#&  # disabled by wol-server lowmem/' \
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

# ---------------------------------------------------------------- Drivers
say "Blacklisting camera, video and audio drivers"
cat > /etc/modprobe.d/wol-server-lowmem.conf <<'EOF'
# wol-server lowmem: headless, without camera, video codecs and audio
blacklist bcm2835_codec
blacklist bcm2835_isp
blacklist bcm2835_v4l2
blacklist bcm2835_unicam
blacklist bcm2835_mmal_vchiq
blacklist vc_sm_cma
blacklist snd_bcm2835
EOF

# ---------------------------------------------------------------- Swap
# By default Raspberry Pi OS writes idle zram pages out to /var/swap on the SD card.
# zram only: no card wear, rpi-swap removes the file on the next boot.
say "Swap only compressed in RAM (zram)"
install -d /etc/rpi/swap.conf.d
cat > /etc/rpi/swap.conf.d/60-wol-server-lowmem.conf <<'EOF'
[Main]
Mechanism=zram
EOF
cat > /etc/sysctl.d/60-wol-server-lowmem.conf <<'EOF'
# zram is faster than re-reading evicted files from the SD card.
# page-cluster=0: zram needs no read-ahead when swapping in.
vm.swappiness = 100
vm.page-cluster = 0
EOF
sysctl -q -p /etc/sysctl.d/60-wol-server-lowmem.conf || true

# ---------------------------------------------------------------- Timer
# Without unattended-upgrades apt-daily only downloads package lists (on the Zero a lot of RAM and
# minutes of CPU). Updates are installed by install.sh or by running apt manually.
say "Disabling daily maintenance runs"
for unit in apt-daily.timer apt-daily-upgrade.timer man-db.timer e2scrub_all.timer \
            e2scrub_reap.service rpi-eeprom-update.service; do
    systemctl cat "$unit" >/dev/null 2>&1 || continue
    # --no-reload: a reload takes 12-20 s on the Zero; the reboot afterwards reads everything anew
    systemctl disable --no-reload --now "$unit" >/dev/null 2>&1 || true
    echo "    $unit disabled"
done

echo
say "Done. Takes effect after a reboot: sudo reboot"
