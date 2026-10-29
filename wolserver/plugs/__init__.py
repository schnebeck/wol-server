# WOL-Server - wolserver/plugs/__init__.py
# Wireless plugs: driver layer and manager.
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

"""Wireless plugs. Importing the package registers the built-in drivers."""

from . import shelly  # noqa: F401 - registers the driver
from .base import DRIVERS, PlugError, describe_drivers, get_driver, probe_all  # noqa: F401
from .manager import ConfirmRequired, PlugManager  # noqa: F401
