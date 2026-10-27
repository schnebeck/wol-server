# WOL-Server - wolserver/plugs/energy.py
# Energy log: hourly values in SQLite, the power curve of the last 24 hours in RAM.
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

"""Energy log of the plugs.

The plug counts the energy itself; the log only takes the difference between two readings. A missed
reading loses nothing: the next difference covers the gap and is spread over the hours it spans. If the
counter goes down (reset, firmware update), the new value counts as the consumption since the reset.
Plugs without a counter are integrated from the power (at most 5 minutes per reading).

Easy on the SD card: the values of the running hour stay in RAM and are written once per hour (and on
shutdown) in a single transaction. The per-minute curve of the last 24 hours lives in RAM only.
"""

import collections
import datetime
import logging
import sqlite3
import threading
import time

log = logging.getLogger("wolserver")

HOUR = 3600
MAX_SPREAD = 31 * 24 * HOUR  # longer gaps go into the hour of the reading
MAX_INTEGRATE = 300  # s, plugs without a counter
CURVE_STEP = 300  # s, resolution of the day curve
SAMPLES = 24 * 60  # minutes in RAM per plug

GROUPS = {"day": "%H", "month": "%d", "year": "%m"}


def _hour(ts):
    return int(ts // HOUR * HOUR)


class EnergyLog:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript("""
            PRAGMA synchronous = NORMAL;
            CREATE TABLE IF NOT EXISTS hours (
                plug TEXT NOT NULL, hour INTEGER NOT NULL, wh REAL NOT NULL DEFAULT 0,
                max_w REAL, max_temp REAL, PRIMARY KEY (plug, hour)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS counters (plug TEXT PRIMARY KEY, ts REAL, energy_wh REAL);
        """)
        self._last = {plug: (ts, wh) for plug, ts, wh in self._db.execute("SELECT plug, ts, energy_wh FROM counters")}
        self._pending = {}  # (plug, hour) -> [wh, max_w, max_temp]
        self._flushed_hour = _hour(clock())
        self._samples = collections.defaultdict(lambda: collections.deque(maxlen=SAMPLES))  # [minute, sum_w, n]

    # -- recording --

    def record(self, plug_id, status):
        """Takes one reading (PlugStatus.to_dict()). Readings older than the last one only feed the curve."""
        ts = status.get("ts") or self.clock()
        power, counter, temp = status.get("power_w"), status.get("energy_wh"), status.get("temperature_c")
        with self._lock:
            if power is not None:
                self._sample(plug_id, ts, power)
            last = self._last.get(plug_id)
            if last and ts <= last[0]:
                return
            delta = 0.0
            if counter is not None:
                if last and last[1] is not None:
                    delta = counter - last[1] if counter >= last[1] else counter
                self._last[plug_id] = (ts, counter)
            elif power is not None:
                if last:
                    delta = power * min(ts - last[0], MAX_INTEGRATE) / HOUR
                self._last[plug_id] = (ts, None)
            else:
                return
            if delta > 0:
                self._spread(plug_id, last[0], ts, delta)
            entry = self._pending.setdefault((plug_id, _hour(ts)), [0.0, None, None])
            if power is not None:
                entry[1] = power if entry[1] is None else max(entry[1], power)
            if temp is not None:
                entry[2] = temp if entry[2] is None else max(entry[2], temp)
            if _hour(ts) > self._flushed_hour:
                self.flush()

    def _spread(self, plug_id, start, end, wh):
        """Distributes wh evenly over the time from start to end, hour by hour."""
        if end - start > MAX_SPREAD:
            start = end - 1
        span = end - start
        t = start
        while t < end:
            nxt = min(_hour(t) + HOUR, end)
            entry = self._pending.setdefault((plug_id, _hour(t)), [0.0, None, None])
            entry[0] += wh * (nxt - t) / span
            t = nxt

    def _sample(self, plug_id, ts, power):
        minute = int(ts // 60 * 60)
        samples = self._samples[plug_id]
        if samples and samples[-1][0] == minute:
            samples[-1][1] += power
            samples[-1][2] += 1
        elif not samples or minute > samples[-1][0]:
            samples.append([minute, power, 1])

    def flush(self):
        """Writes the values in RAM: one transaction, once per hour and on shutdown."""
        with self._lock:
            self._flushed_hour = _hour(self.clock())
            if not self._pending:
                return
            rows = [(plug, hour, wh, max_w, max_temp) for (plug, hour), (wh, max_w, max_temp) in self._pending.items()]
            try:
                with self._db:
                    self._db.executemany("""
                        INSERT INTO hours (plug, hour, wh, max_w, max_temp) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT (plug, hour) DO UPDATE SET wh = wh + excluded.wh,
                            max_w = MAX(COALESCE(max_w, excluded.max_w), COALESCE(excluded.max_w, max_w)),
                            max_temp = MAX(COALESCE(max_temp, excluded.max_temp), COALESCE(excluded.max_temp, max_temp))
                    """, rows)
                    self._db.executemany("INSERT OR REPLACE INTO counters (plug, ts, energy_wh) VALUES (?, ?, ?)",
                                         [(plug, ts, wh) for plug, (ts, wh) in self._last.items()])
            except sqlite3.Error:
                log.exception("Energy log: writing failed, keeping the values in RAM")
                return
            self._pending.clear()

    def forget(self, plug_id):
        """Removes all values of a plug (when it is deleted)."""
        with self._lock:
            self._pending = {k: v for k, v in self._pending.items() if k[0] != plug_id}
            self._last.pop(plug_id, None)
            self._samples.pop(plug_id, None)
            with self._db:
                self._db.execute("DELETE FROM hours WHERE plug = ?", (plug_id,))
                self._db.execute("DELETE FROM counters WHERE plug = ?", (plug_id,))

    def close(self):
        with self._lock:
            self.flush()
            self._db.close()

    # -- queries (database plus the values still in RAM) --

    def _rows(self, start, end, plug_ids=None):
        """(plug, hour, wh, max_w) for start <= hour < end."""
        with self._lock:
            rows = {(p, h): [wh, mw] for p, h, wh, mw in self._db.execute(
                "SELECT plug, hour, wh, max_w FROM hours WHERE hour >= ? AND hour < ?", (start, end))}
            for (p, h), (wh, mw, _) in self._pending.items():
                if start <= h < end:
                    row = rows.setdefault((p, h), [0.0, None])
                    row[0] += wh
                    if mw is not None:
                        row[1] = mw if row[1] is None else max(row[1], mw)
        return [(p, h, wh, mw) for (p, h), (wh, mw) in rows.items() if plug_ids is None or p in plug_ids]

    @staticmethod
    def period(kind, day):
        """Start and end (timestamps) of the day, month or year that contains day (a datetime.date)."""
        if kind == "day":
            first, nxt = day, day + datetime.timedelta(days=1)
        elif kind == "month":
            first = day.replace(day=1)
            nxt = (first + datetime.timedelta(days=32)).replace(day=1)
        elif kind == "year":
            first, nxt = day.replace(month=1, day=1), day.replace(year=day.year + 1, month=1, day=1)
        else:
            raise ValueError("Unknown period: %s" % kind)
        return (int(time.mktime(first.timetuple())), int(time.mktime(nxt.timetuple())))

    def series(self, kind, day, plug_ids=None):
        """Energy per hour (day), per day (month) or per month (year) and plug, in Wh.

        Returns {"start", "end", "buckets": [label, ...], "plugs": {plug: [wh or None, ...]}}."""
        start, end = self.period(kind, day)
        if kind == "day":
            labels = sorted({time.strftime("%H", time.localtime(h)) for h in range(start, end, HOUR)})
        elif kind == "month":
            labels = ["%02d" % d for d in range(1, (datetime.date.fromtimestamp(end - 1)).day + 1)]
        else:
            labels = ["%02d" % m for m in range(1, 13)]
        index = {label: i for i, label in enumerate(labels)}
        plugs = {}
        for plug, hour, wh, _ in self._rows(start, end, plug_ids):
            values = plugs.setdefault(plug, [None] * len(labels))
            i = index[time.strftime(GROUPS[kind], time.localtime(hour))]
            values[i] = (values[i] or 0.0) + wh
        return {"start": start, "end": end, "buckets": labels,
                "plugs": {p: [None if v is None else round(v, 2) for v in vals] for p, vals in plugs.items()}}

    def curve(self, day, plug_ids):
        """Power over the day in CURVE_STEP buckets per plug (W): the minute values from RAM where there are
        some, otherwise the average of the hour from the database. None where nothing is known."""
        start, end = self.period("day", day)
        now = self.clock()
        steps = (end - start) // CURVE_STEP
        hourly = {(p, h): wh for p, h, wh, _ in self._rows(start, end, plug_ids)}
        out = {}
        with self._lock:
            samples = {p: list(self._samples.get(p, ())) for p in plug_ids}
        for plug in plug_ids:
            sums = {}
            for minute, total, n in samples[plug]:
                if start <= minute < end:
                    bucket = sums.setdefault((minute - start) // CURVE_STEP, [0.0, 0])
                    bucket[0] += total / n
                    bucket[1] += 1
            values = []
            for i in range(steps):
                t = start + i * CURVE_STEP
                if i in sums:
                    values.append(round(sums[i][0] / sums[i][1], 1))
                elif _hour(t) + HOUR <= now and (plug, _hour(t)) in hourly:
                    values.append(round(hourly[(plug, _hour(t))], 1))  # Wh in one hour = average W
                else:
                    values.append(None)
            out[plug] = values
        return {"start": start, "step": CURVE_STEP, "plugs": out}

    def totals(self, plug_ids, now=None):
        """Wh today, this month and this year per plug."""
        today = datetime.date.fromtimestamp(now or self.clock())
        out = {p: {} for p in plug_ids}
        for kind in ("day", "month", "year"):
            start, end = self.period(kind, today)
            sums = collections.Counter()
            for plug, _, wh, _ in self._rows(start, end, set(plug_ids)):
                sums[plug] += wh
            for p in plug_ids:
                out[p][kind] = round(sums[p], 2)
        return out

    def standby(self, plug_id, standby_w, days=30):
        """Estimated standby energy per year (Wh): the energy of the hours in which the draw never rose above
        the standby threshold, over the last days, scaled to a year. None with less than a day of data."""
        end = _hour(self.clock())
        rows = self._rows(end - days * 86400, end, {plug_id})
        if len(rows) < 24:
            return None
        quiet = sum(wh for _, _, wh, mw in rows if mw is not None and mw <= standby_w)
        return round(quiet * 8760 / len(rows), 1)
