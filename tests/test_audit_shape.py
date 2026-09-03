"""Audit record shape and serialisation (§5, §18).

Covers: audit records carry no severity; absent fields are absent (no outcome on
an intent, no operation_id on a complete); operation_id appears only for
intent/outcome; and byte-stable vectors for an intent and an outcome record under
a frozen clock and a fixed operation-id factory.
"""

from nm_logging import (
    AuditLog,
    Category,
    EventRegistry,
    EventSchema,
    FieldSpec,
    JsonlAuditSink,
    Outcome,
    Stage,
)
from nm_logging.sinks.jsonl import encode

from .helpers import (
    CollectingAuditSink,
    FrozenClock,
    fixed_operation_id_factory,
    make_audit,
    read_lines,
)

DELETE_EVENT = EventSchema(
    "user.deleted",
    category=Category.ACTIVITY,
    fields=(FieldSpec("target", str, required=True), FieldSpec("reason", str)),
)


def _registry() -> EventRegistry:
    registry = EventRegistry()
    registry.register(DELETE_EVENT)
    return registry


def _log(sink, **kwargs) -> AuditLog:
    return AuditLog(
        "exampleapp",
        _registry(),
        sink,
        clock=FrozenClock(),
        operation_id_factory=fixed_operation_id_factory(),
        **kwargs,
    )


def test_audit_records_carry_no_severity():
    sink = CollectingAuditSink()
    log = _log(sink)
    op = log.intent("user.deleted", actor="alice", target="widget-7")
    op.success()
    for record in sink.records:
        assert not hasattr(record, "severity")
        assert '"severity"' not in encode(record)


def test_intent_record_has_no_outcome_field():
    sink = CollectingAuditSink()
    log = _log(sink)
    log.intent("user.deleted", actor="alice", target="widget-7")
    line = encode(sink.records[0])
    assert '"outcome"' not in line
    assert '"stage":"intent"' in line


def test_complete_record_has_no_operation_id_field():
    # complete() is unavailable on the file backend (§9.2, §22); the COMPLETE
    # record shape is still part of the contract, so it is checked directly.
    record = make_audit(stage=Stage.COMPLETE)
    line = encode(record)
    assert '"operation_id"' not in line
    assert '"stage":"complete"' in line


def test_operation_id_present_only_for_intent_and_outcome():
    sink = CollectingAuditSink()
    log = _log(sink)
    op = log.intent("user.deleted", actor="alice", target="widget-7")
    op.success()
    intent, outcome = sink.records
    assert intent.operation_id is not None
    assert outcome.operation_id is not None
    assert '"operation_id"' in encode(intent)
    assert '"operation_id"' in encode(outcome)


def test_intent_record_byte_vector(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    log = _log(sink)
    try:
        log.intent("user.deleted", actor="alice", target="widget-7")
    finally:
        sink.close()
    lines = read_lines(path)
    assert lines[0] == (
        '{"schema_version":1,"timestamp":"2026-01-02T03:04:05+00:00",'
        '"application":"exampleapp","emitter":"app","event":"user.deleted",'
        '"category":"ACTIVITY","actor":"alice","stage":"intent",'
        '"operation_id":"op-fixed-0001","target":"widget-7"}'
    )


def test_outcome_record_byte_vector(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    log = _log(sink)
    try:
        op = log.intent("user.deleted", actor="alice", target="widget-7")
        op.success()
    finally:
        sink.close()
    lines = read_lines(path)
    assert lines[1] == (
        '{"schema_version":1,"timestamp":"2026-01-02T03:04:05+00:00",'
        '"application":"exampleapp","emitter":"app","event":"user.deleted",'
        '"category":"ACTIVITY","actor":"alice","stage":"outcome",'
        '"operation_id":"op-fixed-0001","outcome":"success"}'
    )


def test_absent_optional_event_field_is_absent():
    # reason is optional and omitted; it does not appear in the record.
    sink = CollectingAuditSink()
    log = _log(sink)
    log.intent("user.deleted", actor="alice", target="widget-7")
    assert "reason" not in sink.records[0].fields
    assert '"reason"' not in encode(sink.records[0])
