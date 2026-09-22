"""The HTTP half: auth, paced calls, and turning Atlassian's answers into
something the dispatcher can decide on.

AUTH, AND WHY THESE TWO. A self-hosted product opening tickets on behalf of an
organisation has no end user present at 02:00 to consent to anything, so the
per-user OAuth dance is the wrong model however modern it looks. What is left:

* **Cloud** - an API token on a dedicated service account, sent as HTTP Basic
  with the account's email as the username. Trivial, works everywhere
  including the Assets endpoints, and attributable to one named account the
  customer can see and revoke.
* **Data Center** - a Personal Access Token as a Bearer. The only sane
  on-prem option; DC has no API tokens.

Both are stored encrypted and neither is ever logged: `_redact` below is the
last line of that defence, because an httpx exception's `repr` happily
includes the request headers.

WHAT THIS MODULE DOES NOT DO. It does not retry. Retrying belongs to the
dispatcher, which owns the outbox row and therefore the only durable record of
how many attempts have happened; a retry loop in here would multiply the two
and turn eight attempts into sixty-four. Every call either returns a result or
raises `JiraError` with enough on it - the status, the parsed `Retry-After`,
whether it is worth repeating - for the dispatcher to schedule the next one.
"""

from __future__ import annotations

import base64
import re
from typing import Any

import httpx

from app.core.logging import get_logger
from app.integrations.backoff import is_retryable, parse_retry_after

log = get_logger("integrations.jira")

#: Atlassian answers most calls in well under a second; a create with a large
#: ADF body and a busy tenant can take several. 20s is generous enough that a
#: timeout means something is wrong, and short enough that a hung connection
#: does not hold an outbox claim for a minute.
TIMEOUT_S = 20.0

#: XSRF guard. Attachment uploads are rejected outright without it, and it is
#: harmless on every other call.
NO_CHECK = {"X-Atlassian-Token": "no-check"}


class JiraError(RuntimeError):
    """One failed call, with what the dispatcher needs to decide what next."""

    def __init__(self, message: str, *, status: int | None = None,
                 retry_after: float | None = None,
                 body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.body = body

    @property
    def retryable(self) -> bool:
        return is_retryable(self.status)

    def __str__(self) -> str:
        base = super().__str__()
        if self.status:
            base = f"{self.status} {base}"
        return f"{base}: {self.body}" if self.body else base


class JiraClient:
    """One configured Atlassian instance.

    Holds an httpx client for the life of the dispatcher, so connections and
    TLS sessions are reused: a storm is hundreds of calls to one host, and
    re-handshaking each time costs more than the calls.
    """

    def __init__(self, *, base_url: str, secret: dict[str, Any],
                 secret_kind: str, limiter: Any = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._limiter = limiter
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=TIMEOUT_S, follow_redirects=False)
        self._auth = _auth_header(secret, secret_kind)

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    # ------------------------------------------------------------ requests

    async def request(self, method: str, path: str, *,
                      json_body: Any = None, params: dict[str, Any] | None = None,
                      issue_key: str | None = None,
                      files: Any = None) -> Any:
        """One call. Paced, authenticated, and decoded.

        ``issue_key`` is not decoration: Jira limits writes against a SINGLE
        issue to 20 in 2 seconds, independently of the per-second endpoint
        limit, and a flapping condition sends the whole estate's traffic at
        one key. Passing it here is what lets the limiter hold a bucket for it.
        """
        if self._limiter is not None:
            await self._limiter.acquire(issue_key)

        # An ABSOLUTE path is used as given. The Assets Imports API is
        # HATEOAS-driven and lives on api.atlassian.com rather than on the
        # site host, so its links are followed verbatim - prefixing them with
        # the site URL is the failure that reads as a 404 from Jira.
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        headers = {"Accept": "application/json", **self._auth}
        if files is None and json_body is not None:
            headers["Content-Type"] = "application/json"
        if files is not None:
            headers.update(NO_CHECK)

        try:
            response = await self._client.request(
                method, url, json=json_body, params=params, headers=headers,
                files=files)
        except httpx.HTTPError as exc:
            # No status at all: DNS, TLS, connect, read timeout. Always worth
            # repeating, and the message is the only evidence of which it was.
            raise JiraError(_redact(str(exc)), status=None) from exc

        return self._decode(response, method, path)

    def _decode(self, response: httpx.Response, method: str, path: str) -> Any:
        if response.status_code == 429:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            log.warning("jira rate limited", path=path,
                        reason=response.headers.get("RateLimit-Reason"),
                        retry_after=retry_after)
            raise JiraError("rate limited", status=429, retry_after=retry_after,
                            body=_summarise(response))

        if response.status_code >= 400:
            raise JiraError(f"{method} {path} failed", status=response.status_code,
                            body=_summarise(response))

        if response.status_code == 204 or not response.content:
            # 204 is the documented success for a transition, and a 201 create
            # of a remote link comes back with an empty body on some versions.
            return None
        try:
            return response.json()
        except ValueError:
            # A 200 that is not JSON means a proxy or an SSO portal answered
            # instead of Jira. Reported as a failure rather than parsed into
            # None, because None would look like a successful transition.
            raise JiraError("response was not JSON - is the base URL a Jira "
                            "instance and not a login portal?",
                            status=response.status_code,
                            body=_summarise(response)) from None

    # ------------------------------------------------------- conveniences

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)

    async def put(self, path: str, **kw: Any) -> Any:
        return await self.request("PUT", path, **kw)


def _auth_header(secret: dict[str, Any], secret_kind: str) -> dict[str, str]:
    if secret_kind == "pat":
        token = secret.get("token") or ""
        if not token:
            raise JiraError("the personal access token is empty")
        return {"Authorization": f"Bearer {token}"}

    if secret_kind == "api_token":
        email = secret.get("username") or secret.get("email") or ""
        token = secret.get("token") or secret.get("password") or ""
        if not email or not token:
            raise JiraError("a Cloud API token needs the account email and the token")
        raw = f"{email}:{token}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}

    raise JiraError(f"unsupported credential kind {secret_kind!r}")


#: Response bodies from Jira carry the field errors that make a 400 fixable,
#: and they are short. A body from something that is NOT Jira - an SSO login
#: page, a proxy error - can be a whole HTML document, and it would go into
#: `outbox.last_error` and onto a settings page.
_BODY_CHARS = 600


def _summarise(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return _redact(response.text[:_BODY_CHARS])
    # Jira's error shape: {"errorMessages": [...], "errors": {"field": "..."}}.
    # Both halves matter - the second is where "customfield_10101 cannot be
    # set, it is not on the appropriate screen" lives, which is the single
    # most common first-run failure.
    if isinstance(payload, dict):
        parts = []
        for message in payload.get("errorMessages") or []:
            parts.append(str(message))
        for field, message in (payload.get("errors") or {}).items():
            parts.append(f"{field}: {message}")
        if parts:
            return _redact("; ".join(parts))[:_BODY_CHARS]
    return _redact(str(payload))[:_BODY_CHARS]


def _redact(text: str) -> str:
    """Strip anything that looks like the credential out of an error string.

    httpx puts the request in the repr of some of its exceptions, and an error
    string here lands in the outbox row, the log and the settings page. This
    is belt and braces - nothing should be putting a header in a message - but
    the cost of being wrong once is a credential in a database column that
    nobody thinks of as sensitive.
    """
    return _CREDENTIAL.sub(r"\1 [redacted]", text)


#: Anything following a scheme name in an Authorization header. Written as one
#: substitution rather than a replace loop on purpose: the obvious loop -
#: "while 'Bearer ' in text, replace it" - never terminates, because the
#: replacement contains the thing it searches for.
_CREDENTIAL = re.compile(r"\b(Basic|Bearer)\s+\S+")
