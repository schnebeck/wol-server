# WOL-Server - wolserver/plugs/shelly.py
# Driver for Shelly plugs of generation 2 and later (local RPC API, no cloud).
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

"""Shelly Gen2+ (Plus, Pro, Gen3, Gen4): one switch component per outlet.

API: https://shelly-api-docs.shelly.cloud/gen2/ - GET /shelly identifies the device without a password,
everything else goes through /rpc/<Method> with digest authentication (user "admin", SHA-256) when a
password is set on the plug. Settings with nested objects go as JSON-RPC (POST /rpc), the documented way.
"""

import hashlib
import json
from urllib.parse import urlencode

from .base import CAPABILITIES, ERRORS, EXPOSURES, PlugDriver, PlugError, PlugInfo, PlugStatus, register
from .http import HttpClient

_TO_STATE = {"restore_last": "restore", "on": "on", "off": "off"}  # "match_input" (factory default) -> "other"
_FROM_STATE = {"restore": "restore_last", "on": "on", "off": "off"}

# exposure -> (section in Shelly.GetConfig, key path of its switch, method that switches it off, config)
_EXPOSURES = {
    "access_point": ("wifi", ("ap", "enable"), "Wifi.SetConfig", {"ap": {"enable": False}}),
    "bluetooth": ("ble", ("enable",), "BLE.SetConfig", {"enable": False}),
    "matter": ("matter", ("enable",), "Matter.SetConfig", {"enable": False}),
    "cloud": ("cloud", ("enable",), "Cloud.SetConfig", {"enable": False}),
}
assert set(_EXPOSURES) == set(EXPOSURES)


def _get(data, path):
    for key in path:
        data = data.get(key) if isinstance(data, dict) else None
    return data


def _mac(raw):
    hex_ = "".join(c for c in str(raw or "").lower() if c in "0123456789abcdef")
    return ":".join(hex_[i:i + 2] for i in range(0, 12, 2)) if len(hex_) == 12 else ""


@register
class ShellyGen2(PlugDriver):
    name = "shelly-gen2"
    label = "Shelly (Gen2+)"
    capabilities = frozenset(CAPABILITIES)
    USER = "admin"

    def make_key(self):
        return self.http.digest_key(self.host, self.USER, self.password) if self.password else None

    @classmethod
    def probe(cls, host, http=None):
        data = (http or HttpClient()).get_json("http://%s/shelly" % host)
        if not isinstance(data, dict) or not isinstance(data.get("gen"), int) or data["gen"] < 2:
            return None  # not a Shelly, or a Gen1 device with a different API
        return PlugInfo(driver=cls.name, model=str(data.get("model") or data.get("app") or ""),
                        name=str(data.get("name") or data.get("id") or ""), mac=_mac(data.get("mac")),
                        firmware=str(data.get("ver") or ""), auth=bool(data.get("auth_en")))

    def _rpc(self, method, **params):
        query = {k: (json.dumps(v) if isinstance(v, (bool, dict)) else v) for k, v in params.items()}
        url = "http://%s/rpc/%s%s" % (self.host, method, "?" + urlencode(query) if query else "")
        data = self.http.get_json(url, self.USER, self.password or self.key or None)
        if not isinstance(data, dict):
            raise PlugError("Unexpected answer from the plug")
        return data

    def _call(self, method, **params):
        """JSON-RPC: POST /rpc with method and params in the body."""
        data = self.http.post_json("http://%s/rpc" % self.host, {"id": 1, "method": method, "params": params},
                                   self.USER, self.password or self.key or None)
        if not isinstance(data, dict):
            raise PlugError("Unexpected answer from the plug")
        if isinstance(data.get("error"), dict):
            raise PlugError("Plug refused %s: %s" % (method, data["error"].get("message") or data["error"].get("code")))
        result = data.get("result")
        return result if isinstance(result, dict) else {}

    def status(self):
        d = self._rpc("Switch.GetStatus", id=self.channel)
        if "output" not in d:
            raise PlugError("The plug has no switch %d" % self.channel)
        number = lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None  # noqa: E731
        counts = d.get("counts") if isinstance(d.get("counts"), dict) else {}
        return PlugStatus(
            on=bool(d["output"]),
            power_w=number(d.get("apower")),
            voltage_v=number(d.get("voltage")),
            current_a=number(d.get("current")),
            energy_wh=number((d.get("aenergy") or {}).get("total")),
            temperature_c=number((d.get("temperature") or {}).get("tC")),
            errors=[e for e in d.get("errors") or [] if e in ERRORS],
            switch_count=int(counts["switch_on"]) if isinstance(counts.get("switch_on"), int) else None,
        )

    def switch(self, on):
        self._rpc("Switch.Set", id=self.channel, on=bool(on))

    def cycle(self, seconds):
        # The plug switches back on by itself, even if Wi-Fi or the WOL-Server fail in between.
        self._rpc("Switch.Set", id=self.channel, on=False, toggle_after=int(seconds))

    def power_on_state(self):
        return _TO_STATE.get(self._rpc("Switch.GetConfig", id=self.channel).get("initial_state"), "other")

    def set_power_on_state(self, state):
        if state not in _FROM_STATE:
            raise PlugError("Unknown state: %s" % state)
        self._rpc("Switch.SetConfig", id=self.channel, config={"initial_state": _FROM_STATE[state]})

    def _device_info(self):
        return self.http.get_json("http://%s/shelly" % self.host)

    def security(self):
        config = self._rpc("Shelly.GetConfig")
        return {"password": bool(self._device_info().get("auth_en")),
                "open": [name for name, (section, path, _, _) in _EXPOSURES.items()
                         if _get(config.get(section), path) is True],
                "restart_required": bool(self._rpc("Sys.GetStatus").get("restart_required"))}

    def secure(self, password=None, disable=()):
        restart = False
        for name in disable:
            if name not in _EXPOSURES:
                raise PlugError("Unknown setting: %s" % name)
            _, _, method, config = _EXPOSURES[name]
            restart = bool(self._call(method, config=config).get("restart_required")) or restart
        if password:
            realm = self._device_info().get("id")
            if not realm:
                raise PlugError("The plug does not report its ID")
            ha1 = hashlib.sha256(("%s:%s:%s" % (self.USER, realm, password)).encode("utf-8")).hexdigest()
            self._call("Shelly.SetAuth", user=self.USER, realm=realm, ha1=ha1)
            self.password = password
            self._rpc("Shelly.GetDeviceInfo")  # the new password works
        return {"restart_required": restart}

    def restart(self):
        self._call("Shelly.Reboot")
