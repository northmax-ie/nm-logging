# nm-logging

Structured operational and audit logging for NorthMax applications.

`nm-logging` owns three things: the record contract, the enforcement of that
contract, and the persistence interface. It does not own any application's event
catalogue - applications declare their own events against it.

The core has no required runtime dependencies and imports on the standard library
alone, because a logging package that can fail to import is a bad logging package.
An optional backend (a database sink, say) would arrive as an opt-in extra that a
default `pip install nm-logging` never pulls.

## Install

```bash
pip install nm-logging
```

Requires Python 3.11+. The package ships type information (`py.typed`).

## Quick start

Events are an allowlist. Each schema declares its fields and their types up
front, so a malformed schema is caught at registration, not at 3 a.m.

```python
from nm_logging import (
    EventRegistry, EventSchema, FieldSpec, Severity,
    OperationalLog, JsonlSink,
)

registry = EventRegistry()
registry.register(EventSchema(
    "widget.assembly.completed",
    severity=Severity.INFO,
    fields=(FieldSpec("assembled", int, required=True),),
    message_template="widget assembly completed assembled={assembled}",
))

log = OperationalLog("exampleapp", registry, JsonlSink("/var/log/exampleapp/operational.jsonl"))
log.info("widget.assembly.completed", assembled=5)
```

Audit logging records who did what, with what outcome, and links intent to
outcome through an internally generated id:

```python
from nm_logging import AuditLog, Category
from nm_logging.sinks.jsonl_audit import JsonlAuditSink

registry.register(EventSchema(
    "config.changed",
    category=Category.ADMIN,
    fields=(FieldSpec("setting", str, required=True),),
))

audit = AuditLog("exampleapp", registry, JsonlAuditSink("/var/log/exampleapp/audit.jsonl"))

with audit.operation("config.changed", actor="alice", setting="retention_days") as op:
    apply_config_change()
    op.success()
```

## Guarantees

The rules below are enforced, not advisory. Each exists because a log that can
be wrong or can leak is worse than no log.

- **Four severities, no DEBUG, no verbosity control.** Severity is a closed set;
  an operational event pins its own severity and a call site cannot vary it.
- **Secret material never enters a record.** Values shaped like an encrypted
  envelope (`ENC[...]`) are rejected rather than passed through, on failure paths
  as well as success paths.
- **Only declared fields are emitted.** There is no `extra=`, no `**context`
  catch-all, no object dumping. Schemas are allowlists.
- **Foreign exception messages are never rendered.** Evidence is the exception
  type and code locations; a message is included only when it carries a
  structural safe-message marker.
- **Audit fails hard; operational fails open.** An operational sink failure never
  propagates to the caller or fails the operation it describes. An audit mutation
  cannot proceed unless durable intent was recorded first, and outcomes are never
  fabricated.
- **Audit records are append-only.** No edit, no per-record delete, no history
  clearing - retention is the only removal mechanism.

## Documentation

- [docs/usage.md](docs/usage.md) - how to consume the library
- [docs/logging-standard.md](docs/logging-standard.md) - normative logging contract

`docs/logging-standard.md` is normative. Any implementation or documentation that
diverges from it is defective.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

## License

MIT - see [LICENSE](LICENSE).
