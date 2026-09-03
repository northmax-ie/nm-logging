"""Abstract contracts the rest of the package depends on, plus the default clock.

Kept here, not inside a concrete implementation, so that both a facade and a
concrete implementation import a protocol downward rather than the facade
importing its core type from one specific implementation module.

The one non-protocol here is ``SystemClock``, the trivial default clock. It moved
here once both the operational and audit facades needed it: a single shared
default avoids one facade importing its clock from the other, which would be a
sideways dependency between peers.

``Reader`` (M6) and ``AuditSink`` (M5) arrive with their milestones.
"""

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from .record import AuditRecord, Record


class Clock(Protocol):
    """Supplies the current time as a timezone-aware UTC datetime.

    The clock is a construction-time dependency of a logging facade, never a
    per-call parameter: a caller-supplied timestamp is a forged timestamp, in
    the same way a caller-supplied nonce is a reused nonce in a cryptographic API
    (§16.1). Record construction rejects a naive or non-UTC value regardless of
    the clock's promises, so a broken clock fails fast rather than persisting a
    bad timestamp.
    """

    def now(self) -> datetime: ...


@runtime_checkable
class Sink(Protocol):
    """A destination for records. ``write`` persists one record; ``close``
    releases the underlying resource.

    ``write`` raises ``SinkError`` when it cannot persist the record (an I/O
    failure), and ``EventSchemaError`` when the record violates the contract the
    sink enforces at encode time — specifically an over-limit record (§14.4).
    The two are distinct on purpose: the operational path contains a schema
    defect one way and a write failure another (see operational.py). There is no
    ``flush`` in the protocol; a sink that needs one owns it privately.
    """

    def write(self, record: Record) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class AuditSink(Protocol):
    """A durable destination for audit records — a different contract from Sink.

    ``append`` returns only once the record is durably persisted. It raises
    ``EventSchemaError`` if the encoded record exceeds the permitted size (a
    contract violation, classified as in the operational sink) and
    ``AuditPersistenceError`` if durability cannot be established — never
    ``SinkError``, so an audit failure can never be swallowed by the operational
    fail-open path (invariant 5). Both leave the record unwritten; raised from an
    intent append and left uncaught by the audit path, either prevents the caller
    from proceeding with the mutation (§9.3). Audit is append-only (§15): there is
    no update or delete here, by construction.

    ``supports_atomic`` reports whether the backend can commit an application
    state change and its audit record in one atomic transaction. When False, an
    audited local mutation must use the intent/outcome model (§9.3, §22); the
    initial file backend is False.
    """

    @property
    def supports_atomic(self) -> bool: ...

    def append(self, record: AuditRecord) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class Reader(Protocol):
    """Reads persisted records back for UI and export, so application UI code
    depends on this abstraction, not the file layout (§22, §24).

    Iterating yields each record as a plain mapping of field name to value, in the
    order written — backend-neutral, so a later database backend presents the
    same shape without applications redefining what their events mean. Filtering
    and pagination are deferred (§26); this offers iteration only.

    ``truncated`` reports, after iteration, whether the final physical line was
    unterminated — a torn tail, the expected residue of a crash mid-append. That
    is the only tolerated damage. Every framed line must be a JSON object; a blank
    line, malformed JSON, a valid non-object, or an undecodable line is corruption
    and raises ``ReaderError`` carrying a line number and byte offset, never the
    line content (which is untrusted and may be secret-bearing). Either way the
    reader never repairs, rewrites, or truncates the source: audit is append-only
    from the application's perspective (§15).
    """

    @property
    def truncated(self) -> bool: ...

    def __iter__(self) -> Iterator[Mapping[str, object]]: ...


class SystemClock:
    """The default clock: timezone-aware UTC from ``datetime.now(UTC)``.

    Uses the timezone-aware constructor, never the deprecated naive-UTC one that
    returns a value without tzinfo (§16.1). Structural match to the Clock
    protocol; a test may substitute a frozen clock.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)
