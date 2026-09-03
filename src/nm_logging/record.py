"""The backend-neutral logical record: the common envelope and the two record
shapes (§16–§18).

This module owns what a record *is*: the contract constants that bound it, the
reserved envelope field names, and the event-ID grammar. It does not own the
event schema or the field guard (events.py) — those need the registry — nor the
safe exception evidence (evidence.py), nor any persistence: a record knows
nothing about files, lines, or JSON.

Construction is a structural backstop, not the schema check. It enforces exactly
what can be checked without a registry: reserved-name collisions, the types and
shapes of the envelope fields, timezone-aware UTC, the presence of the required
envelope fields, and the event-ID grammar — and no more. Schema enforcement
(declared event fields, pinned severity, free-form policy) stays at the facade,
which alone holds the registry. It never echoes an offending value into a message.

Records are frozen dataclasses rather than NamedTuples because construction must
validate — reject a naive timestamp, reject a reserved-name collision, enforce
the operation-id presence rules — and freeze the event-specific fields into a
read-only mapping. A NamedTuple offers no construction hook for that.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType

from .audit_vocab import Category, Outcome, Stage
from .exceptions import EventSchemaError, LoggingConfigurationError
from .severity import Severity

# Actor values that assert no real accountability (§6, settled decision 3). Rejected
# case-insensitively after stripping, so the most obvious fake-accountability
# placeholders cannot enter an audit record by any route. This is not a claim
# that the library can judge whether an arbitrary identity is genuinely
# accountable — supplying a canonical authenticated actor is the consumer's job.
_NON_ACCOUNTABLE_ACTORS = frozenset({"system", "none", "null", "-"})

SCHEMA_VERSION = 1
"""The version of the NorthMax logging record contract (§16.1). Fixed in code,
not configurable: it identifies the contract a reader must understand, and a
per-deployment version would let one application write records another cannot
interpret."""

MAX_RECORD_BYTES = 16384
"""Maximum serialised size of a single record. Fixed, not configurable: the same
interoperability argument that pins a maximum envelope size elsewhere — a configurable
limit lets one application write a record another refuses to read. Enforced where
records are serialised (M4), not here; defined here as a contract constant."""

MAX_FIELD_CHARS = 512
"""Maximum length of a single string field value. Fixed for the same reason as
MAX_RECORD_BYTES. Enforced by the field guard (events.py, M2); defined here so
the contract limits live with the contract."""

# The scalar types a field value may take in v0.1 (§19). No dict, no arbitrary
# object, no repr() fallback: those are the field guard's concern, but the value
# types are named here as part of the contract. bool is a subclass of int, which
# is intentional — both serialise cleanly.
Scalar = str | int | float | bool

# A field value is a scalar, or — where a schema declares a list field — a tuple
# of scalars (R7). A validated list materialises as a *tuple*, not a list, so it
# cannot be mutated between validation and serialisation; serialisation emits it
# as a JSON array unchanged. ``Scalar`` is internal and deliberately not exported
# (R1 is strictly subtractive); ``Evidence`` is not a FieldValue.
FieldValue = Scalar | tuple[Scalar, ...]

EMITTERS = frozenset({"app", "wrapper"})
"""The documented set of runtime entities that may emit a record (§16.1).
``app`` is the application itself. ``wrapper`` is reserved for a future lifecycle
wrapper and is not produced in v0.1; it is part of the contract vocabulary so the
record model is complete, but the application must never emit evidence about its
own death or restart under it (§13.2). Emitter is supplied by the logging setup,
never chosen at a call site (§16.1)."""

# The reserved envelope, audit, and prose field names an event field must not
# collide with (§16–§18, §20). Owned here, with the record contract, because a
# collision must be rejected at construction as well as at serialisation, and
# record.py cannot import events.py. events.py and serialise.py import this set
# rather than duplicating it. ``message`` is the package-owned prose field;
# ``outcome`` is set by the audit path.
RESERVED_FIELD_NAMES = frozenset(
    {
        "schema_version",
        "timestamp",
        "application",
        "emitter",
        "event",
        "severity",
        "category",
        "actor",
        "stage",
        "operation_id",
        "outcome",
        "message",
    }
)

def looks_like_enc_envelope(value: str) -> bool:
    """Whether ``value`` has the structural shape of an encrypted envelope: opens
    with ``ENC[`` and closes with ``]`` (§11).

    The single structural predicate for this shape, reused by the event-field
    guard (events.py) and by audit actor validation, so there is exactly one copy
    (R3a). Secret material must never enter a record, encrypted envelopes
    included; this catches the one structurally identifiable form, and claims
    nothing about arbitrary strings.
    """
    return value.startswith("ENC[") and value.endswith("]")


# fullmatch, not match: with match a trailing "\n" would slip past the anchor
# and admit "myapp\n" as an application id; every frozen grammar here anchors alike.
_APPLICATION_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")

# The event-ID grammar (§21), owned here so record construction can enforce it as
# a structural backstop and events.py can reuse the one definition for schema
# registration. At least two dot-separated segments; each segment is
# ``[a-z][a-z0-9]*(_[a-z0-9]+)*`` — lower-case, no leading digit, no leading,
# trailing, or doubled underscore within a segment.
MAX_EVENT_ID_CHARS = 128
_EVENT_SEGMENT = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
_EVENT_ID_RE = re.compile(rf"{_EVENT_SEGMENT}(?:\.{_EVENT_SEGMENT})+")

_UTC_OFFSET = timedelta(0)


def _validate_application(application: str) -> None:
    if not isinstance(application, str) or _APPLICATION_RE.fullmatch(application) is None:
        # The value is not echoed: a rejected application id is untrusted,
        # potentially unbounded input and must not reach a message or logs.
        raise LoggingConfigurationError(
            "application id must match [a-z][a-z0-9_]{0,63}"
        ) from None


def _validate_emitter(emitter: str) -> None:
    if not isinstance(emitter, str) or emitter not in EMITTERS:
        # The allowed set is small, closed, and documented (§16.1); the offending
        # value is not echoed.
        raise LoggingConfigurationError(
            "emitter must be one of the documented values"
        ) from None


def _validate_event(event: str) -> None:
    # The event-ID grammar is enforced here as a structural backstop, using the
    # same single definition events.py reuses for schema registration. A record
    # built through the facade always carries a registered, grammar-valid event;
    # a record built directly must still satisfy the grammar. The value is not
    # echoed — a rejected event id is untrusted, potentially unbounded input.
    if not isinstance(event, str):
        raise LoggingConfigurationError("event must be a string") from None
    if len(event) > MAX_EVENT_ID_CHARS or _EVENT_ID_RE.fullmatch(event) is None:
        raise LoggingConfigurationError(
            "event id must be at least two dot-separated lower-case segments"
        ) from None


# The permitted scalar types for a field value (§19). bool is a subclass of int,
# so isinstance covers both; both serialise cleanly.
_PERMITTED_SCALAR_TYPES = (str, int, float, bool)


def _is_field_value(value: object) -> bool:
    """Whether ``value`` matches the universal FieldValue representation: a
    permitted scalar, or an immutable tuple of permitted scalars (§19, R7).

    This is the record layer's representational backstop (Finding 4). It rejects
    mappings, lists and other mutable or nested containers, and arbitrary objects,
    so a directly constructed record cannot persist a value outside FieldValue or
    retain a shallow mutable reference. It is deliberately schema-agnostic:
    whether a *particular* field may be list-valued, is required, or exists at all
    is EventSchema's business, held at the facade — not the record's.
    """
    if isinstance(value, tuple):
        return all(isinstance(element, _PERMITTED_SCALAR_TYPES) for element in value)
    return isinstance(value, _PERMITTED_SCALAR_TYPES)


def _validate_timestamp(timestamp: datetime) -> None:
    if not isinstance(timestamp, datetime):
        raise LoggingConfigurationError("record timestamp must be a datetime") from None
    offset = timestamp.utcoffset()
    if offset is None:
        # A naive datetime is rejected at the boundary (§16.1, invariant 10).
        # The deprecated naive-UTC constructor returns such a value, and it is
        # caught precisely here rather than silently persisted.
        raise LoggingConfigurationError(
            "record timestamp must be timezone-aware UTC, not naive"
        ) from None
    if offset != _UTC_OFFSET:
        raise LoggingConfigurationError(
            "record timestamp must be UTC (zero offset)"
        ) from None


@dataclass(frozen=True, slots=True, kw_only=True)
class Record:
    """The common required envelope every record carries (§16.1): schema_version,
    timestamp, application, emitter, event, plus the event-specific fields.

    schema_version is stamped by the library and is not a constructor argument
    (invariant 11): every record written by this version carries SCHEMA_VERSION.
    All records are keyword-constructed; a record is a named type, never a bare
    mapping or tuple.
    """

    application: str
    emitter: str
    event: str
    timestamp: datetime
    fields: Mapping[str, FieldValue] = field(default_factory=dict)
    message: str | None = None
    schema_version: int = field(default=SCHEMA_VERSION, init=False)

    def __post_init__(self) -> None:
        _validate_application(self.application)
        _validate_emitter(self.emitter)
        _validate_event(self.event)
        _validate_timestamp(self.timestamp)
        # The message (§20) is controlled prose rendered by the event definition
        # from already-validated fields; here it is only type-checked. It is never
        # assembled from raw call-site text — that is the facade's contract.
        if self.message is not None and not isinstance(self.message, str):
            raise LoggingConfigurationError("record message must be a string or None") from None
        if not isinstance(self.fields, Mapping):
            raise LoggingConfigurationError("record fields must be a mapping") from None
        collisions = RESERVED_FIELD_NAMES.intersection(self.fields)
        if collisions:
            # An event field named like a reserved envelope, audit, or prose field
            # would displace authoritative metadata at serialisation (§16–§18).
            # Rejected here as a producer defect (EventSchemaError, not a
            # configuration error), independently of the serialisation check, so
            # removing either does not silently reopen the hole. Names are not
            # echoed.
            raise EventSchemaError(
                "event field name collides with a reserved envelope field"
            ) from None
        for value in self.fields.values():
            if not _is_field_value(value):
                # A record value must be a scalar or an immutable tuple of scalars
                # (§19, R7). This universal representational check needs no
                # registry; event-specific field rules stay at the facade. The
                # value is not echoed.
                raise EventSchemaError(
                    "record field value must be a scalar or a tuple of scalars"
                ) from None
        # Copy into a read-only mapping so a caller holding a reference to the
        # dict it passed in cannot mutate the record's fields after construction.
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationalRecord(Record):
    """An operational record (§17): the common envelope plus a severity.

    Carries no audit fields. Severity is one of the four members; there is no
    DEBUG and no threshold (§4).
    """

    severity: Severity

    def __post_init__(self) -> None:
        # Explicit super(): dataclass(slots=True) rebuilds the class, which
        # breaks the __class__ cell that a zero-argument super() relies on.
        super(OperationalRecord, self).__post_init__()
        if not isinstance(self.severity, Severity):
            raise LoggingConfigurationError(
                "operational severity must be a Severity member"
            ) from None


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditRecord(Record):
    """An audit record (§18): the common envelope plus category, actor, stage,
    an operation id for intent/outcome pairs, and an outcome for outcome records.
    It carries NO severity (§5).

    operation_id is required for INTENT and OUTCOME and must be absent for
    COMPLETE, which stands alone (§9.3, §18). outcome is required for OUTCOME,
    absent for INTENT, and optional for COMPLETE (an atomic record may state a
    definitive result, §8). A field with no semantic meaning is omitted, never a
    placeholder (invariant 9).

    The actor must be present, non-empty, and not one of the non-accountable
    placeholder values (§6, settled decision 3): the library refuses obvious fake
    accountability outright, while leaving the supply of a canonical
    authenticated identity to the consumer.
    """

    category: Category
    actor: str
    stage: Stage
    operation_id: str | None = None
    outcome: Outcome | None = None

    def __post_init__(self) -> None:
        # Explicit super(): see OperationalRecord.__post_init__ for why the
        # zero-argument form fails under dataclass(slots=True).
        super(AuditRecord, self).__post_init__()
        if not isinstance(self.category, Category):
            raise LoggingConfigurationError(
                "audit category must be a Category member"
            ) from None
        if not isinstance(self.stage, Stage):
            raise LoggingConfigurationError("audit stage must be a Stage member") from None
        self._validate_actor()
        self._validate_operation_id()
        self._validate_outcome()

    def _validate_actor(self) -> None:
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise LoggingConfigurationError("audit actor must be a non-empty string") from None
        if self.actor.strip().casefold() in _NON_ACCOUNTABLE_ACTORS:
            # The value is not echoed: actor may be personal data, and a rejected
            # placeholder needs no repeating.
            raise LoggingConfigurationError(
                "audit actor must be an accountable identity, not a placeholder"
            ) from None
        if looks_like_enc_envelope(self.actor):
            # Secret material must never enter a record (§11), and reserved
            # identity metadata is no exception (R3a). Same predicate as the field
            # guard; the value is not echoed.
            raise LoggingConfigurationError(
                "audit actor must not be an encrypted envelope"
            ) from None

    def _validate_operation_id(self) -> None:
        needs_id = self.stage in (Stage.INTENT, Stage.OUTCOME)
        has_id = self.operation_id is not None
        if needs_id and not has_id:
            raise LoggingConfigurationError(
                "operation_id is required for intent and outcome records"
            ) from None
        if not needs_id and has_id:
            # A complete record is a single atomic operation with nothing to
            # pair, so an operation_id would be a meaningless placeholder (§18).
            raise LoggingConfigurationError(
                "operation_id must be absent for a complete record"
            ) from None
        if has_id and not (isinstance(self.operation_id, str) and self.operation_id.strip()):
            raise LoggingConfigurationError(
                "operation_id must be a non-empty string when present"
            ) from None

    def _validate_outcome(self) -> None:
        if self.stage is Stage.OUTCOME:
            if not isinstance(self.outcome, Outcome):
                raise LoggingConfigurationError(
                    "an outcome record must carry an Outcome"
                ) from None
        elif self.stage is Stage.INTENT:
            if self.outcome is not None:
                # An intent has no outcome yet; that is the whole point (§9.3).
                raise LoggingConfigurationError(
                    "an intent record must not carry an outcome"
                ) from None
        else:  # COMPLETE: an outcome is optional but, if present, must be valid.
            if self.outcome is not None and not isinstance(self.outcome, Outcome):
                raise LoggingConfigurationError(
                    "outcome must be an Outcome member when present"
                ) from None
