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

Status: **code built 2026-09-02, not yet applied.** 952 backend tests pass (+25),
`ruff` clean, `tsc` and production build clean. Migrations `0045` and `0046`
proved against a throwaway database - up, shelving behaves, down to base - and
**deliberately not run against the live one.**

Shelving is verified end to end rather than asserted: with an alarm standing on
a CRAH and a window opened over it, the device's `max_severity` went
`CRITICAL -> CLEAR`, a second CRAH outside the window stayed `CRITICAL`, the
alarm list showed 1 of 2 with the shelved row still retrievable, the summary
moved `active 2 -> 1` with `shelved=1`, and completing the window put severity
and the list back.

Two design points worth recording, because both were decided rather than
inherited:

**One choke point, not ten.** Every rack, room, topology and power roll-up reads
`device_state`, which `refresh_device_alarm_state` derives from open alarms. The
shelve predicate goes there, so all of them follow and none can be forgotten.
The direct alarm counters in `estate.py` and `sites.py` needed it too - six
sites in total, and a sweep found one more in `sites.py` than the first pass
did, which is why `test_every_open_alarm_query_excludes_shelved` now walks every
repository and fails on any open-alarm query lacking the clause or a written
`shelve-exempt` reason.

**Stamped at raise time, inside the INSERT.** Six detectors raise alarms and a
seventh will be written by somebody who has never read migration 0046, so the
window lookup is a correlated subquery in `raise_alarm` rather than a thing each
caller remembers. The `ON CONFLICT` branch deliberately leaves the mark alone -
an alarm re-raised each poll would otherwise un-shelve itself within seconds.

`maintenance_window.status` is a column advanced by the ingest worker on a
30-second tick, not a comparison against `now()` evaluated per process: the
worker and the API must agree about whether a window is running, and two clocks
at the boundary do not.

~~Still owed: the create-window form.~~ **Closed by the write-UI pass,
2026-09-03.** Scheduling is a three-step dialog whose middle step calls
`/maintenance/windows/preview` live as devices are selected, and start, end and
cancel are on the window's own page.



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

Status: **code built 2026-09-03, not yet applied.** 972 backend tests pass
(+20), `ruff` clean, `tsc` and production build clean. Migration `0047` proved
against a throwaway database - up, cache behaves, down to base - and
**deliberately not run against the live one.**

Built under phase 0's documented defaults, neither of which has been overruled:
**tenancy is out of scope** (so it is not a column and not a tag either), and
**documents are deferred** pending an object-store decision, so `asset_document`
does not exist. Contracts, suppliers and tags are in.

**A contradiction in this document set was found and settled.** `20` §2 said
`warranty_expires` held the *earliest* active covering expiry and §5 said
`MAX(end_date)`. `MAX` is correct: with cover to 2027 and to 2029 the device is
covered until 2029, and the earliest date is when the *first* contract lapses -
a different question, and not the one an asset list asks. §2, migration `0044`'s
comment and the ORM docstring all inherited the wrong wording and are corrected.

The cache has exactly one writer, and that is why `services/contracts.py` exists
rather than the router calling the repository directly. Four paths change cover
- covering a device, uncovering one, moving a contract's dates, deleting a
contract - and each recomputes in the same transaction. Verified end to end:

```
two contracts        -> covered until 2029-02-19 (the LATER of the two)
future contract      -> unchanged; it has not started
dropped the long one -> 2026-10-03
extended the other   -> 2027-03-22
deleted it           -> None
```

A contract that has not started is not cover. Reporting it as such is how a
machine goes to site believing it has support it cannot yet claim.

The 90-day "expiring" threshold is defined once, on the server, and served to
the UI - the tile, the filter and the asset record read the same number rather
than each hard-coding one.

~~Still owed: no create or edit forms for contracts, suppliers or tags.~~
**Closed by the write-UI pass, 2026-09-03.** Documents remain blocked on
phase 0.


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

## Write-UI pass — closing the forms owed by phases 3 and 4

Status: **built 2026-09-03.** No backend change: every endpoint behind these
already existed and had shipped without a form, which had left the module
read-only for everything except the lifecycle transition. `tsc` and the
production build are clean; the 972 backend tests are untouched because nothing
under `backend/` moved.

Six flows, all inside `/assets`: schedule a window (three steps), start/end/
cancel one, record a contract, add a supplier, record work done on an asset, and
create, edit, delete and attach tags.

**The window form is three steps because step two is the point.** A window
scoped too widely is otherwise discovered at 02:00, having silenced a rack
nobody was working on. Selecting devices calls
`POST /maintenance/windows/preview` live and reports what the selection would
actually cover - how many machines go dark downstream, how many are already
alarming, and which of them are not redundantly fed. Taking a feeder into a
window costs its loads their power, not just their alarms, and that sentence
belongs before the commit rather than after.

Two smaller decisions worth recording:

**`ApiError` now keeps the structured body.** Several endpoints refuse with a
shape rather than a string - an illegal lifecycle transition carries its allowed
set, a rack collision names the occupying device. `String(error)` on those
rendered `[object Object]`, so a form told the operator nothing. The sentence is
pulled out for `message`; the object stays on `detail` for forms that want to do
better. Pydantic validation arrays are flattened to `field: reason` rather than
JSON.

**Selection survives a filter change in the device picker.** An operator narrows
to one rack, ticks four things, narrows to another, ticks three more. "Select
all" adds or removes only what is currently shown, so it cannot silently discard
the work that came before it.

Still not built: editing a scheduled window (no `PATCH /maintenance/windows/{id}`
exists), and editing a contract's fields after creation - `PATCH /contracts/{id}`
exists but has no form.

## Phase 5 — parts and reservations

Status: **built 2026-09-03.** 991 backend tests pass (+19), `ruff` clean, `tsc`
and production build clean. Migrations `0048` and `0049` proved against a
throwaway database - up, ledger and U-enforcement behave, down to base - and
**deliberately not run against the live one**, which sits at `0047`.

Two bugs the scratch run caught, both invisible to a unit test:

**PostgreSQL evaluates a CHECK against the row an INSERT proposes, before it
resolves `ON CONFLICT`.** So `INSERT ... VALUES (part, store, -3) ON CONFLICT DO
UPDATE SET on_hand = on_hand + EXCLUDED.on_hand` trips `on_hand >= 0` even
though the update branch would have landed on 7 - a constraint violation on a
movement that is entirely legal. The row is now ensured at zero with
`DO NOTHING` and the delta applied by a separate `UPDATE`, so the check runs
once, against the value that actually results.

**`:attrs::jsonb` and `:u_start + :u_height`** both failed - the first because
SQLAlchemy's `text()` reads `::` as part of the bind name, the second because
Postgres cannot resolve `unknown + unknown` from two untyped parameters. Both
are now explicit `CAST(...)`, which is the form the rest of this codebase
already uses for exactly these reasons.

Verified end to end:

```
receipt 10, consumed 3   -> on_hand 7
overdraw                 -> refused: "750W-PSU: asked for 99, only 7 on hand"
adjustment with note     -> on_hand 6;  ledger vs balance: no discrepancies
adjustment without note  -> refused
reservation U20-23       -> held, backed by a planned device
overlapping reservation  -> refused: "U22-U23 is occupied by RESERVED-march-build-U20"
fulfil                   -> promoted in place; the units are never vacated
expired hold             -> released, placeholder removed
```

`on_hand` has no setter anywhere - not in the repository, not in the API, not in
the UI. Correcting a miscount is an `adjustment` carrying a note, which the
schema requires. `/stock/reconcile` exists to prove the running total and the
ledger agree; a non-empty result means something wrote `part_stock` outside
`move()`, which is the failure the design exists to prevent.

Rack units are enforced by `device_u_no_overlap` and nothing else. A reservation
naming a U range inserts a `planned` device to stand in for it, so the overlap
is refused by the constraint that already works - no cross-table trigger, no
locking to get wrong, and the elevation renders the hold for free. Fulfilling
promotes that row rather than replacing it, so the range is never briefly free
for somebody else's install to slip into.


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

Status: **built 2026-09-03.** 1016 backend tests pass (+25), `ruff` clean, `tsc`
and production build clean. No migration - bulk operates on tables that already
exist.

All four exit criteria verified against a real database:

```
12 moves, the 3rd colliding -> 11 succeeded, 1 refused:
        "SRV-02: rack_unit_occupied - U5 is occupied by SRV-BLOCKER"
the same batch, atomic:true  -> 0 succeeded, 0 landed in the target rack
decommission 40              -> 40 audit rows AND 40 lifecycle events
illegal transition           -> "illegal_transition - decommissioned cannot go
                                 to in_service; allowed: retired, in_stock"
```

**Two defects the verification run found.**

*An illegal transition reported the key `rejected`.* The translator matched on
substrings only, so an application refusal that already carries a good sentence
fell through to the generic branch - and logged itself as an untranslated
surprise. Typed refusals (`IllegalTransitionError`,
`InsufficientStockError`, `ReservationConflictError`) are now matched by class,
*before* the string table, and keep both their stable key and their own message.

*`python-multipart` was missing.* The CSV endpoint uses `File`/`Form`, and
FastAPI raises at request time without it - so the endpoint would have 500ed in
production and CI would have failed on import. Added as a runtime dependency,
not a dev one.

**The import is two-phase and stateless.** `validate` writes nothing and returns
a digest of the bytes it read; `apply` requires that digest back. A file edited
between the two phases no longer matches and is refused. That is tamper-evident
and, unlike a server-side job, cannot expire under somebody reviewing a long
report. The dry run also says which key matched each row, so a row landing on a
device by *name* when serial was expected is visible before it lands.

**What bulk deliberately cannot do.** `/fields` edits ownership and purchase
columns only. Placement goes through `/move`, which has to reason about rack
units, and state goes through `/lifecycle`, which has a matrix - letting a
generic field setter write `rack_id` or `lifecycle` would route around both. The
CSV importer may set an `asset_tag` and never a `serial_number`: a tag is a
sticker somebody applied, a serial is what the hardware reports, and a
spreadsheet should not be able to contradict the machine.


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
