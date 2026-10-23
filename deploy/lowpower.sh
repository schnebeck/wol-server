#!/bin/bash
# Optimiert einen Raspberry Pi Zero W für den Headless-Dauerbetrieb mit wenig Strom
# und wenig SD-Karten-Verschleiß. Mehrfach ausführbar; config.txt wird vorher gesichert.
#
#   sudo ./deploy/lowpower.sh [--led-off] [--keep-hdmi] [--keep-bluetooth]
#
# Danach neu starten. Rückgängig: Block "# >>> wol-server lowpower" aus config.txt
# entfernen (oder Backup zurückspielen) und die genannten Dienste wieder aktivieren.
set -euo pipefail

LED_OFF=0
KEEP_HDMI=0
KEEP_BT=0
for arg in "$@"; do
    case "$arg" in
        --led-off) LED_OFF=1 ;;
        --keep-hdmi) KEEP_HDMI=1 ;;
        --keep-bluetooth) KEEP_BT=1 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "Unbekannte Option: $arg" >&2; exit 1 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

CONFIG=/boot/firmware/config.txt
[ -f "$CONFIG" ] || CONFIG=/boot/config.txt
[ -f "$CONFIG" ] || { echo "config.txt nicht gefunden – ist das ein Raspberry Pi?" >&2; exit 1; }

# ---------------------------------------------------------------- config.txt
say "Firmware-Einstellungen in $CONFIG"
cp -a "$CONFIG" "$CONFIG.bak-$(date +%Y%m%d-%H%M%S)"
sed -i '/^# >>> wol-server lowpower/,/^# <<< wol-server lowpower/d' "$CONFIG"

if [ "$KEEP_HDMI" -eq 0 ]; then
    # Headless braucht keinen KMS-Grafiktreiber: spart RAM und erlaubt das Abschalten von HDMI.
    sed -i 's/^dtoverlay=vc4-kms-v3d/#dtoverlay=vc4-kms-v3d  # deaktiviert von wol-server lowpower/' "$CONFIG"
    sed -i 's/^max_framebuffers=/#max_framebuffers=/' "$CONFIG"
fi

{
    echo "# >>> wol-server lowpower"
    echo "[all]"
    echo "dtparam=audio=off"
    echo "camera_auto_detect=0"
    echo "display_auto_detect=0"
    [ "$KEEP_HDMI" -eq 0 ] && echo "gpu_mem=16"
    [ "$KEEP_BT" -eq 0 ] && echo "dtoverlay=disable-bt"
    if [ "$LED_OFF" -eq 1 ]; then
        echo "dtparam=act_led_trigger=none"
        echo "dtparam=act_led_activelow=on"
    fi
    echo "# <<< wol-server lowpower"
} >> "$CONFIG"

# ---------------------------------------------------------------- HDMI aus
if [ "$KEEP_HDMI" -eq 0 ]; then
    say "HDMI beim Booten abschalten (~25 mA)"
    cat > /etc/systemd/system/hdmi-off.service <<'EOF'
[Unit]
Description=HDMI-Ausgang abschalten (headless)
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'command -v vcgencmd >/dev/null && vcgencmd display_power 0 >/dev/null || true'

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable hdmi-off.service >/dev/null
fi

# ---------------------------------------------------------------- Dienste
say "Nicht benötigte Dienste deaktivieren"
services=(triggerhappy.service ModemManager.service)
[ "$KEEP_BT" -eq 0 ] && services+=(hciuart.service bluetooth.service)
for svc in "${services[@]}"; do
    if systemctl list-unit-files "$svc" >/dev/null 2>&1 && systemctl cat "$svc" >/dev/null 2>&1; then
        systemctl disable --now "$svc" >/dev/null 2>&1 || true
        echo "    $svc deaktiviert"
    fi
done

# ---------------------------------------------------------------- SD-Karte schonen
say "Journal nur im RAM halten"
install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/60-wol-server-volatile.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=16M
EOF
systemctl restart systemd-journald

if systemctl cat dphys-swapfile.service >/dev/null 2>&1; then
    say "Swap-Datei auf der SD-Karte abschalten"
    dphys-swapfile swapoff >/dev/null 2>&1 || true
    systemctl disable --now dphys-swapfile.service >/dev/null 2>&1 || true
fi

# WLAN-Powersave bleibt bewusst AUS – das erledigt wlan-setup.sh (Erreichbarkeit geht vor).

echo
say "Fertig. Bitte neu starten: sudo reboot"
