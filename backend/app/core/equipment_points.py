"""Severity for the plant's own fault points.

The points arrive as one metric (`alarm_state`) with the point name as the
instance, so severity cannot come from the metric - it has to be assigned per
point. Rationalising by consequence is the whole reason a severity axis exists:
a leak and a dirty filter are both true, and only one of them is a phone call.

Lives here rather than in the migration that seeds it so the rules and the
tests read the same list. A point in neither list streams in and raises
nothing, which is the condition this phase exists to end - `test_equipment_
alarms` fails if the lists and the plane disagree in either direction.
"""

from __future__ import annotations

#: The fault threatens load or containment now.
MAJOR_POINTS = [
    # cooling - loss of cooling, or of containment
    "Alarm_Leak", "Alarm_LowFlow", "Alarm_FlowLoss", "Alarm_PumpFault",
    "Alarm_Fault", "Alarm_ActuatorFault", "Alarm_HighPressure",
    "Alarm_HighTemp", "Alarm_HighSupplyTemp", "Alarm_AirflowLoss",
    # power - integrity of the supply
    "Alarm_PhaseLoss", "Alarm_Overcurrent", "Alarm_Undervoltage",
    "Alarm_UnderFrequency", "Battery_Fault", "Charger_Fault",
    "Rectifier_Fault", "Phase_Fault", "Low_Battery", "Fail_To_Transfer",
]

#: Wear, hygiene, or degraded-but-carrying. A work order, not a call-out.
WARNING_POINTS = [
    "Filter_Dirty", "Alarm_HighVibration", "Alarm_LowBasin",
    "Alarm_SensorFault", "Alarm_HighTHD", "Alarm_VoltageImbalance",
    "Alarm_LowEvapTemp", "Alarm_CondPressLimit", "Alarm_HighCHWSupply",
    "Alarm_HighReturnAir", "Not_In_Auto", "Alarm_Low_Fuel",
    "Alarm_Low_Coolant", "Alarm_High_Temp", "Alarm_Transfer", "Fan_Fault",
]
