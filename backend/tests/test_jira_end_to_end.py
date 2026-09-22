"""The whole loop, over real HTTP, against a real database.

Everything else in this suite tests one part against a double. This tests the
WIRING: a real alarm row, the real outbox, the real dispatcher with its real
HTTP client, a Jira that answers on a real socket, the real webhook handler
verifying a real signature, and the real inbox applier moving the real alarm.

**It is not a substitute for a real Jira.** The server below answers the way
Atlassian's documentation says Jira answers, which is exactly the assumption
that cannot be checked without a tenant. What it DOES catch is every bug
between the alarm table and the wire - the ones no amount of mocking finds,
because a mock is written from the same misunderstanding as the code.

Skipped unless `DCIM_TEST_DATABASE_URL` names a database this may migrate and
write to. It must NOT be a live one: the test creates and clears rows.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

# At module scope, not inside `app()`: this file uses `from __future__ import
# annotations`, so every annotation is a STRING and FastAPI resolves it
# against the MODULE's globals. Imported inside the method, `Request` is
# unresolvable and FastAPI silently treats the parameter as a query string -
# which answers 422 to every call and reads as a Jira that hates the payload.
from fastapi import FastAPI, HTTPException, Request

DB_URL = os.environ.get("DCIM_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DB_URL, reason="set DCIM_TEST_DATABASE_URL to run")


# --------------------------------------------------------------- fake jira

class FakeJira:
    """Jira, as its documentation describes it.

    Deliberately strict about the things that are easy to get wrong and that a
    forgiving double would hide: it REQUIRES a project and an issue type,
    rejects an unknown custom field the way a real create screen does, and
    refuses a transition it did not offer.
    """

    def __init__(self) -> None:
        self.issues: dict[str, dict[str, Any]] = {}
        self.comments: dict[str, list[Any]] = {}
        self.remote_links: dict[str, list[Any]] = {}
        self.counter = 0
        self.known_fields = {"customfield_10101"}

    def app(self) -> FastAPI:
        api = FastAPI()

        @api.get("/rest/api/3/myself")
        async def myself():
            return {"displayName": "DCIM bot", "accountId": "svc-1"}

        @api.post("/rest/api/3/issue", status_code=201)
        async def create(request: Request):
            body = await request.json()
            fields = body.get("fields") or {}
            if not (fields.get("project") or {}).get("key"):
                raise HTTPException(400, "project is required")
            if not (fields.get("issuetype") or {}).get("name"):
                raise HTTPException(400, "issuetype is required")
            for key in fields:
                if key.startswith("customfield_") and key not in self.known_fields:
                    # What a real create screen does, and the single most
                    # common first-run failure.
                    raise HTTPException(400, {"errors": {
                        key: "Field cannot be set. It is not on the "
                             "appropriate screen."}})
            self.counter += 1
            key = f"DCOPS-{self.counter}"
            self.issues[key] = {"key": key, "id": str(10_000 + self.counter),
                                "fields": fields, "status": "To Do",
                                "category": "new", "resolution": None}
            return {"key": key, "id": self.issues[key]["id"]}

        @api.post("/rest/api/3/issue/{key}/comment", status_code=201)
        async def comment(key: str, request: Request):
            if key not in self.issues:
                raise HTTPException(404, "no such issue")
            self.comments.setdefault(key, []).append((await request.json())["body"])
            return {"id": "1"}

        @api.put("/rest/api/3/issue/{key}", status_code=204)
        async def update(key: str, request: Request):
            if key not in self.issues:
                raise HTTPException(404, "no such issue")
            body = await request.json()
            self.issues[key]["fields"].update(body.get("fields") or {})
            return None

        @api.post("/rest/api/3/issue/{key}/remotelink", status_code=201)
        async def remotelink(key: str, request: Request):
            body = await request.json()
            links = self.remote_links.setdefault(key, [])
            # globalId is an UPSERT key: posting the same one twice updates.
            links[:] = [x for x in links if x.get("globalId") != body.get("globalId")]
            links.append(body)
            return {"id": len(links)}

        @api.get("/rest/api/3/issue/{key}/transitions")
        async def transitions(key: str):
            return {"transitions": [
                {"id": "31", "name": "Done",
                 "to": {"name": "Done", "statusCategory": {"key": "done"}}}]}

        @api.post("/rest/api/3/issue/{key}/transitions", status_code=204)
        async def do_transition(key: str, request: Request):
            body = await request.json()
            if (body.get("transition") or {}).get("id") != "31":
                raise HTTPException(400, "that transition is not available")
            self.issues[key].update(status="Done", category="done")
            return None

        @api.post("/rest/api/3/search/jql")
        async def search(request: Request):
            body = await request.json()
            jql = body.get("jql") or ""
            hits = [i for i in self.issues.values()
                    if any(label in jql
                           for label in (i["fields"].get("labels") or []))]
            return {"issues": [{"key": i["key"], "id": i["id"]} for i in hits]}

        @api.post("/rest/api/3/issueLink", status_code=201)
        async def link(request: Request):
            await request.json()
            return None

        return api


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
async def fresh_engine():
    """One database engine per test.

    `db.session` caches its engine in a module global, and pytest-asyncio
    gives every test a NEW event loop - so an engine built in one test is
    bound to a loop that is closed by the time the next one runs. The symptom
    is a pool that raises "Event loop is closed" on teardown and queries that
    silently return nothing, which reads as a dispatcher that delivered
    nothing. These tests passed alone and failed together until this existed.
    """
    yield
    from app.db.session import dispose_engine
    await dispose_engine()


@pytest.fixture
def jira():
    """A Jira on a real socket, for the life of one test."""
    import uvicorn

    fake = FakeJira()
    port = free_port()
    config = uvicorn.Config(fake.app(), host="127.0.0.1", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        threading.Event().wait(0.05)
    fake.base_url = f"http://127.0.0.1:{port}"
    yield fake
    server.should_exit = True
    thread.join(timeout=5)


# ------------------------------------------------------------- the estate

DEVICE = uuid.UUID("33333333-0000-4000-8000-0000000000e2")
DC = uuid.UUID("11111111-0000-4000-8000-0000000000e2")
ROOM = uuid.UUID("22222222-0000-4000-8000-0000000000e2")


async def seed(session) -> None:
    from sqlalchemy import text
    await session.execute(text("""
        INSERT INTO datacenter (id, code, name) VALUES (:id, 'E2E', 'End to end')
        ON CONFLICT DO NOTHING"""), {"id": DC})
    await session.execute(text("""
        INSERT INTO room (id, datacenter_id, name) VALUES (:id, :dc, 'E2E Hall')
        ON CONFLICT DO NOTHING"""), {"id": ROOM, "dc": DC})
    await session.execute(text("""
        INSERT INTO device (id, name, device_type, room_id, lifecycle)
        VALUES (:id, 'CRAH-E2E', 'crah', :room, 'in_service')
        ON CONFLICT DO NOTHING"""), {"id": DEVICE, "room": ROOM})


async def raise_alarm(session) -> str:
    from sqlalchemy import text
    row = (await session.execute(text("""
        INSERT INTO alarm (device_id, alarm_type, instance, severity, state,
                           message, source, category, detection,
                           response_class, first_seen, last_seen)
        VALUES (:d, 'supply_temp_high', 'AI:1', 'CRITICAL', 'ACTIVE',
                'Supply air 31.2C above 26.0C', 'poll', 'cooling',
                'threshold', 'alarm', now() - interval '1 hour', now())
        RETURNING id::text"""), {"d": DEVICE})).scalar_one()
    return row


async def cleanup(session) -> None:
    from sqlalchemy import text
    for sql in (
        "DELETE FROM integration_outbox",
        "DELETE FROM integration_inbox",
        "DELETE FROM jira_link",
        "DELETE FROM alarm_history WHERE device_id = :d",
        "DELETE FROM alarm WHERE device_id = :d",
        "DELETE FROM integration",
    ):
        await session.execute(text(sql), {"d": DEVICE} if ":d" in sql else {})


# ------------------------------------------------------------- the loop

async def test_the_whole_loop(jira):
    """Alarm -> ticket -> webhook -> acknowledged, over real HTTP.

    The assertions walk the loop in order, so a failure names the hop that
    broke rather than the end state.
    """
    os.environ["DCIM_DATABASE_URL"] = DB_URL
    os.environ.setdefault("DCIM_PUBLIC_BASE_URL", "https://dcim.e2e.test")

    from app.core.security import encrypt_secret
    from app.db.session import unit_of_work
    from app.integrations import fingerprint as fp
    from app.integrations import inbox
    from app.integrations.dispatcher import Dispatcher
    from app.integrations.jira import webhook
    from app.repositories import integrations as repo

    async with unit_of_work() as session:
        await cleanup(session)
        await seed(session)
        alarm_id = await raise_alarm(session)
        integration = await repo.create_integration(
            session, kind="jira_dc", name="E2E", base_url=jira.base_url,
            cloud_id=None,
            config={"project_key": "DCOPS", "issue_type": "Incident",
                    "policy": {"dwell_s": 0}, "close_on_clear": "transition"},
            blob=encrypt_secret({"token": "e2e-token"}),
            secret_hint="token (9 chars)", secret_kind="pat",
            secret_expires_at=datetime.now(UTC) + timedelta(days=365),
            actor="e2e")
        await repo.update_integration(session, integration["id"],
                                      actor="e2e", enabled=True)

    # ---- 1. the alarm earns a ticket, and the intent is recorded
    async with unit_of_work() as session:
        wide = await repo.alarms_for_export(session, [alarm_id])
        print_ = fp.of_alarm(wide[alarm_id])
        await repo.enqueue(session, [{
            "integration_id": integration["id"], "kind": "alarm_raised",
            "fingerprint": print_, "alarm_id": alarm_id,
            "payload": wide[alarm_id]}])

    # ---- 2. the dispatcher delivers it over real HTTP
    dispatcher = Dispatcher("e2e")
    try:
        delivered = await dispatcher.run_once()
        assert delivered == 1, "the dispatcher did not deliver the row"
        assert jira.issues, "no issue reached Jira"

        key = next(iter(jira.issues))
        created = jira.issues[key]["fields"]
        assert created["project"] == {"key": "DCOPS"}
        assert created["priority"] == {"name": "Highest"}
        assert fp.label(print_) in created["labels"]
        assert jira.remote_links[key][0]["globalId"].endswith(f"&alarm={print_}")

        # ---- 3. the key is remembered, so a second raise does not duplicate
        async with unit_of_work() as session:
            links = await repo.links_for(session, integration["id"], [print_])
            # Through the INBOUND lookup, which is the one that has to carry
            # the condition's identity: a webhook arrives with an issue key
            # and nothing else, and `_current_alarm` resolves the alarm that
            # is open NOW through (device, alarm_type, instance).
            inbound = await repo.link_by_issue(session, integration["id"], key)
        assert links[print_]["issue_key"] == key
        assert inbound["alarm_type"] == "supply_temp_high"
        assert inbound["instance"] == "AI:1"

        async with unit_of_work() as session:
            await repo.enqueue(session, [{
                "integration_id": integration["id"], "kind": "alarm_raised",
                "fingerprint": print_, "alarm_id": alarm_id,
                "payload": wide[alarm_id]}])
        await dispatcher.run_once()
        assert len(jira.issues) == 1, "a second ticket was opened for one fault"

        # ---- 4. a human closes it, and Jira calls back
        async with unit_of_work() as session:
            await repo.set_webhook(
                session, integration["id"], token="e2e-token-path",
                blob=encrypt_secret({"secret": "e2e-webhook-secret"}),
                webhook_id=None, expires_at=None)

        payload = {
            "webhookEvent": "jira:issue_updated",
            "user": {"accountId": "human-1", "displayName": "Dana Okafor"},
            "issue": {"key": key, "fields": {
                "status": {"name": "Done",
                           "statusCategory": {"key": "done"}},
                "resolution": {"name": "Done"}}},
            "changelog": {"items": [{"field": "status",
                                     "fromString": "To Do",
                                     "toString": "Done"}]},
        }
        body = json.dumps(payload).encode()
        signature = webhook.sign(body, b"e2e-webhook-secret")

        async with unit_of_work() as session:
            row = await repo.integration_by_webhook_token(session,
                                                          "e2e-token-path")
            from app.services import integrations as service
            secret = await service.webhook_secret(row)
            assert webhook.verify(body, signature, secret), \
                "the signature this platform would send does not verify"
            await repo.enqueue_inbound(
                session, integration_id=row["id"], event=payload["webhookEvent"],
                issue_key=key, payload=payload,
                dedup_sha=webhook.dedup_sha(body))
            # A redelivery of the same bytes must not enqueue twice.
            again = await repo.enqueue_inbound(
                session, integration_id=row["id"], event=payload["webhookEvent"],
                issue_key=key, payload=payload,
                dedup_sha=webhook.dedup_sha(body))
        assert again is False, "a redelivery was enqueued a second time"

        # ---- 5. the applier acknowledges - and never clears
        async with unit_of_work() as session:
            full = await repo.get_integration(session, integration["id"])
            result = await inbox.apply(session, full, payload)
        assert result["action"] == "acknowledge"

        async with unit_of_work() as session:
            from sqlalchemy import text
            state = (await session.execute(text(
                "SELECT state::text, acknowledged_by FROM alarm WHERE id = :i"),
                {"i": uuid.UUID(alarm_id)})).first()
        assert state[0] == "ACKNOWLEDGED", \
            f"a closed ticket left the alarm {state[0]}"
        assert "human-1" in state[1]

        # ---- 6. the fault actually clears, and the ticket is TOLD
        #
        # It is already closed - a human got there first in step 4 - so there
        # is nothing to transition. What there is, is the other half of this
        # integration's central rule: whoever closed it did so without knowing
        # whether the condition had gone, and this is the confirmation.
        async with unit_of_work() as session:
            from sqlalchemy import text
            await session.execute(text(
                "UPDATE alarm SET state='CLEARED', cleared_at=now() "
                "WHERE id = :i"), {"i": uuid.UUID(alarm_id)})
            await repo.enqueue(session, [{
                "integration_id": integration["id"], "kind": "alarm_cleared",
                "fingerprint": print_, "alarm_id": alarm_id,
                "payload": {**wide[alarm_id], "cleared_at": str(datetime.now(UTC))}}])
        await dispatcher.run_once()

        from app.integrations import adf
        bodies = [adf.to_text(b) for b in jira.comments[key]]
        assert any("confirms the fault is actually gone" in b for b in bodies), \
            "the clear did not confirm the fault was gone"
        assert jira.remote_links[key][-1]["object"]["status"] == {"resolved": True}
        assert len(jira.remote_links[key]) == 1, \
            "globalId did not upsert - the issue has duplicate back-links"

    finally:
        await dispatcher.aclose()
        async with unit_of_work() as session:
            await cleanup(session)


async def test_a_clear_while_the_ticket_is_open_transitions_it(jira):
    """The other order, and the commoner one: the fault clears before anybody
    has touched the ticket."""
    os.environ["DCIM_DATABASE_URL"] = DB_URL

    from app.core.security import encrypt_secret
    from app.db.session import unit_of_work
    from app.integrations import fingerprint as fp
    from app.integrations.dispatcher import Dispatcher
    from app.repositories import integrations as repo

    async with unit_of_work() as session:
        await cleanup(session)
        await seed(session)
        alarm_id = await raise_alarm(session)
        integration = await repo.create_integration(
            session, kind="jira_dc", name="E2E clear", base_url=jira.base_url,
            cloud_id=None,
            config={"project_key": "DCOPS", "issue_type": "Incident",
                    "policy": {"dwell_s": 0}, "close_on_clear": "transition"},
            blob=encrypt_secret({"token": "e2e"}), secret_hint="token",
            secret_kind="pat", secret_expires_at=None, actor="e2e")
        await repo.update_integration(session, integration["id"],
                                      actor="e2e", enabled=True)
        wide = await repo.alarms_for_export(session, [alarm_id])
        print_ = fp.of_alarm(wide[alarm_id])
        await repo.enqueue(session, [{
            "integration_id": integration["id"], "kind": "alarm_raised",
            "fingerprint": print_, "alarm_id": alarm_id,
            "payload": wide[alarm_id]}])

    dispatcher = Dispatcher("e2e-clear")
    try:
        await dispatcher.run_once()
        key = next(iter(jira.issues))
        assert jira.issues[key]["status"] == "To Do"

        async with unit_of_work() as session:
            await repo.enqueue(session, [{
                "integration_id": integration["id"], "kind": "alarm_cleared",
                "fingerprint": print_, "alarm_id": alarm_id,
                "payload": {**wide[alarm_id],
                            "cleared_at": str(datetime.now(UTC))}}])
        await dispatcher.run_once()

        assert jira.issues[key]["status"] == "Done", \
            "the clear did not transition an open ticket"
        async with unit_of_work() as session:
            link = await repo.link_by_issue(session, integration["id"], key)
        assert link["closed_at"] is not None
    finally:
        await dispatcher.aclose()
        async with unit_of_work() as session:
            await cleanup(session)


async def test_a_bad_custom_field_goes_dead_rather_than_retrying(jira):
    """The most common first-run failure, end to end.

    A 400 from a create screen is the payload being wrong. Retrying it eight
    times spends eight requests of a tenant's quota to learn the same thing;
    it must go dead on the first attempt with the field error kept.
    """
    os.environ["DCIM_DATABASE_URL"] = DB_URL

    from app.core.security import encrypt_secret
    from app.db.session import unit_of_work
    from app.integrations import fingerprint as fp
    from app.integrations.dispatcher import Dispatcher
    from app.repositories import integrations as repo

    async with unit_of_work() as session:
        await cleanup(session)
        await seed(session)
        alarm_id = await raise_alarm(session)
        integration = await repo.create_integration(
            session, kind="jira_dc", name="E2E bad field",
            base_url=jira.base_url, cloud_id=None,
            config={"project_key": "DCOPS", "issue_type": "Incident",
                    "policy": {"dwell_s": 0},
                    # A field id from somebody else's site.
                    "fields": {"device": "customfield_99999"}},
            blob=encrypt_secret({"token": "e2e"}), secret_hint="token",
            secret_kind="pat", secret_expires_at=None, actor="e2e")
        await repo.update_integration(session, integration["id"],
                                      actor="e2e", enabled=True)
        wide = await repo.alarms_for_export(session, [alarm_id])
        await repo.enqueue(session, [{
            "integration_id": integration["id"], "kind": "alarm_raised",
            "fingerprint": fp.of_alarm(wide[alarm_id]), "alarm_id": alarm_id,
            "payload": wide[alarm_id]}])

    dispatcher = Dispatcher("e2e-bad")
    try:
        await dispatcher.run_once()
        async with unit_of_work() as session:
            rows = await repo.outbox_rows(session, integration["id"],
                                          state="dead")
        assert len(rows) == 1, "a 400 was not retired on the first attempt"
        assert "customfield_99999" in rows[0]["last_error"]
        assert not jira.issues
    finally:
        await dispatcher.aclose()
        async with unit_of_work() as session:
            await cleanup(session)


def test_this_file_is_not_a_substitute_for_a_real_tenant():
    """Stated as an assertion so it survives being skimmed.

    The server above answers the way Atlassian's documentation says Jira
    answers - which is precisely the assumption that cannot be checked without
    a tenant. What this file proves is that everything between the alarm table
    and the wire is correct GIVEN that assumption.
    """
    assert FakeJira.__doc__ and "documentation describes it" in FakeJira.__doc__
