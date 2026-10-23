"""Passwort-Login mit HMAC-signierten Session-Cookies."""

import base64
import hashlib
import hmac
import json
import os
import threading
import time

ITERATIONS = 200_000  # auf dem Pi Zero W ca. 1 s pro Login – bewusst langsam
MIN_PASSWORD_LENGTH = 8


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password, iterations=ITERATIONS):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, _b64(salt), _b64(digest))


def verify_password(password, stored):
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(iterations))
        return hmac.compare_digest(actual, _unb64(digest))
    except (ValueError, TypeError):
        return False


class Auth:
    """Speichert Passwort-Hash und Cookie-Secret in <data_dir>/auth.json (Modus 0600)."""

    def __init__(self, data_dir, session_days=30):
        self.path = os.path.join(data_dir, "auth.json")
        self.session_seconds = int(session_days * 86400)
        self._lock = threading.Lock()
        self._mtime = None
        self._data = {}
        self._reload()
        if not self._data.get("secret"):
            self._write(dict(self._data, secret=_b64(os.urandom(32))))

    def _reload(self):
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except FileNotFoundError:
            return
        if mtime == self._mtime:
            return
        with open(self.path, encoding="utf-8") as f:
            self._data = json.load(f)
        self._mtime = mtime

    def _write(self, data):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._data = data
        self._mtime = os.stat(self.path).st_mtime_ns

    @property
    def required(self):
        with self._lock:
            self._reload()  # Passwort kann per CLI geändert worden sein
            return bool(self._data.get("password_hash"))

    def check_password(self, password):
        with self._lock:
            stored = self._data.get("password_hash", "")
        return bool(stored) and verify_password(password, stored)

    def set_password(self, password):
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError("Passwort muss mindestens %d Zeichen lang sein" % MIN_PASSWORD_LENGTH)
        with self._lock:
            # neues Secret => alle bestehenden Sessions werden ungültig
            self._write({"password_hash": hash_password(password), "secret": _b64(os.urandom(32))})

    def _sign(self, message):
        secret = self._data.get("secret", "").encode()
        return _b64(hmac.new(secret, message.encode(), hashlib.sha256).digest())

    def make_token(self):
        with self._lock:
            expires = str(int(time.time()) + self.session_seconds)
            return "%s.%s" % (expires, self._sign(expires))

    def verify_token(self, token):
        if not token or "." not in token:
            return False
        expires, sig = token.split(".", 1)
        with self._lock:
            self._reload()
            expected = self._sign(expires)
        if not hmac.compare_digest(sig, expected):
            return False
        return expires.isdigit() and int(expires) > time.time()


class LoginLimiter:
    """Einfache Brute-Force-Bremse: nach N Fehlversuchen pro IP eine Sperrzeit."""

    def __init__(self, max_failures=5, window=300, lockout=300):
        self.max_failures, self.window, self.lockout = max_failures, window, lockout
        self._failures = {}
        self._lock = threading.Lock()

    def blocked(self, ip):
        now = time.time()
        with self._lock:
            times = [t for t in self._failures.get(ip, []) if now - t < max(self.window, self.lockout)]
            self._failures[ip] = times
            recent = [t for t in times if now - t < self.window]
            return len(recent) >= self.max_failures and now - times[-1] < self.lockout

    def fail(self, ip):
        with self._lock:
            self._failures.setdefault(ip, []).append(time.time())

    def reset(self, ip):
        with self._lock:
            self._failures.pop(ip, None)
