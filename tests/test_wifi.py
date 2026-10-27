import os
import re
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from wolserver import wifi
from wolserver.config import DEFAULTS
from wolserver.server import captive_redirect

CLIENT_IP = "192.168.178.50"


class FakeNM:
    """Simulates nmcli/iw as far as WifiManager uses them."""

    def __init__(self, profiles=(), connect_ok=True):
        self.profiles = {}  # uuid -> {"name", "ssid"}
        for i, ssid in enumerate(profiles):
            self.profiles["00000000-0000-0000-0000-%012d" % i] = {"name": ssid, "ssid": ssid}
        self.address = None
        self.connected = None
        self.connect_ok = connect_ok
        self.stations = 0
        self.calls = []
        self.hotspot_args = None

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if argv[0] == "iw":
            return 0, "Station aa:bb:cc:dd:ee:ff (on wlan0)\n" * self.stations, ""
        a = argv[1:]
        if a[:2] == ["--wait", "0"] and "device" in a:
            return 10, "", "Error: no suitable connection"
        if a == ["-t", "-f", "NAME,UUID,TYPE", "connection", "show"]:
            lines = ["%s:%s:802-11-wireless" % (p["name"], u) for u, p in self.profiles.items()]
            lines.append("Wired:11111111-1111-1111-1111-111111111111:802-3-ethernet")
            return 0, "\n".join(lines) + "\n", ""
        if "802-11-wireless.ssid,802-11-wireless.mode" in a:
            p = self.profiles[a[-1]]
            return 0, "802-11-wireless.ssid:%s\n802-11-wireless.mode:infrastructure\n" % p["ssid"], ""
        if "GENERAL.CONNECTION" in a:
            return 0, "GENERAL.CONNECTION:%s\n" % (self.connected or ""), ""
        if "wifi" in a and "list" in a:
            return 0, "Heim:80:WPA2\nHeim:60:WPA2\nCafé\\:Gast:40:\n:30:WPA2\n", ""
        if a[:3] == ["radio", "wifi", "on"]:
            return 0, "", ""
        if a[:2] == ["connection", "delete"]:
            if a[2] == wifi.HOTSPOT_PROFILE:
                return 10, "", "Error: unknown connection"
            self.profiles.pop(a[3], None)
            return 0, "", ""
        if a[:2] == ["connection", "add"]:
            name = a[a.index("con-name") + 1]
            if name == wifi.HOTSPOT_PROFILE:
                self.hotspot_args = a
                return 0, "", ""
            uuid = "22222222-2222-2222-2222-%012d" % len(self.profiles)
            self.profiles[uuid] = {"name": name, "ssid": a[a.index("ssid") + 1]}
            return 0, "Connection '%s' (%s) successfully added.\n" % (name, uuid), ""
        if a[:2] == ["connection", "modify"]:
            return 0, "", ""
        if "up" in a:
            if a[-1] == wifi.HOTSPOT_PROFILE:
                self.address = wifi.HOTSPOT_ADDRESS
                return 0, "", ""
            if not self.connect_ok:
                return 4, "", "Error: Connection activation failed: Secrets were required."
            self.address, self.connected = CLIENT_IP, self.profiles[a[-1]]["name"]
            return 0, "", ""
        if a[:3] == ["connection", "down", wifi.HOTSPOT_PROFILE]:
            self.address = None
            return 0, "", ""
        raise AssertionError("unerwarteter Aufruf: %r" % argv)

    def ran(self, *words):
        return any(all(w in call for w in words) for call in self.calls)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_manager(fake, state_dir, **cfg):
    clock = Clock()
    config = dict(DEFAULTS, hotspot_ssid="WOL-Test", **cfg)
    return wifi.WifiManager(config, state_dir, runner=fake, probe=lambda: fake.address, clock=clock), clock


class ParseTest(unittest.TestCase):
    def test_split_terse(self):
        self.assertEqual(wifi.split_terse(r"a\:b:c\\d:"), ["a:b", "c\\d", ""])

    def test_parse_scan(self):
        nets = wifi.parse_scan("Heim:80:WPA2\nHeim:60:WPA2\nCafé\\:Gast:40:\n:30:WPA2\nX:abc:--\n")
        self.assertEqual([n["ssid"] for n in nets], ["Heim", "Café:Gast", "X"])
        self.assertEqual(nets[0]["signal"], 80)
        self.assertEqual(nets[1]["security"], "")
        self.assertEqual(nets[2]["security"], "")

    def test_parse_details(self):
        d = wifi.parse_details("802-11-wireless.ssid:Mein\\:Netz\nGENERAL.STATE:100 (connected)\n")
        self.assertEqual(d["802-11-wireless.ssid"], "Mein:Netz")
        self.assertEqual(d["GENERAL.STATE"], "100 (connected)")

    def test_validation(self):
        self.assertEqual(wifi.validate_psk(""), "")
        self.assertEqual(wifi.validate_psk("geheim123"), "geheim123")
        self.assertEqual(wifi.validate_psk("a" * 64), "a" * 64)
        for bad in ("kurz", "x" * 64, "pässwort1", 12345678):
            with self.assertRaises(wifi.WifiError):
                wifi.validate_psk(bad)
        self.assertEqual(wifi.validate_ssid("Heim"), "Heim")
        for bad in ("", "  ", "x" * 33, "a\nb", None):
            with self.assertRaises(wifi.WifiError):
                wifi.validate_ssid(bad)

    def test_validate_ipv4(self):
        self.assertEqual(wifi.validate_ipv4(None), [])
        self.assertEqual(wifi.validate_ipv4({"address": " "}), [])
        args = wifi.validate_ipv4({"address": "192.168.1.8", "gateway": "192.168.1.1"})
        self.assertEqual(args, ["ipv4.method", "manual", "ipv4.addresses", "192.168.1.8/24", "ipv4.gateway",
                                "192.168.1.1", "ipv4.dns", "192.168.1.1", "ipv4.ignore-auto-dns", "yes"])
        args = wifi.validate_ipv4({"address": "10.0.0.5/16", "gateway": "10.0.255.254", "dns": "1.1.1.1, 9.9.9.9"})
        self.assertEqual((args[3], args[7]), ("10.0.0.5/16", "1.1.1.1 9.9.9.9"))
        for bad in ({"address": "192.168.1.300", "gateway": "192.168.1.1"},
                    {"address": "192.168.1.0/24", "gateway": "192.168.1.1"},     # network address
                    {"address": "192.168.1.8/24"},                               # gateway missing
                    {"address": "192.168.1.8/24", "gateway": "192.168.2.1"},     # other network
                    {"address": "192.168.1.8/24", "gateway": "192.168.1.8"},
                    {"address": "192.168.1.8/24", "gateway": "192.168.1.1", "dns": "router"}):
            with self.assertRaises(wifi.WifiError):
                wifi.validate_ipv4(bad)

    def test_captive_redirect(self):
        self.assertIsNone(captive_redirect("10.42.0.1", 80))
        self.assertIsNone(captive_redirect("10.42.0.1:8080", 8080))
        self.assertEqual(captive_redirect("connectivitycheck.gstatic.com", 80), "http://10.42.0.1/")
        self.assertEqual(captive_redirect("captive.apple.com", 8080), "http://10.42.0.1:8080/")
        self.assertEqual(captive_redirect(None, 80), "http://10.42.0.1/")


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        patcher = mock.patch.object(wifi.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def job(self, mgr, name, **args):
        job = wifi.Job(name, args)
        mgr.run_job(job)
        return job.result

    def test_no_saved_network_starts_hotspot_after_boot_grace(self):
        fake = FakeNM()
        mgr, clock = make_manager(fake, self.tmp)
        mgr.tick()
        self.assertIsNone(fake.address)
        clock.now += wifi.BOOT_GRACE
        mgr.tick()
        self.assertEqual(fake.address, wifi.HOTSPOT_ADDRESS)
        snap = mgr.snapshot()
        self.assertEqual(snap["mode"], "hotspot")
        self.assertFalse(snap["hotspot"]["secured"])
        self.assertEqual([n["ssid"] for n in snap["scan"]], ["Heim", "Café:Gast"])
        self.assertNotIn("wifi-sec.psk", fake.hotspot_args)
        self.assertIn("shared", fake.hotspot_args)

    def test_known_network_lost_then_hotspot_after_delay(self):
        fake = FakeNM(["Heim"])
        fake.address, fake.connected = CLIENT_IP, "Heim"
        mgr, clock = make_manager(fake, self.tmp, hotspot_delay_minutes=5)
        mgr.tick()
        self.assertEqual(mgr.snapshot()["ssid"], "Heim")
        fake.address = None
        clock.now += 240
        mgr.tick()
        self.assertIsNone(fake.address)
        clock.now += 61
        mgr.tick()
        self.assertEqual(fake.address, wifi.HOTSPOT_ADDRESS)

    def test_hotspot_disabled(self):
        fake = FakeNM()
        mgr, clock = make_manager(fake, self.tmp, hotspot_enabled=False)
        clock.now += 3600
        mgr.tick()
        self.assertIsNone(fake.address)

    def test_hotspot_retries_known_networks_unless_in_use(self):
        fake = FakeNM(["Heim"])
        mgr, clock = make_manager(fake, self.tmp, hotspot_delay_minutes=5, hotspot_retry_minutes=5)
        clock.now += 301
        mgr.tick()
        self.assertEqual(fake.address, wifi.HOTSPOT_ADDRESS)
        # phone is connected to the hotspot: do not switch off
        fake.stations = 1
        clock.now += 301
        mgr.tick()
        self.assertEqual(fake.address, wifi.HOTSPOT_ADDRESS)
        # nobody connected: hotspot off, NetworkManager may look for known Wi-Fi networks
        fake.stations = 0
        mgr.tick()
        self.assertIsNone(fake.address)
        self.assertTrue(fake.ran("device", "connect"))
        clock.now += wifi.ATTEMPT_SECONDS - 1
        mgr.tick()
        self.assertIsNone(fake.address)
        clock.now += 2
        mgr.tick()
        self.assertEqual(fake.address, wifi.HOTSPOT_ADDRESS)

    def test_recent_gui_access_keeps_hotspot(self):
        fake = FakeNM(["Heim"])
        mgr, clock = make_manager(fake, self.tmp)
        clock.now += 301
        mgr.tick()
        clock.now += 301
        mgr.last_request = clock.now - 10
        mgr.tick()
        self.assertEqual(fake.address, wifi.HOTSPOT_ADDRESS)

    def test_connect_from_hotspot(self):
        fake = FakeNM()
        mgr, clock = make_manager(fake, self.tmp)
        clock.now += wifi.BOOT_GRACE
        mgr.tick()
        res = self.job(mgr, "connect", ssid="Heim", password="geheim123")
        self.assertEqual(res, {"ok": True, "switching": True, "message": "Connecting to \"Heim\" ..."})
        self.assertTrue(fake.ran("connection", "down", wifi.HOTSPOT_PROFILE))
        self.assertTrue(fake.ran("connection", "add", "802-11-wireless.cloned-mac-address", "permanent"))
        self.assertEqual(fake.address, CLIENT_IP)
        mgr.tick()
        snap = mgr.snapshot()
        self.assertEqual((snap["mode"], snap["ssid"], snap["saved"]), ("client", "Heim", ["Heim"]))
        self.assertIsNone(snap["last_error"])

    def test_connect_from_hotspot_with_static_ip(self):
        fake = FakeNM()
        mgr, clock = make_manager(fake, self.tmp)
        clock.now += wifi.BOOT_GRACE
        mgr.tick()
        res = self.job(mgr, "connect", ssid="Heim", password="geheim123",
                       ipv4={"address": "192.168.1.8/24", "gateway": "192.168.1.1"})
        self.assertTrue(res["switching"])
        self.assertTrue(fake.ran("connection", "add", "ipv4.method", "manual", "192.168.1.8/24", "192.168.1.1"))
        # invalid input: create nothing, hotspot stays
        fake2 = FakeNM()
        mgr2, clock2 = make_manager(fake2, tempfile.mkdtemp(dir=self.tmp))
        clock2.now += wifi.BOOT_GRACE
        mgr2.tick()
        res = self.job(mgr2, "connect", ssid="Heim", password="geheim123", ipv4={"address": "192.168.1.8/24"})
        self.assertFalse(res["ok"])
        self.assertIn("gateway", res["error"])
        self.assertFalse(fake2.ran("connection", "add", "Heim"))
        self.assertEqual(fake2.address, wifi.HOTSPOT_ADDRESS)

    def test_connect_failure_restores_hotspot(self):
        fake = FakeNM(connect_ok=False)
        mgr, clock = make_manager(fake, self.tmp)
        clock.now += wifi.BOOT_GRACE
        mgr.tick()
        self.job(mgr, "connect", ssid="Heim", password="falsch123")
        self.assertEqual(fake.address, wifi.HOTSPOT_ADDRESS)
        self.assertEqual(fake.profiles, {})
        mgr.tick()
        snap = mgr.snapshot()
        self.assertIn("failed", snap["last_error"]["message"])
        self.assertEqual(snap["saved"], [])

    def test_connect_while_online_only_saves(self):
        fake = FakeNM(["Heim"])
        fake.address, fake.connected = CLIENT_IP, "Heim"
        mgr, _ = make_manager(fake, self.tmp)
        mgr.tick()
        res = self.job(mgr, "connect", ssid="Büro", password="")
        self.assertFalse(res["switching"])
        self.assertFalse(fake.ran("up"))
        self.assertEqual(mgr.snapshot()["saved"], ["Büro", "Heim"])
        # the password of an existing profile is changed, not created anew
        self.job(mgr, "connect", ssid="Heim", password="neuespasswort")
        self.assertTrue(fake.ran("connection", "modify", "wifi-sec.psk", "neuespasswort"))
        self.assertTrue(fake.ran("connection", "modify", "802-11-wireless.cloned-mac-address", "permanent"))
        self.assertEqual(len(fake.profiles), 2)
        res = self.job(mgr, "connect", ssid="x", password="kurz")
        self.assertFalse(res["ok"])

    def test_forget(self):
        fake = FakeNM(["Heim", "Alt"])
        fake.address, fake.connected = CLIENT_IP, "Heim"
        mgr, _ = make_manager(fake, self.tmp)
        mgr.tick()
        self.assertFalse(self.job(mgr, "forget", ssid="Heim")["ok"])
        self.assertTrue(self.job(mgr, "forget", ssid="Alt")["ok"])
        self.assertFalse(self.job(mgr, "forget", ssid="Alt")["ok"])
        self.assertEqual(mgr.snapshot()["saved"], ["Heim"])

    def test_hotspot_password(self):
        fake = FakeNM()
        mgr, clock = make_manager(fake, self.tmp)
        self.assertFalse(self.job(mgr, "hotspot_password", password="kurz")["ok"])
        self.assertTrue(self.job(mgr, "hotspot_password", password="hotspot123")["ok"])
        self.assertEqual(os.stat(mgr.state_file).st_mode & 0o777, 0o600)
        clock.now += wifi.BOOT_GRACE
        mgr.tick()
        self.assertIn("hotspot123", fake.hotspot_args)
        self.assertTrue(mgr.snapshot()["hotspot"]["secured"])

    def test_scan_uses_cache_in_hotspot_mode(self):
        fake = FakeNM()
        mgr, clock = make_manager(fake, self.tmp)
        res = self.job(mgr, "scan")
        self.assertFalse(res["cached"])
        clock.now += wifi.BOOT_GRACE
        mgr.tick()
        scans = sum(1 for c in fake.calls if "list" in c)
        res = self.job(mgr, "scan")
        self.assertTrue(res["cached"])
        self.assertEqual(sum(1 for c in fake.calls if "list" in c), scans)


class ControlSocketTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake = FakeNM(["Heim"])
        self.fake.address, self.fake.connected = CLIENT_IP, "Heim"
        self.mgr, _ = make_manager(self.fake, self.tmp)
        self.path = os.path.join(self.tmp, "run", "control.sock")
        self.stop = threading.Event()

    def tearDown(self):
        self.stop.set()
        shutil.rmtree(self.tmp)

    def start(self, uids):
        server = wifi.ControlServer(self.path, self.mgr, uids)
        server.bind()
        threading.Thread(target=self.mgr.run, args=(self.stop,), daemon=True).start()
        threading.Thread(target=server.serve, args=(self.stop,), daemon=True).start()

    def test_roundtrip(self):
        self.start({os.getuid()})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o660)
        res = wifi.request(self.path, {"cmd": "forget", "ssid": "Gibtsnicht"}, 10)
        self.assertFalse(res["ok"])
        res = wifi.request(self.path, {"cmd": "status"}, 10)
        self.assertEqual(res["status"]["ssid"], "Heim")
        self.assertFalse(wifi.request(self.path, {"cmd": "rm -rf"}, 10)["ok"])

    def test_foreign_uid_rejected(self):
        self.start({os.getuid() + 12345})
        with self.assertRaises((ValueError, OSError)):
            wifi.request(self.path, {"cmd": "status"}, 10)

    def test_missing_socket(self):
        with self.assertRaises(OSError):
            wifi.request(os.path.join(self.tmp, "nope.sock"), {"cmd": "status"}, 2)

    def test_api_proxy(self):
        from tests.test_api import ApiTest

        self.start({os.getuid()})
        api = ApiTest("test_login_flow")
        api.setUp()
        try:
            api.app.config["wifi_socket"] = self.path
            status, data = api.call("GET", "/api/wifi")
            self.assertEqual((status, data["mode"], data["ssid"]), (200, "client", "Heim"))
            status, data = api.call("POST", "/api/wifi/forget", {"ssid": "Heim"})
            self.assertEqual(status, 400)
            self.assertIn("connected", data["error"])
            api.app.config["wifi_socket"] = os.path.join(self.tmp, "nope.sock")
            status, data = api.call("GET", "/api/wifi")
            self.assertEqual(status, 503)
        finally:
            api.tearDown()


if __name__ == "__main__":
    unittest.main()
