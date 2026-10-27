# WOL-Server - wolserver/vpn.py
# OpenVPN client setup from the web GUI (part of wol-wifi).
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

"""Set up the OpenVPN client via the web GUI (part of the root service wol-wifi).

The service checks uploaded configurations and stores them together with a job in its own
state directory. The actual setup (package, /etc/openvpn, systemd) is done by
wol-vpn.service, which wol-vpn.path starts when the job file is written. This way
wol-wifi itself needs neither write access to /etc nor any rights towards systemd.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time

from . import netinfo

log = logging.getLogger("wolserver.vpn")

NAME = "wol"
CONF = "/etc/openvpn/client/%s.conf" % NAME
UNIT = "openvpn-client@%s.service" % NAME
JOB_UNIT = "wol-vpn.service"
MAX_CONFIG = 48 * 1024

# An .ovpn file runs with root privileges. These options run programs, load code or
# write files to arbitrary places - they are not accepted from an uploaded file.
FORBIDDEN = {
    "up", "down", "route-up", "route-pre-down", "ipchange", "tls-verify", "auth-user-pass-verify",
    "client-connect", "client-disconnect", "learn-address", "script-security", "plugin", "iproute",
    "log", "log-append", "status", "writepid", "cd", "chroot", "daemon", "config", "management",
    "askpass", "tmp-dir", "syslog", "dev-node", "secret", "up-delay", "down-pre", "setenv-safe",
}
FORBIDDEN_PREFIXES = ("management-",)
# references to additional files: only a single file can be uploaded
FILE_OPTIONS = {"ca", "cert", "key", "tls-auth", "tls-crypt", "tls-crypt-v2", "pkcs12", "dh",
                "extra-certs", "crl-verify", "http-proxy-user-pass"}
INLINE_TAG = re.compile(r"^\s*<(/?)([a-z0-9-]+)>\s*$", re.I)


class VpnError(Exception):
    pass


def parse_config(text):
    """Checks a client configuration. Returns (cleaned text, info) or raises VpnError."""
    if not isinstance(text, str) or not text.strip():
        raise VpnError("The file is empty.")
    if len(text.encode("utf-8")) > MAX_CONFIG:
        raise VpnError("The file is too large (max. %d KB)." % (MAX_CONFIG // 1024))
    if "\x00" in text:
        raise VpnError("This is not a text file.")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, inline, remotes = [], None, []
    is_client = needs_auth = False
    for number, line in enumerate(lines, 1):
        tag = INLINE_TAG.match(line)
        if inline:
            if tag and tag.group(1) and tag.group(2).lower() == inline:
                inline = None
            out.append(line)
            continue
        if tag and not tag.group(1):
            inline = tag.group(2).lower()
            out.append(line)
            continue
        words = line.split()
        if not words or words[0][0] in "#;":
            out.append(line)
            continue
        option = words[0].lower().lstrip("-")
        if option in FORBIDDEN or option.startswith(FORBIDDEN_PREFIXES):
            raise VpnError("Line %d: \"%s\" is not allowed for security reasons "
                           "(would run programs or write files with root privileges)." % (number, words[0]))
        if option in FILE_OPTIONS and len(words) > 1 and words[1].lower() != "[inline]":
            raise VpnError("Line %d: \"%s\" refers to the file \"%s\". Use a configuration with "
                           "embedded certificates (<%s>...</%s>)." % (number, words[0], words[1],
                                                                     option, option))
        if option in ("client", "tls-client"):
            is_client = True
        elif option == "remote" and len(words) > 1:
            remotes.append(words[1] + (":" + words[2] if len(words) > 2 and words[2].isdigit() else ""))
        elif option == "auth-user-pass":
            needs_auth = True
            line = "auth-user-pass"  # the setup provides the file
        out.append(line)
    if inline:
        raise VpnError("The block <%s> is not closed." % inline)
    if not is_client:
        raise VpnError("This is not a client configuration (\"client\" is missing).")
    if not remotes:
        raise VpnError("The configuration lacks the server (\"remote ...\").")
    return "\n".join(out).strip("\n") + "\n", {"remote": remotes[0], "needs_auth": needs_auth}


def validate_credential(value, what):
    if not isinstance(value, str) or not value or len(value) > 256 or any(c in value for c in "\r\n\x00"):
        raise VpnError("%s is missing or invalid." % what)
    return value


def _run(argv, timeout=10):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=dict(os.environ, LC_ALL="C"))
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout


def _write_private(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


class VpnManager:
    def __init__(self, state_dir, runner=_run, interfaces=netinfo.interfaces, clock=time.time):
        self.dir = os.path.join(state_dir, "vpn")
        self.runner = runner
        self.interfaces = interfaces
        self.clock = clock
        self.lock = threading.Lock()

    def path(self, name):
        return os.path.join(self.dir, name)

    def _read_json(self, name):
        try:
            with open(self.path(name), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _units(self):
        rc, out = self.runner(["systemctl", "show", "-p", "Id,ActiveState,UnitFileState", UNIT, JOB_UNIT])
        units = {}
        if rc == 0:
            for block in out.strip().split("\n\n"):
                props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
                if props.get("Id"):
                    units[props["Id"]] = props
        return units

    def job(self, units=None):
        job = self._read_json("job.json")
        request = self._read_json("request")
        units = units if units is not None else self._units()
        running = units.get(JOB_UNIT, {}).get("ActiveState") in ("activating", "active", "reloading")
        if request and (not job or job.get("id") != request.get("id")):
            # job written, wol-vpn.service has not picked it up yet
            return {"id": request["id"], "action": request["action"], "state": "running",
                    "message": "Starting ...", "ts": request["ts"]}
        if job and job.get("state") == "running" and not running:
            job = dict(job, state="failed", message="Aborted (restart or crash?). Try again.")
        return job

    def status(self):
        units = self._units()
        unit = units.get(UNIT, {})
        address = next((i["address"] for i in self.interfaces()
                        if i["kind"] == "vpn" and i["up"] and i["address"]), None)
        info = self._read_json("info.json") or {}
        return {
            "installed": bool(shutil.which("openvpn") or os.path.exists("/usr/sbin/openvpn")),
            "configured": os.path.exists(CONF),
            "enabled": unit.get("UnitFileState") == "enabled",
            "state": unit.get("ActiveState") or "inactive",
            "address": address,
            "remote": info.get("remote"),
            "uploaded": info.get("ts"),
            "job": self.job(units),
        }

    def _request(self, action):
        job = self.job()
        if job and job.get("state") == "running":
            raise VpnError("A VPN change is already in progress. Wait a moment.")
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        request = {"id": "%d" % (self.clock() * 1000), "action": action, "ts": self.clock()}
        # write directly (do not rename): wol-vpn.path reacts to the file being closed
        fd = os.open(self.path("request"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(request, f)
        return request

    def upload(self, args):
        config, info = parse_config(args.get("config"))
        with self.lock:
            auth = None
            if info["needs_auth"]:
                user = validate_credential(args.get("username"), "The VPN user name")
                auth = "%s\n%s\n" % (user, validate_credential(args.get("password"), "The VPN password"))
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            _write_private(self.path("client.ovpn"), config)
            if auth:
                _write_private(self.path("client.auth"), auth)
            else:
                try:
                    os.remove(self.path("client.auth"))
                except FileNotFoundError:
                    pass
            _write_private(self.path("info.json"), json.dumps({"remote": info["remote"], "ts": self.clock()}))
            self._request("apply")
        log.info("VPN configuration for %s accepted", info["remote"])
        return {"ok": True, "message": "Setting up configuration for %s ..." % info["remote"]}

    def set_enabled(self, args):
        enabled = bool(args.get("enabled"))
        with self.lock:
            if not os.path.exists(CONF):
                raise VpnError("No VPN configuration set up.")
            self._request("enable" if enabled else "disable")
        return {"ok": True, "message": "Switching VPN on ..." if enabled else "Switching VPN off ..."}

    def remove(self, args=None):
        with self.lock:
            self._request("remove")
        return {"ok": True, "message": "Removing VPN configuration ..."}
