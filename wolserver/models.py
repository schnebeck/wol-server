# WOL-Server - wolserver/models.py
# Validation of devices and schedules from the API.
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

"""Validation and normalization of devices and schedules from the API."""

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
        raise ValidationError("%s: text expected" % (label or key))
    value = value.strip()
    if required and not value:
        raise ValidationError("%s must not be empty" % (label or key))
    if len(value) > maxlen:
        raise ValidationError("%s is too long (max. %d characters)" % (label or key, maxlen))
    if any(ord(c) < 32 for c in value):
        raise ValidationError("%s contains invalid characters" % (label or key))
    return value


def _int(data, key, default, lo, hi, label):
    value = data.get(key, default)
    if value in (None, ""):
        value = default
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValidationError("%s: number expected" % label) from None
    if not lo <= value <= hi:
        raise ValidationError("%s must be between %d and %d" % (label, lo, hi))
    return value


def valid_host(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOSTNAME.match(host))


def validate_device(data):
    if not isinstance(data, dict):
        raise ValidationError("JSON object expected")
    dev = {"name": _text(data, "name", 64, required=True, label="Name")}
    try:
        dev["mac"] = normalize_mac(_text(data, "mac", 32, required=True, label="MAC address"))
    except ValueError as e:
        raise ValidationError(str(e)) from None

    dev["host"] = _text(data, "host", 253, label="Host")
    if dev["host"] and not valid_host(dev["host"]):
        raise ValidationError("Host must be an IP address or a host name")

    dev["broadcast"] = _text(data, "broadcast", 15, default="255.255.255.255") or "255.255.255.255"
    try:
        ipaddress.IPv4Address(dev["broadcast"])
    except ValueError:
        raise ValidationError("Broadcast address must be an IPv4 address") from None
    dev["wol_port"] = _int(data, "wol_port", 9, 1, 65535, "WOL port")

    method = data.get("sleep_method") or "none"
    if method not in SLEEP_METHODS:
        raise ValidationError("Unknown sleep method")
    dev["sleep_method"] = method

    dev["ssh_user"] = _text(data, "ssh_user", 64, label="SSH user")
    dev["ssh_port"] = _int(data, "ssh_port", 22, 1, 65535, "SSH port")
    dev["ssh_command"] = _text(data, "ssh_command", 500, label="SSH command")
    dev["http_method"] = (data.get("http_method") or "POST").upper()
    if dev["http_method"] not in HTTP_METHODS:
        raise ValidationError("HTTP method must be GET, POST or PUT")
    dev["http_url"] = _text(data, "http_url", 500, label="URL")
    body = data.get("http_body") or ""
    if not isinstance(body, str) or len(body) > 2000:
        raise ValidationError("HTTP body is invalid or too long")
    dev["http_body"] = body

    if method == "ssh":
        if not dev["host"]:
            raise ValidationError("SSH requires a host")
        if not _SSH_USER.match(dev["ssh_user"]):
            raise ValidationError("Invalid SSH user name")
        if not dev["ssh_command"]:
            raise ValidationError("SSH command must not be empty")
    elif method == "http":
        parts = urlsplit(dev["http_url"])
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValidationError("URL must start with http:// or https://")
    return dev


def validate_schedule(data, device_lookup):
    if not isinstance(data, dict):
        raise ValidationError("JSON object expected")
    device = device_lookup(data.get("device_id") or "")
    if device is None:
        raise ValidationError("Unknown device")
    action = data.get("action")
    if action not in ACTIONS:
        raise ValidationError("Action must be 'wake' or 'sleep'")
    if action == "sleep" and device.get("sleep_method", "none") == "none":
        raise ValidationError("No sleep method is configured for %s" % device["name"])
    time_str = _text(data, "time", 5, required=True, label="Time")
    if not _TIME.match(time_str):
        raise ValidationError("Enter the time in HH:MM format")
    days = data.get("days")
    if not isinstance(days, list) or not days:
        raise ValidationError("Select at least one weekday")
    try:
        days = sorted({int(d) for d in days})
    except (TypeError, ValueError):
        raise ValidationError("Invalid weekdays") from None
    if days[0] < 0 or days[-1] > 6:
        raise ValidationError("Weekdays must be between 0 (Mon) and 6 (Sun)")
    return {
        "device_id": device["id"],
        "action": action,
        "time": time_str,
        "days": days,
        "enabled": bool(data.get("enabled", True)),
        "label": _text(data, "label", 80, label="Label"),
    }
