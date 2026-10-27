"""Network info without external tools: IPv4 interfaces, default route, Wi-Fi signal.

Important when running with OpenVPN: if the VPN server pushes "redirect-gateway def1", a
broadcast to 255.255.255.255 would go through tun0 instead of into the home network. That is
why magic packets are bound to the address of the LAN/Wi-Fi interface (see lan_source()).
"""

import fcntl
import ipaddress
import socket
import struct

SIOCGIFFLAGS = 0x8913
SIOCGIFADDR = 0x8915
SIOCGIFBRDADDR = 0x8919
SIOCGIFNETMASK = 0x891B
IFF_UP = 0x1
IFF_BROADCAST = 0x2
IFF_LOOPBACK = 0x8
IFF_POINTOPOINT = 0x10

VPN_PREFIXES = ("tun", "tap", "wg", "tailscale", "ppp", "zt")
VIRTUAL_PREFIXES = ("docker", "veth", "br-", "virbr", "lxc")


def _ifreq(name):
    return struct.pack("256s", name.encode()[:15])


def _ioctl_addr(sock, name, request):
    return socket.inet_ntoa(fcntl.ioctl(sock.fileno(), request, _ifreq(name))[20:24])


def classify(name, flags):
    if flags & IFF_LOOPBACK:
        return "loopback"
    if name.startswith(VPN_PREFIXES) or flags & IFF_POINTOPOINT:
        return "vpn"
    if name.startswith(VIRTUAL_PREFIXES):
        return "virtual"
    if name.startswith("wl"):
        return "wifi"
    return "lan"


def interfaces():
    """All interfaces with (primary) IPv4 address, type and state."""
    result = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _, name in socket.if_nameindex():
            try:
                raw = fcntl.ioctl(sock.fileno(), SIOCGIFFLAGS, _ifreq(name))
            except OSError:
                continue
            flags = struct.unpack("H", raw[16:18])[0]
            info = {
                "name": name, "kind": classify(name, flags), "up": bool(flags & IFF_UP),
                "address": None, "netmask": None, "broadcast": None,
            }
            try:
                info["address"] = _ioctl_addr(sock, name, SIOCGIFADDR)
                info["netmask"] = _ioctl_addr(sock, name, SIOCGIFNETMASK)
                if flags & IFF_BROADCAST:
                    info["broadcast"] = _ioctl_addr(sock, name, SIOCGIFBRDADDR)
            except OSError:
                pass  # interface without IPv4 address
            result.append(info)
    return result


def ipv6_prefixes(path="/proc/net/if_inet6"):
    """Global IPv6 prefixes (incl. ULA) per interface: [(name, "fd00:1::/64")]."""
    result = []
    try:
        with open(path, encoding="ascii") as f:
            for line in f:
                fields = line.split()
                if len(fields) != 6 or fields[3] != "00":  # scope 00 = global; link-local fe80::/10 separately
                    continue
                addr = ipaddress.IPv6Address(int(fields[0], 16))
                net = ipaddress.IPv6Network("%s/%d" % (addr, int(fields[2], 16)), strict=False)
                result.append((fields[5], str(net)))
    except (OSError, ValueError):
        return []
    return result


def connected_networks():
    """Networks the machine is directly attached to (LAN, Wi-Fi, VPN), without virtual bridges.

    Result: [{"network": "192.168.1.0/24", "interface": "wlan0", "kind": "wifi"}, ...]
    """
    seen, result = set(), []
    kinds = {}
    for i in interfaces():
        kinds[i["name"]] = i["kind"]
        if not i["up"] or not i["address"] or not i["netmask"] or i["kind"] in ("loopback", "virtual"):
            continue
        net = str(ipaddress.IPv4Interface("%s/%s" % (i["address"], i["netmask"])).network)
        if net not in seen:
            seen.add(net)
            result.append({"network": net, "interface": i["name"], "kind": i["kind"]})
    for name, net in ipv6_prefixes():
        kind = kinds.get(name) or classify(name, 0)
        if kind not in ("loopback", "virtual") and net not in seen:
            seen.add(net)
            result.append({"network": net, "interface": name, "kind": kind})
    return result


def default_route_interfaces(path="/proc/net/route"):
    """Interfaces with an IPv4 default route (0.0.0.0/0), sorted by metric."""
    routes = []
    try:
        with open(path, encoding="ascii") as f:
            next(f)
            for line in f:
                fields = line.split()
                if len(fields) >= 8 and fields[1] == "00000000" and fields[7] == "00000000":
                    routes.append((int(fields[6]), fields[0]))
    except (OSError, StopIteration, ValueError):
        return []
    return [name for _, name in sorted(routes)]


def lan_source(preferred=""):
    """Interface through which magic packets reach the local network (dict from interfaces() or None).

    preferred: fixed configured interface (e.g. "wlan0"); empty = automatically via the
    default route, skipping VPN interfaces.
    """
    ifaces = [i for i in interfaces() if i["up"] and i["address"]]
    if preferred:
        return next((i for i in ifaces if i["name"] == preferred), None)
    by_name = {i["name"]: i for i in ifaces if i["kind"] in ("wifi", "lan")}
    for name in default_route_interfaces():
        if name in by_name:
            return by_name[name]
    candidates = sorted(by_name.values(), key=lambda i: i["kind"] != "lan")
    return candidates[0] if candidates else None


def parse_wireless(text):
    """Parses /proc/net/wireless -> {interface: {"link": 0..70, "signal_dbm": int}}."""
    result = {}
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        fields = rest.split()
        try:
            link = int(float(fields[1]))
            level = int(float(fields[2]))
        except (IndexError, ValueError):
            continue
        result[name.strip()] = {"link": link, "signal_dbm": level}
    return result


def wifi_signal(path="/proc/net/wireless"):
    try:
        with open(path, encoding="ascii") as f:
            return parse_wireless(f.read())
    except OSError:
        return {}
