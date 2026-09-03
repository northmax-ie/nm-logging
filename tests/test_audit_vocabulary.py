"""Audit vocabulary: Category, Stage, Outcome (§5, §18).

These are closed vocabularies; the test pins both membership and wire spelling,
because a serialiser (M4) will emit ``member.value`` and a casing drift would be
a silent contract change.
"""

from nm_logging import Category, Outcome, Stage


def test_category_membership_and_spelling():
    assert {c.name: c.value for c in Category} == {
        "ADMIN": "ADMIN",
        "ACTIVITY": "ACTIVITY",
    }


def test_stage_membership_and_spelling():
    # §18: stage values are lower-case.
    assert {s.name: s.value for s in Stage} == {
        "COMPLETE": "complete",
        "INTENT": "intent",
        "OUTCOME": "outcome",
    }


def test_outcome_membership_and_spelling():
    # §9.3 / §18: outcomes are exactly these three, lower-case.
    assert {o.name: o.value for o in Outcome} == {
        "SUCCESS": "success",
        "FAILURE": "failure",
        "INDETERMINATE": "indeterminate",
    }
