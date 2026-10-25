"""Hersteller zu MAC-Adressen aus den Listen des Systems (ieee-data, nmap, arp-scan).

Es wird keine eigene Liste mitgeliefert. Fehlt eine Systemliste, bleibt der Hersteller leer –
nur private (lokal verwaltete) Adressen werden immer erkannt. Gelesen wird bei Bedarf und nur
für die angefragten Präfixe, damit auf dem Pi Zero nicht 30 000 Einträge im RAM liegen.
"""
import csv
import os
import re
import threading

from .netcfg import is_private_mac

SOURCES = (
    ("/usr/share/ieee-data/oui.csv", "csv"),
    ("/usr/share/nmap/nmap-mac-prefixes", "nmap"),
    ("/usr/share/arp-scan/ieee-oui.txt", "tab"),
    ("/usr/share/misc/ieee-oui.txt", "tab"),
)
PRIVATE = "private Adresse"

_lock = threading.Lock()
_cache = {}


def _source():
    for path, kind in SOURCES:
        if os.path.isfile(path):
            return path, kind
    return None, None


def _short(name):
    """„Raspberry Pi Trading Ltd“ → „Raspberry Pi Trading“: Rechtsformen weglassen."""
    name = re.sub(r"[,.]?\s+(co\.?,?\s*ltd|ltd|limited|inc|corp(oration)?|gmbh|ag|llc|s\.?a\.?|b\.?v\.?|oy|ab|kg|plc|"
                  r"co|company|technologies|technology)\.?$", "", name.strip(), flags=re.I)
    return name.strip(" ,.")[:40]


def _scan(prefixes, path, kind):
    found = {}
    wanted = set(prefixes)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            if kind == "csv":
                for row in csv.reader(f):
                    if len(row) >= 3 and row[1].upper() in wanted:
                        found[row[1].upper()] = _short(row[2])
            else:
                for line in f:
                    if line.startswith("#") or len(line) < 8:
                        continue
                    key = line[:6].upper()
                    if key in wanted:
                        found[key] = _short(line[6:].strip(" \t\n"))
    except OSError:
        pass
    return found


def lookup_many(macs, source=None):
    """{mac: Hersteller} – leer, wenn unbekannt. Private Adressen als „private Adresse“."""
    result, missing = {}, set()
    for mac in macs:
        if not mac:
            continue
        if is_private_mac(mac):
            result[mac] = PRIVATE
            continue
        prefix = mac.replace(":", "").replace("-", "")[:6].upper()
        with _lock:
            if prefix in _cache:
                result[mac] = _cache[prefix]
                continue
        missing.add(prefix)
    if missing:
        path, kind = source or _source()
        found = _scan(missing, path, kind) if path else {}
        with _lock:
            for prefix in missing:
                _cache[prefix] = found.get(prefix, "")
                if len(_cache) > 2000:
                    _cache.clear()
        for mac in macs:
            if mac and mac not in result:
                result[mac] = found.get(mac.replace(":", "").replace("-", "")[:6].upper(), "")
    return result


def lookup(mac):
    return lookup_many([mac]).get(mac, "")
