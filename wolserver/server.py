# WOL-Server - wolserver/server.py
# HTTP and HTTPS server: JSON API and the static web GUI.
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

"""HTTP server: JSON API under /api/ and the static web GUI."""

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
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, dnsclient, models, netinfo, plugs, power, wifi, wol
from .auth import ApiTokens, Auth, LoginLimiter
from .scheduler import Scheduler, next_run
from .status import StatusMonitor, check_host
from .store import Store
from .tls import TlsState

log = logging.getLogger("wolserver")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
    "/i18n-de.js": ("i18n-de.js", "text/javascript; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
COOKIE = "wol_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "wol-server"
MAX_BODY = 256 * 1024  # network backup with many reservations
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
    def __init__(self, body, client, authenticated, via_hotspot=False, secure=False, query=None, token=None):
        self.body = body
        self.client = client
        self.authenticated = authenticated
        self.via_hotspot = via_hotspot
        self.secure = secure
        self.query = query or {}
        self.token = token  # API token entry for script requests, else None
        self.cookies = []


def captive_redirect(host_header, port):
    """In the hotspot, redirect foreign host names to the GUI.

    After connecting, phones check e.g. connectivitycheck.gstatic.com or captive.apple.com.
    Since the hotspot resolves all names to the Pi, these requests end up here; the redirect
    makes the phone open the setup page as a captive portal.
    """
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        host = host.split("]")[0] + "]"
    else:
        host = host.split(":")[0]
    if host == wifi.HOTSPOT_ADDRESS:
        return None
    return "http://%s%s/" % (wifi.HOTSPOT_ADDRESS, "" if port == 80 else ":%d" % port)


class AccessList:
    """Allowed source networks for GUI and API.

    "auto" stands for localhost, IPv6 link-local and the networks the WOL-Server is currently attached
    to (Wi-Fi, LAN, VPN tunnel). They are determined again regularly, so the list adapts to new
    addresses and VPN networks by itself. Fixed entries apply in addition.
    """

    AUTO_FIXED = ("127.0.0.0/8", "::1/128", "fe80::/10")
    REFRESH = 30  # s between two queries of the interfaces
    RETRY = 5  # look again sooner for an unknown sender (e.g. VPN just connected)

    def __init__(self, entries, source=netinfo.connected_networks, clock=time.monotonic):
        entries = [entries] if isinstance(entries, str) else list(entries)
        self.auto = "auto" in entries
        fixed = [e for e in entries if e != "auto"] + (list(self.AUTO_FIXED) if self.auto else [])
        self.fixed = []
        for entry in fixed:
            net = ipaddress.ip_network(entry, strict=False)
            if net not in (n for n, _ in self.fixed):
                self.fixed.append((net, "auto" if self.auto and entry in self.AUTO_FIXED else "config"))
        self.source, self.clock = source, clock
        self.lock = threading.Lock()
        self.dynamic, self.checked = [], None

    def _refresh(self, min_age):
        now = self.clock()
        if not self.auto or (self.checked is not None and now - self.checked < min_age):
            return
        self.checked = now
        try:
            found = self.source()
        except OSError as e:
            log.warning("Cannot read the networks of the interfaces: %s", e)
            return
        self.dynamic = [(ipaddress.ip_network(n["network"]), n["interface"]) for n in found]

    def _match(self, ip):
        return any(ip.version == net.version and ip in net for net, _ in self.fixed + self.dynamic)

    def allows(self, ip):
        with self.lock:
            self._refresh(self.REFRESH)
            if self._match(ip):
                return True
            self._refresh(self.RETRY)
            return self._match(ip)

    def describe(self):
        """For display: [{"network": "192.168.1.0/24", "source": "wlan0"}, ...]"""
        with self.lock:
            self._refresh(self.REFRESH)
            entries = self.fixed + [(n, src) for n, src in self.dynamic if n not in (f for f, _ in self.fixed)]
            return [{"network": str(net), "source": src} for net, src in entries]


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
        self.tokens = ApiTokens(data_dir)
        self.limiter = LoginLimiter()
        self.access = AccessList(config["allowed_networks"])
        self.tls = TlsState(config["tls_dir"])
        self.identity = power.SshIdentity(data_dir)
        try:
            self.identity.ensure()
        except Exception:  # noqa: BLE001
            log.exception("Could not create SSH key")
        self.status = StatusMonitor(config["status_cache_seconds"])
        self.plugs = plugs.PlugManager(data_dir, on_event=self.store.add_event)
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
            R("GET", r"/api/devices", self.api_devices, token=True),
            R("POST", r"/api/devices", self.api_device_create),
            R("PUT", r"/api/devices/(?P<item_id>\w+)", self.api_device_update),
            R("DELETE", r"/api/devices/(?P<item_id>\w+)", self.api_device_delete),
            # Device by ID or name; also allowed for API tokens (scripts)
            R("POST", r"/api/devices/(?P<item_id>[^/]+)/(?P<action>wake|sleep)", self.api_device_action, token=True),
            R("GET", r"/api/status", self.api_status, token=True),
            R("GET", r"/api/tokens", self.api_tokens),
            R("POST", r"/api/tokens", self.api_token_create),
            R("DELETE", r"/api/tokens/(?P<token_id>[\w-]+)", self.api_token_delete),
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
            # DHCP and DNS (via wol-wifi, carried out by wol-dns-apply.service)
            R("GET", r"/api/net", self.api_net),
            R("GET", r"/api/net/config", self.api_net_config),
            R("PUT", r"/api/net/settings", self.api_net_settings),
            R("POST", r"/api/net/settings/preview", self.api_net_settings_preview),
            R("POST", r"/api/net/reservations", self.api_net_reservation_save),
            R("PUT", r"/api/net/reservations/(?P<item_id>\w+)", self.api_net_reservation_save),
            R("DELETE", r"/api/net/reservations/(?P<item_id>\w+)", self.api_net_reservation_delete),
            R("POST", r"/api/net/records", self.api_net_record_save),
            R("PUT", r"/api/net/records/(?P<item_id>\w+)", self.api_net_record_save),
            R("DELETE", r"/api/net/records/(?P<item_id>\w+)", self.api_net_record_delete),
            R("POST", r"/api/net/apply", self.api_net_apply),
            R("POST", r"/api/net/discard", self.api_net_discard),
            R("GET", r"/api/net/leases", self.api_net_leases),
            R("POST", r"/api/net/leases/(?P<mac>[0-9A-Fa-f:]{17})/release", self.api_net_release),
            R("GET", r"/api/net/unknown", self.api_net_unknown),
            R("POST", r"/api/net/unknown/(?P<mac>[0-9A-Fa-f:]{17})/approve", self.api_net_approve),
            R("POST", r"/api/net/unknown/(?P<mac>[0-9A-Fa-f:]{17})/ignore", self.api_net_ignore),
            R("DELETE", r"/api/net/ignored/(?P<mac>[0-9A-Fa-f:]{17})", self.api_net_unignore),
            R("POST", r"/api/net/import", self.api_net_import),
            R("POST", r"/api/net/import/commit", self.api_net_import_commit),
            R("GET", r"/api/net/export", self.api_net_export),
            R("POST", r"/api/net/restore", self.api_net_restore),
            R("GET", r"/api/net/history", self.api_net_history),
            R("POST", r"/api/net/history/(?P<item_id>\w+)/restore", self.api_net_history_restore),
            R("POST", r"/api/net/test", self.api_net_test),
            R("GET", r"/api/net/stats", self.api_net_stats),
            R("GET", r"/api/net/logs/(?P<kind>queries|dhcp|service)", self.api_net_logs),
            R("POST", r"/api/net/probe", self.api_net_probe),
            R("POST", r"/api/net/restart", self.api_net_restart),
            R("PUT", r"/api/net/static-ip", self.api_net_static_ip),
            # Wireless plugs (driver layer in wolserver/plugs)
            R("GET", r"/api/plugs", self.api_plugs),
            R("POST", r"/api/plugs", self.api_plug_create),
            R("POST", r"/api/plugs/probe", self.api_plug_probe),
            R("PUT", r"/api/plugs/(?P<item_id>\w+)", self.api_plug_update),
            R("DELETE", r"/api/plugs/(?P<item_id>\w+)", self.api_plug_delete),
            R("POST", r"/api/plugs/(?P<item_id>\w+)/(?P<action>on|off|cycle)", self.api_plug_switch),
            R("GET", r"/api/plugs/(?P<item_id>\w+)/power-on-state", self.api_plug_power_on_state),
            R("PUT", r"/api/plugs/(?P<item_id>\w+)/power-on-state", self.api_plug_power_on_state),
        ]
        self._net_logged = self._read_net_logged()

    @staticmethod
    def _route(method, pattern, handler, auth=True, token=False):
        """token=True: also reachable with an API token (scripts); otherwise only with a session."""
        return method, re.compile("^%s$" % pattern), handler, auth, token

    def resolve(self, method, path):
        allowed = False
        for m, regex, handler, needs_auth, token_ok in self.routes:
            match = regex.match(path)
            if match:
                if m == method:
                    return handler, match.groupdict(), needs_auth, token_ok
                allowed = True
        raise ApiError(405 if allowed else 404, "Method not allowed" if allowed else "Not found")

    def is_allowed(self, ip):
        return self.access.allows(ip)

    def start(self):
        self.scheduler.start()

    def stop(self):
        self.scheduler.stop()
        self.status.shutdown()
        self.plugs.shutdown()

    # -- Actions (used by API and scheduler) --

    def wol_source(self):
        """LAN/Wi-Fi interface for magic packets - determined anew for each action (DHCP, Wi-Fi reconnect)."""
        try:
            return netinfo.lan_source(self.config["wol_interface"])
        except OSError:
            return None

    def run_action(self, device, action, source):
        try:
            iface = self.wol_source()
            if iface is None and self.config["wol_interface"]:
                log.warning("Interface %s has no IPv4 address", self.config["wol_interface"])
            source_ip = iface["address"] if iface else None
            if source_ip == wifi.HOTSPOT_ADDRESS:
                ok, message = False, "Not executed: the Pi is not on the home network (setup hotspot active)"
            elif action == "wake":
                wol.send_magic_packet(
                    device["mac"], device["broadcast"], device["wol_port"], source_ip=source_ip
                )
                if iface:
                    ok, message = True, "Magic packet sent to %s via %s" % (device["mac"], iface["name"])
                else:
                    ok, message = True, "Magic packet sent to %s" % device["mac"]
            else:
                ok, message = power.sleep_device(device, self.identity, source_ip=source_ip)
        except (OSError, ValueError) as e:
            ok, message = False, "Error: %s" % e
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
            raise ApiError(429, "Too many failed attempts - try again in a few minutes")
        password = req.body.get("password")
        if isinstance(password, str) and self.auth.check_password(password):
            self.limiter.reset(req.client)
            req.cookies.append(self._session_cookie(self.auth.make_token(), self.auth.session_seconds, req.secure))
            return 200, {"ok": True}
        self.limiter.fail(req.client)
        log.warning("Failed login from %s", req.client)
        time.sleep(1)
        raise ApiError(401, "Wrong password")

    def api_logout(self, req):
        req.cookies.append(self._session_cookie("", 0, req.secure))
        return 200, {"ok": True}

    @staticmethod
    def _session_cookie(value, max_age, secure=False):
        return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict%s" % (COOKIE, value, max_age,
                                                                           "; Secure" if secure else "")

    def api_password(self, req):
        if self.auth.required and not self.auth.check_password(str(req.body.get("current", ""))):
            time.sleep(1)
            raise ApiError(403, "Current password is wrong")
        new = req.body.get("new")
        if not isinstance(new, str):
            raise ApiError(400, "New password missing")
        try:
            self.auth.set_password(new)
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        req.cookies.append(self._session_cookie(self.auth.make_token(), self.auth.session_seconds, req.secure))
        log.info("Password changed by %s", req.client)
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
            "allowed_networks": self.access.describe(),
            "client": req.client,
            "network": self._network_info(),
            "https": dict(self.tls.status(), port=int(self.config["https_port"]) or None),
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

    # -- Devices --

    def _device_or_404(self, item_id):
        device = self.store.get("devices", item_id)
        if device is None:
            raise ApiError(404, "Device not found")
        return device

    def _device_by_ref(self, ref):
        """Device by ID or by name (case-insensitive), e.g. /api/devices/backup-server/wake."""
        ref = unquote(ref)
        device = self.store.get("devices", ref)
        if device is not None:
            return device
        matches = [d for d in self.store.list("devices") if d["name"].casefold() == ref.casefold()]
        if len(matches) > 1:
            raise ApiError(409, "Several devices are named \"%s\" - use the ID" % ref)
        if not matches:
            raise ApiError(404, "Device not found")
        return matches[0]

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
            raise ApiError(404, "Device not found")
        self.status.invalidate(item_id)
        return 200, {"ok": True}

    MAX_WAIT = 300  # s

    def api_device_action(self, req, item_id, action):
        device = self._device_by_ref(item_id)
        if action == "sleep" and device["sleep_method"] == "none":
            raise ApiError(400, "No sleep method is configured for this device")
        try:
            wait = min(self.MAX_WAIT, max(0, int((req.query.get("wait") or ["0"])[0])))
        except ValueError:
            raise ApiError(400, "wait: number of seconds expected") from None
        if wait and not device.get("host"):
            raise ApiError(400, "wait needs a host or IP address for the device")
        source = "API: %s" % req.token["name"] if req.token else "manual"
        ok, message = self.run_action(device, action, source)
        if not ok:
            return 502, {"ok": False, "message": message}
        result = {"ok": True, "message": message, "device": device["name"]}
        if wait:
            # Scripts can rely on the device: wake -> until it answers, sleep -> until it is gone
            want = action == "wake"
            start = time.monotonic()
            online = check_host(device["host"])
            while online != want and time.monotonic() - start < wait:
                time.sleep(3)
                online = check_host(device["host"])
            self.status.invalidate(device["id"])
            result.update(online=online, waited=round(time.monotonic() - start))
            if online != want:
                result.update(ok=False, message=("%s did not come online within %d s" if want
                                                 else "%s is still online after %d s") % (device["name"], wait))
                return 504, result
        return 200, result

    # -- API tokens (managed in the GUI with a session, used by scripts) --

    def api_tokens(self, req):
        if req.token:
            raise ApiError(403, "API tokens cannot manage tokens")
        return 200, {"tokens": self.tokens.list()}

    def api_token_create(self, req):
        if req.token:
            raise ApiError(403, "API tokens cannot manage tokens")
        try:
            entry = self.tokens.create(req.body.get("name"))
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        log.info("API token \"%s\" created by %s", entry["name"], req.client)
        return 201, entry

    def api_token_delete(self, req, token_id):
        if req.token:
            raise ApiError(403, "API tokens cannot manage tokens")
        if not self.tokens.revoke(token_id):
            raise ApiError(404, "Token not found")
        log.info("API token %s revoked by %s", token_id, req.client)
        return 200, {"ok": True}

    def api_status(self, req):
        return 200, {"status": self.status.get(self.store.list("devices")), "checked": time.time()}

    # -- Schedules --

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
            raise ApiError(404, "Schedule not found")
        updated = self.store.update("schedules", item_id, self._validate_schedule(req.body))
        return 200, self._with_next(updated)

    def api_schedule_delete(self, req, item_id):
        if not self.store.delete("schedules", item_id):
            raise ApiError(404, "Schedule not found")
        return 200, {"ok": True}

    # -- Log --

    def api_events(self, req):
        return 200, {"events": self.store.list_events()}

    def api_events_clear(self, req):
        self.store.clear_events()
        return 200, {"ok": True}

    # -- Wi-Fi (via the wol-wifi service) --

    def _wifi(self, payload, timeout=35):
        try:
            res = wifi.request(self.config["wifi_socket"], payload, timeout)
        except (OSError, ValueError):
            raise ApiError(503, "Wi-Fi management not reachable (wol-wifi service)") from None
        if not res.get("ok"):
            raise ApiError(400, res.get("error") or "Wi-Fi command failed")
        return res

    def api_wifi(self, req):
        return 200, self._wifi({"cmd": "status"}, timeout=5)["status"]

    def api_wifi_scan(self, req):
        return 200, self._wifi({"cmd": "scan"})

    def api_wifi_connect(self, req):
        b = req.body
        res = self._wifi({"cmd": "connect", "ssid": b.get("ssid"), "password": b.get("password") or "",
                          "hidden": bool(b.get("hidden")), "ipv4": b.get("ipv4")})
        log.info("Wi-Fi \"%s\" set up by %s", b.get("ssid"), req.client)
        return 200, res

    def api_wifi_forget(self, req):
        return 200, self._wifi({"cmd": "forget", "ssid": req.body.get("ssid")})

    def api_wifi_hotspot(self, req):
        return 200, self._wifi({"cmd": "hotspot_password", "password": req.body.get("password") or ""})

    # -- VPN (also via wol-wifi, which hands the setup over to wol-vpn.service) --

    def api_vpn(self, req):
        return 200, self._wifi({"cmd": "vpn_status"}, timeout=10)["status"]

    def api_vpn_config(self, req):
        b = req.body
        res = self._wifi({"cmd": "vpn_upload", "config": b.get("config"),
                          "username": b.get("username") or "", "password": b.get("password") or ""}, timeout=15)
        log.info("VPN configuration uploaded by %s", req.client)
        return 200, res

    def api_vpn_enable(self, req):
        enabled = bool(req.body.get("enabled"))
        res = self._wifi({"cmd": "vpn_enable", "enabled": enabled}, timeout=10)
        log.info("VPN %s by %s", "switched on" if enabled else "switched off", req.client)
        return 200, res

    def api_vpn_remove(self, req):
        res = self._wifi({"cmd": "vpn_remove"}, timeout=10)
        log.info("VPN configuration removed by %s", req.client)
        return 200, res

    # -- DHCP and DNS --

    def _net(self, cmd, timeout=15, **args):
        try:
            res = wifi.request(self.config["wifi_socket"], dict(args, cmd=cmd), timeout)
        except (OSError, ValueError):
            raise ApiError(503, "Network management not reachable (wol-wifi service)") from None
        if not res.get("ok"):
            raise ApiError(400, res.get("error") or "Network command failed")
        res.pop("ok", None)
        return res

    def _net_logged_path(self):
        return os.path.join(self.config["data_dir"], "net-protokolliert")

    def _read_net_logged(self):
        return _read_text(self._net_logged_path()) or ""

    def _log_net_job(self, status):
        """Write completed network changes to the log once."""
        job = status.get("job") or {}
        if job.get("state") not in ("ok", "failed") or job.get("action") not in ("apply", "release"):
            return
        if job.get("id") == self._net_logged:
            return
        self._net_logged = job["id"]
        try:
            with open(self._net_logged_path(), "w", encoding="utf-8") as f:
                f.write(job["id"])
        except OSError:
            pass
        change = status.get("last_change") or {}
        if job["state"] == "ok" and change.get("request") == job["id"] and change.get("changes"):
            items = change["changes"]
            message = "; ".join(items[:4]) + (" (+%d more)" % (len(items) - 4) if len(items) > 4 else "")
        else:
            message = job.get("message") or ""
        self.store.add_event(device_id=None, device="Network", action="net",
                             source=change.get("client") or "manual", ok=job["state"] == "ok", message=message)

    def api_net(self, req):
        status = self._net("net_status", timeout=10)
        self._log_net_job(status)
        return 200, status

    def api_net_config(self, req):
        res = self._net("net_config")
        res["devices"] = {d["mac"].lower(): {"id": d["id"], "name": d["name"]} for d in self.store.list("devices")}
        res["plugs"] = self.plugs.by_mac()
        return 200, res

    def api_net_settings(self, req):
        return 200, self._net("net_settings", settings=req.body, client=req.client)

    def api_net_settings_preview(self, req):
        return 200, self._net("net_settings", settings=req.body, preview=True)

    def api_net_reservation_save(self, req, item_id=None):
        data = dict(req.body)
        if item_id:
            data["id"] = item_id
        else:
            data.pop("id", None)
        return 200, self._net("net_reservation_save", reservation=data, client=req.client)

    def api_net_reservation_delete(self, req, item_id):
        return 200, self._net("net_reservation_delete", id=item_id, client=req.client)

    def api_net_record_save(self, req, item_id=None):
        data = dict(req.body)
        if item_id:
            data["id"] = item_id
        else:
            data.pop("id", None)
        return 200, self._net("net_record_save", record=data, client=req.client)

    def api_net_record_delete(self, req, item_id):
        return 200, self._net("net_record_delete", id=item_id, client=req.client)

    def api_net_apply(self, req):
        res = self._net("net_apply", client=req.client)
        log.info("Network changes applied by %s: %s", req.client, "; ".join(res.get("changes") or []))
        return 200, res

    def api_net_discard(self, req):
        return 200, self._net("net_discard")

    def api_net_leases(self, req):
        res = self._net("net_leases")
        devices = {d["mac"].lower(): d["id"] for d in self.store.list("devices")}
        for lease in res.get("leases", []):
            lease["device"] = devices.get(lease["mac"])
        return 200, res

    def api_net_release(self, req, mac):
        return 200, self._net("net_release", mac=mac)

    def api_net_unknown(self, req):
        return 200, self._net("net_unknown")

    def api_net_approve(self, req, mac):
        b = req.body
        res = self._net("net_approve", mac=mac, name=b.get("name"), ip=b.get("ip"), comment=b.get("comment"),
                        client=req.client)
        log.info("Device %s approved by %s", mac, req.client)
        return 200, res

    def api_net_ignore(self, req, mac):
        return 200, self._net("net_ignore", mac=mac, comment=req.body.get("comment") or "")

    def api_net_unignore(self, req, mac):
        return 200, self._net("net_unignore", mac=mac)

    def api_net_import(self, req):
        return 200, self._net("net_import_preview", text=req.body.get("text"))

    def api_net_import_commit(self, req):
        b = req.body
        return 200, self._net("net_import_commit", preview=b.get("preview"), replace=bool(b.get("replace")),
                              take_settings=bool(b.get("take_settings", True)))

    def api_net_export(self, req):
        return 200, self._net("net_export")["export"]

    def api_net_restore(self, req):
        return 200, self._net("net_restore", data=req.body.get("data"))

    def api_net_history(self, req):
        return 200, self._net("net_history")

    def api_net_history_restore(self, req, item_id):
        return 200, self._net("net_history_restore", id=item_id)

    def _dns_target(self):
        cfg = self._net("net_config")["config"]
        return "127.0.0.1", cfg["server"]["port"], cfg

    def api_net_test(self, req):
        server, port, cfg = self._dns_target()
        if not cfg["dns"]["enabled"]:
            raise ApiError(400, "DNS is switched off. Test queries require a switched-on and applied DNS service.")
        text = req.body.get("query")
        if not isinstance(text, str):
            raise ApiError(400, "Enter a name or an IP address")
        query = text.strip()
        if query and "." not in query and ":" not in query:
            query = "%s.%s" % (query, cfg["dns"]["domain"])
        try:
            started = time.monotonic()
            res = dnsclient.lookup(query, server, port)
            res["ms"] = round((time.monotonic() - started) * 1000, 1)
        except dnsclient.DnsError as e:
            raise ApiError(400, str(e)) from None
        return 200, res

    def api_net_stats(self, req):
        server, port, cfg = self._dns_target()
        if not cfg["dns"]["enabled"]:
            return 200, {"available": False}
        try:
            stats = dnsclient.dnsmasq_stats(server, port)
        except dnsclient.DnsError as e:
            return 200, {"available": False, "error": str(e)}
        return 200, dict(stats, available=True)

    def api_net_logs(self, req, kind):
        return 200, self._net("net_logs", timeout=25, kind=kind, lines=200)

    def api_net_probe(self, req):
        return 200, self._net("net_probe")

    def api_net_restart(self, req):
        return 200, self._net("net_restart")

    def api_net_static_ip(self, req):
        res = self._net("net_static_ip", timeout=40, enabled=bool(req.body.get("enabled")))
        log.info("%s (by %s)", res.get("message"), req.client)
        return 200, res

    # -- Wireless plugs --

    def _plug_call(self, fn, *args, **kwargs):
        """Driver and validation errors as API errors. Never 401: the GUI would show its login page."""
        try:
            return fn(*args, **kwargs)
        except KeyError:
            raise ApiError(404, "Plug not found") from None
        except plugs.ConfirmRequired:
            raise
        except plugs.base.PlugUnreachable as e:
            raise ApiError(502, str(e)) from None
        except plugs.PlugError as e:
            raise ApiError(400, str(e)) from None

    def api_plugs(self, req):
        return 200, {"plugs": self.plugs.list(), "drivers": plugs.describe_drivers()}

    def api_plug_probe(self, req):
        b = req.body
        return 200, {"info": self._plug_call(self.plugs.probe, b.get("host"), b.get("driver"))}

    def api_plug_create(self, req):
        plug = self._plug_call(self.plugs.add, req.body)
        log.info("Plug %s added by %s", plug["name"], req.client)
        return 201, plug

    def api_plug_update(self, req, item_id):
        return 200, self._plug_call(self.plugs.update, item_id, req.body)

    def api_plug_delete(self, req, item_id):
        plug = self._plug_call(self.plugs.delete, item_id)
        log.info("Plug %s removed by %s", plug["name"], req.client)
        return 200, {"ok": True}

    def api_plug_switch(self, req, item_id, action):
        try:
            message = self._plug_call(self.plugs.switch, item_id, action, bool(req.body.get("confirm")), "manual")
        except plugs.ConfirmRequired as e:
            # "ok" in the answer: the GUI asks and sends the request again with confirm
            return 409, {"ok": False, "confirm": True, "power_w": e.power_w, "message": str(e)}
        return 200, {"ok": True, "message": message}

    def api_plug_power_on_state(self, req, item_id):
        if req.body.get("state") is not None:
            self._plug_call(self.plugs.set_power_on_state, item_id, req.body.get("state"))
        return 200, {"state": self._plug_call(self.plugs.power_on_state, item_id)}

    # -- static files --

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
        self._head_only = True  # one handler instance lives per keep-alive connection
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
                # Hotspot clients are always allowed (otherwise setup would be impossible).
                location = captive_redirect(self.headers.get("Host"), self.server.server_address[1])
                if location:
                    if self._read_body() is None:
                        return
                    return self._send(302, b"", None, {"Location": location, "Cache-Control": "no-store"})
            elif not self.app.is_allowed(ip):
                log.warning("Access denied for %s", ip)
                self.close_connection = True
                return self._send(403, b"Access is only allowed from the local network / VPN.\n",
                                  "text/plain; charset=utf-8")
            raw = self._read_body()
            if raw is None:
                return
            location = None if via_hotspot or ip.is_loopback else self._https_location()
            if location:
                return self._send(308, b"", None, {"Location": location, "Cache-Control": "no-store"})
            path = urlsplit(self.path).path
            if path.startswith("/api/"):
                return self._handle_api(method, path, raw, str(ip), via_hotspot)
            if method == "GET" and path in STATIC:
                return self._send_static(*STATIC[path])
            self._send(404, b"Not found\n", "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("Unexpected error in %s %s", method, self.path)
            try:
                self._send_json(500, {"error": "Internal server error"})
            except OSError:
                pass

    def _https_location(self):
        """Target of the redirect to HTTPS, or None (already HTTPS, switched off, no valid certificate)."""
        https_port = getattr(self.server, "https_port", None)
        if self.secure or not self.app.config["https_redirect"] or not https_port:
            return None
        valid = self.app.tls.host()
        if not valid:
            return None
        host = self.app.config["https_host"] or valid
        port = "" if https_port == 443 else ":%d" % https_port
        return "https://%s%s%s" % (host, port, self.path if self.path.startswith("/") else "/")

    @property
    def secure(self):
        return isinstance(self.connection, ssl.SSLSocket)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._send_json(413, {"error": "Request too large"})
            return None
        return self.rfile.read(length) if length else b""

    def _handle_api(self, method, path, raw, client, via_hotspot=False):
        req = None
        try:
            handler, params, needs_auth, token_ok = self.app.resolve(method, path)
            token = None
            bearer = self.headers.get("Authorization", "")
            if bearer.lower().startswith("bearer "):
                # Scripts: the browser never sends this header by itself, so no CSRF header is needed
                token = self.app.tokens.verify(bearer[7:].strip())
                if token is None:
                    raise ApiError(401, "Invalid API token")
                if not token_ok:
                    raise ApiError(403, "API tokens may only list devices, query the status and wake devices or put them to sleep")
                authenticated = True
            else:
                if method != "GET" and self.headers.get(CSRF_HEADER) != CSRF_VALUE:
                    raise ApiError(403, "Invalid request (CSRF protection)")
                authenticated = True
                if self.app.auth.required:
                    authenticated = self.app.auth.verify_token(self._cookie(COOKIE))
            if needs_auth and not authenticated:
                raise ApiError(401, "Login required")
            body = {}
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise ApiError(400, "Invalid JSON") from None
                if not isinstance(body, dict):
                    raise ApiError(400, "JSON object expected")
            req = Request(body, client, authenticated, via_hotspot, self.secure,
                          parse_qs(urlsplit(self.path).query), token)
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


class TlsServer(Server):
    """HTTPS: the TLS handshake runs in the connection's thread, not in the accept loop."""

    def finish_request(self, request, client_address):
        context = self.app.tls.current()
        if context is None:
            return  # no certificate yet: close the connection
        request.settimeout(Handler.timeout)
        try:
            wrapped = context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as e:
            log.debug("TLS handshake with %s failed: %s", client_address[0], e)
            return
        try:
            super().finish_request(wrapped, client_address)
        finally:
            # wrap_socket detached the original socket; socketserver only closes that one
            self.shutdown_request(wrapped)


def _bind(cls, app, port):
    host = app.config["listen"]
    try:
        server = cls(host, port, app)
    except OSError as e:
        if host != "::" or e.errno == errno.EADDRINUSE:
            raise
        log.warning("IPv6 not available (%s) - listening on IPv4 only", e)
        server = cls("0.0.0.0", port, app)
    https_port = int(app.config["https_port"])
    server.https_port = https_port if cls is Server else None
    return server


def create_server(app):
    return _bind(Server, app, int(app.config["port"]))


def create_https_server(app):
    """HTTPS server or None (switched off or port not available)."""
    port = int(app.config["https_port"])
    if not port:
        return None
    try:
        return _bind(TlsServer, app, port)
    except OSError as e:
        log.warning("HTTPS on port %d not possible: %s", port, e)
        return None
