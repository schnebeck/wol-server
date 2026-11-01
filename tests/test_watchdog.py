# WOL-Server - tests/test_watchdog.py
# Tests for the hang watch: a device that draws running power without answering gets a power cycle.
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

import unittest
from unittest import mock

from tests import test_api
from wolserver import plugs, watchdog
from wolserver.plugs import fake

DEVICE = {"id": "d1", "name": "backup", "host": "backup.home.arpa"}
PLUG = {"id": "p1", "name": "shelly-backup", "standby_w": 5.0, "running_w": 30.0, "restart_when_hung": True,
        "hang_seconds": 900, "locked": False}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class HangWatchTest(unittest.TestCase):
    def setUp(self):
        self.device, self.plug = dict(DEVICE), dict(PLUG)
        self.answers = False
        self.status = {"on": True, "power_w": 52.8}  # what backup drew all morning while it hung
        self.cycles, self.events = [], []
        self.fail = None
        self.clock = Clock()
        self.watch = watchdog.HangWatch(devices=lambda: [self.device], plug_for=lambda i: self.plug,
                                        online=lambda d: self.answers, live=lambda p: self.status,
                                        cycle=self._cycle, on_event=lambda **e: self.events.append(e),
                                        clock=self.clock)

    def _cycle(self, plug, device):
        if self.fail:
            raise plugs.PlugError(self.fail)
        self.cycles.append((plug["id"], device["id"]))
        return "Power cycle: off, on again after 10 s"

    def run_for(self, minutes, step=0.5):
        end = self.clock.now + minutes * 60
        while self.clock.now < end:
            self.watch.tick()
            self.clock.now += step * 60
        self.watch.tick()

    def test_power_cycle_after_the_time_then_give_up(self):
        self.run_for(14)
        self.assertEqual(self.cycles, [])
        state = self.watch.state("d1")
        self.assertEqual((state["state"], state["restarts"]), ("hanging", 0))
        self.assertLessEqual(state["restart_in"], 60)
        self.run_for(1)
        self.assertEqual(self.cycles, [("p1", "d1")])
        self.assertEqual(self.watch.state("d1")["state"], "restarted")
        ev = self.events[-1]
        self.assertEqual((ev["action"], ev["source"], ev["ok"], ev["device_id"]),
                         ("power_cycle", "hang watch", True, "d1"))
        self.assertEqual(ev["message"],
                         "Hangs (no answer for 900 s at 53 W): Power cycle: off, on again after 10 s (1/2)")
        # It starts again with the full time, hangs again: the second and last power cycle
        self.run_for(16)
        self.assertEqual(len(self.cycles), 2)
        self.run_for(16)
        self.assertEqual(len(self.cycles), 2)
        self.assertEqual(self.watch.state("d1")["state"], "gave_up")
        self.assertEqual(self.events[-1]["message"],
                         "Still no answer after 2 power cycles - automatic restart given up")
        self.run_for(60)
        self.assertEqual((len(self.cycles), len(self.events)), (2, 3))  # given up: quiet
        # An answer starts over
        self.answers = True
        self.watch.tick()
        self.assertIsNone(self.watch.state("d1"))
        self.answers = False
        self.run_for(15)
        self.assertEqual(len(self.cycles), 3)

    def test_below_the_running_threshold_is_no_hang(self):
        # Suspend, standby, shutting down: the time starts again
        self.status = {"on": True, "power_w": 8.0}
        self.run_for(60)
        self.assertEqual(self.cycles, [])
        self.assertIsNone(self.watch.state("d1"))
        self.status = {"on": True, "power_w": 52.8}
        self.run_for(10)
        self.status = {"on": True, "power_w": 8.0}
        self.run_for(1)
        self.status = {"on": True, "power_w": 52.8}
        self.run_for(10)
        self.assertEqual(self.cycles, [])

    def test_relay_off_or_plug_unreachable_is_no_hang(self):
        self.status = {"on": False, "power_w": 0.0}
        self.run_for(30)
        self.status = None
        self.run_for(30)
        self.assertEqual(self.cycles, [])

    def test_no_verdict_from_the_ping_changes_nothing(self):
        self.run_for(10)
        self.answers = None
        self.run_for(10)
        self.answers = False
        self.watch.tick()
        self.assertEqual(len(self.cycles), 1)  # the time ran on

    def test_only_with_the_option_a_host_and_the_running_threshold(self):
        for change in ({"restart_when_hung": False}, {"running_w": None}, {"locked": True}):
            with self.subTest(change):
                self.plug = dict(PLUG, **change)
                self.run_for(60)
                self.assertEqual(self.cycles, [])
                self.assertIsNone(self.watch.state("d1"))
        self.plug = dict(PLUG)
        self.device = dict(DEVICE, host="")
        self.run_for(60)
        self.assertEqual(self.cycles, [])
        # Switched off while it counts: forgotten
        self.device = dict(DEVICE)
        self.run_for(5)
        self.plug = dict(PLUG, restart_when_hung=False)
        self.watch.tick()
        self.assertIsNone(self.watch.state("d1"))

    def test_time_from_the_link(self):
        # backup: online within 150 s after a wake, or the power cycle follows
        self.plug = dict(PLUG, hang_seconds=150)
        self.run_for(2, step=1 / 6)
        self.assertEqual(self.cycles, [])
        self.run_for(0.5, step=1 / 6)
        self.assertEqual(len(self.cycles), 1)
        self.assertEqual(self.events[-1]["message"],
                         "Hangs (no answer for 150 s at 53 W): Power cycle: off, on again after 10 s (1/2)")

    def test_failed_power_cycle(self):
        self.fail = "Plug shelly-backup not reachable"
        self.run_for(15)
        ev = self.events[-1]
        self.assertEqual((ev["ok"], ev["message"]), (False, "Hangs (no answer for 900 s at 53 W), power cycle failed: "
                                                            "Plug shelly-backup not reachable"))
        self.assertEqual(self.watch.state("d1")["restarts"], 0)  # it did not happen, so it does not count
        self.fail = None
        self.run_for(15)
        self.assertEqual(len(self.cycles), 1)
        self.assertTrue(self.events[-1]["message"].endswith("(1/2)"))


class HangWatchLinkTest(unittest.TestCase):
    """The option at the link of device and plug, and the hang watch of the server with a simulated plug."""
    call = test_api.ApiTest.call

    def setUp(self):
        fake.register()
        self.dev = fake.FakeDevice(load_w=52.8, standby_w=0.7)
        fake.FakePlug.devices = {"plug-1": self.dev}
        test_api.ApiTest.setUp(self)
        _, self.device = self.call("POST", "/api/devices", {"name": "backup", "mac": "00:11:22:33:44:55",
                                                            "host": "192.0.2.10"})
        _, self.plug = self.call("POST", "/api/plugs", {"host": "plug-1", "name": "shelly-backup"})

    def tearDown(self):
        test_api.ApiTest.tearDown(self)
        plugs.DRIVERS.pop("fake", None)

    def link(self, **options):
        return self.call("PUT", "/api/devices/%s/plug" % self.device["id"], dict(options, plug_id=self.plug["id"]))

    def test_needs_the_running_threshold_and_a_host(self):
        status, res = self.link(restart_when_hung=True)
        self.assertEqual(status, 400)
        self.assertIn("needs the running threshold", res["error"])
        status, res = self.link(restart_when_hung=True, running_w=30, hang_seconds=30)
        self.assertEqual((status, res["error"]), (400, "Restart after must be between 60 and 14400 seconds"))
        status, res = self.link(restart_when_hung=True, running_w=30, hang_seconds=150)
        self.assertEqual((status, res["plug"]["restart_when_hung"], res["plug"]["hang_seconds"]), (200, True, 150))
        # The plug dialog keeps the option when it saves without it, and refuses to drop the threshold under it
        status, res = self.call("PUT", "/api/plugs/%s" % self.plug["id"], {"name": "shelly-backup", "host": "plug-1",
                                                                          "running_w": 30})
        self.assertEqual((status, res["restart_when_hung"]), (200, True))
        status, res = self.call("PUT", "/api/plugs/%s" % self.plug["id"], {"name": "shelly-backup", "host": "plug-1",
                                                                          "running_w": ""})
        self.assertEqual(status, 400)
        # Without a host there is no ping to watch
        _, other = self.call("POST", "/api/devices", {"name": "no-host", "mac": "00:11:22:33:44:66"})
        status, res = self.call("PUT", "/api/devices/%s/plug" % other["id"],
                                {"plug_id": self.plug["id"], "restart_when_hung": True})
        self.assertEqual(status, 400)
        self.assertIn("needs a host", res["error"])

    def test_the_server_cycles_a_hanging_device(self):
        self.link(restart_when_hung=True, running_w=30, hang_seconds=300)
        clock = Clock()
        self.app.hangs.clock = clock
        with mock.patch.object(self.app, "_answers", return_value=False), \
                mock.patch.object(self.app.plugs, "CYCLE_SECONDS", 0.05):
            self.app.hangs.online = self.app._answers
            self.app.hangs.tick()
            _, res = self.call("GET", "/api/status")
            self.assertEqual(res["hangs"][self.device["id"]]["state"], "hanging")
            self.assertTrue(res["power"][self.device["id"]]["restart_when_hung"])
            clock.now += 5 * 60
            self.app.hangs.tick()
        self.assertFalse(self.dev.on)  # off now, the plug switches back on by itself
        events = self.call("GET", "/api/events")[1]["events"]
        self.assertEqual((events[0]["action"], events[0]["source"], events[0]["device"]),
                         ("power_cycle", "hang watch", "backup"))
        _, res = self.call("GET", "/api/status")
        self.assertEqual(res["hangs"][self.device["id"]]["state"], "restarted")


if __name__ == "__main__":
    unittest.main()
