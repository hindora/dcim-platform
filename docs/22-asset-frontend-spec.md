# 22. Asset & inventory — frontend specification

Status: **specification. Not built.** Extends `12-frontend-spec.md`.

## 1. Scope boundary — read this before writing any component

**The asset module lives entirely under `/assets`. No page outside it changes.**

This is a hard constraint, not a preference. The following are out of scope and
must render byte-identically after the module ships:

| Route | Page |
|---|---|
| `/` | Home |
| `/thermal`, `/power`, `/utilization` | estate pages |
| `/connectivity`, `/platform` | platform health |
| `/devices`, `/devices/:id` | device list and device detail |
| `/racks`, `/racks/:id` | rack list and elevation |
| `/floorplan`, `/topology` | floor plan and topology |
| `/alarms`, `/analytics` | alarms and analytics |
| `/settings/*` | settings |

`/assets` currently renders `DeviceList` (`frontend/src/App.tsx:200`). That one
line is the only edit permitted outside the module: it is repointed at the new
asset workspace. `/devices` keeps `DeviceList` and is untouched.

### The three rules that keep the boundary

**1. Shared components are imported, never edited.** `TimeChart`,
`NumberInput`, `DataTable`, the alarm pill — import them and pass props. If the
asset module needs a behaviour they do not have, **fork the component into
`features/assets/components/`** rather than adding a prop. A new optional prop
looks harmless and is not: it changes the render path of a component that
`DeviceDetail` and `Home` mount, and the constraint above says those cannot
change. A fork costs duplication and buys a guarantee.

**2. Shared CSS is additive and namespaced.** Everything the module adds to
`index.css` is prefixed `.asset-`. No existing selector is edited, no token in
`:root` is redefined, no existing class gains a rule. New tokens may be *added*
to `:root` (they affect nothing until used). The `19` review of this platform's
own history is relevant here: `.table-scroll` once collided with `home.css` and
had to be renamed to `.table-frame`. Namespacing is cheaper than the collision.

**3. API additions are additive** (`21` §Rules). No existing endpoint changes
shape, so the pages that call them cannot change behaviour.

### What this costs, stated honestly

The asset module needs a rack elevation, and one exists at `/racks/:id`. Under
this constraint the module either links out to it — leaving the workspace — or
renders its own. **It renders its own**, reading the same
`/racks/{id}/elevation` endpoint, because an asset-context elevation wants
things the operational one should not grow: reservation blocks, lifecycle
shading, free-U call-outs. Two elevations reading one endpoint is the price of
the boundary, and it is a fair one — the alternative is an operational screen
carrying asset concerns behind feature flags.

The same applies to the device record. `/devices/:id` is the *operational* view:
telemetry, charts, alarms, interfaces, PSUs. `/assets/inventory/:id` is the
*asset record*: identity, ownership, money, lifecycle, support, maintenance,
documents. They are different questions asked by different people, they link to
each other, and neither grows the other's tabs.

## 2. Information architecture

`/assets` is a workspace with its own persistent sub-navigation, not a single
page. The rail sits inside the module and does not touch the app's main menu
beyond the existing `Assets` item.

```
/assets                          Overview      — the estate as an asset base
/assets/inventory                Inventory     — the asset table, filters, bulk
/assets/inventory/:id            Asset record  — one asset, eight tabs
/assets/estate                   Estate        — DC → room → rack drill-down
/assets/estate/rooms/:id         Room          — rows, racks, free space
/assets/estate/racks/:id         Rack          — asset-context elevation
/assets/discovery                Discovery     — the candidate queue
/assets/maintenance              Maintenance   — windows and records
/assets/contracts                Support       — suppliers and contracts
/assets/parts                    Parts         — consumable stock
/assets/reservations             Reservations  — held capacity
/assets/admin/tags               Tags          — the controlled vocabulary
```

Eleven routes is a lot of rail. It is grouped into four sections, and the rail
shows counts so it doubles as a work queue:

| Section | Items |
|---|---|
| **Estate** | Overview, Inventory, Estate |
| **Intake** | Discovery `(n new)`, Reservations `(n held)` |
| **Upkeep** | Maintenance `(n active)`, Support `(n expiring)`, Parts `(n low)` |
| **Manage** | Tags |

A zero count renders as no badge, not as `(0)`.

## 3. Overview — `/assets`

One `GET /assets/summary` call (`21` §3). Nothing on this page paginates and
nothing on it is a chart of a time series — this is a stock-take, not a trend.

Four KPI tiles across the top, then two columns.

**Tiles.** Total assets · In service · Unidentified · Warranty expiring.

`Unidentified` reads **664** on the day this ships, which is 100% of the estate,
and that is the point. `19` B2 is a finding in a document; this tile is the same
fact where an operator sees it. It is a link, and it goes to
`/assets/inventory?has_serial=false`. When the importer is fixed the tile falls
to zero and stops being interesting, which is what a good instrument does.

**Left column — composition.** Assets by category (a bar list, not a donut:
category counts are compared, and comparing arc lengths is harder than comparing
bar lengths). Then assets by lifecycle state, as a single stacked bar with a
legend, because the states are parts of one whole.

**Right column — attention.** A short list of what needs a person: candidates
awaiting promotion, contracts expiring in 90 days, parts below reorder point,
reservations expiring, assets with no serial. Each row is a count and a link.
Empty state is "nothing needs attention", not a blank panel.

**Estate strip along the bottom.** Datacenters, rooms, racks, U used / U
reserved / U free as one proportional bar. Links into `/assets/estate`.

## 4. Inventory — `/assets/inventory`

The asset table. This is the screen the module is judged on.

**Layout.** A filter rail on the left (collapsible, remembered per user in
`localStorage`), the table filling the rest. Not filter chips above the table:
there are twelve filters and chips wrap into three lines and push the data below
the fold.

**Filters**, mapping one-to-one onto `21` §2 query parameters: lifecycle
(multi), device type, category, datacenter, room, rack, vendor, supplier, tag
(key then value, two-step picker), warranty state, owner group, cost centre,
`has serial`. Plus a search box over name, asset tag and serial.

Every filter writes to the URL. A filtered view is a link someone can paste into
a ticket, and that is most of what makes an inventory screen useful.

**Columns**, default set: Asset tag · Name · Type · Model · Location · U ·
Lifecycle · Warranty · Owner. Column chooser, persisted per user. `Location`
renders as `DC1 / Hall A / R2-01` with each segment linking into
`/assets/estate`.

**Density.** Default to a compact row. An inventory table is read by scanning
for one row among hundreds, and generous line height means fewer rows per screen
and more scrolling.

**Lifecycle** renders as a chip whose colour is semantic and separate from the
accent: in service (green), installed (blue), maintenance (amber), planned
(outline), in stock (grey), decommissioned / retired (muted, struck). Colour is
never the only encoding — the chip carries its label.

**Selection and bulk.** Checkbox column; selecting anything raises a bar at the
bottom of the viewport with the count and the four bulk actions (`21` §10). The
bar states what will happen before it happens — "Change lifecycle for 38 assets"
— and every bulk action opens a confirm step showing the row-level dry run.

**The failure report is a first-class view, not a toast.** A bulk operation
returns 38 succeeded and 2 failed; the UI shows the 2 with their names and the
plain-language message, and offers *retry the failed rows* and *copy report*. A
toast that says "2 failed" is how the feature becomes distrusted.

**Empty and loading.** Skeleton rows, not a spinner — the table's shape is known
before the data arrives. Empty-with-filters says which filter is excluding
everything and offers to clear it; empty-without-filters means the import has
not run, and says so.

## 5. Asset record — `/assets/inventory/:id`

**Not `DeviceDetail`.** A separate page, in `features/assets/`, reading
`GET /devices/{id}` and the sub-resources.

**Header.** Asset tag (large, monospace — it is the thing someone reads off a
sticker and matches), name, type, model, lifecycle chip, and a link
**"Open in Devices →"** to `/devices/:id` for telemetry. The reciprocal link on
`DeviceDetail` would be an edit to an out-of-scope page and is therefore **not**
added; the asset record is reached from the asset workspace.

**Tabs.**

| Tab | Contents | Source |
|---|---|---|
| Overview | identity, model, vendor, tags, notes | `GET /devices/{id}` |
| Placement | DC / room / row / rack / U, elevation thumbnail, move action | existing + `/racks/{id}/elevation` |
| Lifecycle | state, transition history with actor and reason, transition action | `/devices/{id}/lifecycle` |
| Support | supplier, purchase, cost, covering contracts, warranty countdown | `/devices/{id}/contracts` |
| Maintenance | windows this asset is in, work records, parts consumed | `/devices/{id}/maintenance` |
| Connections | power feeds A/B, network peers, redundancy state | `/power/chain`, existing interfaces |
| Documents | photos, manuals, invoices, RMA paperwork | `/…/documents` |
| Audit | every change to this asset, newest first | `audit_log` filtered by target |

No telemetry tab and no charts. That is `/devices/:id`, it already does it well,
and duplicating it would be the module reaching outside its boundary in spirit
if not in code.

**The warranty countdown** is a sentence, not a gauge: "Covered until 10 March
2027 — 554 days" or "Expired 3 months ago". Gauges are for bounded ratios; this
is a date.

## 6. Estate — `/assets/estate`

Three levels, asset-flavoured throughout: this drill-down answers *what is
here and what fits*, where the operational floor plan answers *what is wrong*.

**Datacenter list** → rooms with rack counts, U used/free, design kW.
**Room** → rows and racks as a grid, each rack a card with a U-fill bar,
power headroom, and asset count. Free racks are visually distinct because the
first question on this screen is where space is.
**Rack** → the asset-context elevation.

The elevation renders `/racks/{id}/elevation` plus three things the operational
one does not carry: **reservation blocks** (hatched, with project and expiry),
**lifecycle shading** (planned and installed drawn differently from in-service),
and **contiguous free-space call-outs** — "7U free, U12–U18" — because "is there
room for a 4U chassis" is the question, and reading it off a picture is error
prone.

Front and rear faces both, toggled. `device.facing` exists and is used.

## 7. Discovery — `/assets/discovery`

The screen `19` B2 says is the only missing piece of an otherwise finished
subsystem.

A table of candidates: address, protocol, what the probe read (`sysDescr`,
`sysObjectID`, `sysName` from `identity`), suggested type / vendor / model,
first and last seen, and match state. Grouped into **Unmatched** (nothing in
inventory claims this responder — the actionable set) and **Matched** (already
known; shown with a denominator, because "the sweep saw 900 and 894 were
expected" is the useful sentence).

Promote opens a form pre-filled from the suggestion, requiring the two things
discovery cannot know: **placement** (room, rack, U) and **name**. Ignore is one
click and reversible.

**A banner states the truth about matching.** While `with_serial = 0`, matching
falls back to management IP alone, and the page says so — "Serial-number
matching is unavailable: no asset carries a serial. Candidates are matched by
management IP only." A screen that quietly matches on a weaker key than the
operator assumes is worse than one that admits it.

## 8. Maintenance — `/assets/maintenance`

Two views on one route: a calendar-ish timeline of windows (week / month), and
a table. Timeline default, because windows are scheduled against each other and
overlap is the thing you need to see.

**Creating a window** is a three-step flow, and step two is the one that matters:

1. When and what — title, change reference, start, end.
2. **Targets, with a live preview.** Select devices; the panel calls
   `POST /maintenance/windows/preview` and shows: 12 devices selected, 47
   downstream, 2 alarms currently active, and any redundancy warnings — *this
   PDU feeds three single-corded loads*. Scoping a window too widely is
   discovered here, not at 02:00.
3. Confirm.

**An active window's page** shows its targets, elapsed time, and the shelved
alarm count. Shelved alarms are listed, not hidden: the post-work question is
*did anything else break while we were in there*, and this is where it is
answered. Completing a window prompts to record what was done, with parts used
— which posts the stock movements (`21` §7).

## 9. Support — `/assets/contracts`

Supplier list and contract list. A contract row shows reference, supplier, kind,
service level, dates, covered device count, and a renewal countdown. Sorted by
expiry ascending by default, because that is the only sort anyone wants.

Adding devices to a contract reuses the inventory table's filter rail inside a
picker, so "everything of model X bought in 2024" is one selection rather than
200 clicks.

## 10. Parts — `/assets/parts`

Part list with stock across stores, below-reorder rows pinned to the top and
marked with an icon and a label, never colour alone.

A part's page shows per-store on hand and reserved, the reorder point, and the
**movement ledger** — the running record, newest first, each line naming the
device it was consumed on where applicable. There is no editable "quantity"
field anywhere in this UI. Correcting a count is posting an adjustment with a
note, and the note is required. This is deliberate friction: an inventory whose
counts can be silently overwritten is a spreadsheet.

## 11. Reservations — `/assets/reservations`

Table of held capacity: project, owner, location, U range, kW, needed by,
expires, status. Expiring and expired reservations pinned to the top — the
failure mode of this feature everywhere it exists is a rack held for a project
cancelled two years ago that nobody released.

Creating a reservation from a rack's elevation is the primary path: select a U
range on the picture, name the project, set an expiry. Fulfilling it opens the
promote flow with placement pre-filled.

## 12. Visual language

Inherit the app's existing tokens — this is a workspace inside the product, not
a separate product, and `12-frontend-spec.md` and `index.css` already define the
palette, spacing and type scale. The module adds no new accent colour.

What it does add, namespaced `.asset-`:

- **Lifecycle chips**, seven states, each with a label and a distinct fill.
- **Fill bars** for U and power, with the value as text beside the bar — a bar
  alone is unreadable at the precision an operator needs.
- **Hatched reservation blocks** in elevations, so held space is visibly
  different from occupied space at a glance.
- **A `mono-tag` type style** for asset tags and serials, tabular-numeric, since
  those are compared character by character against a sticker.

Semantic colour (warranty expired, stock below reorder, reservation expiring) is
separate from the accent and always paired with a label or icon.

## 13. File layout

```
frontend/src/features/assets/
  AssetWorkspace.tsx          rail + <Outlet/>, owns /assets/*
  Overview.tsx
  inventory/
    InventoryTable.tsx
    FilterRail.tsx
    BulkBar.tsx
    BulkReport.tsx
    AssetRecord.tsx           + tabs/
  estate/
    EstateTree.tsx
    RoomView.tsx
    AssetElevation.tsx        forked, reads /racks/{id}/elevation
  discovery/CandidateQueue.tsx
  maintenance/{WindowTimeline,WindowForm,WindowDetail}.tsx
  contracts/{ContractList,ContractDetail,SupplierList}.tsx
  parts/{PartList,PartDetail,MovementLedger}.tsx
  reservations/ReservationList.tsx
  admin/TagAdmin.tsx
  components/                 forks of shared components, module-local
  assets.css                  every selector prefixed .asset-
```

**Two corrections against what phase 1 actually built.**

*API calls live in `api/client.ts`, not a module-local `api.ts`.* The client's
`request()` is not exported, and duplicating it would give the asset module its
own fetch wrapper outside the shell's 401 handling — a session that expired
while the operator was on `/assets` would fail silently instead of returning
them to the login screen. Every other feature calls through `client.ts`; the
module follows the codebase rather than this document. The additions are
additive: new types, `assetSummary`, `assetFilterOptions`, `assetDevices`,
`powerChain`, `discoveryCandidates`, and optional fields on `DeviceSummary`. No
existing method or type changed meaning.

*`assets.css` is imported by `AssetWorkspace.tsx`, not `main.tsx`.* This is
stricter than planned and worth keeping: the stylesheet cannot load on a page
outside the module at all.

The boundary in §1 is enforceable by looking at the diff. Phase 1's, in full:

```
 backend/app/api/v1/__init__.py      |   2 +   router registration
 backend/app/api/v1/devices.py       |  15 +   additive query params
 backend/app/repositories/devices.py |  50 +   additive filters and columns
 backend/app/schemas/__init__.py     |  13 +   optional fields
 backend/app/services/devices.py     |   9 +
 frontend/src/App.tsx                |  22 +   route subtree + imports
 frontend/src/api/client.ts          | 143 +   additive types and methods
 + backend/app/{api/v1,repositories,services}/assets.py
 + backend/tests/test_assets.py
 + frontend/src/features/assets/**
```

**No file under `features/` outside `features/assets/` was touched, and no
existing test changed.** A pull request for this module that alters a component
mounted by another page is out of scope by construction.
