#!/bin/bash
# WOL-Server - deploy/lowpower.sh
# Tunes a Raspberry Pi Zero W for headless operation with low power.
#
#   Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
#   Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
#   Written by Anthropic Claude Opus 5.5 - AI generated content.
#
#   Free software under the GNU General Public License, version 3 or later.
#   There is no warranty, to the extent permitted by law. The full text is in
#   LICENSES/GPL-3.0-or-later.txt.
#
# SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
# SPDX-FileContributor: Anthropic Claude Opus 5.5 (AI generated content)
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Optimizes a Raspberry Pi Zero W for headless continuous operation with low power
# and little SD card wear. Can be run repeatedly; config.txt is backed up first.
# Memory split, graphics, audio and camera drivers are handled by lowmem.sh.
#
#   sudo ./deploy/lowpower.sh [--led-off] [--keep-hdmi] [--keep-bluetooth]
#
# Reboot afterwards. Undo: remove the block "# >>> wol-server lowpower" from config.txt
# (or restore the backup) and re-enable the listed services.
set -euo pipefail

LED_OFF=0
KEEP_HDMI=0
KEEP_BT=0
for arg in "$@"; do
    case "$arg" in
        --led-off) LED_OFF=1 ;;
        --keep-hdmi) KEEP_HDMI=1 ;;
        --keep-bluetooth) KEEP_BT=1 ;;
        -h|--help) sed -n '17,24p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo." >&2; exit 1; }
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

CONFIG=/boot/firmware/config.txt
[ -f "$CONFIG" ] || CONFIG=/boot/config.txt
[ -f "$CONFIG" ] || { echo "config.txt not found - is this a Raspberry Pi?" >&2; exit 1; }

# ---------------------------------------------------------------- config.txt
say "Firmware settings in $CONFIG"
cp -a "$CONFIG" "$CONFIG.bak-$(date +%Y%m%d-%H%M%S)"
# Work on a copy and swap it in only when completely written: otherwise a reset while
# writing would leave an empty config.txt on the FAT partition.
NEW="$CONFIG.wol-neu"
cp "$CONFIG" "$NEW"
sed -i '/^# >>> wol-server lowpower/,/^# <<< wol-server lowpower/d' "$NEW"

{
    echo "# >>> wol-server lowpower"
    echo "[all]"
    [ "$KEEP_BT" -eq 0 ] && echo "dtoverlay=disable-bt"
    if [ "$LED_OFF" -eq 1 ]; then
        echo "dtparam=act_led_trigger=none"
        echo "dtparam=act_led_activelow=on"
    fi
    echo "# <<< wol-server lowpower"
} >> "$NEW"
sync
mv -f "$NEW" "$CONFIG"
sync

# ---------------------------------------------------------------- HDMI off
if [ "$KEEP_HDMI" -eq 0 ]; then
    say "Switching off HDMI at boot (~25 mA)"
    cat > /etc/systemd/system/hdmi-off.service <<'EOF'
[Unit]
Description=Switch off HDMI output (headless)
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'command -v vcgencmd >/dev/null && vcgencmd display_power 0 >/dev/null || true'

[Install]
WantedBy=multi-user.target
EOF
    systemctl enable --no-reload hdmi-off.service >/dev/null
fi

# ---------------------------------------------------------------- Services
say "Disabling unneeded services"
services=(triggerhappy.service ModemManager.service)
[ "$KEEP_BT" -eq 0 ] && services+=(hciuart.service bluetooth.service)
for svc in "${services[@]}"; do
    if systemctl list-unit-files "$svc" >/dev/null 2>&1 && systemctl cat "$svc" >/dev/null 2>&1; then
        systemctl disable --no-reload --now "$svc" >/dev/null 2>&1 || true
        echo "    $svc disabled"
    fi
done

# ---------------------------------------------------------------- Spare the SD card
say "Keeping the journal in RAM only"
install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/60-wol-server-volatile.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=16M
EOF
systemctl restart systemd-journald

if systemctl cat dphys-swapfile.service >/dev/null 2>&1; then
    say "Disabling the swap file on the SD card"
    dphys-swapfile swapoff >/dev/null 2>&1 || true
    systemctl disable --no-reload --now dphys-swapfile.service >/dev/null 2>&1 || true
fi

# A single reload for all changes above (takes 12-20 s on the Zero)
systemctl daemon-reload

# Wi-Fi powersave stays OFF on purpose - handled by wlan-setup.sh (reachability comes first).

echo
say "Done. Please reboot: sudo reboot"
