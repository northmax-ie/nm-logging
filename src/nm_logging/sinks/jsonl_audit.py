"""The audit append sink with an explicit durability contract (§9, §23, §4).

Audit fails hard: ``append`` returns only once the record is durably on disk, and
raises ``AuditPersistenceError`` otherwise so the caller does not proceed with a
mutation whose intent was never recorded (§9.3). Durability here means:

- the record and its terminating newline are written as one buffer under the
  shared per-file stream lock, looping until every byte is written (§4, R2), so a
  transient short write completes instead of damaging framing;
- the file is ``fsync``-ed after a complete write, before return;
- the containing directory was ``fsync``-ed at construction (below), because the
  file's *existence* is not durable otherwise.

If only a prefix of a record is written, framing is damaged: the stream latches
and refuses further appends, so a torn tail never becomes middle-of-file
corruption. A latched audit stream blocks every subsequent audited mutation —
fail-hard.

Exactly one process may own and write a given authoritative audit stream; the
shared in-process lock serialises threads and instances within that owner. There
is no cross-process advisory lock, by decision. The effective guarantee still
depends on the filesystem, persistent volume, and storage platform beneath (§23).

Audit is append-only from the application's perspective (§15): this sink exposes
no update and no delete, by construction. It does not advertise atomicity, so an
audited local mutation on it must use the intent/outcome model (§9.3, §22).
"""

import os

from ..exceptions import AuditPersistenceError, EventSchemaError, LoggingConfigurationError
from ..record import MAX_RECORD_BYTES, AuditRecord
from . import _streams
from .jsonl import encode  # the single canonical JSON codec, shared with the operational sink


class JsonlAuditSink:
    """A durable append-only JSONL sink for audit records."""

    _KIND = _streams.KIND_AUDIT

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = os.fspath(path)
        try:
            fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        except OSError:
            # A bad audit target at construction; fail hard. The path is not echoed.
            raise AuditPersistenceError("could not open the audit sink target") from None
        # Descriptor hygiene (§4.2): any failure after os.open — fstat, directory
        # fsync, cross-kind registration — closes the descriptor before the
        # exception propagates, so a failed construction leaks nothing.
        try:
            file_identity = _streams.identity(fd)
            # Unconditional directory fsync at construction, before the sink is
            # usable (§4.2): makes the file's existence durable with no shared
            # first-create flag and therefore no publication race between
            # instances. Cheaper than creation detection and also covers a file
            # left un-synced by an earlier process.
            self._fsync_directory()
            state = _streams.register(file_identity, self._KIND)
        except OSError:
            # An fstat failure at construction means the target is unusable.
            os.close(fd)
            raise LoggingConfigurationError("could not stat the audit sink target") from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self._state = state
        self._closed = False

    @property
    def supports_atomic(self) -> bool:
        # The file backend cannot commit application state and audit in one
        # transaction (§22); callers must use intent/outcome.
        return False

    def append(self, record: AuditRecord) -> None:
        """Durably append one audit record. Returns only once the record is
        written in full and fsync-ed.

        Raises ``EventSchemaError`` for a non-audit record kind, a record that is
        not UTF-8 encodable, or a record whose encoded form exceeds the permitted
        size — contract violations, the same classification as in the operational
        sink — and ``AuditPersistenceError`` if durability cannot be established (a
        latched stream, a partial write, a write failure, or an fsync failure). In
        every case the record is not durable; raised from ``intent`` none is
        caught by the audit path, so the mutation does not proceed (§9.3)."""
        if not isinstance(record, AuditRecord):
            # Operational and audit records are stored separately (§22); the wrong
            # kind is a contract violation. The record kind name is not echoed.
            raise EventSchemaError("audit sink received a non-audit record") from None
        line = encode(record)
        try:
            data = line.encode("utf-8")
        except UnicodeEncodeError:
            # Defence in depth (R4): an unencodable record is a contract violation
            # (EventSchemaError), not a durability failure. Nothing is written, so
            # the intent is not durable and the mutation does not proceed. The
            # value is not echoed.
            raise EventSchemaError("audit record is not encodable as UTF-8") from None
        data += b"\n"
        if len(data) > MAX_RECORD_BYTES:
            # An over-limit record is a contract violation (EventSchemaError), not
            # a durability failure. Nothing is written, so the intent is not
            # durable and, propagating uncaught from intent(), the caller does not
            # proceed. Values are not echoed.
            raise EventSchemaError("audit record exceeds the maximum encoded size") from None
        with self._state.lock:
            if self._closed:
                raise AuditPersistenceError("audit sink is closed") from None
            if self._state.latched:
                # A prior partial write damaged framing; refuse without touching
                # the file. Fail-hard.
                raise AuditPersistenceError("audit stream latched after a partial write") from None
            result = _streams.write_completely(self._fd, data, do_fsync=True)
            if result == _streams.WRITE_PARTIAL:
                self._state.latched = True
            if result != _streams.WRITE_COMPLETE:
                # Partial, nothing, or fsync failure: durability not achieved. A
                # foreign OSError message could carry a path; it is suppressed.
                raise AuditPersistenceError("audit append did not achieve durability") from None

    def _fsync_directory(self) -> None:
        directory = os.path.dirname(self._path) or "."
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            raise LoggingConfigurationError(
                "could not open the audit directory for durability"
            ) from None
        try:
            os.fsync(dir_fd)
        except OSError:
            raise LoggingConfigurationError(
                "could not fsync the audit directory"
            ) from None
        finally:
            os.close(dir_fd)

    def close(self) -> None:
        # Under the shared lock, so close cannot race an append; closes only this
        # instance's descriptor. The shared stream state (including any latch)
        # persists for the process.
        with self._state.lock:
            if not self._closed:
                self._closed = True
                try:
                    os.close(self._fd)
                except OSError:
                    raise AuditPersistenceError("closing the audit sink failed") from None
