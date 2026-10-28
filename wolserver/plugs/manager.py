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
rules, the alerts and the energy log apply to every vendor alike. Plugs are stored in plugs.json
(mode 0600: it holds the passwords of the plugs, which never leave the server), the energy values in
energy.db."""

import copy
import json
import logging
import os
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .. import models
from ..wol import normalize_mac
from .base import EXPOSURES, POWER_ON_STATES, PlugError, PlugUnreachable, get_driver, probe_all
from .energy import EnergyLog
from .http import HttpClient

log = logging.getLogger("wolserver")

ACTIONS = ("on", "off", "cycle")
WAKE_METHODS = ("wol", "power", "power_wol")
ERROR_TEXTS = {"overtemp": "Overtemperature", "overpower": "Overload", "overvoltage": "Overvoltage",
               "undervoltage": "Undervoltage"}
EXPOSURE_TEXTS = {"access_point": "access point off", "bluetooth": "Bluetooth off", "matter": "Matter off",
                  "cloud": "cloud off"}


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
    POLL_SECONDS = 60  # background reading for the energy log and the alerts
    DEFAULT_STANDBY_W = 5.0
    # Alerts
    WELD_W = 1.0  # relay off, but more than this flows ...
    WELD_SECONDS = 30  # ... for longer than this: contacts may be welded
    TEMP_WARN_C = 70.0
    UNREACHABLE_SECONDS = 300
    RATED_CYCLES = 10000  # switching cycles of the relay (Shelly Plug M Gen3)
    WEAR_WARN = 0.8

    def __init__(self, data_dir, http=None, on_event=None, workers=4, clock=time.time, device_lookup=None):
        self.path = os.path.join(data_dir, "plugs.json")
        self.http = http or HttpClient()
        self.on_event = on_event or (lambda **event: None)
        self.device_lookup = device_lookup or (lambda device_id: None)  # id -> device or None
        self.clock = clock
        self._lock = threading.RLock()
        self._plugs = []
        self.settings = {"price_kwh": None, "currency": "€"}
        self._live = {}  # plug id -> (monotonic ts, host, result)
        self._alerts = {}  # plug id -> {"active": {key: message}, "since": {key: monotonic ts}}
        self._summary = None
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="plugs")
        self._stop = threading.Event()
        self._thread = None
        self._load()
        self.energy = EnergyLog(os.path.join(data_dir, "energy.db"), clock=clock)

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
        if isinstance(data.get("settings"), dict):
            self.settings.update(data["settings"])

    def _save(self):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"plugs": self._plugs, "settings": self.settings}, f, ensure_ascii=False, indent=1)
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

    def get_public(self, plug_id):
        return self.public(self.get(plug_id))

    def by_mac(self):
        with self._lock:
            return {p["mac"]: {"id": p["id"], "name": p["name"]} for p in self._plugs if p.get("mac")}

    def for_device(self, device_id):
        """The plug a device is plugged into, or None."""
        with self._lock:
            for plug in self._plugs:
                if (plug.get("consumer") or {}).get("id") == device_id:
                    return copy.deepcopy(plug)
        return None

    def consumer_name(self, plug):
        consumer = plug.get("consumer") or {}
        if consumer.get("kind") == "self":
            return "WOL-Server"
        if consumer.get("kind") == "device":
            device = self.device_lookup(consumer.get("id"))
            return device["name"] if device else plug["name"]
        return consumer.get("label") or plug["name"]

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
        if "consumer_device" in data or "consumer_label" in data or "consumer_self" in data:
            device_id = data.get("consumer_device")
            label = _text(data, "consumer_label", 64, "Consumer")
            if data.get("consumer_self"):
                plug["consumer"] = {"kind": "self"}
            elif device_id:
                if self.device_lookup(device_id) is None:
                    raise PlugError("Unknown device")
                plug["consumer"] = {"kind": "device", "id": device_id}
            else:
                plug["consumer"] = {"kind": "label", "label": label} if label else None
        else:
            plug.setdefault("consumer", None)
        try:
            standby = data.get("standby_w", old.get("standby_w", self.DEFAULT_STANDBY_W))
            plug["standby_w"] = round(float(self.DEFAULT_STANDBY_W if standby in (None, "") else standby), 1)
        except (TypeError, ValueError):
            raise PlugError("Standby threshold: number expected") from None
        if not 0 <= plug["standby_w"] <= 500:
            raise PlugError("Standby threshold must be between 0 and 500 W")
        self._validate_device_options(data, plug)
        # Rule 6: the plug of the WOL-Server itself, the router or a switch is never switched off (monitoring is fine)
        plug["locked"] = (bool(data.get("locked", old.get("locked", False)))
                          or (plug.get("consumer") or {}).get("kind") == "self")
        return plug

    def _validate_device_options(self, data, plug):
        """What a plug does for its device: wake method, power cut after sleep, running threshold."""
        method = data.get("wake_method", plug.get("wake_method")) or "wol"
        if method not in WAKE_METHODS:
            raise PlugError("Unknown wake method")
        plug["wake_method"] = method
        plug["cut_after_sleep"] = bool(data.get("cut_after_sleep", plug.get("cut_after_sleep", False)))
        running = data.get("running_w", plug.get("running_w"))
        if running in (None, ""):
            plug["running_w"] = None
        else:
            try:
                plug["running_w"] = round(float(running), 1)
            except (TypeError, ValueError):
                raise PlugError("Running threshold: number expected") from None
            if not plug["standby_w"] < plug["running_w"] <= 4000:
                raise PlugError("Running threshold must be above the standby threshold (max. 4000 W)")

    def _check_unique_consumer(self, plug):
        """A device (and the WOL-Server itself) has at most one plug."""
        consumer = plug.get("consumer") or {}
        if consumer.get("kind") == "self":
            for other in self._plugs:
                if other["id"] != plug.get("id") and (other.get("consumer") or {}).get("kind") == "self":
                    raise PlugError("The WOL-Server is already plugged into %s" % other["name"])
        if consumer.get("kind") == "device":
            for other in self._plugs:
                if other["id"] != plug.get("id") and (other.get("consumer") or {}).get("id") == consumer["id"]:
                    raise PlugError("This device is already plugged into %s" % other["name"])

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
            self._check_unique_consumer(plug)
            plug["id"] = uuid.uuid4().hex[:12]
            plug["created"] = self.clock()
            self._plugs.append(plug)
            self._save()
            self._live[plug["id"]] = (time.monotonic(), plug["host"], {"status": status.to_dict(), "error": None})
        log.info("Plug %s added (%s at %s)", plug["name"], plug["driver"], plug["host"])
        self.energy.record(plug["id"], status.to_dict())  # the first reading counts like every other
        try:
            self.security(plug["id"])  # so the overview can point out an open plug right away
        except PlugError:
            pass
        return self.get_public(plug["id"])

    def update(self, plug_id, data):
        with self._lock:
            old = self._find(plug_id)
            plug = self._validate(data, old)
            self._check_unique_consumer(plug)
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
            self._alerts.pop(plug_id, None)
        self.energy.forget(plug_id)
        return plug

    def link_device(self, device_id, plug_id, options=None):
        """Device side of the link: plug_id (or None) becomes the plug of the device, with its options."""
        with self._lock:
            target = self._find(plug_id) if plug_id else None
            changed = False
            for plug in self._plugs:
                if plug is not target and (plug.get("consumer") or {}).get("id") == device_id:
                    plug["consumer"] = None
                    changed = True
            if target is not None:
                updated = dict(target, consumer={"kind": "device", "id": device_id})
                updated.setdefault("standby_w", self.DEFAULT_STANDBY_W)
                self._validate_device_options(options or {}, updated)
                self._plugs[self._plugs.index(target)] = updated
                changed = True
            if changed:
                self._save()

    def unlink_device(self, device_id, name):
        """The device is deleted: its plug keeps the name as a plain label."""
        with self._lock:
            for plug in self._plugs:
                if (plug.get("consumer") or {}).get("id") == device_id:
                    plug["consumer"] = {"kind": "label", "label": name}
                    self._save()

    def set_settings(self, data):
        price = data.get("price_kwh", self.settings.get("price_kwh"))
        if price in (None, ""):
            price = None
        else:
            try:
                price = round(float(price), 4)
            except (TypeError, ValueError):
                raise PlugError("Price: number expected") from None
            if not 0 <= price <= 10:
                raise PlugError("Price must be between 0 and 10 per kWh")
        currency = _text(data, "currency", 8, "Currency") if "currency" in data else self.settings.get("currency")
        with self._lock:
            self.settings = {"price_kwh": price, "currency": currency or "€"}
            self._save()
            return dict(self.settings)

    # -- live values --

    def _fetch(self, plug):
        try:
            result = {"status": self._driver(plug).status().to_dict(), "error": None}
        except PlugError as e:
            result = {"status": None, "error": str(e), "unreachable": isinstance(e, PlugUnreachable)}
        with self._lock:
            if not any(p["id"] == plug["id"] for p in self._plugs):
                return result  # deleted in the meantime
            self._live[plug["id"]] = (time.monotonic(), plug["host"], result)
        if result["status"]:
            try:
                self.energy.record(plug["id"], result["status"])
            except Exception:  # noqa: BLE001 - the energy log must not disturb the live values
                log.exception("Energy log: reading of %s not recorded", plug["name"])
        self._check_alerts(plug, result)
        return result

    # -- alerts --

    def _check_alerts(self, plug, result):
        """Raises and clears the alerts of a plug; each change goes into the log once."""
        now = time.monotonic()
        st = result["status"]
        with self._lock:
            state = self._alerts.setdefault(plug["id"], {"active": {}, "since": {}})
            since, active = state["since"], state["active"]
            current = {}
            if st is None:
                current = {k: v for k, v in active.items() if k != "unreachable"}  # unknown: keep
                if result.get("unreachable"):
                    if now - since.setdefault("unreachable", now) >= self.UNREACHABLE_SECONDS:
                        current["unreachable"] = "Plug not reachable"
            else:
                since.pop("unreachable", None)
                power = st.get("power_w") or 0
                if not st["on"] and power > self.WELD_W:
                    if now - since.setdefault("welded", now) >= self.WELD_SECONDS:
                        current["welded"] = ("Relay is off, but %s W are flowing: the contacts may be welded"
                                             % _fmt_w(power))
                else:
                    since.pop("welded", None)
                for err in st.get("errors") or ():
                    current["error:" + err] = "Plug reports: %s" % ERROR_TEXTS.get(err, err)
                temp = st.get("temperature_c")
                if temp is not None and temp >= self.TEMP_WARN_C:
                    current["hot"] = "Plug is hot: %d °C" % round(temp)
                count = st.get("switch_count")
                if count is not None and count >= self.RATED_CYCLES * self.WEAR_WARN:
                    current["wear"] = "Relay worn: %d of %d switching cycles used" % (count, self.RATED_CYCLES)
            raised = [current[k] for k in current if k not in active]
            cleared = [active[k] for k in active if k not in current]
            state["active"] = current
        for message in raised:
            log.warning("plug %s: %s", plug["name"], message)
            self.on_event(device_id=None, device=plug["name"], action="alert", source="monitor", ok=False,
                          message=message)
        for message in cleared:
            log.info("plug %s: cleared: %s", plug["name"], message)
            self.on_event(device_id=None, device=plug["name"], action="alert", source="monitor", ok=True,
                          message="Cleared: %s" % message)

    def alerts(self, plug_id):
        with self._lock:
            active = (self._alerts.get(plug_id) or {}).get("active") or {}
            return [{"key": k, "message": v} for k, v in active.items()]

    # -- background reading --

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="plug-poll", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(self.POLL_SECONDS - time.time() % self.POLL_SECONDS + 5):
            try:
                self.poll()
            except Exception:  # noqa: BLE001 - the poller must never die
                log.exception("Error while reading the plugs")

    def poll(self):
        """Reads every plug whose live values are older than half the poll interval."""
        with self._lock:
            ids = [p["id"] for p in self._plugs]
        list(self._pool.map(lambda i: self.live(i, max_age=self.POLL_SECONDS / 2), ids))

    def live(self, plug_id, max_age=CACHE_SECONDS):
        with self._lock:
            plug = copy.deepcopy(self._find(plug_id))
            cached = self._live.get(plug_id)
        if cached and cached[1] == plug["host"] and time.monotonic() - cached[0] < max_age:
            return cached[2]
        return self._fetch(plug)

    def list(self, live=True, max_age=CACHE_SECONDS):
        """All plugs with live values (at most max_age seconds old, fetched in parallel)."""
        with self._lock:
            plugs = copy.deepcopy(self._plugs)
            cached = dict(self._live)
        if not live:
            return [self.public(p) for p in plugs]
        now = time.monotonic()

        def current(p):
            entry = cached.get(p["id"])
            return entry and entry[1] == p["host"] and now - entry[0] < max_age

        stale = [p for p in plugs if not current(p)]
        fresh = dict(zip([p["id"] for p in stale], self._pool.map(self._fetch, stale)))
        return [dict(self.public(p), live=fresh.get(p["id"]) or cached[p["id"]][2], alerts=self.alerts(p["id"]))
                for p in plugs]

    # -- switching --

    def switch(self, plug_id, action, confirm=False, source="manual", log_event=True):
        if action not in ACTIONS:
            raise PlugError("Unknown action: %s" % action)
        plug = self.get(plug_id)
        driver = self._driver(plug)
        if action in ("off", "cycle") and plug.get("locked"):
            raise PlugError("%s supplies the WOL-Server or the network: it is never switched off" % plug["name"])
        if action in ("off", "cycle") and not confirm:
            # Rule 1: cut the power only without asking when nothing is drawing more than standby
            if "metering" not in driver.capabilities:
                raise ConfirmRequired("This plug cannot measure: it is unknown whether the device is still running")
            status = driver.status()
            if status.on and status.power_w is not None and status.power_w > plug.get("standby_w", self.DEFAULT_STANDBY_W):
                raise ConfirmRequired("%s is drawing %s W" % (self.consumer_name(plug), _fmt_w(status.power_w)),
                                      status.power_w)
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
        if log_event or not ok:
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

    # -- security --

    def _set_fields(self, plug_id, **fields):
        with self._lock:
            plug = self._find(plug_id)
            if any(plug.get(k) != v for k, v in fields.items()):
                plug.update(fields)
                self._save()

    def security(self, plug_id):
        """Reads from the plug whether it has a password and which ways in are open; kept for the overview."""
        plug = self.get(plug_id)
        result = self._driver(plug).security()
        self._set_fields(plug_id, security={"password": result["password"], "open": list(result["open"]),
                                            "restart_required": bool(result.get("restart_required")),
                                            "checked": self.clock()})
        return result

    def secure(self, plug_id, password=None, disable=()):
        """Switches the chosen ways in off and sets a password. Without a password the plug keeps the one the
        WOL-Server knows, or gets a random one: only the WOL-Server needs it, a reset of the plug clears it."""
        plug = self.get(plug_id)
        if password is not None and (not isinstance(password, str) or not 8 <= len(password) <= 128):
            raise PlugError("Password: 8 to 128 characters")
        if not isinstance(disable, (list, tuple)) or any(e not in EXPOSURES for e in disable):
            raise PlugError("Unknown setting")
        new_password = password or (None if plug.get("password") else secrets.token_urlsafe(12))
        driver = self._driver(plug)
        try:
            result = driver.secure(new_password, list(disable))
        finally:
            if driver.password != plug.get("password"):  # set on the plug: keep it even if a later step failed
                self._set_fields(plug_id, password=driver.password)
                self._live.pop(plug_id, None)
        state = self.security(plug_id)
        done = (["password set"] if new_password else []) + [EXPOSURE_TEXTS[e] for e in disable]
        self.on_event(device_id=None, device=plug["name"], action="secure", source="manual", ok=True,
                      message="Secured: %s" % ", ".join(done) if done else "Security checked")
        return dict(state, restart_required=bool(state.get("restart_required") or result.get("restart_required")))

    def restart(self, plug_id):
        """Restarts the plug's firmware, e.g. to finish switching Matter off. The output follows the power-on
        state after it: refused where that would cut the power of what is plugged in."""
        plug = self.get(plug_id)
        driver = self._driver(plug)
        status = driver.status()
        if status.on:
            if plug.get("locked"):
                raise PlugError("%s supplies the WOL-Server or the network: it is never restarted" % plug["name"])
            if driver.power_on_state() not in ("restore", "on"):
                raise PlugError("After a restart %s would stay off. Set \"After a power outage\" to \"Restore last "
                                "state\" or \"On\" first, or restart it while it is off" % plug["name"])
        driver.restart()
        with self._lock:
            self._live.pop(plug_id, None)
        self.on_event(device_id=None, device=plug["name"], action="secure", source="manual", ok=True,
                      message="Plug restarted")
        return {"ok": True, "message": "%s restarts, back in about 10 s" % plug["name"]}

    # -- energy --

    def energy_summary(self, max_age=30):
        """Today, month and year per plug plus the estimated standby energy per year (Wh); cached briefly,
        the GUI asks every few seconds."""
        with self._lock:
            plugs = copy.deepcopy(self._plugs)
            cached = self._summary
        key = [(p["id"], p.get("standby_w")) for p in plugs]
        if cached and cached[1] == key and time.monotonic() - cached[0] < max_age:
            return cached[2]
        totals = self.energy.totals([p["id"] for p in plugs])
        for p in plugs:  # a locked plug is never switched off: its standby cannot be saved, so it is not shown
            totals[p["id"]]["standby_year"] = None if p.get("locked") else self.energy.standby(
                p["id"], p.get("standby_w", self.DEFAULT_STANDBY_W))
        with self._lock:
            self._summary = (time.monotonic(), key, totals)
        return totals

    def shutdown(self):
        self._stop.set()
        self._pool.shutdown(wait=False)
        try:
            self.energy.close()
        except Exception:  # noqa: BLE001
            log.exception("Energy log: closing failed")


def _fmt_w(watts):
    return ("%.1f" % watts).rstrip("0").rstrip(".") if watts < 10 else "%d" % round(watts)
