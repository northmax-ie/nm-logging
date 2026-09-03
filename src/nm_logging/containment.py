"""Foreign/runtime logging containment (§25).

Python standard-library, framework, and third-party libraries log through their
own facilities and severity vocabularies. Those sources must not cross the
NorthMax emit boundary into authoritative operational or audit storage: a
third-party DEBUG line must not become a NorthMax record, and an arbitrary
foreign message must not bypass the secret and privacy protections (§13.1, §25).

nm_logging enforces this **at its own boundary**, structurally: it ships no
``logging.Handler`` at all, so nm-logging exposes no route by which root, stdlib,
or third-party logging can enter authoritative NorthMax storage — no NorthMax
sink can be attached to a logger, because none is a ``logging.Handler``.

This is a guarantee about nm-logging's own surface, not about the whole process.
It cannot, and does not claim to, stop a consuming application or unrelated code
from independently misconfiguring Python logging, adding its own handlers, or
writing directly to the same files. What it guarantees is that nothing nm-logging
provides is such a route.

``install`` makes the intended configuration explicit rather than inherited from
Python's defaults: foreign logs go to stderr for independent platform capture
(§13.3), and records below a threshold are dropped rather than emitted. It never
attaches a NorthMax sink — there is none to attach — and it touches only stdlib
logging, never the NorthMax write path.

A safe adapter that turned third-party WARNING/ERROR into a declared
package-owned event (logger name, level, and exception type only — never the
foreign message, which is unsafe by §13.1) is deliberately left out of scope
here; §25 makes it optional, and this module is containment, not adaptation.
"""

import logging
import sys
from typing import TextIO

# Marks the handler this module installs, so install is idempotent and uninstall
# removes only what it added.
_INSTALLED_MARKER = "_nm_logging_containment_handler"

DROP_BELOW = logging.WARNING
"""Foreign records below this level (DEBUG and INFO) are dropped from the
platform-captured channel. Fixed rather than configurable: the channel exists to
surface foreign warnings and errors for platform capture, not to reproduce
third-party debug/trace volume, which the NorthMax model excludes (§4, §25). It
governs only the stderr channel; NorthMax records never flow through here."""


def install(stream: TextIO | None = None, *, level: int = DROP_BELOW) -> logging.Handler:
    """Route stdlib/third-party logging to a platform-captured stderr channel,
    dropping records below ``level``, and never into NorthMax storage.

    ``level`` is applied to the installed handler only. This function does not
    change the root logger's global level or any other handler: it adds one
    handler and configures that. Idempotent: a second call reuses the handler it
    installed rather than stacking duplicates. Returns that handler for
    inspection. ``stream`` defaults to the live ``sys.stderr`` resolved now; pass
    a stream to capture it in a test.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, _INSTALLED_MARKER, False):
            handler.setLevel(level)
            return handler
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setLevel(level)
    setattr(handler, _INSTALLED_MARKER, True)
    # Only the handler is configured; the root logger's level is left untouched.
    root.addHandler(handler)
    return handler


def uninstall() -> None:
    """Remove the handler ``install`` added, if present. Leaves any other handlers
    the application configured untouched."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _INSTALLED_MARKER, False):
            root.removeHandler(handler)
            handler.close()


def is_installed() -> bool:
    """Whether the containment handler is currently attached to the root logger."""
    root = logging.getLogger()
    return any(getattr(h, _INSTALLED_MARKER, False) for h in root.handlers)
