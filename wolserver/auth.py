"""Password login with HMAC-signed session cookies."""

import base64
import hashlib
import hmac
import json
import os
import threading
import time

ITERATIONS = 200_000  # about 1 s per login on the Pi Zero W - deliberately slow
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
    """Stores password hash and cookie secret in <data_dir>/auth.json (mode 0600)."""

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
            self._reload()  # the password may have been changed via the CLI
            return bool(self._data.get("password_hash"))

    def check_password(self, password):
        with self._lock:
            stored = self._data.get("password_hash", "")
        return bool(stored) and verify_password(password, stored)

    def set_password(self, password):
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError("Password must be at least %d characters long" % MIN_PASSWORD_LENGTH)
        with self._lock:
            # new secret => all existing sessions become invalid
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
    """Simple brute-force brake: a lockout period after N failed attempts per IP."""

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


class ApiTokens:
    """API tokens for scripts, stored as SHA-256 hashes in <data_dir>/api_tokens.json (mode 0600).

    A token carries 256 random bits, so a fast hash is enough (unlike passwords). The plain token
    is shown only once, when it is created.
    """

    PREFIX = "wol_"
    MAX_TOKENS = 20
    TOUCH_SECONDS = 60  # write "last used" at most once a minute (spares the SD card)

    def __init__(self, data_dir, clock=time.time):
        self.path = os.path.join(data_dir, "api_tokens.json")
        self.clock = clock
        self._lock = threading.Lock()
        try:
            with open(self.path, encoding="utf-8") as f:
                self._tokens = json.load(f).get("tokens", [])
        except FileNotFoundError:
            self._tokens = []

    def _save(self):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"tokens": self._tokens}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _public(entry):
        return {k: entry[k] for k in ("id", "name", "created", "last_used")}

    def list(self):
        with self._lock:
            return [self._public(t) for t in self._tokens]

    def create(self, name):
        name = " ".join(str(name or "").split())[:40]
        if not name:
            raise ValueError("Name is required")
        with self._lock:
            if len(self._tokens) >= self.MAX_TOKENS:
                raise ValueError("At most %d API tokens" % self.MAX_TOKENS)
            token = self.PREFIX + _b64(os.urandom(32))
            entry = {"id": _b64(os.urandom(6)), "name": name, "hash": self._hash(token),
                     "created": int(self.clock()), "last_used": None}
            self._tokens.append(entry)
            self._save()
            return dict(self._public(entry), token=token)

    def revoke(self, token_id):
        with self._lock:
            before = len(self._tokens)
            self._tokens = [t for t in self._tokens if t["id"] != token_id]
            if len(self._tokens) == before:
                return False
            self._save()
            return True

    def verify(self, token):
        """Matching entry (without hash) or None."""
        if not token or not token.startswith(self.PREFIX):
            return None
        digest = self._hash(token)
        with self._lock:
            entry = next((t for t in self._tokens if hmac.compare_digest(t["hash"], digest)), None)
            if entry is None:
                return None
            now = int(self.clock())
            if not entry["last_used"] or now - entry["last_used"] >= self.TOUCH_SECONDS:
                entry["last_used"] = now
                self._save()
            return self._public(entry)
