"""R1 — the supported public path is the enforced path.

The top-level surface is pinned to the approved fixture; the raw record
constructors are off it; event fields cannot displace authoritative envelope
metadata (rejected at construction *and* at serialisation, each in isolation);
each concrete sink rejects the wrong record kind; and supported emission still
runs through schema validation. Removing the three names alone does not close the
finding — every requirement below has its own assertion.
"""

import importlib

import pytest

from nm_logging import (
    Category,
    EventRegistry,
    EventSchema,
    EventSchemaError,
    FieldSpec,
    JsonlAuditSink,
    JsonlSink,
    OperationalLog,
    Severity,
    Stage,
)
import nm_logging
from nm_logging.record import AuditRecord, OperationalRecord, Record
from nm_logging.serialise import to_mapping

from .helpers import CollectingSink, FrozenClock, make_audit, make_operational

# The exact approved __all__ fixture. It started at the 38 names approved for R1
# (strictly subtractive); Unit B then adds exactly one deliberate public
# exception, AuditFinalisationError, which callers must be able to catch by name
# (§5.6). Any other change must go through owner approval — this is the contract.
APPROVED_ALL = [
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

_RESERVED_ENVELOPE_KEYS = [
    "schema_version",
    "event",
    "severity",
    "category",
    "actor",
    "stage",
    "operation_id",
    "outcome",
]


# --- the surface itself ---------------------------------------------------


def test_all_matches_the_approved_fixture_exactly():
    assert nm_logging.__all__ == APPROVED_ALL


def test_all_is_sorted_and_every_name_resolves():
    assert nm_logging.__all__ == sorted(nm_logging.__all__)
    assert all(hasattr(nm_logging, name) for name in nm_logging.__all__)


@pytest.mark.parametrize("name", ["Record", "OperationalRecord", "AuditRecord"])
def test_record_classes_are_off_the_top_level_surface(name):
    assert name not in nm_logging.__all__
    assert not hasattr(nm_logging, name)


def test_from_package_import_of_record_fails():
    with pytest.raises(ImportError):
        from nm_logging import Record  # noqa: F401


def test_record_classes_remain_available_from_the_implementation_module():
    # The direct-import caveat: implementation modules stay importable for
    # internal use; this is documented as outside the supported-API guarantee and
    # is not a security boundary.
    mod = importlib.import_module("nm_logging.record")
    assert mod.Record is Record
    assert mod.OperationalRecord is OperationalRecord
    assert mod.AuditRecord is AuditRecord


# --- reserved-name collisions (construction and serialisation, in isolation) --


def test_reserved_collision_rejected_at_construction():
    with pytest.raises(EventSchemaError):
        OperationalRecord(
            application="exampleapp",
            emitter="app",
            event="a.b",
            timestamp=FrozenClock().now(),
            severity=Severity.INFO,
            fields={"severity": "FORGED"},
        )


def test_reserved_collision_rejected_at_serialisation_in_isolation():
    # Build a clean record, then inject a reserved-named field past construction
    # to prove the serialisation guard stands on its own — removing the
    # construction check would not reopen the hole.
    record = make_operational()
    object.__setattr__(record, "fields", {"event": "forged.event"})
    with pytest.raises(EventSchemaError):
        to_mapping(record)


@pytest.mark.parametrize("reserved", _RESERVED_ENVELOPE_KEYS)
def test_no_event_field_can_displace_envelope_metadata(reserved):
    record = make_operational()
    object.__setattr__(record, "fields", {reserved: "forged"})
    with pytest.raises(EventSchemaError):
        to_mapping(record)


def test_a_clean_record_still_serialises():
    mapping = to_mapping(make_operational(fields={"eligible": 5}))
    assert mapping["severity"] == "INFO"
    assert mapping["eligible"] == 5


# --- wrong record kinds rejected at each sink -----------------------------


def test_audit_sink_rejects_an_operational_record(tmp_path):
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    try:
        with pytest.raises(EventSchemaError):
            sink.append(make_operational())
        assert (tmp_path / "audit.jsonl").read_text(encoding="utf-8") == ""
    finally:
        sink.close()


def test_operational_sink_rejects_an_audit_record(tmp_path):
    sink = JsonlSink(tmp_path / "op.jsonl")
    try:
        with pytest.raises(EventSchemaError):
            sink.write(make_audit())
        assert (tmp_path / "op.jsonl").read_text(encoding="utf-8") == ""
    finally:
        sink.close()


def test_each_sink_accepts_its_own_kind(tmp_path):
    op_sink = JsonlSink(tmp_path / "op.jsonl")
    au_sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    try:
        op_sink.write(make_operational())
        au_sink.append(make_audit())
    finally:
        op_sink.close()
        au_sink.close()
    assert (tmp_path / "op.jsonl").read_text(encoding="utf-8").strip() != ""
    assert (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip() != ""


# --- supported emission still runs through schema validation ---------------


def test_supported_emission_passes_through_schema_validation():
    registry = EventRegistry()
    registry.register(
        EventSchema("thing.happened", severity=Severity.INFO, fields=(FieldSpec("n", int),))
    )
    sink = CollectingSink()
    log = OperationalLog("exampleapp", registry, sink, clock=FrozenClock(), strict=True)

    # A valid emission goes through and reaches the sink.
    log.info("thing.happened", n=1)
    assert len(sink.records) == 1

    # An undeclared field is still rejected by the schema (strict surfaces it).
    with pytest.raises(EventSchemaError):
        log.info("thing.happened", undeclared="x")
