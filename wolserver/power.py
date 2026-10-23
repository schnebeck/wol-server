"""Geräte schlafen legen: SSH-Befehl, HTTP-Anfrage oder Sleep-on-LAN-Paket."""

import logging
import os
import shutil
import socket
import subprocess
import urllib.error
import urllib.request

from . import wol

log = logging.getLogger("wolserver")

SSH_TIMEOUT = 25


class SshIdentity:
    """Eigenes Schlüsselpaar des WOL-Servers; der öffentliche Schlüssel wird in der GUI angezeigt."""

    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, ".ssh")
        self.key = os.path.join(self.dir, "id_ed25519")
        self.known_hosts = os.path.join(self.dir, "known_hosts")

    def ensure(self):
        if os.path.exists(self.key):
            return
        keygen = shutil.which("ssh-keygen")
        if not keygen:
            log.warning("ssh-keygen nicht gefunden – SSH-Schlafmethode nicht verfügbar")
            return
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        comment = "wol-server@%s" % socket.gethostname()
        subprocess.run(
            [keygen, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", self.key],
            check=True, stdin=subprocess.DEVNULL, timeout=60,
        )
        log.info("SSH-Schlüssel erzeugt: %s", self.key)

    def public_key(self):
        try:
            with open(self.key + ".pub", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""


def sleep_device(device, identity, source_ip=None):
    """Liefert (ok, meldung)."""
    method = device.get("sleep_method", "none")
    if method == "ssh":
        return _sleep_ssh(device, identity)
    if method == "http":
        return _sleep_http(device)
    if method == "sol":
        wol.send_magic_packet(
            device["mac"], device["broadcast"], device["wol_port"], reverse=True, source_ip=source_ip
        )
        return True, "Sleep-on-LAN-Paket gesendet"
    return False, "Keine Schlaf-Methode eingerichtet"


def _sleep_ssh(device, identity):
    ssh = shutil.which("ssh")
    if not ssh:
        return False, "ssh-Client nicht installiert"
    if not os.path.exists(identity.key):
        return False, "SSH-Schlüssel fehlt (%s)" % identity.key
    cmd = [
        ssh, "-i", identity.key, "-p", str(device["ssh_port"]), "-l", device["ssh_user"],
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "ConnectTimeout=6",
        "-o", "ServerAliveInterval=3",
        "-o", "ServerAliveCountMax=2",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=" + identity.known_hosts,
        "-o", "LogLevel=ERROR",
        "--", device["host"], device["ssh_command"],
    ]
    try:
        res = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=SSH_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return True, "Keine Antwort nach %d s – Gerät schläft vermutlich bereits" % SSH_TIMEOUT
    err = (res.stderr or res.stdout or "").strip().splitlines()
    detail = err[-1] if err else ""
    if res.returncode == 0:
        return True, "SSH-Befehl ausgeführt"
    if res.returncode == 255 and ("closed by" in detail or "reset" in detail or "broken pipe" in detail.lower()):
        return True, "Verbindung vom Gerät getrennt – geht vermutlich schlafen"
    return False, "SSH fehlgeschlagen (Code %d): %s" % (res.returncode, detail or "keine Details")


def _sleep_http(device):
    body = device.get("http_body") or ""
    req = urllib.request.Request(
        device["http_url"], data=body.encode("utf-8") if body else None, method=device["http_method"]
    )
    if body:
        is_json = body.lstrip().startswith(("{", "["))
        req.add_header("Content-Type", "application/json" if is_json else "text/plain; charset=utf-8")
    req.add_header("User-Agent", "WOL-Server")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True, "HTTP %d" % resp.status
    except urllib.error.HTTPError as e:
        return False, "HTTP %d %s" % (e.code, e.reason)
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return False, "HTTP-Anfrage fehlgeschlagen: %s" % reason
