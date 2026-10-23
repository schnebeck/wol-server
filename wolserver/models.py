"""Validierung und Normalisierung von Geräten und Zeitplänen aus der API."""

import ipaddress
import re
from urllib.parse import urlsplit

from .wol import normalize_mac

SLEEP_METHODS = ("none", "ssh", "http", "sol")
HTTP_METHODS = ("GET", "POST", "PUT")
ACTIONS = ("wake", "sleep")

_HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.\-_]*[A-Za-z0-9])?$")
_SSH_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@\\\-]{0,63}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ValidationError(ValueError):
    pass


def _text(data, key, maxlen, default="", required=False, label=None):
    value = data.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValidationError("%s: Text erwartet" % (label or key))
    value = value.strip()
    if required and not value:
        raise ValidationError("%s darf nicht leer sein" % (label or key))
    if len(value) > maxlen:
        raise ValidationError("%s ist zu lang (max. %d Zeichen)" % (label or key, maxlen))
    if any(ord(c) < 32 for c in value):
        raise ValidationError("%s enthält ungültige Zeichen" % (label or key))
    return value


def _int(data, key, default, lo, hi, label):
    value = data.get(key, default)
    if value in (None, ""):
        value = default
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValidationError("%s: Zahl erwartet" % label) from None
    if not lo <= value <= hi:
        raise ValidationError("%s muss zwischen %d und %d liegen" % (label, lo, hi))
    return value


def valid_host(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOSTNAME.match(host))


def validate_device(data):
    if not isinstance(data, dict):
        raise ValidationError("JSON-Objekt erwartet")
    dev = {"name": _text(data, "name", 64, required=True, label="Name")}
    try:
        dev["mac"] = normalize_mac(_text(data, "mac", 32, required=True, label="MAC-Adresse"))
    except ValueError as e:
        raise ValidationError(str(e)) from None

    dev["host"] = _text(data, "host", 253, label="Host")
    if dev["host"] and not valid_host(dev["host"]):
        raise ValidationError("Host muss eine IP-Adresse oder ein Hostname sein")

    dev["broadcast"] = _text(data, "broadcast", 15, default="255.255.255.255") or "255.255.255.255"
    try:
        ipaddress.IPv4Address(dev["broadcast"])
    except ValueError:
        raise ValidationError("Broadcast-Adresse muss eine IPv4-Adresse sein") from None
    dev["wol_port"] = _int(data, "wol_port", 9, 1, 65535, "WOL-Port")

    method = data.get("sleep_method") or "none"
    if method not in SLEEP_METHODS:
        raise ValidationError("Unbekannte Schlaf-Methode")
    dev["sleep_method"] = method

    dev["ssh_user"] = _text(data, "ssh_user", 64, label="SSH-Benutzer")
    dev["ssh_port"] = _int(data, "ssh_port", 22, 1, 65535, "SSH-Port")
    dev["ssh_command"] = _text(data, "ssh_command", 500, label="SSH-Befehl")
    dev["http_method"] = (data.get("http_method") or "POST").upper()
    if dev["http_method"] not in HTTP_METHODS:
        raise ValidationError("HTTP-Methode muss GET, POST oder PUT sein")
    dev["http_url"] = _text(data, "http_url", 500, label="URL")
    body = data.get("http_body") or ""
    if not isinstance(body, str) or len(body) > 2000:
        raise ValidationError("HTTP-Body ist ungültig oder zu lang")
    dev["http_body"] = body

    if method == "ssh":
        if not dev["host"]:
            raise ValidationError("Für SSH wird ein Host benötigt")
        if not _SSH_USER.match(dev["ssh_user"]):
            raise ValidationError("Ungültiger SSH-Benutzername")
        if not dev["ssh_command"]:
            raise ValidationError("SSH-Befehl darf nicht leer sein")
    elif method == "http":
        parts = urlsplit(dev["http_url"])
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValidationError("URL muss mit http:// oder https:// beginnen")
    return dev


def validate_schedule(data, device_lookup):
    if not isinstance(data, dict):
        raise ValidationError("JSON-Objekt erwartet")
    device = device_lookup(data.get("device_id") or "")
    if device is None:
        raise ValidationError("Unbekanntes Gerät")
    action = data.get("action")
    if action not in ACTIONS:
        raise ValidationError("Aktion muss 'wake' oder 'sleep' sein")
    if action == "sleep" and device.get("sleep_method", "none") == "none":
        raise ValidationError("Für %s ist keine Schlaf-Methode eingerichtet" % device["name"])
    time_str = _text(data, "time", 5, required=True, label="Uhrzeit")
    if not _TIME.match(time_str):
        raise ValidationError("Uhrzeit im Format HH:MM angeben")
    days = data.get("days")
    if not isinstance(days, list) or not days:
        raise ValidationError("Mindestens einen Wochentag auswählen")
    try:
        days = sorted({int(d) for d in days})
    except (TypeError, ValueError):
        raise ValidationError("Ungültige Wochentage") from None
    if days[0] < 0 or days[-1] > 6:
        raise ValidationError("Wochentage müssen zwischen 0 (Mo) und 6 (So) liegen")
    return {
        "device_id": device["id"],
        "action": action,
        "time": time_str,
        "days": days,
        "enabled": bool(data.get("enabled", True)),
        "label": _text(data, "label", 80, label="Bezeichnung"),
    }
