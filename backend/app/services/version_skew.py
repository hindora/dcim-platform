"""How far behind a collector's version is allowed to be (docs/26 Phase 7).

Zabbix's "outdated keeps collecting" rule is deliberately preferred over
Device42/SolarWinds hard version locks (docs/26's own Decisions section) -
an OT site that freezes change control for months must not go dark just
because nobody has scheduled a collector upgrade there. So nothing here
ever blocks a batch of telemetry: nothing in app/services/collector_gateway.py
calls this module at all. What DOES change with distance from the
platform's own release is what an operator is TOLD (an alarm, once the gap
crosses a real threshold) and, from N-2, whether the collector is trusted
with NEW work - both handled elsewhere, this module only classifies.

Generation distance is measured in MINOR versions within the same MAJOR - a
major version difference is always "rejected", the same posture a hard
major-version incompatibility deserves regardless of how the minor numbers
happen to line up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)")

CURRENT = "current"     # N
SUPPORTED = "supported"  # N-1
OUTDATED = "outdated"    # N-2 - accepted, new work withheld
REJECTED = "rejected"    # older than N-2, or a whole major version behind
UNKNOWN = "unknown"      # unparseable - a dev build, or a version never sent


@dataclass(frozen=True, slots=True)
class Version:
    major: int
    minor: int


def parse(raw: str | None) -> Version | None:
    """None for anything that is not a plain "X.Y[.Z]" - most notably "dev",
    the Go collector's own default (main.go's ldflags-injected version var)
    for any build that did not go through a real release. A dev build is
    exempt from skew entirely - see classify - rather than either trusted
    as current or punished as ancient, because it is neither: it is simply
    outside what this policy can reason about."""
    if not raw:
        return None
    m = _VERSION_RE.match(raw.strip())
    if not m:
        return None
    return Version(major=int(m.group(1)), minor=int(m.group(2)))


def classify(platform_version: str, collector_version: str | None) -> str:
    """platform_version is this running platform's own release (app.
    __version__); collector_version is whatever a collector's heartbeat
    reported. Never raises - an unparseable input on either side is UNKNOWN,
    not an error, since a version string is operator-facing text a real
    deployment does not always control the exact shape of."""
    platform = parse(platform_version)
    collector = parse(collector_version)
    if platform is None or collector is None:
        return UNKNOWN
    if (collector.major, collector.minor) >= (platform.major, platform.minor):
        # Equal, or somehow ahead of the platform serving it (a collector
        # updated before the core, or a downgrade on the platform side) -
        # neither is a skew problem worth alarming on.
        return CURRENT
    if collector.major < platform.major:
        return REJECTED
    distance = platform.minor - collector.minor
    if distance == 1:
        return SUPPORTED
    if distance == 2:
        return OUTDATED
    return REJECTED
