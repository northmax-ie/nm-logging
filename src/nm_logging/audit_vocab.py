"""Audit vocabulary: Category, Stage, Outcome (§5, §18).

Audit is independent of operational severity and is not another logging level
(§5). These three closed vocabularies are the whole of it; each value's spelling
is the canonical wire form, matching the standard's casing (category upper-case,
stage and outcome lower-case).
"""

from enum import Enum


class Category(Enum):
    """What kind of accountable fact an audit record represents (§5).

    ADMIN: an accountable change to the application's own administrative state.
    ACTIVITY: a consequential action performed through the application where
    attribution to an actor materially adds information. There is no third
    catch-all category; state change alone does not imply audit (§7).
    """

    ADMIN = "ADMIN"
    ACTIVITY = "ACTIVITY"


class Stage(Enum):
    """Where a record sits in the audit durability model (§9, §18).

    COMPLETE: a one-record audit operation, available only where mutation and
    audit genuinely commit atomically (§9.2). INTENT: durable accountable intent
    recorded before a non-atomic mutation. OUTCOME: the result of that mutation
    attempt. INTENT and OUTCOME are separate append-only records linked by an
    operation id; COMPLETE stands alone and carries none (§9.3).
    """

    COMPLETE = "complete"
    INTENT = "intent"
    OUTCOME = "outcome"


class Outcome(Enum):
    """The result of an audited mutation attempt (§9.3).

    SUCCESS: the intended effect is known to have occurred. FAILURE: the
    intended effect is known not to have occurred. INDETERMINATE: the
    application cannot establish whether the side effect occurred. Outcomes are
    never fabricated, and an intent with no outcome is a valid incomplete
    operation awaiting reconciliation, not one of these values by default.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    INDETERMINATE = "indeterminate"
