# WOL-Server - tests/test_led.py
# Tests: status LED patterns.
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

import os
import shutil
import tempfile
import unittest
from unittest import mock

from wolserver import led, wifi
from wolserver.config import DEFAULTS

from .test_wifi import CLIENT_IP, FakeNM, make_manager


class LedTest(unittest.TestCase):
    """Against a mocked /sys/class/leds/ACT."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = os.path.join(self.tmp, "ACT")
        os.mkdir(self.dir)
        self.marker = os.path.join(self.tmp, "wol-firstboot")
        self.write("trigger", "none [mmc0] timer heartbeat actpwr default-on")

    def write(self, name, value):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(value)

    def read(self, name):
        with open(os.path.join(self.dir, name)) as f:
            return f.read()

    def make(self):
        return led.StatusLed(dirs=(os.path.join(self.tmp, "fehlt"), self.dir), marker=self.marker)

    def test_patterns(self):
        status = self.make()
        self.assertEqual(status.normal, "mmc0")
        status.update("offline")
        self.assertEqual((self.read("trigger"), self.read("delay_on"), self.read("delay_off")),
                         ("timer", "1000", "1000"))
        status.update("hotspot")
        self.assertEqual(self.read("trigger"), "heartbeat")
        status.update("client", pending="Heim")
        self.assertEqual((self.read("trigger"), self.read("delay_on")), ("timer", "100"))
        status.update("client")
        self.assertEqual(self.read("trigger"), "mmc0")

    def test_firstboot_marker_wins(self):
        status = self.make()
        open(self.marker, "w").close()
        status.update("hotspot")
        self.assertEqual((self.read("trigger"), self.read("delay_on")), ("timer", "100"))
        os.remove(self.marker)
        status.update("hotspot")
        self.assertEqual(self.read("trigger"), "heartbeat")

    def test_writes_only_on_change(self):
        status = self.make()
        status.update("hotspot")
        self.write("trigger", "geändert")
        status.update("hotspot")
        self.assertEqual(self.read("trigger"), "geändert")

    def test_own_trigger_is_not_normal(self):
        # e.g. after a crash of the service or when firstboot.sh already makes the LED blink
        self.write("trigger", "none mmc0 [timer] heartbeat actpwr")
        self.assertEqual(self.make().normal, "actpwr")
        self.write("trigger", "[none] mmc0 timer")  # lowpower.sh --led-off
        self.assertEqual(self.make().normal, "none")

    def test_missing_or_readonly_led(self):
        status = led.StatusLed(dirs=(os.path.join(self.tmp, "fehlt"),), marker=self.marker)
        status.update("hotspot")  # no error
        status = self.make()
        with mock.patch.object(status, "_write", side_effect=PermissionError("read-only")):
            status.update("hotspot")
        self.assertIsNone(status.path)
        status.update("offline")  # disabled, no further attempt

    def test_manager_drives_led(self):
        fake = FakeNM(["Heim"])
        fake.address, fake.connected = CLIENT_IP, "Heim"
        status = mock.Mock()
        mgr = wifi.WifiManager(dict(DEFAULTS, hotspot_ssid="WOL-Test"), self.tmp, runner=fake,
                               probe=lambda: fake.address, led=status)
        mgr.tick()
        status.update.assert_called_with("client", None)
        fake.address = None
        mgr.tick()
        status.update.assert_called_with("offline", None)

    def test_manager_without_led(self):
        mgr, _ = make_manager(FakeNM(), self.tmp)
        mgr.tick()  # led=None: no exception


if __name__ == "__main__":
    unittest.main()
