"""An anomaly is a finding, not an alarm.

A detector that notices a CRAH drifting, a chiller short-cycling or a sensor
gone flat has found something an operator should look at. That is not the
same thing as an alarm: ACTIVE -> CLEARED is the wrong lifecycle for a week-
long drift a CUSUM has been integrating, advisories must not count toward the
alarm-rate KPI, and they need dismissal feedback alarms do not have. And it is
never a metric: a score is not a measurement (docs/29 section 1).

So the anomaly layer gets its own seven tables, all empty here (docs/29
section 4). Seeds are 0112, which is Phase 1; this migration is Phase 0.

  anomaly_detector            what each detector is, its version and defaults
  anomaly_override            one device's exception to one detector
  anomaly_baseline_exclusion  spans a nightly fit must not learn as normal
  anomaly_baseline            nightly fitted models
  anomaly_state               checkpointed accumulators (Redis is the hot copy)
  anomaly_incident            L5 groups with a ranked root
  anomaly_finding             the advisory stream and the record behind every
                              derived alert

They are raw-SQL tables with no ORM class, like alarm and alarm_rule_override,
and none is a hypertable: the finding dedupe key is a partial unique index
without ts, rows are updated rather than appended, and other tables point at
them (docs/29 section 3). Retention is a nightly batched prune, not a policy.

Every CHECK is written literally as ck_<table>_<what>, which is what the
metadata naming convention would expand <what> to, so a later migration can
drop it either by the full name in raw SQL or with
op.drop_constraint("<what>", "<table>", type_="check").

Downgrade drops the tables (their indexes and CHECKs go with them). Derived
alarms already raised stay in `alarm` with source = 'analytics'; they are
ordinary alarms and outlive this. Redis keys under dcim:anomaly:* are left to
expire.

Revision ID: 0111
Revises: 0110
"""

from __future__ import annotations

from alembic import op

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An FK takes SHARE ROW EXCLUSIVE on the referenced table, and `alarm` is
    # written by ingest continuously. Fail fast and retry rather than queue
    # behind ingest and stall it while waiting (docs/29 section 13).
    op.execute("SET LOCAL lock_timeout = '5s'")

    # --- anomaly_detector (docs/29 section 4.1) ------------------------------
    op.execute(r"""
        CREATE TABLE anomaly_detector (
            id               text        PRIMARY KEY,
            version          integer     NOT NULL DEFAULT 1,
            layer            smallint    NOT NULL,
            phase            smallint    NOT NULL,
            name             text        NOT NULL,
            diagnosis        text        NOT NULL,
            device_types     text[]      NOT NULL,
            -- The metric whose series keys the finding. '*' = every series the
            -- detector reads (L0 data quality; RM-2 over several keys).
            primary_metric   text        NOT NULL,
            required_metrics text[]      NOT NULL DEFAULT '{}',
            default_lane     text        NOT NULL DEFAULT 'advisory',
            default_kind     text        NOT NULL DEFAULT 'undetermined',
            alarm_type       text,
            severity         severity_t,
            cadence_s        integer     NOT NULL DEFAULT 60,
            params           jsonb       NOT NULL DEFAULT '{}'::jsonb,
            enabled          boolean     NOT NULL DEFAULT true,
            notes            text,
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now(),
            updated_by       text,
            CONSTRAINT ck_anomaly_detector_id
                CHECK (id ~ '^[A-Z]{2}-[0-9]{1,2}$' OR id = 'L5'),
            CONSTRAINT ck_anomaly_detector_layer
                CHECK (layer IN (0, 2, 3, 4, 5)),
            -- Layer 5 only as the 'L5' row: it is a switch, not a scorer.
            CONSTRAINT ck_anomaly_detector_l5
                CHECK ((layer = 5) = (id = 'L5')),
            CONSTRAINT ck_anomaly_detector_phase
                CHECK (phase BETWEEN 1 AND 5),
            CONSTRAINT ck_anomaly_detector_device_types
                CHECK (cardinality(device_types) > 0 OR layer = 5),
            CONSTRAINT ck_anomaly_detector_primary_metric
                CHECK (primary_metric ~ '^([a-z][a-z0-9_]*|\*)$'),
            CONSTRAINT ck_anomaly_detector_lane
                CHECK (default_lane IN ('advisory', 'alert')),
            CONSTRAINT ck_anomaly_detector_kind
                CHECK (default_kind IN ('sensor', 'process', 'undetermined')),
            -- Derived alerts are MINOR or WARNING. MAJOR is reachable only
            -- through L5 promotion at runtime, never as a default.
            CONSTRAINT ck_anomaly_detector_severity
                CHECK (severity IS NULL OR severity IN ('WARNING', 'MINOR')),
            CONSTRAINT ck_anomaly_detector_alert
                CHECK (default_lane = 'advisory'
                       OR (alarm_type IS NOT NULL AND severity IS NOT NULL)),
            CONSTRAINT ck_anomaly_detector_alarm_type
                CHECK (alarm_type IS NULL OR alarm_type ~ '^[a-z][a-z0-9_]*$'),
            -- A multiple of the 60 s sweep: L2 at 60, L3/L4 at 300.
            CONSTRAINT ck_anomaly_detector_cadence
                CHECK (cadence_s >= 60 AND cadence_s % 60 = 0),
            CONSTRAINT ck_anomaly_detector_params
                CHECK (jsonb_typeof(params) = 'object')
        )
    """)
    # Two detectors may never share an alarm_type: the alarm key is
    # (device, alarm_type, instance), so a shared type would merge two
    # diagnoses into one row and let one detector clear the other's alert.
    op.execute("""
        CREATE UNIQUE INDEX ux_anomaly_detector_alarm_type
            ON anomaly_detector (alarm_type) WHERE alarm_type IS NOT NULL
    """)

    # --- anomaly_override (docs/29 section 4.2) ------------------------------
    op.execute("""
        CREATE TABLE anomaly_override (
            detector_id text        NOT NULL REFERENCES anomaly_detector(id) ON DELETE CASCADE,
            device_id   uuid        NOT NULL REFERENCES device(id)           ON DELETE CASCADE,
            params      jsonb,
            enabled     boolean,
            -- Time-limited shelving; null = until removed.
            expires_at  timestamptz,
            reason      text,
            created_by  text        NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (detector_id, device_id),
            CONSTRAINT ck_anomaly_override_params
                CHECK (params IS NULL OR jsonb_typeof(params) = 'object'),
            CONSTRAINT ck_anomaly_override_nonempty
                CHECK (params IS NOT NULL OR enabled IS NOT NULL),
            -- A mute must say why: a muted detector is invisible.
            CONSTRAINT ck_anomaly_override_mute_reason
                CHECK (enabled IS DISTINCT FROM false OR reason IS NOT NULL)
        )
    """)
    op.execute("CREATE INDEX ix_anomaly_override_device ON anomaly_override (device_id)")

    # --- anomaly_baseline_exclusion (docs/29 section 4.3) --------------------
    op.execute("""
        CREATE TABLE anomaly_baseline_exclusion (
            id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            device_id     uuid        REFERENCES device(id)     ON DELETE CASCADE,
            room_id       uuid        REFERENCES room(id)       ON DELETE CASCADE,
            datacenter_id uuid        REFERENCES datacenter(id) ON DELETE CASCADE,
            detector_id   text        REFERENCES anomaly_detector(id) ON DELETE CASCADE,
            metric_id     smallint    REFERENCES metric(id),
            starts_at     timestamptz NOT NULL,
            ends_at       timestamptz,
            source        text        NOT NULL,
            source_ref    text,
            reason        text        NOT NULL,
            created_by    text        NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_anomaly_baseline_exclusion_scope
                CHECK (num_nonnulls(device_id, room_id, datacenter_id) = 1),
            CONSTRAINT ck_anomaly_baseline_exclusion_dates
                CHECK (ends_at IS NULL OR ends_at > starts_at),
            CONSTRAINT ck_anomaly_baseline_exclusion_source
                CHECK (source IN ('maintenance', 'injected_fault',
                                  'confirmed_finding', 'manual'))
        )
    """)
    # Idempotent sync of maintenance windows and ledger entries. NULLS NOT
    # DISTINCT because two of the three scope columns are always null.
    op.execute("""
        CREATE UNIQUE INDEX ux_anomaly_baseline_exclusion_source
            ON anomaly_baseline_exclusion
               (source, source_ref, device_id, room_id, datacenter_id)
            NULLS NOT DISTINCT
            WHERE source_ref IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_baseline_exclusion_device_span
            ON anomaly_baseline_exclusion
            USING gist (device_id,
                        tstzrange(starts_at, COALESCE(ends_at, 'infinity'::timestamptz), '[)'))
            WHERE device_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_baseline_exclusion_room_span
            ON anomaly_baseline_exclusion
            USING gist (room_id,
                        tstzrange(starts_at, COALESCE(ends_at, 'infinity'::timestamptz), '[)'))
            WHERE room_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_baseline_exclusion_starts
            ON anomaly_baseline_exclusion (starts_at DESC)
    """)

    # --- anomaly_baseline (docs/29 section 4.4) ------------------------------
    op.execute("""
        CREATE TABLE anomaly_baseline (
            id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            detector_id      text        NOT NULL REFERENCES anomaly_detector(id) ON DELETE CASCADE,
            detector_version integer     NOT NULL,
            -- Device-scoped, except the room-scoped influence fit (Phase 4).
            device_id        uuid        REFERENCES device(id) ON DELETE CASCADE,
            room_id          uuid        REFERENCES room(id)   ON DELETE CASCADE,
            metric_id        smallint    NOT NULL REFERENCES metric(id),
            instance         text        NOT NULL DEFAULT '',
            model_kind       text        NOT NULL,
            model            jsonb       NOT NULL,
            source_rollup    text        NOT NULL,
            fit_from         timestamptz NOT NULL,
            fit_to           timestamptz NOT NULL,
            n                integer     NOT NULL,
            excluded_s       integer     NOT NULL DEFAULT 0,
            exclusion_ids    uuid[]      NOT NULL DEFAULT '{}',
            r2               double precision,
            resid_sigma      double precision,
            status           text        NOT NULL,
            is_current       boolean     NOT NULL DEFAULT true,
            fitted_at        timestamptz NOT NULL DEFAULT now(),
            superseded_at    timestamptz,
            CONSTRAINT ck_anomaly_baseline_scope
                CHECK (num_nonnulls(device_id, room_id) = 1),
            CONSTRAINT ck_anomaly_baseline_model_kind
                CHECK (model_kind IN ('ols', 'profile_how', 'curve', 'first_order',
                                      'peer', 'influence')),
            CONSTRAINT ck_anomaly_baseline_source_rollup
                CHECK (source_rollup IN ('telemetry_5m', 'telemetry_1h', 'telemetry_1d')),
            CONSTRAINT ck_anomaly_baseline_status
                CHECK (status IN ('ok', 'thin', 'rejected')),
            CONSTRAINT ck_anomaly_baseline_span
                CHECK (fit_to > fit_from),
            CONSTRAINT ck_anomaly_baseline_model
                CHECK (jsonb_typeof(model) = 'object'),
            -- A rejected fit never becomes current.
            CONSTRAINT ck_anomaly_baseline_current
                CHECK (NOT is_current OR (status <> 'rejected' AND superseded_at IS NULL))
        )
    """)
    # One current model per series; a double fit from two workers fails loudly.
    op.execute("""
        CREATE UNIQUE INDEX ux_anomaly_baseline_current
            ON anomaly_baseline (detector_id, device_id, room_id, metric_id, instance)
            NULLS NOT DISTINCT
            WHERE is_current
    """)
    # GET /expected: the band at t comes from the fit current at t, so
    # superseded rows are needed too. Not partial for that reason.
    op.execute("""
        CREATE INDEX ix_anomaly_baseline_series
            ON anomaly_baseline (device_id, metric_id, instance, fitted_at DESC)
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_baseline_prune
            ON anomaly_baseline (superseded_at) WHERE NOT is_current
    """)

    # --- anomaly_state (docs/29 section 4.5) ---------------------------------
    # fillfactor 80 and nothing indexed that a checkpoint rewrites, so the
    # upserts stay HOT.
    op.execute("""
        CREATE TABLE anomaly_state (
            detector_id      text        NOT NULL REFERENCES anomaly_detector(id) ON DELETE CASCADE,
            device_id        uuid        NOT NULL REFERENCES device(id)           ON DELETE CASCADE,
            metric_id        smallint    NOT NULL REFERENCES metric(id),
            instance         text        NOT NULL DEFAULT '',
            detector_version integer     NOT NULL,
            baseline_id      uuid        REFERENCES anomaly_baseline(id) ON DELETE SET NULL,
            state            jsonb       NOT NULL,
            last_sample_at   timestamptz,
            updated_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (detector_id, device_id, metric_id, instance),
            CONSTRAINT ck_anomaly_state_state CHECK (jsonb_typeof(state) = 'object')
        ) WITH (fillfactor = 80)
    """)
    op.execute("CREATE INDEX ix_anomaly_state_device ON anomaly_state (device_id)")

    # --- anomaly_incident (docs/29 section 4.6) ------------------------------
    op.execute("""
        CREATE TABLE anomaly_incident (
            id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            state                 text        NOT NULL DEFAULT 'open',
            -- 'device:<uuid>' when L5 found a root device, 'room:<uuid>' for
            -- the room-level fallback used until the influence fit exists.
            root_key              text        NOT NULL,
            tier                  text        NOT NULL,
            root_device_id        uuid        REFERENCES device(id) ON DELETE SET NULL,
            root_finding_id       uuid,       -- FK added below (circular)
            root_alarm_id         uuid        REFERENCES alarm(id)  ON DELETE SET NULL,
            datacenter_id         uuid        REFERENCES datacenter(id) ON DELETE SET NULL,
            room_id               uuid        REFERENCES room(id)   ON DELETE SET NULL,
            title                 text        NOT NULL,
            summary               text,
            member_alarm_ids      uuid[]      NOT NULL DEFAULT '{}',
            member_count          integer     NOT NULL DEFAULT 0,
            impact_kw             numeric(10,2),
            evidence              jsonb       NOT NULL DEFAULT '{}'::jsonb,
            explanation           jsonb,
            explained_at          timestamptz,
            -- Operator thumbs on the L6 text (POST /incidents/{id}/explain/feedback).
            explanation_feedback  jsonb       NOT NULL DEFAULT '[]'::jsonb,
            opened_at             timestamptz NOT NULL DEFAULT now(),
            last_activity_at      timestamptz NOT NULL DEFAULT now(),
            resolved_at           timestamptz,
            CONSTRAINT ck_anomaly_incident_state
                CHECK (state IN ('open', 'resolved')),
            CONSTRAINT ck_anomaly_incident_resolved
                CHECK ((state = 'resolved') = (resolved_at IS NOT NULL)),
            CONSTRAINT ck_anomaly_incident_tier
                CHECK (tier IN ('power', 'plant', 'loop', 'unit', 'zone', 'server', 'room')),
            CONSTRAINT ck_anomaly_incident_root_key
                CHECK (root_key ~ '^(device|room):[0-9a-f-]{36}$'),
            CONSTRAINT ck_anomaly_incident_evidence
                CHECK (jsonb_typeof(evidence) = 'object'),
            CONSTRAINT ck_anomaly_incident_explanation
                CHECK (explanation IS NULL OR jsonb_typeof(explanation) = 'object'),
            CONSTRAINT ck_anomaly_incident_explanation_feedback
                CHECK (jsonb_typeof(explanation_feedback) = 'array')
        )
    """)
    # One open incident per root.
    op.execute("""
        CREATE UNIQUE INDEX ux_anomaly_incident_open_root
            ON anomaly_incident (root_key) WHERE state = 'open'
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_incident_open
            ON anomaly_incident (impact_kw DESC NULLS LAST, opened_at DESC)
            WHERE state = 'open'
    """)
    op.execute("CREATE INDEX ix_anomaly_incident_opened ON anomaly_incident (opened_at DESC)")
    op.execute("""
        CREATE INDEX ix_anomaly_incident_member_alarms
            ON anomaly_incident USING gin (member_alarm_ids)
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_incident_root_alarm
            ON anomaly_incident (root_alarm_id) WHERE root_alarm_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_incident_root_finding
            ON anomaly_incident (root_finding_id) WHERE root_finding_id IS NOT NULL
    """)

    # --- anomaly_finding (docs/29 section 4.7) -------------------------------
    op.execute("""
        CREATE TABLE anomaly_finding (
            id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
            detector_id      text        NOT NULL REFERENCES anomaly_detector(id) ON DELETE RESTRICT,
            detector_version integer     NOT NULL,
            layer            smallint    NOT NULL,
            lane             text        NOT NULL,
            kind             text        NOT NULL DEFAULT 'undetermined',
            state            text        NOT NULL DEFAULT 'open',
            -- The operator's latest verdict, kept through resolution.
            verdict          text,
            device_id        uuid        NOT NULL REFERENCES device(id) ON DELETE CASCADE,
            metric_id        smallint    NOT NULL REFERENCES metric(id),
            instance         text        NOT NULL DEFAULT '',
            -- Where the device was when the finding opened. Stamped once.
            datacenter_id    uuid        REFERENCES datacenter(id) ON DELETE SET NULL,
            room_id          uuid        REFERENCES room(id)       ON DELETE SET NULL,
            first_seen       timestamptz NOT NULL,
            raised_at        timestamptz NOT NULL,
            last_seen        timestamptz NOT NULL,
            occurrences      integer     NOT NULL DEFAULT 1,
            reopen_count     integer     NOT NULL DEFAULT 0,
            score            double precision,
            score_peak       double precision,
            expected         double precision,
            observed         double precision,
            impact_kw        numeric(10,2),
            summary          text        NOT NULL,
            evidence         jsonb       NOT NULL DEFAULT '{}'::jsonb,
            baseline_id      uuid        REFERENCES anomaly_baseline(id) ON DELETE SET NULL,
            incident_id      uuid        REFERENCES anomaly_incident(id) ON DELETE SET NULL,
            alarm_id         uuid        REFERENCES alarm(id)            ON DELETE SET NULL,
            promoted_at      timestamptz,
            feedback         jsonb       NOT NULL DEFAULT '[]'::jsonb,
            acknowledged_at  timestamptz,
            acknowledged_by  text,
            resolved_at      timestamptz,
            resolved_by      text,
            resolution       text,
            updated_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_anomaly_finding_layer
                CHECK (layer IN (0, 2, 3, 4)),
            CONSTRAINT ck_anomaly_finding_lane
                CHECK (lane IN ('advisory', 'alert')),
            CONSTRAINT ck_anomaly_finding_kind
                CHECK (kind IN ('sensor', 'process', 'undetermined')),
            CONSTRAINT ck_anomaly_finding_state
                CHECK (state IN ('open', 'acknowledged', 'dismissed', 'confirmed', 'resolved')),
            CONSTRAINT ck_anomaly_finding_verdict
                CHECK (verdict IS NULL OR verdict IN ('confirmed', 'dismissed')),
            -- IS NOT DISTINCT FROM, not `=`: with verdict NULL, `verdict =
            -- state` is NULL and a NULL CHECK passes, which would admit the
            -- exact row docs/29 section 14 says this must reject.
            CONSTRAINT ck_anomaly_finding_verdict_state
                CHECK (state NOT IN ('confirmed', 'dismissed')
                       OR verdict IS NOT DISTINCT FROM state),
            CONSTRAINT ck_anomaly_finding_resolution
                CHECK (resolution IS NULL OR resolution IN
                       ('cleared', 'device_retired', 'detector_disabled', 'muted',
                        'superseded', 'manual')),
            CONSTRAINT ck_anomaly_finding_resolved
                CHECK ((state = 'resolved') = (resolved_at IS NOT NULL)
                       AND (state = 'resolved') = (resolution IS NOT NULL)),
            CONSTRAINT ck_anomaly_finding_times
                CHECK (raised_at >= first_seen AND last_seen >= first_seen),
            CONSTRAINT ck_anomaly_finding_counts
                CHECK (occurrences >= 1 AND reopen_count >= 0),
            CONSTRAINT ck_anomaly_finding_evidence
                CHECK (jsonb_typeof(evidence) = 'object'),
            CONSTRAINT ck_anomaly_finding_feedback
                CHECK (jsonb_typeof(feedback) = 'array')
        ) WITH (fillfactor = 80)
    """)
    # THE dedupe key and the sweep upsert's ON CONFLICT target. A dismissed
    # finding keeps the key occupied until the condition clears.
    op.execute("""
        CREATE UNIQUE INDEX ux_anomaly_finding_open_key
            ON anomaly_finding (detector_id, device_id, metric_id, instance)
            WHERE state <> 'resolved'
    """)
    # Reopen window lookup.
    op.execute("""
        CREATE INDEX ix_anomaly_finding_recent_resolved
            ON anomaly_finding (detector_id, device_id, metric_id, instance, resolved_at DESC)
            WHERE state = 'resolved'
    """)
    # GET /findings default view. Deliberately not on last_seen, which moves
    # every sweep: indexing it would make every touch a non-HOT update.
    op.execute("""
        CREATE INDEX ix_anomaly_finding_active
            ON anomaly_finding (lane, impact_kw DESC NULLS LAST, first_seen DESC)
            WHERE state <> 'resolved'
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_finding_device
            ON anomaly_finding (device_id, first_seen DESC)
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_finding_detector
            ON anomaly_finding (detector_id, first_seen DESC)
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_finding_room
            ON anomaly_finding (room_id, first_seen DESC) WHERE room_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_finding_first_seen
            ON anomaly_finding (first_seen DESC)
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_finding_incident
            ON anomaly_finding (incident_id) WHERE incident_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_finding_alarm
            ON anomaly_finding (alarm_id) WHERE alarm_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_finding_baseline
            ON anomaly_finding (baseline_id) WHERE baseline_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX ix_anomaly_finding_prune
            ON anomaly_finding (resolved_at) WHERE state = 'resolved'
    """)

    # --- the circular FK (docs/29 section 4.8) -------------------------------
    op.execute("""
        ALTER TABLE anomaly_incident
            ADD CONSTRAINT fk_anomaly_incident_root_finding_id_anomaly_finding
            FOREIGN KEY (root_finding_id) REFERENCES anomaly_finding(id) ON DELETE SET NULL
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE anomaly_incident
            DROP CONSTRAINT IF EXISTS fk_anomaly_incident_root_finding_id_anomaly_finding
    """)
    # Indexes and CHECKs go with their tables; no Timescale jobs exist.
    for table in ("anomaly_finding", "anomaly_incident", "anomaly_state",
                  "anomaly_baseline", "anomaly_baseline_exclusion",
                  "anomaly_override", "anomaly_detector"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
