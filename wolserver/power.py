"""Put devices to sleep: SSH command, HTTP request or Sleep-on-LAN packet."""

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
    """The WOL-Server's own key pair; the public key is shown in the GUI."""

    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, ".ssh")
        self.key = os.path.join(self.dir, "id_ed25519")
        self.known_hosts = os.path.join(self.dir, "known_hosts")

    def ensure(self):
        if os.path.exists(self.key):
            return
        keygen = shutil.which("ssh-keygen")
        if not keygen:
            log.warning("ssh-keygen not found - SSH sleep method not available")
            return
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        comment = "wol-server@%s" % socket.gethostname()
        subprocess.run(
            [keygen, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", self.key],
            check=True, stdin=subprocess.DEVNULL, timeout=60,
        )
        log.info("SSH key created: %s", self.key)

    def public_key(self):
        try:
            with open(self.key + ".pub", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""


def sleep_device(device, identity, source_ip=None):
    """Returns (ok, message)."""
    method = device.get("sleep_method", "none")
    if method == "ssh":
        return _sleep_ssh(device, identity)
    if method == "http":
        return _sleep_http(device)
    if method == "sol":
        wol.send_magic_packet(
            device["mac"], device["broadcast"], device["wol_port"], reverse=True, source_ip=source_ip
        )
        return True, "Sleep-on-LAN packet sent"
    return False, "No sleep method configured"


def _sleep_ssh(device, identity):
    ssh = shutil.which("ssh")
    if not ssh:
        return False, "ssh client not installed"
    if not os.path.exists(identity.key):
        return False, "SSH key missing (%s)" % identity.key
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
        return True, "No response after %d s - device is probably already asleep" % SSH_TIMEOUT
    err = (res.stderr or res.stdout or "").strip().splitlines()
    detail = err[-1] if err else ""
    if res.returncode == 0:
        return True, "SSH command executed"
    if res.returncode == 255 and ("closed by" in detail or "reset" in detail or "broken pipe" in detail.lower()):
        return True, "Connection closed by the device - probably going to sleep"
    return False, "SSH failed (code %d): %s" % (res.returncode, detail or "no details")


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
        return False, "HTTP request failed: %s" % reason
