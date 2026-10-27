# WOL-Server - tests/test_api.py
# Tests: HTTP API, sessions, access list and API tokens.
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
import shutil
import tempfile
import threading
import unittest

from wolserver.config import DEFAULTS
from wolserver.server import App, Server


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfg = dict(DEFAULTS, listen="127.0.0.1", port=0, data_dir=self.tmp)
        self.app = App(cfg)
        self.server = Server("127.0.0.1", 0, self.app)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cookie = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.stop()
        shutil.rmtree(self.tmp)

    def call(self, method, path, body=None, csrf=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if csrf:
            headers["X-Requested-With"] = "wol-server"
        if self.cookie:
            headers["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, data, headers)
        resp = conn.getresponse()
        raw = resp.read()
        cookie = resp.getheader("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";")[0]
        conn.close()
        ctype = resp.getheader("Content-Type", "")
        return resp.status, (json.loads(raw) if ctype.startswith("application/json") else raw)

    def test_static_and_security_headers(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read()
        self.assertEqual(resp.status, 200)
        self.assertIn(b"WOL-Server", body)
        self.assertIn("default-src 'self'", resp.getheader("Content-Security-Policy"))
        etag = resp.getheader("ETag")
        conn.request("GET", "/", headers={"If-None-Match": etag})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 304)
        conn.request("GET", "/../wolserver/server.py")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 404)

    def test_device_and_schedule_crud(self):
        status, dev = self.call("POST", "/api/devices", {"name": "PC", "mac": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(status, 201)
        status, sched = self.call("POST", "/api/schedules", {
            "device_id": dev["id"], "action": "wake", "time": "06:45", "days": [0, 1, 2, 3, 4],
        })
        self.assertEqual(status, 201)
        self.assertTrue(sched["next_run"].endswith("06:45"))
        status, data = self.call("GET", "/api/schedules")
        self.assertEqual(len(data["schedules"]), 1)
        status, upd = self.call("PUT", "/api/devices/%s" % dev["id"], dict(dev, name="Neu"))
        self.assertEqual(upd["name"], "Neu")
        status, _ = self.call("DELETE", "/api/devices/%s" % dev["id"])
        self.assertEqual(status, 200)
        status, data = self.call("GET", "/api/schedules")
        self.assertEqual(data["schedules"], [])
        self.assertEqual(self.call("DELETE", "/api/devices/nope")[0], 404)

    def test_wake_logs_event(self):
        _, dev = self.call("POST", "/api/devices", {
            "name": "PC", "mac": "aa:bb:cc:dd:ee:ff", "broadcast": "127.0.0.1", "wol_port": 9,
        })
        status, res = self.call("POST", "/api/devices/%s/wake" % dev["id"])
        self.assertEqual(status, 200)
        self.assertTrue(res["ok"])
        _, events = self.call("GET", "/api/events")
        self.assertEqual(events["events"][0]["source"], "manual")
        status, res = self.call("POST", "/api/devices/%s/sleep" % dev["id"])
        self.assertEqual(status, 400)

    def test_csrf_and_errors(self):
        self.assertEqual(self.call("POST", "/api/devices", {"name": "x"}, csrf=False)[0], 403)
        self.assertEqual(self.call("GET", "/api/unknown")[0], 404)
        self.assertEqual(self.call("PATCH", "/api/devices")[0], 501)
        self.assertEqual(self.call("PUT", "/api/devices")[0], 405)
        status, err = self.call("POST", "/api/devices", {"name": "x", "mac": "123"})
        self.assertEqual(status, 400)
        self.assertIn("MAC", err["error"])

    def test_login_flow(self):
        self.app.auth.set_password("geheim123")
        self.assertEqual(self.call("GET", "/api/devices")[0], 401)
        _, session = self.call("GET", "/api/session")
        self.assertEqual(session, {"auth_required": True, "authenticated": False, "hotspot": False})
        self.assertEqual(self.call("POST", "/api/login", {"password": "falsch"})[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"password": "geheim123"})[0], 200)
        self.assertEqual(self.call("GET", "/api/devices")[0], 200)
        self.call("POST", "/api/logout")
        self.assertEqual(self.call("GET", "/api/devices")[0], 401)

    def test_client_network_allowlist(self):
        from wolserver.server import AccessList
        self.app.access = AccessList(["10.9.9.0/24"])
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/session")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 403)

    def test_hotspot_clients_and_captive_portal(self):
        # requests arriving at the hotspot address: always allowed, foreign hosts redirected
        from unittest import mock

        from wolserver import wifi

        self.app.networks = []
        with mock.patch.object(wifi, "HOTSPOT_ADDRESS", "127.0.0.1"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port)
            conn.request("GET", "/generate_204", headers={"Host": "connectivitycheck.gstatic.com"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 302)
            self.assertEqual(resp.getheader("Location"), "http://127.0.0.1:%d/" % self.port)
            status, session = self.call("GET", "/api/session")
            self.assertEqual(status, 200)
            self.assertTrue(session["hotspot"])


if __name__ == "__main__":
    unittest.main()


class AccessListTest(unittest.TestCase):
    def test_auto_follows_interfaces(self):
        import ipaddress

        from wolserver.server import AccessList
        nets = [{"network": "192.168.1.0/24", "interface": "wlan0", "kind": "wifi"}]
        now = [0.0]
        calls = []

        def source():
            calls.append(now[0])
            return list(nets)
        acl = AccessList(["auto", "172.20.0.0/16"], source=source, clock=lambda: now[0])
        ip = ipaddress.ip_address
        for allowed in ("192.168.1.12", "127.0.0.1", "::1", "fe80::1", "172.20.3.4"):
            self.assertTrue(acl.allows(ip(allowed)), allowed)
        self.assertFalse(acl.allows(ip("10.1.1.7")))
        # VPN connects: an unknown sender triggers a new query after RETRY
        nets.append({"network": "10.1.1.0/24", "interface": "tun0", "kind": "vpn"})
        self.assertFalse(acl.allows(ip("10.1.1.7")))  # just looked
        now[0] += AccessList.RETRY
        self.assertTrue(acl.allows(ip("10.1.1.7")))
        # known senders trigger no query as long as REFRESH has not expired
        before = len(calls)
        acl.allows(ip("192.168.1.12"))
        self.assertEqual(len(calls), before)
        sources = {e["network"]: e["source"] for e in acl.describe()}
        self.assertEqual(sources["10.1.1.0/24"], "tun0")
        self.assertEqual(sources["172.20.0.0/16"], "config")
        self.assertEqual(sources["127.0.0.0/8"], "auto")
        # without "auto": only the fixed entries, no query of the interfaces
        fixed = AccessList(["192.168.1.0/24"], source=lambda: self.fail("no query expected"))
        self.assertFalse(fixed.allows(ip("127.0.0.1")))
        self.assertTrue(fixed.allows(ip("192.168.1.5")))

    def test_ipv6_prefixes(self):
        import os
        import tempfile

        from wolserver import netinfo
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("fd001234000000000000000000000008 03 40 00 00     wlan0\n"
                    "fe80000000000000023af2cf44f40ffc 03 40 20 80     wlan0\n"
                    "00000000000000000000000000000001 01 80 10 80        lo\n")
        try:
            self.assertEqual(netinfo.ipv6_prefixes(f.name), [("wlan0", "fd00:1234::/64")])
        finally:
            os.unlink(f.name)


class ApiTokenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfg = dict(DEFAULTS, listen="127.0.0.1", port=0, data_dir=self.tmp)
        self.app = App(cfg)
        self.app.auth.set_password("geheim123")
        self.server = Server("127.0.0.1", 0, self.app)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.sent = []
        from unittest import mock
        patcher = mock.patch("wolserver.wol.send_magic_packet", lambda *a, **kw: self.sent.append(a))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.stop()
        shutil.rmtree(self.tmp)

    def request(self, method, path, token=None, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        hdrs = dict(headers or {})
        if token:
            hdrs["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = json.dumps(body)
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, data, hdrs)
        resp = conn.getresponse()
        payload = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, payload

    def test_tokens(self):
        from unittest import mock
        device = self.app.store.add("devices", {"name": "Backup-Server", "mac": "a8:b8:e0:07:4b:4e", "host": "192.168.1.10",
                                                "broadcast": "255.255.255.255", "wol_port": 9, "sleep_method": "none"})
        entry = self.app.tokens.create("backup script")
        token = entry["token"]
        self.assertTrue(token.startswith("wol_"))
        with open(self.app.tokens.path, encoding="utf-8") as f:
            self.assertNotIn(token, f.read())  # only the hash is stored

        # Wake by name, case-insensitive, without CSRF header
        status, data = self.request("POST", "/api/devices/backup-server/wake", token)
        self.assertEqual((status, data["ok"], data["device"]), (200, True, "Backup-Server"))
        self.assertEqual(self.sent[0][0], "a8:b8:e0:07:4b:4e")
        self.assertEqual(self.app.store.list("events")[0]["source"], "API: backup script")
        self.assertEqual(self.request("POST", "/api/devices/%s/wake" % device["id"], token)[0], 200)
        self.assertEqual(self.request("POST", "/api/devices/nas/wake", token)[0], 404)
        self.assertEqual(self.request("GET", "/api/devices", token)[0], 200)
        self.assertEqual(self.request("GET", "/api/status", token)[0], 200)

        # Everything else stays closed for tokens; wrong tokens are rejected
        self.assertEqual(self.request("GET", "/api/info", token)[0], 403)
        self.assertEqual(self.request("DELETE", "/api/devices/%s" % device["id"], token)[0], 403)
        self.assertEqual(self.request("GET", "/api/tokens", token)[0], 403)
        self.assertEqual(self.request("GET", "/api/devices", "wol_falsch")[0], 401)
        self.assertEqual(self.request("GET", "/api/devices")[0], 401)
        self.assertEqual(self.request("POST", "/api/devices/sleeper/sleep", token)[0], 404)

        # wait: until the device answers (simulated), otherwise 504
        answers = iter([False, True])
        with mock.patch("wolserver.server.check_host", lambda host: next(answers)), mock.patch("time.sleep"):
            status, data = self.request("POST", "/api/devices/Backup-Server/wake?wait=60", token)
        self.assertEqual((status, data["online"]), (200, True))
        with mock.patch("wolserver.server.check_host", lambda host: False), mock.patch("time.sleep"), \
                mock.patch("wolserver.server.App.MAX_WAIT", 0):
            status, data = self.request("POST", "/api/devices/Backup-Server/wake?wait=5", token)
        self.assertEqual(status, 200)  # MAX_WAIT 0: no waiting at all
        with mock.patch("wolserver.server.check_host", lambda host: False):
            status, data = self.request("POST", "/api/devices/Backup-Server/wake?wait=1", token)
        self.assertEqual((status, data["ok"], data["online"]), (504, False, False))
        self.assertEqual(self.request("POST", "/api/devices/Backup-Server/wake?wait=x", token)[0], 400)

        # Revoke: the token stops working at once
        self.assertTrue(self.app.tokens.revoke(entry["id"]))
        self.assertEqual(self.request("GET", "/api/devices", token)[0], 401)
        self.assertEqual(self.app.tokens.list(), [])
