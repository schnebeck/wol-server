"""Kommandozeile: `python3 -m wolserver [serve|passwd|wake|wifi-daemon]`."""

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
    from .server import App, create_server

    app = App(cfg)
    server = create_server(app)
    host, port = server.server_address[:2]
    logging.getLogger("wolserver").info(
        "WOL-Server %s lauscht auf [%s]:%d, Daten in %s", __version__, host, port, cfg["data_dir"]
    )
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    app.start()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 5}, daemon=True)
    thread.start()
    stop.wait()
    logging.getLogger("wolserver").info("Beende …")
    app.stop()
    server.shutdown()
    server.server_close()
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
    args = parser.parse_args(argv)

    if args.cmd == "wake":
        return cmd_wake(args.mac, args.broadcast, args.port)

    logging.basicConfig(format="%(levelname)s %(message)s", level=logging.INFO)
    cfg = load_config(args.config)
    logging.getLogger().setLevel(str(cfg["log_level"]).upper())
    if args.cmd == "passwd":
        return cmd_passwd(cfg, args.stdin)
    if args.cmd == "wifi-daemon":
        from .wifi import run_daemon

        return run_daemon(cfg)
    return cmd_serve(cfg)


if __name__ == "__main__":
    sys.exit(main())
