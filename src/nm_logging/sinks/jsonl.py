"""The operational append sink: one JSON record per line (§22).

JSON encoding lives here, not above the sink (§22): ``encode`` turns a record's
canonical mapping into the exact wire line, and the sink appends it. The encoding
is fixed so output is byte-stable — ``ensure_ascii=False`` (UTF-8, no \\uXXXX
noise), ``separators=(",", ":")`` (no incidental whitespace), and
``allow_nan=False`` so a NaN or infinity that somehow reached here fails loudly
rather than emitting invalid JSON.

The record and its terminating newline are written as one buffer under the shared
per-file stream lock, looping until every byte is written (§4, R2). A transient
short write completes rather than damaging framing. If only a prefix is written,
the stream latches and refuses further appends, so a torn tail never becomes
middle-of-file corruption. All failures here are fail-open (§14.1): they raise
``SinkError`` into the operational path's containment. It adds no queue or
batching; queueing is deferred (§26). It does not fsync: operational logging has
no durability contract (§14.1); durability belongs to the audit sink.

The stream state is keyed by file identity and shared with every sink on that
file, and a cross-kind collision with an audit sink is rejected at construction
(§4.4).
"""

import json
import os

from ..exceptions import EventSchemaError, SinkError
from ..record import MAX_RECORD_BYTES, OperationalRecord, Record
from ..serialise import to_mapping
from . import _streams


def encode(record: Record) -> str:
    """Return the canonical one-line JSON encoding of ``record`` (no newline).

    Kind-agnostic and single-sourced, so the operational sink, the audit sink, and
    the byte-stable vector test share one codec.
    """
    return json.dumps(
        to_mapping(record),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


class JsonlSink:
    """Appends operational records as JSON lines to a file.

    ``write`` raises ``EventSchemaError`` for a non-operational record kind, a
    record whose encoded form exceeds ``MAX_RECORD_BYTES``, or one that is not
    UTF-8 encodable — producer defects the operational path contains as schema
    defects (§14.4) — and ``SinkError`` for a write failure, which the operational
    path treats as fail-open degradation (§14.1). The two are kept distinct so
    those behaviours never merge. This sink is for operational records only; audit
    records go to ``JsonlAuditSink`` (§22).
    """

    _KIND = _streams.KIND_OPERATIONAL

    def __init__(self, path: str | os.PathLike[str]) -> None:
        # O_APPEND so every write lands at end-of-file; O_CREAT so a fresh log
        # starts cleanly. The open itself is where an unusable target surfaces.
        try:
            fd = os.open(os.fspath(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        except OSError:
            # A bad sink target at construction. The path is not echoed.
            raise SinkError("could not open the operational sink target") from None
        # Descriptor hygiene (§4.2): any failure after os.open closes the
        # descriptor before propagating, so a failed construction leaks nothing.
        try:
            file_identity = _streams.identity(fd)
            state = _streams.register(file_identity, self._KIND)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self._state = state
        self._closed = False

    def write(self, record: Record) -> None:
        if not isinstance(record, OperationalRecord):
            # Operational and audit records are stored separately (§22); the wrong
            # kind is a producer defect. The kind name is not echoed.
            raise EventSchemaError("operational sink received a non-operational record") from None
        data = _encode_line_bytes(record)
        with self._state.lock:
            if self._closed:
                raise SinkError("operational sink is closed") from None
            if self._state.latched:
                # A prior partial write damaged framing; refuse without touching
                # the file. Fail-open: the operational path contains this.
                raise SinkError("operational stream latched after a partial write") from None
            result = _streams.write_completely(self._fd, data, do_fsync=False)
            if result == _streams.WRITE_PARTIAL:
                self._state.latched = True
            if result != _streams.WRITE_COMPLETE:
                # A foreign OSError message could carry a path; it is suppressed.
                raise SinkError("operational sink write failed") from None

    def close(self) -> None:
        # Under the shared lock, so close cannot race an append; closes only this
        # instance's descriptor. Not a durability mechanism, not left to __del__.
        with self._state.lock:
            if not self._closed:
                self._closed = True
                try:
                    os.close(self._fd)
                except OSError:
                    raise SinkError("closing the operational sink failed") from None


def _encode_line_bytes(record: Record) -> bytes:
    """Encode a record to its UTF-8 wire line (with newline), enforcing the size
    limit. An unencodable or over-limit record is a producer defect
    (EventSchemaError), detected before any byte is written (R1, R4)."""
    line = encode(record)
    try:
        data = line.encode("utf-8")
    except UnicodeEncodeError:
        # Defence in depth (R4): validation rejects unencodable field strings, but
        # a record reaching a sink by another route must not fail as a write
        # error. Not a SinkError; nothing is written, so nothing latches. The
        # value is not echoed.
        raise EventSchemaError("record is not encodable as UTF-8") from None
    data += b"\n"
    if len(data) > MAX_RECORD_BYTES:
        # A producer defect, not an I/O failure. Values are not echoed.
        raise EventSchemaError("record exceeds the maximum encoded size") from None
    return data
