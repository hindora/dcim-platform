# 23. Asset & inventory — implementation plan

Status: **plan. Nothing started.** Follows `19` (review), `20` (data model),
`21` (API), `22` (frontend).

Phases are ordered by value per week, not by the plan's section numbers. Each
phase is shippable on its own and each has an exit criterion that can be
checked, not asserted.

## Standing constraints

These hold for every phase and are the first thing a review checks.

1. **`/assets` only.** No page outside the module changes. The single permitted
   edit outside `features/assets/` is repointing `/assets` in `App.tsx`
   (`22` §1).
2. **No `assets` table.** `device` is the asset (`19` B1, `20` §1).
3. **Additive API only.** No existing endpoint changes shape (`21`).
4. **Additive, namespaced CSS.** `.asset-` prefix; no existing selector edited.
5. **Three CI gates before every backend push**: `pytest`, `ruff check app tests`,
   and `tsc` for frontend. `ruff` is the one that has bitten this repo before.
6. **The user commits.** Phases end with a working tree, not a commit.

## Phase 0 — decide two things

Neither is code and both block schema.

| Decision | Options | Default if nobody decides |
|---|---|---|
| **Tenancy in scope?** (`19` B8) | column + repository filter from day one / explicitly out | **Out.** And then `tenant` must not exist as an attribute or a tag either |
| **Document storage** (`20` §10) | object store (MinIO/S3) / defer documents entirely | **Defer.** No blobs in PostgreSQL |

Exit: both written down in this file. Tenancy is cheap now and expensive in
phase 4.

## Phase 1 — the workspace, on what already exists

Status: **built 2026-09-02.** 920 backend tests pass (11 new), `ruff` clean,
`tsc` clean, production build clean. Not yet deployed — the WSL runtime still
runs the previous commit. Delivered: overview, inventory with URL-backed
filters, asset record (Overview / Placement / Connections), estate drill-down,
asset-context elevation with contiguous-gap call-outs, and the discovery queue.
Deferred within the phase: bulk selection and the column chooser, which belong
with phase 6's write path.

**No migrations. No new tables. No backend beyond one endpoint.**

The whole of `22` §2–§6 rendered against endpoints that ship today: overview,
inventory table with filters and URL state, asset record (Overview, Placement,
Connections tabs only), estate drill-down, asset-context rack elevation.

Backend: `GET /assets/summary` (`21` §3), plus the additive filter parameters
and row fields on `GET /devices` (`21` §2). Both are reads.

This is the largest visible change in the plan and the cheapest. It also puts
the `Unidentified: 664` tile in front of a person, which is what makes phase 2
obviously necessary rather than an argument in a document.

**Exit criteria**

- `/assets` renders the workspace; `/devices`, `/devices/:id`, `/racks/:id`,
  `/`, `/thermal`, `/power`, `/utilization` render identically to before —
  verified by diffing the route components, not by eyeballing.
- Every inventory filter round-trips through the URL: paste a filtered link into
  a new tab, get the same rows.
- The elevation at `/assets/estate/racks/:id` shows free-U call-outs and
  lifecycle shading, and reads only `/racks/{id}/elevation`.
- `git diff --stat` touches nothing outside `features/assets/`, `App.tsx` (one
  line), `index.css` (additive `.asset-` block), and the backend additions.

## Phase 2 — identity

Status: **code built 2026-09-02, not yet applied.** 927 backend tests pass,
`ruff` clean, build clean; the simulator suite passes with 16 new tests. The
migrations have been proved against a throwaway database and **deliberately not
run against the live one** — that, and the re-import, are deployment steps the
operator takes.

A defect was found and fixed on the way through, and it is the reason this
phase was more than a column. **The simulator's planes disagreed about serial
numbers.** SNMP served `sha1(id)[:7]` through `entPhysicalSerialNum` while
Redfish served `SN-<id[:8]>` on both `ComputerSystem` and `Chassis` — two
strings for one chassis. A collector polling both planes would have read two
identities off one server and had every reason to file it twice, which is the
exact duplicate-asset failure §9's reconciliation exists to prevent. Real
hardware burns one serial in at manufacture and reports it identically over
Redfish, IPMI FRU and ENTITY-MIB. `core.device_manager.device_serial()` is now
the single source, every plane reads it, and the serial is materialised onto the
`Device` dataclass so the topology export carries it.

The formats are vendor-shaped rather than one flat hash — Dell 7-char service
tag, HPE `SGH421X9KL`, Cisco `FOC` + year + week + sequence, APC 12 — because
vendor tooling matches on the shape, and a simulator that emits one shape for
everything means that parsing is never exercised until real gear arrives. The
charset excludes `I`, `O`, `0` and `1` the way real service tags do: the string
gets read off a sticker by a person under a rack.

Measured on the real topology: **664 devices, 664 distinct serials, 0
collisions**, stable across an export/import/export round trip.

The gate. `19` B2: 664 devices, 0 serials, 0 asset tags, no unique index.
Everything after this phase reads an asset identity, so it comes second.

1. Simulator export emits a **stable** synthetic `serial_number` per device —
   stable because reconciliation is only testable if the same device produces
   the same serial across exports.
2. `backend/app/importer/simulator.py` maps it, idempotently on `external_id`.
3. Run the import. Check for duplicate serials **before** migrating.
4. Migration `0044` adds the `device` columns and the two partial unique indexes.

Then the discovery queue (`22` §7), which is a screen over a subsystem that
already works — including the banner that admits matching is IP-only until
serials land.

**Exit criteria**

- `SELECT count(*) FROM device WHERE serial_number IS NULL` returns 0.
- `ix_device_serial_unique` and `ix_device_asset_tag_unique` exist and the index
  build succeeded on real data.
- The overview's `Unidentified` tile reads 0 and the tile stops being a link.
- A discovery run against the live plane produces candidates whose
  `matched_device_id` is populated by serial, not by IP — verified by nulling a
  candidate's address and re-matching.

## Phase 3 — lifecycle, and making maintenance mean something

Two migrations that must not share a transaction (`20` §3): `0043` adds the
three enum labels and nothing else; `0045` adds `device_lifecycle_event` and
backfills from `commissioned_at` / `decommissioned_at`.

Then `0047`: maintenance windows, targets, records, and
`alarm.shelved_by_window`.

The part that carries the value is not the tables. It is **shelving**
(`20` §6): an alarm on a device inside an active window is raised, stored,
marked, and excluded from the active list, the roll-ups and notifications.

**The roll-up exclusion is the step most likely to be missed.** Rack and room
severity roll-ups and every counter in `18-alert-taxonomy.md` need
`WHERE shelved_by_window IS NULL`, or a room turns red for a planned filter
change and the whole feature is worse than not having it.

**Exit criteria**

- A window over one CRAH, started, with the device faulted in the simulator:
  the alarm exists in the database with `shelved_by_window` set, does **not**
  appear in `/alarms` active, does **not** raise the room's roll-up severity,
  and **does** appear in the window's `shelved_alarms` count. Verified live
  against the running plane, not only in a fixture — this platform has a
  documented history of fixture-green, live-broken (`17-operations-runbook.md`).
- Completing the window un-shelves correctly: still-active alarms return to the
  active list; cleared ones do not resurrect.
- `POST /devices/{id}/lifecycle` writes both a `device_lifecycle_event` and an
  `audit_log` row, and an illegal transition returns `409` with the allowed set.
- Preview (`21` §6) reports downstream devices and redundancy warnings using the
  existing `/topology/impact` and `/power/chain` — no new traversal code.

## Phase 4 — support, tags, documents

Pure CRUD, no telemetry coupling, no alarm coupling. The most parallelisable
phase and the least risky.

Migrations `0046` (supplier, contract, `device_support`) and `0048` (tags).
`asset_document` only if phase 0 chose an object store.

The one non-obvious rule: `device.warranty_expires` is a cache, and the contract
link is the truth. Only the recompute writes it (`20` §5).

**Exit criteria**

- Adding a contract covering 200 devices updates `warranty_expires` on all 200
  in the same transaction; changing the contract's `end_date` updates them
  again; removing the link recomputes to the next-best contract or NULL.
- The overview's warranty tile, the inventory `warranty_state` filter, and the
  asset record's countdown all derive from the same server-side function — no
  90-day threshold defined in the frontend.
- A tag renamed in `/assets/admin/tags` renames everywhere; deleting a tag
  detaches it and does not delete objects.

## Phase 5 — parts and reservations

The two genuinely new subsystems. Migrations `0049` (part, store, `part_stock`,
`stock_movement`) and `0050` (`capacity_reservation`).

Parts carry one rule worth defending in review: **no endpoint sets `on_hand`.**
Every change is a movement and `on_hand` is the running total (`21` §7). The
first request after this ships will be for an editable quantity field; the
answer is an adjustment movement with a required note.

Reservations use the backing-`planned`-device approach (`20` §9 option 2), so
`device_u_no_overlap` enforces U non-overlap with no new constraint and no
cross-table trigger.

**Exit criteria**

- Consuming a part on a maintenance record posts the movement in the same
  transaction and fails the whole record when stock is insufficient.
- `SUM(delta)` over `stock_movement` equals `on_hand` for every
  `(part, store)` — a check that runs in CI against a seeded fixture.
- Two reservations for overlapping U in one rack: the second returns `409`
  `reservation_conflict` with a message naming the occupier, not a constraint
  name.
- Every reservation has an `expires_at`, and expired ones surface at the top of
  the list.

## Phase 6 — bulk operations

Deliberately last. Bulk is the sharpest tool in the module and it should be
built when the constraints it will hit are all in place — otherwise the failure
translation table (`21` §11) is written twice.

The three properties from `21` §10 are the acceptance criteria, not
implementation notes: per-row transactions, a row-level report, one audit row
per device.

**Exit criteria**

- A bulk move of 12 devices where the 3rd collides: 11 succeed, 1 is reported by
  name with `rack_unit_occupied` and a message naming the occupying device.
- `"atomic": true` on the same batch: 0 succeed, nothing is written.
- A bulk decommission of 40 devices writes 40 `audit_log` rows and 40
  `device_lifecycle_event` rows.
- CSV import is two-phase; the dry run states which key matched each row
  (`external_id` → `serial_number` → `asset_tag` → `name`) and writes nothing.

## Not scheduled

**Structured cabling** (`19` B7, `20` §11). Blocked on a data problem before a
schema one: patch panels are not modelled as port-bearing devices, and a patch
path is meaningless without them. The route in is a `patch_panel` device type
with `interface` rows for its ports — which the schema already supports.

**Denormalised counts and materialised views** (`19` B11). 664 devices. Build
the queries, measure them, denormalise what is actually slow.

**Procurement states** `ordered` / `received` (`19` B5). Belongs to a purchasing
system. Revisit only if this platform is asked to run purchase orders.

## Risk register

| Risk | Phase | Mitigation |
|---|---|---|
| Unique index build fails on duplicate serials | 2 | import and check duplicates before migrating; a loud failure is the design |
| `ALTER TYPE ADD VALUE` used in its own transaction | 3 | `0043` adds labels and does nothing else (`20` §3) |
| Shelved alarms leak into roll-ups | 3 | live verification against the running plane, not fixtures alone |
| A "small prop" added to a shared component | all | fork into `features/assets/components/`; the diff makes it visible |
| CRLF churn on edited files | all | check `git diff` against `git diff -w` before every push |
| Tenancy retrofitted after phase 4 | 0 | decide in phase 0 |
