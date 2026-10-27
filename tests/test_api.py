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
        self.assertEqual(events["events"][0]["source"], "manuell")
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
        # Anfragen, die an der Hotspot-Adresse ankommen: immer erlaubt, fremde Hosts umgeleitet
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
        # VPN verbindet sich: Ein unbekannter Absender löst nach RETRY eine neue Abfrage aus
        nets.append({"network": "10.1.1.0/24", "interface": "tun0", "kind": "vpn"})
        self.assertFalse(acl.allows(ip("10.1.1.7")))  # gerade erst nachgesehen
        now[0] += AccessList.RETRY
        self.assertTrue(acl.allows(ip("10.1.1.7")))
        # Bekannte Absender lösen keine Abfrage aus, solange REFRESH nicht abgelaufen ist
        before = len(calls)
        acl.allows(ip("192.168.1.12"))
        self.assertEqual(len(calls), before)
        sources = {e["network"]: e["source"] for e in acl.describe()}
        self.assertEqual(sources["10.1.1.0/24"], "tun0")
        self.assertEqual(sources["172.20.0.0/16"], "config")
        self.assertEqual(sources["127.0.0.0/8"], "auto")
        # Ohne "auto": nur die festen Einträge, keine Abfrage der Schnittstellen
        fixed = AccessList(["192.168.1.0/24"], source=lambda: self.fail("keine Abfrage erwartet"))
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
