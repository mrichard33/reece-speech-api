"""Simple in-memory requests-per-minute limiter, one window per API key."""

import hashlib
import threading
import time
from collections import deque
from typing import Callable


class RateLimiter:
    def __init__(self, per_minute: int, window_seconds: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self.per_minute = per_minute
        self.window = window_seconds
        self.clock = clock
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        # Keyed on a hash so the raw key is not kept around as a dict key.
        bucket_id = hashlib.sha256(key.encode()).hexdigest()
        now = self.clock()
        with self._lock:
            hits = self._hits.setdefault(bucket_id, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return False
            hits.append(now)
            return True
