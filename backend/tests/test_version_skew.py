"""docs/26 Phase 7's skew classification. Pure, no DB, no clock."""

from __future__ import annotations

from app.services import version_skew as vs


def test_the_current_release_is_current():
    assert vs.classify("2.5.0", "2.5.0") == vs.CURRENT


def test_a_patch_difference_is_still_current():
    """PATCH never counts toward skew - only MAJOR.MINOR generations do."""
    assert vs.classify("2.5.9", "2.5.0") == vs.CURRENT
    assert vs.classify("2.5.0", "2.5.9") == vs.CURRENT


def test_one_minor_behind_is_supported():
    assert vs.classify("2.5.0", "2.4.0") == vs.SUPPORTED


def test_two_minors_behind_is_outdated():
    assert vs.classify("2.5.0", "2.3.0") == vs.OUTDATED


def test_three_minors_behind_is_rejected():
    assert vs.classify("2.5.0", "2.2.0") == vs.REJECTED


def test_a_whole_major_version_behind_is_rejected_regardless_of_minor():
    """A collector on 1.9 talking to a 2.0 platform is not "9 generations
    ahead of N-2" by the minor-number arithmetic alone - it is a different
    major version, and that is always rejected."""
    assert vs.classify("2.0.0", "1.9.0") == vs.REJECTED


def test_a_collector_ahead_of_the_platform_is_current_not_an_error():
    assert vs.classify("2.5.0", "2.6.0") == vs.CURRENT
    assert vs.classify("2.5.0", "3.0.0") == vs.CURRENT


def test_a_dev_build_is_unknown_not_rejected():
    """main.go's default for any build that did not go through a real
    release. Punishing it as ancient would alarm on every developer
    checkout and every collector built from source; trusting it as current
    would defeat the point of having a policy at all. Neither - it is
    simply outside what this policy can classify."""
    assert vs.classify("2.5.0", "dev") == vs.UNKNOWN


def test_a_missing_collector_version_is_unknown():
    assert vs.classify("2.5.0", None) == vs.UNKNOWN
    assert vs.classify("2.5.0", "") == vs.UNKNOWN


def test_an_unparseable_platform_version_is_also_unknown():
    assert vs.classify("not-a-version", "2.5.0") == vs.UNKNOWN


def test_parse_accepts_a_leading_v():
    assert vs.parse("v2.5.0") == vs.Version(major=2, minor=5)


def test_parse_ignores_a_prerelease_suffix():
    assert vs.parse("2.5.0-rc1") == vs.Version(major=2, minor=5)
