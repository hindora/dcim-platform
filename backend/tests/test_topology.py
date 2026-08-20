"""Topology scope, layer and SQL-shape rules.

These are the parts that decide what a caller gets back before a single row is
read, so they are worth pinning without a database.
"""

from __future__ import annotations

import uuid

import pytest

from app.repositories import topology as repo
from app.services import topology as svc

ROOM = str(uuid.uuid4())


# --- scope -------------------------------------------------------------------

@pytest.mark.parametrize("scope,kind", [
    (f"room:{ROOM}", "room"),
    (f"RACK:{ROOM}", "rack"),          # case is not the caller's problem
    (f"datacenter:{ROOM}", "datacenter"),
    (f"device:{ROOM}", "device"),
])
def test_scope_parses_every_supported_anchor(scope, kind):
    got_kind, got_id = svc.parse_scope(scope)
    assert (got_kind, got_id) == (kind, ROOM)


@pytest.mark.parametrize("scope", [
    "room",                       # no id
    f"room:{ROOM}extra",          # not a uuid
    "floor:" + ROOM,              # not a scope type
    f"{ROOM}",                    # bare id, no type
    "room:",                      # empty id
])
def test_bad_scopes_are_rejected_before_sql(scope):
    """A malformed scope must fail here, not as a cast error from Postgres.

    Passing it through would surface as a 500 with a message about
    invalid input syntax for type uuid, which tells the caller nothing about
    the parameter they actually got wrong.
    """
    with pytest.raises(svc.TopologyError):
        svc.parse_scope(scope)


def test_scope_id_is_validated_as_a_uuid_not_just_non_empty():
    # 'or 1=1' style input reaches a CAST otherwise.
    with pytest.raises(svc.TopologyError):
        svc.parse_scope("room:' OR 1=1 --")


# --- layers ------------------------------------------------------------------

def test_network_is_an_alias_for_the_production_enum():
    """The API spec and operators say 'network'; layer_t says 'production'."""
    assert svc.resolve_layer("network") == "production"
    assert svc.resolve_layer("production") == "production"


@pytest.mark.parametrize("layer", ["power", "cooling", "management", "fieldbus"])
def test_layers_map_to_themselves(layer):
    assert svc.resolve_layer(layer) == layer


def test_physical_is_refused_with_an_explanation_not_an_empty_graph():
    """Physical containment is not in the connection table.

    Returning an empty graph would read as "nothing is racked in this room",
    which is a far more alarming answer than "ask a different endpoint".
    """
    with pytest.raises(svc.TopologyError) as err:
        svc.resolve_layer("physical")
    assert "elevation" in str(err.value) or "floor plan" in str(err.value)


def test_unknown_layer_lists_what_is_available():
    with pytest.raises(svc.TopologyError) as err:
        svc.resolve_layer("banana")
    for expected in ("power", "cooling", "physical"):
        assert expected in str(err.value)


# --- SQL shape ---------------------------------------------------------------

def test_every_scope_type_has_seed_sql():
    """A scope the service accepts but the repository cannot seed is a 500."""
    assert set(repo._SEED_SQL) == svc.SCOPE_TYPES


@pytest.mark.parametrize("scope_type", sorted(svc.SCOPE_TYPES))
def test_node_sql_is_parameterised_and_bounded(scope_type):
    sql = repo._nodes_sql(scope_type)
    # Bound parameters only - the scope id must never be interpolated.
    assert ":scope_id" in sql
    assert ":layer" in sql and ":depth" in sql and ":cap" in sql
    # Depth has to bound the recursion or the CTE walks the whole graph.
    assert "r.depth < :depth" in sql
    # UNION, not UNION ALL: the power graph has cycles through dual feeds.
    assert "UNION\n" in sql and "UNION ALL" not in sql
    assert "LIMIT :cap" in sql


@pytest.mark.parametrize("scope_type", ["room", "datacenter"])
def test_containment_seeds_fall_back_to_device_room(scope_type):
    """Floor-standing plant sits in a room but in no rack.

    Chillers, switchgear and UPS are exactly what the power and cooling layers
    are about, and they have rack_id NULL. Seeding only through the rack chain
    would silently drop them.
    """
    assert "COALESCE(rr.room_id, d.room_id)" in repo._SEED_SQL[scope_type]


def test_seeds_exclude_decommissioned_devices():
    for scope_type in ("rack", "room", "datacenter"):
        assert "decommissioned" in repo._SEED_SQL[scope_type]


def test_edges_are_restricted_to_both_endpoints():
    """An edge to a node the client was not given renders as a line to nowhere."""
    assert "c.a_device_id = ANY" in repo._EDGES_SQL
    assert "c.b_device_id = ANY" in repo._EDGES_SQL


def test_anchor_nodes_outrank_degree_when_truncating():
    """The device you asked about must survive its own query."""
    sql = repo._nodes_sql("device")
    assert "ORDER BY (g.depth = 0) DESC" in sql
