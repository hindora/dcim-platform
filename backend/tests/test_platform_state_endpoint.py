"""The trust banner's own source has to be reachable and cheap.

The banner that says "the numbers on this page cannot be trusted" is drawn on
every page, including the ones that never load a site. Hanging it off
/sites/overview would have made every page pay for a fleet-wide rollup, and -
worse - would have taken the warning away exactly when the estate query is
failing, which is when its subject is most likely true.
"""

from __future__ import annotations

from app.api.v1 import sites as route_module


def _routes() -> list[tuple[str, set[str]]]:
    """Read the sites router itself.

    The aggregate api_router wraps each include in a _IncludedRouter with no
    path of its own, so matching order has to be read where it is declared.
    """
    return [(r.path, set(getattr(r, "methods", set())))
            for r in route_module.router.routes if getattr(r, "path", None)]


def _paths() -> dict[str, set[str]]:
    return dict(_routes())


def test_the_banner_has_an_endpoint_of_its_own():
    assert "GET" in _paths()["/sites/platform/state"]


def test_it_is_not_shadowed_by_the_site_parameter_route():
    """`platform` must not be read as a datacenter id.

    /sites/{datacenter_id}/kpi is declared on the same prefix. It cannot match
    /sites/platform/state - the second segment is the literal `kpi` - but the
    two are one careless edit apart, and a shadowed route would fail as a 404
    on a banner nobody would think to look for.
    """
    paths = _paths()
    assert "/sites/{datacenter_id}/kpi" in paths
    ordered = [path for path, _ in _routes()]
    assert ordered.index("/sites/platform/state") < ordered.index(
        "/sites/{datacenter_id}/kpi"), (
        "the literal route must be declared before the parameterised one")


def test_the_state_endpoint_does_not_build_the_estate_rollup():
    """Cheapness is the point, and it is checked at the source.

    platform_health answers one question with two queries. If it ever starts
    calling the overview it stops being safe to poll from every page.
    """
    import inspect

    from app.services import sites

    src = inspect.getsource(sites.platform_health)
    for heavy in ("overview(", "site_rows", "room_rows"):
        assert heavy not in src, f"platform_health reached for {heavy}"
