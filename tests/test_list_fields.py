"""R7 — list-valued fields are typed, immutable, and round-trip as JSON arrays.

``Scalar = str | int | float | bool``; ``FieldValue = Scalar | tuple[Scalar, ...]``.
A validated list field materialises as a tuple (so it cannot be mutated between
validation and serialisation) and serialises to a JSON array unchanged. ``Scalar``
is internal and not exported; the ``__all__`` fixture is unchanged by R7.
"""

import json

import nm_logging
from nm_logging import (
    EventRegistry,
    EventSchema,
    FieldSpec,
    JsonlSink,
    OperationalLog,
    Severity,
)

from .helpers import FrozenClock, read_lines

LIST_EVENT = EventSchema(
    "batch.done", severity=Severity.INFO, fields=(FieldSpec("codes", int, is_list=True),)
)


def test_validated_list_field_is_a_tuple():
    result = LIST_EVENT.validate_operational(Severity.INFO, {"codes": [1, 2, 3]})
    assert isinstance(result["codes"], tuple)
    assert result["codes"] == (1, 2, 3)


def test_list_field_round_trips_to_a_json_array(tmp_path):
    path = tmp_path / "op.jsonl"
    sink = JsonlSink(path)
    registry = EventRegistry()
    registry.register(LIST_EVENT)
    log = OperationalLog("exampleapp", registry, sink, clock=FrozenClock())
    log.info("batch.done", codes=[10, 20, 30])
    sink.close()
    line = read_lines(path)[0]
    assert '"codes":[10,20,30]' in line  # emitted as a JSON array
    assert json.loads(line)["codes"] == [10, 20, 30]  # reads back as a list


def test_mutating_the_callers_list_after_emit_changes_nothing(tmp_path):
    path = tmp_path / "op.jsonl"
    sink = JsonlSink(path)
    registry = EventRegistry()
    registry.register(LIST_EVENT)
    log = OperationalLog("exampleapp", registry, sink, clock=FrozenClock())
    codes = [1, 2, 3]
    log.info("batch.done", codes=codes)
    codes.append(999)  # mutate the caller's original list after emit
    sink.close()
    line = read_lines(path)[0]
    assert "999" not in line  # neither the record nor the output changed
    assert json.loads(line)["codes"] == [1, 2, 3]


def test_scalar_is_not_exported():
    # R1 is strictly subtractive: Scalar exists now but must not be exported, and
    # the fixture is unchanged by R7.
    assert "Scalar" not in nm_logging.__all__
    assert not hasattr(nm_logging, "Scalar")
    assert "FieldValue" in nm_logging.__all__  # FieldValue stays exported
