"""Erreichbarkeitsprüfung: ICMP-Ping, ersatzweise TCP-Probes.

Ein TCP-RST (Verbindung abgelehnt) zählt ebenfalls als "online" – damit werden auch
Windows-Rechner erkannt, deren Firewall ICMP blockiert.
"""

import errno
import logging
import os
import select
import shutil
import socket
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("wolserver")

PROBE_PORTS = (22, 445, 3389, 80, 443, 139)
_ALIVE_ERRNOS = (0, errno.ECONNREFUSED)
_dgram_icmp_ok = True  # wird False, sobald der Kernel ICMP-Datagram-Sockets verweigert


def _checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def _resolve(host):
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    infos.sort(key=lambda i: i[0] != socket.AF_INET)  # IPv4 bevorzugen
    family, _, _, _, sockaddr = infos[0]
    return family, sockaddr[0]


def icmp_ping(family, addr, timeout):
    """True/False, oder None wenn unprivilegiertes ICMP nicht erlaubt ist."""
    global _dgram_icmp_ok
    if not _dgram_icmp_ok:
        return None
    v4 = family == socket.AF_INET
    proto = socket.IPPROTO_ICMP if v4 else socket.IPPROTO_ICMPV6
    request, reply = (8, 0) if v4 else (128, 129)
    try:
        sock = socket.socket(family, socket.SOCK_DGRAM, proto)
    except OSError:
        _dgram_icmp_ok = False
        log.info("ICMP-Datagram-Sockets nicht erlaubt (net.ipv4.ping_group_range) – nutze Fallback")
        return None
    with sock:
        seq = int.from_bytes(os.urandom(2), "big")
        payload = b"wol-server"
        header = struct.pack("!BBHHH", request, 0, 0, 0, seq)
        if v4:
            header = struct.pack("!BBHHH", request, 0, _checksum(header + payload), 0, seq)
        deadline = time.monotonic() + timeout
        try:
            sock.sendto(header + payload, (addr, 0))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                ready, _, _ = select.select([sock], [], [], remaining)
                if not ready:
                    return False
                data = sock.recv(1024)
                if len(data) >= 8:
                    rtype, _, _, _, rseq = struct.unpack("!BBHHH", data[:8])
                    if rtype == reply and rseq == seq:
                        return True
        except OSError:
            return False


def system_ping(addr, timeout):
    exe = shutil.which("ping")
    if not exe:
        return None
    try:
        res = subprocess.run(
            [exe, "-n", "-q", "-c", "1", "-W", str(max(1, round(timeout))), addr],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode == 0:
        return True
    return False if res.returncode == 1 else None  # 2 = Fehler (z. B. keine Rechte)


def tcp_probe(family, addr, ports=PROBE_PORTS, timeout=1.0):
    socks = {}
    try:
        for port in ports:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.setblocking(False)
            err = s.connect_ex((addr, port))
            if err in _ALIVE_ERRNOS:
                s.close()
                return True
            if err in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EAGAIN):
                socks[s] = port
            else:
                s.close()
        deadline = time.monotonic() + timeout
        while socks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _, writable, _ = select.select([], list(socks), [], remaining)
            for s in writable:
                if s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) in _ALIVE_ERRNOS:
                    return True
                socks.pop(s)
                s.close()
        return False
    finally:
        for s in socks:
            s.close()


def check_host(host, timeout=1.0):
    try:
        family, addr = _resolve(host)
    except OSError:
        return False
    alive = icmp_ping(family, addr, timeout)
    if alive is None:
        alive = system_ping(addr, timeout)
    if alive:
        return True
    return tcp_probe(family, addr, timeout=timeout)


class StatusMonitor:
    """Prüft Geräte nur bei Bedarf (wenn die GUI fragt) und cached das Ergebnis kurz."""

    def __init__(self, cache_seconds=5, workers=6, checker=check_host):
        self.cache_seconds = cache_seconds
        self._checker = checker
        self._cache = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="status")

    def get(self, devices):
        now = time.monotonic()
        result, todo = {}, []
        with self._lock:
            for dev in devices:
                if not dev.get("host"):
                    result[dev["id"]] = None
                    continue
                cached = self._cache.get(dev["id"])
                if cached and cached[1] == dev["host"] and now - cached[0] < self.cache_seconds:
                    result[dev["id"]] = cached[2]
                else:
                    todo.append(dev)
        futures = [(dev, self._pool.submit(self._checker, dev["host"])) for dev in todo]
        for dev, fut in futures:
            try:
                online = bool(fut.result())
            except Exception:  # noqa: BLE001 – ein defekter Check darf die API nicht stören
                log.exception("Statusprüfung für %s fehlgeschlagen", dev["host"])
                online = False
            result[dev["id"]] = online
            with self._lock:
                self._cache[dev["id"]] = (time.monotonic(), dev["host"], online)
        return result

    def invalidate(self, device_id):
        with self._lock:
            self._cache.pop(device_id, None)

    def shutdown(self):
        self._pool.shutdown(wait=False)
