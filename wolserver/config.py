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
    # HTTPS (0 = aus). Zertifikat: tls_dir/cert.pem und key.pem, abgeholt per `wol-server cert-fetch`
    "https_port": 443,
    "tls_dir": "/etc/wol-server/tls",
    "https_host": "",  # Name im Zertifikat, z. B. wol.home.arpa; leer = erster Name im Zertifikat
    "https_redirect": True,  # HTTP auf HTTPS umleiten, sobald ein gültiges Zertifikat da ist (nicht im Hotspot)
    "cert_source": "",  # SSH-Ziel, das Schlüssel und Zertifikat liefert, z. B. wolcert@10.1.1.3
    "data_dir": "/var/lib/wol-server",
    # Nur Clients aus diesen Netzen dürfen die GUI/API nutzen. "auto" = localhost, IPv6-Link-local und
    # die Netze, an denen der Server gerade hängt (LAN/WLAN, VPN); weitere Netze lassen sich ergänzen.
    "allowed_networks": ["auto"],
    # Interface für Magic Packets; leer = automatisch (Default-Route, VPN-Interfaces ausgenommen)
    "wol_interface": "",
    # WLAN-Verwaltung (Dienst wol-wifi): Einrichtungs-Hotspot, wenn kein bekanntes WLAN erreichbar ist
    "wifi_interface": "wlan0",
    "hotspot_enabled": True,
    "hotspot_ssid": "",  # leer = "WOL-Setup-XXXX" (Ende der MAC-Adresse)
    "hotspot_delay_minutes": 5,  # so lange ohne WLAN, bis der Hotspot startet
    "hotspot_retry_minutes": 5,  # Abstand, in dem der Hotspot nach bekannten WLANs sucht
    "wifi_socket": "/run/wol-wifi/control.sock",
    "status_led": True,
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
