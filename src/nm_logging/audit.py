"""AuditLog and AuditOperation: the fail-hard audit emit path (§9, §23).

Audit is not operational logging with a different severity; it is a different
failure contract. Where operational logging fails open, audit fails hard: if
durable intent cannot be recorded, the API raises and the caller must not perform
the mutation (§9.3, invariant 5). This path never catches ``AuditPersistenceError``
to soften it, and it shares no code with the operational fail-open path.

The non-atomic model (§9.3), used with the file backend:

    op = audit.intent(event, actor=..., **fields)   # durable before it returns
    try:
        perform_mutation()
    except KnownFailure:
        op.failure(**fields)                         # the effect did not occur
    except Exception:
        op.indeterminate(**fields)                   # cannot establish whether it did
        raise
    else:
        op.success(**fields)                         # the effect occurred

or the context-manager form, which writes ``indeterminate`` automatically if an
exception escapes the block (settled decision 2):

    with audit.operation(event, actor=..., **fields) as op:
        perform_mutation()
        op.success(**fields)

The subtlest rule in the standard (§9.3): a failure to append the *outcome*
raises ``AuditPersistenceError`` and leaves the intent orphaned for
reconciliation. It does NOT undo, retract, or reclassify the mutation, which may
already have happened. Outcomes are never fabricated (invariant 7): an intent
with no outcome is a valid, visible, incomplete operation, not an assumed
success.

``complete`` (§9.2) is available only where the sink advertises a genuine atomic
state-plus-audit boundary. The file backend does not, so ``complete`` raises
there and an audited local mutation uses intent/outcome (§22).
"""

import uuid
from collections.abc import Callable, Mapping

from .events import EventRegistry
from .exceptions import (
    AuditFinalisationError,
    AuditPersistenceError,
    AuditUsageError,
    EventSchemaError,
    LoggingConfigurationError,
)
from .interfaces import AuditSink, Clock, SystemClock
from .audit_vocab import Outcome
from .record import AuditRecord, Stage
# Envelope-identity validation is single-sourced in record.py.
from .record import _validate_application, _validate_emitter

# Operation states (§5.4). Branch on state, never on exception type.
_OPEN = "open"
_FINALISED = "finalised"
_FINALISATION_FAILED = "finalisation_failed"


def _default_operation_id() -> str:
    # Generated internally, never a caller-supplied parameter — for the same
    # reason the timestamp is not: a caller-supplied id is a colliding id.
    return uuid.uuid4().hex


class AuditLog:
    """The audit logging facade for one application.

    ``application`` and ``emitter`` are set here, not at call sites (§16.1). The
    timestamp comes from the clock and the operation id from an internal factory;
    neither is ever a call parameter. There is no health or fallback here: audit
    does not fail open, so a persistence failure propagates to the caller.
    """

    def __init__(
        self,
        application: str,
        registry: EventRegistry,
        sink: AuditSink,
        *,
        clock: Clock | None = None,
        emitter: str = "app",
        operation_id_factory: Callable[[], str] | None = None,
    ) -> None:
        _validate_application(application)
        _validate_emitter(emitter)
        if not isinstance(registry, EventRegistry):
            raise LoggingConfigurationError("registry must be an EventRegistry") from None
        self._application = application
        self._emitter = emitter
        self._registry = registry
        self._sink = sink
        self._clock = clock if clock is not None else SystemClock()
        self._new_operation_id = (
            operation_id_factory if operation_id_factory is not None else _default_operation_id
        )

    def complete(self, event: str, *, actor: str, **fields: object) -> None:
        """Record a one-record atomic audit operation (§9.2).

        Available only on a backend that can commit an application state change
        and its audit record in one transaction. No such backend exists in v0.1 —
        the file sink advertises no atomic capability — so this always raises
        here, and an audited local mutation must use intent/outcome (§9.3, §22).
        The single-record commit path is deliberately left unbuilt until the
        first atomic sink exists, rather than shipping unreachable machinery.
        """
        if not self._sink.supports_atomic:
            raise LoggingConfigurationError(
                "complete requires an atomic state-plus-audit sink; this backend "
                "uses intent/outcome"
            ) from None
        raise LoggingConfigurationError(
            "atomic complete is not implemented in this release"
        ) from None

    def intent(self, event: str, *, actor: str, **fields: object) -> "AuditOperation":
        """Durably record accountable intent before a non-atomic mutation (§9.3).

        Returns an ``AuditOperation`` only after the intent is durable. If
        durability cannot be established this raises ``AuditPersistenceError`` and
        the caller must not perform the mutation.
        """
        schema = self._registry.get(event)
        validated = schema.validate_audit(fields, stage=Stage.INTENT)
        message = schema.render_message(validated)
        operation_id = self._new_operation_id()
        record = self._build(
            event, schema.category, actor, Stage.INTENT, validated, message,
            operation_id=operation_id,
        )
        normalized = None
        try:
            self._sink.append(record)
        except AuditPersistenceError as original:
            # Normalize every AuditPersistenceError leaving the facade (§5.1). The
            # untrusted sink-owned original is kept OFF the exception chain — held
            # on the normalized error's private diagnostic attribute — so its
            # arbitrary message can reach neither a traceback nor build_evidence().
            normalized = self._normalized_persistence_error(operation_id, original)
        if normalized is not None:
            # Raised outside the ``except`` so its __context__ is not the original.
            # No handle is returned; the mutation must not proceed.
            raise normalized from None
        return AuditOperation(self, event, schema, actor, operation_id)

    def operation(self, event: str, *, actor: str, **fields: object) -> "AuditOperation":
        """The context-manager form of ``intent``. Records durable intent now;
        on ``__exit__`` writes ``indeterminate`` if an exception escapes the
        block, and requires an explicit outcome on a clean exit.

        An event declaring a ``required_on_outcome`` field cannot be finalised by
        the automatic ``indeterminate()`` (it supplies no fields), so it is
        rejected here — **before** ``intent()`` writes anything — with
        ``AuditUsageError`` (R5). The check is here, not in ``__enter__``, because
        ``operation()`` calls ``intent()`` before Python invokes ``__enter__``; a
        check in ``__enter__`` would fire after a durable intent, orphaning it for
        a call that should never have started.
        """
        schema = self._registry.get(event)
        if not schema.supports_context_manager:
            # The event ID is grammar-valid by construction, so naming it is safe;
            # no field names or values appear.
            raise AuditUsageError(
                f"event {event!r} declares a required_on_outcome field and cannot "
                f"be used as a context manager; use intent() with an explicit "
                f"success()/failure()/indeterminate()"
            ) from None
        op = self.intent(event, actor=actor, **fields)
        op._as_context_manager = True
        return op

    # -- internal, used by AuditOperation ----------------------------------

    def _append_outcome(
        self,
        event: str,
        schema,
        actor: str,
        operation_id: str,
        outcome: Outcome,
        fields: Mapping[str, object],
    ) -> None:
        # Stage-aware required fields: an outcome carries only the fields marked
        # required_on_outcome, not the intent's required fields, to which it is
        # linked by operation id (§9.3).
        validated = schema.validate_audit(fields, stage=Stage.OUTCOME)
        message = schema.render_message(validated)
        record = self._build(
            event, schema.category, actor, Stage.OUTCOME, validated, message,
            operation_id=operation_id, outcome=outcome,
        )
        # A failure here raises AuditPersistenceError and leaves the intent
        # orphaned; it does not retract the mutation (§9.3).
        self._sink.append(record)

    def _build(
        self,
        event: str,
        category,
        actor: str,
        stage: Stage,
        fields: Mapping[str, object],
        message: str | None,
        *,
        operation_id: str | None = None,
        outcome: Outcome | None = None,
    ) -> AuditRecord:
        return AuditRecord(
            application=self._application,
            emitter=self._emitter,
            event=event,
            timestamp=self._clock.now(),
            category=category,
            actor=actor,
            stage=stage,
            operation_id=operation_id,
            outcome=outcome,
            fields=fields,
            message=message,
        )

    def _normalized_persistence_error(
        self, operation_id: str, original: AuditPersistenceError
    ) -> AuditPersistenceError:
        """A fresh, package-controlled AuditPersistenceError for a persistence
        failure leaving the facade (§5.1).

        Fixed, package-controlled message and the operation id; no text or subclass
        copied from ``original``. Only this facade-created instance is safe for
        traceback or evidence rendering.

        A conforming custom sink may raise ``AuditPersistenceError`` — a package
        type — with an arbitrary, secret-bearing message. Its message was *not*
        created through a NorthMax-controlled safe path, so it is untrusted. This
        method **never mutates** that sink-owned exception (Finding 2). Instead the
        original is kept entirely OFF the exception chain: the caller raises the
        normalized error such that ``original`` is not its ``__context__``, and the
        original is attached here to a private ``_sink_failure`` attribute of the
        normalized error. ``build_evidence()`` walks only ``__cause__`` and
        ``__context__``, so it never reaches ``_sink_failure`` — the original stays
        programmatically reachable for diagnostics without its message ever being
        rendered. No lexical scanning is involved.
        """
        error = AuditPersistenceError("audit persistence failed")
        error.operation_id = operation_id
        # Off-chain diagnostic edge on the package-owned normalized error. We set
        # an attribute on ``error`` (which we own), never on ``original``.
        error._sink_failure = original
        return error


class AuditOperation:
    """A handle on an in-progress audited mutation, linking intent to outcome by
    a single internally-generated operation id.

    A small state machine drives it (§5.4): OPEN until an outcome is recorded,
    then FINALISED; or FINALISATION_FAILED if a recording attempt failed, storing
    that failure. Exactly one outcome is ever recorded; a second attempt from any
    non-OPEN state raises ``AuditUsageError``. Behaviour branches on state, never
    on exception type.

    As a context manager it writes ``indeterminate`` automatically if an exception
    escapes the block (an exception mid-mutation does not establish whether the
    effect landed); it raises ``AuditUsageError`` on a clean exit with no outcome
    (a forgotten outcome is a programming error, and the honest state is an
    orphaned intent, never a fabricated success); it re-raises a stored
    finalisation failure rather than let it be swallowed by a catch inside the
    block; and where a finalisation failure coincides with a body exception it
    raises ``AuditFinalisationError`` (§5.2), audit-failure-dominant.
    """

    def __init__(self, log: AuditLog, event: str, schema, actor: str, operation_id: str) -> None:
        self._log = log
        self._event = event
        self._schema = schema
        self._actor = actor
        self._operation_id = operation_id
        self._state = _OPEN
        self._finalisation_exception: BaseException | None = None
        self._as_context_manager = False

    @property
    def operation_id(self) -> str:
        return self._operation_id

    @property
    def finalised(self) -> bool:
        return self._state == _FINALISED

    def success(self, **fields: object) -> None:
        """Record that the intended effect is known to have occurred (§9.3)."""
        self._finalise(Outcome.SUCCESS, fields)

    def failure(self, **fields: object) -> None:
        """Record that the intended effect is known NOT to have occurred (§9.3)."""
        self._finalise(Outcome.FAILURE, fields)

    def indeterminate(self, **fields: object) -> None:
        """Record that whether the effect occurred cannot be established (§9.3)."""
        self._finalise(Outcome.INDETERMINATE, fields)

    def _finalise(self, outcome: Outcome, fields: Mapping[str, object]) -> None:
        if self._state != _OPEN:
            # No second outcome is ever attempted from any state other than OPEN
            # (§5.4): a double finalisation, or a call after a prior failure.
            raise AuditUsageError(
                "audit operation already finalised or its finalisation failed"
            ) from None
        persistence_failure = None
        try:
            self._log._append_outcome(
                self._event, self._schema, self._actor, self._operation_id, outcome, fields
            )
        except BaseException as original:
            # INVARIANT 1 (Finding 1): the instant the sink raises ANY BaseException,
            # transition to FINALISATION_FAILED and store the original — before any
            # normalization, wrapping, or classification. Classification decides
            # only what propagates, never the state transition; nothing after this
            # may leave the operation OPEN or permit a second outcome attempt, even
            # if a later step were to raise. BaseException is caught deliberately so
            # the invariant holds even under an interrupt.
            self._state = _FINALISATION_FAILED
            self._finalisation_exception = original
            if isinstance(original, AuditPersistenceError):
                # A genuine persistence failure: normalize (§5.1) and store the
                # normalized error. The original is untrusted and never mutated; it
                # is kept off the exception chain (raised below, outside this
                # ``except``, so its __context__ is not the original), reachable
                # only via the normalized error's private diagnostic attribute.
                persistence_failure = self._log._normalized_persistence_error(
                    self._operation_id, original
                )
                self._finalisation_exception = persistence_failure
            else:
                # Any other failure keeps its own type (§5.1 Rule 2; never
                # relabelled) and is re-raised unchanged.
                raise
        else:
            # Marked FINALISED only after a durable append.
            self._state = _FINALISED
            return
        # Persistence branch only: raise the normalized error OUTSIDE the ``except``,
        # so the untrusted original is not its __context__.
        raise persistence_failure from None

    def __enter__(self) -> "AuditOperation":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._state == _FINALISED:
            # The recorded outcome stands; write nothing and propagate any
            # exception in flight (§5.4).
            return False

        if self._state == _FINALISATION_FAILED:
            if exc is None:
                # Swallow-prevention (§5.4): a caller cannot swallow an audit
                # finalisation failure by catching it inside the block. Re-raise
                # the stored finalisation exception.
                raise self._finalisation_exception
            if exc is self._finalisation_exception:
                # The stored failure is itself propagating; no second attempt.
                return False
            # A different body exception: dual failure (§5.2).
            self._raise_finalisation_error(exc)

        # state == OPEN
        if exc is None:
            # Clean exit, no outcome: a programming error. The intent is left
            # orphaned (visible for reconciliation); no outcome is fabricated.
            raise AuditUsageError(
                "audited operation exited without recording an outcome"
            ) from None

        # An exception escaped mid-mutation: attempt an automatic indeterminate
        # (§5.4). Whether the side effect landed is genuinely unknown.
        try:
            self.indeterminate()
        except Exception:
            # The automatic finalisation itself failed with an ordinary exception
            # (persistence, schema, or unexpected): dual failure. _finalise has
            # already stored the finalisation exception and set FINALISATION_FAILED,
            # so no second outcome can be attempted. Raising inside this ``except``
            # sets the finalisation error's __context__ to the finalisation failure
            # itself (identical to __cause__ in branch A); the body exception stays
            # reachable deeper, through the normalized error's own context chain,
            # and the evidence walker still finds it. Both are unrendered.
            self._raise_finalisation_error(exc)
        # A BaseException that is not an Exception (e.g. KeyboardInterrupt) is not
        # caught here: _finalise has already recorded FINALISATION_FAILED, so the
        # no-second-outcome invariant holds, and the interrupt propagates unchanged
        # rather than being wrapped or relabelled.
        #
        # The automatic indeterminate succeeded: re-raise the body exception
        # unchanged by returning False.
        return False

    def _raise_finalisation_error(self, body_exception: BaseException) -> None:
        """Raise ``AuditFinalisationError`` for a finalisation failure coinciding
        with a body exception (§5.2), audit-failure-dominant (§5.7).

        Only a facade-normalized package error is ever rendered as ``__cause__``
        (§5.2). Branch A (the finalisation failure was a persistence failure)
        chains the normalized error, whose fixed message is safe; branch B (any
        other type) renders no cause at all, so traceback safety never depends on
        the inner exception's suppression flags. The body exception is always
        reachable at ``.body_exception``, and reachable (unrendered) through the
        exception chain — directly as ``__context__`` when raised alongside a
        distinct body exception, or one link deeper, through the normalized error's
        own context, on the automatic-indeterminate path. Either way the evidence
        walker locates it and omits its foreign message.
        """
        finalisation_exception = self._finalisation_exception
        error = AuditFinalisationError(
            body_exception=body_exception,
            finalisation_exception=finalisation_exception,
            operation_id=self._operation_id,
        )
        # Exactly one note (§5.5): a fixed prefix plus the two type names only.
        error.add_note(
            "nm-logging audit finalisation failed: "
            f"body={type(body_exception).__qualname__}, "
            f"finalisation={type(finalisation_exception).__qualname__}"
        )
        if isinstance(finalisation_exception, AuditPersistenceError):
            # Branch A. Deliberate chaining, against the package's ``from None``
            # house style (§5.8): the objects stay in process and rendering into a
            # record still runs through evidence.py, which excludes foreign
            # messages regardless of chain position.
            raise error from finalisation_exception
        # Branch B: render no cause; the true reason is on .finalisation_exception,
        # never rendered (§5.2).
        raise error from None
