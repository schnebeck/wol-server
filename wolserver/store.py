# WOL-Server - wolserver/store.py
# Persistence in a single JSON file, written atomically.
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

"""Persistence in a single JSON file (written atomically, easy on the SD card)."""

import copy
import json
import logging
import os
import threading
import time
import uuid

log = logging.getLogger("wolserver")

COLLECTIONS = ("devices", "schedules")
MAX_EVENTS = 300


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._data = {"devices": [], "schedules": [], "events": []}
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            broken = "%s.broken-%d" % (self.path, time.time())
            log.error("Data file %s unreadable (%s) - moving it to %s", self.path, e, broken)
            os.replace(self.path, broken)
            return
        for key in self._data:
            if isinstance(data.get(key), list):
                self._data[key] = data[key]

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # -- generic CRUD operations for "devices" and "schedules" --

    def list(self, collection):
        with self._lock:
            return copy.deepcopy(self._data[collection])

    def get(self, collection, item_id):
        with self._lock:
            for item in self._data[collection]:
                if item["id"] == item_id:
                    return copy.deepcopy(item)
        return None

    def add(self, collection, item):
        with self._lock:
            item = dict(item, id=uuid.uuid4().hex[:12])
            self._data[collection].append(item)
            self._save()
            return copy.deepcopy(item)

    def update(self, collection, item_id, item):
        with self._lock:
            items = self._data[collection]
            for i, old in enumerate(items):
                if old["id"] == item_id:
                    items[i] = dict(item, id=item_id)
                    self._save()
                    return copy.deepcopy(items[i])
        return None

    def delete(self, collection, item_id):
        with self._lock:
            items = self._data[collection]
            remaining = [i for i in items if i["id"] != item_id]
            if len(remaining) == len(items):
                return False
            self._data[collection] = remaining
            if collection == "devices":
                self._data["schedules"] = [
                    s for s in self._data["schedules"] if s.get("device_id") != item_id
                ]
            self._save()
            return True

    # -- event log --

    def add_event(self, **event):
        with self._lock:
            event.setdefault("ts", time.time())
            events = self._data["events"]
            events.append(event)
            del events[:-MAX_EVENTS]
            self._save()

    def list_events(self, limit=MAX_EVENTS):
        with self._lock:
            return copy.deepcopy(self._data["events"][-limit:][::-1])

    def clear_events(self):
        with self._lock:
            self._data["events"] = []
            self._save()
