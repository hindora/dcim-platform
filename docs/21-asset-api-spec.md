# 21. Asset & inventory — API specification

Status: **specification. Not built.** Extends `10-api-spec.md`; everything here
is additive and lives under the same `/api/v1` prefix, the same bearer auth and
the same cursor-pagination envelope.

Two rules govern this document.

**No existing endpoint changes shape.** The asset module is a consumer of the
platform, not a rewrite of it. Every endpoint listed as *existing* below is
called exactly as it is called today, with the same response schema. If the
asset UI needs a field that an existing endpoint does not return, the field is
added as an **optional** key, or a new endpoint is added beside it — never a
changed or removed key, because `/devices` and `/devices/{id}` back the Devices
pages, which are out of scope (see `22-asset-frontend-spec.md` §1).

**`device` is the asset** (`19` B1). There is no `/assets/{id}` resource
returning a different object than `/devices/{id}`. Asset-specific data hangs off
the device as sub-resources.

## 1. What already exists and is reused unchanged

These serve the asset module with no backend work at all.

| Method | Path | Serves |
|---|---|---|
| GET | `/datacenters` | estate tree, top level |
| GET | `/rooms`, `/rooms/{id}` | room list and detail |
| GET | `/rooms/{id}/rows` | rows within a room |
| GET | `/rooms/{id}/floorplan` | floor plan geometry |
| GET | `/estate/rooms/{id}/kpi` | room KPI block |
| GET | `/racks`, `/racks/{id}` | rack list and detail |
| GET | `/racks/{id}/elevation` | U-by-U rack contents |
| GET | `/devices` | list, cursor-paged, filterable |
| GET | `/devices/{id}` | device detail incl. nameplate |
| GET | `/devices/{id}/interfaces` | network ports |
| GET | `/devices/{id}/power-supplies` | PSUs and their feeds |
| GET | `/devices/{id}/history` | telemetry series |
| GET | `/capacity` | estate capacity roll-up |
| GET | `/power/chain/{device_id}` | upstream power path |
| GET | `/topology/impact/{device_id}` | downstream impact |
| GET | `/discovery/candidates` | discovery queue |
| POST | `/discovery/candidates/{id}/promote` | promote to device |
| POST | `/discovery/candidates/{id}/ignore` | dismiss |
| GET | `/alarms` | filterable by `device_id` |

## 2. Additions to existing responses

Additive optional keys only. Consumers that ignore them are unaffected, which
is what keeps the Devices pages out of scope.

### `GET /devices/{id}`

Gains an `asset` block, present only when at least one field is populated:

```json
{
  "id": "…", "name": "…", "device_type": "server",
  "asset": {
    "asset_tag": "DC1-A-00412",
    "serial_number": "SGH421X9KL",
    "supplier": { "id": "…", "name": "Insight" },
    "purchase_date": "2024-03-11",
    "purchase_order": "PO-88213",
    "purchase_cost": 8420.00,
    "currency": "USD",
    "install_date": "2024-04-02",
    "warranty_expires": "2027-03-10",
    "warranty_state": "active",
    "eol_date": null,
    "eos_date": "2029-03-10",
    "owner_group": "platform-eng",
    "cost_centre": "CC-4021",
    "tags": [{ "key": "env", "value": "prod", "colour": "#3f9e5a" }]
  }
}
```

`warranty_state` is derived, not stored: `active` | `expiring` (within 90 days)
| `expired` | `unknown`. Derived server-side so the list, the detail page and
the KPI tile cannot disagree about the threshold.

### `GET /devices`

Gains optional query parameters. All are AND-combined with the existing filters.

| Parameter | Type | Notes |
|---|---|---|
| `lifecycle` | repeated enum | `planned`,`in_stock`,`installed`,`in_service`,`maintenance`,`decommissioned`,`retired` |
| `asset_tag` | string | exact match |
| `serial_number` | string | exact match |
| `has_serial` | bool | the B2 gap, made visible: `false` lists what needs reconciling |
| `warranty_state` | enum | `active` \| `expiring` \| `expired` \| `unknown` |
| `warranty_before` | date | expiry earlier than |
| `supplier_id` | uuid | |
| `owner_group` | string | |
| `cost_centre` | string | |
| `tag` | repeated `key:value` | `tag=env:prod&tag=tier:1` — AND within the filter |
| `in_maintenance` | bool | inside an active window, or `admin_state=maintenance` |
| `q` | string | trigram over name, asset tag, serial — backed by `ix_device_name_trgm` |

Each returned row gains `asset_tag`, `serial_number`, `warranty_state` and
`lifecycle` so the asset table renders without an N+1.

> **Default scope.** `/devices` continues to return every lifecycle state by
> default so the Devices pages are unchanged. The asset UI passes an explicit
> `lifecycle` filter. Anything that reports a *count* of the estate excludes
> `planned`, `decommissioned` and `retired` — see `20` §9.

## 3. Asset landing

```
GET /assets/summary
```

One call for the whole landing page — the counts, not the rows.

```json
{
  "totals": {
    "assets": 664, "in_service": 651, "installed": 4, "planned": 6,
    "in_stock": 0, "maintenance": 3, "decommissioned": 0, "retired": 0
  },
  "identity": {
    "with_asset_tag": 0, "with_serial": 0, "unidentified": 664
  },
  "warranty": {
    "active": 0, "expiring_90d": 0, "expired": 0, "unknown": 664
  },
  "discovery": { "new_candidates": 0, "unmatched": 0 },
  "maintenance": { "active_windows": 0, "devices_in_window": 0 },
  "stock": { "parts_below_reorder": 0 },
  "estate": {
    "datacenters": 2, "rooms": 16, "racks": 44,
    "u_total": 1848, "u_used": 0, "u_reserved": 0
  },
  "by_category": [
    { "category": "compute", "count": 480 },
    { "category": "network", "count": 96 }
  ]
}
```

`identity.unidentified` is on the landing page deliberately. It reads 664 today,
which is `19` B2 stated as a number an operator sees rather than a finding in a
document.

## 4. Lifecycle

```
GET   /devices/{id}/lifecycle          → transition history, newest first
POST  /devices/{id}/lifecycle          → record a transition
```

```json
POST /devices/{id}/lifecycle
{ "to_state": "maintenance", "reason": "PSU swap, both feeds", "change_ref": "CHG-4471" }
```

Writes a `device_lifecycle_event` row, updates `device.lifecycle`, and writes an
`audit_log` row. All three in one transaction; if the audit write fails it is
logged and the transition still commits, per `backend/app/core/audit.py`.

Illegal transitions are rejected `409` with the allowed set in the body. The
matrix is small enough to state:

| From | May go to |
|---|---|
| `planned` | `in_stock`, `installed`, `decommissioned` |
| `in_stock` | `installed`, `planned`, `retired` |
| `installed` | `in_service`, `in_stock`, `decommissioned` |
| `in_service` | `maintenance`, `decommissioned` |
| `maintenance` | `in_service`, `decommissioned` |
| `decommissioned` | `retired`, `in_stock` (returned to spares) |
| `retired` | — terminal |

`decommissioned → in_stock` is not a mistake: a machine pulled from service and
kept as a spare is the ordinary path, and forbidding it makes operators create a
duplicate record.

## 5. Suppliers and contracts

```
GET    /suppliers
POST   /suppliers
GET    /suppliers/{id}
PATCH  /suppliers/{id}
DELETE /suppliers/{id}                 → 409 if referenced

GET    /contracts                       ?kind= &supplier_id= &expiring_days= &status=
POST   /contracts
GET    /contracts/{id}                  → includes covered device count
PATCH  /contracts/{id}
DELETE /contracts/{id}

GET    /contracts/{id}/devices          → covered devices, cursor-paged
POST   /contracts/{id}/devices          { "device_ids": [...] }
DELETE /contracts/{id}/devices/{device_id}

GET    /devices/{id}/contracts          → contracts covering this device
```

Any write to `contracts/{id}/devices` or to a contract's dates recomputes
`device.warranty_expires` for the affected devices, in the same transaction.
That recompute is the only writer of the column (`20` §5).

## 6. Maintenance

```
GET    /maintenance/windows             ?status= &from= &to= &device_id=
POST   /maintenance/windows
GET    /maintenance/windows/{id}        → targets + shelved alarm count
PATCH  /maintenance/windows/{id}
POST   /maintenance/windows/{id}/start      → status: active   (early start)
POST   /maintenance/windows/{id}/complete   → status: completed
POST   /maintenance/windows/{id}/cancel     → status: cancelled

POST   /maintenance/windows/{id}/targets    { "device_ids": [...] }
DELETE /maintenance/windows/{id}/targets/{device_id}

GET    /devices/{id}/maintenance        → records, newest first
POST   /devices/{id}/maintenance        → record work done
```

`GET /maintenance/windows/{id}` returns `shelved_alarms`, the count of alarms
suppressed from the active list by this window. It is the number that tells an
operator the window was scoped too widely, and it is also how the post-work
question — *did anything else break* — gets answered.

**Preview before you commit.** A window that shelves the wrong things is
discovered at 02:00 otherwise:

```
POST /maintenance/windows/preview
{ "device_ids": [...], "starts_at": "…", "ends_at": "…" }
→ { "devices": 12, "downstream_devices": 47, "alarms_currently_active": 2,
    "redundancy_warnings": [ { "device_id": "…", "reason": "single-corded load" } ] }
```

`downstream_devices` and `redundancy_warnings` come from the existing
`/topology/impact/{device_id}` and `/power/chain/{device_id}` — no new
traversal code.

## 7. Parts and stock

```
GET    /parts                           ?category= &vendor_id= &below_reorder= &q=
POST   /parts
GET    /parts/{id}                      → includes stock across all stores
PATCH  /parts/{id}
DELETE /parts/{id}                      → 409 if stock or movements exist

GET    /stores
POST   /stores
GET    /stores/{id}                     → stock lines held here

GET    /parts/{id}/stock                → per-store on_hand / reserved
POST   /parts/{id}/movements            → the only way stock changes
GET    /parts/{id}/movements            ?from= &to= &reason=
```

```json
POST /parts/{id}/movements
{ "store_id": "…", "delta": -1, "reason": "consumed",
  "device_id": "…", "record_id": "…", "note": "PSU 2 replaced" }
```

**There is no endpoint that sets `on_hand` directly.** Every change is a
movement, and `on_hand` is the running total. A stock count that disagrees with
reality is corrected by posting an `adjustment` movement with a note, which
leaves a record of the correction — the thing an inventory system exists to
have.

Consuming a part from a maintenance record posts the movement automatically:
`POST /devices/{id}/maintenance` with `parts_used` writes one `stock_movement`
per line, in the same transaction, and fails the whole record if stock is
insufficient.

## 8. Tags

```
GET    /tags                            → all defined tags with usage counts
POST   /tags                            { "key": "env", "value": "prod", "colour": "…" }
PATCH  /tags/{id}
DELETE /tags/{id}                       → detaches from all objects

GET    /tags/{id}/objects
POST   /objects/{object_type}/{object_id}/tags     { "tag_ids": [...] }
DELETE /objects/{object_type}/{object_id}/tags/{tag_id}
```

`object_type` is `device` | `rack` | `room`, validated against that list in the
repository layer the same way connection terminations are (`20` §8).

## 9. Reservations

```
GET    /reservations                    ?rack_id= &room_id= &status= &project=
POST   /reservations
GET    /reservations/{id}
PATCH  /reservations/{id}
POST   /reservations/{id}/fulfil        { "device_id": "…" }
POST   /reservations/{id}/release
```

Creating a reservation with a U range also creates the backing `planned` device
row (`20` §9, option 2), so `device_u_no_overlap` enforces non-overlap with no
new constraint. A conflicting reservation therefore returns `409` from the
database, and the handler translates the constraint name into a message naming
the occupying device — an operator needs to know *what* is in U20-24, not that
an exclusion constraint fired.

`POST /reservations/{id}/fulfil` promotes the backing row: it becomes the real
device rather than being deleted and recreated, so the reservation's history
stays attached to the machine that filled it.

## 10. Bulk operations

`19` B9: the contract, not the checkbox.

```
POST /assets/bulk/lifecycle    { "device_ids": [...], "to_state": "…", "reason": "…" }
POST /assets/bulk/tags         { "device_ids": [...], "add": [...], "remove": [...] }
POST /assets/bulk/fields       { "device_ids": [...], "set": { "owner_group": "…" } }
POST /assets/bulk/move         { "moves": [ { "device_id": "…", "rack_id": "…", "u_start": 12 } ] }
```

Three properties, fixed for all four:

**Per-row transactions, not per-batch.** 40 devices means 40 savepoints. A batch
that fails on row 3 must not silently discard rows 1 and 2, and must not roll
back a successful move because a later one collided. The exception is
`bulk/move`, where a caller may pass `"atomic": true` to get all-or-nothing —
because moving half a rack is sometimes worse than moving none of it.

**A row-level report, always `207`-shaped.** Never a bare `200` with a count:

```json
{
  "succeeded": 38,
  "failed": [
    { "device_id": "…", "name": "SRV-DC1-HA-R2-11",
      "error": "rack_unit_occupied",
      "message": "U20–U23 in rack HA-R2-01 is occupied by SRV-DC1-HA-R2-09" },
    { "device_id": "…", "name": "PDUA-DC1-HB-R1-02",
      "error": "illegal_transition",
      "message": "in_service cannot go to in_stock; decommission it first" }
  ]
}
```

`error` is a stable machine key; `message` names the object in the operator's
vocabulary. `rack_unit_occupied` is `device_u_no_overlap` translated;
`mgmt_ip_in_use` is `ix_device_mgmt_ip_live` translated. Every constraint the
bulk path can hit gets a translation, or the feature returns database jargon to
a facilities technician.

**One audit row per device.** Not one per batch. A bulk decommission of 40
devices is 40 `audit_log` rows and 40 `device_lifecycle_event` rows, or it is
not an audit trail.

### CSV

```
GET  /assets/export                     ?<same filters as /devices>  → text/csv
POST /assets/import                     multipart, → dry-run report
POST /assets/import/{job_id}/commit
```

Import is **always** two-phase. The upload validates and returns the same
row-level report shape as above without writing anything; the commit applies it.
An import that discovers 2 bad rows in 400 at write time has already written
398, and the operator has no way to know which.

Matching for import is by `external_id`, then `serial_number`, then `asset_tag`,
then `name` — first hit wins, and the dry-run report states which key matched
each row. That ordering is `19` B2 in operational form, and it is why the serial
columns get their unique indexes before this endpoint is built.

## 11. Error keys

Stable across the module. The UI maps these to messages; the strings above are
the defaults.

| Key | Cause |
|---|---|
| `rack_unit_occupied` | `device_u_no_overlap` |
| `mgmt_ip_in_use` | `ix_device_mgmt_ip_live` |
| `serial_in_use` | `ix_device_serial_unique` |
| `asset_tag_in_use` | `ix_device_asset_tag_unique` |
| `illegal_transition` | lifecycle matrix, §4 |
| `insufficient_stock` | `part_stock.on_hand` would go negative |
| `contract_dates_invalid` | `support_contract_dates` |
| `reservation_conflict` | overlapping held U range |
| `referenced` | delete blocked by a dependent row |

## 12. Authorisation

The module adds no new auth model. It adds one distinction the existing one
already supports and the asset UI must respect: **reads are broad, writes are
not.** Lifecycle transitions, bulk operations, stock movements and reservation
release are write operations on the physical estate, and they are the operations
`audit_log` exists for.

If tenancy is brought into scope (`19` B8), it filters here — in the repository
layer, on every list endpoint — and not in the frontend. That is stated now
because retrofitting it later is the expensive path.
