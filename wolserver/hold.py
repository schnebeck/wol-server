# WOL-Server - wolserver/hold.py
# Keep a requested device awake on its own side: an inhibitor over SSH for as long as it is requested.
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

"""A request tells the WOL-Server not to put a device to sleep; the device itself does not hear it, and its own
power management may put it to sleep anyway. For a device with "keep awake while requested" switched on, the
WOL-Server opens an SSH connection once the device answers and runs the hold command there (by default
systemd-inhibit ... cat): the inhibitor holds for as long as the connection is open. When the last request is done
the connection is closed and the inhibitor is gone - also when the WOL-Server stops or the network breaks.

A requested device that stops answering anyway is a contradiction: with the hold switched on it is woken again (at
most every REWAKE_EVERY seconds, REWAKE_TRIES times), and the hold follows once it is up. See
docs/proposal-device-hold.md."""

import logging
import subprocess
import threading
import time

from . import power

log = logging.getLogger("wolserver")

TICK = 10  # s between checks
RETRY = 60  # s before a failed hold is tried again
START_WAIT = 3  # s a new connection must live before it counts as held
RELEASE_WAIT = 3  # s for the device side to let go before the connection is killed
SILENT_TICKS = 2  # a requested device that does not answer this many checks in a row counts as asleep ...
BOOT_SECONDS = 300  # ... once it has answered since it was requested, or this long after the request
REWAKE_EVERY = 300  # s between two wakes of a requested device that fell asleep
REWAKE_TRIES = 3


class DeviceHolds:
    def __init__(self, identity, device_lookup, requested, online, wake, clock=time.monotonic, popen=subprocess.Popen):
        self.identity = identity
        self.device_lookup = device_lookup  # id -> device or None
        self.requested = requested  # () -> {device id: [client, ...]} with open requests
        self.online = online  # device -> True/False/None (ping)
        self.wake = wake  # (device, source) -> (ok, message), like the button
        self.clock = clock
        self.popen = popen
        self._lock = threading.RLock()
        self._holds = {}  # device id -> {proc, since, state, error, tried, silent, woken, wakes}
        self._stop = threading.Event()
        self._nudge = threading.Event()
        self._thread = None

    # -- state for the GUI --

    def state(self, device_id):
        """None, or {state, since, error} of a device with an open request. state: held (the inhibitor stands),
        waiting (the device does not answer yet), failed (the hold command did not hold), waking (it fell asleep
        although requested and was woken again), asleep (it keeps falling asleep; no more wakes)."""
        with self._lock:
            h = self._holds.get(device_id)
            if not h:
                return None
            return {"state": h["state"], "since": h["since"], "error": h["error"]}

    # -- connection --

    def _start(self, device, h):
        h["tried"] = self.clock()
        cmd, reason = power.ssh_command(device, self.identity, device.get("hold_command") or "", 30, 3)
        if cmd is None:
            return self._failed(device, h, reason)
        try:
            proc = self.popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except OSError as e:
            return self._failed(device, h, str(e))
        try:
            proc.wait(START_WAIT)
        except subprocess.TimeoutExpired:  # still running: the command on the device waits, the hold stands
            h.update(proc=proc, state="held", since=time.time(), error=None, silent=0)
            log.info("Hold %s: kept awake on the device while requested", device["name"])
            return
        err = (proc.stderr.read().decode("utf-8", "replace") if proc.stderr else "").strip().splitlines()
        self._failed(device, h, "the hold command ended at once (code %s)%s"
                     % (proc.returncode, ": " + err[-1] if err else ""))

    def _failed(self, device, h, error):
        if h["state"] != "failed" or h["error"] != error:
            log.warning("Hold %s failed: %s", device["name"], error)
        h.update(proc=None, state="failed", error=error)

    def _close(self, h):
        proc = h.get("proc")
        h["proc"] = None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.close()  # the command on the device sees the end of its input and lets go
        except OSError:
            pass
        try:
            proc.wait(RELEASE_WAIT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(RELEASE_WAIT)

    def release(self, device_id):
        """Lets go of the device, e.g. right before it is put to sleep. Blocks until the connection is closed."""
        with self._lock:
            h = self._holds.pop(device_id, None)
            if h:
                self._close(h)
                if h["state"] == "held":
                    device = self.device_lookup(device_id)
                    log.info("Hold %s: released", device["name"] if device else device_id)

    def nudge(self):
        """A request came or went: check now, not in up to TICK seconds."""
        self._nudge.set()

    # -- time --

    def tick(self):
        wanted = self.requested()
        with self._lock:
            for device_id in [d for d in self._holds if d not in wanted]:
                self.release(device_id)
            for device_id, clients in wanted.items():
                device = self.device_lookup(device_id)
                if device is None or not device.get("hold") or device.get("sleep_method") != "ssh":
                    self.release(device_id)
                    continue
                h = self._holds.setdefault(device_id, {"proc": None, "since": None, "state": "waiting", "error": None,
                                                       "tried": None, "silent": 0, "woken": None, "wakes": 0,
                                                       "wanted": self.clock(), "seen": False})
                self._check(device, h, clients)

    def _check(self, device, h, clients):
        if h["proc"] is not None and h["proc"].poll() is not None:
            self._failed(device, h, "connection lost (code %s)" % h["proc"].returncode)
        answers = self.online(device)
        if answers is False:
            h["silent"] += 1
        elif answers:
            h["silent"], h["seen"] = 0, True
            if h["state"] in ("asleep", "waking"):
                log.info("Hold %s: answers again", device["name"])
                h.update(state="waiting", error=None)
        booting = not h["seen"] and self.clock() - h["wanted"] < BOOT_SECONDS  # woken by the request, still starting
        if h["silent"] >= SILENT_TICKS and not booting:
            self._asleep(device, h, clients)
            return
        if h["proc"] is None and answers and (h["tried"] is None or self.clock() - h["tried"] >= RETRY):
            self._start(device, h)

    def _asleep(self, device, h, clients):
        """Requested, but it does not answer: the contradiction the hold is there for. Wake it again."""
        self._close(h)
        if h["state"] not in ("asleep", "waking"):
            log.warning("Contradiction: %s does not answer although %s requested it", device["name"], ", ".join(clients))
            h.update(state="waking", error=None, wakes=0, woken=None)
        if h["state"] == "asleep":
            return  # given up
        if h["woken"] is not None and self.clock() - h["woken"] < REWAKE_EVERY:
            return  # woken, give it time to start
        if h["wakes"] >= REWAKE_TRIES:
            log.warning("Hold %s: given up after %d wakes - it falls asleep again and again", device["name"], h["wakes"])
            h["state"] = "asleep"
            return
        h["woken"] = self.clock()
        h["wakes"] += 1
        ok, message = self.wake(device, "hold")
        (log.info if ok else log.warning)("Hold %s: woken again (%d/%d): %s", device["name"], h["wakes"], REWAKE_TRIES,
                                          message)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="holds", daemon=True)
            self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            self._nudge.wait(TICK)
            self._nudge.clear()
            if self._stop.is_set():
                break
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - must never die
                log.exception("Holds: error in the timer")

    def stop(self):
        self._stop.set()
        self._nudge.set()
        with self._lock:
            for device_id in list(self._holds):
                self.release(device_id)
