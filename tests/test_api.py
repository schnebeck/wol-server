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
        self.assertEqual(session, {"auth_required": True, "authenticated": False})
        self.assertEqual(self.call("POST", "/api/login", {"password": "falsch"})[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"password": "geheim123"})[0], 200)
        self.assertEqual(self.call("GET", "/api/devices")[0], 200)
        self.call("POST", "/api/logout")
        self.assertEqual(self.call("GET", "/api/devices")[0], 401)

    def test_client_network_allowlist(self):
        self.app.networks = []
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/session")
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
