"""Shared per-file stream state, keyed by file identity (§4 of the remediation).

The object to protect is the *file*, not the path. A symlink, hardlink, or bind
mount gives one file two names; path keying would miss that, and a path-keyed
reset could clear protection for an instance still holding a descriptor on the
damaged inode. So state is keyed by ``(st_dev, st_ino)`` taken from ``os.fstat``
after ``os.open``.

Each entry carries:

- ``lock`` — serialises the latch check, the write completion loop, and the fsync
  as one critical section, and serialises ``close`` against them; shared by every
  sink instance holding that file identity, so two writers cannot interleave.
- ``latched`` — once a *partial* record has been written, framing is damaged;
  further authoritative appends are refused so a torn tail never becomes
  middle-of-file corruption. Shared, so a second instance cannot keep appending.
- ``stream_kind`` — ``operational`` or ``audit``, fixed at first registration.
  Operational and audit records are stored separately (§22); a cross-kind
  collision on one identity is rejected, so an operational partial write can
  never latch an audit stream.

The latch is a property of the file and shared; its *consequence* is not — audit
fails hard (`AuditPersistenceError`), operational fails open (`SinkError`). The
two taxonomies are not unified merely because they share this registry.

Entries are never removed (rotation is deferred, §26; removal must never drop a
latched entry). A latched file stays latched for the process; recovery is a
restart or replacing the file with a new identity, then reconstructing the sink.
"""

import os
import threading

from ..exceptions import LoggingConfigurationError

KIND_OPERATIONAL = "operational"
KIND_AUDIT = "audit"


class _StreamState:
    """Mutable shared state for one file identity. ``latched`` is guarded by
    ``lock``; it is only ever set True, never cleared within the process."""

    __slots__ = ("lock", "latched", "stream_kind")

    def __init__(self, stream_kind: str) -> None:
        self.lock = threading.Lock()
        self.latched = False
        self.stream_kind = stream_kind


_STREAMS: dict[tuple[int, int], _StreamState] = {}
_GUARD = threading.Lock()


def identity(fd: int) -> tuple[int, int]:
    """The ``(st_dev, st_ino)`` identity of an open descriptor. Raises OSError if
    the descriptor cannot be stat-ed; the caller closes the descriptor."""
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino)


def register(file_identity: tuple[int, int], stream_kind: str) -> _StreamState:
    """Return the shared state for ``file_identity``, creating it on first sight.

    Raises ``LoggingConfigurationError`` if a sink of a different kind is already
    registered for this identity (§22, §4.4): operational and audit records must
    not share a file, and a shared latch would otherwise let an operational
    partial write fail-hard an audit stream. Two sinks of the same kind share the
    entry — that is the intended shared-lock case.
    """
    with _GUARD:
        state = _STREAMS.get(file_identity)
        if state is None:
            state = _StreamState(stream_kind)
            _STREAMS[file_identity] = state
            return state
        if state.stream_kind != stream_kind:
            # Neither the path nor the kinds are echoed.
            raise LoggingConfigurationError(
                "a sink of a different kind is already registered for this file identity"
            ) from None
        return state


# Results of a write attempt. The caller maps these to its own failure contract.
WRITE_COMPLETE = "complete"
WRITE_PARTIAL = "partial"        # 0 < bytes_written < len(buffer): latch
WRITE_NOTHING = "nothing"        # zero bytes written: framing intact, no latch
WRITE_SYNC_FAILED = "sync_failed"  # complete write, fsync failed: no latch


def write_completely(fd: int, data: bytes, *, do_fsync: bool) -> str:
    """Write ``data`` in full with a completion loop, optionally fsync, and report
    the outcome. Must be called under the stream lock.

    A positive ``os.write`` return is progress and the loop continues; zero,
    negative, or ``OSError`` ends the attempt. POSIX reports a partial write as a
    short return rather than an error, so the running count is accurate even when
    a later call raises. Returns:

    - ``WRITE_COMPLETE`` — every byte written (and fsync succeeded, if requested);
    - ``WRITE_PARTIAL`` — some but not all bytes written (framing damaged: latch);
    - ``WRITE_NOTHING`` — nothing written (framing intact: do not latch);
    - ``WRITE_SYNC_FAILED`` — all bytes written but fsync failed (framing intact,
      no latch; still a durability failure for a caller that requested fsync).
    """
    total = len(data)
    written = 0
    while written < total:
        try:
            n = os.write(fd, data[written:])
        except OSError:
            break
        if n <= 0:
            break
        written += n
    if written != total:
        return WRITE_PARTIAL if written > 0 else WRITE_NOTHING
    if do_fsync:
        try:
            os.fsync(fd)
        except OSError:
            return WRITE_SYNC_FAILED
    return WRITE_COMPLETE
