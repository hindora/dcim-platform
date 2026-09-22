"""Proving it was Jira, and reading what it said.

The signature half is security code, so the tests are about the ways it can be
wrong rather than the way it is right: a body that was re-serialised, a
truncated digest, the wrong algorithm, an empty secret.

The reading half is about restraint. Most of what a webhook delivers is not a
status change, and a reader that acts on all of it re-acknowledges an alarm
every time somebody edits a description.
"""

from __future__ import annotations

import json

from app.integrations.jira import webhook

SECRET = b"a-shared-secret"


def body_of(payload: dict) -> bytes:
    return json.dumps(payload).encode()


# ------------------------------------------------------------- signatures

def test_a_correctly_signed_body_verifies():
    body = body_of({"webhookEvent": "jira:issue_updated"})
    assert webhook.verify(body, webhook.sign(body, SECRET), SECRET)


def test_a_body_re_serialised_before_verification_fails():
    """The failure mode the handler is shaped to avoid, and the reason it
    HMACs `await request.body()` rather than the parsed payload. Worse than a
    plain failure: most payloads round-trip identically, so a middleware that
    re-encodes breaks this intermittently."""
    # A room called "Salle Froide" - UTF-8 on the wire, and ï once
    # Python has re-encoded it. This is the realistic case: the payloads that
    # survive a re-serialisation are the ASCII ones, so the bug hides until a
    # customer names a room in French.
    original = '{"webhookEvent": "jira:issue_updated", "room": "Salle Froïde"}'\
        .encode()
    signature = webhook.sign(original, SECRET)
    round_tripped = json.dumps(json.loads(original)).encode()
    assert round_tripped != original
    assert not webhook.verify(round_tripped, signature, SECRET)


def test_the_wrong_secret_fails():
    body = body_of({"webhookEvent": "x"})
    assert not webhook.verify(body, webhook.sign(body, b"other"), SECRET)


def test_a_missing_or_empty_header_fails():
    body = body_of({})
    assert not webhook.verify(body, None, SECRET)
    assert not webhook.verify(body, "", SECRET)
    assert not webhook.verify(body, "sha256=", SECRET)


def test_an_empty_secret_never_verifies():
    """An integration with no webhook secret stored must refuse everything,
    not accept everything - which is what an `hmac` over b"" would do if the
    guard were missing."""
    body = body_of({})
    assert not webhook.verify(body, webhook.sign(body, b""), b"")


def test_another_algorithm_is_refused():
    body = body_of({})
    digest = webhook.sign(body, SECRET).split("=", 1)[1]
    assert not webhook.verify(body, f"sha1={digest}", SECRET)


def test_a_truncated_digest_is_refused():
    """A prefix match would make the signature guessable one byte at a time."""
    body = body_of({})
    full = webhook.sign(body, SECRET)
    assert not webhook.verify(body, full[:-8], SECRET)


def test_the_header_is_case_insensitive_about_its_hex():
    body = body_of({})
    algo, digest = webhook.sign(body, SECRET).split("=", 1)
    assert webhook.verify(body, f"{algo.upper()}={digest.upper()}", SECRET)


def test_the_redelivery_key_is_the_exact_bytes():
    assert webhook.dedup_sha(b"a") == webhook.dedup_sha(b"a")
    assert webhook.dedup_sha(b"a") != webhook.dedup_sha(b"a ")


# ---------------------------------------------------------------- reading

def updated(*, to_status="Done", category="done", resolution=None,
            from_status="In Progress", items=None):
    return {
        "webhookEvent": "jira:issue_updated",
        "user": {"accountId": "5b10a", "displayName": "Sam Rivers"},
        "issue": {"key": "DCOPS-142", "fields": {
            "status": {"name": to_status, "statusCategory": {"key": category}},
            "resolution": {"name": resolution} if resolution else None}},
        "changelog": {"items": items if items is not None else [
            {"field": "status", "fromString": from_status,
             "toString": to_status}]},
    }


def test_a_move_to_done_acknowledges():
    decision = webhook.interpret(updated(resolution="Done"))
    assert decision.action == "acknowledge"
    assert decision.issue_key == "DCOPS-142"
    assert decision.resolution == "Done"


def test_the_actor_carries_the_account_id_not_just_a_name():
    """Display names change and are not unique; an audit row has to survive
    somebody being renamed."""
    assert "5b10a" in webhook.interpret(updated()).actor


def test_a_declined_resolution_is_not_an_acknowledgement():
    """Nobody said the condition was dealt with. Acknowledging here would take
    a live fault off the console on the strength of "not this one"."""
    decision = webhook.interpret(updated(resolution="Won't Fix"))
    assert decision.action == "declined"


def test_the_declined_list_is_configurable_and_case_insensitive():
    decision = webhook.interpret(updated(resolution="NOT PLANNED"),
                                 declined=("not planned",))
    assert decision.action == "declined"


def test_a_move_to_an_open_status_is_reported_as_moved():
    """Reopen or progress is decided by the applier, which knows where the
    issue was; this payload does not carry it."""
    decision = webhook.interpret(
        updated(to_status="In Progress", category="indeterminate",
                from_status="Done"))
    assert decision.action == "moved"
    assert decision.status_category == "indeterminate"


def test_an_update_that_is_not_a_status_change_is_ignored():
    """Most of what arrives. Acting on it would re-acknowledge an alarm every
    time somebody tidies a ticket."""
    decision = webhook.interpret(updated(items=[
        {"field": "description", "fromString": "a", "toString": "b"}]))
    assert decision.action == "ignore"


def test_an_empty_changelog_is_ignored():
    assert webhook.interpret(updated(items=[])).action == "ignore"


def test_a_deleted_issue_is_reported_rather_than_ignored():
    """An alarm now links to an issue that does not exist, which means a
    condition somebody was told about has stopped being tracked."""
    decision = webhook.interpret({
        "webhookEvent": "jira:issue_deleted",
        "issue": {"key": "DCOPS-142"}})
    assert decision.action == "orphaned" and decision.issue_key == "DCOPS-142"


def test_a_comment_is_carried_through():
    decision = webhook.interpret({
        "webhookEvent": "comment_created",
        "issue": {"key": "DCOPS-142"},
        "comment": {"body": "Replaced the fan tray, watching it."}})
    assert decision.action == "comment"
    assert "fan tray" in decision.note


def test_an_adf_comment_is_flattened_rather_than_dropped():
    """A comment is the one place an engineer writes what they actually
    found, and Cloud sends it as a document."""
    decision = webhook.interpret({
        "webhookEvent": "comment_created",
        "issue": {"key": "DCOPS-142"},
        "comment": {"body": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "Filter was blocked."}]}]}}})
    assert decision.note == "Filter was blocked."


def test_an_empty_comment_is_ignored():
    assert webhook.interpret({
        "webhookEvent": "comment_created",
        "issue": {"key": "DCOPS-142"},
        "comment": {"body": ""}}).action == "ignore"


def test_an_event_we_did_not_ask_for_is_ignored():
    assert webhook.interpret({"webhookEvent": "jira:worklog_updated",
                              "issue": {"key": "X-1"}}).action == "ignore"


def test_a_field_id_spelling_of_the_changelog_item_is_understood():
    """Jira sends `field` in most payloads and `fieldId` in some."""
    decision = webhook.interpret(updated(items=[
        {"fieldId": "status", "fromString": "To Do", "toString": "Done"}]))
    assert decision.action == "acknowledge"


def test_a_status_category_is_read_rather_than_the_status_name():
    """A customer who renamed Done to Resolved must not break this."""
    decision = webhook.interpret(updated(to_status="Resolved",
                                         category="done", resolution="Fixed"))
    assert decision.action == "acknowledge"


def test_a_missing_user_does_not_crash_the_reader():
    """Automation for Jira rules post without a user block."""
    payload = updated()
    payload.pop("user")
    assert webhook.interpret(payload).actor == "jira:unknown"
