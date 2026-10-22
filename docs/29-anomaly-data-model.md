# 29 — Anomaly detection: data model

Status: **specification. Not built.** Migrations `0111`–`0115` are reserved for
it. The head today is `0110_an_endpoint_can_have_a_write_credential.py`.

This document specifies the schema, indexes, retention, Redis keys and settings
for the anomaly detector designed in `28-anomaly-detection-design.md`. The API
over it is `30-anomaly-api-spec.md`, the UI is `31`, the simulator ground truth
is `32`, and the phased build, tests and detector tuning are `33`. Names here
follow the shared build contract exactly. Where the code disagrees with the
contract or with doc 28, a **Contract deviation** or **Design deviation** note
says so. Nothing is silently renamed.

Code references use `P:` for `dcim-platform/`. Every claim about existing code
carries a `file:line`.

If you read one thing, read §1. It is the rule the rest of the document obeys.

## 1. The rule: a finding is not an alarm, and it is never a metric

Three stores already exist and none of them is the right home for a finding:

| Store | Why a finding does not go there |
|---|---|
| `alarm` (`P:backend/alembic/versions/0005_alarms_and_events.py:65-113`) | ACTIVE → CLEARED is the wrong lifecycle for a finding. A CUSUM that resets would "clear" a week-long drift. Advisories must not count toward the alarm-rate KPI, and they need dismissal feedback that alarms lack (doc 28 §"Advisories live in their own queue"). |
| `telemetry_sample` + rollups (`0002_timescale.py:27-146`) | A score is not a measurement. Adding a registry key is a six-step chain ending in a Go collector rebuild (doc 28; memory rule "adding a metric key"), and the expected band can be computed from `anomaly_baseline` on read. |
| `device_state.metrics` | Hot keys only, overwritten in place. It has no history. |

So:

| Need | Where it goes |
|---|---|
| What each detector is, its version and its default parameters | `anomaly_detector` |
| One device's exception to one detector (sensitivity, mute) | `anomaly_override` |
| Accumulators that must survive a restart (EWMA, CUSUM±, counters) | `anomaly_state` (checkpoint); Redis holds the hot copy |
| Nightly fitted models | `anomaly_baseline` |
| Spans that must not be learned as normal | `anomaly_baseline_exclusion` |
| The advisory stream and the record behind every derived alert | `anomaly_finding` |
| L5 groups with a ranked root | `anomaly_incident` |
| A derived **alert** | the existing `alarm` row, raised with `raise_alarm(source="analytics")` and linked from `anomaly_finding.alarm_id` |

Every one of the seven tables is keyed on `device.id` (uuid) or reaches it
through a parent, as doc 20 §1 requires of everything new.

All seven are **raw-SQL tables created in alembic and absent from
`backend/app/models/`**. That is the project's convention for alarm-adjacent
and hypertable-adjacent tables: `alarm`, `alarm_rule`, `alarm_rule_override`,
`maintenance_window`, `integration_outbox` and `audit_log` have no ORM class
either (`P:backend/app/models/` holds only `endpoints.py`, `inventory.py` and
`state.py`). They are read through `repositories/anomalies.py`.

## 2. Entity overview

```
                      anomaly_detector (id 'CR-1', version, params)
                     /        |          |            \
                    /         |          |             \
     anomaly_override   anomaly_state   anomaly_baseline   anomaly_baseline_exclusion
     (detector,device)  (detector,      (detector, device,   (device | room | datacenter,
                         device,metric,  metric, instance,    detector?, metric?, span)
                         instance)       fitted model)              |
                              \              |                      | read by the nightly fit
                               \             |                      |
                                \            v                      |
                                 +---> anomaly_finding <------------+ (a confirmed finding
                                       (detector, device, metric,      writes an exclusion)
                                        instance, state, lane, kind)
                                        |            |          \
                          incident_id   |            | alarm_id  \ baseline_id
                                        v            v
                              anomaly_incident     alarm (existing; source='analytics',
                              (root finding |       detection='derived')
                               root alarm,
                               member_alarm_ids[])
```

| From | To | Cardinality | ON DELETE | Why |
|---|---|---|---|---|
| `anomaly_override.detector_id` | `anomaly_detector.id` | n:1 | CASCADE | An override of a detector that is gone means nothing |
| `anomaly_override.device_id` | `device.id` | n:1 | CASCADE | Mirrors `alarm_rule_override` (`0108:43-46`) |
| `anomaly_state.*` | detector, device, metric, baseline | n:1 | CASCADE / CASCADE / NO ACTION / SET NULL | State is derived and can be rebuilt |
| `anomaly_baseline.*` | detector, device or room | n:1 | CASCADE | A model of a device that is gone is garbage |
| `anomaly_baseline_exclusion.*` | device, room, datacenter, detector | n:1 | CASCADE | Scope gone, exclusion gone |
| `anomaly_finding.detector_id` | `anomaly_detector.id` | n:1 | **RESTRICT** | Findings are the precision record. Removing a detector must be a deliberate migration that deletes its findings first (§9) |
| `anomaly_finding.device_id` | `device.id` | n:1 | CASCADE | Same as `alarm.device_id` (`0005:68-69`) |
| `anomaly_finding.alarm_id` | `alarm.id` | n:1 | SET NULL | The finding outlives an alarm deleted with its device |
| `anomaly_finding.incident_id` | `anomaly_incident.id` | n:1 | SET NULL | Pruning an incident must not delete its evidence |
| `anomaly_finding.baseline_id` | `anomaly_baseline.id` | n:1 | SET NULL | Superseded fits are pruned after 30 days. The finding's `evidence.baseline` keeps a snapshot of the coefficients that scored it |
| `anomaly_incident.root_finding_id` | `anomaly_finding.id` | n:1 | SET NULL | Circular with `finding.incident_id`, so it is added after both tables exist (§4.8) |
| `anomaly_incident.root_alarm_id` | `alarm.id` | n:1 | SET NULL | |
| `anomaly_incident.member_alarm_ids[]` | `alarm.id` | n:m | none (array) | An array element cannot carry an FK. Readers join and drop dangling ids |

FK column types come from the parents. `device.id` is uuid
(`0001_baseline.py:148-150`). `metric.id` is **smallint**
(`0001_baseline.py:407`), as is `telemetry_sample.metric_id` (`0002:72`).
`alarm.id` is uuid (`0005:67-68`), and so are `room.id` and `datacenter.id`
(`0001:48-50,66-68`). `instance` is `text NOT NULL DEFAULT ''`, the telemetry
convention (`0002:73`), so a series key compares equal to its telemetry key.

## 3. Hypertables: none, and why

None of the seven tables is a hypertable, and none gets a Timescale policy.

| Reason | Detail |
|---|---|
| The dedupe key is a partial unique index without `ts` | "One non-resolved finding per (detector, device, metric, instance)" is `UNIQUE ... WHERE state <> 'resolved'`. Timescale requires every unique index on a hypertable to include the partitioning column, so the index that makes the sweep idempotent could not exist. `alarm` is a plain table for the same reason (`0005:101-107`). |
| Rows are updated, not appended | A finding's `last_seen`, `occurrences` and `score` move every sweep, and `anomaly_state` is a checkpoint (one row per series, overwritten). Compressed chunks penalise updates, and a 1-day compression lag like `0053:56` would compress rows that are still live. |
| Other tables reference them | `finding → incident`, `incident → finding`, `finding → baseline`. FKs that point *at* hypertables are recent and restricted in TimescaleDB (support for them arrived in 2.16, and this was not verified against the `timescale/timescaledb-ha:pg16` image, `P:deploy/docker-compose.yml:13`). Plain tables avoid the question. |
| They are small | §6: under ~200 MB in total after two years at the design budget. |
| Downgrades stay safe | There are no policy jobs to remove first, so the "tuple concurrently deleted" downgrade race described at `0002:150-163` and `0042:73-77` cannot happen here. |

Retention is therefore a **nightly prune** run by `_maybe_fit_baselines`
under its advisory lock (§6.3), not `add_retention_policy`.

## 4. DDL (migration 0111)

All DDL is raw SQL in `op.execute`, one statement per call, following
`0068_an_outbound_call_is_not_a_publish.py:44-101`.

**Constraint naming.** The metadata naming convention is
`ck_%(table_name)s_%(constraint_name)s` (`P:backend/app/db/base.py:13-19`),
and alembic applies it to `op.create_check_constraint` **and** to
`op.drop_constraint(..., type_="check")` (`0109:26-28`; memory rule "Alembic ck
naming"). Raw SQL bypasses the convention, so the name written is the live
name. Every CHECK below is therefore written literally as
`ck_<table>_<what>`, which is exactly what the convention would expand a bare
`<what>` to. A later migration can then drop it either with raw
`ALTER TABLE <t> DROP CONSTRAINT ck_<t>_<what>` or with
`op.drop_constraint("<what>", "<t>", type_="check")`, and both resolve to the
same name. Verify with
`SELECT conname FROM pg_constraint WHERE conrelid = '<t>'::regclass` after
upgrade. Named FKs follow `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s`
(`base.py:17`).

`gen_random_uuid()` comes from pgcrypto, and GiST on uuid comes from
btree_gist. Both are installed by `0001_baseline.py:38-39`. `NULLS NOT DISTINCT`
needs PostgreSQL 15+, and the image is pg16 (`docker-compose.yml:13`).

### 4.1 `anomaly_detector`

```sql
CREATE TABLE anomaly_detector (
    id               text        PRIMARY KEY,            -- 'CR-1', 'DQ-3' (doc 28 IDs); 'L5' switch row
    version          integer     NOT NULL DEFAULT 1,     -- bumped on any params/logic change
    layer            smallint    NOT NULL,               -- 0 | 2 | 3 | 4 | 5 (the L5 switch row)
    phase            smallint    NOT NULL,               -- doc 28 phase that ships it
    name             text        NOT NULL,               -- 'CRAH supply over setpoint'
    diagnosis        text        NOT NULL,               -- what it means physically, one line
    device_types     text[]      NOT NULL,               -- device_type.code values
    -- The metric whose series keys the finding. '*' = every series the
    -- detector reads (L0 data quality; RM-2 over several temperature keys).
    primary_metric   text        NOT NULL,
    required_metrics text[]      NOT NULL DEFAULT '{}',  -- 'key' or 'key{INSTANCE}'
    default_lane     text        NOT NULL DEFAULT 'advisory',
    default_kind     text        NOT NULL DEFAULT 'undetermined',
    -- Used when the finding is in (or promoted to) the alert lane.
    alarm_type       text,
    severity         severity_t,
    cadence_s        integer     NOT NULL DEFAULT 60,
    params           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    enabled          boolean     NOT NULL DEFAULT true,
    notes            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    updated_by       text,
    -- Detector IDs, plus the single L5 correlator switch row (doc 33 §3).
    CONSTRAINT ck_anomaly_detector_id
        CHECK (id ~ '^[A-Z]{2}-[0-9]{1,2}$' OR id = 'L5'),
    CONSTRAINT ck_anomaly_detector_layer
        CHECK (layer IN (0, 2, 3, 4, 5)),
    -- Layer 5 only as the 'L5' row: it is a switch, not a scorer.
    CONSTRAINT ck_anomaly_detector_l5
        CHECK ((layer = 5) = (id = 'L5')),
    CONSTRAINT ck_anomaly_detector_phase
        CHECK (phase BETWEEN 1 AND 5),
    -- The L5 row scores no device type; every detector scores at least one.
    CONSTRAINT ck_anomaly_detector_device_types
        CHECK (cardinality(device_types) > 0 OR layer = 5),
    CONSTRAINT ck_anomaly_detector_primary_metric
        CHECK (primary_metric ~ '^([a-z][a-z0-9_]*|\*)$'),
    CONSTRAINT ck_anomaly_detector_lane
        CHECK (default_lane IN ('advisory', 'alert')),
    CONSTRAINT ck_anomaly_detector_kind
        CHECK (default_kind IN ('sensor', 'process', 'undetermined')),
    -- Policy: derived alerts are MINOR or WARNING (doc 28 lane table). MAJOR
    -- is reachable only through L5 promotion at runtime, never as a default.
    CONSTRAINT ck_anomaly_detector_severity
        CHECK (severity IS NULL OR severity IN ('WARNING', 'MINOR')),
    CONSTRAINT ck_anomaly_detector_alert
        CHECK (default_lane = 'advisory'
               OR (alarm_type IS NOT NULL AND severity IS NOT NULL)),
    CONSTRAINT ck_anomaly_detector_alarm_type
        CHECK (alarm_type IS NULL OR alarm_type ~ '^[a-z][a-z0-9_]*$'),
    -- A multiple of the 60 s sweep: L2 at 60, L3/L4 at 300 (doc 28 layer table).
    CONSTRAINT ck_anomaly_detector_cadence
        CHECK (cadence_s >= 60 AND cadence_s % 60 = 0),
    CONSTRAINT ck_anomaly_detector_params
        CHECK (jsonb_typeof(params) = 'object')
);

-- Two detectors may never share an alarm_type. The alarm key is
-- (device, alarm_type, instance) (0005:105), so a shared type would merge two
-- diagnoses into one alarm row and let one detector clear the other's alert.
CREATE UNIQUE INDEX ux_anomaly_detector_alarm_type
    ON anomaly_detector (alarm_type) WHERE alarm_type IS NOT NULL;
```

**The `L5` row is the correlator's switch** (doc 33 §3 and its "L5 switch
row" deviation). It has `layer = 5`, `phase = 2`, `device_types = '{}'`,
`primary_metric = '*'`, `default_lane = 'advisory'`, and no `alarm_type`.
Its `params` hold `major_enabled` (default **false**) and `use_influence`
(default false). Turning L5 off, turning MAJOR escalation on, and switching to
zone grouping are therefore all the same `PATCH /detectors/L5`, versioned and
audited like any detector. It never owns findings: `anomaly_finding.layer` stays
`0|2|3|4` (§4.7). Its version is stamped into `anomaly_incident.evidence.v_l5`.

There is no index on `enabled` or `layer`. The table holds about 60 rows and
is read whole into the catalogue cache (`analytics/catalogue.py`), so a
sequential scan is the right plan.

`version` is the detector's identity for evaluation. Every finding copies it
(`anomaly_finding.detector_version`), so the harness in doc 28 §"Score events"
reports per `(id, version)`. PATCH `/detectors/{id}` with changed `params`
increments it (doc 30). The before/after values go to `audit_log`
(`0014_audit_log.py:28-45`), so params history needs no table of its own.

### 4.2 `anomaly_override`

This mirrors `alarm_rule_override` (`0108:41-64`): a null column inherits the
detector's value, and `enabled = false` is the mute.

```sql
CREATE TABLE anomaly_override (
    detector_id text        NOT NULL REFERENCES anomaly_detector(id) ON DELETE CASCADE,
    device_id   uuid        NOT NULL REFERENCES device(id)           ON DELETE CASCADE,
    params      jsonb,                    -- null = inherit; keys merged over detector.params
    enabled     boolean,                  -- false = mute on this device; null = inherit
    -- Time-limited shelving. ISA-18.2 shelving expires; a permanent mute is
    -- how a sensor nobody fixed stays dark for a year. Null = until removed.
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
    -- A mute must say why. Deliberately stricter than alarm_rule_override,
    -- whose reason is nullable (0108:56): a muted detector is invisible.
    CONSTRAINT ck_anomaly_override_mute_reason
        CHECK (enabled IS DISTINCT FROM false OR reason IS NOT NULL)
);
-- Device page "what is overridden here", and the FK cascade from device.
CREATE INDEX ix_anomaly_override_device ON anomaly_override (device_id);
```

The PK `(detector_id, device_id)` serves `PUT/DELETE /overrides/{detector_id}/{device_id}`
and the sweep's lookup. Effective params are a shallow merge,
`detector.params || override.params`, with the override winning key by key.
Effective enabled is `DCIM_ANOMALY_ENABLED AND detector.enabled AND
COALESCE(override.enabled, true)`, ignoring an override whose `expires_at` has
passed.

### 4.3 `anomaly_baseline_exclusion`

```sql
CREATE TABLE anomaly_baseline_exclusion (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Exactly one scope. Site-wide exists for weather events and campaigns.
    device_id     uuid        REFERENCES device(id)     ON DELETE CASCADE,
    room_id       uuid        REFERENCES room(id)       ON DELETE CASCADE,
    datacenter_id uuid        REFERENCES datacenter(id) ON DELETE CASCADE,
    detector_id   text        REFERENCES anomaly_detector(id) ON DELETE CASCADE, -- null = all
    metric_id     smallint    REFERENCES metric(id),                             -- null = all
    starts_at     timestamptz NOT NULL,
    ends_at       timestamptz,               -- null = still open (fault in progress)
    source        text        NOT NULL,
    source_ref    text,                      -- maintenance_window.id | sim ledger id | finding id
    reason        text        NOT NULL,
    created_by    text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_anomaly_baseline_exclusion_scope
        CHECK (num_nonnulls(device_id, room_id, datacenter_id) = 1),
    CONSTRAINT ck_anomaly_baseline_exclusion_dates
        CHECK (ends_at IS NULL OR ends_at > starts_at),
    CONSTRAINT ck_anomaly_baseline_exclusion_source
        CHECK (source IN ('maintenance', 'injected_fault', 'confirmed_finding', 'manual'))
);

-- Idempotent sync: re-importing a maintenance window or a simulator ledger
-- entry must not duplicate it. NULLS NOT DISTINCT because two of the three
-- scope columns are always null.
CREATE UNIQUE INDEX ux_anomaly_baseline_exclusion_source
    ON anomaly_baseline_exclusion (source, source_ref, device_id, room_id, datacenter_id)
    NULLS NOT DISTINCT
    WHERE source_ref IS NOT NULL;

-- The nightly fit's one question per series: "which spans overlapping
-- [fit_from, fit_to) apply to this device". GiST over (uuid, range) answers it
-- with one index probe; btree_gist (0001:39) supplies the uuid opclass.
CREATE INDEX ix_anomaly_baseline_exclusion_device_span
    ON anomaly_baseline_exclusion
    USING gist (device_id, tstzrange(starts_at, COALESCE(ends_at, 'infinity'::timestamptz), '[)'))
    WHERE device_id IS NOT NULL;
CREATE INDEX ix_anomaly_baseline_exclusion_room_span
    ON anomaly_baseline_exclusion
    USING gist (room_id, tstzrange(starts_at, COALESCE(ends_at, 'infinity'::timestamptz), '[)'))
    WHERE room_id IS NOT NULL;
-- GET /exclusions lists newest first; also the 2-year prune.
CREATE INDEX ix_anomaly_baseline_exclusion_starts
    ON anomaly_baseline_exclusion (starts_at DESC);
```

There is no datacenter-span index. With two datacenters a site-wide span is a
handful of rows, and a scan is cheaper than the index.

Exclusions are **copied**, not joined live from `maintenance_window`
(`0046:43-70`). Windows are edited and cancelled after the fact, and the
simulator ledger lives in another process. The exclusion row is the record of
what a given fit actually left out, and `anomaly_baseline.exclusion_ids` points
at it.

### 4.4 `anomaly_baseline`

```sql
CREATE TABLE anomaly_baseline (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    detector_id      text        NOT NULL REFERENCES anomaly_detector(id) ON DELETE CASCADE,
    detector_version integer     NOT NULL,
    -- Device-scoped for every detector except the room-scoped influence fit
    -- (Phase 4, doc 27 D9), which is stored under RM-4 with device_id null.
    device_id        uuid        REFERENCES device(id) ON DELETE CASCADE,
    room_id          uuid        REFERENCES room(id)   ON DELETE CASCADE,
    metric_id        smallint    NOT NULL REFERENCES metric(id),
    instance         text        NOT NULL DEFAULT '',
    model_kind       text        NOT NULL,
    model            jsonb       NOT NULL,      -- §7.3
    source_rollup    text        NOT NULL,      -- what the fit read
    fit_from         timestamptz NOT NULL,
    fit_to           timestamptz NOT NULL,
    n                integer     NOT NULL,      -- buckets used, after exclusions
    excluded_s       integer     NOT NULL DEFAULT 0,
    exclusion_ids    uuid[]      NOT NULL DEFAULT '{}',
    r2               double precision,
    resid_sigma      double precision,          -- at the fit resolution; per-resolution in model
    status           text        NOT NULL,
    is_current       boolean     NOT NULL DEFAULT true,
    fitted_at        timestamptz NOT NULL DEFAULT now(),
    superseded_at    timestamptz,
    CONSTRAINT ck_anomaly_baseline_scope
        CHECK (num_nonnulls(device_id, room_id) = 1),
    CONSTRAINT ck_anomaly_baseline_model_kind
        CHECK (model_kind IN ('ols', 'profile_how', 'curve', 'first_order', 'peer', 'influence')),
    CONSTRAINT ck_anomaly_baseline_source_rollup
        CHECK (source_rollup IN ('telemetry_5m', 'telemetry_1h', 'telemetry_1d')),
    CONSTRAINT ck_anomaly_baseline_status
        CHECK (status IN ('ok', 'thin', 'rejected')),
    CONSTRAINT ck_anomaly_baseline_span
        CHECK (fit_to > fit_from),
    CONSTRAINT ck_anomaly_baseline_model
        CHECK (jsonb_typeof(model) = 'object'),
    -- A rejected fit never becomes current: the scorer keeps the last good one.
    CONSTRAINT ck_anomaly_baseline_current
        CHECK (NOT is_current OR (status <> 'rejected' AND superseded_at IS NULL))
);

-- One current model per series. The nightly swap (UPDATE old SET is_current =
-- false, superseded_at = now(); INSERT new) runs in one transaction per batch,
-- and this index makes a double-fit from two workers fail loudly instead of
-- leaving two "current" models.
CREATE UNIQUE INDEX ux_anomaly_baseline_current
    ON anomaly_baseline (detector_id, device_id, room_id, metric_id, instance)
    NULLS NOT DISTINCT
    WHERE is_current;

-- GET /expected?device_id&metric&instance&from&to: the band at time t comes
-- from the fit that was current at t (fitted_at <= t < superseded_at), so
-- historical bands need superseded rows too. Not partial for that reason.
CREATE INDEX ix_anomaly_baseline_series
    ON anomaly_baseline (device_id, metric_id, instance, fitted_at DESC);

-- The 30-day prune of superseded fits.
CREATE INDEX ix_anomaly_baseline_prune
    ON anomaly_baseline (superseded_at) WHERE NOT is_current;
```

`source_rollup` allows `telemetry_5m`, `telemetry_1h` and `telemetry_1d`.
Raw data is kept 30 days (`0051:35`) and 1m data 7 days (`0052:50`). 5m
lasts 2 years (`0052:51`), and 1h and 1d are kept forever (`0002:143-146`,
`0042:17-19`). A fit longer than 7 days therefore cannot read 1m.

### 4.5 `anomaly_state`

```sql
CREATE TABLE anomaly_state (
    detector_id      text        NOT NULL REFERENCES anomaly_detector(id) ON DELETE CASCADE,
    device_id        uuid        NOT NULL REFERENCES device(id)           ON DELETE CASCADE,
    metric_id        smallint    NOT NULL REFERENCES metric(id),
    instance         text        NOT NULL DEFAULT '',
    detector_version integer     NOT NULL,
    baseline_id      uuid        REFERENCES anomaly_baseline(id) ON DELETE SET NULL,
    state            jsonb       NOT NULL,          -- §7.2
    last_sample_at   timestamptz,                   -- newest sample folded into state
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (detector_id, device_id, metric_id, instance),
    CONSTRAINT ck_anomaly_state_state CHECK (jsonb_typeof(state) = 'object')
) WITH (fillfactor = 80);

-- FK cascade from device (the PK leads with detector_id, so it cannot serve it)
-- and the device page's "what is this device accumulating".
CREATE INDEX ix_anomaly_state_device ON anomaly_state (device_id);
```

`fillfactor = 80` and leaving `updated_at`, `state` and `last_sample_at`
unindexed keep checkpoint upserts HOT (heap-only), so a checkpoint rewrites
no index entries. The 7-day stale-row prune scans about 1,400 rows and needs
no index.

### 4.6 `anomaly_incident`

```sql
CREATE TABLE anomaly_incident (
    id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    state                 text        NOT NULL DEFAULT 'open',
    -- What the incident is about, as one comparable key: 'device:<uuid>' when
    -- L5 found a root device, 'room:<uuid>' for the room-level fallback used
    -- until the influence fit exists (doc 28 §cascade).
    root_key              text        NOT NULL,
    tier                  text        NOT NULL,
    root_device_id        uuid        REFERENCES device(id) ON DELETE SET NULL,
    root_finding_id       uuid,       -- FK added in §4.8 (circular)
    root_alarm_id         uuid        REFERENCES alarm(id)  ON DELETE SET NULL,
    datacenter_id         uuid        REFERENCES datacenter(id) ON DELETE SET NULL,
    room_id               uuid        REFERENCES room(id)   ON DELETE SET NULL,
    title                 text        NOT NULL,
    summary               text,                       -- deterministic template (analytics/evidence.py)
    member_alarm_ids      uuid[]      NOT NULL DEFAULT '{}',
    member_count          integer     NOT NULL DEFAULT 0,  -- findings + alarms, denormalised for the list
    impact_kw             numeric(10,2),
    evidence              jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- the bundle, §7.4
    explanation           jsonb,                      -- L6, §7.5; null when explainer = none
    explained_at          timestamptz,
    -- Operator thumbs on the L6 text (POST /incidents/{id}/explain/feedback, doc 30).
    explanation_feedback  jsonb       NOT NULL DEFAULT '[]'::jsonb,
    opened_at             timestamptz NOT NULL DEFAULT now(),
    last_activity_at      timestamptz NOT NULL DEFAULT now(),
    resolved_at           timestamptz,
    CONSTRAINT ck_anomaly_incident_state
        CHECK (state IN ('open', 'resolved')),
    CONSTRAINT ck_anomaly_incident_resolved
        CHECK ((state = 'resolved') = (resolved_at IS NOT NULL)),
    -- Doc 28's five cooling tiers, the power hop above them, and the room fallback.
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
);

-- One open incident per root. L5 merges into it instead of opening a second;
-- when re-ranking moves the root, the UPDATE of root_key hits this index and
-- the correlator merges the two incidents.
CREATE UNIQUE INDEX ux_anomaly_incident_open_root
    ON anomaly_incident (root_key) WHERE state = 'open';
-- GET /incidents default: open, by impact then age.
CREATE INDEX ix_anomaly_incident_open
    ON anomaly_incident (impact_kw DESC NULLS LAST, opened_at DESC) WHERE state = 'open';
-- GET /incidents?since=..., history, and the 2-year prune.
CREATE INDEX ix_anomaly_incident_opened ON anomaly_incident (opened_at DESC);
-- "Which incident is this alarm in": the alarm detail evidence panel.
CREATE INDEX ix_anomaly_incident_member_alarms
    ON anomaly_incident USING gin (member_alarm_ids);
CREATE INDEX ix_anomaly_incident_root_alarm
    ON anomaly_incident (root_alarm_id) WHERE root_alarm_id IS NOT NULL;
CREATE INDEX ix_anomaly_incident_root_finding
    ON anomaly_incident (root_finding_id) WHERE root_finding_id IS NOT NULL;
```

**Explanation feedback is a column, not a table**, which answers the
coordinator's question. Thumbs are low-volume: at most a few per incident, and
incidents run at ~10 a day (§6). They are only ever read together with the
explanation they rate, and the explanation is itself a column on this row.
A separate table would add a join and an FK for no query that needs it. Each
element is `{at, by, vote: "up"|"down", explanation_sha256, note?}`.
`explanation_sha256` ties a vote to the text that was shown, so a
re-explanation does not inherit the previous text's votes. The vote is also
written to `audit_log`, which is how the "operator thumbs tracked" exit
criterion of doc 28 Phase 5 is counted across incidents.

### 4.7 `anomaly_finding`

```sql
CREATE TABLE anomaly_finding (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    detector_id      text        NOT NULL REFERENCES anomaly_detector(id) ON DELETE RESTRICT,
    detector_version integer     NOT NULL,
    layer            smallint    NOT NULL,
    lane             text        NOT NULL,
    kind             text        NOT NULL DEFAULT 'undetermined',
    state            text        NOT NULL DEFAULT 'open',
    -- The operator's latest verdict, kept through resolution (§8.1).
    verdict          text,
    device_id        uuid        NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    metric_id        smallint    NOT NULL REFERENCES metric(id),
    instance         text        NOT NULL DEFAULT '',
    -- Where the device was when the finding opened, for the datacenter/room
    -- filters. Stamped once, like alarm.category: history says what was true then.
    datacenter_id    uuid        REFERENCES datacenter(id) ON DELETE SET NULL,
    room_id          uuid        REFERENCES room(id)       ON DELETE SET NULL,
    first_seen       timestamptz NOT NULL,   -- condition onset (start of dwell)
    raised_at        timestamptz NOT NULL,   -- dwell satisfied, finding opened
    last_seen        timestamptz NOT NULL,   -- last sweep the condition held
    occurrences      integer     NOT NULL DEFAULT 1,  -- sweeps the condition held
    reopen_count     integer     NOT NULL DEFAULT 0,  -- reopened inside reopen_window_s
    score            double precision,       -- current normalised score (z, CUSUM/h, ...)
    score_peak       double precision,
    expected         double precision,       -- in the metric's registry unit
    observed         double precision,
    impact_kw        numeric(10,2),          -- triage sort key (doc 28 §advisories)
    summary          text        NOT NULL,   -- deterministic template text
    evidence         jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- §7.4
    baseline_id      uuid        REFERENCES anomaly_baseline(id) ON DELETE SET NULL,
    incident_id      uuid        REFERENCES anomaly_incident(id) ON DELETE SET NULL,
    alarm_id         uuid        REFERENCES alarm(id)            ON DELETE SET NULL,
    promoted_at      timestamptz,            -- advisory -> alert
    feedback         jsonb       NOT NULL DEFAULT '[]'::jsonb,   -- append-only, §8.4
    acknowledged_at  timestamptz,
    acknowledged_by  text,
    resolved_at      timestamptz,
    resolved_by      text,                   -- 'system' or a user
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
    -- While a verdict state is live, the verdict column says the same thing.
    CONSTRAINT ck_anomaly_finding_verdict_state
        CHECK (state NOT IN ('confirmed', 'dismissed') OR verdict IS NOT DISTINCT FROM state),  -- not '= state': a NULL verdict would make the CHECK NULL, which Postgres passes
    CONSTRAINT ck_anomaly_finding_resolution
        CHECK (resolution IS NULL OR resolution IN
               ('cleared', 'device_retired', 'detector_disabled', 'muted', 'superseded', 'manual')),
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
) WITH (fillfactor = 80);
```

Indexes, each with the query path it serves:

```sql
-- THE dedupe key, and the ON CONFLICT target of the sweep's upsert. One
-- non-resolved finding per series per detector; a dismissed finding keeps the
-- key occupied until the condition clears (§8.1), which is what stops a
-- dismissal from re-raising on the next sweep.
CREATE UNIQUE INDEX ux_anomaly_finding_open_key
    ON anomaly_finding (detector_id, device_id, metric_id, instance)
    WHERE state <> 'resolved';

-- Reopen window: "was this exact series resolved in the last reopen_window_s?"
-- Absorbs flapping into reopen_count instead of a new row per flap.
CREATE INDEX ix_anomaly_finding_recent_resolved
    ON anomaly_finding (detector_id, device_id, metric_id, instance, resolved_at DESC)
    WHERE state = 'resolved';

-- GET /findings default view (non-resolved, by lane, impact then age).
-- Deliberately NOT on last_seen: last_seen moves every sweep, and indexing it
-- would make every touch a non-HOT update.
CREATE INDEX ix_anomaly_finding_active
    ON anomaly_finding (lane, impact_kw DESC NULLS LAST, first_seen DESC)
    WHERE state <> 'resolved';

-- ?device_id= and the device page; also the FK cascade from device.
CREATE INDEX ix_anomaly_finding_device ON anomaly_finding (device_id, first_seen DESC);

-- ?detector= and the per-detector precision / findings-per-day budget.
CREATE INDEX ix_anomaly_finding_detector ON anomaly_finding (detector_id, first_seen DESC);

-- ?room=. (?datacenter= has two values estate-wide; it rides the since index
-- plus a filter rather than earning an index of its own.)
CREATE INDEX ix_anomaly_finding_room
    ON anomaly_finding (room_id, first_seen DESC) WHERE room_id IS NOT NULL;

-- ?since=, history listing, and findings/hour for GET /health.
CREATE INDEX ix_anomaly_finding_first_seen ON anomaly_finding (first_seen DESC);

-- Incident members (GET /incidents/{id}), and FK SET NULL on incident prune.
CREATE INDEX ix_anomaly_finding_incident
    ON anomaly_finding (incident_id) WHERE incident_id IS NOT NULL;

-- Alarm detail evidence panel ("the finding behind this derived alarm"), and
-- FK SET NULL when alarms are deleted with their device: without it, each
-- alarm delete would scan this table.
CREATE INDEX ix_anomaly_finding_alarm
    ON anomaly_finding (alarm_id) WHERE alarm_id IS NOT NULL;

-- FK SET NULL during the nightly prune of ~1,400 superseded baselines.
CREATE INDEX ix_anomaly_finding_baseline
    ON anomaly_finding (baseline_id) WHERE baseline_id IS NOT NULL;

-- The 2-year prune of resolved findings.
CREATE INDEX ix_anomaly_finding_prune
    ON anomaly_finding (resolved_at) WHERE state = 'resolved';
```

**HOT discipline.** A sweep touches `last_seen`, `occurrences`, `score`,
`score_peak`, `observed` and `updated_at` on every non-resolved finding. None of
them is indexed, so the touch is a HOT update. `impact_kw` *is* indexed,
because it is the triage sort, so it is rewritten only when it changes by more
than 10% or 1 kW. `evidence` is rewritten only on open, state change,
escalation, or at most every 15 min, to avoid rewriting a TOASTed jsonb every
60 s [judgement].

### 4.8 The circular FK

```sql
ALTER TABLE anomaly_incident
    ADD CONSTRAINT fk_anomaly_incident_root_finding_id_anomaly_finding
    FOREIGN KEY (root_finding_id) REFERENCES anomaly_finding(id) ON DELETE SET NULL;
```

## 5. Contract and design deviations

| # | Kind | What | Recommendation |
|---|---|---|---|
| D1 | Contract deviation | The contract names L0 as `analytics/detectors/quality.py` but doc 28 gives **no detector IDs for L0**. It lists six checks (doc 28 §"Sensor faults"). | Use `DQ-1`…`DQ-6` (data quality). `DQ-1`–`DQ-5` are seeded in 0112. `DQ-6` (bias/drift via a redundancy residual) needs baselines, so it ships in 0114. |
| D2 | Contract deviation | Settings list lacks a credential and a timeout for a cloud explainer. | Added per doc 30: `DCIM_ANOMALY_EXPLAINER_API_KEY` (SecretStr) and `DCIM_ANOMALY_EXPLAINER_TIMEOUT_S` (§11). |
| D3 | Contract clarification | `Finding.state` has five values. If `resolved` overwrote `confirmed` or `dismissed`, the verdict needed for per-detector precision would be lost. | Keep the five states as named, and add a `verdict` column that survives resolution (§8.1). Nothing is renamed. |
| D4 | Contract clarification | "Redis keys (… sweep lock/heartbeat)". The sweep **lock** is a Postgres advisory lock (contract, worker section). | Redis carries the heartbeat and lock holder (for `/health`) and the cadence gate, but never the lock (§10). |
| D5 | Contract clarification | "0112 … any taxonomy rows if table-backed". The taxonomy is **code-backed**: `BY_ALARM_TYPE` is a Python dict (`P:backend/app/core/alert_taxonomy.py:328`), and its SQL CASE is generated from it at raise time (`alert_taxonomy.py:710`; used at `repositories/alarms.py:250-253`). | 0112 writes no taxonomy SQL. The Phase 1 code change adds `BY_ALARM_TYPE` entries and the matching `NON_TRAP_ALARM_TYPES` test entries (§9.3). |
| D6 | Design deviation (doc 28) | Doc 28 says the next migration after "0108" applies (doc 28 §"The code already has the slots"). The head is **0110** (`0110_an_endpoint_can_have_a_write_credential.py:27-28`). | Use 0111–0115 per the contract. |
| D7 | Design deviation (doc 28) | **VA-1 on CRAH valves is not measurable.** A CRAH exposes one valve point, `CHW_Valve → valve_position_pct{CHW}` (`P:contracts/mappings/bacnet/objects.yaml:152`), with no commanded/feedback pair. Only `valve` devices carry `MEASURED`/`COMMANDED` (`objects.yaml:115-116`), which is what `plant.py` compares (`P:backend/app/services/plant.py:209-223,652-657`). | VA-1 `device_types = {valve}`. Hunting (VA-2 / CR-6) needs only one position, so CR-6 still applies to CRAHs. Real iCOM units usually publish the valve *command*. Feedback depends on firmware and actuator. |
| D8 | Design deviation (doc 28) | **PU-3 "command vs proving" has no command point.** The pump mapping has `Run_Status` (proof) but no start command (`objects.yaml:76-88`). | Seed PU-3 in its measurable form: proof disagrees with process (`Run_Status = 0` while speed > 10% or flow > 5% of the running mean, or `Run_Status = 1` while flow < 5% after 60 s). Standby-fails-to-start needs lead/standby pairing from the cooling layer and is deferred. |
| D9 | Design deviation (doc 28) | **CR-4 (CRAH filter ΔP) has no signal.** The CRAH mapping has only the `Filter_Dirty` boolean (`objects.yaml:161`) and no `filter_diff_pressure`. The CDU does have `Filter_DP` (`objects.yaml:137`). | **Resolved by doc 32/33.** The simulator publishes a new CRAH BACnet point `Filter_DP`, mapped to the existing key `filter_diff_pressure` (doc 32 D6). Simulator S1 moved to Phase 1, so the mapping row and the Phase 1 collector rebuild land there (doc 33 task 1.22), and no registry key or `metric` upsert is needed. 0114 seeds CR-4 **enabled**, conditional on 1.22 having shipped. If it has not, 0114 seeds it disabled. Its history is about 5 weeks old by the Phase 3 fit (doc 33). CD-3 is unaffected. |
| D10 | Design deviation (doc 28) | **CH-3 (chiller approach drift) has no approach point.** The chiller mapping has no `approach_temp` and no refrigerant temperatures, only `water_pressure{EVAP,COND}` (`objects.yaml:53-73`). | **Resolved by doc 32/33.** The simulator publishes chiller `Cond_Approach_Temp` and `Evap_Approach_Temp`, mapped to the existing key as `approach_temp{COND}` and `approach_temp{EVAP}` (doc 32 D6; doc 33 task 1.22, Phase 1 collector rebuild). CH-3 scores both instances, so condenser fouling and evaporator fouling are distinguished by instance. 0114 seeds it **enabled** on the same 1.22 condition as CR-4. On a real Carrier 19DV the equivalent points are vendor-dependent and were not checked against a point list. CT-1 is fine: tower approach = `water_supply_temp{COND}` − `outdoor_wet_bulb_temp`, both mapped (`objects.yaml:96-97,107-108`). |
| D11 | Design deviation (doc 28) | **PL-1/PL-2 have no anchor device.** Doc 28 itself notes the CHW loop has no entity. A finding needs `device_id NOT NULL`. | Seed PL-1 per chiller (evaporator ΔT vs flow), disabled. PL-2 (plant balance) waits for a loop entity, and is not seeded. |
| D12 | Design deviation (doc 28) | Doc 28 says `raise_alarm` gives "device rollup … for free". It does not: callers run `refresh_device_alarm_state` themselves (`P:backend/app/alarms/service.py:254,305,409`). | The sweep calls it for touched devices, as `sweep_staleness` does (`service.py:826-917`). |
| D13 | Design deviation (doc 28) | Doc 28 asks whether `raise_alarm` carries arbitrary detail. `alarm.attributes jsonb` exists (`0005:99`), but `raise_alarm`'s signature has no attributes argument (`P:backend/app/repositories/alarms.py:217-225`) and its INSERT does not write the column (`:241-245`). | Per doc 30: add an optional `attributes` argument to `raise_alarm`, merged as `alarm.attributes || EXCLUDED.attributes`. **No new column on `alarm`, and no migration.** |
| D14 | Code trap | `detection_for` returns `state`, not `derived`, whenever `metric_key` is `alarm_state` or `equipment_state` (`alert_taxonomy.py:694-707`). CH-4 and PU-3 read `equipment_state`. | Analytics raises pass the primary *numeric* metric as `metric_key`, or `None` (for example CH-4), never a state key. Test in doc 33. |
| D15 | Policy vs doc 28 | Doc 28 calls PU-3 a "MAJOR candidate". Policy says derived alerts are MINOR/WARNING. | `ck_anomaly_detector_severity` enforces MINOR/WARNING defaults. MAJOR is reached only when L5 escalates an incident root's existing alarm in place, with confirmed impact (doc 28 §"Promotion"; doc 33 task 2.4), and only while `L5.params.major_enabled` is true (default false, §12.1). |
| D17 | Contract deviation (doc 33) | Doc 33 wants a catalogue row for the L5 correlator so every rollback is one PATCH. The contract lists L5 only as `analytics/correlator.py`. | Seed an `L5` switch row in 0112 (§4.1, §12.1). This required widening `ck_anomaly_detector_id`, `_layer` and `_device_types`, and adding `ck_anomaly_detector_l5`. |
| D18 | Resolves doc 33 Q12 | 29 seeded CD-1 `over_setpoint_k 1.0`. The display verdict uses `CDU_SUPPLY_FAULT_MARGIN_K = 2.0` (`P:backend/app/services/thermal.py:177`). The display and the alert must share one value (`test_anomaly_rule_parity`, doc 33). | The CD-1 seed is now **2.0 K** (§12.1). The detector reads the `thermal.py` constant's value as its default, so the two cannot drift. |
| D19 | Resolves doc 33 Q17 | 29 seeded CD-5, SV-1, UP-1 and TX-1 enabled with no simulator ground truth (doc 32 §13.6). | 0114 seeds all four **disabled** until simulator S4 provides `loop_leak`, `server_thermal`, `room_hvac_loss` and `xfmr_cooling` (§12.2). No pre-fit PATCH is needed. |
| D16 | Doc 28 naming | Doc 28 lists `anomaly_incident.members` and `opened/closed`. | Findings join through `anomaly_finding.incident_id` and alarms through `member_alarm_ids uuid[]`. The timestamps are `opened_at`/`resolved_at`, because the contract's incident state is open\|resolved. |

## 6. Retention, compression and sizing (~716 devices)

The estate counts come from doc 28 §"Every device class": 310 servers, 82
sensors, 80 PDUs, 28 CRAHs, 24 EV2s, 16 RPPs, 14 pumps, 12 CDUs, 6 chillers, 6
towers, 4 UPSs, 4 generators, 4 ATSs, 2 transformers and 4 valves. Every figure
below is an estimate [judgement], and the arithmetic is shown so it can be
redone with measured numbers.

### 6.1 Series counts

**L0 series** (Phase 1), as (device, metric, instance) triples scored for
data quality:

| Class | Count × numeric series | Series |
|---|---|---|
| CRAH | 28 × 9 (supply, return, setpoint, fan, valve, output, RH, airflow, power) | 252 |
| Chiller | 6 × 12 | 72 |
| Tower | 6 × 10 | 60 |
| Pump | 14 × 8 | 112 |
| CDU | 12 × 12 | 144 |
| Valve | 4 × 3 | 12 |
| Sensor | 82 × 3 | 246 |
| PDU probe | 80 × 2 | 160 |
| UPS / TX / Gen | 4×4 + 2×2 + 4×3 | 32 |
| Server | 310 × 4 (inlet, CPU temp, fan, power) | 1,240 |
| **Total** | | **2,330** |

**L2 detector instances** (Phase 1): CR-1/2/6 3×28 = 84; CH-1/4/5 3×6 = 18;
CD-1/6 2×12 = 24; VA-1/2 2×4 = 8; PU-3 14; RM-2/3 2×(82+80) = 324; GN-1 4;
CT-2/3 (disabled) 12. That gives **≈ 488**.

**L3 instances** (Phase 3): CH-2/3 12; CT-1/4 12; PU-1/2/4 42; CR-3/4 56;
CD-2/3/4/5 48; SV-1/2 620; PD-1/2 2×(80+16+24) = 240; UP-1/2 8; TX-1 2; DQ-6
162. That gives **≈ 1,202**.

**L4 instances** (Phase 4): CR-5 28; RM-4 162; SV-3 per CDU loop 12. That
gives **≈ 202**.

### 6.2 Per table

| Table | Rows | Bytes/row (est.) | Size | Arithmetic |
|---|---|---|---|---|
| `anomaly_detector` | ~60 | 1 KB | 60 KB | — |
| `anomaly_override` | < 500 realistic | 0.3 KB | < 0.2 MB | ceiling 716 × 60 = 43k rows ≈ 13 MB, never approached |
| `anomaly_state` | ≈ 1,400 | 0.6 KB (state ~350 B + tuple/key ~90 B + 2 index entries + 20% fillfactor slack) | **≈ 0.85 MB** | (1,202 + 202) × 0.6 KB. L0/L2 state is Redis-only (§10.2). |
| `anomaly_baseline` | 1,400 current + 30 × 1,400 superseded = 43,400 | ≈ 1.1 KB avg | **≈ 48 MB** | OLS model ~0.4 KB. Hour-of-week profile 168 bins × [median, MAD, n] ≈ 5 KB JSON, ~2.5 KB after TOAST pglz. Assume 20% use profiles: 0.2×2.5 + 0.8×0.4 = 0.82 KB, + 0.25 KB row/index ≈ 1.1 KB. That is 1.5 MB/night, × 31 nights kept. The per-resolution sigma (§7.3) adds ~60 B/row. |
| `anomaly_baseline_exclusion` | ~5,000 over 2 y | 0.3 KB | **≈ 1.5 MB** | maintenance (tens/month × ~10 targets) + campaign ledger entries (hundreds per campaign) |
| `anomaly_finding` | 50/day × 730 d = 36,500 | ≈ 2.5 KB (row ~0.5 KB + evidence ~3 KB → ~1.5 KB TOASTed + 11 index entries ~0.5 KB) | **≈ 91 MB** | design budget "tens per day" (doc 28 §advisories) taken as 50/day |
| `anomaly_incident` | 10/day × 730 d = 7,300 | ≈ 8 KB (bundle ~6 KB + explanation ~2 KB, TOASTed) | **≈ 58 MB** | |
| **Total at 2 years** | | | **≈ 200 MB** | ≈ 1.4% of the 14 GB database (memory: Docker disk audit) |

A mis-tuned detector at 500 findings/day would put `anomaly_finding` at
≈ 0.9 GB over two years. That is why `/health` flags any detector above 100
findings/day, and why the chattering cap (`reopen_window_s`) folds flaps into
one row.

**Write rates.** Assume ~100 findings open at once (50/day × ~2 days
mean open time). Touches are ~100 HOT updates per 60 s sweep, about 1.7/s.
A state checkpoint is ≤ 1,400 rows every 300 s in one batched upsert: about
1 MB of WAL each time, 12 MB/h, or ~290 MB/day of WAL churn, which is recycled
rather than stored. For scale, ingest takes ~2.4 telemetry entries/s at ~49 KB,
about 10 GB/day of payload (doc 26 Phase 3 sizing, via the inventory Q1 gap
note), so the checkpoint adds about 3%.

**Read rates.**

- **Sweep (60 s).** Re-reading 30 min of raw data for every L0/L2 series is
  ≈ 650 BACnet series × 60 + 246 sensor series × 180 + 1,400 SNMP/Redfish
  series × 15, about 104k rows per sweep. Reading **incrementally** (only
  `ts > cursor` per series, through the `(device_id, metric_id, instance, ts)`
  PK, `0002:76`) with rolling windows held in Redis (§10.3) cuts it to new
  samples only: ≈ 650×2 + 246×6 + 1,400×0.5 ≈ 3.5k rows. Recent chunks are
  uncompressed for the first day (`0053:56`), so these are row-store reads.
- **Nightly fit.** 1,400 series × 28 days × 24 h of `telemetry_1h`
  ≈ 0.94 M rows, ×2 with regressors ≈ 1.9 M rows. These are batched per
  (detector, metric) with `device_id = ANY(...)`. Check the plan with `EXPLAIN`
  against the cagg's materialisation hypertable first, because its index
  layout was not verified for this access path.

### 6.3 Retention and prune rules

None of these tables gets a Timescale policy (§3). The nightly step runs the
prune after the fit, under the baseline lock, in batches of 5,000 rows per
transaction
(`DELETE ... WHERE id IN (SELECT id ... LIMIT 5000)`) so no transaction holds a
snapshot open for long on the 7.7 GB host:

| Table | Rule | Why |
|---|---|---|
| `anomaly_finding` | delete `state = 'resolved' AND resolved_at < now() - 2 years` | Matches `alarm_history`'s 2 years (`0053:46,69-70`). Verdicts are the labelled data the precision figures come from. |
| `anomaly_incident` | delete `state = 'resolved' AND resolved_at < now() - 2 years` | Same horizon. Member findings get `incident_id` set NULL first via FK. |
| `anomaly_baseline` | delete `NOT is_current AND superseded_at < now() - 30 days` | Finding evidence snapshots the coefficients used (§7.4). Bands older than 30 days are drawn with the oldest retained fit and labelled as such (doc 30). |
| `anomaly_state` | delete `updated_at < now() - 7 days` | The series stopped being scored. Device delete already cascades. |
| `anomaly_baseline_exclusion` | delete `ends_at < now() - 2 years` | Longer than any fit window (≤ 365 d for seasonal). |
| `anomaly_override` | delete `expires_at < now() - 7 days` | Expired overrides are already ignored at read. The week keeps "what was muted last week" answerable. |

**Compression.** No Timescale compression applies, since these are not
hypertables. Large jsonb (`evidence`, `model`, `explanation`) is compressed by
TOAST with pglz by default. Switching those columns to
`SET COMPRESSION lz4` is optional and cheaper on CPU. Before adding it to
0111, confirm that the image's server was built with lz4
(`SHOW default_toast_compression` accepts `lz4`). That was not verified.

## 7. jsonb shapes

Every document carries `"v"`, its schema version. Readers reject an unknown
major version instead of guessing. Units are always the metric registry's unit
for the key (`P:contracts/metrics/registry.yaml`).

### 7.1 `anomaly_detector.params` / `anomaly_override.params`

Common keys, shared by every layer:

```jsonc
{
  "v": 1,
  "dwell_s": 900,            // condition must hold this long before a finding opens
  "clear_dwell_s": 300,      // and be false this long before it resolves
  "reopen_window_s": 1800,   // a re-raise inside this reopens the same row
  "min_samples": 3,          // never decide on fewer GOOD samples than this
  "window_s": 1800,          // raw read window (L0/L2)
  "gate": {                  // operating-state gating (G36 style)
    "running_min_s": 600,              // unit proven running at least this long
    "restart_exclusion_s": 0,          // ignore this long after a start
    "exclude_fire_predischarge": true, // DSS:6336-6343 stops every CRAH in the room
    "exclude_standby": true,
    "exclude_maintenance": true        // active suppress window (0046:59)
  }
}
```

An L2 example (CR-1):

```json
{ "v": 1, "dwell_s": 900, "clear_dwell_s": 300, "reopen_window_s": 1800,
  "min_samples": 3, "window_s": 1800,
  "margin_k": 3.0, "clear_margin_k": 2.0, "valve_starved_pct": 95,
  "gate": { "running_min_s": 600, "exclude_fire_predischarge": true,
            "exclude_standby": true, "exclude_maintenance": true } }
```

An L3 example (CH-2):

```json
{ "v": 1, "dwell_s": 0, "clear_dwell_s": 3600, "reopen_window_s": 86400,
  "model": "ols", "regressors": ["compressor_load_pct", "outdoor_wet_bulb_temp"],
  "fit_days": 28, "source_rollup": "telemetry_1h", "min_bucket_samples": 40,
  "min_r2": 0.5, "min_n": 168,
  "ewma_lambda": 0.1, "cusum_k_sigma": 0.5, "cusum_h_sigma": 5.0,
  "band_k_sigma": 2.0,
  "gate": { "running_min_s": 900, "restart_exclusion_s": 480, "max_dload_pct_per_min": 2.0 },
  "impact": { "kind": "chiller_extra_kw" } }
```

### 7.2 `anomaly_state.state`

```json
{ "v": 1,
  "ewma": -0.84, "ewma_var": 0.31,
  "cusum_pos": 0.0, "cusum_neg": 3.92,
  "n": 4180, "last_ts": "2026-10-21T09:14:30Z", "last_value": 4.71,
  "gate_open_since": "2026-10-21T02:01:00Z",
  "dwell_since": null, "clear_since": null,
  "baseline_id": "6f0c...", "detector_version": 1,
  "reset": { "reason": "restore_too_old", "at": "2026-10-19T03:00:00Z" } }
```

`reset` is present only after a deliberate reset (§10.4), so an evaluation run
can see that an accumulator did not resume.

### 7.3 `anomaly_baseline.model`

Per the coordinator's item 2, **every model stores a residual σ per
resolution**, because `/expected` serves bands at 5m, 1h and 1d. A σ fitted at
1h is wrong at 5m (too narrow, since averaging shrinks noise) and wrong at 1d
(too wide). So the fitter computes residuals at each rollup over the fit window
and stores them:

```jsonc
{ "v": 1,
  "kind": "ols",
  "target": { "metric": "cop", "instance": "" },
  "coef": { "intercept": 7.12, "compressor_load_pct": 0.018, "outdoor_wet_bulb_temp": -0.094 },
  "x_ranges": { "compressor_load_pct": [22.0, 96.0], "outdoor_wet_bulb_temp": [3.1, 24.8] },
  // Outside x_ranges the scorer marks the point "extrapolated" and does not accumulate.
  "resid_sigma": { "5m": 0.21, "1h": 0.12, "1d": 0.07 },
  "resid_mad":   { "5m": 0.17, "1h": 0.10, "1d": 0.06 },
  "n_by_resolution": { "5m": 7900, "1h": 662, "1d": 27 },
  "r2": 0.81 }
```

```jsonc
{ "v": 1,
  "kind": "profile_how",          // hour-of-week median/MAD
  "target": { "metric": "fan_speed_pct", "instance": "" },
  "tz": "UTC",
  "bins": [[61.2, 1.8, 4], [60.9, 1.7, 4] /* ... 168 entries: [median, mad, n] */],
  "resid_sigma": { "5m": 2.6, "1h": 1.9, "1d": 0.8 },
  "n_by_resolution": { "5m": 7900, "1h": 662, "1d": 27 } }
```

The other kinds use the same `resid_sigma` block. `curve` (pump head/flow
curve) stores `coef` per term of H = a·N² + b·N·Q + c·Q². `first_order`
(transformer oil) stores `tau_s`, `gain`, `offset`. `peer` (L4 robust z)
stores per-peer `median`/`mad` over the fit window and the peer list. The
`influence` kind is room-scoped: an `{"crahs": [...], "racks": [...],
"w": [[...]]}` matrix plus `r2` per rack, labelled
`"estimate": true` (doc 28 §cascade, doc 27 D9).

`anomaly_baseline.resid_sigma` (the column) duplicates the σ at
`source_rollup`'s resolution, so lists can sort and filter by fit quality
without opening the jsonb.

### 7.4 `anomaly_finding.evidence` and `anomaly_incident.evidence`

A finding's evidence is the deterministic record of why it opened. Every item
has an ID that L6 must cite (doc 28 §"LLMs explain incidents"):

```json
{ "v": 1,
  "window": { "from": "2026-10-21T08:30:00Z", "to": "2026-10-21T09:15:00Z" },
  "rule": { "expr": "supply_air_temp - air_setpoint_temp > 3.0 AND valve_position_pct{CHW} >= 95",
            "dwell_s": 900, "params_version": 1 },
  "diagnosis": "chw_starved",
  "expected": 18.0, "observed": 21.6, "lower": null, "upper": 21.0,
  "signals": [
    { "id": "S1", "device_id": "…", "metric": "supply_air_temp", "instance": "",
      "min": 20.9, "max": 21.8, "mean": 21.4, "slope_per_h": 0.6, "last": 21.6, "n": 31 },
    { "id": "S2", "device_id": "…", "metric": "valve_position_pct", "instance": "CHW",
      "min": 96.0, "max": 100.0, "mean": 99.1, "slope_per_h": 0.0, "last": 100.0, "n": 31 } ],
  "gates": { "running_since": "2026-10-20T22:00:00Z", "fire_predischarge": false,
             "standby": false, "maintenance_window": null },
  "masks": [ { "detector": "DQ-5", "series": "…|supply_air_temp|", "masked": false } ],
  "baseline": null,
  "alarm_history": [] }
```

For L3, `baseline` holds `{id, kind, fitted_at, coef, resid_sigma}`, a
snapshot that outlives the 30-day baseline prune. `alarm_history` lists the
earlier alarm ids when a reopened finding raised new alarm rows (§9.2).

The incident's `evidence` is **the L6 input bundle** (doc 28 §"guarded
explainer"): the root and member finding evidence by reference, plus topology
edges and context:

```json
{ "v": 1,
  "root": { "kind": "finding", "id": "…", "tier": "unit" },
  "members": [ { "id": "F1", "finding_id": "…" }, { "id": "A1", "alarm_id": "…" } ],
  "signals": [ { "id": "S1", "...": "summaries only, never raw series" } ],
  "topology": [ { "id": "T1", "from": "CHW-HDR-DC1", "to": "CRAH-A-03", "layer": "cooling" } ],
  "context": [ { "id": "C1", "kind": "maintenance_window", "ref": "…" },
               { "id": "C2", "kind": "setpoint_change", "device_id": "…", "at": "…" } ],
  "similar": [ { "id": "H1", "incident_id": "…", "similarity": 0.72 } ],
  "bundle_sha256": "…" }
```

### 7.5 `anomaly_incident.explanation`

```json
{ "v": 1,
  "explainer": "cloud", "model": "<DCIM_ANOMALY_EXPLAINER_MODEL>",
  "temperature": 0, "bundle_sha256": "…", "prompt_sha256": "…",
  "output": {
    "summary": "…",
    "hypotheses": [ { "text": "…", "confidence": 0.6,
                      "supporting": ["S1", "T1"], "contradicting": ["C2"] } ],
    "checks": ["…"], "unknowns": ["…"] },
  "validator": { "ok": true, "rejected_reason": null },
  "fallback_used": false,
  "explanation_sha256": "…",
  "generated_at": "2026-10-21T09:16:02Z" }
```

When the validator rejects the output, `fallback_used` is true and `output`
is the template summary from `analytics/evidence.py`. The full prompt and raw
model output go to `audit_log` (`action = 'anomaly.explain'`, `before = null`,
`after = {prompt, raw_output}`), which keeps the audit trail without a new table.

## 8. Data lifecycle

### 8.1 Finding states

| From | To | By | Effect |
|---|---|---|---|
| — | `open` | sweep, when the condition has held `dwell_s` | `INSERT … ON CONFLICT ON ux_anomaly_finding_open_key DO UPDATE` (touch). `first_seen` = onset and `raised_at` = now. Websocket `anomaly.finding.opened`. |
| `resolved` (within `reopen_window_s`) | `open` | sweep | Same row: `reopen_count + 1`, `resolved_at/resolution` cleared, `verdict` kept. Found through `ix_anomaly_finding_recent_resolved` and done under the sweep lock, so it cannot race a new insert. |
| `open` | `acknowledged` | feedback `acknowledge` | sets `acknowledged_at/by` |
| `open`, `acknowledged` | `confirmed` | feedback `confirm` | `verdict = 'confirmed'`. A `process` finding also writes an exclusion (§8.3). |
| `open`, `acknowledged` | `dismissed` | feedback `dismiss` | `verdict = 'dismissed'`. The row keeps occupying the dedupe key, so the sweep keeps touching `last_seen` silently and nothing re-raises, until the condition clears. |
| `acknowledged`, `confirmed`, `dismissed` | `open` | feedback `reopen` | `verdict` set to NULL |
| any non-resolved | `resolved` | sweep | `resolution`: `cleared` (false for `clear_dwell_s`), `device_retired` (lifecycle decommissioned/retired), `detector_disabled`, `muted` (override `enabled = false`), `superseded` (detector version change with incompatible state). `resolved_by = 'system'`. |
| any non-resolved | `resolved` | operator | `resolution = 'manual'` |

`verdict` is what makes precision computable. A dismissed finding that later
clears ends as `state = 'resolved', verdict = 'dismissed'`: a false positive
that went away by itself. "Stale" (open > 24 h with no feedback, doc 28
§advisories) is **derived in the query** (`state = 'open' AND first_seen <
now() - 24h AND feedback = '[]'`), not stored, so it can never drift.

A device under an active suppressing maintenance window (`0046:59`) is gated
out (`gate.exclude_maintenance`). No finding opens for it, so findings mirror
the raise-time shelving that `raise_alarm` applies to alarms
(`repositories/alarms.py:261-300`).

### 8.2 Dedupe key and chattering

The dedupe key is `(detector_id, device_id, metric_id, instance)` for
non-resolved rows. `instance` is normalised with
`alert_taxonomy.alarm_instance(metric_key, instance, alarm_type)`
(`alert_taxonomy.py:841-862`) before the write, so a device-scoped metric's
finding key and its alarm key agree. Flaps inside `reopen_window_s` add to
`reopen_count` on one row. More than 3 reopens in a window [judgement] holds
the finding open, and stops resolving and reopening it, until the condition
clears for a full `reopen_window_s`.

### 8.3 Exclusions written by the lifecycle

| Event | Exclusion written |
|---|---|
| Maintenance window becomes `active` (maintenance ticker, `worker.py:515-540`) | `source = 'maintenance'`, `source_ref = window id`, one row per target device, `ends_at` = window end (updated if edited) |
| Finding `confirmed` with `kind = 'process'` | `source = 'confirmed_finding'`, `source_ref = finding id`, device scope, `detector_id = NULL` (every detector on the device learns around it), `starts_at = first_seen`, `ends_at` = NULL until the finding resolves, then `resolved_at` |
| Simulator ledger import (doc 32) | `source = 'injected_fault'`, `source_ref = ledger id` |
| Operator (`POST /exclusions`) | `source = 'manual'` |

A dismissed finding writes no exclusion, because the data was normal. This is
the doc 28 rule that a fault must be "excluded from baselines, not just
silenced", or the next refit learns it as normal.

### 8.4 Feedback storage

`anomaly_finding.feedback` is append-only. Each element is
`{at, by, action: "acknowledge"|"dismiss"|"confirm"|"reopen", note?,
from_state, to_state, detector_version}`. `state`, `verdict` and
`acknowledged_*` are the current projection of that list. Each feedback is
also an `audit_log` row (`action = 'anomaly.finding.feedback'`,
`target_type = 'anomaly_finding'`, `target_id`). Per-detector precision is
`count(verdict = 'confirmed') / count(verdict IS NOT NULL)` grouped by
`(detector_id, detector_version)`, served by `ix_anomaly_finding_detector`.

### 8.5 Incident membership

L5 runs when a finding opens or changes (doc 28 layer table):

1. It walks the cooling dependency graph upstream to the nearest tier that has
   an open `process` finding or alarm within ±10 min (doc 28 §cascade). That
   gives `root_key`, `tier`, `root_finding_id` or `root_alarm_id`.
2. It upserts the incident on `ux_anomaly_incident_open_root`, sets
   `finding.incident_id` for member findings, and appends alarm ids to
   `member_alarm_ids`. `sensor` findings are never roots (doc 28 §sensor).
3. **Symptom marking on alarms** uses the existing columns
   `alarm.is_symptom` and `alarm.root_cause_alarm_id` (`0005:96-98`), but
   only when the root has an alarm row: a root `alarm`, or a root finding in
   the alert lane with `alarm_id`. `root_cause_alarm_id` is an FK to `alarm`
   and cannot point at an advisory finding. In that case the incident itself is
   the link, and the alarm list's grouping reads `ix_anomaly_incident_member_alarms`.
   **Only analytics-raised alarms (`alarm.source = 'analytics'`) are ever
   marked as symptoms.** Threshold, state, trap and staleness alarms may be
   incident members (in `member_alarm_ids`, or as `root_alarm_id`), but L5 never
   sets `is_symptom` or `root_cause_alarm_id` on them. The `UPDATE` carries
   `AND source = 'analytics'`. A hard L1 alarm stays a first-class alarm, never
   hidden by a statistical grouping, and `alarms/correlation.py:378-429` keeps
   sole ownership of symptom marking for the alarms it raises (doc 30).
4. The incident resolves when every member finding is resolved and every
   member alarm is CLEARED, after a 15 min hold-down [judgement]. A new
   symptom inside the hold-down rejoins the same row.

## 9. Alarm linkage

### 9.1 Which findings raise alarms

`lane = 'alert'` findings, meaning L2 detectors whose `default_lane = 'alert'`,
and advisory findings promoted on corroboration (doc 28 §"Promotion"). Each one
also raises an `alarm` row. Advisory findings never do.

### 9.2 Raise and clear, in the sweep's transaction

```
raise_alarm(session,
    device_id     = finding.device_id,
    alarm_type    = detector.alarm_type,             # one per detector, unique (§4.1)
    instance      = finding.instance,                # already alarm_instance()-normalised
    severity      = detector.severity,               # WARNING | MINOR
    message       = finding.summary,
    source        = "analytics",                     # -> detection 'derived' (alert_taxonomy.py:186)
    observed_at   = finding.raised_at,
    metric_key    = <primary numeric metric, or None; never alarm_state/equipment_state (D14)>,
    value         = finding.observed,
    threshold     = <the bound that was crossed>,
    category      = alert_taxonomy.BY_ALARM_TYPE[alarm_type],
    attributes    = {"anomaly": {"finding_id": ..., "detector_id": ..., "detector_version": ...,
                                 "expected": ..., "observed": ...}})   # new optional arg (D13)
-> finding.alarm_id = returned id
-> repo.refresh_device_alarm_state(session, [device_id])      # D12
-> integration_outbox.enqueue_actions(session, actions)       # same txn, outbox.py:54
-> after commit: fanout.alarm(kind, alarm)                    # fanout.py:99-111
```

This inherits, from existing code:

- dedup on `(device_id, alarm_type, instance) WHERE state <> 'CLEARED'` (`0005:105-107`);
- raise-time shelving for maintenance windows and `installed` devices (`repositories/alarms.py:261-300`);
- the response class from severity, so MINOR/WARNING become `alert` (`alert_taxonomy.py:285-305`);
- no Jira ticket by default, because policy requires `response_class` in the allowed list and severity ≥ MAJOR unless the type is allow-listed (`P:backend/app/integrations/policy.py:57-74`).

**Clear.** When the finding resolves, `clear_alarms(session, device_id=…,
alarm_types=[detector.alarm_type], instance=…, at=…, by="analytics")`
(`repositories/alarms.py:379-398`) runs, followed by outbox and fanout.
`finding.alarm_id` keeps pointing at the cleared alarm as history. If a
reopened finding raises again, a new alarm row is created (the old one is
CLEARED), `alarm_id` moves to it, and the old id is appended to
`evidence.alarm_history`.

**Promotion.** `lane` goes from `advisory` to `alert` and `promoted_at` is set.
The alarm is raised with the detector's `alarm_type`/`severity`, which advisory
L3 detectors carry for exactly this purpose. MAJOR is set only by L5 with
confirmed impact (D15).

**Never `raise_platform_alarm`.** Platform and discovery alarms reach neither
the websocket nor the outbox (`P:backend/app/api/v1/alarms.py:98-169`,
`repositories/alarms.py:840`).

### 9.3 alarm_type naming and taxonomy entries

Alarm types are snake_case **condition** names prefixed by device class,
matching the existing vocabulary (`cooling_degraded`, `chiller_flow_loss`,
`alert_taxonomy.py:526-534`). The detector ID is not used as the type: it is
not a condition name, and it lives in `alarm.attributes.anomaly.detector_id`.
None of the names below appears anywhere in `backend/app`, `backend/alembic`,
`backend/tests` or `contracts` (grep, 2026-10-21), so none can merge into an
existing threshold or trap alarm. Each needs:

1. a `BY_ALARM_TYPE` entry (`alert_taxonomy.py:328`), or `classify()` falls
   through to the fallback (`tests/test_alert_taxonomy.py:133-136`);
2. an entry in the test's `NON_TRAP_ALARM_TYPES` (`tests/test_alert_taxonomy.py:282`),
   or `test_no_unreachable_alarm_types` fails (`:341-353`).

| alarm_type | Detector | Category |
|---|---|---|
| `crah_supply_over_setpoint` | CR-1 | COOLING |
| `crah_low_delta_t` | CR-2 | COOLING |
| `crah_valve_hunting` | CR-6 | COOLING |
| `chiller_chw_over_setpoint` | CH-1 | COOLING |
| `chiller_short_cycling` | CH-4 | COOLING |
| `chiller_condenser_rising` | CH-5 | COOLING |
| `chiller_low_delta_t` | PL-1 (disabled) | COOLING |
| `cdu_facility_starved` | CD-1 | COOLING |
| `cdu_condensation_risk` | CD-6 | COOLING |
| `valve_position_mismatch` | VA-1 | COOLING |
| `valve_hunting` | VA-2 | COOLING |
| `pump_failed_to_prove` | PU-3 | COOLING |
| `tower_fan_saturated` | CT-2 (disabled) | COOLING |
| `tower_basin_imbalance` | CT-3 (disabled) | COOLING |
| `inlet_rate_of_change` | RM-2 | ENVIRONMENTAL |
| `rack_hot_spot` | RM-3 | ENVIRONMENTAL |
| `generator_fuel_loss` | GN-1 | POWER |
| `ats_transfer_rate` | AT-1 (disabled) | POWER |

COOLING and ENVIRONMENTAL make up the home "Cooling & Environment" strip
(`STRIP_GROUPS`, `alert_taxonomy.py:757`), so derived alerts appear there with
no UI work.

**L3 promotion types (Phase 3, 0114; taxonomy in doc 33 task 3.5).** These are
the `alarm_type` values advisory L3 detectors carry for promotion (§9.2). Each
needs both entries listed above, in the same PR. These names are confirmed for
docs 29 and 33, with one correction:

| alarm_type | Detector | Category | Note |
|---|---|---|---|
| `chiller_efficiency_degraded` | CH-2 | COOLING | as doc 33 |
| `chiller_approach_degraded` | CH-3 | COOLING | **Corrects doc 33's `chiller_condenser_fouling`.** CH-3 now scores `approach_temp{COND}` and `{EVAP}` (D10), so it can be evaporator fouling too. The instance names the side, and the type follows the `*_approach_degraded` pattern of CT-1/CD-2. |
| `tower_approach_degraded` | CT-1 | COOLING | as doc 33 |
| `pump_curve_degraded` | PU-1 | COOLING | as doc 33 |
| `pump_efficiency_degraded` | PU-2 | COOLING | as doc 33 |
| `crah_fan_creep` | CR-3 | COOLING | as doc 33 |
| `crah_filter_loading` | CR-4 | COOLING | as doc 33. No collision: the existing `cooling_filter_dirty` (`alert_taxonomy.py:531`) is the BACnet `Filter_Dirty` bit, a different condition and source. |
| `cdu_approach_degraded` | CD-2 | COOLING | as doc 33 |
| `cdu_filter_loading` | CD-3 | COOLING | as doc 33 |

The other L3 detectors (CT-4, PU-4, CD-4, CD-5, SV-1/2, PD-1/2, UP-1/2, TX-1,
DQ-6) carry **no** promotion type in 0114. They stay advisory-only, because
`test_no_unreachable_alarm_types` forbids taxonomy names that nothing raises.
They gain a type only when a corroboration path is designed for them. The L5
row raises no type of its own: it escalates the root's existing alarm. None of
the nine names appears in the repo today (grep, 2026-10-21).

## 10. Redis keys

The conventions are taken from existing code. Keys use the `dcim:` prefix
(`dcim:dwell`, `dcim:alarm-rules:version`, `dcim:bool:`, `dcim:ingest:heartbeat`;
`service.py:49,56`, `changelog.py:59`, `platform_monitor.py:35`). Hot per-series
state is one hash with a whole-hash TTL refreshed on every write (`service.py:49-50,146-154`).

The image is `redis:7-alpine` (`docker-compose.yml:42`), a floating tag, so
per-field TTL (`HEXPIRE`, Redis 7.4) cannot be assumed. Fields of series that
stop being scored are removed by the sweep (`HDEL` of any field not scored for
24 h).

| Key | Type | Value | TTL | Written by | Lost → |
|---|---|---|---|---|---|
| `dcim:anomaly:acc` | hash; field `{detector_id}\|{device_id}\|{metric_id}\|{instance}` | accumulator / dwell state (§7.2 shape, compact wire form like `DwellState.to_wire`) | 24 h, refreshed per write | sweep | L0/L2 dwell restarts (≤ 30 min re-wait, the same trade as `dcim:dwell`). L3/L4 restore from `anomaly_state` (§10.4). |
| `dcim:anomaly:win` | hash; field `{device_id}\|{metric_id}\|{instance}` | rolling window: last ≤ 32 `(ts, value, quality)` + read cursor `last_ts` | 24 h, refreshed per write | sweep | Next sweep backfills 30 min from `telemetry_sample` (one-off full read, §6.2) |
| `dcim:anomaly:sweep` | string (JSON) | heartbeat: `{consumer, started_at, finished_at, duration_ms, series_scored, findings_opened, findings_resolved, alerts_raised, error}` | 10 × `DCIM_ANOMALY_SWEEP_S` (600 s) | lock holder, after each sweep | `/health` reports "no sweep in 10 min". Also the **cadence gate**, §10.1. |
| `dcim:anomaly:sweep:log` | list (JSON, capped) | one entry per timer firing on either replica: `{consumer, at, outcome, duration_ms}`, outcome one of `ran`, `cadence`, `lock_held`, `error` | 2 h | every firing, both replicas | `/health`'s `runs_1h`, `gate_skips_1h`, `errors_1h`, `p95_ms_1h` across replicas (neither the heartbeat nor per-process Prometheus can give these). Added during the Phase 0 build. |
| `dcim:anomaly:baseline` | string (JSON) | last fit: `{consumer, fit_date, started_at, finished_at, series_fitted, rejected, pruned_rows}` | 48 h | baseline holder | `/health` only. The once-a-day guard is in Postgres (§10.1). |
| `dcim:anomaly:catalogue:version` | string (counter) | `INCR` on detector PATCH, override PUT/DELETE, exclusion POST/DELETE | none | API | The sweep reloads on its 60 s refresh anyway, the same as `RULES_VERSION_KEY` (`service.py:51-60`) |
| `dcim:anomaly:explain:{incident_id}` | string, `SET NX` | `{consumer, started_at}` (the **explain lock**) | `DCIM_ANOMALY_EXPLAINER_TIMEOUT_S` + 30 s | `POST /incidents/{id}/explain` and on-open explain | A second concurrent explain of one incident gets 409 (doc 30) instead of paying twice |
| `dcim:anomaly:bundle:{incident_id}:{last_activity_epoch}` | string (JSON) | **evidence-bundle cache**: the built §7.4 bundle | 600 s | `analytics/evidence.py` | Rebuilt on demand. The epoch in the key means any incident change misses the cache by construction, with no invalidation code. |

Memory, at the §6.1 counts: `acc` ≈ (2,330 + 488 + 1,404) fields × ~300 B
≈ 1.3 MB. `win` ≈ 2,330 × 32 × ~20 B ≈ 1.5 MB. Bundles are ≈ 10/day × 6 KB,
which is negligible.

### 10.1 Locks and the cadence gate

| Constant (in code, not env) | Value | Existing neighbours |
|---|---|---|
| `ANOMALY_SWEEP_LOCK_KEY` | `0x44_43_49_4D_41` ("DCIMA") | `assigner.ADVISORY_LOCK_KEY = 0x44_43_49_4D_35` "DCIM5" (`P:backend/app/services/assigner.py:53`); `rollout.ADVISORY_LOCK_KEY = 0x44_43_49_4D_37` "DCIM7" (`services/rollout.py:48`); `collector_visibility._LOCK_KEY = 0x636F6C76` "colv" (`alarms/collector_visibility.py:45`) |
| `ANOMALY_BASELINE_LOCK_KEY` | `0x44_43_49_4D_42` ("DCIMB") | grep for `pg_try_advisory` / `0x44_43` finds no other users |

- **Sweep:** `pg_try_advisory_xact_lock(ANOMALY_SWEEP_LOCK_KEY)` inside the
  sweep's own `unit_of_work()` (`P:backend/app/db/session.py:54`), exactly as
  `assigner.run` does (`assigner.py:133-137`). The lock releases at commit.
  A sweep is ~3.5k row reads and a few writes, so the transaction lasts
  seconds.
- **Cadence gate.** The two ingest replicas (`docker-compose.yml:113`) run
  unaligned monotonic timers (`worker.py:164-174`). If each one took the lock
  whenever its own timer fired, replica B would sweep 30 s after A, which
  doubles the work and halves every dwell. So the lock holder first reads
  `dcim:anomaly:sweep.finished_at` and skips if it is less than
  0.9 × `DCIM_ANOMALY_SWEEP_S` old. If the key is missing it sweeps, which
  is safe because the lock still serialises.
- **Baseline:** this is a session-level `pg_try_advisory_lock` on a dedicated
  connection with `pg_advisory_unlock` in `finally`, not the xact variant. The
  fit spans many short per-batch transactions, and holding one transaction for
  minutes would pin the vacuum horizon on a host that already swaps under
  Timescale jobs [judgement]. A worker death closes the connection and releases
  the lock. The once-per-UTC-day guard is in Postgres:
  `SELECT max(fitted_at) FROM anomaly_baseline` ≥ today at
  `DCIM_ANOMALY_BASELINE_HOUR_UTC` means skip. Postgres is used because a Redis
  flush must not trigger a second full fit.

### 10.2 What is checkpointed

| State | Store of record | Why |
|---|---|---|
| L0 rolling windows, L2 dwell/clear timers | Redis only | Rebuildable from ≤ 30 min of `telemetry_sample`. Same reasoning as `dcim:dwell` (24 h TTL, `service.py:50`). |
| L3/L4 accumulators (EWMA, CUSUM±, n, gate timers ≥ 1 h) | Redis hot, **`anomaly_state` checkpoint** | Hours of accumulated evidence. A restart that erases it erases the drift it was tracking (doc 28 §code). |

### 10.3 Checkpoint rule

1. **Periodic:** every `CHECKPOINT_EVERY_SWEEPS = 5` sweeps (300 s at the
   default), upsert every *dirty* L3/L4 series in one statement
   (`INSERT … SELECT FROM unnest(...) ON CONFLICT (pk) DO UPDATE … WHERE
   EXCLUDED.updated_at > anomaly_state.updated_at`). The `WHERE` means an older
   copy never overwrites a newer one.
2. **Event:** in the same transaction as any finding open, escalate or resolve
   on that series, so the DB finding and its accumulator always agree.
3. **Shutdown:** a best-effort flush on SIGTERM.

Worst-case loss is 300 s of accumulation if Redis and the worker die
together, which is ≤ 2–8% of an hours-scale CUSUM.

### 10.4 Restore rule

On a Redis miss, load from `anomaly_state`. **Reset rather than resume**, and
set `state.reset`, if any of these holds:

- `updated_at < now() - MAX_RESTORE_AGE_S` (6 h [judgement]: a CUSUM must not integrate across a hole);
- `detector_version` ≠ the detector's current version;
- `baseline_id` ≠ the current baseline (residuals against a different model are not additive).

When Redis holds a newer `last_ts` than the DB row, Redis wins.

## 11. Settings and environment

Settings are read by `Settings` in `P:backend/app/core/config.py` (pydantic
`BaseSettings`, `env_prefix="DCIM_"`, `config.py:18-24`; `get_settings()`
cached, `:201-203`). Add:

```python
# --- anomaly detection (docs/28, docs/29) --------------------------------
#: Master switch. Off by default: the sweep and the nightly fit return
#: immediately, and the API serves the catalogue read-only.
anomaly_enabled: bool = False
#: L2 cadence. L3/L4 run every Nth sweep per anomaly_detector.cadence_s.
anomaly_sweep_s: int = Field(60, ge=30, le=600)
#: UTC hour the nightly baseline fit and prune run (after the 1d rollup's
#: refresh; 0042:56-62 refreshes hourly with a 1-day end offset).
anomaly_baseline_hour_utc: int = Field(3, ge=0, le=23)
#: L6 explainer. 'none' leaves the system fully functional (doc 28).
anomaly_explainer: Literal["none", "cloud", "local"] = "none"
anomaly_explainer_model: str = ""
anomaly_explainer_url: str = ""
#: Contract deviation D2, per doc 30.
anomaly_explainer_api_key: SecretStr | None = None
anomaly_explainer_timeout_s: int = Field(30, ge=5, le=120)
```

| Env var | Default | Notes |
|---|---|---|
| `DCIM_ANOMALY_ENABLED` | `false` | |
| `DCIM_ANOMALY_SWEEP_S` | `60` | The cadence-gate threshold and the heartbeat TTL derive from it |
| `DCIM_ANOMALY_BASELINE_HOUR_UTC` | `3` | |
| `DCIM_ANOMALY_EXPLAINER` | `none` | A model validator refuses `cloud` without `API_KEY` and `local` without `URL` |
| `DCIM_ANOMALY_EXPLAINER_MODEL` | `""` | |
| `DCIM_ANOMALY_EXPLAINER_URL` | `""` | |
| `DCIM_ANOMALY_EXPLAINER_API_KEY` | unset | **Added (D2).** A SecretStr, never logged |
| `DCIM_ANOMALY_EXPLAINER_TIMEOUT_S` | `30` | **Added (D2).** Also sets the explain-lock TTL |

Tunables that belong in code as module constants, like `STALENESS_SWEEP_S`
(`worker.py:95`), and not in env: `CHECKPOINT_EVERY_SWEEPS = 5`,
`ACC_TTL_S = 86_400`, `MAX_RESTORE_AGE_S = 21_600`, `PRUNE_BATCH = 5_000`,
`BUNDLE_CACHE_TTL_S = 600`, and the two lock keys.

## 12. Seed rows

All `params` values are **starting values to tune** with the doc 28 harness.
Doc 33 owns the tuning. The ones cited to code come from the existing display
verdicts, so the display and the alert cannot disagree (doc 28 §code). Every
row has `version = 1`. `ON CONFLICT (id) DO NOTHING` means a re-run never
clobbers an operator's edit.

### 12.1 Migration 0112: Phase 1 (L0 + L2)

**L0** rows: `layer = 0`, `phase = 1`, `default_lane = advisory`,
`default_kind = sensor`, `primary_metric = '*'`, `cadence_s = 60`, no
alarm_type. Device types are {crah, chiller, cooling_tower, pump, cdu, valve,
sensor, pdu, ups, transformer, generator, server}.

| ID | Check | Default params | Enabled |
|---|---|---|---|
| DQ-1 | Series dropout: no sample within grace while the endpoint still delivers other series (endpoint-level silence is already `telemetry_stale`) | `grace_intervals: 3, min_grace_s: 300, max_grace_s: 10800` (`P:backend/app/alarms/staleness.py:36-56`) | yes |
| DQ-2 | Non-GOOD run (ingest stores out-of-range as `suspect`, `worker.py:778`) | `window_s: 1800, min_bad_fraction: 0.5, min_samples: 5` | yes |
| DQ-3 | Physical range and sentinels | `bounds: {supply_air_temp:[5,40], return_air_temp:[10,55], inlet_temperature:[5,50], ambient_temperature:[0,50], water_supply_temp:[1,60], relative_humidity:[0.5,99.5]}`, `sentinels: [-40.0, -127.0, 85.0, 327.67, -327.68]`, `dwell_samples: 3` | yes |
| DQ-4 | Spike (Hampel). A single spike is masked, not reported; a finding opens only on frequent spikes | `window: 11, k_mad: 5.0, min_mad_abs: {temperature: 0.1, pct: 0.5}, max_spikes_per_h: 6` | yes |
| DQ-5 | Flatline while a coupled variable moves | `min_samples: 20, coupled: {supply_air_temp: [["valve_position_pct","CHW"],["fan_speed_pct",""]], water_supply_temp: [["compressor_load_pct",""]], inlet_temperature: [["cpu_utilization",""]]}, coupled_min_range: {pct: 5.0, temperature: 0.5}` | yes |

The DQ-3 sentinels encode real failure values [judgement]. −40 is a common
open-thermistor reading on BMS analog inputs. −127 and 85 are DS18B20
disconnect and power-on values. ±327.67 is an int16/100 Modbus overflow.
DQ-5 exists for the simulator trap that file-backed SNMP points never move
(memory "SNMP values are file-backed"; doc 28 §sensor): a point is flagged only
when its coupled variable moves.

**L2** rows: `layer = 2`, `phase = 1`, `cadence_s = 60`, `default_kind = process`.

| ID | Device types | Primary metric | Required metrics | Lane | alarm_type | Sev | Dwell / clear | Key params | Enabled |
|---|---|---|---|---|---|---|---|---|---|
| CR-1 | crah | `supply_air_temp` | `air_setpoint_temp`, `valve_position_pct{CHW}`, `equipment_state{Unit_Running}` | alert | `crah_supply_over_setpoint` | MINOR | 900 / 300 | `margin_k 3.0` (`services/thermal.py:52`), `clear_margin_k 2.0`, `valve_starved_pct 95`, `running_min_s 600` | yes |
| CR-2 | crah | `return_air_temp` | `supply_air_temp`, `fan_speed_pct`, `equipment_state{Unit_Running}` | alert | `crah_low_delta_t` | MINOR | 900 / 600 | `min_delta_k 4.0`, `clear_delta_k 5.0`, `min_fan_pct 30`, `running_min_s 600` | yes |
| CR-6 | crah | `valve_position_pct` (CHW) | `equipment_state{Unit_Running}` | alert | `crah_valve_hunting` | WARNING | window 1800 / 1800 | `min_reversals 6`, `min_step_pct 5` | yes |
| CH-1 | chiller | `water_supply_temp` (CHW) | `water_setpoint_temp{CHW}`, `compressor_load_pct`, `equipment_state{Chiller_Running}` | alert | `chiller_chw_over_setpoint` | MINOR | 900 / 300 | `margin_k 1.0`, `clear_margin_k 0.5`, `max_load_pct 95`, `running_min_s 900`, `restart_exclusion_s 480` | yes |
| CH-4 | chiller | `equipment_state` (Chiller_Running) | — | alert | `chiller_short_cycling` | MINOR | immediate / 3600 | `window_s 3600`, `max_starts 3`, `lookback_s 1800` (last-known logic, `P:backend/app/ingest/changelog.py:48,52`); raise with `metric_key = None` (D14) | yes |
| CH-5 | chiller | `water_return_temp` (COND) | `water_pressure{COND}`, `equipment_state{Chiller_Running}` | alert | `chiller_condenser_rising` | MINOR | 2 samples / 600 | `rise_k_per_min 0.3`, `trip_c 42.0`, `horizon_s 600` | yes |
| CD-1 | cdu | `water_supply_temp` ('') | `valve_position_pct{FACILITY_CHW}`, `water_setpoint_temp`, `equipment_state{Unit_Running}` | alert | `cdu_facility_starved` | MINOR | 300 / 300 | `valve_pct 95`, `over_setpoint_k 2.0` (= `CDU_SUPPLY_FAULT_MARGIN_K`, `services/thermal.py:177`; D18 resolves doc 33 Q12) | yes |
| CD-6 | cdu | `water_supply_temp` ('') | room sensors' `ambient_temperature`, `relative_humidity` | alert | `cdu_condensation_risk` | MINOR | 600 / 600 | `min_margin_k 2.0`, `clear_margin_k 3.0`, `dew_source "room_max"` (`core/ashrae.py:120` `dew_point_c`) | yes |
| VA-1 | valve (D7) | `valve_position_pct` (MEASURED) | `valve_position_pct{COMMANDED}` | alert | `valve_position_mismatch` | MINOR | 600 / 300 | `deviation_pct 10` (`services/plant.py:54`), `clear_pct 5`, `settle_s 120` | yes |
| VA-2 | valve | `valve_position_pct` (MEASURED) | — | alert | `valve_hunting` | WARNING | window 1800 / 1800 | `min_reversals 6`, `min_step_pct 5` | yes |
| PU-3 | pump (D8) | `water_flow` | `equipment_state{Run_Status}`, `pump_speed_pct` | alert | `pump_failed_to_prove` | MINOR | 2 samples (60 s) / 3 samples | `min_speed_pct 10`, `min_flow_fraction 0.05`, `prove_s 60`; raise with `metric_key = water_flow` (D14) | yes |
| RM-2 | sensor, pdu | `*` over `inlet_temperature`, `ambient_temperature` | — | alert | `inlet_rate_of_change` | MINOR | 3 samples / 900 | `max_rate_k_per_h 20.0` (`core/ashrae.py:68`), `window_s 900` | yes |
| RM-3 | sensor, pdu | `inlet_temperature` | room peers' inlet | alert | `rack_hot_spot` | MINOR | 900 / 600 | `margin_k 3.0` over room p90 (`services/thermal.py:44,48`), `clear_margin_k 2.0`, `min_room_sensors 5` | yes |
| GN-1 | generator | `fuel_level_pct` | `equipment_state{Engine_Running}` (`P:contracts/mappings/modbus/templates.yaml:58`) | alert | `generator_fuel_loss` | MINOR | 1800 / 1800 | `min_drop_pct 1.0` per `window_s 3600` while not running, `not_running_min_s 1800`, `ignore_rises true` | yes |
| CT-2 | cooling_tower | `fan_speed_pct` | `water_supply_temp{COND}`, `outdoor_wet_bulb_temp`, `equipment_state{Fan_Status}` | alert | `tower_fan_saturated` | MINOR | 1200 / 600 | `fan_pct 98`, `approach_rise_k_per_h 0.5` | **no** (not in doc 28 Phase 1 list) |
| CT-3 | cooling_tower | `basin_level_pct` | `makeup_water_flow` | alert | `tower_basin_imbalance` | MINOR | 1800 / 1800 | `fall_pct_per_h 2.0` while make-up > 0 | **no** |
| AT-1 | ats | `transfer_count` | — | alert | `ats_transfer_rate` | MINOR | immediate / 86400 | `max_transfers_24h 2` | **no** |
| PL-1 | chiller (D11) | `water_return_temp` (CHW) | `water_supply_temp{CHW}`, `water_flow{CHW}` | alert | `chiller_low_delta_t` | MINOR | 1800 / 900 | `design_delta_k 5.5`, `min_fraction 0.6`, `flow_rising true` | **no** |

**L5 switch row** (D17): `id = 'L5'`, `layer = 5`, `phase = 2`,
`device_types = '{}'`, `primary_metric = '*'`, `default_lane = advisory`,
`default_kind = process`, `cadence_s = 60`, no `alarm_type`/`severity`,
`enabled = true`. Seeding it in 0112 means Phase 2 needs no migration, and the
global `DCIM_ANOMALY_ENABLED = false` still holds everything off.

```json
{ "v": 1,
  "window_s": 600,            // ±10 min upstream association (doc 28 §cascade)
  "hold_down_s": 900,         // incident resolve hold-down (§8.5)
  "major_enabled": false,     // MAJOR escalation of the root's alarm; on only after the P2 gate
  "major_min_racks": 3,       // confirmed impact: >= N racks with RM-2/RM-3 under one root
  "use_influence": false }    // Phase 4 zone roots; false = room grouping
```

Realism notes on the defaults [judgement unless cited]:

- **CR-2.** A Liebert PCW is designed around a return–supply difference of
  roughly 10–12 K, so 4 K is a loud floor, not a target.
- **CR-6 / VA-2.** At 30 s BACnet polling, the shortest observable period is
  about 60 s. Real hunting runs at 1–5 min periods, so it is visible. It is not
  visible on 120 s SNMP points (doc 28 §cadence).
- **CH-4.** The limit varies by maker. Centrifugal machines typically enforce
  a start-to-start interval in the controller (often quoted as 15–30 min), but
  this was not checked against the Carrier 19DV manual.
- **CH-5.** A real chiller trips on refrigerant head pressure. `trip_c 42.0`
  is the simulator's condenser trip point (inventory Q6) and must become a
  per-site parameter.
- **GN-1.** Fuel-level readings move with temperature, and polishing systems
  shuttle fuel. That is why the dwell is long and rises are ignored.

### 12.2 Migration 0114: Phase 3 (L3)

`layer = 3`, `phase = 3`, `cadence_s = 300`, `default_lane = advisory`,
`default_kind = undetermined`. Params are doc 33's. The shape is §7.1 (L3).

| ID | Device types | Model kind | Enabled at seed | Blocker |
|---|---|---|---|---|
| CH-2 | chiller | ols (cop ~ load, wet-bulb) | yes | — |
| CH-3 | chiller | ols (`approach_temp{COND}`, `{EVAP}` ~ load) | yes, if doc 33 task 1.22 shipped | D10 resolved: new simulator points, mapped in Phase 1 |
| CT-1 | cooling_tower | ols (approach ~ load, wet-bulb) | yes | — |
| CT-4 | cooling_tower | profile_how (vibration) | yes | — |
| PU-1 | pump | curve | yes | — |
| PU-2 | pump | ols (power ~ speed³) | yes | — |
| PU-4 | pump | ols (motor_temp ~ power) | yes | — |
| CR-3 | crah | ols (fan ~ room load) | yes | — |
| CR-4 | crah | ols slope (`filter_diff_pressure`) | yes, if doc 33 task 1.22 shipped | D9 resolved: new CRAH `Filter_DP`, mapped in Phase 1 |
| CD-2 | cdu | ols (approach ~ thermal_load) | yes | — |
| CD-3 | cdu | ols slope (filter ΔP) | yes | — |
| CD-4 | cdu | ols (speed ~ flow) | yes | — |
| CD-5 | cdu | ols slope (loop pressure) | **no** | D19 / doc 33 Q17: no truth until S4 `loop_leak`. The interim `cdu-slow-leak` ramp tests plumbing only. |
| SV-1 | server | ols (cpu_temp ~ cpu%, inlet) | **no** | D19 / Q17: until S4 `server_thermal` |
| SV-2 | server | ols (fan ~ inlet) | yes | — |
| PD-1 | pdu, rpp, energy_monitor | ols (power ~ cpu) | yes | — |
| PD-2 | pdu, rpp, energy_monitor | profile_how (phase imbalance) | yes | — |
| UP-1 | ups | ols (battery temp ~ room ambient) | **no** | D19 / Q17: until S4 `room_hvac_loss` |
| UP-2 | ups | ols (runtime ~ load) | yes | — |
| TX-1 | transformer | first_order (oil τ ≈ 3 h) | **no** | D19 / Q17: until S4 `xfmr_cooling` |
| DQ-6 | sensor, pdu | peer + balance residual | yes | `layer = 0`, `default_kind = sensor` |

Each detector that gets a promotion `alarm_type` needs the §9.3 taxonomy
entries in the same PR. Those are CH-2, CH-3, CT-1, PU-1, PU-2, CR-3, CR-4,
CD-2 and CD-3 (§9.3 L3 table). The CR-4/CH-3 condition is decided when 0114 is
written: if the task 1.22 mapping rows are live and the points have history,
seed `true`, otherwise `false` and enable them later by PATCH. Disabled rows
here still get their `params` seeded, so turning one on is a PATCH, not a
migration.

### 12.3 Migration 0115: Phase 4 (L4)

`layer = 4`, `phase = 4`, `cadence_s = 300`, advisory.

| ID | Device types | Model kind | Notes |
|---|---|---|---|
| CR-5 | crah | peer (robust z over the hall's siblings) | 30 min dwell |
| RM-4 | sensor, pdu | peer (aisle residual); also owns the room-scoped `influence` baseline | needs `DCIM_SIM_AIR_MODEL=spatial` and 4+ weeks of history (doc 28) |
| SV-3 | cdu | peer (fraction of cold-plate servers on the loop with SV-1/SV-2 rising) | Anchored to the CDU so `device_id` stays NOT NULL. The room-wide variant is L5 evidence, not a finding (open item for doc 33). |

## 13. Migration plan

File names follow the repo's sentence style. Each migration is one deploy,
matched to one phase. Check Redis `XINFO GROUPS` lag before and after each
deploy (policy; memory "batch deploys on the tight host").

| Migration | File | Phase | Contents |
|---|---|---|---|
| `0111` | `0111_an_anomaly_is_a_finding_not_an_alarm.py` | 0 | the seven tables, empty |
| `0112` | `0112_the_rules_that_need_no_history.py` | 1 | DQ-1..5 + L2 rows + the `L5` switch row (§12.1) |
| `0113` | `0113_a_spike_is_judged_against_the_reading.py` | 1 | Live tuning after the first sweep: DQ-4 `min_dev_rel` 0.02 (spread floor as a share of the reading; power, flow and pressure unit families), VA-2 and CR-6 `min_step_pct` 5 → 10 |
| `0114` | `0114_a_residual_needs_a_baseline.py` | 3 | L3 rows + DQ-6 (§12.2) |
| `0115` | `0115_a_unit_is_judged_against_its_siblings.py` | 4 | L4 rows (§12.3) |

Phase 2 (L5) and Phase 5 (L6) need no migration: the `L5` row is seeded by 0112, and `anomaly_incident`,
`explanation` and `explanation_feedback` already exist from 0111.

### 0111

Upgrade:

1. `SET LOCAL lock_timeout = '5s'`. Creating an FK takes a SHARE ROW EXCLUSIVE
   lock on the referenced table (`device`, `alarm`, `metric`, `room`,
   `datacenter`), and `alarm` is written by ingest continuously. On empty new
   tables the lock is held for milliseconds, but acquiring it queues behind
   ingest transactions and blocks them while it waits. Failing fast and
   retrying beats a stalled ingest.
2. Create, in this order: `anomaly_detector` (§4.1), `anomaly_override`
   (§4.2), `anomaly_baseline_exclusion` (§4.3), `anomaly_baseline` (§4.4),
   `anomaly_state` (§4.5), `anomaly_incident` (§4.6, without the root
   finding FK), `anomaly_finding` (§4.7), and their indexes.
3. `ALTER TABLE anomaly_incident ADD CONSTRAINT fk_anomaly_incident_root_finding_id_anomaly_finding …` (§4.8).

Downgrade:

1. `ALTER TABLE anomaly_incident DROP CONSTRAINT IF EXISTS fk_anomaly_incident_root_finding_id_anomaly_finding`.
2. `DROP TABLE IF EXISTS` on `anomaly_finding`, `anomaly_incident`,
   `anomaly_state`, `anomaly_baseline`, `anomaly_baseline_exclusion`,
   `anomaly_override`, `anomaly_detector`, in that order. Indexes and CHECKs
   go with their tables, so no constraint is dropped by name.
3. No Timescale jobs exist to remove (§3).
4. Redis keys under `dcim:anomaly:*` are left to expire (≤ 48 h), or removed by
   hand with `--scan --pattern 'dcim:anomaly:*'`. A migration does not touch Redis.

Derived alarms already raised stay in `alarm` with `source = 'analytics'`.
They are ordinary alarms and outlive the downgrade.

### 0112

Upgrade: one `INSERT INTO anomaly_detector (...) VALUES (...), ... ON CONFLICT
(id) DO NOTHING` with the §12.1 rows. `params` is written as a JSON literal
cast `::jsonb`.

Downgrade:

1. `DELETE FROM anomaly_finding WHERE detector_id IN (...)`. This is required
   because the FK is RESTRICT. The data loss is deliberate and documented in
   the docstring, as `0109:32-34` does for device actions.
2. `DELETE FROM anomaly_detector WHERE id IN (...)`. Overrides, state,
   baselines and detector-scoped exclusions cascade.
3. Derived alarms for these types are not cleared by the migration. An operator
   clears them, or they remain as history.

### 0114 / 0115

Same pattern as 0112, with the §12.2 / §12.3 IDs. The 0114 downgrade also
deletes the baselines of its detectors (by cascade). 0115's `influence`
baseline rows go with RM-4.

## 14. Acceptance

The schema is accepted when all of these hold:

- [ ] `alembic upgrade head` from 0110 creates exactly seven `anomaly_*` tables, and `alembic downgrade 0110` removes them all with no leftover constraint, index or job (`SELECT * FROM timescaledb_information.jobs` unchanged).
- [ ] `alembic downgrade base` / `upgrade head` round-trips in the CI Migrations job (memory: only CI exercises downgrade).
- [ ] `SELECT conname FROM pg_constraint WHERE conrelid = 'anomaly_finding'::regclass` lists `ck_anomaly_finding_*` names exactly as written, with no double prefix.
- [ ] No `anomaly_*` table is a hypertable (`timescaledb_information.hypertables` has none).
- [ ] Inserting a second non-resolved finding with the same `(detector_id, device_id, metric_id, instance)` fails on `ux_anomaly_finding_open_key`. The same insert succeeds when the first is `resolved`.
- [ ] A finding with `state = 'dismissed', verdict = NULL` is rejected by `ck_anomaly_finding_verdict_state`. A `resolved` row without `resolution` is rejected.
- [ ] A detector row with `default_lane = 'alert'` and no `alarm_type`, or with `severity = 'MAJOR'`, is rejected.
- [ ] Two detectors with the same `alarm_type` are rejected by `ux_anomaly_detector_alarm_type`.
- [ ] Deleting a device cascades its findings, state, baselines, overrides and exclusions. Deleting an alarm sets `anomaly_finding.alarm_id` to NULL. Deleting a detector that has findings fails.
- [ ] Two `is_current` baselines for one series are rejected. A `rejected` baseline cannot be current.
- [ ] `EXPLAIN` of the default `GET /findings`, `?device_id=`, `?detector=`, `?room=`, `?since=`, the reopen lookup and `GET /expected` each uses the index named for it in §4.
- [ ] A sweep touch of an open finding (only `last_seen`, `occurrences`, `score` changed) is a HOT update (`pg_stat_user_tables.n_tup_hot_upd` increments).
- [ ] 0112 seeds DQ-1..DQ-5, the 18 L2 rows of §12.1 (CT-2, CT-3, AT-1 and PL-1 disabled) and the `L5` switch row with `major_enabled = false`. Re-running 0112 after an operator PATCH leaves the operator's params intact.
- [ ] A row with `layer = 5` and an id other than `L5`, or `id = 'L5'` with another layer, is rejected by `ck_anomaly_detector_l5`.
- [ ] CD-1's seeded `over_setpoint_k` equals `thermal.CDU_SUPPLY_FAULT_MARGIN_K` (2.0), checked by `test_anomaly_rule_parity`.
- [ ] 0114 seeds CD-5, SV-1, UP-1 and TX-1 with `enabled = false`, and CR-4/CH-3 per the task 1.22 condition.
- [ ] The nine L3 promotion types in §9.3 are in both taxonomy files after Phase 3, and no other L3 type is.
- [ ] Every `alarm_type` in §9.3 is in `BY_ALARM_TYPE` and in `NON_TRAP_ALARM_TYPES`, and `pytest backend/tests/test_alert_taxonomy.py` passes.
- [ ] A derived alarm raised by CH-4 or PU-3 has `detection = 'derived'`, not `state` (D14).
- [ ] With two ingest replicas, exactly one sweep runs per `DCIM_ANOMALY_SWEEP_S`, and the `dcim:anomaly:sweep` heartbeat shows a single consumer per interval (cadence gate, §10.1).
- [ ] Killing the worker and flushing `dcim:anomaly:acc` resumes L3 accumulators from `anomaly_state` with ≤ 300 s of loss. A checkpoint older than 6 h resets, with `state.reset` recorded.
- [ ] A second `POST /incidents/{id}/explain` while one runs is refused through `dcim:anomaly:explain:{id}`.
- [ ] Each `anomaly_baseline.model` carries `resid_sigma` for `5m`, `1h` and `1d`.
- [ ] With `DCIM_ANOMALY_ENABLED=false` the worker does no anomaly reads or writes. With `DCIM_ANOMALY_EXPLAINER=cloud` and no API key, startup refuses with a clear error.
- [ ] Redis `XINFO GROUPS` lag for `dcim-ingest` is not worse after the 0111/0112 deploy than before it.
- [ ] After a 7-day soak, total `anomaly_*` size (`pg_total_relation_size`) is within 2× of the §6 projection pro-rated to 7 days.
