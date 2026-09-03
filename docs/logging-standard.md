# NorthMax Application Logging — Working Baseline

## 1. Purpose and scope

This document defines the working baseline for how NorthMax applications log.

The design is being developed initially through NorthMax Enclave, but it is
intentionally application-independent. The same principles must remain usable
for:

- full interactive applications such as Enclave;
- autonomous/background services;
- scheduled workers;
- applications using shared NorthMax application frameworks;
- future NorthMax applications with unrelated functional purposes.

The objective is consistency of semantics and behaviour across NorthMax
applications. Individual applications define the events relevant to their own
functionality; they do not redefine the logging model.

This remains a working baseline rather than a final normative specification.
Implementation details may evolve, but the principles below are considered
settled unless later requirements demonstrate a genuine need to change them.

## 2. Governing principle

Every piece of logging must earn its existence.

Logging exists to preserve operationally or accountably meaningful facts.

It does not exist to provide execution traces, dump application state
indiscriminately, compensate for poor diagnostics with volume, or expose
implementation details merely because the underlying framework makes doing so
easy.

A related implementation principle is:

NorthMax applications log through the shared logging package. Applications do
not individually invent storage formats, logging semantics, secret handling,
audit mechanisms, or persistence behaviour.

## 3. Operational logging

NorthMax applications use a fixed operational severity vocabulary:

INFO / WARNING / ERROR / CRITICAL

There is no runtime-selectable application log level.

Applications may naturally use some severities more frequently than others.
Availability of a severity does not create an obligation to manufacture events
for it.

### 3.1 INFO

INFO records meaningful expected application activity or a meaningful normal
state transition.

Typical uses include autonomous/background work where the log provides the
historical timeline that would otherwise not exist.

Examples:

```
INFO update run completed eligible=5 updated=5
INFO reconciliation completed processed=1842 changed=17
INFO CUCM connectivity restored
```

An event qualifying as INFO does not automatically mean it must be emitted. Very
frequent expected no-op activity may provide no useful information and may
therefore be omitted.

### 3.2 WARNING

WARNING records an abnormal or degraded condition where the affected capability
can still operate meaningfully.

Examples include degraded performance, approaching limits, or conditions
requiring attention without actual loss of the affected capability.

Noise must not be controlled by falsifying severity. An unchanged warning
condition should not necessarily be emitted repeatedly merely because a scheduler
runs again.

### 3.3 ERROR

ERROR records failure of an operation or capability while the application as a
whole remains meaningfully operational.

Examples:

```
ERROR CUCM connectivity lost
ERROR IPTrade integration unavailable
ERROR scheduled operation aborted due to unexpected exception
```

A configured and expected optional capability is still a real capability. If it
becomes unavailable, that capability has failed and ERROR is appropriate.

A capability deliberately disabled or not configured in a deployment is not
considered unavailable or failed. It is outside expected operation and normally
produces no failure event.

Recovery does not retroactively downgrade a genuine failure. An ERROR may later
be followed by an INFO recovery event.

### 3.4 CRITICAL

CRITICAL records a condition where the application can no longer safely or
meaningfully perform its intended core function and intervention is required.

Examples may include:

- unusable encryption material preventing access to required configuration;
- loss of essential application state or storage;
- audit persistence failure where the resulting policy prevents the application
  from performing its core mutable functionality.

CRITICAL is about the application's intended function, not merely whether the
process remains technically alive.

### 3.5 Severity applies to logical outcomes

Severity applies to the meaningful logical operation or capability, not every
internal attempt.

Expected retry behaviour that ultimately succeeds normally remains INFO.

For example:

- First connection attempt fails
- Retry succeeds within normal retry policy
- Overall synchronization completes successfully

The successful logical operation remains INFO.

Resilience should not make a healthy application appear unhealthy merely because
internal retry machinery performed its job.

## 4. DEBUG is not part of the NorthMax application logging model

NorthMax applications do not expose a DEBUG severity or runtime diagnostic
verbosity control.

In particular, production logs are not used for:

```
entered function X
calling function Y
returned from function Y
leaving function X
```

Execution tracing, interactive debugging, profiling, runtime inspection and
equivalent development facilities are separate concerns.

The governing rule is:

Operational logs describe meaningful application behaviour, not program
execution.

If a future concrete operational requirement demonstrates that additional
diagnostic instrumentation is necessary, the standard may be revisited. DEBUG
does not exist merely because the underlying programming language provides it.

## 5. Audit logging

Audit is independent from operational severity.

It does not represent another logging level.

NorthMax currently defines two audit categories:

ADMIN
ACTIVITY

### 5.1 ADMIN

ADMIN records accountable changes to the application's own administrative state.

Examples include:

- configuration changes;
- credential replacement;
- trust/certificate changes;
- permissions or role changes;
- logging/retention configuration changes.

The ADMIN audit record is the authoritative accountable change record for such
changes.

A separate parallel configuration-history mechanism must not duplicate the same
fact.

### 5.2 ACTIVITY

ACTIVITY records consequential actions performed through the application where
attribution to an accountable actor materially adds information.

Example:

```
actor=alice
event=user.created
target=ChatGPT
extension=6666
phone_model=Cisco8852
```

The important question is:

Would identifying the actor materially add information beyond the operational
fact?

If yes, audit is warranted.

If no, operational logging is sufficient.

## 6. Expected intent versus accountable intent

This distinction is a central part of the NorthMax logging model.

An autonomous service such as AutoUpdate exists specifically to discover and
update devices.

Therefore:

```
INFO update run completed discovered=5 updated=5
```

is sufficient.

Adding:

```
ACTIVITY actor=system ...
```

provides no useful accountability and is therefore unnecessary.

Conversely, Enclave does not independently decide to create a user. If alice
instructs Enclave to create a user, attribution materially matters.

Therefore that is ACTIVITY audit.

The working rule is:

Expected autonomous application intent is operational.

Actor-created intent is audited where accountability materially adds
information.

This remains true across asynchronous execution.

If an actor creates a schedule, creation or alteration of that schedule may be
audited. Subsequent autonomous executions of the established schedule are
expected application behaviour and normally become operational INFO.

The standard does not manufacture fake accountability through actor=system.

## 7. State change alone does not imply audit

The rule is not:

persistent change = audit

Autonomous applications may exist specifically to change state.

Audit is driven by meaningful accountability, not merely by whether something was
written.

Likewise, no general secondary rule such as "security-critical changes are always
audit" is introduced.

If an autonomous security-related event later demonstrates a genuine audit
requirement, that event may be handled explicitly. The standard will not create a
broad subjective security-criticality taxonomy without demonstrated need.

Initial autonomous generation of application cryptographic material, for example,
is expected application behaviour and may be recorded operationally:

```
INFO event=crypto.key.generated reason=initialization
```

No fake accountable actor is required.

The baseline does not currently require indefinite forensic retention of this
event. If proving cryptographic key genesis later becomes a requirement, that is
treated as a retention requirement rather than a reason to create actorless audit
records.

## 8. Audit and operational records may coexist

Audit and operational logging answer different questions.

An auditable action may also produce an independent operational record where each
provides materially different information.

For example:

```
ACTIVITY actor=alice event=user.delete outcome=failure
ERROR CUCM user deletion failed ...
```

The audit record answers:

Who attempted what, and with what outcome?

The operational record answers:

What failed technically?

Successful audit records are not automatically duplicated into INFO.

Each record must independently earn its existence.

## 9. Audit durability and mutation consistency

Audit integrity is mandatory.

### 9.1 Audit execution boundary

Audit requirements begin once an authenticated and authorized mutation request
has passed validation and reached the point where the application is prepared to
execute it.

Malformed input or pre-execution validation failures need not create audit noise.

### 9.2 Atomic mutations

Where the mutation and its audit record can genuinely be committed as one atomic
transaction, they should be.

Conceptually:

```
BEGIN
  change application state
  persist audit record
COMMIT
```

Either both succeed or neither does.

This applies only where the chosen persistence mechanisms genuinely provide a
shared atomic transaction boundary.

Application ownership of both pieces of state does not, by itself, imply
atomicity.

### 9.3 Non-atomic audited mutations

Whenever atomicity is unavailable, the audit model becomes:

```
authorize + validate
        ↓
durably record audit intent
        ↓
perform mutation
        ↓
append audit outcome
```

This applies regardless of why atomicity is unavailable.

Examples include:

- mutation of an external system such as CUCM;
- local application state stored separately from append-oriented audit files;
- any other backend combination that cannot guarantee a single atomic commit.

Failure to durably persist the intent prevents execution of the mutation.

Intent and outcome are separate append-only records linked through a unique audit
operation identifier.

Outcomes distinguish:

- SUCCESS — the intended effect is known to have occurred.
- FAILURE — the intended effect is known not to have occurred.
- INDETERMINATE — the application cannot establish whether the side effect
  occurred.

An intent with no outcome represents an incomplete audit operation and remains
visible for reconciliation.

The application must not misrepresent a completed or possibly completed mutation
merely because audit finalisation subsequently failed.

A future persistence backend that permits genuine atomic state-plus-audit commits
may use the simpler atomic model where appropriate.

## 10. Failed authentication

Failed authentication is operational rather than audit.

Before successful authentication, there is no established accountable actor, only
a claimed identity and/or source.

One mistyped password may warrant no record at all.

A materially abnormal authentication state such as throttling being engaged may
warrant an operational WARNING.

Failed authentication does not become ACTIVITY merely because it is
security-related.

## 11. Secret material

Secret material must never enter operational or audit logging.

This prohibition is absolute and applies to:

- all operational severities;
- all audit categories;
- normal paths;
- failure and exception paths;
- any future diagnostic facilities.

Secret material includes, but is not limited to:

- passwords;
- PINs;
- API tokens;
- bearer tokens;
- session secrets and cookies;
- private keys;
- encryption keys;
- bootstrap/recovery tokens;
- authorization headers;
- credential-bearing URLs;
- equivalent authentication or cryptographic material.

Encrypted secret envelopes such as ENC[...] are also excluded.

The fact that material is currently encrypted does not create logging value, and
retained ciphertext may become sensitive if associated keys are later
compromised.

Credential audit records record the fact of a credential change, never its value.

## 12. Data minimisation and privacy

The absence of secret material does not automatically make data suitable for
logging.

NorthMax applications log only the minimum information materially necessary to
establish the operational or accountability fact represented by an event.

Personal data may be logged where it genuinely serves that purpose.

For example, audit actor identity may be essential because the purpose of audit
is accountability.

The standard does not pseudonymise or mask identity where doing so would defeat
the accountability requirement.

Preferred practice is to use the least descriptive identifier sufficient for the
event.

Free-form content, arbitrary request/response bodies, notes, descriptions,
headers, user-controlled text and whole application objects are not logging
context by default.

Event schemas operate effectively as allowlists.

Structural allowlisting controls which fields may be emitted. It does not
automatically make the contents of an allowed field safe.

A field capable of carrying free-form or user-controlled content remains an
explicit residual responsibility. Such fields are prohibited by default unless
the event requirement specifically permits and constrains their use.

Log and audit access must therefore be treated as access to potentially
confidential or personal information and appropriately authenticated/authorized.

Bulk export of operational or audit history may itself be audit-worthy because
attribution to the exporting actor materially adds information.

Retention must remain proportionate to the purpose of the records.

The interaction between long audit retention and personal-data-bearing audit
records is deployment- and purpose-dependent and remains an explicit
retention/privacy design item rather than a fixed universal duration in this
baseline.

## 13. Exceptions and tracebacks

Unexpected exceptions do not reintroduce DEBUG.

A traceback associated with a genuine unexpected failure is failure evidence, not
execution tracing.

### 13.1 Safe exception evidence

NorthMax exception records may contain controlled information such as:

- exception type;
- controlled application-defined diagnostic description;
- code locations;
- safe structured event context.

They must not contain:

- frame-local dumps;
- argument values or object representations;
- arbitrary exception context dumps;
- whole request/config/header objects;
- uncontrolled foreign exception messages.

Messages are safe only when explicitly created through a NorthMax-controlled safe
exception path.

The renderer never attempts to inspect an arbitrary exception message and decide
that it "looks safe."

The same rule applies throughout exception cause and context chains.

Foreign exception messages remain unsafe even when wrapped inside a controlled
NorthMax exception.

### 13.2 Application and wrapper lifecycle evidence

Runtime entity ownership is represented explicitly through the common record
schema.

The application emits application records.

A separate wrapper may emit lifecycle evidence such as unexpected application
exit or successful restart.

The application must not pretend it emitted evidence about its own death or
birth.

### 13.3 Platform capture is the crash-evidence floor

Wrapper lifecycle evidence is emitted to stdout/stderr so the surrounding
container/platform logging facility can capture it independently of
application-owned persistence.

Safe structured lifecycle records may additionally be persisted locally
best-effort for UI convenience.

Raw stderr is not copied into application-owned logging storage.

This preserves the secret-material invariant.

There is an acknowledged diagnostic window where the process may die after useful
evidence reaches platform capture but before a safe local record has persisted.

Even following a successful restart, the UI may therefore show lifecycle evidence
without the underlying traceback.

Platform logs may still be required.

Abrupt termination outside application control cannot guarantee a final
application-owned record.

If the application cannot start sufficiently to expose its diagnostics UI,
investigation may therefore require the team operating the pod/platform.

### 13.4 Restart episodes

Repeated restart attempts must not overwrite or obscure the evidence from the
first failure of the current restart episode.

The episode resets after the application has returned to an appropriately healthy
state.

Exact reset mechanics remain implementation detail.

## 14. Operational logging failure

Operational logging and audit intentionally have different failure behaviour.

### 14.1 Operational logging fails open

Failure to persist an operational log record must not, by itself, cause the
application operation being described to fail.

Operational logging failure represents degraded observability, not automatically
failed application functionality.

### 14.2 Health state and fallback

Operational logging degradation should be represented through application health
independently of the failed sink.

Where available, a minimal safe failure record is emitted through an independent
platform-captured channel such as stderr/stdout.

The failed logging sink is not used to report its own failure.

Repeated sink failures must not create recursive or uncontrolled fallback volume.

### 14.3 Lost records

Operational records lost during sink unavailability are accepted as lost.

NorthMax does not initially introduce unbounded queues, replay journals or a
journal-of-the-journal merely to reconstruct operational history.

Recovery should be observable, but missing records are not fabricated after the
fact.

### 14.4 Logging programming errors

Malformed or invalid operational logging calls must not ordinarily break
otherwise valid production application behaviour.

The logging package should contain the failure and surface the logging defect
independently.

Development and test environments may enforce logging/schema errors more
aggressively so defects are discovered before release.

Audit retains its separate fail-hard integrity rules.

## 15. Retention and local audit integrity

Operational and audit records have separate retention policies.

Audit would normally be retained longer than operational history, although exact
default durations remain an implementation decision.

Audit is append-only from the application's perspective.

Existing audit records are not modified through normal application operation.

Unexpired audit history must not be silently discarded merely to reclaim space.

If the application can no longer persist required audit records, the
audit-failure policy applies.

Operational history may be sacrificed under exceptional storage pressure where
necessary to protect application health.

Operational logging must not be permitted to consume capacity required for
reliable audit persistence.

The normal application interface does not provide:

- editing of individual audit records;
- deletion of individual audit records;
- a generic "clear audit history" operation.

Retention is the normal removal mechanism.

The local audit stream is authoritative for application accountability.

This does not claim cryptographic immutability or protection against an
administrator with sufficient host/platform/storage privilege.

Independent remote retention may be added later if required.

## 16. Common structured record model

The NorthMax logging standard defines a backend-neutral logical record.

### 16.1 Common required envelope

Every record contains:

```
schema_version
timestamp
application
emitter
event
```

**schema_version**

Identifies the version of the NorthMax logging record contract.

**timestamp**

The event timestamp is always recorded in UTC.

There is no local-time persistence, no DST interpretation, and no
timezone-dependent event storage.

Presentation layers may convert UTC for human display if required.

Implementation must generate timezone-aware UTC timestamps and must not accept
naive local timestamps into persisted records.

**application**

Stable identifier for the consuming NorthMax application.

**emitter**

Short stable identifier for the runtime entity that emitted the record.

Initially:

```
emitter=app
```

A future lifecycle wrapper may use:

```
emitter=wrapper
```

Emitter is logging metadata supplied by the logging setup rather than selected
arbitrarily at individual event call sites.

The set of emitter values must remain small and documented and may grow only
where genuine additional runtime producers exist.

**event**

Stable machine-readable identifier describing the fact represented by the record.

The consumer application owns its event namespace.

The shared library owns the grammar and contract, not a global catalogue of every
event every NorthMax application may emit.

## 17. Operational record shape

Operational records add:

```
severity=INFO|WARNING|ERROR|CRITICAL
```

Event-specific fields are then added according to the consumer-defined event
schema.

Example:

```
schema_version=1
timestamp=...
application=autoupdate
emitter=app
event=update.run.completed
severity=INFO
eligible=5
updated=5
failed=0
```

## 18. Audit record shape

Audit records add:

```
category=ADMIN|ACTIVITY
actor
stage
```

Stage supports the audit durability model:

```
complete
intent
outcome
```

complete represents a one-record audit operation where mutation and audit can
genuinely be committed atomically.

intent represents durable accountable intent before a non-atomic audited
mutation.

outcome records the result of that mutation attempt.

operation_id is required for intent/outcome pairs.

Outcome values currently include:

```
success
failure
indeterminate
```

Target and other fields are included where meaningful to the particular event.

Fields that have no semantic meaning are absent rather than populated with fake
values such as N/A, system, or null merely to create a rectangular schema.

## 19. Event-specific fields

The common record envelope remains deliberately small.

Each consuming application defines the events it emits and the structured fields
permitted for each event.

The logging package does not provide a generic arbitrary context/object dumping
facility.

Applications do not hand whole dictionaries, request objects, configuration
objects, headers or arbitrary nested structures to the logger.

This provides:

- schema consistency;
- secret-material protection;
- data minimisation;
- stable downstream interpretation;
- protection against accidental field-name drift.

The exact mechanism through which consumers declare/register their events and
permitted fields remains implementation design.

## 20. Human-readable messages

Records may include controlled human-readable prose for UI, file-tail or operator
use.

The prose is not the machine contract.

Downstream tooling relies on the stable event identifier and structured fields
rather than parsing English text.

Human-readable messages should preferably be owned by the event
definition/shared implementation rather than assembled at call sites from
arbitrary runtime or user content.

An allowlisted message field does not make arbitrary message content safe.
User-controlled or otherwise untrusted prose remains excluded unless explicitly
permitted by the event definition.

## 21. Event namespaces

NorthMax applications own their own event namespace.

For example, Enclave may define events such as:

```
iptrade.connectivity.lost
cucm.connectivity.restored
user.created
```

AutoUpdate may independently define:

```
update.run.completed
device.update.blocked
```

The NorthMax logging package does not need knowledge of IPTrade, CUCM, device
updates or other application-specific concepts.

The library accepts consumer-defined event identifiers subject to common syntax
and structural rules.

Detailed naming/registration mechanics remain an implementation backlog item.

## 22. Local persistence — initial implementation

NorthMax logging v0.1 uses local append-oriented files as the authoritative local
persistence mechanism.

Operational and audit records are stored separately.

Structured line-oriented storage, most naturally JSONL, is the expected initial
implementation because it is:

- simple;
- machine-readable;
- append-friendly;
- inspectable with ordinary tools;
- easy to export;
- directly aligned with the structured event model.

The logical schema does not depend on JSONL.

Applications emit through the shared logging package rather than opening or
parsing logging files directly.

Likewise, application UI code should consume logging records through a
logging-reader/service abstraction rather than depending directly on file layout.

This allows the persistence backend to evolve without changing the event contract
or application emitters.

Because the initial file backend does not provide atomic transactions across
application state and audit files, local audited mutations that cannot otherwise
be committed atomically must use the intent/outcome model defined in §9.3.

## 23. Persistence guarantees

Operational and audit streams use the same logical logging standard but have
different durability requirements.

Operational persistence fails open as described earlier.

Audit persistence must satisfy the durability requirements of the audit model.

Where durable audit intent must exist before a mutation proceeds, merely
accepting a userspace buffered write is not sufficient.

The storage implementation must provide an appropriate durability guarantee
before execution proceeds.

For the initial local file implementation this will require explicit handling of
userspace and operating-system buffering, but the baseline does not prescribe a
specific function call as the abstract durability contract.

The effective guarantee must also reflect the semantics of the underlying
filesystem, persistent volume, and storage platform.

## 24. Future persistence and external sinks

A database-backed implementation is explicitly permitted as a later evolution,
potentially beginning with a local database such as SQLite where appropriate.

The v0.1 file implementation is not considered a semantic compromise.

Because the logical record contract and application API are storage-independent,
a later database backend should not require applications to redefine what their
events mean.

A database backend may permit genuinely atomic application-state-plus-audit
commits for operations that require intent/outcome under the initial file
backend.

Remote rsyslog/syslog/SIEM forwarding is outside the initial release.

Remote logging is additive and must never become an application availability
dependency.

The application must not become unusable merely because a remote syslog or SIEM
destination is unavailable.

NorthMax operational severities retain their defined semantics when exported.

In particular, NorthMax CRITICAL maps naturally to syslog CRIT rather than being
silently reinterpreted as ALERT or EMERG based on destination-specific policy.

Destination systems may independently alert on specific event identifiers where
stronger escalation is required.

## 25. Foreign/runtime logging

NorthMax's logging contract applies to authoritative application-owned logging.

Python standard-library, framework, runtime and third-party libraries may use
their own logging facilities and severity vocabularies.

Those sources must not be allowed to bypass the NorthMax emit boundary into
authoritative application operational or audit storage.

In particular:

- third-party DEBUG output must not become NorthMax DEBUG by accident;
- arbitrary third-party exception/prose output must not bypass NorthMax secret
  and privacy protections;
- framework/root logger propagation must be explicitly controlled;
- handling of safe third-party WARNING/ERROR evidence must be defined by the
  shared package or runtime integration rather than inherited accidentally from
  default Python logging configuration.

Exact routing, suppression, adaptation and platform-capture behaviour remain
implementation design.

## 26. Explicitly deferred implementation matters

The following remain implementation or later-design concerns rather than
unresolved logging philosophy:

- exact operational retention default;
- exact audit retention default;
- interaction between audit retention and personal-data-bearing records;
- key-genesis retention requirements if later required;
- storage sizing;
- file rotation strategy;
- rotation by size/time;
- compression;
- file and directory naming;
- quota/reserved-capacity mechanics;
- storage high-watermark behaviour;
- concrete event registration API;
- exact event-ID naming grammar;
- handling/routing of Python standard-library, framework and third-party loggers;
- concrete audit-file durability implementation;
- operational writer buffering/queueing strategy, if any;
- database backend design;
- rsyslog/syslog/SIEM integration;
- UI filtering/pagination implementation;
- restart-episode health-reset mechanics;
- exact wrapper implementation.

These details must conform to the principles in this document but do not
currently block the standard.

## 27. Working model — condensed

NorthMax applications use four fixed operational severities:

INFO / WARNING / ERROR / CRITICAL

There is no DEBUG severity and no runtime verbosity control.

Operational logging records meaningful application behaviour.

Audit is separate and uses:

ADMIN / ACTIVITY

Expected autonomous intent is operational.

Accountable actor-created intent is audited where attribution materially adds
information.

There is no fake actor=system audit trail.

Secret material is never logged.

Other logged data is minimised to what materially establishes the event.

Operational logging fails open.

Required audit persistence fails hard.

Where state mutation and audit can genuinely be committed atomically, they are
committed together.

Where atomicity is unavailable, audited mutations use durable intent before the
mutation and an append-only outcome afterwards.

UTC is used for every persisted event timestamp without exception.

All events use a common backend-neutral structured record envelope.

Consumers own their application event namespace.

The shared NorthMax logging package owns the logging contract, enforcement and
persistence interface.

v0.1 uses separate structured local files for operational and audit persistence.

Future database and remote logging backends remain additive evolutions rather
than changes to application logging semantics.

The specification defines how NorthMax applications log. The shared package makes
doing so the natural and consistent path.
