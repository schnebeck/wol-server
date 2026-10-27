#!/bin/bash
# WOL-Server - deploy/vpn-apply.sh
# Runs VPN jobs from the web GUI as root (wol-vpn.service).
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
# Runs VPN jobs from the web GUI. Started by wol-vpn.service as soon as the wol-wifi
# service writes a request file (wol-vpn.path). Runs as root.
#
#   request  {"id": …, "action": "apply"|"enable"|"disable"|"remove"}   (from wol-wifi)
#   job.json {"id": …, "action": …, "state": "running"|"ok"|"failed", "message": …}
set -uo pipefail

STATE=${WOL_VPN_STATE:-/var/lib/wol-wifi/vpn}   # variable only for tests
UNIT=openvpn-client@wol.service
SETUP=${WOL_VPN_SETUP:-"$(cd "$(dirname "$0")" && pwd)/openvpn-setup.sh"}

field() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' "$STATE/request" "$1"; }
job() {   # job <state> <message>
    python3 - "$STATE/job.json" "$id" "$action" "$1" "$2" <<'PY'
import json, os, sys, time
path, id_, action, state, message = sys.argv[1:]
tmp = path + ".tmp"
with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    json.dump({"id": id_, "action": action, "state": state, "message": message, "ts": time.time()}, f)
os.replace(tmp, path)
PY
}

# Last lines of the output as error text for the GUI (without color codes and progress lines)
reason() { sed 's/\x1b\[[0-9;]*m//g' "$log" | grep -v -e '^==>' -e '^[[:space:]]*$' | tail -2 | tr '\n' ' '; }

# wol-vpn.path does not trigger again for jobs arriving while one is being processed -
# so process them in a loop until the last one is done.
done_id=""
while [ -f "$STATE/request" ]; do
    id=$(field id) || exit 1
    action=$(field action)
    last=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("id",""))' "$STATE/job.json" 2>/dev/null)
    if [ -z "$id" ] || [ "$id" = "$done_id" ] || [ "$id" = "$last" ]; then
        break   # nothing new
    fi
    job running "Running …"
    log="$STATE/job.log"
    case "$action" in
        apply)
            args=("$STATE/client.ovpn" --no-wait)
            [ -f "$STATE/client.auth" ] && args+=(--auth "$STATE/client.auth")
            if "$SETUP" "${args[@]}" > "$log" 2>&1; then
                job ok "Set up and switched on"
            else
                job failed "Setup failed: $(reason)"
            fi ;;
        enable)
            if systemctl enable --now "$UNIT" > "$log" 2>&1; then job ok "Switched on"
            else job failed "Switching on failed: $(reason)"; fi ;;
        disable)
            if systemctl disable --now "$UNIT" > "$log" 2>&1; then job ok "Switched off"
            else job failed "Switching off failed: $(reason)"; fi ;;
        remove)
            systemctl disable --now "$UNIT" > "$log" 2>&1 || true
            rm -rf /etc/openvpn/client/wol.conf /etc/openvpn/client/wol.auth "/etc/systemd/system/$UNIT.d"
            rm -f "$STATE/client.ovpn" "$STATE/client.auth" "$STATE/info.json"
            systemctl daemon-reload
            job ok "Removed" ;;
        *)
            job failed "Unknown job: $action" ;;
    esac
    done_id=$id
done
exit 0
