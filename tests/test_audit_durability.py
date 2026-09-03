"""Audit durability and the intent/outcome contract (§9, §23, invariants 5–7).

Covers: intent durable before return; a durability failure prevents the mutation
(the API raises and returns no operation); an orphan intent remains visible;
double finalisation is rejected; an outcome-append failure does not retract the
mutation; the operation id is internal and stable; the context manager defaults
to indeterminate on an escaping exception; and complete() is refused on the
non-atomic file backend.
"""

import pytest

from nm_logging import (
    AuditLog,
    AuditOperation,
    AuditPersistenceError,
    AuditUsageError,
    Category,
    EventRegistry,
    EventSchema,
    EventSchemaError,
    FieldSpec,
    JsonlAuditSink,
    LoggingConfigurationError,
    Outcome,
    Stage,
)

from .helpers import (
    CollectingAuditSink,
    FrozenClock,
    fixed_operation_id_factory,
    read_lines,
)

DELETE_EVENT = EventSchema(
    "user.deleted",
    category=Category.ACTIVITY,
    fields=(FieldSpec("target", str, required=True), FieldSpec("reason", str)),
)

# A field required only on the outcome, to exercise stage-aware required-ness.
JOB_EVENT = EventSchema(
    "job.run",
    category=Category.ACTIVITY,
    fields=(
        FieldSpec("job_id", str, required=True),
        FieldSpec("code", int, required_on_outcome=True),
    ),
)

# A list field so a guard-passing call can still exceed the record size limit.
BATCH_EVENT = EventSchema(
    "batch.audited",
    category=Category.ACTIVITY,
    fields=(FieldSpec("items", str, is_list=True, required=True),),
)


def _registry() -> EventRegistry:
    registry = EventRegistry()
    registry.register(DELETE_EVENT)
    registry.register(JOB_EVENT)
    registry.register(BATCH_EVENT)
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


# --- intent durability ----------------------------------------------------


def test_intent_is_written_before_intent_returns(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    log = _log(sink)
    op = log.intent("user.deleted", actor="alice", target="widget-7")
    try:
        # By the time intent() has returned, the intent record is durably on disk.
        lines = read_lines(path)
        assert len(lines) == 1
        assert '"stage":"intent"' in lines[0]
        assert isinstance(op, AuditOperation)
    finally:
        sink.close()


def test_durability_failure_prevents_the_mutation():
    # The first append (the intent) fails: intent() raises and returns nothing,
    # so a caller following the pattern never performs the mutation.
    sink = CollectingAuditSink(fail_on={1})
    log = _log(sink)
    with pytest.raises(AuditPersistenceError):
        log.intent("user.deleted", actor="alice", target="widget-7")
    assert sink.records == []


def test_orphan_intent_is_visible():
    # An intent with no outcome is a valid, incomplete operation for reconciliation.
    sink = CollectingAuditSink()
    log = _log(sink)
    log.intent("user.deleted", actor="alice", target="widget-7")
    assert len(sink.records) == 1
    intent = sink.records[0]
    assert intent.stage is Stage.INTENT
    assert intent.outcome is None


# --- finalisation ---------------------------------------------------------


def test_success_appends_an_outcome_record():
    sink = CollectingAuditSink()
    log = _log(sink)
    op = log.intent("user.deleted", actor="alice", target="widget-7")
    op.success()
    assert len(sink.records) == 2
    outcome = sink.records[1]
    assert outcome.stage is Stage.OUTCOME
    assert outcome.outcome is Outcome.SUCCESS
    assert outcome.operation_id == op.operation_id


def test_outcome_need_not_repeat_intent_required_fields():
    # target is required on the intent; the outcome, linked by operation id, need
    # not repeat it (stage-aware required-ness).
    sink = CollectingAuditSink()
    log = _log(sink)
    op = log.intent("user.deleted", actor="alice", target="widget-7")
    op.success()  # no target supplied
    assert sink.records[1].stage is Stage.OUTCOME


def test_required_on_outcome_field_enforced_on_outcome_not_intent():
    sink = CollectingAuditSink()
    log = _log(sink)
    # code is required_on_outcome, not required on intent: the intent omits it,
    # and the outcome that supplies it succeeds.
    op = log.intent("job.run", actor="alice", job_id="j1")
    assert sink.records[0].stage is Stage.INTENT
    assert "code" not in sink.records[0].fields
    op.success(code=0)
    assert sink.records[-1].fields["code"] == 0


def test_a_failed_explicit_finalisation_is_terminal():
    # Unit B (§5.4): an explicit outcome that fails validation sets
    # FINALISATION_FAILED; no second outcome is attempted from any non-OPEN state.
    sink = CollectingAuditSink()
    log = _log(sink)
    op = log.intent("job.run", actor="alice", job_id="j1")
    with pytest.raises(EventSchemaError):
        op.success()  # missing required_on_outcome 'code'
    assert op.finalised is False  # the outcome was not recorded
    with pytest.raises(AuditUsageError):
        op.success(code=0)  # no retry: the operation already failed finalisation


def test_intent_still_requires_its_own_required_fields():
    # Stage-awareness must not make a genuinely intent-required field optional.
    sink = CollectingAuditSink()
    log = _log(sink)
    with pytest.raises(EventSchemaError):
        log.intent("job.run", actor="alice")  # missing required job_id


def test_double_finalisation_is_rejected():
    sink = CollectingAuditSink()
    log = _log(sink)
    op = log.intent("user.deleted", actor="alice", target="widget-7")
    op.success()
    with pytest.raises(AuditUsageError):
        op.success()
    with pytest.raises(AuditUsageError):
        op.failure()
    # Only the intent and the single outcome were written.
    assert len(sink.records) == 2


def test_outcome_append_failure_does_not_retract():
    # The second append (the outcome) fails: the outcome call raises, the intent
    # remains (orphaned), and nothing is retracted. The op is not finalised.
    sink = CollectingAuditSink(fail_on={2})
    log = _log(sink)
    op = log.intent("user.deleted", actor="alice", target="widget-7")
    with pytest.raises(AuditPersistenceError):
        op.success()
    assert op.finalised is False
    assert len(sink.records) == 1  # the intent is still there, unretracted
    assert sink.records[0].stage is Stage.INTENT
    # The operation is now terminally FINALISATION_FAILED: a failed finalisation is
    # terminal, so no second outcome is attempted through this handle (a further
    # call raises AuditUsageError). Reconciliation of the orphaned intent is an
    # out-of-band concern, not a retry on the same handle.
    with pytest.raises(AuditUsageError):
        op.success()


# --- operation id ---------------------------------------------------------


def test_operation_id_links_intent_and_outcome():
    sink = CollectingAuditSink()
    log = _log(sink)
    op = log.intent("user.deleted", actor="alice", target="widget-7")
    op.success()
    assert sink.records[0].operation_id == op.operation_id
    assert sink.records[1].operation_id == op.operation_id


def test_operation_id_cannot_be_supplied_by_the_caller():
    # operation_id is a reserved field name, so passing it is rejected as an
    # undeclared field rather than accepted as a caller-chosen id.
    sink = CollectingAuditSink()
    log = _log(sink)
    with pytest.raises(EventSchemaError):
        log.intent("user.deleted", actor="alice", target="x", operation_id="chosen")


def test_operation_ids_are_unique_by_default(tmp_path):
    # With the real internal factory (no fixed override), two operations differ.
    sink = CollectingAuditSink()
    log = AuditLog("exampleapp", _registry(), sink, clock=FrozenClock())
    a = log.intent("user.deleted", actor="alice", target="a")
    b = log.intent("user.deleted", actor="alice", target="b")
    assert a.operation_id != b.operation_id


# --- unknown event / actor (fail hard) ------------------------------------


def test_unknown_audit_event_raises():
    sink = CollectingAuditSink()
    log = _log(sink)
    with pytest.raises(EventSchemaError):
        log.intent("never.registered", actor="alice")
    assert sink.records == []


@pytest.mark.parametrize("bad_actor", ["", "   ", "system", "System", "none", "null", "-"])
def test_non_accountable_actor_rejected(bad_actor):
    sink = CollectingAuditSink()
    log = _log(sink)
    with pytest.raises(LoggingConfigurationError):
        log.intent("user.deleted", actor=bad_actor, target="x")
    assert sink.records == []


def test_actor_shaped_as_encrypted_envelope_rejected_with_nothing_persisted(tmp_path):
    # R3a: reserved identity metadata rejects the structurally prohibited envelope
    # form, through the normal audit facade, with nothing written.
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    log = AuditLog(
        "exampleapp",
        _registry(),
        sink,
        clock=FrozenClock(),
        operation_id_factory=fixed_operation_id_factory(),
    )
    try:
        with pytest.raises(LoggingConfigurationError):
            log.intent("user.deleted", actor="ENC[v1:aes256gcm:gen2:x]", target="widget-7")
        assert path.read_text(encoding="utf-8") == ""  # nothing persisted
    finally:
        sink.close()


# --- context manager ------------------------------------------------------


def test_context_manager_success():
    sink = CollectingAuditSink()
    log = _log(sink)
    with log.operation("user.deleted", actor="alice", target="widget-7") as op:
        op.success()
    assert sink.records[1].outcome is Outcome.SUCCESS


def test_context_manager_writes_indeterminate_on_escaping_exception():
    sink = CollectingAuditSink()
    log = _log(sink)

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with log.operation("user.deleted", actor="alice", target="widget-7"):
            raise Boom("mid-mutation")
    assert len(sink.records) == 2
    assert sink.records[1].outcome is Outcome.INDETERMINATE


def test_context_manager_explicit_failure_is_kept_through_no_exception():
    sink = CollectingAuditSink()
    log = _log(sink)
    with log.operation("user.deleted", actor="alice", target="widget-7") as op:
        op.failure(reason="not found")
    assert sink.records[1].outcome is Outcome.FAILURE


def test_context_manager_clean_exit_without_outcome_is_a_usage_error():
    # A forgotten outcome is a programming error; the intent is left orphaned,
    # never an assumed success.
    sink = CollectingAuditSink()
    log = _log(sink)
    with pytest.raises(AuditUsageError):
        with log.operation("user.deleted", actor="alice", target="widget-7"):
            pass
    assert len(sink.records) == 1  # only the intent; no fabricated outcome
    assert sink.records[0].stage is Stage.INTENT


def test_context_manager_explicit_outcome_then_exception_keeps_first_outcome():
    sink = CollectingAuditSink()
    log = _log(sink)

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with log.operation("user.deleted", actor="alice", target="widget-7") as op:
            op.success()
            raise Boom("after success")
    # The success stands; no second (indeterminate) outcome is written.
    assert len(sink.records) == 2
    assert sink.records[1].outcome is Outcome.SUCCESS


# --- oversize record prevents the mutation --------------------------------


def test_oversize_audit_intent_raises_schema_error_and_writes_nothing(tmp_path):
    # A guard-passing call whose encoded intent exceeds the record size limit: a
    # list of many max-length strings. The audit sink refuses it with
    # EventSchemaError; nothing is durable, so the mutation must not proceed.
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    registry = EventRegistry()
    registry.register(BATCH_EVENT)
    log = AuditLog(
        "exampleapp",
        registry,
        sink,
        clock=FrozenClock(),
        operation_id_factory=fixed_operation_id_factory(),
    )
    big = ["y" * 512 for _ in range(60)]  # ~30 KB, over MAX_RECORD_BYTES
    try:
        with pytest.raises(EventSchemaError):
            log.intent("batch.audited", actor="alice", items=big)
        # Nothing was durably written, so a caller following the pattern never
        # performs the mutation.
        assert path.read_text(encoding="utf-8") == ""
    finally:
        sink.close()


# --- complete() and atomic capability (§9.2, §22) -------------------------


def test_file_audit_sink_advertises_no_atomic_capability(tmp_path):
    # v0.1's file backend cannot commit state and audit in one transaction.
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    try:
        assert sink.supports_atomic is False
    finally:
        sink.close()


def test_complete_refused_on_non_atomic_sink():
    sink = CollectingAuditSink(atomic=False)
    log = _log(sink)
    with pytest.raises(LoggingConfigurationError):
        log.complete("user.deleted", actor="alice", target="widget-7")
    assert sink.records == []


def test_complete_unavailable_even_if_a_sink_claims_atomic():
    # No atomic sink exists in v0.1; complete() is not implemented, so it refuses
    # even a sink advertising the capability rather than pretending to commit.
    sink = CollectingAuditSink(atomic=True)
    log = _log(sink)
    with pytest.raises(LoggingConfigurationError):
        log.complete("user.deleted", actor="alice", target="widget-7")
    assert sink.records == []


# --- R5: context-manager-incompatible schemas rejected before intent ------


def test_context_manager_rejects_a_required_on_outcome_schema():
    # job.run declares code as required_on_outcome; the automatic indeterminate()
    # could not satisfy it, so operation() refuses before any intent is written.
    sink = CollectingAuditSink()
    log = _log(sink)
    with pytest.raises(AuditUsageError) as exc_info:
        with log.operation("job.run", actor="alice", job_id="j1"):
            pass  # never reached: operation() raises before the block is entered
    assert sink.records == []  # nothing written, no orphan intent
    message = str(exc_info.value)
    assert "job.run" in message  # the grammar-valid event id is safe to name
    assert "code" not in message  # no field names
    assert "job_id" not in message
    assert "j1" not in message  # no values


def test_required_on_outcome_schema_still_works_through_the_handle_api():
    sink = CollectingAuditSink()
    log = _log(sink)
    op = log.intent("job.run", actor="alice", job_id="j1")
    op.success(code=0)
    assert [r.stage for r in sink.records] == [Stage.INTENT, Stage.OUTCOME]


def test_compatible_schema_is_unaffected_as_a_context_manager():
    sink = CollectingAuditSink()
    log = _log(sink)
    with log.operation("user.deleted", actor="alice", target="widget-7") as op:
        op.success()
    assert sink.records[1].stage is Stage.OUTCOME


def test_context_manager_rejection_writes_nothing_to_the_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    registry = EventRegistry()
    registry.register(JOB_EVENT)
    log = AuditLog(
        "exampleapp",
        registry,
        sink,
        clock=FrozenClock(),
        operation_id_factory=fixed_operation_id_factory(),
    )
    before = path.read_bytes()
    try:
        with pytest.raises(AuditUsageError):
            with log.operation("job.run", actor="alice", job_id="j1"):
                pass
    finally:
        sink.close()
    assert path.read_bytes() == before  # byte-identical: no intent written
