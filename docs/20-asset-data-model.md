# 20. Asset & inventory — data model

Status: **specification. Not built.** Migrations `0043`–`0050` are reserved for
it; the latest applied migration is `0042`.

This document specifies the schema delta for the asset module. It follows from
`19-asset-inventory-review.md`, and its governing decision is B1: **there is no
`assets` table.** `device` is the asset. Everything here either adds columns to
`device` or adds a table keyed on `device.id`.

If you read one thing, read §1 — it is the rule the rest of the document obeys.

## 1. The rule: extend, do not parallel

Every downstream row in this schema is keyed on `device.id` — telemetry and its
four continuous aggregates, alarms, `device_endpoint`, `endpoint_state`,
`device_state`, collector assignment, and both sides of `connection`. A second
identity for the same physical thing means a mapping table, and the mapping is
where drift lives.

So:

| Need | Where it goes |
|---|---|
| Asset identity, tag, serial, purchase | columns on `device` |
| Anything one-to-many on an asset | new table with `device_id` FK |
| Consumable stock | its own domain, joined to `device` only when consumed |
| Anything already modelled | nothing — reuse it |

The word "asset" is a UI label for a `device` row. `21-asset-api-spec.md` keeps
the API on `/devices` for exactly this reason and exposes the asset views as
sub-resources.

## 2. Columns added to `device`

```sql
ALTER TABLE device
    ADD COLUMN supplier_id       uuid REFERENCES supplier(id),
    ADD COLUMN purchase_date     date,
    ADD COLUMN purchase_order    text,
    ADD COLUMN purchase_cost     numeric(12,2),
    ADD COLUMN currency          char(3),
    ADD COLUMN install_date      date,
    ADD COLUMN warranty_expires  date,
    ADD COLUMN eol_date          date,
    ADD COLUMN eos_date          date,
    ADD COLUMN owner_group       text,
    ADD COLUMN cost_centre       text,
    ADD COLUMN notes             text;
```

`serial_number` and `asset_tag` already exist and are not re-added. They are
**constrained** instead, which is B2:

```sql
-- Unique where present. A partial index, not a plain UNIQUE: 664 rows are NULL
-- today and will stay NULL until the importer is fixed, and NULLs are not
-- comparable anyway - being explicit documents the intent.
CREATE UNIQUE INDEX ix_device_serial_unique ON device (serial_number)
    WHERE serial_number IS NOT NULL;
CREATE UNIQUE INDEX ix_device_asset_tag_unique ON device (asset_tag)
    WHERE asset_tag IS NOT NULL;

-- Warranty expiry is read as a filter and a sort on the asset list, on every
-- page load. Partial, because an asset with no warranty date is not a row the
-- "expiring soon" query ever wants.
CREATE INDEX ix_device_warranty_expires ON device (warranty_expires)
    WHERE warranty_expires IS NOT NULL;
```

**`warranty_expires` is denormalised on purpose.** The authoritative record is
`support_contract` (§5); this column is the earliest active covering expiry,
maintained by the same code that writes the contract link. It exists because the
asset list has to sort and filter 664 rows by expiry without a three-table join
on every keystroke, and because the "expiring in 90 days" count on the landing
page is otherwise an aggregate over a join. It is a cache, and the contract
table is the truth — anything that writes `device_support` must recompute it.

### Populating serial numbers

The index above is worthless until the data arrives. The path is:

1. simulator export emits `serial_number` per device (it knows the SKU and can
   mint a stable synthetic serial — this is a simulator, and a *stable* fake is
   what makes reconciliation testable);
2. `backend/app/importer/simulator.py` maps it onto `device.serial_number`,
   idempotently on `external_id` as it already does;
3. the unique index goes on **after** the first successful import, so a
   collision fails the migration loudly rather than silently deduplicating.

Asset tags are a facility artefact, not a device fact. They are entered by hand
or bulk-imported from CSV, and the column stays NULL until someone does that.

## 3. Lifecycle: three states and a history table

`lifecycle_t` today is `planned | in_service | maintenance | decommissioned`.
Three values are added (B5):

```sql
ALTER TYPE lifecycle_t ADD VALUE IF NOT EXISTS 'in_stock'  AFTER 'planned';
ALTER TYPE lifecycle_t ADD VALUE IF NOT EXISTS 'installed' AFTER 'in_stock';
ALTER TYPE lifecycle_t ADD VALUE IF NOT EXISTS 'retired'   AFTER 'decommissioned';
```

> **Migration gotcha.** `ALTER TYPE ... ADD VALUE` runs inside a transaction on
> PostgreSQL 12+, but the new label **cannot be used in the same transaction**
> that added it. A migration that adds `in_stock` and then `UPDATE device SET
> lifecycle = 'in_stock'` fails with *unsafe use of new value*. Split it: one
> migration adds the labels, the next one uses them. Alembic will not warn you.

The full state set and what each one means operationally:

| State | Racked | Polled | Alarms | Counts against capacity |
|---|---|---|---|---|
| `planned` | position may be held | no | no | **yes** — that is the point |
| `in_stock` | no | no | no | no |
| `installed` | yes | yes | **no** | yes |
| `in_service` | yes | yes | yes | yes |
| `maintenance` | yes | yes | shelved (§6) | yes |
| `decommissioned` | no | no | no | no |
| `retired` | no | no | no | no |

`installed` earns its row because of the alarm column: a machine that is racked
and cabled but not yet accepted should appear in the elevation and in capacity,
and must not page anyone. Today there is no state that does both.

### `device_lifecycle_event`

```sql
CREATE TABLE device_lifecycle_event (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id    uuid NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    from_state   lifecycle_t,              -- NULL on creation
    to_state     lifecycle_t NOT NULL,
    reason       text,                     -- free text; the change board reads this
    change_ref   text,                     -- external change/ticket reference
    actor        text NOT NULL,
    ts           timestamptz NOT NULL DEFAULT now(),
    attributes   jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ix_dle_device_ts ON device_lifecycle_event (device_id, ts DESC);
CREATE INDEX ix_dle_ts ON device_lifecycle_event (ts DESC);
```

This does **not** replace `audit_log`, and the distinction matters. `audit_log`
records that a field changed, generically, for compliance, with credential
scrubbing on the way in. `device_lifecycle_event` records a *business event* an
operator asked for, with a reason, on a query path that answers "show me this
asset's history" in one index scan. Both are written on a transition. Neither is
derivable from the other.

`commissioned_at` and `decommissioned_at` stay on `device` as denormalised
first/last markers, and are recomputed from this table.

## 4. Suppliers

`vendor` is the manufacturer — it exists, and it carries `enterprise_oid` for
trap mapping, which is a hint about what it is for. Who you *bought from* and
who *supports it* are different parties and often different from each other.

```sql
CREATE TABLE supplier (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text NOT NULL UNIQUE,
    account_ref   text,
    contact_name  text,
    contact_email text,
    contact_phone text,
    notes         text,
    attributes    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
```

## 5. Warranty and support contracts

§18 asks for warranty tracking. Modelled as a contract that *covers* assets,
not as a date on each one, because one contract covers many devices and renews
as a unit — putting the date only on the device means renewing 200 rows and
getting 197 of them.

```sql
CREATE TABLE support_contract (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id    uuid REFERENCES supplier(id),
    reference      text NOT NULL,          -- the supplier's contract number
    kind           text NOT NULL,          -- warranty | support | maintenance
    service_level  text,                   -- NBD, 4h onsite, 24x7x4 ...
    start_date     date NOT NULL,
    end_date       date NOT NULL,
    cost           numeric(12,2),
    currency       char(3),
    auto_renew     boolean NOT NULL DEFAULT false,
    notes          text,
    attributes     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT support_contract_dates CHECK (end_date >= start_date),
    CONSTRAINT support_contract_ref_uq UNIQUE (supplier_id, reference)
);

CREATE TABLE device_support (
    device_id   uuid NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    contract_id uuid NOT NULL REFERENCES support_contract(id) ON DELETE CASCADE,
    added_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, contract_id)
);
CREATE INDEX ix_device_support_contract ON device_support (contract_id);
```

`device.warranty_expires` is recomputed as
`MAX(end_date)` over the device's active contracts whenever a `device_support`
row is written or a contract's dates change. That recompute is the only thing
allowed to write the column.

## 6. Maintenance windows, and what they do to alarms

This is B4, and it is the section with a real design decision in it.

```sql
CREATE TABLE maintenance_window (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title         text NOT NULL,
    description   text,
    change_ref    text,
    kind          text NOT NULL DEFAULT 'planned',  -- planned | emergency
    starts_at     timestamptz NOT NULL,
    ends_at       timestamptz NOT NULL,
    -- scheduled -> active -> completed | cancelled. Advanced by a ticker, not
    -- by reading the clock at query time: an alarm decision has to be the same
    -- for the ingest worker and the API, and clock-derived state is not.
    status        text NOT NULL DEFAULT 'scheduled',
    suppress      boolean NOT NULL DEFAULT true,
    created_by    text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT maintenance_window_dates CHECK (ends_at > starts_at)
);
CREATE INDEX ix_mw_active ON maintenance_window (starts_at, ends_at)
    WHERE status IN ('scheduled', 'active');

CREATE TABLE maintenance_target (
    window_id  uuid NOT NULL REFERENCES maintenance_window(id) ON DELETE CASCADE,
    device_id  uuid NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    PRIMARY KEY (window_id, device_id)
);
CREATE INDEX ix_maintenance_target_device ON maintenance_target (device_id);

-- What was actually done. Separate from the window because emergency work has
-- a record and no window, and a window can end with nothing done.
CREATE TABLE maintenance_record (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id    uuid NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    window_id    uuid REFERENCES maintenance_window(id) ON DELETE SET NULL,
    performed_at timestamptz NOT NULL DEFAULT now(),
    performed_by text NOT NULL,
    kind         text NOT NULL,   -- preventive | corrective | firmware | replacement
    summary      text NOT NULL,
    detail       text,
    parts_used   jsonb NOT NULL DEFAULT '[]'::jsonb,  -- see §7
    attributes   jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ix_maintenance_record_device ON maintenance_record (device_id, performed_at DESC);
```

### Shelve, do not suppress

The decision B4 demands: an alarm on a device inside an active window is
**raised and stored as normal**, then marked, and excluded from the active list,
the roll-ups and the notification path.

```sql
ALTER TABLE alarm ADD COLUMN shelved_by_window uuid REFERENCES maintenance_window(id);
CREATE INDEX ix_alarm_shelved ON alarm (shelved_by_window)
    WHERE shelved_by_window IS NOT NULL;
```

Never-raising is cheaper and wrong. The question asked after every work window
is "did anything *else* break while we were in there", and it cannot be answered
from alarms that were never written. Shelving keeps the record, keeps the
timeline honest, and lets the window's own page show "3 alarms shelved" — which
is also how you find out the window was scoped too widely.

The rack and room severity roll-ups must exclude shelved alarms, or a room goes
red for a planned filter change. That is a `WHERE shelved_by_window IS NULL` in
the roll-up query and in `18-alert-taxonomy.md`'s counters, and it is the part
most likely to be forgotten.

`device.admin_state = 'maintenance'` stays what it is — an operator's manual
override, independent of any window — and is honoured by the same predicate.

## 7. Consumables: parts, stock and movement

B3: this is its own domain. It shares nothing with `device` except that a part
can be consumed *on* a device.

```sql
CREATE TABLE part (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sku            text NOT NULL UNIQUE,     -- manufacturer part number
    name           text NOT NULL,
    category       text NOT NULL,            -- psu | fan | memory | disk | optic | cable | other
    vendor_id      uuid REFERENCES vendor(id),
    -- Which device types this part fits. Advisory, used to offer the right
    -- parts on a device's maintenance form, never enforced: cross-compatible
    -- parts are the norm and a hard FK here would be wrong within a month.
    fits_types     text[] NOT NULL DEFAULT '{}',
    unit_cost      numeric(12,2),
    currency       char(3),
    attributes     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- Where parts are kept. A store is a place, not a rack: it may be a room in a
-- datacenter or an offsite depot, so room_id is nullable.
CREATE TABLE store (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text NOT NULL,
    datacenter_id uuid REFERENCES datacenter(id),
    room_id       uuid REFERENCES room(id),
    location_note text,
    CONSTRAINT store_name_uq UNIQUE (datacenter_id, name)
);

CREATE TABLE part_stock (
    part_id      uuid NOT NULL REFERENCES part(id) ON DELETE CASCADE,
    store_id     uuid NOT NULL REFERENCES store(id) ON DELETE CASCADE,
    on_hand      integer NOT NULL DEFAULT 0,
    reserved     integer NOT NULL DEFAULT 0,
    reorder_at   integer,
    reorder_to   integer,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (part_id, store_id),
    CONSTRAINT part_stock_nonneg CHECK (on_hand >= 0 AND reserved >= 0),
    CONSTRAINT part_stock_reserved CHECK (reserved <= on_hand)
);

-- The ledger. on_hand is a running total DERIVED from these rows; the movement
-- is the record. Without it, "we had four last week" is unanswerable and every
-- discrepancy is someone's memory against a number.
CREATE TABLE stock_movement (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    part_id      uuid NOT NULL REFERENCES part(id) ON DELETE CASCADE,
    store_id     uuid NOT NULL REFERENCES store(id) ON DELETE CASCADE,
    delta        integer NOT NULL,         -- +receipt, -consumption
    reason       text NOT NULL,            -- receipt | consumed | adjustment | rma | transfer
    device_id    uuid REFERENCES device(id) ON DELETE SET NULL,  -- when consumed
    record_id    uuid REFERENCES maintenance_record(id) ON DELETE SET NULL,
    actor        text NOT NULL,
    ts           timestamptz NOT NULL DEFAULT now(),
    note         text,
    CONSTRAINT stock_movement_nonzero CHECK (delta <> 0)
);
CREATE INDEX ix_stock_movement_part ON stock_movement (part_id, ts DESC);
CREATE INDEX ix_stock_movement_device ON stock_movement (device_id)
    WHERE device_id IS NOT NULL;
```

**A spare server is not a part.** It has a serial, it will be racked, it will be
polled. It is a `device` with `lifecycle = 'in_stock'` and `rack_id IS NULL`.
The rule, stated once so it is not re-litigated per item: *if the individual is
tracked, it is a device; if only the count is tracked, it is a part.*

## 8. Tags

`attributes` JSONB exists on `device`, `rack` and `room` and is the right place
for one-off values. It is the wrong place for a controlled vocabulary you want
to filter and count by, because there is no list of valid keys and no way to
rename one.

```sql
CREATE TABLE tag (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key         text NOT NULL,
    value       text NOT NULL,
    colour      text,
    description text,
    CONSTRAINT tag_kv_uq UNIQUE (key, value)
);

-- Polymorphic like connection terminations, and for the same reason: tags apply
-- to devices, racks and rooms, and three near-identical join tables is worse.
CREATE TABLE tag_assignment (
    tag_id      uuid NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
    object_type text NOT NULL,   -- device | rack | room
    object_id   uuid NOT NULL,
    assigned_by text NOT NULL,
    assigned_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tag_id, object_type, object_id)
);
CREATE INDEX ix_tag_assignment_object ON tag_assignment (object_type, object_id);
```

`key`/`value` rather than a flat label, so `env=prod` and `env=dev` are one
dimension with two values and the UI can offer a picker rather than a text box.

**Tenancy does not go here** (B8). If tenancy is in scope it is a column with a
foreign key and a filter in the repository layer; if it is not, it should not
exist as a tag either, because a `tenant=` tag will be populated and then
relied on.

## 9. Capacity reservation

B6. A reservation holds space and power that no device occupies yet.

```sql
CREATE TABLE capacity_reservation (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rack_id      uuid REFERENCES rack(id) ON DELETE CASCADE,
    room_id      uuid REFERENCES room(id) ON DELETE CASCADE,
    project      text NOT NULL,
    owner_group  text,
    u_start      integer,
    u_height     integer,
    power_kw     numeric(8,2),
    cool_kw      numeric(8,2),
    needed_by    date,
    expires_at   date NOT NULL,     -- reservations must die on their own
    status       text NOT NULL DEFAULT 'held',  -- held | fulfilled | released | expired
    created_by   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    notes        text,
    CONSTRAINT reservation_scope CHECK (rack_id IS NOT NULL OR room_id IS NOT NULL),
    CONSTRAINT reservation_u CHECK (
        (u_start IS NULL AND u_height IS NULL) OR
        (u_start IS NOT NULL AND u_height >= 1))
);
```

### The U-range problem

A reservation that names `u_start` and `u_height` must not overlap a device or
another reservation. `device_u_no_overlap` is an exclusion constraint on
`device`, and PostgreSQL exclusion constraints do not span tables — so this
cannot simply be extended.

Two options, and the second is recommended:

1. **Cross-table trigger.** A `BEFORE INSERT/UPDATE` trigger on each table
   checking the other. Correct, and it needs explicit locking to be safe under
   concurrency, which is easy to get subtly wrong.
2. **A reservation *is* a planned device.** Insert a `device` row with
   `lifecycle = 'planned'`, a name like `RESERVED-<project>`, `rack_id` and
   `u_start` set. `device_u_no_overlap` then enforces it with no new code, the
   rack elevation renders it for free, and promoting the reservation is an
   UPDATE of the same row rather than a create-and-delete.

Option 2 keeps `capacity_reservation` for the power and cooling part and for
room-level holds with no specific U, and gives the U-space part to the
constraint that already works. The cost is that `planned` devices must be
excluded from every "device count" the platform reports, which is a predicate
the estate queries need anyway.

Reservations expire. `expires_at` is NOT NULL because the failure mode of this
feature everywhere it exists is a rack held for a project that was cancelled in
2023 and nobody released.

## 10. Documents

```sql
CREATE TABLE asset_document (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_type   text NOT NULL,   -- device | rack | room | contract
    object_id     uuid NOT NULL,
    title         text NOT NULL,
    kind          text,            -- photo | manual | invoice | rma | diagram
    content_type  text NOT NULL,
    size_bytes    bigint NOT NULL,
    sha256        char(64) NOT NULL,
    storage_key   text NOT NULL,   -- object store key; the blob is NOT in Postgres
    uploaded_by   text NOT NULL,
    uploaded_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT asset_document_size CHECK (size_bytes > 0)
);
CREATE INDEX ix_asset_document_object ON asset_document (object_type, object_id);
```

Blobs do not go in PostgreSQL. A rack photo is 4 MB, an operator will upload
forty of them in an afternoon, and `pg_dump` is how you find out. `storage_key`
points at object storage; the deployment currently has none, so this table is
the last thing built and the first thing that needs an infrastructure decision.

## 11. Cabling — sketched, deferred

B7. Recorded here so the shape is known before it is needed, and deliberately
not scheduled (`23` puts it last).

The model that works is a **cable as a physical segment**, with the logical
`connection` composed of an ordered chain of them:

```sql
-- SKETCH. Not in the migration plan.
CREATE TABLE cable (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    label         text,            -- what is printed on the jacket
    cable_type    text NOT NULL,   -- cat6a | om4 | dac | power
    length_m      numeric(6,2),
    colour        text,
    a_termination_type termination_t NOT NULL,
    a_termination_id   uuid,
    b_termination_type termination_t NOT NULL,
    b_termination_id   uuid
);
-- and connection gains: path uuid[] - the ordered cable ids the logical link
-- traverses, empty for a direct attach.
```

The reason it is deferred rather than dropped: patch panels are not modelled as
port-bearing devices today, and a patch path is meaningless without them.
Getting there is a `device_type` of `patch_panel` with `interface` rows for its
ports — which the schema already supports — and that is a data problem before it
is a schema problem.

## 12. Migration order

`ALTER TYPE ... ADD VALUE` must not share a transaction with any use of the new
label, which is why `0043` does nothing else.

**Maintenance and support contracts swapped numbers when phase 3 was built.**
Alembic is a linear chain, phase 3 lands before phase 4, and a table cannot
depend on a revision that does not exist yet - so maintenance took `0046` and
support contracts moved to `0047`. The original numbering assumed the phases
could be built in any order; they cannot.

| Migration | Contents | Depends on |
|---|---|---|
| `0043` | `lifecycle_t` gains `in_stock`, `installed`, `retired` — nothing else | — |
| `0044` | `supplier`; `device` columns; unique indexes on serial and asset tag | 0043 |
| `0045` | `device_lifecycle_event`, backfilled from `commissioned_at` / `decommissioned_at` | 0044 |
| `0046` | `maintenance_window`, `maintenance_target`, `maintenance_record`; `alarm.shelved_by_window` | 0043 |
| `0047` | `support_contract`, `device_support`; `warranty_expires` recompute | 0044 |
| `0048` | `tag`, `tag_assignment` | — |
| `0049` | `part`, `store`, `part_stock`, `stock_movement` | — |
| `0050` | `capacity_reservation` | 0043 |

`asset_document` is unnumbered: it waits on an object-store decision.

The unique index in `0044` is the one that can fail on real data. Run the
importer first, check for duplicate serials, then migrate — a failed unique
index build is a loud, recoverable error, and that is the point of doing it in
this order rather than defining the column as UNIQUE from the start.

## 13. What is deliberately not here

| Not built | Because |
|---|---|
| `assets` table | B1 — `device` is the asset |
| `locations` table | `datacenter` → `room` → `rack_row` exists |
| `rack_positions` | `device.u_start` + `device_u_no_overlap` is stronger |
| `asset_interfaces` | `interface` exists, 5,700 rows |
| `asset_relationships`, `power_connections` | `connection` exists, 5 layers, with `redundancy_side` |
| `ordered` / `received` lifecycle states | procurement, not DCIM (B5) |
| `tenant` column | undecided and must be decided, not defaulted (B8) |
| Denormalised asset counts, materialised views | premature at 664 devices (B11) |
