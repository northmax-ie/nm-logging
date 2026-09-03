# nm-logging — usage

A short tour of the v0.1 API. `docs/logging-standard.md` is normative; this file
shows how the package expresses it. Every example event here is invented — the
package holds no application's event catalogue.

Import by symbol. Never alias the package to `logging`:

```python
from nm_logging import (
    EventRegistry, EventSchema, FieldSpec, Severity, Category,
    OperationalLog, AuditLog, JsonlSink,
)
from nm_logging.sinks.jsonl_audit import JsonlAuditSink
```

## Declaring events

Events are an allowlist. Each `EventSchema` declares its fields, their types, and
which are required; a malformed schema fails at registration, not at 3 a.m. An
operational schema pins its severity; an audit schema sets its category.

```python
registry = EventRegistry()

# Operational: severity is pinned to the event.
registry.register(EventSchema(
    "widget.assembly.completed",
    severity=Severity.INFO,
    fields=(
        FieldSpec("assembled", int, required=True),
        FieldSpec("failed", int),
    ),
    message_template="widget assembly completed assembled={assembled}",
))

registry.register(EventSchema(
    "widget.line.stalled",
    severity=Severity.ERROR,
))

# Audit: a category, and an actor supplied per call.
registry.register(EventSchema(
    "config.changed",
    category=Category.ADMIN,
    fields=(FieldSpec("setting", str, required=True),),
))

registry.register(EventSchema(
    "user.deprovisioned",
    category=Category.ACTIVITY,
    fields=(
        FieldSpec("target", str, required=True),
        # A field the outcome must carry, though the intent need not:
        FieldSpec("result_code", int, required_on_outcome=True),
    ),
))
```

Field values are limited to `str`, `int`, `float`, `bool` (and, where a schema
declares `is_list=True`, a list of those). A string that looks like an encrypted
envelope (`ENC[...]`) is refused, and free-form text fields are denied unless a
schema explicitly permits them.

## Operational logging (fails open)

```python
op_sink = JsonlSink("/var/log/exampleapp/operational.jsonl")
log = OperationalLog("exampleapp", registry, op_sink)

log.info("widget.assembly.completed", assembled=5, failed=0)
log.error("widget.line.stalled")
```

The call site chooses `info` / `warning` / `error` / `critical`; the method must
match the event's pinned severity. `application`, `emitter`, `schema_version`,
and the timestamp are set by the library, never at the call site.

If a sink write fails, the call still returns: logging degrades observably rather
than failing the operation it describes. Poll health to surface it:

```python
if log.health.degraded:
    ...  # the application reads health; the write path never calls back into it
```

A malformed logging call (unknown event, wrong field, wrong severity) is a
programming defect. In production it is contained — a package-owned defect record
is written naming the event and the violation, never the offending values — and
the call returns. Construct with `strict=True` in development and tests to have
those defects raise instead.

## Audit logging (fails hard)

Audit records who did what, with what outcome, and it fails hard: if durable
intent cannot be recorded, the mutation must not proceed.

### Intent / outcome — the handle form

```python
audit_sink = JsonlAuditSink("/var/log/exampleapp/audit.jsonl")
audit = AuditLog("exampleapp", registry, audit_sink)

op = audit.intent("user.deprovisioned", actor="alice", target="user-42")
try:
    perform_deprovision()
except KnownFailure:
    op.failure(result_code=1)      # the effect did not occur
except Exception:
    op.indeterminate(result_code=-1)  # cannot establish whether it did
    raise
else:
    op.success(result_code=0)      # the effect occurred
```

`intent()` returns only once the intent is durable. The outcome need not repeat
the intent's required fields — it is linked by an internally generated
`operation_id` — but must carry any field marked `required_on_outcome`. A failure
to append the outcome raises and leaves the intent orphaned for reconciliation;
it does not retract a mutation that may already have happened, and no outcome is
ever fabricated.

### Intent / outcome — the context-manager form

```python
with audit.operation("config.changed", actor="alice", setting="retention_days") as op:
    apply_config_change()
    op.success()
```

An event that declares a `required_on_outcome` field **cannot** be used as a
context manager: the automatic `indeterminate()` supplies no fields, so it could
only fail *after* a durable intent had been written. `operation()` rejects such an
event with `AuditUsageError` **before** writing anything; use the handle form
above, which lets you supply the outcome field. (The message names the event id
and nothing else.)

What `__exit__` does, by the operation's state:

| State at exit | Exception escaping the block | Behaviour |
| --- | --- | --- |
| no outcome recorded | none | `AuditUsageError`; the intent is left orphaned (no outcome is fabricated) |
| no outcome recorded | `E` | writes `indeterminate` automatically, then re-raises `E` unchanged |
| no outcome recorded | `E`, and the automatic `indeterminate` also fails | raises `AuditFinalisationError` (see below) |
| outcome recorded | none | returns cleanly; the recorded outcome stands |
| outcome recorded | `E` | writes nothing; propagates `E`; the outcome stands |
| finalisation already failed | none | re-raises the stored finalisation failure — you **cannot** swallow it by catching it inside the block |
| finalisation already failed | a different `E` | raises `AuditFinalisationError` |

A failed finalisation is terminal: once an outcome attempt fails, no second
outcome is attempted, and a further explicit call raises `AuditUsageError`.

**`AuditFinalisationError`** means an outcome could not be established while, or
after, the block ran: a durable intent exists with no outcome. `.operation_id`
identifies it for reconciliation, `.finalisation_exception` gives the true reason
with its true type, and `.body_exception` gives whatever escaped the block.

**What to catch.** `AuditFinalisationError` deliberately does **not** subclass
`AuditPersistenceError` (it may represent a finalisation failure that was not a
persistence failure), so an existing `except AuditPersistenceError` will not
catch it. Catch it where that is the condition you mean to handle:

- `except AuditFinalisationError` — the dual-failure / orphaned-intent case;
- `except (AuditPersistenceError, AuditFinalisationError)` — to handle both a
  durability failure and a finalisation failure at one point;
- `except NmLoggingError` — only when you deliberately want the whole package
  error family (it also catches `EventSchemaError`, `ReaderError`,
  `LoggingConfigurationError`, `AuditUsageError`, and the rest), which is rarely
  what you want around a single audited mutation.

```python
try:
    with audit.operation("config.changed", actor="alice", setting="retention_days") as op:
        apply_config_change()
        op.success()
except AuditFinalisationError as exc:
    reconcile(exc.operation_id)   # an intent exists with no outcome
```

`actor` must be a real accountable identity: empty, whitespace, the placeholders
`system` / `none` / `null` / `-`, and an `ENC[...]` envelope form are all rejected.

`complete()` (a single atomic state-plus-audit record) requires a backend that
can commit both in one transaction. The v0.1 file backend cannot, so it raises;
use intent/outcome.

## Reading records back

Application UI and export code read through the reader, not the file layout, so
the backend can change without touching them. The concrete reader lives outside
the top-level surface because nothing write-side may depend on it:

```python
from nm_logging.reader.jsonl import JsonlReader

reader = JsonlReader("/var/log/exampleapp/operational.jsonl")
for record in reader:
    ...  # a plain mapping: record["event"], record["severity"], ...

if reader.truncated:
    ...  # the final line was a torn tail (a crash mid-append); records before it are intact
```

A torn final line is tolerated and reported via `truncated`; a corrupt (fully
framed but unparseable) line raises `ReaderError`. The reader never repairs or
rewrites the file — audit is append-only from the application's perspective.

## Containing third-party logging

Standard-library, framework, and third-party loggers must not reach NorthMax
storage. nm-logging exposes no route by which they can (it ships no
`logging.Handler`); `install` additionally routes them to a platform-captured
stderr channel and drops their DEBUG/INFO noise:

```python
from nm_logging.containment import install

install()  # foreign WARNING/ERROR -> stderr for platform capture; below that, dropped
```

This governs only nm-logging's own boundary. It cannot stop a consuming
application from configuring Python logging however it likes.

## Not in v0.1 — and where each attaches

These are deliberately absent, not stubbed. Because the record
contract and the facades are storage-independent, each attaches without changing
what events mean:

- **Rotation, retention, storage high-watermark** — behind the sink layer; the
  record contract and emitters are unaffected.
- **A database backend** — new `Sink` / `AuditSink` / `Reader` implementations;
  an atomic backend may additionally advertise `supports_atomic` and enable
  `complete()`.
- **Remote syslog / SIEM forwarding** — an additive consumer of the record
  contract; `SYSLOG_KEYWORD` is the severity mapping to build on. It must never
  become an availability dependency.
- **UI filtering and pagination** — the reader layer, above today's iteration.
- **The lifecycle wrapper** (unexpected-exit / restart evidence) — a separate
  producer emitting under `emitter="wrapper"`; the application never reports its
  own death.
