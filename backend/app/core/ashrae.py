"""The ASHRAE TC 9.9 envelope, as something a row can be scored against.

The thermal page has always called its headline figure "ASHRAE compliance"
while checking one of the three things ASHRAE writes down. The envelope is not
a temperature range. It is:

  * DRY BULB at the equipment intake,
  * MOISTURE, capped twice - a dew-point ceiling and a relative-humidity
    ceiling, with a dew-point FLOOR underneath,
  * RATE OF CHANGE, because thermal shock and condensation damage hardware
    whether or not the air ever leaves the band.

A hall can hold 22 C every hour of the year and be outside the envelope on
moisture alone. A hall can sit at 26 C and be fine, or be four minutes into a
cooling loss climbing 30 K/hour, and the dry-bulb reading is the same number
in both.

WHICH CLASS decides the allowable envelope, and the recommended envelope is
the same for all of them - that is the point of "recommended". A1 is the
default because it is the tightest and because an estate that has not said
otherwise should be graded against the strictest thing its kit might be.

The moisture ceiling is a PAIR and both bind: 60 % RH or a 15 C dew point,
whichever is reached first. At 20 C, 60 % RH is a dew point of 12.0 C and the
humidity limit bites; at 27 C the same 60 % is 18.6 C dew point and the dew
point limit bites four kelvin earlier. Checking only RH, which is what this
platform collected until now, passes a hall that ASHRAE fails.

Source: ASHRAE TC 9.9, "Thermal Guidelines for Data Processing Environments",
classes A1-A4, intake air.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

#: Equipment classes, tightest allowable first. `A1` is the default for a room
#: that has not been classified.
CLASSES = ("A1", "A2", "A3", "A4")
DEFAULT_CLASS = "A1"


@dataclass(frozen=True)
class Envelope:
    """One class's limits. Temperatures in °C, humidity in %, rate in K/hour."""

    name: str
    #: The recommended band - the same for every class, and the one the
    #: headline figure is scored against. Outside it is inefficient; outside
    #: ALLOWABLE is where hardware is at risk.
    rec_low_c: float
    rec_high_c: float
    allow_low_c: float
    allow_high_c: float
    #: Moisture, recommended. The floor is a dew point; the ceiling is a dew
    #: point AND a relative humidity, whichever binds first.
    rec_dp_low_c: float
    rec_dp_high_c: float
    rec_rh_high_pct: float
    #: Moisture, allowable. Wider on every axis, and the RH floor appears -
    #: below 8 % a floor builds static charge whatever the temperature.
    allow_dp_low_c: float
    allow_dp_high_c: float
    allow_rh_low_pct: float
    allow_rh_high_pct: float
    #: Maximum rate of change. 20 K/hour for these classes; a room holding tape
    #: is held to 5, which is why this is per-room rather than a constant.
    max_rate_k_per_h: float = 20.0


#: The recommended band does not vary by class - only what the equipment will
#: SURVIVE does. Writing it out per class anyway, because a reader checking one
#: row against the standard should not have to know that.
_REC = {"rec_low_c": 18.0, "rec_high_c": 27.0,
        "rec_dp_low_c": -9.0, "rec_dp_high_c": 15.0, "rec_rh_high_pct": 60.0}

ENVELOPES: dict[str, Envelope] = {
    "A1": Envelope(name="A1", allow_low_c=15.0, allow_high_c=32.0,
                   allow_dp_low_c=-12.0, allow_dp_high_c=17.0,
                   allow_rh_low_pct=8.0, allow_rh_high_pct=80.0, **_REC),
    "A2": Envelope(name="A2", allow_low_c=10.0, allow_high_c=35.0,
                   allow_dp_low_c=-12.0, allow_dp_high_c=21.0,
                   allow_rh_low_pct=8.0, allow_rh_high_pct=80.0, **_REC),
    # A3 and A4 widen the ceiling for kit built to run hot, and their moisture
    # ceiling rises with it. The RH floor stays at 8 %: static does not care
    # how hot the room is rated.
    "A3": Envelope(name="A3", allow_low_c=5.0, allow_high_c=40.0,
                   allow_dp_low_c=-12.0, allow_dp_high_c=24.0,
                   allow_rh_low_pct=8.0, allow_rh_high_pct=85.0, **_REC),
    "A4": Envelope(name="A4", allow_low_c=5.0, allow_high_c=45.0,
                   allow_dp_low_c=-12.0, allow_dp_high_c=24.0,
                   allow_rh_low_pct=8.0, allow_rh_high_pct=90.0, **_REC),
}

#: Rate limit for a room that holds tape. Tape is the one medium in a hall that
#: a fast swing destroys rather than merely stresses.
TAPE_RATE_K_PER_H = 5.0


def envelope_for(ashrae_class: str | None,
                 max_rate_k_per_h: float | None = None) -> Envelope:
    """The envelope a room is graded against. Unclassified means A1."""
    env = ENVELOPES.get((ashrae_class or DEFAULT_CLASS).upper().strip(),
                        ENVELOPES[DEFAULT_CLASS])
    if max_rate_k_per_h is not None and max_rate_k_per_h != env.max_rate_k_per_h:
        return Envelope(**{**env.__dict__, "max_rate_k_per_h": max_rate_k_per_h})
    return env


# ------------------------------------------------------------------ moisture
#: Magnus-Tetens with the Sonntag coefficients, the same relation the simulator
#: publishes its probes with. Kept here too because dew point is DERIVED on
#: this side for every probe that does not publish one - a Raritan DPX2 is a
#: thermistor and a humidity element on an RJ-12 lead, and no amount of asking
#: will get a dew point out of it.
_MAGNUS_B = 17.62
_MAGNUS_C = 243.12


def dew_point_c(dry_bulb_c: float, rh_pct: float) -> float:
    """Dew point (°C) from dry bulb (°C) and relative humidity (%)."""
    rh = max(0.5, min(100.0, float(rh_pct)))
    t = float(dry_bulb_c)
    gamma = math.log(rh / 100.0) + (_MAGNUS_B * t) / (_MAGNUS_C + t)
    return (_MAGNUS_C * gamma) / (_MAGNUS_B - gamma)


#: Saturation vapour pressure at sea level, in hPa, from the same Magnus
#: relation the dew point uses. Kept private: every caller here wants a ratio
#: of two of these, and the absolute value is only meaningful alongside a
#: station pressure nobody at these sites measures.
_ES_0 = 6.112

#: Psychrometer constant, K⁻¹. The value depends on how the wet bulb is
#: ventilated: 6.53e-4 is the WMO figure for an ASPIRATED psychrometer, which
#: is what a cooling-tower wet-bulb sensor is - it sits in the tower's own
#: airstream. A sling psychrometer read by hand is 6.62e-4, and using the
#: wrong one biases RH by roughly a point.
_PSYCHROMETER_A = 6.53e-4

#: Station pressure, hPa. ASSUMED, not measured: no site here has a barometer.
#: The sensitivity is mild - a 30 hPa swing, which is most of the range real
#: weather covers, moves RH by about a point - but it is an assumption and the
#: caller is expected to say so rather than publish the result as a reading.
_ASSUMED_STATION_HPA = 1013.25


def _saturation_hpa(t_c: float) -> float:
    return _ES_0 * math.exp((_MAGNUS_B * t_c) / (_MAGNUS_C + t_c))


def rh_from_wet_bulb(dry_bulb_c: float, wet_bulb_c: float,
                     station_hpa: float = _ASSUMED_STATION_HPA) -> float | None:
    """Relative humidity (%) from a dry/wet bulb pair.

    This is a DERIVATION, not a measurement, and callers must label it as one.
    A hygrometer measures moisture; this infers it from the evaporative
    depression between two thermometers, which is the same physics a sling
    psychrometer uses and is how a BMS graphic shows RH at a site whose only
    moisture instrument is the tower's wet bulb.

    Returns None when the pair cannot be believed. Wet bulb above dry bulb is
    thermodynamically impossible - the wet bulb is depressed BY evaporation -
    so it means a swapped sensor, a dry wick, or two points read at different
    moments, and every one of those is a fault to investigate rather than a
    number to publish.
    """
    t, tw = float(dry_bulb_c), float(wet_bulb_c)
    # Half a kelvin of tolerance: at saturation the two genuinely converge, and
    # a pair of sensors with ordinary calibration error will cross over.
    if tw > t + 0.5:
        return None
    es_t = _saturation_hpa(t)
    if es_t <= 0:
        return None
    # Actual vapour pressure, by the psychrometric equation.
    e = _saturation_hpa(min(tw, t)) - _PSYCHROMETER_A * station_hpa * (t - tw)
    return max(0.0, min(100.0, e / es_t * 100.0))


# -------------------------------------------------------------------- grading
#: What a reading can be, worst last. The page paints `warn` for allowable and
#: `critical` for outside it, the same two tones the inlet alarm rules use.
IN_BAND = "in_band"
ALLOWABLE = "allowable"
OUT = "out"


def grade_temperature(value_c: float, env: Envelope) -> str:
    if env.rec_low_c <= value_c <= env.rec_high_c:
        return IN_BAND
    if env.allow_low_c <= value_c <= env.allow_high_c:
        return ALLOWABLE
    return OUT


def grade_moisture(dp_c: float | None, rh_pct: float | None, env: Envelope) -> str | None:
    """The moisture leg, from whichever of the two the probe gives us.

    Returns None when the probe measures no moisture at all - which is most
    racks, and is an absence rather than a pass. A rack with a bare thermistor
    cannot tell you its hall is compliant.

    Both ceilings bind, so the grade is the WORSE of the dew-point verdict and
    the humidity one. A hall at 27 C / 60 % RH passes the humidity limit and
    fails the dew-point one by 3.6 K.
    """
    if dp_c is None and rh_pct is None:
        return None
    verdicts = []
    if dp_c is not None:
        if env.rec_dp_low_c <= dp_c <= env.rec_dp_high_c:
            verdicts.append(IN_BAND)
        elif env.allow_dp_low_c <= dp_c <= env.allow_dp_high_c:
            verdicts.append(ALLOWABLE)
        else:
            verdicts.append(OUT)
    if rh_pct is not None:
        # The recommended band has no RH floor - it is written as a dew point,
        # which the leg above already checked. Only the ceiling applies here.
        if rh_pct <= env.rec_rh_high_pct:
            verdicts.append(IN_BAND)
        elif env.allow_rh_low_pct <= rh_pct <= env.allow_rh_high_pct:
            verdicts.append(ALLOWABLE)
        else:
            verdicts.append(OUT)
    order = {IN_BAND: 0, ALLOWABLE: 1, OUT: 2}
    return max(verdicts, key=lambda v: order[v])


def grade_rate(k_per_h: float | None, env: Envelope) -> str | None:
    """The rate leg. None when nothing was measured recently enough to say.

    Sign is dropped: the limit is on how fast the air moves, and a 25 K/hour
    collapse after a chiller trip and a 25 K/hour recovery are the same stress
    on the same hardware. There is no "allowable" tier here - ASHRAE writes one
    number - so a breach is straight out.
    """
    if k_per_h is None:
        return None
    return IN_BAND if abs(k_per_h) <= env.max_rate_k_per_h else OUT


def grade_envelope(temp_c: float, env: Envelope, *, dp_c: float | None = None,
                   rh_pct: float | None = None,
                   rate_k_per_h: float | None = None) -> str:
    """The whole envelope, which is the worst of the legs that could be judged.

    A leg nobody measured does not fail the reading - it is missing evidence,
    not a breach, and the payload reports coverage separately so a 100 % on a
    floor with no humidity probe cannot be mistaken for a 100 % on one that
    has them.
    """
    order = {IN_BAND: 0, ALLOWABLE: 1, OUT: 2}
    worst = grade_temperature(temp_c, env)
    for leg in (grade_moisture(dp_c, rh_pct, env), grade_rate(rate_k_per_h, env)):
        if leg is not None and order[leg] > order[worst]:
            worst = leg
    return worst
