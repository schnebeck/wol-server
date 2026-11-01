# WOL-Server - wolserver/watchdog.py
# Restart a hanging device: it draws running power but does not answer, so its plug cycles the power.
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

"""A device can hang where nothing on the device itself can help: in the firmware, the boot loader or early in the
kernel, e.g. right after a wake. It then draws its running power but never answers, and every further magic packet
goes nowhere - the device is on. Only the power helps.

For a device whose plug link has "Restart when it hangs" switched on, the hang watch checks every TICK seconds: no
answer to ping while the plug measures more than the running threshold, without a break, for hang_seconds. Then the
plug cycles the power (off, on again after 10 s - the plug switches back on by itself), at most MAX_RESTARTS times in
a row. A device that answers again starts over; one that still hangs after that is left alone and named on its card.

Without an answer from the plug, a relay that is off, or a draw at or below the running threshold (standby, suspend,
shutting down), the time starts again. The running threshold is required: the standby threshold alone cannot tell a
device in suspend to RAM from one that hangs. See docs/proposal-smart-plugs.md."""

import logging
import threading
import time

from .plugs.manager import _fmt_w

log = logging.getLogger("wolserver")

TICK = 10  # s between checks: a short time set at the device (e.g. 150 s) should not run long
MAX_RESTARTS = 2  # power cycles in a row without an answer in between
DEFAULT_SECONDS = 900  # as PlugManager.DEFAULT_HANG_SECONDS, for links saved before the option existed
SOURCE = "hang watch"


class HangWatch:
    def __init__(self, devices, plug_for, online, live, cycle, on_event=None, clock=time.monotonic):
        self.devices = devices  # () -> [device, ...]
        self.plug_for = plug_for  # device id -> plug (with its link options) or None
        self.online = online  # device -> True/False/None (ping)
        self.live = live  # plug id -> {"on": bool, "power_w": float or None} or None (plug not reachable)
        self.cycle = cycle  # (plug, device) -> message; raises on failure
        self.on_event = on_event or (lambda **event: None)
        self.clock = clock
        self._lock = threading.Lock()
        self._watch = {}  # device id -> {since, restarts, gave_up, seconds}
        self._stop = threading.Event()
        self._thread = None

    # -- state for the GUI --

    def state(self, device_id):
        """None, or what the watch sees: hanging (no answer at running power, restart_in seconds left), restarted
        (power cycled, waiting for an answer), gave_up (still hangs after MAX_RESTARTS power cycles)."""
        with self._lock:
            w = self._watch.get(device_id)
            if not w:
                return None
            out = {"restarts": w["restarts"], "max_restarts": MAX_RESTARTS}
            if w["gave_up"]:
                return dict(out, state="gave_up")
            if w["since"] is not None:
                left = w["seconds"] - (self.clock() - w["since"])
                return dict(out, state="hanging", restart_in=max(0, round(left)))
            return dict(out, state="restarted") if w["restarts"] else None

    # -- time --

    def tick(self):
        seen = set()
        for device in self.devices():
            plug = self.plug_for(device["id"])
            if not plug or not plug.get("restart_when_hung") or not device.get("host") or plug.get("locked") \
                    or plug.get("running_w") is None:
                continue
            seen.add(device["id"])
            try:
                self._check(device, plug)
            except Exception:  # noqa: BLE001 - one device must not stop the others
                log.exception("Hang watch %s: error in the check", device["name"])
        with self._lock:
            for device_id in [d for d in self._watch if d not in seen]:
                del self._watch[device_id]  # option off, plug unlinked or device deleted

    def _check(self, device, plug):
        answers = self.online(device)
        with self._lock:
            w = self._watch.get(device["id"])
        if answers:
            if w and w["restarts"]:
                log.info("Hang watch %s: answers again after %d power cycle(s)", device["name"], w["restarts"])
            with self._lock:
                self._watch.pop(device["id"], None)
            return
        if answers is None:
            return  # no verdict from the ping: change nothing
        st = self.live(plug["id"])
        power_w = st.get("power_w") if st and st.get("on") else None
        running = power_w is not None and power_w > plug["running_w"]
        seconds = plug.get("hang_seconds") or DEFAULT_SECONDS
        now = self.clock()
        with self._lock:
            w = self._watch.setdefault(device["id"], {"since": None, "restarts": 0, "gave_up": False,
                                                      "seconds": seconds})
            w["seconds"] = seconds
            if not running:
                w["since"] = None
                if not w["restarts"] and not w["gave_up"]:
                    del self._watch[device["id"]]
                return
            if w["since"] is None:
                w["since"] = now
                return
            if w["gave_up"] or now - w["since"] < seconds:
                return
            if w["restarts"] >= MAX_RESTARTS:
                w.update(gave_up=True, since=None)
                gave_up = True
            else:
                w["restarts"] += 1
                w["since"] = None  # the device starts anew: it gets the full time again
                gave_up = False
            restarts = w["restarts"]
        if gave_up:
            message = "Still no answer after %d power cycles - automatic restart given up" % restarts
            log.warning("Hang watch %s: %s", device["name"], message)
            self.on_event(device_id=device["id"], device=device["name"], action="alert", source=SOURCE, ok=False,
                          message=message)
            return
        reason = "no answer for %d s at %s W" % (seconds, _fmt_w(power_w))
        try:
            result = self.cycle(plug, device)
            ok, message = True, "Hangs (%s): %s (%d/%d)" % (reason, result, restarts, MAX_RESTARTS)
        except Exception as e:  # noqa: BLE001 - the plug did not switch; try again after the full time
            ok, message = False, "Hangs (%s), power cycle failed: %s" % (reason, e)
            with self._lock:
                w["restarts"] -= 1  # no power cycle happened: it does not count
                w["since"] = now
        (log.warning if ok else log.error)("Hang watch %s: %s", device["name"], message)
        self.on_event(device_id=device["id"], device=device["name"], action="power_cycle", source=SOURCE, ok=ok,
                      message=message)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="hang-watch", daemon=True)
            self._thread.start()

    def _loop(self):
        while not self._stop.wait(TICK):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - must never die
                log.exception("Hang watch: error in the timer")

    def stop(self):
        self._stop.set()
