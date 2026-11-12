"""
Daily and monthly budgets per API, in that API's billing unit, kept in SQLite so a
restart or redeploy does not reset them. Rows hold only counts: no IPs, places or tokens.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from server.errors import ApiError, Busy
from server.guard.clock import Clock

log = logging.getLogger("time_map.budget")

UNITS = {"matrix": "elements"}  # everything else is billed per request


class BudgetExhausted(ApiError):
    status = 503


def _next_midnight(now: datetime) -> datetime:
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _next_month(now: datetime) -> datetime:
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first + timedelta(days=32)).replace(day=1)


def seconds_until(later: datetime, now: datetime) -> int:
    return int((later - now).total_seconds()) + 1


DAILY_MESSAGE = "Today's allowance for this feature is used up. It resets at midnight UTC."
MONTHLY_MESSAGE = "This month's allowance for this feature is used up. It resets on the 1st (UTC)."


def daily_exhausted(now: datetime, message: str = DAILY_MESSAGE) -> BudgetExhausted:
    return BudgetExhausted(message, code="daily_budget_exhausted",
                           headers={"Retry-After": str(seconds_until(_next_midnight(now), now))})


def monthly_exhausted(now: datetime, message: str = MONTHLY_MESSAGE) -> BudgetExhausted:
    return BudgetExhausted(message, code="monthly_budget_exhausted",
                           headers={"Retry-After": str(seconds_until(_next_month(now), now))})


@dataclass(frozen=True)
class Reservation:
    """What was charged, and to which periods, so a refund undoes exactly that."""
    api: str
    units: int
    day: str
    month: str


class UsageStore:
    def __init__(self, path: str, daily: dict[str, int], monthly: dict[str, int], clock: Clock):
        self.daily = daily
        self.monthly = monthly
        self.clock = clock
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        # Refusals are counted in memory (for the usage log), so a flood of refused
        # requests costs no writes.
        self._refused: Counter[tuple[str, str]] = Counter()
        try:
            db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=1)
            db.execute("PRAGMA journal_mode=WAL")
            # WAL + NORMAL: commits do not fsync; a power cut can lose the last few
            # increments, never corrupt the file. These calls run on the event loop.
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS usage ("
                " period TEXT NOT NULL, api TEXT NOT NULL,"
                " requests INTEGER NOT NULL DEFAULT 0, units INTEGER NOT NULL DEFAULT 0,"
                " refused INTEGER NOT NULL DEFAULT 0,"
                " PRIMARY KEY (period, api))"
            )
            self._db = db
        except sqlite3.Error as exc:
            # Fail closed: without the store we cannot tell how much has been spent.
            log.error("usage store unavailable (%s); billable requests will be refused",
                      type(exc).__name__)

    @property
    def available(self) -> bool:
        return self._db is not None

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    # ── periods ─────────────────────────────────────────────────────
    def _periods(self) -> tuple[str, str, datetime]:
        now = self.clock.utcnow()
        return f"d:{now:%Y-%m-%d}", f"m:{now:%Y-%m}", now

    def _units(self, period: str, api: str) -> int:
        row = self._db.execute(
            "SELECT units FROM usage WHERE period=? AND api=?", (period, api)
        ).fetchone()
        return int(row[0]) if row else 0

    def _bump(self, period: str, api: str, requests: int, units: int) -> None:
        self._db.execute(
            "INSERT INTO usage (period, api, requests, units) VALUES (?,?,?,?)"
            " ON CONFLICT(period, api) DO UPDATE SET requests=requests+excluded.requests,"
            " units=units+excluded.units",
            (period, api, requests, units),
        )

    def _over(self, api: str, units: int, day: str, month: str, now: datetime) -> BudgetExhausted | None:
        if self._units(month, api) + units > self.monthly.get(api, 0):
            return monthly_exhausted(now)
        if self._units(day, api) + units > self.daily.get(api, 0):
            return daily_exhausted(now)
        return None

    # ── public ──────────────────────────────────────────────────────
    def check(self, api: str, units: int) -> None:
        """Refuse now if `units` would not fit; charges nothing (a later reserve() decides)."""
        if self._db is None:
            raise Busy()
        day, month, now = self._periods()
        with self._lock:
            try:
                err = self._over(api, units, day, month, now)
            except sqlite3.Error as exc:
                log.error("usage store read failed (%s)", type(exc).__name__)
                raise Busy() from None
        if err is not None:
            self._refused[(day, api)] += 1
            raise err

    def reserve(self, api: str, units: int) -> Reservation:
        """Count `units` against today's and this month's budget, or refuse."""
        if self._db is None:
            raise Busy()
        day, month, now = self._periods()
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    err = self._over(api, units, day, month, now)
                    if err is None:
                        self._bump(day, api, 1, units)
                        self._bump(month, api, 1, units)
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
            except sqlite3.Error as exc:
                log.error("usage store write failed (%s)", type(exc).__name__)
                raise Busy() from None
        if err is not None:
            self._refused[(day, api)] += 1
            raise err
        return Reservation(api, units, day, month)

    def refund(self, res: Reservation) -> None:
        """Give back a reservation for a request that was never sent, in the periods it was charged to."""
        if self._db is None:
            return
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                self._bump(res.day, res.api, -1, -res.units)
                self._bump(res.month, res.api, -1, -res.units)
                self._db.execute("COMMIT")
            except sqlite3.Error as exc:
                log.error("usage store refund failed (%s)", type(exc).__name__)
                try:
                    self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass

    def remaining_today(self, api: str) -> int:
        if self._db is None:
            return 0
        day, month, _ = self._periods()
        with self._lock:
            return max(0, min(
                self.daily.get(api, 0) - self._units(day, api),
                self.monthly.get(api, 0) - self._units(month, api),
            ))

    def snapshot(self, period: str | None = None) -> dict[str, dict[str, int]]:
        if self._db is None:
            return {}
        day = period or self._periods()[0]
        with self._lock:
            rows = self._db.execute(
                "SELECT api, requests, units FROM usage WHERE period=? ORDER BY api", (day,)
            ).fetchall()
        out = {api: {"requests": r, "units": u, "refused": 0} for api, r, u in rows}
        for (d, api), n in self._refused.items():
            if d == day:
                out.setdefault(api, {"requests": 0, "units": 0, "refused": 0})["refused"] = n
        # Forget refusal counts from earlier days.
        for key in [k for k in self._refused if k[0] < self._periods()[0]]:
            del self._refused[key]
        return out
