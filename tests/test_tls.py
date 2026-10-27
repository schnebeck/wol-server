# WOL-Server - tests/test_tls.py
# Tests: HTTPS certificate handling and the HTTPS server.
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

import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest

from wolserver import server as server_mod
from wolserver import tls
from wolserver.config import DEFAULTS
from wolserver.server import App, create_https_server, create_server

NAME = "wol.lan.example.org"


def make_cert(directory, name=NAME, days=90, tag="a"):
    """Self-signed certificate with openssl -> (key, certificate) as bytes."""
    key, cert = os.path.join(directory, tag + ".key"), os.path.join(directory, tag + ".crt")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                    "-nodes", "-keyout", key, "-out", cert, "-days", str(days), "-subj", "/CN=" + name,
                    "-addext", "subjectAltName=DNS:%s" % name], check=True, capture_output=True)
    with open(key, "rb") as f1, open(cert, "rb") as f2:
        return f1.read(), f2.read()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(shutil.which("openssl"), "openssl fehlt")
class BundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tls_dir = os.path.join(self.tmp, "tls")
        os.mkdir(self.tls_dir)
        self.key, self.cert = make_cert(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_install_and_checks(self):
        self.assertTrue(tls.install_bundle(self.key + self.cert, self.tls_dir, NAME))
        info = tls.cert_info(os.path.join(self.tls_dir, tls.CERT))
        self.assertEqual(info["names"], [NAME])
        self.assertGreater(info["not_after"], time.time() + 80 * 86400)
        self.assertEqual(oct(os.stat(os.path.join(self.tls_dir, tls.KEY)).st_mode & 0o777), "0o640")
        # unchanged: do nothing; order in the bundle does not matter
        self.assertFalse(tls.install_bundle(self.cert + self.key, self.tls_dir, NAME))
        self.assertEqual(sorted(os.listdir(self.tls_dir)), [tls.CERT, tls.KEY])

        other_key, other_cert = make_cert(self.tmp, name="anders.example.org", tag="b")
        cases = [
            (other_key + self.cert, "invalid"),                  # key does not match
            (other_key + other_cert, "not valid for"),           # wrong name
            (self.cert, "exactly one key"),
            (b"Hello", "exactly one key"),
        ]
        for data, message in cases:
            with self.assertRaises(tls.FetchError) as cm:
                tls.install_bundle(data, self.tls_dir, NAME)
            self.assertIn(message, str(cm.exception))
        with self.assertRaises(tls.FetchError):
            tls.install_bundle(self.key + self.cert, self.tls_dir, NAME, wall=lambda: time.time() + 89 * 86400)
        # failures leave the previous certificate untouched
        self.assertEqual(tls.cert_info(os.path.join(self.tls_dir, tls.CERT))["names"], [NAME])
        self.assertEqual(sorted(os.listdir(self.tls_dir)), [tls.CERT, tls.KEY])

    def test_fetch_runs_ssh(self):
        cfg = dict(DEFAULTS, tls_dir=self.tls_dir, cert_source="wolcert@10.1.1.3")
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, self.key + self.cert, b"")
        self.assertEqual(tls.fetch(cfg, runner), self.key + self.cert)
        self.assertEqual(calls[0][0], "ssh")
        self.assertEqual(calls[0][-1], "wolcert@10.1.1.3")
        self.assertIn("StrictHostKeyChecking=yes", calls[0])

        def failing(argv, **kw):
            return subprocess.CompletedProcess(argv, 255, b"", b"ssh: connect to host 10.1.1.3: No route to host\n")
        with self.assertRaises(tls.FetchError) as cm:
            tls.fetch(cfg, failing)
        self.assertIn("No route to host", str(cm.exception))
        with self.assertRaises(tls.FetchError):
            tls.fetch(dict(cfg, cert_source=""), runner)

    def test_state_notices_renewal(self):
        now = [0.0]
        state = tls.TlsState(self.tls_dir, clock=lambda: now[0])
        self.assertIsNone(state.current())
        self.assertEqual(state.status(), {"active": False})
        tls.install_bundle(self.key + self.cert, self.tls_dir, NAME)
        self.assertIsNone(state.current())  # only after CHECK_SECONDS
        now[0] += tls.TlsState.CHECK_SECONDS
        self.assertIsNotNone(state.current())
        self.assertEqual(state.host(), NAME)
        # broken files: the previous certificate stays
        first = state.current()
        with open(os.path.join(self.tls_dir, tls.CERT), "wb") as f:
            f.write(b"kaputt")
        now[0] += tls.TlsState.CHECK_SECONDS
        self.assertIs(state.current(), first)
        # expired: no target for the redirect
        expired = tls.TlsState(self.tls_dir, clock=lambda: now[0], wall=lambda: time.time() + 400 * 86400)
        tls.install_bundle(self.key + self.cert, self.tls_dir, NAME)
        self.assertIsNone(expired.host())


@unittest.skipUnless(shutil.which("openssl"), "openssl fehlt")
class HttpsServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        tls_dir = os.path.join(self.tmp, "tls")
        os.mkdir(tls_dir)
        key, cert = make_cert(self.tmp)
        tls.install_bundle(key + cert, tls_dir, NAME)
        cfg = dict(DEFAULTS, listen="127.0.0.1", port=0, https_port=free_port(),
                   data_dir=os.path.join(self.tmp, "data"), tls_dir=tls_dir)
        self.app = App(cfg)
        self.http = create_server(self.app)
        self.https = create_https_server(self.app)
        for server in (self.http, self.https):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in (self.http, self.https):
            server.shutdown()
            server.server_close()
        self.app.stop()
        shutil.rmtree(self.tmp)

    def _https(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection("127.0.0.1", self.app.config["https_port"], context=ctx, timeout=10)

    def test_https_and_redirect(self):
        conn = self._https()
        conn.request("GET", "/api/session")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("authenticated", json.loads(resp.read()))
        self.assertEqual(conn.sock.getpeercert(binary_form=True)[:1], b"0")  # DER certificate delivered
        conn.close()

        # HTTP from outside (not loopback) redirects; loopback (CLI, tests) does not
        conn = http.client.HTTPConnection("127.0.0.1", self.http.server_address[1], timeout=10)
        conn.request("GET", "/api/session")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        original = server_mod.Handler._client_ip
        server_mod.Handler._client_ip = lambda self: server_mod.Handler._plain_ip("192.168.1.12")
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.http.server_address[1], timeout=10)
            conn.request("GET", "/geraete?x=1")
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 308)
            self.assertEqual(resp.getheader("Location"),
                             "https://%s:%d/geraete?x=1" % (NAME, self.app.config["https_port"]))
            self.app.config["https_redirect"] = False
            conn.request("GET", "/api/session")
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
        finally:
            server_mod.Handler._client_ip = original

    def test_secure_cookie_over_https(self):
        self.app.auth.set_password("geheim123")
        conn = self._https()
        conn.request("POST", "/api/login", json.dumps({"password": "geheim123"}),
                     {"Content-Type": "application/json", "X-Requested-With": "wol-server"})
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 200)
        self.assertIn("Secure", resp.getheader("Set-Cookie"))
        conn.request("GET", "/api/info", headers={"Cookie": resp.getheader("Set-Cookie").split(";")[0]})
        info = json.loads(conn.getresponse().read())
        self.assertTrue(info["https"]["active"])
        self.assertEqual(info["https"]["names"], [NAME])
        conn.close()
