# WOL-Server - wolserver/config.py
# Loads the server configuration (JSON) with defaults.
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

"""Loading the server configuration (JSON)."""

import json
import logging
import os

log = logging.getLogger("wolserver")

DEFAULT_CONFIG_PATH = "/etc/wol-server/config.json"

DEFAULTS = {
    # "::" listens dual-stack on IPv4 and IPv6; falls back to 0.0.0.0 if IPv6 is missing.
    "listen": "::",
    "port": 80,
    # HTTPS (0 = off). Certificate: tls_dir/cert.pem and key.pem, fetched via `wol-server cert-fetch`
    "https_port": 443,
    "tls_dir": "/etc/wol-server/tls",
    "https_host": "",  # name in the certificate, e.g. wol.home.arpa; empty = first name in the certificate
    "https_redirect": True,  # redirect HTTP to HTTPS once a valid certificate exists (not in the hotspot)
    "cert_source": "",  # SSH target that delivers key and certificate, e.g. wolcert@10.1.1.3
    "data_dir": "/var/lib/wol-server",
    # Only clients from these networks may use the GUI/API. "auto" = localhost, IPv6 link-local and
    # the networks the server is currently attached to (LAN/Wi-Fi, VPN); more networks can be added.
    "allowed_networks": ["auto"],
    # interface for magic packets; empty = automatic (default route, VPN interfaces excluded)
    "wol_interface": "",
    # Wi-Fi management (wol-wifi service): setup hotspot when no known Wi-Fi is reachable
    "wifi_interface": "wlan0",
    "hotspot_enabled": True,
    "hotspot_ssid": "",  # empty = "WOL-Setup-XXXX" (end of the MAC address)
    "hotspot_delay_minutes": 5,  # time without Wi-Fi until the hotspot starts
    "hotspot_retry_minutes": 5,  # interval at which the hotspot looks for known Wi-Fi networks
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
            raise ValueError("%s: configuration must be a JSON object" % path)
        for key in sorted(set(user) - set(DEFAULTS)):
            log.warning("Unknown configuration option ignored: %s", key)
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    else:
        log.warning("Configuration file %s not found - using defaults", path)
    if not os.path.isabs(cfg["data_dir"]):
        base = os.path.dirname(os.path.abspath(path))
        cfg["data_dir"] = os.path.normpath(os.path.join(base, cfg["data_dir"]))
    cfg["config_path"] = path
    return cfg
