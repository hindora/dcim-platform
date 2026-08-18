"""Fix the migration driver and use Alembic's autocommit_block for CAGGs."""
import pathlib

# 1. Add psycopg3 as the SYNC driver for Alembic. asyncpg cannot run Alembic,
#    and bare postgresql:// silently resolves to psycopg2, which is not a dep.
p = pathlib.Path("backend/pyproject.toml")
s = p.read_text(encoding="utf-8")
assert '"asyncpg>=0.29",' in s
s = s.replace('"asyncpg>=0.29",',
              '"asyncpg>=0.29",\n    "psycopg[binary]>=3.1",')
p.write_text(s, encoding="utf-8", newline="\n")
print("pyproject: added psycopg[binary]")

# 2. Point sync_database_url at psycopg explicitly.
p = pathlib.Path("backend/app/core/config.py")
s = p.read_text(encoding="utf-8")
old = '''    @property
    def sync_database_url(self) -> str:
        """Alembic runs synchronously; strip the asyncpg driver."""
        return self.database_url.replace("+asyncpg", "")'''
new = '''    @property
    def sync_database_url(self) -> str:
        """Alembic runs synchronously, so swap asyncpg for psycopg (v3).

        Stripping the driver entirely would leave a bare ``postgresql://`` URL,
        which SQLAlchemy resolves to psycopg2 - a package this project does not
        depend on, producing a ModuleNotFoundError only when migrations run.
        """
        return self.database_url.replace("+asyncpg", "+psycopg")'''
assert old in s
p.write_text(s.replace(old, new, 1), encoding="utf-8", newline="\n")
print("config: sync_database_url -> +psycopg")

# 3. Replace the raw COMMIT with Alembic's supported autocommit_block.
p = pathlib.Path("backend/alembic/versions/0002_timescale.py")
s = p.read_text(encoding="utf-8")

s = s.replace('''Notes
-----
Continuous aggregates cannot be created inside a transaction block, so this
migration commits the surrounding transaction first and then issues DDL in
autocommit. That is why the statements below are deliberately idempotent
(IF NOT EXISTS / catalog guards): a failure part-way cannot be rolled back.''',
'''Notes
-----
Continuous aggregates cannot be created inside a transaction block, so that DDL
runs inside ``autocommit_block()`` - Alembic's supported escape hatch, rather
than issuing a raw COMMIT and hoping the driver cooperates. Statements in that
block are deliberately idempotent (IF NOT EXISTS / if_not_exists => TRUE)
because a failure part-way through cannot be rolled back.''')

s = s.replace('''    conn = op.get_bind()
    conn.execute(sa.text("COMMIT"))

    conn.execute(sa.text("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_1m''',
'''    with op.get_context().autocommit_block():
        op.execute("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_1m''')

s = s.replace('''        WITH NO DATA
    """))

    conn.execute(sa.text("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_5m''',
'''        WITH NO DATA
        """)
        op.execute("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_5m''')

s = s.replace('''        WITH NO DATA
    """))

    conn.execute(sa.text("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_1h''',
'''        WITH NO DATA
        """)
        op.execute("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_1h''')

s = s.replace('''        WITH NO DATA
    """))

    for view, start, end, sched in (
        ("telemetry_1m", "3 hours", "1 minute", "1 minute"),
        ("telemetry_5m", "1 day", "5 minutes", "5 minutes"),
        ("telemetry_1h", "7 days", "1 hour", "30 minutes"),
    ):
        conn.execute(sa.text(f"""
            SELECT add_continuous_aggregate_policy('{view}',
                start_offset      => INTERVAL '{start}',
                end_offset        => INTERVAL '{end}',
                schedule_interval => INTERVAL '{sched}',
                if_not_exists     => TRUE)
        """))

    # 1h is deliberately kept forever: it is what capacity trending needs and
    # it is tiny.
    conn.execute(sa.text("SELECT add_retention_policy('telemetry_1m', INTERVAL '1 year')"))
    conn.execute(sa.text("SELECT add_retention_policy('telemetry_5m', INTERVAL '2 years')"))''',
'''        WITH NO DATA
        """)

        for view, start, end, sched in (
            ("telemetry_1m", "3 hours", "1 minute", "1 minute"),
            ("telemetry_5m", "1 day", "5 minutes", "5 minutes"),
            ("telemetry_1h", "7 days", "1 hour", "30 minutes"),
        ):
            op.execute(f"""
                SELECT add_continuous_aggregate_policy('{view}',
                    start_offset      => INTERVAL '{start}',
                    end_offset        => INTERVAL '{end}',
                    schedule_interval => INTERVAL '{sched}',
                    if_not_exists     => TRUE)
            """)

        # 1h is deliberately kept forever: it is what capacity trending needs
        # and it is tiny.
        op.execute("SELECT add_retention_policy('telemetry_1m', INTERVAL '1 year')")
        op.execute("SELECT add_retention_policy('telemetry_5m', INTERVAL '2 years')")''')

s = s.replace('''def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("COMMIT"))
    for view in ("telemetry_1h", "telemetry_5m", "telemetry_1m"):
        conn.execute(sa.text(f"DROP MATERIALIZED VIEW IF EXISTS {view}"))
    for table in ("poll_result", "telemetry_text", "telemetry_bool", "telemetry_sample"):
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))''',
'''def downgrade() -> None:
    # Drop order matters: 1h is built from 5m, which is built from 1m.
    with op.get_context().autocommit_block():
        for view in ("telemetry_1h", "telemetry_5m", "telemetry_1m"):
            op.execute(f"DROP MATERIALIZED VIEW IF EXISTS {view}")
    for table in ("poll_result", "telemetry_text", "telemetry_bool", "telemetry_sample"):
        op.execute(f"DROP TABLE IF EXISTS {table}")''')

assert "COMMIT" not in s, "a raw COMMIT survived the patch"
p.write_text(s, encoding="utf-8", newline="\n")
print("0002: raw COMMIT -> autocommit_block")
