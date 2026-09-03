"""Operational logging fails open (§14.1–§14.3).

A sink write failure must not reach the caller: the call returns, health goes
degraded, exactly one bounded fallback line is emitted per failure, the fallback
is rate-limited, the sink is not retried, and there is no queue. Recovery is
observable on the next successful write.
"""

import io

from nm_logging import EventRegistry, EventSchema, OperationalLog, Severity
from nm_logging.sinks.stderr import MAX_LINES_PER_WINDOW, StderrFallback

from .helpers import FailingSink, FlakySink, FrozenClock

EVENT = EventSchema("thing.happened", severity=Severity.INFO)


def _log(sink, *, fallback=None, strict=False):
    registry = EventRegistry()
    registry.register(EVENT)
    return OperationalLog(
        "exampleapp",
        registry,
        sink,
        clock=FrozenClock(),
        fallback=fallback,
        strict=strict,
    )


def test_sink_failure_does_not_reach_the_caller():
    sink = FailingSink()
    log = _log(sink, fallback=StderrFallback(io.StringIO()))
    # Returns normally despite the sink raising.
    assert log.info("thing.happened") is None
    assert sink.calls == 1  # called once, not retried


def test_sink_failure_degrades_health():
    sink = FailingSink()
    log = _log(sink, fallback=StderrFallback(io.StringIO()))
    assert log.health.degraded is False
    log.info("thing.happened")
    assert log.health.degraded is True
    snap = log.health.snapshot()
    assert snap.total_failures == 1
    assert snap.last_failure_kind == "sink_write"


def test_exactly_one_fallback_line_per_failure():
    stream = io.StringIO()
    log = _log(FailingSink(), fallback=StderrFallback(stream))
    log.info("thing.happened")
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("nm-logging: ")


def test_fallback_is_rate_limited_with_a_single_summary():
    stream = io.StringIO()
    # A large window so nothing resets mid-test.
    fallback = StderrFallback(stream, max_lines=MAX_LINES_PER_WINDOW, window_seconds=10_000)
    log = _log(FailingSink(), fallback=fallback)
    for _ in range(MAX_LINES_PER_WINDOW + 10):
        log.info("thing.happened")
    lines = stream.getvalue().splitlines()
    # At most the budget plus one suppression summary; never unbounded.
    assert len(lines) == MAX_LINES_PER_WINDOW + 1
    assert "suppressed" in lines[-1]


def test_no_queue_is_kept():
    # Lost records are lost (§14.3): no buffer, no replay journal on the facade.
    log = _log(FailingSink(), fallback=StderrFallback(io.StringIO()))
    log.info("thing.happened")
    assert not hasattr(log, "_queue")
    assert not hasattr(log, "_buffer")
    assert not hasattr(log, "_journal")


def test_recovery_is_observable():
    sink = FlakySink(fail_times=1)
    log = _log(sink, fallback=StderrFallback(io.StringIO()))
    log.info("thing.happened")  # fails
    assert log.health.degraded is True
    log.info("thing.happened")  # succeeds
    assert log.health.degraded is False
    snap = log.health.snapshot()
    assert snap.total_failures == 1
    assert snap.total_recoveries == 1
    assert len(sink.records) == 1


def test_sink_failure_is_contained_even_in_strict_mode():
    # Failing open is an invariant, not a development convenience: strict mode
    # escalates schema defects, never a sink I/O failure.
    sink = FailingSink()
    log = _log(sink, fallback=StderrFallback(io.StringIO()), strict=True)
    assert log.info("thing.happened") is None
    assert log.health.degraded is True
