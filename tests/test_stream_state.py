"""Unit A — shared per-file stream state, the write-completion loop, the latch,
directory durability, descriptor hygiene, and cross-kind rejection (R2, §4).

The completion loop turns one write into several, which is why it ships with
shared locking (interleaving), the directory fix (durability), and cross-kind
rejection (an operational partial write must not latch an audit stream). These
tests exercise the coordinated behaviour.
"""

import io
import json
import os
import threading

import pytest

from nm_logging import (
    AuditPersistenceError,
    EventRegistry,
    EventSchema,
    JsonlAuditSink,
    JsonlSink,
    LoggingConfigurationError,
    OperationalLog,
    Severity,
    SinkError,
)
from nm_logging.sinks import _streams
from nm_logging.sinks.jsonl import encode
from nm_logging.sinks.stderr import StderrFallback

from .helpers import FrozenClock, make_audit, make_operational, read_lines

OSERROR = "OSERROR"  # sentinel for a planned os.write raising OSError


def _fake_write(plan, real_write):
    """A fake ``os.write`` driven by a plan of per-call outcomes.

    Each entry is an int count (that many bytes are actually written and
    returned), the string ``"rest"`` (write and return all remaining bytes), or
    ``OSERROR`` (raise OSError). Actually writing the returned count keeps the
    file's bytes truthful, so a completion assertion means what it says. An empty
    plan raises — proving no unexpected write occurred.
    """
    plan = list(plan)

    def fake(fd, data):
        if not plan:
            raise AssertionError("os.write called more times than planned")
        step = plan.pop(0)
        if step == OSERROR:
            raise OSError("mock write failure")
        n = len(data) if step == "rest" else min(step, len(data))
        real_write(fd, data[:n])
        return n

    return fake


def _fsync_spy(real_fsync):
    calls: list[int] = []

    def spy(fd):
        calls.append(fd)
        return real_fsync(fd)

    return spy, calls


# --- R2: the completion loop --------------------------------------------------


def test_short_positive_returns_complete_the_write(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)  # constructed before patching (real dir fsync)
    record = make_audit()
    expected = encode(record) + "\n"
    real_write = os.write
    fsync_spy, fsync_calls = _fsync_spy(os.fsync)
    # 1 byte, then 4, then the rest — a positive return is progress, not failure.
    monkeypatch.setattr(os, "write", _fake_write([1, 4, "rest"], real_write))
    monkeypatch.setattr(os, "fsync", fsync_spy)
    try:
        sink.append(record)
    finally:
        monkeypatch.undo()
        sink.close()
    assert path.read_text(encoding="utf-8") == expected
    assert fsync_calls, "audit append must fsync the file after a complete write"


def test_every_audit_append_fsyncs_the_file(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)  # constructed first, so its dir fsync is unspied
    fsync_spy, fsync_calls = _fsync_spy(os.fsync)
    monkeypatch.setattr(os, "fsync", fsync_spy)
    try:
        for _ in range(3):
            sink.append(make_audit())
        assert len(fsync_calls) == 3  # one file fsync per append
    finally:
        monkeypatch.undo()
        sink.close()


def test_audit_partial_write_latches(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    real_write = os.write
    monkeypatch.setattr(os, "write", _fake_write([1, 0], real_write))
    with pytest.raises(AuditPersistenceError):
        sink.append(make_audit())
    assert sink._state.latched is True
    monkeypatch.undo()
    sink.close()


def test_audit_oserror_mid_write_latches(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    real_write = os.write
    monkeypatch.setattr(os, "write", _fake_write([1, OSERROR], real_write))
    with pytest.raises(AuditPersistenceError):
        sink.append(make_audit())
    assert sink._state.latched is True
    monkeypatch.undo()
    sink.close()


def test_zero_byte_write_fails_without_latching(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    real_write = os.write
    monkeypatch.setattr(os, "write", _fake_write([0], real_write))
    with pytest.raises(AuditPersistenceError):
        sink.append(make_audit())
    assert sink._state.latched is False  # framing intact
    monkeypatch.undo()
    # A later append on the same stream succeeds once the mock is removed.
    sink.append(make_audit())
    sink.close()
    assert len(read_lines(path)) == 1


def test_oserror_first_call_fails_without_latching(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)
    real_write = os.write
    monkeypatch.setattr(os, "write", _fake_write([OSERROR], real_write))
    with pytest.raises(AuditPersistenceError):
        sink.append(make_audit())
    assert sink._state.latched is False
    monkeypatch.undo()
    sink.append(make_audit())
    sink.close()
    assert len(read_lines(path)) == 1


def test_audit_fsync_failure_after_complete_write_does_not_latch(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path)

    def failing_fsync(fd):
        raise OSError("mock fsync failure")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(AuditPersistenceError):
        sink.append(make_audit())
    # Complete framing: nothing latched, though durability failed.
    assert sink._state.latched is False
    monkeypatch.undo()
    sink.close()


def test_latched_stream_refuses_further_appends_without_touching_file(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    a1 = JsonlAuditSink(path)
    a2 = JsonlAuditSink(path)
    assert a1._state is a2._state  # same shared state
    real_write = os.write
    monkeypatch.setattr(os, "write", _fake_write([1, 0], real_write))
    with pytest.raises(AuditPersistenceError):
        a1.append(make_audit())  # partial -> latch
    monkeypatch.undo()
    size_after_latch = path.stat().st_size
    # a2 shares the latch and refuses without touching the file (the write path
    # is never reached, so no os.write occurs).
    with pytest.raises(AuditPersistenceError):
        a2.append(make_audit())
    assert path.stat().st_size == size_after_latch
    a1.close()
    a2.close()


# --- R2: operational stays fail-open -----------------------------------------


def test_operational_partial_write_fails_open(tmp_path, monkeypatch):
    path = tmp_path / "op.jsonl"
    sink = JsonlSink(path)
    registry = EventRegistry()
    registry.register(EventSchema("thing.happened", severity=Severity.INFO))
    buf = io.StringIO()
    log = OperationalLog(
        "exampleapp", registry, sink, clock=FrozenClock(), fallback=StderrFallback(buf)
    )
    real_write = os.write
    monkeypatch.setattr(os, "write", _fake_write([1, 0], real_write))
    # info() returns normally despite the partial write; health degrades; latched.
    assert log.info("thing.happened") is None
    assert log.health.degraded is True
    assert sink._state.latched is True
    assert len(buf.getvalue().splitlines()) == 1  # exactly one bounded fallback line
    monkeypatch.undo()
    # A subsequent info() also returns normally (latched -> contained fail-open).
    assert log.info("thing.happened") is None
    sink.close()


# --- §4.7: shared state, concurrency, identity -------------------------------


def test_two_instances_share_lock_and_latch(tmp_path):
    path = tmp_path / "audit.jsonl"
    a, b = JsonlAuditSink(path), JsonlAuditSink(path)
    try:
        assert a._state is b._state
        assert a._state.lock is b._state.lock
    finally:
        a.close()
        b.close()


def _concurrent_appends(sink_factory, append, tmp_path, filename):
    path = tmp_path / filename
    sinks = [sink_factory(path) for _ in range(4)]
    per_thread = 40
    errors: list[BaseException] = []

    def worker(sink):
        try:
            for _ in range(per_thread):
                append(sink)
        except BaseException as exc:  # pragma: no cover - surfaced via errors
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(s,)) for s in sinks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for s in sinks:
        s.close()
    assert errors == []
    lines = read_lines(path)
    assert len(lines) == 4 * per_thread
    for line in lines:
        parsed = json.loads(line)  # every line is a complete, valid JSON object
        assert isinstance(parsed, dict)


def test_concurrent_audit_appends_never_interleave(tmp_path):
    _concurrent_appends(JsonlAuditSink, lambda s: s.append(make_audit()), tmp_path, "audit.jsonl")


def test_concurrent_operational_appends_never_interleave(tmp_path):
    _concurrent_appends(JsonlSink, lambda s: s.write(make_operational()), tmp_path, "op.jsonl")


def test_file_replacement_gets_a_different_identity(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    first = JsonlAuditSink(path)
    # Latch the first identity via a partial write.
    real_write = os.write
    monkeypatch.setattr(os, "write", _fake_write([1, 0], real_write))
    with pytest.raises(AuditPersistenceError):
        first.append(make_audit())
    monkeypatch.undo()
    assert first._state.latched is True
    # Replace the file with a new inode.
    os.remove(path)
    second = JsonlAuditSink(path)
    try:
        assert second._state is not first._state  # different identity
        assert second._state.latched is False  # the old latch did not carry over
        second.append(make_audit())  # the fresh stream works
    finally:
        first.close()
        second.close()
    assert first._state.latched is True  # old identity stays latched


def test_construction_against_a_latched_identity_succeeds_but_appends_refused(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    a = JsonlAuditSink(path)
    real_write = os.write
    monkeypatch.setattr(os, "write", _fake_write([1, 0], real_write))
    with pytest.raises(AuditPersistenceError):
        a.append(make_audit())
    monkeypatch.undo()
    # A new sink on the still-latched identity constructs fine; its appends are
    # refused. Construction stays free of write-path semantics.
    b = JsonlAuditSink(path)
    try:
        with pytest.raises(AuditPersistenceError):
            b.append(make_audit())
    finally:
        a.close()
        b.close()


def test_close_during_concurrent_append_does_not_interleave(tmp_path):
    path = tmp_path / "audit.jsonl"
    a, b = JsonlAuditSink(path), JsonlAuditSink(path)
    stop = threading.Event()
    errors: list[BaseException] = []

    def appender():
        try:
            while not stop.is_set():
                a.append(make_audit())
        except AuditPersistenceError:
            pass  # a.close() may land first; that is fine
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    t = threading.Thread(target=appender)
    t.start()
    b.close()  # closes only b's descriptor, under the shared lock
    stop.set()
    t.join()
    a.close()
    assert errors == []
    for line in read_lines(path):
        assert isinstance(json.loads(line), dict)


# --- §4.4: cross-kind rejection ----------------------------------------------


def test_cross_kind_rejected_operational_then_audit(tmp_path):
    path = tmp_path / "shared.jsonl"
    op = JsonlSink(path)
    try:
        with pytest.raises(LoggingConfigurationError):
            JsonlAuditSink(path)
    finally:
        op.close()


def test_cross_kind_rejected_audit_then_operational(tmp_path):
    path = tmp_path / "shared.jsonl"
    au = JsonlAuditSink(path)
    try:
        with pytest.raises(LoggingConfigurationError):
            JsonlSink(path)
    finally:
        au.close()


def test_cross_kind_rejected_through_symlink_alias(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text("", encoding="utf-8")
    link = tmp_path / "link.jsonl"
    os.symlink(real, link)
    op = JsonlSink(real)
    try:
        with pytest.raises(LoggingConfigurationError):
            JsonlAuditSink(link)  # same inode via the symlink
    finally:
        op.close()


def test_cross_kind_rejected_through_hardlink_alias(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text("", encoding="utf-8")
    hard = tmp_path / "hard.jsonl"
    os.link(real, hard)
    op = JsonlSink(real)
    try:
        with pytest.raises(LoggingConfigurationError):
            JsonlAuditSink(hard)
    finally:
        op.close()


def test_same_kind_on_one_identity_is_allowed(tmp_path):
    path = tmp_path / "audit.jsonl"
    a, b = JsonlAuditSink(path), JsonlAuditSink(path)
    a.close()
    b.close()
    op1, op2 = JsonlSink(tmp_path / "op.jsonl"), JsonlSink(tmp_path / "op.jsonl")
    op1.close()
    op2.close()


# --- §4.2: descriptor hygiene ------------------------------------------------


def _open_close_spies(monkeypatch):
    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close = os.open, os.close

    def spy_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def spy_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "close", spy_close)
    return opened, closed


def test_descriptor_closed_on_directory_fsync_failure(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    opened, closed = _open_close_spies(monkeypatch)

    def failing_fsync(fd):
        raise OSError("mock dir fsync failure")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(LoggingConfigurationError):
        JsonlAuditSink(path)
    # Every descriptor opened during the failed construction was closed.
    assert set(opened) <= set(closed), (opened, closed)


def test_descriptor_closed_on_cross_kind_rejection(tmp_path, monkeypatch):
    path = tmp_path / "shared.jsonl"
    op = JsonlSink(path)  # establishes the operational kind for this identity
    try:
        opened, closed = _open_close_spies(monkeypatch)
        with pytest.raises(LoggingConfigurationError):
            JsonlAuditSink(path)  # cross-kind -> rejected after os.open
        assert set(opened) <= set(closed), (opened, closed)
    finally:
        op.close()


# --- §4.2: directory durability at construction ------------------------------


def test_directory_is_fsynced_at_construction_before_any_append(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    fsync_spy, fsync_calls = _fsync_spy(os.fsync)
    monkeypatch.setattr(os, "fsync", fsync_spy)
    sink = JsonlAuditSink(path)
    try:
        # A directory fsync happened during construction, before any append.
        assert len(fsync_calls) >= 1
    finally:
        monkeypatch.undo()
        sink.close()


def test_second_instance_does_not_owe_the_first_directory_fsync(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    fsync_spy, fsync_calls = _fsync_spy(os.fsync)
    monkeypatch.setattr(os, "fsync", fsync_spy)
    a = JsonlAuditSink(path)  # non-existent path -> creates it, dir fsync #1
    b = JsonlAuditSink(path)  # now-existing -> dir fsync #2 (unconditional)
    try:
        # Both constructors fsynced the directory before any append, so an append
        # through B is never the first event to make the directory entry durable.
        assert len(fsync_calls) >= 2
        appends_before = len(fsync_calls)
        b.append(make_audit())
        assert len(fsync_calls) > appends_before  # the append adds a file fsync
    finally:
        monkeypatch.undo()
        a.close()
        b.close()


def test_directory_fsync_failure_at_construction_yields_no_usable_sink(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"

    def failing_fsync(fd):
        raise OSError("mock dir fsync failure")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(LoggingConfigurationError):
        JsonlAuditSink(path)
