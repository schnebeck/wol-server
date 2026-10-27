# WOL-Server - wolserver/plugs/manager.py
# Plug manager: stored plugs, live values, switching with the safety rules.
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

"""The core of the plug support. It talks to every plug through the driver interface, so the safety
rules apply to every vendor alike. Plugs are stored in plugs.json (mode 0600: it holds the passwords
of the plugs, which never leave the server)."""

import copy
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .. import models
from ..wol import normalize_mac
from .base import POWER_ON_STATES, PlugError, PlugUnreachable, get_driver, probe_all
from .http import HttpClient

log = logging.getLogger("wolserver")

ACTIONS = ("on", "off", "cycle")


class ConfirmRequired(PlugError):
    """Switching off would cut a device that is drawing power: the user has to confirm."""

    def __init__(self, message, power_w=None):
        super().__init__(message)
        self.power_w = power_w


def _text(data, key, maxlen, label, required=False):
    value = data.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value.strip()) > maxlen or any(ord(c) < 32 for c in value):
        raise PlugError("%s is invalid or too long (max. %d characters)" % (label, maxlen))
    value = value.strip()
    if required and not value:
        raise PlugError("%s must not be empty" % label)
    return value


class PlugManager:
    CACHE_SECONDS = 5
    CYCLE_SECONDS = 10
    DEFAULT_STANDBY_W = 5.0

    def __init__(self, data_dir, http=None, on_event=None, workers=4, clock=time.time):
        self.path = os.path.join(data_dir, "plugs.json")
        self.http = http or HttpClient()
        self.on_event = on_event or (lambda **event: None)
        self.clock = clock
        self._lock = threading.RLock()
        self._plugs = []
        self._live = {}  # plug id -> (monotonic ts, host, result)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="plugs")
        self._load()

    # -- storage --

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            broken = "%s.broken-%d" % (self.path, time.time())
            log.error("Plug file %s unreadable (%s) - moving it to %s", self.path, e, broken)
            os.replace(self.path, broken)
            return
        if isinstance(data.get("plugs"), list):
            self._plugs = data["plugs"]

    def _save(self):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"plugs": self._plugs}, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _find(self, plug_id):
        for plug in self._plugs:
            if plug["id"] == plug_id:
                return plug
        raise KeyError(plug_id)

    @staticmethod
    def public(plug):
        """Plug for the GUI: without the password."""
        out = {k: v for k, v in plug.items() if k != "password"}
        out["has_password"] = bool(plug.get("password"))
        return out

    def get(self, plug_id):
        with self._lock:
            return copy.deepcopy(self._find(plug_id))

    def by_mac(self):
        with self._lock:
            return {p["mac"]: {"id": p["id"], "name": p["name"]} for p in self._plugs if p.get("mac")}

    # -- validation --

    def _validate(self, data, old=None):
        old = old or {}
        plug = dict(old)
        plug["name"] = _text(data, "name", 64, "Name", required=True)
        host = _text(data, "host", 253, "Host", required=True)
        if not models.valid_host(host):
            raise PlugError("Host must be an IP address or a host name")
        plug["host"] = host
        driver = data.get("driver") or old.get("driver")
        get_driver(driver)
        plug["driver"] = driver
        try:
            plug["channel"] = int(data.get("channel", old.get("channel", 0)) or 0)
        except (TypeError, ValueError):
            raise PlugError("Channel: number expected") from None
        if not 0 <= plug["channel"] <= 15:
            raise PlugError("Channel must be between 0 and 15")
        password = data.get("password")
        if password is not None and (not isinstance(password, str) or len(password) > 128):
            raise PlugError("Password is invalid or too long")
        if password:
            plug["password"] = password
        elif data.get("clear_password"):
            plug["password"] = ""
        else:
            plug.setdefault("password", "")
        mac = data.get("mac")
        if mac:
            try:
                plug["mac"] = normalize_mac(mac).lower()
            except ValueError as e:
                raise PlugError(str(e)) from None
        label = _text(data, "consumer_label", 64, "Consumer")
        plug["consumer"] = {"kind": "label", "label": label} if label else None
        try:
            standby = data.get("standby_w", old.get("standby_w", self.DEFAULT_STANDBY_W))
            plug["standby_w"] = round(float(self.DEFAULT_STANDBY_W if standby in (None, "") else standby), 1)
        except (TypeError, ValueError):
            raise PlugError("Standby threshold: number expected") from None
        if not 0 <= plug["standby_w"] <= 500:
            raise PlugError("Standby threshold must be between 0 and 500 W")
        return plug

    def _driver(self, plug):
        return get_driver(plug["driver"])(plug["host"], plug.get("channel", 0), plug.get("password", ""), self.http)

    # -- detection, add, change, delete --

    def probe(self, host, driver="auto"):
        """What is at host? Read-only; for the dialog before adding."""
        if not isinstance(host, str) or not models.valid_host(host.strip()):
            raise PlugError("Host must be an IP address or a host name")
        host = host.strip()
        info = probe_all(host, self.http) if driver in (None, "", "auto") else get_driver(driver).probe(host, self.http)
        if info is None:
            raise PlugError("No supported plug found at %s" % host)
        return info.to_dict()

    def add(self, data):
        data = dict(data)
        host = data.get("host")
        info = self.probe(host, data.get("driver"))
        data["driver"] = info["driver"]
        data["mac"] = data.get("mac") or info["mac"]  # the reservation's MAC wins
        if not (data.get("name") or "").strip():
            data["name"] = info["name"] or host
        plug = self._validate(data)
        plug.update(model=info["model"], firmware=info["firmware"])
        status = self._driver(plug).status()  # checks the password before saving
        with self._lock:
            if plug.get("mac") and any(p.get("mac") == plug["mac"] and p.get("channel", 0) == plug["channel"]
                                       for p in self._plugs):
                raise PlugError("This plug is already set up")
            plug["id"] = uuid.uuid4().hex[:12]
            plug["created"] = self.clock()
            self._plugs.append(plug)
            self._save()
            self._live[plug["id"]] = (time.monotonic(), plug["host"], {"status": status.to_dict(), "error": None})
        log.info("Plug %s added (%s at %s)", plug["name"], plug["driver"], plug["host"])
        return self.public(plug)

    def update(self, plug_id, data):
        with self._lock:
            old = self._find(plug_id)
            plug = self._validate(data, old)
            if plug["host"] != old["host"] or plug["password"] != old.get("password"):
                self._driver(plug).status()  # new address or password: check before saving
            self._plugs[self._plugs.index(old)] = plug
            self._save()
            self._live.pop(plug_id, None)
            return self.public(plug)

    def delete(self, plug_id):
        with self._lock:
            plug = self._find(plug_id)
            self._plugs.remove(plug)
            self._save()
            self._live.pop(plug_id, None)
            return plug

    # -- live values --

    def _fetch(self, plug):
        try:
            result = {"status": self._driver(plug).status().to_dict(), "error": None}
        except PlugError as e:
            result = {"status": None, "error": str(e), "unreachable": isinstance(e, PlugUnreachable)}
        with self._lock:
            self._live[plug["id"]] = (time.monotonic(), plug["host"], result)
        return result

    def live(self, plug_id, max_age=CACHE_SECONDS):
        with self._lock:
            plug = copy.deepcopy(self._find(plug_id))
            cached = self._live.get(plug_id)
        if cached and cached[1] == plug["host"] and time.monotonic() - cached[0] < max_age:
            return cached[2]
        return self._fetch(plug)

    def list(self, live=True):
        """All plugs with live values (at most CACHE_SECONDS old, fetched in parallel)."""
        with self._lock:
            plugs = copy.deepcopy(self._plugs)
            cached = dict(self._live)
        if not live:
            return [self.public(p) for p in plugs]
        now = time.monotonic()

        def current(p):
            entry = cached.get(p["id"])
            return entry and entry[1] == p["host"] and now - entry[0] < self.CACHE_SECONDS

        stale = [p for p in plugs if not current(p)]
        fresh = dict(zip([p["id"] for p in stale], self._pool.map(self._fetch, stale)))
        return [dict(self.public(p), live=fresh.get(p["id"]) or cached[p["id"]][2]) for p in plugs]

    # -- switching --

    def switch(self, plug_id, action, confirm=False, source="manual"):
        if action not in ACTIONS:
            raise PlugError("Unknown action: %s" % action)
        plug = self.get(plug_id)
        driver = self._driver(plug)
        if action in ("off", "cycle") and not confirm:
            # Rule 1: cut the power only without asking when nothing is drawing more than standby
            if "metering" not in driver.capabilities:
                raise ConfirmRequired("This plug cannot measure: it is unknown whether the device is still running")
            status = driver.status()
            if status.on and status.power_w is not None and status.power_w > plug.get("standby_w", self.DEFAULT_STANDBY_W):
                consumer = (plug.get("consumer") or {}).get("label") or plug["name"]
                raise ConfirmRequired("%s is drawing %s W" % (consumer, _fmt_w(status.power_w)), status.power_w)
        try:
            if action == "cycle":
                driver.cycle(self.CYCLE_SECONDS)
                message = "Power cycle: off, on again after %d s" % self.CYCLE_SECONDS
            else:
                driver.switch(action == "on")
                message = "Power on" if action == "on" else "Power off"
            ok = True
        except PlugError as e:
            ok, message = False, str(e)
        with self._lock:
            self._live.pop(plug_id, None)
        (log.info if ok else log.warning)("plug %s %s (%s): %s", action, plug["name"], source, message)
        self.on_event(device_id=None, device=plug["name"], action="power_" + action, source=source, ok=ok,
                      message=message)
        if not ok:
            raise PlugError(message)
        return message

    def power_on_state(self, plug_id):
        return self._driver(self.get(plug_id)).power_on_state()

    def set_power_on_state(self, plug_id, state):
        if state not in POWER_ON_STATES:
            raise PlugError("Unknown state: %s" % state)
        self._driver(self.get(plug_id)).set_power_on_state(state)

    def shutdown(self):
        self._pool.shutdown(wait=False)


def _fmt_w(watts):
    return ("%.1f" % watts).rstrip("0").rstrip(".") if watts < 10 else "%d" % round(watts)
