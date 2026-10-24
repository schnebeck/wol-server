"""Grüne Aktivitäts-LED (ACT) des Pi als Statusanzeige.

Genutzt werden nur Blinkmuster des Kernels (timer, heartbeat). Sie kosten keine CPU und
sind unabhängig davon, ob die LED invertiert angeschlossen ist (Pi Zero): Dort tauschen
höchstens Hell- und Dunkelphase. Deshalb sind die Timer-Muster symmetrisch.
"""
import logging
import os

log = logging.getLogger("wolserver.led")

LED_DIRS = ("/sys/class/leds/ACT", "/sys/class/leds/led0")
FIRSTBOOT_MARKER = "/run/wol-firstboot"  # legt firstboot.sh für die Dauer der Einrichtung an

PATTERNS = {
    "setup": ("timer", 100, 100),      # schnell: Einrichtung oder Verbindungsaufbau läuft
    "hotspot": ("heartbeat",),         # Herzschlag: Einrichtungs-Hotspot aktiv
    "offline": ("timer", 1000, 1000),  # langsam: kein Netz
    "normal": None,                    # Standardanzeige des Systems (SD-Zugriffe oder aus)
}
OWN_TRIGGERS = {"timer", "heartbeat"}
FALLBACK_TRIGGERS = ("actpwr", "mmc0", "default-on")


def pattern_for(mode, pending=None, firstboot=False):
    if firstboot or pending:
        return "setup"
    return {"hotspot": "hotspot", "client": "normal"}.get(mode, "offline")


class StatusLed:
    def __init__(self, dirs=LED_DIRS, marker=FIRSTBOOT_MARKER):
        self.marker = marker
        self.path = next((d for d in dirs if os.path.exists(os.path.join(d, "trigger"))), None)
        self.current = None
        self.normal = None
        if self.path is None:
            log.info("Keine Status-LED gefunden")
            return
        try:
            available, active = self._triggers()
        except OSError as e:
            log.warning("Status-LED nicht lesbar: %s", e)
            self.path = None
            return
        # Den Zustand vor uns als "normal" merken – außer er stammt von uns (z. B. nach Absturz
        # des Dienstes oder von firstboot.sh), dann die übliche Standardanzeige.
        if active is None or active in OWN_TRIGGERS:
            active = next((t for t in FALLBACK_TRIGGERS if t in available), "none")
        self.normal = active

    def _triggers(self):
        with open(os.path.join(self.path, "trigger"), encoding="ascii") as f:
            words = f.read().split()
        active = next((w[1:-1] for w in words if w.startswith("[") and w.endswith("]")), None)
        return [w.strip("[]") for w in words], active

    def _write(self, name, value):
        with open(os.path.join(self.path, name), "w", encoding="ascii") as f:
            f.write(str(value))

    def set(self, pattern):
        if self.path is None or pattern == self.current:
            return
        spec = PATTERNS[pattern] or (self.normal,)
        try:
            self._write("trigger", spec[0])
            if spec[0] == "timer":
                self._write("delay_on", spec[1])
                self._write("delay_off", spec[2])
        except OSError as e:
            log.warning("Status-LED nicht ansteuerbar (%s) – Anzeige abgeschaltet", e)
            self.path = None
            return
        self.current = pattern

    def update(self, mode, pending=None):
        self.set(pattern_for(mode, pending, os.path.exists(self.marker)))
