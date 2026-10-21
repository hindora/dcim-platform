"""Anomaly detection (docs/28-33).

The sweep, the catalogue, the statistics it scores with and the evaluation
harness. Nothing here is imported by the request path except the read-only
health and catalogue views; the sweep runs in the ingest worker.
"""
