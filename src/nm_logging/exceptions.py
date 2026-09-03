"""Exception hierarchy. Everything inherits from NmLoggingError.

The two failure contracts of this package (§14.1 operational fails open, §23
audit fails hard) are kept apart in the type lattice as well as in the code:
``AuditPersistenceError`` is a direct subclass of ``NmLoggingError`` and
deliberately NOT of ``SinkError``, so the operational path's generic sink
handling can never swallow an audit-durability failure. See its docstring.
"""


class NmLoggingError(Exception):
    """Base class for every error raised by nm-logging.

    Carries the structural safe-message marker (settled decision 6): the whole
    hierarchy's messages are NorthMax-controlled and never echo untrusted or
    unbounded input — event ids, field names, and field values are all withheld —
    so the evidence renderer (evidence.py) may include their message text. The
    marker is a bare class attribute checked with ``getattr`` and matched against
    the name in ``evidence.SAFE_MESSAGE_ATTR``; it is deliberately not imported
    from evidence.py, so any package can adopt the same convention without a
    dependency edge in either direction.
    """

    # See evidence.SAFE_MESSAGE_ATTR. Kept as a literal, not an import, so the
    # marker stays a pure structural convention shared by value.
    log_safe_message = True


class LoggingConfigurationError(NmLoggingError):
    """The logging setup is wrong: a bad application id or emitter, a duplicate
    or malformed event registration, a reserved-name collision, an unusable sink
    target at construction, or a clock that yields a naive or non-UTC timestamp.

    It is not raised for a call site that violates a declared event schema (that
    is EventSchemaError) nor for a sink that fails to write at runtime (SinkError).
    """


class EventSchemaError(NmLoggingError):
    """A call site violated a declared schema: an unknown event, an undeclared
    field, a wrong type, a disallowed severity, or an oversized record.

    This is the §14.4 logging programming error. It is raised in strict
    (development/test) mode and contained in production, where the operational
    path emits a package-owned defect record instead. It is not a durability
    failure and never a substitute for AuditPersistenceError.
    """


class SinkError(NmLoggingError):
    """A sink could not write a record.

    Raised by sink implementations and caught, contained, by the operational
    path (§14.1): the caller never sees it. It is NOT the audit-durability
    failure type; see AuditPersistenceError, which intentionally does not inherit
    from this class so it can never be caught by operational sink handling.
    """


class AuditPersistenceError(NmLoggingError):
    """Required audit durability was not achieved (§9.3, §23).

    A DIRECT subclass of NmLoggingError, deliberately NOT of SinkError, so the
    operational fail-open path — which catches SinkError and contains it — can
    never swallow it. Audit fails hard: when durable intent cannot be recorded,
    the caller must not perform the mutation; when the outcome cannot be
    appended, the mutation is not retracted and the intent is left orphaned for
    reconciliation (§9.3). This exception carries no offending value and never
    the failing record's contents.

    ``operation_id`` identifies the operation for reconciliation. It is set on
    the facade-normalized instance (audit.py §5.1); a sink-raised instance may
    leave it None.
    """

    operation_id: str | None = None


class AuditUsageError(NmLoggingError):
    """An audit operation was misused, for example finalised twice.

    A programming error in how the audit API was called, distinct from a
    durability failure (AuditPersistenceError): the storage is fine, the caller
    is not. It never masks or replaces a durability failure.
    """


class AuditFinalisationError(NmLoggingError):
    """An audited operation's outcome could not be established: finalisation
    failed while, or after, the audited block ran, so a durable intent exists
    with no outcome. ``operation_id`` identifies it for reconciliation;
    ``finalisation_exception`` gives the true reason with its true type, and
    ``body_exception`` gives whatever escaped the block.

    Deliberately does **not** subclass ``AuditPersistenceError`` (v6 §5.6). The
    class may represent a finalisation failure that was not a persistence failure
    at all — an ``EventSchemaError`` from encoding, say — so inheriting the
    persistence classification would assert in the hierarchy something that may be
    false, the same falsification §5.1 Rule 2 forbids one level up for
    ``.finalisation_exception``. The true classification lives on that attribute,
    which keeps its own type. It derives instead from the nearest existing neutral
    base, ``NmLoggingError`` (the tree has no audit-neutral intermediate, and this
    work introduces none).

    Consequence for callers: an existing ``except AuditPersistenceError`` will
    **not** catch this. Catch the neutral base ``NmLoggingError``, or name
    ``AuditFinalisationError`` explicitly (see usage.md).
    """

    def __init__(
        self,
        *,
        body_exception: BaseException,
        finalisation_exception: BaseException,
        operation_id: str,
    ) -> None:
        super().__init__("audit outcome could not be established")
        self.body_exception = body_exception
        self.finalisation_exception = finalisation_exception
        self.operation_id = operation_id


class ReaderError(NmLoggingError):
    """A persisted record could not be read: a stored, fully framed line is not
    valid JSON — corruption.

    Distinct from a torn tail: an unterminated final line is the expected residue
    of a crash mid-append, which a reader tolerates by stopping and reporting
    truncation rather than raising (§15). A newline-terminated line that does not
    parse — anywhere in the file, the final record included — is corruption and
    raises this instead. It carries a location, never the record's content, and
    the reader never repairs or rewrites the file.
    """
