"""Interface-name normalisation, and resolution against inventory.

A physical port has a different identity depending on who is asked. SNMP
offers ifIndex, ifName, ifDescr and ifAlias; openconfig offers ``name``;
vendors abbreviate in the CLI and expand in the MIB. The same port is
therefore ``GigabitEthernet0/0``, ``Gi0/0``, ``gi0/0`` and ``2``.

The collector normalises what it reads before it becomes a metric instance.
This module is the second half: it maps whatever arrived onto the name
INVENTORY holds, because inventory is the only authority on what a port is
called. Between the two, one port is one series regardless of which plane
collected it.

The expansion rule is implemented twice - here and in the collector's
``internal/normalize`` - and two implementations of one rule drift silently.
Both test against ``contracts/testdata/interface_names.json``.
"""

from __future__ import annotations

# Short forms real gear emits, longest first: "Te" would otherwise swallow
# "TenGigE" and "Gi" would swallow "GigabitEthernet".
_ABBREVIATIONS: tuple[tuple[str, str], ...] = (
    ("hundredgige", "HundredGigabitEthernet"),
    ("twentyfivegige", "TwentyFiveGigE"),
    ("fortygige", "FortyGigabitEthernet"),
    ("tengige", "TenGigabitEthernet"),
    ("tengig", "TenGigabitEthernet"),
    ("gigabitethernet", "GigabitEthernet"),
    ("fastethernet", "FastEthernet"),
    ("port-channel", "Port-channel"),
    ("ethernet", "Ethernet"),
    ("management", "Management"),
    ("loopback", "Loopback"),
    ("vlan", "Vlan"),
    ("hu", "HundredGigabitEthernet"),
    ("fo", "FortyGigabitEthernet"),
    ("twe", "TwentyFiveGigE"),
    ("te", "TenGigabitEthernet"),
    ("gi", "GigabitEthernet"),
    ("fa", "FastEthernet"),
    ("po", "Port-channel"),
    ("mgmt", "Management"),
    ("ma", "Management"),
    ("lo", "Loopback"),
    ("vl", "Vlan"),
)

# "Et"/"Eth" are deliberately absent. A Linux host calls its first NIC eth0,
# and a server's OS agent reports that alongside a switch reporting Ethernet0;
# expanding one into the other merges two unrelated ports into one series,
# which is worse than the problem this module solves and invisible when it
# happens - the series looks fine and its numbers are two cables added up.

_STRUCTURAL = frozenset("/.:")


def _split_prefix(name: str) -> tuple[str, str]:
    for i, ch in enumerate(name):
        if not (ch.isalpha() or ch == "-"):
            return name[:i], name[i:]
    return name, ""


def interface_name(raw: str) -> str:
    """Expand a short form. Anything unrecognised is returned unchanged.

    Expansion requires a digit after the prefix, so ``Gi0/0`` expands and
    ``Gigabit uplink`` does not. A name this function does not recognise is
    far more likely to be a vendor convention nobody here has seen than a
    mistake, and rewriting it would invent an identity.
    """
    name = (raw or "").strip()
    if not name:
        return ""

    prefix, rest = _split_prefix(name)
    if not prefix or not rest or not rest[0].isdigit():
        return name

    lowered = prefix.lower()
    for short, long in _ABBREVIATIONS:
        if lowered == short:
            return long + rest
    return name


def interface_key(raw: str) -> str:
    """The form used to COMPARE two names for one port.

    Case and non-structural punctuation are dropped; slashes, dots and colons
    are kept, because ``Ethernet1/1`` and ``Ethernet11`` are different ports.
    """
    expanded = interface_name(raw)
    out = []
    for ch in expanded:
        if ch.isalnum():
            out.append(ch.lower())
        elif ch in _STRUCTURAL:
            out.append(ch)
    return "".join(out)


class InterfaceIndex:
    """Every way one device's ports can be named, mapped to inventory's name.

    Built per device from the ``interface`` table. A lookup answers "which port
    is this?" for a name from any plane, and the answer is always the name
    inventory holds - so a chart, an alarm and a link both reference the same
    string.
    """

    __slots__ = ("_by_key",)

    def __init__(self, rows: list[tuple[str, int | None]]) -> None:
        self._by_key: dict[str, str] = {}
        for name, if_index in rows:
            if not name:
                continue
            self._by_key.setdefault(interface_key(name), name)
            if if_index is not None:
                # An agent indexing by ifIndex reports a bare number. It is a
                # weak identity - ifIndex is not stable across a reboot - but
                # it is the only one some agents offer, and resolving it here
                # is what stops "2" and "GigabitEthernet0/1" being two series.
                self._by_key.setdefault(str(if_index), name)

    def resolve(self, instance: str) -> str | None:
        """Inventory's name for this instance, or None if it is not a port."""
        if not instance:
            return None
        return self._by_key.get(interface_key(instance)) or \
            self._by_key.get(instance.strip())

    def __len__(self) -> int:
        return len(self._by_key)
