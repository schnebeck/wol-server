"""HTTP-Server: JSON-API unter /api/ und die statische Web-GUI."""

import datetime
import errno
import hashlib
import http.cookies
import ipaddress
import json
import logging
import os
import re
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import __version__, models, netinfo, power, wifi, wol
from .auth import Auth, LoginLimiter
from .scheduler import Scheduler, next_run
from .status import StatusMonitor
from .store import Store

log = logging.getLogger("wolserver")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
COOKIE = "wol_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "wol-server"
MAX_BODY = 64 * 1024
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Request:
    def __init__(self, body, client, authenticated, via_hotspot=False):
        self.body = body
        self.client = client
        self.authenticated = authenticated
        self.via_hotspot = via_hotspot
        self.cookies = []


def captive_redirect(host_header, port):
    """Im Hotspot fremde Hostnamen auf die GUI umleiten.

    Handys prüfen nach dem Verbinden z. B. connectivitycheck.gstatic.com oder captive.apple.com.
    Da der Hotspot alle Namen auf den Pi auflöst, landen diese Anfragen hier; die Umleitung
    lässt das Handy die Einrichtungsseite als Captive Portal öffnen.
    """
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        host = host.split("]")[0] + "]"
    else:
        host = host.split(":")[0]
    if host == wifi.HOTSPOT_ADDRESS:
        return None
    return "http://%s%s/" % (wifi.HOTSPOT_ADDRESS, "" if port == 80 else ":%d" % port)


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


class App:
    def __init__(self, config):
        self.config = config
        data_dir = config["data_dir"]
        os.makedirs(data_dir, exist_ok=True)
        self.store = Store(os.path.join(data_dir, "data.json"))
        self.auth = Auth(data_dir, config["session_days"])
        self.limiter = LoginLimiter()
        self.networks = [ipaddress.ip_network(n, strict=False) for n in config["allowed_networks"]]
        self.identity = power.SshIdentity(data_dir)
        try:
            self.identity.ensure()
        except Exception:  # noqa: BLE001
            log.exception("Konnte SSH-Schlüssel nicht erzeugen")
        self.status = StatusMonitor(config["status_cache_seconds"])
        self.scheduler = Scheduler(self.store, self.run_action)
        self.started = time.time()
        self._static_cache = {}

        R = self._route
        self.routes = [
            R("GET", r"/api/session", self.api_session, auth=False),
            R("POST", r"/api/login", self.api_login, auth=False),
            R("POST", r"/api/logout", self.api_logout, auth=False),
            R("GET", r"/api/info", self.api_info),
            R("POST", r"/api/password", self.api_password),
            R("GET", r"/api/devices", self.api_devices),
            R("POST", r"/api/devices", self.api_device_create),
            R("PUT", r"/api/devices/(?P<item_id>\w+)", self.api_device_update),
            R("DELETE", r"/api/devices/(?P<item_id>\w+)", self.api_device_delete),
            R("POST", r"/api/devices/(?P<item_id>\w+)/(?P<action>wake|sleep)", self.api_device_action),
            R("GET", r"/api/status", self.api_status),
            R("GET", r"/api/schedules", self.api_schedules),
            R("POST", r"/api/schedules", self.api_schedule_create),
            R("PUT", r"/api/schedules/(?P<item_id>\w+)", self.api_schedule_update),
            R("DELETE", r"/api/schedules/(?P<item_id>\w+)", self.api_schedule_delete),
            R("GET", r"/api/events", self.api_events),
            R("DELETE", r"/api/events", self.api_events_clear),
            R("GET", r"/api/wifi", self.api_wifi),
            R("POST", r"/api/wifi/scan", self.api_wifi_scan),
            R("POST", r"/api/wifi/connect", self.api_wifi_connect),
            R("POST", r"/api/wifi/forget", self.api_wifi_forget),
            R("PUT", r"/api/wifi/hotspot", self.api_wifi_hotspot),
            R("GET", r"/api/vpn", self.api_vpn),
            R("POST", r"/api/vpn/config", self.api_vpn_config),
            R("PUT", r"/api/vpn", self.api_vpn_enable),
            R("DELETE", r"/api/vpn", self.api_vpn_remove),
        ]

    @staticmethod
    def _route(method, pattern, handler, auth=True):
        return method, re.compile("^%s$" % pattern), handler, auth

    def resolve(self, method, path):
        allowed = False
        for m, regex, handler, needs_auth in self.routes:
            match = regex.match(path)
            if match:
                if m == method:
                    return handler, match.groupdict(), needs_auth
                allowed = True
        raise ApiError(405 if allowed else 404, "Methode nicht erlaubt" if allowed else "Nicht gefunden")

    def is_allowed(self, ip):
        return any(ip in net for net in self.networks if net.version == ip.version)

    def start(self):
        self.scheduler.start()

    def stop(self):
        self.scheduler.stop()
        self.status.shutdown()

    # -- Aktionen (genutzt von API und Zeitplaner) --

    def wol_source(self):
        """LAN/WLAN-Interface für Magic Packets – pro Aktion neu ermittelt (DHCP, WLAN-Reconnect)."""
        try:
            return netinfo.lan_source(self.config["wol_interface"])
        except OSError:
            return None

    def run_action(self, device, action, source):
        try:
            iface = self.wol_source()
            if iface is None and self.config["wol_interface"]:
                log.warning("Interface %s hat keine IPv4-Adresse", self.config["wol_interface"])
            source_ip = iface["address"] if iface else None
            via = " über %s" % iface["name"] if iface else ""
            if source_ip == wifi.HOTSPOT_ADDRESS:
                ok, message = False, "Nicht ausgeführt: Der Pi ist nicht im Heimnetz (Einrichtungs-Hotspot aktiv)"
            elif action == "wake":
                wol.send_magic_packet(
                    device["mac"], device["broadcast"], device["wol_port"], source_ip=source_ip
                )
                ok, message = True, "Magic Packet an %s%s gesendet" % (device["mac"], via)
            else:
                ok, message = power.sleep_device(device, self.identity, source_ip=source_ip)
        except (OSError, ValueError) as e:
            ok, message = False, "Fehler: %s" % e
        self.status.invalidate(device["id"])
        (log.info if ok else log.warning)("%s %s (%s): %s", action, device["name"], source, message)
        self.store.add_event(
            device_id=device["id"], device=device["name"], action=action,
            source=source, ok=ok, message=message,
        )
        return ok, message

    # -- Session --

    def api_session(self, req):
        return 200, {
            "auth_required": self.auth.required,
            "authenticated": req.authenticated,
            "hotspot": req.via_hotspot,
        }

    def api_login(self, req):
        if not self.auth.required:
            return 200, {"ok": True}
        if self.limiter.blocked(req.client):
            raise ApiError(429, "Zu viele Fehlversuche – bitte in einigen Minuten erneut versuchen")
        password = req.body.get("password")
        if isinstance(password, str) and self.auth.check_password(password):
            self.limiter.reset(req.client)
            req.cookies.append(self._session_cookie(self.auth.make_token(), self.auth.session_seconds))
            return 200, {"ok": True}
        self.limiter.fail(req.client)
        log.warning("Fehlgeschlagener Login von %s", req.client)
        time.sleep(1)
        raise ApiError(401, "Falsches Passwort")

    def api_logout(self, req):
        req.cookies.append(self._session_cookie("", 0))
        return 200, {"ok": True}

    @staticmethod
    def _session_cookie(value, max_age):
        return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (COOKIE, value, max_age)

    def api_password(self, req):
        if self.auth.required and not self.auth.check_password(str(req.body.get("current", ""))):
            time.sleep(1)
            raise ApiError(403, "Aktuelles Passwort ist falsch")
        new = req.body.get("new")
        if not isinstance(new, str):
            raise ApiError(400, "Neues Passwort fehlt")
        try:
            self.auth.set_password(new)
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        req.cookies.append(self._session_cookie(self.auth.make_token(), self.auth.session_seconds))
        log.info("Passwort geändert von %s", req.client)
        return 200, {"ok": True}

    def api_info(self, req):
        temp = _read_text("/sys/class/thermal/thermal_zone0/temp")
        uptime = _read_text("/proc/uptime")
        model = _read_text("/proc/device-tree/model")
        return 200, {
            "version": __version__,
            "hostname": socket.gethostname(),
            "model": model.rstrip("\x00") if model else None,
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
            "timezone": time.strftime("%Z"),
            "system_uptime": float(uptime.split()[0]) if uptime else None,
            "service_uptime": time.time() - self.started,
            "cpu_temp": int(temp) / 1000 if temp and temp.isdigit() else None,
            "load": os.getloadavg()[0],
            "ssh_public_key": self.identity.public_key(),
            "auth_required": self.auth.required,
            "allowed_networks": self.config["allowed_networks"],
            "client": req.client,
            "network": self._network_info(),
        }

    def _network_info(self):
        try:
            ifaces = [i for i in netinfo.interfaces() if i["kind"] not in ("loopback", "virtual")]
        except OSError:
            ifaces = []
        wifi = netinfo.wifi_signal()
        for iface in ifaces:
            iface["wifi"] = wifi.get(iface["name"])
        source = self.wol_source()
        return {
            "interfaces": ifaces,
            "wol_interface": source["name"] if source else None,
            "port": self.config["port"],
        }

    # -- Geräte --

    def _device_or_404(self, item_id):
        device = self.store.get("devices", item_id)
        if device is None:
            raise ApiError(404, "Gerät nicht gefunden")
        return device

    def api_devices(self, req):
        return 200, {"devices": self.store.list("devices")}

    def api_device_create(self, req):
        try:
            device = models.validate_device(req.body)
        except models.ValidationError as e:
            raise ApiError(400, str(e)) from None
        return 201, self.store.add("devices", device)

    def api_device_update(self, req, item_id):
        self._device_or_404(item_id)
        try:
            device = models.validate_device(req.body)
        except models.ValidationError as e:
            raise ApiError(400, str(e)) from None
        self.status.invalidate(item_id)
        return 200, self.store.update("devices", item_id, device)

    def api_device_delete(self, req, item_id):
        if not self.store.delete("devices", item_id):
            raise ApiError(404, "Gerät nicht gefunden")
        self.status.invalidate(item_id)
        return 200, {"ok": True}

    def api_device_action(self, req, item_id, action):
        device = self._device_or_404(item_id)
        if action == "sleep" and device["sleep_method"] == "none":
            raise ApiError(400, "Für dieses Gerät ist keine Schlaf-Methode eingerichtet")
        ok, message = self.run_action(device, action, "manuell")
        return (200 if ok else 502), {"ok": ok, "message": message}

    def api_status(self, req):
        return 200, {"status": self.status.get(self.store.list("devices")), "checked": time.time()}

    # -- Zeitpläne --

    def _validate_schedule(self, body):
        try:
            return models.validate_schedule(body, lambda i: self.store.get("devices", i))
        except models.ValidationError as e:
            raise ApiError(400, str(e)) from None

    @staticmethod
    def _with_next(schedule):
        nxt = next_run(schedule)
        return dict(schedule, next_run=nxt.isoformat(timespec="minutes") if nxt else None)

    def api_schedules(self, req):
        items = sorted(self.store.list("schedules"), key=lambda s: s["time"])
        return 200, {
            "schedules": [self._with_next(s) for s in items],
            "now": datetime.datetime.now().isoformat(timespec="seconds"),
        }

    def api_schedule_create(self, req):
        return 201, self._with_next(self.store.add("schedules", self._validate_schedule(req.body)))

    def api_schedule_update(self, req, item_id):
        if self.store.get("schedules", item_id) is None:
            raise ApiError(404, "Zeitplan nicht gefunden")
        updated = self.store.update("schedules", item_id, self._validate_schedule(req.body))
        return 200, self._with_next(updated)

    def api_schedule_delete(self, req, item_id):
        if not self.store.delete("schedules", item_id):
            raise ApiError(404, "Zeitplan nicht gefunden")
        return 200, {"ok": True}

    # -- Protokoll --

    def api_events(self, req):
        return 200, {"events": self.store.list_events()}

    def api_events_clear(self, req):
        self.store.clear_events()
        return 200, {"ok": True}

    # -- WLAN (über den Dienst wol-wifi) --

    def _wifi(self, payload, timeout=35):
        try:
            res = wifi.request(self.config["wifi_socket"], payload, timeout)
        except (OSError, ValueError):
            raise ApiError(503, "WLAN-Verwaltung nicht erreichbar (Dienst wol-wifi)") from None
        if not res.get("ok"):
            raise ApiError(400, res.get("error") or "WLAN-Befehl fehlgeschlagen")
        return res

    def api_wifi(self, req):
        return 200, self._wifi({"cmd": "status"}, timeout=5)["status"]

    def api_wifi_scan(self, req):
        return 200, self._wifi({"cmd": "scan"})

    def api_wifi_connect(self, req):
        b = req.body
        res = self._wifi({"cmd": "connect", "ssid": b.get("ssid"), "password": b.get("password") or "",
                          "hidden": bool(b.get("hidden"))})
        log.info("WLAN „%s“ eingerichtet von %s", b.get("ssid"), req.client)
        return 200, res

    def api_wifi_forget(self, req):
        return 200, self._wifi({"cmd": "forget", "ssid": req.body.get("ssid")})

    def api_wifi_hotspot(self, req):
        return 200, self._wifi({"cmd": "hotspot_password", "password": req.body.get("password") or ""})

    # -- VPN (ebenfalls über wol-wifi, der die Einrichtung an wol-vpn.service übergibt) --

    def api_vpn(self, req):
        return 200, self._wifi({"cmd": "vpn_status"}, timeout=10)["status"]

    def api_vpn_config(self, req):
        b = req.body
        res = self._wifi({"cmd": "vpn_upload", "config": b.get("config"),
                          "username": b.get("username") or "", "password": b.get("password") or ""}, timeout=15)
        log.info("VPN-Konfiguration hochgeladen von %s", req.client)
        return 200, res

    def api_vpn_enable(self, req):
        enabled = bool(req.body.get("enabled"))
        res = self._wifi({"cmd": "vpn_enable", "enabled": enabled}, timeout=10)
        log.info("VPN %s von %s", "eingeschaltet" if enabled else "ausgeschaltet", req.client)
        return 200, res

    def api_vpn_remove(self, req):
        res = self._wifi({"cmd": "vpn_remove"}, timeout=10)
        log.info("VPN-Konfiguration entfernt von %s", req.client)
        return 200, res

    # -- statische Dateien --

    def static_file(self, name):
        path = os.path.join(WEB_DIR, name)
        mtime = os.stat(path).st_mtime_ns
        cached = self._static_cache.get(name)
        if cached is None or cached[0] != mtime:
            with open(path, "rb") as f:
                data = f.read()
            etag = '"%s"' % hashlib.sha1(data).hexdigest()[:16]
            cached = self._static_cache[name] = (mtime, data, etag)
        return cached[1], cached[2]


class Handler(BaseHTTPRequestHandler):
    server_version = "WOL-Server/" + __version__
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30

    @property
    def app(self):
        return self.server.app

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._head_only = True  # Handler-Instanz lebt pro Keep-Alive-Verbindung
        try:
            self._handle("GET")
        finally:
            self._head_only = False

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    @staticmethod
    def _plain_ip(address):
        ip = ipaddress.ip_address(address.split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return ip

    def _client_ip(self):
        return self._plain_ip(self.client_address[0])

    def _via_hotspot(self):
        try:
            return str(self._plain_ip(self.connection.getsockname()[0])) == wifi.HOTSPOT_ADDRESS
        except (OSError, ValueError):
            return False

    def _handle(self, method):
        try:
            ip = self._client_ip()
            via_hotspot = self._via_hotspot()
            if via_hotspot:
                # Hotspot-Clients sind immer erlaubt (sonst wäre die Einrichtung unmöglich).
                location = captive_redirect(self.headers.get("Host"), self.server.server_address[1])
                if location:
                    if self._read_body() is None:
                        return
                    return self._send(302, b"", None, {"Location": location, "Cache-Control": "no-store"})
            elif not self.app.is_allowed(ip):
                log.warning("Zugriff verweigert für %s", ip)
                self.close_connection = True
                return self._send(403, b"Zugriff nur aus dem lokalen Netz / VPN erlaubt.\n",
                                  "text/plain; charset=utf-8")
            raw = self._read_body()
            if raw is None:
                return
            path = urlsplit(self.path).path
            if path.startswith("/api/"):
                return self._handle_api(method, path, raw, str(ip), via_hotspot)
            if method == "GET" and path in STATIC:
                return self._send_static(*STATIC[path])
            self._send(404, b"Nicht gefunden\n", "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("Unerwarteter Fehler bei %s %s", method, self.path)
            try:
                self._send_json(500, {"error": "Interner Serverfehler"})
            except OSError:
                pass

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._send_json(413, {"error": "Anfrage zu groß"})
            return None
        return self.rfile.read(length) if length else b""

    def _handle_api(self, method, path, raw, client, via_hotspot=False):
        req = None
        try:
            handler, params, needs_auth = self.app.resolve(method, path)
            if method != "GET" and self.headers.get(CSRF_HEADER) != CSRF_VALUE:
                raise ApiError(403, "Ungültige Anfrage (CSRF-Schutz)")
            authenticated = True
            if self.app.auth.required:
                authenticated = self.app.auth.verify_token(self._cookie(COOKIE))
            if needs_auth and not authenticated:
                raise ApiError(401, "Anmeldung erforderlich")
            body = {}
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise ApiError(400, "Ungültiges JSON") from None
                if not isinstance(body, dict):
                    raise ApiError(400, "JSON-Objekt erwartet")
            req = Request(body, client, authenticated, via_hotspot)
            status, payload = handler(req, **params)
        except ApiError as e:
            status, payload = e.status, {"error": e.message}
        self._send_json(status, payload, req.cookies if req else ())

    def _cookie(self, name):
        header = self.headers.get("Cookie")
        if not header:
            return None
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(header)
        except http.cookies.CookieError:
            return None
        return jar[name].value if name in jar else None

    def _send_static(self, name, content_type):
        data, etag = self.app.static_file(name)
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, b"", None, {"ETag": etag, "Cache-Control": "no-cache"})
        self._send(200, data, content_type, {"ETag": etag, "Cache-Control": "no-cache"})

    def _send_json(self, status, payload, cookies=()):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Cache-Control": "no-store"}
        self._send(status, data, "application/json; charset=utf-8", headers, cookies)

    def _send(self, status, data, content_type, headers=None, cookies=()):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in dict(SECURITY_HEADERS, **(headers or {})).items():
            self.send_header(key, value)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if data and not getattr(self, "_head_only", False):
            self.wfile.write(data)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host, port, app):
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        super().__init__((host, port), Handler, bind_and_activate=False)
        self.app = app
        try:
            if self.address_family == socket.AF_INET6:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            self.server_bind()
            self.server_activate()
        except OSError:
            self.server_close()
            raise


def create_server(app):
    host, port = app.config["listen"], int(app.config["port"])
    try:
        return Server(host, port, app)
    except OSError as e:
        if host != "::" or e.errno == errno.EADDRINUSE:
            raise
        log.warning("IPv6 nicht verfügbar (%s) – lausche nur auf IPv4", e)
        return Server("0.0.0.0", port, app)
