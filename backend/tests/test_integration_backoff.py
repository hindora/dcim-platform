"""When to try again, and when to stop.

Two properties matter more than the arithmetic:

* `Retry-After` is obeyed VERBATIM - not jittered, not capped, not shortened.
  Atlassian documents that retrying early returns the same or a longer
  header, so guessing sooner is slower as well as rude.
* A 400 never retries. It is the payload being wrong - a custom field that is
  not on the project's create screen, a renamed priority - and eight identical
  attempts spend eight requests of the tenant's hourly quota to learn the same
  thing, then bury the one useful error under seven copies.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from app.integrations import backoff

# ---------------------------------------------------------------- schedule

def test_the_delay_grows_and_then_stops_growing():
    rand = random.Random(0)
    delays = [backoff.delay_s(n, rand=rand) for n in range(1, 12)]
    # Monotone in the underlying schedule, which jitter can locally violate -
    # so compare the ends rather than adjacent pairs.
    assert delays[0] < delays[5]
    assert all(d <= backoff.CAP_S * backoff.JITTER[1] for d in delays)


def test_the_first_failure_waits_about_the_base():
    rand = random.Random(1)
    delay = backoff.delay_s(1, rand=rand)
    assert backoff.BASE_S * backoff.JITTER[0] <= delay \
        <= backoff.BASE_S * backoff.JITTER[1]


def test_jitter_actually_varies():
    """Not decoration. A storm enqueues hundreds of rows, they all fail
    against the same 429, and an unjittered schedule marches the whole batch
    back at Atlassian in lockstep - a self-inflicted thundering herd against
    the limiter that is already unhappy."""
    values = {backoff.delay_s(4, rand=random.Random(seed)) for seed in range(20)}
    assert len(values) > 10


def test_retry_after_is_obeyed_exactly():
    assert backoff.delay_s(1, retry_after=90.0) == 90.0
    # Not capped either, even well past CAP_S: the server said so.
    assert backoff.delay_s(8, retry_after=3600.0) == 3600.0


def test_retry_after_of_zero_is_honoured_rather_than_falling_through():
    """0 is a valid instruction - "you may go now" - and treating it as absent
    would substitute a two-second guess for an explicit answer."""
    assert backoff.delay_s(3, retry_after=0.0) == 0.0


def test_next_attempt_at_is_in_the_future():
    now = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    when = backoff.next_attempt_at(1, retry_after=30.0, now=now)
    assert when == now + timedelta(seconds=30)


# ------------------------------------------------------------ retryability

@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 408, 409])
def test_transient_statuses_retry(status):
    assert backoff.is_retryable(status)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
def test_a_statement_about_the_request_does_not_retry(status):
    assert not backoff.is_retryable(status)


def test_no_status_at_all_always_retries():
    """DNS, TLS, connect, read timeout. Nothing was said about the request."""
    assert backoff.is_retryable(None)


# ------------------------------------------------------------- Retry-After

def test_delay_seconds_form():
    assert backoff.parse_retry_after("120") == 120.0


def test_http_date_form():
    """RFC 9110 allows both, and Atlassian has been seen sending both. A
    parser that handles only the integer form returns None on the date form
    and silently falls back to the schedule - which usually works and
    occasionally hammers a tenant that asked for a two-minute pause."""
    now = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    header = format_datetime(now + timedelta(seconds=90), usegmt=True)
    assert backoff.parse_retry_after(header, now=now) == pytest.approx(90, abs=1)


def test_a_date_in_the_past_is_clamped_to_zero():
    now = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    header = format_datetime(now - timedelta(seconds=90), usegmt=True)
    assert backoff.parse_retry_after(header, now=now) == 0.0


@pytest.mark.parametrize("value", [None, "", "   ", "soon"])
def test_an_unusable_header_falls_back_to_the_schedule(value):
    assert backoff.parse_retry_after(value) is None
