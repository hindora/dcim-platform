"""Detectors: L0 data quality (quality.py) and L2 state-gated rules (rules.py).

Pure functions over a device's recent samples. No database, no Redis: the
sweep (analytics/sweep.py) reads the windows, hands each device's view here,
and persists what comes back.
"""
