"""Audit is append-only from the application's perspective (§15).

The library exposes no edit, no per-record delete, and no clear-history operation
on the audit surface — not even privately. Retention is the only removal
mechanism (out of scope for v0.1). This test guards that by name across the audit
facade, the audit operation handle, the concrete audit sink, and the AuditSink
protocol, including their private attributes.
"""

import nm_logging
from nm_logging import AuditLog, AuditOperation, AuditSink, JsonlAuditSink

# Verbs that would imply mutation or removal of already-written audit history.
_MUTATION_VERBS = (
    "delete",
    "remove",
    "clear",
    "truncate",
    "purge",
    "drop",
    "erase",
    "wipe",
    "edit",
    "update",
    "overwrite",
    "rewrite",
    "destroy",
)


def _offending_names(obj) -> list[str]:
    # All attributes, including private ones (§15: "not even privately"), except
    # Python dunders, which are not part of this package's surface.
    names = [n for n in dir(obj) if not (n.startswith("__") and n.endswith("__"))]
    return [n for n in names if any(verb in n.lower() for verb in _MUTATION_VERBS)]


def test_audit_log_has_no_mutation_api():
    assert _offending_names(AuditLog) == []


def test_audit_operation_has_no_mutation_api():
    assert _offending_names(AuditOperation) == []


def test_audit_sink_has_no_mutation_api():
    assert _offending_names(JsonlAuditSink) == []


def test_audit_sink_protocol_is_append_only():
    # The protocol offers append/close/supports_atomic and nothing that mutates.
    members = {n for n in dir(AuditSink) if not (n.startswith("__") and n.endswith("__"))}
    assert "append" in members
    assert _offending_names(AuditSink) == []


def test_public_surface_has_no_mutation_export():
    offenders = [
        name
        for name in nm_logging.__all__
        if any(verb in name.lower() for verb in _MUTATION_VERBS)
    ]
    assert offenders == []


def test_audit_sink_exposes_no_seek_or_write_over_history():
    # Belt and braces: no random-access primitives that would let history be
    # rewritten in place.
    for forbidden in ("seek", "write", "insert", "replace"):
        assert not hasattr(JsonlAuditSink, forbidden)
