"""DHCP and DNS with dnsmasq: data model, validation, configuration, import and evaluation.

Pure logic without side effects - used by the root service (netsvc) and in the tests. The GUI
only supplies structured data; the dnsmasq configuration is built exclusively here from
validated fields, so no options (e.g. dhcp-script) can be injected.
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


# ---------------------------------------------------------------- Names and addresses


def normalize_name(raw):
    """Turn a device name into a valid DNS name (RFC 1123): "Anna's iPhone" → annas-iphone."""
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
    """Locally administered MAC (second-lowest bit of the first byte): typical for "private Wi-Fi addresses"."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, AttributeError, IndexError):
        return False


def _mac(value, what="MAC address"):
    try:
        mac = normalize_mac(value).lower()
    except (ValueError, AttributeError, TypeError):
        raise NetError("Invalid %s: %s" % (what, value)) from None
    if int(mac[:2], 16) & 0x01:
        raise NetError("%s %s is a multicast address" % (what, mac))
    return mac


def _ipv4(value, what):
    try:
        ip = ipaddress.IPv4Address(str(value).strip())
    except (ipaddress.AddressValueError, ValueError):
        raise NetError("%s: not a valid IPv4 address (%s)" % (what, value)) from None
    return ip


def _ip_any(value, what):
    try:
        return ipaddress.ip_address(str(value).strip())
    except ValueError:
        raise NetError("%s: not a valid IP address (%s)" % (what, value)) from None


def _text(value, what, maxlen, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise NetError("%s: text expected" % what)
    value = value.strip()
    if required and not value:
        raise NetError("%s must not be empty" % what)
    if len(value) > maxlen:
        raise NetError("%s is too long (max. %d characters)" % (what, maxlen))
    if any(ord(c) < 32 for c in value):
        raise NetError("%s contains control characters" % what)
    return value


def _int(value, what, lo, hi):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise NetError("%s: number expected" % what) from None
    if not lo <= number <= hi:
        raise NetError("%s must be between %d and %d" % (what, lo, hi))
    return number


def _ip_list(values, what, limit=4):
    if values in (None, ""):
        return []
    if isinstance(values, str):
        values = [v for v in re.split(r"[\s,;]+", values) if v]
    if not isinstance(values, list) or len(values) > limit:
        raise NetError("%s: at most %d addresses" % (what, limit))
    return [str(_ipv4(v, what)) for v in values]


def parse_lease_time(value):
    """Seconds from 600, "600", "10m", "12h", "1d" or "7 Tage" (German units accepted) - checked for 2 min to 7 days."""
    if isinstance(value, bool):
        raise NetError("Invalid lease time")
    if isinstance(value, (int, float)):
        seconds = int(value)
    else:
        match = re.match(r"^\s*(\d+)\s*(s|sek|m|min|h|std|d|t|tag|tage)?\s*$", str(value or "").lower())
        if not match:
            raise NetError("Invalid lease time: %s (e.g. 600, 30m, 12h, 1d)" % value)
        factor = {"s": 1, "sek": 1, "m": 60, "min": 60, "h": 3600, "std": 3600, "d": 86400, "t": 86400,
                  "tag": 86400, "tage": 86400}.get(match.group(2) or "s")
        seconds = int(match.group(1)) * factor
    if not LEASE_MIN <= seconds <= LEASE_MAX:
        raise NetError("Lease time must be between 2 minutes and 7 days")
    return seconds


def fmt_lease_time(seconds):
    if seconds % 86400 == 0:
        days = seconds // 86400
        return "%d day%s" % (days, "" if days == 1 else "s")
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


# ---------------------------------------------------------------- Validation


def validate_reservation(data, net=None):
    if not isinstance(data, dict):
        raise NetError("Reservation: object expected")
    mac = _mac(data.get("mac"))
    ip = _ipv4(data.get("ip"), "IP address")
    if net is not None:
        if ip not in net:
            raise NetError("%s is outside the network %s" % (ip, net))
        if ip in (net.network_address, net.broadcast_address):
            raise NetError("%s is the network or broadcast address" % ip)
    raw_name = _text(data.get("name"), "Name", 63)
    name = normalize_name(raw_name)
    if raw_name and not name:
        raise NetError('Name "%s" contains no valid character' % raw_name)
    lease = data.get("lease_time")
    return {
        "id": data.get("id") if isinstance(data.get("id"), str) and re.match(r"^\w{1,32}$", data["id"]) else new_id(),
        "mac": mac,
        "ip": str(ip),
        "name": name,
        "comment": _text(data.get("comment"), "Comment", 120),
        "enabled": bool(data.get("enabled", True)),
        "lease_time": None if lease in (None, "", 0) else parse_lease_time(lease),
    }


def validate_record(data):
    if not isinstance(data, dict):
        raise NetError("DNS record: object expected")
    rtype = data.get("type")
    if rtype not in RECORD_TYPES:
        raise NetError("Unknown record type: %s" % rtype)
    raw_name = _text(data.get("name"), "Name", 63, required=rtype != "mx").lower()
    record = {
        "id": data.get("id") if isinstance(data.get("id"), str) and re.match(r"^\w{1,32}$", data["id"]) else new_id(),
        "type": rtype, "name": raw_name, "comment": _text(data.get("comment"), "Comment", 120),
    }
    if rtype == "srv":
        if not _SRV_NAME.match(raw_name):
            raise NetError("SRV name in the format _service._tcp, e.g. _ldap._tcp")
    elif raw_name and not valid_label(raw_name):
        raise NetError('Invalid name "%s": only a-z, 0-9 and -, no dots (the domain is appended)' % raw_name)
    value = _text(data.get("value"), "Value", 255, required=True)
    if rtype == "host":
        value = str(_ip_any(value, "IP address"))
    elif rtype in ("alias", "mx", "srv"):
        value = value.lower().rstrip(".")
        if not valid_domain(value):
            raise NetError('Target "%s" is not a valid name' % value)
    elif rtype == "txt":
        if '"' in value or "\\" in value:
            raise NetError("TXT text must not contain quotes or \\")
    record["value"] = value
    if rtype == "mx":
        record["priority"] = _int(data.get("priority", 10), "Priority", 0, 65535)
    if rtype == "srv":
        record["port"] = _int(data.get("port"), "Port", 1, 65535)
        record["priority"] = _int(data.get("priority", 0), "Priority", 0, 65535)
        record["weight"] = _int(data.get("weight", 0), "Weight", 0, 65535)
    return record


def validate_settings(cfg, data):
    """Take over basic settings from the GUI. Returns a new configuration."""
    if not isinstance(data, dict):
        raise NetError("Settings: object expected")
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
    """Validate and normalize the whole configuration (also on import and before applying)."""
    if not isinstance(data, dict):
        raise NetError("Configuration: object expected")
    cfg = copy.deepcopy(DEFAULT_NETWORK)
    for section in ("server", "dhcp", "dns"):
        if isinstance(data.get(section), dict):
            cfg[section].update({k: v for k, v in data[section].items() if k in cfg[section]})
    s, d, n = cfg["server"], cfg["dhcp"], cfg["dns"]

    s["interface"] = _text(s["interface"], "Interface", 15, required=True)
    if not _IFACE.match(s["interface"]):
        raise NetError("Invalid interface: %s" % s["interface"])
    s["prefix"] = _int(s["prefix"], "Netmask (prefix)", 8, 30)
    s["port"] = _int(s["port"], "DNS-Port", 1, 65535)
    s["name"] = normalize_name(_text(s["name"], "Server name", 63)) or "wol"
    s["ip"] = str(_ipv4(s["ip"], "Server address")) if s["ip"] else ""
    net = network_of(cfg)

    n["enabled"] = bool(n["enabled"])
    n["domain"] = _text(n["domain"], "Domain", 253, required=True).lower().rstrip(".")
    if not valid_domain(n["domain"]) or "." not in n["domain"]:
        raise NetError("Invalid domain: %s (e.g. home.arpa)" % n["domain"])
    if n["domain"].endswith(".local") or n["domain"] == "local":
        raise NetError("Domains under .local are reserved for mDNS (alternative e.g. home.arpa)")
    n["upstream"] = _ip_list(n["upstream"], "Upstream DNS")
    n["ttl"] = _int(n["ttl"], "TTL", 0, 86400)
    n["cache_size"] = _int(n["cache_size"], "Cache size", 0, 10000)
    n["log_queries"] = bool(n["log_queries"])
    conditional = []
    for item in n["conditional"] or []:
        if not isinstance(item, dict):
            raise NetError("Conditional forwarding: object expected")
        domain = _text(item.get("domain"), "Forwarding domain", 253, required=True).lower().rstrip(".")
        if not valid_domain(domain):
            raise NetError("Invalid forwarding domain: %s" % domain)
        if domain == n["domain"] or domain.endswith("." + n["domain"]):
            raise NetError("%s is part of the local domain and cannot be forwarded" % domain)
        conditional.append({"domain": domain, "server": str(_ipv4(item.get("server"), "Server for %s" % domain))})
    if len(conditional) > 20:
        raise NetError("At most 20 conditional forwardings")
    n["conditional"] = conditional

    d["enabled"] = bool(d["enabled"])
    if d["mode"] not in MODES:
        raise NetError("Invalid DHCP mode: %s" % d["mode"])
    d["lease_time"] = parse_lease_time(d["lease_time"])
    d["gateway"] = str(_ipv4(d["gateway"], "Gateway")) if d["gateway"] else ""
    d["dns"] = _ip_list(d["dns"], "DNS servers for clients")
    d["ntp"] = _ip_list(d["ntp"], "NTP servers")
    search = d["search"] or []
    if isinstance(search, str):
        search = [v for v in re.split(r"[\s,;]+", search) if v]
    if not isinstance(search, list) or len(search) > 6:
        raise NetError("At most 6 additional search domains")
    d["search"] = []
    for item in search:
        item = _text(item, "Search domain", 253).lower().rstrip(".")
        if not valid_domain(item):
            raise NetError("Invalid search domain: %s" % item)
        if item != n["domain"] and item not in d["search"]:
            d["search"].append(item)
    d["boot_file"] = _text(d["boot_file"], "Boot file", 128)
    if d["boot_file"] and (not _BOOT_FILE.match(d["boot_file"]) or ".." in d["boot_file"]):
        raise NetError("Invalid boot file (only letters, digits, . _ - /)")
    d["boot_server"] = str(_ipv4(d["boot_server"], "TFTP server")) if d["boot_server"] else ""
    d["tftp"] = bool(d["tftp"])
    for key in ("range_start", "range_end"):
        d[key] = str(_ipv4(d[key], "Address range")) if d[key] else ""

    if net is not None:
        for what, ip in (("Gateway", d["gateway"]), ("Range start", d["range_start"]), ("Range end", d["range_end"])):
            if ip and ipaddress.IPv4Address(ip) not in net:
                raise NetError("%s %s is outside the network %s" % (what, ip, net))
    if d["mode"] == "range":
        if not (d["range_start"] and d["range_end"]):
            raise NetError('Mode "address range" needs a start and an end')
        if ipaddress.IPv4Address(d["range_start"]) > ipaddress.IPv4Address(d["range_end"]):
            raise NetError("The address range starts after its end")
    if d["enabled"]:
        if net is None:
            raise NetError("DHCP requires a static IP address for the server (Settings → Server)")
        if not d["gateway"]:
            raise NetError("DHCP requires a gateway (router)")
    if n["enabled"] and not n["upstream"]:
        raise NetError("DNS requires at least one upstream server (e.g. the router)")

    reservations, records, ignored = [], [], []
    for item in data.get("reservations") or []:
        reservations.append(validate_reservation(item, net))
    for item in data.get("records") or []:
        records.append(validate_record(item))
    for item in data.get("ignored") or []:
        if isinstance(item, dict):
            ignored.append({"mac": _mac(item.get("mac")), "comment": _text(item.get("comment"), "Comment", 120),
                            "ts": float(item.get("ts") or 0)})
    if len(reservations) > MAX_RESERVATIONS or len(records) > MAX_RECORDS or len(ignored) > MAX_IGNORED:
        raise NetError("Too many entries")
    cfg["reservations"], cfg["records"], cfg["ignored"] = reservations, records, ignored
    check_conflicts(cfg)
    return cfg


def check_conflicts(cfg):
    """Uniqueness of MAC, IP and names; aliases must point to known names."""
    net = network_of(cfg)
    d = cfg["dhcp"]
    server_ip = cfg["server"]["ip"]
    macs, ips, names = {}, {}, {cfg["server"]["name"]: "the WOL-Server"}
    rng = None
    if d["mode"] == "range" and d["range_start"] and d["range_end"]:
        rng = (ipaddress.IPv4Address(d["range_start"]), ipaddress.IPv4Address(d["range_end"]))
    for r in cfg["reservations"]:
        label = r["name"] or r["mac"]
        if r["mac"] in macs:
            raise NetError("MAC %s is duplicated (%s and %s)" % (r["mac"], macs[r["mac"]], label))
        macs[r["mac"]] = label
        if r["ip"] in ips:
            raise NetError("IP %s is assigned twice (%s and %s)" % (r["ip"], ips[r["ip"]], label))
        if r["ip"] == server_ip:
            raise NetError("IP %s belongs to the WOL-Server itself (%s)" % (r["ip"], label))
        ips[r["ip"]] = label
        if rng and rng[0] <= ipaddress.IPv4Address(r["ip"]) <= rng[1]:
            raise NetError("IP %s of %s is in the dynamic range; reservations must be outside it" % (r["ip"], label))
        if net is not None and ipaddress.IPv4Address(r["ip"]) not in net:
            raise NetError("IP %s of %s is outside the network %s" % (r["ip"], label, net))
        if r["name"]:
            if r["name"] in names:
                raise NetError('Name "%s" is duplicated (%s)' % (r["name"], names[r["name"]]))
            names[r["name"]] = "reservation %s" % r["ip"]
    # Host records may share a name (e.g. IPv4 and IPv6); otherwise every name is unique.
    host_names = set()
    for rec in cfg["records"]:
        if rec["type"] == "host":
            if rec["name"] in names:
                raise NetError('Name "%s" is already taken (%s)' % (rec["name"], names[rec["name"]]))
            host_names.add(rec["name"])
    for rec in cfg["records"]:
        if rec["type"] == "alias":
            if rec["name"] in names or rec["name"] in host_names:
                raise NetError('Alias "%s" collides with an existing name' % rec["name"])
            names[rec["name"]] = "Alias"
    local = set(names) | host_names
    domain = cfg["dns"]["domain"]
    for rec in cfg["records"]:
        if rec["type"] == "alias":
            target = rec["value"]
            short = target[: -len(domain) - 1] if target.endswith("." + domain) else target
            if short not in local:
                raise NetError('Alias "%s": target "%s" is not a local name '
                               "(reservation, record or WOL-Server)" % (rec["name"], target))
            if short == rec["name"]:
                raise NetError('Alias "%s" points to itself' % rec["name"])
    return True


# ---------------------------------------------------------------- Helpers for the GUI


def next_free_ip(cfg, leases=(), start_host=10):
    """Lowest free address from .start_host on: not server, gateway, reservation, record, lease or range."""
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
    """Find a free name: annas-iphone, annas-iphone-2, …"""
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
    ("server", "ip"): "Server address", ("server", "prefix"): "Netmask", ("server", "name"): "Server name",
    ("server", "interface"): "Interface", ("server", "port"): "DNS port",
    ("dhcp", "enabled"): "DHCP", ("dhcp", "mode"): "DHCP mode", ("dhcp", "range_start"): "Range start",
    ("dhcp", "range_end"): "Range end", ("dhcp", "lease_time"): "Lease time", ("dhcp", "gateway"): "Gateway",
    ("dhcp", "dns"): "DNS servers for clients", ("dhcp", "ntp"): "NTP servers", ("dhcp", "search"): "Search domains",
    ("dhcp", "boot_file"): "Boot file", ("dhcp", "boot_server"): "TFTP server", ("dhcp", "tftp"): "Own TFTP server",
    ("dns", "enabled"): "DNS", ("dns", "domain"): "Domain", ("dns", "upstream"): "Upstream DNS",
    ("dns", "conditional"): "Conditional forwarding", ("dns", "ttl"): "TTL", ("dns", "cache_size"): "Cache size",
    ("dns", "log_queries"): "Query log",
}


def _fmt_value(section, key, value):
    if isinstance(value, bool):
        return "on" if value else "off"
    if key == "lease_time":
        return fmt_lease_time(value)
    if key == "mode":
        return "known devices only" if value == "known-only" else "address range"
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
    """Readable list of the changes from old to new (for the Apply bar and the History)."""
    changes = []
    for (section, key), label in SETTING_LABELS.items():
        a, b = old[section].get(key), new[section].get(key)
        if a != b:
            changes.append("%s: %s → %s" % (label, _fmt_value(section, key, a), _fmt_value(section, key, b)))
    olds = {r["id"]: r for r in old["reservations"]}
    news = {r["id"]: r for r in new["reservations"]}
    for rid, r in news.items():
        if rid not in olds:
            changes.append("New reservation: %s" % _res_label(r))
        elif olds[rid] != r:
            o = olds[rid]
            parts = [p for p, k in (("IP %s → %s" % (o["ip"], r["ip"]), "ip"), ("Name %s → %s" % (o["name"] or "–", r["name"] or "–"), "name"),
                                    ("MAC %s → %s" % (o["mac"], r["mac"]), "mac"),
                                    ("active" if r["enabled"] else "inactive", "enabled"),
                                    ("Lease time", "lease_time"), ("Comment", "comment")) if o[k] != r[k]]
            changes.append("Reservation %s: %s" % (r["name"] or r["mac"], ", ".join(parts)))
    for rid, r in olds.items():
        if rid not in news:
            changes.append("Removed reservation: %s" % _res_label(r))
    oldr = {r["id"]: r for r in old["records"]}
    newr = {r["id"]: r for r in new["records"]}
    for rid, r in newr.items():
        if rid not in oldr:
            changes.append("New DNS record: %s" % _rec_label(r))
        elif oldr[rid] != r:
            changes.append("Changed DNS record: %s" % _rec_label(r))
    for rid, r in oldr.items():
        if rid not in newr:
            changes.append("Removed DNS record: %s" % _rec_label(r))
    oldi = {i["mac"] for i in old["ignored"]}
    newi = {i["mac"] for i in new["ignored"]}
    changes += ["Ignored: %s" % m for m in sorted(newi - oldi)]
    changes += ["No longer ignored: %s" % m for m in sorted(oldi - newi)]
    return changes


# ---------------------------------------------------------------- dnsmasq configuration

HOOK = "/opt/wol-server/deploy/dns-lease-hook.sh"


def render(cfg, conf_dir="/etc/wol-dns", data_dir="/var/lib/wol-dns", user="dnsmasq", hook=HOOK):
    """Files for dnsmasq: {"dnsmasq.conf": …, "hosts.dhcp": …, "hosts.dns": …}."""
    cfg = validate_config(cfg)
    s, d, n = cfg["server"], cfg["dhcp"], cfg["dns"]
    net = network_of(cfg)
    domain = n["domain"]
    lines = [
        "# Generated by WOL-Server - make changes in the web GUI (Network), not here.",
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
        def full(target):  # targets without a dot are local names
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
    hosts_dhcp = ["# MAC,IP,Name[,Lease] - generated by WOL-Server"]
    for r in cfg["reservations"]:
        if not r["enabled"]:
            continue
        fields = [r["mac"], r["ip"]] + ([r["name"]] if r["name"] else [])
        if r["lease_time"]:
            fields.append(str(r["lease_time"]))
        hosts_dhcp.append(",".join(fields))
    hosts_dns = ["# IP Name - generated by WOL-Server"]
    if s["ip"]:
        hosts_dns.append("%s %s" % (s["ip"], s["name"]))
    for rec in cfg["records"]:
        if rec["type"] == "host":
            hosts_dns.append("%s %s" % (rec["value"], rec["name"]))
    # Make reservations resolvable even without an active lease (e.g. sleeping devices)
    for r in cfg["reservations"]:
        if r["enabled"] and r["name"]:
            hosts_dns.append("%s %s" % (r["ip"], r["name"]))
    return {
        "dnsmasq.conf": "\n".join(lines) + "\n",
        "hosts.dhcp": "\n".join(hosts_dhcp) + "\n",
        "hosts.dns": "\n".join(hosts_dns) + "\n",
    }


# ---------------------------------------------------------------- Leases, events, journal


def parse_leases(text, now=None):
    """dnsmasq.leases: "<expiry> <MAC> <IP> <name|*> <client-ID|*>" (expiry 0 = unlimited)."""
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
    """Lines of the lease hook: "<time> <add|old|del> <MAC> <IP> [name]" → {mac: {last_seen, events}}."""
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
    """Rejected DHCP requests from `journalctl -u wol-dns -o short-unix` → {mac: {...}}."""
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
    # The name may also be logged after the rejection (same transaction ID)
    return result


# ---------------------------------------------------------------- Import from ISC dhcpd


_TOKEN = re.compile(r'\s*(?:(#[^\n]*)|("(?:[^"\\]|\\.)*")|([{};,])|([^\s{};,"#]+))')


def _tokens(text):
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match or match.end() == pos:
            if text[pos:].strip():
                raise NetError("dhcpd.conf: unexpected character at position %d" % pos)
            break
        pos = match.end()
        if match.group(1):
            continue
        token = match.group(2) or match.group(3) or match.group(4)
        if token:
            yield token


def _parse_blocks(tokens):
    """List of statements: (words, sub-block|None)."""
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
    """Read an ISC dhcpd configuration → preview {settings, reservations, warnings}. Changes nothing."""
    if not isinstance(text, str) or not text.strip():
        raise NetError("The file is empty")
    if len(text) > 512 * 1024:
        raise NetError("The file is too large")
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
                warnings.append('Class "%s" is not imported' % (words[1] if len(words) > 1 else "?"))
            else:
                walk(block, scope)

    walk(statements, settings)
    preview = {"settings": {}, "reservations": [], "warnings": warnings}
    subnets = settings.get("subnets") or []
    if len(subnets) > 1:
        warnings.append("Several subnets found - only %s is imported" % subnets[0]["network"])
    out = preview["settings"]
    if subnets:
        try:
            net = ipaddress.IPv4Network("%s/%s" % (subnets[0]["network"], subnets[0]["netmask"]))
            out["prefix"] = net.prefixlen
            out["network"] = str(net)
        except ValueError:
            warnings.append("Subnet %s is invalid" % subnets[0])
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
            warnings.append('Address range %s-%s ignored: "deny unknown-clients" means "known devices only"'
                            % tuple(settings["range"]))

    seen_macs, seen_ips, seen_names = set(), set(), set()
    for host in hosts:
        entry = {"original_name": host["name"], "mac": "", "ip": "", "name": "", "comment": "", "problems": []}
        try:
            entry["mac"] = _mac(host.get("mac"))
        except NetError as e:
            entry["problems"].append(str(e) if host.get("mac") else "no MAC address")
        if host.get("ip"):
            try:
                entry["ip"] = str(_ipv4(host["ip"], "IP"))
            except NetError:
                entry["problems"].append('IP "%s" is not an address (host name?)' % host["ip"])
        else:
            entry["problems"].append("no fixed address")
        name = normalize_name(host.get("host-name") or host["name"])
        entry["name"] = name
        if name != host["name"].lower():  # mere lowercasing is not worth a mention
            entry["comment"] = 'imported as "%s"' % host["name"]
        if entry["mac"] in seen_macs:
            entry["problems"].append("duplicate MAC")
        if entry["ip"] and entry["ip"] in seen_ips:
            entry["problems"].append("duplicate IP")
        if name in seen_names:
            entry["problems"].append("duplicate name")
        seen_macs.add(entry["mac"])
        seen_ips.add(entry["ip"])
        seen_names.add(name)
        preview["reservations"].append(entry)
    return preview


def annotate_import(cfg, preview, server_mac=None):
    """Check a preview against the current configuration: own server, conflicts with existing entries."""
    server_ip = cfg["server"]["ip"]
    net = network_of(cfg) or (ipaddress.IPv4Network(preview["settings"]["network"])
                              if preview.get("settings", {}).get("network") else None)
    existing = {r["mac"]: r for r in cfg["reservations"]}
    ips = {r["ip"]: r for r in cfg["reservations"]}
    for item in preview.get("reservations") or []:
        if item["ip"] and (item["ip"] == server_ip or (server_mac and item["mac"] == server_mac)):
            item["problems"].append("WOL-Server itself: static IP address instead of reservation")
        elif item["ip"] and net is not None and ipaddress.IPv4Address(item["ip"]) not in net:
            item["problems"].append("IP is outside the network %s" % net)
        if item["mac"] in existing:
            old = existing[item["mac"]]
            changes = [label for label, key in (("IP %s → %s" % (old["ip"], item["ip"]), "ip"),
                                                ("Name %s → %s" % (old["name"] or "–", item["name"]), "name"))
                       if old[key] != item[key]]
            item["exists"] = "changes %s" % ", ".join(changes) if changes else "already present"
        elif item["ip"] in ips:
            item["problems"].append("IP already assigned to %s" % (ips[item["ip"]]["name"] or ips[item["ip"]]["mac"]))
    return preview


def merge_import(cfg, preview, replace=False, take_settings=True):
    """Merge a preview into a configuration. Entries with problems are skipped."""
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
