"""OperationalLog: the fail-open operational emit path (§14).

Behaviour, per §14:

- A malformed logging call (unknown event, undeclared field, wrong type, wrong
  severity, an over-limit record) is a §14.4 logging programming error. In strict
  mode it is raised, so defects surface aggressively in development and test. In
  production it is contained: a package-owned defect record is written naming the
  event id and the violation kind — never the offending values — and the call
  returns normally.

- A sink write failure is degraded observability, not a failed operation
  (§14.1). Health is marked degraded, one bounded fallback line is emitted, and
  the call returns normally. The caller never sees it. Lost records are lost;
  there is no queue and no replay journal (§14.3).

The two contracts never merge. EventSchemaError (a defect) and SinkError (a write
failure) are caught separately and drive the two behaviours above. Crucially,
SinkError is contained even in strict mode — failing open is an invariant, not a
development convenience — while only defects escalate under strict.

There is no DEBUG method and no level control (§4): the surface is exactly
``info``, ``warning``, ``error``, ``critical``.
"""

from collections.abc import Mapping

from .events import EventRegistry, EventSchema, FieldSpec
from .exceptions import EventSchemaError, LoggingConfigurationError, SinkError
from .health import LoggingHealth
from .interfaces import Clock, Sink, SystemClock
from .record import MAX_EVENT_ID_CHARS, OperationalRecord, _EVENT_ID_RE
# The envelope-identity validators are single-sourced in record.py; the facade
# reuses them so application/emitter are validated once, at construction.
from .record import _validate_application, _validate_emitter
from .severity import Severity
from .sinks.stderr import StderrFallback

# The package's own self-report for a contained logging defect (§14.4), in the
# reserved namespace (settled decision 5). It names the offending event and the kind
# of violation, never the offending field values.
DEFECT_EVENT = "nmlogging.operational.defect"
_DEFECT_SCHEMA = EventSchema(
    DEFECT_EVENT,
    severity=Severity.ERROR,
    fields=(
        FieldSpec("violation", str, required=True),
        FieldSpec("offending_event", str),
    ),
)


def _is_safe_event_ref(value: object) -> bool:
    # An unknown-event defect carries whatever the caller passed as the event id.
    # Embed it only if it satisfies the event-ID grammar (R3b); otherwise the
    # field is absent (§18: absent, not a placeholder). This refuses arbitrary
    # caller input — including credential-shaped input — on the path that persists
    # a defect record, applying the refuse-to-echo-untrusted-input
    # rule with more force because this record is persisted.
    return (
        isinstance(value, str)
        and len(value) <= MAX_EVENT_ID_CHARS
        and _EVENT_ID_RE.fullmatch(value) is not None
    )


class OperationalLog:
    """The operational logging facade for one application.

    ``application`` and ``emitter`` are set here, not at call sites (§16.1), and
    validated at construction. The timestamp comes from the clock, never a call
    parameter. ``strict`` escalates logging defects to exceptions for development
    and test; it is not a log level (§14.4).
    """

    def __init__(
        self,
        application: str,
        registry: EventRegistry,
        sink: Sink,
        *,
        clock: Clock | None = None,
        health: LoggingHealth | None = None,
        fallback: StderrFallback | None = None,
        emitter: str = "app",
        strict: bool = False,
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
        self._health = health if health is not None else LoggingHealth()
        self._fallback = fallback if fallback is not None else StderrFallback()
        self._strict = strict
        # Register the reserved defect self-report once; reuse it if a sibling
        # facade already registered it on this shared registry.
        if DEFECT_EVENT not in registry:
            registry.register_reserved(_DEFECT_SCHEMA)
        self._defect_schema = registry.get(DEFECT_EVENT)

    @property
    def health(self) -> LoggingHealth:
        return self._health

    def info(self, event: str, **fields: object) -> None:
        self._emit(Severity.INFO, event, fields)

    def warning(self, event: str, **fields: object) -> None:
        self._emit(Severity.WARNING, event, fields)

    def error(self, event: str, **fields: object) -> None:
        self._emit(Severity.ERROR, event, fields)

    def critical(self, event: str, **fields: object) -> None:
        self._emit(Severity.CRITICAL, event, fields)

    def close(self) -> None:
        """Close the underlying sink. A lifecycle call, not part of the fail-open
        write path, so a close failure is not silently contained."""
        self._sink.close()

    # -- internals ----------------------------------------------------------

    def _emit(self, severity: Severity, event: str, fields: Mapping[str, object]) -> None:
        try:
            schema = self._registry.get(event)
        except EventSchemaError as exc:
            self._handle_defect(event, exc, "unknown_event")
            return

        try:
            validated = schema.validate_operational(severity, fields)
            message = schema.render_message(validated)
            record = OperationalRecord(
                application=self._application,
                emitter=self._emitter,
                event=event,
                timestamp=self._clock.now(),
                severity=severity,
                fields=validated,
                message=message,
            )
        except EventSchemaError as exc:
            self._handle_defect(event, exc, "schema_violation")
            return
        except LoggingConfigurationError as exc:
            # A broken clock (naive/non-UTC) or similar setup fault surfacing at
            # emit time is still a programming defect, contained the same way.
            self._handle_defect(event, exc, "record_construction")
            return

        self._write(record, event)

    def _write(self, record: OperationalRecord, event: str) -> None:
        try:
            self._sink.write(record)
        except EventSchemaError as exc:
            # An over-limit record: a producer defect detected at encode time.
            self._handle_defect(event, exc, "oversize")
            return
        except SinkError:
            # Fail open (§14.1): degraded observability, not a failed operation.
            # Contained even under strict mode — failing open is an invariant.
            self._degrade("sink_write")
            return
        self._health.mark_healthy()

    def _handle_defect(self, event: str, exc: Exception, kind: str) -> None:
        if self._strict:
            # Aggressive development/test behaviour (§14.4): surface the defect.
            raise exc
        self._emit_defect_record(event, kind)

    def _emit_defect_record(self, event: str, kind: str) -> None:
        fields: dict[str, object] = {"violation": kind}
        if _is_safe_event_ref(event):
            fields["offending_event"] = event
        try:
            validated = self._defect_schema.validate_operational(Severity.ERROR, fields)
            message = self._defect_schema.render_message(validated)
            record = OperationalRecord(
                application=self._application,
                emitter=self._emitter,
                event=DEFECT_EVENT,
                timestamp=self._clock.now(),
                severity=Severity.ERROR,
                fields=validated,
                message=message,
            )
            self._sink.write(record)
        except Exception:
            # The defect record itself could not be produced or written. Do not
            # recurse into defect handling; degrade and use the bounded fallback.
            self._degrade("defect_unwritable")
        else:
            self._health.mark_healthy()

    def _degrade(self, kind: str) -> None:
        self._health.mark_degraded(kind)
        # The fallback never raises, but guard the write path regardless.
        try:
            self._fallback.emit(f"operational sink degraded ({kind})")
        except Exception:
            pass
