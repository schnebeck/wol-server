import datetime
import os
import socket
import tempfile
import time
import unittest

from wolserver import auth, models, wol
from wolserver.scheduler import Scheduler, next_run
from wolserver.store import Store


class WolTest(unittest.TestCase):
    def test_normalize_mac_formats(self):
        for raw in ("aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff", "aabbccddeeff"):
            self.assertEqual(wol.normalize_mac(raw), "AA:BB:CC:DD:EE:FF")

    def test_invalid_mac(self):
        for raw in ("", "aa:bb:cc", "zz:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff:00"):
            with self.assertRaises(ValueError):
                wol.normalize_mac(raw)

    def test_magic_packet(self):
        pkt = wol.magic_packet("01:02:03:04:05:06")
        self.assertEqual(len(pkt), 102)
        self.assertEqual(pkt[:6], b"\xff" * 6)
        self.assertEqual(pkt[6:12], bytes([1, 2, 3, 4, 5, 6]))
        self.assertEqual(pkt[-6:], bytes([1, 2, 3, 4, 5, 6]))

    def test_sleep_on_lan_packet_is_reversed(self):
        pkt = wol.magic_packet("01:02:03:04:05:06", reverse=True)
        self.assertEqual(pkt[6:12], bytes([6, 5, 4, 3, 2, 1]))

    def test_send_magic_packet_arrives(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx:
            rx.bind(("127.0.0.1", 0))
            rx.settimeout(2)
            port = rx.getsockname()[1]
            wol.send_magic_packet("01:02:03:04:05:06", "127.0.0.1", port, count=1)
            data, _ = rx.recvfrom(200)
        self.assertEqual(data, wol.magic_packet("01:02:03:04:05:06"))


class ModelTest(unittest.TestCase):
    def test_device_defaults(self):
        dev = models.validate_device({"name": " PC ", "mac": "aabbccddeeff"})
        self.assertEqual(dev["name"], "PC")
        self.assertEqual(dev["broadcast"], "255.255.255.255")
        self.assertEqual(dev["wol_port"], 9)
        self.assertEqual(dev["sleep_method"], "none")

    def test_device_rejects_bad_input(self):
        bad = [
            {"name": "", "mac": "aabbccddeeff"},
            {"name": "x", "mac": "aabbccddeeff", "host": "-oProxyCommand=evil"},
            {"name": "x", "mac": "aabbccddeeff", "broadcast": "foo"},
            {"name": "x", "mac": "aabbccddeeff", "wol_port": 70000},
            {"name": "x", "mac": "aabbccddeeff", "sleep_method": "ssh", "ssh_user": "u", "ssh_command": "c"},
            {"name": "x", "mac": "aabbccddeeff", "host": "h", "sleep_method": "ssh", "ssh_user": "-l", "ssh_command": "c"},
            {"name": "x", "mac": "aabbccddeeff", "sleep_method": "http", "http_url": "file:///etc/passwd"},
            {"name": "a\nb", "mac": "aabbccddeeff"},
        ]
        for data in bad:
            with self.assertRaises(models.ValidationError, msg=data):
                models.validate_device(data)

    def test_schedule_validation(self):
        devices = {"d1": {"id": "d1", "name": "PC", "sleep_method": "none"}}
        lookup = devices.get
        s = models.validate_schedule({"device_id": "d1", "action": "wake", "time": "07:05", "days": [4, 0, 0]}, lookup)
        self.assertEqual(s["days"], [0, 4])
        with self.assertRaises(models.ValidationError):
            models.validate_schedule({"device_id": "d1", "action": "sleep", "time": "07:05", "days": [0]}, lookup)
        with self.assertRaises(models.ValidationError):
            models.validate_schedule({"device_id": "d1", "action": "wake", "time": "24:00", "days": [0]}, lookup)
        with self.assertRaises(models.ValidationError):
            models.validate_schedule({"device_id": "d1", "action": "wake", "time": "07:00", "days": [7]}, lookup)


class AuthTest(unittest.TestCase):
    def test_hash_roundtrip(self):
        h = auth.hash_password("geheim123", iterations=1000)
        self.assertTrue(auth.verify_password("geheim123", h))
        self.assertFalse(auth.verify_password("falsch", h))
        self.assertFalse(auth.verify_password("x", "kaputt"))

    def test_tokens_and_rotation(self):
        with tempfile.TemporaryDirectory() as d:
            a = auth.Auth(d)
            self.assertFalse(a.required)
            a.set_password("geheim123")
            self.assertTrue(a.required)
            token = a.make_token()
            self.assertTrue(a.verify_token(token))
            self.assertFalse(a.verify_token(token + "x"))
            self.assertFalse(a.verify_token("1.abc"))
            # zweite Instanz (z. B. CLI) ändert das Passwort -> alte Sessions ungültig
            time.sleep(0.01)
            auth.Auth(d).set_password("anderes123")
            self.assertFalse(a.verify_token(token))
            self.assertEqual(os.stat(os.path.join(d, "auth.json")).st_mode & 0o777, 0o600)

    def test_limiter(self):
        lim = auth.LoginLimiter(max_failures=3, window=60, lockout=60)
        for _ in range(3):
            self.assertFalse(lim.blocked("1.2.3.4"))
            lim.fail("1.2.3.4")
        self.assertTrue(lim.blocked("1.2.3.4"))
        self.assertFalse(lim.blocked("5.6.7.8"))
        lim.reset("1.2.3.4")
        self.assertFalse(lim.blocked("1.2.3.4"))


class StoreTest(unittest.TestCase):
    def test_persist_and_cascade(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "data.json")
            s = Store(path)
            dev = s.add("devices", {"name": "PC"})
            s.add("schedules", {"device_id": dev["id"], "time": "07:00"})
            s.add_event(device="PC", action="wake", ok=True)
            s2 = Store(path)
            self.assertEqual(len(s2.list("devices")), 1)
            self.assertEqual(len(s2.list_events()), 1)
            self.assertTrue(s2.delete("devices", dev["id"]))
            self.assertEqual(s2.list("schedules"), [])

    def test_corrupt_file_is_moved_aside(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "data.json")
            with open(path, "w") as f:
                f.write("{kaputt")
            s = Store(path)
            self.assertEqual(s.list("devices"), [])
            self.assertTrue(any(n.startswith("data.json.broken-") for n in os.listdir(d)))


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "data.json"))
        self.dev = self.store.add("devices", {"name": "PC"})
        self.calls = []
        self.sched = Scheduler(self.store, lambda d, a, s: self.calls.append((d["name"], a, s)))

    def tearDown(self):
        self.tmp.cleanup()

    def _ts(self, y, mo, d, h, mi):
        return int(time.mktime((y, mo, d, h, mi, 0, 0, 0, -1)))

    def _add(self, hhmm, days, enabled=True):
        return self.store.add("schedules", {
            "device_id": self.dev["id"], "action": "wake", "time": hhmm, "days": days, "enabled": enabled,
        })

    def _wait_calls(self, n):
        deadline = time.time() + 2
        while len(self.calls) < n and time.time() < deadline:
            time.sleep(0.01)

    def test_fires_on_matching_minute_and_day(self):
        s = self._add("07:30", [0])  # Montag
        t = self._ts(2026, 10, 26, 7, 30)  # Montag
        self.assertEqual(self.sched.tick(t - 60, t), [s["id"]])
        self._wait_calls(1)
        self.assertEqual(self.calls, [("PC", "wake", "Zeitplan")])
        self.assertEqual(self.sched.tick(t - 60, t), [])  # nicht doppelt
        tue = self._ts(2026, 10, 27, 7, 30)
        self.assertEqual(self.sched.tick(tue - 60, tue), [])

    def test_disabled_and_backwards_clock(self):
        self._add("07:30", [0], enabled=False)
        t = self._ts(2026, 10, 26, 7, 30)
        self.assertEqual(self.sched.tick(t - 60, t), [])
        self.assertEqual(self.sched.tick(t + 600, t), [])

    def test_catchup_is_limited(self):
        early = self._add("07:00", [0, 1, 2, 3, 4, 5, 6])
        late = self._add("07:55", [0, 1, 2, 3, 4, 5, 6])
        start = self._ts(2026, 10, 26, 6, 0)
        now = self._ts(2026, 10, 26, 8, 0)  # Uhr springt 2 h nach vorn (NTP nach Boot)
        fired = self.sched.tick(start, now)
        self.assertIn(late["id"], fired)
        self.assertNotIn(early["id"], fired)

    def test_next_run(self):
        sched = {"enabled": True, "time": "07:30", "days": [0]}
        now = datetime.datetime(2026, 10, 26, 7, 30)  # Montag, genau zur Zeit
        self.assertEqual(next_run(sched, now), datetime.datetime(2026, 11, 2, 7, 30))
        now = datetime.datetime(2026, 10, 25, 23, 0)  # Sonntag
        self.assertEqual(next_run(sched, now), datetime.datetime(2026, 10, 26, 7, 30))
        self.assertIsNone(next_run(dict(sched, enabled=False), now))


if __name__ == "__main__":
    unittest.main()
