#!/usr/bin/env python3
# WOL-Server - tools/demo.py
# Local demo for trying out and testing the web GUI, with invented sample data.
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

"""Demo of the WOL-Server web GUI - only for local GUI tests, never on the network.

The real web server and the real code of the root service (network manager, control socket, DNS job)
run with stand-ins for systemctl, nmcli and journalctl. Wi-Fi, VPN, plugs, pings, magic packets and
SSH are simulated, so the demo does not send anything into the real network. If dnsmasq is installed,
a real dnsmasq answers DNS on 127.0.0.1 (high port, no DHCP, no root), so test queries and statistics
are real. All names, addresses and MACs are invented.

    python3 tools/demo.py [--port 8090] [--dns-port 15353] [--no-dnsmasq]

Every start begins with fresh sample data in a temporary directory.
"""

import argparse
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.test_wifi import FakeNM  # noqa: E402 - the test double of NetworkManager
from wolserver import dnsjob, netcfg, netinfo, netsvc, power, status, vpn, wifi, wol  # noqa: E402
from wolserver.config import DEFAULTS  # noqa: E402
from wolserver.plugs import fake as fakeplug  # noqa: E402
from wolserver import server as server_module  # noqa: E402
from wolserver.server import App, create_server  # noqa: E402

parser = argparse.ArgumentParser(description="Local demo of the WOL-Server web GUI with invented data")
parser.add_argument("--port", type=int, default=8090, help="port of the web GUI (default 8090)")
parser.add_argument("--dns-port", type=int, default=15353, help="port of the demo dnsmasq (default 15353)")
parser.add_argument("--no-dnsmasq", action="store_true", help="do not start dnsmasq, even if it is installed")
args = parser.parse_args()

USE_DNSMASQ = not args.no_dnsmasq and shutil.which("dnsmasq") is not None
NOW = time.time()
RUN = tempfile.mkdtemp(prefix="wol-demo-")
for d in ("data", "state", "conf", "dns-data", "dnsmasq"):
    os.makedirs(os.path.join(RUN, d))
os.chdir(RUN)  # relative socket path (AF_UNIX paths are limited to 108 characters)

# --- the demo machine: host name, interfaces, Wi-Fi signal (no access to the real system) ---
IFACES = [
    {"name": "wlan0", "kind": "wifi", "up": True, "address": "192.168.1.8", "netmask": "255.255.255.0",
     "broadcast": "192.168.1.255"},
    {"name": "tun0", "kind": "vpn", "up": True, "address": "10.8.0.8", "netmask": "255.255.255.0", "broadcast": None},
]
socket.gethostname = lambda: "wol"
netinfo.interfaces = lambda: [dict(i) for i in IFACES]
netinfo.default_route_interfaces = lambda path=None: ["wlan0"]
netinfo.lan_source = lambda preferred="": dict(IFACES[0])
netinfo.wifi_signal = lambda path=None: {"wlan0": {"link": 56, "signal_dbm": -54}}
netinfo.connected_networks = lambda: [{"network": "192.168.1.0/24", "interface": "wlan0", "kind": "wifi"},
                                      {"network": "10.8.0.0/24", "interface": "tun0", "kind": "vpn"}]

# --- devices: simulated status, magic packets and sleep stay inside the demo ---
ONLINE = set()  # hosts without a plug that answer; devices with a plug answer while they run (see PLUGGED)


def check_host(host, timeout=1.0):
    for sim in PLUGGED.values():
        if sim["host"] == host:
            dev = fakeplug.FakePlug.devices[sim["plug"]]
            return dev.on and dev.load_w > 0
    return host in ONLINE


status.check_host = server_module.check_host = check_host


def fake_magic_packet(mac, broadcast="255.255.255.255", port=9, count=3, source_ip=None):
    wol.normalize_mac(mac)
    sim = PLUGGED.get(wol.normalize_mac(mac))
    if sim:
        sim["override"] = (time.time() + 7200, True)  # runs for two hours


def fake_sleep(device, identity, source_ip=None):
    ONLINE.discard(device.get("host"))
    sim = PLUGGED.get(device["mac"])
    if sim:
        sim["override"] = (time.time() + 8 * 3600, False)
    return True, "Sleep command sent to %s (simulated)" % device["name"]


# --- devices with a plug: when they run, what they draw ---
def office_runs(lt):
    hm = lt.tm_hour + lt.tm_min / 60
    if lt.tm_wday < 5:
        return 7.5 <= hm < 12.25 or 13 <= hm < 17.5 or (lt.tm_wday in (0, 2) and 19.5 <= hm < 21.5)
    return lt.tm_wday == 5 and 10 <= hm < 12


def nas_runs(lt):
    hm = lt.tm_hour + lt.tm_min / 60
    return 6.75 <= hm < 23.5


def backup_runs(lt):
    hm = lt.tm_hour + lt.tm_min / 60
    return 5.5 <= hm < 6.5 or 18 <= hm < 18.33


def backup_relay(lt):  # the schedules cut its standby at night
    hm = lt.tm_hour + lt.tm_min / 60
    return not (hm >= 23.75 or hm < 5.5)


PLUGGED = {  # MAC of the device -> simulation
    "00:D8:61:D7:1D:36": {"plug": "192.168.1.31", "host": "office-pc.home.arpa", "runs": office_runs,
                          "load": (64, 112)},
    "00:11:32:4C:19:E2": {"plug": "192.168.1.33", "host": "nas.home.arpa", "runs": nas_runs, "load": (16, 23)},
    "A8:B8:E0:07:4B:4E": {"plug": "192.168.1.34", "host": "192.168.1.10", "runs": backup_runs, "load": (38, 55),
                          "relay": backup_relay},
    "self": {"plug": "192.168.1.32", "host": "", "runs": lambda lt: True, "load": (0.9, 1.4)},  # the Pi Zero itself
}
for sim in PLUGGED.values():
    sim["override"] = None


def sim_power(sim, ts, rng, relay=True):
    """Power of a simulated device at ts: load while it runs, standby otherwise, 0 without power."""
    lt = time.localtime(ts)
    if not relay:
        return 0.0, False
    runs = sim["runs"](lt)
    lo, hi = sim["load"]
    return (rng.uniform(lo, hi) if runs else 0.0), runs


wol.send_magic_packet = fake_magic_packet
power.sleep_device = fake_sleep

SAMPLE_DHCPD = """
option domain-name "oldnet.local";
option domain-name-servers 192.168.1.1;
default-lease-time 600;
max-lease-time 7200;
subnet 192.168.1.0 netmask 255.255.255.0 {
    authoritative;
    deny unknown-clients;
    option ntp-servers 192.168.1.1;
    option routers 192.168.1.1;
    host repeaterLAN { hardware ethernet d2:ce:1e:f7:77:47; fixed-address 192.168.1.2; }
    host repeaterWLAN { hardware ethernet f6:b0:14:2d:b5:1f; fixed-address 192.168.1.3; }
    host office-pc { hardware ethernet 00:d8:61:d7:1d:36; fixed-address 192.168.1.9; }
    host Tablet-Ben { hardware ethernet ac:22:0b:20:87:e3; fixed-address 192.168.1.13; }
    host firestick { hardware ethernet 00:bb:3a:20:6b:a3; fixed-address 192.168.1.15; }
    host Phone-Anna { hardware ethernet 40:4d:7f:03:16:ab; fixed-address 192.168.1.21; }
    host CNC-Controller { hardware ethernet 00:22:43:b9:7a:59; fixed-address 192.168.1.24; }
    host Media-Pi { hardware ethernet b8:27:eb:34:5e:22; fixed-address 192.168.1.25; }
    host Camera { hardware ethernet fc:c2:de:1d:e7:89; fixed-address 192.168.1.26; }
    host photo-printer { hardware ethernet d8:49:2f:e9:a7:92; fixed-address 192.168.1.30; }
    host plug-office-pc { hardware ethernet a8:03:2a:05:54:db; fixed-address 192.168.1.31; }
    host plug-nas { hardware ethernet a8:03:2a:df:a7:70; fixed-address 192.168.1.33; }
    host plug-backup { hardware ethernet a8:03:2a:fc:d5:1a; fixed-address 192.168.1.34; }
    host sat-tv { hardware ethernet 00:05:9e:11:03:64; fixed-address 192.168.1.37; }
    host Workshop-PC { hardware ethernet 00:24:d7:9d:07:05; fixed-address 192.168.1.38; }
    host Printer { hardware ethernet 94:dd:f8:bd:83:30; fixed-address 192.168.1.40; }
    host wol { hardware ethernet b8:27:eb:4a:4f:f1; fixed-address 192.168.1.8; }
}
"""


class DemoSystem:
    """Stand-in for systemctl, nmcli and journalctl; remembers the state of wol-dns."""

    def __init__(self):
        self.dns_active = False
        self.dns_enabled = False
        self.method = "manual"
        self.dnsmasq = None
        t = NOW
        self.journal_lines = [
            "%.1f wol dnsmasq-dhcp[812]: 2041 client provides name: Annas-iPhone" % (t - 420),
            "%.1f wol dnsmasq-dhcp[812]: 2041 DHCPDISCOVER(wlan0) 5a:1f:3c:c6:e5:fd ignored" % (t - 419),
            "%.1f wol dnsmasq-dhcp[812]: 2042 client provides name: Annas-iPhone" % (t - 300),
            "%.1f wol dnsmasq-dhcp[812]: 2042 DHCPDISCOVER(wlan0) 5a:1f:3c:c6:e5:fd ignored" % (t - 299),
            "%.1f wol dnsmasq-dhcp[812]: 2050 client provides name: ShellyPlugMG3-A8032AD4E5F7" % (t - 3600),
            "%.1f wol dnsmasq-dhcp[812]: 2050 DHCPDISCOVER(wlan0) a8:03:2a:d4:e5:f7 ignored" % (t - 3599),
        ]
        for i, (name, client) in enumerate([("example.org", "192.168.1.9"), ("office-pc.home.arpa", "192.168.1.13"),
                                            ("connectivity-check.ubuntu.com", "192.168.1.9"),
                                            ("sat-tv.home.arpa", "192.168.1.15"), ("api.github.com", "192.168.1.9"),
                                            ("9.1.168.192.in-addr.arpa", "192.168.1.21")]):
            self.journal_lines.append("%.1f wol dnsmasq[812]: %d query[%s] %s from %s"
                                      % (t - 200 + i * 7, 900 + i, "PTR" if "arpa" in name else "A", name, client))
        for i, (kind, detail) in enumerate([("DHCPREQUEST", "192.168.1.9 00:d8:61:d7:1d:36"),
                                             ("DHCPACK", "192.168.1.9 00:d8:61:d7:1d:36 office-pc"),
                                             ("DHCPREQUEST", "192.168.1.21 40:4d:7f:03:16:ab"),
                                             ("DHCPACK", "192.168.1.21 40:4d:7f:03:16:ab phone-anna")]):
            self.journal_lines.append("%.1f wol dnsmasq-dhcp[812]: %d %s(wlan0) %s"
                                      % (t - 150 + i * 3, 3000 + i, kind, detail))

    def __call__(self, argv, timeout=15):
        if argv[0] == "systemctl":
            return self.systemctl(argv[1:])
        if argv[0] == "nmcli":
            if "GENERAL.CONNECTION" in argv:
                return 0, "HomeNet\n"
            if "ipv4.method" in argv and "show" in argv:
                return 0, self.method + "\n"
            if "modify" in argv and "ipv4.method" in argv:
                self.method = "manual" if "manual" in argv else "auto"
            return 0, ""
        if argv[0] == "journalctl":
            lines = self.journal_lines
            if "--grep" in argv:
                pattern = re.compile(argv[argv.index("--grep") + 1])
                lines = [line for line in lines if pattern.search(line)]
            return 0, "\n".join(sorted(lines)) + "\n"
        if argv[0] == "dnsmasq":
            if not USE_DNSMASQ:
                return 0, "dnsmasq: syntax check OK.\n"
            proc = subprocess.run(argv, capture_output=True, text=True, env=dict(os.environ, LC_ALL="C"))
            return proc.returncode, proc.stdout + proc.stderr
        return 0, ""

    def systemctl(self, args):
        unit_args = [a for a in args if not a.startswith("-")]
        cmd = unit_args[0] if unit_args else ""
        if cmd == "show":
            if "--value" in args:
                return 0, "inactive\n"
            return 0, "ActiveState=%s\nUnitFileState=%s\n" % ("active" if self.dns_active else "inactive",
                                                               "enabled" if self.dns_enabled else "disabled")
        if cmd == "is-active":
            return (0 if self.dns_active else 3), ""
        if cmd == "is-enabled":
            return (0 if self.dns_enabled else 1), ""
        if cmd == "enable":
            self.dns_enabled = True
            if "--now" in args:
                self.start_dnsmasq()
        elif cmd == "disable":
            self.dns_enabled = False
            if "--now" in args:
                self.stop_dnsmasq()
        elif cmd in ("start", "restart", "reload"):
            self.start_dnsmasq()
        elif cmd == "stop":
            self.stop_dnsmasq()
        return 0, ""

    def start_dnsmasq(self):
        """Real dnsmasq for DNS only: configuration without DHCP, on lo, without root."""
        self.stop_dnsmasq()
        src = os.path.join(RUN, "conf", "dnsmasq.conf")
        if not os.path.exists(src):
            return
        with open(src, encoding="utf-8") as f:
            lines = [line for line in f
                     if not line.startswith(("dhcp-", "log-dhcp", "enable-tftp", "tftp-", "user=", "interface="))]
        if not USE_DNSMASQ or not any(line.startswith("port=") and line.strip() != "port=0" for line in lines):
            self.dns_active = True  # without dnsmasq, or DHCP only: counts as running in the demo
            return
        lines.append("interface=lo\n")
        conf = os.path.join(RUN, "dnsmasq", "demo.conf")
        with open(conf, "w", encoding="utf-8") as f:
            f.writelines(lines)
        self.dnsmasq = subprocess.Popen(["dnsmasq", "--keep-in-foreground", "--conf-file=" + conf,
                                         "--pid-file=" + os.path.join(RUN, "dnsmasq", "pid")],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.3)
        self.dns_active = self.dnsmasq.poll() is None

    def stop_dnsmasq(self):
        if self.dnsmasq and self.dnsmasq.poll() is None:
            self.dnsmasq.terminate()
            self.dnsmasq.wait()
        self.dnsmasq = None
        self.dns_active = False


SYSTEM = DemoSystem()
ARP = {"192.168.1.1": "3c:a6:2f:81:f4:68", "192.168.1.10": "a8:b8:e0:07:4b:4e", "192.168.1.9": "00:d8:61:d7:1d:36",
       "192.168.1.21": "40:4d:7f:03:16:ab"}

net = netsvc.NetManager(os.path.join(RUN, "state"), conf_dir=os.path.join(RUN, "conf"),
                        data_dir=os.path.join(RUN, "dns-data"), runner=SYSTEM, interfaces=lambda: IFACES[:1],
                        arp=lambda: dict(ARP), user=os.environ.get("USER", "nobody"), arp_probe=lambda ips: set())
net._gateway = lambda iface: "192.168.1.1"
_server_state = net._server_state
net._server_state = lambda cfg: dict(_server_state(cfg), mac="b8:27:eb:4a:4f:f1")

# Initial state: import from the old DHCP server, DNS and DHCP on, one entry in the history
cfg = net.active()
cfg["server"]["port"] = args.dns_port
cfg["server"]["name"] = "wol"
preview = netcfg.annotate_import(cfg, netcfg.import_dhcpd(SAMPLE_DHCPD), "b8:27:eb:4a:4f:f1")
cfg, _ = netcfg.merge_import(cfg, preview)
cfg["dhcp"]["lease_time"] = 43200
cfg["dns"]["enabled"] = True
cfg["dhcp"]["enabled"] = True
cfg["records"] = [netcfg.validate_record(r) for r in (
    {"type": "host", "name": "router", "value": "192.168.1.1", "comment": "Router"},
    {"type": "host", "name": "backup", "value": "192.168.1.10", "comment": "Backup server (static IP)"},
    {"type": "alias", "name": "laser", "value": "printer"},
    {"type": "alias", "name": "tv", "value": "sat-tv"},
    {"type": "srv", "name": "_ipp._tcp", "value": "printer", "port": 631, "comment": "Printer for AirPrint"},
)]
for r in cfg["reservations"]:
    if r["name"] == "cnc-controller":
        r["enabled"] = False
        r["comment"] = "in storage"
cfg = netcfg.validate_config(cfg)

# Leases and lease events as written by dnsmasq
leases, events = [], []
for i, (mac, ip, host) in enumerate([
        ("00:d8:61:d7:1d:36", "192.168.1.9", "office-pc"), ("40:4d:7f:03:16:ab", "192.168.1.21", "iPhone"),
        ("00:bb:3a:20:6b:a3", "192.168.1.15", "amazon-fire"), ("94:dd:f8:bd:83:30", "192.168.1.40", "BRN0A1B2C3D4E5F"),
        ("00:05:9e:11:03:64", "192.168.1.37", "sat-tv"), ("a8:03:2a:05:54:db", "192.168.1.31", "ShellyPlugMG3-A8032A0554DB"),
        ("b8:27:eb:34:5e:22", "192.168.1.25", "LibreELEC"), ("ac:22:0b:20:87:e3", "192.168.1.13", "android-tablet")]):
    leases.append("%d %s %s %s *" % (NOW + 43200 - i * 3700, mac, ip, host))
    events.append("%d old %s %s %s" % (NOW - i * 913, mac, ip, host))
with open(os.path.join(RUN, "dns-data", "dnsmasq.leases"), "w", encoding="utf-8") as f:
    f.write("\n".join(leases) + "\n")
with open(os.path.join(RUN, "dns-data", "lease-events.log"), "w", encoding="utf-8") as f:
    f.write("\n".join(events) + "\n")

# Run the first apply right away through the real job path
JOB = dnsjob.Job(os.path.join(RUN, "state", "dns"), os.path.join(RUN, "conf"), os.path.join(RUN, "dns-data"),
                 runner=SYSTEM, sleep=lambda s: time.sleep(min(s, 0.3)),
                 prober=lambda iface, mac, timeout: (time.sleep(1.5), [{
                     "offered": "192.168.1.8", "server_id": "192.168.1.10", "router": "192.168.1.1",
                     "lease_time": 600, "source": "192.168.1.10"}])[1],
                 addresses=lambda iface: ["192.168.1.8"])
net._apply(cfg, client="192.168.1.12", from_draft=False, note="Import from the old DHCP server (192.168.1.10)")
JOB.run()
net.status()


def job_loop():
    while True:
        try:
            if os.listdir(os.path.join(RUN, "state", "dns", "requests")):
                time.sleep(0.8)  # as on the Pi: a job takes a moment
                JOB.run()
        except Exception as e:  # noqa: BLE001
            print("Job:", e, flush=True)
        time.sleep(0.4)


threading.Thread(target=job_loop, daemon=True).start()
net.settings({"settings": {"dhcp": {"lease_time": 86400}}})  # an open draft, so the apply bar is visible

# Wi-Fi and VPN (wol-wifi service with test doubles)
nm = FakeNM(["HomeNet"])
nm.address, nm.connected = "192.168.1.8", "HomeNet"
manager = wifi.WifiManager(dict(DEFAULTS, hotspot_ssid="WOL-Setup-3F7A"), os.path.join(RUN, "state"), runner=nm,
                           probe=lambda: nm.address, on_mode=net.wifi_changed)
vpn_manager = vpn.VpnManager(os.path.join(RUN, "state"), runner=lambda argv, timeout=10: (0, ""),
                             interfaces=lambda: IFACES[:1])
control = wifi.ControlServer("control.sock", manager, {os.getuid()}, vpn_manager, net)
control.bind()
stop = threading.Event()
threading.Thread(target=manager.run, args=(stop,), daemon=True).start()
threading.Thread(target=control.serve, args=(stop,), daemon=True).start()

# Simulated wireless plugs; the loads follow the devices above
fakeplug.register()
fakeplug.FakePlug.devices = {
    "192.168.1.31": fakeplug.FakeDevice(model="S3PL-30110EU", mac="a8:03:2a:05:54:db", password="demo", standby_w=1.3,
                                        temperature_c=34.6),
    "192.168.1.33": fakeplug.FakeDevice(model="S3PL-30110EU", mac="a8:03:2a:df:a7:70", password="demo", standby_w=3.2,
                                        temperature_c=29.1),
    "192.168.1.34": fakeplug.FakeDevice(model="S3PL-30110EU", mac="a8:03:2a:fc:d5:1a", standby_w=1.1,
                                        temperature_c=26.0),
    "192.168.1.32": fakeplug.FakeDevice(model="S3PL-30110EU", mac="a8:03:2a:3c:91:0e", password="demo",
                                        temperature_c=23.5),
    # The unknown Shelly in the overview: approving it gives it the next free address, then "Detect" finds it
    "192.168.1.11": fakeplug.FakeDevice(model="S3PL-30110EU", mac="a8:03:2a:d4:e5:f7"),
}
for dev, energy, count in zip(fakeplug.FakePlug.devices.values(), (15342.1, 9120.4, 412.7, 980.3), (312, 87, 41, 2)):
    dev.energy_wh, dev.switch_count = energy, count

app = App(dict(DEFAULTS, listen="127.0.0.1", port=args.port, data_dir=os.path.join(RUN, "data"),
               wifi_socket="control.sock"))
app.status._checker = status.check_host
device_ids = {}
for name, mac, host, method in (("Office PC", "00:D8:61:D7:1D:36", "office-pc.home.arpa", "none"),
                                ("Backup-Server", "A8:B8:E0:07:4B:4E", "192.168.1.10", "none"),
                                ("NAS", "00:11:32:4C:19:E2", "nas.home.arpa", "ssh"),
                                ("Media server", "DC:A6:32:5E:07:3B", "media-pi.home.arpa", "none")):
    device_ids[name] = app.store.add("devices", {
        "name": name, "mac": mac, "host": host, "broadcast": "192.168.1.255", "wol_port": 9, "sleep_method": method,
        "ssh_user": "admin" if method == "ssh" else "", "ssh_port": 22,
        "ssh_command": "sudo systemctl suspend" if method == "ssh" else "", "http_method": "POST", "http_url": "",
        "http_body": ""})["id"]
plug_ids = {}
for host, name, consumer, password, options in (
        ("192.168.1.31", "plug-office-pc", "Office PC", "demo", {"running_w": 30}),
        ("192.168.1.33", "plug-nas", "NAS", "demo", {"running_w": 10}),
        ("192.168.1.34", "plug-backup", "Backup-Server", "", {"running_w": 20, "wake_method": "power"}),
        ("192.168.1.32", "plug-wol", None, "demo", {"consumer_self": True, "standby_w": 0.5})):
    plug_ids[name] = app.plugs.add(dict(options, host=host, name=name, password=password,
                                        consumer_device=device_ids.get(consumer)))["id"]
app.plugs.set_settings({"price_kwh": 0.32, "currency": "€"})


def seed_energy(days=90):
    """Energy history of the last days, the curve of the last 24 hours, and the live loads from now on."""
    rng = random.Random(7)
    energy = app.plugs.energy
    now = time.time()
    rows = []
    for sim in PLUGGED.values():
        plug_id = next(p["id"] for p in app.plugs.list(live=False) if p["host"] == sim["plug"])
        dev = fakeplug.FakePlug.devices[sim["plug"]]
        start = int((now - days * 86400) // 3600 * 3600)
        for hour in range(start, int(now), 3600):
            watts = []
            for minute in range(0, 60, 5):
                ts = hour + minute * 60
                if ts >= now:
                    break
                relay = sim.get("relay", lambda lt: True)(time.localtime(ts))
                load, _ = sim_power(sim, ts, rng, relay)
                watts.append(load or (dev.standby_w * rng.uniform(0.92, 1.08) if relay else 0.0))
            wh = sum(watts) / 12  # 5-minute values
            rows.append((plug_id, hour, round(wh, 3), round(max(watts), 1), round(24 + max(watts) * 0.09, 1)))
            if hour >= now - 86400:
                for minute in range(0, min(60, int((now - hour) // 60))):
                    ts = hour + minute * 60
                    relay = sim.get("relay", lambda lt: True)(time.localtime(ts))
                    load, _ = sim_power(sim, ts, rng, relay)
                    power = load or (dev.standby_w * rng.uniform(0.95, 1.05) if relay else 0.0)
                    energy._samples[plug_id].append([ts // 60 * 60, power, 1])
        with dev.lock:
            dev.tick()
        energy._last[plug_id] = (now, dev.energy_wh)
    with energy._db:
        energy._db.executemany("INSERT INTO hours (plug, hour, wh, max_w, max_temp) VALUES (?, ?, ?, ?, ?)", rows)


def simulate():
    """Every 20 s: the devices with a plug draw what they do at this time of day (or after WOL/sleep)."""
    rng = random.Random()
    while True:
        now = time.time()
        for sim in PLUGGED.values():
            dev = fakeplug.FakePlug.devices[sim["plug"]]
            override = sim["override"]
            if override and override[0] < now:
                override = sim["override"] = None
            lo, hi = sim["load"]
            runs = override[1] if override else sim["runs"](time.localtime(now))
            with dev.lock:
                dev.tick()
                dev.load_w = rng.uniform(lo, hi) if runs else 0.0
                dev.temperature_c = round(24 + dev.power_w * 0.09 + rng.uniform(-0.3, 0.3), 1)
        time.sleep(20)


for sim in PLUGGED.values():
    if "relay" in sim:
        fakeplug.FakePlug.devices[sim["plug"]].on = sim["relay"](time.localtime())
seed_energy()
threading.Thread(target=simulate, daemon=True).start()
for target, action, at, days, label in (
        (("device_id", device_ids["NAS"]), "wake", "06:45", [0, 1, 2, 3, 4], ""),
        (("device_id", device_ids["Office PC"]), "wake", "07:30", [0, 1, 2, 3, 4], ""),
        (("device_id", device_ids["NAS"]), "sleep", "23:30", list(range(7)), ""),
        (("plug_id", plug_ids["plug-backup"]), "power_off", "23:45", list(range(7)), "cut standby"),
        (("plug_id", plug_ids["plug-backup"]), "power_on", "05:30", list(range(7)), "")):
    app.store.add("schedules", {target[0]: target[1], "action": action, "time": at, "days": days, "enabled": True,
                                "label": label, **({"force": False} if target[0] == "plug_id" else {})})
app.start()
server = create_server(app)


def shutdown(*_):
    SYSTEM.stop_dnsmasq()
    shutil.rmtree(RUN, ignore_errors=True)
    os._exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
print("WOL-Server demo: http://127.0.0.1:%d/  (data in %s, %s)" % (
    args.port, RUN, "DNS on 127.0.0.1:%d" % args.dns_port if USE_DNSMASQ else "DNS simulated"), flush=True)
print("Stop with Ctrl+C.", flush=True)
server.serve_forever()
