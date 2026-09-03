"""Shared fixtures.

Kept small in M1: the record model has no I/O and few collaborators. The frozen
clock is provided as a fixture so later milestones that build records through a
facade can reuse it.
"""

import pytest

from .helpers import FrozenClock


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture(autouse=True)
def _reset_stream_registry():
    """Isolate the module-level per-file stream registry between tests.

    Production never clears it (§4: entries are never removed, a latch persists
    for the process). Tests must not inherit another test's latch, and pytest may
    reuse an inode across temp files within a run, so the registry is cleared
    around each test. This is a test-isolation measure only; no production reset
    exists.
    """
    from nm_logging.sinks import _streams

    _streams._STREAMS.clear()
    yield
    _streams._STREAMS.clear()
