"""Test-configuration constants and small builders shared across test modules.

Example identifiers are invented, not real NorthMax application or event names:
the package contains no application-specific knowledge (§21), and the tests keep
to the same discipline so a fixture can never smuggle a real catalogue in.
"""

from datetime import UTC, datetime
from pathlib import Path

from nm_logging import (
    AuditPersistenceError,
    Category,
    Severity,
    SinkError,
    Stage,
)

# The record classes are not on the top-level surface (R1); tests build them
# directly through the implementation module, which is the sanctioned internal
# path for constructing records outside the enforced facade.
from nm_logging.record import AuditRecord, OperationalRecord

EXAMPLE_OPERATION_ID_FACTORY_VALUE = "op-fixed-0001"

EXAMPLE_APPLICATION = "exampleapp"
EXAMPLE_EVENT = "example.thing.happened"
EXAMPLE_ACTOR = "alice"
EXAMPLE_OPERATION_ID = "op-0001"

# A fixed instant used wherever a record needs a timestamp. Timezone-aware UTC,
# so it is a valid record timestamp; the record model rejects anything naive.
FROZEN_TIMESTAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


class FrozenClock:
    """A Clock that always returns the same fixed instant.

    Structurally satisfies the Clock protocol without importing or subclassing
    it, the way a test double satisfies a protocol by shape, not by inheritance.
    Used to prove the record model's timestamp handling under a known instant.
    """

    def __init__(self, instant: datetime = FROZEN_TIMESTAMP) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant


def make_operational(**overrides) -> OperationalRecord:
    """Build an OperationalRecord with valid defaults, overridable per test."""
    params = dict(
        application=EXAMPLE_APPLICATION,
        emitter="app",
        event=EXAMPLE_EVENT,
        timestamp=FROZEN_TIMESTAMP,
        severity=Severity.INFO,
    )
    params.update(overrides)
    return OperationalRecord(**params)


def make_audit(**overrides) -> AuditRecord:
    """Build an AuditRecord with valid defaults, overridable per test.

    Defaults to a COMPLETE record, which carries no operation_id; tests that
    exercise intent/outcome pass ``stage`` and ``operation_id`` explicitly.
    """
    params = dict(
        application=EXAMPLE_APPLICATION,
        emitter="app",
        event=EXAMPLE_EVENT,
        timestamp=FROZEN_TIMESTAMP,
        category=Category.ACTIVITY,
        actor=EXAMPLE_ACTOR,
        stage=Stage.COMPLETE,
    )
    params.update(overrides)
    return AuditRecord(**params)


def read_lines(path) -> list[str]:
    """Return the non-empty lines of a JSONL file."""
    text = Path(path).read_text(encoding="utf-8")
    return text.splitlines()


class CollectingSink:
    """A Sink that keeps every record in memory, for inspection.

    It never encodes and never enforces size, so it exercises the emit path's
    control flow without the JSONL sink's byte concerns.
    """

    def __init__(self) -> None:
        self.records: list = []
        self.closed = False

    def write(self, record) -> None:
        self.records.append(record)

    def close(self) -> None:
        self.closed = True


class FailingSink:
    """A Sink whose every write raises SinkError. Counts calls so a test can
    prove the fail-open path does not retry or recurse."""

    def __init__(self) -> None:
        self.calls = 0

    def write(self, record) -> None:
        self.calls += 1
        raise SinkError("write failed")

    def close(self) -> None:
        pass


class FlakySink:
    """Fails its first ``fail_times`` writes with SinkError, then collects.

    Used to prove recovery is observable: the write after the failures marks
    health healthy again.
    """

    def __init__(self, fail_times: int = 1) -> None:
        self._left = fail_times
        self.records: list = []
        self.calls = 0

    def write(self, record) -> None:
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise SinkError("flaky write failed")
        self.records.append(record)

    def close(self) -> None:
        pass


class CollectingAuditSink:
    """An AuditSink that collects records, for inspection.

    ``atomic`` sets ``supports_atomic``. ``fail_on`` is a set of 1-based append
    indices that raise AuditPersistenceError instead of collecting, so a test can
    fail the intent (index 1) or the outcome (index 2) precisely.
    """

    def __init__(self, *, atomic: bool = False, fail_on: set[int] | None = None) -> None:
        self.records: list = []
        self._atomic = atomic
        self._fail_on = set(fail_on or ())
        self.calls = 0
        self.closed = False

    @property
    def supports_atomic(self) -> bool:
        return self._atomic

    def append(self, record) -> None:
        self.calls += 1
        if self.calls in self._fail_on:
            raise AuditPersistenceError("simulated durability failure")
        self.records.append(record)

    def close(self) -> None:
        self.closed = True


def fixed_operation_id_factory(value: str = EXAMPLE_OPERATION_ID_FACTORY_VALUE):
    """An operation-id factory returning a fixed value, for byte-stable vectors."""
    return lambda: value
