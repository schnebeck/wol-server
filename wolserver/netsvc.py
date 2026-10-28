# WOL-Server - wolserver/netsvc.py
# Manages DHCP and DNS: drafts, jobs, history and leases.
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

"""Manage DHCP and DNS - part of the root service wol-wifi (without capabilities).

The GUI edits a draft (draft.json). "Apply" renders the dnsmasq files from it into a staging
area and drops a job into requests/. wol-dns-apply.service (root) checks, installs, restarts
dnsmasq and tests; only after success does the draft become the active state (network.json)
and enter the History. That way an error has no consequences for the network.
"""
import copy
import ipaddress
import json
import logging
import os
import pwd
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid

from . import netcfg, netinfo, oui
from .netcfg import NetError

log = logging.getLogger("wolserver.netsvc")

UNIT = "wol-dns.service"
JOB_UNIT = "wol-dns-apply.service"
HOTSPOT_ADDRESS = "10.42.0.1"
HISTORY_KEEP = 20
JOBS_KEEP = 30


def _run(argv, timeout=15):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ, LC_ALL="C"))
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def _read_json(path):
    text = _read(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _write_json(path, data, mode=0o600):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _prefix(info):
    try:
        return ipaddress.IPv4Network("0.0.0.0/%s" % info["netmask"]).prefixlen
    except (KeyError, TypeError, ValueError):
        return 24


def read_arp(path="/proc/net/arp"):
    """{ip: mac} of the neighbours with a valid entry (flags 0x2)."""
    result = {}
    for line in (_read(path, "") or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[2] != "0x0" and parts[3] != "00:00:00:00:00:00":
            result[parts[0]] = parts[3].lower()
    return result


PROBE_FREE_TTL = 600  # s: an address found free is checked again after this
PROBE_USED_TTL = 86400  # s: an address that answered stays taken for suggestions


def probe_arp(ips, arp=read_arp, wait=1.0):
    """The addresses among ips that answer ARP. A device with a static address (no reservation, no lease)
    answers too, even if its firewall drops everything: one UDP datagram is enough to make the kernel ask."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for ip in ips:
            try:
                sock.sendto(b"", (ip, 9))  # discard port: only the ARP request matters
            except OSError:
                pass
    time.sleep(wait)
    found = arp()
    return {ip for ip in ips if ip in found}


class NetManager:
    def __init__(self, state_dir, conf_dir="/etc/wol-dns", data_dir="/var/lib/wol-dns", runner=_run,
                 clock=time.time, interfaces=netinfo.interfaces, arp=read_arp, journal=None, user=None,
                 vendor_source=None, arp_probe=None):
        self.dir = os.path.join(state_dir, "dns")
        self.conf_dir = conf_dir
        self.data_dir = data_dir
        self.runner = runner
        self.clock = clock
        self.interfaces = interfaces
        self.arp = arp
        self.journal = journal or self._journal
        self.arp_probe = arp_probe or (lambda ips: probe_arp(ips, self.arp))
        self._probed = {}  # ip -> (time, answered)
        self.user = user
        self.vendor_source = vendor_source
        self.lock = threading.RLock()
        for sub in ("", "requests", "jobs", "staging", "applying", "history"):
            os.makedirs(os.path.join(self.dir, sub), mode=0o700, exist_ok=True)
        self.wifi_mode = None
        self._unknown_cache = (0.0, None)
        self._syncing = False

    # -- Files --

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def _dnsmasq_user(self):
        if self.user:
            return self.user
        for name in ("dnsmasq", "nobody"):
            try:
                pwd.getpwnam(name)
                return name
            except KeyError:
                continue
        return "nobody"

    def _iface_info(self, iface):
        for i in self.interfaces():
            if i["name"] == iface and i["address"]:
                return i
        return None

    def _defaults(self):
        cfg = copy.deepcopy(netcfg.DEFAULT_NETWORK)
        info = self._iface_info(cfg["server"]["interface"])
        if info and info["address"] != HOTSPOT_ADDRESS:
            cfg["server"]["ip"] = info["address"]
            cfg["server"]["prefix"] = _prefix(info)
        gateway = self._gateway(cfg["server"]["interface"])
        if gateway:
            cfg["dhcp"]["gateway"] = gateway
            cfg["dns"]["upstream"] = [gateway]
            cfg["dhcp"]["ntp"] = [gateway]
        try:
            import socket
            cfg["server"]["name"] = netcfg.normalize_name(socket.gethostname()) or "wol"
        except OSError:
            pass
        return netcfg.validate_config(cfg)

    def _gateway(self, iface):
        try:
            with open("/proc/net/route", encoding="ascii") as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if len(parts) > 2 and parts[0] == iface and parts[1] == "00000000":
                        return str(ipaddress.IPv4Address(bytes.fromhex(parts[2])[::-1]))
        except (OSError, ValueError):
            pass
        return None

    def active(self):
        if not self._syncing:
            self._sync()  # merge successful applies first
        cfg = _read_json(self.path("network.json"))
        if cfg is None:
            return self._defaults()
        try:
            return netcfg.validate_config(cfg)
        except NetError as e:
            log.error("network.json invalid (%s) - using defaults", e)
            return self._defaults()

    def draft(self):
        if not self._syncing:
            self._sync()  # a successful apply of the draft closes it
        cfg = _read_json(self.path("draft.json"))
        if cfg is None:
            return None
        try:
            return netcfg.validate_config(cfg)
        except NetError:
            return None

    def working(self):
        return self.draft() or self.active()

    def _save_draft(self, cfg):
        cfg = netcfg.validate_config(cfg)
        if cfg == self.active():
            self._drop("draft.json")
        else:
            _write_json(self.path("draft.json"), cfg)
        return cfg

    def _drop(self, *parts):
        try:
            os.remove(self.path(*parts))
        except FileNotFoundError:
            pass

    # -- Jobs for wol-dns-apply.service --

    def _request(self, action, **extra):
        request_id = "%013d-%s" % (int(self.clock() * 1000), uuid.uuid4().hex[:6])
        data = dict(extra, id=request_id, action=action, ts=self.clock(),
                    iface=self.active()["server"]["interface"])
        _write_json(self.path("requests", request_id + ".json"), data)
        return request_id

    def _jobs(self):
        jobs = []
        for name in sorted(os.listdir(self.path("jobs"))):
            if name.endswith(".json"):
                job = _read_json(self.path("jobs", name))
                if job:
                    jobs.append(job)
        return jobs

    def _pending_requests(self):
        return sorted(n[:-5] for n in os.listdir(self.path("requests")) if n.endswith(".json"))

    def _sync(self):
        """Merge the results of finished jobs: successful applies become active."""
        with self.lock:
            if self._syncing:
                return
            self._syncing = True
            try:
                self._sync_jobs()
            finally:
                self._syncing = False

    def _sync_jobs(self):
        # clean up old job results
        names = sorted(n for n in os.listdir(self.path("jobs")) if n.endswith(".json"))
        for name in names[:-JOBS_KEEP]:
            self._drop("jobs", name)
        if not os.listdir(self.path("applying")):
            return
        jobs = {j["id"]: j for j in self._jobs()}
        for name in sorted(os.listdir(self.path("applying"))):
            if not name.endswith(".json"):
                continue
            request_id = name[:-5]
            job = jobs.get(request_id)
            if job is None or job.get("state") == "running":
                continue
            applying = _read_json(self.path("applying", name)) or {}
            if job.get("state") == "ok" and applying.get("config"):
                old = self.active()
                new = netcfg.validate_config(applying["config"])
                _write_json(self.path("network.json"), new)
                self._history_add(old, new, dict(applying, request=request_id))
                draft = self.draft()
                if draft is not None and applying.get("from_draft") and draft == new:
                    self._drop("draft.json")
                log.info("Network configuration applied (%s)", request_id)
                self._own_resolver(new)
            self._drop("applying", name)
            shutil.rmtree(self.path("staging", request_id), ignore_errors=True)

    def _job_state(self):
        pending = self._pending_requests()
        jobs = self._jobs()
        last = jobs[-1] if jobs else None
        running = bool(pending) or bool(last and last.get("state") == "running")
        if last and last.get("state") == "running":
            rc, out = self.runner(["systemctl", "show", "-p", "ActiveState", "--value", JOB_UNIT])
            if rc == 0 and out.strip() not in ("activating", "active") and not pending:
                last = dict(last, state="failed", message="Aborted (reboot?). Please try again.")
                running = False
        return running, last, pending

    def _busy_check(self):
        running, _, _ = self._job_state()
        if running:
            raise NetError("A change is already in progress. Please wait a moment.")

    def _apply(self, cfg, client="", from_draft=True, note=""):
        cfg = netcfg.validate_config(cfg)
        self._busy_check()
        old = self.active()
        changes = netcfg.diff(old, cfg)
        if note:
            changes.insert(0, note)
        files = netcfg.render(cfg, self.conf_dir, self.data_dir, self._dnsmasq_user())
        request_id = "%013d-%s" % (int(self.clock() * 1000), uuid.uuid4().hex[:6])
        staging = self.path("staging", request_id)
        os.makedirs(staging, mode=0o700, exist_ok=True)
        for name, content in files.items():
            with open(os.path.join(staging, name), "w", encoding="utf-8") as f:
                f.write(content)
        _write_json(self.path("applying", request_id + ".json"),
                    {"config": cfg, "client": client, "changes": changes, "from_draft": from_draft})
        enabled = cfg["dns"]["enabled"] or cfg["dhcp"]["enabled"]
        data = {"id": request_id, "action": "apply", "ts": self.clock(), "iface": cfg["server"]["interface"],
                "staging": staging, "enabled": enabled, "dns": cfg["dns"]["enabled"],
                "port": cfg["server"]["port"], "test_name": "%s.%s" % (cfg["server"]["name"], cfg["dns"]["domain"]),
                "expect": cfg["server"]["ip"]}
        _write_json(self.path("requests", request_id + ".json"), data)
        log.info("Network change requested (%s): %s", request_id, "; ".join(changes) or "unchanged")
        return request_id, changes

    def _history_add(self, old, new, applying):
        entry = {"id": uuid.uuid4().hex[:10], "ts": self.clock(), "client": applying.get("client", ""),
                 "request": applying.get("request"), "changes": applying.get("changes") or netcfg.diff(old, new),
                 "config": new}
        _write_json(self.path("history", "%013d-%s.json" % (int(self.clock() * 1000), entry["id"])), entry)
        names = sorted(os.listdir(self.path("history")))
        for name in names[:-HISTORY_KEEP]:
            self._drop("history", name)

    # -- Data for the GUI --

    def _leases(self):
        text = _read(os.path.join(self.data_dir, "dnsmasq.leases"), "")
        return netcfg.parse_leases(text, self.clock())

    def _events(self):
        return netcfg.parse_lease_events(_read(os.path.join(self.data_dir, "lease-events.log"), ""))

    def _journal(self, args):
        rc, out = self.runner(["journalctl", "-u", UNIT, "-o", "short-unix", "--no-pager"] + args, timeout=20)
        return out if rc == 0 else ""

    def _nm_connection(self, iface):
        rc, out = self.runner(["nmcli", "-t", "-g", "GENERAL.CONNECTION", "device", "show", iface])
        name = out.strip() if rc == 0 else ""
        if not name or name == "--":
            return None, None
        rc, out = self.runner(["nmcli", "-t", "-g", "ipv4.method", "connection", "show", name])
        return name, (out.strip() if rc == 0 else None)

    def _server_state(self, cfg):
        iface = cfg["server"]["interface"]
        info = self._iface_info(iface)
        mac = (_read("/sys/class/net/%s/address" % iface, "") or "").strip().lower() or None
        connection, method = self._nm_connection(iface)
        return {"interface": iface, "address": info["address"] if info else None,
                "prefix": _prefix(info) if info else None, "mac": mac,
                "connection": connection, "method": method, "hotspot": bool(info and info["address"] == HOTSPOT_ADDRESS)}

    def _service(self):
        rc, out = self.runner(["systemctl", "show", "-p", "ActiveState,UnitFileState", UNIT])
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line) if rc == 0 else {}
        return {"state": props.get("ActiveState") or "unknown", "enabled": props.get("UnitFileState") == "enabled"}

    def status(self, args=None):
        with self.lock:
            self._sync()
            active = self.active()
            draft = self.draft()
            running, job, pending = self._job_state()
            server = self._server_state(active)
            leases = self._leases()
            unknown = self._unknown(active, leases)
            probe = _read_json(self.path("probe.json"))
            warnings = []
            if server["hotspot"]:
                warnings.append("Setup hotspot active: DHCP and DNS are paused.")
            if active["dhcp"]["enabled"] and server["method"] == "auto":
                warnings.append("DHCP is enabled, but the WOL-Server obtains its own address via DHCP. "
                                "Set a static IP address under Settings → Server.")
            if active["dhcp"]["enabled"] and server["address"] and server["address"] != active["server"]["ip"]:
                warnings.append("Current address of the WOL-Server: %s, configured: %s." % (server["address"], active["server"]["ip"]))
            if probe and probe.get("servers"):
                others = [s for s in probe["servers"] if (s.get("server_id") or s.get("source")) != active["server"]["ip"]]
                if others and active["dhcp"]["enabled"]:
                    warnings.append("Another DHCP server on the network: %s. Two DHCP servers interfere with each other."
                                    % ", ".join(s.get("server_id") or s.get("source") for s in others))
            last_change = None
            names = sorted(os.listdir(self.path("history")))
            if names:
                entry = _read_json(self.path("history", names[-1])) or {}
                last_change = {k: entry.get(k) for k in ("id", "ts", "client", "request", "changes")}
            return {
                "last_change": last_change,
                "service": self._service(),
                "server": server,
                "dns_enabled": active["dns"]["enabled"],
                "dhcp_enabled": active["dhcp"]["enabled"],
                "mode": active["dhcp"]["mode"],
                "domain": active["dns"]["domain"],
                "lease_time": active["dhcp"]["lease_time"],
                "pending": netcfg.diff(active, draft) if draft else [],
                "running": running,
                "job": job,
                "queued": len(pending),
                "counts": {"reservations": len(active["reservations"]), "records": len(active["records"]),
                           "leases": len(leases), "unknown": len(unknown)},
                "unknown": unknown[:5],
                "probe": probe,
                "warnings": warnings,
            }

    def config(self, args=None):
        with self.lock:
            self._sync()
            active = self.active()
            draft = self.draft()
            # While a job is running, the GUI already shows the requested state
            cfg = draft or self._applying() or active
            leases = self._leases()
            events = self._events()
            macs = {r["mac"] for r in cfg["reservations"]} | {lease["mac"] for lease in leases}
            arp = self.arp()
            return {
                "config": cfg,
                "pending": netcfg.diff(active, draft) if draft else [],
                "meta": {
                    "vendors": oui.lookup_many(sorted(macs), self.vendor_source),
                    "last_seen": {mac: e["last_seen"] for mac, e in events.items()},
                    "leases": {lease["mac"]: lease["ip"] for lease in leases},
                    "online": sorted(set(arp.values())),
                    "next_ip": (self._free_ips(cfg, leases + [{"ip": ip} for ip in arp], 1) or [None])[0],
                    "lease_presets": list(netcfg.LEASE_PRESETS),
                    "network": str(netcfg.network_of(cfg)) if netcfg.network_of(cfg) else None,
                },
            }

    def _applying(self):
        names = sorted(n for n in os.listdir(self.path("applying")) if n.endswith(".json"))
        applying = _read_json(self.path("applying", names[-1])) if names else None
        try:
            return netcfg.validate_config(applying["config"]) if applying else None
        except (NetError, KeyError, TypeError):
            return None

    def leases(self, args=None):
        with self.lock:
            cfg = self.working()
            leases = self._leases()
            events = self._events()
            by_mac = {r["mac"]: r for r in cfg["reservations"]}
            vendors = oui.lookup_many([lease["mac"] for lease in leases], self.vendor_source)
            out = []
            for lease in leases:
                res = by_mac.get(lease["mac"])
                out.append(dict(lease, vendor=vendors.get(lease["mac"], ""), reservation=res["id"] if res else None,
                                name=(res["name"] if res and res["name"] else lease["hostname"]),
                                last_seen=events.get(lease["mac"], {}).get("last_seen") or None,
                                private=netcfg.is_private_mac(lease["mac"])))
            out.sort(key=lambda x: tuple(int(p) for p in x["ip"].split(".")) if "." in x["ip"] else (999,))
            return {"leases": out, "now": self.clock()}

    def _unknown(self, cfg, leases=()):
        known = {r["mac"] for r in cfg["reservations"] if r["enabled"]}
        ignored = {i["mac"] for i in cfg["ignored"]}
        stamp, found = self._unknown_cache
        if found is None or self.clock() - stamp > 30:
            found = netcfg.parse_unknown(self.journal(["--since", "-24h", "-n", "5000"]))
            self._unknown_cache = (self.clock(), found)
        items = [u for mac, u in found.items() if mac not in known and mac not in ignored]
        if not items:
            return []
        vendors = oui.lookup_many([u["mac"] for u in items], self.vendor_source)
        arp_ips = list(self.arp())
        taken = list(leases) + [{"ip": ip} for ip in arp_ips]
        free = self._free_ips(cfg, taken, len(items))
        suggestion_cfg = copy.deepcopy(cfg)
        result = []
        for i, u in enumerate(sorted(items, key=lambda x: -x["last"])):
            ip = free[i] if i < len(free) else None
            name = netcfg.unique_name(suggestion_cfg, u["name"] or vendors.get(u["mac"]) or "geraet")
            suggestion_cfg["reservations"].append({"id": "x", "mac": u["mac"], "ip": ip or "", "name": name})
            result.append(dict(u, vendor=vendors.get(u["mac"], ""), private=netcfg.is_private_mac(u["mac"]),
                               suggested_name=name, suggested_ip=ip))
        return result

    def _free_ips(self, cfg, taken, count):
        """Up to count free addresses for suggestions. Free in the configuration is not enough: a device with a
        static address has neither reservation nor lease, so the candidates are checked on the network first."""
        now = self.clock()
        self._probed = {ip: (ts, used) for ip, (ts, used) in self._probed.items()
                        if now - ts < (PROBE_USED_TTL if used else PROBE_FREE_TTL)}
        taken = list(taken) + [{"ip": ip} for ip, (_, used) in self._probed.items() if used]
        found = []
        for _ in range(5):
            found, candidates = [], list(taken)
            while len(found) < count:
                ip = netcfg.next_free_ip(cfg, candidates)
                if not ip:
                    break
                found.append(ip)
                candidates.append({"ip": ip})
            unchecked = [ip for ip in found if ip not in self._probed]
            if not unchecked:
                break
            used = self.arp_probe(unchecked)
            for ip in unchecked:
                self._probed[ip] = (now, ip in used)
            if not used:
                break
            taken += [{"ip": ip} for ip in used]
        return [ip for ip in found if not self._probed.get(ip, (0, False))[1]]

    def unknown(self, args=None):
        with self.lock:
            return {"unknown": self._unknown(self.working(), self._leases())}

    # -- Changes: effective immediately without an open draft --

    def _change(self, mutate, client, note=""):
        """Apply a change immediately. If a draft is open (import, backup, history), it goes there."""
        draft = self.draft()
        if draft is not None:
            self._save_draft(mutate(draft))
            return {"ok": True, "draft": True, "message": "Saved in draft"}
        active = self.active()
        new = netcfg.validate_config(mutate(copy.deepcopy(active)))
        if new == active:
            return {"ok": True, "message": "No change"}
        request_id, changes = self._apply(new, client, from_draft=False, note=note)
        return {"ok": True, "request": request_id, "changes": changes, "message": "Applying …"}

    def settings(self, args):
        with self.lock:
            data = args.get("settings") or {}
            if args.get("preview"):
                base = self.working()
                return {"ok": True, "changes": netcfg.diff(base, netcfg.validate_settings(base, data)),
                        "draft": self.draft() is not None}
            return self._change(lambda cfg: netcfg.validate_settings(cfg, data), args.get("client", ""))

    def reservation_save(self, args):
        with self.lock:
            data = args.get("reservation") or {}
            item = netcfg.validate_reservation(data, netcfg.network_of(self.working()))
            rid = data.get("id")

            def mutate(cfg):
                if rid:
                    if not any(r["id"] == rid for r in cfg["reservations"]):
                        raise NetError("Reservation not found")
                    cfg["reservations"] = [item if r["id"] == rid else r for r in cfg["reservations"]]
                else:
                    cfg["reservations"].append(item)
                # stop ignoring an approved MAC
                cfg["ignored"] = [i for i in cfg["ignored"] if i["mac"] != item["mac"]]
                return cfg

            return dict(self._change(mutate, args.get("client", "")), reservation=item)

    def reservation_delete(self, args):
        with self.lock:
            def mutate(cfg):
                before = len(cfg["reservations"])
                cfg["reservations"] = [r for r in cfg["reservations"] if r["id"] != args.get("id")]
                if len(cfg["reservations"]) == before:
                    raise NetError("Reservation not found")
                return cfg

            return self._change(mutate, args.get("client", ""))

    def record_save(self, args):
        with self.lock:
            data = args.get("record") or {}
            item = netcfg.validate_record(data)
            rid = data.get("id")

            def mutate(cfg):
                if rid:
                    if not any(r["id"] == rid for r in cfg["records"]):
                        raise NetError("Record not found")
                    cfg["records"] = [item if r["id"] == rid else r for r in cfg["records"]]
                else:
                    cfg["records"].append(item)
                return cfg

            return dict(self._change(mutate, args.get("client", "")), record=item)

    def record_delete(self, args):
        with self.lock:
            def mutate(cfg):
                before = len(cfg["records"])
                cfg["records"] = [r for r in cfg["records"] if r["id"] != args.get("id")]
                if len(cfg["records"]) == before:
                    raise NetError("Record not found")
                return cfg

            return self._change(mutate, args.get("client", ""))

    # -- Draft (import, backup, history) --

    def discard(self, args=None):
        with self.lock:
            self._drop("draft.json")
            return {"ok": True}

    def apply(self, args=None):
        args = args or {}
        with self.lock:
            draft = self.draft()
            if draft is None:
                raise NetError("There are no changes to apply.")
            request_id, changes = self._apply(draft, args.get("client", ""))
            return {"ok": True, "request": request_id, "changes": changes,
                    "message": ("Applying one change …" if len(changes) == 1
                                else "Applying %d changes …" % len(changes))}

    def _apply_direct(self, mutate, client, note):
        """Change to the active state that takes effect immediately (approve, ignore); an open draft gets it too."""
        active = self.active()
        new = mutate(copy.deepcopy(active))
        request_id, changes = self._apply(new, client, from_draft=False, note=note)
        draft = self.draft()
        if draft is not None:
            try:
                self._save_draft(mutate(draft))
            except NetError:
                pass
        return request_id, changes

    def approve(self, args):
        with self.lock:
            mac = netcfg._mac(args.get("mac"))
            item = netcfg.validate_reservation({"mac": mac, "ip": args.get("ip"), "name": args.get("name"),
                                                "comment": args.get("comment") or "approved on %s" % time.strftime("%d.%m.%Y"),
                                                "enabled": True}, netcfg.network_of(self.active()))

            def mutate(cfg):
                cfg["reservations"] = [r for r in cfg["reservations"] if r["mac"] != mac] + [item]
                cfg["ignored"] = [i for i in cfg["ignored"] if i["mac"] != mac]
                return netcfg.validate_config(cfg)

            request_id, _ = self._apply_direct(mutate, args.get("client", ""), "Approved: %s (%s)" % (item["name"], item["ip"]))
            return {"ok": True, "request": request_id, "reservation": item,
                    "message": "%s is approved and gets %s on its next request." % (item["name"] or mac, item["ip"])}

    def ignore(self, args):
        with self.lock:
            mac = netcfg._mac(args.get("mac"))
            entry = {"mac": mac, "comment": netcfg._text(args.get("comment"), "Comment", 120), "ts": self.clock()}
            cfg = self.working()
            cfg["ignored"] = [i for i in cfg["ignored"] if i["mac"] != mac] + [entry]
            # Ignoring only affects the list in the GUI - save directly without touching dnsmasq
            active = self.active()
            active["ignored"] = [i for i in active["ignored"] if i["mac"] != mac] + [entry]
            _write_json(self.path("network.json"), netcfg.validate_config(active))
            if self.draft() is not None:
                self._save_draft(cfg)
            return {"ok": True}

    def unignore(self, args):
        with self.lock:
            mac = netcfg._mac(args.get("mac"))
            active = self.active()
            active["ignored"] = [i for i in active["ignored"] if i["mac"] != mac]
            _write_json(self.path("network.json"), netcfg.validate_config(active))
            draft = self.draft()
            if draft is not None:
                draft["ignored"] = [i for i in draft["ignored"] if i["mac"] != mac]
                self._save_draft(draft)
            return {"ok": True}

    def release(self, args):
        with self.lock:
            mac = netcfg._mac(args.get("mac"))
            lease = next((x for x in self._leases() if x["mac"] == mac), None)
            if lease is None:
                raise NetError("There is no lease for %s." % mac)
            self._busy_check()
            request_id = self._request("release", mac=mac, ip=lease["ip"])
            return {"ok": True, "request": request_id, "message": "Releasing lease of %s …" % lease["ip"]}

    def restart(self, args=None):
        with self.lock:
            self._busy_check()
            return {"ok": True, "request": self._request("restart"), "message": "Restarting service …"}

    def probe(self, args=None):
        with self.lock:
            self._busy_check()
            server = self._server_state(self.active())
            if not server["mac"]:
                raise NetError("MAC address of %s unknown" % server["interface"])
            return {"ok": True, "request": self._request("probe", mac=server["mac"]),
                    "message": "Searching for DHCP servers (approx. 5 s) …"}

    def _own_resolver(self, cfg):
        """The WOL-Server's own lookups follow the DNS setting.

        With DNS on it asks itself first (127.0.0.1): otherwise names in the local domain go to the
        router and, with a public wildcard for the domain, resolve to the internet - the online check
        then probes the wrong host. The previous servers stay as fallback. Only with a static address;
        with DHCP the resolver comes from the DHCP server.
        """
        server = self._server_state(cfg)
        name = server["connection"]
        if not name or server["hotspot"] or server["method"] != "manual":
            return
        rc, out = self.runner(["nmcli", "-t", "-g", "ipv4.dns", "connection", "show", name])
        if rc != 0:
            return
        current = [d for d in re.split(r"[,\s]+", out.strip()) if d]
        fallback = [d for d in current if d != "127.0.0.1"] or cfg["dns"]["upstream"] or [cfg["dhcp"]["gateway"]]
        wanted = (["127.0.0.1"] if cfg["dns"]["enabled"] else []) + [d for d in fallback if d]
        if not wanted or wanted == current:
            return
        rc, _ = self.runner(["nmcli", "connection", "modify", name, "ipv4.dns", " ".join(wanted),
                             "ipv4.ignore-auto-dns", "yes"])
        if rc == 0:
            self.runner(["nmcli", "device", "reapply", server["interface"]], timeout=30)
            log.info("Own DNS resolver: %s (%s)", " ".join(wanted), name)

    def wifi_changed(self, mode):
        """Reported by the Wi-Fi part: pause dnsmasq in hotspot mode, start it again afterwards."""
        with self.lock:
            if mode == "client":
                self._own_resolver(self.active())
            previous, self.wifi_mode = self.wifi_mode, mode
            if previous is None or (mode == "hotspot") == (previous == "hotspot"):
                return
            active = self.active()
            if active["dns"]["enabled"] or active["dhcp"]["enabled"]:
                self._request("hotspot", hotspot=mode == "hotspot")

    # -- Import, export, history --

    def import_preview(self, args):
        with self.lock:
            preview = netcfg.import_dhcpd(args.get("text"))
            server = self._server_state(self.active())
            return {"ok": True, "preview": netcfg.annotate_import(self.working(), preview, server["mac"])}

    def import_commit(self, args):
        with self.lock:
            preview = args.get("preview")
            if not isinstance(preview, dict):
                raise NetError("Preview missing")
            cfg, stats = netcfg.merge_import(self.working(), preview, bool(args.get("replace")),
                                             bool(args.get("take_settings", True)))
            self._save_draft(cfg)
            return {"ok": True, "stats": stats,
                    "message": "%d new, %d updated, %d skipped (draft)."
                               % (stats["added"], stats["updated"], stats["skipped"])}

    def export(self, args=None):
        with self.lock:
            return {"ok": True, "export": {"wol_server_network": 1, "exported": self.clock(), "config": self.active()}}

    def restore(self, args):
        with self.lock:
            data = args.get("data")
            if isinstance(data, dict) and "config" in data:
                data = data["config"]
            cfg = netcfg.validate_config(data)
            self._save_draft(cfg)
            return {"ok": True, "message": "Configuration from backup loaded as draft."}

    def history(self, args=None):
        with self.lock:
            self._sync()
            items = []
            for name in sorted(os.listdir(self.path("history")), reverse=True):
                entry = _read_json(self.path("history", name))
                if entry:
                    items.append({k: entry.get(k) for k in ("id", "ts", "client", "changes")})
            return {"history": items}

    def history_restore(self, args):
        with self.lock:
            for name in os.listdir(self.path("history")):
                entry = _read_json(self.path("history", name))
                if entry and entry.get("id") == args.get("id"):
                    self._save_draft(entry["config"])
                    return {"ok": True, "message": "Version from %s loaded as draft."
                                                   % time.strftime("%d.%m. %H:%M", time.localtime(entry["ts"]))}
            raise NetError("Version not found")

    # -- Logs --

    def logs(self, args):
        kind = args.get("kind")
        lines = max(10, min(int(args.get("lines") or 200), 1000))
        if kind == "queries":
            text = self.journal(["-n", str(lines * 3), "--grep", r"query\["])
            out = []
            for line in text.splitlines():
                match = re.match(netcfg.JOURNAL_PREFIX + r"dnsmasq\[\d+\]:\s+(?:\d+\s+)?query\[(\w+)\]\s+(\S+)\s+from\s+(\S+)", line)
                if match:
                    out.append({"ts": float(match.group(1)), "type": match.group(2), "name": match.group(3),
                                "client": match.group(4)})
            return {"entries": out[-lines:]}
        if kind == "dhcp":
            text = self.journal(["-n", str(lines * 4), "--grep", r"DHCP(ACK|NAK|RELEASE|DECLINE|DISCOVER|OFFER|REQUEST)"])
            out = []
            for line in text.splitlines():
                match = re.match(netcfg.JOURNAL_PREFIX + r"dnsmasq-dhcp\[\d+\]:\s+(?:\d+\s+)?(DHCP\w+)\(([^)]+)\)\s*(.*)$", line)
                if match:
                    out.append({"ts": float(match.group(1)), "type": match.group(2), "detail": match.group(4)[:160]})
            return {"entries": out[-lines:]}
        if kind == "service":
            text = self.journal(["-n", str(lines)])
            return {"entries": [{"ts": float(l.split()[0]), "text": l.split(":", 1)[-1].strip()[:240]}
                                for l in text.splitlines() if l[:1].isdigit()][-lines:]}
        raise NetError("Unknown log")

    # -- static IP address for the WOL-Server --

    def static_ip(self, args):
        with self.lock:
            cfg = self.working()
            server = self._server_state(cfg)
            if not server["connection"]:
                raise NetError("No active Wi-Fi connection")
            if server["hotspot"]:
                raise NetError("Not possible in hotspot mode")
            name = server["connection"]
            if args.get("enabled"):
                ip = cfg["server"]["ip"] or server["address"]
                gateway = cfg["dhcp"]["gateway"] or self._gateway(server["interface"])
                if not ip or not gateway:
                    raise NetError("Address or gateway unknown - enter them under Settings")
                dns = "127.0.0.1 %s" % gateway if cfg["dns"]["enabled"] else gateway
                argv = ["nmcli", "connection", "modify", name, "ipv4.method", "manual",
                        "ipv4.addresses", "%s/%d" % (ip, cfg["server"]["prefix"]), "ipv4.gateway", gateway,
                        "ipv4.dns", dns, "ipv4.ignore-auto-dns", "yes"]
                message = "Static IP address %s set" % ip
            else:
                argv = ["nmcli", "connection", "modify", name, "ipv4.method", "auto", "ipv4.addresses", "",
                        "ipv4.gateway", "", "ipv4.dns", "", "ipv4.ignore-auto-dns", "no"]
                message = "Address via DHCP again"
            rc, _ = self.runner(argv)
            if rc != 0:
                raise NetError("NetworkManager rejected the change")
            # Apply without reconnecting; the Wi-Fi connection stays up
            self.runner(["nmcli", "device", "reapply", server["interface"]], timeout=30)
            log.info("%s (%s)", message, name)
            return {"ok": True, "message": message}
