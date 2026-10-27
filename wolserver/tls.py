"""HTTPS: Zertifikat laden, Erneuerungen bemerken und vom Webserver abholen.

Das Zertifikat (Let's Encrypt) erneuert ein Webserver; der Pi holt Zertifikat und Schlüssel per SSH
über das VPN ab (`wol-server cert-fetch`, Dienst wol-cert.timer) und legt sie in tls_dir ab:
cert.pem (Zertifikat mit Kette) und key.pem. Der Web-Server lädt sie ohne Neustart nach.
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
FETCH_KEY = "fetch_key"  # SSH-Schlüssel für den Abruf
KNOWN_HOSTS = "known_hosts"  # Host-Schlüssel des Webservers
MIN_DAYS = 2  # kürzer gültige Zertifikate nimmt der Abruf nicht mehr an

_PEM = re.compile(rb"-----BEGIN ([A-Z ]+)-----\r?\n.+?-----END \1-----\r?\n?", re.S)


def cert_info(path):
    """Namen und Ablaufzeit des ersten Zertifikats in path: {"names": [...], "not_after": epoch}."""
    # _test_decode_cert ist die einzige Möglichkeit der Standardbibliothek, ein Zertifikat zu lesen
    decoded = ssl._ssl._test_decode_cert(path)  # pylint: disable=protected-access
    names = [value for kind, value in decoded.get("subjectAltName", ()) if kind == "DNS"]
    return {"names": names, "not_after": calendar.timegm(time.strptime(decoded["notAfter"], "%b %d %H:%M:%S %Y %Z"))}


def server_context(cert, key):
    """TLS-Kontext für den Server; scheitert, wenn Schlüssel und Zertifikat nicht zusammenpassen."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


class TlsState:
    """Aktuelles Zertifikat für HTTPS; prüft höchstens einmal pro Minute, ob es erneuert wurde."""

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
            log.warning("HTTPS-Zertifikat in %s unbrauchbar: %s", os.path.dirname(self.cert), e)
            return  # bisheriges behalten
        self.stamp, self.context, self.info = stamp, context, info
        log.info("HTTPS-Zertifikat geladen: %s, gültig bis %s", ", ".join(info["names"]) or "?",
                 time.strftime("%d.%m.%Y", time.localtime(info["not_after"])))

    def current(self):
        """TLS-Kontext für eine neue Verbindung oder None."""
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
        """Hostname für die Umleitung von HTTP; None ohne gültiges Zertifikat."""
        status = self.status()
        return status["names"][0] if status["active"] and status["names"] else None


# -- Abruf vom Webserver (als root, Dienst wol-cert) --

class FetchError(Exception):
    pass


def split_bundle(data):
    """PEM-Bündel → (Schlüssel, Zertifikatskette) als Bytes."""
    keys, certs = [], []
    for match in _PEM.finditer(data):
        block = match.group(0).rstrip(b"\r\n") + b"\n"
        (certs if match.group(1) == b"CERTIFICATE" else keys).append(block)
    if len(keys) != 1 or not certs:
        raise FetchError("Antwort enthält nicht genau einen Schlüssel und ein Zertifikat")
    if b"PRIVATE KEY" not in keys[0].split(b"\n", 1)[0]:
        raise FetchError("Unbekannter Schlüsseltyp")
    return keys[0], b"".join(certs)


def install_bundle(data, tls_dir, expect_name, group_gid=None, wall=time.time):
    """Bündel prüfen und nach tls_dir übernehmen. Ergebnis: True, wenn sich etwas geändert hat."""
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
            raise FetchError("Zertifikat oder Schlüssel ungültig: %s" % e) from None
        if expect_name and expect_name not in info["names"]:
            raise FetchError("Zertifikat gilt nicht für %s (sondern %s)" % (expect_name, ", ".join(info["names"])))
        if info["not_after"] < wall() + MIN_DAYS * 86400:
            raise FetchError("Zertifikat läuft in weniger als %d Tagen ab" % MIN_DAYS)
        old_cert, old_key = os.path.join(tls_dir, CERT), os.path.join(tls_dir, KEY)
        try:
            with open(old_cert, "rb") as f1, open(old_key, "rb") as f2:
                if f1.read() == chain and f2.read() == key:
                    return False
        except OSError:
            pass
        for path in (new_cert, new_key):
            if group_gid is not None:
                os.chown(path, 0, group_gid)  # lesbar für den Web-Server (Gruppe wol)
        # Schlüssel zuerst: Der Web-Server lädt erst, wenn sich beide Dateien geändert haben
        os.replace(new_key, old_key)
        os.replace(new_cert, old_cert)
    log.info("HTTPS-Zertifikat übernommen: %s, gültig bis %s", ", ".join(info["names"]),
             time.strftime("%d.%m.%Y", time.localtime(info["not_after"])))
    return True


def fetch(cfg, runner=subprocess.run):
    """Bündel per SSH abholen (der Webserver liefert es per command= in authorized_keys)."""
    source = cfg.get("cert_source") or ""
    if not source:
        raise FetchError("Keine Quelle eingestellt (cert_source in der Konfiguration)")
    tls_dir = cfg["tls_dir"]
    argv = ["ssh", "-i", os.path.join(tls_dir, FETCH_KEY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
            "-o", "UserKnownHostsFile=" + os.path.join(tls_dir, KNOWN_HOSTS), "-o", "StrictHostKeyChecking=yes",
            "-o", "IdentitiesOnly=yes", source]
    try:
        result = runner(argv, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise FetchError("SSH zu %s fehlgeschlagen: %s" % (source, e)) from None
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise FetchError("SSH zu %s fehlgeschlagen: %s" % (source, detail[-1] if detail else result.returncode))
    return result.stdout
