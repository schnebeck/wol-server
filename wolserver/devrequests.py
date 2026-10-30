# WOL-Server - wolserver/devrequests.py
# Requests: several clients share a device; it sleeps when the last one is done.
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

"""Requests for a device (docs/proposal-device-requests.md).

A client requests a device, sends heartbeats while it works and finishes, with or without asking for sleep.
The device sleeps only when no request is open any more; after the last one a short grace period lets the
next client take over without a sleep and wake round trip. A request without heartbeats expires after its
ttl, so a crashed client does not keep the device awake forever; ttl 0 means: until it is released.

Requests are kept in requests.json; heartbeats only touch RAM (easy on the SD card). After a restart every
request gets one ttl from the start to send its next heartbeat.
"""

import json
import logging
import os
import threading
import time
import uuid

log = logging.getLogger("wolserver")

DEFAULT_TTL = 900  # s without a heartbeat until a request expires
MIN_TTL = 60
MAX_TTL = 7 * 86400  # one heartbeat per week at least; ttl 0 needs none at all
GRACE = 120  # s after the last request before a requested sleep runs
RETRY = 300  # s between attempts when the sleep fails
MAX_TRIES = 7  # the first attempt and six retries
MAX_OPEN = 200
TICK = 10  # s


class RequestError(Exception):
    """Invalid input; the message reaches the client."""


def _client_name(value):
    if not isinstance(value, str) or not value.strip():
        raise RequestError("client: name of the requesting client expected")
    value = value.strip()
    if len(value) > 64 or any(ord(c) < 32 for c in value):
        raise RequestError("client: at most 64 printable characters")
    return value


def _ttl(value):
    if value is None:
        return DEFAULT_TTL
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError("ttl: seconds expected (0 = until released)")
    value = int(value)
    if value != 0 and not MIN_TTL <= value <= MAX_TTL:
        raise RequestError("ttl: 0 (until released) or %d to %d seconds" % (MIN_TTL, MAX_TTL))
    return value


class DeviceRequests:
    def __init__(self, path, sleep, device_lookup, on_event=None, clock=time.time, grace=GRACE):
        self.path = path
        self.sleep = sleep  # (device, source) -> (ok, message); runs the sleep like the button does
        self.device_lookup = device_lookup  # id -> device or None
        self.on_event = on_event or (lambda **event: None)
        self.clock = clock
        self.grace = grace
        self._lock = threading.RLock()
        self._requests = {}  # id -> {id, device_id, client, ttl, created, renewed, source}
        self._sleep = {}  # device_id -> {since, by, at (None while requests are open), tries}
        self._stop = threading.Event()
        self._thread = None
        self._load()

    # -- storage --

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.error("Requests file %s unreadable (%s) - starting without requests", self.path, e)
            return
        now = self.clock()
        for item in data.get("requests") or []:
            if isinstance(item, dict) and {"id", "device_id", "client", "ttl"} <= set(item):
                item["renewed"] = now  # one ttl from the start to send the next heartbeat
                self._requests[item["id"]] = item
        for device_id, entry in (data.get("sleep") or {}).items():
            if isinstance(entry, dict):
                self._sleep[device_id] = dict(entry, at=None, tries=0)

    def _save(self):
        data = {"requests": sorted(self._requests.values(), key=lambda r: r["created"]),
                "sleep": {d: {"since": e["since"], "by": e["by"]} for d, e in self._sleep.items()}}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Requests: writing %s failed", self.path)

    # -- queries --

    def _open(self, device_id):
        return sorted((r for r in self._requests.values() if r["device_id"] == device_id), key=lambda r: r["created"])

    def _public(self, req):
        out = {k: req[k] for k in ("id", "device_id", "client", "ttl", "created", "renewed", "source")}
        out["expires"] = req["renewed"] + req["ttl"] if req["ttl"] else None
        return out

    def clients(self, device_id):
        """Names of the clients that hold the device (empty: it may sleep)."""
        with self._lock:
            return [r["client"] for r in self._open(device_id)]

    def get(self, device_id):
        with self._lock:
            entry = self._sleep.get(device_id)
            return {"requests": [self._public(r) for r in self._open(device_id)],
                    "sleep": {k: entry[k] for k in ("since", "by", "at")} if entry else None}

    def open_clients(self):
        """{device id: [client, ...]} of every device with an open request (for the hold)."""
        with self._lock:
            out = {}
            for req in sorted(self._requests.values(), key=lambda r: r["created"]):
                out.setdefault(req["device_id"], []).append(req["client"])
            return out

    def snapshot(self, device_ids):
        """For the status of the GUI: only devices with requests or a remembered sleep."""
        with self._lock:
            ids = {r["device_id"] for r in self._requests.values()} | set(self._sleep)
        return {d: self.get(d) for d in device_ids if d in ids}

    # -- changes --

    def _event(self, device_id, source, message, ok=True):
        device = self.device_lookup(device_id)
        self.on_event(device_id=device_id, device=device["name"] if device else device_id, action="request",
                      source=source, ok=ok, message=message)

    def create(self, device, client, ttl=None, source="manual"):
        client, ttl = _client_name(client), _ttl(ttl)
        now = self.clock()
        with self._lock:
            if len(self._requests) >= MAX_OPEN:
                raise RequestError("Too many open requests")
            req = {"id": uuid.uuid4().hex[:12], "device_id": device["id"], "client": client, "ttl": ttl,
                   "created": now, "renewed": now, "source": source}
            self._requests[req["id"]] = req
            if device["id"] in self._sleep:
                self._sleep[device["id"]].update(at=None, tries=0)  # needed again: the sleep waits
            self._save()
        self._event(device["id"], source, "Requested by %s" % client)
        return self._public(req)

    def owner(self, device_id, request_id):
        """Source of a request ("API: <token>" or "manual"); KeyError if unknown."""
        with self._lock:
            req = self._requests.get(request_id)
            if req is None or req["device_id"] != device_id:
                raise KeyError(request_id)
            return req["source"]

    def renew(self, device_id, request_id):
        """Heartbeat. KeyError if the request is unknown or has expired."""
        with self._lock:
            req = self._requests.get(request_id)
            if req is None or req["device_id"] != device_id:
                raise KeyError(request_id)
            req["renewed"] = self.clock()
            return self._public(req)

    def release(self, device_id, request_id, then_sleep=False, source="manual", ended_by=None):
        """Finishes a request. Returns the sleep state of the device afterwards (see request_sleep)."""
        with self._lock:
            req = self._requests.get(request_id)
            if req is None or req["device_id"] != device_id:
                raise KeyError(request_id)
            del self._requests[request_id]
            if then_sleep:
                self._remember_sleep(device_id, req["client"])
            elif not self._open(device_id) and device_id in self._sleep:
                self._sleep[device_id]["at"] = self.clock() + self.grace
            self._save()
            state = self._state(device_id)
        if ended_by:
            message = "Ended in the GUI: %s" % req["client"]
        else:
            message = "Finished: %s%s" % (req["client"], " (sleep requested)" if then_sleep else "")
        self._event(device_id, source, message)
        return state

    def _remember_sleep(self, device_id, by):
        entry = self._sleep.setdefault(device_id, {"since": self.clock(), "by": by, "at": None, "tries": 0})
        if not self._open(device_id):
            entry["at"] = self.clock() + self.grace

    def _state(self, device_id):
        entry = self._sleep.get(device_id)
        return {"sleep": "pending" if entry else None, "waiting_for": [r["client"] for r in self._open(device_id)],
                "sleep_at": entry["at"] if entry else None}

    def request_sleep(self, device, by, source="manual"):
        """Sleep when the device is idle. "now" if nothing is open (the caller puts it to sleep at once),
        else "pending": the sleep runs after the last request (plus the grace period)."""
        with self._lock:
            waiting = [r["client"] for r in self._open(device["id"])]
            if not waiting:
                self._sleep.pop(device["id"], None)
                self._save()
                return {"sleep": "now", "waiting_for": []}
            self._remember_sleep(device["id"], by)
            self._save()
        self._event(device["id"], source, "Sleep requested by %s: waits for %s" % (by, ", ".join(waiting)))
        return {"sleep": "pending", "waiting_for": waiting}

    def cancel_sleep(self, device_id, source="manual"):
        with self._lock:
            entry = self._sleep.pop(device_id, None)
            if entry:
                self._save()
        if entry:
            self._event(device_id, source, "Sleep request cancelled")
        return entry is not None

    def forget_device(self, device_id):
        with self._lock:
            self._requests = {k: r for k, r in self._requests.items() if r["device_id"] != device_id}
            self._sleep.pop(device_id, None)
            self._save()

    # -- time --

    def tick(self):
        """Expires requests without heartbeat and runs the sleeps whose time has come."""
        now = self.clock()
        expired, due = [], []
        with self._lock:
            for req in list(self._requests.values()):
                if req["ttl"] and now - req["renewed"] > req["ttl"]:
                    del self._requests[req["id"]]
                    expired.append(req)
                    entry = self._sleep.get(req["device_id"])
                    if entry and not self._open(req["device_id"]):
                        entry["at"] = now + self.grace
            for device_id, entry in list(self._sleep.items()):
                if entry["at"] is None and not self._open(device_id):
                    entry["at"] = now + self.grace  # e.g. after a restart
                if entry["at"] is not None and entry["at"] <= now and not self._open(device_id):
                    entry["tries"] += 1
                    entry["at"] = now + RETRY if entry["tries"] < MAX_TRIES else None
                    due.append((device_id, entry["tries"]))
                    if entry["at"] is None:
                        del self._sleep[device_id]
            if expired or due:
                self._save()
        for req in expired:
            minutes = req["ttl"] // 60
            self._event(req["device_id"], "requests", "Expired: %s (no heartbeat for %d min)" % (req["client"], minutes),
                        ok=False)
        for device_id, tries in due:
            device = self.device_lookup(device_id)
            if device is None:
                with self._lock:
                    self._sleep.pop(device_id, None)
                continue
            ok, _ = self.sleep(device, "requests")
            with self._lock:
                if ok:
                    self._sleep.pop(device_id, None)
                    self._save()
            if not ok and tries >= MAX_TRIES:
                self._event(device_id, "requests", "Sleep given up after %d attempts" % tries, ok=False)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="requests", daemon=True)
            self._thread.start()

    def _loop(self):
        while not self._stop.wait(TICK):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - must never die
                log.exception("Requests: error in the timer")

    def stop(self):
        self._stop.set()
