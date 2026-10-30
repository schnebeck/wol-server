# WOL-Server - wolserver/plugs/http.py
# Small HTTP client for plug drivers: JSON, timeouts and digest authentication (MD5, SHA-256).
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

"""HTTP for plug drivers.

Python's urllib only knows digest authentication with MD5 and SHA-1; Shelly devices require
SHA-256 (RFC 7616). This client does both and keeps the last challenge per host, so an authenticated
request usually needs a single round trip. In place of the password it also takes the key derived from it
(digest_key), so the WOL-Server does not have to keep the password itself.
"""

import hashlib
import http.client
import json
import os
import re
import threading
from urllib.parse import urlsplit

from .base import PlugAuthError, PlugError, PlugUnreachable

_PARAM = re.compile(r'(\w+)=(?:"([^"]*)"|([^,\s]*))')
_HASHES = {"MD5": hashlib.md5, "SHA-256": hashlib.sha256}


def parse_challenge(header):
    """'Digest realm="x", nonce="y", ...' -> {"realm": "x", "nonce": "y", ...} or None."""
    if not header or not header.lower().startswith("digest "):
        return None
    return {k.lower(): quoted or plain for k, quoted, plain in _PARAM.findall(header[7:])}


def _hash(challenge):
    algorithm = (challenge.get("algorithm") or "MD5").upper()
    if algorithm not in _HASHES:
        raise PlugAuthError("Unsupported authentication method: %s" % algorithm)
    return algorithm, lambda text: _HASHES[algorithm](text.encode("utf-8")).hexdigest()


def digest_key(challenge, user, password):
    """The key that stands in for the password: HA1 of RFC 7616, the value the plug keeps itself. It is bound
    to the realm, so it works for this one plug only and does not give the password away."""
    algorithm, h = _hash(challenge)
    realm = challenge.get("realm", "")
    return {"realm": realm, "algorithm": algorithm, "ha1": h("%s:%s:%s" % (user, realm, password))}


def digest_response(challenge, method, uri, user, password, nc, cnonce):
    """Value of the Authorization header for a digest challenge (RFC 7616). password is the password or
    the key from digest_key()."""
    algorithm, h = _hash(challenge)
    realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
    if isinstance(password, dict):
        if (password.get("realm"), password.get("algorithm")) != (realm, algorithm):
            raise PlugAuthError("The stored key does not fit the plug (reset or replaced?) - enter the password again")
        ha1 = password["ha1"]
    else:
        ha1 = h("%s:%s:%s" % (user, realm, password))
    ha2 = h("%s:%s" % (method, uri))
    qop = "auth" if "auth" in (challenge.get("qop") or "").split(",") else None
    if qop:
        response = h("%s:%s:%08x:%s:%s:%s" % (ha1, nonce, nc, cnonce, qop, ha2))
    else:
        response = h("%s:%s:%s" % (ha1, nonce, ha2))
    parts = ['username="%s"' % user, 'realm="%s"' % realm, 'nonce="%s"' % nonce, 'uri="%s"' % uri,
             "algorithm=%s" % algorithm, 'response="%s"' % response]
    if qop:
        parts += ["qop=%s" % qop, "nc=%08x" % nc, 'cnonce="%s"' % cnonce]
    if challenge.get("opaque"):
        parts.append('opaque="%s"' % challenge["opaque"])
    return "Digest " + ", ".join(parts)


class HttpClient:
    def __init__(self, timeout=3.0):
        self.timeout = timeout
        self._challenges = {}  # host -> [challenge, nc]
        self._lock = threading.Lock()

    def digest_key(self, host, user, password):
        """digest_key() for the last challenge of host, or None if host has not asked for a password yet."""
        with self._lock:
            entry = self._challenges.get(host)
        return digest_key(entry[0], user, password) if entry else None

    def get_json(self, url, user=None, password=None):
        return self._json("GET", url, None, user, password)

    def post_json(self, url, payload, user=None, password=None):
        return self._json("POST", url, json.dumps(payload).encode("utf-8"), user, password)

    def _json(self, method, url, data, user, password):
        parts = urlsplit(url)
        path = parts.path + ("?" + parts.query if parts.query else "")
        host = parts.netloc
        status, headers, body = self._request(host, method, path, data, user, password)
        if status == 401 and password:
            challenge = parse_challenge(headers.get("www-authenticate"))
            if challenge is None:
                raise PlugAuthError("The plug asks for a password in an unsupported way")
            with self._lock:
                self._challenges[host] = [challenge, 0]
            status, headers, body = self._request(host, method, path, data, user, password)
        if status == 401:
            raise PlugAuthError("Password required" if not password else "Wrong password for the plug")
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, ValueError):
            raise PlugError("Unexpected answer from the plug (no JSON)") from None
        if status != 200:
            message = data.get("message") if isinstance(data, dict) else None
            raise PlugError("Plug answered with error %d%s" % (status, ": %s" % message if message else ""))
        return data

    def _request(self, host, method, path, data, user, password):
        headers = {"Accept": "application/json", "Connection": "close"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if password:
            with self._lock:
                entry = self._challenges.get(host)
                if entry:
                    entry[1] += 1
                    headers["Authorization"] = digest_response(entry[0], method, path, user, password, entry[1],
                                                               os.urandom(8).hex())
        conn = http.client.HTTPConnection(host, timeout=self.timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            body = resp.read(1024 * 1024)
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
        except (OSError, http.client.HTTPException) as e:
            raise PlugUnreachable("Plug %s not reachable (%s)" % (host, getattr(e, "strerror", None) or e)) from None
        finally:
            conn.close()
