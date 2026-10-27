#!/bin/bash
# Makes Wi-Fi-only operation of the Pi Zero W robust (called by install.sh).
#
#   sudo ./deploy/wlan-setup.sh [--iface wlan0] [--reboot-after 30] [--no-hw-watchdog]
#
# - Wi-Fi powersave off: with powersave the Zero misses incoming connections
#   and broadcasts. It costs only a few mA.
# - Fixed MAC address: NetworkManager joins every Wi-Fi with the hardware MAC, never
#   with a random one - otherwise DHCP reservations and router rules do not match.
# - Wi-Fi watchdog: checks the gateway every minute. After 3 failures Wi-Fi is
#   reconnected, after --reboot-after minutes without network the Pi reboots (0 = never).
# - Hardware watchdog: reboots the Pi if the system hangs completely.
set -euo pipefail

IFACE=wlan0
REBOOT_AFTER=30
HW_WATCHDOG=1
while [ $# -gt 0 ]; do
    case "$1" in
        --iface) IFACE="$2"; shift 2 ;;
        --reboot-after) REBOOT_AFTER="$2"; shift 2 ;;
        --no-hw-watchdog) HW_WATCHDOG=0; shift ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo." >&2; exit 1; }
[[ "$IFACE" =~ ^[a-z0-9]+$ ]] || { echo "Invalid interface: $IFACE" >&2; exit 1; }
[[ "$REBOOT_AFTER" =~ ^[0-9]+$ ]] || { echo "--reboot-after: minutes as a number" >&2; exit 1; }
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- Powersave
say "Disabling Wi-Fi powersave on $IFACE"
if [ -d /etc/NetworkManager/conf.d ]; then
    cat > /etc/NetworkManager/conf.d/60-wol-server-wifi-powersave.conf <<'EOF'
[connection]
wifi.powersave = 2
EOF
    say "Fixed MAC address for all Wi-Fi connections"
    cat > /etc/NetworkManager/conf.d/61-wol-server-wifi-mac.conf <<'EOF'
[connection]
wifi.cloned-mac-address = permanent
EOF
fi
cat > /etc/systemd/system/wifi-powersave-off@.service <<'EOF'
[Unit]
Description=Disable Wi-Fi powersave on %i
After=sys-subsystem-net-devices-%i.device network-online.target
BindsTo=sys-subsystem-net-devices-%i.device

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'command -v iw >/dev/null && iw dev %i set power_save off || true'

[Install]
WantedBy=sys-subsystem-net-devices-%i.device
EOF
# Remove the old single unit from earlier versions of lowpower.sh
if [ -f /etc/systemd/system/wifi-powersave-off.service ]; then
    systemctl disable wifi-powersave-off.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/wifi-powersave-off.service
fi
# 2>/dev/null: notice "dependency to a non-existent unit" while udev has not reported the device yet
systemctl enable --no-reload "wifi-powersave-off@$IFACE.service" >/dev/null 2>&1
if command -v iw >/dev/null; then
    iw dev "$IFACE" set power_save off 2>/dev/null || true
fi

# ---------------------------------------------------------------- Watchdog
say "Wi-Fi watchdog (reconnect after 3 min, reboot after $REBOOT_AFTER min without network)"
cat > /etc/default/wol-wlan-watchdog <<EOF
# Configuration of the Wi-Fi watchdog (wol-server)
IFACE=$IFACE
# Optional fixed check host instead of the gateway, e.g. CHECK_HOST=192.168.178.1
CHECK_HOST=
# Consecutive failures (1 per minute) after which Wi-Fi is reconnected
RECONNECT_AFTER=3
# Minutes without network until reboot; 0 = never. Reboot no earlier than 60 min after boot.
REBOOT_AFTER=$REBOOT_AFTER
EOF

cat > /usr/local/sbin/wol-wlan-watchdog <<'EOF'
#!/bin/sh
# Checks the Wi-Fi connection; called every minute by wol-wlan-watchdog.timer.
IFACE=wlan0; CHECK_HOST=; RECONNECT_AFTER=3; REBOOT_AFTER=30
[ -r /etc/default/wol-wlan-watchdog ] && . /etc/default/wol-wlan-watchdog
STATE=/run/wol-wlan-watchdog.fails

# Setup hotspot active (service wol-wifi): there is no gateway, and a reconnect or reboot
# would interrupt the setup. Here the hotspot replaces the reboot.
if ip -4 addr show dev "$IFACE" 2>/dev/null | grep -q "inet 10\.42\.0\.1/"; then
    rm -f "$STATE"
    exit 0
fi

target=$CHECK_HOST
[ -n "$target" ] || target=$(ip -4 route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')

if [ -n "$target" ] && ping -n -q -c 2 -W 3 -I "$IFACE" "$target" >/dev/null 2>&1; then
    if [ -f "$STATE" ]; then
        logger -t wlan-watchdog "Connection restored ($target)"
        rm -f "$STATE"
    fi
    exit 0
fi

fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"
logger -t wlan-watchdog "No connection via $IFACE to ${target:-<no gateway>} (attempt $fails)"

if [ $((fails % RECONNECT_AFTER)) -eq 0 ]; then
    logger -t wlan-watchdog "Reconnecting $IFACE"
    if command -v nmcli >/dev/null && nmcli -t -f RUNNING general 2>/dev/null | grep -q running; then
        nmcli radio wifi off; sleep 3; nmcli radio wifi on
    else
        ip link set "$IFACE" down; sleep 3; ip link set "$IFACE" up
    fi
fi

uptime_s=$(cut -d. -f1 /proc/uptime)
if [ "$REBOOT_AFTER" -gt 0 ] && [ "$fails" -ge "$REBOOT_AFTER" ] && [ "$uptime_s" -ge 3600 ]; then
    logger -t wlan-watchdog "No network for $fails min - rebooting"
    systemctl reboot
fi
EOF
chmod 0755 /usr/local/sbin/wol-wlan-watchdog

cat > /etc/systemd/system/wol-wlan-watchdog.service <<'EOF'
[Unit]
Description=Check the Wi-Fi connection and restore it if needed
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/wol-wlan-watchdog
Nice=10
EOF
cat > /etc/systemd/system/wol-wlan-watchdog.timer <<'EOF'
[Unit]
Description=Wi-Fi watchdog every minute

[Timer]
OnBootSec=3min
OnUnitActiveSec=1min
AccuracySec=15s

[Install]
WantedBy=timers.target
EOF
# A single reload for all units of this script (takes 12-20 s on the Zero)
systemctl daemon-reload
systemctl enable --no-reload wol-wlan-watchdog.timer >/dev/null
systemctl start wol-wlan-watchdog.timer

# ---------------------------------------------------------------- Hardware-Watchdog
if [ "$HW_WATCHDOG" -eq 1 ]; then
    say "Enabling hardware watchdog (reboot on system hang)"
    install -d /etc/systemd/system.conf.d
    cat > /etc/systemd/system.conf.d/60-wol-server-watchdog.conf <<'EOF'
[Manager]
# Same as the Raspberry Pi OS default. Not shorter: under full SD card load (fstrim, apt)
# systemd on the Zero can block for over 10 s - a tight watchdog then resets the Pi in the
# middle of its work. The kernel bridges the 15 s limit of the BCM2835 hardware.
RuntimeWatchdogSec=1min
RebootWatchdogSec=2min
EOF
    # No daemon-reexec: the next daemon-reload (or the next boot) already reads the setting;
    # it must not disturb a running first boot.
    sync
fi

say "Wi-Fi hardening set up"
