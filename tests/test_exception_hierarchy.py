"""The exception hierarchy, and the one structural rule that keeps the two
failure contracts apart (invariant 5).

Operational logging fails open by catching SinkError and containing it; audit
fails hard by raising AuditPersistenceError. If AuditPersistenceError were a
SinkError, the operational path's generic sink handling could swallow it and
silently turn a fail-hard guarantee into fail-open. It is therefore a direct
subclass of NmLoggingError and deliberately not of SinkError.
"""

import pytest

from nm_logging import (
    AuditPersistenceError,
    AuditUsageError,
    EventSchemaError,
    LoggingConfigurationError,
    NmLoggingError,
    SinkError,
)

ALL_ERRORS = [
    LoggingConfigurationError,
    EventSchemaError,
    SinkError,
    AuditPersistenceError,
    AuditUsageError,
]


@pytest.mark.parametrize("error_cls", ALL_ERRORS)
def test_every_error_descends_from_the_base(error_cls):
    assert issubclass(error_cls, NmLoggingError)


def test_audit_persistence_error_is_not_a_sink_error():
    # The load-bearing invariant: it must never be caught by operational sink
    # handling. If someone reparents it under SinkError, this fails.
    assert not issubclass(AuditPersistenceError, SinkError)
    assert issubclass(AuditPersistenceError, NmLoggingError)


def test_audit_usage_error_is_not_a_sink_error():
    assert not issubclass(AuditUsageError, SinkError)


def test_operational_sink_handling_cannot_swallow_an_audit_failure():
    # A behavioural statement of the invariant: the operational path contains
    # SinkError; an audit-durability failure must escape that same handler.
    with pytest.raises(AuditPersistenceError):
        try:
            raise AuditPersistenceError("durability not achieved")
        except SinkError:  # pragma: no cover - must not catch
            pytest.fail("AuditPersistenceError was caught as a SinkError")


def test_sink_error_is_contained_by_its_own_handler():
    # The complementary case: a SinkError is exactly what the operational path
    # catches, so this handler does catch it.
    caught = False
    try:
        raise SinkError("write failed")
    except SinkError:
        caught = True
    assert caught
