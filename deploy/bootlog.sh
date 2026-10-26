#!/bin/bash
# Schreibt bei jedem Start einen Kurzbericht auf die Boot-Partition (wol-boot.log). So lässt sich ein
# Pi ohne Netz über einen Kartenleser untersuchen. Der vorige Bericht bleibt als wol-boot.1.log.
# Aufgerufen von wol-bootlog.timer drei Minuten nach dem Start.
set -u

BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot
OUT="$BOOT/wol-boot.log"
NEW="$OUT.neu"

section() { printf '\n===== %s\n' "$1"; }
{
    echo "WOL-Server – Startbericht vom $(date '+%F %T %Z')"
    section "System"
    uname -a
    uptime
    cat /proc/cmdline
    command -v vcgencmd >/dev/null && vcgencmd get_throttled && vcgencmd measure_temp
    section "Speicher"
    free -m
    swapon --show
    section "Netz"
    ip -br addr
    ip route
    nmcli -t -f DEVICE,STATE,CONNECTION device
    section "Dienste"
    echo "System: $(systemctl is-system-running)"
    systemctl --failed --no-legend --plain
    for unit in wol-server wol-wifi wol-dns openvpn-client@wol; do
        printf '%-22s %s\n' "$unit" "$(systemctl is-active "$unit")"
    done
    section "Warnungen und Fehler seit dem Start"
    journalctl -b -p warning --no-pager -n 150 -o short-monotonic
    section "WOL-Dienste"
    journalctl -b -u 'wol-*' --no-pager -n 80 -o short-monotonic
} > "$NEW" 2>&1

[ -f "$OUT" ] && mv -f "$OUT" "$BOOT/wol-boot.1.log"
mv -f "$NEW" "$OUT"
sync
