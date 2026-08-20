"""Which end of a connection is upstream, per layer.

One definition, because two copies of this drift and the failure is silent.
Correlation walks it towards a cause; impact analysis walks it away from one.
Both get the wrong answer - not an error - if the direction is wrong.

    management: device  -> OOB switch     (a=device,  b=switch)   REVERSED
    power:      feeder  -> load           (a=feeder,  b=load)
    cooling:    plant   -> terminal unit  (a=plant,   b=unit)
    fieldbus:   gateway -> field device   (a=gateway, b=device)
    production: switch  -> host           (a=switch,  b=host)

Management is the odd one out because the managed device holds its own cable
end, while a gateway, a PDU or a switch owns the trunk.
"""

from __future__ import annotations

# layer -> (upstream column, downstream column)
UPSTREAM_COL: dict[str, tuple[str, str]] = {
    "management": ("b_device_id", "a_device_id"),
    "fieldbus": ("a_device_id", "b_device_id"),
    "power": ("a_device_id", "b_device_id"),
    "cooling": ("a_device_id", "b_device_id"),
    "production": ("a_device_id", "b_device_id"),
}


def upstream_column(layer: str) -> str:
    return UPSTREAM_COL[layer][0]


def downstream_column(layer: str) -> str:
    return UPSTREAM_COL[layer][1]
