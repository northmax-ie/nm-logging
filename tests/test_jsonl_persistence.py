"""The JSONL operational sink: byte-stable output and append framing (§22).

Pins the produced format byte for byte under a frozen clock, so a writer-format
change cannot slip past
a permissive reader. Also covers one-record-per-line framing and the sink's
record-size enforcement. (The torn-final-line case is the reader's contract and
is tested with the reader in M6.)
"""

from datetime import UTC, datetime

import pytest

from nm_logging import (
    MAX_RECORD_BYTES,
    Category,
    EventSchemaError,
    JsonlSink,
    Severity,
    Stage,
)

# Record classes are off the top-level surface (R1); built here via the
# implementation module for direct-serialisation vectors.
from nm_logging.record import AuditRecord, OperationalRecord
from nm_logging.sinks.jsonl import encode

from .helpers import read_lines

FROZEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_operational_record_byte_vector():
    record = OperationalRecord(
        application="exampleapp",
        emitter="app",
        event="update.run.completed",
        timestamp=FROZEN,
        severity=Severity.INFO,
        fields={"eligible": 5, "updated": 5},
        message="update run completed eligible=5 updated=5",
    )
    assert encode(record) == (
        '{"schema_version":1,"timestamp":"2026-01-02T03:04:05+00:00",'
        '"application":"exampleapp","emitter":"app","event":"update.run.completed",'
        '"severity":"INFO","eligible":5,"updated":5,'
        '"message":"update run completed eligible=5 updated=5"}'
    )


def test_audit_record_byte_vector():
    record = AuditRecord(
        application="exampleapp",
        emitter="app",
        event="user.created",
        timestamp=FROZEN,
        category=Category.ACTIVITY,
        actor="alice",
        stage=Stage.COMPLETE,
        fields={"target": "widget-7"},
    )
    assert encode(record) == (
        '{"schema_version":1,"timestamp":"2026-01-02T03:04:05+00:00",'
        '"application":"exampleapp","emitter":"app","event":"user.created",'
        '"category":"ACTIVITY","actor":"alice","stage":"complete",'
        '"target":"widget-7"}'
    )


def test_absent_optional_fields_are_absent_in_output():
    # No message, no event fields: the mapping stops at the envelope + severity.
    record = OperationalRecord(
        application="exampleapp",
        emitter="app",
        event="a.b",
        timestamp=FROZEN,
        severity=Severity.WARNING,
    )
    assert encode(record) == (
        '{"schema_version":1,"timestamp":"2026-01-02T03:04:05+00:00",'
        '"application":"exampleapp","emitter":"app","event":"a.b","severity":"WARNING"}'
    )


def test_non_ascii_is_written_as_utf8_not_escaped():
    record = OperationalRecord(
        application="exampleapp",
        emitter="app",
        event="a.b",
        timestamp=FROZEN,
        severity=Severity.INFO,
        fields={"name": "Ölandsråd"},
    )
    assert '"name":"Ölandsråd"' in encode(record)
    assert "\\u" not in encode(record)


def test_writes_one_record_per_line(tmp_path):
    path = tmp_path / "op.jsonl"
    sink = JsonlSink(path)
    try:
        for i in range(3):
            sink.write(
                OperationalRecord(
                    application="exampleapp",
                    emitter="app",
                    event="a.b",
                    timestamp=FROZEN,
                    severity=Severity.INFO,
                    fields={"n": i},
                )
            )
    finally:
        sink.close()
    lines = read_lines(path)
    assert len(lines) == 3
    assert lines[0] == encode(
        OperationalRecord(
            application="exampleapp",
            emitter="app",
            event="a.b",
            timestamp=FROZEN,
            severity=Severity.INFO,
            fields={"n": 0},
        )
    )


def test_oversize_record_is_rejected_and_not_written(tmp_path):
    path = tmp_path / "op.jsonl"
    sink = JsonlSink(path)
    # Built directly, bypassing the field guard, to exceed the encoded limit.
    huge = OperationalRecord(
        application="exampleapp",
        emitter="app",
        event="a.b",
        timestamp=FROZEN,
        severity=Severity.INFO,
        fields={"blob": "x" * (MAX_RECORD_BYTES + 100)},
    )
    try:
        with pytest.raises(EventSchemaError):
            sink.write(huge)
        # Nothing was written: the file is empty.
        assert path.read_text(encoding="utf-8") == ""
    finally:
        sink.close()


def test_unusable_target_raises_sink_error_at_construction(tmp_path):
    from nm_logging import SinkError

    # A directory path cannot be opened for writing as a file.
    with pytest.raises(SinkError):
        JsonlSink(tmp_path)


# --- reader round-trip and torn-line handling (§22, §15) ------------------

from nm_logging.reader.jsonl import JsonlReader  # noqa: E402


def _write(sink, **fields):
    sink.write(
        OperationalRecord(
            application="exampleapp",
            emitter="app",
            event="a.b",
            timestamp=FROZEN,
            severity=Severity.INFO,
            fields=fields,
        )
    )


def test_reader_round_trips_written_records(tmp_path):
    path = tmp_path / "op.jsonl"
    sink = JsonlSink(path)
    try:
        _write(sink, n=1)
        _write(sink, n=2)
    finally:
        sink.close()
    reader = JsonlReader(path)
    records = list(reader)
    assert [r["n"] for r in records] == [1, 2]
    assert records[0]["event"] == "a.b"
    assert records[0]["severity"] == "INFO"
    assert reader.truncated is False


def test_reader_on_missing_file_is_empty(tmp_path):
    reader = JsonlReader(tmp_path / "does-not-exist.jsonl")
    assert list(reader) == []
    assert reader.truncated is False


def test_reader_on_empty_file_is_empty(tmp_path):
    path = tmp_path / "op.jsonl"
    path.write_text("", encoding="utf-8")
    reader = JsonlReader(path)
    assert list(reader) == []
    assert reader.truncated is False


def test_reader_tolerates_a_torn_final_line(tmp_path):
    # A complete record, then a partial line with no terminating newline: the
    # residue of a crash mid-append.
    path = tmp_path / "op.jsonl"
    path.write_text('{"event":"a.b","n":1}\n{"event":"a.b","n":2', encoding="utf-8")
    reader = JsonlReader(path)
    records = list(reader)
    assert [r["n"] for r in records] == [1]  # only the complete record
    assert reader.truncated is True


def test_reader_raises_on_a_corrupt_framed_final_line(tmp_path):
    # A newline-terminated line that does not parse is corruption, not a torn
    # tail — even as the final record. It must raise, not truncate.
    from nm_logging import ReaderError

    path = tmp_path / "op.jsonl"
    path.write_text('{"event":"a.b","n":1}\nnot json at all\n', encoding="utf-8")
    reader = JsonlReader(path)
    with pytest.raises(ReaderError):
        list(reader)


def test_reader_raises_on_a_corrupt_middle_line(tmp_path):
    # Malformed JSON before the final line must raise too, not be swallowed.
    from nm_logging import ReaderError

    path = tmp_path / "op.jsonl"
    path.write_text(
        '{"event":"a.b","n":1}\nnot json\n{"event":"a.b","n":3}\n', encoding="utf-8"
    )
    with pytest.raises(ReaderError):
        list(JsonlReader(path))


def test_reader_never_rewrites_the_file_on_a_torn_tail(tmp_path):
    # A torn final line must not be repaired (§15).
    path = tmp_path / "op.jsonl"
    original = '{"event":"a.b","n":1}\n{"event":"a.b","n":2'
    path.write_text(original, encoding="utf-8")
    list(JsonlReader(path))
    assert path.read_text(encoding="utf-8") == original


def test_reader_never_rewrites_the_file_on_corruption(tmp_path):
    from nm_logging import ReaderError

    path = tmp_path / "op.jsonl"
    original = '{"event":"a.b","n":1}\nnot json\n'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ReaderError):
        list(JsonlReader(path))
    assert path.read_text(encoding="utf-8") == original


# --- R6: torn tail and nothing else ---------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        b"\n",  # a blank line is not a record
        b"[]\n",  # valid JSON, not an object
        b'"str"\n',  # valid JSON string
        b"123\n",  # valid JSON number
        b"null\n",  # valid JSON null
        b"{malformed\n",  # malformed JSON
    ],
)
def test_reader_rejects_a_framed_non_object_or_malformed_line(tmp_path, payload):
    from nm_logging import ReaderError

    path = tmp_path / "op.jsonl"
    path.write_bytes(payload)
    with pytest.raises(ReaderError):
        list(JsonlReader(path))
    assert path.read_bytes() == payload  # never repaired


def test_reader_rejects_invalid_utf8(tmp_path):
    from nm_logging import ReaderError

    path = tmp_path / "op.jsonl"
    original = b"\xff\xfe not utf-8\n"
    path.write_bytes(original)
    with pytest.raises(ReaderError):
        list(JsonlReader(path))
    assert path.read_bytes() == original


def test_reader_error_carries_position_but_no_content(tmp_path):
    from nm_logging import ReaderError

    path = tmp_path / "op.jsonl"
    # A malformed second line whose text would be secret-bearing if echoed.
    path.write_bytes(b'{"event":"a.b"}\n{"tok":Bearer_secret_value}\n')
    try:
        list(JsonlReader(path))
    except ReaderError as exc:
        message = str(exc)
        assert "line 2" in message
        assert "byte offset" in message
        assert "Bearer_secret_value" not in message  # content never echoed
        assert "tok" not in message
    else:
        raise AssertionError("expected ReaderError")


def test_reader_error_on_a_middle_line_stops_and_never_repairs(tmp_path):
    from nm_logging import ReaderError

    path = tmp_path / "op.jsonl"
    original = b'{"event":"a.b"}\n[]\n{"event":"a.b"}\n'  # non-object in the middle
    path.write_bytes(original)
    with pytest.raises(ReaderError):
        list(JsonlReader(path))
    assert path.read_bytes() == original
