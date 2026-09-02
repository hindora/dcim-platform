# 19. Asset & inventory — review of the proposed plan

Status: **review only. Nothing in this document has been built.** It is the
argument for what `20`–`23` specify.

The subject is a 27-section plan for an Asset & Inventory Management module,
opened from the `Assets` item in the main menu. This document reviews that plan
against the platform as it actually runs, the same way `01-architecture-review.md`
reviewed the original architecture.

Every count below was read out of the running system on 2026-09-02 (664 devices,
2 datacenters, 16 rooms, 44 racks, 25 device types in use), or out of the source
at the file and line cited. Where the plan and the schema disagree, the schema
wins, because it has data in it.

## Verdict

**The domain coverage is good and the situational awareness is not.** Roughly
sixty percent of the plan describes subsystems that exist, are populated, and
are serving traffic today. Five areas are genuinely new. One item, built as
written, would take a working system apart.

| # | Finding | Severity |
|---|---|---|
| B1 | §21 defines seven tables that already exist under other names, giving every asset a **second identity** — the one thing §24 exists to prevent | **Critical** |
| B2 | §9 reconciliation cannot match anything: `serial_number` and `asset_tag` are NULL on **all 664 devices** and carry no unique index | **Critical** |
| B3 | §6 models spare parts as asset *types* while §10 models them as *quantities*. Serialised instances and count-tracked consumables are not one table | **High** |
| B4 | §17 puts assets into Maintenance but never says what that does to alarms. The state exists (`admin_state = maintenance`) and **nothing reads it** | **High** |
| B5 | §11 defines ten lifecycle states, three of which name one condition and two of which are procurement; the transition **history** that matters is absent | **High** |
| B6 | §13 measures capacity and never reserves it. A DCIM is asked to hold space, not only to report it | **Medium** |
| B7 | Structured cabling is absent. Patch panel appears as an asset type; cable identity, length and patch path do not appear at all | **Medium** |
| B8 | §16 lists `tenant` among custom metadata fields. Tenancy is an access boundary and a billing dimension, not a free-text attribute | **Medium** |
| B9 | §15 bulk operations have no write path, no validation contract and no audit story, against a schema whose constraints will reject half of them | **Medium** |
| B10 | §3 and §22 propose navigation that exists today under different labels; `/assets` already resolves | **Low** |
| B11 | §26 sizes for 40,000 assets. The estate is 664 and the shape of the bottleneck is not yet known | **Low** |

## 1. B1 — the parallel `assets` table

§21 defines `assets`, `locations`, `racks`, `rack_positions`,
`asset_interfaces`, `asset_relationships` and `power_connections`. All seven
exist.

| §21 proposes | Exists as | Rows |
|---|---|---|
| `assets` | `device` | 664 |
| `locations` | `datacenter` → `room` → `rack_row` | 2 / 16 / — |
| `racks` | `rack` | 44 |
| `rack_positions` | `device.rack_id` + `u_start` + `u_height` | — |
| `asset_interfaces` | `interface` | ~5,700 |
| `asset_relationships` | `connection`, 5 layers | ~2,566 |
| `power_connections` | `connection` where `layer = 'power'` | ~840 |

The problem is not duplication of effort. It is duplication of **identity**.

§24 asks for one stable identifier shared by inventory, telemetry and topology,
and it is right to. A new `assets` table creates a second one on the day it is
created. Every downstream row in this schema is keyed on `device.id`: the
telemetry hypertables and their four continuous aggregates, every alarm,
`device_endpoint`, `endpoint_state`, `device_state`, poll results, collector
assignment, and both sides of `connection`. An `assets` table either adopts
those foreign keys — in which case it is `device` with a different name — or it
sits beside them behind a mapping, and the mapping is precisely where drift
lives. Six months in, the question "why does the asset list say 664 and the
alarm console say 661" has no cheap answer.

Two specific losses are worth naming, because they are not obvious from a table
listing.

**`device_u_no_overlap`.** `backend/alembic/versions/0001_baseline.py:193`
declares:

```sql
ALTER TABLE device ADD CONSTRAINT device_u_no_overlap
EXCLUDE USING gist (
    rack_id WITH =,
    int4range(u_start, u_start + u_height, '[)') WITH &&
) WHERE (rack_id IS NOT NULL AND u_start IS NOT NULL)
```

Two devices can never claim the same rack unit — not "should not", *cannot*, at
the storage layer, including through a bulk import or a hand-written UPDATE. A
`rack_positions` table starts without this and has to rebuild it, or it silently
accepts two servers in U41 and someone finds out on the floor with a screwdriver.

**Polymorphic terminations.** `connection` deliberately has no foreign key on
`a_termination_id` / `b_termination_id`, because a power cord lands on an
`outlet` and a `power_supply` while a patch lands on two `interface` rows, and
one table has to hold both. It carries `redundancy_side`, which is the only
column that answers the question asked during an event — *is this load still fed
from the other side?* — and two partial unique indexes that enforce one cable
per port and one cord per outlet. `asset_relationships` plus `power_connections`
is that model split in half, and the half that gets split is redundancy.

**The fix.** "Asset" is a *view* over the device model, not a rival to it.
Extend `device` with the fields it lacks, add the genuinely new tables beside it
keyed on `device.id`, and let the word `Assets` be what the interface says.
`20-asset-data-model.md` specifies exactly that delta.

## 2. B2 — reconciliation is fully built and completely inert

§8 and §9 describe discovery and reconciliation as new work.
`0012_discovery.py` built them. `discovery_candidate` carries `address`,
`protocol`, an `identity` JSONB of whatever the probe could read,
`suggested_device_type`, `suggested_vendor`, `suggested_model`, a
`matched_device_id` FK, and a `status`, with a partial unique index that stops a
nightly sweep growing a new row for the same unmanaged device every night.
`/discovery/candidates` lists them, and promote and ignore both exist. The
workflow §9 asks for is essentially implemented; what is missing is a screen.

It cannot match anything.

```
devices=664  with_serial=0  with_asset_tag=0
```

Both columns exist on `device` (`backend/app/models/inventory.py:178-179`). Both
are NULL for the entire estate, and neither carries a unique index. Serial
number is the first key in §9's own matching list. Until the simulator export
carries serials, the importer writes them, and the columns are constrained
unique-where-not-null, every discovery run produces duplicate candidates and an
operator resolves them by hand — which is the manual process the module exists
to remove.

This is cheap to fix and it gates a third of the plan. It belongs in phase two,
before anything that reads an asset identity.

## 3. B3 — assets and consumables are two different things

§6 lists "Spare Server, Spare PSU, Spare Fan, Memory Module, Transceiver,
Cable" among asset *types*. §10 then tracks the same items as *quantities* with
reorder thresholds and locations. Those are two models and the plan holds both.

The distinction is not stylistic:

| | Serialised asset | Consumable stock |
|---|---|---|
| Identity | one row per physical unit | one row per part and location |
| Location | rack and U, or room | a store, a shelf |
| Quantity | always 1 | 0..n, and it moves |
| Telemetry | yes | never |
| Lifecycle | per unit | not applicable |
| Question asked | "where is *this* one" | "do we have *any*" |

Put them in one table and every row is half NULL, `quantity` is meaningless for
the individuals, and `rack_id` is meaningless for the stock. NetBox separates
`Device` from `InventoryItem`; dcTrack separates Assets from Parts; the reason
is the same in both.

The edge case is worth deciding deliberately rather than discovering: **a spare
server has a serial number.** It should be a `device` with
`lifecycle = 'in_stock'` and no rack, not an inventory line — and a spare fan
should be an inventory line and never a device. The rule that separates them is
whether the individual is tracked or only the count.

## 4. B4 — maintenance mode must suppress alarms

§17 defines maintenance records and moves an asset into a Maintenance state. It
never says what that state does to the alarm pipeline.

In practice this is the most-used property of the state. Planned work generates
exactly the signals the alarm engine is built to escalate: a server powered down
reads as unreachable, a CRAH isolated for a filter change reads as cooling lost,
a PDU on maintenance bypass reads as redundancy lost. If those page, operators
learn to ignore the console during work windows, and the console then stops
working for the unplanned case too — which is the expensive failure, not the
noise.

The state already exists. `AdminState.MAINTENANCE` is declared in
`backend/app/models/enums.py`, `admin_state` is a column on `device`,
`interface` and `connection`, and the alarm engine does not consult it. That is
a small change with a large effect and it should ship with §17, not after it.

The design question §17 must answer, and does not: does a maintenance window
**suppress** alarms — never raised, invisible — or **shelve** them — raised,
recorded, held out of the active list and the notification path? Shelving is the
right answer for a DCIM, because the post-work question "did anything else break
while we were in there" is unanswerable if the alarms were never written, and it
is the more expensive one to build, so it needs deciding before the schema is
cut. `18-alert-taxonomy.md` already distinguishes detection from response, which
is the hook to hang it on.

## 5. B5 — the lifecycle is over-modelled and under-recorded

§11 proposes ten states. Three of them name one condition: `installed`,
`commissioned` and `operational` all mean the machine is in the rack and
working, and no operator distinguishes them a week later. `operational` is a
rename of the existing `in_service`. Two more — `ordered` and `received` — are
procurement, which is real work but belongs to a purchasing system unless this
platform is going to run purchase orders, and the plan does not say it will.

Today's enum has four: `planned`, `in_service`, `maintenance`,
`decommissioned` (`backend/app/models/enums.py`). Three additions carry their
weight, because each changes what the system does:

| State | Why it earns a row |
|---|---|
| `in_stock` | received, not placed: no rack, no polling, but it is an asset and it is findable |
| `installed` | racked and cabled, not yet in service: it should appear in elevations and *not* alarm |
| `retired` | decommissioned and disposed: leaves the estate, keeps its history and its audit trail |

Seven states, each observable and each with a distinct consequence.

The count is the smaller half of the finding. **There is no transition history
in any form.** `commissioned_at` and `decommissioned_at` are two timestamps on
`device`; they cannot answer "who moved this to maintenance on the 14th and
why", and they are overwritten on the next transition. `audit_log` would capture
it generically — it has actor, action, `target_type`/`target_id`, before and
after (`0014_audit_log.py`) — but answering "show this asset's history" from it
means scanning an append-only table with a JSONB predicate, and it will not hold
the *reason*, which is the field a change board asks for. That is a dedicated
table, specified in `20`.

## 6. B6 — capacity is measured, never committed

§13 tracks rack U used and free, power draw against rated, and cooling. It does
that well and most of it exists: `rack.u_height`, `rack.rated_power_kw`,
`rack.rated_cool_kw`, `room.design_it_kw`, `room.designed_racks`, and
`/capacity`.

What a DCIM is actually asked to do is *hold* capacity: reserve 12U and 4 kW in
rack `S-014` for a project landing in March, so that a second project planning
in February cannot claim the same rack. "Reserved" appears once in the plan, in
§10, and only for spare parts.

Without reservation, capacity is a report. Two teams read the same free-U number
and both act on it, and the conflict surfaces at install time. This is not
exotic; it is the ordinary reason the module gets bought.

It is also cheap here, because `planned` already exists as a lifecycle state and
a planned device with `rack_id` and `u_start` set is *already* excluded from the
U-range by `device_u_no_overlap`. A reservation that is not yet a device — a
project holding raw U and kW — needs its own row, and `20` specifies it.

## 7. B7 — structured cabling is absent

Patch panel appears once in §6, as an asset type. Cable identity does not appear
at all: no cable ID, no type, no length, no path through patch panels.

Answering "which cable do I pull" is a core DCIM function, and the port-to-port
`connection` rows the platform holds today are the *logical* link, not the
physical run. A production link from a server NIC to a ToR usually crosses two
patch panels; pulling the wrong one takes down a neighbour.

`connection` is close to able to carry it — it already terminates
polymorphically and holds `link_type` and `attributes` — but a patch path is a
*chain* of physical segments under one logical link, and that is a modelling
decision, not a column. It should be decided deliberately and it can be
deferred; it should not be discovered late. `20` sketches the shape and `23`
puts it last.

## 8. B8 — tenancy is not a custom field

§16 lists `tenant` among suggested custom metadata fields, beside `cost centre`
and `project`. Cost centre and project are metadata. Tenancy is not.

If this platform ever serves colocation, internal chargeback, or any case where
one group must not see another's assets, tenancy is an access boundary on every
read path and a billing dimension on every power query. Retrofitting it across
33 tables and 68 endpoints after the fact is the kind of change that stalls a
roadmap for a quarter.

The plan should say explicitly whether it is in scope. If yes, it is a column
with a foreign key and a filter in the repository layer from day one. If no, it
should not appear as a field at all, because a free-text `tenant` attribute will
be populated by someone and then relied on.

## 9. B9 — bulk operations need a contract, not a checkbox

§15 proposes bulk edit, bulk move, bulk decommission and CSV import/export.
Against this schema, half of those will be rejected at the storage layer and the
plan does not say what happens then.

A bulk move of twelve devices into one rack hits `device_u_no_overlap` on the
third. A bulk edit that sets `mgmt_ip` hits `ix_device_mgmt_ip_live`, which is
unique among non-decommissioned devices. A CSV import of 400 rows with two bad
ones has to decide, before it is written, whether it is all-or-nothing or
partial-with-a-report.

Three things have to be specified before this is built: the transaction boundary
(one per batch, or one per row), the failure report (which row, which
constraint, in words an operator can act on), and the audit granularity — a bulk
decommission of 40 devices is 40 audit rows or it is not an audit trail. The
existing `backend/app/core/audit.py` records one action per call and scrubs
credentials on the way in; the bulk path must not become the one place that
writes a single summary row.

## 10. B10 and B11 — navigation and scale

§3 and §22 propose an asset landing page, room and rack navigation, and a device
detail view. `/assets` already routes (`frontend/src/App.tsx:200`), as do
`/racks`, `/racks/:id` (rack elevation), `/floorplan`, `/devices` and
`/devices/:id`. The backend behind them exists and needs nothing new:
`/estate/rooms/{id}/kpi`, `/rooms/{id}/rows`, `/rooms/{id}/floorplan`,
`/racks/{id}/elevation`.

The module is nonetheless scoped to build its **own** screens for these, under
`/assets`, rather than reusing the operational ones — a boundary set
deliberately so that Home, Thermal, Power, Utilization, Connectivity, Devices
and the device detail page cannot regress while the asset module is built.
`22-asset-frontend-spec.md` §1 states the rule and what it costs. The finding
stands regardless: this is a frontend pass over endpoints that already return
the data, not a platform build, and it is the cheapest visible win in the plan.

§26 sizes the module for 40,000 assets with denormalised counts and materialised
views. The estate is 664 devices across 2 datacenters. `ix_device_name_trgm`
already backs substring search and the list endpoint already pages by cursor.
Optimising now means optimising against a guess about which query is slow, and
the module's real query shapes are not written yet. Build the queries, measure
them, then denormalise the ones that need it.

## 11. Section scorecard

Verdicts are against what runs now. *Partial* generally means the data exists
and the screen does not, which is weeks of frontend rather than months of
platform.

| § | Section | Verdict | Where it already lives |
|---|---|---|---|
| 1 | Core objectives | Partial | 7 of 10 questions answerable from existing endpoints |
| 2 | Physical hierarchy | **Built** | `datacenter` → `room` → `rack_row` → `rack` → `device` |
| 3 | Landing page | Partial | `/estate/*`, `/datacenters`, `/rooms`; no landing screen |
| 4 | Room view | **Built** | `/estate/rooms/{id}/kpi`, `/rooms/{id}/rows`, `/floorplan` |
| 5 | Rack management | **Built** | `/racks/{id}/elevation`, `RackElevationView` |
| 6 | Classification | **Built** | `device_type(code, display_name, category, is_rack_mounted, icon)` |
| 7 | Asset detail | Partial | 7 of 13 proposed tabs ship today |
| 8 | Discovery | **Built** | `discovery_run`, `discovery_candidate`, promote/ignore |
| 9 | Reconciliation | **Inert** | schema ready; 0 of 664 devices carry a serial (B2) |
| 10 | Spare parts | **New** | nothing equivalent — and correctly separate (B3) |
| 11 | Lifecycle | Partial | 4 states of 10; no transition history (B5) |
| 12 | Relationships | **Built** | `connection` × 5 layers, `/power/chain`, `/topology/impact` |
| 13 | Capacity | Partial | `/capacity` and rated columns; no reservation (B6) |
| 14 | Search & filter | Partial | cursor pagination + `ix_device_name_trgm` |
| 15 | Bulk operations | **New** | needs a contract first (B9) |
| 16 | Tags & metadata | Partial | `attributes` JSONB on device, rack, room; no tag model |
| 17 | Maintenance | **New** | state exists, nothing reads it (B4) |
| 18 | Warranty & contracts | **New** | nothing equivalent |
| 19 | Audit | **Built** | `audit_log` with actor, action, target, before/after, scrubbing |
| 20 | Status model | **Built** | `lifecycle` / `admin_state` / `device_state` already separated |
| 21 | Database schema | **Harmful** | duplicates seven live tables (B1) |
| 22 | UI navigation | Partial | `/assets` already resolves (B10) |
| 23 | UX principles | Sound | matches the existing interface |
| 24 | Layer separation | **Built** | this is already the architecture |
| 25 | Future integration | **Built** | `/topology/impact/{device_id}` ships today |
| 26 | Scalability | Premature | correct shape, wrong estate size (B11) |
| 27 | Final UX | Partial | achievable on existing endpoints once §3 and §22 land |

## 12. What the plan gets right

§20 and §24 are the strongest parts of the document, and they describe how this
platform is already built: health, administrative state and lifecycle held
apart, and inventory, telemetry and topology kept as separate layers over one
identity. §23's UX principles are sound and consistent with the existing
interface. The domain coverage is close to complete — apart from cabling (B7)
and reservation (B6), almost nothing a DCIM needs is missing from the list.

The gap is situational. The plan is written as if for an empty repository, and
the repository is not empty. Read against the running system it is less a build
order than a good audit of what is finished, what is exposed, and what was never
started.

`20-asset-data-model.md` specifies the schema delta, `21` the API, `22` the
frontend, and `23` the order to build them in.
