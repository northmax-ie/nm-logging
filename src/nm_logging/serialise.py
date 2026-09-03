"""Record -> canonical ordered mapping (§16–§18).

This is the storage-independent half of serialisation: it turns a record into a
plain mapping with keys in the one canonical order and enum members reduced to
their wire spelling. It does not encode JSON, open files, or know about lines —
those belong to a sink (§22). Keeping the order here, single-sourced, is what
makes the byte-stable vector test meaningful: a writer-format change cannot slip
past a permissive reader if every writer goes through this one function.

Key order is fixed: the common envelope in §16.1 order, then the record-kind
fields (operational severity; audit category/actor/stage/operation_id), then the
event-specific fields in their schema-declared order, then the controlled
message last. A field with no value is absent, never a placeholder (invariant 9).

Event fields cannot displace authoritative envelope metadata: a field whose name
collides with a reserved envelope, audit, or prose name is rejected here with
``EventSchemaError`` before it is placed. Record construction rejects the same
collision, so this is defence in depth (R1) — removing either check does not
silently reopen the hole. The envelope keys are also written before the event
fields are considered, so even a check regression could not let a field overwrite
one already present.
"""

from typing import Any

from .exceptions import EventSchemaError
from .record import RESERVED_FIELD_NAMES, AuditRecord, OperationalRecord, Record


def to_mapping(record: Record) -> dict[str, Any]:
    """Return the canonical ordered mapping for ``record``.

    Insertion order is the canonical order; a sink encodes it without reordering.
    """
    mapping: dict[str, Any] = {
        "schema_version": record.schema_version,
        # Timezone-aware UTC, validated at record construction; isoformat yields
        # an unambiguous "...+00:00". No local-time rendering (§16.1).
        "timestamp": record.timestamp.isoformat(),
        "application": record.application,
        "emitter": record.emitter,
        "event": record.event,
    }

    if isinstance(record, OperationalRecord):
        mapping["severity"] = record.severity.value
    elif isinstance(record, AuditRecord):
        mapping["category"] = record.category.value
        mapping["actor"] = record.actor
        mapping["stage"] = record.stage.value
        if record.operation_id is not None:
            mapping["operation_id"] = record.operation_id
        if record.outcome is not None:
            mapping["outcome"] = record.outcome.value

    for name, value in record.fields.items():
        if name in RESERVED_FIELD_NAMES:
            # An event field must not displace authoritative envelope metadata
            # (§16–§18). Rejected independently of record construction; the name
            # is not echoed.
            raise EventSchemaError(
                "event field name collides with a reserved envelope field"
            ) from None
        mapping[name] = value

    if record.message is not None:
        mapping["message"] = record.message

    return mapping
