"""The independent, platform-captured fallback channel (§14.2, §13.3).

When the operational sink fails, its failure must be reported somewhere that does
not depend on the thing that just failed. That somewhere is stderr, captured by
the surrounding container/platform independently of application-owned storage.

This channel is deliberately minimal and bounded. It carries a short, safe line —
never a record's fields, never secret material — because the write path reaches
it precisely when the structured sink is unavailable. It is rate-limited: after
``MAX_LINES_PER_WINDOW`` lines within ``WINDOW_SECONDS`` it emits one summary
line and then goes quiet until the window resets, so a persistently failing sink
cannot turn into unbounded stderr volume. It never calls back into the logging
package or the failing sink, and it can never re-enter itself.
"""

import sys
from typing import Callable, TextIO

MAX_LINES_PER_WINDOW = 5
"""Maximum fallback lines emitted per window before suppression. Fixed, not
configurable: the fallback exists to make degradation visible, not to reproduce
the lost log, and an unbounded fallback would defeat the whole point of failing
open quietly."""

WINDOW_SECONDS = 60.0
"""The rate-limit window. Fixed for the same reason as MAX_LINES_PER_WINDOW."""

_PREFIX = "nm-logging: "


class StderrFallback:
    """A bounded, self-contained fallback writer.

    The monotonic clock is injectable for testing; it defaults to
    ``time.monotonic``. The stream defaults to the live ``sys.stderr`` resolved
    at emit time, so a test capturing stderr still sees the output.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        monotonic: Callable[[], float] | None = None,
        max_lines: int = MAX_LINES_PER_WINDOW,
        window_seconds: float = WINDOW_SECONDS,
    ) -> None:
        if monotonic is None:
            import time

            monotonic = time.monotonic
        self._stream = stream
        self._monotonic = monotonic
        self._max_lines = max_lines
        self._window_seconds = window_seconds
        self._window_start = monotonic()
        self._count = 0
        self._suppressing = False
        self._reentrant = False

    def emit(self, text: str) -> None:
        """Emit one bounded, safe fallback line. Never raises; never re-enters.

        ``text`` must already be safe — a short description of the degradation,
        with no record fields or secret material. This method does not inspect or
        sanitise it beyond stripping newlines that would break line framing.
        """
        if self._reentrant:
            # A stream whose write triggered another emit must not recurse.
            return
        self._reentrant = True
        try:
            if not self._allow():
                return
            safe = text.replace("\r", " ").replace("\n", " ")
            self._write(_PREFIX + safe)
        finally:
            self._reentrant = False

    def _allow(self) -> bool:
        now = self._monotonic()
        if now - self._window_start >= self._window_seconds:
            # New window: reset the budget and clear suppression.
            self._window_start = now
            self._count = 0
            self._suppressing = False
        if self._count < self._max_lines:
            self._count += 1
            return True
        if not self._suppressing:
            # Cross the threshold once: emit a single summary, then go quiet.
            self._suppressing = True
            self._write(
                _PREFIX
                + f"further fallback messages suppressed for up to {int(self._window_seconds)}s"
            )
        return False

    def _write(self, line: str) -> None:
        stream = self._stream if self._stream is not None else sys.stderr
        try:
            stream.write(line + "\n")
            stream.flush()
        except Exception:
            # The fallback is the last resort; if even stderr fails, there is
            # nowhere left to report and it must not raise into the write path.
            pass
