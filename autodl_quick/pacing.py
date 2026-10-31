"""Timing for sequential inventory probes; creation is never scheduled here."""

from __future__ import annotations

import random


class PollCadence:
    HOT_SECONDS = 20
    REJECTION_SECONDS = 10

    def __init__(self, interval: float, now: float):
        self.interval = interval
        self.hot_until = now + self.HOT_SECONDS
        self.failures = 0

    def activity(self, now: float) -> None:
        self.hot_until = now + self.HOT_SECONDS

    def delay(self, started: float, now: float, *, failed: bool = False,
              retry_after: float = 0) -> float:
        if failed:
            self.failures += 1
            self.activity(now)
            # Failures wait from completion. A server cooldown is a lower bound.
            backoff = max(self.interval, min(60, max(2, self.interval) * 2 ** min(self.failures - 1, 6)))
            return max(backoff * random.uniform(1, 1.1), retry_after)
        self.failures = 0
        interval = self.interval
        if now >= self.hot_until:
            interval = max(interval, min(30, interval * 3))
        # Count successful probes from their start, without overlapping probes or
        # catching up on missed ticks. Positive jitter never speeds up the rate.
        return max(0.1, started + interval * random.uniform(1, 1.1) - now)
