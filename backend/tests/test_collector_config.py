"""What a collector may be told from the platform, and what it may not.

The line matters more than any individual field. The collector's file holds
what lets it reach this platform - its id, the API address, the token, Redis -
and none of that is settable from here. Break the path to the control plane
from the control plane and nobody can repair it from the control plane either;
somebody drives to the site. Zabbix draws the line in the same place: a proxy's
ServerActive lives in its config file and no part of the frontend can move it.
"""

from __future__ import annotations

import pytest

from app.services import collector_config as cfg
from app.services.collector_config import CollectorConfigError

# ------------------------------------------------------- what is off limits


def test_nothing_that_reaches_the_platform_is_settable():
    """The rule this module exists to enforce.

    A settable Redis URL or API address is one typo away from a collector that
    can no longer be told anything - including the correction.
    """
    for forbidden in ("dcim", "redis", "collector", "mappings", "publisher",
                      "limits", "observability"):
        with pytest.raises(CollectorConfigError):
            cfg.validate({forbidden: {"base_url": "http://elsewhere"}})


def test_an_unknown_field_inside_a_known_section_is_refused():
    """Accepting it would store a setting no collector will ever read."""
    with pytest.raises(CollectorConfigError):
        cfg.validate({"snmp": {"max_repetitions": 25}})


# ------------------------------------------------------------- the values


def test_a_listener_needs_a_host_and_a_port():
    """host:port, with the host half genuinely free.

    0.0.0.0 accepts on every interface; a single address accepts on the
    management network and nowhere else, which is a real hardening choice on a
    multi-homed collector.
    """
    clean = cfg.validate({"snmp_trap": {"listen": "0.0.0.0:1162"}})
    assert clean["snmp_trap"]["listen"] == "0.0.0.0:1162"
    assert cfg.validate({"snmp_trap": {"listen": "10.50.0.9:162"}})

    for bad in ("1162", "0.0.0.0:", "0.0.0.0:x", "0.0.0.0:70000"):
        with pytest.raises(CollectorConfigError):
            cfg.validate({"snmp_trap": {"listen": bad}})


def test_seconds_are_bounded_on_both_sides():
    assert cfg.validate({"snmp": {"timeout_s": 6}})["snmp"]["timeout_s"] == 6
    with pytest.raises(CollectorConfigError):
        cfg.validate({"snmp": {"timeout_s": 0}})
    with pytest.raises(CollectorConfigError):
        cfg.validate({"snmp": {"timeout_s": 600}})


def test_a_null_clears_an_override_rather_than_storing_one():
    """Absent means "the collector's file decides".

    That is what lets a default change in a release reach every collector that
    never overrode it, instead of every collector carrying a frozen copy of
    the defaults that were current when somebody first opened this page.
    """
    assert cfg.validate({"snmp": {"timeout_s": None}}) == {}


def test_zero_is_a_value_where_zero_means_something():
    """BACnet local port 0 asks the kernel for an ephemeral port, which is the
    sensible default - 47808 is needed only to receive broadcasts."""
    assert cfg.validate({"bacnet": {"local_port": 0}}) == {"bacnet": {"local_port": 0}}


def test_disabling_a_plane_is_expressible():
    assert cfg.validate({"bacnet": {"enabled": False}}) \
        == {"bacnet": {"enabled": False}}


# -------------------------------------------------- applied versus stored


def test_the_trap_block_is_the_only_thing_that_applies_live():
    """Everything else is read once, when the adapters are built.

    The trap receiver owns its socket and its workers, so it can be closed and
    reopened in place. Reporting a concurrency change as applied would describe
    an estate that does not exist.
    """
    live = {p.split(".")[0] for p in cfg.LIVE_PATHS}
    assert live == {"snmp_trap"}


def test_a_restart_requiring_change_is_named_not_counted():
    """"3 settings need a restart" is not something an operator can act on."""
    pending = cfg.restart_needed({}, {"snmp": {"max_concurrent": 96},
                                      "bacnet": {"local_port": 47808}})
    assert any("Max concurrent" in p for p in pending)
    assert any("Local port" in p for p in pending)


def test_moving_the_trap_listener_needs_no_restart():
    assert cfg.restart_needed({}, {"snmp_trap": {"listen": "0.0.0.0:162"}}) == []


def test_setting_a_value_back_to_what_it_was_is_not_pending():
    before = {"snmp": {"max_concurrent": 96}}
    assert cfg.restart_needed(before, before) == []


# ------------------------------------------------------------ the schema


def test_the_schema_travels_with_the_data():
    """So the form is not a second copy of the rules, drifting from the one
    the server validates against."""
    described = cfg.describe()
    keys = {s["key"] for s in described["sections"]}
    assert keys == set(cfg.SCHEMA)
    for section in described["sections"]:
        for field in section["fields"]:
            assert field["when"] in (cfg.LIVE, cfg.ON_RESTART)
            assert field["help"], f"{section['key']}.{field['key']} has no help"
            # One line, because it is on screen permanently and next to
            # thirty others. The reasoning goes in `detail`, which is a
            # hover away - read once, by whoever decides to touch the
            # setting, rather than every time by everyone.
            assert len(field["help"]) <= 70, (
                f"{section['key']}.{field['key']} help is "
                f"{len(field['help'])} chars: move the reasoning to detail")


def test_the_listeners_are_marked_as_dangerous():
    """Moving one silences a plane with no error anywhere - every device keeps
    sending to the old address. The UI needs to know which sections carry that
    consequence so it can say so before saving."""
    dangerous = {s["key"] for s in cfg.describe()["sections"] if s["danger"]}
    assert dangerous == {"snmp_trap", "redfish_event"}


# ---------------------------------------------- what the form has to show


def test_gnmi_offers_no_retries_because_the_collector_has_none():
    """A Get is one RPC over a pooled connection; a Subscribe reconnects.

    GNMICfg has no Retries field, so offering the setting would store a value
    nothing ever reads - the same fault as a metric group no mapping defines,
    and just as invisible.
    """
    assert "retries" not in cfg.SCHEMA["gnmi"]["fields"]
    assert "retries" in cfg.SCHEMA["snmp"]["fields"]
    assert "retries" in cfg.SCHEMA["bacnet"]["fields"]


def test_every_settable_field_can_be_reported_back():
    """The collector reports its effective config under these exact keys.

    The form puts a real value in front of every field by looking the key up in
    that report, so a key present here and absent there is a field that renders
    empty for ever - which is precisely the bug this pairing prevents.
    """
    # Mirrors collector/internal/config/effective.go.
    reported = {
        "snmp": {"enabled", "max_concurrent", "per_host", "timeout_s", "retries"},
        "redfish": {"enabled", "max_concurrent", "per_host", "timeout_s", "retries"},
        "modbus": {"enabled", "max_concurrent", "per_host", "timeout_s", "retries"},
        "bacnet": {"enabled", "max_concurrent", "per_host", "timeout_s", "retries",
                   "local_port"},
        "gnmi": {"enabled", "max_concurrent", "per_host", "timeout_s", "stream"},
        "snmp_trap": {"enabled", "listen", "workers", "rate_limit_per_minute"},
        "redfish_event": {"enabled", "listen", "advertise", "tls"},
    }
    for section, spec in cfg.SCHEMA.items():
        missing = set(spec["fields"]) - reported[section]
        assert not missing, (
            f"{section}: {missing} is settable but the collector reports no "
            f"value for it, so the field would render empty for ever")
