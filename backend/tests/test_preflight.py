"""docs/26 Phase 8's preflight pass/fail rule. Pure, no DB."""

from __future__ import annotations

from app.api.v1.collector import preflight_passed
from app.schemas import PreflightCheck


def chk(status: str) -> PreflightCheck:
    return PreflightCheck(check="x", status=status)


def test_all_ok_passes():
    assert preflight_passed([chk("ok"), chk("ok")]) is True


def test_a_skipped_check_does_not_fail_the_run():
    """No pool assigned yet to sample, say - neither pass nor fail on its
    own, and must not drag an otherwise-clean run down."""
    assert preflight_passed([chk("ok"), chk("skipped")]) is True


def test_a_warn_fails_the_run():
    """A passing run should mean nothing left to fix, not "nothing
    outright failed" - warn still blocks a clean pass."""
    assert preflight_passed([chk("ok"), chk("warn")]) is False


def test_a_fail_fails_the_run():
    assert preflight_passed([chk("ok"), chk("fail")]) is False


def test_no_checks_at_all_passes_vacuously():
    assert preflight_passed([]) is True
