# WOL-Server - wolserver/led.py
# The green activity LED as a status indicator.
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

"""The Pi's green activity LED (ACT) as a status indicator.

Only kernel blink patterns are used (timer, heartbeat). They cost no CPU and do not
depend on whether the LED is wired inverted (Pi Zero): there, at most the on and off
phases swap. That is why the timer patterns are symmetric.
"""
import logging
import os

log = logging.getLogger("wolserver.led")

LED_DIRS = ("/sys/class/leds/ACT", "/sys/class/leds/led0")
FIRSTBOOT_MARKER = "/run/wol-firstboot"  # created by firstboot.sh for the duration of the setup

PATTERNS = {
    "setup": ("timer", 100, 100),      # fast: setup or connection in progress
    "hotspot": ("heartbeat",),         # heartbeat: setup hotspot active
    "offline": ("timer", 1000, 1000),  # slow: no network
    "normal": None,                    # system default indicator (SD access or off)
}
OWN_TRIGGERS = {"timer", "heartbeat"}
FALLBACK_TRIGGERS = ("actpwr", "mmc0", "default-on")


def pattern_for(mode, pending=None, firstboot=False):
    if firstboot or pending:
        return "setup"
    return {"hotspot": "hotspot", "client": "normal"}.get(mode, "offline")


class StatusLed:
    def __init__(self, dirs=LED_DIRS, marker=FIRSTBOOT_MARKER):
        self.marker = marker
        self.path = next((d for d in dirs if os.path.exists(os.path.join(d, "trigger"))), None)
        self.current = None
        self.normal = None
        if self.path is None:
            log.info("No status LED found")
            return
        try:
            available, active = self._triggers()
        except OSError as e:
            log.warning("Status LED not readable: %s", e)
            self.path = None
            return
        # Remember the state before us as "normal" - unless it comes from us (e.g. after a crash
        # of the service or from firstboot.sh); then use the usual default indicator.
        if active is None or active in OWN_TRIGGERS:
            active = next((t for t in FALLBACK_TRIGGERS if t in available), "none")
        self.normal = active

    def _triggers(self):
        with open(os.path.join(self.path, "trigger"), encoding="ascii") as f:
            words = f.read().split()
        active = next((w[1:-1] for w in words if w.startswith("[") and w.endswith("]")), None)
        return [w.strip("[]") for w in words], active

    def _write(self, name, value):
        with open(os.path.join(self.path, name), "w", encoding="ascii") as f:
            f.write(str(value))

    def set(self, pattern):
        if self.path is None or pattern == self.current:
            return
        spec = PATTERNS[pattern] or (self.normal,)
        try:
            self._write("trigger", spec[0])
            if spec[0] == "timer":
                self._write("delay_on", spec[1])
                self._write("delay_off", spec[2])
        except OSError as e:
            log.warning("Status LED not controllable (%s) - indicator disabled", e)
            self.path = None
            return
        self.current = pattern

    def update(self, mode, pending=None):
        self.set(pattern_for(mode, pending, os.path.exists(self.marker)))
