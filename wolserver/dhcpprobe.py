"""Search for DHCP servers on the network (send DHCPDISCOVER, collect offers).

Called as root by wol-dns-apply.service (port 68 and SO_BINDTODEVICE need
privileges):  python3 -m wolserver.dhcpprobe --iface wlan0 --mac b8:27:eb:… --out probe.json

The probe is sent with the own MAC: servers in "known devices only" mode answer only known
clients, and the WOL-Server is usually known to the old server. An offer is never
accepted (no DHCPREQUEST), so nothing changes on the network.
"""
import argparse
import ipaddress
import json
import os
import socket
import struct
import sys
import time

MAGIC = b"\x63\x82\x53\x63"
DISCOVER, OFFER = 1, 2


def build_discover(mac, xid):
    chaddr = bytes.fromhex(mac.replace(":", "")) + b"\x00" * 10
    header = struct.pack("!BBBBIHH4s4s4s4s16s64s128s", 1, 1, 6, 0, xid, 0, 0x8000,
                         b"\x00" * 4, b"\x00" * 4, b"\x00" * 4, b"\x00" * 4, chaddr, b"", b"")
    options = bytes([53, 1, DISCOVER, 55, 5, 1, 3, 6, 51, 54]) + b"\xff"
    return header + MAGIC + options


def parse_options(data):
    options, i = {}, 0
    while i < len(data):
        code = data[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(data):
            break
        length = data[i + 1]
        options[code] = data[i + 2:i + 2 + length]
        i += 2 + length
    return options


def parse_offer(packet, xid):
    if len(packet) < 240 or packet[0] != 2 or packet[236:240] != MAGIC:
        return None
    if struct.unpack("!I", packet[4:8])[0] != xid:
        return None
    options = parse_options(packet[240:])
    if options.get(53, b"\x00")[:1] != bytes([OFFER]):
        return None
    server = options.get(54)
    router = options.get(3)
    return {
        "offered": str(ipaddress.IPv4Address(packet[16:20])),
        "server_id": str(ipaddress.IPv4Address(server[:4])) if server and len(server) >= 4 else None,
        "router": str(ipaddress.IPv4Address(router[:4])) if router and len(router) >= 4 else None,
        "lease_time": struct.unpack("!I", options[51][:4])[0] if len(options.get(51, b"")) >= 4 else None,
    }


def probe(iface, mac, timeout=4.0):
    xid = struct.unpack("!I", os.urandom(4))[0]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if iface:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
        sock.bind(("", 68))
        sock.sendto(build_discover(mac, xid), ("255.255.255.255", 67))
        offers, seen = [], set()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                packet, (source, _port) = sock.recvfrom(2048)
            except socket.timeout:
                break
            offer = parse_offer(packet, xid)
            if offer and (offer["server_id"] or source) not in seen:
                offer["source"] = source
                seen.add(offer["server_id"] or source)
                offers.append(offer)
    return offers


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search for DHCP servers")
    parser.add_argument("--iface", default="wlan0")
    parser.add_argument("--mac", required=True)
    parser.add_argument("--out")
    parser.add_argument("--timeout", type=float, default=4.0)
    args = parser.parse_args(argv)
    result = {"ts": time.time(), "iface": args.iface}
    try:
        result["servers"] = probe(args.iface, args.mac.lower(), args.timeout)
        result["ok"] = True
    except OSError as e:
        result["ok"], result["error"], result["servers"] = False, str(e), []
    text = json.dumps(result, ensure_ascii=False)
    if args.out:
        tmp = args.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, args.out)
    else:
        print(text)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
