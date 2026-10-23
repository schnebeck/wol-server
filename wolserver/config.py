"""Laden der Server-Konfiguration (JSON)."""

import json
import logging
import os

log = logging.getLogger("wolserver")

DEFAULT_CONFIG_PATH = "/etc/wol-server/config.json"

DEFAULTS = {
    # "::" lauscht dual-stack auf IPv4 und IPv6; fällt bei fehlendem IPv6 auf 0.0.0.0 zurück.
    "listen": "::",
    "port": 80,
    "data_dir": "/var/lib/wol-server",
    # Nur Clients aus diesen Netzen dürfen die GUI/API nutzen (Intranet + VPN).
    "allowed_networks": [
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",  # CGNAT, u. a. Tailscale
        "fc00::/7",  # IPv6 ULA, u. a. Tailscale/WireGuard
        "fe80::/10",
    ],
    # Interface für Magic Packets; leer = automatisch (Default-Route, VPN-Interfaces ausgenommen)
    "wol_interface": "",
    # WLAN-Verwaltung (Dienst wol-wifi): Einrichtungs-Hotspot, wenn kein bekanntes WLAN erreichbar ist
    "wifi_interface": "wlan0",
    "hotspot_enabled": True,
    "hotspot_ssid": "",  # leer = "WOL-Setup-XXXX" (Ende der MAC-Adresse)
    "hotspot_delay_minutes": 5,  # so lange ohne WLAN, bis der Hotspot startet
    "hotspot_retry_minutes": 5,  # Abstand, in dem der Hotspot nach bekannten WLANs sucht
    "wifi_socket": "/run/wol-wifi/control.sock",
    "session_days": 30,
    "status_cache_seconds": 5,
    "log_level": "INFO",
}


def load_config(path=None):
    path = path or os.environ.get("WOL_CONFIG") or DEFAULT_CONFIG_PATH
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError("%s: Konfiguration muss ein JSON-Objekt sein" % path)
        for key in sorted(set(user) - set(DEFAULTS)):
            log.warning("Unbekannte Konfigurationsoption ignoriert: %s", key)
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    else:
        log.warning("Konfigurationsdatei %s nicht gefunden – verwende Standardwerte", path)
    if not os.path.isabs(cfg["data_dir"]):
        base = os.path.dirname(os.path.abspath(path))
        cfg["data_dir"] = os.path.normpath(os.path.join(base, cfg["data_dir"]))
    cfg["config_path"] = path
    return cfg
