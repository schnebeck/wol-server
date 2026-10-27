#!/bin/bash
# WOL-Server - deploy/firstboot.sh
# First-boot setup of a WOL-Server SD card image.
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
# First-boot setup of a WOL-Server image.
#
# Called by cloud-init (deploy/firstboot/user-data on the boot partition); reads the settings
# from /boot/firmware/wol-setup.txt. No Internet required: Raspberry Pi OS Lite (Trixie) ships
# all packages. Only OpenVPN is installed later, once Internet is available.
#
# Afterwards the settings, VPN keys and sources are deleted from the boot partition and the
# log is stored there as wol-setup.log.
#
# cloud-init runs the script only once. If the Pi aborts halfway (power loss, hang and
# hardware watchdog), wol-firstboot.service resumes the setup on the next boot; every step
# therefore tolerates being repeated.
set -uo pipefail

BOOT=/boot/firmware
SRC_BOOT="$BOOT/wol-server"
WORK=/var/tmp/wol-server-src
LOG=/var/log/wol-firstboot.log
STATUS="FAILED"
MARKER=/run/wol-firstboot  # while it exists, the LED blinks fast (also via wol-wifi)
RETRY_UNIT=/etc/systemd/system/wol-firstboot.service

# The log is written to the boot partition right away: if the setup hangs, it can be read
# with a card reader. finish() writes the cleaned-up version at the end.
exec > >(tee -a "$LOG" "$BOOT/wol-setup.log") 2>&1
log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$*"
    sync -f "$BOOT/wol-setup.log" 2>/dev/null
}

# Status LED: "fast" = fast blinking, "normal" = default indication (SD access)
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

# shellcheck disable=SC2317  # called via trap
finish() {
    systemctl stop wol-boottrace.service >/dev/null 2>&1   # boot trace from user-data
    # Remove secrets from the FAT partition - even if something went wrong
    rm -f "$BOOT/wol-setup.txt" "$BOOT/openvpn.ovpn" "$BOOT/openvpn.auth" "$BOOT/ssh_authorized_keys"
    rm -rf "$SRC_BOOT" "$WORK"
    rm -f "$MARKER"
    systemctl disable --no-reload wol-firstboot.service >/dev/null 2>&1
    rm -f "$RETRY_UNIT" /etc/systemd/system/multi-user.target.wants/wol-firstboot.service
    # If wol-wifi is running, it takes over the LED on its next pass; otherwise default indication
    systemctl is-active -q wol-wifi || led normal
    log "Setup finished: $STATUS"
    sleep 1
    # After a crash the log may contain null bytes (blocks never written)
    tr -d '\000' < "$LOG" > "$BOOT/wol-setup.log" 2>/dev/null || true
    sync
}

[ "$(id -u)" -eq 0 ] || { log "Must run as root"; exit 1; }
exec 9> /run/wol-firstboot.lock
flock -n 9 || { log "Setup is already running"; exit 0; }
trap finish EXIT

log "WOL-Server: first-boot setup"
[ -d "$SRC_BOOT/wolserver" ] || { log "$SRC_BOOT missing - nothing to do"; exit 1; }
touch "$MARKER"
led fast

# Resume after an abort: run again on the next boot until finish() removes the unit.
# sync so the file safely survives a hard reset.
cat > "$RETRY_UNIT" <<EOF
[Unit]
Description=WOL-Server: resume setup after an abort
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
systemctl enable --no-reload wol-firstboot.service >/dev/null 2>&1 || log "Could not set up resume after abort"
sync

# ---------------------------------------------------------------- Read settings
declare -A CFG=()
if [ -f "$BOOT/wol-setup.txt" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}
        [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
        if [[ "$line" =~ ^[[:space:]]*([A-Z_]+)=(.*)$ ]]; then
            CFG[${BASH_REMATCH[1]}]=${BASH_REMATCH[2]}
        else
            log "Line in wol-setup.txt not understood, ignored: ${line%%=*}"
        fi
    done < "$BOOT/wol-setup.txt"
else
    log "No wol-setup.txt found - using defaults"
fi

# setting NAME DEFAULT: value without surrounding quotes; except for passwords also without surrounding whitespace
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

# ---------------------------------------------------------------- Copy sources to ext4
# The FAT partition lacks Unix permissions, and scripts edited on Windows have CRLF.
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
    log "Invalid HOSTNAME '$HOST' - hostname stays $(hostname)"
fi

if [ -f "/usr/share/zoneinfo/$TZONE" ]; then
    log "Time zone: $TZONE"
    timedatectl set-timezone "$TZONE" 2>/dev/null || ln -sf "/usr/share/zoneinfo/$TZONE" /etc/localtime
else
    log "Unknown time zone '$TZONE' - stays $(cat /etc/timezone 2>/dev/null || echo UTC)"
fi

if [[ "$COUNTRY" =~ ^[A-Z]{2}$ ]]; then
    log "Wi-Fi country: $COUNTRY"
    raspi-config nonint do_wifi_country "$COUNTRY" || log "Could not set Wi-Fi country"
else
    log "Invalid COUNTRY '$COUNTRY' - using DE"
    COUNTRY=DE
    raspi-config nonint do_wifi_country DE || true
fi
if command -v rfkill >/dev/null; then rfkill unblock wifi || true; fi

# ---------------------------------------------------------------- Maintenance access
# Raspberry Pi OS creates the user from userconf.txt (userconfig.service) - wait for it
for _ in $(seq 60); do
    [ "$(systemctl show -p ActiveState --value userconfig.service 2>/dev/null)" = activating ] || break
    sleep 2
done
first_user=$(getent passwd 1000 | cut -d: -f1)
if [ "$ADMIN" = root ]; then
    # Maintenance directly as root: no renaming, key and password apply to root.
    # SSH allows root only with a key (Debian default PermitRootLogin prohibit-password).
    log "Maintenance access as root"
elif [[ "$ADMIN" =~ ^[a-z][a-z0-9-]{0,31}$ ]] && [ -n "$first_user" ] && [ "$first_user" != "$ADMIN" ]; then
    if getent passwd "$ADMIN" >/dev/null; then
        log "User $ADMIN already exists as a system user - keeping $first_user"
    else
        log "User $first_user -> $ADMIN"
        /usr/lib/userconf-pi/userconf "$ADMIN" "" || log "Renaming failed"
    fi
fi
[ "$ADMIN" = root ] || ADMIN=$(getent passwd 1000 | cut -d: -f1)
if [ -n "$ADMIN_PASSWORD" ]; then
    printf '%s:%s\n' "$ADMIN" "$ADMIN_PASSWORD" | chpasswd && log "Password set for $ADMIN"
fi
if [ -s "$BOOT/ssh_authorized_keys" ]; then
    home=$(getent passwd "$ADMIN" | cut -d: -f6)
    install -d -m 0700 -o "$ADMIN" -g "$ADMIN" "$home/.ssh"
    tr -d '\r' < "$BOOT/ssh_authorized_keys" > "$home/.ssh/authorized_keys"
    chown "$ADMIN:$ADMIN" "$home/.ssh/authorized_keys"
    chmod 0600 "$home/.ssh/authorized_keys"
    log "SSH keys installed for $ADMIN ($(grep -c . "$home/.ssh/authorized_keys") keys)"
fi
if yes_no SSH no; then
    systemctl enable --no-reload --now ssh >/dev/null 2>&1 && log "SSH server enabled (user $ADMIN)"
    if [ ! -s "$BOOT/ssh_authorized_keys" ] && [ -z "$ADMIN_PASSWORD" ]; then
        log "Note: SSH is on, but neither a key nor ADMIN_PASSWORD is set - login not possible"
    elif [ "$ADMIN" = root ] && [ ! -s "$BOOT/ssh_authorized_keys" ]; then
        log "Note: root may log in via SSH only with a key - ADMIN_PASSWORD applies only on the console"
    fi
fi

# ---------------------------------------------------------------- Preset Wi-Fi
# Before install.sh so the hotspot service already knows the Wi-Fi when it starts.
if [ -n "$WIFI_SSID" ]; then
    log "Adding Wi-Fi '$WIFI_SSID'"
    settings=(connection.autoconnect yes 802-11-wireless.powersave 2
              802-11-wireless.cloned-mac-address permanent)
    [ -n "$WIFI_PASSWORD" ] && settings+=(wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$WIFI_PASSWORD")
    if nmcli connection show id "$WIFI_SSID" >/dev/null 2>&1; then
        # Resume: profile already exists (and may be carrying the connection right now)
        args=(connection modify id "$WIFI_SSID" "${settings[@]}")
    else
        args=(connection add type wifi ifname wlan0 con-name "$WIFI_SSID" ssid "$WIFI_SSID" "${settings[@]}")
    fi
    nmcli "${args[@]}" >/dev/null || log "Could not create Wi-Fi profile - set up via the hotspot instead"
fi

# ---------------------------------------------------------------- WOL-Server
log "Installing WOL-Server"
if ! bash "$WORK/deploy/install.sh" --no-password --country "$COUNTRY"; then
    log "install.sh failed"
    exit 1
fi
sync
if [ -n "$GUI_PASSWORD" ]; then
    if printf '%s\n' "$GUI_PASSWORD" | /usr/local/bin/wol-server passwd --stdin >/dev/null; then
        log "GUI password set"
    else
        log "GUI password invalid (at least 8 characters) - please set it in the GUI"
    fi
fi

# ---------------------------------------------------------------- OpenVPN (once Internet is available)
if [ -s "$BOOT/openvpn.ovpn" ]; then
    log "OpenVPN configuration found - will be installed once Internet is available"
    install -d -m 0700 /etc/wol-server/firstboot
    install -m 0600 "$BOOT/openvpn.ovpn" /etc/wol-server/firstboot/client.ovpn
    if [ -s "$BOOT/openvpn.auth" ]; then
        install -d -m 0755 /etc/openvpn/client
        tr -d '\r' < "$BOOT/openvpn.auth" > /etc/openvpn/client/wol.auth
        chmod 0600 /etc/openvpn/client/wol.auth
    fi
    cat > /usr/local/sbin/wol-openvpn-pending <<'EOF'
#!/bin/sh
# Installs OpenVPN and sets up the client once Internet is available (once, after the first boot).
PENDING=/etc/wol-server/firstboot/client.ovpn
[ -f "$PENDING" ] || exit 0
apt-get update -q --error-on=any || exit 1   # no Internet yet - systemd retries later
/opt/wol-server/deploy/openvpn-setup.sh "$PENDING" || true   # tunnel may come up later
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
Description=WOL-Server: set up OpenVPN once Internet is available
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
    systemctl enable wol-openvpn-pending.service >/dev/null   # reloads by itself
    systemctl start --no-block wol-openvpn-pending.service
fi

# ---------------------------------------------------------------- Finish
# cloud-init is no longer needed; with it disabled the Zero boots noticeably faster.
touch /etc/cloud/cloud-init.disabled

STATUS="OK - GUI at http://$(hostname).local/"
# Driver blacklists, swap and firmware settings take effect only after a reboot
log "Setting up low-memory mode"
bash /opt/wol-server/deploy/lowmem.sh || log "lowmem.sh failed - Pi continues without low-memory mode"
if yes_no LOWPOWER yes; then
    log "Setting up low-power mode"
    bash /opt/wol-server/deploy/lowpower.sh || log "lowpower.sh failed - Pi continues without low-power mode"
fi
STATUS="$STATUS (reboot in 1 min)"
shutdown -r +1 "WOL-Server set up - rebooting for low-memory and low-power mode" || true
exit 0
