"""Email notifications: relay settings, routing rules, messages, and sending."""

from __future__ import annotations

import socketserver
import threading
from datetime import UTC, datetime

import pytest

from app.notifications import message as msg
from app.notifications import rules, smtp

# ------------------------------------------------------------------ a relay


class _Relay(socketserver.StreamRequestHandler):
    """Just enough SMTP to receive a message. Refuses any address with 'reject'."""

    def _say(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())

    def handle(self) -> None:
        self._say("220 test relay")
        while True:
            line = self.rfile.readline().decode().strip()
            if not line:
                return
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                self._say("250-test relay")
                self._say("250 8BITMIME")
            elif verb == "MAIL":
                self._say("250 ok")
            elif verb == "RCPT":
                self._say("550 no such user" if "reject" in line else "250 ok")
            elif verb == "DATA":
                self._say("354 go")
                body = []
                while (chunk := self.rfile.readline().decode()) != ".\r\n":
                    body.append(chunk)
                self.server.received.append("".join(body))  # type: ignore[attr-defined]
                self._say("250 queued")
            elif verb == "QUIT":
                self._say("221 bye")
                return
            else:
                self._say("502 not implemented")


@pytest.fixture
def relay():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Relay)
    server.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _cfg(port: int, **kw) -> smtp.SmtpConfig:
    base = {
        "enabled": True,
        "host": "127.0.0.1",
        "port": port,
        "security": "none",
        "from_address": "dcim@example.com",
    }
    return smtp.SmtpConfig(**(base | kw))


# ------------------------------------------------------------------ settings


def test_a_login_over_a_plain_connection_is_refused():
    with pytest.raises(smtp.SmtpSettingsError, match="clear text"):
        smtp.clean_settings(
            {
                "host": "relay.example.com",
                "security": "none",
                "username": "dcim",
                "password": "pw",
                "from_address": "a@b.co",
            },
            smtp.SmtpConfig(),
        )


def test_email_cannot_be_turned_on_without_a_host_and_sender():
    with pytest.raises(smtp.SmtpSettingsError, match="host and from address"):
        smtp.clean_settings({"enabled": True}, smtp.SmtpConfig())


def test_the_password_is_write_only_and_its_hint_says_nothing_about_it():
    cur = smtp.SmtpConfig(username="dcim", password="old-secret", password_hint="saved")
    kept = smtp.clean_settings({"host": "relay.example.com"}, cur)
    assert kept.password == "old-secret"
    changed = smtp.clean_settings({"password": "hunter22"}, cur)
    assert changed.password == "hunter22" and "22" not in changed.password_hint
    assert "hunter" not in str(changed.public())
    cleared = smtp.clean_settings({"password": ""}, cur)
    assert cleared.password is None and cleared.public()["password_set"] is False


def test_hosts_and_addresses_are_checked():
    for bad in ({"host": "smtp://relay"}, {"from_address": "not-an-address"}, {"port": 70000}):
        with pytest.raises(smtp.SmtpSettingsError):
            smtp.clean_settings(bad, smtp.SmtpConfig())


# ------------------------------------------------------------------ rules


def test_recipients_split_dedupe_and_refuse_typos():
    assert rules.clean_recipients("noc@x.com; Facilities@x.com\nnoc@X.com") == [
        "noc@x.com",
        "Facilities@x.com",
    ]
    with pytest.raises(rules.RuleError, match="not an email address"):
        rules.clean_recipients("noc@x.com, bob")
    with pytest.raises(rules.RuleError, match="at least one"):
        rules.clean_recipients("  ")
    with pytest.raises(rules.RuleError, match="at most 20"):
        rules.clean_recipients([f"u{i}@x.com" for i in range(21)])


def test_a_rule_uses_the_ticketing_policy_clauses():
    out = rules.clean(
        {
            "name": " NOC  on-call ",
            "recipients": "noc@x.com",
            "policy": {"min_severity": "CRITICAL", "categories": ["power"]},
        }
    )
    assert out["name"] == "NOC on-call"
    assert out["policy"] == {"min_severity": "CRITICAL", "categories": ["power"]}
    with pytest.raises(rules.RuleError):
        rules.clean({"name": "x", "recipients": "a@b.co", "policy": {"min_severity": "LOUD"}})
    with pytest.raises(rules.RuleError, match="matches nothing"):
        rules.clean({"name": "x", "recipients": "a@b.co", "policy": {"categories": []}})
    with pytest.raises(rules.RuleError, match="at most an hour"):
        rules.clean({"name": "x", "recipients": "a@b.co", "policy": {"dwell_s": 7200}})


def test_an_email_rule_does_not_wait_by_default():
    assert rules.resolved_policy({})["dwell_s"] == 0


# ------------------------------------------------------------------ message

ALARM = {
    "id": "a1",
    "severity": "CRITICAL",
    "message": "Input current 31.2 A above 30 A",
    "alarm_type": "input_current_high",
    "device_name": "DC1-PDU-R12-A",
    "device_type": "pdu",
    "mgmt_ip": "10.50.1.23",
    "datacenter_code": "DC1",
    "room_name": "Server Hall A",
    "rack_name": "R12",
    "u_start": 40,
    "trigger_value": 31.2,
    "threshold": 30.0,
    "category": "power",
    "response_class": "alarm",
    "first_seen": datetime(2026, 11, 2, 9, 14, tzinfo=UTC),
    "cleared_at": datetime(2026, 11, 2, 9, 40, tzinfo=UTC),
    "occurrence_count": 3,
}


def test_the_subject_carries_severity_device_and_condition():
    assert msg.subject("alarm_raised", ALARM) == (
        "[CRITICAL] DC1-PDU-R12-A: Input current 31.2 A above 30 A"
    )
    assert msg.subject("alarm_cleared", ALARM).startswith("[CLEARED] DC1-PDU-R12-A")


def test_the_body_leads_with_where_and_links_back():
    text = msg.body(
        "alarm_raised",
        ALARM,
        rule_name="NOC",
        rule_summary="CRITICAL and MAJOR alarms",
        org_name="Acme",
        link_base_url="https://dcim.example.com",
    )
    assert text.startswith("CRITICAL raised at 2026-11-02 09:14 UTC")
    assert "DC1 / Server Hall A / R12, U40" in text
    assert "31.2 (threshold 30)" in text
    assert "https://dcim.example.com/alarms/a1" in text
    assert "rule “NOC”" in text


def test_a_rule_summary_reads_as_a_phrase():
    p = rules.resolved_policy({"min_severity": "MAJOR"})
    assert msg.rule_summary(p, notify_on_clear=True) == (
        "CRITICAL and MAJOR alarms, and when they clear"
    )
    p = rules.resolved_policy({"min_severity": "MINOR", "categories": ["power"], "dwell_s": 300})
    assert msg.rule_summary(p, notify_on_clear=False) == (
        "MINOR or worse alarms in power active for 5 min"
    )


# ------------------------------------------------------------------ sending


def test_a_message_reaches_the_relay_with_its_headers(relay):
    cfg = _cfg(relay.server_address[1])
    m = smtp.base_message(cfg, "Acme", ["noc@x.com"], "subject here", "body here")
    assert smtp.send_batch(cfg, [m]) == [None]
    raw = relay.received[0]
    assert "Subject: subject here" in raw
    assert "Auto-Submitted: auto-generated" in raw
    assert "Acme DCIM <dcim@example.com>" in raw


def test_a_refused_recipient_is_permanent_and_not_retried(relay):
    cfg = _cfg(relay.server_address[1])
    m = smtp.base_message(cfg, "Acme", ["reject@x.com"], "s", "b")
    (err,) = smtp.send_batch(cfg, [m])
    assert err is not None and err.permanent


def test_starttls_on_a_relay_without_it_is_explained(relay):
    cfg = _cfg(relay.server_address[1], security="starttls")
    m = smtp.base_message(cfg, "Acme", ["noc@x.com"], "s", "b")
    (err,) = smtp.send_batch(cfg, [m])
    assert err is not None and "STARTTLS" in str(err)


def test_an_unreachable_relay_is_retried():
    cfg = _cfg(1)  # nothing listens on port 1
    m = smtp.base_message(cfg, "Acme", ["noc@x.com"], "s", "b")
    (err,) = smtp.send_batch(cfg, [m])
    assert err is not None and not err.permanent


def test_notification_endpoints_need_the_right_role():
    from fastapi.testclient import TestClient

    from app.core.security import issue_token
    from app.main import app

    viewer = {"Authorization": f"Bearer {issue_token('v', 'viewer')}"}
    operator = {"Authorization": f"Bearer {issue_token('o', 'operator')}"}
    with TestClient(app) as client:
        assert client.get("/api/v1/notifications/rules").status_code == 401
        assert client.get("/api/v1/notifications/rules", headers=viewer).status_code == 403
        r = client.post(
            "/api/v1/notifications/rules",
            headers=operator,
            json={"name": "x", "recipients": "a@b.co"},
        )
        assert r.status_code == 403
        assert client.put("/api/v1/settings/smtp", headers=operator, json={}).status_code == 403


def test_a_clear_with_no_recorded_time_does_not_say_unknown():
    a = {**ALARM, "cleared_at": None}
    text = msg.body(
        "alarm_cleared", a, rule_name="NOC", rule_summary="x", org_name="Acme", link_base_url=""
    )
    assert text.startswith("Alarm cleared\n")
    assert "unknown" not in text.splitlines()[0]


def test_a_host_address_loses_its_prefix_length():
    a = {**ALARM, "mgmt_ip": "10.52.11.32/32"}
    text = msg.body(
        "alarm_raised", a, rule_name="NOC", rule_summary="x", org_name="Acme", link_base_url=""
    )
    assert "(pdu, 10.52.11.32)" in text


def test_an_ordinary_subject_stays_on_one_header_line(relay):
    cfg = _cfg(relay.server_address[1])
    subject = "[MAJOR] PDUA-DC1-HA-R2-03: PDU at 84.91% of its nameplate, above 80.0%"
    m = smtp.base_message(cfg, "Acme", ["noc@x.com"], subject, "b")
    assert smtp.send_batch(cfg, [m]) == [None]
    assert f"Subject: {subject}\r\n" in relay.received[0]
