#!/bin/bash
# Writes a short report to the boot partition on every boot (wol-boot.log), so a Pi without
# network can be examined with a card reader. The previous report is kept as wol-boot.1.log.
# Called by wol-bootlog.timer three minutes after boot.
set -u

BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot
OUT="$BOOT/wol-boot.log"
NEW="$OUT.neu"

section() { printf '\n===== %s\n' "$1"; }
{
    echo "WOL-Server - boot report of $(date '+%F %T %Z')"
    section "System"
    uname -a
    uptime
    cat /proc/cmdline
    command -v vcgencmd >/dev/null && vcgencmd get_throttled && vcgencmd measure_temp
    section "Memory"
    free -m
    swapon --show
    section "Network"
    ip -br addr
    ip route
    nmcli -t -f DEVICE,STATE,CONNECTION device
    section "Services"
    echo "System: $(systemctl is-system-running)"
    systemctl --failed --no-legend --plain
    for unit in wol-server wol-wifi wol-dns openvpn-client@wol; do
        printf '%-22s %s\n' "$unit" "$(systemctl is-active "$unit")"
    done
    section "Warnings and errors since boot"
    journalctl -b -p warning --no-pager -n 150 -o short-monotonic
    section "WOL services"
    journalctl -b -u 'wol-*' --no-pager -n 80 -o short-monotonic
} > "$NEW" 2>&1

[ -f "$OUT" ] && mv -f "$OUT" "$BOOT/wol-boot.1.log"
mv -f "$NEW" "$OUT"
sync
