#!/bin/bash
# Einrichtung beim ersten Start eines WOL-Server-Images.
#
# Wird von cloud-init aufgerufen (deploy/firstboot/user-data auf der Boot-Partition) und liest
# die Einstellungen aus /boot/firmware/wol-setup.txt. Es braucht kein Internet: Raspberry Pi OS
# Lite (Trixie) bringt alle Pakete mit. Nur OpenVPN wird nachinstalliert, sobald Internet da ist.
#
# Danach werden Einstellungen, VPN-Schlüssel und Quellen von der Boot-Partition gelöscht
# und das Protokoll als wol-setup.log dort abgelegt.
#
# cloud-init startet das Skript nur einmal. Bricht der Pi mittendrin ab (Stromausfall, Hänger
# und Hardware-Watchdog), setzt wol-firstboot.service die Einrichtung beim nächsten Start
# fort; alle Schritte vertragen deshalb eine Wiederholung.
set -uo pipefail

BOOT=/boot/firmware
SRC_BOOT="$BOOT/wol-server"
WORK=/var/tmp/wol-server-src
LOG=/var/log/wol-firstboot.log
STATUS="FEHLGESCHLAGEN"
MARKER=/run/wol-firstboot  # solange er existiert, blinkt die LED schnell (auch über wol-wifi)
RETRY_UNIT=/etc/systemd/system/wol-firstboot.service

exec > >(tee -a "$LOG") 2>&1
log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }

# Status-LED: "fast" = schnelles Blinken, "normal" = Standardanzeige (SD-Zugriffe)
led() {
    local dir trigger
    for dir in /sys/class/leds/ACT /sys/class/leds/led0; do
        [ -w "$dir/trigger" ] || continue
        if [ "$1" = fast ]; then
            echo timer > "$dir/trigger" && echo 100 > "$dir/delay_on" && echo 100 > "$dir/delay_off"
        else
            for trigger in actpwr mmc0 default-on; do
                grep -qw "$trigger" "$dir/trigger" && { echo "$trigger" > "$dir/trigger"; break; }
            done
        fi
        return 0
    done
} 2>/dev/null

# shellcheck disable=SC2317  # wird über trap aufgerufen
finish() {
    # Geheimnisse von der FAT-Partition entfernen – auch wenn etwas schiefging
    rm -f "$BOOT/wol-setup.txt" "$BOOT/openvpn.ovpn" "$BOOT/openvpn.auth" "$BOOT/ssh_authorized_keys"
    rm -rf "$SRC_BOOT" "$WORK"
    rm -f "$MARKER"
    systemctl disable --no-reload wol-firstboot.service >/dev/null 2>&1
    rm -f "$RETRY_UNIT" /etc/systemd/system/multi-user.target.wants/wol-firstboot.service
    # Läuft wol-wifi, übernimmt er die LED beim nächsten Durchlauf; sonst Standardanzeige
    systemctl is-active -q wol-wifi || led normal
    log "Einrichtung beendet: $STATUS"
    sleep 1
    # Nach einem Absturz stehen ggf. Nullbytes im Protokoll (nie geschriebene Blöcke)
    tr -d '\000' < "$LOG" > "$BOOT/wol-setup.log" 2>/dev/null || true
    sync
}

[ "$(id -u)" -eq 0 ] || { log "Muss als root laufen"; exit 1; }
exec 9> /run/wol-firstboot.lock
flock -n 9 || { log "Einrichtung läuft bereits"; exit 0; }
trap finish EXIT

log "WOL-Server: Einrichtung beim ersten Start"
[ -d "$SRC_BOOT/wolserver" ] || { log "$SRC_BOOT fehlt – nichts zu tun"; exit 1; }
touch "$MARKER"
led fast

# Wiederanlauf nach einem Abbruch: beim nächsten Start erneut ausführen, bis finish() die
# Einheit entfernt. sync, damit die Datei einen harten Reset sicher übersteht.
cat > "$RETRY_UNIT" <<EOF
[Unit]
Description=WOL-Server: Einrichtung nach Abbruch fortsetzen
ConditionPathExists=$SRC_BOOT/deploy/firstboot.sh
Wants=network-online.target
After=network-online.target NetworkManager.service userconfig.service

[Service]
Type=oneshot
ExecStart=/bin/bash $SRC_BOOT/deploy/firstboot.sh
TimeoutStartSec=infinity

[Install]
WantedBy=multi-user.target
EOF
systemctl enable --no-reload wol-firstboot.service >/dev/null 2>&1 || log "Wiederanlauf nicht einrichtbar"
sync

# ---------------------------------------------------------------- Einstellungen lesen
declare -A CFG=()
if [ -f "$BOOT/wol-setup.txt" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}
        [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
        if [[ "$line" =~ ^[[:space:]]*([A-Z_]+)=(.*)$ ]]; then
            CFG[${BASH_REMATCH[1]}]=${BASH_REMATCH[2]}
        else
            log "Zeile in wol-setup.txt nicht verstanden, ignoriert: ${line%%=*}"
        fi
    done < "$BOOT/wol-setup.txt"
else
    log "Keine wol-setup.txt gefunden – verwende Standardwerte"
fi

# setting NAME STANDARD: Wert ohne umgebende Anführungszeichen; außer bei Passwörtern auch ohne Leerraum
setting() {
    local value=${CFG[$1]:-}
    if [[ "$1" != *PASSWORD ]]; then
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
    fi
    if [[ ${#value} -ge 2 && ( "$value" == \"*\" || "$value" == \'*\' ) ]]; then
        value=${value:1:${#value}-2}
    fi
    printf '%s' "${value:-$2}"
}
yes_no() { [[ "$(setting "$1" "$2" | tr '[:upper:]' '[:lower:]')" =~ ^(yes|ja|true|1|on)$ ]]; }

HOST=$(setting HOSTNAME wol | tr '[:upper:]' '[:lower:]')
TZONE=$(setting TIMEZONE Europe/Berlin)
COUNTRY=$(setting COUNTRY DE | tr '[:lower:]' '[:upper:]')
GUI_PASSWORD=$(setting GUI_PASSWORD "")
WIFI_SSID=$(setting WIFI_SSID "")
WIFI_PASSWORD=$(setting WIFI_PASSWORD "")
ADMIN=$(setting ADMIN_USER admin)
ADMIN_PASSWORD=$(setting ADMIN_PASSWORD "")

# ---------------------------------------------------------------- Quellen auf ext4 kopieren
# Auf der FAT-Partition fehlen Unix-Rechte, und unter Windows bearbeitete Skripte haben CRLF.
rm -rf "$WORK"
cp -r "$SRC_BOOT" "$WORK"
find "$WORK" -name '*.sh' -exec sed -i 's/\r$//' {} +
chmod 0755 "$WORK"/deploy/*.sh

# ---------------------------------------------------------------- System
if [[ "$HOST" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
    log "Hostname: $HOST"
    hostnamectl set-hostname "$HOST"
    if grep -q '^127\.0\.1\.1' /etc/hosts; then
        sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$HOST/" /etc/hosts
    else
        printf '127.0.1.1\t%s\n' "$HOST" >> /etc/hosts
    fi
else
    log "Ungültiger HOSTNAME '$HOST' – Hostname bleibt $(hostname)"
fi

if [ -f "/usr/share/zoneinfo/$TZONE" ]; then
    log "Zeitzone: $TZONE"
    timedatectl set-timezone "$TZONE" 2>/dev/null || ln -sf "/usr/share/zoneinfo/$TZONE" /etc/localtime
else
    log "Unbekannte Zeitzone '$TZONE' – bleibt $(cat /etc/timezone 2>/dev/null || echo UTC)"
fi

if [[ "$COUNTRY" =~ ^[A-Z]{2}$ ]]; then
    log "WLAN-Land: $COUNTRY"
    raspi-config nonint do_wifi_country "$COUNTRY" || log "WLAN-Land konnte nicht gesetzt werden"
else
    log "Ungültiges COUNTRY '$COUNTRY' – verwende DE"
    COUNTRY=DE
    raspi-config nonint do_wifi_country DE || true
fi
if command -v rfkill >/dev/null; then rfkill unblock wifi || true; fi

# ---------------------------------------------------------------- Wartungszugang
# Raspberry Pi OS legt den Benutzer aus userconf.txt an (userconfig.service) – abwarten
for _ in $(seq 60); do
    [ "$(systemctl show -p ActiveState --value userconfig.service 2>/dev/null)" = activating ] || break
    sleep 2
done
first_user=$(getent passwd 1000 | cut -d: -f1)
if [ "$ADMIN" = root ]; then
    # Wartung direkt als root: kein Umbenennen, Schlüssel und Passwort gelten für root.
    # SSH erlaubt root nur mit Schlüssel (Debian-Standard PermitRootLogin prohibit-password).
    log "Wartungszugang als root"
elif [[ "$ADMIN" =~ ^[a-z][a-z0-9-]{0,31}$ ]] && [ -n "$first_user" ] && [ "$first_user" != "$ADMIN" ]; then
    if getent passwd "$ADMIN" >/dev/null; then
        log "Benutzer $ADMIN existiert schon als Systembenutzer – bleibe bei $first_user"
    else
        log "Benutzer $first_user → $ADMIN"
        /usr/lib/userconf-pi/userconf "$ADMIN" "" || log "Umbenennen fehlgeschlagen"
    fi
fi
[ "$ADMIN" = root ] || ADMIN=$(getent passwd 1000 | cut -d: -f1)
if [ -n "$ADMIN_PASSWORD" ]; then
    printf '%s:%s\n' "$ADMIN" "$ADMIN_PASSWORD" | chpasswd && log "Passwort für $ADMIN gesetzt"
fi
if [ -s "$BOOT/ssh_authorized_keys" ]; then
    home=$(getent passwd "$ADMIN" | cut -d: -f6)
    install -d -m 0700 -o "$ADMIN" -g "$ADMIN" "$home/.ssh"
    tr -d '\r' < "$BOOT/ssh_authorized_keys" > "$home/.ssh/authorized_keys"
    chown "$ADMIN:$ADMIN" "$home/.ssh/authorized_keys"
    chmod 0600 "$home/.ssh/authorized_keys"
    log "SSH-Schlüssel für $ADMIN übernommen ($(grep -c . "$home/.ssh/authorized_keys") Schlüssel)"
fi
if yes_no SSH no; then
    systemctl enable --no-reload --now ssh >/dev/null 2>&1 && log "SSH-Server aktiviert (Benutzer $ADMIN)"
    if [ ! -s "$BOOT/ssh_authorized_keys" ] && [ -z "$ADMIN_PASSWORD" ]; then
        log "Hinweis: SSH ist an, aber weder Schlüssel noch ADMIN_PASSWORD gesetzt – Login nicht möglich"
    elif [ "$ADMIN" = root ] && [ ! -s "$BOOT/ssh_authorized_keys" ]; then
        log "Hinweis: root darf sich per SSH nur mit Schlüssel anmelden – ADMIN_PASSWORD gilt nur an der Konsole"
    fi
fi

# ---------------------------------------------------------------- WLAN vorgeben
# Vor install.sh, damit der Hotspot-Dienst das WLAN beim Start schon kennt.
if [ -n "$WIFI_SSID" ]; then
    log "WLAN '$WIFI_SSID' eintragen"
    settings=(connection.autoconnect yes 802-11-wireless.powersave 2
              802-11-wireless.cloned-mac-address permanent)
    [ -n "$WIFI_PASSWORD" ] && settings+=(wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$WIFI_PASSWORD")
    if nmcli connection show id "$WIFI_SSID" >/dev/null 2>&1; then
        # Wiederanlauf: Profil besteht schon (und trägt evtl. gerade die Verbindung)
        args=(connection modify id "$WIFI_SSID" "${settings[@]}")
    else
        args=(connection add type wifi ifname wlan0 con-name "$WIFI_SSID" ssid "$WIFI_SSID" "${settings[@]}")
    fi
    nmcli "${args[@]}" >/dev/null || log "WLAN-Profil konnte nicht angelegt werden – Einrichtung dann über den Hotspot"
fi

# ---------------------------------------------------------------- WOL-Server
log "Installiere den WOL-Server"
if ! bash "$WORK/deploy/install.sh" --no-password --country "$COUNTRY"; then
    log "install.sh ist fehlgeschlagen"
    exit 1
fi
sync
if [ -n "$GUI_PASSWORD" ]; then
    if printf '%s\n' "$GUI_PASSWORD" | /usr/local/bin/wol-server passwd --stdin >/dev/null; then
        log "GUI-Passwort gesetzt"
    else
        log "GUI-Passwort ungültig (mindestens 8 Zeichen) – bitte in der GUI festlegen"
    fi
fi

# ---------------------------------------------------------------- OpenVPN (sobald Internet da ist)
if [ -s "$BOOT/openvpn.ovpn" ]; then
    log "OpenVPN-Konfiguration gefunden – wird installiert, sobald Internet verfügbar ist"
    install -d -m 0700 /etc/wol-server/firstboot
    install -m 0600 "$BOOT/openvpn.ovpn" /etc/wol-server/firstboot/client.ovpn
    if [ -s "$BOOT/openvpn.auth" ]; then
        install -d -m 0755 /etc/openvpn/client
        tr -d '\r' < "$BOOT/openvpn.auth" > /etc/openvpn/client/wol.auth
        chmod 0600 /etc/openvpn/client/wol.auth
    fi
    cat > /usr/local/sbin/wol-openvpn-pending <<'EOF'
#!/bin/sh
# Installiert OpenVPN und richtet den Client ein, sobald Internet da ist (einmalig nach dem Erststart).
PENDING=/etc/wol-server/firstboot/client.ovpn
[ -f "$PENDING" ] || exit 0
apt-get update -q --error-on=any || exit 1   # noch kein Internet – systemd versucht es später erneut
/opt/wol-server/deploy/openvpn-setup.sh "$PENDING" || true   # Tunnel darf auch später erst stehen
if command -v openvpn >/dev/null && [ -f /etc/openvpn/client/wol.conf ]; then
    rm -rf /etc/wol-server/firstboot
    systemctl disable wol-openvpn-pending.service
    exit 0
fi
exit 1
EOF
    chmod 0755 /usr/local/sbin/wol-openvpn-pending
    cat > /etc/systemd/system/wol-openvpn-pending.service <<'EOF'
[Unit]
Description=WOL-Server: OpenVPN einrichten, sobald Internet verfügbar ist
Wants=network-online.target
After=network-online.target wol-server.service
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/wol-openvpn-pending
Restart=on-failure
RestartSec=120

[Install]
WantedBy=multi-user.target
EOF
    systemctl enable wol-openvpn-pending.service >/dev/null   # lädt selbst neu
    systemctl start --no-block wol-openvpn-pending.service
fi

# ---------------------------------------------------------------- Abschluss
# cloud-init wird nicht mehr gebraucht; abgeschaltet startet der Zero spürbar schneller.
touch /etc/cloud/cloud-init.disabled

STATUS="OK – GUI unter http://$(hostname).local/"
if yes_no LOWPOWER yes; then
    log "Stromsparmodus einrichten"
    if bash /opt/wol-server/deploy/lowpower.sh; then
        STATUS="$STATUS (Neustart für den Stromsparmodus in 1 min)"
        shutdown -r +1 "WOL-Server eingerichtet – Neustart für den Stromsparmodus" || true
    else
        log "lowpower.sh fehlgeschlagen – Pi läuft ohne Stromsparmodus weiter"
    fi
fi
exit 0
