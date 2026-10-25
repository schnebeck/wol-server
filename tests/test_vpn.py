import json
import os
import shutil
import tempfile
import threading
import unittest

from wolserver import vpn, wifi

from .test_wifi import CLIENT_IP, FakeNM, make_manager

GOOD = """client
dev tun
proto udp
remote vpn.example.org 1194
remote-cert-tls server
key-direction 1
<ca>
-----BEGIN CERTIFICATE-----
up /bin/evil
-----END CERTIFICATE-----
</ca>
<tls-auth>
x
</tls-auth>
"""


def fake_systemctl(units):
    def runner(argv, timeout=10):
        blocks = ["Id=%s\nActiveState=%s\nUnitFileState=%s" % (u, *units.get(u, ("inactive", "disabled")))
                  for u in argv[4:]]
        return 0, "\n\n".join(blocks) + "\n"
    return runner


class ParseTest(unittest.TestCase):
    def test_good_config(self):
        text, info = vpn.parse_config(GOOD.replace("\n", "\r\n"))
        self.assertNotIn("\r", text)
        self.assertEqual(info, {"remote": "vpn.example.org:1194", "needs_auth": False})
        self.assertIn("up /bin/evil", text)  # Inhalt eines Inline-Blocks ist keine Option

    def test_auth_user_pass_is_normalised(self):
        text, info = vpn.parse_config(GOOD + "auth-user-pass /etc/zugang.txt\n")
        self.assertTrue(info["needs_auth"])
        self.assertIn("\nauth-user-pass\n", text)

    def test_rejects_dangerous_options(self):
        for line in ("up /tmp/x.sh", "script-security 2", "--plugin /lib/x.so", "log /etc/passwd",
                     "status /root/x", "management 0.0.0.0 7505", "management-hold", "  Down /x"):
            with self.subTest(line=line), self.assertRaises(vpn.VpnError) as ctx:
                vpn.parse_config(GOOD + line + "\n")
            self.assertIn("nicht erlaubt", str(ctx.exception))

    def test_rejects_file_references(self):
        for line in ("ca ca.crt", "tls-crypt ta.key", "tls-auth ta.key 1", "pkcs12 /etc/x.p12"):
            with self.subTest(line=line), self.assertRaises(vpn.VpnError) as ctx:
                vpn.parse_config(GOOD + line + "\n")
            self.assertIn("eingebetteten", str(ctx.exception))
        vpn.parse_config(GOOD + "tls-auth [inline] 1\n")  # erlaubt

    def test_rejects_non_client_and_broken(self):
        cases = {"": "leer", "remote x 1194\n": "Client", "client\n": "remote", GOOD + "<key>\nabc\n": "<key>",
                 "client\nremote a\n" + "#" * vpn.MAX_CONFIG: "groß", "client\x00": "Textdatei"}
        for text, expected in cases.items():
            with self.subTest(expected=expected), self.assertRaises(vpn.VpnError) as ctx:
                vpn.parse_config(text)
            self.assertIn(expected, str(ctx.exception))

    def test_credentials(self):
        self.assertEqual(vpn.validate_credential("anna", "Name"), "anna")
        for bad in ("", "a\nb", None, "x" * 300):
            with self.assertRaises(vpn.VpnError):
                vpn.validate_credential(bad, "Name")


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.units = {}
        self.ifaces = []
        self.now = 1000.0
        self.mgr = vpn.VpnManager(self.tmp, runner=fake_systemctl(self.units),
                                  interfaces=lambda: self.ifaces, clock=lambda: self.now)

    def read(self, name):
        with open(os.path.join(self.tmp, "vpn", name)) as f:
            return f.read()

    def finish_job(self, state="ok", message="Fertig"):
        request = json.loads(self.read("request"))
        with open(os.path.join(self.tmp, "vpn", "job.json"), "w") as f:
            json.dump(dict(request, state=state, message=message), f)

    def test_upload_writes_private_files_and_request(self):
        res = self.mgr.upload({"config": GOOD + "auth-user-pass\n", "username": "pi", "password": "geheim"})
        self.assertTrue(res["ok"])
        self.assertEqual(self.read("client.auth"), "pi\ngeheim\n")
        for name in ("client.ovpn", "client.auth", "request"):
            self.assertEqual(os.stat(os.path.join(self.tmp, "vpn", name)).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.read("request"))["action"], "apply")
        job = self.mgr.status()["job"]
        self.assertEqual((job["state"], job["action"]), ("running", "apply"))

    def test_running_job_blocks_new_requests(self):
        self.mgr.upload({"config": GOOD})
        with self.assertRaises(vpn.VpnError):
            self.mgr.remove()
        self.units[vpn.JOB_UNIT] = ("active", "static")
        self.finish_job()
        self.mgr.remove()
        self.assertEqual(json.loads(self.read("request"))["action"], "remove")

    def test_missing_credentials(self):
        with self.assertRaises(vpn.VpnError):
            self.mgr.upload({"config": GOOD + "auth-user-pass\n", "username": "pi"})
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "vpn", "request")))

    def test_crashed_job_is_reported(self):
        self.mgr.upload({"config": GOOD})
        request = json.loads(self.read("request"))
        with open(os.path.join(self.tmp, "vpn", "job.json"), "w") as f:
            json.dump(dict(request, state="running", message="Läuft …"), f)
        job = self.mgr.status()["job"]  # wol-vpn.service ist nicht aktiv
        self.assertEqual(job["state"], "failed")
        self.assertIn("Abgebrochen", job["message"])

    def test_status(self):
        self.units[vpn.UNIT] = ("active", "enabled")
        self.ifaces[:] = [{"name": "wlan0", "kind": "wifi", "up": True, "address": CLIENT_IP},
                          {"name": "tun0", "kind": "vpn", "up": True, "address": "10.8.0.6"}]
        self.mgr.upload({"config": GOOD})
        self.finish_job()
        status = self.mgr.status()
        self.assertEqual((status["enabled"], status["state"], status["address"], status["remote"]),
                         (True, "active", "10.8.0.6", "vpn.example.org:1194"))
        self.assertEqual(status["job"]["state"], "ok")

    def test_enable_needs_config(self):
        with self.assertRaises(vpn.VpnError):
            self.mgr.set_enabled({"enabled": True})


class SocketTest(unittest.TestCase):
    def test_vpn_commands_over_socket_and_api(self):
        from tests.test_api import ApiTest

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        fake = FakeNM(["Heim"])
        fake.address, fake.connected = CLIENT_IP, "Heim"
        mgr, _ = make_manager(fake, tmp)
        manager = vpn.VpnManager(tmp, runner=fake_systemctl({}), interfaces=lambda: [])
        path = os.path.join(tmp, "control.sock")
        server = wifi.ControlServer(path, mgr, {os.getuid()}, manager)
        server.bind()
        stop = threading.Event()
        self.addCleanup(stop.set)
        threading.Thread(target=server.serve, args=(stop,), daemon=True).start()

        api = ApiTest("test_login_flow")
        api.setUp()
        try:
            api.app.config["wifi_socket"] = path
            status, data = api.call("GET", "/api/vpn")
            self.assertEqual((status, data["configured"], data["job"]), (200, False, None))
            status, data = api.call("POST", "/api/vpn/config", {"config": GOOD + "up /x\n"})
            self.assertEqual(status, 400)
            self.assertIn("nicht erlaubt", data["error"])
            big = GOOD + "# " + "x" * 40000 + "\n"  # größer als der alte Socket-Puffer
            status, data = api.call("POST", "/api/vpn/config", {"config": big})
            self.assertEqual(status, 200, data)
            status, data = api.call("GET", "/api/vpn")
            self.assertEqual((data["job"]["state"], data["remote"]), ("running", "vpn.example.org:1194"))
            status, data = api.call("DELETE", "/api/vpn")
            self.assertEqual(status, 400)  # Einrichtung läuft noch
        finally:
            api.tearDown()


if __name__ == "__main__":
    unittest.main()
