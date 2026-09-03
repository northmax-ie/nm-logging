"""Logging programming errors: strict raises, production contains (§14.4).

In strict mode a malformed logging call raises, so defects are found before
release. In production the call is contained: a package-owned defect record is
written naming the event id and the violation kind, and — the load-bearing part —
never the offending field values. Covers unknown event, schema violation, and an
over-limit record.
"""

import pytest

from nm_logging import (
    EventRegistry,
    EventSchema,
    EventSchemaError,
    FieldSpec,
    JsonlSink,
    OperationalLog,
    Severity,
)
from nm_logging.operational import DEFECT_EVENT

from .helpers import CollectingSink, FrozenClock, read_lines

INT_EVENT = EventSchema("thing.counted", severity=Severity.INFO, fields=(FieldSpec("count", int),))
ERROR_EVENT = EventSchema("thing.failed", severity=Severity.ERROR)
LIST_EVENT = EventSchema(
    "thing.batched", severity=Severity.INFO, fields=(FieldSpec("blobs", str, is_list=True),)
)


def _registry() -> EventRegistry:
    registry = EventRegistry()
    registry.register(INT_EVENT)
    registry.register(ERROR_EVENT)
    registry.register(LIST_EVENT)
    return registry


def _log(sink, *, strict: bool) -> OperationalLog:
    return OperationalLog("exampleapp", _registry(), sink, clock=FrozenClock(), strict=strict)


# --- strict mode raises ---------------------------------------------------


def test_strict_raises_on_unknown_event():
    log = _log(CollectingSink(), strict=True)
    with pytest.raises(EventSchemaError):
        log.info("never.registered")


# --- Finding 3: malformed (unhashable/wrong-type) event id stays fail-open --


@pytest.mark.parametrize("bad_event", [[], {}, ("a", "b"), 123, None])
def test_malformed_event_id_is_contained_fail_open(bad_event):
    # An unhashable or wrong-type event id must not escape as a TypeError and fail
    # the application operation being described (§14.4). It is contained like any
    # unknown event, with no echo of the value.
    sink = CollectingSink()
    log = _log(sink, strict=False)
    assert log.info(bad_event) is None
    assert log.health.degraded is False
    defect = sink.records[0]
    assert defect.event == DEFECT_EVENT
    assert defect.fields["violation"] == "unknown_event"
    assert "offending_event" not in defect.fields  # not a grammar-valid string
    assert str(bad_event) not in str(defect.fields)


def test_strict_surfaces_a_malformed_event_id():
    log = _log(CollectingSink(), strict=True)
    with pytest.raises(EventSchemaError):
        log.info([])


def test_strict_raises_on_undeclared_field():
    log = _log(CollectingSink(), strict=True)
    with pytest.raises(EventSchemaError):
        log.info("thing.counted", count=1, surprise=2)


def test_strict_raises_on_wrong_type():
    log = _log(CollectingSink(), strict=True)
    with pytest.raises(EventSchemaError):
        log.info("thing.counted", count="not an int")


def test_strict_raises_on_severity_mismatch():
    # thing.failed is pinned ERROR; calling info() is a call-site mismatch.
    log = _log(CollectingSink(), strict=True)
    with pytest.raises(EventSchemaError):
        log.info("thing.failed")


# --- production contains and emits a defect record ------------------------


def test_unknown_event_contained_and_recorded():
    sink = CollectingSink()
    log = _log(sink, strict=False)
    assert log.info("never.registered") is None
    assert len(sink.records) == 1
    defect = sink.records[0]
    assert defect.event == DEFECT_EVENT
    assert defect.severity is Severity.ERROR
    assert defect.fields["violation"] == "unknown_event"
    # "never.registered" is grammar-valid, so naming it is safe (R3b).
    assert defect.fields["offending_event"] == "never.registered"


def test_defect_record_omits_a_grammar_invalid_event_name():
    # R3b: arbitrary caller input — including credential-shaped input — must not
    # be echoed into the persisted defect record. A grammar-violating event name
    # is omitted (§18: absent, not a placeholder).
    sink = CollectingSink()
    log = _log(sink, strict=False)
    arbitrary = "Bearer sk-not-a-real-token and has spaces"
    log.info(arbitrary)  # unknown AND grammar-invalid
    defect = sink.records[0]
    assert defect.fields["violation"] == "unknown_event"
    assert "offending_event" not in defect.fields  # not echoed
    assert arbitrary not in str(defect.fields)
    assert arbitrary not in (defect.message or "")


def test_defect_record_names_a_grammar_valid_unknown_event():
    sink = CollectingSink()
    log = _log(sink, strict=False)
    log.info("some.unregistered.event")  # unknown but grammar-valid
    defect = sink.records[0]
    assert defect.fields["violation"] == "unknown_event"
    assert defect.fields["offending_event"] == "some.unregistered.event"


def test_schema_violation_defect_omits_offending_values():
    sink = CollectingSink()
    log = _log(sink, strict=False)
    secret = "SECRET_VALUE_do_not_leak_9z"
    log.info("thing.counted", count=secret)  # wrong type
    defect = sink.records[0]
    assert defect.fields["violation"] == "schema_violation"
    assert defect.fields["offending_event"] == "thing.counted"
    # The offending value appears nowhere in the defect record.
    assert secret not in str(defect.fields)
    assert secret not in (defect.message or "")


def test_oversize_record_contained_and_recorded(tmp_path):
    # A guard-passing call whose encoded record still exceeds the size limit:
    # a list of many max-length strings. The JSONL sink rejects it; the path
    # contains it and writes a defect record instead.
    path = tmp_path / "op.jsonl"
    sink = JsonlSink(path)
    log = OperationalLog("exampleapp", _registry(), sink, clock=FrozenClock(), strict=False)
    big = ["y" * 512 for _ in range(60)]  # ~30 KB, over MAX_RECORD_BYTES
    try:
        assert log.info("thing.batched", blobs=big) is None
    finally:
        sink.close()
    lines = read_lines(path)
    # Only the defect record is present; the oversize record was not written.
    assert len(lines) == 1
    assert '"event":"nmlogging.operational.defect"' in lines[0]
    assert '"violation":"oversize"' in lines[0]
    assert "yyyyy" not in lines[0]


def test_defect_record_does_not_recurse_when_sink_fails():
    # If even the defect record cannot be written, the path degrades via the
    # fallback rather than recursing into defect handling.
    from .helpers import FailingSink

    sink = FailingSink()
    log = _log(sink, strict=False)
    log.info("never.registered")  # unknown event -> defect -> sink fails
    # The sink saw the defect-record write attempt (once), not an infinite loop.
    assert sink.calls == 1
    assert log.health.degraded is True
