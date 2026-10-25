#!/bin/sh
# Von dnsmasq bei jeder Lease-Änderung aufgerufen (dhcp-script, als Benutzer dnsmasq):
#   <add|old|del> <MAC> <IP> [Name]
# Schreibt eine Zeile für „zuletzt gesehen“ und den Lease-Verlauf in der Web-GUI.
LOG=/var/lib/wol-dns/lease-events.log
case "$1" in add|old|del) ;; *) exit 0 ;; esac
printf '%s %s %s %s %s\n' "$(date +%s)" "$1" "$2" "$3" "${4:-}" >> "$LOG" 2>/dev/null || exit 0
# Klein halten: ab 4000 Zeilen nur die letzten 2000 behalten
if [ "$(wc -l < "$LOG")" -gt 4000 ]; then
    tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
exit 0
