"""Per-user daily character quota and request rate limiting.

The daily quota is persisted in SQLite so it survives restarts; the rate
limiter is an in-process token bucket. Both assume a **single worker process**
- see the note in the systemd unit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Tuple

try:  # Python 3.9+ stdlib; missing only on a system without tzdata
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

__all__ = ["quota_day", "day_reset_epoch", "TokenBucket", "RateLimiter"]


def _zone(tz_name: str):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tz_name)
    except Exception:  # pragma: no cover - unknown zone falls back to UTC
        return None


def quota_day(tz_name: str = "UTC", *, now: float | None = None) -> str:
    """Today's ``YYYY-MM-DD`` in the quota timezone.

    Quotas reset at local midnight, so an Israeli user's day rolls over when
    it does for them, not at 02:00 or 03:00.
    """
    moment = datetime.fromtimestamp(now if now is not None else time.time(), tz=_zone("UTC"))
    zone = _zone(tz_name)
    if zone is not None:
        moment = moment.astimezone(zone)
    return moment.strftime("%Y-%m-%d")


def day_reset_epoch(tz_name: str = "UTC", *, now: float | None = None) -> int:
    """Epoch second of the next local midnight - the ``resets_at`` in the API."""
    zone = _zone(tz_name) or _zone("UTC")
    moment = datetime.fromtimestamp(now if now is not None else time.time(), tz=zone)
    tomorrow = (moment + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(tomorrow.timestamp())


@dataclass
class TokenBucket:
    """Classic token bucket: ``capacity`` burst, refilled at ``rate`` per second."""

    capacity: float
    rate: float
    tokens: float = field(default=0.0)
    updated: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.tokens <= 0:
            self.tokens = self.capacity

    def take(self, amount: float = 1.0, *, now: float | None = None) -> Tuple[bool, float]:
        """Try to spend a token. Returns ``(allowed, retry_after_seconds)``."""
        moment = now if now is not None else time.monotonic()
        elapsed = max(0.0, moment - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated = moment
        if self.tokens >= amount:
            self.tokens -= amount
            return True, 0.0
        missing = amount - self.tokens
        return False, missing / self.rate if self.rate > 0 else 60.0


class RateLimiter:
    """Named token buckets, e.g. one per user id or per client IP."""

    def __init__(self, per_minute: int, burst: int) -> None:
        self.rate = max(per_minute, 1) / 60.0
        self.capacity = float(max(burst, 1))
        self._buckets: Dict[str, TokenBucket] = {}

    def check(self, key: str, *, now: float | None = None) -> Tuple[bool, float]:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(capacity=self.capacity, rate=self.rate)
            self._buckets[key] = bucket
        allowed, retry_after = bucket.take(now=now)
        if len(self._buckets) > 10_000:  # keep the dict from growing unbounded
            self._evict(now=now)
        return allowed, retry_after

    def _evict(self, *, now: float | None = None) -> None:
        moment = now if now is not None else time.monotonic()
        stale = [k for k, b in self._buckets.items() if moment - b.updated > 3600]
        for key in stale:
            self._buckets.pop(key, None)
