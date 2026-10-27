#!/bin/bash
# WOL-Server - deploy/boottrace.sh
# Boot trace during the first boot: system state every 15 s on the boot partition.
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
# Boot trace on the first boot: writes the state to the boot partition every 15 s
# (wol-trace.log, previous snapshot in wol-trace.1.log). If the boot hangs, the last snapshot
# shows what systemd is waiting for - readable with any card reader.
# Started by cloud-init (bootcmd in user-data), stopped by firstboot.sh, at the latest after 45 min.
set -u

BOOT=/boot/firmware
OUT="$BOOT/wol-trace.log"
end=$(( $(date +%s) + 45 * 60 ))

section() { printf '\n===== %s\n' "$1"; }
while [ "$(date +%s)" -lt "$end" ]; do
    {
        echo "Snapshot $(date '+%F %T'), $(cut -d' ' -f1 /proc/uptime) s since boot, load $(cut -d' ' -f1-3 /proc/loadavg)"
        section "Pending systemd jobs"
        systemctl list-jobs --no-pager --no-legend
        section "Failed units"
        systemctl --failed --no-pager --no-legend --plain
        section "Memory"
        free -m
        section "Processes"
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
