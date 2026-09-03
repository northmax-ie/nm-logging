"""nm-logging: shared structured operational and audit logging for NorthMax apps.

Import this package by symbol. Never alias it to ``logging``, which is a stdlib
module already in use in consuming applications. This package owns the record
contract, its enforcement, and the
persistence interface; it owns no application's event catalogue.

The public surface is: the severity and audit vocabularies; the exception
hierarchy; the record contract constants; the event grammar, schema, and
registry; safe exception evidence; the operational and audit facades with their
health and sinks; and the ``Reader`` protocol. There is no DEBUG severity and no
runtime verbosity control, by design (§4).

The record classes ``Record``, ``OperationalRecord``, and ``AuditRecord`` are
deliberately **not** on this surface (R1). Applications emit through the facades,
which enforce the schema; the raw constructors would let a caller forge an
authoritative record, so the supported path and the enforced path are the same
one. The classes still exist for internal use at ``nm_logging.record``.

Direct import of implementation modules (``nm_logging.record``,
``nm_logging.serialise``, the ``sinks`` package, and so on) is outside the
supported-API guarantee and is **not** treated as a security boundary: Python
cannot make it impossible, and R1 does not pretend otherwise. The guarantee is
that the *supported* surface routes through enforcement.

Two more things live deliberately just outside this top-level surface. The
concrete ``JsonlReader`` is at ``nm_logging.reader.jsonl``, because nothing in the
write-side package may import the reader (§22). Foreign-logging containment is at
``nm_logging.containment`` (``install`` / ``uninstall``), kept off the top level
because a bare ``install`` is too generic a name to export.
"""

from .audit import AuditLog, AuditOperation
from .audit_vocab import Category, Outcome, Stage
from .exceptions import (
    AuditFinalisationError,
    AuditPersistenceError,
    AuditUsageError,
    EventSchemaError,
    LoggingConfigurationError,
    NmLoggingError,
    ReaderError,
    SinkError,
)
from .events import EventRegistry, EventSchema, FieldSpec
from .evidence import (
    SAFE_MESSAGE_ATTR,
    CodeLocation,
    Evidence,
    ExceptionEvidence,
    build_evidence,
)
from .health import HealthSnapshot, LoggingHealth
from .interfaces import AuditSink, Clock, Reader, Sink, SystemClock
from .operational import OperationalLog
from .record import (
    EMITTERS,
    MAX_FIELD_CHARS,
    MAX_RECORD_BYTES,
    SCHEMA_VERSION,
    FieldValue,
)
from .severity import SYSLOG_KEYWORD, Severity
from .sinks.jsonl import JsonlSink
from .sinks.jsonl_audit import JsonlAuditSink
from .sinks.stderr import StderrFallback

__all__ = [
    "AuditFinalisationError",
    "AuditLog",
    "AuditOperation",
    "AuditPersistenceError",
    "AuditSink",
    "AuditUsageError",
    "Category",
    "Clock",
    "CodeLocation",
    "EMITTERS",
    "EventRegistry",
    "EventSchema",
    "EventSchemaError",
    "Evidence",
    "ExceptionEvidence",
    "FieldSpec",
    "FieldValue",
    "HealthSnapshot",
    "JsonlAuditSink",
    "JsonlSink",
    "LoggingConfigurationError",
    "LoggingHealth",
    "MAX_FIELD_CHARS",
    "MAX_RECORD_BYTES",
    "NmLoggingError",
    "OperationalLog",
    "Outcome",
    "Reader",
    "ReaderError",
    "SAFE_MESSAGE_ATTR",
    "SCHEMA_VERSION",
    "SYSLOG_KEYWORD",
    "Severity",
    "Sink",
    "SinkError",
    "Stage",
    "StderrFallback",
    "SystemClock",
    "build_evidence",
]
