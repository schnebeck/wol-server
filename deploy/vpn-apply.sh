#!/bin/bash
# Führt VPN-Aufträge aus der Web-GUI aus. Gestartet von wol-vpn.service, sobald der Dienst
# wol-wifi eine Auftragsdatei schreibt (wol-vpn.path). Läuft als root.
#
#   request  {"id": …, "action": "apply"|"enable"|"disable"|"remove"}   (von wol-wifi)
#   job.json {"id": …, "action": …, "state": "running"|"ok"|"failed", "message": …}
set -uo pipefail

STATE=${WOL_VPN_STATE:-/var/lib/wol-wifi/vpn}   # Variable nur für Tests
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

# Letzte Zeilen der Ausgabe als Fehlertext für die GUI (ohne Farbcodes und Fortschrittszeilen)
reason() { sed 's/\x1b\[[0-9;]*m//g' "$log" | grep -v -e '^==>' -e '^[[:space:]]*$' | tail -2 | tr '\n' ' '; }

# Neue Aufträge, die während der Arbeit eintreffen, löst wol-vpn.path nicht erneut aus –
# deshalb in einer Schleife abarbeiten, bis der letzte erledigt ist.
done_id=""
while [ -f "$STATE/request" ]; do
    id=$(field id) || exit 1
    action=$(field action)
    last=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("id",""))' "$STATE/job.json" 2>/dev/null)
    if [ -z "$id" ] || [ "$id" = "$done_id" ] || [ "$id" = "$last" ]; then
        break   # nichts Neues
    fi
    job running "Läuft …"
    log="$STATE/job.log"
    case "$action" in
        apply)
            args=("$STATE/client.ovpn" --no-wait)
            [ -f "$STATE/client.auth" ] && args+=(--auth "$STATE/client.auth")
            if "$SETUP" "${args[@]}" > "$log" 2>&1; then
                job ok "Eingerichtet und eingeschaltet"
            else
                job failed "Einrichtung fehlgeschlagen: $(reason)"
            fi ;;
        enable)
            if systemctl enable --now "$UNIT" > "$log" 2>&1; then job ok "Eingeschaltet"
            else job failed "Einschalten fehlgeschlagen: $(reason)"; fi ;;
        disable)
            if systemctl disable --now "$UNIT" > "$log" 2>&1; then job ok "Ausgeschaltet"
            else job failed "Ausschalten fehlgeschlagen: $(reason)"; fi ;;
        remove)
            systemctl disable --now "$UNIT" > "$log" 2>&1 || true
            rm -rf /etc/openvpn/client/wol.conf /etc/openvpn/client/wol.auth "/etc/systemd/system/$UNIT.d"
            rm -f "$STATE/client.ovpn" "$STATE/client.auth" "$STATE/info.json"
            systemctl daemon-reload
            job ok "Entfernt" ;;
        *)
            job failed "Unbekannter Auftrag: $action" ;;
    esac
    done_id=$id
done
exit 0
