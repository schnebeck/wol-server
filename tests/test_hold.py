# WOL-Server - tests/test_hold.py
# Tests for the hold (a requested device stays awake on its own side) and the contradiction checks of the status.
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

import io
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from wolserver import hold, models
from wolserver.config import DEFAULTS
from wolserver.server import App


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeProc:
    """An ssh process: runs until its input is closed, or ends at once with exit_code."""

    def __init__(self, argv, exit_code=None, stderr=b""):
        self.argv = argv
        self.returncode = exit_code
        self.stdin = mock.Mock()
        self.stdin.close.side_effect = self._end
        self.stderr = io.BytesIO(stderr)
        self.killed = False

    def _end(self):
        self.returncode = 0

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("ssh", timeout)
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed, self.returncode = True, -9


DEVICE = {"id": "d1", "name": "backup", "host": "backup.home.arpa", "mac": "00:11:22:33:44:55",
          "sleep_method": "ssh", "ssh_user": "root", "ssh_port": 22, "ssh_command": "/usr/local/sbin/wol-sleep",
          "hold": True, "hold_command": "hold"}


class DeviceHoldsTest(unittest.TestCase):
    def setUp(self):
        self.device = dict(DEVICE)
        self.wanted = {}
        self.answers = True
        self.procs = []
        self.exit_code = None
        self.woken = []
        self.clock = Clock()
        patcher = mock.patch.object(hold.power, "ssh_command", lambda device, identity, remote, *a: (["ssh", remote], None))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.holds = hold.DeviceHolds(None, device_lookup=lambda i: self.device if i == "d1" else None,
                                      requested=lambda: self.wanted, online=lambda d: self.answers,
                                      wake=lambda d, source: self.woken.append(source) or (True, "Magic packet sent"),
                                      clock=self.clock, popen=self.popen)

    def popen(self, argv, **kw):
        self.procs.append(FakeProc(argv, self.exit_code, b"Permission denied (publickey)." if self.exit_code else b""))
        return self.procs[-1]

    def test_held_while_requested_released_after(self):
        self.holds.tick()
        self.assertEqual(self.procs, [])  # nobody requests it
        self.wanted = {"d1": ["laptop-backup"]}
        with self.assertLogs("wolserver", "INFO") as logs:
            self.holds.tick()
        self.assertEqual((len(self.procs), self.procs[0].argv), (1, ["ssh", "hold"]))
        self.assertEqual(self.holds.state("d1")["state"], "held")
        self.assertIn("kept awake on the device", logs.output[0])
        self.holds.tick()
        self.assertEqual(len(self.procs), 1)  # one connection, not one per check
        self.wanted = {}
        self.holds.tick()
        self.assertTrue(self.procs[0].stdin.close.called)  # end of input: the device side lets go
        self.assertIsNone(self.holds.state("d1"))

    def test_release_before_sleep_blocks_until_closed(self):
        self.wanted = {"d1": ["c"]}
        self.holds.tick()
        self.holds.release("d1")
        self.assertEqual(self.procs[0].returncode, 0)
        self.assertIsNone(self.holds.state("d1"))

    def test_nothing_without_the_switch_or_while_asleep(self):
        self.wanted = {"d1": ["c"]}
        self.device["hold"] = False
        self.holds.tick()
        self.device.update(hold=True, sleep_method="http")
        self.holds.tick()
        self.assertEqual(self.procs, [])
        self.device["sleep_method"] = "ssh"
        self.answers = False  # woken by the request, still starting: wait, do not wake again
        for _ in range(5):
            self.holds.tick()
        self.assertEqual((self.procs, self.woken, self.holds.state("d1")["state"]), ([], [], "waiting"))

    def test_failed_hold_is_retried_once_a_minute(self):
        self.wanted = {"d1": ["c"]}
        self.exit_code = 255
        with self.assertLogs("wolserver", "WARNING") as logs:
            self.holds.tick()
        state = self.holds.state("d1")
        self.assertEqual(state["state"], "failed")
        self.assertIn("Permission denied", state["error"])
        self.assertEqual(len(logs.output), 1)
        self.holds.tick()
        self.assertEqual(len(self.procs), 1)  # not before RETRY
        self.clock.now += hold.RETRY
        self.exit_code = None
        self.holds.tick()
        self.assertEqual((len(self.procs), self.holds.state("d1")["state"]), (2, "held"))
        # The device reboots: the connection drops, the hold comes back
        self.procs[-1].returncode = 255
        self.clock.now += hold.RETRY
        with self.assertLogs("wolserver", "WARNING") as logs:
            self.holds.tick()
        self.assertIn("connection lost (code 255)", logs.output[0])
        self.assertEqual((len(self.procs), self.holds.state("d1")["state"]), (3, "held"))

    def test_asleep_although_requested_is_woken_again(self):
        self.wanted = {"d1": ["laptop-backup"]}
        self.holds.tick()  # held
        self.answers = False  # its own power management put it to sleep anyway
        self.holds.tick()
        self.assertEqual(self.woken, [])  # one silent check is not enough
        with self.assertLogs("wolserver", "WARNING") as logs:
            self.holds.tick()
        self.assertIn("Contradiction: backup does not answer although laptop-backup requested it", logs.output[0])
        self.assertEqual((self.woken, self.holds.state("d1")["state"]), (["hold"], "asleep"))
        self.holds.tick()
        self.assertEqual(len(self.woken), 1)  # not again before REWAKE_EVERY
        with self.assertLogs("wolserver", "WARNING") as logs:
            for _ in range(hold.REWAKE_TRIES + 2):
                self.clock.now += hold.REWAKE_EVERY
                self.holds.tick()
        self.assertEqual(len(self.woken), hold.REWAKE_TRIES)  # then it gives up
        self.assertIn("given up after 3 wakes", logs.output[-1])
        self.answers = True
        self.clock.now += hold.RETRY
        self.holds.tick()
        self.holds.tick()
        self.assertEqual(self.holds.state("d1")["state"], "held")


class ModelTest(unittest.TestCase):
    def test_hold_needs_ssh_and_has_a_default(self):
        base = {"name": "pc", "mac": "00:11:22:33:44:55", "host": "pc.home.arpa", "ssh_user": "root",
                "ssh_command": "sudo systemctl suspend"}
        dev = models.validate_device(dict(base, sleep_method="ssh", hold=True))
        self.assertEqual((dev["hold"], dev["hold_command"]), (True, models.DEFAULT_HOLD_COMMAND))
        with self.assertRaisesRegex(models.ValidationError, "SSH sleep method"):
            models.validate_device(dict(base, sleep_method="http", http_url="http://pc/sleep", hold=True))
        self.assertFalse(models.validate_device(dict(base, sleep_method="none"))["hold"])


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = App(dict(DEFAULTS, listen="127.0.0.1", port=0, data_dir=self.tmp))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.addCleanup(self.app.stop)
        self.device = self.app.store.add("devices", models.validate_device(
            {"name": "backup", "mac": "00:11:22:33:44:55", "host": "192.0.2.10", "sleep_method": "ssh",
             "ssh_user": "root", "ssh_command": "/usr/local/sbin/wol-sleep", "hold": True, "hold_command": "hold"}))

    def test_the_hold_is_released_before_the_device_is_put_to_sleep(self):
        order = []
        with mock.patch.object(self.app.holds, "release", lambda device_id: order.append("release")), \
                mock.patch("wolserver.server.check_host", lambda host: True), \
                mock.patch("wolserver.server.power.sleep_device", lambda *a, **kw: order.append("sleep") or (True, "ok")):
            self.app.run_action(self.device, "sleep", "manual")
        self.assertEqual(order, ["release", "sleep"])

    def test_a_device_that_does_not_answer_is_not_sent_to_sleep(self):
        # Asleep or off already: the wanted state, not an SSH error ("No route to host") in the log
        sent = []
        with mock.patch("wolserver.server.check_host", lambda host: False), \
                mock.patch("wolserver.server.power.sleep_device", lambda *a, **kw: sent.append(1) or (False, "no route")):
            ok, message = self.app.run_action(self.device, "sleep", "manual")
        self.assertEqual((ok, message, sent), (True, "Already off or asleep - nothing to do", []))
        event = self.app.store.list_events(1)[0]
        self.assertEqual((event["action"], event["ok"], event["message"]),
                         ("sleep", True, "Already off or asleep - nothing to do"))

    def test_contradictions_are_shown_and_logged_once(self):
        self.app.CONFLICT_SECONDS = 0
        self.app.requests.create(self.device, "laptop-backup", 900, "manual")
        open_ = self.app.requests.snapshot([self.device["id"]])
        for r in open_[self.device["id"]]["requests"]:
            r["created"] -= self.app.BOOT_SECONDS + 1  # long since requested
        devices = [self.device]
        with self.assertLogs("wolserver", "INFO") as logs:
            first = self.app._check_conflicts(devices, {self.device["id"]: False}, {}, open_)
            again = self.app._check_conflicts(devices, {self.device["id"]: False}, {}, open_)
            gone = self.app._check_conflicts(devices, {self.device["id"]: True}, {}, open_)
        self.assertEqual(first, {self.device["id"]: ["Asleep although laptop-backup requested it"]})
        self.assertEqual((again, gone), (first, {}))
        self.assertEqual([line.split(":", 2)[2] for line in logs.output],
                         ["Contradiction: backup: Asleep although laptop-backup requested it",
                          "Contradiction gone: backup: Asleep although laptop-backup requested it"])
        # Plug off but the device answers; online at standby power
        power = {self.device["id"]: {"reachable": True, "on": False, "power_w": 0.0, "standby_w": 5.0}}
        with self.assertLogs("wolserver", "WARNING"):
            self.assertIn("plug is off", self.app._check_conflicts(devices, {self.device["id"]: True}, power, {})[
                self.device["id"]][0])
        power[self.device["id"]].update(on=True, power_w=1.2)
        with self.assertLogs("wolserver", "INFO") as logs:
            self.assertIn("standby power (1.2 W)", self.app._check_conflicts(devices, {self.device["id"]: True}, power,
                                                                               {})[self.device["id"]][0])
        self.assertTrue(any("Contradiction gone: backup: Answers although its plug is off" in line for line in logs.output))
        self.assertTrue(any("Contradiction: backup: Online at standby power" in line for line in logs.output))
        power[self.device["id"]]["power_w"] = 0.0  # below what the plug can measure: no claim either way
        self.assertEqual(self.app._check_conflicts(devices, {self.device["id"]: True}, power, {}), {})

    def test_a_short_contradiction_is_neither_shown_nor_logged(self):
        power = {self.device["id"]: {"reachable": True, "on": False, "power_w": 0.0, "standby_w": 5.0}}
        with self.assertNoLogs("wolserver", "WARNING"):
            self.assertEqual(self.app._check_conflicts([self.device], {self.device["id"]: True}, power, {}), {})


if __name__ == "__main__":
    unittest.main()
