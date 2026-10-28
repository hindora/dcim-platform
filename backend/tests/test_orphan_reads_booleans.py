"""The orphan sweep must see a boolean where booleans are kept.

Booleans are change-logged into telemetry_bool. The sweep read only
telemetry_sample, so every rule alarm on a boolean - psu_failure on psu_state,
breaker_tripped on bank_breaker_closed - looked like a key nobody published and
was closed as orphaned within a minute of being raised, then re-raised on the
next poll: 20 psu_failure rows churned that way between 2026-10-27 and -28.
The live behaviour is covered in test_reconciliation_live (needs a database).
"""

from app.alarms import reconcile


def test_the_orphan_query_reads_the_boolean_table_too():
    sql = str(reconcile._ORPHANED_KEY)
    assert "FROM telemetry_bool" in sql
    assert "keyed_bool.hit IS NULL" in sql
