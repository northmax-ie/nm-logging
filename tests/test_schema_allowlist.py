"""The field guard: event schemas as allowlists (§12, §19), severity pinning
(open decision 4), and message templates (§20).

Covers undeclared fields, wrong types, free-form default-deny, NaN/infinity,
oversize strings, required-field omission, the pinned-severity check, the
operational/audit split, lists of scalars, absent-not-empty, declared field
order, and template validation and rendering.
"""

import pytest

from nm_logging import (
    Category,
    EventSchema,
    EventSchemaError,
    FieldSpec,
    LoggingConfigurationError,
    Severity,
    Stage,
)

INFO_EVENT = EventSchema(
    "update.run.completed",
    severity=Severity.INFO,
    fields=(
        FieldSpec("eligible", int, required=True),
        FieldSpec("updated", int),
        FieldSpec("note", str),
    ),
)

AUDIT_EVENT = EventSchema(
    "user.created",
    category=Category.ACTIVITY,
    fields=(FieldSpec("target", str, required=True),),
)


# --- allowlisting ----------------------------------------------------------


def test_undeclared_field_rejected():
    with pytest.raises(EventSchemaError):
        INFO_EVENT.validate_operational(Severity.INFO, {"eligible": 1, "surprise": 2})


def test_required_field_missing_rejected():
    with pytest.raises(EventSchemaError):
        INFO_EVENT.validate_operational(Severity.INFO, {"updated": 5})


def test_optional_field_absent_is_omitted_not_defaulted():
    # Absent, not empty (invariant 9): an omitted optional field does not appear.
    result = INFO_EVENT.validate_operational(Severity.INFO, {"eligible": 5})
    assert result == {"eligible": 5}
    assert "updated" not in result
    assert "note" not in result


def test_validated_fields_follow_declared_order():
    # Byte-stable serialisation (M4) depends on a deterministic field order.
    result = INFO_EVENT.validate_operational(
        Severity.INFO, {"note": "x", "updated": 2, "eligible": 1}
    )
    assert list(result.keys()) == ["eligible", "updated", "note"]


# --- types -----------------------------------------------------------------


def test_wrong_type_rejected():
    with pytest.raises(EventSchemaError):
        INFO_EVENT.validate_operational(Severity.INFO, {"eligible": "not an int"})


def test_bool_not_accepted_for_int_field():
    # bool is an int subclass; it must not be widened into an int field.
    with pytest.raises(EventSchemaError):
        INFO_EVENT.validate_operational(Severity.INFO, {"eligible": True})


def test_int_not_accepted_for_float_field():
    schema = EventSchema("m.reading", severity=Severity.INFO, fields=(FieldSpec("ratio", float),))
    with pytest.raises(EventSchemaError):
        schema.validate_operational(Severity.INFO, {"ratio": 5})
    # A genuine float is accepted.
    assert schema.validate_operational(Severity.INFO, {"ratio": 5.0}) == {"ratio": 5.0}


def test_bool_field_accepts_only_bool():
    schema = EventSchema("m.flag", severity=Severity.INFO, fields=(FieldSpec("ok", bool),))
    assert schema.validate_operational(Severity.INFO, {"ok": True}) == {"ok": True}
    with pytest.raises(EventSchemaError):
        schema.validate_operational(Severity.INFO, {"ok": 1})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_float_nan_and_infinity_rejected(bad):
    schema = EventSchema("m.reading", severity=Severity.INFO, fields=(FieldSpec("ratio", float),))
    with pytest.raises(EventSchemaError):
        schema.validate_operational(Severity.INFO, {"ratio": bad})


def test_oversize_string_rejected():
    schema = EventSchema("m.note", severity=Severity.INFO, fields=(FieldSpec("text", str),))
    with pytest.raises(EventSchemaError):
        schema.validate_operational(Severity.INFO, {"text": "x" * 513})
    # Exactly at the limit is accepted.
    assert schema.validate_operational(Severity.INFO, {"text": "x" * 512})["text"] == "x" * 512


# --- free-form default deny (§12) -----------------------------------------


def test_free_form_field_denied_by_default():
    with pytest.raises(LoggingConfigurationError):
        FieldSpec("description", str, free_form=True)


def test_free_form_field_allowed_when_explicitly_permitted():
    spec = FieldSpec("description", str, free_form=True, free_form_permitted=True)
    assert spec.free_form and spec.free_form_permitted
    schema = EventSchema("m.noted", severity=Severity.INFO, fields=(spec,))
    # Still guarded: a permitted free-form field does not relax length or the
    # encrypted-envelope refusal.
    assert schema.validate_operational(Severity.INFO, {"description": "free text"}) == {
        "description": "free text"
    }
    with pytest.raises(EventSchemaError):
        schema.validate_operational(Severity.INFO, {"description": "x" * 513})


# --- severity pinning (open decision 4) -----------------------------------


def test_severity_must_match_the_pinned_value():
    with pytest.raises(EventSchemaError):
        INFO_EVENT.validate_operational(Severity.ERROR, {"eligible": 1})
    # The pinned severity passes.
    assert INFO_EVENT.validate_operational(Severity.INFO, {"eligible": 1}) == {"eligible": 1}


# --- operational / audit split --------------------------------------------


def test_schema_must_set_exactly_one_of_severity_or_category():
    with pytest.raises(LoggingConfigurationError):
        EventSchema("x.y")  # neither
    with pytest.raises(LoggingConfigurationError):
        EventSchema("x.y", severity=Severity.INFO, category=Category.ADMIN)  # both


def test_operational_guard_refuses_an_audit_event():
    with pytest.raises(EventSchemaError):
        AUDIT_EVENT.validate_operational(Severity.INFO, {"target": "t"})


def test_audit_guard_refuses_an_operational_event():
    with pytest.raises(EventSchemaError):
        INFO_EVENT.validate_audit({"eligible": 1}, stage=Stage.INTENT)


def test_audit_guard_accepts_a_valid_audit_call():
    assert AUDIT_EVENT.validate_audit({"target": "widget-7"}, stage=Stage.INTENT) == {
        "target": "widget-7"
    }


def test_audit_outcome_stage_relaxes_intent_required_fields():
    # target is required on the intent but not on the outcome, which links back
    # by operation id (stage-aware required-ness).
    assert AUDIT_EVENT.validate_audit({}, stage=Stage.OUTCOME) == {}
    with pytest.raises(EventSchemaError):
        AUDIT_EVENT.validate_audit({}, stage=Stage.INTENT)


# --- lists of scalars ------------------------------------------------------


def test_list_of_scalars_when_declared():
    schema = EventSchema(
        "batch.done", severity=Severity.INFO, fields=(FieldSpec("codes", int, is_list=True),)
    )
    # A validated list field materialises as an immutable tuple (R7).
    assert schema.validate_operational(Severity.INFO, {"codes": [1, 2, 3]}) == {
        "codes": (1, 2, 3)
    }
    assert schema.validate_operational(Severity.INFO, {"codes": (4, 5)}) == {"codes": (4, 5)}


def test_list_element_of_wrong_type_rejected():
    schema = EventSchema(
        "batch.done", severity=Severity.INFO, fields=(FieldSpec("codes", int, is_list=True),)
    )
    with pytest.raises(EventSchemaError):
        schema.validate_operational(Severity.INFO, {"codes": [1, "two"]})


def test_non_list_value_for_list_field_rejected():
    schema = EventSchema(
        "batch.done", severity=Severity.INFO, fields=(FieldSpec("codes", int, is_list=True),)
    )
    with pytest.raises(EventSchemaError):
        schema.validate_operational(Severity.INFO, {"codes": 1})
    # A bare string is a sequence but is not a list of scalars.
    with pytest.raises(EventSchemaError):
        schema.validate_operational(Severity.INFO, {"codes": "12"})


def test_scalar_value_for_list_field_wrapped_only_when_declared():
    # A scalar into a non-list field is fine; the reverse is guarded above.
    schema = EventSchema("m.one", severity=Severity.INFO, fields=(FieldSpec("code", int),))
    assert schema.validate_operational(Severity.INFO, {"code": 7}) == {"code": 7}


# --- message templates (§20) ----------------------------------------------


def test_message_template_renders_from_validated_fields():
    schema = EventSchema(
        "update.run.completed",
        severity=Severity.INFO,
        fields=(FieldSpec("eligible", int, required=True), FieldSpec("updated", int, required=True)),
        message_template="update run completed eligible={eligible} updated={updated}",
    )
    fields = schema.validate_operational(Severity.INFO, {"eligible": 5, "updated": 5})
    assert schema.render_message(fields) == "update run completed eligible=5 updated=5"


def test_schema_without_template_renders_no_message():
    assert INFO_EVENT.render_message({"eligible": 1}) is None


def test_template_referencing_an_optional_field_rejected():
    # Only required fields may appear, so rendering can never fail on an absent
    # optional field.
    with pytest.raises(LoggingConfigurationError):
        EventSchema(
            "m.note",
            severity=Severity.INFO,
            fields=(FieldSpec("text", str),),
            message_template="note: {text}",
        )


def test_template_rejecting_positional_placeholder():
    with pytest.raises(LoggingConfigurationError):
        EventSchema(
            "m.note",
            severity=Severity.INFO,
            fields=(FieldSpec("text", str, required=True),),
            message_template="note: {}",
        )


def test_template_rejecting_attribute_access():
    with pytest.raises(LoggingConfigurationError):
        EventSchema(
            "m.note",
            severity=Severity.INFO,
            fields=(FieldSpec("text", str, required=True),),
            message_template="note: {text.__class__}",
        )


def test_template_rejecting_conversion():
    with pytest.raises(LoggingConfigurationError):
        EventSchema(
            "m.note",
            severity=Severity.INFO,
            fields=(FieldSpec("text", str, required=True),),
            message_template="note: {text!r}",
        )
