import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest

from wolserver import dhcpprobe, dnsclient, dnsjob, netcfg, netsvc, oui, wifi
from wolserver.netcfg import NetError

from .test_wifi import CLIENT_IP, FakeNM, make_manager

DNSMASQ = shutil.which("dnsmasq")

DHCPD_CONF = """
# ISC dhcpd wie auf dem Backup-Server
option domain-name "oldnet.local";
option domain-name-servers 192.168.1.1;
default-lease-time 600;
max-lease-time 7200;
subnet 192.168.1.0 netmask 255.255.255.0 {
    authoritative;
    range 192.168.1.254 192.168.1.254;
    deny unknown-clients;
    option ntp-servers 192.168.1.1;
    option routers 192.168.1.1;
    host Iphone-Anna { hardware ethernet 40:4D:7F:03:16:AB; fixed-address 192.168.1.21; }
    host LinuxCNC_Alfa { hardware ethernet 00:24:d7:9d:07:05; fixed-address 192.168.1.38; }
    group {
        host office { hardware ethernet 00:d8:61:d7:1d:36; fixed-address 192.168.1.9; }
    }
    host wol { hardware ethernet b8:27:eb:4a:4f:f1; fixed-address 192.168.1.8; }
    host kaputt { hardware ethernet zz:00; fixed-address 192.168.1.99; }
    host "Annas Nexus 7" { hardware ethernet ac:22:0b:20:87:e3; fixed-address 192.168.1.13; }
    host doppelt { hardware ethernet 00:11:22:33:44:55; fixed-address 192.168.1.21; }
}
class "drucker" { match if substring (hardware, 1, 3) = 94:dd:f8; }
"""


def base_config(**changes):
    cfg = netcfg.validate_config(netcfg.DEFAULT_NETWORK)
    cfg["server"].update(ip="192.168.1.8", prefix=24, name="wol")
    cfg["dhcp"].update(gateway="192.168.1.1", ntp=["192.168.1.1"])
    cfg["dns"].update(upstream=["192.168.1.1"])
    for section, values in changes.items():
        if isinstance(values, dict):
            cfg[section].update(values)
        else:
            cfg[section] = values
    return netcfg.validate_config(cfg)


def res(mac, ip, name, **extra):
    return dict({"mac": mac, "ip": ip, "name": name}, **extra)


class NamesTest(unittest.TestCase):
    def test_normalize_name(self):
        cases = {"Anna's iPhone": "annas-iphone", "LinuxCNC_Alfa": "linuxcnc-alfa", "Müllers Büro-PC": "muellers-buero-pc",
                 "  --x--  ": "x", "Ça va": "ca-va", "a" * 80: "a" * 63, "!!!": "", None: "", "Straße 1": "strasse-1"}
        for raw, expected in cases.items():
            self.assertEqual(netcfg.normalize_name(raw), expected, raw)

    def test_private_mac(self):
        self.assertTrue(netcfg.is_private_mac("5a:1f:3c:00:00:01"))
        self.assertFalse(netcfg.is_private_mac("b8:27:eb:4a:4f:f1"))
        self.assertFalse(netcfg.is_private_mac("kaputt"))

    def test_lease_time(self):
        self.assertEqual(netcfg.parse_lease_time("12h"), 43200)
        self.assertEqual(netcfg.parse_lease_time("30 min"), 1800)
        self.assertEqual(netcfg.parse_lease_time("1 Tag"), 86400)
        self.assertEqual(netcfg.parse_lease_time(600), 600)
        for bad in ("60", "8d", "zwölf", True, -5):
            with self.assertRaises(NetError):
                netcfg.parse_lease_time(bad)
        self.assertEqual([netcfg.fmt_lease_time(x) for x in (600, 43200, 86400, 7 * 86400, 90)],
                         ["10 min", "12 h", "1 Tag", "7 Tage", "90 s"])


class ConfigTest(unittest.TestCase):
    def test_defaults_are_valid_and_off(self):
        cfg = netcfg.validate_config(netcfg.DEFAULT_NETWORK)
        self.assertFalse(cfg["dns"]["enabled"] or cfg["dhcp"]["enabled"])
        self.assertEqual(cfg["dns"]["domain"], "home.arpa")
        self.assertEqual(cfg["dhcp"]["lease_time"], 43200)

    def test_rejects_bad_settings(self):
        cases = [
            ({"dns": {"domain": "oldnet.local"}}, "mDNS"),
            ({"dns": {"domain": "nopunkt"}}, "Domäne"),
            ({"dhcp": {"enabled": True, "gateway": ""}}, "Gateway"),
            ({"dhcp": {"mode": "range", "range_start": "192.168.1.200", "range_end": "192.168.1.100"}}, "beginnt"),
            ({"dhcp": {"mode": "range"}}, "Anfang und Ende"),
            ({"dhcp": {"gateway": "10.0.0.1"}}, "nicht im Netz"),
            ({"dns": {"enabled": True, "upstream": []}}, "Upstream"),
            ({"dhcp": {"boot_file": "../etc/shadow"}}, "Boot-Datei"),
            ({"dns": {"conditional": [{"domain": "x.home.arpa", "server": "10.0.0.1"}]}}, "eigenen Domäne"),
            ({"server": {"interface": "wlan0; rm"}}, "Interface"),
        ]
        for changes, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(NetError) as ctx:
                base_config(**changes)
            self.assertIn(expected, str(ctx.exception))

    def test_dhcp_needs_server_address(self):
        cfg = netcfg.validate_config(netcfg.DEFAULT_NETWORK)
        cfg["dhcp"].update(enabled=True, gateway="192.168.1.1")
        with self.assertRaises(NetError) as ctx:
            netcfg.validate_config(cfg)
        self.assertIn("feste Adresse", str(ctx.exception))

    def test_reservation_conflicts(self):
        good = [res("00:d8:61:d7:1d:36", "192.168.1.9", "Office")]
        cfg = base_config(reservations=good)
        self.assertEqual(cfg["reservations"][0]["name"], "office")
        self.assertEqual(cfg["reservations"][0]["mac"], "00:d8:61:d7:1d:36")
        cases = [
            (good + [res("00:D8:61:D7:1D:36", "192.168.1.10", "x")], "MAC"),
            (good + [res("00:11:22:33:44:55", "192.168.1.9", "x")], "IP 192.168.1.9 ist doppelt"),
            (good + [res("00:11:22:33:44:55", "192.168.1.10", "OFFICE")], "Name „office“"),
            ([res("00:11:22:33:44:55", "192.168.1.8", "x")], "WOL-Server"),
            ([res("00:11:22:33:44:55", "192.168.2.5", "x")], "nicht im Netz"),
            ([res("01:00:5e:00:00:01", "192.168.1.5", "x")], "Multicast"),
            ([res("00:11:22:33:44:55", "192.168.1.255", "x")], "Broadcast"),
            ([res("00:11:22:33:44:55", "192.168.1.5", "wol")], "Name „wol“"),
        ]
        for reservations, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(NetError) as ctx:
                base_config(reservations=reservations)
            self.assertIn(expected, str(ctx.exception))
        with self.assertRaises(NetError) as ctx:
            base_config(dhcp={"mode": "range", "range_start": "192.168.1.200", "range_end": "192.168.1.250"},
                        reservations=[res("00:11:22:33:44:55", "192.168.1.210", "x")])
        self.assertIn("dynamischen Bereich", str(ctx.exception))

    def test_records(self):
        cfg = base_config(reservations=[res("00:05:9e:11:03:64", "192.168.1.37", "satip")], records=[
            {"type": "host", "name": "router", "value": "192.168.1.1"},
            {"type": "host", "name": "router", "value": "fd00::1"},
            {"type": "alias", "name": "fernseher", "value": "satip"},
            {"type": "alias", "name": "tv", "value": "fernseher.home.arpa"},
            {"type": "mx", "name": "", "value": "mail.example.org", "priority": 10},
            {"type": "srv", "name": "_ipp._tcp", "value": "router", "port": 631},
            {"type": "txt", "name": "info", "value": "Hallo"},
        ])
        self.assertEqual(len(cfg["records"]), 7)
        cases = [
            ({"type": "alias", "name": "x", "value": "gibtsnicht"}, "lokaler Name"),
            ({"type": "alias", "name": "satip", "value": "router"}, "kollidiert"),
            ({"type": "host", "name": "satip", "value": "192.168.1.2"}, "schon vergeben"),
            ({"type": "srv", "name": "ipp", "value": "router", "port": 631}, "_dienst._tcp"),
            ({"type": "txt", "name": "t", "value": 'a"b'}, "Anführungszeichen"),
            ({"type": "host", "name": "a.b", "value": "192.168.1.2"}, "ohne Punkte"),
            ({"type": "cname", "name": "a", "value": "b"}, "Eintragstyp"),
            ({"type": "alias", "name": "self", "value": "self"}, "sich selbst"),
        ]
        for record, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(NetError) as ctx:
                base_config(reservations=[res("00:05:9e:11:03:64", "192.168.1.37", "satip")],
                            records=[{"type": "host", "name": "router", "value": "192.168.1.1"}, record])
            self.assertIn(expected, str(ctx.exception))

    def test_settings_merge(self):
        cfg = base_config()
        new = netcfg.validate_settings(cfg, {"dhcp": {"lease_time": "1d", "search": "oldnet.local, home.arpa"},
                                             "dns": {"enabled": True}, "evil": {"x": 1}})
        self.assertEqual(new["dhcp"]["lease_time"], 86400)
        self.assertEqual(new["dhcp"]["search"], ["oldnet.local"])  # eigene Domäne nicht doppelt
        self.assertTrue(new["dns"]["enabled"])
        self.assertNotIn("evil", new)


class RenderTest(unittest.TestCase):
    def cfg(self, **changes):
        return base_config(reservations=[res("00:d8:61:d7:1d:36", "192.168.1.9", "office", lease_time="1h"),
                                         res("00:11:22:33:44:55", "192.168.1.30", "aus", enabled=False)],
                           records=[{"type": "host", "name": "router", "value": "192.168.1.1"},
                                    {"type": "alias", "name": "pc", "value": "office"},
                                    {"type": "srv", "name": "_ipp._tcp", "value": "router", "port": 631}],
                           **changes)

    def test_known_only(self):
        files = netcfg.render(self.cfg(dns={"enabled": True}, dhcp={"enabled": True, "search": ["oldnet.local"]}))
        conf = files["dnsmasq.conf"]
        for line in ("dhcp-range=192.168.1.0,static,255.255.255.0,43200", "dhcp-ignore=tag:!known",
                     "dhcp-option=option:dns-server,192.168.1.8", "server=192.168.1.1", "local=/home.arpa/",
                     "cname=pc.home.arpa,office-pc.home.arpa", "no-hosts",
                     "srv-host=_ipp._tcp.home.arpa,router.home.arpa,631,0,0",
                     "dhcp-option=option:domain-search,home.arpa,oldnet.local",
                     "dhcp-script=/opt/wol-server/deploy/dns-lease-hook.sh"):
            self.assertIn(line + "\n", conf)
        self.assertEqual(files["hosts.dhcp"].splitlines()[1:], ["00:d8:61:d7:1d:36,192.168.1.9,office,3600"])
        self.assertIn("192.168.1.8 wol\n", files["hosts.dns"])
        self.assertIn("192.168.1.9 office\n", files["hosts.dns"])
        self.assertNotIn("aus", files["hosts.dns"])

    def test_range_and_dns_off(self):
        conf = netcfg.render(self.cfg(dhcp={"enabled": True, "mode": "range", "range_start": "192.168.1.200",
                                            "range_end": "192.168.1.249", "boot_file": "pxelinux.0", "tftp": True}))["dnsmasq.conf"]
        self.assertIn("port=0\n", conf)
        self.assertIn("dhcp-range=192.168.1.200,192.168.1.249,255.255.255.0,43200\n", conf)
        self.assertIn("dhcp-option=option:dns-server,192.168.1.1\n", conf)  # DNS aus → Router
        self.assertIn("dhcp-boot=pxelinux.0,,192.168.1.8\n", conf)
        self.assertIn("tftp-secure\n", conf)
        self.assertNotIn("dhcp-ignore", conf)

    def test_only_dns(self):
        conf = netcfg.render(self.cfg(dns={"enabled": True, "log_queries": True}))["dnsmasq.conf"]
        self.assertNotIn("dhcp-range", conf)
        self.assertIn("log-queries\n", conf)

    @unittest.skipUnless(DNSMASQ, "dnsmasq nicht installiert")
    def test_dnsmasq_accepts_generated_config(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for changes in ({"dns": {"enabled": True}}, {"dns": {"enabled": True}, "dhcp": {"enabled": True}},
                        {"dhcp": {"enabled": True, "mode": "range", "range_start": "192.168.1.200", "range_end": "192.168.1.249"}}):
            files = netcfg.render(self.cfg(**changes), conf_dir=tmp, data_dir=tmp, user=os.environ.get("USER", "nobody"),
                                  hook="/bin/true")
            for name, content in files.items():
                with open(os.path.join(tmp, name), "w") as f:
                    f.write(content)
            proc = subprocess.run([DNSMASQ, "--test", "--conf-file=%s/dnsmasq.conf" % tmp], capture_output=True,
                                  text=True, env=dict(os.environ, LC_ALL="C"))
            self.assertEqual(proc.returncode, 0, proc.stderr)


class HelpersTest(unittest.TestCase):
    def test_diff(self):
        old = base_config(reservations=[res("00:d8:61:d7:1d:36", "192.168.1.9", "office")])
        new = json.loads(json.dumps(old))
        new["dhcp"]["lease_time"] = 600
        new["reservations"][0]["ip"] = "192.168.1.19"
        new["reservations"].append(netcfg.validate_reservation(res("00:11:22:33:44:55", "192.168.1.30", "neu")))
        new["records"].append(netcfg.validate_record({"type": "host", "name": "nas", "value": "192.168.1.5"}))
        changes = netcfg.diff(old, netcfg.validate_config(new))
        self.assertIn("Lease-Zeit: 12 h → 10 min", changes)
        self.assertIn("Reservierung office: IP 192.168.1.9 → 192.168.1.19", changes)
        self.assertIn("Reservierung neu: neu (192.168.1.30)", changes)
        self.assertIn("DNS-Eintrag neu: nas → 192.168.1.5", changes)
        self.assertEqual(netcfg.diff(old, old), [])

    def test_next_free_ip_and_unique_name(self):
        cfg = base_config(reservations=[res("00:d8:61:d7:1d:36", "192.168.1.10", "office")],
                          records=[{"type": "host", "name": "nas", "value": "192.168.1.11"}])
        self.assertEqual(netcfg.next_free_ip(cfg, [{"ip": "192.168.1.12"}]), "192.168.1.13")
        self.assertEqual(netcfg.unique_name(cfg, "Office"), "office-2")
        self.assertEqual(netcfg.unique_name(cfg, "!!!"), "geraet")
        self.assertIsNone(netcfg.next_free_ip(netcfg.validate_config(netcfg.DEFAULT_NETWORK)))

    def test_parse_leases_and_events(self):
        now = 1_000_000
        text = ("1000100 00:d8:61:d7:1d:36 192.168.1.9 office 01:00:d8:61:d7:1d:36\n"
                "999000 00:11:22:33:44:55 192.168.1.30 alt *\n"
                "0 AA:BB:CC:DD:EE:FF 192.168.1.40 * *\n"
                "duid 00:01:00:01\nkaputt\n")
        leases = netcfg.parse_leases(text, now)
        self.assertEqual([(l["ip"], l["hostname"], l["expires"]) for l in leases],
                         [("192.168.1.9", "office", 1000100), ("192.168.1.40", "", None)])
        events = netcfg.parse_lease_events("100 add 00:d8:61:d7:1d:36 192.168.1.9 office\n200 old 00:d8:61:d7:1d:36 192.168.1.9\n"
                                           "300 del 00:d8:61:d7:1d:36 192.168.1.9\nmüll\n")
        self.assertEqual(events["00:d8:61:d7:1d:36"]["last_seen"], 200)
        self.assertEqual(len(events["00:d8:61:d7:1d:36"]["events"]), 3)

    def test_parse_unknown(self):
        journal = "\n".join([
            "1700000000.1 wol dnsmasq-dhcp[1]: 123 available DHCP subnet: 192.168.1.0/255.255.255.0",
            "1700000000.2 wol dnsmasq-dhcp[1]: 123 client provides name: Annas-iPhone",
            "1700000000.3 wol dnsmasq-dhcp[1]: 123 DHCPDISCOVER(wlan0) 5a:1f:3c:00:00:01 ignored",
            "1700000060.0 wol dnsmasq-dhcp[1]: 124 DHCPDISCOVER(wlan0) 5a:1f:3c:00:00:01 ignored",
            "1700000100.0 wol dnsmasq-dhcp[1]: DHCPDISCOVER(wlan0) 00:11:22:33:44:55 no address available",
            "1700000200.0 wol dnsmasq[1]: query[A] heise.de from 192.168.1.12",
        ])
        found = netcfg.parse_unknown(journal)
        self.assertEqual(found["5a:1f:3c:00:00:01"]["name"], "Annas-iPhone")
        self.assertEqual(found["5a:1f:3c:00:00:01"]["count"], 2)
        self.assertEqual(found["5a:1f:3c:00:00:01"]["last"], 1700000060.0)
        self.assertEqual(found["00:11:22:33:44:55"]["count"], 1)


class ImportTest(unittest.TestCase):
    def test_preview(self):
        preview = netcfg.import_dhcpd(DHCPD_CONF)
        st = preview["settings"]
        self.assertEqual((st["gateway"], st["lease_time"], st["mode"], st["prefix"]), ("192.168.1.1", 600, "known-only", 24))
        self.assertEqual(st["domain"], "oldnet.local")
        by_orig = {r["original_name"]: r for r in preview["reservations"]}
        self.assertEqual(by_orig["LinuxCNC_Alfa"]["name"], "linuxcnc-alfa")
        self.assertEqual(by_orig["Iphone-Anna"]["mac"], "40:4d:7f:03:16:ab")
        self.assertEqual(by_orig["office"]["ip"], "192.168.1.9")  # aus group { … }
        self.assertEqual(by_orig["Annas Nexus 7"]["name"], "annas-nexus-7")
        self.assertTrue(by_orig["kaputt"]["problems"])
        self.assertIn("IP doppelt", by_orig["doppelt"]["problems"])
        self.assertTrue(any("drucker" in w for w in preview["warnings"]))
        self.assertTrue(any("deny unknown-clients" in w for w in preview["warnings"]))

    def test_annotate_and_merge(self):
        cfg = base_config(reservations=[res("00:d8:61:d7:1d:36", "192.168.1.50", "alt")])
        preview = netcfg.annotate_import(cfg, netcfg.import_dhcpd(DHCPD_CONF), "b8:27:eb:4a:4f:f1")
        by_orig = {r["original_name"]: r for r in preview["reservations"]}
        self.assertTrue(any("WOL-Server selbst" in p for p in by_orig["wol"]["problems"]))
        self.assertEqual(by_orig["office"]["exists"], "ändert IP 192.168.1.50 → 192.168.1.9, Name alt → office")
        new, stats = netcfg.merge_import(cfg, preview)
        self.assertEqual(stats, {"added": 3, "updated": 1, "skipped": 3})
        self.assertEqual(new["dhcp"]["lease_time"], 600)
        self.assertEqual(new["dhcp"]["search"], ["oldnet.local"])  # .local nicht als Domäne
        self.assertEqual(new["dns"]["domain"], "home.arpa")
        self.assertEqual({r["name"]: r["ip"] for r in new["reservations"]}["office"], "192.168.1.9")
        replaced, stats = netcfg.merge_import(cfg, preview, replace=True, take_settings=False)
        self.assertEqual(stats["updated"], 0)
        self.assertEqual(replaced["dhcp"]["lease_time"], 43200)

    def test_garbage(self):
        for text in ("", "   "):
            with self.assertRaises(NetError):
                netcfg.import_dhcpd(text)
        self.assertEqual(netcfg.import_dhcpd("irgendwas;")["reservations"], [])


class OuiTest(unittest.TestCase):
    def setUp(self):
        oui._cache.clear()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_csv_and_private(self):
        path = os.path.join(self.tmp, "oui.csv")
        with open(path, "w") as f:
            f.write('Registry,Assignment,Organization Name,Organization Address\n'
                    'MA-L,B827EB,Raspberry Pi Foundation,"Cambridge GB"\nMA-L,00D861,Micro-Star INTL CO.  LTD.,x\n')
        found = oui.lookup_many(["b8:27:eb:4a:4f:f1", "5a:1f:3c:00:00:01", "00:d8:61:d7:1d:36", "00:00:01:00:00:00"],
                                (path, "csv"))
        self.assertEqual(found["b8:27:eb:4a:4f:f1"], "Raspberry Pi Foundation")
        self.assertEqual(found["5a:1f:3c:00:00:01"], oui.PRIVATE)
        self.assertEqual(found["00:d8:61:d7:1d:36"], "Micro-Star INTL")
        self.assertEqual(found["00:00:01:00:00:00"], "")

    def test_nmap_format(self):
        path = os.path.join(self.tmp, "nmap")
        with open(path, "w") as f:
            f.write("# Kommentar\nB827EB Raspberry Pi Foundation\n")
        self.assertEqual(oui.lookup_many(["b8:27:eb:00:00:01"], (path, "nmap"))["b8:27:eb:00:00:01"],
                         "Raspberry Pi Foundation")


class FakeDns:
    """Minimaler UDP-DNS-Server für Paket-Tests."""

    def __init__(self, answer):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.answer = answer
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(512)
            except OSError:
                return
            self.sock.sendto(self.answer(data), addr)


class DnsClientTest(unittest.TestCase):
    def test_roundtrip_with_compression(self):
        def answer(query):
            qid = query[:2]
            question = query[12:]
            # CNAME pc → office (Zeiger auf Frage), dann A-Eintrag
            cname_target = b"\x04aida\xc0\x0f"  # Name „office“ + Zeiger auf „lan…“-Rest der Frage
            rr1 = b"\xc0\x0c" + struct.pack("!HHIH", 5, 1, 60, len(cname_target)) + cname_target
            rr2 = b"\xc0" + bytes([12 + len(question) + 12]) + struct.pack("!HHIH", 1, 1, 60, 4) + bytes([192, 168, 1, 9])
            return qid + struct.pack("!HHHHH", 0x8580, 1, 2, 0, 0) + question + rr1 + rr2
        server = FakeDns(answer)
        self.addCleanup(server.sock.close)
        result = dnsclient.query("pc.home.arpa", "A", "127.0.0.1", server.port)
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["authoritative"])
        self.assertEqual([(a["type"], a["data"]) for a in result["answers"]],
                         [("CNAME", "office-pc.home.arpa"), ("A", "192.168.1.9")])

    def test_timeout_and_validation(self):
        silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        silent.bind(("127.0.0.1", 0))
        self.addCleanup(silent.close)
        with self.assertRaises(dnsclient.DnsError):
            dnsclient.query("x.de", "A", "127.0.0.1", silent.getsockname()[1], timeout=0.3)
        with self.assertRaises(dnsclient.DnsError):
            dnsclient.lookup("")
        with self.assertRaises(dnsclient.DnsError):
            dnsclient.encode_name("a" * 64 + ".de")
        self.assertEqual(dnsclient.reverse_name("192.168.1.9"), "9.1.168.192.in-addr.arpa")

    @unittest.skipUnless(DNSMASQ, "dnsmasq nicht installiert")
    def test_against_real_dnsmasq(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cfg = base_config(reservations=[res("00:d8:61:d7:1d:36", "192.168.1.9", "office")],
                          records=[{"type": "alias", "name": "pc", "value": "office"},
                                   {"type": "txt", "name": "info", "value": "Hallo Welt"}],
                          dns={"enabled": True, "upstream": ["127.0.0.1"]})
        cfg["server"].update(interface="lo", port=port)
        for name, content in netcfg.render(cfg, tmp, tmp, os.environ.get("USER", "nobody"), "/bin/true").items():
            with open(os.path.join(tmp, name), "w") as f:
                f.write(content)
        proc = subprocess.Popen([DNSMASQ, "--keep-in-foreground", "--conf-file=%s/dnsmasq.conf" % tmp,
                                 "--pid-file=%s/pid" % tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        for _ in range(30):
            try:
                result = dnsclient.lookup("pc.home.arpa", "127.0.0.1", port, timeout=0.5)
                break
            except dnsclient.DnsError:
                time.sleep(0.1)
        self.assertEqual([(a["type"], a["data"]) for a in result["answers"]],
                         [("CNAME", "office-pc.home.arpa"), ("A", "192.168.1.9")])
        self.assertEqual(dnsclient.lookup("192.168.1.9", "127.0.0.1", port)["answers"][0]["data"], "office-pc.home.arpa")
        self.assertEqual(dnsclient.lookup("192.168.1.8", "127.0.0.1", port)["answers"][0]["data"], "wol.home.arpa")
        self.assertEqual(dnsclient.query("info.home.arpa", "TXT", "127.0.0.1", port)["answers"][0]["data"], "Hallo Welt")
        self.assertEqual(dnsclient.lookup("weg.home.arpa", "127.0.0.1", port)["rcode"], 3)
        self.assertEqual(dnsclient.dnsmasq_stats("127.0.0.1", port)["cachesize"], 1000)


class ProbeTest(unittest.TestCase):
    def test_discover_and_offer(self):
        packet = dhcpprobe.build_discover("b8:27:eb:4a:4f:f1", 0x12345678)
        self.assertEqual(len(packet), 240 + 11)
        self.assertEqual(packet[28:34], bytes.fromhex("b827eb4a4ff1"))
        self.assertEqual(dhcpprobe.parse_options(packet[240:])[53], b"\x01")
        offer = bytearray(packet[:240])
        offer[0] = 2
        offer[16:20] = bytes([192, 168, 1, 8])
        options = bytes([53, 1, 2, 54, 4, 192, 168, 1, 10, 3, 4, 192, 168, 1, 1, 51, 4, 0, 0, 2, 88, 255])
        parsed = dhcpprobe.parse_offer(bytes(offer) + options, 0x12345678)
        self.assertEqual(parsed, {"offered": "192.168.1.8", "server_id": "192.168.1.10", "router": "192.168.1.1",
                                  "lease_time": 600})
        self.assertIsNone(dhcpprobe.parse_offer(bytes(offer) + options, 0x1))
        self.assertIsNone(dhcpprobe.parse_offer(b"kurz", 0x12345678))


class Systemctl:
    """Antworten von systemctl/nmcli/journalctl für NetManager."""

    def __init__(self):
        self.calls = []
        self.dns_state = "inactive"
        self.method = "auto"
        self.journal = ""

    def __call__(self, argv, timeout=15):
        self.calls.append(argv)
        if argv[:2] == ["systemctl", "show"]:
            if "--value" in argv:
                return 0, "inactive\n"
            return 0, "ActiveState=%s\nUnitFileState=disabled\n" % self.dns_state
        if argv[:2] == ["nmcli", "-t"] and "device" in argv:
            return 0, "HomeNet\n"
        if argv[:2] == ["nmcli", "-t"] and "connection" in argv:
            return 0, self.method + "\n"
        if argv[0] == "nmcli":
            return 0, ""
        if argv[0] == "journalctl":
            return 0, self.journal
        return 1, ""


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        self.sys = Systemctl()
        self.now = 1_700_000_000.0
        self.ifaces = [{"name": "wlan0", "kind": "wifi", "up": True, "address": "192.168.1.8", "netmask": "255.255.255.0"}]
        self.arp = {"192.168.1.10": "a8:b8:e0:07:4b:4e"}
        self.mgr = netsvc.NetManager(self.tmp, conf_dir=os.path.join(self.tmp, "conf"), data_dir=self.data,
                                     runner=self.sys, clock=lambda: self.now, interfaces=lambda: self.ifaces,
                                     arp=lambda: self.arp, user="nobody", vendor_source=(None, None))
        self.mgr._gateway = lambda iface: "192.168.1.1"

    def finish(self, state="ok", message="fertig"):
        """Spielt wol-dns-apply.service: alle Aufträge mit Ergebnis versehen."""
        for name in sorted(os.listdir(self.mgr.path("requests"))):
            request = json.load(open(self.mgr.path("requests", name)))
            json.dump({"id": request["id"], "action": request["action"], "state": state, "message": message,
                       "ts": self.now}, open(self.mgr.path("jobs", name), "w"))
            os.remove(self.mgr.path("requests", name))
        self.now += 1

    def test_defaults_from_system(self):
        cfg = self.mgr.active()
        self.assertEqual((cfg["server"]["ip"], cfg["dhcp"]["gateway"], cfg["dns"]["upstream"]),
                         ("192.168.1.8", "192.168.1.1", ["192.168.1.1"]))

    def test_draft_apply_cycle(self):
        self.mgr.settings({"settings": {"dns": {"enabled": True}}})
        saved = self.mgr.reservation_save({"reservation": {"mac": "00:d8:61:d7:1d:36", "ip": "192.168.1.9", "name": "Office"}})
        rid = saved["reservation"]["id"]
        status = self.mgr.status()
        self.assertEqual(status["pending"], ["DNS: aus → an", "Reservierung neu: office (192.168.1.9)"])
        self.assertFalse(status["running"])
        res = self.mgr.apply({"client": "192.168.1.12"})
        self.assertIn("2 Änderungen", res["message"])
        request = json.load(open(self.mgr.path("requests", res["request"] + ".json")))
        name = self.mgr.active()["server"]["name"]  # aus dem Hostnamen des Rechners
        self.assertEqual((request["action"], request["enabled"], request["expect"], request["test_name"]),
                         ("apply", True, "192.168.1.8", "%s.home.arpa" % name))
        staged = open(os.path.join(request["staging"], "hosts.dhcp")).read()
        self.assertIn("00:d8:61:d7:1d:36,192.168.1.9,office", staged)
        self.assertTrue(self.mgr.status()["running"])
        with self.assertRaises(NetError):  # zweite Änderung während der ersten
            self.mgr.apply()
        self.finish("ok")
        status = self.mgr.status()
        self.assertEqual(status["pending"], [])
        self.assertTrue(status["dns_enabled"])
        self.assertEqual(status["last_change"]["request"], res["request"])
        self.assertEqual(status["last_change"]["client"], "192.168.1.12")
        self.assertEqual(self.mgr.active()["reservations"][0]["id"], rid)
        self.assertFalse(os.path.exists(request["staging"]))
        history = self.mgr.history()["history"]
        self.assertEqual(len(history), 1)
        self.assertIn("Reservierung neu: office (192.168.1.9)", history[0]["changes"])

    def test_failed_apply_keeps_draft(self):
        self.mgr.settings({"settings": {"dns": {"enabled": True}}})
        self.mgr.apply()
        self.finish("failed", "dnsmasq lehnt ab")
        status = self.mgr.status()
        self.assertEqual(status["job"]["state"], "failed")
        self.assertEqual(status["pending"], ["DNS: aus → an"])
        self.assertFalse(self.mgr.active()["dns"]["enabled"])

    def test_discard_and_noop(self):
        self.mgr.settings({"settings": {"dhcp": {"lease_time": "1h"}}})
        self.assertTrue(os.path.exists(self.mgr.path("draft.json")))
        self.mgr.settings({"settings": {"dhcp": {"lease_time": "12h"}}})  # zurück auf den aktiven Stand
        self.assertFalse(os.path.exists(self.mgr.path("draft.json")))
        with self.assertRaises(NetError):
            self.mgr.apply()
        self.mgr.settings({"settings": {"dhcp": {"lease_time": "1h"}}})
        self.mgr.discard()
        self.assertEqual(self.mgr.status()["pending"], [])

    def test_records_and_errors(self):
        rec = self.mgr.record_save({"record": {"type": "host", "name": "nas", "value": "192.168.1.5"}})["record"]
        self.mgr.record_save({"record": dict(rec, value="192.168.1.6")})
        self.assertEqual(self.mgr.working()["records"][0]["value"], "192.168.1.6")
        self.mgr.record_delete({"id": rec["id"]})
        with self.assertRaises(NetError):
            self.mgr.record_delete({"id": rec["id"]})
        with self.assertRaises(NetError):
            self.mgr.reservation_save({"reservation": {"id": "gibtsnicht", "mac": "00:11:22:33:44:55", "ip": "192.168.1.9"}})

    def test_unknown_approve_ignore(self):
        self.sys.journal = ("1700000000.0 wol dnsmasq-dhcp[1]: 7 client provides name: Anna's iPhone\n"
                            "1700000000.1 wol dnsmasq-dhcp[1]: 7 DHCPDISCOVER(wlan0) 5a:1f:3c:00:00:01 ignored\n"
                            "1700000001.0 wol dnsmasq-dhcp[1]: DHCPDISCOVER(wlan0) 00:11:22:33:44:55 ignored\n")
        self.mgr.settings({"settings": {"dhcp": {"enabled": True}, "dns": {"enabled": True}}})
        self.mgr.apply()
        self.finish()
        unknown = {u["mac"]: u for u in self.mgr.unknown()["unknown"]}
        phone = unknown["5a:1f:3c:00:00:01"]
        self.assertEqual((phone["suggested_name"], phone["private"]), ("annas-iphone", True))
        # .10 ist per ARP belegt; jüngste Anfrage zuerst, jede bekommt eine eigene Adresse
        self.assertEqual({phone["suggested_ip"], unknown["00:11:22:33:44:55"]["suggested_ip"]},
                         {"192.168.1.11", "192.168.1.12"})
        # Entwurf mit anderer Änderung darf die Freigabe nicht blockieren
        self.mgr.settings({"settings": {"dhcp": {"lease_time": "1d"}}})
        res = self.mgr.approve({"mac": phone["mac"], "name": phone["suggested_name"], "ip": phone["suggested_ip"]})
        self.assertIn("annas-iphone", res["message"])
        request = json.load(open(self.mgr.path("requests", res["request"] + ".json")))
        self.assertIn("5a:1f:3c:00:00:01,%s,annas-iphone" % phone["suggested_ip"],
                      open(os.path.join(request["staging"], "hosts.dhcp")).read())
        self.assertNotIn("86400", open(os.path.join(request["staging"], "dnsmasq.conf")).read())  # Entwurf bleibt Entwurf
        self.assertIn("Lease-Zeit: 12 h → 1 Tag", self.mgr.status()["pending"])
        self.finish()
        self.mgr._unknown_cache = (0, None)
        self.assertEqual([u["mac"] for u in self.mgr.unknown()["unknown"]], ["00:11:22:33:44:55"])
        self.assertTrue(any(r["mac"] == phone["mac"] for r in self.mgr.draft()["reservations"]))
        self.mgr.ignore({"mac": "00:11:22:33:44:55", "comment": "Nachbar"})
        self.assertEqual(self.mgr.unknown()["unknown"], [])
        self.assertEqual(self.mgr.active()["ignored"][0]["comment"], "Nachbar")
        self.mgr.unignore({"mac": "00:11:22:33:44:55"})
        self.assertEqual(len(self.mgr.unknown()["unknown"]), 1)

    def test_leases_and_config_meta(self):
        with open(os.path.join(self.data, "dnsmasq.leases"), "w") as f:
            f.write("%d 00:d8:61:d7:1d:36 192.168.1.9 office-pc *\n%d 5a:1f:3c:00:00:01 192.168.1.40 * *\n"
                    % (self.now + 3600, self.now + 60))
        with open(os.path.join(self.data, "lease-events.log"), "w") as f:
            f.write("%d add 00:d8:61:d7:1d:36 192.168.1.9 office-pc\n" % (self.now - 100))
        self.mgr.reservation_save({"reservation": {"mac": "00:d8:61:d7:1d:36", "ip": "192.168.1.9", "name": "office"}})
        leases = self.mgr.leases()["leases"]
        self.assertEqual([(l["ip"], l["name"], bool(l["reservation"])) for l in leases],
                         [("192.168.1.9", "office", True), ("192.168.1.40", "", False)])
        self.assertTrue(leases[1]["private"])
        meta = self.mgr.config()["meta"]
        self.assertEqual(meta["last_seen"]["00:d8:61:d7:1d:36"], self.now - 100)
        self.assertEqual(meta["next_ip"], "192.168.1.11")
        self.assertEqual(meta["network"], "192.168.1.0/24")
        res = self.mgr.release({"mac": "5a:1f:3c:00:00:01"})
        request = json.load(open(self.mgr.path("requests", res["request"] + ".json")))
        self.assertEqual((request["action"], request["ip"]), ("release", "192.168.1.40"))
        with self.assertRaises(NetError):
            self.mgr.release({"mac": "00:00:00:00:00:01"})  # läuft noch / keine Lease

    def test_import_export_restore_history(self):
        preview = self.mgr.import_preview({"text": DHCPD_CONF})["preview"]
        self.assertTrue(any("WOL-Server selbst" in p for r in preview["reservations"] for p in r["problems"]))
        res = self.mgr.import_commit({"preview": preview})
        self.assertEqual(res["stats"]["added"], 4)
        self.assertEqual(len(self.mgr.working()["reservations"]), 4)
        self.mgr.apply()
        self.finish()
        export = self.mgr.export()["export"]
        self.assertEqual(len(export["config"]["reservations"]), 4)
        self.mgr.reservation_delete({"id": export["config"]["reservations"][0]["id"]})
        self.mgr.restore({"data": export})
        self.assertEqual(self.mgr.status()["pending"], [])  # entspricht wieder dem aktiven Stand
        with self.assertRaises(NetError):
            self.mgr.restore({"data": {"config": {"dns": {"domain": "x.local"}}}})
        self.mgr.settings({"settings": {"dhcp": {"lease_time": "1h"}}})
        self.mgr.apply()
        self.finish()
        history = self.mgr.history()["history"]
        self.assertEqual(len(history), 2)
        self.mgr.history_restore({"id": history[1]["id"]})
        self.assertEqual(self.mgr.status()["pending"], ["Lease-Zeit: 1 h → 10 min"])

    def test_hotspot_and_probe_and_restart(self):
        self.mgr.wifi_changed("client")
        self.assertEqual(os.listdir(self.mgr.path("requests")), [])  # erste Meldung, dnsmasq aus
        self.mgr.settings({"settings": {"dns": {"enabled": True}}})
        self.mgr.apply()
        self.finish()
        self.mgr.wifi_changed("hotspot")
        self.mgr.wifi_changed("hotspot")
        names = os.listdir(self.mgr.path("requests"))
        self.assertEqual(len(names), 1)
        self.assertTrue(json.load(open(self.mgr.path("requests", names[0])))["hotspot"])
        self.finish()
        self.mgr.wifi_changed("offline")  # Hotspot vorbei → wieder starten
        self.assertFalse(json.load(open(self.mgr.path("requests", os.listdir(self.mgr.path("requests"))[0])))["hotspot"])
        self.finish()
        with self.assertRaises(NetError):
            self.mgr.probe()  # MAC von wlan0 im Test unbekannt
        self.mgr._server_state = lambda cfg: {"interface": "wlan0", "mac": "b8:27:eb:4a:4f:f1", "address": "192.168.1.8",
                                              "prefix": 24, "connection": "x", "method": "manual", "hotspot": False}
        self.assertIn("request", self.mgr.probe())
        self.finish()
        self.assertIn("request", self.mgr.restart())

    def test_warnings(self):
        self.mgr.settings({"settings": {"dhcp": {"enabled": True}}})
        self.mgr.apply()
        self.finish()
        json.dump({"ts": self.now, "ok": True, "servers": [{"server_id": "192.168.1.10", "offered": "192.168.1.8"}]},
                  open(self.mgr.path("probe.json"), "w"))
        warnings = " ".join(self.mgr.status()["warnings"])
        self.assertIn("feste Adresse", warnings)
        self.assertIn("weiterer DHCP-Server antwortet: 192.168.1.10", warnings)

    def test_static_ip(self):
        self.mgr.static_ip({"enabled": True})
        modify = next(c for c in self.sys.calls if c[:3] == ["nmcli", "connection", "modify"])
        self.assertEqual(modify[3], "HomeNet")
        self.assertIn("192.168.1.8/24", modify)
        self.assertIn("manual", modify)
        self.assertIn(["nmcli", "device", "reapply", "wlan0"], self.sys.calls)
        self.mgr.static_ip({"enabled": False})
        self.assertIn("auto", self.sys.calls[-2])

    def test_logs(self):
        self.sys.journal = ("1700000000.5 wol dnsmasq[9]: query[A] heise.de from 192.168.1.12\n"
                            "1700000001.5 wol dnsmasq[9]: 12 query[PTR] 9.1.168.192.in-addr.arpa from 192.168.1.12\n"
                            "1700000002.0 wol dnsmasq-dhcp[9]: 77 DHCPACK(wlan0) 192.168.1.9 00:d8:61:d7:1d:36 office\n")
        queries = self.mgr.logs({"kind": "queries"})["entries"]
        self.assertEqual([(q["type"], q["name"]) for q in queries], [("A", "heise.de"), ("PTR", "9.1.168.192.in-addr.arpa")])
        dhcp = self.mgr.logs({"kind": "dhcp"})["entries"]
        self.assertEqual(dhcp[0]["type"], "DHCPACK")
        with self.assertRaises(NetError):
            self.mgr.logs({"kind": "passwd"})


class JobRunner:
    def __init__(self):
        self.calls = []
        self.test_rc = 0
        self.active = True
        self.enabled = True

    def __call__(self, argv, timeout=60):
        self.calls.append(argv)
        if argv[0] == "dnsmasq":
            return self.test_rc, "" if self.test_rc == 0 else "dnsmasq: bad option at line 3"
        if argv[:2] == ["systemctl", "is-active"]:
            return (0 if self.active else 3), ""
        if argv[:2] == ["systemctl", "is-enabled"]:
            return (0 if self.enabled else 1), ""
        if argv[0] == "systemctl" and argv[1] in ("restart", "start"):
            self.active = True
        if argv[0] == "systemctl" and (argv[1] == "stop" or "--now" in argv):
            self.active = False
        if argv[0] == "journalctl":
            return 0, "dnsmasq: failed to bind DHCP server socket: Address in use"
        return 0, ""


class JobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        self.conf = os.path.join(self.tmp, "conf")
        for d in ("requests", "jobs", "staging"):
            os.makedirs(os.path.join(self.state, d))
        self.runner = JobRunner()
        self.answers = {"A": ["192.168.1.8"]}
        self.addresses = ["192.168.1.8"]

        def query(name, qtype, server, port, timeout):
            return {"rcode": 0, "status": "OK", "answers": [{"type": "A", "data": ip} for ip in self.answers["A"]]}

        self.job = dnsjob.Job(self.state, self.conf, os.path.join(self.tmp, "data"), runner=self.runner,
                              sleep=lambda s: None, query=query, prober=lambda iface, mac, t: [{"server_id": "192.168.1.10"}],
                              addresses=lambda iface: self.addresses)

    def request(self, action, **extra):
        rid = "%013d" % (len(os.listdir(os.path.join(self.state, "jobs"))) + len(os.listdir(os.path.join(self.state, "requests"))))
        data = dict(extra, id=rid, action=action, iface="wlan0")
        if action == "apply":
            staging = os.path.join(self.state, "staging", rid)
            os.makedirs(staging)
            for name, content in extra.pop("files", {}).items():
                open(os.path.join(staging, name), "w").write(content)
            data.update(staging=staging, enabled=extra.get("enabled", True), dns=True, port=53,
                        test_name="wol.home.arpa", expect="192.168.1.8")
            data.pop("files", None)
        json.dump(data, open(os.path.join(self.state, "requests", rid + ".json"), "w"))
        return rid

    def result(self, rid):
        return json.load(open(os.path.join(self.state, "jobs", rid + ".json")))

    def files(self, version):
        return {"dnsmasq.conf": "port=53\n# %s\n" % version, "hosts.dhcp": "# %s\n" % version, "hosts.dns": "# %s\n" % version}

    def test_apply_restart_then_reload(self):
        self.runner.active = False
        rid = self.request("apply", files=self.files("v1"))
        self.job.run()
        self.assertEqual(self.result(rid)["state"], "ok", self.result(rid))
        self.assertIn(["systemctl", "restart", "wol-dns.service"], self.runner.calls)
        self.assertEqual(open(os.path.join(self.conf, "hosts.dhcp")).read(), "# v1\n")
        self.assertEqual(os.listdir(os.path.join(self.state, "requests")), [])
        # nur Reservierungen geändert → SIGHUP statt Neustart
        self.runner.active = True
        self.runner.calls.clear()
        files = self.files("v1")
        files["hosts.dhcp"] = "# v2\n"
        rid = self.request("apply", files=files)
        self.job.run()
        self.assertIn("neu geladen", self.result(rid)["message"])
        self.assertIn(["systemctl", "reload", "wol-dns.service"], self.runner.calls)

    def test_rejected_config_is_rolled_back(self):
        self.job.run()
        self.request("apply", files=self.files("v1"))
        self.job.run()
        self.runner.test_rc = 1
        rid = self.request("apply", files=self.files("v2"))
        self.job.run()
        result = self.result(rid)
        self.assertEqual(result["state"], "failed")
        self.assertIn("vorherige Stand wurde wiederhergestellt", result["message"])
        self.assertIn("bad option", result["message"])
        self.assertEqual(open(os.path.join(self.conf, "dnsmasq.conf")).read(), "port=53\n# v1\n")

    def test_failed_self_test_rolls_back(self):
        self.request("apply", files=self.files("v1"))
        self.job.run()
        self.answers["A"] = []
        rid = self.request("apply", files=self.files("v2"))
        self.job.run()
        self.assertIn("Selbsttest", self.result(rid)["message"])
        self.assertEqual(open(os.path.join(self.conf, "hosts.dns")).read(), "# v1\n")

    def test_service_does_not_start(self):
        self.runner.active = False
        self.runner.calls.clear()
        rid = self.request("apply", files=self.files("v1"))
        self.runner.active = False
        orig = self.runner.__call__

        def never_active(argv, timeout=60):
            if argv[:2] == ["systemctl", "is-active"]:
                self.runner.calls.append(argv)
                return 3, ""
            return orig(argv, timeout)
        self.job.runner = never_active
        self.job.run()
        self.assertIn("startet", self.result(rid)["message"])
        self.assertIn("Address in use", self.result(rid)["message"])
        self.assertFalse(os.path.exists(os.path.join(self.conf, "dnsmasq.conf")))  # vorher gab es keine

    def test_disable_and_hotspot(self):
        rid = self.request("apply", files=self.files("v1"), enabled=False)
        self.job.run()
        self.assertEqual(self.result(rid)["message"], "DHCP und DNS ausgeschaltet")
        self.assertIn(["systemctl", "disable", "--no-reload", "--now", "wol-dns.service"], self.runner.calls)
        self.addresses = ["10.42.0.1"]
        rid = self.request("apply", files=self.files("v2"))
        self.job.run()
        self.assertIn("Hotspot", self.result(rid)["message"])
        rid = self.request("hotspot", hotspot=False)
        self.job.run()
        self.assertIn(["systemctl", "start", "wol-dns.service"], self.runner.calls)

    def test_staging_outside_state_is_refused(self):
        rid = "0000000000001"
        json.dump({"id": rid, "action": "apply", "staging": "/etc"}, open(os.path.join(self.state, "requests", rid + ".json"), "w"))
        self.job.run()
        self.assertIn("außerhalb", self.result(rid)["message"])

    def test_release_probe_unknown(self):
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        open(os.path.join(data, "dnsmasq.leases"), "w").write("1 00:11:22:33:44:55 192.168.1.40 x *\n2 00:d8:61:d7:1d:36 192.168.1.9 office *\n")
        rid = self.request("release", mac="00:11:22:33:44:55", ip="192.168.1.40")
        with unittest.mock.patch("shutil.which", return_value=None):
            self.job.run()
        self.assertEqual(self.result(rid)["state"], "ok")
        self.assertEqual(open(os.path.join(data, "dnsmasq.leases")).read(), "2 00:d8:61:d7:1d:36 192.168.1.9 office *\n")
        rid = self.request("probe", mac="b8:27:eb:4a:4f:f1")
        self.job.run()
        self.assertEqual(self.result(rid)["message"], "1 DHCP-Server gefunden")
        self.assertEqual(json.load(open(os.path.join(self.state, "probe.json")))["servers"][0]["server_id"], "192.168.1.10")
        rid = self.request("rm-rf")
        open(os.path.join(self.state, "requests", "kaputt.json"), "w").write("{nicht json")
        self.job.run()
        self.assertEqual(self.result(rid)["state"], "failed")
        self.assertEqual(os.listdir(os.path.join(self.state, "requests")), [])


class SocketApiTest(unittest.TestCase):
    def test_net_over_socket_and_api(self):
        from tests.test_api import ApiTest

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        fake = FakeNM(["Heim"])
        fake.address, fake.connected = CLIENT_IP, "Heim"
        mgr, _ = make_manager(fake, tmp)
        sysctl = Systemctl()
        net = netsvc.NetManager(tmp, conf_dir=os.path.join(tmp, "conf"), data_dir=os.path.join(tmp, "data"), runner=sysctl,
                                interfaces=lambda: [{"name": "wlan0", "kind": "wifi", "up": True, "address": "192.168.1.8",
                                                     "netmask": "255.255.255.0"}], arp=lambda: {}, user="nobody",
                                vendor_source=(None, None))
        net._gateway = lambda iface: "192.168.1.1"
        path = os.path.join(tmp, "control.sock")
        server = wifi.ControlServer(path, mgr, {os.getuid()}, None, net)
        server.bind()
        stop = threading.Event()
        self.addCleanup(stop.set)
        threading.Thread(target=server.serve, args=(stop,), daemon=True).start()

        api = ApiTest("test_login_flow")
        api.setUp()
        try:
            api.app.config["wifi_socket"] = path
            status, data = api.call("GET", "/api/net")
            self.assertEqual((status, data["dns_enabled"], data["pending"]), (200, False, []))
            status, data = api.call("POST", "/api/net/reservations", {"mac": "00:d8:61:d7:1d:36", "ip": "192.168.1.9", "name": "office"})
            self.assertEqual(status, 200, data)
            rid = data["reservation"]["id"]
            status, data = api.call("PUT", "/api/net/reservations/%s" % rid, {"mac": "00:d8:61:d7:1d:36", "ip": "192.168.1.19", "name": "office"})
            self.assertEqual(data["reservation"]["ip"], "192.168.1.19")
            status, data = api.call("POST", "/api/net/reservations", {"mac": "kaputt", "ip": "192.168.1.9"})
            self.assertEqual(status, 400)
            self.assertIn("MAC", data["error"])
            api.call("POST", "/api/devices", {"name": "Office", "mac": "00:D8:61:D7:1D:36"})
            status, data = api.call("GET", "/api/net/config")
            self.assertEqual(data["devices"]["00:d8:61:d7:1d:36"]["name"], "Office")
            self.assertEqual(data["pending"], ["Reservierung neu: office (192.168.1.19)"])
            status, data = api.call("PUT", "/api/net/settings", {"dns": {"enabled": True}})
            self.assertTrue(data["config"]["dns"]["enabled"])
            status, data = api.call("POST", "/api/net/apply")
            self.assertEqual(status, 200, data)
            # wol-dns-apply.service spielen
            for name in os.listdir(net.path("requests")):
                req = json.load(open(net.path("requests", name)))
                json.dump({"id": req["id"], "action": "apply", "state": "ok", "message": "ok", "ts": 1},
                          open(net.path("jobs", name), "w"))
                os.remove(net.path("requests", name))
            status, data = api.call("GET", "/api/net")
            self.assertTrue(data["dns_enabled"])
            status, events = api.call("GET", "/api/events")
            net_events = [e for e in events["events"] if e["action"] == "net"]
            self.assertEqual(len(net_events), 1)
            self.assertIn("DNS: aus → an", net_events[0]["message"])
            api.call("GET", "/api/net")  # nicht doppelt protokollieren
            self.assertEqual(len([e for e in api.call("GET", "/api/events")[1]["events"] if e["action"] == "net"]), 1)
            status, data = api.call("POST", "/api/net/import", {"text": DHCPD_CONF})
            self.assertEqual(status, 200)
            status, data = api.call("POST", "/api/net/import/commit", {"preview": data["preview"]})
            self.assertIn("neu", data["message"])
            status, data = api.call("GET", "/api/net/export")
            self.assertEqual(data["wol_server_network"], 1)
            status, data = api.call("POST", "/api/net/test", {"query": "office"})
            self.assertEqual(status, 400)  # kein dnsmasq im Test → Fehlermeldung statt Absturz
            status, data = api.call("GET", "/api/net/stats")
            self.assertFalse(data["available"])
            status, data = api.call("GET", "/api/net/logs/queries")
            self.assertEqual(data["entries"], [])
            self.assertEqual(api.call("GET", "/api/net/logs/passwd")[0], 404)
            api.app.config["wifi_socket"] = os.path.join(tmp, "nope.sock")
            self.assertEqual(api.call("GET", "/api/net")[0], 503)
        finally:
            api.tearDown()


import unittest.mock  # noqa: E402  (für patch in JobTest)

if __name__ == "__main__":
    unittest.main()
