"""Writing the audit log, and stripping what must never reach it.

The stripping is the part worth reading. An audit log is one of the easiest
places to leak a secret, because the honest instinct - "record what changed" -
puts the whole object in the row, and the whole object is where the password
lives. So redaction happens here, on the way in, rather than being left to
every caller to remember.

It is the same rule the logging processor applies, deliberately: one predicate,
used in both places, so a key that is redacted in a log line cannot be
un-redacted in an audit row.

Recording never raises. An audit write that fails must not fail the operation
it is describing - refusing an alarm acknowledgement because the audit table is
full turns a bookkeeping problem into an outage - but it must be loud, so the
failure is logged at error level with the action that went unrecorded.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger("audit")

# The same shape the structlog processor uses. Anything whose KEY looks like a
# secret is replaced; values are never inspected, because a heuristic on values
# both misses secrets that look ordinary and mangles data that does not.
SENSITIVE_KEY = re.compile(
    r"(password|secret|token|community|private_key|credential|authorization"
    r"|passphrase|api_key|auth)", re.I)

REDACTED = "[redacted]"

# Depth cap. A recursive walk over an attacker-influenced structure is a stack
# overflow waiting to happen, and nothing worth auditing is nested this deep.
MAX_DEPTH = 8


def scrub(value: Any, _depth: int = 0) -> Any:
    """Recursively redact anything credential-shaped.

    Byte strings are dropped entirely rather than decoded: the one bytes column
    in this schema is ``credential.secret_enc``, and a ciphertext in an audit
    row is still the secret, one key away.
    """
    if _depth > MAX_DEPTH:
        return "[too deep]"
    if isinstance(value, dict):
        return {
            k: (REDACTED if SENSITIVE_KEY.search(str(k)) else scrub(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(v, _depth + 1) for v in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return REDACTED
    return value


async def record(session: AsyncSession, *, actor: str, action: str,
                 target_type: str | None = None, target_id: str | None = None,
                 before: Any = None, after: Any = None,
                 ip: str | None = None, user_agent: str | None = None,
                 outcome: str = "ok") -> None:
    """Write one audit row. Never raises.

    The caller owns the transaction: this participates in it rather than
    committing, so an audited change and its record land together or not at
    all. A change that succeeded with no audit row is exactly the pair this
    table exists to make impossible.
    """
    try:
        await session.execute(text("""
            INSERT INTO audit_log (actor, action, target_type, target_id,
                                   before, after, ip, user_agent, outcome)
            VALUES (:actor, :action, :target_type, :target_id,
                    CAST(:before AS jsonb), CAST(:after AS jsonb),
                    CAST(:ip AS inet), :user_agent, :outcome)
        """), {
            "actor": actor, "action": action, "target_type": target_type,
            "target_id": target_id,
            "before": json.dumps(scrub(before), default=str) if before is not None else None,
            "after": json.dumps(scrub(after), default=str) if after is not None else None,
            # A malformed address must not lose the row: the identity and the
            # action matter more than where it came from.
            "ip": ip if _plausible_ip(ip) else None,
            "user_agent": (user_agent or "")[:512] or None,
            "outcome": outcome,
        })
    except Exception as exc:
        # Loud, but not fatal. Refusing the operation because its bookkeeping
        # failed turns a full audit table into an outage.
        log.error("audit write failed", action=action, actor=actor,
                  target_id=target_id, error=str(exc))


def _plausible_ip(value: str | None) -> bool:
    if not value:
        return False
    import ipaddress
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def actor_of(principal: Any) -> str:
    """A stable actor string for a user principal."""
    username = getattr(principal, "username", None)
    return username or "anonymous"


def client_of(request: Any) -> tuple[str | None, str | None]:
    """Address and user agent, defensively.

    ``request.client`` is None behind some ASGI servers and in tests, and an
    audit call must not be the thing that raises.
    """
    try:
        ip = request.client.host if request.client else None
    except Exception:
        ip = None
    try:
        agent = request.headers.get("user-agent")
    except Exception:
        agent = None
    return ip, agent
