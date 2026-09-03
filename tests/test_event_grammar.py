"""Event-ID and field-name grammars, and registry policy (§19–§21).

Covers fullmatch behaviour including the trailing-newline case, the two-segment
minimum, reserved envelope field-name collisions, the reserved nmlogging.*
namespace, and duplicate/unknown-event handling.
"""

import pytest

from nm_logging import (
    Category,
    EventRegistry,
    EventSchema,
    EventSchemaError,
    FieldSpec,
    LoggingConfigurationError,
    Severity,
)
from nm_logging.events import RESERVED_FIELD_NAMES


def _op(event_id: str) -> EventSchema:
    return EventSchema(event_id, severity=Severity.INFO)


# --- event id grammar -----------------------------------------------------


@pytest.mark.parametrize(
    "event_id",
    [
        "user.created",
        "update.run.completed",
        "cucm.connectivity.restored",  # a brief-listed grammar example
        "a.b",
        "ab.cd_ef",
        "one.two.three.four",
        "run_completed.ok",
    ],
)
def test_valid_event_ids_accepted(event_id):
    assert _op(event_id).event_id == event_id


@pytest.mark.parametrize(
    "event_id",
    [
        "",
        "user",  # one segment; at least two required
        "User.Created",  # upper-case
        "1user.created",  # leading digit
        "user..created",  # empty segment
        "user.created_",  # trailing underscore in a segment
        "_user.created",  # leading underscore
        "user.cre__ated",  # doubled underscore in a segment
        "user.created.",  # trailing dot
        ".user.created",  # leading dot
        "user.crea ted",  # space
        "user.created\n",  # trailing newline: the fullmatch case
    ],
)
def test_invalid_event_ids_rejected(event_id):
    with pytest.raises(LoggingConfigurationError):
        _op(event_id)


def test_event_id_over_maximum_length_rejected():
    # 129 characters across valid segments.
    long_id = ".".join(["seg"] * 33)  # 33*3 + 32 dots = 131 chars
    assert len(long_id) > 128
    with pytest.raises(LoggingConfigurationError):
        _op(long_id)


def test_event_id_grammar_failure_does_not_echo_the_value():
    marker = "BadEventMARKER_do_not_leak"
    try:
        _op(marker)
    except LoggingConfigurationError as exc:
        assert marker not in str(exc)
    else:
        raise AssertionError("expected LoggingConfigurationError")


# --- field name grammar ---------------------------------------------------


@pytest.mark.parametrize("name", ["a", "eligible", "run_completed", "x9", "a_b_c", "trailing_"])
def test_valid_field_names_accepted(name):
    assert FieldSpec(name, int).name == name


@pytest.mark.parametrize(
    "name",
    ["", "Eligible", "1eligible", "with space", "with-dash", "a.b", "x" * 65],
)
def test_invalid_field_names_rejected(name):
    with pytest.raises(LoggingConfigurationError):
        FieldSpec(name, int)


@pytest.mark.parametrize("reserved", sorted(RESERVED_FIELD_NAMES))
def test_reserved_field_names_rejected(reserved):
    # A consumer must not declare a field that collides with a reserved
    # envelope, audit, or prose name (§16–§18, §20).
    with pytest.raises(LoggingConfigurationError):
        FieldSpec(reserved, str)


def test_duplicate_field_name_in_schema_rejected():
    with pytest.raises(LoggingConfigurationError):
        EventSchema(
            "some.event",
            severity=Severity.INFO,
            fields=(FieldSpec("count", int), FieldSpec("count", str)),
        )


# --- registry: duplicates, reserved namespace, unknown --------------------


def test_registry_rejects_duplicate_registration():
    registry = EventRegistry()
    registry.register(_op("some.event"))
    with pytest.raises(LoggingConfigurationError):
        registry.register(_op("some.event"))


def test_registry_refuses_reserved_namespace_for_consumers():
    registry = EventRegistry()
    with pytest.raises(LoggingConfigurationError):
        registry.register(_op("nmlogging.sink.failed"))


def test_registry_allows_reserved_namespace_for_the_package():
    registry = EventRegistry()
    registry.register_reserved(_op("nmlogging.sink.failed"))
    assert "nmlogging.sink.failed" in registry


def test_register_reserved_refuses_non_reserved_namespace():
    registry = EventRegistry()
    with pytest.raises(LoggingConfigurationError):
        registry.register_reserved(_op("some.event"))


def test_reserved_and_consumer_registrations_share_one_namespace():
    # A consumer event and a package event coexist, and each excludes a later
    # duplicate of itself.
    registry = EventRegistry()
    registry.register(_op("some.event"))
    registry.register_reserved(_op("nmlogging.sink.failed"))
    with pytest.raises(LoggingConfigurationError):
        registry.register_reserved(_op("nmlogging.sink.failed"))


def test_unknown_event_lookup_raises_event_schema_error():
    # Emitting an unregistered event is a call-site violation, not a lookup miss.
    registry = EventRegistry()
    with pytest.raises(EventSchemaError):
        registry.get("never.registered")


def test_get_returns_the_registered_schema():
    registry = EventRegistry()
    schema = EventSchema("user.created", category=Category.ACTIVITY)
    registry.register(schema)
    assert registry.get("user.created") is schema
