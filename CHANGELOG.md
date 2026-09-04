# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Package semantic versioning applies to the package independently of the logging
record contract. The record contract is defined in `docs/logging-standard.md`,
which is normative: any implementation or documentation that diverges from it is
defective. The `schema_version` carried by every record identifies that contract.
A released record-contract version is immutable; incompatible changes require a
new `schema_version`.

## [0.1.0] - 2026-09-04

### Added

- Record contract: a common envelope plus the operational and audit record
  shapes. `application`, `emitter`, `schema_version`, and the timestamp are set
  by the logging setup, never at the call site; timestamps are timezone-aware
  UTC generated internally, and naive datetimes are rejected at the boundary.
- Four fixed operational severities (INFO, WARNING, ERROR, CRITICAL). There is
  no DEBUG and no runtime verbosity control; an operational schema pins its
  severity and a call site cannot vary it.
- Event schemas as allowlists: `EventRegistry`, `EventSchema`, `FieldSpec`, the
  event-ID and field grammars, duplicate-registration and reserved-name checks,
  and the reserved `nmlogging.*` namespace for the package's own self-reports.
- `OperationalLog`: the fail-open emit path. A sink failure never propagates to
  the caller or fails the operation it describes; degradation is surfaced through
  health state. A `strict` development toggle raises on malformed calls instead
  of containing them.
- `AuditLog` and `AuditOperation`: the fail-hard emit path. Durable intent is
  recorded before a non-atomic audited mutation, intent is linked to outcome by
  an internally generated `operation_id`, and outcomes (`success`, `failure`,
  `indeterminate`) are never fabricated. The context-manager form writes
  `indeterminate` when an exception escapes the block. Audit records are
  append-only: no edit, no per-record delete, no history clearing.
- Audit vocabulary: `Category` (ADMIN, ACTIVITY), `Stage`, and `Outcome`.
  Non-accountable actors are rejected (empty/whitespace, a case-insensitive
  denylist, and the `ENC[...]` envelope form).
- Safe exception evidence: the exception type and code locations only, with the
  exception's own message rendered solely behind the structural safe-message
  marker. Foreign exception messages, caller context, and object dumps are never
  rendered, including through `__cause__` and `__context__` chains.
- Secret containment: values shaped like an encrypted envelope (`ENC[...]`) are
  rejected in fields and in `actor` rather than passed through, on failure and
  exception paths as well as success paths. Rejected input is never echoed into
  a defect record.
- Storage-independent persistence: a JSONL operational sink, a JSONL audit sink
  with an explicit durability contract (directory fsync on construction), and a
  bounded, non-recursive stderr fallback channel. One authoritative writer per
  stream, with in-process serialisation keyed by file identity.
- Reader abstraction: `JsonlReader` reads records back through the reader
  interface rather than the file layout. A torn final line is tolerated and
  reported; a fully framed but unparseable line raises.
- Containment of third-party logging: no `nm_logging` sink is installed as a
  `logging.Handler` on a shared or root logger, so standard-library and
  third-party logging cannot reach authoritative storage.
- Syslog severity mapping exposed for a future forwarder.
- `NmLoggingError` exception hierarchy; messages never echo untrusted or
  unbounded input.
- Ships `py.typed`; no required runtime dependencies (standard library only).
- `docs/logging-standard.md` (normative record contract) and `docs/usage.md`
  (how to consume the library).
