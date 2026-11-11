"""
Daily and monthly budgets per API, in that API's billing unit, kept in SQLite so a
restart or redeploy does not reset them. Rows hold only counts: no IPs, places or tokens.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta

from server.errors import ApiError, Busy
from server.guard.clock import Clock

log = logging.getLogger("time_map.budget")

UNITS = {"matrix": "elements"}  # everything else is billed per request


class BudgetExhausted(ApiError):
    status = 503


def _next_midnight(now: datetime) -> datetime:
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


DAILY_MESSAGE = "Today's allowance for this feature is used up. It resets at midnight UTC."


def daily_exhausted(now: datetime, message: str = DAILY_MESSAGE) -> BudgetExhausted:
    retry = int((_next_midnight(now) - now).total_seconds()) + 1
    return BudgetExhausted(message, code="daily_budget_exhausted",
                           headers={"Retry-After": str(retry)})


class UsageStore:
    def __init__(self, path: str, daily: dict[str, int], monthly: dict[str, int], clock: Clock):
        self.daily = daily
        self.monthly = monthly
        self.clock = clock
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5)
            db.execute("PRAGMA journal_mode=WAL")
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

    def _bump(self, period: str, api: str, requests: int, units: int, refused: int) -> None:
        self._db.execute(
            "INSERT INTO usage (period, api, requests, units, refused) VALUES (?,?,?,?,?)"
            " ON CONFLICT(period, api) DO UPDATE SET requests=requests+excluded.requests,"
            " units=units+excluded.units, refused=refused+excluded.refused",
            (period, api, requests, units, refused),
        )

    # ── public ──────────────────────────────────────────────────────
    def reserve(self, api: str, units: int) -> None:
        """Count `units` against today's and this month's budget, or refuse."""
        if self._db is None:
            raise Busy()
        day, month, now = self._periods()
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    used_day = self._units(day, api)
                    used_month = self._units(month, api)
                    over_month = used_month + units > self.monthly.get(api, 0)
                    over_day = used_day + units > self.daily.get(api, 0)
                    if over_month or over_day:
                        self._bump(day, api, 0, 0, 1)
                        self._db.execute("COMMIT")
                    else:
                        self._bump(day, api, 1, units, 0)
                        self._bump(month, api, 1, units, 0)
                        self._db.execute("COMMIT")
                        return
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
            except sqlite3.Error as exc:
                log.error("usage store write failed (%s)", type(exc).__name__)
                raise Busy() from None
        if over_month:
            raise BudgetExhausted(
                "This month's allowance for this feature is used up. It resets on the 1st (UTC).",
                code="monthly_budget_exhausted",
            )
        raise daily_exhausted(now)

    def refund(self, api: str, units: int) -> None:
        """Give back a reservation for a request that was never sent."""
        if self._db is None:
            return
        day, month, _ = self._periods()
        with self._lock:
            try:
                self._bump(day, api, -1, -units, 0)
                self._bump(month, api, -1, -units, 0)
            except sqlite3.Error as exc:
                log.error("usage store refund failed (%s)", type(exc).__name__)

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
                "SELECT api, requests, units, refused FROM usage WHERE period=? ORDER BY api", (day,)
            ).fetchall()
        return {api: {"requests": r, "units": u, "refused": f} for api, r, u, f in rows}
