"""Operational severity: four members, no DEBUG, the syslog mapping (§4, §24)."""

import pytest

import nm_logging
from nm_logging import SYSLOG_KEYWORD, Severity


def test_exactly_four_members():
    assert {s.name for s in Severity} == {"INFO", "WARNING", "ERROR", "CRITICAL"}
    assert len(Severity) == 4


def test_values_are_upper_case_wire_spellings():
    # §17: severity is stored and emitted upper-case; the enum value is the wire
    # form, so a serialiser can use it directly.
    for member in Severity:
        assert member.value == member.name


def test_there_is_no_debug_member():
    # Invariant 1 / §4: there is no DEBUG severity. If it were ever added, this
    # fails.
    with pytest.raises(KeyError):
        Severity["DEBUG"]
    assert not any(s.name == "DEBUG" for s in Severity)


def test_public_all_is_sorted():
    # CLAUDE.md requires a sorted __all__. This guards the
    # ordering as the surface grows each milestone.
    assert nm_logging.__all__ == sorted(nm_logging.__all__)


def test_no_debug_or_level_control_on_the_public_surface():
    # No DEBUG, no set_level, no verbosity switch anywhere in what the package
    # exports (§4). The public surface is the enforcement boundary consumers see.
    surface = set(nm_logging.__all__)
    assert "DEBUG" not in surface
    forbidden = {"set_level", "setLevel", "DEBUG", "NM_LOG_LEVEL"}
    assert forbidden.isdisjoint(dir(nm_logging))


def test_syslog_mapping_is_complete_and_correct():
    # §24: CRITICAL maps to syslog CRIT, ERROR to ERR; WARNING and INFO keep
    # their names. Every severity has a mapping and nothing extra does.
    assert dict(SYSLOG_KEYWORD) == {
        Severity.INFO: "INFO",
        Severity.WARNING: "WARNING",
        Severity.ERROR: "ERR",
        Severity.CRITICAL: "CRIT",
    }
    assert set(SYSLOG_KEYWORD) == set(Severity)


def test_syslog_mapping_is_read_only():
    # A module-level mapping others read must not be mutable in place.
    with pytest.raises(TypeError):
        SYSLOG_KEYWORD[Severity.INFO] = "changed"  # type: ignore[index]
