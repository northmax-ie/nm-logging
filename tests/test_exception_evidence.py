"""Safe exception evidence (§13.1, invariant 4).

Covers: no frame locals or argument values; a foreign message excluded even when
wrapped in a controlled exception; the same rule through __cause__ and
__context__; the depth cap; cycle tolerance; and the structural safe-message
marker. Uses the synthetic markers so a leak would be visible.
"""

import pytest

from nm_logging import (
    CodeLocation,
    Evidence,
    ExceptionEvidence,
    NmLoggingError,
    build_evidence,
)
from nm_logging.evidence import (
    MAX_CHAIN_DEPTH,
    MAX_TRACEBACK_FRAMES,
    SAFE_MESSAGE_ATTR,
)

from .synthetic_sensitive_material import (
    SECRET_MARKER,
    SYNTHETIC_BEARER_TOKEN,
)


class ControlledError(NmLoggingError):
    """A NorthMax-controlled exception; inherits the safe-message marker."""


def _haystack(evidence: Evidence) -> str:
    """Every string the evidence exposes, concatenated, for leak assertions."""
    parts: list[str] = []
    for entry in evidence.chain:
        parts.append(entry.exception_type)
        parts.append(entry.message or "")
        parts.append(entry.relation or "")
        for loc in entry.locations:
            parts.append(f"{loc.file}:{loc.line}:{loc.function}")
    return "\n".join(parts)


def _raise_foreign_with_secret_local():
    # A foreign exception, raised in a frame that holds a secret local. The
    # secret is never in the (foreign, excluded) message; it can only leak via
    # frame locals, which evidence must not read.
    secret_local = SECRET_MARKER  # noqa: F841  - intentionally unused
    raise ValueError("a foreign message")


# --- the marker -----------------------------------------------------------


def test_northmax_exceptions_carry_the_marker():
    assert getattr(NmLoggingError, SAFE_MESSAGE_ATTR, False) is True
    assert getattr(ControlledError("x"), SAFE_MESSAGE_ATTR, False) is True


def test_plain_exceptions_do_not_carry_the_marker():
    assert getattr(ValueError("x"), SAFE_MESSAGE_ATTR, False) is False


def test_controlled_message_is_included():
    try:
        raise ControlledError("a safe controlled message")
    except ControlledError as exc:
        evidence = build_evidence(exc)
    assert evidence.chain[0].message == "a safe controlled message"
    assert evidence.chain[0].exception_type == "ControlledError"


def test_foreign_message_is_excluded():
    # The foreign message carries a secret; it must not appear, and the message
    # field is None, not the string.
    try:
        raise ValueError(SYNTHETIC_BEARER_TOKEN)
    except ValueError as exc:
        evidence = build_evidence(exc)
    assert evidence.chain[0].message is None
    assert evidence.chain[0].exception_type == "ValueError"
    assert SYNTHETIC_BEARER_TOKEN not in _haystack(evidence)


def test_a_truthy_non_true_marker_does_not_count():
    exc = ValueError("x")
    exc.log_safe_message = "yes"  # truthy but not True
    assert build_evidence(exc).chain[0].message is None


# --- no locals, no argument values ----------------------------------------


def test_frame_locals_are_not_read():
    try:
        _raise_foreign_with_secret_local()
    except ValueError as exc:
        evidence = build_evidence(exc)
    assert SECRET_MARKER not in _haystack(evidence)
    # The location of the raising frame is still recorded.
    assert any(loc.function == "_raise_foreign_with_secret_local" for loc in evidence.chain[0].locations)


def test_locations_have_no_source_text_attribute():
    try:
        raise ValueError("x")
    except ValueError as exc:
        evidence = build_evidence(exc)
    loc = evidence.chain[0].locations[0]
    # A code location is exactly file/line/function — no source text field.
    assert isinstance(loc, CodeLocation)
    assert set(loc.__slots__) == {"file", "line", "function"}


# --- chain traversal (§13.1) ----------------------------------------------


def test_foreign_message_excluded_when_wrapped_via_cause():
    try:
        try:
            raise ValueError(SYNTHETIC_BEARER_TOKEN)
        except ValueError as inner:
            raise ControlledError("safe wrapper") from inner
    except ControlledError as exc:
        evidence = build_evidence(exc)
    types = [e.exception_type for e in evidence.chain]
    assert "ControlledError" in types and "ValueError" in types
    # The controlled wrapper's message is kept; the foreign cause's is not.
    controlled = next(e for e in evidence.chain if e.exception_type == "ControlledError")
    foreign = next(e for e in evidence.chain if e.exception_type == "ValueError")
    assert controlled.message == "safe wrapper"
    assert foreign.message is None
    assert foreign.relation == "cause"
    assert SYNTHETIC_BEARER_TOKEN not in _haystack(evidence)


def test_foreign_message_excluded_through_implicit_context():
    # No `from`: the ValueError becomes the ControlledError's __context__.
    try:
        try:
            raise ValueError(SECRET_MARKER)
        except ValueError:
            raise ControlledError("safe wrapper")
    except ControlledError as exc:
        evidence = build_evidence(exc)
    foreign = next(e for e in evidence.chain if e.exception_type == "ValueError")
    assert foreign.message is None
    assert foreign.relation == "context"
    assert SECRET_MARKER not in _haystack(evidence)


def test_depth_cap_truncates_a_long_chain():
    # Build a chain deeper than the cap by explicit chaining.
    exc: BaseException = ValueError("root")
    for i in range(MAX_CHAIN_DEPTH + 5):
        parent = ValueError(f"level {i}")
        parent.__cause__ = exc
        exc = parent
    evidence = build_evidence(exc)
    assert evidence.truncated is True
    assert len(evidence.chain) == MAX_CHAIN_DEPTH


def test_cycle_is_tolerated():
    a = ValueError("a")
    b = ValueError("b")
    a.__cause__ = b
    b.__cause__ = a  # a cycle
    evidence = build_evidence(a)  # must terminate
    # Each exception recorded once.
    assert len(evidence.chain) == 2


def test_traceback_frames_are_capped():
    def recurse(n):
        if n == 0:
            raise ValueError("deep")
        recurse(n - 1)

    try:
        recurse(MAX_TRACEBACK_FRAMES + 30)
    except ValueError as exc:
        evidence = build_evidence(exc)
    assert len(evidence.chain[0].locations) <= MAX_TRACEBACK_FRAMES


def test_deep_traceback_retains_the_innermost_raising_frame():
    # Finding 6: the cap keeps the INNERMOST frames (nearest the failure), as the
    # module documents — not the outermost. A deep chain must still record the
    # actual raising location.
    def recurse(n):
        if n == 0:
            raise ValueError("at the bottom")
        recurse(n - 1)

    try:
        recurse(MAX_TRACEBACK_FRAMES + 10)
    except ValueError as exc:
        # The true innermost frame — walk the traceback to its end.
        tb = exc.__traceback__
        while tb.tb_next is not None:
            tb = tb.tb_next
        innermost_line = tb.tb_lineno
        evidence = build_evidence(exc)
    locations = evidence.chain[0].locations
    assert len(locations) == MAX_TRACEBACK_FRAMES  # bounded
    assert locations[-1].function == "recurse"
    assert locations[-1].line == innermost_line  # the raising frame is retained


# --- R3c: the unconstrained surfaces are gone -----------------------------


def test_build_evidence_takes_no_description_or_context():
    # The free-form description and caller context are removed (R3c): safe
    # event-specific context travels through declared event fields, not here.
    exc = ValueError("x")
    with pytest.raises(TypeError):
        build_evidence(exc, description="d")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        build_evidence(exc, context={"k": 1})  # type: ignore[call-arg]


def test_exception_evidence_has_no_description_or_context_fields():
    import dataclasses

    names = {f.name for f in dataclasses.fields(ExceptionEvidence)}
    assert names == {"exception_type", "message", "locations", "relation"}
