"""Import a branch-circuit monitor's panel schedule from the meter itself.

A BCM stores which breaker each CT is clamped to, written in at commissioning.
This reads that schedule off the meter's BACnet object descriptions and records
it, so a reading on channel 1 can be attributed to the transfer switch it is
actually measuring instead of being one of forty-two anonymous numbers.

WHY THIS IS NOT IN THE COLLECTOR. The collector polls; a schedule is not a
poll. It changes when an electrician moves a CT, which is a commissioning
event, not a 30-second one, and the collector's discovery pass already reads
object NAMES for its own point mapping - adding a second meaning to that pass
would tie "which metric is this" to "whose load is this", and they are
answered by different people at different times. A DCIM imports a panel
schedule; it does not re-derive one every minute.

WHAT IS DELIBERATELY SMALL HERE. This speaks exactly enough BACnet to ask one
question - ReadProperty, description, one object - because that is all a
commissioning import needs. It is not a second protocol stack and must not
grow into one: anything that needs values needs the collector.
"""

from __future__ import annotations

import asyncio
import re
import socket
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)

BACNET_PORT = 47808

# BACnet application-layer constants. Named rather than inlined because a bare
# 28 in a UDP payload is unreadable six months later.
_OBJ_ANALOG_INPUT = 0
_SVC_READ_PROPERTY = 12
_PROP_DESCRIPTION = 28

# Verdigris EV2 channel N publishes at base (N+1)*1000; offset 2 is Active
# Power. Any of the five would carry the same label - they are one CT - and kW
# is the one this platform reads, so it is the one asked.
_CKT_BASE = 1000
_CKT_KW_OFFSET = 2

# "Circuit 7 Active Power — PDUA-DC1-HA-R2-01" / "... — Spare". The em dash is
# what the meter writes; the hyphen is accepted because a hand-commissioned
# meter in the field will have been typed by a person.
_LABEL = re.compile(r"^Circuit\s+\d+\s+.*?\s+[—-]\s+(?P<who>.+?)\s*$")

_SPARE = {"spare", "unused", "not used", "n/a", "-"}


@dataclass(frozen=True)
class Channel:
    """One CT channel as the meter describes it."""
    instance: str            # "Ckt01" - what the sample's instance carries
    label: str | None        # the branch as named, None for a spare way
    raw: str                 # the description verbatim, for the audit trail


def _read_property(ip: str, object_instance: int, prop: int,
                   timeout: float) -> bytes:
    """One ReadProperty, one datagram, one answer.

    Unconfirmed-request framing with expecting-reply set: BVLL original-unicast,
    NPDU version 1, then the APDU. No segmentation, no retries - a description
    is 40 bytes and a meter that cannot answer that in one go is a meter this
    import should report rather than work around.
    """
    oid = (object_instance | (_OBJ_ANALOG_INPUT << 22)).to_bytes(4, "big")
    apdu = bytes([0x00, 0x05, 0x01, _SVC_READ_PROPERTY, 0x0C]) + oid \
        + bytes([0x19, prop])
    npdu = bytes([0x01, 0x04]) + apdu
    frame = bytes([0x81, 0x0A, (4 + len(npdu)) >> 8, (4 + len(npdu)) & 0xFF]) + npdu

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(frame, (ip, BACNET_PORT))
        data, _ = sock.recvfrom(1500)
        return data
    finally:
        sock.close()


def _decode_charstring(frame: bytes) -> str | None:
    """The CharacterString out of a ReadProperty-ACK.

    Application tag 7, UTF-8 encoding byte, then the text. Scanned for rather
    than parsed from a fixed offset because the ACK's header length varies with
    the object identifier's encoding, and a wrong offset would silently read a
    label one byte short.
    """
    for i in range(len(frame) - 2):
        tag = frame[i]
        if tag >> 4 != 7:                      # not a CharacterString
            continue
        length = tag & 0x07
        start = i + 1
        if length == 5:                        # extended length follows
            if start >= len(frame):
                continue
            length = frame[start]
            start += 1
        if length < 2 or start + length > len(frame):
            continue
        if frame[start] != 0:                  # encoding 0 = UTF-8
            continue
        text = frame[start + 1:start + length].decode("utf-8", "replace")
        if text.strip():
            return text.strip()
    return None


def parse_label(description: str) -> str | None:
    """The branch name out of a channel description, or None for a spare way.

    A description that does not match the commissioned shape returns None too:
    "Circuit 7 Active Power" is a meter nobody has commissioned, and inventing
    a branch for it would be worse than admitting there is no schedule.
    """
    m = _LABEL.match(description.strip())
    if not m:
        return None
    who = m.group("who").strip()
    return None if who.lower() in _SPARE else who


def read_schedule(ip: str, channels: int, *, timeout: float = 2.0) -> list[Channel]:
    """Read every channel's description off one meter.

    Channels that do not answer are left out rather than recorded as spare: a
    timeout is "this platform does not know", and a spare way is "the meter
    says nothing is clamped here". Storing one as the other would retire a live
    branch on a dropped packet.
    """
    out: list[Channel] = []
    for ckt in range(1, channels + 1):
        instance = (ckt + 1) * _CKT_BASE + _CKT_KW_OFFSET
        try:
            frame = _read_property(ip, instance, _PROP_DESCRIPTION, timeout)
        except (TimeoutError, OSError) as exc:
            log.debug("meter channel did not answer", ip=ip, channel=ckt, error=str(exc))
            continue
        raw = _decode_charstring(frame)
        if not raw:
            continue
        out.append(Channel(instance=f"Ckt{ckt:02d}", label=parse_label(raw), raw=raw))
    return out


async def read_schedule_async(ip: str, channels: int,
                              *, timeout: float = 2.0) -> list[Channel]:
    """`read_schedule` off the event loop.

    The reads are blocking sockets and a 42-channel meter is 42 of them. Run
    inline they would hold the loop for as long as the slowest meter takes to
    answer, which on a timeout is the whole import.
    """
    return await asyncio.to_thread(read_schedule, ip, channels, timeout=timeout)
