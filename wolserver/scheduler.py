"""Zeitplaner: wacht einmal pro Minute auf und führt fällige Aktionen aus.

Der Pi Zero W hat keine Echtzeituhr. Springt die Uhr nach dem Booten per NTP nach vorn,
werden höchstens MAX_CATCHUP_MINUTES verpasste Minuten nachgeholt; Rücksprünge lösen
nichts aus. Jede Ausführung wird pro Schedule/Datum/Uhrzeit gemerkt, damit die doppelte
Stunde bei der Zeitumstellung nicht zweimal auslöst.
"""

import datetime
import logging
import threading
import time

log = logging.getLogger("wolserver")

MAX_CATCHUP_MINUTES = 10


def next_run(schedule, now=None):
    """Nächster Ausführungszeitpunkt als naive lokale datetime (oder None)."""
    if not schedule.get("enabled") or not schedule.get("days"):
        return None
    now = now or datetime.datetime.now()
    hour, minute = (int(x) for x in schedule["time"].split(":"))
    for offset in range(8):
        day = (now + datetime.timedelta(days=offset)).date()
        if day.weekday() in schedule["days"]:
            candidate = datetime.datetime.combine(day, datetime.time(hour, minute))
            if candidate > now:
                return candidate
    return None


class Scheduler:
    def __init__(self, store, run_action, clock=time.time):
        self.store = store
        self.run_action = run_action  # (device, action, source) -> (ok, message)
        self.clock = clock
        self._stop = threading.Event()
        self._thread = None
        self._fired = {}

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        last = self._minute(self.clock())
        while not self._stop.wait(60.2 - self.clock() % 60):
            now = self._minute(self.clock())
            try:
                self.tick(last, now)
            except Exception:  # noqa: BLE001 – der Zeitplaner darf nie sterben
                log.exception("Fehler im Zeitplaner")
            last = now

    @staticmethod
    def _minute(ts):
        return int(ts // 60 * 60)

    def tick(self, last, now):
        """Führt alle Schedules aus, die in den Minuten (last, now] fällig waren."""
        if now <= last:
            return []
        start = max(last + 60, now - (MAX_CATCHUP_MINUTES - 1) * 60)
        fired = []
        for ts in range(start, now + 60, 60):
            fired.extend(self._run_minute(ts))
        self._prune(now)
        return fired

    def _run_minute(self, ts):
        lt = time.localtime(ts)
        hhmm = "%02d:%02d" % (lt.tm_hour, lt.tm_min)
        date = time.strftime("%Y-%m-%d", lt)
        fired = []
        for sched in self.store.list("schedules"):
            if not sched.get("enabled") or sched["time"] != hhmm or lt.tm_wday not in sched["days"]:
                continue
            key = (sched["id"], date, hhmm)
            if key in self._fired:
                continue
            self._fired[key] = ts
            device = self.store.get("devices", sched["device_id"])
            if device is None:
                continue
            log.info("Zeitplan: %s → %s (%s)", device["name"], sched["action"], hhmm)
            threading.Thread(
                target=self.run_action, args=(device, sched["action"], "Zeitplan"),
                name="action-%s" % sched["id"], daemon=True,
            ).start()
            fired.append(sched["id"])
        return fired

    def _prune(self, now):
        cutoff = now - 2 * 86400
        self._fired = {k: v for k, v in self._fired.items() if v >= cutoff}
