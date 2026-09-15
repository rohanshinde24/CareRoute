"""A circuit breaker for calls that leave this process.

Bounded retries with jitter stop one caller hammering a struggling service, but
they do not stop *every* caller doing it at once. While a dependency is down,
each request still pays the full retry budget before failing, so the outage is
converted into latency and the caller's own threads or connections are consumed
waiting on something already known to be broken. The breaker short-circuits
that: after enough consecutive failures it fails immediately, and periodically
lets one request through to see whether recovery has happened.

Two design points worth stating.

**State is per process, not per instance.** A new gateway is constructed for
every request, so a breaker living on the gateway would reset constantly and
never trip. Breakers are held in a module-level registry keyed by target.

**Only transient failures count.** A malformed payload or a 4xx means this
system's request was wrong, not that the dependency is unhealthy; tripping on
those would turn a bug on our side into an apparent outage on theirs.
"""

from __future__ import annotations

import enum
import logging
import threading
import time

logger = logging.getLogger(__name__)


class CircuitState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(RuntimeError):
    """Raised instead of attempting a call the breaker believes will fail."""


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 5, reset_seconds: float = 30.0, clock=time.monotonic):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        # Set while a half-open probe is in flight, so only one request is used
        # to test recovery rather than the whole waiting crowd.
        self._probe_in_flight = False

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._observe()

    def _observe(self) -> CircuitState:
        """Caller must hold the lock. Promotes OPEN to HALF_OPEN once cool."""
        if self._state is CircuitState.OPEN and self._opened_at is not None:
            if self._clock() - self._opened_at >= self.reset_seconds:
                self._state = CircuitState.HALF_OPEN
                self._probe_in_flight = False
        return self._state

    def before_call(self) -> None:
        """Raise CircuitOpen if this call should not be attempted."""
        with self._lock:
            state = self._observe()
            if state is CircuitState.CLOSED:
                return
            if state is CircuitState.HALF_OPEN and not self._probe_in_flight:
                self._probe_in_flight = True
                return
            raise CircuitOpen(f"{self.name} circuit is {state.value}; not attempting the call")

    def record_success(self) -> None:
        with self._lock:
            if self._state is not CircuitState.CLOSED:
                logger.info("%s circuit closing after a successful call", self.name)
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    def record_failure(self) -> None:
        """Only transient failures should reach this."""
        with self._lock:
            self._probe_in_flight = False
            # A failed probe re-opens immediately; the dependency is still down.
            if self._state is CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
                logger.warning("%s circuit re-opening; probe failed", self.name)
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                if self._state is not CircuitState.OPEN:
                    logger.warning("%s circuit opening after %s consecutive failures", self.name, self._consecutive_failures)
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()

    def reset(self) -> None:
        """For tests and for an operator who knows the dependency is back."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None
            self._probe_in_flight = False


_breakers: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def breaker_for(name: str, failure_threshold: int = 5, reset_seconds: float = 30.0) -> CircuitBreaker:
    """Shared breaker for a target, created once per process.

    Thresholds on an existing breaker are updated rather than ignored. Silently
    discarding them would mean a configuration change appeared to take effect
    and did not, which is the kind of difference nobody notices until a breaker
    fails to trip during an incident.
    """
    with _registry_lock:
        existing = _breakers.get(name)
        if existing is None:
            existing = CircuitBreaker(name, failure_threshold, reset_seconds)
            _breakers[name] = existing
            return existing
        if existing.failure_threshold != failure_threshold or existing.reset_seconds != reset_seconds:
            existing.failure_threshold = failure_threshold
            existing.reset_seconds = reset_seconds
        return existing


def reset_all() -> None:
    with _registry_lock:
        for breaker in _breakers.values():
            breaker.reset()
