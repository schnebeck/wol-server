#!/bin/sh
# WOL-Server - deploy/wol-sleep.sh
# Forced command for the WOL-Server's key on a target device: sleep on request, refuse while
# the device is in use, and hold it awake while a client requests it.
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
# Install on the target as root:
#   install -m 755 wol-sleep.sh /usr/local/sbin/wol-sleep
# and bind the WOL-Server's key to it in /root/.ssh/authorized_keys (one line, the Pi's address):
#   from="PI-ADDRESS",command="/usr/local/sbin/wol-sleep",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA… wol-server@…
# In the GUI: sleep method SSH, user root, command /usr/local/sbin/wol-sleep (what runs is this
# script either way). For "Keep awake while requested" set the hold command to: hold
#
# Needs systemd (systemctl, systemd-inhibit); busctl and python3 for the inhibitor check.

# What "sleep" means here: suspend (S3, woken by WOL) or poweroff (S5, for firmware without
# S3 - the network card must then support WOL from soft-off).
ACTION="suspend"
# Refuse while someone works at the machine: a graphical session that is active and not locked.
CHECK_SESSION="yes"
# A service that must not be cut off, e.g. "snapshot-backup.service" (empty: none). Only a
# fallback for work that does not announce itself with an inhibitor.
BUSY_UNIT=""

# "hold": the WOL-Server keeps this machine awake while a client requests it. The inhibitor
# lasts as long as the SSH connection: cat waits for input that never comes and ends when the
# WOL-Server closes the connection, or loses it. Nothing below runs then - keep this case
# first: a script without it would put the machine to sleep on "hold".
if [ "${SSH_ORIGINAL_COMMAND:-}" = "hold" ]; then
    exec systemd-inhibit --what=sleep:idle --who=WOL-Server --why="Requested via the WOL-Server" --mode=block cat
fi

# "who (why)" of every block inhibitor for $1 (shutdown or sleep), separated by "; ". Whatever
# announces work to the system counts - a backup, an update wrapped in systemd-inhibit, a long
# job. Delay inhibitors (desktop, NetworkManager) only want time to clean up and are ignored.
# Without busctl or python3 this check is skipped; the checks below it still apply.
inhibited_by() {
    command -v busctl >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 || return 0
    busctl call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager \
        ListInhibitors --json=short 2>/dev/null | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)["data"][0]
except Exception:
    sys.exit(0)
print("; ".join("%s (%s)" % (who, why) for what, who, why, mode, uid, pid in rows
                if mode == "block" and sys.argv[1] in what.split(":")))
' "$1"
}

# An active, unlocked graphical session - by the session's state, never by text on a command line
session_in_use() {
    for s in $(loginctl list-sessions --no-legend | awk '{print $1}'); do
        p=$(loginctl show-session "$s" -p Class -p Type -p Active -p LockedHint 2>/dev/null)
        case "$p" in *Class=user*) ;; *) continue ;; esac
        case "$p" in *Type=x11*|*Type=wayland*) ;; *) continue ;; esac
        case "$p" in *Active=yes*) ;; *) continue ;; esac
        case "$p" in *LockedHint=no*) return 0 ;; esac
    done
    return 1
}

case "$ACTION" in suspend) what="sleep" ;; poweroff) what="shutdown" ;; *) echo "unknown ACTION: $ACTION"; exit 2 ;; esac

held=$(inhibited_by "$what")
if [ -n "$held" ]; then
    echo "in use: $held - not going to $ACTION"
    exit 1
fi
if [ -n "$BUSY_UNIT" ] && systemctl is-active --quiet "$BUSY_UNIT"; then
    echo "$BUSY_UNIT is running - not going to $ACTION"
    exit 1
fi
if [ "$CHECK_SESSION" = "yes" ] && session_in_use; then
    echo "someone is working at this machine (session unlocked) - not going to $ACTION"
    exit 1
fi
logger -t wol-sleep "$ACTION by the WOL-Server"
systemctl "$ACTION"
