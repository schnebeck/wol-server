"""Wake-on-LAN Magic Packets."""

import re
import socket
import time

_MAC_DIGITS = re.compile(r"^[0-9a-f]{12}$")


def normalize_mac(mac):
    """Accepts AA:BB:.., AA-BB-.., AABB.CCDD.., aabbcc.. and returns AA:BB:CC:DD:EE:FF."""
    digits = re.sub(r"[\s:.\-]", "", str(mac)).lower()
    if not _MAC_DIGITS.match(digits):
        raise ValueError("Invalid MAC address: %s" % mac)
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)).upper()


def magic_packet(mac, reverse=False):
    """6x 0xFF + 16x MAC. reverse=True creates the Sleep-on-LAN packet (MAC reversed)."""
    raw = bytes.fromhex(normalize_mac(mac).replace(":", ""))
    if reverse:
        raw = raw[::-1]
    return b"\xff" * 6 + raw * 16


def send_magic_packet(mac, broadcast="255.255.255.255", port=9, count=5, reverse=False, source_ip=None):
    """Sends the packet count times (Wi-Fi broadcasts are not acknowledged and get lost more easily).

    source_ip binds the socket to the address of the LAN/Wi-Fi interface; Linux then sends a
    broadcast to 255.255.255.255 through exactly this interface - even if a VPN has taken over
    the default route.
    """
    packet = magic_packet(mac, reverse=reverse)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if source_ip:
            sock.bind((source_ip, 0))
        for i in range(count):
            if i:
                time.sleep(0.05)
            sock.sendto(packet, (broadcast, port))
