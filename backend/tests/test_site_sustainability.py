"""WUE, CUE and derived outdoor humidity on the site KPI drawer.

These three are the panel's sustainability figures and every one of them is a
weaker claim than PUE: WUE counts only the water the fleet happens to meter,
CUE multiplies by a factor nobody at the site can measure, and humidity is
inferred from two thermometers rather than read off a hygrometer.

So the tests are mostly about LABELLING. The arithmetic here is a few lines and
hard to get wrong; publishing one of these without saying what it counted, what
it excluded or that it was derived is the failure that actually reaches a
sustainability report.
"""

from __future__ import annotations

from app.core.ashrae import rh_from_wet_bulb
from app.services import sites as s

# --- WUE ---------------------------------------------------------------------


def _water(litres: float, *, towers: int = 2, gaps: int = 0, samples: int = 100):
    return {"litres": litres, "towers": towers, "gaps": gaps, "samples": samples}


def test_wue_is_litres_of_makeup_over_it_kwh():
    wue = s._wue(_water(340.0), 136.0)
    assert wue["value"] == 2.5


def test_wue_says_what_it_counted_and_over_what_window():
    """The denominator and the window are the whole meaning of the ratio.

    Two sites quoting WUE over different periods are not comparable, and a
    reader cannot tell without being told.
    """
    note = s._wue(_water(340.0), 136.0)["note"]
    assert "340 L makeup" in note
    assert "136 kWh IT" in note
    assert "1 h" in note


def test_wue_names_its_method_so_it_is_not_read_as_a_totaliser():
    """The meter is a flow transmitter being integrated, not a counter being
    differenced. That distinction decides how much a missed poll costs."""
    assert s._wue(_water(340.0), 136.0)["method"] == "tower makeup, flow-integrated"


def test_an_excluded_gap_makes_the_figure_a_lower_bound_and_says_so():
    """Water the integration refused to invent is still water that was drawn.

    Silently returning a smaller number would read as an efficiency
    improvement, which is the most damaging direction for this error.
    """
    note = s._wue(_water(340.0, gaps=3), 136.0)["note"]
    assert "3 gap(s)" in note
    assert "lower bound" in note


def test_a_high_wue_is_explained_by_low_load_not_reported_as_a_leak():
    wue = s._wue(_water(900.0), 100.0)
    assert wue["value"] == 9.0
    assert "IT load is low" in wue["note"]


def test_wue_without_it_energy_is_null_with_a_reason():
    for denominator in (None, 0.0):
        wue = s._wue(_water(340.0), denominator)
        assert wue["value"] is None
        assert "IT energy" in wue["note"]


def test_wue_without_a_meter_reading_is_null_rather_than_zero():
    """No samples means the meter said nothing, NOT that no water was drawn.

    Zero litres over real IT energy is a WUE of 0.0, which would be a world
    record rather than a missing reading.
    """
    wue = s._wue(_water(0.0, samples=0), 136.0)
    assert wue["value"] is None
    assert "no makeup-water meter" in wue["note"]


# --- CUE ---------------------------------------------------------------------


def _dc(factor=0.441, basis="eGRID subregion RFCW annual total output rate"):
    attrs = {}
    if factor is not None:
        attrs["grid_carbon_kg_per_kwh"] = factor
        attrs["grid_carbon_basis"] = basis
    return {"attributes": attrs}


def test_cue_is_pue_times_the_grid_factor():
    cue = s._cue({"pue": 1.3}, _dc(0.441))
    assert cue["value"] == round(1.3 * 0.441, 3)


def test_cue_carries_the_factor_and_where_it_came_from():
    """An unattributed carbon number is worse than no carbon number."""
    note = s._cue({"pue": 1.3}, _dc(0.441))["note"]
    assert "0.441 kg CO2e/kWh" in note
    assert "eGRID subregion RFCW" in note


def test_cue_states_that_on_site_generation_is_excluded():
    """Scope 2 only. A month spent on diesel would under-report here, and the
    tile has to say so rather than let the figure stand unqualified."""
    assert "excludes on-site generation" in s._cue({"pue": 1.3}, _dc())["note"]


def test_cue_without_a_factor_is_null_and_says_it_is_published_data():
    """The reason matters: it tells the reader this is a configuration gap they
    can close, not a missing sensor they would have to install."""
    cue = s._cue({"pue": 1.3}, _dc(factor=None))
    assert cue["value"] is None
    assert "published data" in cue["note"]


def test_cue_without_pue_is_null_with_a_reason():
    cue = s._cue({"pue": None}, _dc())
    assert cue["value"] is None
    assert "PUE is unavailable" in cue["note"]


# --- derived humidity --------------------------------------------------------


def test_humidity_is_derived_from_the_dry_and_wet_bulb_pair():
    rh = rh_from_wet_bulb(15.0, 9.0)
    assert 43.0 < rh < 45.0


def test_a_saturated_pair_is_a_hundred_percent():
    assert rh_from_wet_bulb(20.0, 20.0) == 100.0


def test_a_wet_bulb_above_dry_bulb_is_refused_as_a_fault():
    """Evaporation can only depress the wet bulb. Above the dry bulb means a
    swapped sensor or a dry wick, which is a fault to raise rather than a
    humidity to publish."""
    assert rh_from_wet_bulb(10.0, 14.0) is None


def test_a_marginal_crossover_is_tolerated_as_calibration_error():
    """At saturation the two genuinely converge and ordinary sensor error puts
    them the wrong way round. Half a kelvin is noise, not a fault."""
    assert rh_from_wet_bulb(10.0, 10.3) == 100.0


def test_a_wider_depression_gives_drier_air():
    assert rh_from_wet_bulb(30.0, 28.0) > rh_from_wet_bulb(30.0, 18.0)


def test_the_weather_block_marks_humidity_as_derived_and_says_so():
    """The value is inferred, and a tile that showed it like a thermometer
    reading would turn an assumption into a measurement."""
    w = s._weather({
        "outdoor_dry_bulb_temp": {"value": 15.0, "ts": None},
        "outdoor_wet_bulb_temp": {"value": 9.0, "ts": None},
    })
    assert w["humidity_derived"] is True
    assert w["humidity_pct"] == 44.0
    assert "derived from dry/wet bulb" in w["humidity_note"]


def test_no_wet_bulb_leaves_humidity_absent_with_the_reason():
    w = s._weather({"outdoor_dry_bulb_temp": {"value": 15.0, "ts": None}})
    assert w["humidity_pct"] is None
    assert w["humidity_derived"] is False
    assert "no dry/wet bulb pair" in w["humidity_note"]


def test_a_bad_pair_reports_the_fault_rather_than_a_missing_sensor():
    w = s._weather({
        "outdoor_dry_bulb_temp": {"value": 10.0, "ts": None},
        "outdoor_wet_bulb_temp": {"value": 14.0, "ts": None},
    })
    assert w["humidity_pct"] is None
    assert "cannot be believed" in w["humidity_note"]


def test_wind_has_no_source_at_all():
    """Unlike humidity, there is nothing on a tower to derive it from."""
    w = s._weather({"outdoor_dry_bulb_temp": {"value": 15.0, "ts": None}})
    assert w["wind_speed_ms"] is None


# --- cooling headroom --------------------------------------------------------
#
# The tile sits beside power and space, so it answers THEIR question - how much
# room is left - not the staging question /cooling answers. These pin that
# difference, because the two denominators are easy to swap and the wrong one
# reads perfectly plausible.

from app.services.cooling import Chiller, Loop, PlantView, headroom  # noqa: E402


def _chw(kw: float) -> Loop:
    """A chilled-water loop carrying `kw`, at a realistic 5 K ΔT."""
    delta = 5.0
    return Loop("CHW", supply_c=7.0, return_c=7.0 + delta,
                flow_l_s=kw / (delta * 4.186))


def _chiller(name: str, rated: float | None, *, running: bool = True,
             load_kw: float = 0.0) -> Chiller:
    return Chiller(device_id=name, name=name, status="ONLINE", running=running,
                   rated_kw=rated, chw=_chw(load_kw) if load_kw else None)


def _plant(chillers: list[Chiller]) -> PlantView:
    return PlantView(chillers=chillers)


def test_no_chiller_reporting_is_null_with_a_reason():
    h = headroom(_plant([]))
    assert h["pct"] is None
    assert "no chiller" in h["note"]


def test_the_largest_machine_is_reserved_as_n_plus_one():
    """Four 800 kW machines are 3200 kW installed but only 2400 kW usable.

    Planning into the redundant chiller promises a hall cooling it loses on the
    first failure.
    """
    h = headroom(_plant([_chiller(f"CH{i}", 800.0) for i in range(4)]))
    assert "2400 kW usable, 3200 kW installed" in h["basis"]
    assert "held as N+1 spare" in h["note"]


def test_the_percentage_is_load_over_usable_not_over_installed():
    """1200 kW through a 3200 kW plant is 50 % of what can be planned into, not
    37.5 % of the nameplate total."""
    h = headroom(_plant([_chiller("CH1", 800.0, load_kw=1200.0)]
                        + [_chiller(f"CH{i}", 800.0) for i in range(2, 5)]))
    assert h["pct"] == 50.0


def test_a_standby_machine_still_counts_toward_installed_capacity():
    """Headroom is what could be grown into, so a chiller that is staged off is
    capacity. That is the whole reason this does not reuse the staging figure,
    which divides by what is RUNNING and rises when the plant stages down."""
    running_only = headroom(_plant([_chiller("CH1", 800.0, load_kw=600.0),
                                    _chiller("CH2", 800.0)]))
    with_standby = headroom(_plant([_chiller("CH1", 800.0, load_kw=600.0),
                                    _chiller("CH2", 800.0),
                                    _chiller("CH3", 800.0, running=False)]))
    assert with_standby["pct"] < running_only["pct"]


def test_a_missing_nameplate_is_declared_with_its_direction():
    """Capacity is the highest output ever observed, so a machine that has
    never run has none - which understates installed and reads HIGH."""
    h = headroom(_plant([_chiller("CH1", 800.0), _chiller("CH2", 800.0),
                         _chiller("CH3", None)]))
    assert "1 machine(s) have no nameplate" in h["note"]
    assert "reads high" in h["note"]


def test_a_plant_with_no_nameplates_at_all_is_null_not_zero():
    h = headroom(_plant([_chiller("CH1", None), _chiller("CH2", None)]))
    assert h["pct"] is None
    assert "has a nameplate yet" in h["note"]


def test_a_single_rated_machine_has_no_n_plus_one_to_measure():
    """One machine reserved as the spare leaves nothing to divide by, and 0 %
    or 100 % would both be a lie."""
    h = headroom(_plant([_chiller("CH1", 800.0), _chiller("CH2", None)]))
    assert h["pct"] is None
    assert "no N+1 capacity" in h["note"]
