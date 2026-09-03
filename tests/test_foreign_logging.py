"""Foreign/runtime logging isolation (§25, invariant 14).

nm-logging neither manages foreign logging nor bridges records between Python
stdlib logging and authoritative NorthMax operational or audit storage.
"""

import importlib.util
import io
import logging

import nm_logging
from nm_logging import (
    AuditLog,
    Category,
    EventRegistry,
    EventSchema,
    JsonlAuditSink,
    JsonlSink,
    OperationalLog,
    Severity,
)

from .helpers import FrozenClock


def _registry() -> EventRegistry:
    registry = EventRegistry()
    registry.register(EventSchema("thing.happened", severity=Severity.INFO))
    registry.register(EventSchema("config.changed", category=Category.ADMIN))
    return registry


def _every_public_class():
    for name in nm_logging.__all__:
        obj = getattr(nm_logging, name)
        if isinstance(obj, type):
            yield name, obj


def test_foreign_logging_management_api_is_absent():
    assert importlib.util.find_spec("nm_logging.containment") is None


def test_no_public_class_is_a_stdlib_logging_handler():
    for name, cls in _every_public_class():
        assert not issubclass(cls, logging.Handler), name


def test_authoritative_sinks_are_not_stdlib_logging_handlers():
    assert not issubclass(JsonlSink, logging.Handler)
    assert not issubclass(JsonlAuditSink, logging.Handler)


def test_foreign_logging_does_not_reach_authoritative_storage(tmp_path):
    op_path = tmp_path / "op.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    op_sink = JsonlSink(op_path)
    audit_sink = JsonlAuditSink(audit_path)
    registry = _registry()
    OperationalLog("exampleapp", registry, op_sink, clock=FrozenClock())
    AuditLog("exampleapp", registry, audit_sink, clock=FrozenClock())

    foreign = logging.getLogger("nm_logging_tests.foreign_component")
    saved_handlers = foreign.handlers[:]
    saved_level = foreign.level
    saved_propagate = foreign.propagate
    saved_disabled = foreign.disabled
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    try:
        foreign.handlers[:] = [handler]
        foreign.setLevel(logging.DEBUG)
        foreign.propagate = False
        foreign.disabled = False
        foreign.debug("foreign debug")
        foreign.warning("foreign warning")
    finally:
        foreign.handlers[:] = saved_handlers
        foreign.setLevel(saved_level)
        foreign.propagate = saved_propagate
        foreign.disabled = saved_disabled
        handler.close()
        op_sink.close()
        audit_sink.close()

    assert "foreign debug" in stream.getvalue()
    assert "foreign warning" in stream.getvalue()
    assert op_path.read_text(encoding="utf-8") == ""
    assert audit_path.read_text(encoding="utf-8") == ""


def test_northmax_records_are_not_forwarded_to_root_logging(tmp_path):
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    op_path = tmp_path / "op.jsonl"
    audit_path = tmp_path / "audit.jsonl"
    op_sink = JsonlSink(op_path)
    audit_sink = JsonlAuditSink(audit_path)
    try:
        root.handlers[:] = [handler]
        root.setLevel(logging.DEBUG)
        registry = _registry()
        operational = OperationalLog("exampleapp", registry, op_sink, clock=FrozenClock())
        audit = AuditLog("exampleapp", registry, audit_sink, clock=FrozenClock())

        operational.info("thing.happened")
        operation = audit.intent("config.changed", actor="alice")
        operation.success()
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        handler.close()
        op_sink.close()
        audit_sink.close()

    assert stream.getvalue() == ""
    assert '"event":"thing.happened"' in op_path.read_text(encoding="utf-8")
    assert '"event":"config.changed"' in audit_path.read_text(encoding="utf-8")


def test_normal_setup_and_use_do_not_modify_root_logger(tmp_path):
    root = logging.getLogger()
    handlers_before = root.handlers[:]
    level_before = root.level
    handler_levels_before = [handler.level for handler in handlers_before]
    op_sink = JsonlSink(tmp_path / "op.jsonl")
    audit_sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    try:
        registry = _registry()
        operational = OperationalLog("exampleapp", registry, op_sink, clock=FrozenClock())
        audit = AuditLog("exampleapp", registry, audit_sink, clock=FrozenClock())
        operational.info("thing.happened")
        operation = audit.intent("config.changed", actor="alice")
        operation.success()
    finally:
        op_sink.close()
        audit_sink.close()

    assert root.handlers == handlers_before
    assert root.level == level_before
    assert [handler.level for handler in root.handlers] == handler_levels_before
