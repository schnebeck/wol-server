"""OpenVPN-Client über die Web-GUI einrichten (Teil des Root-Dienstes wol-wifi).

Der Dienst prüft hochgeladene Konfigurationen und legt sie mit einem Auftrag im eigenen
State-Verzeichnis ab. Die eigentliche Einrichtung (Paket, /etc/openvpn, systemd) übernimmt
wol-vpn.service, das wol-vpn.path beim Schreiben der Auftragsdatei startet. So braucht
wol-wifi selbst weder Schreibrechte in /etc noch Rechte gegenüber systemd.
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

# Eine .ovpn-Datei läuft mit root-Rechten. Diese Optionen führen Programme aus, laden Code oder
# schreiben Dateien an beliebige Orte – aus einer hochgeladenen Datei werden sie nicht übernommen.
FORBIDDEN = {
    "up", "down", "route-up", "route-pre-down", "ipchange", "tls-verify", "auth-user-pass-verify",
    "client-connect", "client-disconnect", "learn-address", "script-security", "plugin", "iproute",
    "log", "log-append", "status", "writepid", "cd", "chroot", "daemon", "config", "management",
    "askpass", "tmp-dir", "syslog", "dev-node", "secret", "up-delay", "down-pre", "setenv-safe",
}
FORBIDDEN_PREFIXES = ("management-",)
# Verweise auf Zusatzdateien: Hochladen lässt sich nur eine einzelne Datei
FILE_OPTIONS = {"ca", "cert", "key", "tls-auth", "tls-crypt", "tls-crypt-v2", "pkcs12", "dh",
                "extra-certs", "crl-verify", "http-proxy-user-pass"}
INLINE_TAG = re.compile(r"^\s*<(/?)([a-z0-9-]+)>\s*$", re.I)


class VpnError(Exception):
    pass


def parse_config(text):
    """Prüft eine Client-Konfiguration. Liefert (bereinigter Text, Infos) oder wirft VpnError."""
    if not isinstance(text, str) or not text.strip():
        raise VpnError("Die Datei ist leer.")
    if len(text.encode("utf-8")) > MAX_CONFIG:
        raise VpnError("Die Datei ist zu groß (max. %d KB)." % (MAX_CONFIG // 1024))
    if "\x00" in text:
        raise VpnError("Das ist keine Textdatei.")
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
            raise VpnError("Zeile %d: „%s“ ist aus Sicherheitsgründen nicht erlaubt "
                           "(würde mit root-Rechten Programme ausführen oder Dateien schreiben)." % (number, words[0]))
        if option in FILE_OPTIONS and len(words) > 1 and words[1].lower() != "[inline]":
            raise VpnError("Zeile %d: „%s“ verweist auf die Datei „%s“. Bitte eine Konfiguration mit "
                           "eingebetteten Zertifikaten verwenden (<%s>…</%s>)." % (number, words[0], words[1],
                                                                              option, option))
        if option in ("client", "tls-client"):
            is_client = True
        elif option == "remote" and len(words) > 1:
            remotes.append(words[1] + (":" + words[2] if len(words) > 2 and words[2].isdigit() else ""))
        elif option == "auth-user-pass":
            needs_auth = True
            line = "auth-user-pass"  # Datei setzt die Einrichtung
        out.append(line)
    if inline:
        raise VpnError("Der Block <%s> wird nicht geschlossen." % inline)
    if not is_client:
        raise VpnError("Das ist keine Client-Konfiguration (es fehlt „client“).")
    if not remotes:
        raise VpnError("In der Konfiguration fehlt der Server („remote …“).")
    return "\n".join(out).strip("\n") + "\n", {"remote": remotes[0], "needs_auth": needs_auth}


def validate_credential(value, what):
    if not isinstance(value, str) or not value or len(value) > 256 or any(c in value for c in "\r\n\x00"):
        raise VpnError("%s fehlt oder ist ungültig." % what)
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
            # Auftrag geschrieben, wol-vpn.service hat ihn noch nicht übernommen
            return {"id": request["id"], "action": request["action"], "state": "running",
                    "message": "Wird gestartet …", "ts": request["ts"]}
        if job and job.get("state") == "running" and not running:
            job = dict(job, state="failed", message="Abgebrochen (Neustart oder Absturz?). Bitte erneut versuchen.")
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
            raise VpnError("Eine VPN-Änderung wird bereits ausgeführt. Bitte kurz warten.")
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        request = {"id": "%d" % (self.clock() * 1000), "action": action, "ts": self.clock()}
        # Direkt schreiben (nicht umbenennen): wol-vpn.path reagiert auf das Schließen der Datei
        fd = os.open(self.path("request"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(request, f)
        return request

    def upload(self, args):
        config, info = parse_config(args.get("config"))
        with self.lock:
            auth = None
            if info["needs_auth"]:
                user = validate_credential(args.get("username"), "Der VPN-Benutzername")
                auth = "%s\n%s\n" % (user, validate_credential(args.get("password"), "Das VPN-Passwort"))
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
        log.info("VPN-Konfiguration für %s übernommen", info["remote"])
        return {"ok": True, "message": "Konfiguration für %s wird eingerichtet …" % info["remote"]}

    def set_enabled(self, args):
        enabled = bool(args.get("enabled"))
        with self.lock:
            if not os.path.exists(CONF):
                raise VpnError("Keine VPN-Konfiguration eingerichtet.")
            self._request("enable" if enabled else "disable")
        return {"ok": True, "message": "VPN wird %s …" % ("eingeschaltet" if enabled else "ausgeschaltet")}

    def remove(self, args=None):
        with self.lock:
            self._request("remove")
        return {"ok": True, "message": "VPN-Konfiguration wird entfernt …"}
