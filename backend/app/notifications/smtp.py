"""The mail relay: its settings, and sending through it.

How real sites do this: the DCIM does not deliver mail itself, it hands every
message to the site's relay (Exchange / Microsoft 365, Postfix, an SMTP
gateway) and the relay delivers it. So the settings are the relay's - host,
port, how the connection is secured, an optional login - plus who the mail is
from. Port 587 with STARTTLS is the submission default; 465 is TLS from the
first byte; 25 with no TLS is an internal relay that trusts the network.

Sending is the stdlib `smtplib`, run in a thread. It is blocking, but the
volume here is a handful of messages a minute at worst, and a dependency on an
async SMTP client would buy nothing a thread does not.
"""

from __future__ import annotations

import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from email import policy as email_policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret

SECURITY = ("starttls", "tls", "none")
TIMEOUT_S = 20

#: Deliberately simple: one @, something before it, a dot in the domain, no
#: spaces or angle brackets. Full RFC 5322 validation accepts addresses no relay
#: will route; this refuses typos and leaves the rest to the relay's answer.
_EMAIL = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$|^\[?[0-9A-Fa-f:.]+\]?$")


class SmtpSettingsError(ValueError):
    """Settings that cannot be saved, with a sentence a form can show."""


class SendError(Exception):
    """A send that failed. `permanent` means retrying cannot help."""

    def __init__(self, message: str, *, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass
class SmtpConfig:
    enabled: bool = False
    host: str = ""
    port: int = 587
    security: str = "starttls"
    username: str = ""
    password: str | None = field(default=None, repr=False)
    password_hint: str | None = None
    from_address: str = ""
    from_name: str = ""
    reply_to: str = ""
    link_base_url: str = ""
    updated_at: datetime | None = None
    updated_by: str | None = None

    def public(self) -> dict[str, Any]:
        """What the API may return. Never the password - only whether one is set."""
        return {
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
            "security": self.security,
            "username": self.username,
            "password_set": self.password_hint is not None,
            "password_hint": self.password_hint,
            "from_address": self.from_address,
            "from_name": self.from_name,
            "reply_to": self.reply_to,
            "link_base_url": self.link_base_url,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


def valid_email(address: str) -> bool:
    return bool(_EMAIL.match(address or ""))


def clean_settings(raw: dict[str, Any], current: SmtpConfig) -> SmtpConfig:
    """Validate a submitted form against what is stored, and return the result.

    `password` follows the usual write-only rule: absent keeps the stored one,
    an empty string removes it, anything else replaces it.
    """
    cfg = SmtpConfig(**{k: getattr(current, k) for k in SmtpConfig.__dataclass_fields__})

    def text_field(key: str, max_len: int) -> str:
        value = raw.get(key, getattr(cfg, key))
        value = "" if value is None else str(value).strip()
        if len(value) > max_len:
            raise SmtpSettingsError(f"{key.replace('_', ' ')} is longer than {max_len} characters")
        if any(ord(c) < 32 for c in value):
            raise SmtpSettingsError(f"{key.replace('_', ' ')} contains a control character")
        return value

    cfg.enabled = bool(raw.get("enabled", cfg.enabled))
    cfg.host = text_field("host", 253)
    cfg.username = text_field("username", 200)
    cfg.from_address = text_field("from_address", 254)
    cfg.from_name = text_field("from_name", 100)
    cfg.reply_to = text_field("reply_to", 254)
    cfg.link_base_url = text_field("link_base_url", 300).rstrip("/")

    security = raw.get("security", cfg.security)
    if security not in SECURITY:
        raise SmtpSettingsError("security is one of STARTTLS, TLS or none")
    cfg.security = security
    try:
        cfg.port = int(raw.get("port", cfg.port))
    except (TypeError, ValueError) as exc:
        raise SmtpSettingsError("port must be a number") from exc
    if not 1 <= cfg.port <= 65535:
        raise SmtpSettingsError("port must be between 1 and 65535")

    if "password" in raw:
        pw = raw.get("password")
        if pw in (None, ""):
            cfg.password, cfg.password_hint = None, None
        else:
            pw = str(pw)
            if len(pw) > 500:
                raise SmtpSettingsError("password is longer than 500 characters")
            cfg.password = pw
            cfg.password_hint = _hint(pw)

    if cfg.host and not _HOST.match(cfg.host):
        raise SmtpSettingsError(
            "host must be a host name or an IP address, without a scheme or port"
        )
    for key in ("from_address", "reply_to"):
        value = getattr(cfg, key)
        if value and not valid_email(value):
            raise SmtpSettingsError(f"{key.replace('_', ' ')} is not an email address")
    if cfg.link_base_url and not re.match(r"^https?://", cfg.link_base_url):
        raise SmtpSettingsError("link base URL must start with http:// or https://")
    if cfg.password and not cfg.username:
        raise SmtpSettingsError("a password needs a username to log in with")
    if cfg.username and cfg.security == "none":
        # smtplib would send it. A relay login over a plain connection puts the
        # password on the wire in base64, which is clear text with extra steps.
        raise SmtpSettingsError(
            "a login over an unencrypted connection sends the password in clear text; "
            "use STARTTLS or TLS, or remove the login for an internal relay"
        )
    if cfg.enabled:
        missing = [n for n, v in (("host", cfg.host), ("from address", cfg.from_address)) if not v]
        if missing:
            raise SmtpSettingsError(f"email cannot be turned on without a {' and '.join(missing)}")
    return cfg


def _hint(password: str) -> str:
    """Says that a password is saved, and nothing about it.

    Not the last characters, the way an API-token hint does: a token is random,
    a relay password is often a person's, and two characters of it in a form is
    two characters too many.
    """
    del password
    return "saved"


async def load(session: AsyncSession, *, with_password: bool = False) -> SmtpConfig:
    row = (
        (await session.execute(text("SELECT * FROM smtp_setting WHERE id = 1"))).mappings().first()
    )
    if row is None:
        return SmtpConfig()
    cfg = SmtpConfig(
        enabled=row["enabled"],
        host=row["host"],
        port=row["port"],
        security=row["security"],
        username=row["username"],
        password_hint=row["password_hint"],
        from_address=row["from_address"],
        from_name=row["from_name"],
        reply_to=row["reply_to"],
        link_base_url=row["link_base_url"],
        updated_at=row["updated_at"],
        updated_by=row["updated_by"],
    )
    if with_password and row["password_secret"]:
        cfg.password = decrypt_secret(bytes(row["password_secret"])).get("password")
    return cfg


async def save(
    session: AsyncSession, cfg: SmtpConfig, actor: str, *, password_changed: bool
) -> None:
    """Upsert the single row. The caller owns the transaction (and the audit)."""
    params: dict[str, Any] = {
        "enabled": cfg.enabled,
        "host": cfg.host,
        "port": cfg.port,
        "security": cfg.security,
        "username": cfg.username,
        "from_address": cfg.from_address,
        "from_name": cfg.from_name,
        "reply_to": cfg.reply_to,
        "link_base_url": cfg.link_base_url,
        "actor": actor,
    }
    pw_cols = pw_vals = pw_set = ""
    if password_changed:
        params["secret"] = encrypt_secret({"password": cfg.password}) if cfg.password else None
        params["hint"] = cfg.password_hint
        pw_cols = ", password_secret, password_hint"
        pw_vals = ", :secret, :hint"
        pw_set = (
            ", password_secret = EXCLUDED.password_secret, password_hint = EXCLUDED.password_hint"
        )
    await session.execute(
        text(f"""
        INSERT INTO smtp_setting (id, enabled, host, port, security, username,
                                  from_address, from_name, reply_to, link_base_url,
                                  updated_at, updated_by{pw_cols})
        VALUES (1, :enabled, :host, :port, :security, :username, :from_address,
                :from_name, :reply_to, :link_base_url, now(), :actor{pw_vals})
        ON CONFLICT (id) DO UPDATE SET
            enabled = EXCLUDED.enabled, host = EXCLUDED.host, port = EXCLUDED.port,
            security = EXCLUDED.security, username = EXCLUDED.username,
            from_address = EXCLUDED.from_address, from_name = EXCLUDED.from_name,
            reply_to = EXCLUDED.reply_to, link_base_url = EXCLUDED.link_base_url,
            updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by{pw_set}
    """),
        params,
    )


# ------------------------------------------------------------------ sending


def from_header(cfg: SmtpConfig, org_name: str) -> str:
    return formataddr((cfg.from_name or f"{org_name} DCIM", cfg.from_address))


def base_message(
    cfg: SmtpConfig, org_name: str, to: list[str], subject: str, body: str
) -> EmailMessage:
    # 998 is the RFC 5322 hard limit. At the default 78 a subject of ordinary
    # length folds straight after "Subject:", which some clients show as a
    # blank first line in the notification preview.
    msg = EmailMessage(policy=email_policy.SMTP.clone(max_line_length=998))
    msg["From"] = from_header(cfg, org_name)
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    if cfg.reply_to:
        msg["Reply-To"] = cfg.reply_to
    # RFC 3834: tells out-of-office responders not to answer a machine.
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Auto-Response-Suppress"] = "All"
    msg.set_content(body)
    return msg


def send_batch(cfg: SmtpConfig, messages: list[EmailMessage]) -> list[SendError | None]:
    """Send messages over ONE connection, in order. Blocking - run in a thread.

    Returns one entry per message: None for delivered to the relay, or the
    error. A connection that cannot be opened at all fails every message with
    the same (retryable) error.
    """
    try:
        client = _connect(cfg)
    except SendError as exc:
        return [exc] * len(messages)
    results: list[SendError | None] = []
    try:
        for msg in messages:
            try:
                refused = client.send_message(msg)
                if refused:
                    bad = ", ".join(f"{a} ({c})" for a, (c, _) in refused.items())
                    # Delivered to the others. Not retried: a retry would mail
                    # the accepted recipients twice.
                    results.append(
                        SendError(f"the relay refused some recipients: {bad}", permanent=True)
                    )
                else:
                    results.append(None)
            except smtplib.SMTPServerDisconnected as exc:
                results.append(SendError(f"the relay disconnected: {exc}", permanent=False))
                results.extend(
                    [SendError("not sent: the connection was lost", permanent=False)]
                    * (len(messages) - len(results))
                )
                break
            except smtplib.SMTPException as exc:
                results.append(_classify(exc))
    finally:
        try:
            client.quit()
        except Exception:
            client.close()
    return results


def _connect(cfg: SmtpConfig) -> smtplib.SMTP:
    if not cfg.host:
        raise SendError("no mail relay host is set", permanent=True)
    context = ssl.create_default_context()
    try:
        if cfg.security == "tls":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                cfg.host, cfg.port, timeout=TIMEOUT_S, context=context
            )
        else:
            client = smtplib.SMTP(cfg.host, cfg.port, timeout=TIMEOUT_S)
        client.ehlo()
        if cfg.security == "starttls":
            if not client.has_extn("starttls"):
                client.close()
                raise SendError(
                    "the relay does not offer STARTTLS; choose TLS or none, or check the port",
                    permanent=True,
                )
            client.starttls(context=context)
            client.ehlo()
        if cfg.username:
            client.login(cfg.username, cfg.password or "")
        return client
    except SendError:
        raise
    except smtplib.SMTPAuthenticationError as exc:
        raise SendError(
            f"the relay refused the login ({exc.smtp_code}); check the username and password",
            permanent=False,
        ) from exc
    except ssl.SSLError as exc:
        raise SendError(
            f"TLS failed: {exc.reason or exc}. Check the security setting "
            "matches the port (587 STARTTLS, 465 TLS)",
            permanent=False,
        ) from exc
    except TimeoutError as exc:
        raise SendError(
            f"no answer from {cfg.host}:{cfg.port} within {TIMEOUT_S}s", permanent=False
        ) from exc
    except OSError as exc:
        raise SendError(
            f"could not connect to {cfg.host}:{cfg.port}: {exc}", permanent=False
        ) from exc
    except smtplib.SMTPException as exc:
        raise _classify(exc) from exc


def _classify(exc: smtplib.SMTPException) -> SendError:
    """5xx is the relay saying no for good; 4xx and anything else, try again."""
    code = getattr(exc, "smtp_code", None)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [c for c, _ in exc.recipients.values()]
        code = min(codes) if codes else None
        return SendError(
            f"the relay refused every recipient ({', '.join(exc.recipients)})",
            permanent=bool(code and 500 <= code < 600),
        )
    detail = getattr(exc, "smtp_error", b"")
    if isinstance(detail, bytes):
        detail = detail.decode("utf-8", "replace")
    message = f"the relay answered {code}: {detail}".strip() if code else str(exc)
    return SendError(message, permanent=bool(code and 500 <= code < 600))
