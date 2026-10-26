"""WLAN-Verwaltung mit Einrichtungs-Hotspot (Dienst wol-wifi, läuft als root).

Im Normalbetrieb ist der Pi WLAN-Client im Heimnetz – nur so erreichen Magic Packets die
Zielgeräte. Findet er kein bekanntes WLAN, öffnet er über NetworkManager einen eigenen
Hotspot. Dort lässt sich das WLAN in der Web-GUI einrichten; ein DNS-Eintrag leitet alle
Namen auf den Pi um, sodass Handys die Oberfläche als Captive Portal von selbst öffnen.

Der Web-Server (Benutzer wol) spricht über einen Unix-Socket mit diesem Dienst und braucht
dadurch selbst keine Rechte für NetworkManager.
"""

import ipaddress
import json
import logging
import os
import pwd
import queue
import re
import signal
import socket
import struct
import subprocess
import threading
import time

from . import led, netinfo, netsvc, vpn

log = logging.getLogger("wolserver.wifi")

HOTSPOT_ADDRESS = "10.42.0.1"
HOTSPOT_PROFILE = "wol-hotspot"
HOTSPOT_CHANNEL = 6
TICK = 15  # s zwischen zwei Prüfungen
BOOT_GRACE = 30  # s bis zum Hotspot, wenn gar kein WLAN gespeichert ist
ATTEMPT_SECONDS = 90  # so lange darf ein Verbindungsversuch aus dem Hotspot heraus dauern
BUSY_SECONDS = 300  # nach einem GUI-Zugriff den Hotspot nicht automatisch beenden
MAX_REQUEST = 512 * 1024  # VPN-Konfiguration mit Zertifikaten, Netzwerk-Sicherungen


class WifiError(Exception):
    pass


# ---------------------------------------------------------------- nmcli-Ausgaben


def split_terse(line):
    """Zerlegt eine Zeile von `nmcli -t` (":" als Trenner, "\\:" und "\\\\" maskiert)."""
    fields, current, escaped = [], [], False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


def parse_details(text):
    """`nmcli -t -f a,b connection show X` → {"a": ..., "b": ...}"""
    result = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            result[key] = re.sub(r"\\(.)", r"\1", value)
    return result


def parse_scan(text, limit=30):
    """`nmcli -t -f SSID,SIGNAL,SECURITY device wifi list` → je SSID der stärkste Eintrag."""
    best = {}
    for line in text.splitlines():
        fields = split_terse(line)
        if len(fields) < 3 or not fields[0]:
            continue
        ssid, signal_str, security = fields[0], fields[1], fields[2]
        try:
            strength = int(signal_str)
        except ValueError:
            strength = 0
        security = "" if security in ("", "--") else security
        if ssid not in best or strength > best[ssid]["signal"]:
            best[ssid] = {"ssid": ssid, "signal": strength, "security": security}
    return sorted(best.values(), key=lambda n: -n["signal"])[:limit]


def validate_ssid(ssid):
    if not isinstance(ssid, str) or not ssid.strip():
        raise WifiError("Bitte den WLAN-Namen (SSID) angeben.")
    if len(ssid.encode("utf-8")) > 32:
        raise WifiError("Der WLAN-Name darf höchstens 32 Bytes lang sein.")
    if any(ord(c) < 32 for c in ssid):
        raise WifiError("Der WLAN-Name enthält ungültige Zeichen.")
    return ssid


def validate_psk(psk, what="Das WLAN-Passwort"):
    """Leer = offenes Netz; sonst WPA-Passphrase (8–63 Zeichen ASCII) oder 64 Hex-Zeichen."""
    if psk in (None, ""):
        return ""
    if not isinstance(psk, str):
        raise WifiError("%s ist ungültig." % what)
    if re.fullmatch(r"[0-9a-fA-F]{64}", psk):
        return psk
    if not 8 <= len(psk) <= 63:
        raise WifiError("%s muss 8 bis 63 Zeichen lang sein." % what)
    if any(not 32 <= ord(c) <= 126 for c in psk):
        raise WifiError("%s darf nur ASCII-Zeichen enthalten (keine Umlaute)." % what)
    return psk


def validate_ipv4(data):
    """Feste Adresse für das WLAN-Profil → nmcli-Argumente; ohne Adresse [] (DHCP).

    data: {"address": "192.168.1.8/24", "gateway": "192.168.1.1", "dns": "192.168.1.1 1.1.1.1"}
    """
    data = data if isinstance(data, dict) else {}
    address = str(data.get("address") or "").strip()
    if not address:
        return []
    try:
        iface = ipaddress.IPv4Interface(address if "/" in address else address + "/24")
    except ValueError:
        raise WifiError("Ungültige Adresse, Beispiel: 192.168.1.8/24") from None
    net = iface.network
    if not 8 <= net.prefixlen <= 30 or iface.ip in (net.network_address, net.broadcast_address):
        raise WifiError("Die Adresse %s ist im Netz %s nicht nutzbar." % (iface.ip, net))
    try:
        gateway = ipaddress.IPv4Address(str(data.get("gateway") or "").strip())
    except ValueError:
        raise WifiError("Bei fester Adresse ist ein gültiges Gateway nötig, z. B. der Router.") from None
    if gateway not in net or gateway == iface.ip:
        raise WifiError("Das Gateway %s liegt nicht im Netz %s." % (gateway, net))
    try:
        dns = [ipaddress.IPv4Address(d) for d in re.split(r"[\s,;]+", str(data.get("dns") or "").strip()) if d]
    except ValueError:
        raise WifiError("Ungültiger DNS-Server, Beispiel: 192.168.1.1") from None
    return ["ipv4.method", "manual", "ipv4.addresses", str(iface), "ipv4.gateway", str(gateway),
            "ipv4.dns", " ".join(str(d) for d in dns or [gateway]), "ipv4.ignore-auto-dns", "yes"]


def default_hotspot_ssid(iface):
    try:
        with open("/sys/class/net/%s/address" % iface, encoding="ascii") as f:
            mac = f.read().strip().replace(":", "")
        return "WOL-Setup-%s" % mac[-4:].upper()
    except OSError:
        return "WOL-Setup"


def _run(argv, timeout):
    env = dict(os.environ, LC_ALL="C")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return 127, "", "%s nicht gefunden" % argv[0]
    except subprocess.TimeoutExpired:
        return 124, "", "Zeitüberschreitung bei %s" % " ".join(argv[:3])
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------- Zustandsautomat


class Job:
    def __init__(self, name, args):
        self.name = name
        self.args = args
        self.result = None
        self.done = threading.Event()

    def reply(self, result):
        if not self.done.is_set():
            self.result = result
            self.done.set()


class WifiManager:
    """Alle NetworkManager-Aufrufe laufen nacheinander im Worker-Thread (run()).

    Modi: client (mit WLAN verbunden), hotspot (eigener Hotspot aktiv), offline.
    """

    def __init__(self, cfg, state_dir, runner=None, probe=None, clock=time.monotonic, led=None, on_mode=None):
        self.iface = cfg["wifi_interface"]
        self.enabled = bool(cfg["hotspot_enabled"])
        self.hotspot_ssid = cfg["hotspot_ssid"] or default_hotspot_ssid(self.iface)
        self.delay = float(cfg["hotspot_delay_minutes"]) * 60
        self.retry = float(cfg["hotspot_retry_minutes"]) * 60
        self.runner = runner or _run
        self.probe = probe or self._probe_address
        self.clock = clock
        self.led = led
        self.on_mode = on_mode  # z. B. NetManager: dnsmasq im Hotspot pausieren
        self.state_file = os.path.join(state_dir, "hotspot.json")
        self.jobs = queue.Queue()
        self.last_request = None  # gesetzt vom Control-Thread bei GUI-Zugriffen

        now = clock()
        self.started = now
        self.last_online = now
        self.hotspot_since = None
        self.hotspot_started_at = None  # Wanduhr, für die Anzeige
        self.wait_until = None  # bis dahin keinen Hotspot starten (Verbindungsversuch/Backoff)
        self.mode = None
        self.address = None
        self.profiles = []  # [{"name", "uuid", "ssid"}]
        self.profiles_time = None
        self.current_ssid = None
        self.scan_cache = []
        self.scan_time = None
        self.pending = None
        self.last_error = None
        self._lock = threading.Lock()
        self._snapshot = {}

    # -- Hilfen --

    def nm(self, *args, timeout=20):
        rc, out, err = self.runner(["nmcli", *args], timeout)
        if rc != 0:
            message = (err or out).strip().splitlines()
            raise WifiError(message[-1] if message else "nmcli %s fehlgeschlagen (%d)" % (args[0], rc))
        return out

    def _probe_address(self):
        try:
            for iface in netinfo.interfaces():
                if iface["name"] == self.iface and iface["up"]:
                    return iface["address"]
        except OSError:
            pass
        return None

    def _stations(self):
        rc, out, _ = self.runner(["iw", "dev", self.iface, "station", "dump"], 10)
        return out.count("Station ") if rc == 0 else None

    def _hotspot_password(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f).get("password", "")
        except (OSError, ValueError):
            return ""

    def _error(self, message):
        log.warning("%s", message)
        self.last_error = {"message": message, "ts": time.time()}

    def snapshot(self):
        with self._lock:
            return dict(self._snapshot)

    def _publish(self):
        snap = {
            "interface": self.iface,
            "mode": self.mode or "offline",
            "address": self.address,
            "ssid": self.current_ssid if self.mode == "client" else None,
            "pending": self.pending,
            "saved": sorted({p["ssid"] for p in self.profiles}, key=str.lower),
            "hotspot": {
                "enabled": self.enabled,
                "ssid": self.hotspot_ssid,
                "secured": bool(self._hotspot_password()),
                "address": HOTSPOT_ADDRESS,
                "delay_minutes": self.delay / 60,
                "since": self.hotspot_started_at if self.mode == "hotspot" else None,
            },
            "scan": self.scan_cache,
            "scan_time": self.scan_time,
            "last_error": self.last_error,
        }
        with self._lock:
            self._snapshot = snap
        if self.led is not None:
            self.led.update(snap["mode"], self.pending)

    # -- NetworkManager --

    def refresh_profiles(self):
        out = self.nm("-t", "-f", "NAME,UUID,TYPE", "connection", "show")
        profiles = []
        for line in out.splitlines():
            fields = split_terse(line)
            if len(fields) < 3 or fields[2] != "802-11-wireless" or fields[0] == HOTSPOT_PROFILE:
                continue
            details = parse_details(self.nm(
                "-t", "-f", "802-11-wireless.ssid,802-11-wireless.mode", "connection", "show", "uuid", fields[1]))
            if details.get("802-11-wireless.mode") == "ap":
                continue
            profiles.append({"name": fields[0], "uuid": fields[1],
                             "ssid": details.get("802-11-wireless.ssid") or fields[0]})
        self.profiles = profiles
        self.profiles_time = self.clock()
        self.current_ssid = None
        if self.mode == "client":
            try:
                name = parse_details(self.nm("-t", "-f", "GENERAL.CONNECTION", "device", "show", self.iface)) \
                    .get("GENERAL.CONNECTION")
            except WifiError:
                name = None
            self.current_ssid = next((p["ssid"] for p in profiles if p["name"] == name), name or None)

    def scan(self, rescan=True):
        out = self.nm("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", self.iface,
                      "--rescan", "yes" if rescan else "auto", timeout=30)
        self.scan_cache = parse_scan(out)
        self.scan_time = time.time()

    def _write_hotspot_profile(self):
        # Bei jedem Start neu anlegen: So greifen geänderte SSID und Passwort sofort.
        try:
            self.nm("connection", "delete", HOTSPOT_PROFILE)
        except WifiError:
            pass
        args = [
            "connection", "add", "type", "wifi", "ifname", self.iface, "con-name", HOTSPOT_PROFILE,
            "autoconnect", "no", "ssid", self.hotspot_ssid,
            "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
            "802-11-wireless.channel", str(HOTSPOT_CHANNEL), "802-11-wireless.powersave", "2",
            "ipv4.method", "shared", "ipv4.addresses", HOTSPOT_ADDRESS + "/24", "ipv6.method", "disabled",
        ]
        password = self._hotspot_password()
        if password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp",
                     "wifi-sec.group", "ccmp", "wifi-sec.psk", password]
        self.nm(*args)

    def start_hotspot(self):
        now = self.clock()
        try:
            self.nm("radio", "wifi", "on")
        except WifiError as e:
            log.warning("WLAN-Funk nicht einschaltbar: %s", e)
        # Im Hotspot-Betrieb kann der Zero nicht scannen – Liste vorher merken.
        try:
            self.scan(rescan=True)
        except WifiError as e:
            log.warning("WLAN-Suche fehlgeschlagen: %s", e)
        try:
            self._write_hotspot_profile()
            self.nm("--wait", "30", "connection", "up", HOTSPOT_PROFILE, timeout=45)
        except WifiError as e:
            self._error("Hotspot konnte nicht gestartet werden: %s" % e)
            self.wait_until = now + 120
            return False
        log.info("Einrichtungs-Hotspot „%s“ gestartet (http://%s/)", self.hotspot_ssid, HOTSPOT_ADDRESS)
        self.mode, self.address = "hotspot", HOTSPOT_ADDRESS
        self.hotspot_since, self.hotspot_started_at = now, time.time()
        self.wait_until = None
        return True

    def stop_hotspot(self):
        try:
            self.nm("connection", "down", HOTSPOT_PROFILE)
        except WifiError as e:
            log.warning("Hotspot ließ sich nicht beenden: %s", e)
        self.hotspot_since = self.hotspot_started_at = None
        self.mode, self.address = "offline", None

    def _hotspot_in_use(self, now):
        if self.last_request is not None and now - self.last_request < BUSY_SECONDS:
            return True
        return bool(self._stations())

    # -- periodische Prüfung --

    def tick(self):
        now = self.clock()
        address = self.probe()
        mode = "hotspot" if address == HOTSPOT_ADDRESS else "client" if address else "offline"
        if mode != self.mode or address != self.address or self.profiles_time is None \
                or now - self.profiles_time > 300:
            if mode != self.mode and self.mode is not None:
                log.info("WLAN: %s → %s%s", self.mode, mode, " (%s)" % address if address else "")
            self.mode, self.address = mode, address
            try:
                self.refresh_profiles()
            except WifiError as e:
                log.warning("WLAN-Profile nicht lesbar: %s", e)

        if self.on_mode is not None:
            try:
                self.on_mode(mode)
            except Exception:  # noqa: BLE001 – WLAN-Logik darf daran nie scheitern
                log.exception("Fehler beim Melden des WLAN-Zustands")
        if mode == "client":
            self.last_online = now
            self.hotspot_since = self.wait_until = None
        elif mode == "hotspot":
            if self.hotspot_since is None:  # z. B. nach Neustart des Dienstes übernommen
                self.hotspot_since, self.hotspot_started_at = now, time.time()
            if self.profiles and now - self.hotspot_since >= self.retry and not self._hotspot_in_use(now):
                log.info("Beende den Hotspot kurz und suche bekannte WLANs")
                self.stop_hotspot()
                self.wait_until = now + ATTEMPT_SECONDS
                try:
                    self.nm("--wait", "0", "device", "connect", self.iface)
                except WifiError:
                    pass  # kein passendes WLAN sichtbar – Hotspot kommt nach ATTEMPT_SECONDS zurück
        elif self.enabled and not (self.wait_until and now < self.wait_until):
            due = self.last_online + self.delay if self.profiles else self.started + BOOT_GRACE
            if now >= due:
                if self.profiles:
                    log.info("Seit %d min kein WLAN – starte Einrichtungs-Hotspot", (now - self.last_online) // 60)
                else:
                    log.info("Kein WLAN eingerichtet – starte Einrichtungs-Hotspot")
                self.start_hotspot()
        self._publish()

    # -- Befehle aus der GUI --

    def do_scan(self, job):
        cached = self.mode == "hotspot"
        if not cached:
            self.scan(rescan=True)
        job.reply({"ok": True, "networks": self.scan_cache, "scan_time": self.scan_time, "cached": cached})

    def do_connect(self, job):
        ssid = validate_ssid(job.args.get("ssid"))
        psk = validate_psk(job.args.get("password"))
        hidden = "yes" if job.args.get("hidden") else "no"
        ipv4 = validate_ipv4(job.args.get("ipv4"))
        existing = [p for p in self.profiles if p["ssid"] == ssid]
        active = self.mode == "client" and ssid == self.current_ssid
        created = False
        # Hinweis: Das Passwort steht kurz in der Prozessliste von nmcli. Auf dem Pi gibt es außer
        # root, pi und wol (dem Web-Server, der es ohnehin kennt) niemanden, der es lesen könnte.
        if existing and psk:
            uuid = existing[0]["uuid"]
            self.nm("connection", "modify", "uuid", uuid, "802-11-wireless.hidden", hidden,
                    "connection.autoconnect", "yes", "802-11-wireless.cloned-mac-address", "permanent",
                    "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk, *ipv4)
        else:
            if existing and active:
                raise WifiError("„%s“ ist verbunden. Für die Umstellung auf ein offenes Netz "
                                "zuerst ein anderes WLAN verbinden." % ssid)
            for profile in existing:
                self.nm("connection", "delete", "uuid", profile["uuid"])
            args = ["connection", "add", "type", "wifi", "ifname", self.iface, "con-name", ssid, "ssid", ssid,
                    "connection.autoconnect", "yes", "802-11-wireless.hidden", hidden,
                    "802-11-wireless.powersave", "2",
                    # Immer die Hardware-MAC – sonst passen DHCP-Reservierung und Freigaben nicht
                    "802-11-wireless.cloned-mac-address", "permanent"]
            if psk:
                args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
            out = self.nm(*args, *ipv4)
            match = re.search(r"\(([0-9a-f-]{36})\)", out)
            uuid = match.group(1) if match else None
            created = True
        self.refresh_profiles()
        if uuid is None:
            uuid = next((p["uuid"] for p in self.profiles if p["ssid"] == ssid), None)
        log.info("WLAN „%s“ gespeichert%s", ssid, " (feste Adresse %s)" % ipv4[3] if ipv4 else "")

        if self.mode == "client":
            job.reply({"ok": True, "switching": False,
                       "message": "„%s“ gespeichert. Es wird genutzt, sobald das aktuelle WLAN nicht "
                                  "erreichbar ist." % ssid})
            self._publish()
            return

        # Antwort vor dem Umschalten senden – danach ist die Verbindung zum Handy weg.
        job.reply({"ok": True, "switching": True, "message": "Verbinde mit „%s“ …" % ssid})
        self.pending, self.last_error = ssid, None
        self._publish()
        was_hotspot = self.mode == "hotspot"
        if was_hotspot:
            time.sleep(2)  # dem Web-Server Zeit geben, die Antwort auszuliefern
            self.stop_hotspot()
        try:
            self.nm("--wait", "45", "connection", "up", "uuid", uuid, timeout=60)
            log.info("Mit WLAN „%s“ verbunden", ssid)
            self.last_online = self.clock()
        except WifiError as e:
            if created:
                try:
                    self.nm("connection", "delete", "uuid", uuid)
                except WifiError:
                    pass
                self.refresh_profiles()
            self._error("Verbindung mit „%s“ fehlgeschlagen (%s). Bitte Netzname und Passwort prüfen."
                        % (ssid, e))
            if was_hotspot and self.enabled:
                self.start_hotspot()
        finally:
            self.pending = None

    def do_forget(self, job):
        ssid = job.args.get("ssid")
        matches = [p for p in self.profiles if p["ssid"] == ssid]
        if not matches:
            raise WifiError("Dieses WLAN ist nicht gespeichert.")
        if self.mode == "client" and ssid == self.current_ssid:
            raise WifiError("Das aktuell verbundene WLAN kann nicht entfernt werden.")
        for profile in matches:
            self.nm("connection", "delete", "uuid", profile["uuid"])
        self.refresh_profiles()
        log.info("WLAN „%s“ entfernt", ssid)
        job.reply({"ok": True})

    def do_hotspot_password(self, job):
        password = validate_psk(job.args.get("password"), "Das Hotspot-Passwort")
        if len(password) == 64:
            raise WifiError("Das Hotspot-Passwort muss 8 bis 63 Zeichen lang sein.")
        tmp = self.state_file + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"password": password}, f)
        os.replace(tmp, self.state_file)
        log.info("Hotspot-Passwort %s", "gesetzt" if password else "entfernt (offener Hotspot)")
        later = " Gilt ab dem nächsten Start des Hotspots." if self.mode == "hotspot" else ""
        job.reply({"ok": True, "message": ("Hotspot-Passwort gespeichert." if password
                                           else "Hotspot ohne Passwort gespeichert.") + later})

    # -- Worker --

    def submit(self, name, args=None, wait=20):
        job = Job(name, args or {})
        self.jobs.put(job)
        if job.done.wait(wait):
            return job.result
        return {"ok": True, "pending": True, "message": "Wird ausgeführt …"}

    def run_job(self, job):
        handler = getattr(self, "do_" + job.name, None)
        try:
            if handler is None:
                raise WifiError("Unbekannter Befehl")
            handler(job)
        except WifiError as e:
            job.reply({"ok": False, "error": str(e)})
        except Exception:
            job.reply({"ok": False, "error": "Interner Fehler im WLAN-Dienst"})
            raise
        finally:
            job.reply({"ok": True})
            self._publish()

    def run(self, stop):
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("Fehler in der WLAN-Prüfung")
            try:
                job = self.jobs.get(timeout=TICK)
            except queue.Empty:
                continue
            try:
                self.run_job(job)
            except Exception:  # noqa: BLE001
                log.exception("Fehler bei WLAN-Befehl %s", job.name)


# ---------------------------------------------------------------- Unix-Socket


class ControlServer:
    """Eine JSON-Zeile pro Verbindung: {"cmd": "status"|"scan"|"connect"|...} → JSON-Antwort."""

    WAIT = {"scan": 30, "connect": 25, "forget": 20, "hotspot_password": 10}
    # VPN-Befehle laufen direkt (nicht in der WLAN-Warteschlange): Sie schreiben nur Dateien.
    VPN = {"vpn_status": "status", "vpn_upload": "upload", "vpn_enable": "set_enabled", "vpn_remove": "remove"}
    # DHCP/DNS: ebenfalls direkt, Methoden von netsvc.NetManager
    NET = {
        "net_status": "status", "net_config": "config", "net_leases": "leases", "net_unknown": "unknown",
        "net_settings": "settings", "net_reservation_save": "reservation_save",
        "net_reservation_delete": "reservation_delete", "net_record_save": "record_save",
        "net_record_delete": "record_delete", "net_discard": "discard", "net_apply": "apply",
        "net_approve": "approve", "net_ignore": "ignore", "net_unignore": "unignore", "net_release": "release",
        "net_restart": "restart", "net_probe": "probe", "net_import_preview": "import_preview",
        "net_import_commit": "import_commit", "net_export": "export", "net_restore": "restore",
        "net_history": "history", "net_history_restore": "history_restore", "net_logs": "logs",
        "net_static_ip": "static_ip",
    }

    def __init__(self, path, manager, allowed_uids, vpn_manager=None, net_manager=None):
        self.path = path
        self.manager = manager
        self.vpn = vpn_manager
        self.net = net_manager
        self.allowed_uids = set(allowed_uids)
        self.sock = None

    def bind(self):
        if os.path.dirname(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o660)  # Gruppe wol (Group= in der systemd-Unit)
        self.sock.listen(8)
        self.sock.settimeout(1)

    def serve(self, stop):
        if self.sock is None:
            self.bind()
        try:
            while not stop.is_set():
                try:
                    conn, _ = self.sock.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self.handle, args=(conn,), daemon=True).start()
        finally:
            self.sock.close()
            try:
                os.unlink(self.path)
            except OSError:
                pass

    def handle(self, conn):
        with conn:
            conn.settimeout(5)
            try:
                creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                uid = struct.unpack("3i", creds)[1]
                if uid not in self.allowed_uids:
                    log.warning("WLAN-Socket: Zugriff von uid %d abgelehnt", uid)
                    return
                data = b""
                while not data.endswith(b"\n") and len(data) <= MAX_REQUEST:
                    chunk = conn.recv(MAX_REQUEST)
                    if not chunk:
                        break
                    data += chunk
                try:
                    req = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    req = None
                response = self.dispatch(req) if isinstance(req, dict) else {"ok": False, "error": "Ungültige Anfrage"}
                conn.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            except OSError:
                pass

    def dispatch(self, req):
        cmd = req.get("cmd")
        self.manager.last_request = self.manager.clock()
        if cmd == "status":
            return {"ok": True, "status": self.manager.snapshot()}
        args = {k: v for k, v in req.items() if k != "cmd"}
        if cmd in self.WAIT:
            return self.manager.submit(cmd, args, self.WAIT[cmd])
        if cmd in self.VPN and self.vpn is not None:
            try:
                if cmd == "vpn_status":
                    return {"ok": True, "status": self.vpn.status()}
                return getattr(self.vpn, self.VPN[cmd])(args)
            except (vpn.VpnError, OSError) as e:
                return {"ok": False, "error": str(e)}
        if cmd in self.NET and self.net is not None:
            try:
                result = getattr(self.net, self.NET[cmd])(args)
                return result if "ok" in result else dict(result, ok=True)
            except (netsvc.NetError, OSError, ValueError, TypeError) as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "Unbekannter Befehl"}


def request(path, payload, timeout=35):
    """Client-Seite für den Web-Server. Wirft OSError, wenn der Dienst nicht läuft."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode("utf-8"))


def run_daemon(cfg):
    if os.geteuid() != 0:
        log.warning("Läuft nicht als root – NetworkManager wird Änderungen ablehnen")
    state_dir = os.environ.get("STATE_DIRECTORY", "/var/lib/wol-wifi").split(":")[0]
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    status_led = led.StatusLed() if cfg["status_led"] else None
    net_manager = netsvc.NetManager(state_dir)
    manager = WifiManager(cfg, state_dir, led=status_led, on_mode=net_manager.wifi_changed)
    allowed = {0, os.geteuid()}
    try:
        allowed.add(pwd.getpwnam("wol").pw_uid)
    except KeyError:
        pass
    server = ControlServer(cfg["wifi_socket"], manager, allowed, vpn.VpnManager(state_dir), net_manager)
    server.bind()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("WLAN-Verwaltung für %s gestartet (Hotspot %s: „%s“)", manager.iface,
             "aktiv" if manager.enabled else "aus", manager.hotspot_ssid)
    threading.Thread(target=manager.run, args=(stop,), daemon=True).start()
    server.serve(stop)
    if status_led is not None:
        status_led.set("normal")
    return 0
