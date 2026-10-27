#!/bin/sh
# WOL-Server - deploy/dns-lease-hook.sh
# dnsmasq lease hook: records lease events for the web GUI.
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
# Called by dnsmasq on every lease change (dhcp-script, as user dnsmasq):
#   <add|old|del> <MAC> <IP> [name]
# Writes one line for "last seen" and the lease history in the web GUI.
LOG=/var/lib/wol-dns/lease-events.log
case "$1" in add|old|del) ;; *) exit 0 ;; esac
printf '%s %s %s %s %s\n' "$(date +%s)" "$1" "$2" "$3" "${4:-}" >> "$LOG" 2>/dev/null || exit 0
# Keep it small: above 4000 lines keep only the last 2000
if [ "$(wc -l < "$LOG")" -gt 4000 ]; then
    tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
exit 0
