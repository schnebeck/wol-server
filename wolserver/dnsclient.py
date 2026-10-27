"""Small DNS client (UDP) for test queries and the dnsmasq cache statistics."""
import ipaddress
import os
import socket
import struct

TYPES = {"A": 1, "NS": 2, "CNAME": 5, "PTR": 12, "MX": 15, "TXT": 16, "AAAA": 28, "SRV": 33}
TYPE_NAMES = {v: k for k, v in TYPES.items()}
RCODES = {0: "OK", 1: "Format error", 2: "Server failure", 3: "Name unknown (NXDOMAIN)", 4: "Not supported",
          5: "Refused"}
CLASS_IN, CLASS_CHAOS = 1, 3


class DnsError(Exception):
    pass


def reverse_name(ip):
    return ipaddress.ip_address(ip).reverse_pointer


def encode_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        try:
            raw = label.encode("idna") if label else b""
        except UnicodeError:
            raw = b""
        if not raw or len(raw) > 63:
            raise DnsError("Invalid name: %s" % name)
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def build_query(name, qtype, qclass=CLASS_IN, qid=None):
    qid = qid if qid is not None else struct.unpack("!H", os.urandom(2))[0]
    header = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)  # RD set
    return qid, header + encode_name(name) + struct.pack("!HH", qtype, qclass)


def _read_name(data, pos, depth=0):
    labels = []
    jumped, end = False, pos
    while True:
        if pos >= len(data) or depth > 20:
            raise DnsError("Response corrupted")
        length = data[pos]
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(data):
                raise DnsError("Response corrupted")
            pointer = ((length & 0x3F) << 8) | data[pos + 1]
            if not jumped:
                end = pos + 2
            pos, jumped, depth = pointer, True, depth + 1
            continue
        if length == 0:
            if not jumped:
                end = pos + 1
            return ".".join(labels), end
        labels.append(data[pos + 1:pos + 1 + length].decode("ascii", "replace"))
        pos += 1 + length


def _rdata(data, pos, rtype, length):
    raw = data[pos:pos + length]
    if rtype == 1 and length == 4:
        return str(ipaddress.IPv4Address(raw))
    if rtype == 28 and length == 16:
        return str(ipaddress.IPv6Address(raw))
    if rtype in (2, 5, 12):
        return _read_name(data, pos)[0]
    if rtype == 15:
        return "%d %s" % (struct.unpack("!H", raw[:2])[0], _read_name(data, pos + 2)[0])
    if rtype == 33:
        prio, weight, port = struct.unpack("!HHH", raw[:6])
        return "%d %d %d %s" % (prio, weight, port, _read_name(data, pos + 6)[0])
    if rtype == 16:
        parts, i = [], 0
        while i < len(raw):
            parts.append(raw[i + 1:i + 1 + raw[i]].decode("utf-8", "replace"))
            i += 1 + raw[i]
        return "".join(parts)
    return raw.hex()


def parse_response(data, qid=None):
    if len(data) < 12:
        raise DnsError("Response too short")
    rid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
    if qid is not None and rid != qid:
        raise DnsError("Response does not match the query")
    pos = 12
    for _ in range(qd):
        _, pos = _read_name(data, pos)
        pos += 4
    answers = []
    for _ in range(an):
        name, pos = _read_name(data, pos)
        rtype, _rclass, ttl, length = struct.unpack("!HHIH", data[pos:pos + 10])
        pos += 10
        answers.append({"name": name, "type": TYPE_NAMES.get(rtype, str(rtype)), "ttl": ttl,
                        "data": _rdata(data, pos, rtype, length)})
        pos += length
    rcode = flags & 0x0F
    return {"rcode": rcode, "status": RCODES.get(rcode, str(rcode)), "authoritative": bool(flags & 0x0400),
            "answers": answers}


def query(name, qtype="A", server="127.0.0.1", port=53, timeout=2.0, qclass=CLASS_IN):
    code = TYPES.get(qtype.upper()) if isinstance(qtype, str) else qtype
    if not code:
        raise DnsError("Unknown type: %s" % qtype)
    qid, packet = build_query(name, code, qclass)
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(packet, (server, port))
            while True:
                data, _ = sock.recvfrom(4096)
                try:
                    return parse_response(data, qid)
                except DnsError as e:
                    if "does not match" not in str(e):
                        raise
        except socket.timeout:
            raise DnsError("No response from %s (timeout)" % server) from None
        except OSError as e:
            raise DnsError("DNS server %s unreachable: %s" % (server, e.strerror or e)) from None


def lookup(text, server="127.0.0.1", port=53, timeout=2.0):
    """Test query as in the GUI: IP → PTR, otherwise A and AAAA (including CNAME chains)."""
    text = (text or "").strip().rstrip(".")
    if not text or len(text) > 253:
        raise DnsError("Enter a name or an IP address")
    try:
        ipaddress.ip_address(text)
        res = query(reverse_name(text), "PTR", server, port, timeout)
        res["query"], res["qtype"] = text, "PTR"
        return res
    except ValueError:
        pass
    res = query(text, "A", server, port, timeout)
    try:
        six = query(text, "AAAA", server, port, timeout)
        known = {(a["type"], a["data"]) for a in res["answers"]}
        res["answers"] += [a for a in six["answers"] if (a["type"], a["data"]) not in known]
    except DnsError:
        pass
    res["query"], res["qtype"] = text, "A/AAAA"
    return res


def dnsmasq_stats(server="127.0.0.1", port=53, timeout=1.5):
    """Cache figures via the dnsmasq CHAOS TXT queries (cachesize.bind, hits.bind …)."""
    stats = {}
    for key in ("cachesize", "insertions", "evictions", "misses", "hits"):
        res = query("%s.bind" % key, TYPES["TXT"], server, port, timeout, CLASS_CHAOS)
        try:
            stats[key] = int(res["answers"][0]["data"]) if res["answers"] else None
        except ValueError:
            stats[key] = None
    try:
        res = query("servers.bind", TYPES["TXT"], server, port, timeout, CLASS_CHAOS)
        servers = []
        for answer in res["answers"]:
            parts = answer["data"].split()
            if len(parts) >= 3:
                servers.append({"server": parts[0].split("#")[0], "queries": int(parts[1]), "failed": int(parts[2])})
        stats["servers"] = servers
    except (DnsError, ValueError):
        stats["servers"] = []
    return stats
