"""Import layering: imports go downward only (CLAUDE.md "Layout and layering").

An AST-based layer check. It grows one milestone at a time: it
asserts what must hold given the modules that exist now. record.py is the bottom
of the contract layer and must not reach up to events.py; events.py must not
reach the persistence or facade layers that land later.
"""

import ast
from pathlib import Path

from nm_logging import audit as audit_module
from nm_logging import containment as containment_module
from nm_logging import events as events_module
from nm_logging import evidence as evidence_module
from nm_logging import record as record_module
from nm_logging import serialise as serialise_module
from nm_logging.sinks import jsonl as jsonl_module
from nm_logging.sinks import jsonl_audit as jsonl_audit_module


def _imported_sibling_modules(source: str) -> set[str]:
    """Top-level names of intra-package modules imported by ``source``.

    Captures ``from .x import ...`` and ``from .x.y import ...`` as ``x``, and
    ``from . import x`` as ``x``. Absolute ``import nm_logging.x`` is captured as
    ``x`` too, though the package uses relative imports throughout.
    """
    names: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level > 0 and node.module:
                names.add(node.module.split(".")[0])
            elif node.level > 0:  # from . import x
                names.update(alias.name for alias in node.names)
            elif node.module and node.module.split(".")[0] == "nm_logging":
                parts = node.module.split(".")
                if len(parts) > 1:
                    names.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "nm_logging" and len(parts) > 1:
                    names.add(parts[1])
    return names


def _source_of(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


def test_record_does_not_import_upward():
    # record.py is the contract floor: it knows nothing of events, evidence, the
    # persistence layer, or the facade.
    imported = _imported_sibling_modules(_source_of(record_module))
    forbidden = {
        "events",
        "evidence",
        "operational",
        "audit",
        "health",
        "containment",
        "sinks",
        "reader",
    }
    assert forbidden.isdisjoint(imported), imported & forbidden


def test_events_does_not_import_persistence_or_facade():
    # events.py owns the grammar and the field guard; it must not reach the
    # persistence layer or the operational/audit facades.
    imported = _imported_sibling_modules(_source_of(events_module))
    forbidden = {
        "operational",
        "audit",
        "health",
        "containment",
        "sinks",
        "reader",
        "evidence",
    }
    assert forbidden.isdisjoint(imported), imported & forbidden


def test_evidence_depends_only_downward():
    # evidence.py is a peer of events.py above record.py. It must not reach the
    # persistence or facade layers, and it stays independent of the event layer
    # (it re-implements a defensive scalar check rather than importing the field
    # guard, so it can never raise a schema error on a failure path).
    imported = _imported_sibling_modules(_source_of(evidence_module))
    forbidden = {
        "events",
        "operational",
        "audit",
        "health",
        "containment",
        "sinks",
        "reader",
    }
    assert forbidden.isdisjoint(imported), imported & forbidden


def test_serialise_depends_only_on_record():
    # serialise.py turns a record into a mapping; it must not know about sinks,
    # files, or the facade.
    imported = _imported_sibling_modules(_source_of(serialise_module))
    forbidden = {
        "events",
        "evidence",
        "operational",
        "audit",
        "health",
        "containment",
        "sinks",
        "reader",
    }
    assert forbidden.isdisjoint(imported), imported & forbidden


def test_jsonl_sink_does_not_import_the_facade():
    # A sink knows nothing about OperationalLog or AuditLog (CLAUDE.md layering);
    # it may import the serialise/record contract below it.
    imported = _imported_sibling_modules(_source_of(jsonl_module))
    forbidden = {"operational", "audit", "health", "reader", "containment", "events"}
    assert forbidden.isdisjoint(imported), imported & forbidden


def test_audit_facade_does_not_import_a_concrete_sink_or_peer_facade():
    # AuditLog takes an AuditSink via its constructor; it must not import a
    # concrete sink, the operational facade, or the reader.
    imported = _imported_sibling_modules(_source_of(audit_module))
    forbidden = {"operational", "sinks", "health", "reader", "containment"}
    assert forbidden.isdisjoint(imported), imported & forbidden


def test_audit_sink_does_not_import_the_facade():
    imported = _imported_sibling_modules(_source_of(jsonl_audit_module))
    forbidden = {"operational", "audit", "health", "reader", "containment", "events"}
    assert forbidden.isdisjoint(imported), imported & forbidden


def test_containment_imports_nothing_from_the_package():
    # Containment touches only stdlib logging; it must not reach into the NorthMax
    # write path, so foreign-logging handling cannot become a route into storage.
    imported = _imported_sibling_modules(_source_of(containment_module))
    assert imported == set(), imported


def test_nothing_in_src_imports_the_reader():
    # The write-side contract must not depend on the reader, so the persistence
    # backend can evolve without touching it (§22, §24). Every module under src/
    # except the reader package itself is checked.
    import nm_logging

    package_root = Path(nm_logging.__file__).parent
    offenders = []
    for py_file in package_root.rglob("*.py"):
        if "reader" in py_file.relative_to(package_root).parts:
            continue  # the reader may refer to itself
        if "reader" in _imported_sibling_modules(py_file.read_text(encoding="utf-8")):
            offenders.append(str(py_file.relative_to(package_root)))
    assert offenders == [], offenders
