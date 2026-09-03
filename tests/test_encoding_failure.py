"""R4 — a schema-valid string must not fail operational logging closed.

A lone surrogate is a valid ``str`` but not UTF-8 encodable. Validation rejects it
as ``EventSchemaError`` (the policy); both sinks convert a `UnicodeEncodeError`
during encoding into ``EventSchemaError`` too (defence in depth) — never
``SinkError`` or ``AuditPersistenceError``. So operational production contains it
as a defect with health undegraded, strict surfaces it, audit ``intent()`` fails
before the mutation, and no stream latches (no byte was attempted).
"""

import pytest

from nm_logging import (
    EventRegistry,
    EventSchema,
    EventSchemaError,
    FieldSpec,
    JsonlAuditSink,
    JsonlSink,
    OperationalLog,
    Severity,
)
from nm_logging.operational import DEFECT_EVENT
from nm_logging.record import AuditRecord, OperationalRecord

from .helpers import CollectingSink, FrozenClock, make_audit

LONE_SURROGATE = "\ud800"  # a valid str, not UTF-8 encodable

STR_EVENT = EventSchema("thing.noted", severity=Severity.INFO, fields=(FieldSpec("note", str),))


def _op_registry() -> EventRegistry:
    registry = EventRegistry()
    registry.register(STR_EVENT)
    return registry


# --- validation (the policy) ----------------------------------------------


def test_lone_surrogate_rejected_at_validation():
    with pytest.raises(EventSchemaError):
        STR_EVENT.validate_operational(Severity.INFO, {"note": LONE_SURROGATE})


def test_valid_unicode_still_accepted():
    result = STR_EVENT.validate_operational(Severity.INFO, {"note": "Ölandsråd — 日本語"})
    assert result["note"] == "Ölandsråd — 日本語"


# --- operational facade: contained, health undegraded ----------------------


def test_operational_info_contains_unencodable_and_stays_healthy():
    sink = CollectingSink()
    log = OperationalLog("exampleapp", _op_registry(), sink, clock=FrozenClock())
    assert log.info("thing.noted", note=LONE_SURROGATE) is None
    # Health is not falsely reported as a storage failure.
    assert log.health.degraded is False
    # A defect record was emitted (rejected at validation, so schema_violation).
    assert len(sink.records) == 1
    defect = sink.records[0]
    assert defect.event == DEFECT_EVENT
    assert defect.fields["violation"] == "schema_violation"


def test_strict_mode_surfaces_the_unencodable_defect():
    sink = CollectingSink()
    log = OperationalLog("exampleapp", _op_registry(), sink, clock=FrozenClock(), strict=True)
    with pytest.raises(EventSchemaError):
        log.info("thing.noted", note=LONE_SURROGATE)


# --- sinks: defence in depth, EventSchemaError not UnicodeEncodeError -------


def test_operational_sink_rejects_unencodable_record_without_latching(tmp_path):
    path = tmp_path / "op.jsonl"
    sink = JsonlSink(path)
    # Built directly, bypassing validation, to reach the sink's own guard.
    record = OperationalRecord(
        application="exampleapp",
        emitter="app",
        event="thing.noted",
        timestamp=FrozenClock().now(),
        severity=Severity.INFO,
        fields={"note": LONE_SURROGATE},
    )
    try:
        with pytest.raises(EventSchemaError):
            sink.write(record)
        assert sink._state.latched is False  # no byte attempted
        assert path.read_text(encoding="utf-8") == ""
    finally:
        sink.close()


def test_audit_sink_rejects_unencodable_record_without_latching(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    record = make_audit(fields={"note": LONE_SURROGATE})
    assert isinstance(record, AuditRecord)
    try:
        with pytest.raises(EventSchemaError):
            sink.append(record)
        assert sink._state.latched is False
        assert path.read_text(encoding="utf-8") == ""
    finally:
        sink.close()
