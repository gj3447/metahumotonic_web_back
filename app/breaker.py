"""Tiny time-based circuit breaker.

Replaces the permanent `_failed = True` latch (full-verify finding: a single
transient blip in Neo4j/Redis/Mongo otherwise degraded a replica until pod
restart). After a failure the breaker stays open for `cooldown` seconds, then
allows one retry; a success resets it.
"""

from __future__ import annotations

import time

DEFAULT_COOLDOWN = 30.0


class Breaker:
    def __init__(self, cooldown: float = DEFAULT_COOLDOWN) -> None:
        self.cooldown = cooldown
        self._retry_after = 0.0

    def is_open(self) -> bool:
        """True ⇒ skip the dependency (recently failed, still cooling down)."""
        return time.monotonic() < self._retry_after

    def trip(self) -> None:
        self._retry_after = time.monotonic() + self.cooldown

    def reset(self) -> None:
        self._retry_after = 0.0
