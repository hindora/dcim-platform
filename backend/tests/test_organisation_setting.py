"""The organisation name: saved in Settings, else DCIM_ORG_NAME, else the product."""

from __future__ import annotations

import pytest

from app.services import instance_settings as org


def test_a_saved_name_wins_over_the_environment():
    assert org.resolve("Acme Hosting", "Oracle Corp.") == ("Acme Hosting", "settings")


def test_no_saved_name_falls_back_to_the_environment_then_the_product():
    assert org.resolve(None, "Oracle Corp.") == ("Oracle Corp.", "environment")
    assert org.resolve(None, "   ") == (org.PRODUCT_NAME, "default")
    assert org.resolve(None, None) == (org.PRODUCT_NAME, "default")


def test_whitespace_collapses_and_blank_means_not_set():
    assert org.clean_org_name("  Acme\n  Hosting\t") == "Acme Hosting"
    assert org.clean_org_name("   ") is None
    assert org.clean_org_name("") is None
    assert org.clean_org_name(None) is None


def test_a_name_too_long_for_a_heading_is_refused_in_plain_words():
    assert org.clean_org_name("x" * org.MAX_ORG_NAME) == "x" * org.MAX_ORG_NAME
    with pytest.raises(org.OrgNameError, match="limit is 80"):
        org.clean_org_name("x" * (org.MAX_ORG_NAME + 1))


def test_control_characters_are_refused_not_stripped():
    with pytest.raises(org.OrgNameError, match="control character"):
        org.clean_org_name("Acme\x00Hosting")


def test_non_latin_names_are_kept_as_typed():
    assert org.clean_org_name("Société Générale") == "Société Générale"
    assert org.clean_org_name("株式会社データ") == "株式会社データ"


def test_renaming_needs_an_admin_and_a_token():
    from fastapi.testclient import TestClient

    from app.core.security import issue_token
    from app.main import app

    with TestClient(app) as client:
        assert client.put("/api/v1/settings/organisation",
                          json={"org_name": "Acme"}).status_code == 401
        viewer = issue_token("someone", "viewer")
        r = client.put("/api/v1/settings/organisation", json={"org_name": "Acme"},
                       headers={"Authorization": f"Bearer {viewer}"})
        assert r.status_code == 403
