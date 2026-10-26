#!/bin/bash
# Ablaufverfolgung beim ersten Start: schreibt alle 15 s den Zustand auf die Boot-Partition
# (wol-trace.log, vorige Momentaufnahme in wol-trace.1.log). Hängt der Start, zeigt die letzte
# Momentaufnahme, worauf systemd wartet – lesbar mit jedem Kartenleser.
# Gestartet von cloud-init (bootcmd in user-data), beendet von firstboot.sh, spätestens nach 45 min.
set -u

BOOT=/boot/firmware
OUT="$BOOT/wol-trace.log"
end=$(( $(date +%s) + 45 * 60 ))

section() { printf '\n===== %s\n' "$1"; }
while [ "$(date +%s)" -lt "$end" ]; do
    {
        echo "Momentaufnahme $(date '+%F %T'), seit dem Start $(cut -d' ' -f1 /proc/uptime) s, Last $(cut -d' ' -f1-3 /proc/loadavg)"
        section "Wartende systemd-Aufträge"
        systemctl list-jobs --no-pager --no-legend
        section "Fehlgeschlagene Units"
        systemctl --failed --no-pager --no-legend --plain
        section "Speicher"
        free -m
        section "Prozesse"
        ps -eo pid,stat,etime,pcpu,rss,args --sort=-pcpu | head -15 | cut -c1-160
        section "Kernel"
        dmesg | tail -40
        section "Journal"
        journalctl -b --no-pager -n 60 -o short-monotonic
    } > "$OUT.neu" 2>&1
    [ -f "$OUT" ] && mv -f "$OUT" "$BOOT/wol-trace.1.log"
    mv -f "$OUT.neu" "$OUT"
    sync -f "$OUT"
    sleep 15
done
