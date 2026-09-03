"""Safe exception evidence (§13.1).

A genuine unexpected failure produces evidence, not an execution trace. Evidence
is the exception type and its code locations, plus an exception's own message
*only* when the exception explicitly marks it safe. The renderer never inspects a
message and decides it looks safe (invariant 4).

What is deliberately never produced here (§13.1):

- frame-local dumps: the traceback is walked for file/line/function only; frame
  locals are never read;
- argument values or object representations: no ``repr()`` of anything;
- ``str(exc)`` of a foreign exception: a message appears only behind the marker.

There is no free-form ``description`` and no caller ``context`` (R3c): a schema-
declared field genuinely is "application-defined and controlled" per §13.1 while
a free string is not, and §20 places messages with the event definition. Safe
event-specific context therefore travels through normal declared event fields via
the schema guard, not through this diagnostic structure. ``Evidence`` is a
standalone diagnostic object; it is not a ``FieldValue`` and is not part of the
authoritative record model — adding serialised evidence to records, if ever
wanted, is a separate future decision.

The same rule is applied at every level of the ``__cause__`` and ``__context__``
chains, so a foreign exception wrapped inside a controlled one keeps its foreign
message excluded. This module depends on nothing else in the package, so evidence
stays independent of the event layer and can never raise a schema error on a
failure path.

The safe-message marker is a structural convention (settled decision 6): a class
attribute named by ``SAFE_MESSAGE_ATTR``, set to exactly ``True``. nm-logging's
own exceptions set it (see exceptions.py); other packages may adopt it without
importing this one.
"""

from collections import deque
from dataclasses import dataclass

# The attribute an exception sets to opt its message into evidence. Matched by
# value against exceptions.NmLoggingError.log_safe_message; not imported
# there, so the marker remains a dependency-free structural convention.
SAFE_MESSAGE_ATTR = "log_safe_message"

MAX_CHAIN_DEPTH = 10
"""Maximum number of exceptions recorded from a cause/context chain. Fixed, not
configurable: a chain is bounded evidence, not a data structure to reproduce in
full, and an unbounded walk would let a pathological chain inflate a record
without limit."""

MAX_TRACEBACK_FRAMES = 20
"""Maximum code locations recorded per exception. Fixed for the same reason as
MAX_CHAIN_DEPTH: a runaway recursion must not turn one failure into an unbounded
record. The innermost frames are kept, being nearest the failure."""


@dataclass(frozen=True, slots=True)
class CodeLocation:
    """One frame of a traceback: file, line, and function name only.

    No source text and no frame locals — a code location is where the failure
    was, not what was in scope there (§13.1).
    """

    file: str
    line: int
    function: str


@dataclass(frozen=True, slots=True)
class ExceptionEvidence:
    """Safe evidence for a single exception in the chain.

    ``message`` is present only when the exception carried the safe-message
    marker; for any foreign exception it is None. ``relation`` is None for the
    raised exception and ``"cause"`` or ``"context"`` for a chained one.
    """

    exception_type: str
    message: str | None
    locations: tuple[CodeLocation, ...]
    relation: str | None = None


@dataclass(frozen=True, slots=True)
class Evidence:
    """The full safe evidence for a failure: the raised exception followed by its
    walked cause/context chain. ``truncated`` is True if the chain hit the depth
    cap and further exceptions were not recorded."""

    chain: tuple[ExceptionEvidence, ...]
    truncated: bool = False


def _message_is_safe(exc: BaseException) -> bool:
    # Exactly True, not merely truthy: an unrelated truthy attribute must not be
    # mistaken for an opt-in.
    return getattr(exc, SAFE_MESSAGE_ATTR, False) is True


def _safe_message(exc: BaseException) -> str | None:
    if not _message_is_safe(exc):
        return None
    try:
        return str(exc)
    except Exception:
        # Evidence is built on a failure path and must never itself raise; a
        # broken __str__ simply yields no message.
        return None


def _locations(exc: BaseException) -> tuple[CodeLocation, ...]:
    # A traceback runs outermost -> innermost, so a sliding window of the last
    # MAX_TRACEBACK_FRAMES keeps the *innermost* frames — nearest the failure, the
    # most diagnostic, and what this module documents (Finding 6). ``deque`` bounds
    # storage to the cap even for a very deep chain: earlier frames fall off the
    # left. f_locals is never touched.
    window: deque[CodeLocation] = deque(maxlen=MAX_TRACEBACK_FRAMES)
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        window.append(
            CodeLocation(file=code.co_filename, line=tb.tb_lineno, function=code.co_name)
        )
        tb = tb.tb_next
    return tuple(window)


def _evidence_for(exc: BaseException, relation: str | None) -> ExceptionEvidence:
    return ExceptionEvidence(
        exception_type=type(exc).__qualname__,
        message=_safe_message(exc),
        locations=_locations(exc),
        relation=relation,
    )


def build_evidence(exc: BaseException) -> Evidence:
    """Build safe evidence for ``exc`` and its cause/context chain.

    Each exception contributes its type, code locations, and — behind the marker —
    its message, nothing more. The walk visits each exception once (cycles are
    tolerated) and stops after MAX_CHAIN_DEPTH exceptions, setting ``truncated``.
    """
    entries: list[ExceptionEvidence] = []
    visited: set[int] = set()
    truncated = False

    queue: deque[tuple[BaseException, str | None]] = deque()
    queue.append((exc, None))

    while queue:
        current, relation = queue.popleft()
        if id(current) in visited:
            continue
        if len(entries) >= MAX_CHAIN_DEPTH:
            truncated = True
            break
        visited.add(id(current))
        entries.append(_evidence_for(current, relation))
        # Walk both links (§13.1): __cause__ (explicit ``raise ... from``) and
        # __context__ (implicit during handling). A suppressed context is still
        # walked — its message is withheld by the marker rule regardless, so
        # walking it can only omit, never leak.
        for related, rel in ((current.__cause__, "cause"), (current.__context__, "context")):
            if related is not None and id(related) not in visited:
                queue.append((related, rel))

    return Evidence(chain=tuple(entries), truncated=truncated)
