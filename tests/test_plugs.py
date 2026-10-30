# WOL-Server - tests/test_plugs.py
# Tests for the plug driver layer: digest authentication, Shelly driver, manager and API.
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

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from tests import test_api
from wolserver import plugs
from wolserver.plugs import fake
from wolserver.plugs.base import PlugAuthError, PlugUnreachable
from wolserver.plugs.http import HttpClient, digest_key, digest_response, parse_challenge
from wolserver.plugs.shelly import ShellyGen2

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "shelly")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


class DigestTest(unittest.TestCase):
    # Example from RFC 7616, section 3.9.1
    CHALLENGE = ('Digest realm="http-auth@example.org", qop="auth, auth-int", algorithm=%s, '
                 'nonce="7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v", '
                 'opaque="FQhe/qaU925kfnzjCev0ciny7QMkPqMAFRtzCUYo5tdS"')
    CNONCE = "f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ"

    def response(self, algorithm):
        challenge = parse_challenge(self.CHALLENGE % algorithm)
        header = digest_response(challenge, "GET", "/dir/index.html", "Mufasa", "Circle of Life", 1, self.CNONCE)
        return parse_challenge(header)["response"]

    def test_rfc7616_vectors(self):
        self.assertEqual(self.response("SHA-256"), "753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1")
        self.assertEqual(self.response("MD5"), "8ca523f5e9506fed4657c9700eebdbec")

    def test_key_stands_in_for_the_password(self):
        challenge = parse_challenge(self.CHALLENGE % "SHA-256")
        key = digest_key(challenge, "Mufasa", "Circle of Life")
        self.assertEqual(key, {"realm": "http-auth@example.org", "algorithm": "SHA-256", "ha1": hashlib.sha256(
            b"Mufasa:http-auth@example.org:Circle of Life").hexdigest()})
        header = digest_response(challenge, "GET", "/dir/index.html", "Mufasa", key, 1, self.CNONCE)
        self.assertEqual(parse_challenge(header)["response"], self.response("SHA-256"))
        # bound to the realm: another plug (or the same one after a reset) does not accept it
        for other in (dict(key, realm="shellyplugmg3-000000"), dict(key, algorithm="MD5")):
            with self.assertRaisesRegex(PlugAuthError, "enter the password again"):
                digest_response(challenge, "GET", "/", "Mufasa", other, 1, self.CNONCE)

    def test_parse_challenge(self):
        c = parse_challenge('Digest qop="auth", realm="shellyplugmg3-a8032a", nonce="60dc59c6", algorithm=SHA-256')
        self.assertEqual(c, {"qop": "auth", "realm": "shellyplugmg3-a8032a", "nonce": "60dc59c6", "algorithm": "SHA-256"})
        self.assertIsNone(parse_challenge('Basic realm="x"'))


class FakeShelly(BaseHTTPRequestHandler):
    """Answers like a Shelly Gen2+ and requires digest authentication (SHA-256) for /rpc."""

    password = "geheim"
    realm = "shellyplugmg3-a8032ad4e5f6"
    calls = []
    requests = 0
    output = True
    config = None  # Shelly.GetConfig, a fresh copy of the fixture per test
    new_password = None  # the password a test is going to set: Shelly.SetAuth only sends its hash
    pending = None  # config switched off that only takes effect with a restart (Matter on a real Shelly)

    def log_message(self, *args):
        pass

    def _json(self, status, payload, headers=None):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self, method):
        if not self.password:
            return True
        auth = parse_challenge(self.headers.get("Authorization"))
        if not auth:
            return False
        challenge = {"realm": self.realm, "nonce": auth["nonce"], "qop": "auth", "algorithm": "SHA-256"}
        expected = digest_response(challenge, method, self.path, "admin", self.password, int(auth["nc"], 16),
                                   auth["cnonce"])
        return parse_challenge(expected)["response"] == auth["response"]

    def _unauthorized(self):
        return self._json(401, {"code": 401, "message": "unauthorized"}, {
            "WWW-Authenticate": 'Digest qop="auth", realm="%s", nonce="%x", algorithm=SHA-256'
                                % (self.realm, int(time.time()))})

    def do_POST(self):
        """JSON-RPC: POST /rpc {"id", "method", "params"}."""
        type(self).requests += 1
        if self.path != "/rpc":
            return self._json(404, {"code": 404, "message": "not found"})
        if not self._authorized("POST"):
            return self._unauthorized()
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method, params = req["method"], req.get("params") or {}
        type(self).calls.append((method, params))
        sections = {"Wifi.SetConfig": "wifi", "BLE.SetConfig": "ble", "Matter.SetConfig": "matter",
                    "Cloud.SetConfig": "cloud"}
        if method == "Matter.SetConfig":  # like the real plug: stays on until the restart
            type(self).pending = params["config"]
            return self._json(200, {"id": req["id"], "result": {"restart_required": True}})
        if method == "Shelly.Reboot":
            if type(self).pending:
                type(self).config["matter"].update(type(self).pending)
                type(self).pending = None
            return self._json(200, {"id": req["id"], "result": None})
        if method in sections:
            section = type(self).config[sections[method]]
            for key, value in params["config"].items():
                if isinstance(value, dict):
                    section[key].update(value)
                else:
                    section[key] = value
            return self._json(200, {"id": req["id"], "result": {"restart_required": False}})
        if method == "Shelly.SetAuth":
            ha1 = hashlib.sha256(("admin:%s:%s" % (self.realm, self.new_password)).encode()).hexdigest()
            if params != {"user": "admin", "realm": self.realm, "ha1": ha1}:
                return self._json(200, {"id": req["id"], "error": {"code": -103, "message": "Invalid argument"}})
            type(self).password = self.new_password
            return self._json(200, {"id": req["id"], "result": None})
        return self._json(200, {"id": req["id"], "error": {"code": 404, "message": "No handler for %s" % method}})

    def do_GET(self):
        type(self).requests += 1
        parts = urlsplit(self.path)
        if parts.path == "/shelly":
            return self._json(200, dict(fixture("shelly.json"), auth_en=bool(self.password)))
        if not self._authorized("GET"):
            return self._unauthorized()
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        type(self).calls.append((parts.path, params))
        if parts.path == "/rpc/Switch.GetStatus":
            if params.get("id") != "0":
                return self._json(500, {"code": -105, "message": "Argument 'id', value %s not found!" % params.get("id")})
            return self._json(200, dict(fixture("switch_status.json"), output=type(self).output))
        if parts.path == "/rpc/Switch.Set":
            type(self).output = params["on"] == "true"
            return self._json(200, {"was_on": True})
        if parts.path == "/rpc/Switch.GetConfig":
            return self._json(200, fixture("switch_config.json"))
        if parts.path == "/rpc/Switch.SetConfig":
            return self._json(200, {"restart_required": False})
        if parts.path == "/rpc/Shelly.GetConfig":
            return self._json(200, self.config)
        if parts.path == "/rpc/Shelly.GetDeviceInfo":
            return self._json(200, fixture("shelly.json"))
        if parts.path == "/rpc/Sys.GetStatus":
            return self._json(200, {"restart_required": bool(self.pending), "uptime": 4235})
        return self._json(404, {"code": 404, "message": "No handler for %s" % parts.path})


class ShellyTest(unittest.TestCase):
    def setUp(self):
        FakeShelly.calls, FakeShelly.requests, FakeShelly.output = [], 0, True
        FakeShelly.password, FakeShelly.config, FakeShelly.new_password = "geheim", copy.deepcopy(fixture("config.json")), None
        FakeShelly.pending = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeShelly)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.host = "127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def driver(self, password="geheim", channel=0):
        return ShellyGen2(self.host, channel, password, HttpClient(timeout=2))

    def test_probe_without_password(self):
        info = ShellyGen2.probe(self.host)
        self.assertEqual((info.driver, info.model, info.mac, info.firmware, info.auth),
                         ("shelly-gen2", "S3PL-30110EU", "a8:03:2a:d4:e5:f6", "1.7.1", True))
        self.assertEqual(plugs.probe_all(self.host).driver, "shelly-gen2")

    def test_status_is_normalized(self):
        st = self.driver().status()
        self.assertEqual((st.on, st.power_w, st.voltage_v, st.current_a), (True, 92.4, 231.2, 0.412))
        self.assertEqual((st.energy_wh, st.temperature_c, st.switch_count, st.errors), (15342.117, 34.6, 312, []))

    def test_switch_cycle_and_power_on_state(self):
        d = self.driver()
        d.switch(False)
        self.assertFalse(d.status().on)
        d.cycle(10)
        self.assertIn(("/rpc/Switch.Set", {"id": "0", "on": "false", "toggle_after": "10"}), FakeShelly.calls)
        self.assertEqual(d.power_on_state(), "restore")
        d.set_power_on_state("on")
        self.assertEqual(FakeShelly.calls[-1], ("/rpc/Switch.SetConfig", {"id": "0", "config": '{"initial_state": "on"}'}))

    def test_security_and_secure(self):
        d = self.driver()
        self.assertEqual(d.security(), {"password": True, "open": ["access_point", "bluetooth", "matter"],
                                        "restart_required": False})
        FakeShelly.new_password = "neu-und-lang"
        res = d.secure("neu-und-lang", ["access_point", "bluetooth", "matter"])
        self.assertEqual(res, {"restart_required": True})
        self.assertEqual(d.password, "neu-und-lang")
        self.assertEqual(FakeShelly.password, "neu-und-lang")
        self.assertIn(("Wifi.SetConfig", {"config": {"ap": {"enable": False}}}), FakeShelly.calls)
        self.assertEqual(d.security(), {"password": True, "open": ["matter"], "restart_required": True})
        d.restart()  # Matter goes off with the restart
        self.assertEqual(d.security(), {"password": True, "open": [], "restart_required": False})
        with self.assertRaises(PlugAuthError):
            self.driver("geheim").status()  # the old password no longer works

    def test_secure_a_plug_without_password(self):
        FakeShelly.password = ""
        d = self.driver("")
        self.assertFalse(d.security()["password"])
        FakeShelly.new_password = "zufall-123"
        d.secure("zufall-123", [])
        self.assertTrue(self.driver("zufall-123").security()["password"])
        with self.assertRaisesRegex(plugs.PlugError, "Unknown setting"):
            d.secure(None, ["telnet"])

    def test_key_in_place_of_the_password(self):
        d = self.driver()
        self.assertIsNone(d.make_key())  # no challenge yet
        d.status()
        key = d.make_key()
        self.assertEqual(key, {"realm": FakeShelly.realm, "algorithm": "SHA-256", "ha1": hashlib.sha256(
            ("admin:%s:geheim" % FakeShelly.realm).encode()).hexdigest()})
        self.assertTrue(ShellyGen2(self.host, 0, "", HttpClient(timeout=2), key=key).status().on)
        with self.assertRaises(PlugAuthError):
            ShellyGen2(self.host, 0, "", HttpClient(timeout=2), key=dict(key, ha1="0" * 64)).status()
        self.assertIsNone(self.driver("").make_key())

    def manager_with_plug(self, **fields):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        mgr = plugs.PlugManager(tmp, http=HttpClient(timeout=2))
        self.addCleanup(mgr.shutdown)
        plug = dict({"id": "p1", "name": "office", "host": self.host, "driver": "shelly-gen2", "channel": 0,
                     "password": "", "consumer": None, "standby_w": 5.0, "locked": False}, **fields)
        mgr._plugs.append(plug)
        mgr._save()
        return mgr, os.path.join(tmp, "plugs.json")

    def test_manager_replaces_a_stored_password_by_the_key(self):
        mgr, path = self.manager_with_plug(password="geheim")  # stored before keys existed
        self.assertIsNone(mgr.live("p1", max_age=0)["error"])
        stored = mgr.get("p1")
        self.assertEqual((stored["password"], stored["key"]["realm"]), ("", FakeShelly.realm))
        with open(path, encoding="utf-8") as f:
            self.assertNotIn("geheim", f.read())
        self.assertTrue(mgr.get_public("p1")["has_password"])
        self.assertNotIn("key", mgr.get_public("p1"))
        reloaded = plugs.PlugManager(os.path.dirname(path), http=HttpClient(timeout=2))
        self.addCleanup(reloaded.shutdown)
        self.assertIsNone(reloaded.live("p1", max_age=0)["error"])  # the key alone is enough

    def test_manager_keeps_only_the_key_of_a_new_password(self):
        mgr, path = self.manager_with_plug(password="geheim")
        mgr.live("p1", max_age=0)
        FakeShelly.new_password = "neu-und-lang"
        mgr.secure("p1", "neu-und-lang", [])
        stored = mgr.get("p1")
        self.assertEqual(stored["password"], "")
        self.assertEqual(stored["key"]["ha1"], hashlib.sha256(("admin:%s:neu-und-lang" % FakeShelly.realm)
                                                              .encode()).hexdigest())
        with open(path, encoding="utf-8") as f:
            self.assertNotIn("neu-und-lang", f.read())
        self.assertIsNone(mgr.live("p1", max_age=0)["error"])

    def test_factory_power_on_state(self):
        d = self.driver()
        d.http.get_json = lambda url, *a: dict(fixture("switch_config.json"), initial_state="match_input")
        self.assertEqual(d.power_on_state(), "other")

    def test_one_round_trip_once_the_challenge_is_known(self):
        client = HttpClient(timeout=2)
        d = ShellyGen2(self.host, 0, "geheim", client)
        d.status()
        d.status()
        self.assertEqual(len(FakeShelly.calls), 2)
        self.assertEqual(FakeShelly.requests, 3)  # one 401 for the first challenge, then direct

    def test_errors(self):
        with self.assertRaises(PlugAuthError):
            self.driver("falsch").status()
        with self.assertRaises(PlugAuthError):
            self.driver("").status()
        with self.assertRaisesRegex(plugs.PlugError, "not found"):
            self.driver(channel=3).status()
        with self.assertRaises(PlugUnreachable):
            ShellyGen2("127.0.0.1:1", http=HttpClient(timeout=1)).status()


class ManagerTest(unittest.TestCase):
    def setUp(self):
        fake.register()
        self.tmp = tempfile.mkdtemp()
        fake.FakePlug.devices = {"plug-1": fake.FakeDevice(mac="02:00:00:00:00:01", password="pw", load_w=80)}
        self.events = []
        self.mgr = plugs.PlugManager(self.tmp, on_event=lambda **e: self.events.append(e))

    def tearDown(self):
        self.mgr.shutdown()
        shutil.rmtree(self.tmp)
        plugs.DRIVERS.pop("fake", None)

    def test_add_list_and_password_stays_on_the_server(self):
        with self.assertRaisesRegex(plugs.PlugError, "Password required"):
            self.mgr.add({"host": "plug-1"})
        plug = self.mgr.add({"host": "plug-1", "password": "pw", "consumer_label": "Office PC"})
        self.assertEqual((plug["driver"], plug["name"], plug["mac"], plug["has_password"]),
                         ("fake", "plug-1", "02:00:00:00:00:01", True))
        self.assertNotIn("password", plug)
        listed = self.mgr.list()[0]
        self.assertNotIn("password", listed)
        self.assertEqual((listed["live"]["status"]["power_w"], listed["consumer"]),
                         (80.0, {"kind": "label", "label": "Office PC"}))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.tmp, "plugs.json")).st_mode), 0o600)
        with self.assertRaisesRegex(plugs.PlugError, "already set up"):
            self.mgr.add({"host": "plug-1", "password": "pw"})
        # Changing without a password keeps the stored one
        self.mgr.update(plug["id"], {"name": "Office", "host": "plug-1"})
        self.assertEqual(self.mgr.get(plug["id"])["password"], "pw")
        self.assertEqual(plugs.PlugManager(self.tmp).list()[0]["name"], "Office")

    def test_unknown_host(self):
        with self.assertRaises(PlugUnreachable):
            self.mgr.add({"host": "nowhere"})
        with self.assertRaisesRegex(plugs.PlugError, "Host must be"):
            self.mgr.probe("bad host!")

    def test_off_needs_confirmation_while_power_is_drawn(self):
        plug = self.mgr.add({"host": "plug-1", "password": "pw", "standby_w": 3})
        with self.assertRaises(plugs.ConfirmRequired) as ctx:
            self.mgr.switch(plug["id"], "off")
        self.assertEqual(ctx.exception.power_w, 80.0)
        self.assertTrue(fake.FakePlug.devices["plug-1"].on)
        self.mgr.switch(plug["id"], "off", confirm=True)
        self.assertFalse(fake.FakePlug.devices["plug-1"].on)
        # At standby level no confirmation is needed
        dev = fake.FakePlug.devices["plug-1"]
        dev.load_w = 0
        self.mgr.switch(plug["id"], "on")
        self.mgr.switch(plug["id"], "off")
        self.assertEqual([(e["action"], e["ok"]) for e in self.events],
                         [("power_off", True), ("power_on", True), ("power_off", True)])

    def test_cycle_switches_back_on(self):
        self.mgr.CYCLE_SECONDS = 0.2
        dev = fake.FakePlug.devices["plug-1"]
        dev.load_w = 0
        plug = self.mgr.add({"host": "plug-1", "password": "pw"})
        self.mgr.switch(plug["id"], "cycle")
        self.assertFalse(dev.on)
        time.sleep(0.5)
        self.assertTrue(dev.on)
        self.assertEqual(dev.switch_count, 1)

    def test_power_on_state(self):
        plug = self.mgr.add({"host": "plug-1", "password": "pw"})
        self.mgr.set_power_on_state(plug["id"], "on")
        self.assertEqual(self.mgr.power_on_state(plug["id"]), "on")
        with self.assertRaises(plugs.PlugError):
            self.mgr.set_power_on_state(plug["id"], "sometimes")

    def test_secure(self):
        dev = fake.FakePlug.devices["plug-1"]
        plug = self.mgr.add({"host": "plug-1", "password": "pw"})
        self.assertEqual((plug["security"]["password"], plug["security"]["open"]), (True, ["access_point"]))
        res = self.mgr.secure(plug["id"], None, ["access_point"])
        self.assertEqual((res["password"], res["open"], dev.password), (True, [], "pw"))  # known password stays
        self.assertEqual(self.events[-1]["message"], "Secured: access point off")
        self.assertEqual(self.mgr.get_public(plug["id"])["security"]["open"], [])
        with self.assertRaisesRegex(plugs.PlugError, "8 to 128"):
            self.mgr.secure(plug["id"], "kurz", [])
        with self.assertRaisesRegex(plugs.PlugError, "Unknown setting"):
            self.mgr.secure(plug["id"], None, ["telnet"])

    def test_restart_finishes_matter_but_never_cuts_power(self):
        dev = fake.FakePlug.devices["plug-1"]
        dev.exposures = {"matter"}
        plug = self.mgr.add({"host": "plug-1", "password": "pw"})
        res = self.mgr.secure(plug["id"], None, ["matter"])
        self.assertEqual((res["open"], res["restart_required"]), (["matter"], True))
        self.assertTrue(self.mgr.get_public(plug["id"])["security"]["restart_required"])
        dev.power_on_state = "off"  # a restart would leave the PC without power
        with self.assertRaisesRegex(plugs.PlugError, "would stay off"):
            self.mgr.restart(plug["id"])
        dev.power_on_state = "restore"
        self.assertTrue(self.mgr.restart(plug["id"])["ok"])
        self.assertEqual(self.events[-1]["message"], "Plug restarted")
        self.assertEqual(self.mgr.security(plug["id"]), {"password": True, "open": [], "restart_required": False})
        self.assertTrue(dev.on)
        locked = self.mgr.update(plug["id"], {"name": "router", "host": "plug-1", "locked": True})
        with self.assertRaisesRegex(plugs.PlugError, "never restarted"):
            self.mgr.restart(locked["id"])

    def test_secure_chooses_a_password(self):
        fake.FakePlug.devices["plug-2"] = dev = fake.FakeDevice(mac="02:00:00:00:00:02")
        plug = self.mgr.add({"host": "plug-2"})
        self.assertFalse(plug["security"]["password"])
        self.mgr.secure(plug["id"], None, [])
        self.assertGreaterEqual(len(dev.password), 16)
        self.assertEqual(self.mgr.get(plug["id"])["password"], dev.password)
        self.assertEqual(self.events[-1]["message"], "Secured: password set")
        self.mgr.live(plug["id"], max_age=0)  # the new password works for the poller
        self.assertIsNone(self.mgr.live(plug["id"], max_age=0)["error"])


class PlugApiTest(unittest.TestCase):
    call = test_api.ApiTest.call

    def setUp(self):
        fake.register()
        fake.FakePlug.devices = {"plug-1": fake.FakeDevice(load_w=50)}
        test_api.ApiTest.setUp(self)

    def tearDown(self):
        test_api.ApiTest.tearDown(self)
        plugs.DRIVERS.pop("fake", None)

    def test_plug_api(self):
        status, res = self.call("POST", "/api/plugs/probe", {"host": "plug-1"})
        self.assertEqual((status, res["info"]["driver"]), (200, "fake"))
        status, res = self.call("POST", "/api/plugs/probe", {"host": "nowhere"})
        self.assertEqual(status, 502)
        status, plug = self.call("POST", "/api/plugs", {"host": "plug-1", "name": "Desk"})
        self.assertEqual(status, 201)
        status, res = self.call("GET", "/api/plugs")
        self.assertEqual(res["plugs"][0]["live"]["status"]["power_w"], 50.0)
        self.assertIn("fake", [d["name"] for d in res["drivers"]])
        status, res = self.call("POST", "/api/plugs/%s/off" % plug["id"], {})
        self.assertEqual((status, res["confirm"], res["power_w"]), (409, True, 50.0))
        status, res = self.call("POST", "/api/plugs/%s/off" % plug["id"], {"confirm": True})
        self.assertEqual((status, res["ok"]), (200, True))
        status, res = self.call("GET", "/api/events")
        self.assertEqual(res["events"][0]["action"], "power_off")
        status, res = self.call("PUT", "/api/plugs/%s/power-on-state" % plug["id"], {"state": "off"})
        self.assertEqual(res["state"], "off")
        status, res = self.call("GET", "/api/plugs/%s/security" % plug["id"])
        self.assertEqual((status, res["open"]), (200, ["access_point"]))
        status, res = self.call("POST", "/api/plugs/%s/security" % plug["id"], {"disable": ["access_point"]})
        self.assertEqual((status, res["password"], res["open"]), (200, True, []))
        status, res = self.call("POST", "/api/plugs/%s/security" % plug["id"], {"password": "kurz"})
        self.assertEqual(status, 400)
        status, res = self.call("POST", "/api/plugs/%s/restart" % plug["id"], {})
        self.assertEqual((status, res["ok"]), (200, True))
        status, res = self.call("GET", "/api/net/config")  # network service not running in the test
        self.assertEqual(status, 503)
        self.assertEqual(self.call("DELETE", "/api/plugs/%s" % plug["id"])[0], 200)
        self.assertEqual(self.call("DELETE", "/api/plugs/%s" % plug["id"])[0], 404)

    def test_plug_schedules(self):
        status, plug = self.call("POST", "/api/plugs", {"host": "plug-1", "name": "Printer"})
        base = {"plug_id": plug["id"], "time": "23:00", "days": [0, 1, 2, 3, 4, 5, 6]}
        status, res = self.call("POST", "/api/schedules", dict(base, action="wake"))
        self.assertEqual(status, 400)
        status, res = self.call("POST", "/api/schedules", dict(base, plug_id="nope", action="power_off"))
        self.assertEqual((status, res["error"]), (400, "Unknown plug"))
        status, sched = self.call("POST", "/api/schedules", dict(base, action="power_off"))
        self.assertEqual((status, sched["plug_id"], sched["force"]), (201, plug["id"], False))
        self.assertNotIn("device_id", sched)
        # Scheduled "off" while 50 W are drawn: skipped and logged, nobody can confirm at night
        ok, message = self.app.run_plug_action(plug["id"], "power_off", False, "schedule")
        self.assertEqual((ok, message), (False, "Skipped: Printer is drawing 50 W"))
        self.assertTrue(fake.FakePlug.devices["plug-1"].on)
        events = self.call("GET", "/api/events")[1]["events"]
        self.assertEqual((events[0]["action"], events[0]["source"], events[0]["ok"]), ("power_off", "schedule", False))
        # With force the schedule switches anyway
        ok, message = self.app.run_plug_action(plug["id"], "power_off", True, "schedule")
        self.assertTrue(ok)
        self.assertFalse(fake.FakePlug.devices["plug-1"].on)
        # The scheduler hands plug schedules to run_plug
        calls = []
        sched_obj = self.app.scheduler
        sched_obj.run_plug = lambda *args: calls.append(args)
        ts = time.mktime(time.strptime("2026-10-26 23:00", "%Y-%m-%d %H:%M"))
        sched_obj.tick(int(ts) - 60, int(ts))
        time.sleep(0.2)
        self.assertEqual(calls, [(plug["id"], "power_off", False, "schedule")])
        # Removing the plug removes its schedules
        self.call("DELETE", "/api/plugs/%s" % plug["id"])
        self.assertEqual(self.call("GET", "/api/schedules")[1]["schedules"], [])


if __name__ == "__main__":
    unittest.main()
