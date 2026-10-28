"""The alarm names the supply the faceplate names.

Live, 2026-10-27: the Power page's Feeds cell said PSU1 for a failed supply and
the alarm for the same supply said "Power supply 0 reported failed". Redfish
MemberIds on this fleet are 0-based; the silk-screen and the technician are not.
"""

from __future__ import annotations

from app.alarms.engine import AlarmKey, Rule, _render_state
from app.core.psu import psu_label

TPL = ("Power supply {point} reported failed by the BMC; the chassis is "
       "running on its other supply")


def _rule(metric: str = "psu_state") -> Rule:
    return Rule(id="psu-failure", name="psu", alarm_type="psu_failure",
                severity="MAJOR", message_tpl=TPL, metric_key=metric)


def test_member_id_zero_is_psu1_in_the_alarm():
    msg = _render_state(_rule(), AlarmKey("d", "psu_failure", "0"))
    assert msg.startswith("Power supply PSU1 reported failed")


def test_a_vendor_name_is_kept():
    assert psu_label("PSU.Slot.1") == "PSU.Slot.1"
    assert psu_label("1") == "PSU2"
    assert psu_label("") == "PSU?"


def test_other_state_points_are_untouched():
    rule = Rule(id="x", name="x", alarm_type="leak", severity="MAJOR",
                message_tpl="{point} asserted", metric_key="alarm_state")
    assert _render_state(rule, AlarmKey("d", "leak", "Alarm_Leak")) == "Alarm Leak asserted"
