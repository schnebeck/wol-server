# WOL-Server - wolserver/__main__.py
# Command line: serve, passwd, wake, wifi-daemon, cert-fetch.
#
#   Copyright (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
#   Produced by Thorsten Schnebeck - the idea, the decisions, the testing.
#   Written by Anthropic Claude Opus 5.5 - AI generated content.
#
#   Free software under the GNU General Public License, version 3 or later.
#   There is no warranty, to the extent permitted by law. The full text is in
#   LICENSES/GPL-3.0-or-later.txt.
#
# SPDX-FileCopyrightText: (C) 2026 Thorsten Schnebeck <thorsten.schnebeck@gmx.net>
# SPDX-FileContributor: Anthropic Claude Opus 5.5 (AI generated content)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Command line: `python3 -m wolserver [serve|passwd|wake|wifi-daemon|cert-fetch]`."""

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
    log.info("WOL-Server %s listening on %s, data in %s", __version__,
             ", ".join("[%s]:%d" % s.server_address[:2] for s in servers), cfg["data_dir"])
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    app.start()
    for server in servers:
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 5}, daemon=True).start()
    stop.wait()
    log.info("Shutting down ...")
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
        print("Certificate not fetched: %s" % e, file=sys.stderr)
        return 1
    print("Certificate renewed." if changed else "Certificate unchanged.")
    return 0


def cmd_passwd(cfg, from_stdin):
    from .auth import Auth

    os.makedirs(cfg["data_dir"], exist_ok=True)
    auth = Auth(cfg["data_dir"], cfg["session_days"])
    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Repeat: "):
            print("Passwords do not match.", file=sys.stderr)
            return 1
    try:
        auth.set_password(password)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print("Password set. Existing sessions have been logged out.")
    return 0


def cmd_wake(mac, broadcast, port):
    from .wol import normalize_mac, send_magic_packet

    send_magic_packet(mac, broadcast, port)
    print("Magic packet sent to %s via %s:%d." % (normalize_mac(mac), broadcast, port))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wol-server", description="Wake-on-LAN manager")
    parser.add_argument("-c", "--config", help="path to config.json (default: /etc/wol-server/config.json)")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="start web server and scheduler (default)")
    pw = sub.add_parser("passwd", help="set the password for the web GUI")
    pw.add_argument("--stdin", action="store_true", help="read the password from stdin")
    wake = sub.add_parser("wake", help="send a magic packet directly")
    wake.add_argument("mac")
    wake.add_argument("--broadcast", default="255.255.255.255")
    wake.add_argument("--port", type=int, default=9)
    sub.add_parser("wifi-daemon", help="Wi-Fi management with setup hotspot (as root, service wol-wifi)")
    sub.add_parser("cert-fetch", help="fetch the HTTPS certificate from the web server (as root, service wol-cert)")
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
