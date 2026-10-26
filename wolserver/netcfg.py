"""DHCP und DNS mit dnsmasq: Datenmodell, Prüfung, Konfiguration, Import und Auswertung.

Reine Logik ohne Seiteneffekte – genutzt vom Root-Dienst (netsvc) und in den Tests. Die GUI
liefert nur strukturierte Daten; die dnsmasq-Konfiguration entsteht ausschließlich hier aus
geprüften Feldern, damit sich keine Optionen (z. B. dhcp-script) einschleusen lassen.
"""
import copy
import ipaddress
import re
import time
import unicodedata
import uuid

from .wol import normalize_mac

LEASE_MIN, LEASE_MAX = 120, 7 * 86400
LEASE_PRESETS = (600, 3600, 4 * 3600, 12 * 3600, 86400, 7 * 86400)
MAX_RESERVATIONS, MAX_RECORDS, MAX_IGNORED = 500, 200, 500
RECORD_TYPES = ("host", "alias", "mx", "txt", "srv")
MODES = ("known-only", "range")

DEFAULT_NETWORK = {
    "version": 1,
    "server": {"interface": "wlan0", "ip": "", "prefix": 24, "name": "wol", "port": 53},
    "dhcp": {
        "enabled": False, "mode": "known-only", "range_start": "", "range_end": "",
        "lease_time": 12 * 3600, "gateway": "", "dns": [], "ntp": [], "search": [],
        "boot_file": "", "boot_server": "", "tftp": False,
    },
    "dns": {
        "enabled": False, "domain": "home.arpa", "upstream": [], "conditional": [],
        "ttl": 60, "cache_size": 1000, "log_queries": False,
    },
    "reservations": [],
    "records": [],
    "ignored": [],
}

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SRV_NAME = re.compile(r"^_[a-z0-9-]{1,62}\._(tcp|udp|tls)$")
_BOOT_FILE = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
_IFACE = re.compile(r"^[a-z0-9]{1,15}$")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"})


class NetError(ValueError):
    pass


def new_id():
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------- Namen und Adressen


def normalize_name(raw):
    """Gerätenamen zu einem gültigen DNS-Namen (RFC 1123) machen: „Anna's iPhone“ → annas-iphone."""
    if not isinstance(raw, str):
        return ""
    text = unicodedata.normalize("NFKD", raw.translate(_UMLAUTS))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"['’`´]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return re.sub(r"-{2,}", "-", text)[:63].strip("-")


def valid_label(name):
    return bool(_LABEL.match(name or ""))


def valid_domain(name):
    labels = (name or "").rstrip(".").split(".")
    return 0 < len(name or "") <= 253 and all(valid_label(label) for label in labels)


def is_private_mac(mac):
    """Lokal verwaltete MAC (zweitniedrigstes Bit im ersten Byte): typisch für „private WLAN-Adressen“."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, AttributeError, IndexError):
        return False


def _mac(value, what="MAC-Adresse"):
    try:
        mac = normalize_mac(value).lower()
    except (ValueError, AttributeError, TypeError):
        raise NetError("%s ungültig: %s" % (what, value)) from None
    if int(mac[:2], 16) & 0x01:
        raise NetError("%s %s ist eine Multicast-Adresse" % (what, mac))
    return mac


def _ipv4(value, what):
    try:
        ip = ipaddress.IPv4Address(str(value).strip())
    except (ipaddress.AddressValueError, ValueError):
        raise NetError("%s: keine gültige IPv4-Adresse (%s)" % (what, value)) from None
    return ip


def _ip_any(value, what):
    try:
        return ipaddress.ip_address(str(value).strip())
    except ValueError:
        raise NetError("%s: keine gültige IP-Adresse (%s)" % (what, value)) from None


def _text(value, what, maxlen, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise NetError("%s: Text erwartet" % what)
    value = value.strip()
    if required and not value:
        raise NetError("%s darf nicht leer sein" % what)
    if len(value) > maxlen:
        raise NetError("%s ist zu lang (max. %d Zeichen)" % (what, maxlen))
    if any(ord(c) < 32 for c in value):
        raise NetError("%s enthält Steuerzeichen" % what)
    return value


def _int(value, what, lo, hi):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise NetError("%s: Zahl erwartet" % what) from None
    if not lo <= number <= hi:
        raise NetError("%s muss zwischen %d und %d liegen" % (what, lo, hi))
    return number


def _ip_list(values, what, limit=4):
    if values in (None, ""):
        return []
    if isinstance(values, str):
        values = [v for v in re.split(r"[\s,;]+", values) if v]
    if not isinstance(values, list) or len(values) > limit:
        raise NetError("%s: höchstens %d Adressen" % (what, limit))
    return [str(_ipv4(v, what)) for v in values]


def parse_lease_time(value):
    """Sekunden aus 600, "600", "10m", "12h", "1d" oder "7 Tage" – geprüft auf 2 min bis 7 Tage."""
    if isinstance(value, bool):
        raise NetError("Lease-Zeit ungültig")
    if isinstance(value, (int, float)):
        seconds = int(value)
    else:
        match = re.match(r"^\s*(\d+)\s*(s|sek|m|min|h|std|d|t|tag|tage)?\s*$", str(value or "").lower())
        if not match:
            raise NetError("Lease-Zeit ungültig: %s (z. B. 600, 30m, 12h, 1d)" % value)
        factor = {"s": 1, "sek": 1, "m": 60, "min": 60, "h": 3600, "std": 3600, "d": 86400, "t": 86400,
                  "tag": 86400, "tage": 86400}.get(match.group(2) or "s")
        seconds = int(match.group(1)) * factor
    if not LEASE_MIN <= seconds <= LEASE_MAX:
        raise NetError("Lease-Zeit muss zwischen 2 Minuten und 7 Tagen liegen")
    return seconds


def fmt_lease_time(seconds):
    if seconds % 86400 == 0:
        days = seconds // 86400
        return "%d Tag%s" % (days, "" if days == 1 else "e")
    if seconds % 3600 == 0:
        return "%d h" % (seconds // 3600)
    if seconds % 60 == 0:
        return "%d min" % (seconds // 60)
    return "%d s" % seconds


def network_of(cfg):
    server = cfg["server"]
    if not server.get("ip"):
        return None
    return ipaddress.IPv4Network("%s/%d" % (server["ip"], server["prefix"]), strict=False)


# ---------------------------------------------------------------- Prüfung


def validate_reservation(data, net=None):
    if not isinstance(data, dict):
        raise NetError("Reservierung: Objekt erwartet")
    mac = _mac(data.get("mac"))
    ip = _ipv4(data.get("ip"), "IP-Adresse")
    if net is not None:
        if ip not in net:
            raise NetError("%s liegt nicht im Netz %s" % (ip, net))
        if ip in (net.network_address, net.broadcast_address):
            raise NetError("%s ist Netz- oder Broadcast-Adresse" % ip)
    raw_name = _text(data.get("name"), "Name", 63)
    name = normalize_name(raw_name)
    if raw_name and not name:
        raise NetError("Name „%s“ enthält kein gültiges Zeichen" % raw_name)
    lease = data.get("lease_time")
    return {
        "id": data.get("id") if isinstance(data.get("id"), str) and re.match(r"^\w{1,32}$", data["id"]) else new_id(),
        "mac": mac,
        "ip": str(ip),
        "name": name,
        "comment": _text(data.get("comment"), "Kommentar", 120),
        "enabled": bool(data.get("enabled", True)),
        "lease_time": None if lease in (None, "", 0) else parse_lease_time(lease),
    }


def validate_record(data):
    if not isinstance(data, dict):
        raise NetError("DNS-Eintrag: Objekt erwartet")
    rtype = data.get("type")
    if rtype not in RECORD_TYPES:
        raise NetError("Unbekannter Eintragstyp: %s" % rtype)
    raw_name = _text(data.get("name"), "Name", 63, required=rtype != "mx").lower()
    record = {
        "id": data.get("id") if isinstance(data.get("id"), str) and re.match(r"^\w{1,32}$", data["id"]) else new_id(),
        "type": rtype, "name": raw_name, "comment": _text(data.get("comment"), "Kommentar", 120),
    }
    if rtype == "srv":
        if not _SRV_NAME.match(raw_name):
            raise NetError("SRV-Name im Format _dienst._tcp, z. B. _ldap._tcp")
    elif raw_name and not valid_label(raw_name):
        raise NetError("Name „%s“ ungültig: nur a–z, 0–9 und -, ohne Punkte (die Domäne wird angehängt)" % raw_name)
    value = _text(data.get("value"), "Wert", 255, required=True)
    if rtype == "host":
        value = str(_ip_any(value, "IP-Adresse"))
    elif rtype in ("alias", "mx", "srv"):
        value = value.lower().rstrip(".")
        if not valid_domain(value):
            raise NetError("Ziel „%s“ ist kein gültiger Name" % value)
    elif rtype == "txt":
        if '"' in value or "\\" in value:
            raise NetError("TXT-Text darf keine Anführungszeichen oder \\ enthalten")
    record["value"] = value
    if rtype == "mx":
        record["priority"] = _int(data.get("priority", 10), "Priorität", 0, 65535)
    if rtype == "srv":
        record["port"] = _int(data.get("port"), "Port", 1, 65535)
        record["priority"] = _int(data.get("priority", 0), "Priorität", 0, 65535)
        record["weight"] = _int(data.get("weight", 0), "Gewicht", 0, 65535)
    return record


def validate_settings(cfg, data):
    """Grundeinstellungen aus der GUI übernehmen. Liefert eine neue Konfiguration."""
    if not isinstance(data, dict):
        raise NetError("Einstellungen: Objekt erwartet")
    new = copy.deepcopy(cfg)
    server, dhcp, dns = data.get("server") or {}, data.get("dhcp") or {}, data.get("dns") or {}
    for key in ("ip", "prefix", "name", "interface", "port"):
        if key in server:
            new["server"][key] = server[key]
    for key in DEFAULT_NETWORK["dhcp"]:
        if key in dhcp:
            new["dhcp"][key] = dhcp[key]
    for key in DEFAULT_NETWORK["dns"]:
        if key in dns:
            new["dns"][key] = dns[key]
    return validate_config(new)


def validate_config(data):
    """Gesamte Konfiguration prüfen und normalisieren (auch beim Import und vor dem Übernehmen)."""
    if not isinstance(data, dict):
        raise NetError("Konfiguration: Objekt erwartet")
    cfg = copy.deepcopy(DEFAULT_NETWORK)
    for section in ("server", "dhcp", "dns"):
        if isinstance(data.get(section), dict):
            cfg[section].update({k: v for k, v in data[section].items() if k in cfg[section]})
    s, d, n = cfg["server"], cfg["dhcp"], cfg["dns"]

    s["interface"] = _text(s["interface"], "Interface", 15, required=True)
    if not _IFACE.match(s["interface"]):
        raise NetError("Interface ungültig: %s" % s["interface"])
    s["prefix"] = _int(s["prefix"], "Netzmaske (Präfix)", 8, 30)
    s["port"] = _int(s["port"], "DNS-Port", 1, 65535)
    s["name"] = normalize_name(_text(s["name"], "Name des Servers", 63)) or "wol"
    s["ip"] = str(_ipv4(s["ip"], "Adresse des Servers")) if s["ip"] else ""
    net = network_of(cfg)

    n["enabled"] = bool(n["enabled"])
    n["domain"] = _text(n["domain"], "Domäne", 253, required=True).lower().rstrip(".")
    if not valid_domain(n["domain"]) or "." not in n["domain"]:
        raise NetError("Domäne ungültig: %s (z. B. home.arpa)" % n["domain"])
    if n["domain"].endswith(".local") or n["domain"] == "local":
        raise NetError("Domänen unter .local sind für mDNS reserviert (Alternative z. B. home.arpa)")
    n["upstream"] = _ip_list(n["upstream"], "Upstream-DNS")
    n["ttl"] = _int(n["ttl"], "TTL", 0, 86400)
    n["cache_size"] = _int(n["cache_size"], "Cache-Größe", 0, 10000)
    n["log_queries"] = bool(n["log_queries"])
    conditional = []
    for item in n["conditional"] or []:
        if not isinstance(item, dict):
            raise NetError("Bedingte Weiterleitung: Objekt erwartet")
        domain = _text(item.get("domain"), "Weiterleitungs-Domäne", 253, required=True).lower().rstrip(".")
        if not valid_domain(domain):
            raise NetError("Weiterleitungs-Domäne ungültig: %s" % domain)
        if domain == n["domain"] or domain.endswith("." + n["domain"]):
            raise NetError("%s gehört zur eigenen Domäne und kann nicht weitergeleitet werden" % domain)
        conditional.append({"domain": domain, "server": str(_ipv4(item.get("server"), "Server für %s" % domain))})
    if len(conditional) > 20:
        raise NetError("Höchstens 20 bedingte Weiterleitungen")
    n["conditional"] = conditional

    d["enabled"] = bool(d["enabled"])
    if d["mode"] not in MODES:
        raise NetError("DHCP-Modus ungültig: %s" % d["mode"])
    d["lease_time"] = parse_lease_time(d["lease_time"])
    d["gateway"] = str(_ipv4(d["gateway"], "Gateway")) if d["gateway"] else ""
    d["dns"] = _ip_list(d["dns"], "DNS-Server für Clients")
    d["ntp"] = _ip_list(d["ntp"], "NTP-Server")
    search = d["search"] or []
    if isinstance(search, str):
        search = [v for v in re.split(r"[\s,;]+", search) if v]
    if not isinstance(search, list) or len(search) > 6:
        raise NetError("Höchstens 6 zusätzliche Suchdomänen")
    d["search"] = []
    for item in search:
        item = _text(item, "Suchdomäne", 253).lower().rstrip(".")
        if not valid_domain(item):
            raise NetError("Suchdomäne ungültig: %s" % item)
        if item != n["domain"] and item not in d["search"]:
            d["search"].append(item)
    d["boot_file"] = _text(d["boot_file"], "Boot-Datei", 128)
    if d["boot_file"] and (not _BOOT_FILE.match(d["boot_file"]) or ".." in d["boot_file"]):
        raise NetError("Boot-Datei ungültig (nur Buchstaben, Ziffern, . _ - /)")
    d["boot_server"] = str(_ipv4(d["boot_server"], "TFTP-Server")) if d["boot_server"] else ""
    d["tftp"] = bool(d["tftp"])
    for key in ("range_start", "range_end"):
        d[key] = str(_ipv4(d[key], "Adressbereich")) if d[key] else ""

    if net is not None:
        for what, ip in (("Gateway", d["gateway"]), ("Bereichsanfang", d["range_start"]), ("Bereichsende", d["range_end"])):
            if ip and ipaddress.IPv4Address(ip) not in net:
                raise NetError("%s %s liegt nicht im Netz %s" % (what, ip, net))
    if d["mode"] == "range":
        if not (d["range_start"] and d["range_end"]):
            raise NetError("Für den Modus „Adressbereich“ bitte Anfang und Ende angeben")
        if ipaddress.IPv4Address(d["range_start"]) > ipaddress.IPv4Address(d["range_end"]):
            raise NetError("Der Adressbereich beginnt nach seinem Ende")
    if d["enabled"]:
        if net is None:
            raise NetError("Für DHCP braucht der Server eine feste Adresse (Einstellungen → Server)")
        if not d["gateway"]:
            raise NetError("Für DHCP bitte ein Gateway (Router) angeben")
    if n["enabled"] and not n["upstream"]:
        raise NetError("Für DNS bitte mindestens einen Upstream-Server angeben (z. B. den Router)")

    reservations, records, ignored = [], [], []
    for item in data.get("reservations") or []:
        reservations.append(validate_reservation(item, net))
    for item in data.get("records") or []:
        records.append(validate_record(item))
    for item in data.get("ignored") or []:
        if isinstance(item, dict):
            ignored.append({"mac": _mac(item.get("mac")), "comment": _text(item.get("comment"), "Kommentar", 120),
                            "ts": float(item.get("ts") or 0)})
    if len(reservations) > MAX_RESERVATIONS or len(records) > MAX_RECORDS or len(ignored) > MAX_IGNORED:
        raise NetError("Zu viele Einträge")
    cfg["reservations"], cfg["records"], cfg["ignored"] = reservations, records, ignored
    check_conflicts(cfg)
    return cfg


def check_conflicts(cfg):
    """Eindeutigkeit von MAC, IP und Namen; Aliasse müssen auf bekannte Namen zeigen."""
    net = network_of(cfg)
    d = cfg["dhcp"]
    server_ip = cfg["server"]["ip"]
    macs, ips, names = {}, {}, {cfg["server"]["name"]: "dem WOL-Server"}
    rng = None
    if d["mode"] == "range" and d["range_start"] and d["range_end"]:
        rng = (ipaddress.IPv4Address(d["range_start"]), ipaddress.IPv4Address(d["range_end"]))
    for r in cfg["reservations"]:
        label = r["name"] or r["mac"]
        if r["mac"] in macs:
            raise NetError("MAC %s ist doppelt (%s und %s)" % (r["mac"], macs[r["mac"]], label))
        macs[r["mac"]] = label
        if r["ip"] in ips:
            raise NetError("IP %s ist doppelt vergeben (%s und %s)" % (r["ip"], ips[r["ip"]], label))
        if r["ip"] == server_ip:
            raise NetError("IP %s gehört dem WOL-Server selbst (%s)" % (r["ip"], label))
        ips[r["ip"]] = label
        if rng and rng[0] <= ipaddress.IPv4Address(r["ip"]) <= rng[1]:
            raise NetError("IP %s von %s liegt im dynamischen Bereich; Reservierungen müssen außerhalb liegen" % (r["ip"], label))
        if net is not None and ipaddress.IPv4Address(r["ip"]) not in net:
            raise NetError("IP %s von %s liegt nicht im Netz %s" % (r["ip"], label, net))
        if r["name"]:
            if r["name"] in names:
                raise NetError("Name „%s“ ist doppelt (%s)" % (r["name"], names[r["name"]]))
            names[r["name"]] = "Reservierung %s" % r["ip"]
    # Host-Einträge dürfen sich einen Namen teilen (z. B. IPv4 und IPv6), sonst ist jeder Name eindeutig.
    host_names = set()
    for rec in cfg["records"]:
        if rec["type"] == "host":
            if rec["name"] in names:
                raise NetError("Name „%s“ ist schon vergeben (%s)" % (rec["name"], names[rec["name"]]))
            host_names.add(rec["name"])
    for rec in cfg["records"]:
        if rec["type"] == "alias":
            if rec["name"] in names or rec["name"] in host_names:
                raise NetError("Alias „%s“ kollidiert mit einem vorhandenen Namen" % rec["name"])
            names[rec["name"]] = "Alias"
    local = set(names) | host_names
    domain = cfg["dns"]["domain"]
    for rec in cfg["records"]:
        if rec["type"] == "alias":
            target = rec["value"]
            short = target[: -len(domain) - 1] if target.endswith("." + domain) else target
            if short not in local:
                raise NetError("Alias „%s“: Ziel „%s“ ist kein lokaler Name "
                               "(Reservierung, Eintrag oder WOL-Server)" % (rec["name"], target))
            if short == rec["name"]:
                raise NetError("Alias „%s“ zeigt auf sich selbst" % rec["name"])
    return True


# ---------------------------------------------------------------- Hilfen für die GUI


def next_free_ip(cfg, leases=(), start_host=10):
    """Niedrigste freie Adresse ab .start_host: nicht Server, Gateway, Reservierung, Eintrag, Lease, Bereich."""
    net = network_of(cfg)
    if net is None:
        return None
    used = {cfg["server"]["ip"], cfg["dhcp"]["gateway"]}
    used.update(r["ip"] for r in cfg["reservations"])
    used.update(rec["value"] for rec in cfg["records"] if rec["type"] == "host")
    used.update(lease["ip"] for lease in leases)
    d = cfg["dhcp"]
    rng = None
    if d["mode"] == "range" and d["range_start"] and d["range_end"]:
        rng = (ipaddress.IPv4Address(d["range_start"]), ipaddress.IPv4Address(d["range_end"]))
    for host in net.hosts():
        if int(host) - int(net.network_address) < start_host or str(host) in used:
            continue
        if rng and rng[0] <= host <= rng[1]:
            continue
        return str(host)
    return None


def unique_name(cfg, wanted, ignore_id=None):
    """Freien Namen finden: annas-iphone, annas-iphone-2, …"""
    taken = {cfg["server"]["name"]}
    taken.update(r["name"] for r in cfg["reservations"] if r["name"] and r["id"] != ignore_id)
    taken.update(rec["name"] for rec in cfg["records"] if rec["type"] in ("host", "alias"))
    base = normalize_name(wanted) or "geraet"
    name, counter = base, 2
    while name in taken:
        suffix = "-%d" % counter
        name = base[: 63 - len(suffix)] + suffix
        counter += 1
    return name


SETTING_LABELS = {
    ("server", "ip"): "Adresse des Servers", ("server", "prefix"): "Netzmaske", ("server", "name"): "Name des Servers",
    ("server", "interface"): "Interface", ("server", "port"): "DNS-Port",
    ("dhcp", "enabled"): "DHCP", ("dhcp", "mode"): "DHCP-Modus", ("dhcp", "range_start"): "Bereichsanfang",
    ("dhcp", "range_end"): "Bereichsende", ("dhcp", "lease_time"): "Lease-Zeit", ("dhcp", "gateway"): "Gateway",
    ("dhcp", "dns"): "DNS-Server für Clients", ("dhcp", "ntp"): "NTP-Server", ("dhcp", "search"): "Suchdomänen",
    ("dhcp", "boot_file"): "Boot-Datei", ("dhcp", "boot_server"): "TFTP-Server", ("dhcp", "tftp"): "Eigener TFTP-Server",
    ("dns", "enabled"): "DNS", ("dns", "domain"): "Domäne", ("dns", "upstream"): "Upstream-DNS",
    ("dns", "conditional"): "Bedingte Weiterleitungen", ("dns", "ttl"): "TTL", ("dns", "cache_size"): "Cache-Größe",
    ("dns", "log_queries"): "Abfrageprotokoll",
}


def _fmt_value(section, key, value):
    if isinstance(value, bool):
        return "an" if value else "aus"
    if key == "lease_time":
        return fmt_lease_time(value)
    if key == "mode":
        return "nur bekannte Geräte" if value == "known-only" else "Adressbereich"
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return ", ".join("%s → %s" % (v["domain"], v["server"]) for v in value) or "–"
        return ", ".join(value) or "–"
    return str(value) if value not in ("", None) else "–"


def _res_label(r):
    return "%s (%s)" % (r["name"] or r["mac"], r["ip"])


def _rec_label(rec):
    if rec["type"] == "host":
        return "%s → %s" % (rec["name"], rec["value"])
    if rec["type"] == "alias":
        return "Alias %s → %s" % (rec["name"], rec["value"])
    return "%s %s" % (rec["type"].upper(), rec["name"] or "@")


def diff(old, new):
    """Lesbare Liste der Änderungen von old nach new (für Übernehmen-Leiste und Verlauf)."""
    changes = []
    for (section, key), label in SETTING_LABELS.items():
        a, b = old[section].get(key), new[section].get(key)
        if a != b:
            changes.append("%s: %s → %s" % (label, _fmt_value(section, key, a), _fmt_value(section, key, b)))
    olds = {r["id"]: r for r in old["reservations"]}
    news = {r["id"]: r for r in new["reservations"]}
    for rid, r in news.items():
        if rid not in olds:
            changes.append("Reservierung neu: %s" % _res_label(r))
        elif olds[rid] != r:
            o = olds[rid]
            parts = [p for p, k in (("IP %s → %s" % (o["ip"], r["ip"]), "ip"), ("Name %s → %s" % (o["name"] or "–", r["name"] or "–"), "name"),
                                    ("MAC %s → %s" % (o["mac"], r["mac"]), "mac"),
                                    ("aktiv" if r["enabled"] else "inaktiv", "enabled"),
                                    ("Lease-Zeit", "lease_time"), ("Kommentar", "comment")) if o[k] != r[k]]
            changes.append("Reservierung %s: %s" % (r["name"] or r["mac"], ", ".join(parts)))
    for rid, r in olds.items():
        if rid not in news:
            changes.append("Reservierung entfernt: %s" % _res_label(r))
    oldr = {r["id"]: r for r in old["records"]}
    newr = {r["id"]: r for r in new["records"]}
    for rid, r in newr.items():
        if rid not in oldr:
            changes.append("DNS-Eintrag neu: %s" % _rec_label(r))
        elif oldr[rid] != r:
            changes.append("DNS-Eintrag geändert: %s" % _rec_label(r))
    for rid, r in oldr.items():
        if rid not in newr:
            changes.append("DNS-Eintrag entfernt: %s" % _rec_label(r))
    oldi = {i["mac"] for i in old["ignored"]}
    newi = {i["mac"] for i in new["ignored"]}
    changes += ["Ignoriert: %s" % m for m in sorted(newi - oldi)]
    changes += ["Nicht mehr ignoriert: %s" % m for m in sorted(oldi - newi)]
    return changes


# ---------------------------------------------------------------- dnsmasq-Konfiguration

HOOK = "/opt/wol-server/deploy/dns-lease-hook.sh"


def render(cfg, conf_dir="/etc/wol-dns", data_dir="/var/lib/wol-dns", user="dnsmasq", hook=HOOK):
    """Dateien für dnsmasq: {"dnsmasq.conf": …, "hosts.dhcp": …, "hosts.dns": …}."""
    cfg = validate_config(cfg)
    s, d, n = cfg["server"], cfg["dhcp"], cfg["dns"]
    net = network_of(cfg)
    domain = n["domain"]
    lines = [
        "# Erzeugt vom WOL-Server – Änderungen bitte über die Web-GUI (Netzwerk), nicht hier.",
        "interface=%s" % s["interface"],
        "bind-dynamic",
        "user=%s" % user,
        "no-hosts",
        "log-facility=-",
    ]
    if n["enabled"]:
        lines += [
            "port=%d" % s["port"],
            "no-resolv",
            "domain-needed",
            "bogus-priv",
            "cache-size=%d" % n["cache_size"],
            "local-ttl=%d" % n["ttl"],
            "dhcp-ttl=%d" % n["ttl"],
        ]
        lines += ["server=%s" % ip for ip in n["upstream"]]
        lines += ["server=/%s/%s" % (c["domain"], c["server"]) for c in n["conditional"]]
        lines += ["local=/%s/" % domain, "domain=%s" % domain, "expand-hosts",
                  "addn-hosts=%s/hosts.dns" % conf_dir]
        def full(target):  # Ziele ohne Punkt sind lokale Namen
            return target if "." in target else "%s.%s" % (target, domain)

        for rec in cfg["records"]:
            fqdn = "%s.%s" % (rec["name"], domain) if rec["name"] else domain
            if rec["type"] == "alias":
                target = rec["value"] if rec["value"].endswith("." + domain) else "%s.%s" % (rec["value"], domain)
                lines.append("cname=%s,%s" % (fqdn, target))
            elif rec["type"] == "mx":
                lines.append("mx-host=%s,%s,%d" % (fqdn, full(rec["value"]), rec["priority"]))
            elif rec["type"] == "txt":
                lines.append('txt-record=%s,"%s"' % (fqdn, rec["value"]))
            elif rec["type"] == "srv":
                lines.append("srv-host=%s,%s,%d,%d,%d" % (fqdn, full(rec["value"]), rec["port"], rec["priority"], rec["weight"]))
        if n["log_queries"]:
            lines.append("log-queries")
    else:
        lines.append("port=0")
    if d["enabled"] and net is not None:
        lease = d["lease_time"]
        if d["mode"] == "known-only":
            lines += ["dhcp-range=%s,static,%s,%d" % (net.network_address, net.netmask, lease),
                      "dhcp-ignore=tag:!known"]
        else:
            lines.append("dhcp-range=%s,%s,%s,%d" % (d["range_start"], d["range_end"], net.netmask, lease))
        dns_servers = d["dns"] or ([s["ip"]] if n["enabled"] else [d["gateway"]])
        lines += [
            "dhcp-authoritative",
            "dhcp-option=option:router,%s" % d["gateway"],
            "dhcp-option=option:dns-server,%s" % ",".join(dns_servers),
            "dhcp-option=option:domain-name,%s" % domain,
            "dhcp-option=option:domain-search,%s" % ",".join([domain] + d["search"]),
        ]
        if d["ntp"]:
            lines.append("dhcp-option=option:ntp-server,%s" % ",".join(d["ntp"]))
        if d["boot_file"]:
            server = d["boot_server"] or (s["ip"] if d["tftp"] else "")
            lines.append("dhcp-boot=%s%s" % (d["boot_file"], ",,%s" % server if server else ""))
        if d["tftp"]:
            lines += ["enable-tftp", "tftp-root=/srv/tftp", "tftp-secure"]
        lines += [
            "dhcp-hostsfile=%s/hosts.dhcp" % conf_dir,
            "dhcp-leasefile=%s/dnsmasq.leases" % data_dir,
            "dhcp-script=%s" % hook,
            "dhcp-scriptuser=%s" % user,
            "log-dhcp",
        ]
    hosts_dhcp = ["# MAC,IP,Name[,Lease] – erzeugt vom WOL-Server"]
    for r in cfg["reservations"]:
        if not r["enabled"]:
            continue
        fields = [r["mac"], r["ip"]] + ([r["name"]] if r["name"] else [])
        if r["lease_time"]:
            fields.append(str(r["lease_time"]))
        hosts_dhcp.append(",".join(fields))
    hosts_dns = ["# IP Name – erzeugt vom WOL-Server"]
    if s["ip"]:
        hosts_dns.append("%s %s" % (s["ip"], s["name"]))
    for rec in cfg["records"]:
        if rec["type"] == "host":
            hosts_dns.append("%s %s" % (rec["value"], rec["name"]))
    # Reservierungen auch ohne aktive Lease auflösbar machen (z. B. schlafende Geräte)
    for r in cfg["reservations"]:
        if r["enabled"] and r["name"]:
            hosts_dns.append("%s %s" % (r["ip"], r["name"]))
    return {
        "dnsmasq.conf": "\n".join(lines) + "\n",
        "hosts.dhcp": "\n".join(hosts_dhcp) + "\n",
        "hosts.dns": "\n".join(hosts_dns) + "\n",
    }


# ---------------------------------------------------------------- Leases, Ereignisse, Journal


def parse_leases(text, now=None):
    """dnsmasq.leases: "<Ablauf> <MAC> <IP> <Name|*> <Client-ID|*>" (Ablauf 0 = unbegrenzt)."""
    now = time.time() if now is None else now
    leases = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] == "duid":
            continue
        try:
            expires = int(parts[0])
            mac = normalize_mac(parts[1]).lower()
            ip = str(ipaddress.ip_address(parts[2]))
        except ValueError:
            continue
        if expires and expires < now:
            continue
        leases.append({"expires": expires or None, "mac": mac, "ip": ip,
                       "hostname": "" if parts[3] == "*" else parts[3][:63]})
    return leases


def parse_lease_events(text, limit_per_mac=20):
    """Zeilen des Lease-Hooks: "<Zeit> <add|old|del> <MAC> <IP> [Name]" → {mac: {last_seen, events}}."""
    seen = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[1] not in ("add", "old", "del"):
            continue
        try:
            ts = float(parts[0])
            mac = normalize_mac(parts[2]).lower()
        except ValueError:
            continue
        entry = seen.setdefault(mac, {"last_seen": 0, "events": []})
        if parts[1] != "del":
            entry["last_seen"] = max(entry["last_seen"], ts)
        entry["events"].append({"ts": ts, "action": parts[1], "ip": parts[3], "name": parts[4] if len(parts) > 4 else ""})
        del entry["events"][:-limit_per_mac]
    return seen


_JOURNAL = re.compile(r"^(?P<ts>\d+(?:\.\d+)?)\s+\S+\s+dnsmasq-dhcp\[\d+\]:\s+(?:(?P<xid>\d+)\s+)?(?P<msg>.*)$")
_IGNORED = re.compile(r"^DHCP(?:DISCOVER|REQUEST)\((?P<iface>[^)]+)\)\s+(?:\S+\s+)?(?P<mac>[0-9a-f:]{17})\s+(?P<why>ignored|no address available)", re.I)
_CLIENT_NAME = re.compile(r"^client provides name:\s+(?P<name>.+)$", re.I)


def parse_unknown(journal_text):
    """Abgewiesene DHCP-Anfragen aus `journalctl -u wol-dns -o short-unix` → {mac: {...}}."""
    names_by_xid, result = {}, {}
    for line in (journal_text or "").splitlines():
        match = _JOURNAL.match(line.strip())
        if not match:
            continue
        msg, xid, ts = match.group("msg"), match.group("xid"), float(match.group("ts"))
        name = _CLIENT_NAME.match(msg)
        if name and xid:
            names_by_xid[xid] = name.group("name").strip()[:63]
            continue
        ignored = _IGNORED.match(msg)
        if not ignored:
            continue
        mac = ignored.group("mac").lower()
        entry = result.setdefault(mac, {"mac": mac, "name": "", "first": ts, "last": ts, "count": 0})
        entry["count"] += 1
        entry["first"], entry["last"] = min(entry["first"], ts), max(entry["last"], ts)
        if xid and xid in names_by_xid:
            entry["name"] = names_by_xid[xid]
    # Name kann auch nach der Abweisung geloggt werden (gleiche Transaktions-ID)
    return result


# ---------------------------------------------------------------- Import aus ISC dhcpd


_TOKEN = re.compile(r'\s*(?:(#[^\n]*)|("(?:[^"\\]|\\.)*")|([{};,])|([^\s{};,"#]+))')


def _tokens(text):
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match or match.end() == pos:
            if text[pos:].strip():
                raise NetError("dhcpd.conf: unerwartetes Zeichen bei Position %d" % pos)
            break
        pos = match.end()
        if match.group(1):
            continue
        token = match.group(2) or match.group(3) or match.group(4)
        if token:
            yield token


def _parse_blocks(tokens):
    """Liste von Anweisungen: (wörter, unterblock|None)."""
    statements, current = [], []
    for token in tokens:
        if token == ";":
            if current:
                statements.append((current, None))
            current = []
        elif token == "{":
            statements.append((current, _parse_blocks(tokens)))
            current = []
        elif token == "}":
            if current:
                statements.append((current, None))
            return statements
        else:
            current.append(token[1:-1] if token.startswith('"') else token)
    if current:
        statements.append((current, None))
    return statements


def import_dhcpd(text):
    """ISC-dhcpd-Konfiguration lesen → Vorschau {settings, reservations, warnings}. Ändert nichts."""
    if not isinstance(text, str) or not text.strip():
        raise NetError("Die Datei ist leer")
    if len(text) > 512 * 1024:
        raise NetError("Die Datei ist zu groß")
    statements = _parse_blocks(iter(list(_tokens(text))))
    settings, hosts, warnings = {}, [], []

    def option(words, target):
        if words[0] == "option" and len(words) >= 3:
            values = [w for w in words[2:] if w != ","]
            target[words[1]] = values
        elif words[0] in ("default-lease-time", "max-lease-time") and len(words) == 2:
            target[words[0]] = words[1]
        elif words[0] == "range" and len(words) >= 3:
            target["range"] = words[-2:]
        elif words[:2] == ["deny", "unknown-clients"]:
            target["deny-unknown"] = True
        elif words[0] == "authoritative":
            target["authoritative"] = True

    def walk(items, scope):
        for words, block in items:
            if not words:
                continue
            if block is None:
                option(words, scope)
                continue
            if words[0] == "host" and len(words) >= 2:
                host = {"name": words[1]}
                for hw, sub in block:
                    if sub is None and hw[:2] == ["hardware", "ethernet"] and len(hw) >= 3:
                        host["mac"] = hw[2]
                    elif sub is None and hw[0] == "fixed-address" and len(hw) >= 2:
                        host["ip"] = hw[1]
                    elif sub is None and hw[:2] == ["option", "host-name"] and len(hw) >= 3:
                        host["host-name"] = hw[2]
                    elif sub is None and hw[0] == "ddns-hostname" and len(hw) >= 2:
                        host["host-name"] = hw[1]
                hosts.append(host)
            elif words[0] == "subnet" and len(words) >= 4:
                scope.setdefault("subnets", []).append({"network": words[1], "netmask": words[3]})
                walk(block, scope)
            elif words[0] in ("group", "shared-network", "pool"):
                walk(block, scope)
            elif words[0] == "class":
                warnings.append("Klasse „%s“ wird nicht übernommen" % (words[1] if len(words) > 1 else "?"))
            else:
                walk(block, scope)

    walk(statements, settings)
    preview = {"settings": {}, "reservations": [], "warnings": warnings}
    subnets = settings.get("subnets") or []
    if len(subnets) > 1:
        warnings.append("Mehrere Subnetze gefunden – übernommen wird nur %s" % subnets[0]["network"])
    out = preview["settings"]
    if subnets:
        try:
            net = ipaddress.IPv4Network("%s/%s" % (subnets[0]["network"], subnets[0]["netmask"]))
            out["prefix"] = net.prefixlen
            out["network"] = str(net)
        except ValueError:
            warnings.append("Subnetz %s ist ungültig" % subnets[0])
    if "routers" in settings:
        out["gateway"] = settings["routers"][0]
    if "domain-name-servers" in settings:
        out["dns"] = settings["domain-name-servers"]
    if "ntp-servers" in settings:
        out["ntp"] = settings["ntp-servers"]
    if "domain-name" in settings:
        out["domain"] = settings["domain-name"][0]
    for key in ("default-lease-time",):
        if key in settings:
            try:
                out["lease_time"] = int(settings[key])
            except ValueError:
                pass
    out["mode"] = "known-only" if settings.get("deny-unknown") else "range"
    if "range" in settings:
        out["range_start"], out["range_end"] = settings["range"]
        if settings.get("deny-unknown"):
            warnings.append("Adressbereich %s–%s ignoriert: „deny unknown-clients“ bedeutet „nur bekannte Geräte“"
                            % tuple(settings["range"]))

    seen_macs, seen_ips, seen_names = set(), set(), set()
    for host in hosts:
        entry = {"original_name": host["name"], "mac": "", "ip": "", "name": "", "comment": "", "problems": []}
        try:
            entry["mac"] = _mac(host.get("mac"))
        except NetError as e:
            entry["problems"].append(str(e) if host.get("mac") else "keine MAC-Adresse")
        if host.get("ip"):
            try:
                entry["ip"] = str(_ipv4(host["ip"], "IP"))
            except NetError:
                entry["problems"].append("IP „%s“ ist keine Adresse (Hostname?)" % host["ip"])
        else:
            entry["problems"].append("keine feste Adresse")
        name = normalize_name(host.get("host-name") or host["name"])
        entry["name"] = name
        if name != host["name"].lower():  # reine Kleinschreibung ist keine Erwähnung wert
            entry["comment"] = "importiert als „%s“" % host["name"]
        if entry["mac"] in seen_macs:
            entry["problems"].append("MAC doppelt")
        if entry["ip"] and entry["ip"] in seen_ips:
            entry["problems"].append("IP doppelt")
        if name in seen_names:
            entry["problems"].append("Name doppelt")
        seen_macs.add(entry["mac"])
        seen_ips.add(entry["ip"])
        seen_names.add(name)
        preview["reservations"].append(entry)
    return preview


def annotate_import(cfg, preview, server_mac=None):
    """Vorschau gegen die aktuelle Konfiguration prüfen: eigener Server, Konflikte mit Vorhandenem."""
    server_ip = cfg["server"]["ip"]
    net = network_of(cfg) or (ipaddress.IPv4Network(preview["settings"]["network"])
                              if preview.get("settings", {}).get("network") else None)
    existing = {r["mac"]: r for r in cfg["reservations"]}
    ips = {r["ip"]: r for r in cfg["reservations"]}
    for item in preview.get("reservations") or []:
        if item["ip"] and (item["ip"] == server_ip or (server_mac and item["mac"] == server_mac)):
            item["problems"].append("WOL-Server selbst: feste Adresse statt Reservierung")
        elif item["ip"] and net is not None and ipaddress.IPv4Address(item["ip"]) not in net:
            item["problems"].append("IP liegt nicht im Netz %s" % net)
        if item["mac"] in existing:
            old = existing[item["mac"]]
            changes = [label for label, key in (("IP %s → %s" % (old["ip"], item["ip"]), "ip"),
                                                ("Name %s → %s" % (old["name"] or "–", item["name"]), "name"))
                       if old[key] != item[key]]
            item["exists"] = "ändert %s" % ", ".join(changes) if changes else "schon vorhanden"
        elif item["ip"] in ips:
            item["problems"].append("IP schon vergeben an %s" % (ips[item["ip"]]["name"] or ips[item["ip"]]["mac"]))
    return preview


def merge_import(cfg, preview, replace=False, take_settings=True):
    """Vorschau in eine Konfiguration übernehmen. Einträge mit Problemen werden übersprungen."""
    new = copy.deepcopy(cfg)
    st = preview.get("settings") or {}
    if take_settings:
        for key in ("gateway", "lease_time", "mode", "range_start", "range_end"):
            if key in st:
                new["dhcp"][key] = st[key]
        for key in ("ntp",):
            if key in st:
                new["dhcp"][key] = st[key]
        if "prefix" in st:
            new["server"]["prefix"] = st["prefix"]
        if st.get("domain") and not st["domain"].endswith(".local"):
            new["dns"]["domain"] = st["domain"]
        elif st.get("domain") and st["domain"] not in new["dhcp"]["search"]:
            new["dhcp"]["search"] = list(new["dhcp"]["search"]) + [st["domain"]]
        if new["dhcp"]["mode"] == "known-only":
            new["dhcp"]["range_start"] = new["dhcp"]["range_end"] = ""
    if replace:
        new["reservations"] = []
    existing = {r["mac"]: r for r in new["reservations"]}
    added = updated = skipped = 0
    for item in preview.get("reservations") or []:
        if item.get("problems") or item.get("skip"):
            skipped += 1
            continue
        if item["mac"] in existing:
            existing[item["mac"]].update(ip=item["ip"], name=item["name"])
            updated += 1
        else:
            r = {"id": new_id(), "mac": item["mac"], "ip": item["ip"], "name": item["name"],
                 "comment": item.get("comment", ""), "enabled": True, "lease_time": None}
            new["reservations"].append(r)
            existing[item["mac"]] = r
            added += 1
    return validate_config(new), {"added": added, "updated": updated, "skipped": skipped}
