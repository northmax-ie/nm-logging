"""Operational severity: the four members and the syslog mapping (§3, §24).

There is no DEBUG and no runtime verbosity control (§4). The type has exactly
four members; there is no fifth for tracing, and no set_level or environment
switch anywhere in the package. A future strict development toggle (§14.4) is
not a log level and must never be modelled as one here.
"""

from enum import Enum
from types import MappingProxyType
from typing import Mapping


class Severity(Enum):
    """The four operational severities, most to least serious in meaning but not
    ordered as an API: there is no threshold, no comparison, and no filtering by
    level (§4). The value is the canonical wire spelling, upper-case per §17."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# Mapping to syslog severity keywords for a future forwarder (§24). NorthMax
# CRITICAL maps to syslog CRIT rather than being reinterpreted as ALERT or EMERG
# by destination policy, and ERROR maps to ERR; WARNING and INFO keep their
# names. Defined here so the mapping is single-sourced, but no forwarder is
# implemented in v0.1 — remote forwarding is out of scope (§24, §26).
SYSLOG_KEYWORD: Mapping[Severity, str] = MappingProxyType(
    {
        Severity.INFO: "INFO",
        Severity.WARNING: "WARNING",
        Severity.ERROR: "ERR",
        Severity.CRITICAL: "CRIT",
    }
)
