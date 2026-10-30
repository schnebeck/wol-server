# WOL-Server - tests/test_energy.py
# Tests of the energy log, the plug alerts and plugs linked to devices.
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

import datetime
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from tests import test_api
from wolserver import plugs
from wolserver.plugs import fake
from wolserver.plugs.energy import EnergyLog


class Clock:
    def __init__(self, ts):
        self.ts = ts

    def __call__(self):
        return self.ts


def local(text):
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M"))


class EnergyLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "energy.db")
        self.clock = Clock(local("2026-10-27 10:00"))
        self.log = EnergyLog(self.path, clock=self.clock)
        self.day = datetime.date(2026, 10, 27)

    def tearDown(self):
        self.log.close()
        shutil.rmtree(self.tmp)

    def read(self, plug, ts, counter=None, power=None, temp=None):
        self.clock.ts = ts
        self.log.record(plug, {"ts": ts, "energy_wh": counter, "power_w": power, "temperature_c": temp})

    def hours(self, plug="p"):
        return self.log.series("day", self.day)["plugs"].get(plug)

    def test_recent_power_in_5_s_steps(self):
        t0 = local("2026-10-27 10:00")
        self.read("p", t0 - 200, 1000, 40)  # too old to hold into the window
        for k in range(12):  # live view open: a reading every 5 s
            self.read("p", t0 + 3000 + k * 5, 1000, 100 + k)
        self.read("p", t0 + 3300, 1001, 7)  # view closed: once a minute
        self.read("p", t0 + 3360, 1001, 8)
        self.clock.ts = t0 + 3400
        res = self.log.recent(["p", "q"])
        values, start = res["plugs"]["p"], res["start"]
        self.assertEqual((res["step"], len(values), start), (5, 720, t0 + 3400 - 719 * 5))
        at = lambda ts: values[int(ts - start) // 5]  # noqa: E731
        self.assertIsNone(at(t0 + 2990))
        self.assertEqual((at(t0 + 3000), at(t0 + 3055)), (100.0, 111.0))
        self.assertEqual(at(t0 + 3140), 111.0)  # the last reading holds for 90 s
        self.assertIsNone(at(t0 + 3150))
        self.assertEqual((at(t0 + 3330), at(t0 + 3400)), (7.0, 8.0))
        self.assertEqual(res["plugs"]["q"], [None] * 720)

    def test_counter_differences_per_hour(self):
        self.read("p", local("2026-10-27 10:00"), 1000, 60)
        self.read("p", local("2026-10-27 10:30"), 1030, 60)
        self.read("p", local("2026-10-27 11:30"), 1090, 60)  # spans two hours: split by time
        values = self.hours()
        self.assertEqual(values[10], 60.0)
        self.assertEqual(values[11], 30.0)
        self.assertIsNone(values[12])

    def test_counter_reset_counts_the_new_value(self):
        self.read("p", local("2026-10-27 10:00"), 5000)
        self.read("p", local("2026-10-27 10:10"), 5010)
        self.read("p", local("2026-10-27 10:20"), 4)  # firmware update: counter starts again
        self.assertEqual(self.hours()[10], 14.0)

    def test_older_readings_do_not_count_twice(self):
        self.read("p", local("2026-10-27 10:00"), 100)
        self.read("p", local("2026-10-27 10:10"), 110)
        self.read("p", local("2026-10-27 10:05"), 105)
        self.assertEqual(self.hours()[10], 10.0)

    def test_without_counter_the_power_is_integrated(self):
        self.read("p", local("2026-10-27 10:00"), power=120)
        self.read("p", local("2026-10-27 10:01"), power=120)
        self.read("p", local("2026-10-27 10:31"), power=120)  # gap: at most 5 minutes count
        self.assertEqual(self.hours()[10], 12.0)

    def test_flush_once_per_hour_and_restart_covers_the_gap(self):
        self.read("p", local("2026-10-27 10:00"), 0)
        self.read("p", local("2026-10-27 10:30"), 50)

        def db_rows():
            return self.log._db.execute("SELECT COUNT(*) FROM hours").fetchone()[0]

        self.assertEqual(db_rows(), 0)  # still in RAM
        self.read("p", local("2026-10-27 11:00"), 100)
        self.assertEqual(db_rows(), 2)  # new hour: written in one go
        self.log.close()
        # Server off for two hours; the first reading afterwards covers the gap
        self.clock.ts = local("2026-10-27 13:00")
        self.log = EnergyLog(self.path, clock=self.clock)
        self.read("p", local("2026-10-27 13:00"), 300)
        values = self.hours()
        self.assertEqual(values[10:13], [100.0, 100.0, 100.0])

    def test_month_year_totals_and_curve(self):
        self.read("a", local("2026-10-26 10:00"), 0, 50)
        self.read("a", local("2026-10-26 11:00"), 50, 50)
        self.read("a", local("2026-10-27 09:00"), 100, 50)  # 50 Wh spread over the night
        self.read("b", local("2026-10-27 09:00"), 0, 10)
        self.read("b", local("2026-10-27 09:30"), 5, 10)
        month = self.log.series("month", self.day)
        self.assertEqual(len(month["buckets"]), 31)
        self.assertAlmostEqual(sum(v or 0 for v in month["plugs"]["a"]), 100.0, places=1)
        self.assertEqual(month["plugs"]["b"][26], 5.0)
        year = self.log.series("year", self.day)
        self.assertAlmostEqual(year["plugs"]["a"][9], 100.0, places=1)
        totals = self.log.totals(["a", "b"])
        self.assertEqual(totals["b"], {"day": 5.0, "month": 5.0, "year": 5.0})
        curve = self.log.curve(self.day, ["b"])
        self.assertEqual(curve["step"], 300)
        self.assertEqual(curve["plugs"]["b"][9 * 12], 10.0)  # 09:00 from the minute values
        self.assertIsNone(curve["plugs"]["b"][20 * 12])  # not yet

    def test_standby_estimate(self):
        ts = local("2026-10-26 00:00")
        for hour in range(48):  # 2 days: 2 W standby, 2 hours a day running at 100 W
            running = hour % 24 in (19, 20)
            self.read("p", ts + hour * 3600, hour * 2.0, 100 if running else 2)
        self.clock.ts = ts + 48 * 3600
        # 47 hours of differences: 4 running hours, the hour after each running hour is not quiet either
        estimate = self.log.standby("p", 5)
        self.assertGreater(estimate, 2 * 8760 * 0.8)
        self.assertLess(estimate, 2 * 8760)
        self.assertIsNone(self.log.standby("other", 5))

    def test_forget(self):
        self.read("p", local("2026-10-27 10:00"), 0)
        self.read("p", local("2026-10-27 11:00"), 10)
        self.log.forget("p")
        self.assertIsNone(self.hours())


class AlertTest(unittest.TestCase):
    def setUp(self):
        fake.register()
        self.tmp = tempfile.mkdtemp()
        self.dev = fake.FakeDevice(load_w=40)
        fake.FakePlug.devices = {"plug-1": self.dev}
        self.events = []
        self.mgr = plugs.PlugManager(self.tmp, on_event=lambda **e: self.events.append(e))
        self.plug = self.mgr.add({"host": "plug-1", "name": "Desk"})
        self.mono = 1000.0

    def tearDown(self):
        self.mgr.shutdown()
        shutil.rmtree(self.tmp)
        plugs.DRIVERS.pop("fake", None)

    def fetch(self, advance=0):
        self.mono += advance
        with mock.patch("time.monotonic", lambda: self.mono):
            self.mgr._fetch(self.mgr.get(self.plug["id"]))
        return [a["key"] for a in self.mgr.alerts(self.plug["id"])]

    def test_welded_contacts(self):
        self.dev.on = False
        self.dev.load_w = 0
        with mock.patch.object(fake.FakeDevice, "power_w", 3.0):  # relay off, but power flows
            self.assertEqual(self.fetch(), [])
            self.assertEqual(self.fetch(45), ["welded"])
        self.assertEqual(self.fetch(60), [])
        self.assertEqual([(e["ok"], e["message"][:21]) for e in self.events],
                         [(False, "Relay is off, but 3 W"), (True, "Cleared: Relay is off")])

    def test_temperature_errors_wear_and_unreachable(self):
        self.dev.temperature_c = 75
        self.dev.errors = ["overtemp"]
        self.dev.switch_count = 8500
        self.assertEqual(sorted(self.fetch()), ["error:overtemp", "hot", "wear"])
        self.assertEqual(len(self.events), 3)
        self.fetch(60)
        self.assertEqual(len(self.events), 3)  # logged once
        fake.FakePlug.devices = {}
        self.assertEqual(sorted(self.fetch(60)), ["error:overtemp", "hot", "wear"])  # unknown: kept
        self.assertIn("unreachable", self.fetch(400))
        listed = self.mgr.list()[0]
        self.assertIn("Plug not reachable", [a["message"] for a in listed["alerts"]])


class LinkedDeviceTest(unittest.TestCase):
    call = test_api.ApiTest.call

    def setUp(self):
        fake.register()
        self.dev = fake.FakeDevice(load_w=0, standby_w=1.5)
        fake.FakePlug.devices = {"plug-1": self.dev}
        test_api.ApiTest.setUp(self)
        self.sent = []
        patcher = mock.patch("wolserver.wol.send_magic_packet", lambda *a, **kw: self.sent.append(a))
        patcher.start()
        self.addCleanup(patcher.stop)
        _, self.device = self.call("POST", "/api/devices", {"name": "Office PC", "mac": "00:11:22:33:44:55"})
        _, self.plug = self.call("POST", "/api/plugs", {"host": "plug-1", "name": "plug-office"})

    def tearDown(self):
        test_api.ApiTest.tearDown(self)
        plugs.DRIVERS.pop("fake", None)

    def link(self, **options):
        return self.call("PUT", "/api/devices/%s/plug" % self.device["id"], dict(options, plug_id=self.plug["id"]))

    def test_link_from_both_sides(self):
        status, res = self.link(wake_method="power_wol", cut_after_sleep=True)
        self.assertEqual((status, res["plug"]["consumer"]), (200, {"kind": "device", "id": self.device["id"]}))
        _, res = self.call("GET", "/api/plugs")
        self.assertEqual(res["plugs"][0]["consumer_name"], "Office PC")
        self.assertEqual(res["plugs"][0]["energy"]["day"], 0)
        _, res = self.call("GET", "/api/status")
        power = res["power"][self.device["id"]]
        self.assertEqual((power["plug"], power["power_w"], power["wake_method"]), ("plug-office", 1.5, "power_wol"))
        # Plug side: a second plug cannot take the same device
        fake.FakePlug.devices["plug-2"] = fake.FakeDevice(mac="02:00:00:00:00:02")
        status, res = self.call("POST", "/api/plugs", {"host": "plug-2", "name": "other",
                                                       "consumer_device": self.device["id"]})
        self.assertEqual((status, res["error"]), (400, "This device is already plugged into plug-office"))
        # Unlink from the device side
        self.call("PUT", "/api/devices/%s/plug" % self.device["id"], {"plug_id": None})
        self.assertIsNone(self.call("GET", "/api/plugs")[1]["plugs"][0]["consumer"])
        # Deleting the device keeps its name at the plug
        self.link()
        self.call("DELETE", "/api/devices/%s" % self.device["id"])
        self.assertEqual(self.call("GET", "/api/plugs")[1]["plugs"][0]["consumer"],
                         {"kind": "label", "label": "Office PC"})

    def test_wake_methods(self):
        self.link(wake_method="power")
        self.dev.on = False
        status, res = self.call("POST", "/api/devices/%s/wake" % self.device["id"])
        self.assertEqual((status, res["message"]), (200, "Power switched on via plug-office"))
        self.assertTrue(self.dev.on)
        self.assertEqual(self.sent, [])
        # Relay on, device in standby: a power cycle starts it
        status, res = self.call("POST", "/api/devices/%s/wake" % self.device["id"])
        self.assertEqual(res["message"], "Power cycle via plug-office: the device starts when the power returns")
        self.dev.set(True)
        self.dev.load_w = 90
        status, res = self.call("POST", "/api/devices/%s/wake" % self.device["id"])
        self.assertEqual(res["message"], "Power is already on: Office PC draws 90 W")
        # Power on, then WOL
        self.link(wake_method="power_wol")
        self.dev.on, self.dev.load_w = False, 0
        with mock.patch("wolserver.server.App.POWER_WOL_DELAY", 0):
            status, res = self.call("POST", "/api/devices/%s/wake" % self.device["id"])
        self.assertTrue(self.dev.on)
        self.assertEqual(len(self.sent), 1)
        events = [e["action"] for e in self.call("GET", "/api/events")[1]["events"]]
        self.assertEqual(events[:2], ["wake", "power_on"])

    def test_wake_by_wol_switches_the_power_on_first(self):
        # Wake method "wol" (the default): with the relay on only the magic packet goes out ...
        self.link()
        status, res = self.call("POST", "/api/devices/%s/wake" % self.device["id"])
        self.assertEqual((status, len(self.sent), self.dev.switch_count), (200, 1, 0))
        # ... with the relay off the packet would reach nothing: power first, then the packet
        self.dev.on = False
        with mock.patch("wolserver.server.App.POWER_WOL_DELAY", 0):
            status, res = self.call("POST", "/api/devices/%s/wake" % self.device["id"])
        self.assertEqual((status, self.dev.on, len(self.sent)), (200, True, 2))
        events = [e["action"] for e in self.call("GET", "/api/events")[1]["events"]]
        self.assertEqual(events[:2], ["wake", "power_on"])
        # A plug that does not answer does not stop the packet
        fake.FakePlug.devices = {}
        status, res = self.call("POST", "/api/devices/%s/wake" % self.device["id"])
        self.assertEqual((status, len(self.sent)), (200, 3))

    def test_cut_after_sleep(self):
        self.link(cut_after_sleep=True)
        self.app.CUT_INTERVAL = 0.01
        self.app.CUT_WAIT = 1
        device = self.app.store.get("devices", self.device["id"])
        self.app._cut_after_sleep(device, self.plug["id"], "manual")
        self.assertFalse(self.dev.on)  # standby only: power cut
        # The device does not shut down: power stays on, a warning is logged
        self.dev.set(True)
        self.dev.load_w = 60
        self.app.CUT_WAIT = 0.1
        self.app._cut_after_sleep(device, self.plug["id"], "manual")
        self.assertTrue(self.dev.on)
        event = self.call("GET", "/api/events")[1]["events"][0]
        self.assertEqual(event["message"], "Power not cut: Office PC still draws 60 W after 0 minutes")

    def test_never_switched_off(self):
        # The WOL-Server measures its own plug, but never cuts it (rule 6)
        status, res = self.call("PUT", "/api/plugs/%s" % self.plug["id"], {"host": "plug-1", "name": "plug-office",
                                                                              "consumer_self": True})
        self.assertEqual((status, res["consumer"], res["locked"]), (200, {"kind": "self"}, True))
        self.assertEqual(self.call("GET", "/api/plugs")[1]["plugs"][0]["consumer_name"], "WOL-Server")
        for action in ("off", "cycle"):
            status, res = self.call("POST", "/api/plugs/%s/%s" % (self.plug["id"], action), {"confirm": True})
            self.assertEqual((status, res["error"]),
                             (400, "plug-office supplies the WOL-Server or the network: it is never switched off"))
        self.assertTrue(self.dev.on)
        self.assertEqual(self.call("POST", "/api/plugs/%s/on" % self.plug["id"], {})[0], 200)
        base = {"plug_id": self.plug["id"], "time": "23:00", "days": [0]}
        self.assertEqual(self.call("POST", "/api/schedules", dict(base, action="power_off", force=True))[0], 400)
        self.assertEqual(self.call("POST", "/api/schedules", dict(base, action="power_on"))[0], 201)
        # A second plug cannot also supply the WOL-Server; a locked router plug is fine
        fake.FakePlug.devices["plug-2"] = fake.FakeDevice(mac="02:00:00:00:00:02")
        status, res = self.call("POST", "/api/plugs", {"host": "plug-2", "name": "other", "consumer_self": True})
        self.assertEqual((status, res["error"]), (400, "The WOL-Server is already plugged into plug-office"))
        status, res = self.call("POST", "/api/plugs", {"host": "plug-2", "name": "router", "consumer_label": "Router",
                                                       "locked": True})
        self.assertEqual((status, res["locked"]), (201, True))

    def test_energy_api_and_settings(self):
        status, res = self.call("GET", "/api/energy?period=day")
        self.assertEqual(status, 200)
        self.assertEqual(len(res["curve"]["plugs"][self.plug["id"]]), 288)
        self.assertEqual(res["plugs"][0]["name"], "plug-office")
        status, res = self.call("GET", "/api/energy?period=year&date=2026-03-01")
        self.assertEqual((status, len(res["buckets"])), (200, 12))
        status, res = self.call("GET", "/api/energy?period=hour")
        self.assertEqual((status, res["period"], len(res["curve"]["plugs"][self.plug["id"]])), (200, "hour", 720))
        self.assertIsNotNone(res["plugs"][0]["power_w"])
        self.assertIsNotNone(res["curve"]["plugs"][self.plug["id"]][-1])  # just read
        self.assertEqual(self.call("GET", "/api/energy?period=week")[0], 400)
        self.assertEqual(self.call("GET", "/api/energy?date=yesterday")[0], 400)
        status, res = self.call("PUT", "/api/energy/settings", {"price_kwh": "0.32", "currency": "€"})
        self.assertEqual(res["settings"], {"price_kwh": 0.32, "currency": "€"})
        self.assertEqual(self.call("PUT", "/api/energy/settings", {"price_kwh": -1})[0], 400)
        self.assertEqual(self.call("GET", "/api/plugs")[1]["settings"]["price_kwh"], 0.32)


if __name__ == "__main__":
    unittest.main()
