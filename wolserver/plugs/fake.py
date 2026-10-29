# WOL-Server - wolserver/plugs/fake.py
# Simulated plug for tests and the demo; not registered in normal operation.
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

"""A plug that only exists in memory. Call register() to make it available (tests, demo).

FakePlug.devices maps host -> FakeDevice; a host that is not in it counts as unreachable.
"""

import threading
import time

from .base import (CAPABILITIES, DRIVERS, EXPOSURES, PlugAuthError, PlugDriver, PlugError, PlugInfo, PlugStatus,
                   PlugUnreachable)


class FakeDevice:
    def __init__(self, model="Fake Plug", mac="02:00:00:00:00:01", password="", load_w=0.0, standby_w=1.0,
                 temperature_c=30.0, clock=time.time):
        self.model, self.mac, self.password = model, mac, password
        self.load_w, self.standby_w, self.temperature_c = load_w, standby_w, temperature_c
        self.on, self.energy_wh, self.switch_count, self.errors = True, 0.0, 0, []
        self.power_on_state = "restore"
        self.exposures = {"access_point"}  # like a new plug: its own Wi-Fi is open
        self.pending = set()  # switched off, but only gone after a restart (like Matter on a Shelly)
        self.clock = clock
        self._last = clock()
        self.lock = threading.Lock()

    @property
    def power_w(self):
        return (self.load_w or self.standby_w) if self.on else 0.0

    def tick(self):
        """Integrate the energy since the last call (like the counter in a real plug)."""
        now = self.clock()
        self.energy_wh += self.power_w * (now - self._last) / 3600
        self._last = now

    def set(self, on):
        with self.lock:
            self.tick()
            if on and not self.on:
                self.switch_count += 1
            self.on = on


class FakePlug(PlugDriver):
    name = "fake"
    label = "Simulated plug"
    capabilities = frozenset(CAPABILITIES)
    devices = {}

    @classmethod
    def probe(cls, host, http=None):
        dev = cls.devices.get(host)
        if dev is None:
            raise PlugUnreachable("Plug %s not reachable (%s)" % (host, "simulated"))
        return PlugInfo(driver=cls.name, model=dev.model, name=host, mac=dev.mac, firmware="1.0",
                        auth=bool(dev.password))

    def _device(self):
        dev = self.devices.get(self.host)
        if dev is None:
            raise PlugUnreachable("Plug %s not reachable (%s)" % (self.host, "simulated"))
        if dev.password and dev.password != self.password:
            raise PlugAuthError("Wrong password for the plug" if self.password else "Password required")
        return dev

    def status(self):
        dev = self._device()
        with dev.lock:
            dev.tick()
            power = dev.power_w
            return PlugStatus(on=dev.on, power_w=round(power, 1), voltage_v=230.4,
                              current_a=round(power / 230.4, 3), energy_wh=round(dev.energy_wh, 3),
                              temperature_c=dev.temperature_c, errors=list(dev.errors),
                              switch_count=dev.switch_count)

    def switch(self, on):
        self._device().set(bool(on))

    def cycle(self, seconds):
        dev = self._device()
        dev.set(False)
        timer = threading.Timer(seconds, dev.set, (True,))
        timer.daemon = True
        timer.start()

    def power_on_state(self):
        return self._device().power_on_state

    def set_power_on_state(self, state):
        self._device().power_on_state = state

    def security(self):
        dev = self._device()
        return {"password": bool(dev.password), "open": [e for e in EXPOSURES if e in dev.exposures],
                "restart_required": bool(dev.pending)}

    def secure(self, password=None, disable=()):
        dev = self._device()
        unknown = set(disable) - set(EXPOSURES)
        if unknown:
            raise PlugError("Unknown setting: %s" % sorted(unknown)[0])
        dev.pending |= set(disable) & {"matter"} & dev.exposures
        dev.exposures -= set(disable) - {"matter"}
        if password:
            dev.password = self.password = password
        return {"restart_required": bool(dev.pending)}

    def restart(self):
        dev = self._device()
        dev.exposures -= dev.pending
        dev.pending = set()
        if dev.power_on_state == "off":
            dev.set(False)


def register():
    DRIVERS[FakePlug.name] = FakePlug
    return FakePlug
