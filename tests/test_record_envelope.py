"""The common envelope and the two record shapes (§16–§18).

Covers the required fields, the fixed schema_version, timezone-aware UTC
timestamps with naive and non-UTC values rejected, the audit operation-id
presence rules, absent-not-empty, and that audit records carry no severity.
"""

import dataclasses
import warnings
from datetime import UTC, datetime, timedelta, timezone

import pytest

from nm_logging import (
    SCHEMA_VERSION,
    Category,
    LoggingConfigurationError,
    Outcome,
    Severity,
    Stage,
)

# Record classes are off the top-level surface (R1); constructed here via the
# implementation module, the sanctioned internal path.
from nm_logging.record import AuditRecord, OperationalRecord

from .helpers import (
    EXAMPLE_OPERATION_ID,
    FROZEN_TIMESTAMP,
    make_audit,
    make_operational,
)


# --- common envelope ------------------------------------------------------


def test_operational_record_has_the_required_envelope():
    record = make_operational(fields={"eligible": 5, "updated": 5})
    assert record.application == "exampleapp"
    assert record.emitter == "app"
    assert record.event == "example.thing.happened"
    assert record.timestamp == FROZEN_TIMESTAMP
    assert record.severity is Severity.INFO
    assert record.fields == {"eligible": 5, "updated": 5}


def test_schema_version_is_stamped_and_not_a_constructor_argument():
    # Invariant 11: schema_version is set by the library, not the call site. It
    # is init=False, so passing it is a TypeError, and every record carries the
    # current contract version.
    assert make_operational().schema_version == SCHEMA_VERSION
    assert make_audit().schema_version == SCHEMA_VERSION
    with pytest.raises(TypeError):
        make_operational(schema_version=2)


def test_records_are_frozen():
    record = make_operational()
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.severity = Severity.ERROR  # type: ignore[misc]


def test_event_fields_are_read_only_and_copied():
    source = {"count": 1}
    record = make_operational(fields=source)
    # Mutating the original dict must not reach into the record.
    source["count"] = 999
    assert record.fields["count"] == 1
    # And the record's own mapping is not mutable in place.
    with pytest.raises(TypeError):
        record.fields["count"] = 2  # type: ignore[index]


def test_fields_default_to_an_empty_mapping():
    assert dict(make_operational().fields) == {}


# --- timestamps (§16.1, invariant 10) -------------------------------------


def test_timezone_aware_utc_timestamp_is_accepted():
    for tzinfo in (UTC, timezone.utc, timezone(timedelta(0))):
        record = make_operational(timestamp=datetime(2026, 6, 1, tzinfo=tzinfo))
        assert record.timestamp.utcoffset() == timedelta(0)


def test_naive_timestamp_is_rejected():
    with pytest.raises(LoggingConfigurationError):
        make_operational(timestamp=datetime(2026, 6, 1, 12, 0, 0))


def test_utcnow_is_rejected_because_it_is_naive():
    # The named hazard: datetime.utcnow() returns a naive value and would pass a
    # careless test. It must be rejected here. The call is deliberate, so its own
    # deprecation warning is suppressed rather than allowed to colour the suite.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        naive_utcnow = datetime.utcnow()
    with pytest.raises(LoggingConfigurationError):
        make_operational(timestamp=naive_utcnow)


def test_non_utc_offset_timestamp_is_rejected():
    plus_two = timezone(timedelta(hours=2))
    with pytest.raises(LoggingConfigurationError):
        make_operational(timestamp=datetime(2026, 6, 1, 12, 0, 0, tzinfo=plus_two))


def test_timestamp_is_mandatory_at_the_record_layer():
    # The record layer requires an explicit timestamp; there is no default and no
    # way to construct a record without one. The stronger guarantee — that the
    # public logging API exposes no timestamp parameter at all (invariant 10) —
    # is enforced at the facade and tested with it (M4/M5).
    with pytest.raises(TypeError):
        OperationalRecord(
            application="exampleapp",
            emitter="app",
            event="example.thing.happened",
            severity=Severity.INFO,
        )


# --- application / emitter / event grammar --------------------------------


@pytest.mark.parametrize(
    "bad_application",
    ["", "Enclave", "1app", "app-name", "app.name", "app name", "x" * 65],
)
def test_bad_application_id_rejected(bad_application):
    with pytest.raises(LoggingConfigurationError):
        make_operational(application=bad_application)


def test_application_rejection_does_not_echo_the_value():
    marker = "SHOULD_NOT_LEAK_2f9a"
    try:
        make_operational(application=marker)
    except LoggingConfigurationError as exc:
        assert marker not in str(exc)
    else:
        raise AssertionError("expected LoggingConfigurationError")


def test_known_emitters_accepted():
    assert make_operational(emitter="app").emitter == "app"
    # wrapper is a reserved but valid contract value (§16.1), even though v0.1
    # produces no wrapper records.
    assert make_operational(emitter="wrapper").emitter == "wrapper"


@pytest.mark.parametrize("bad_emitter", ["", "APP", "application", "system", "sidecar"])
def test_unknown_emitter_rejected(bad_emitter):
    with pytest.raises(LoggingConfigurationError):
        make_operational(emitter=bad_emitter)


def test_empty_event_rejected():
    with pytest.raises(LoggingConfigurationError):
        make_operational(event="")


# --- operational shape ----------------------------------------------------


def test_operational_severity_must_be_a_severity_member():
    with pytest.raises(LoggingConfigurationError):
        make_operational(severity="INFO")


# --- audit shape (§18) ----------------------------------------------------


def test_audit_record_carries_no_severity():
    # §5: audit is independent of operational severity. The shape must have no
    # severity field at all, so it cannot be smuggled in.
    audit = make_audit()
    assert not hasattr(audit, "severity")
    field_names = {f.name for f in dataclasses.fields(AuditRecord)}
    assert "severity" not in field_names


def test_complete_record_has_no_operation_id():
    # Absent, not empty (invariant 9): a complete record pairs with nothing, so
    # operation_id is absent, not a placeholder.
    audit = make_audit(stage=Stage.COMPLETE)
    assert audit.operation_id is None


def test_complete_record_with_operation_id_rejected():
    with pytest.raises(LoggingConfigurationError):
        make_audit(stage=Stage.COMPLETE, operation_id=EXAMPLE_OPERATION_ID)


def test_intent_requires_operation_id():
    with pytest.raises(LoggingConfigurationError):
        make_audit(stage=Stage.INTENT)


def test_outcome_requires_operation_id():
    with pytest.raises(LoggingConfigurationError):
        make_audit(stage=Stage.OUTCOME)


def test_intent_and_outcome_with_operation_id_accepted():
    intent = make_audit(stage=Stage.INTENT, operation_id=EXAMPLE_OPERATION_ID)
    outcome = make_audit(
        stage=Stage.OUTCOME, operation_id=EXAMPLE_OPERATION_ID, outcome=Outcome.SUCCESS
    )
    assert intent.operation_id == EXAMPLE_OPERATION_ID
    assert outcome.operation_id == EXAMPLE_OPERATION_ID


def test_outcome_record_requires_an_outcome():
    with pytest.raises(LoggingConfigurationError):
        make_audit(stage=Stage.OUTCOME, operation_id=EXAMPLE_OPERATION_ID)


def test_intent_record_rejects_an_outcome():
    with pytest.raises(LoggingConfigurationError):
        make_audit(
            stage=Stage.INTENT, operation_id=EXAMPLE_OPERATION_ID, outcome=Outcome.SUCCESS
        )


def test_complete_record_allows_an_optional_outcome():
    # A complete atomic record may state a definitive result (§8).
    assert make_audit(stage=Stage.COMPLETE, outcome=Outcome.FAILURE).outcome is Outcome.FAILURE
    assert make_audit(stage=Stage.COMPLETE).outcome is None


@pytest.mark.parametrize("bad_actor", ["system", "System", "SYSTEM", "none", "null", "-", "  system  "])
def test_non_accountable_actor_values_rejected(bad_actor):
    with pytest.raises(LoggingConfigurationError):
        make_audit(actor=bad_actor)


@pytest.mark.parametrize("bad_actor", ["", "   ", "\t\n"])
def test_empty_or_whitespace_actor_rejected(bad_actor):
    with pytest.raises(LoggingConfigurationError):
        make_audit(actor=bad_actor)


def test_category_and_stage_must_be_enum_members():
    with pytest.raises(LoggingConfigurationError):
        make_audit(category="ACTIVITY")
    with pytest.raises(LoggingConfigurationError):
        make_audit(stage="complete")


def test_audit_record_keeps_its_envelope_and_audit_fields():
    audit = make_audit(
        category=Category.ADMIN,
        stage=Stage.INTENT,
        operation_id=EXAMPLE_OPERATION_ID,
        fields={"target": "widget-7"},
    )
    assert audit.category is Category.ADMIN
    assert audit.stage is Stage.INTENT
    assert audit.actor == "alice"
    assert audit.operation_id == EXAMPLE_OPERATION_ID
    assert audit.fields == {"target": "widget-7"}
    assert audit.schema_version == SCHEMA_VERSION
