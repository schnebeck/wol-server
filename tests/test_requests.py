# WOL-Server - tests/test_requests.py
# Tests for requests: several clients share a device, it sleeps after the last one.
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

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from wolserver import plugs
from wolserver.config import DEFAULTS
from wolserver.devrequests import DeviceRequests, RequestError
from wolserver.plugs import fake
from wolserver.server import App, Server

DEVICE = {"id": "dev1", "name": "backup"}


class Clock:
    def __init__(self, ts=1_800_000_000.0):
        self.ts = ts

    def __call__(self):
        return self.ts


class DeviceRequestsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "requests.json")
        self.clock = Clock()
        self.slept, self.events, self.sleep_ok = [], [], True
        self.book = self.make()

    def make(self):
        def sleep(device, source):
            self.slept.append((device["name"], source))
            return self.sleep_ok, "done" if self.sleep_ok else "refused"
        return DeviceRequests(self.path, sleep, lambda i: DEVICE if i == "dev1" else None,
                              on_event=lambda **e: self.events.append(e), clock=self.clock)

    def later(self, seconds):
        self.clock.ts += seconds
        self.book.tick()

    def test_heartbeat_keeps_a_request_and_its_absence_expires_it(self):
        req = self.book.create(DEVICE, "aida", ttl=120)
        self.assertEqual((req["client"], req["expires"]), ("aida", self.clock.ts + 120))
        for _ in range(10):  # far longer than the ttl, but with heartbeats
            self.later(100)
            self.book.renew("dev1", req["id"])
        self.assertEqual(self.book.clients("dev1"), ["aida"])
        self.later(121)
        self.assertEqual(self.book.clients("dev1"), [])
        self.assertEqual(self.events[-1]["message"], "Expired: aida (no heartbeat for 2 min)")
        with self.assertRaises(KeyError):
            self.book.renew("dev1", req["id"])

    def test_until_released_needs_no_heartbeat(self):
        self.book.create(DEVICE, "ki-job", ttl=0)
        self.later(30 * 86400)
        self.assertEqual(self.book.clients("dev1"), ["ki-job"])

    def test_sleep_waits_for_the_last_request_and_a_grace_period(self):
        a = self.book.create(DEVICE, "aida")
        m = self.book.create(DEVICE, "mail")
        state = self.book.release("dev1", a["id"], then_sleep=True)
        self.assertEqual((state["sleep"], state["waiting_for"]), ("pending", ["mail"]))
        for _ in range(6):  # mail still works for an hour, with heartbeats
            self.later(600)
            self.book.renew("dev1", m["id"])
        self.assertEqual(self.slept, [])
        self.book.release("dev1", m["id"])  # without asking for sleep: aida's request still counts
        self.later(60)
        self.assertEqual(self.slept, [])  # grace period
        s = self.book.create(DEVICE, "susi")  # the next client takes over: no sleep in between
        self.later(200)
        self.assertEqual(self.slept, [])
        self.book.release("dev1", s["id"])
        self.later(121)
        self.assertEqual(self.slept, [("backup", "requests")])
        self.assertIsNone(self.book.get("dev1")["sleep"])

    def test_sleep_when_idle(self):
        self.assertEqual(self.book.request_sleep(DEVICE, "schedule")["sleep"], "now")
        r = self.book.create(DEVICE, "aida")
        state = self.book.request_sleep(DEVICE, "schedule")
        self.assertEqual((state["sleep"], state["waiting_for"]), ("pending", ["aida"]))
        self.assertEqual(self.events[-1]["message"], "Sleep requested by schedule: waits for aida")
        self.assertTrue(self.book.cancel_sleep("dev1"))
        self.book.release("dev1", r["id"])
        self.later(300)
        self.assertEqual(self.slept, [])
        self.assertFalse(self.book.cancel_sleep("dev1"))

    def test_an_expired_request_lets_the_device_sleep(self):
        r = self.book.create(DEVICE, "laptop", ttl=60)
        self.book.request_sleep(DEVICE, "schedule")
        self.later(61)  # laptop gone without a word
        self.assertEqual(self.slept, [])
        self.later(121)
        self.assertEqual(len(self.slept), 1)
        with self.assertRaises(KeyError):
            self.book.release("dev1", r["id"])

    def test_a_refused_sleep_is_retried_and_then_given_up(self):
        self.sleep_ok = False
        r = self.book.create(DEVICE, "aida")
        self.book.release("dev1", r["id"], then_sleep=True)
        for _ in range(10):
            self.later(301)
        self.assertEqual(len(self.slept), 7)
        self.assertEqual(self.events[-1]["message"], "Sleep given up after 7 attempts")
        self.assertIsNone(self.book.get("dev1")["sleep"])

    def test_requests_survive_a_restart(self):
        r = self.book.create(DEVICE, "aida", ttl=600)
        self.book.create(DEVICE, "mail")
        self.book.request_sleep(DEVICE, "schedule")
        self.clock.ts += 590
        self.book = self.make()  # restart: one ttl from now for the next heartbeat
        self.later(300)
        self.assertEqual(self.book.clients("dev1"), ["aida", "mail"])
        self.assertEqual(self.book.get("dev1")["sleep"]["by"], "schedule")
        self.book.renew("dev1", r["id"])
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["requests"]), 2)

    def test_validation(self):
        for client, ttl in (("", None), ("x" * 65, None), ("a\nb", None), ("aida", 30), ("aida", "lang"),
                            ("aida", True), ("aida", 8 * 86400)):
            with self.assertRaises(RequestError):
                self.book.create(DEVICE, client, ttl)
        self.book.forget_device("dev1")


class RequestApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = App(dict(DEFAULTS, listen="127.0.0.1", port=0, data_dir=self.tmp))
        self.app.auth.set_password("geheim123")
        self.server = Server("127.0.0.1", 0, self.app)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.sent, self.slept = [], []
        for target, fn in (("wolserver.wol.send_magic_packet", lambda *a, **kw: self.sent.append(a)),
                           ("wolserver.power.sleep_device", lambda d, *a, **kw: (self.slept.append(d["name"]) or True,
                                                                                  "SSH command executed"))):
            patcher = mock.patch(target, fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.device = self.app.store.add("devices", {
            "name": "backup", "mac": "a8:b8:e0:07:4b:4e", "host": "192.168.1.10", "broadcast": "255.255.255.255",
            "wol_port": 9, "sleep_method": "ssh", "ssh_user": "root", "ssh_port": 22, "ssh_command": "poweroff"})
        self.token = self.app.tokens.create("aida")["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.stop()
        shutil.rmtree(self.tmp)
        plugs.DRIVERS.pop("fake", None)

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        hdrs = {"Authorization": "Bearer " + self.token}
        data = None
        if body is not None:
            data = json.dumps(body)
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, data, hdrs)
        resp = conn.getresponse()
        payload = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, payload

    def test_backup_clients_share_the_server(self):
        answers = iter([False, False, True])
        with mock.patch("wolserver.server.check_host", lambda host: next(answers)), mock.patch("time.sleep"):
            status, a = self.call("POST", "/api/devices/backup/requests?wait=60", {"client": "aida"})
        self.assertEqual((status, a["online"], a["client"], a["ttl"]), (201, True, "aida", 900))
        self.assertEqual(len(self.sent), 1)  # woken
        with mock.patch("wolserver.server.check_host", lambda host: True):
            status, m = self.call("POST", "/api/devices/backup/requests", {"client": "mail", "ttl": 0})
        self.assertEqual((status, m["online"], len(self.sent)), (201, True, 1))  # online: nothing sent
        status, res = self.call("GET", "/api/devices/backup/requests")
        self.assertEqual([r["client"] for r in res["requests"]], ["aida", "mail"])
        self.assertEqual(self.call("PUT", "/api/devices/backup/requests/%s" % a["id"])[0], 200)
        self.assertEqual(self.call("PUT", "/api/devices/backup/requests/gibtsnicht")[0], 404)
        other = self.app.requests.create(self.device, "susi", source="API: susi")
        self.assertEqual(self.call("PUT", "/api/devices/backup/requests/%s" % other["id"])[0], 403)  # not ours
        self.assertEqual(self.call("DELETE", "/api/devices/backup/requests/%s" % other["id"])[0], 403)
        self.app.requests.release(self.device["id"], other["id"])

        # Plain sleep asks first, when=idle waits, force goes through
        status, res = self.call("POST", "/api/devices/backup/sleep")
        self.assertEqual((status, res["confirm"], res["waiting_for"]), (409, True, ["aida", "mail"]))
        status, res = self.call("POST", "/api/devices/backup/sleep?when=idle")
        self.assertEqual((status, res["pending"]), (202, True))
        self.assertEqual(self.call("DELETE", "/api/devices/backup/sleep")[0], 200)
        self.assertEqual(self.call("DELETE", "/api/devices/backup/sleep")[0], 404)
        self.assertEqual(self.slept, [])

        # aida finishes first: sleep waits for mail; mail finishes: sleep after the grace period
        status, res = self.call("DELETE", "/api/devices/backup/requests/%s?then=sleep" % a["id"])
        self.assertEqual((status, res["sleep"], res["waiting_for"]), (200, "pending", ["mail"]))
        _, st = self.call("GET", "/api/status")
        self.assertEqual(st["requests"][self.device["id"]]["sleep"]["by"], "aida")
        status, res = self.call("DELETE", "/api/devices/backup/requests/%s" % m["id"])
        self.assertEqual((status, res["waiting_for"]), (200, []))
        self.app.requests.grace = 0
        with mock.patch.object(self.app.requests, "clock", lambda: res["sleep_at"] + 1):
            self.app.requests.tick()
        self.assertEqual(self.slept, ["backup"])
        events = [e["message"] for e in self.app.store.list_events()][::-1]
        self.assertIn("Requested by aida", events)
        self.assertIn("Finished: aida (sleep requested)", events)
        self.assertEqual(self.app.store.list_events()[0]["source"], "requests")

        status, res = self.call("POST", "/api/devices/backup/sleep?force=1")
        self.assertEqual(status, 200)
        self.assertEqual(self.call("POST", "/api/devices/backup/requests", {"client": "x", "ttl": 5})[0], 400)
        self.assertEqual(self.call("POST", "/api/devices/backup/sleep?when=later")[0], 400)
        self.assertEqual(self.call("GET", "/api/devices/nas/requests")[0], 404)

    def test_a_schedule_waits_and_plugs_stay_on(self):
        req = self.app.requests.create(self.device, "ki-job", ttl=0)
        ok, message = self.app.run_scheduled(self.device, "sleep", "schedule")
        self.assertEqual((ok, message, self.slept), (True, "Deferred: in use by ki-job", []))

        fake.register()
        fake.FakePlug.devices = {"plug-1": fake.FakeDevice(load_w=80)}
        plug = self.app.plugs.add({"host": "plug-1", "name": "plug-backup", "consumer_device": self.device["id"]})
        ok, message = self.app.run_plug_action(plug["id"], "power_off", True, "schedule")
        self.assertEqual((ok, message), (False, "Skipped: backup is in use by ki-job"))
        self.assertTrue(fake.FakePlug.devices["plug-1"].on)

        self.app.requests.grace = 0
        self.app.requests.release(self.device["id"], req["id"])
        self.app.requests.tick()
        self.assertEqual(self.slept, ["backup"])  # the deferred schedule
