# WOL-Server - wolserver/plugs/base.py
# Driver interface for wireless plugs: common data types, errors and the driver registry.
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

"""Driver interface for wireless plugs (hardware abstraction).

Drivers only talk to the hardware and convert its answers into one common form (watts, watt-hours,
degrees Celsius, normalized error codes). They know nothing about devices, safety rules or statistics:
that logic lives in the plug manager and therefore applies to every vendor alike.
"""

import time
from dataclasses import asdict, dataclass, field

# What a driver's hardware can do. The manager adapts to it (e.g. no safety check without metering).
CAPABILITIES = ("metering", "energy_counter", "temperature", "switch_count", "device_cycle", "power_on_state",
                "errors", "security")
POWER_ON_STATES = ("restore", "on", "off")
ERRORS = ("overtemp", "overpower", "overvoltage", "undervoltage")
# Ways into the plug besides its local API that the WOL-Server does not need: its own (often open) Wi-Fi
# access point, Bluetooth, Matter and the vendor cloud. secure() switches the chosen ones off.
EXPOSURES = ("access_point", "bluetooth", "matter", "cloud")


class PlugError(Exception):
    """Generic driver error; the message reaches the GUI."""


class PlugUnreachable(PlugError):
    pass


class PlugAuthError(PlugError):
    pass


class PlugUnsupported(PlugError):
    pass


@dataclass
class PlugInfo:
    """Result of a probe: what the device says about itself."""

    driver: str
    model: str = ""
    name: str = ""  # name or ID the device reports
    mac: str = ""  # aa:bb:cc:dd:ee:ff or ""
    firmware: str = ""
    auth: bool = False  # password protection switched on
    channels: int = 1

    def to_dict(self):
        return asdict(self)


@dataclass
class PlugStatus:
    """Live values; fields the hardware cannot supply are None."""

    on: bool
    power_w: float = None
    voltage_v: float = None
    current_a: float = None
    energy_wh: float = None  # counter of the plug, it only grows (except after a reset)
    temperature_c: float = None
    errors: list = field(default_factory=list)  # subset of ERRORS
    switch_count: int = None
    ts: float = field(default_factory=time.time)

    def to_dict(self):
        return asdict(self)


class PlugDriver:
    """Base class. A plug is addressed by driver + host + channel."""

    name = ""  # registry key, stored with the plug
    label = ""  # shown in the GUI
    capabilities = frozenset()

    def __init__(self, host, channel=0, password="", http=None):
        from .http import HttpClient  # noqa: PLC0415 - avoids a cycle for drivers without HTTP

        self.host = host
        self.channel = channel
        self.password = password
        self.http = http or HttpClient()

    @classmethod
    def probe(cls, host, http=None):
        """PlugInfo if the device at host belongs to this driver, else None. Must not change anything."""
        raise NotImplementedError

    def status(self):
        raise NotImplementedError

    def switch(self, on):
        raise NotImplementedError

    def cycle(self, seconds):
        """Off, then on again after seconds - on the device itself (capability device_cycle)."""
        raise PlugUnsupported("This plug cannot run a power cycle by itself")

    def power_on_state(self):
        """State after a power outage: one of POWER_ON_STATES, or "other"."""
        raise PlugUnsupported("This plug has no setting for the state after a power outage")

    def set_power_on_state(self, state):
        raise PlugUnsupported("This plug has no setting for the state after a power outage")

    def security(self):
        """{"password": bool, "open": [exposure, ...]}: is a password set, which EXPOSURES are switched on."""
        raise PlugUnsupported("This plug cannot report its security settings")

    def secure(self, password=None, disable=()):
        """Switches the given EXPOSURES off, then sets password (if given) and uses it from then on.
        Returns {"restart_required": bool}: some settings only take effect after the plug restarts."""
        raise PlugUnsupported("This plug cannot be secured by the WOL-Server")


DRIVERS = {}


def register(cls):
    DRIVERS[cls.name] = cls
    return cls


def get_driver(name):
    try:
        return DRIVERS[name]
    except KeyError:
        raise PlugError("Unknown plug type: %s" % name) from None


def probe_all(host, http=None, drivers=None):
    """Ask every driver whether it recognizes the device at host; first match or None."""
    unreachable = None
    for cls in (drivers or DRIVERS.values()):
        try:
            info = cls.probe(host, http)
        except PlugUnreachable as e:
            unreachable = e
            continue
        except PlugError:
            continue
        if info:
            return info
    if unreachable:
        raise unreachable
    return None


def describe_drivers():
    return [{"name": d.name, "label": d.label, "capabilities": sorted(d.capabilities)}
            for d in DRIVERS.values()]
