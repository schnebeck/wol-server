"""Wake-on-LAN Magic Packets."""

import re
import socket
import time

_MAC_DIGITS = re.compile(r"^[0-9a-f]{12}$")


def normalize_mac(mac):
    """Akzeptiert AA:BB:.., AA-BB-.., AABB.CCDD.., aabbcc.. und liefert AA:BB:CC:DD:EE:FF."""
    digits = re.sub(r"[\s:.\-]", "", str(mac)).lower()
    if not _MAC_DIGITS.match(digits):
        raise ValueError("Ungültige MAC-Adresse: %s" % mac)
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)).upper()


def magic_packet(mac, reverse=False):
    """6×0xFF + 16×MAC. reverse=True erzeugt das Sleep-on-LAN-Paket (MAC rückwärts)."""
    raw = bytes.fromhex(normalize_mac(mac).replace(":", ""))
    if reverse:
        raw = raw[::-1]
    return b"\xff" * 6 + raw * 16


def send_magic_packet(mac, broadcast="255.255.255.255", port=9, count=5, reverse=False, source_ip=None):
    """Sendet das Paket count-mal (WLAN-Broadcasts werden nicht bestätigt und gehen leichter verloren).

    source_ip bindet den Socket an die Adresse des LAN/WLAN-Interfaces; Linux schickt einen
    Broadcast an 255.255.255.255 dann über genau dieses Interface – auch wenn ein VPN die
    Default-Route übernommen hat.
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
