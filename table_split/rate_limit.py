"""
table_split.rate_limit — Thread-safe sliding-window rate limiter for Table Split endpoints.

Enforces limits for:
- Join attempts per IP (`join_ip`)
- Join attempts per join token / session (`join_token`)
- Preference updates per participant (`preference_participant`)
- Session chat/message updates per participant (`message_participant`)
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitRule:
    max_requests: int
    window_seconds: int


class RateLimitExceededError(Exception):
    """Raised when a caller exceeds the configured sliding-window rate limit."""

    def __init__(self, bucket: str, retry_after_seconds: int) -> None:
        self.bucket = bucket
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__(f"Rate limit exceeded for '{bucket}'. Retry after {self.retry_after_seconds}s.")


DEFAULT_RULES: dict[str, RateLimitRule] = {
    "join_ip": RateLimitRule(max_requests=15, window_seconds=60),
    "join_token": RateLimitRule(max_requests=25, window_seconds=60),
    "preference_participant": RateLimitRule(max_requests=30, window_seconds=60),
    "message_participant": RateLimitRule(max_requests=20, window_seconds=60),
}


class SlidingWindowRateLimiter:
    """In-memory thread-safe sliding-window rate limiter."""

    def __init__(self, rules: dict[str, RateLimitRule] | None = None) -> None:
        self._rules: dict[str, RateLimitRule] = dict(rules or DEFAULT_RULES)
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def set_rule(self, category: str, max_requests: int, window_seconds: int) -> None:
        with self._lock:
            self._rules[category] = RateLimitRule(max_requests=max_requests, window_seconds=window_seconds)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def check_and_consume(
        self,
        category: str,
        identifier: str,
        now: float | None = None,
    ) -> None:
        """
        Records a hit for `(category, identifier)` if within limit, or raises
        RateLimitExceededError with retry_after_seconds.
        """
        current_ts = time.monotonic() if now is None else now
        rule = self._rules.get(category)
        if rule is None or rule.max_requests <= 0:
            return

        bucket_key = f"{category}:{identifier}"
        cutoff = current_ts - rule.window_seconds

        with self._lock:
            q = self._buckets[bucket_key]
            while q and q[0] <= cutoff:
                q.popleft()

            if len(q) >= rule.max_requests:
                oldest = q[0]
                retry_after = int(oldest + rule.window_seconds - current_ts) + 1
                raise RateLimitExceededError(category, max(1, retry_after))

            q.append(current_ts)
