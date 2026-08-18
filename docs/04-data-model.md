# 04 — Data Model

PostgreSQL 16 + TimescaleDB. One database, three logical zones:

- **Inventory** — slowly changing, the source of truth for what exists and where.
- **Operational state** — small, hot, mutable. Current status only.
- **History** — hypertables. Append-only. Never queried to answer "is it up?".

---

## 1. ER overview

```
                         ┌────────────┐
                         │ datacenter │
                         └─────┬──────┘
                               │ 1:N
                         ┌─────▼──────┐
                         │    room    │
                         └─────┬──────┘
                               │ 1:N
                         ┌─────▼──────┐
                         │    row     │
                         └─────┬──────┘
                               │ 1:N
                         ┌─────▼──────┐        ┌──────────────┐
                         │    rack    │───N:1──│ rack_model   │
                         └─────┬──────┘        └──────────────┘
                               │ 1:N (rack_position: rack_id, u_start, u_height, facing)
                               │
    ┌──────────┐         ┌─────▼──────┐        ┌──────────────┐       ┌──────────┐
    │ vendor   │──1:N───▶│   model    │◀──N:1──│   device     │──N:1─▶│device_type│
    └──────────┘         └────────────┘        └──┬──┬──┬──┬──┘       └──────────┘
                                                  │  │  │  │
             ┌────────────────────────────────────┘  │  │  └──────────────────────┐
             │                     ┌─────────────────┘  └────────┐                │
     ┌───────▼────────┐    ┌───────▼────────┐          ┌─────────▼──────┐  ┌──────▼───────┐
     │  interface     │    │    outlet      │          │  power_supply  │  │device_endpoint│
     └───────┬────────┘    └───────┬────────┘          └────────┬───────┘  └──────┬───────┘
             │                     │                            │                 │ N:1
             └──────────┬──────────┴────────────────────────────┘          ┌──────▼───────┐
                        │  (typed terminations)                            │  credential  │
                  ┌─────▼──────┐                                           └──────────────┘
                  │ connection │  layer ∈ {production, management, power, cooling, fieldbus}
                  └────────────┘

    ┌──────────────┐   ┌──────────────┐   ┌──────────┐   ┌────────┐   ┌──────────────────┐
    │ device_state │   │endpoint_state│   │  alarm   │   │ event  │   │collector_instance│
    └──────────────┘   └──────────────┘   └────┬─────┘   └────────┘   └──────────────────┘
                                               │ self-ref: root_cause_alarm_id
                                          ┌────▼──────┐
                                          │alarm_rule │
                                          └───────────┘

    HYPERTABLES:  telemetry_sample · telemetry_bool · telemetry_text · alarm_history · poll_result
```

---

## 2. Enumerations

Defined as PostgreSQL enums where the value set is stable, and as lookup tables
where operators may extend them.

```sql
CREATE TYPE protocol_t        AS ENUM ('snmp','snmp_trap','gnmi','bacnet','redfish','modbus','sflow','manual');
CREATE TYPE endpoint_role_t   AS ENUM ('os_agent','bmc','native_card','field_device','gateway','router');
CREATE TYPE comm_status_t     AS ENUM ('ONLINE','DEGRADED','OFFLINE','UNKNOWN','DISABLED');
CREATE TYPE health_t          AS ENUM ('OK','WARNING','CRITICAL','UNKNOWN');
CREATE TYPE severity_t        AS ENUM ('CLEAR','INFO','WARNING','MINOR','MAJOR','CRITICAL');
CREATE TYPE alarm_state_t     AS ENUM ('ACTIVE','ACKNOWLEDGED','CLEARED');
CREATE TYPE layer_t           AS ENUM ('production','management','power','cooling','fieldbus');
CREATE TYPE termination_t     AS ENUM ('interface','outlet','psu','none');
CREATE TYPE value_type_t      AS ENUM ('gauge','counter','delta','bool','text');
CREATE TYPE quality_t         AS ENUM ('good','stale','suspect','bad','no_data');
CREATE TYPE admin_state_t     AS ENUM ('enabled','disabled','maintenance');
```

`severity_t` ordering matters: it is used for `MAX(severity)` rollups on racks
and rooms. Postgres enums order by declaration, so the declaration above is the
precedence.

---

## 3. Inventory DDL

### 3.1 Physical hierarchy

```sql
CREATE TABLE datacenter (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code          text NOT NULL UNIQUE,          -- 'DC1'
    name          text NOT NULL,
    city          text,
    country       text,
    timezone      text NOT NULL DEFAULT 'UTC',
    design_it_kw  numeric(10,2),                 -- design IT load, for capacity %
    design_pue    numeric(4,3),
    attributes    jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE room (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    datacenter_id  uuid NOT NULL REFERENCES datacenter(id) ON DELETE CASCADE,
    name           text NOT NULL,                -- 'Server Hall A'
    floor          text,
    room_type      text NOT NULL DEFAULT 'data_hall',  -- data_hall|plant|electrical|network
    -- floorplan extents, metres
    width_m        numeric(8,2),
    depth_m        numeric(8,2),
    design_it_kw   numeric(10,2),
    attributes     jsonb NOT NULL DEFAULT '{}',
    UNIQUE (datacenter_id, name)
);

CREATE TABLE "row" (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    room_id    uuid NOT NULL REFERENCES room(id) ON DELETE CASCADE,
    name       text NOT NULL,                    -- 'R2'
    ordinal    int  NOT NULL,
    cold_aisle text,
    hot_aisle  text,
    UNIQUE (room_id, name)
);

CREATE TABLE rack (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    row_id         uuid NOT NULL REFERENCES "row"(id) ON DELETE CASCADE,
    name           text NOT NULL,
    ordinal        int  NOT NULL,
    u_height       int  NOT NULL DEFAULT 42,
    facing         char(1),                      -- 'N'|'S'|'E'|'W'
    floor_x        numeric(8,2),                 -- floorplan coordinates, metres
    floor_y        numeric(8,2),
    rated_power_kw numeric(8,2),                 -- design limit for capacity views
    rated_cool_kw  numeric(8,2),
    attributes     jsonb NOT NULL DEFAULT '{}',
    UNIQUE (row_id, name)
);
```

`rack_position` is not a separate table — a device's placement is
`(rack_id, u_start, u_height, facing)` on `device`, with an exclusion constraint
preventing two devices occupying the same U. That is simpler than a positions
table and gives you the collision check for free:

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE device ADD CONSTRAINT device_u_no_overlap
  EXCLUDE USING gist (
      rack_id WITH =,
      int4range(u_start, u_start + u_height, '[)') WITH &&
  ) WHERE (rack_id IS NOT NULL AND u_start IS NOT NULL);
```

This is the single highest-value constraint in the schema. Without it you will
eventually have two servers claiming U18 and no way to notice.

### 3.2 Catalog

```sql
CREATE TABLE vendor (
    id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    enterprise_oid text                      -- '1.3.6.1.4.1.318' for APC — used by trap mapping
);

CREATE TABLE device_type (
    code            text PRIMARY KEY,        -- 'server','switch','chiller','crah','ups','pdu',...
    display_name    text NOT NULL,
    category        text NOT NULL,           -- 'it'|'network'|'power'|'cooling'|'environment'|'facility'
    is_rack_mounted boolean NOT NULL DEFAULT true,
    icon            text
);

CREATE TABLE model (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor_id       uuid NOT NULL REFERENCES vendor(id),
    device_type     text NOT NULL REFERENCES device_type(code),
    name            text NOT NULL,           -- 'Supermicro SYS-121H-TNR LCC'
    u_height        int  NOT NULL DEFAULT 1,
    rated_power_w   int,                     -- nameplate; drives PDU/UPS load %
    rated_capacity  numeric(12,2),           -- kW for chillers/CRAH/CDU, kVA for UPS
    capacity_unit   text,
    attributes      jsonb NOT NULL DEFAULT '{}',
    UNIQUE (vendor_id, name)
);
```

### 3.3 Device and its terminations

```sql
CREATE TABLE device (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id    text UNIQUE,              -- simulator device id ('fa03fbfd') — the seed-import key
    name           text NOT NULL,
    device_type    text NOT NULL REFERENCES device_type(code),
    model_id       uuid REFERENCES model(id),
    vendor_id      uuid REFERENCES vendor(id),
    serial_number  text,
    asset_tag      text,

    -- placement. rack_id NULL for floor-standing plant; room_id then carries it.
    room_id        uuid REFERENCES room(id),
    rack_id        uuid REFERENCES rack(id),
    u_start        int,
    u_height       int NOT NULL DEFAULT 1,
    facing         char(1),
    floor_x        numeric(8,2),
    floor_y        numeric(8,2),

    -- primary addresses, denormalised for display and for trap source resolution
    primary_ip     inet,
    mgmt_ip        inet,

    admin_state    admin_state_t NOT NULL DEFAULT 'enabled',
    lifecycle      text NOT NULL DEFAULT 'in_service',   -- planned|in_service|maintenance|decommissioned
    commissioned_at timestamptz,
    decommissioned_at timestamptz,
    attributes     jsonb NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON device (device_type);
CREATE INDEX ON device (rack_id);
CREATE INDEX ON device (room_id);
CREATE INDEX ON device USING gin (attributes jsonb_path_ops);
CREATE UNIQUE INDEX ON device (mgmt_ip) WHERE mgmt_ip IS NOT NULL AND lifecycle <> 'decommissioned';

CREATE TABLE interface (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id    uuid NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    if_index     int,                        -- SNMP ifIndex; NULL if unknown
    name         text NOT NULL,              -- 'GigabitEthernet1/0/1'
    role         text NOT NULL DEFAULT 'data',   -- 'data'|'mgmt'
    speed_bps    bigint,
    mac          macaddr,
    ip           inet,
    admin_state  admin_state_t NOT NULL DEFAULT 'enabled',
    attributes   jsonb NOT NULL DEFAULT '{}',
    UNIQUE (device_id, name)
);
CREATE UNIQUE INDEX ON interface (device_id, if_index) WHERE if_index IS NOT NULL;

CREATE TABLE outlet (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id     uuid NOT NULL REFERENCES device(id) ON DELETE CASCADE,  -- the PDU
    number        int  NOT NULL,
    connector     text NOT NULL,             -- 'C13'|'C19'
    rated_amps    numeric(6,2),
    phase         char(1),                   -- 'A'|'B'|'C' for 3-phase PDUs
    branch        text,                      -- breaker/branch id
    UNIQUE (device_id, number)
);

CREATE TABLE power_supply (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id     uuid NOT NULL REFERENCES device(id) ON DELETE CASCADE,  -- the load
    number        int  NOT NULL,
    connector     text NOT NULL,             -- 'C14'|'C20'
    rated_watts   int,
    UNIQUE (device_id, number)
);
```

### 3.4 The connection graph (finding A3)

```sql
CREATE TABLE connection (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    layer              layer_t NOT NULL,
    link_type          text,                     -- 'ethernet'|'cord'|'chw_supply'|'chw_return'|'mstp'|'rs485'
    a_device_id        uuid NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    a_termination_type termination_t NOT NULL DEFAULT 'none',
    a_termination_id   uuid,                     -- interface.id | outlet.id | power_supply.id
    b_device_id        uuid NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    b_termination_type termination_t NOT NULL DEFAULT 'none',
    b_termination_id   uuid,
    redundancy_side    char(1),                  -- 'A'|'B' for power/cooling paths
    admin_state        admin_state_t NOT NULL DEFAULT 'enabled',
    oper_state         text NOT NULL DEFAULT 'unknown',  -- up|down|unknown
    attributes         jsonb NOT NULL DEFAULT '{}',
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON connection (layer, a_device_id);
CREATE INDEX ON connection (layer, b_device_id);
CREATE UNIQUE INDEX connection_a_term_uq ON connection (a_termination_type, a_termination_id)
    WHERE a_termination_type <> 'none';
CREATE UNIQUE INDEX connection_b_term_uq ON connection (b_termination_type, b_termination_id)
    WHERE b_termination_type <> 'none';
```

The two partial unique indexes enforce the physical truth that **one port takes
one cable and one outlet takes one cord**. Referential integrity on
`a_termination_id` cannot be a foreign key because it is polymorphic; enforce it
in the repository layer and with a nightly consistency check.

> Direction is meaningful for `power`, `cooling` and `fieldbus` (A feeds B), and
> meaningless for `production`/`management`. Store it consistently as
> **A = source/upstream, B = load/downstream** and never rely on row order.

### 3.5 Endpoints and credentials (finding A2)

```sql
CREATE TABLE credential (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name         text NOT NULL UNIQUE,
    protocol     protocol_t NOT NULL,
    kind         text NOT NULL,               -- 'snmp_v2c'|'snmp_v3'|'http_basic'|'redfish_session'|'none'
    secret_enc   bytea NOT NULL,              -- AES-GCM(nonce || ciphertext || tag); key from env/KMS
    secret_hint  text,                        -- 'community: <ip>' — safe to display, never the secret
    created_at   timestamptz NOT NULL DEFAULT now(),
    rotated_at   timestamptz
);

CREATE TABLE poll_profile (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name              text NOT NULL UNIQUE,
    interval_s        int  NOT NULL,
    timeout_ms        int  NOT NULL DEFAULT 3000,
    retries           int  NOT NULL DEFAULT 2,
    metric_selectors  text[] NOT NULL DEFAULT '{}',  -- registry metric keys or groups; empty = all
    push_enabled      boolean NOT NULL DEFAULT false -- subscribe (Redfish events / BACnet COV)
);

CREATE TABLE device_endpoint (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id       uuid NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    protocol        protocol_t NOT NULL,
    role            endpoint_role_t NOT NULL,
    address         inet,                     -- NULL only for pure sub-devices addressed via a parent
    port            int,
    addressing      jsonb NOT NULL DEFAULT '{}',
        -- snmp    : {"community_ref":"cred"}                 (community itself lives in credential)
        -- gnmi    : {"target":"10.51.11.25"}
        -- bacnet  : {"instance":2001}
        --           {"network":2001,"mac":12}                (MS/TP, with via_endpoint_id set)
        -- modbus  : {"unit_id":7}
        -- redfish : {"base":"/redfish/v1","verify_tls":false}
    via_endpoint_id uuid REFERENCES device_endpoint(id) ON DELETE SET NULL,  -- gateway/router
    credential_id   uuid REFERENCES credential(id),
    poll_profile_id uuid NOT NULL REFERENCES poll_profile(id),
    collector_id    text,                     -- shard assignment; NULL = any
    enabled         boolean NOT NULL DEFAULT true,
    admin_state     admin_state_t NOT NULL DEFAULT 'enabled',
    attributes      jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON device_endpoint (device_id);
CREATE INDEX ON device_endpoint (collector_id) WHERE enabled;
CREATE INDEX ON device_endpoint (address);      -- trap/event source resolution
CREATE INDEX ON device_endpoint (via_endpoint_id);
```

`address` is indexed because the **trap receiver resolution path** is
`source IP → endpoint → device`, and it runs on every inbound trap. Cache it in
Redis, but the index is the fallback.

---

## 4. Operational state

```sql
CREATE TABLE endpoint_state (
    endpoint_id           uuid PRIMARY KEY REFERENCES device_endpoint(id) ON DELETE CASCADE,
    status                comm_status_t NOT NULL DEFAULT 'UNKNOWN',
    last_seen             timestamptz,
    last_success          timestamptz,
    last_failure          timestamptz,
    last_error            text,
    consecutive_failures  int NOT NULL DEFAULT 0,
    poll_count            bigint NOT NULL DEFAULT 0,
    fail_count            bigint NOT NULL DEFAULT 0,
    timeout_count         bigint NOT NULL DEFAULT 0,
    auth_fail_count       bigint NOT NULL DEFAULT 0,
    last_latency_ms       int,
    collector_id          text,
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE device_state (
    device_id        uuid PRIMARY KEY REFERENCES device(id) ON DELETE CASCADE,
    status           comm_status_t NOT NULL DEFAULT 'UNKNOWN',   -- derived from endpoint_state
    health           health_t NOT NULL DEFAULT 'UNKNOWN',        -- derived from alarms
    max_severity     severity_t NOT NULL DEFAULT 'CLEAR',
    active_alarms    int NOT NULL DEFAULT 0,
    last_seen        timestamptz,
    -- hot metrics, denormalised for the dashboard and rack view. Deliberately
    -- a SMALL fixed set; anything else is a hypertable query.
    power_w          numeric(12,2),
    inlet_temp_c     numeric(6,2),
    cpu_util_pct     numeric(5,2),
    humidity_pct     numeric(5,2),
    metrics          jsonb NOT NULL DEFAULT '{}',   -- {"metric_key": {"v":..,"t":"..","q":"good"}}
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON device_state (status);
CREATE INDEX ON device_state (max_severity) WHERE max_severity <> 'CLEAR';

CREATE TABLE collector_instance (
    id              text PRIMARY KEY,          -- 'col-1'
    version         text,
    hostname        text,
    started_at      timestamptz,
    last_heartbeat  timestamptz NOT NULL,
    endpoints_owned int NOT NULL DEFAULT 0,
    status          text NOT NULL DEFAULT 'UNKNOWN',   -- HEALTHY|DEGRADED|STALE
    stats           jsonb NOT NULL DEFAULT '{}'
);
```

Keeping a bounded `metrics jsonb` on `device_state` is a deliberate trade: it
lets the rack view render 42 devices × 6 values in one query instead of 252
hypertable lookups. Cap it (the ingest worker writes only registry metrics
flagged `hot: true`) or it will grow without limit.

---

## 5. Alarms and events

```sql
CREATE TABLE alarm_rule (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name           text NOT NULL UNIQUE,
    alarm_type     text NOT NULL,               -- canonical, e.g. 'cpu_temp_high'
    enabled        boolean NOT NULL DEFAULT true,
    -- scope
    device_types   text[] NOT NULL DEFAULT '{}',-- empty = all
    device_filter  jsonb NOT NULL DEFAULT '{}', -- {"room_id": "...", "attributes.tier":"1"}
    -- condition
    metric_key     text,                        -- NULL for event-sourced rules
    operator       text,                        -- '>' '<' '>=' '<=' '==' '!=' 'absent'
    threshold      numeric,
    clear_threshold numeric,                    -- hysteresis; NULL = same as threshold
    dwell_samples  int NOT NULL DEFAULT 3,
    dwell_seconds  int,
    clear_dwell_samples int NOT NULL DEFAULT 2,
    severity       severity_t NOT NULL,
    -- staleness rules use this instead of metric/operator
    stale_after_s  int,
    message_tpl    text NOT NULL,
    attributes     jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE alarm (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id           uuid NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    endpoint_id         uuid REFERENCES device_endpoint(id) ON DELETE SET NULL,
    alarm_type          text NOT NULL,
    instance            text NOT NULL DEFAULT '',   -- ifIndex, sensor id, BACnet object, phase
    rule_id             uuid REFERENCES alarm_rule(id),
    severity            severity_t NOT NULL,
    prev_severity       severity_t,
    state               alarm_state_t NOT NULL DEFAULT 'ACTIVE',
    message             text NOT NULL,
    metric_key          text,
    trigger_value       numeric,
    threshold           numeric,
    source              text NOT NULL,              -- 'threshold'|'trap'|'redfish_event'|'bacnet_cov'|'comm'
    first_seen          timestamptz NOT NULL DEFAULT now(),
    last_seen           timestamptz NOT NULL DEFAULT now(),
    occurrence_count    int NOT NULL DEFAULT 1,
    acknowledged_at     timestamptz,
    acknowledged_by     text,
    ack_note            text,
    cleared_at          timestamptz,
    cleared_by          text,                       -- 'system'|username
    -- correlation
    root_cause_alarm_id uuid REFERENCES alarm(id) ON DELETE SET NULL,
    is_symptom          boolean NOT NULL DEFAULT false,
    attributes          jsonb NOT NULL DEFAULT '{}'
);

-- THE constraint that makes raise/update/clear idempotent (finding A6.3)
CREATE UNIQUE INDEX alarm_active_key
    ON alarm (device_id, alarm_type, instance)
    WHERE state <> 'CLEARED';

CREATE INDEX ON alarm (state, severity, last_seen DESC);
CREATE INDEX ON alarm (device_id) WHERE state <> 'CLEARED';
CREATE INDEX ON alarm (root_cause_alarm_id) WHERE is_symptom;

CREATE TABLE event (
    id            bigserial PRIMARY KEY,
    ts            timestamptz NOT NULL DEFAULT now(),
    device_id     uuid REFERENCES device(id) ON DELETE SET NULL,
    endpoint_id   uuid REFERENCES device_endpoint(id) ON DELETE SET NULL,
    source_ip     inet,                     -- kept even when resolution fails
    event_type    text NOT NULL,            -- canonical: 'link_down','ups_on_battery',...
    source        text NOT NULL,            -- 'snmp_trap'|'redfish_event'|'bacnet_cov'|'system'
    severity      severity_t NOT NULL DEFAULT 'INFO',
    message       text NOT NULL,
    raw           jsonb NOT NULL DEFAULT '{}',  -- varbinds, MessageArgs, COV payload
    alarm_id      uuid REFERENCES alarm(id) ON DELETE SET NULL,
    correlation_id uuid
);
```

`event` grows fast and is a natural hypertable — see §6.

**Unresolvable traps are still recorded.** If `source_ip` resolves to no
endpoint, the event is written with `device_id = NULL` and raises a
platform-level `unknown_trap_source` alarm. Silently dropping them is how you end
up debugging "the DCIM never saw it" for two days.

---

## 6. Time-series (TimescaleDB)

### 6.1 Metric dimension

```sql
CREATE TABLE metric (
    id            smallserial PRIMARY KEY,
    key           text NOT NULL UNIQUE,       -- 'cpu_temperature' — from contracts/metrics/registry.yaml
    display_name  text NOT NULL,
    unit          text NOT NULL,              -- 'C','W','A','V','pct','lps','kpa','bps','count'
    value_type    value_type_t NOT NULL,
    aggregation   text NOT NULL DEFAULT 'avg',-- avg|sum|max|min|last
    min_valid     numeric,
    max_valid     numeric,
    stale_after_s int NOT NULL DEFAULT 300,
    is_hot        boolean NOT NULL DEFAULT false   -- mirrored into device_state.metrics
);
```

Loaded from the registry at migration/boot time. `metric.id` is a `smallint`,
which is what the hypertable stores (finding B3).

### 6.2 Numeric samples

```sql
CREATE TABLE telemetry_sample (
    ts          timestamptz  NOT NULL,        -- observed_at
    device_id   uuid         NOT NULL,
    metric_id   smallint     NOT NULL REFERENCES metric(id),
    instance    text         NOT NULL DEFAULT '',   -- ifIndex / sensor id / phase / object instance
    value       double precision NOT NULL,
    quality     quality_t    NOT NULL DEFAULT 'good',
    PRIMARY KEY (device_id, metric_id, instance, ts)
);
SELECT create_hypertable('telemetry_sample','ts',
       chunk_time_interval => INTERVAL '1 day');

ALTER TABLE telemetry_sample SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id, metric_id, instance',
    timescaledb.compress_orderby   = 'ts DESC'
);
SELECT add_compression_policy('telemetry_sample', INTERVAL '7 days');
SELECT add_retention_policy  ('telemetry_sample', INTERVAL '90 days');
```

The primary key doubles as the idempotency guarantee for at-least-once delivery:
a duplicated batch conflicts and is discarded with `ON CONFLICT DO NOTHING`.

**Counters** are stored twice: the raw counter under its own metric key
(`if_in_octets`, `value_type = counter`) and the derived rate under a separate
key (`if_in_bps`, `value_type = gauge`). Deriving at query time from a compressed
hypertable is far slower and cannot see across a gap correctly.

### 6.3 Booleans and text

Separate narrow tables, because storing `Chiller_Running` as a float in the same
hypertable wrecks compression ratios and makes every query cast.

```sql
CREATE TABLE telemetry_bool (
    ts timestamptz NOT NULL, device_id uuid NOT NULL, metric_id smallint NOT NULL,
    instance text NOT NULL DEFAULT '', value boolean NOT NULL,
    quality quality_t NOT NULL DEFAULT 'good',
    PRIMARY KEY (device_id, metric_id, instance, ts)
);
SELECT create_hypertable('telemetry_bool','ts', chunk_time_interval => INTERVAL '7 days');

CREATE TABLE telemetry_text (
    ts timestamptz NOT NULL, device_id uuid NOT NULL, metric_id smallint NOT NULL,
    instance text NOT NULL DEFAULT '', value text NOT NULL,
    quality quality_t NOT NULL DEFAULT 'good',
    PRIMARY KEY (device_id, metric_id, instance, ts)
);
SELECT create_hypertable('telemetry_text','ts', chunk_time_interval => INTERVAL '7 days');
```

Binary points are naturally **sparse** — write on change plus a heartbeat every
N minutes, not every poll. That is why the chunk interval is a week.

### 6.4 Continuous aggregates

Without these, a 30-day chart reads raw samples. With them, it reads a few
hundred rows.

```sql
CREATE MATERIALIZED VIEW telemetry_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', ts) AS bucket,
       device_id, metric_id, instance,
       avg(value) AS avg_value, min(value) AS min_value,
       max(value) AS max_value, last(value, ts) AS last_value,
       count(*)   AS sample_count
FROM telemetry_sample
GROUP BY bucket, device_id, metric_id, instance;

SELECT add_continuous_aggregate_policy('telemetry_1m',
    start_offset => INTERVAL '3 hours', end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute');

-- and the same shape for telemetry_5m (from 1m) and telemetry_1h (from 5m),
-- using hierarchical continuous aggregates.
SELECT add_retention_policy('telemetry_1m', INTERVAL '1 year');
-- telemetry_1h: no retention. It is small and it is what capacity trending needs.
```

**Query routing rule** (implemented once in the telemetry repository):

| Requested window | Source |
|---|---|
| ≤ 6 h | `telemetry_sample` |
| ≤ 7 d | `telemetry_1m` |
| ≤ 90 d | `telemetry_5m` |
| > 90 d | `telemetry_1h` |

### 6.5 Poll results and alarm history

```sql
CREATE TABLE poll_result (
    ts timestamptz NOT NULL, endpoint_id uuid NOT NULL, collector_id text NOT NULL,
    success boolean NOT NULL, latency_ms int, error_class text, metrics_returned int
);
SELECT create_hypertable('poll_result','ts', chunk_time_interval => INTERVAL '1 day');
SELECT add_retention_policy('poll_result', INTERVAL '14 days');

CREATE TABLE alarm_history (
    ts timestamptz NOT NULL, alarm_id uuid NOT NULL, device_id uuid NOT NULL,
    action text NOT NULL,            -- raised|escalated|deescalated|acknowledged|cleared|suppressed
    severity severity_t, actor text, detail jsonb NOT NULL DEFAULT '{}'
);
SELECT create_hypertable('alarm_history','ts', chunk_time_interval => INTERVAL '30 days');
```

`poll_result` is what makes "why is this device flapping" answerable. It is also
the input for the collector-health view.

---

## 7. Discovery staging (finding A9)

```sql
CREATE TABLE discovery_run (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
    method text NOT NULL,             -- 'snmp_sweep'|'bacnet_whois'|'redfish_probe'|'seed_import'
    scope  jsonb NOT NULL,            -- {"subnets":["10.51.0.0/16"]}
    found int NOT NULL DEFAULT 0, promoted int NOT NULL DEFAULT 0, status text NOT NULL
);

CREATE TABLE discovery_candidate (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id uuid NOT NULL REFERENCES discovery_run(id) ON DELETE CASCADE,
    address inet, protocol protocol_t NOT NULL,
    identity jsonb NOT NULL,          -- sysDescr/sysObjectID, BACnet instance+vendor, Redfish Manufacturer
    suggested_device_type text, suggested_vendor text, suggested_model text,
    matched_device_id uuid REFERENCES device(id),   -- non-NULL = already in inventory
    status text NOT NULL DEFAULT 'new',             -- new|promoted|ignored|duplicate
    first_seen timestamptz NOT NULL DEFAULT now(), last_seen timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON discovery_candidate (address, protocol) WHERE status = 'new';
```

---

## 8. Derived views the UI needs

```sql
-- rack roll-up: powers the rack list, the floor plan heat map, and capacity views
CREATE VIEW v_rack_summary AS
SELECT r.id AS rack_id, r.name, ro.id AS row_id, rm.id AS room_id, dc.id AS datacenter_id,
       count(d.id)                                   AS device_count,
       count(*) FILTER (WHERE ds.status = 'ONLINE')  AS online_count,
       count(*) FILTER (WHERE ds.status = 'OFFLINE') AS offline_count,
       coalesce(sum(ds.power_w), 0) / 1000.0         AS load_kw,
       r.rated_power_kw,
       CASE WHEN r.rated_power_kw > 0
            THEN 100.0 * coalesce(sum(ds.power_w),0)/1000.0 / r.rated_power_kw END AS load_pct,
       max(ds.inlet_temp_c)                          AS max_inlet_c,
       coalesce(max(ds.max_severity), 'CLEAR')       AS max_severity,
       r.u_height - coalesce(sum(d.u_height), 0)     AS free_u
FROM rack r
JOIN "row" ro ON ro.id = r.row_id
JOIN room rm  ON rm.id = ro.room_id
JOIN datacenter dc ON dc.id = rm.datacenter_id
LEFT JOIN device d      ON d.rack_id = r.id AND d.lifecycle = 'in_service'
LEFT JOIN device_state ds ON ds.device_id = d.id
GROUP BY r.id, ro.id, rm.id, dc.id;
```

Add equivalent `v_room_summary` and `v_datacenter_summary`. Materialise them only
if measurement says you need to — at 664 devices these are sub-10 ms.

---

## 9. Migration and seeding order

1. Extensions: `pgcrypto`, `btree_gist`, `timescaledb`.
2. Enums → catalog tables (`vendor`, `device_type`, `model`).
3. `metric` loaded from `contracts/metrics/registry.yaml` (idempotent upsert; a
   removed metric is marked deprecated, never deleted — the hypertable still
   references it).
4. Physical hierarchy, then devices, then interfaces/outlets/PSUs, then
   connections (connections last — they reference terminations).
5. `credential`, `poll_profile`, `device_endpoint`.
6. Hypertables, compression, retention, continuous aggregates.
7. Default `alarm_rule` set.

The seed importer (`backend/app/importer/`) performs 4–5 from the simulator's
`GET /api/topology/export`. See `16-simulator-integration.md` for the field
mapping.
