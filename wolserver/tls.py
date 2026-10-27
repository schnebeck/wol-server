"""HTTPS: load the certificate, notice renewals and fetch it from the web server.

A web server renews the certificate (Let's Encrypt); the Pi fetches certificate and key via SSH
over the VPN (`wol-server cert-fetch`, service wol-cert.timer) and stores them in tls_dir:
cert.pem (certificate with chain) and key.pem. The web server reloads them without a restart.
"""

import calendar
import logging
import os
import re
import ssl
import subprocess
import tempfile
import threading
import time

log = logging.getLogger("wolserver")

CERT = "cert.pem"
KEY = "key.pem"
FETCH_KEY = "fetch_key"  # SSH key for fetching
KNOWN_HOSTS = "known_hosts"  # host key of the web server
MIN_DAYS = 2  # the fetch no longer accepts certificates valid for a shorter time

_PEM = re.compile(rb"-----BEGIN ([A-Z ]+)-----\r?\n.+?-----END \1-----\r?\n?", re.S)


def cert_info(path):
    """Names and expiry time of the first certificate in path: {"names": [...], "not_after": epoch}."""
    # _test_decode_cert is the only way in the standard library to read a certificate
    decoded = ssl._ssl._test_decode_cert(path)  # pylint: disable=protected-access
    names = [value for kind, value in decoded.get("subjectAltName", ()) if kind == "DNS"]
    return {"names": names, "not_after": calendar.timegm(time.strptime(decoded["notAfter"], "%b %d %H:%M:%S %Y %Z"))}


def server_context(cert, key):
    """TLS context for the server; fails if key and certificate do not match."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


class TlsState:
    """Current certificate for HTTPS; checks at most once per minute whether it was renewed."""

    CHECK_SECONDS = 60

    def __init__(self, tls_dir, clock=time.monotonic, wall=time.time):
        self.cert = os.path.join(tls_dir, CERT)
        self.key = os.path.join(tls_dir, KEY)
        self.clock, self.wall = clock, wall
        self.lock = threading.Lock()
        self.checked = None
        self.stamp = None
        self.context = None
        self.info = {}

    def _refresh(self):
        now = self.clock()
        if self.checked is not None and now - self.checked < self.CHECK_SECONDS:
            return
        self.checked = now
        try:
            stamp = (os.stat(self.cert).st_mtime_ns, os.stat(self.key).st_mtime_ns)
        except OSError:
            self.stamp, self.context, self.info = None, None, {}
            return
        if stamp == self.stamp:
            return
        try:
            context, info = server_context(self.cert, self.key), cert_info(self.cert)
        except (OSError, ssl.SSLError, ValueError, KeyError) as e:
            log.warning("HTTPS certificate in %s unusable: %s", os.path.dirname(self.cert), e)
            return  # keep the previous one
        self.stamp, self.context, self.info = stamp, context, info
        log.info("HTTPS certificate loaded: %s, valid until %s", ", ".join(info["names"]) or "?",
                 time.strftime("%d.%m.%Y", time.localtime(info["not_after"])))

    def current(self):
        """TLS context for a new connection, or None."""
        with self.lock:
            self._refresh()
            return self.context

    def status(self):
        with self.lock:
            self._refresh()
            if not self.context:
                return {"active": False}
            return {"active": self.info["not_after"] > self.wall(), "names": self.info["names"],
                    "not_after": self.info["not_after"]}

    def host(self):
        """Host name for the redirect from HTTP; None without a valid certificate."""
        status = self.status()
        return status["names"][0] if status["active"] and status["names"] else None


# -- fetching from the web server (as root, service wol-cert) --

class FetchError(Exception):
    pass


def split_bundle(data):
    """PEM bundle -> (key, certificate chain) as bytes."""
    keys, certs = [], []
    for match in _PEM.finditer(data):
        block = match.group(0).rstrip(b"\r\n") + b"\n"
        (certs if match.group(1) == b"CERTIFICATE" else keys).append(block)
    if len(keys) != 1 or not certs:
        raise FetchError("Response does not contain exactly one key and a certificate")
    if b"PRIVATE KEY" not in keys[0].split(b"\n", 1)[0]:
        raise FetchError("Unknown key type")
    return keys[0], b"".join(certs)


def install_bundle(data, tls_dir, expect_name, group_gid=None, wall=time.time):
    """Check the bundle and install it into tls_dir. Result: True if something changed."""
    key, chain = split_bundle(data)
    with tempfile.TemporaryDirectory(dir=tls_dir) as tmp:
        new_cert, new_key = os.path.join(tmp, CERT), os.path.join(tmp, KEY)
        for path, content in ((new_cert, chain), (new_key, key)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
            with os.fdopen(fd, "wb") as f:
                f.write(content)
        try:
            server_context(new_cert, new_key)
            info = cert_info(new_cert)
        except (ssl.SSLError, ValueError, KeyError) as e:
            raise FetchError("Certificate or key invalid: %s" % e) from None
        if expect_name and expect_name not in info["names"]:
            raise FetchError("Certificate is not valid for %s (but for %s)" % (expect_name, ", ".join(info["names"])))
        if info["not_after"] < wall() + MIN_DAYS * 86400:
            raise FetchError("Certificate expires in less than %d days" % MIN_DAYS)
        old_cert, old_key = os.path.join(tls_dir, CERT), os.path.join(tls_dir, KEY)
        try:
            with open(old_cert, "rb") as f1, open(old_key, "rb") as f2:
                if f1.read() == chain and f2.read() == key:
                    return False
        except OSError:
            pass
        for path in (new_cert, new_key):
            if group_gid is not None:
                os.chown(path, 0, group_gid)  # readable for the web server (group wol)
        # key first: the web server only reloads once both files have changed
        os.replace(new_key, old_key)
        os.replace(new_cert, old_cert)
    log.info("HTTPS certificate installed: %s, valid until %s", ", ".join(info["names"]),
             time.strftime("%d.%m.%Y", time.localtime(info["not_after"])))
    return True


def fetch(cfg, runner=subprocess.run):
    """Fetch the bundle via SSH (the web server delivers it via command= in authorized_keys)."""
    source = cfg.get("cert_source") or ""
    if not source:
        raise FetchError("No source configured (cert_source in the configuration)")
    tls_dir = cfg["tls_dir"]
    argv = ["ssh", "-i", os.path.join(tls_dir, FETCH_KEY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
            "-o", "UserKnownHostsFile=" + os.path.join(tls_dir, KNOWN_HOSTS), "-o", "StrictHostKeyChecking=yes",
            "-o", "IdentitiesOnly=yes", source]
    try:
        result = runner(argv, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise FetchError("SSH to %s failed: %s" % (source, e)) from None
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise FetchError("SSH to %s failed: %s" % (source, detail[-1] if detail else result.returncode))
    return result.stdout
