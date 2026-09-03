"""A JSONL reader (§22).

Turns the line-oriented file back into records for UI and export, so callers
iterate records without knowing about lines, encodings, or paths. It yields each
record as the plain mapping (JSON object) that was written, in file order.

It tolerates a torn tail and nothing else (R6, §15/M6). Reading is byte-oriented
and split on the newline terminator, so the *only* truncation signal is a final
physical line without a terminating newline — the expected residue of a crash
mid-append: iteration stops cleanly and ``truncated`` is set. Every
newline-terminated line must decode as UTF-8 and parse as a JSON object; a blank
line, malformed JSON, a valid non-object (``[]``, ``"s"``, ``123``, ``null``), or
a decoding failure is corruption and raises ``ReaderError`` — anywhere in the
file, the final record included. A silently skipped framed line in audit history
would be the worst case, so nothing is skipped.

``ReaderError`` carries a line number and byte offset and never the line content,
which is untrusted and may be secret-bearing. The authoritative file is never
repaired, rewritten, or truncated.

Apart from that one exception type, this module depends on nothing else in the
package — not the record model — so the persistence backend and the reader can
evolve independently.
"""

import json
import os
from collections.abc import Iterator, Mapping

from ..exceptions import ReaderError


class JsonlReader:
    """Iterates the JSON-object records in a JSONL file, tolerating a torn tail.

    A missing file reads as empty (a log not yet written to is not an error).
    ``truncated`` is meaningful after iteration: it is True only if the final
    physical line was unterminated (a torn tail). Any framed line that is not a
    JSON object — blank, malformed, non-object, or undecodable — raises
    ``ReaderError``. Re-iterating re-reads the file and recomputes ``truncated``.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = os.fspath(path)
        self._truncated = False

    @property
    def truncated(self) -> bool:
        return self._truncated

    def __iter__(self) -> Iterator[Mapping[str, object]]:
        self._truncated = False
        try:
            handle = open(self._path, "rb")
        except FileNotFoundError:
            return
        with handle:
            offset = 0
            lineno = 0
            # Binary iteration splits on b"\n" only (no universal-newline
            # translation) and yields an unterminated final chunk as the last
            # item — exactly the torn-tail signal.
            for raw in handle:
                lineno += 1
                line_start = offset
                offset += len(raw)
                if not raw.endswith(b"\n"):
                    # An unterminated final physical line is a torn tail. Stop and
                    # report; never yield a half-written record.
                    if raw:
                        self._truncated = True
                    return
                body = raw[:-1]
                try:
                    text = body.decode("utf-8")
                except UnicodeDecodeError:
                    raise ReaderError(
                        f"undecodable record at line {lineno}, byte offset {line_start}"
                    ) from None
                try:
                    record = json.loads(text)
                except ValueError:
                    # Malformed JSON, or a blank line (empty is not valid JSON):
                    # corruption, not a torn tail. Position only; no content.
                    raise ReaderError(
                        f"corrupt record at line {lineno}, byte offset {line_start}"
                    ) from None
                if not isinstance(record, dict):
                    # The interface promises mappings; a valid non-object ([], a
                    # string, a number, null) is corruption of an authoritative
                    # stream, not a record.
                    raise ReaderError(
                        f"non-object record at line {lineno}, byte offset {line_start}"
                    ) from None
                yield record
