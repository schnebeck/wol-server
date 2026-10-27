"""Kommandozeile: `python3 -m wolserver [serve|passwd|wake|wifi-daemon|cert-fetch]`."""

import argparse
import getpass
import logging
import os
import signal
import sys
import threading

from . import __version__
from .config import load_config


def cmd_serve(cfg):
    from .server import App, create_https_server, create_server

    app = App(cfg)
    servers = [s for s in (create_server(app), create_https_server(app)) if s]
    log = logging.getLogger("wolserver")
    log.info("WOL-Server %s lauscht auf %s, Daten in %s", __version__,
             ", ".join("[%s]:%d" % s.server_address[:2] for s in servers), cfg["data_dir"])
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    app.start()
    for server in servers:
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 5}, daemon=True).start()
    stop.wait()
    log.info("Beende …")
    app.stop()
    for server in servers:
        server.shutdown()
        server.server_close()
    return 0


def cmd_cert_fetch(cfg):
    import grp

    from . import tls

    try:
        data = tls.fetch(cfg)
        try:
            gid = grp.getgrnam("wol").gr_gid
        except KeyError:
            gid = None
        changed = tls.install_bundle(data, cfg["tls_dir"], cfg["https_host"], gid)
    except (tls.FetchError, OSError) as e:
        print("Zertifikat nicht abgeholt: %s" % e, file=sys.stderr)
        return 1
    print("Zertifikat erneuert." if changed else "Zertifikat unverändert.")
    return 0


def cmd_passwd(cfg, from_stdin):
    from .auth import Auth

    os.makedirs(cfg["data_dir"], exist_ok=True)
    auth = Auth(cfg["data_dir"], cfg["session_days"])
    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Neues Passwort: ")
        if password != getpass.getpass("Wiederholen: "):
            print("Passwörter stimmen nicht überein.", file=sys.stderr)
            return 1
    try:
        auth.set_password(password)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print("Passwort gesetzt. Bestehende Sitzungen wurden abgemeldet.")
    return 0


def cmd_wake(mac, broadcast, port):
    from .wol import normalize_mac, send_magic_packet

    send_magic_packet(mac, broadcast, port)
    print("Magic Packet an %s via %s:%d gesendet." % (normalize_mac(mac), broadcast, port))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wol-server", description="Wake-on-LAN-Manager")
    parser.add_argument("-c", "--config", help="Pfad zur config.json (Standard: /etc/wol-server/config.json)")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="Web-Server und Zeitplaner starten (Standard)")
    pw = sub.add_parser("passwd", help="Passwort für die Web-GUI setzen")
    pw.add_argument("--stdin", action="store_true", help="Passwort von stdin lesen")
    wake = sub.add_parser("wake", help="Magic Packet direkt senden")
    wake.add_argument("mac")
    wake.add_argument("--broadcast", default="255.255.255.255")
    wake.add_argument("--port", type=int, default=9)
    sub.add_parser("wifi-daemon", help="WLAN-Verwaltung mit Einrichtungs-Hotspot (als root, Dienst wol-wifi)")
    sub.add_parser("cert-fetch", help="HTTPS-Zertifikat vom Webserver abholen (als root, Dienst wol-cert)")
    args = parser.parse_args(argv)

    if args.cmd == "wake":
        return cmd_wake(args.mac, args.broadcast, args.port)

    logging.basicConfig(format="%(levelname)s %(message)s", level=logging.INFO)
    cfg = load_config(args.config)
    logging.getLogger().setLevel(str(cfg["log_level"]).upper())
    if args.cmd == "passwd":
        return cmd_passwd(cfg, args.stdin)
    if args.cmd == "cert-fetch":
        return cmd_cert_fetch(cfg)
    if args.cmd == "wifi-daemon":
        from .wifi import run_daemon

        return run_daemon(cfg)
    return cmd_serve(cfg)


if __name__ == "__main__":
    sys.exit(main())
