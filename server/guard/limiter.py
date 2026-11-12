from __future__ import annotations

from collections import OrderedDict, deque

from server.errors import Busy
from server.guard.clock import Clock


class SlidingWindow:
    """At most `limit` acquisitions in any `window_s` seconds."""

    def __init__(self, limit: int, window_s: float, clock: Clock):
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self._hits: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._hits and now - self._hits[0] >= self.window_s:
            self._hits.popleft()

    def try_acquire(self) -> float | None:
        """Take a slot and return None, or return the seconds until one frees up."""
        now = self.clock.monotonic()
        self._prune(now)
        if len(self._hits) < self.limit:
            self._hits.append(now)
            return None
        if not self._hits:  # limit of 0
            return self.window_s
        return max(0.0, self._hits[0] + self.window_s - now)

    def available(self) -> int:
        """Slots free right now."""
        self._prune(self.clock.monotonic())
        return max(0, self.limit - len(self._hits))

    async def acquire(self, max_wait_s: float) -> float:
        """Take a slot, waiting up to max_wait_s. Returns the slot's timestamp (for give_back)."""
        waited = 0.0
        while True:
            wait = self.try_acquire()
            if wait is None:
                return self._hits[-1]
            if self.limit <= 0 or waited + wait > max_wait_s:
                raise Busy(headers={"Retry-After": str(max(1, int(wait) + 1))})
            await self.clock.sleep(wait)
            waited += wait

    def acquire_now(self) -> float:
        """Take a slot without waiting, or raise Busy. Returns the slot's timestamp."""
        wait = self.try_acquire()
        if wait is not None:
            raise Busy(headers={"Retry-After": str(max(1, int(wait) + 1))})
        return self._hits[-1]

    def give_back(self, stamp: float) -> None:
        """Return a slot that was taken but not used for a request."""
        try:
            self._hits.remove(stamp)
        except ValueError:
            pass  # already expired


class KeyedLimiter:
    """A sliding window per key (client IP), with a bounded number of keys."""

    def __init__(self, limit: int, window_s: float, max_keys: int, clock: Clock):
        self.limit = limit
        self.window_s = window_s
        self.max_keys = max_keys
        self.clock = clock
        self._keys: OrderedDict[str, deque[float]] = OrderedDict()

    def hit(self, key: str) -> float | None:
        """Count one request. Returns None if allowed, else seconds until allowed."""
        now = self.clock.monotonic()
        hits = self._keys.get(key)
        if hits is None:
            hits = deque()
            self._keys[key] = hits
            while len(self._keys) > self.max_keys:
                self._keys.popitem(last=False)
        else:
            self._keys.move_to_end(key)
        while hits and now - hits[0] >= self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return self.window_s if not hits else max(0.0, hits[0] + self.window_s - now)
        hits.append(now)
        return None

    def __len__(self) -> int:
        return len(self._keys)
