"""Secret material and value-echo hygiene (§11, invariant 2).

The field guard refuses a value shaped like an encrypted envelope, and no
rejection echoes the offending value into an exception message or traceback
(the M2 slice). Through the operational path (M4): a secret value reaches no
emitted record, no bounded fallback line, no exception, no traceback, and no
``caplog`` — the last because the package never routes through stdlib logging
(invariant 14), so caplog stays empty.
"""

import io
import traceback

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
from nm_logging.sinks.stderr import StderrFallback

from .helpers import FailingSink, FrozenClock, read_lines
from .synthetic_sensitive_material import (
    SECRET_MARKER,
    SYNTHETIC_BEARER_TOKEN,
    SYNTHETIC_ENC_ENVELOPE,
)

STR_EVENT = EventSchema("m.noted", severity=Severity.INFO, fields=(FieldSpec("text", str),))
INT_EVENT = EventSchema("m.counted", severity=Severity.INFO, fields=(FieldSpec("count", int),))


def _rendered(exc: EventSchemaError) -> str:
    return str(exc) + "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )


def test_encrypted_envelope_value_is_refused():
    # §11 / invariant 2: an ENC[...]-shaped value is rejected, not passed through.
    with pytest.raises(EventSchemaError):
        STR_EVENT.validate_operational(Severity.INFO, {"text": SYNTHETIC_ENC_ENVELOPE})


def test_encrypted_envelope_rejection_does_not_echo_the_value():
    try:
        STR_EVENT.validate_operational(Severity.INFO, {"text": SYNTHETIC_ENC_ENVELOPE})
    except EventSchemaError as exc:
        haystack = _rendered(exc)
        assert SYNTHETIC_ENC_ENVELOPE not in haystack
        # from None: the cause is suppressed so no chained frame can carry it.
        assert exc.__cause__ is None
        assert exc.__suppress_context__ is True
    else:
        raise AssertionError("expected EventSchemaError")


def test_wrong_type_rejection_does_not_echo_a_secret_value():
    # A secret-shaped string handed to an int field is rejected on type; the
    # value must not surface in the message or traceback.
    try:
        INT_EVENT.validate_operational(Severity.INFO, {"count": SYNTHETIC_BEARER_TOKEN})
    except EventSchemaError as exc:
        assert SYNTHETIC_BEARER_TOKEN not in _rendered(exc)
    else:
        raise AssertionError("expected EventSchemaError")


def test_oversize_rejection_does_not_echo_the_value():
    oversize = SECRET_MARKER * 40  # comfortably over MAX_FIELD_CHARS
    assert len(oversize) > 512
    try:
        STR_EVENT.validate_operational(Severity.INFO, {"text": oversize})
    except EventSchemaError as exc:
        haystack = _rendered(exc)
        assert oversize not in haystack
        assert SECRET_MARKER not in haystack
    else:
        raise AssertionError("expected EventSchemaError")


# --- through the operational path (§11 across all surfaces) ----------------


def _registry() -> EventRegistry:
    registry = EventRegistry()
    registry.register(STR_EVENT)
    return registry


def test_encrypted_envelope_never_reaches_the_record_or_caplog(tmp_path, caplog):
    path = tmp_path / "op.jsonl"
    sink = JsonlSink(path)
    log = OperationalLog("exampleapp", _registry(), sink, clock=FrozenClock(), strict=False)
    with caplog.at_level("DEBUG"):
        log.info("m.noted", text=SYNTHETIC_ENC_ENVELOPE)  # refused; contained as defect
    sink.close()
    written = path.read_text(encoding="utf-8")
    # A defect record was written, but never the envelope value.
    assert SYNTHETIC_ENC_ENVELOPE not in written
    assert '"violation":"schema_violation"' in written
    # The package never routes through stdlib logging, so caplog is empty.
    assert caplog.text == ""


def test_field_value_never_appears_in_the_fallback_line(tmp_path, caplog):
    # A legitimate (non-secret-shaped) value that would be written normally, but
    # the sink fails. The bounded fallback line must not carry the value.
    stream = io.StringIO()
    log = OperationalLog(
        "exampleapp",
        _registry(),
        FailingSink(),
        clock=FrozenClock(),
        fallback=StderrFallback(stream),
    )
    with caplog.at_level("DEBUG"):
        log.info("m.noted", text=SYNTHETIC_BEARER_TOKEN)
    fallback_output = stream.getvalue()
    assert fallback_output != ""  # degradation was reported
    assert SYNTHETIC_BEARER_TOKEN not in fallback_output
    assert caplog.text == ""


def test_oversize_secret_never_reaches_the_record(tmp_path):
    path = tmp_path / "op.jsonl"
    sink = JsonlSink(path)
    log = OperationalLog("exampleapp", _registry(), sink, clock=FrozenClock(), strict=False)
    oversize_secret = SECRET_MARKER * 40  # over MAX_FIELD_CHARS
    log.info("m.noted", text=oversize_secret)  # refused at the guard
    sink.close()
    written = path.read_text(encoding="utf-8")
    assert SECRET_MARKER not in written


def test_declared_field_value_is_persisted_unexamined(tmp_path):
    # The residual-responsibility boundary (R3d, §12): a value in a declared
    # string field that is not structurally prohibited is accepted and persisted
    # verbatim. The package rejects the one structural form it can identify
    # (ENC[...]) but does not, and must not appear to, scan arbitrary strings for
    # secrets — that stays the consumer schema's responsibility. This is the
    # actual persistence boundary, distinct from the fallback-hygiene test above.
    path = tmp_path / "op.jsonl"
    sink = JsonlSink(path)
    log = OperationalLog("exampleapp", _registry(), sink, clock=FrozenClock())
    log.info("m.noted", text=SYNTHETIC_BEARER_TOKEN)
    sink.close()
    written = path.read_text(encoding="utf-8")
    assert SYNTHETIC_BEARER_TOKEN in written  # persisted unexamined
