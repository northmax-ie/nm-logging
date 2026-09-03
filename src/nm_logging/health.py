"""Logging health: a small readable state the application polls (§14.2).

Operational degradation is represented here, independently of the failed sink,
so the application can surface it. The write path only *sets* state on this
object; it never calls back into application code. That is deliberate: a health
callback invoked on the write path would reintroduce reentrancy exactly where the
system is already failing (§ hazards). The application reads; it is not called.

Failure kinds recorded here are short fixed strings describing the category of
failure (for example ``"sink_write"``), never a record's values.
"""

from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """An immutable point-in-time view of logging health.

    ``degraded`` is the current state; the counters are monotonic totals over the
    life of the health object, so a poller can see churn even if the state has
    since recovered.
    """

    degraded: bool
    total_failures: int
    total_recoveries: int
    last_failure_kind: str | None


class LoggingHealth:
    """Mutable health state, written by the write path and read by the app.

    A lock guards the small state so a multi-threaded writer cannot tear a
    snapshot. No method here calls out; recovery is observed by the next
    successful write calling ``mark_healthy`` (§14.2).
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._degraded = False
        self._total_failures = 0
        self._total_recoveries = 0
        self._last_failure_kind: str | None = None

    def mark_degraded(self, kind: str) -> None:
        """Record a failure of the given kind and enter the degraded state."""
        with self._lock:
            self._degraded = True
            self._total_failures += 1
            self._last_failure_kind = kind

    def mark_healthy(self) -> None:
        """Record that a write succeeded; leave the degraded state if in it.

        Called after every successful write. Recovery is thus observable without
        the write path calling into application code.
        """
        with self._lock:
            if self._degraded:
                self._degraded = False
                self._total_recoveries += 1

    @property
    def degraded(self) -> bool:
        with self._lock:
            return self._degraded

    def snapshot(self) -> HealthSnapshot:
        with self._lock:
            return HealthSnapshot(
                degraded=self._degraded,
                total_failures=self._total_failures,
                total_recoveries=self._total_recoveries,
                last_failure_kind=self._last_failure_kind,
            )
