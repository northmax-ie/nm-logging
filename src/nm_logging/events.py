"""Event-ID grammar, field grammar, EventSchema, and EventRegistry (§19–§21).

This module owns the enforcement that makes an event schema an allowlist: which
events exist, which fields each event may carry, their types, which are required,
and the controlled message template. It knows nothing about persistence and does
not import records; records are built by the facade (M4) from the mapping this
module's field guard returns.

Two decisions are settled here (CLAUDE.md settled decisions 4 and 5):

- An operational schema pins its severity. The call-site method
  (log.info/…/critical) is checked against the pinned value, so §3.2's rule
  against controlling noise by falsifying severity is structurally enforceable
  rather than a convention. A concept that is genuinely two severities is two
  events (a ``.lost`` event and a ``.restored`` event), not one event with a
  varying severity.
- The ``nmlogging.*`` event namespace is reserved for the package's own
  self-reports. Consumer registration of it is refused; the package registers
  its own via ``register_reserved`` (used from M4). This prevents a consumer
  colliding with a package event later.

Error messages here never echo an event id, a field name, or a field value.
Following CLAUDE.md, untrusted or unbounded input arriving toward
the emit path must not reach a message and thence a log; the caller has the
schema and the call in hand and does not need it echoed back.
"""

import math
import re
import string
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .audit_vocab import Category, Stage
from .exceptions import EventSchemaError, LoggingConfigurationError
from .record import (
    MAX_EVENT_ID_CHARS,
    MAX_FIELD_CHARS,
    RESERVED_FIELD_NAMES,
    FieldValue,
    _EVENT_ID_RE,
    looks_like_enc_envelope,
)
from .severity import Severity

# RESERVED_FIELD_NAMES and the event-ID grammar (MAX_EVENT_ID_CHARS, _EVENT_ID_RE)
# are owned by record.py, which enforces them at record construction, and are
# imported here so schema registration reuses the one definition rather than
# duplicating it. The import also makes ``events.RESERVED_FIELD_NAMES`` resolve
# for callers that reference it there.

# The event namespace reserved for the package's own self-reports (open
# decision 5). Matched on the first dot-separated segment.
RESERVED_EVENT_NAMESPACE = "nmlogging"

# The scalar types a field value may take in v0.1 (§19). No dict, no arbitrary
# object, no repr() fallback. A list is allowed only where a schema declares it,
# and only of these element types.
_ALLOWED_SCALAR_TYPES = (str, int, float, bool)

# A field name is more permissive than an event segment: it may carry trailing or
# doubled underscores. It is still lower-case, no leading digit, and bounded.
_FIELD_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _first_segment(event_id: str) -> str:
    return event_id.split(".", 1)[0]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One permitted field of an event (§19).

    ``type`` is the scalar type (str, int, float, or bool); when ``is_list`` is
    set it is the element type of a list of such scalars. ``free_form`` marks a
    field expected to carry free-form or user-controlled text (§12); such a field
    is prohibited unless ``free_form_permitted`` is also set — the event has then
    explicitly taken responsibility for constraining its content. Marking a field
    free-form does not relax the value guard: a string is still length-bounded
    and still refused if it looks like an encrypted envelope.

    Required-ness is stage-aware for audit events. ``required`` means the field
    must be present on the record that carries the event's detail — the intent,
    or an atomic complete. An outcome links back to its intent by operation id
    and so need not repeat those (§9.3); a field the *outcome* must nonetheless
    carry is marked ``required_on_outcome``. For operational events, which have
    no stages, only ``required`` applies.
    """

    name: str
    type: type
    required: bool = False
    is_list: bool = False
    free_form: bool = False
    free_form_permitted: bool = False
    required_on_outcome: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _FIELD_NAME_RE.fullmatch(self.name) is None:
            # The name is not echoed: a rejected field name is untrusted input.
            raise LoggingConfigurationError(
                "field name must match [a-z][a-z0-9_]{0,63}"
            ) from None
        if self.name in RESERVED_FIELD_NAMES:
            # A bounded, grammar-valid name that collides with a reserved
            # envelope/audit/prose name (§16–§18, §20).
            raise LoggingConfigurationError(
                "field name collides with a reserved envelope field name"
            ) from None
        if self.type not in _ALLOWED_SCALAR_TYPES:
            raise LoggingConfigurationError(
                "field type must be str, int, float, or bool"
            ) from None
        if self.free_form and not self.free_form_permitted:
            # Default deny (§12): a free-form field must be explicitly permitted.
            raise LoggingConfigurationError(
                "a free-form field must be explicitly permitted by the schema"
            ) from None


@dataclass(frozen=True, slots=True)
class EventSchema:
    """A consumer-declared event and its permitted fields (§19).

    Exactly one of ``severity`` (operational, §17) or ``category`` (audit, §18)
    must be set; that choice determines the record kind. An operational schema's
    severity is pinned: a call site cannot vary it. The schema validates itself
    at construction, so a malformed schema fails at import rather than at 3 a.m.

    A ``message_template`` (§20) is owned by the event definition and rendered
    only from already-validated fields; it may reference only *required* fields,
    so rendering can never fail on an absent optional field, and it is never
    assembled from runtime or user-supplied text at a call site.
    """

    event_id: str
    severity: Severity | None = None
    category: Category | None = None
    fields: tuple[FieldSpec, ...] = ()
    message_template: str | None = None
    _by_name: Mapping[str, FieldSpec] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self._validate_event_id()
        self._validate_kind()
        by_name = self._build_field_index()
        object.__setattr__(self, "_by_name", MappingProxyType(by_name))
        self._validate_message_template(by_name)

    # -- construction-time validation --------------------------------------

    def _validate_event_id(self) -> None:
        if not isinstance(self.event_id, str):
            raise LoggingConfigurationError("event id must be a string") from None
        if len(self.event_id) > MAX_EVENT_ID_CHARS:
            # Checked before the regex, so a pathologically long id cannot even
            # reach it; the value is not echoed.
            raise LoggingConfigurationError("event id exceeds the maximum length") from None
        if _EVENT_ID_RE.fullmatch(self.event_id) is None:
            raise LoggingConfigurationError(
                "event id must be at least two dot-separated lower-case segments"
            ) from None

    def _validate_kind(self) -> None:
        has_severity = self.severity is not None
        has_category = self.category is not None
        if has_severity == has_category:
            # Neither, or both: an event is operational xor audit.
            raise LoggingConfigurationError(
                "an event schema must set exactly one of severity or category"
            ) from None
        if has_severity and not isinstance(self.severity, Severity):
            raise LoggingConfigurationError(
                "operational severity must be a Severity member"
            ) from None
        if has_category and not isinstance(self.category, Category):
            raise LoggingConfigurationError(
                "audit category must be a Category member"
            ) from None

    def _build_field_index(self) -> dict[str, FieldSpec]:
        specs = tuple(self.fields)
        object.__setattr__(self, "fields", specs)
        by_name: dict[str, FieldSpec] = {}
        for spec in specs:
            if not isinstance(spec, FieldSpec):
                raise LoggingConfigurationError(
                    "schema fields must be FieldSpec instances"
                ) from None
            if spec.name in by_name:
                raise LoggingConfigurationError(
                    "duplicate field name in event schema"
                ) from None
            by_name[spec.name] = spec
        return by_name

    def _validate_message_template(self, by_name: Mapping[str, FieldSpec]) -> None:
        if self.message_template is None:
            return
        if not isinstance(self.message_template, str):
            raise LoggingConfigurationError("message template must be a string") from None
        required = {name for name, spec in by_name.items() if spec.required}
        for _literal, field_name, _spec, conversion in string.Formatter().parse(
            self.message_template
        ):
            if field_name is None:
                continue  # a run of literal text with no placeholder
            if field_name == "":
                raise LoggingConfigurationError(
                    "message template must not use positional placeholders"
                ) from None
            if "." in field_name or "[" in field_name:
                # No attribute or index access: {actor.__class__} must be
                # impossible, so a template cannot reach past the field value.
                raise LoggingConfigurationError(
                    "message template placeholders must be plain field names"
                ) from None
            if conversion is not None:
                # No !r/!s/!a: a repr conversion could render more than the value.
                raise LoggingConfigurationError(
                    "message template placeholders must not use a conversion"
                ) from None
            if _spec:
                # No format spec: rendering stays pure substitution, so a
                # type-incompatible spec (e.g. {note:d} on a string) can never
                # raise at emit time on the fail-open path.
                raise LoggingConfigurationError(
                    "message template placeholders must not use a format spec"
                ) from None
            if field_name not in required:
                # Only required fields, so rendering never fails on an absent
                # optional field, and the template cannot name an unknown field.
                raise LoggingConfigurationError(
                    "message template may reference only required fields"
                ) from None

    # -- kind ---------------------------------------------------------------

    @property
    def is_operational(self) -> bool:
        return self.severity is not None

    @property
    def is_audit(self) -> bool:
        return self.category is not None

    @property
    def supports_context_manager(self) -> bool:
        """Whether this event may be used through ``AuditLog.operation()`` (R5).

        False when any field is ``required_on_outcome``: the context manager's
        automatic ``indeterminate()`` on an escaping exception supplies no fields,
        so validation would fail *after* a durable intent and a mutation attempt,
        demoting the body exception (§9.3). Such an event must use the handle API,
        which lets the caller supply the required outcome field.
        """
        return not any(spec.required_on_outcome for spec in self.fields)

    # -- the field guard (call-site enforcement) ---------------------------

    def validate_operational(
        self, severity: Severity, fields: Mapping[str, object]
    ) -> dict[str, FieldValue]:
        """Guard an operational call: the event must be operational, the severity
        must equal the pinned value, and the fields must pass the guard. Returns
        the validated fields in declared order. Raises EventSchemaError."""
        if not self.is_operational:
            raise EventSchemaError("event is not an operational event") from None
        if severity is not self.severity:
            # Severity pinning (settled decision 4): a call site cannot vary it.
            raise EventSchemaError(
                "severity does not match the event's pinned severity"
            ) from None
        return self._guard_fields(fields, self._required_field_names(for_outcome=False))

    def validate_audit(
        self, fields: Mapping[str, object], *, stage: Stage
    ) -> dict[str, FieldValue]:
        """Guard an audit call for a given stage. The event must be an audit
        event; the fields must pass the guard; the required fields are those
        required *for this stage* (§9.3). Returns the validated fields in
        declared order. Raises EventSchemaError.

        Required-ness is stage-aware: an intent or complete must carry the fields
        marked ``required``; an outcome, linked to its intent by operation id,
        must carry only the fields marked ``required_on_outcome``.
        """
        if not self.is_audit:
            raise EventSchemaError("event is not an audit event") from None
        for_outcome = stage is Stage.OUTCOME
        return self._guard_fields(fields, self._required_field_names(for_outcome=for_outcome))

    def _required_field_names(self, *, for_outcome: bool) -> frozenset[str]:
        if for_outcome:
            return frozenset(s.name for s in self.fields if s.required_on_outcome)
        return frozenset(s.name for s in self.fields if s.required)

    def render_message(self, validated_fields: Mapping[str, FieldValue]) -> str | None:
        """Render the message template from already-validated fields (§20), or
        None if the event declares no template.

        A template may reference only ``required`` fields, so it always renders on
        an intent or complete record, where those fields are enforced present. On
        an outcome record such a field may be legitimately absent (it was required
        for the intent, not the outcome), and then no message is rendered rather
        than raising. This tolerance never weakens required-field enforcement:
        that is validate_audit's job per stage, done independently of rendering.
        """
        if self.message_template is None:
            return None
        try:
            return self.message_template.format(**validated_fields)
        except KeyError:
            return None

    def _guard_fields(
        self, fields: Mapping[str, object], required_names: frozenset[str]
    ) -> dict[str, FieldValue]:
        if not isinstance(fields, Mapping):
            raise EventSchemaError("event fields must be given as a mapping") from None
        for name in fields:
            if name not in self._by_name:
                # An allowlist: a field the event did not declare is refused
                # (§19). The name is not echoed.
                raise EventSchemaError("call provided a field the event does not declare") from None
        result: dict[str, FieldValue] = {}
        for spec in self.fields:
            if spec.name in fields:
                result[spec.name] = _guard_value(spec, fields[spec.name])
            elif spec.name in required_names:
                raise EventSchemaError("call omitted a required field") from None
            # An absent optional field is omitted, not defaulted (invariant 9).
        return result


def _guard_value(spec: FieldSpec, value: object) -> FieldValue:
    if spec.is_list:
        # A list of scalars, permitted only because the schema declared it (§19).
        # str/bytes are sequences but are not "a list of scalars".
        if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
            raise EventSchemaError("field expects a list of scalars") from None
        # Materialise as a tuple, not a list (R7): the validated value cannot then
        # be mutated between validation and serialisation, which still emits a JSON
        # array. A fresh tuple also detaches it from the caller's sequence.
        return tuple(_guard_scalar(spec.type, element) for element in value)
    return _guard_scalar(spec.type, value)


def _guard_scalar(expected: type, value: object) -> FieldValue:
    # bool is a subclass of int, so it is matched first and never widened into an
    # int or float field: True must not be logged as 1.
    if expected is bool:
        if not isinstance(value, bool):
            raise EventSchemaError("field expects a bool") from None
        return value
    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise EventSchemaError("field expects an int") from None
        return value
    if expected is float:
        if isinstance(value, bool) or not isinstance(value, float):
            raise EventSchemaError("field expects a float") from None
        if not math.isfinite(value):
            # NaN and infinity are not valid JSON; json.dumps would emit them
            # under allow_nan and produce an unreadable record (§ hazards).
            raise EventSchemaError("float field is NaN or infinite") from None
        return value
    if expected is str:
        if not isinstance(value, str):
            raise EventSchemaError("field expects a str") from None
        if len(value) > MAX_FIELD_CHARS:
            # The value is not echoed.
            raise EventSchemaError("string field exceeds the maximum length") from None
        if looks_like_enc_envelope(value):
            # Secret-material invariant (§11, invariant 2): a value shaped like an
            # encrypted envelope is refused, not passed through, and never echoed.
            # The one structural predicate, shared with actor validation (R3a).
            raise EventSchemaError(
                "string field looks like an encrypted envelope and is refused"
            ) from None
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            # A lone surrogate is a valid str but not UTF-8 encodable; accepting it
            # would make an operational record fail closed at the sink (R4, §14.1).
            # Rejected here as a producer defect; the value is not echoed.
            raise EventSchemaError("string field is not encodable as UTF-8") from None
        return value
    # Unreachable: FieldSpec construction restricts type to the allowed scalars.
    raise EventSchemaError("field has an unsupported type") from None


class EventRegistry:
    """The set of events an application has declared, keyed by event id.

    Registration validates and rejects duplicates so a malformed or colliding
    schema fails at import. Consumer registration of the reserved ``nmlogging.*``
    namespace is refused; the package registers its own self-reports through
    ``register_reserved``.
    """

    def __init__(self) -> None:
        self._schemas: dict[str, EventSchema] = {}

    def register(self, schema: EventSchema) -> None:
        """Register a consumer event. Refuses the reserved namespace and refuses a
        duplicate. Raises LoggingConfigurationError."""
        if _first_segment(schema.event_id) == RESERVED_EVENT_NAMESPACE:
            raise LoggingConfigurationError(
                "the nmlogging.* event namespace is reserved for the logging package"
            ) from None
        self._insert(schema)

    def register_reserved(self, schema: EventSchema) -> None:
        """Register a package self-report in the reserved namespace. Not for
        consumer use: it refuses anything outside ``nmlogging.*``. Present from M2
        so the reservation is real and usable; the package's own events land in
        M4."""
        if _first_segment(schema.event_id) != RESERVED_EVENT_NAMESPACE:
            raise LoggingConfigurationError(
                "register_reserved accepts only the reserved nmlogging.* namespace"
            ) from None
        self._insert(schema)

    def _insert(self, schema: EventSchema) -> None:
        if not isinstance(schema, EventSchema):
            raise LoggingConfigurationError("only an EventSchema may be registered") from None
        if schema.event_id in self._schemas:
            # A duplicate registration is a setup error, caught at import. The id
            # is not echoed.
            raise LoggingConfigurationError("event is already registered") from None
        self._schemas[schema.event_id] = schema

    def get(self, event_id: object) -> EventSchema:
        """Return the schema for ``event_id``. Emitting an unregistered event is a
        call-site violation, so a miss raises EventSchemaError, not a lookup
        error. The id is not echoed.

        The type is checked before the dictionary lookup so an unhashable value
        (e.g. a list) raises EventSchemaError rather than a TypeError that would
        escape the operational fail-open path (Finding 3, §14.4)."""
        if not isinstance(event_id, str):
            raise EventSchemaError("event is not a string") from None
        try:
            return self._schemas[event_id]
        except KeyError:
            raise EventSchemaError("event is not registered") from None

    def __contains__(self, event_id: object) -> bool:
        return isinstance(event_id, str) and event_id in self._schemas
