"""Foreign/runtime logging containment (§25, invariant 14).

nm-logging exposes no route by which root/stdlib/third-party logging can enter
authoritative NorthMax storage: root-logger propagation reaches no NorthMax sink,
and third-party DEBUG never becomes a NorthMax record. The guarantee is about
nm-logging's own surface — the strongest form is structural, that nm_logging
ships no logging.Handler — asserted directly here alongside behavioural checks
through install(). (nm-logging cannot stop a consuming application from
misconfiguring Python logging on its own; that is out of its hands.)
"""

import io
import logging

import pytest

import nm_logging
from nm_logging import EventRegistry, EventSchema, JsonlAuditSink, JsonlSink, OperationalLog, Severity
from nm_logging.containment import install, is_installed, uninstall

from .helpers import FrozenClock, read_lines


@pytest.fixture
def clean_root_logger():
    """Snapshot and restore the root logger, so a test's logging config cannot
    leak into others."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        yield root
    finally:
        uninstall()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _every_public_class():
    for name in nm_logging.__all__:
        obj = getattr(nm_logging, name)
        if isinstance(obj, type):
            yield name, obj


def test_no_public_class_is_a_logging_handler():
    # Invariant 14: no NorthMax sink is ever a logging.Handler, so none can be
    # attached to a shared or root logger.
    for name, cls in _every_public_class():
        assert not issubclass(cls, logging.Handler), name


def test_concrete_sinks_are_not_logging_handlers():
    assert not issubclass(JsonlSink, logging.Handler)
    assert not issubclass(JsonlAuditSink, logging.Handler)


def test_install_is_idempotent(clean_root_logger):
    stream = io.StringIO()
    first = install(stream)
    second = install(stream)
    assert first is second
    installed = [h for h in clean_root_logger.handlers if h is first]
    assert len(installed) == 1
    assert is_installed()


def test_install_sets_handler_level_not_root_level(clean_root_logger):
    # The level applies to the installed handler; the root logger's global level
    # is left untouched.
    clean_root_logger.setLevel(logging.INFO)
    handler = install(io.StringIO(), level=logging.WARNING)
    assert handler.level == logging.WARNING
    assert clean_root_logger.level == logging.INFO  # unchanged


def test_third_party_logging_does_not_reach_authoritative_storage(tmp_path, clean_root_logger):
    # An authoritative operational sink and log.
    op_path = tmp_path / "op.jsonl"
    sink = JsonlSink(op_path)
    registry = EventRegistry()
    registry.register(EventSchema("thing.happened", severity=Severity.INFO))
    log = OperationalLog("exampleapp", registry, sink, clock=FrozenClock())

    stream = io.StringIO()
    install(stream)

    # Force third-party DEBUG all the way through to handlers.
    clean_root_logger.setLevel(logging.DEBUG)
    third_party = logging.getLogger("thirdparty.lib")
    third_party.setLevel(logging.DEBUG)
    third_party.debug("a foreign debug line with SECRET_MARKER_x")
    third_party.warning("a foreign warning line")

    sink.close()

    # Nothing from stdlib logging became a NorthMax record.
    assert op_path.read_text(encoding="utf-8") == ""
    assert log.health.degraded is False

    captured = stream.getvalue()
    # The warning reached the platform-captured channel; the DEBUG was dropped.
    assert "a foreign warning line" in captured
    assert "SECRET_MARKER_x" not in captured


def test_root_handlers_include_no_authoritative_sink(tmp_path, clean_root_logger):
    sink = JsonlSink(tmp_path / "op.jsonl")
    try:
        install(io.StringIO())
        for handler in clean_root_logger.handlers:
            assert not isinstance(handler, (JsonlSink, JsonlAuditSink))
            # And it is a real logging.Handler, unlike a NorthMax sink.
            assert isinstance(handler, logging.Handler)
    finally:
        sink.close()


def test_a_northmax_record_still_reaches_its_own_sink(tmp_path, clean_root_logger):
    # Containment must not disturb the NorthMax path itself.
    op_path = tmp_path / "op.jsonl"
    sink = JsonlSink(op_path)
    registry = EventRegistry()
    registry.register(EventSchema("thing.happened", severity=Severity.INFO))
    log = OperationalLog("exampleapp", registry, sink, clock=FrozenClock())

    install(io.StringIO())
    log.info("thing.happened")
    sink.close()

    lines = read_lines(op_path)
    assert len(lines) == 1
    assert '"event":"thing.happened"' in lines[0]
