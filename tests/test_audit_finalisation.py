"""Unit B — dual-failure exception policy and the operation state machine (§5).

Covers the two chain shapes (§5.2), the hierarchy correction (§5.6), the
EventSchemaError taxonomy regression (§5.1 Rule 2), facade normalization (§5.1),
the state table (§5.4) including the swallow case, the note (§5.5), the evidence
walker locating the body exception through suppressed context, and the custom-sink
traceback-safety guarantee at the AuditSink boundary.
"""

import traceback

import pytest

from nm_logging import (
    AuditFinalisationError,
    AuditLog,
    AuditPersistenceError,
    AuditUsageError,
    Category,
    EventRegistry,
    EventSchema,
    EventSchemaError,
    FieldSpec,
    NmLoggingError,
    Outcome,
    Stage,
    build_evidence,
)

from .helpers import CollectingAuditSink, FrozenClock, fixed_operation_id_factory
from .synthetic_sensitive_material import SECRET_MARKER

DELETE_EVENT = EventSchema(
    "user.deleted",
    category=Category.ACTIVITY,
    fields=(FieldSpec("target", str, required=True),),
)


class BodyError(Exception):
    """A foreign body exception; not a NorthMax type, so unmarked."""


def _registry() -> EventRegistry:
    registry = EventRegistry()
    registry.register(DELETE_EVENT)
    return registry


def _log(sink) -> AuditLog:
    return AuditLog(
        "exampleapp",
        _registry(),
        sink,
        clock=FrozenClock(),
        operation_id_factory=fixed_operation_id_factory(),
    )


class _OutcomePersistenceFailingSink:
    """Conforming custom AuditSink: intent appends succeed, the outcome append
    raises an UNSUPPRESSED AuditPersistenceError carrying secret material.

    This drives the branch-A / custom-sink guarantee at the protocol boundary: the
    guarantee must hold because the facade normalizes and suppresses, not because
    any sink cooperated. Remove the normalization and this sink leaks its
    secret-bearing message into tracebacks.
    """

    def __init__(self) -> None:
        self.records: list = []

    @property
    def supports_atomic(self) -> bool:
        return False

    def append(self, record) -> None:
        if record.stage is Stage.OUTCOME:
            raise AuditPersistenceError(f"custom sink failure exposing {SECRET_MARKER}")
        self.records.append(record)

    def close(self) -> None:
        pass


class _OutcomeSchemaFailingSink:
    """Custom AuditSink whose outcome append raises EventSchemaError — a
    non-persistence finalisation failure (branch B)."""

    def __init__(self) -> None:
        self.records: list = []

    @property
    def supports_atomic(self) -> bool:
        return False

    def append(self, record) -> None:
        if record.stage is Stage.OUTCOME:
            raise EventSchemaError("outcome rejected at encode")
        self.records.append(record)

    def close(self) -> None:
        pass


class _IntentPersistenceFailingSink:
    """Custom AuditSink whose intent append raises a secret-bearing
    AuditPersistenceError — to exercise §5.1 normalization at intent()."""

    @property
    def supports_atomic(self) -> bool:
        return False

    def append(self, record) -> None:
        raise AuditPersistenceError(f"intent sink failure exposing {SECRET_MARKER}")

    def close(self) -> None:
        pass


def _rendered(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _reachable_types(exc: BaseException) -> set[type]:
    seen: set[type] = set()
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or type(current) in seen:
            continue
        seen.add(type(current))
        stack.extend([current.__cause__, current.__context__])
    return seen


# --- hierarchy (§5.6) ------------------------------------------------------


def test_finalisation_error_is_not_a_persistence_error():
    assert not issubclass(AuditFinalisationError, AuditPersistenceError)
    assert issubclass(AuditFinalisationError, NmLoggingError)


# --- branch A: finalisation failure was a persistence failure (§5.2) -------


def test_branch_a_chain_shape():
    sink = _OutcomePersistenceFailingSink()
    log = _log(sink)
    with pytest.raises(AuditFinalisationError) as exc_info:
        with log.operation("user.deleted", actor="alice", target="widget-7"):
            raise BodyError("body failed")
    err = exc_info.value
    # __cause__ is the facade-normalized persistence error (fixed message).
    assert isinstance(err.__cause__, AuditPersistenceError)
    assert err.__cause__.args[0] == "audit persistence failed"
    assert err.__cause__.operation_id == "op-fixed-0001"
    assert err.__suppress_context__ is True
    assert isinstance(err.body_exception, BodyError)
    assert err.finalisation_exception is err.__cause__
    assert err.operation_id == "op-fixed-0001"
    # The normalized cause itself stops the chain rendering.
    assert err.__cause__.__cause__ is None
    assert err.__cause__.__suppress_context__ is True


def test_custom_sink_traceback_safety():
    """The traceback-safety guarantee holds at the AuditSink boundary because the
    facade normalizes and suppresses, NOT because the sink cooperated. If
    normalization is removed, a conforming custom sink starts leaking its
    secret-bearing message into tracebacks, and this test is what catches it."""
    sink = _OutcomePersistenceFailingSink()
    log = _log(sink)
    with pytest.raises(AuditFinalisationError) as exc_info:
        with log.operation("user.deleted", actor="alice", target="widget-7"):
            raise BodyError(f"body exposing {SECRET_MARKER}")
    err = exc_info.value
    rendered = _rendered(err)
    assert SECRET_MARKER not in rendered  # neither body nor sink message exposed
    assert "custom sink failure" not in rendered
    # The body remains programmatically available; operation_id too.
    assert isinstance(err.body_exception, BodyError)
    assert err.operation_id == "op-fixed-0001"


# --- branch B: finalisation failure was anything else (§5.2, §5.1 Rule 2) --


def test_branch_b_chain_shape_and_taxonomy():
    sink = _OutcomeSchemaFailingSink()
    log = _log(sink)
    with pytest.raises(AuditFinalisationError) as exc_info:
        with log.operation("user.deleted", actor="alice", target="widget-7"):
            raise BodyError("body failed")
    err = exc_info.value
    assert err.__cause__ is None  # branch B renders no cause
    assert err.__suppress_context__ is True
    # The finalisation exception keeps its own type, never relabelled.
    assert isinstance(err.finalisation_exception, EventSchemaError)
    assert isinstance(err.body_exception, BodyError)
    # No AuditPersistenceError is constructed anywhere in the chain.
    assert AuditPersistenceError not in _reachable_types(err)


# --- facade normalization at intent() (§5.1 scope) -------------------------


def test_intent_persistence_failure_is_normalized():
    sink = _IntentPersistenceFailingSink()
    log = _log(sink)
    with pytest.raises(AuditPersistenceError) as exc_info:
        log.intent("user.deleted", actor="alice", target="widget-7")
    err = exc_info.value
    assert err.args[0] == "audit persistence failed"  # fixed message
    assert err.operation_id == "op-fixed-0001"
    assert SECRET_MARKER not in _rendered(err)  # original message suppressed


# --- state table (§5.4) ----------------------------------------------------


def test_successful_auto_indeterminate_reraises_body_unchanged():
    sink = CollectingAuditSink()
    log = _log(sink)
    with pytest.raises(BodyError):
        with log.operation("user.deleted", actor="alice", target="widget-7"):
            raise BodyError("boom")
    assert [r.stage for r in sink.records] == [Stage.INTENT, Stage.OUTCOME]
    assert sink.records[1].outcome is Outcome.INDETERMINATE


def test_finalisation_failure_cannot_be_swallowed_inside_the_block():
    sink = _OutcomePersistenceFailingSink()
    log = _log(sink)
    with pytest.raises(AuditPersistenceError):
        with log.operation("user.deleted", actor="alice", target="widget-7") as op:
            try:
                op.success()  # outcome append fails -> stored finalisation exception
            except AuditPersistenceError:
                pass  # the caller tries to swallow it
    # __exit__ with no body exception re-raises the stored finalisation exception.


def test_explicit_outcome_failure_propagates_with_operation_id_and_no_second_write():
    sink = CollectingAuditSink(fail_on={2})  # intent ok, outcome append fails
    log = _log(sink)
    op = log.intent("user.deleted", actor="alice", target="widget-7")
    with pytest.raises(AuditPersistenceError) as exc_info:
        op.success()
    assert exc_info.value.operation_id == "op-fixed-0001"
    assert len(sink.records) == 1  # the intent only; no second write attempted


def test_finalised_then_body_exception_keeps_the_recorded_outcome():
    sink = CollectingAuditSink()
    log = _log(sink)
    with pytest.raises(BodyError):
        with log.operation("user.deleted", actor="alice", target="widget-7") as op:
            op.success()
            raise BodyError("after success")
    assert [r.stage for r in sink.records] == [Stage.INTENT, Stage.OUTCOME]
    assert sink.records[1].outcome is Outcome.SUCCESS  # single-use, unchanged


# --- the evidence walker (§5.9) --------------------------------------------


def test_evidence_walker_locates_body_and_omits_its_message():
    # A non-secret sink failure keeps the evidence chain's marked messages safe,
    # so any leak would be the body exception's own (foreign) message.
    sink = CollectingAuditSink(fail_on={2})
    log = _log(sink)
    with pytest.raises(AuditFinalisationError) as exc_info:
        with log.operation("user.deleted", actor="alice", target="widget-7"):
            raise BodyError(f"body exposing {SECRET_MARKER}")
    err = exc_info.value
    evidence = build_evidence(err)
    types = [entry.exception_type for entry in evidence.chain]
    assert "BodyError" in types  # located through the suppressed context
    body_entry = next(e for e in evidence.chain if e.exception_type == "BodyError")
    assert body_entry.message is None  # foreign message omitted
    haystack = "\n".join(
        (entry.message or "") for entry in evidence.chain
    )
    assert SECRET_MARKER not in haystack


# --- the note (§5.5) -------------------------------------------------------


def test_note_contains_only_the_two_type_qualnames():
    sink = _OutcomeSchemaFailingSink()
    log = _log(sink)
    with pytest.raises(AuditFinalisationError) as exc_info:
        with log.operation("user.deleted", actor="alice", target="widget-7"):
            raise BodyError(f"body exposing {SECRET_MARKER}")
    err = exc_info.value
    notes = getattr(err, "__notes__", [])
    assert len(notes) == 1
    note = notes[0]
    assert "BodyError" in note
    assert "EventSchemaError" in note
    assert SECRET_MARKER not in note  # type names only, no message


# --- Finding 1: every failed finalisation is terminal, whatever the type ----


class _OutcomeRuntimeFailingSink:
    """Outcome append raises an UNEXPECTED RuntimeError — neither a persistence
    nor a schema failure. It must still make the operation terminal."""

    def __init__(self) -> None:
        self.records: list = []
        self.outcome_attempts = 0

    @property
    def supports_atomic(self) -> bool:
        return False

    def append(self, record) -> None:
        if record.stage is Stage.OUTCOME:
            self.outcome_attempts += 1
            raise RuntimeError("unexpected sink failure")
        self.records.append(record)

    def close(self) -> None:
        pass


class _OutcomeWritesThenRaisesSink:
    """Outcome append writes the record and *then* raises — the effect may already
    exist, so a second attempt would risk conflicting outcome records."""

    def __init__(self) -> None:
        self.records: list = []
        self.outcome_attempts = 0

    @property
    def supports_atomic(self) -> bool:
        return False

    def append(self, record) -> None:
        if record.stage is Stage.OUTCOME:
            self.outcome_attempts += 1
            self.records.append(record)  # already written
            raise RuntimeError("sink raised after writing")
        self.records.append(record)

    def close(self) -> None:
        pass


def test_unexpected_error_in_explicit_finalisation_is_terminal_and_keeps_type():
    sink = _OutcomeRuntimeFailingSink()
    log = _log(sink)
    op = log.intent("user.deleted", actor="alice", target="widget-7")
    with pytest.raises(RuntimeError):  # true type kept, never relabelled
        op.success()
    assert op.finalised is False
    with pytest.raises(AuditUsageError):  # no second outcome from a non-OPEN state
        op.success()
    assert sink.outcome_attempts == 1  # exactly one outcome attempt


def test_unexpected_error_in_automatic_finalisation_is_dual_failure():
    sink = _OutcomeRuntimeFailingSink()
    log = _log(sink)
    with pytest.raises(AuditFinalisationError) as exc_info:
        with log.operation("user.deleted", actor="alice", target="widget-7"):
            raise BodyError("body failed")
    err = exc_info.value
    assert isinstance(err.finalisation_exception, RuntimeError)  # kept, not relabelled
    assert isinstance(err.body_exception, BodyError)
    assert AuditPersistenceError not in _reachable_types(err)  # never fabricated
    assert sink.outcome_attempts == 1


def test_failure_after_the_sink_may_have_written_the_outcome_is_terminal():
    sink = _OutcomeWritesThenRaisesSink()
    log = _log(sink)
    op = log.intent("user.deleted", actor="alice", target="widget-7")
    with pytest.raises(RuntimeError):
        op.success()
    assert sink.outcome_attempts == 1
    with pytest.raises(AuditUsageError):
        op.indeterminate()  # no second attempt, even though an outcome may exist
    assert sink.outcome_attempts == 1


def test_caught_unexpected_finalisation_failure_then_clean_exit_is_not_swallowed():
    sink = _OutcomeRuntimeFailingSink()
    log = _log(sink)
    with pytest.raises(RuntimeError):  # __exit__ re-raises the stored failure
        with log.operation("user.deleted", actor="alice", target="widget-7") as op:
            try:
                op.success()
            except RuntimeError:
                pass  # the caller tries to swallow it
    assert sink.outcome_attempts == 1


def test_different_body_exception_after_unexpected_finalisation_failure():
    sink = _OutcomeRuntimeFailingSink()
    log = _log(sink)

    class Other(Exception):
        pass

    with pytest.raises(AuditFinalisationError) as exc_info:
        with log.operation("user.deleted", actor="alice", target="widget-7") as op:
            try:
                op.success()  # RuntimeError -> FINALISATION_FAILED
            except RuntimeError:
                pass
            raise Other("a later, different body exception")
    err = exc_info.value
    assert isinstance(err.finalisation_exception, RuntimeError)
    assert isinstance(err.body_exception, Other)
    assert sink.outcome_attempts == 1  # still only one outcome attempt overall


# --- Finding 2: a custom sink's secret message is absent from build_evidence -


def test_custom_sink_secret_message_absent_from_build_evidence():
    sink = _OutcomePersistenceFailingSink()  # raises AuditPersistenceError carrying SECRET_MARKER
    log = _log(sink)
    with pytest.raises(AuditFinalisationError) as exc_info:
        with log.operation("user.deleted", actor="alice", target="widget-7"):
            raise BodyError("body failed")
    err = exc_info.value
    rendered = _rendered(err)
    evidence = build_evidence(err)
    evidence_text = "\n".join((entry.message or "") for entry in evidence.chain)
    assert SECRET_MARKER not in rendered  # traceback safe
    assert SECRET_MARKER not in evidence_text  # AND build_evidence safe (Finding 2)
    # The original persistence failure is still reachable as context, just not safe.
    assert AuditPersistenceError in _reachable_types(err)


# --- Findings 1 & 2 re-review: terminal state + off-chain untrusted original -


class _RefusingPersistenceError(AuditPersistenceError):
    """A sink exception that refuses attribute assignment."""

    def __setattr__(self, name, value):
        raise AttributeError("read-only exception")


class _SetattrRaisesExceptionError(AuditPersistenceError):
    """A sink exception whose __setattr__ raises an ordinary Exception."""

    def __setattr__(self, name, value):
        raise RuntimeError("attribute assignment blocked")


class _SetattrRaisesBaseError(AuditPersistenceError):
    """A sink exception whose __setattr__ raises a BaseException."""

    def __setattr__(self, name, value):
        raise KeyboardInterrupt("attribute assignment interrupt")


# The normal case plus three hostile subclasses. The old fix mutated the original
# (original.log_safe_message = False); on these it would raise before the terminal
# state was set, leaving the operation OPEN. The correct fix never touches them.
_SINK_ERROR_CLASSES = [
    AuditPersistenceError,
    _RefusingPersistenceError,
    _SetattrRaisesExceptionError,
    _SetattrRaisesBaseError,
]


def _secret_outcome_sink(exc_cls):
    class _Sink:
        def __init__(self):
            self.records = []
            self.outcome_attempts = 0

        @property
        def supports_atomic(self):
            return False

        def append(self, record):
            if record.stage is Stage.OUTCOME:
                self.outcome_attempts += 1
                raise exc_cls(f"custom sink failure exposing {SECRET_MARKER}")
            self.records.append(record)

        def close(self):
            pass

    return _Sink()


def _chain_objects(exc):
    seen, ids, stack = [], set(), [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in ids:
            continue
        ids.add(id(current))
        seen.append(current)
        stack.extend([current.__cause__, current.__context__])
    return seen


@pytest.mark.parametrize("exc_cls", _SINK_ERROR_CLASSES)
def test_dual_persistence_failure_is_terminal_and_never_leaks(exc_cls):
    sink = _secret_outcome_sink(exc_cls)
    log = _log(sink)
    with pytest.raises(AuditFinalisationError) as exc_info:
        with log.operation("user.deleted", actor="alice", target="widget-7"):
            raise BodyError("body failed")
    err = exc_info.value
    # exactly one outcome attempt; the operation is terminal (a further explicit
    # attempt would raise AuditUsageError — proven in the explicit test below).
    assert sink.outcome_attempts == 1
    # secret confined to neither the formatted traceback nor build_evidence.
    assert SECRET_MARKER not in _rendered(err)
    ev_text = "\n".join((entry.message or "") for entry in build_evidence(err).chain)
    assert SECRET_MARKER not in ev_text
    # outward classification correct: the normalized, package-controlled error.
    normalized = err.finalisation_exception
    assert isinstance(normalized, AuditPersistenceError)
    assert normalized.args[0] == "audit persistence failed"
    assert normalized.operation_id == "op-fixed-0001"
    # the original sink exception is programmatically reachable, and OFF the chain.
    original = normalized._sink_failure
    assert isinstance(original, exc_cls)
    chain_ids = {id(o) for o in _chain_objects(err)}
    assert id(original) not in chain_ids  # not on any __cause__/__context__ edge
    # combined body + finalisation: truthful AuditFinalisationError attributes.
    assert isinstance(err.body_exception, BodyError)
    assert err.operation_id == "op-fixed-0001"


@pytest.mark.parametrize("exc_cls", _SINK_ERROR_CLASSES)
def test_explicit_persistence_failure_is_terminal_one_attempt(exc_cls):
    sink = _secret_outcome_sink(exc_cls)
    log = _log(sink)
    op = log.intent("user.deleted", actor="alice", target="widget-7")
    with pytest.raises(AuditPersistenceError) as exc_info:
        op.success()
    assert op.finalised is False  # terminal FINALISATION_FAILED, not FINALISED
    assert sink.outcome_attempts == 1
    with pytest.raises(AuditUsageError):
        op.success()  # no second attempt from a non-OPEN state
    assert sink.outcome_attempts == 1
    err = exc_info.value
    assert err.args[0] == "audit persistence failed"
    assert SECRET_MARKER not in _rendered(err)
    assert SECRET_MARKER not in "\n".join((e.message or "") for e in build_evidence(err).chain)
    # original reachable off the chain, on the normalized error itself.
    assert isinstance(err._sink_failure, exc_cls)
    assert id(err._sink_failure) not in {id(o) for o in _chain_objects(err)}
