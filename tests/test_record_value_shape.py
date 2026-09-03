"""Finding 4 — the record layer enforces the universal FieldValue representation.

`record.py` is the structural backstop for *representation*: a field value is a
permitted scalar or an immutable tuple of permitted scalars, and nothing else.
This is schema-agnostic — whether a particular field may be list-valued, is
required, or exists at all remains EventSchema's business at the facade. So a
directly constructed record cannot persist a mapping, a list, a nested container,
or an arbitrary object, and normal schema-created records are unaffected.
"""

import pytest

import nm_logging
from nm_logging import (
    EventRegistry,
    EventSchema,
    EventSchemaError,
    FieldSpec,
    Severity,
)
from nm_logging.record import AuditRecord, OperationalRecord

from .helpers import make_audit, make_operational


@pytest.mark.parametrize("value", ["s", 5, 1.5, True, False, (1, 2, "three", True), ()])
def test_scalar_and_tuple_of_scalars_accepted(value):
    record = make_operational(fields={"f": value})
    assert record.fields["f"] == value


@pytest.mark.parametrize(
    "value",
    [
        {"a": 1},  # nested mapping
        [1, 2],  # list (mutable, not FieldValue after R7)
        ({"a": 1},),  # tuple containing a mapping
        ((1, 2),),  # nested tuple
        ([1],),  # tuple containing a list
        (1, [2]),  # tuple with a nested container element
        object(),  # arbitrary object
        {1, 2},  # set
    ],
)
def test_non_field_value_rejected_at_raw_construction(value):
    with pytest.raises(EventSchemaError):
        make_operational(fields={"f": value})


def test_backstop_applies_to_audit_records_too():
    with pytest.raises(EventSchemaError):
        make_audit(fields={"f": {"nested": 1}})
    with pytest.raises(EventSchemaError):
        make_audit(fields={"f": [1, 2]})


def test_schema_created_records_are_unaffected():
    # A list field validated through EventSchema materialises as a tuple (R7),
    # which the record backstop accepts unchanged: the facade path still works.
    registry = EventRegistry()
    registry.register(
        EventSchema("batch.done", severity=Severity.INFO, fields=(FieldSpec("codes", int, is_list=True),))
    )
    validated = registry.get("batch.done").validate_operational(Severity.INFO, {"codes": [1, 2, 3]})
    assert isinstance(validated["codes"], tuple)
    record = make_operational(event="batch.done", fields=validated)
    assert record.fields["codes"] == (1, 2, 3)


def test_field_value_backstop_adds_no_public_surface():
    # Finding 4 is a structural backstop only; it introduces no top-level export.
    assert len(nm_logging.__all__) == 39
    assert not hasattr(nm_logging, "_is_field_value")
