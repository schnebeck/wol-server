"""Aufträge aus der Web-GUI für dnsmasq ausführen – läuft als root in wol-dns-apply.service.

Der Root-Dienst wol-wifi legt Aufträge als JSON-Dateien in requests/ ab; wol-dns-apply.path
startet diesen Dienst, sobald dort etwas liegt. Ergebnisse landen in jobs/<id>.json.

Übernehmen: alten Stand sichern → neue Dateien einspielen → dnsmasq --test → neu laden bzw.
neu starten → Selbsttest per DNS-Abfrage. Schlägt etwas fehl, wird der alte Stand
wiederhergestellt; das Netz läuft dann unverändert weiter.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import time

from . import dhcpprobe, dnsclient

log = logging.getLogger("wolserver.dnsjob")

STATE = "/var/lib/wol-wifi/dns"
CONF = "/etc/wol-dns"
DATA = "/var/lib/wol-dns"
UNIT = "wol-dns.service"
FILES = ("dnsmasq.conf", "hosts.dhcp", "hosts.dns")
HOTSPOT_ADDRESS = "10.42.0.1"


class JobError(Exception):
    pass


def _run(argv, timeout=60):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=dict(os.environ, LC_ALL="C"))
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


class Job:
    def __init__(self, state=STATE, conf=CONF, data=DATA, runner=_run, sleep=time.sleep, query=dnsclient.query,
                 prober=dhcpprobe.probe, clock=time.time, addresses=None):
        self.state, self.conf, self.data = state, conf, data
        self.runner, self.sleep, self.query, self.prober, self.clock = runner, sleep, query, prober, clock
        self.addresses = addresses or self._addresses

    # -- Hilfen --

    def systemctl(self, *args):
        return self.runner(["systemctl"] + list(args))

    def is_active(self):
        return self.systemctl("is-active", "--quiet", UNIT)[0] == 0

    def journal_tail(self, lines=6):
        rc, out = self.runner(["journalctl", "-u", UNIT, "-n", str(lines), "-o", "cat", "--no-pager"])
        return " | ".join(l.strip() for l in out.splitlines()[-lines:]) if rc == 0 else ""

    @staticmethod
    def _addresses(iface):
        from . import netinfo
        return [i["address"] for i in netinfo.interfaces() if i["name"] == iface and i["address"]]

    def hotspot_active(self, iface):
        return HOTSPOT_ADDRESS in self.addresses(iface)

    def finish(self, request, state, message, detail=""):
        result = {"id": request["id"], "action": request.get("action"), "state": state, "message": message,
                  "ts": self.clock()}
        if detail:
            result["detail"] = detail[:2000]
        _write_json(os.path.join(self.state, "jobs", request["id"] + ".json"), result)

    # -- Ablauf --

    def run(self):
        """Alle Aufträge der Reihe nach abarbeiten, auch solche, die währenddessen eintreffen."""
        requests_dir = os.path.join(self.state, "requests")
        while True:
            names = sorted(n for n in os.listdir(requests_dir) if n.endswith(".json"))
            if not names:
                return 0
            for name in names:
                path = os.path.join(requests_dir, name)
                try:
                    with open(path, encoding="utf-8") as f:
                        request = json.load(f)
                except (OSError, ValueError):
                    request = {"id": name[:-5], "action": "?"}
                request.setdefault("id", name[:-5])
                try:
                    self.finish(request, "running", "Läuft …")
                    handler = getattr(self, "do_" + str(request.get("action")), None)
                    if handler is None:
                        raise JobError("Unbekannter Auftrag: %s" % request.get("action"))
                    message = handler(request)
                    self.finish(request, "ok", message)
                except JobError as e:
                    log.warning("Auftrag %s fehlgeschlagen: %s", request["id"], e)
                    self.finish(request, "failed", str(e))
                except Exception as e:  # noqa: BLE001 – Auftrag nie liegen lassen (Endlosschleife des .path)
                    log.exception("Auftrag %s abgebrochen", request["id"])
                    self.finish(request, "failed", "Interner Fehler: %s" % e)
                finally:
                    try:
                        os.remove(path)
                    except FileNotFoundError:
                        pass

    # -- Übernehmen --

    def _backup(self):
        backup = os.path.join(self.conf, ".vorher")
        shutil.rmtree(backup, ignore_errors=True)
        os.makedirs(backup, mode=0o755)
        for name in FILES:
            src = os.path.join(self.conf, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(backup, name))
        return backup

    def _restore(self, backup):
        for name in FILES:
            src, dst = os.path.join(backup, name), os.path.join(self.conf, name)
            if os.path.exists(src):
                shutil.copy2(src, dst)
            elif os.path.exists(dst):
                os.remove(dst)
        if os.path.exists(os.path.join(self.conf, "dnsmasq.conf")) and self.systemctl("is-enabled", UNIT)[0] == 0:
            self.systemctl("restart", UNIT)
        else:
            self.systemctl("disable", "--no-reload", "--now", UNIT)

    def _fail(self, backup, reason, detail=""):
        self._restore(backup)
        raise JobError("%s – der vorherige Stand wurde wiederhergestellt.%s" % (reason, (" " + detail) if detail else ""))

    def _self_test(self, request):
        name, expect, port = request.get("test_name"), request.get("expect"), int(request.get("port") or 53)
        last = "keine Antwort"
        for _ in range(6):
            try:
                res = self.query(name, "A", "127.0.0.1", port, 2.0)
                ips = [a["data"] for a in res["answers"] if a["type"] == "A"]
                if expect in ips or (not expect and res["rcode"] == 0):
                    return True, ""
                last = "%s → %s" % (name, ", ".join(ips) or res["status"])
            except dnsclient.DnsError as e:
                last = str(e)
            self.sleep(1)
        return False, last

    def do_apply(self, request):
        staging = os.path.realpath(request.get("staging") or "")
        if not staging.startswith(os.path.realpath(os.path.join(self.state, "staging")) + os.sep):
            raise JobError("Zwischenstand liegt außerhalb des erlaubten Verzeichnisses")
        if not all(os.path.isfile(os.path.join(staging, n)) for n in FILES):
            raise JobError("Zwischenstand unvollständig")
        os.makedirs(self.conf, mode=0o755, exist_ok=True)
        os.makedirs(self.data, mode=0o755, exist_ok=True)
        conf_changed = _read(os.path.join(self.conf, "dnsmasq.conf")) != _read(os.path.join(staging, "dnsmasq.conf"))
        backup = self._backup()
        for name in FILES:
            tmp = os.path.join(self.conf, name + ".neu")
            shutil.copyfile(os.path.join(staging, name), tmp)
            os.chmod(tmp, 0o644)
            os.replace(tmp, os.path.join(self.conf, name))

        rc, out = self.runner(["dnsmasq", "--test", "--conf-file=%s/dnsmasq.conf" % self.conf])
        if rc != 0:
            self._fail(backup, "dnsmasq lehnt die Konfiguration ab", out.splitlines()[-1] if out else "")

        if not request.get("enabled"):
            self.systemctl("disable", "--no-reload", "--now", UNIT)
            return "DHCP und DNS ausgeschaltet"

        self.systemctl("enable", "--no-reload", UNIT)
        if self.hotspot_active(request.get("iface") or "wlan0"):
            self.systemctl("stop", UNIT)
            return "Gespeichert – wird aktiv, sobald der Einrichtungs-Hotspot beendet ist"

        was_active = self.is_active()
        if was_active and not conf_changed:
            self.systemctl("reload", UNIT)
            how = "neu geladen"
        else:
            self.systemctl("restart", UNIT)
            how = "neu gestartet"
        self.sleep(1.5)
        if not self.is_active():
            self._fail(backup, "dnsmasq startet mit der neuen Konfiguration nicht", self.journal_tail())
        if request.get("dns"):
            ok, why = self._self_test(request)
            if not ok:
                self._fail(backup, "DNS-Selbsttest fehlgeschlagen (%s)" % why)
        return "Übernommen, dnsmasq %s" % how

    # -- weitere Aufträge --

    def do_release(self, request):
        mac, ip, iface = request.get("mac"), request.get("ip"), request.get("iface") or "wlan0"
        if shutil.which("dhcp_release"):
            rc, out = self.runner(["dhcp_release", iface, ip, mac])
            if rc == 0:
                return "Lease von %s freigegeben" % ip
        leasefile = os.path.join(self.data, "dnsmasq.leases")
        self.systemctl("stop", UNIT)
        try:
            lines = (_read(leasefile) or "").splitlines()
            kept = [l for l in lines if mac not in l.lower()]
            with open(leasefile, "w", encoding="utf-8") as f:
                f.write("\n".join(kept) + ("\n" if kept else ""))
        finally:
            self.systemctl("start", UNIT)
        return "Lease von %s gelöscht (dnsmasq neu gestartet)" % ip

    def do_restart(self, request):
        self.systemctl("restart", UNIT)
        self.sleep(1.5)
        if not self.is_active():
            raise JobError("dnsmasq läuft nicht: %s" % self.journal_tail())
        return "dnsmasq neu gestartet"

    def do_hotspot(self, request):
        if request.get("hotspot"):
            self.systemctl("stop", UNIT)
            return "Pausiert (Einrichtungs-Hotspot aktiv)"
        if self.systemctl("is-enabled", UNIT)[0] == 0:
            self.systemctl("start", UNIT)
            return "Nach dem Hotspot-Betrieb wieder gestartet"
        return "Nichts zu tun"

    def do_probe(self, request):
        iface = request.get("iface") or "wlan0"
        try:
            servers = self.prober(iface, request["mac"], 4.0)
            result = {"ts": self.clock(), "ok": True, "servers": servers, "iface": iface}
        except OSError as e:
            result = {"ts": self.clock(), "ok": False, "error": str(e), "servers": [], "iface": iface}
        _write_json(os.path.join(self.state, "probe.json"), result)
        if not result["ok"]:
            raise JobError("Suche fehlgeschlagen: %s" % result["error"])
        count = len(result["servers"])
        return "%s DHCP-Server gefunden" % ("Kein" if not count else count)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return Job().run()


if __name__ == "__main__":
    sys.exit(main())
