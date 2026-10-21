# 30 — Anomaly detection: API specification

Status: **specification. Not built.** This document defines the REST,
websocket and outbox surface of `28-anomaly-detection-design.md`. It is
reconciled with `29-anomaly-data-model.md` (tables, jsonb shapes, settings,
Redis keys, alarm types), `31-anomaly-frontend-spec.md` (the screens that
call these endpoints; its API asks D3–D11 are answered in §15) and
`33-anomaly-implementation-plan.md` (phases and the severity cap).

All new endpoints live under `/api/v1/anomalies`. They use the same bearer
auth and role ladder as every other router, and the list envelope that
`/devices` already returns. **This document is authoritative for wire field
names.** Doc 31 §16's TypeScript follows it, and §15 lists the corrections
doc 31 still has to make.

Codebase references are `path:line` relative to `backend/app/` unless they
start with `frontend/` or `alembic/`. Every reference was read for this
document. **[Judgement]** marks a design choice that no existing code or
source settles. **Contract deviation** marks a place where code reading shows
the shared contract needs a change. Each one keeps the contract name and says
what is recommended.

## 1. Principles

**Additive only. No existing endpoint changes shape.** Nothing under
`/alarms`, `/devices`, `/integrations` or `/ws` gains, loses or retypes a key.

- **Derived alerts are ordinary `alarm` rows.** They already appear in
  `/alarms` with `detection=derived`, because
  `DETECTION_BY_SOURCE["analytics"]` is already `DERIVED`
  (`core/alert_taxonomy.py:186`).
- **Alarm detail comes from a new filter.** The detail a UI needs about a
  derived alarm (expected against observed, evidence, incident) comes from
  `GET /anomalies/findings?alarm_id=` (doc 31 D4). It is not a new key on
  `GET /alarms/{id}`.

The only changes to existing code are internal and backwards-compatible:

| Change | Why | Existing callers |
|---|---|---|
| `repositories/alarms.raise_alarm` gains an optional `attributes` kwarg. It writes the existing `alarm.attributes` jsonb column (`alembic/versions/0005_alarms_and_events.py:99`), merged on conflict, as `{"anomaly": {finding_id, detector_id, detector_version, expected, observed}}` (doc 29 §9.2, D13) | The alarm row carries its own link, for audit and for Jira body drafting (doc 33 task 5.4) | Unchanged. The default `None` writes `{}`. `_ALARM_SELECT` (`repositories/alarms.py:444-456`) does not select `attributes`, so no response changes |
| The sweep calls `repo.refresh_device_alarm_state` once per sweep for the devices it touched (doc 29 D12, doc 33 D8) | `raise_alarm` does not refresh `device_state` by itself | Existing function, new caller |
| `api/v1/__init__.py` includes `anomalies.router` | Registration (`api/v1/__init__.py:36-68`, mounted at `settings.api_prefix` by `main.py:143`) | None |

`raise_alarm`'s signature today (`repositories/alarms.py:217-225`) has no
detail field, and that confirms doc 28's open question. The column exists,
but the function never writes it, so doc 28's alternative of a new `evidence`
column on `alarm` is not needed.

**Analytics never hides a hard alarm.** The L5 correlator may mark a
*derived* alarm (`source='analytics'`) as a symptom with
`correlation.mark_symptom` (`alarms/correlation.py:211-217`). It must never
set `is_symptom` on an L1 alarm: a threshold, state, trap or staleness
alarm. An L1 alarm can be a *member* of an incident and appear in its tree,
but it stays visible on the console.

This is **[judgement]**, for a realism reason. In production, the advisory
tier sits above the BMS and DCIM alarm stream and does not suppress it. Only
deterministic topology correlation does that, and here that is `correlate`
(`alarms/correlation.py:378-397`). A statistical root that is wrong must not
be able to make a real high-temperature alarm disappear.

> **Alignment note for doc 29 §8.5 step 3.** That step marks symptoms
> whenever the root has an alarm row, but it does not restrict which alarms
> are marked. It should add "and the symptom alarm's `source = 'analytics'`".

**The LLM is never in the alarm path.** Detection, raising, clearing,
promotion, escalation and correlation all run in the ingest worker's sweep.
Each completes the same way whatever `DCIM_ANOMALY_EXPLAINER` is set to.
The only caller of the explainer is `POST /incidents/{id}/explain` (§7).
With `explainer=none` that endpoint returns `409`, and every other endpoint
behaves identically.

**Feature-off is not an error.** With `DCIM_ANOMALY_ENABLED=false`:

- Every read endpoint returns empty pages.
- `GET /health` reports `"enabled": false`, which is how the UI decides what
  to hide (doc 31 D6).
- Configuration writes still succeed (detectors, overrides, exclusions), so
  an operator can prepare the catalogue before the sweep is switched on.
- No endpoint returns `503` because the feature is off.

**Errors are structured.** Every `4xx` from this router sets `detail` to
`{"error": "<stable key>", "message": "<sentence>", ...}`. The frontend's
`request()` already handles a dict detail and keeps it as
`ApiError.structured` (`frontend/src/api/client.ts:86-99`). §12 lists the
keys.

**Writes are audited.** Every state-changing call writes one `audit_log` row
through `core/audit.record` (`core/audit.py:67-71`), with `before` and
`after`. This mirrors alarm acknowledge and the override endpoints
(`api/v1/alarms.py:116-121, 428-432`). Action names follow doc 29:
`anomaly.finding.feedback`, `anomaly.explain`, and the others listed per
endpoint below.

## 2. Authorisation matrix

The roles are `viewer < operator < admin` (`core/security.py:118`). A token
with no role claim is treated as `viewer` (`core/security.py:163`). Roles
are enforced as a dependency with `require_role(...)`, never inside a
handler (`core/security.py:166-177`).

| Method | Path | Minimum role | Mirrors |
|---|---|---|---|
| GET | `/anomalies/findings`, `/findings/{id}` | viewer | `GET /alarms` (`api/v1/alarms.py:59`) |
| POST | `/anomalies/findings/{id}/feedback` | operator | Alarm acknowledge (`api/v1/alarms.py:104`) |
| GET | `/anomalies/incidents`, `/incidents/{id}` | viewer | |
| POST | `/anomalies/incidents/{id}/feedback` (bulk) | operator | Same as per-finding feedback |
| POST | `/anomalies/incidents/{id}/explain` | operator | Costs money and may send data off-site, so a read-only role cannot trigger it |
| POST | `/anomalies/incidents/{id}/explain/feedback` | operator | |
| GET | `/anomalies/detectors` | viewer | `GET /alarm-rules` |
| PATCH | `/anomalies/detectors/{id}` | **admin** | Rule edit is admin (`api/v1/alarms.py:270`) because it changes estate policy. This includes the L5 row and its `major_enabled` switch |
| GET | `/anomalies/overrides` | viewer | `GET /devices/{id}/alarm-rules` |
| PUT/DELETE | `/anomalies/overrides/{detector_id}/{device_id}` | operator | Per-device alarm override is operator (`api/v1/alarms.py:400, 443`) |
| GET | `/anomalies/expected` | viewer | `GET /devices/{id}/history` |
| GET | `/anomalies/exclusions` | viewer | |
| POST/DELETE | `/anomalies/exclusions`, `/exclusions/{id}` | operator | Maintenance window writes are operator (`api/v1/maintenance.py:99`) |
| GET | `/anomalies/health` | viewer | Doc 31 D6 requires that viewers can read it |

> **Testing note.** `POST /login` always issues an `admin` token
> (`api/v1/misc.py:55`). To check the `403`s you need a hand-minted viewer
> token:
> `python -c "from app.core.security import issue_token; print(issue_token('v','viewer'))"`.

## 3. Common conventions

**Pagination.** The codebase has two list shapes:

- `GET /alarms` returns `{"items": [...]}` with `limit`/`offset` and no
  total (`api/v1/alarms.py:54-57, 75`).
- `GET /devices` returns `Page{items, next_cursor, total}`
  (`schemas/__init__.py:22-25`), with `cursor` *or* `offset` and an opt-in
  `with_total` (`api/v1/devices.py:72-114`).

Every paged anomaly list uses the **`Page` envelope with
`limit`/`offset`/`with_total`**, and `next_cursor` is always `null`. This
matches doc 31 §18 item 5, which assumes `limit`/`offset` plus `total`. It
gives `components/Pagination.tsx` its "1–50 of 212"
(`frontend/src/components/Pagination.tsx:48-63`), and it reuses the existing
frontend `Page<T>` (`frontend/src/api/client.ts:1813-1817`).

| Param | Type | Default | Notes |
|---|---|---|---|
| `limit` | int 1–500 | 50 | |
| `offset` | int ≥ 0 | 0 | The order is total, because it ends in `id`. The same guarantee as `repositories/alarms.py:573-581` |
| `with_total` | bool | false | Runs `COUNT(*)` over the filtered set |

> **Contract deviation (minor).** The contract says "paginated like other
> lists", and the code has two styles. This spec picks the `Page` envelope
> with offset paging.

**Time.** Timestamps are ISO 8601 with an offset, and UTC on output. Query
parameters accept ISO 8601.

**Location filters.**

- `room` is a room UUID, as on `GET /alarms` (`api/v1/alarms.py:48-50`).
- `datacenter` takes either a UUID (matched against `id`) or a code such as
  `DC1` (matched against `code`).
- Findings filter on the `datacenter_id`/`room_id` stamped when the finding
  opened (doc 29 §4.7). That is the same "history says what was true then"
  rule as `alarm.category`.

**Enums** (contract and doc 29 names):

| Name | Values |
|---|---|
| `FindingState` | `open` \| `acknowledged` \| `dismissed` \| `confirmed` \| `resolved` |
| `Verdict` | `confirmed` \| `dismissed` \| `null`. Doc 29 D3 adds this column. It survives resolution |
| `FindingKind` | `sensor` \| `process` \| `undetermined` |
| `FindingLane` | `advisory` \| `alert` |
| `Resolution` | `cleared` \| `device_retired` \| `detector_disabled` \| `muted` \| `superseded` \| `manual` (doc 29 §4.7) |
| `IncidentState` | `open` \| `resolved` |
| `Tier` | `power` \| `plant` \| `loop` \| `unit` \| `zone` \| `server` \| `room` (doc 29 `ck_anomaly_incident_tier`). `room` is the room-level fallback used until the influence fit exists |
| `Layer` | `L0` \| `L2` \| `L3` \| `L4` on findings. The catalogue also has the `L5` switch row (doc 33 D13) |
| `FeedbackAction` | `acknowledge` \| `dismiss` \| `confirm` \| `reopen` |
| `DismissReason` | `not_a_fault` \| `expected_operation` \| `sensor_fault` \| `duplicate` (doc 31 D5) |

## 4. Findings

### 4.1 `GET /anomalies/findings`

| Param | Type | Notes |
|---|---|---|
| `state` | repeated `FindingState` | Defaults to `open`, `acknowledged` and `confirmed`, meaning what is still true. Mirrors the alarm list's open-by-default (`api/v1/alarms.py:69`) |
| `lane` | repeated `FindingLane` | |
| `kind` | repeated `FindingKind` | `kind=sensor` is the sensor-health queue |
| `detector` | repeated detector id | |
| `device_id` | uuid | |
| `datacenter` | uuid or code | |
| `room` | uuid | |
| `since` | datetime | `last_seen >= since`, meaning "was true at any point since" |
| `alarm_id` | uuid | **Contract deviation (additive; doc 31 D4).** Returns the finding behind a derived alarm |
| `incident_id` | uuid | **Contract deviation (additive).** Members of one incident |
| `stale` | bool | Open for more than 24 h with no feedback. Derived in the query, never stored (doc 29 §8.1) |
| `with_facets` | bool | **Contract deviation (additive; doc 31 §18 item 4).** Adds `facets` with counts over the same filters minus `state`, so the tiles and facet counts need one call |
| `limit`, `offset`, `with_total` | | See §3 |

The order is `impact_kw DESC NULLS LAST, last_seen DESC, id`
(`ix_anomaly_finding_*`, doc 29). An enum value that is not in §3 returns
`400 unknown_enum`. This follows the alarm list (`api/v1/alarms.py:61-68`).

```json
{
  "items": [
    {
      "id": "6c1f6e1e-6a51-4c0b-9b55-0f2d7b1d2a11",
      "detector_id": "CR-4",
      "detector_name": "CRAH filter loading",
      "detector_version": 2,
      "layer": "L3",
      "summary": "Filter loading on CRAH-DC1-HA-03",
      "device_id": "1b0e…",
      "device_name": "CRAH-DC1-HA-03",
      "device_type": "crah",
      "metric_key": "filter_diff_pressure",
      "instance": "",
      "unit": "kPa",
      "kind": "process",
      "lane": "advisory",
      "state": "open",
      "verdict": null,
      "severity": null,
      "first_seen": "2026-10-19T03:12:00+00:00",
      "raised_at": "2026-10-19T04:12:00+00:00",
      "last_seen": "2026-10-21T08:41:00+00:00",
      "occurrences": 2871,
      "reopen_count": 0,
      "score": 6.4,
      "score_unit": "sigma",
      "expected": 0.118,
      "observed": 0.162,
      "impact_kw": 1.9,
      "stale": true,
      "incident_id": null,
      "alarm_id": null,
      "datacenter_code": "DC1",
      "room_id": "a3…",
      "room_name": "Hall A",
      "rack_name": null,
      "resolved_at": null,
      "resolution": null
    }
  ],
  "next_cursor": null,
  "total": 1,
  "facets": {
    "open_incidents": 1, "open_findings": 41, "open_sensor": 9, "new_24h": 19,
    "by_state": { "open": 30, "acknowledged": 8, "confirmed": 3, "dismissed": 0, "resolved": 0 },
    "by_lane": { "advisory": 37, "alert": 4 },
    "by_kind": { "sensor": 9, "process": 26, "undetermined": 6 }
  }
}
```

Field notes:

- **`summary`** is the deterministic template text stored in the
  `anomaly_finding.summary` column (doc 29). It is never LLM text.
- **`first_seen`** is when the condition began. **`raised_at`** is when its
  dwell was satisfied and the finding opened (doc 29 §4.7).
- **`score`** is the detector's normalised statistic. `score_unit` is
  `sigma` (L3 CUSUM and L4 robust z), `seconds` (dwell-type L0) or `ratio`.
  Its meaning depends on the layer, so the UI shows it next to `layer`, never
  on its own.
- **`severity`** is set only on alert-lane findings, and is the severity of
  the alarm.
- **`facets`** is present only when `with_facets=true`. Its shape is doc 31's
  `AnomalySummary`.

### 4.2 `GET /anomalies/findings/{id}`

This returns the list row plus:

- **`detector`**: `{id, name, version, layer, params}`, using the effective
  parameters after any override.
- **`evidence`**: the `anomaly_finding.evidence` jsonb exactly as doc 29 §7.4
  defines it. That includes `window`, `rule` (`expr`, `dwell_s`,
  `params_version`), `diagnosis`, `expected`, `observed`, `lower`, `upper`,
  `signals[]`, `gates`, `masks`, `baseline` and `alarm_history`. Every
  `signals[].id` (`S1`…) is a citable evidence ID.
- **`rule_limit`**: `{value, label}` on L2 findings, derived from
  `evidence.upper`/`lower` and the rule. Doc 31 draws it as a reference line.
- **`feedback[]`**: the append-only list from doc 29 §8.4. Each element is
  `{at, by, action, reason, note, from_state, to_state, detector_version, via}`.
  `via` is `"finding"` or `"alarm"` (§4.4).

An unknown id returns `404 not_found`.

### 4.3 `POST /anomalies/findings/{id}/feedback`

```json
{ "action": "dismiss", "reason": "expected_operation", "note": "Filter swapped 2026-10-18" }
```

- **`reason`** is a `DismissReason`. It is **required when `action` is
  `dismiss`**, otherwise the call returns `422 reason_required`, and it is
  rejected on any other action with `422 reason_not_allowed`. This is doc 31
  D5: dismissal is how detector precision is measured, and free text cannot
  be counted. It mirrors "a mute needs a reason"
  (`api/v1/alarms.py:410-412`).
- **`note`** is optional and at most 1000 characters, like `AckRequest`
  (`api/v1/alarms.py:26-27`).

**State machine.** Operators move a finding through the transitions below.
The sweep owns `resolved` and the reopen-within-window case. This is
doc 29 §8.1, unchanged.

| From \ action | `acknowledge` | `dismiss` | `confirm` | `reopen` |
|---|---|---|---|---|
| `open` | → `acknowledged` | → `dismissed` | → `confirmed` | 409 |
| `acknowledged` | 409 | → `dismissed` | → `confirmed` | → `open` |
| `confirmed` | 409 | 409 | 409 | → `open` |
| `dismissed` | 409 | 409 | 409 | → `open` |
| `resolved` | 409 | 409 | 409 | 409 |

Changing a verdict from `confirmed` to `dismissed`, or back, goes through
`reopen`. That is two audited steps, so every verdict in the precision
figures has one clear author and time. This matches doc 31 §5.6, where
Reopen is offered on dismissed and confirmed findings.

**What each operator action writes** (doc 29 §8.1, §8.3, §8.4):

| Action | Row changes | Side effects |
|---|---|---|
| `acknowledge` | `acknowledged_at`, `acknowledged_by` | Mirrored to the alarm on alert-lane findings (§4.4) |
| `confirm` | `verdict = 'confirmed'` | A `process` finding also writes a `confirmed_finding` baseline exclusion (doc 29 §8.3), so the next refit does not learn the fault as normal |
| `dismiss` | `verdict = 'dismissed'` | The row keeps holding the dedupe key, so the sweep touches `last_seen` silently. Nothing re-raises or re-publishes until the condition clears |
| `reopen` | `verdict = NULL` | |

**System transitions** (sweep only):

| From | To | When |
|---|---|---|
| any non-resolved, `dismissed` included | `resolved`, with `resolution` set | `cleared` (clear for `clear_dwell_s`), `device_retired`, `detector_disabled`, `muted` or `superseded`. `verdict` is kept, so a dismissed finding that clears ends as `resolved` + `verdict='dismissed'`, a counted false positive |
| `resolved`, inside `reopen_window_s` | `open` (same row) | `reopen_count + 1`, `verdict` kept. More than 3 reopens holds the finding open (doc 29 §8.2) |
| `resolved`, outside the window | new `open` row | |

Doc 29 also defines an operator `→ resolved (resolution='manual')`. **No
API action exposes it.** The contract has four feedback actions and doc 31
has no Resolve button. A condition the detector still sees cannot be
resolved by hand. The fix for a detector that is wrong is a mute override,
which resolves the finding with `muted`. `manual` stays reserved.

Behaviour:

- **Race safety.** The update is one conditional
  `UPDATE … WHERE state = ANY(:allowed_from) RETURNING`, the form alarm
  acknowledge uses (`repositories/alarms.py:620-628`). Two concurrent
  presses get one `200` and one `409`.
- **The `409 illegal_transition`** body carries the allowed set, for
  example
  `{"error":"illegal_transition","message":"…","state":"confirmed","allowed":["reopen"]}`.
- **One transaction.** The call appends a `feedback` element, writes
  `audit_log` with `action = 'anomaly.finding.feedback'`,
  `target_type = 'anomaly_finding'` (doc 29 §8.4), commits, and then
  publishes `anomaly.finding.updated` (§8).
- **Dismissing or confirming an alert-lane finding never clears its alarm.**
  The manual clear is the alarm action the code singles out as able to hide
  a live fault (`api/v1/alarms.py:160-161`). It stays on the alarm, under
  its own button.

Response `200` is the updated `AnomalyFinding`, the §4.1 row shape, as doc
31's `findingFeedback` expects.

### 4.4 One acknowledgement path for alert-lane findings (doc 31 D11)

An alert-lane finding and its derived alarm describe one condition. They
must not end up with two acknowledgements that disagree.

**`alarm.state` is authoritative for acknowledgement.** The alarm is the
ISA-18.2 object that carries acknowledge semantics, the history row and the
audit row. It is also what the Jira inbox already acknowledges when a ticket
reaches Done (`integrations/inbox.py:3-11`).

**Finding → alarm: immediate, same transaction.** When
`POST /findings/{id}/feedback {"action":"acknowledge"}` targets an
alert-lane finding with an open `alarm_id`, the handler:

1. Runs `repo.acknowledge(session, alarm_id, user, note)`
   (`repositories/alarms.py:620-628`).
2. Runs `repo.record_history(... action="acknowledged")` and writes the
   `alarm.acknowledge` audit row, exactly as `POST /alarms/{id}/acknowledge`
   does (`api/v1/alarms.py:106-121`).
3. Sets the finding to `acknowledged`, with `via: "finding"`.

All of that happens in one transaction.

- If the alarm was already `ACKNOWLEDGED`, the finding is still acknowledged,
  and the alarm is not touched or errored. The `409` from the alarm path is
  not propagated.
- If the alarm is already `CLEARED`, only the finding changes.

**Alarm → finding: next sweep.** An acknowledgement made on the alarm, from
`POST /alarms/{id}/acknowledge` or from the Jira inbox, reaches the finding
within one `DCIM_ANOMALY_SWEEP_S`. The sweep sets any `open` alert-lane
finding whose alarm is `ACKNOWLEDGED` to `acknowledged`, with
`acknowledged_by = alarm.acknowledged_by` and a `feedback` element with
`via: "alarm"`.

The sync is done in the sweep, not in the existing endpoint, so that
`api/v1/alarms.py` and the inbox stay unchanged (§1). The lag of up to 60 s
is acceptable for an `alert`-class condition, which belongs to whoever
schedules the work.

**What this means for doc 31.** Doc 31's interim rule ("hide the finding's
Acknowledge on alert-lane rows") can be dropped. Both buttons now do the
same thing. Confirm fault, Dismiss and Reopen stay finding-only, because the
alarm has no verdict.

Reopening a finding does not un-acknowledge its alarm. The alarm lifecycle
has no ACKNOWLEDGED → ACTIVE transition, and this spec does not add one.

## 5. Incidents

### 5.1 `GET /anomalies/incidents`

| Param | Type | Notes |
|---|---|---|
| `state` | repeated `IncidentState` | Default `open` |
| `datacenter`, `room` | | See §3 |
| `tier` | repeated `Tier` | |
| `root_device_id` | uuid | |
| `since` | datetime | `last_activity_at >= since` |
| `limit`, `offset`, `with_total` | | |

The order is `impact_kw DESC NULLS LAST, opened_at DESC, id`
(`ix_anomaly_incident_open`, doc 29).

```json
{
  "items": [
    {
      "id": "d0a4…",
      "state": "open",
      "title": "CHW supply loss: CH-DC1-02 tripped",
      "summary": "CH-DC1-02 tripped at 07:58; 5 of 7 Hall A CRAHs capacity-starved",
      "tier": "plant",
      "root_device_id": "77c…",
      "root_device_name": "CH-DC1-02",
      "root_finding_id": null,
      "root_alarm_id": "9e2…",
      "root_ref": "chiller_trip",
      "lane": "alert",
      "opened_at": "2026-10-21T07:58:30+00:00",
      "last_activity_at": "2026-10-21T08:12:00+00:00",
      "resolved_at": null,
      "member_count": 17,
      "alarm_count": 6,
      "sensor_count": 0,
      "suppressed_symptoms": 4,
      "impact_kw": 412.0,
      "datacenter_code": "DC1",
      "room_name": "Hall A",
      "has_explanation": false
    }
  ],
  "next_cursor": null,
  "total": 1
}
```

Field notes:

- **`root_ref`** is the root finding's detector id, or the root alarm's
  `alarm_type`.
- **`lane`** is `alert` if any member is an alarm, otherwise `advisory`.
- **`member_count`** is the denormalised column (doc 29), findings plus
  alarms.
- **`suppressed_symptoms`** counts only derived alarms that L5 marked (§1).

### 5.2 `GET /anomalies/incidents/{id}`

This returns the list row plus `members`, `context`, `also_considered`,
`evidence`, `explanation` and `explanation_feedback`. An unknown id returns
`404 not_found`.

```json
{
  "id": "d0a4…",
  "…": "every list field",
  "members": [
    { "type": "alarm", "id": "9e2…", "is_root": true, "parent_id": null, "tier": "plant",
      "device_id": "77c…", "device_name": "CH-DC1-02", "ref": "chiller_trip",
      "severity": "MAJOR", "state": "ACTIVE", "onset": "2026-10-21T07:58:30+00:00",
      "evidence_id": "A1" },
    { "type": "inferred", "id": "loop:DC1-CHW-A", "is_root": false, "parent_id": "9e2…",
      "tier": "loop", "label": "CHW loop A (inferred from cooling edges)", "inferred": true },
    { "type": "finding", "id": "4be…", "is_root": false, "parent_id": "loop:DC1-CHW-A",
      "tier": "unit", "device_id": "1b0e…", "device_name": "CRAH-DC1-HA-03",
      "ref": "CR-1", "lane": "alert", "kind": "process", "state": "open",
      "alarm_id": "c51…", "role": "symptom", "onset": "2026-10-21T08:00:10+00:00",
      "evidence_id": "F3" },
    { "type": "alarm", "id": "e07…", "is_root": false, "parent_id": "4be…", "tier": "zone",
      "device_id": "…", "device_name": "SEN-DC1-HA-R2-04", "ref": "inlet_temp_high",
      "severity": "WARNING", "state": "ACTIVE", "role": "context", "evidence_id": "A4" }
  ],
  "context": [ { "id": "X1", "type": "context", "text": "No fire pre-discharge in Hall A" } ],
  "also_considered": [ { "device_name": "PU-DC1-CHW-02", "reason": "proved running throughout" } ],
  "evidence": { "…": "the bundle, §5.3" },
  "explanation": null,
  "explanation_feedback": []
}
```

- **Members form a tree** through `parent_id`, which is what doc 31 §6.5
  draws. An `inferred` node is a CHW loop with no device row. Doc 28 says the
  loop has no explicit entity and is inferred from cooling edges.
- **`role`** is `root`, `symptom` or `context`.
  - `symptom` means explained by the root. For a derived alarm it also means
    `is_symptom`.
  - `context` means related and inside the window, but not suppressed.
  - Every L1 alarm member is `context` or `root`, never `symptom` (§1).

### 5.3 The evidence bundle

The bundle is stored in `anomaly_incident.evidence` (doc 29 §7.4), and the
correlator rewrites it when membership or the root changes. Reads are served
from the Redis cache `dcim:anomaly:bundle:{incident_id}:{last_activity_epoch}`
(600 s, doc 29 §10). The epoch is part of the key, so any change to the
incident misses the cache, with no invalidation code. The bundle is the
explainer's only input. With no explainer, it is also the alert-detail panel.

```json
{
  "v": 1,
  "bundle_sha256": "5f1c…",
  "built_at": "2026-10-21T08:12:04+00:00",
  "summary": "CH-DC1-02 tripped at 07:58. CHW supply rose 6.9→13.8 °C. 5 of 7 Hall A CRAHs crossed setpoint +3 K with CHW valves ≥ 95% within 2 min. 4 rack inlets rose; no sensor findings in the chain.",
  "devices": [ { "id": "77c…", "name": "CH-DC1-02", "type": "chiller" } ],
  "items": [
    { "id": "A1", "type": "alert", "text": "chiller_trip MAJOR on CH-DC1-02 at 07:58",
      "alarm_id": "9e2…", "device_id": "77c…", "ts": "…" },
    { "id": "F3", "type": "finding", "text": "CR-1 on CRAH-DC1-HA-03: 23.4 °C vs 18.0 °C",
      "finding_id": "4be…", "device_id": "1b0e…", "ts": "…" },
    { "id": "S1", "type": "series", "text": "CHW supply 6.9→13.8 °C",
      "device_id": "77c…", "metric_key": "water_supply_temp", "instance": "CHW",
      "window": { "from": "…", "to": "…" },
      "summary": { "min": 6.9, "max": 13.8, "slope_per_h": 41.2, "unit": "C" } },
    { "id": "T1", "type": "edge", "text": "CH-DC1-02 → CHW header A (cooling)" },
    { "id": "C1", "type": "setpoint_change", "text": "CRAH-DC1-HA-03 air setpoint 18→20 °C", "ts": "…" },
    { "id": "M1", "type": "maintenance", "text": "No active window on chain devices" },
    { "id": "H1", "type": "similar_incident", "text": "2026-09-30 chiller_trip, confirmed",
      "href": "/analytics?view=anomaly&incident=…" }
  ]
}
```

- **Evidence types** follow doc 31 `EvidenceType`: `alert`, `finding`,
  `series`, `edge`, `setpoint_change`, `maintenance`, `similar_incident`,
  `doc`. Every item has a deterministic `text` one-liner.
- **ID prefixes:**

  | Prefix | Item |
  |---|---|
  | `A` | alert or alarm |
  | `F` | finding |
  | `S` | series |
  | `T` | topology edge |
  | `C` | setpoint change |
  | `M` | maintenance |
  | `H` | history |
  | `D` | doc snippet |
  | `X` | context (in the incident's `context`) |

  IDs are never renumbered for a given `bundle_sha256`.
- **Series are summaries only**: min, max, slope and last over a window,
  never raw samples. This bounds the bundle's size, and it is the egress
  rule for a cloud explainer (§7.4).
- **`bundle_sha256`** is the sha256 of the canonical JSON (sorted keys) of
  `items` plus `devices`. It is the cache key in §7.3.

### 5.4 `POST /anomalies/incidents/{id}/feedback` (bulk; doc 31 D10)

```json
{ "action": "acknowledge", "scope": "symptoms", "note": "Chiller trip being handled under CHG-5512" }
{ "action": "dismiss", "scope": "all", "reason": "expected_operation", "note": "Planned CHW changeover" }
```

**Fields:**

- **`action`**: `acknowledge`, `dismiss` or `confirm`. `reopen` is not
  offered in bulk: reopening is a per-finding judgement. Asking for it
  returns `422 action_not_allowed`.
- **`scope`**:
  - `symptoms` (the default): member findings with `role = symptom`.
  - `all`: every member finding, the root included.
  - `sensor`: member findings with `kind = sensor`, which are never roots.
- **`reason`** and **`note`** follow the same rules as §4.3.

**Behaviour:**

- **Findings only.** Bulk feedback applies to member findings. Member
  alarms are reached only through §4.4's sync on alert-lane findings. An L1
  alarm in the incident is never acknowledged in bulk, because an analytics
  grouping must not acknowledge a hard alarm (§1).
- **One savepoint per finding, never all-or-nothing.** This is the doc 21
  §10 rule: a bulk call that fails on row 3 must not throw away rows 1 and 2.
- **Rows already in the target state, or where the transition is illegal,**
  are reported and skipped. They do not fail the call.
- **Audit and fanout per finding:** one `anomaly.finding.feedback` audit row
  per finding changed, plus one `anomaly.incident.updated` frame
  (`change: "bulk_feedback"`). There is no frame per finding.

The response is always this row-level report, never a bare count:

```json
{
  "incident_id": "d0a4…",
  "succeeded": 9,
  "skipped": [ { "finding_id": "…", "state": "acknowledged", "error": "illegal_transition" } ],
  "failed": []
}
```

**Phasing.** Doc 31 D10 recommends no incident-level feedback in the first
UI phase, and the UI must never loop N single requests client-side. This
endpoint is specified now so that doc 33 can schedule it, recommended with
Phase 2, when incidents first exist. Until it ships, the UI offers per-finding
actions only.

## 6. Detectors, overrides, exclusions

### 6.1 `GET /anomalies/detectors`

The catalogue holds about 50 rows plus the `L5` switch row (doc 33 D13). It
is not paged and returns `{"items": [...]}`, like `GET /alarm-rules`
(`api/v1/alarms.py:197-202`).

```json
{
  "items": [
    {
      "id": "CR-1",
      "name": "CRAH supply over setpoint",
      "version": 1,
      "layer": "L2",
      "alarm_type": "crah_supply_over_setpoint",
      "device_types": ["crah"],
      "required_metrics": ["supply_air_temp", "air_setpoint_temp", "valve_position_pct", "equipment_state"],
      "enabled": true,
      "lane": "alert",
      "severity": "MINOR",
      "params": { "over_setpoint_k": 3.0, "valve_starved_pct": 95, "dwell_s": 900, "min_run_s": 600 },
      "param_schema": [
        { "key": "over_setpoint_k", "label": "Over setpoint", "unit": "K", "kind": "number",
          "min": 0.5, "max": 10, "step": 0.1, "default": 3.0,
          "help": "Supply above setpoint before the rule counts" },
        { "key": "valve_starved_pct", "label": "CHW valve starved at", "unit": "%", "kind": "number",
          "min": 50, "max": 100, "step": 1, "default": 95 },
        { "key": "dwell_s", "label": "Dwell", "unit": "s", "kind": "int",
          "min": 60, "max": 86400, "step": 60, "default": 900 },
        { "key": "min_run_s", "label": "Running at least", "unit": "s", "kind": "int",
          "min": 0, "max": 7200, "step": 60, "default": 600 }
      ],
      "devices_in_scope": 28,
      "inactive_devices": 0,
      "open_findings": 0,
      "confirmed": 2,
      "dismissed": 0,
      "precision": 1.0,
      "baseline": null
    },
    {
      "id": "L5",
      "name": "Cooling correlator",
      "version": 1,
      "layer": "L5",
      "alarm_type": null,
      "device_types": [],
      "required_metrics": [],
      "enabled": true,
      "lane": null,
      "severity": null,
      "params": { "major_enabled": false, "use_influence": false, "window_s": 600, "escalate_racks": 3 },
      "param_schema": [
        { "key": "major_enabled", "label": "Escalate roots to MAJOR", "kind": "bool", "default": false,
          "help": "Off until the Phase 2 gate passes (doc 33)" },
        { "key": "use_influence", "label": "Use fitted CRAH zones", "kind": "bool", "default": false },
        { "key": "window_s", "label": "Correlation window", "unit": "s", "kind": "int",
          "min": 60, "max": 3600, "step": 60, "default": 600 },
        { "key": "escalate_racks", "label": "Racks for escalation", "kind": "int",
          "min": 1, "max": 50, "step": 1, "default": 3 }
      ]
    }
  ]
}
```

**`param_schema` (doc 31 D9).** This is a `ParamSpec[]` with `key`,
`label`, `unit`, `kind` (`number`, `int`, `bool` or `enum`), `min`, `max`,
`step`, `options`, `default` and `help`.

- It comes from the detector class in `analytics/catalogue.py`, which is the
  only source of validation for `PATCH` and for override `params`.
- `default` is the seeded value (doc 29 §12, as corrected by doc 33 §3.7).
  Settings uses it for "reset to default".
- A key that is in `params` but has no spec renders read-only in the UI.

**The counters:**

| Field | Meaning |
|---|---|
| `devices_in_scope` | Devices of the type with every `required_metric` |
| `inactive_devices` | Devices of the type that lack one or more points. A detector disables itself per device in that case, which is G36's own rule. The count makes it visible |
| `confirmed`, `dismissed` | Counted from `verdict` over the current `version` |
| `precision` | `confirmed / (confirmed + dismissed)`, or `null` when nothing has a verdict (doc 29 §8.4) |
| `baseline` | `{fitted_at, days, n, refused}` for L3 and L4, otherwise `null` |

### 6.2 `PATCH /anomalies/detectors/{id}` (admin)

```json
{ "enabled": true, "params": { "over_setpoint_k": 2.5 } }
```

**Validation.**

- The body forbids unknown fields (`model_config = {"extra": "forbid"}`),
  like `RulePatch` (`api/v1/alarms.py:249-257`).
- `params` is a partial merge.
- A key that is not in `param_schema` returns `422 unknown_param`. A value
  out of range or of the wrong kind returns `422 param_out_of_range`. Both
  name the key.

**Versioning (doc 31 §18 item 3).**

- **A parameter change bumps `version`. Changing `enabled` does not.** The
  `L5` row is the exception: its parameters are switches, not detection
  maths, so no change to it bumps `version`.
- **A version bump resets the detector's accumulators.** On the next sweep,
  doc 29 §10.4's restore rule sees `detector_version` ≠ current and resets
  rather than resumes, so CUSUMs restart from zero.
- **Open findings keep their old version** and resolve normally. If the
  change makes their accumulated state meaningless, they are resolved with
  `resolution = 'superseded'` instead. An example is a change to a CUSUM's
  `k` or `h`.
- **What doc 31's Save dialog says:** "Saving starts version N+1. The
  detector re-learns its running totals, so slow-drift findings may take
  hours to reappear."

**Effects after commit.**

- The call runs `INCR` on `dcim:anomaly:catalogue:version` (doc 29 §10), and
  the worker reloads on its next sweep.
- Disabling a detector resolves its open findings with `detector_disabled`
  and clears their derived alarms through `clear_alarms`.
- `PATCH /detectors/L5 {"params": {"major_enabled": true}}` is the only way
  MAJOR escalation is switched on (§9).

**Audit and responses.**

- Audited as `anomaly.detector.edit`. An unknown id returns
  `404 unknown_detector`.
- A `200` returns
  `{"ok": true, "detector_id": "CR-1", "version": 2, "changed": ["params.over_setpoint_k"], "detector": {…}}`.
- A no-op returns `"changed": []`, as `edit_rule` does
  (`api/v1/alarms.py:284-286`).

### 6.3 Overrides

```
GET    /anomalies/overrides                      ?detector= &device_id= &limit &offset &with_total
PUT    /anomalies/overrides/{detector_id}/{device_id}
DELETE /anomalies/overrides/{detector_id}/{device_id}
```

These mirror `alarm_rule_override` and its endpoints
(`api/v1/alarms.py:381-456`), on the doc 29 §4.2 table.

```json
PUT { "enabled": false, "reason": "CRAH-DC1-HA-07 return sensor relocated; awaiting recal" }
PUT { "params": { "over_setpoint_k": 4.0 }, "reason": "end-of-row unit, sees bypass air", "expires_at": "2026-11-30T00:00:00Z" }
```

**Validation.**

- `params` are checked against `param_schema`.
- `enabled: false` with no `reason` returns `422 mute_needs_reason`. The
  database enforces the same rule with `ck_anomaly_override_mute_reason`.
- An override that sets nothing returns `422 override_noop`
  (`api/v1/alarms.py:418-419`).
- `expires_at` is optional. After it passes, the override is ignored
  (doc 29 §4.2).

**Effects.**

- A mute resolves the device's open findings for that detector with
  `muted`.
- The call runs `INCR` on the catalogue version.
- An unknown device returns `404 not_found`. An unknown detector returns
  `404 unknown_detector`.
- Audited as `anomaly.override.put` and `anomaly.override.remove`.

A list row:

```json
{ "detector_id": "CR-1", "device_id": "…", "device_name": "CRAH-DC1-HA-07",
  "enabled": false, "params": {}, "reason": "…", "expires_at": null,
  "updated_by": "jsmith", "updated_at": "2026-10-20T10:11:00+00:00" }
```

### 6.4 Baseline exclusions

```
GET    /anomalies/exclusions        ?device_id= &room= &detector= &source= &active= &from= &to= &limit &offset &with_total
POST   /anomalies/exclusions
DELETE /anomalies/exclusions/{id}
```

Rows are the doc 29 §4.3 table. **`GET` lists every source**:

| `source` | Written by |
|---|---|
| `maintenance` | the maintenance ticker, one row per target device |
| `confirmed_finding` | `confirm` on a process finding (§4.3) |
| `injected_fault` | campaign tooling, carrying the simulator ledger id (doc 32) |
| `manual` | operators |

```json
POST {
  "device_id": "1b0e…",
  "room_id": null,
  "datacenter_id": null,
  "detector_id": null,
  "metric_key": null,
  "starts_at": "2026-10-18T06:00:00+00:00",
  "ends_at":   "2026-10-18T09:30:00+00:00",
  "source": "injected_fault",
  "source_ref": "f-000193",
  "reason": "sim campaign: filter_loading ramp on CRAH-DC1-HA-03"
}
```

**Fields.**

- **Scope:** exactly one of `device_id`, `room_id` or `datacenter_id`,
  otherwise `422 exclusion_scope` (`ck_anomaly_baseline_exclusion_scope`).
- **Dates:** `ends_at` may be `null`, meaning still open. If it is set and
  not after `starts_at`, the call returns `422 exclusion_dates_invalid`.
- **Source:** a POST may carry `source` `manual` (the default) or
  `injected_fault`. `maintenance` and `confirmed_finding` are system-owned,
  so a POST with either returns `422 source_not_allowed`.
- **Idempotent replay:** a duplicate `(source, source_ref, scope)` returns
  `200` with the existing row (`ux_anomaly_baseline_exclusion_source`), so
  ledger imports can be replayed. A new row returns `201`. `created_by` is
  the caller.

**Delete and timing.**

- **`DELETE` of a system-owned row returns `409 exclusion_system_owned`.** A
  maintenance exclusion follows its window, and a confirmed-finding exclusion
  follows its finding. Deleting either by hand would let the next refit
  learn a fault as normal.
- An unknown id returns `404 not_found`.
- Changes take effect at the next nightly fit. They never rewrite a baseline
  that has already been fitted.
- Audited as `anomaly.exclusion.create` and `anomaly.exclusion.delete`.

## 7. Explain (L6)

### 7.1 `POST /anomalies/incidents/{id}/explain` (operator)

```json
{ "force": false }
```

| Condition | Status | Body |
|---|---|---|
| Incident unknown | 404 | `not_found` |
| `DCIM_ANOMALY_EXPLAINER=none` | **409** | `{"error":"explainer_disabled","message":"No explainer is configured; the evidence and template summary are complete without one."}` |
| Explain lock held (`dcim:anomaly:explain:{incident_id}`, doc 29 §10) | 409 | `explain_in_progress`, with `Retry-After` |
| `force=true` within 300 s of the last run, or the estate-wide hourly cap reached | 429 | `explain_rate_limited`, with `Retry-After` |
| Stored explanation matches the current `bundle_sha256` and `force=false` | 200 | stored, `"cached": true` |
| Model answered and passed validation | 200 | `"fallback_used": false` |
| Timeout, error or validation failure | 200 | `"fallback_used": true` |

The endpoint never returns `5xx` because the model failed: the template
fallback *is* the answer. A `5xx` means the platform itself failed. The UI
does not need to probe for the `409`, because `/health.explainer` says
whether the panel exists (doc 31 D6).

The response is the stored `anomaly_incident.explanation` (doc 29 §7.5)
plus three derived fields: `source`, `label` and `stale`.

```json
{
  "incident_id": "d0a4…",
  "cached": false,
  "explanation": {
    "v": 1,
    "source": "llm",
    "label": "AI-generated: candidates to check, not a diagnosis",
    "explainer": "cloud",
    "model": "<DCIM_ANOMALY_EXPLAINER_MODEL>",
    "temperature": 0,
    "bundle_sha256": "5f1c…",
    "prompt_sha256": "a90e…",
    "output": {
      "summary": "Chilled-water supply was lost when CH-DC1-02 tripped (A1)…",
      "hypotheses": [
        { "text": "Single-chiller trip with no standby stage-up", "confidence": 0.7,
          "supporting": ["A1", "S1", "F3"], "contradicting": [] },
        { "text": "CHW header flow loss (pump)", "confidence": 0.15,
          "supporting": ["S1"], "contradicting": ["S4"] }
      ],
      "checks": [ "Confirm CH-DC1-03 staged; check its equipment_state (S5)" ],
      "unknowns": [ "No pump differential pressure in the bundle" ]
    },
    "validator": { "ok": true, "rejected_reason": null, "errors": [] },
    "fallback_used": false,
    "explanation_sha256": "c3d1…",
    "generated_at": "2026-10-21T08:13:40+00:00",
    "stale": false
  }
}
```

When `fallback_used` is true:

- `source` is `"template"`.
- `output.summary` is the bundle's template `summary`, and the hypotheses,
  checks and unknowns are empty.
- `validator.rejected_reason` is `timeout`, `explainer_error`,
  `schema_invalid` or `validation_failed`.

### 7.2 `POST /anomalies/incidents/{id}/explain/feedback` (doc 31 D8)

```json
{ "useful": true, "note": "Matched what the plant engineer found" }
```

- **Path and body match doc 31 D8 and doc 33 task 5.3.**
- **Storage:** the vote is appended to `anomaly_incident.explanation_feedback`
  (doc 29 §4.6) as `{at, by, vote: "up"|"down", explanation_sha256, note}`.
  `useful: true` is stored as `up`.
- **Tied to the text shown.** `explanation_sha256` is the current
  explanation's, so a later re-explanation does not inherit the votes.
- **One vote per user per `explanation_sha256`.** A repeat replaces the
  user's earlier vote.
- **No explanation stored** returns `409 no_explanation`.
- **Audit:** `anomaly.explanation.feedback`.
- **Response `200`:** `{"ok": true, "up": 3, "down": 1}`, counted over the
  current `explanation_sha256`.

> Doc 29 §4.6's column comment names this path
> `/incidents/{id}/explanation/feedback`. This spec uses doc 31's
> `/explain/feedback`. The comment should be updated; the column is
> unaffected.

### 7.3 Timeouts, concurrency, idempotency

**Timeout.** There is one setting, `DCIM_ANOMALY_EXPLAINER_TIMEOUT_S`, with
a default of 30 (doc 29 §11, D2). The connect timeout is
`min(5, timeout)`. One retry is allowed, on a connect error or a 5xx only,
inside the same budget. A local 7–8B model on CPU needs the setting raised,
typically to 90. That is a deployment choice, not a second code path.

**One run per incident.** The explain lock is `SET NX` on
`dcim:anomaly:explain:{incident_id}`, with a TTL of the timeout plus 30 s
(doc 29 §10). A second caller gets `409 explain_in_progress`.

**Where it runs.** The call runs in the API process, async, using the
existing `httpx` dependency (`backend/pyproject.toml:21`). It never runs in
the ingest worker. **[Judgement]**: a slow model must not steal ingest ticks
on the 7.7 GB host. Doc 33 Phase 5 sets the exit criterion "explain p95 <
20 s and never blocks a sweep".

**Hourly cap.** The estate-wide cap is 60 explains per hour. It exists to
stop loops, not to save money.

**Idempotency.** The key is `(incident_id, bundle_sha256, explainer,
model)`. A match with `force=false` returns the stored explanation without
calling the model.

**Staleness.** `GET /incidents/{id}` returns the stored explanation with
`stale: true` once the incident's `bundle_sha256` has moved on. The UI then
offers "re-explain". The platform never re-explains automatically under this
spec. Doc 29's lock row mentions an "on-open explain". If doc 33 Phase 5
turns that on, it runs as an after-commit task in the API, never inside
`run_sweep`, and it takes the same lock.

### 7.4 Guardrails (doc 28 L6, made testable)

1. **The bundle is the only input.**
   - The prompt is the system instruction plus the bundle JSON inside a
     delimited block.
   - Device names, alarm messages and operator notes are data, never
     instructions. The validator does not trust them either.
   - The bundle holds summaries only (§5.3), which is the egress control for
     a cloud explainer.
   - The operator can see exactly what is sent, because
     `GET /incidents/{id}` returns the same `evidence`.
2. **Schema-constrained output.**
   - `summary`: at most 1200 characters.
   - `hypotheses[]`: at most 5, each with `text`, `confidence` in [0,1],
     `supporting[]` and `contradicting[]`.
   - `checks[]`: at most 8.
   - `unknowns[]`: at most 8.
   - Output that fails to parse falls back with `schema_invalid`.
3. **Citation validation.**
   - Every cited ID must exist in `bundle.items`.
   - Every hypothesis needs at least one supporting ID.
   - Free text must not name an estate device that is absent from
     `bundle.devices`, checked by exact match against estate device names.
   - Any failure falls back with `validation_failed`. `validator.errors`
     lists each problem, for example `"unknown evidence id Z9"`.
4. **Read-only tools only.** If tools are given, they are:
   - `metric_window(device_id, metric, instance, from, to)`, which returns a
     summary;
   - `topology_neighbours(device_id, layer)`;
   - `runbook_search(q)`.

   Each tool rejects any device that is absent from `bundle.devices`, and a
   run makes at most 4 tool calls. **No tool writes**: there is no path to
   BACnet, Modbus or SNMP SET, plant overrides, setpoints, device actions, or
   alarm or finding state. `test_anomaly_explainer_readonly.py` (doc 33
   Phase 5) checks this statically.
5. **Never in the alarm path.** `analytics/explainer.py` is imported only by
   `api/v1/anomalies.py`. An import test checks that it is not reachable from
   `analytics/sweep.py`, `analytics/correlator.py` or `ingest/worker.py`.
6. **Audit (doc 29 §7.5).**
   - Every run writes `audit_log` with `action = 'anomaly.explain'`,
     `before = null` and `after = {prompt, raw_output}`. Rejected output is
     included, so the full trail lives in the existing audit table.
   - `anomaly_incident.explanation` keeps only the hashes, the validated
     output and the validator result.
   - Temperature is 0.

**Vendor note.**

- **"Cloud"** means a hosted API, for example Anthropic's Messages API. Do
  not hardcode a model id: take it from `DCIM_ANOMALY_EXPLAINER_MODEL`, and
  check the provider's current model list and pricing at build time.
- **"Local"** means an OpenAI-compatible `/v1/chat/completions` endpoint at
  `DCIM_ANOMALY_EXPLAINER_URL`. llama.cpp server, vLLM and Ollama all expose
  one.
- **Schema enforcement differs.** JSON-schema-constrained decoding is
  vendor-specific and available on some local servers but not all. The
  validator in item 3 is the guarantee either way.

## 8. Websocket events

The anomaly events use the existing transport, unchanged:

| Piece | Where |
|---|---|
| Ticket and handshake | `POST /ws/ticket`, then `/ws?ticket=` (`api/v1/ws.py:32-84`) |
| Publish path | Redis pub/sub on `dcim:ws:<topic>` through `Fanout` (`ingest/fanout.py:26, 122-126`) |
| Subscribe | `{"op":"subscribe","topics":[…]}` (`api/v1/ws.py:97-105`) |

**Topic: `anomalies`.** This confirms the topic doc 31 D3 assumed, and doc 33
task 1.11 uses the same name. The hub accepts any topic string
(`websocket/hub.py:95-104`). Finding events also go to `device:{device_id}`,
so an open device page refreshes its band and badge. Incident events go to
`anomalies` only.

> **Contract deviation (naming style).** Existing frames use snake_case names:
> `alarm_created`, `telemetry_update`, `device_status_change`
> (`ingest/fanout.py:64, 94, 105`). The contract's dotted names are kept,
> and doc 31 D3 agrees. They work unchanged, because `useFrames` matches the
> exact string (`frontend/src/ws/useSocket.ts:39-45`) and `Frame.event` is a
> plain `string` (`frontend/src/ws/client.ts:8-11`). Doc 11 should record
> that the dotted form is deliberate.

The envelope is `{"event": <type>, "<object>": {...}}`, the same as
`{"event": kind, "alarm": alarm}` (`ingest/fanout.py:105`). The `finding`
and `incident` objects are the list rows from §4.1 and §5.1.

```json
{ "event": "anomaly.finding.opened",   "finding":  { "…": "§4.1 row" } }
{ "event": "anomaly.finding.updated",  "change": "acknowledged", "via": "alarm", "finding": { "…": "§4.1 row" } }
{ "event": "anomaly.finding.resolved", "finding":  { "…": "§4.1 row, state resolved, resolution set" } }
{ "event": "anomaly.incident.opened",  "incident": { "…": "§5.1 row" } }
{ "event": "anomaly.incident.updated", "change": "member_added", "incident": { "…": "§5.1 row" } }
```

**`change` values.**

| Event | Values |
|---|---|
| `finding.updated` | `acknowledged`, `dismissed`, `confirmed`, `reopened`, `promoted` (advisory → alert lane), `kind_changed`, `incident_attached`, `score_band`, `bulk` |
| `incident.updated` | `member_added`, `root_changed`, `escalated` (root alarm raised to MAJOR, §9), `bulk_feedback`, `explained`, `resolved` |

- `via` is present on `acknowledged` (§4.4).
- The contract has no `incident.resolved` event, so resolution is `updated`
  with `change: "resolved"`.

**Rate limiting and coalescing.** These are done at the publisher, because
the hub coalesces only `telemetry_update` (`websocket/hub.py:34, 142-150`).

- **No "still true" frames.** A change that touches only `last_seen`,
  `occurrences` or score drift publishes nothing. `score_band` fires only
  when the score crosses ×1, ×2 or ×4 of the detector's threshold.
- **One frame per object per sweep.** The sweep collects frames in a dict
  keyed by `(object_type, id)`, last write wins, and publishes them after
  commit.
- **At most 50 finding frames per sweep, plus one summary** (doc 33 task
  1.11, `test_anomaly_fanout_cap.py`). Above the cap, the rest are dropped
  and one `{"event":"anomaly.finding.updated","change":"bulk","count":N}` is
  sent. Incident frames are never capped.
  - Clients re-fetch over REST on any anomaly frame, because REST is the
    socket's source of truth (`frontend/src/ws/client.ts:1-6`).
  - The cap exists because each session queue holds 256 frames, and a full
    queue disconnects the session (`websocket/hub.py:46, 170-178`).
- **Derived alarms keep the existing alarm frames.** They go out through
  `fanout.alarm` as `alarm_created`, `alarm_updated` and `alarm_cleared`
  (`ingest/fanout.py:99-111`), never coalesced and never capped, like every
  other alarm. The matching anomaly frame is separate, and it is capped.
- **API-side changes publish after commit.** Feedback, bulk feedback,
  PATCH, explain and thumbs publish their frames after commit through a
  `Fanout(Redis)` built in the API.
  - The finding → alarm acknowledgement (§4.4) also publishes
    `alarm_updated`. The existing `POST /alarms/{id}/acknowledge` publishes
    nothing (`api/v1/alarms.py:98-123`); this call fills that gap for this
    path only.
  - Doc 28 cites `alarms.py:98-169` as "platform and discovery alarms do not
    publish". Those lines are actually the manual acknowledge and clear
    handlers. The conclusion for the detector holds: the sweep must use
    `raise_alarm` with the worker's fanout and outbox.

## 9. How derived alerts flow through alarms, websocket and Jira

```
run_sweep (worker step; DCIMA xact lock + cadence gate, doc 29 §10.1; one unit_of_work)
  ├─ alert-lane finding opens ─► repo.raise_alarm(source="analytics",
  │                                alarm_type=detector.alarm_type,      # snake_case, doc 29 §9.3
  │                                severity=min(detector.severity, DERIVED_SEVERITY_CAP),
  │                                metric_key, value=observed, threshold,
  │                                category=BY_ALARM_TYPE[alarm_type],
  │                                attributes={"anomaly": {...}})
  │                             └─ AlarmAction(kind, alarm)            (alarms/service.py:64-66)
  ├─ finding.alarm_id = alarm id
  ├─ finding resolves ─► repo.clear_alarms(..., by="analytics") ─► AlarmAction("alarm_cleared")
  ├─ alarm ACKNOWLEDGED while finding open ─► finding acknowledged, via "alarm"   (§4.4)
  ├─ L5: correlation.mark_symptom(derived alarm → root alarm)          (derived only, §1)
  ├─ L5: if L5.major_enabled and ≥ escalate_racks racks impacted:
  │        raise_alarm(... severity="MAJOR") on the ROOT's existing alarm key  (escalates in place)
  ├─ repo.refresh_device_alarm_state(session, touched_devices)          (doc 29 D12)
  ├─ integration_outbox.enqueue_actions(session, actions)               (same transaction)
  └─ commit
after commit:
  ├─ fanout.alarm(action.kind, action.alarm)  → alarms, alarms:critical, device:{id}
  └─ anomaly frames (coalesced, ≤ 50 + 1)     → anomalies, device:{id}
```

This follows the template of `_maybe_sweep_staleness`
(`ingest/worker.py:461-491`): the sweep runs, the outbox is enqueued in the
same transaction, and fanout happens after commit.

**What the derived alarm inherits from `raise_alarm`:**

| Inherited behaviour | Where |
|---|---|
| Dedup on `(device, alarm_type, instance)` | `repositories/alarms.py:308` |
| Raise-time shelving by an active maintenance window, or by the `installed` lifecycle | `:261-300` |
| `detection = derived` | `:364` |
| Severity escalates freely, and de-escalates only from the same source | `:326-332` |
| The `change` key (`created`, `escalated`, `touched`) | `:370-375` |
| Response class `alert` at MINOR or WARNING | `core/alert_taxonomy.py:285-305` |

The existing correlator returns `None` for these alarm types
(`alarms/correlation.py:378-397`), so L5 is the only correlator that touches
them.

**Alarm types** are the snake_case condition names in doc 29 §9.3, for
example `crah_supply_over_setpoint` (CR-1) and `cdu_condensation_risk`
(CD-6). They are not the detector id, which travels in
`attributes.anomaly.detector_id` (doc 33 D4). Each type needs a
`BY_ALARM_TYPE` entry (`core/alert_taxonomy.py:328`) and an entry in the
test's `NON_TRAP_ALARM_TYPES`, in the same PR.

**Severity cap** (doc 33 §1 item 2, D5, tasks 0.11, 1.18 and 2.4):

- **Every `source="analytics"` raise is capped at MINOR.** The cap is
  `DERIVED_SEVERITY_CAP = "MINOR"` in `analytics/catalogue.py`, applied in
  `analytics/sweep.py` as `min(detector.severity, cap)`. No detector or
  promotion can raise above it. This includes PU-3, which doc 28 called a
  "MAJOR candidate".
- **MAJOR comes only from L5 escalation.** With confirmed impact (at least
  `escalate_racks` racks with RM-2 or RM-3 under one root), L5 escalates the
  **root's existing alarm** to MAJOR in place. That is the same upsert key,
  through the escalate-freely clause (`repositories/alarms.py:326-332`).
  There is no new alarm type.
- **Escalation is off until the Phase 2 gate.** It is guarded by the `L5`
  catalogue row's `params.major_enabled`, which is `false` until the gate
  passes. It is switched with `PATCH /anomalies/detectors/L5` (admin,
  audited).
- **`/health` shows both switches**, as `severity_cap` and
  `l5_major_enabled` (§11), so the state of the cap is never a guess.

**Jira: none by default.** The default policy requires `response_class` in
`["alarm"]` and `severity ≥ MAJOR` (`integrations/config.py:68-94`;
`integrations/policy.py:58-72`). A MINOR derived alert fails the first
clause, and `policy.explain` says so in words. There are two existing ways
in, and a third that follows from escalation:

1. **Opt in per alarm type.** Add the type to the integration's
   `policy.alarm_types`, for example
   `["crah_supply_over_setpoint","cdu_condensation_risk"]`.
   - A listed type skips the class, severity and category clauses.
   - The symptom, shelved and 300 s dwell clauses still apply
     (`integrations/policy.py:54-57, 78-97`), so a derived symptom under an
     incident root still does not open a ticket.
   - `POST /integrations/{id}/policy/preview` (`api/v1/integrations.py:266`)
     shows what would match.
2. **One-off.** An operator can use `POST /alarms/{id}/ticket`
   (`api/v1/integrations.py:499`).
3. **Escalation.** Once `L5.major_enabled` is on, an escalated root alarm is
   MAJOR with response class `alarm`, so it matches the default policy after
   300 s. That is deliberate: MAJOR means act now. It is why the switch is
   admin-only and off until the gate.

Clears follow the outbox's own rule. A clear is always a candidate, so an
opted-in ticket closes when the condition clears
(`integrations/outbox.py:66-79`).

**Advisories never reach the outbox.** L3 and L4 findings are not alarms.
They raise only when promoted to the alert lane on L2 corroboration
(doc 29 §9.1), and then they are capped at MINOR like everything else.

## 10. `GET /anomalies/expected`: the expected band

| Param | Type | Notes |
|---|---|---|
| `device_id` | uuid, required | |
| `metric` | registry key, **repeatable, optional** | Doc 31 D7b. If omitted, every modelled series for the device is returned |
| `instance` | string, optional | When given, it applies to every `metric`. When omitted, every instance with a baseline is returned |
| `from` | datetime, required | `Query(alias="from")`, because `from` is a Python keyword |
| `to` | datetime, default now | |
| `interval` | `auto` \| `1m` \| `5m` \| `1h` \| `1d` | Doc 31 D7a. The UI passes the `interval` that `/history` reported, so the two line up bucket for bucket |
| `detector` | detector id, optional | Picks a baseline when more than one covers a series |
| `k` | float 0.5–6, optional | The band half-width in σ. Defaults to the detector's `params.k_band`, or 3 if it has none |

> **Contract deviation (additive).** `interval`, the repeatable `metric`,
> `detector` and `k` are not in the contract. The parameters keep the
> contract's names, `from` and `to`, which doc 31 §16's `expectedBand()` also
> uses. An earlier draft of this spec recommended renaming them to
> `start`/`end` to match `/devices/{id}/history` (`api/v1/devices.py:197-198`).
> That recommendation is **withdrawn**, so docs 30 and 31 agree. The client
> maps names in one place.

**Resolution rules.** The band is computed at the bucket of the history
series it overlays.

- With an explicit `interval` (the normal case, D7a), that bucket is used,
  subject to the retention step-up in the table below.
- With `interval=auto`, the bucket comes from
  `repositories/telemetry.choose_source` (`repositories/telemetry.py:57-80`).
  It targets about 200 points (`:36`) on the ladder 1m → 5m → 1h → 1d
  (`:46-51`).

| Window (auto) | Bucket | Regressor source | σ used |
|---|---|---|---|
| ≤ 3 h 20 min | 1m | `telemetry_1m` | `resid_sigma["5m"]`, with `sigma_source: "5m"` (no 1m σ is fitted) |
| ≤ 16 h 40 min | 5m | `telemetry_5m` | `resid_sigma["5m"]` |
| ≤ 8.3 days | 1h | `telemetry_1h` | `resid_sigma["1h"]` |
| longer | 1d | `telemetry_1d` | `resid_sigma["1d"]` |
| `from` older than the bucket's retention | step to the next coarser bucket that covers it | 1m keeps 7 d, 5m 2 y, and 1h/1d forever | as stepped |

Five constraints apply:

- **Raw is never served.** Residual σ is not defined at poll resolution. An
  explicit `interval=raw` returns `422 window_invalid`. `/history` at `raw`
  is a short-window case the band does not cover.
- **σ is per resolution.** Doc 29 §7.3 stores `resid_sigma` keyed `5m`,
  `1h` and `1d`. Averaging shrinks noise, so 1h σ on 1m points would draw a
  band that is much too narrow.
- **Regression models need their regressors at the same bucket.** An
  example is COP from `compressor_load_pct` and `outdoor_wet_bulb_temp`. If
  a regressor is missing for a bucket, that bucket's point is null. It is
  not interpolated.
- **Hour-of-week profiles** look up the bucket's hour of the week. A 1d
  bucket uses the mean of that day's 24 hourly values.
- **Model kinds served** are those in doc 29 §7.3 (profile, OLS, slope,
  curve, first-order, peer), plus `rule_limit` for L2 rules that compare
  against a moving limit. For CR-1, `upper = air_setpoint_temp +
  over_setpoint_k` and `lower` is null. That gives the Telemetry tab a useful
  line from Phase 1 [judgement].

Response `200`, always `{items: ExpectedBand[]}` (doc 31 D7b):

```json
{
  "items": [
    {
      "device_id": "…", "metric_key": "cop", "instance": "", "unit": "ratio",
      "detector_id": "CH-2", "detector_version": 1, "kind": "ols",
      "k": 3.0, "interval": "1h", "sigma_source": "1h",
      "fit_from": "2026-08-01T00:00:00+00:00", "fit_to": "2026-10-20T23:00:00+00:00",
      "fitted_at": "2026-10-21T03:02:55+00:00", "n": 1940, "r2": 0.81,
      "points": [
        [1792486800000, 4.92, 5.31, 5.70],
        [1792490400000, null, null, null]
      ],
      "reason": null,
      "first_fit_due": null,
      "alternatives": [ { "detector_id": "PL-2", "kind": "ols" } ]
    },
    {
      "device_id": "…", "metric_key": "approach_temp", "instance": "COND", "unit": "K",
      "detector_id": "CH-3", "detector_version": 1, "kind": null,
      "k": 3.0, "interval": "1h", "points": [],
      "reason": "insufficient_history", "first_fit_due": "2026-11-04T03:00:00+00:00"
    }
  ]
}
```

- **Points** are `[epoch_ms, lower, expected, upper]` (doc 31 §16).
  - A null triple is a gap, for example a missing regressor or a thin
    bucket.
  - A `rule_limit` band has `expected = null` and only the bound or bounds
    that the rule defines.
  - Observed values are not repeated. The UI already has them from
    `/history`, which it fetches first.
- **Points before `fit_from`** use the current model. Older model versions
  are not replayed. The UI may shade that span using `fit_from`.
- **No band is not an error (doc 31 D7c).** Such an item returns
  `points: []` and a `reason`, so the empty state can say why:

  | `reason` | Meaning |
  |---|---|
  | `no_baseline` | No detector models this series |
  | `insufficient_history` | `first_fit_due` gives when the first fit is expected |
  | `detector_disabled` | The detector is switched off |
  | `refused` | The fitter rejected thin data |
  | `feature_disabled` | `DCIM_ANOMALY_ENABLED=false` |

- **Status codes.**
  - A `metric` that has no baseline still returns an item with a reason.
  - `404 not_found` only for an unknown device.
  - `422 window_invalid` when `to <= from`, the window is over 400 days, or
    `interval=raw`.
  - `422 unknown_metric` for a key that is not in the registry.
- **No HTTP caching.** Points are capped at `MAX_ROWS` (10,000) per band,
  the same as history (`repositories/telemetry.py:40`).

## 11. `GET /anomalies/health`

The top-level fields are doc 31's `AnomalyHealth` (D6) plus the two switch
states from doc 33. `detail` carries the operational figures.

```json
{
  "enabled": true,
  "explainer": "none",
  "sweep_s": 60,
  "last_sweep_at": "2026-10-21T08:41:01+00:00",
  "last_sweep_ms": 842,
  "findings_per_hour": 2,
  "budget_per_hour": 4,
  "lock_holder": "ingest-1:31",
  "baseline_last_fit_at": "2026-10-21T03:04:11+00:00",
  "severity_cap": "MINOR",
  "l5_major_enabled": false,
  "detail": {
    "sweep": { "p95_ms_1h": 1310, "runs_1h": 60, "gate_skips_1h": 58, "errors_1h": 0,
               "last_error": null, "series_scored": 3480 },
    "baselines": { "last_fit_duration_s": 214, "series_fitted": 412, "rejected": 3 },
    "findings": { "open": 41, "opened_24h": 19,
                  "by_lane": { "advisory": 37, "alert": 4 },
                  "by_kind": { "sensor": 9, "process": 26, "undetermined": 6 } },
    "incidents": { "open": 1 },
    "explain": { "runs_1h": 0, "fallbacks_1h": 0 }
  }
}
```

- **`lock_holder`** and the `last_sweep_*` fields come from the
  `dcim:anomaly:sweep` heartbeat (doc 29 §10): its `consumer`,
  `finished_at` and `duration_ms`. Postgres cannot answer "who swept last",
  because `pg_try_advisory_xact_lock` is visible in `pg_locks` only while
  the transaction is open (`services/assigner.py:135`). If the heartbeat
  key has expired (600 s), `last_sweep_at` is null, which the UI shows as
  "no sweep in 10 min".
- **`gate_skips_1h`** should sit near `runs_1h × (replicas − 1)` on the
  two-replica deploy (`deploy/docker-compose.yml:113`). Those skips come
  from the cadence gate (doc 29 §10.1), and this is how doc 33 Phase 0's
  "exactly one sweep" criterion is observed.
- **`budget_per_hour`** is the advisory budget (doc 33 §7). Null if no
  budget is set.
- **`severity_cap`** is `DERIVED_SEVERITY_CAP` (doc 33 task 1.18).
  **`l5_major_enabled`** is the L5 row's switch (§9).
- **With `enabled: false`**, the numeric fields are 0 or null. `explainer`
  still reports the configured value.

## 12. Error keys

| Key | Status | Cause |
|---|---|---|
| `not_found` | 404 | Unknown finding, incident, exclusion, override or device |
| `unknown_detector` | 404 | Detector id not in the catalogue |
| `unknown_enum` | 400 | Filter value not in §3 |
| `unknown_metric` | 422 | `/expected` metric key not in the registry |
| `illegal_transition` | 409 | §4.3 state machine. Carries `state` and `allowed` |
| `reason_required` | 422 | `dismiss` without a `DismissReason` |
| `reason_not_allowed` | 422 | `reason` on an action other than `dismiss` |
| `action_not_allowed` | 422 | Bulk `reopen` (§5.4) |
| `unknown_param` | 422 | `params` key not in `param_schema` |
| `param_out_of_range` | 422 | Value outside its `ParamSpec` |
| `mute_needs_reason` | 422 | Override `enabled:false` without `reason` |
| `override_noop` | 422 | Override that sets nothing |
| `exclusion_scope` | 422 | Not exactly one of device, room or datacenter |
| `exclusion_dates_invalid` | 422 | `ends_at <= starts_at` |
| `source_not_allowed` | 422 | POST exclusion with a system-owned `source` |
| `exclusion_system_owned` | 409 | DELETE of a `maintenance` or `confirmed_finding` exclusion |
| `window_invalid` | 422 | `/expected`: `to <= from`, window over 400 days, or `interval=raw` |
| `explainer_disabled` | 409 | `DCIM_ANOMALY_EXPLAINER=none` |
| `explain_in_progress` | 409 | Explain lock held |
| `explain_rate_limited` | 429 | `force` within 300 s, or the hourly cap reached |
| `no_explanation` | 409 | Thumbs on an incident with no stored explanation |

## 13. Frontend API client changes (`frontend/src/api/client.ts`)

All changes are additive. No existing type or method changes. `Alarm`
(`client.ts:1753-1790`) already has `detection`, which is enough to decide
whether to load the evidence panel.

- Doc 31 §16 holds the TypeScript.
- The types go after the alarm types (around `client.ts:2672`).
- The methods go in `export const api` (`client.ts:3890`).
- Lists use `Page<T>` (`client.ts:1813`).

Doc 31 §16 needs the corrections below to match this spec. §15 summarises
them.

| Doc 31 §16 item | Correct to |
|---|---|
| `CoolingTier = 'plant'\|'chw_loop'\|'terminal'\|'zone'\|'server'\|'power'` | `'power'\|'plant'\|'loop'\|'unit'\|'zone'\|'server'\|'room'` (doc 29 `ck_anomaly_incident_tier`) |
| `DetectorLayer` | Add `'L5'` for the catalogue switch row |
| `AnomalyFinding` | Add `summary`, `verdict`, `raised_at`, `reopen_count`, `resolved_at`, `resolution`, `room_id` |
| `FindingFeedback` | Add `from_state`, `to_state`, `detector_version`, `via` |
| `AnomalyIncident` | Add `title`, `root_ref`, `last_activity_at`, `suppressed_symptoms` and `has_explanation`, and use `opened_at`/`resolved_at` (not `opened`/`closed`), as stored in doc 29 |
| `IncidentMember` | Flat (`type`, `id`, `is_root`, `parent_id`, `tier`, `role`, `inferred`, `label`, `ref`, …) rather than nested `finding`/`alarm` objects. The detail pane fetches the finding or alarm by id when opened |
| `EvidenceBundle` | `{v, bundle_sha256, built_at, summary, devices, items}` |
| `Explanation` | The §7.1 shape (`source`, `output{…}`, `validator`, `fallback_used`, `explanation_sha256`, `stale`). Doc 31's `status: 'ok'\|'rejected'\|'error'` maps to `fallback_used` plus `validator.rejected_reason` |
| `ExpectedBand` | Add `kind`, `sigma_source`, `fitted_at`, `r2` and `alternatives`. Points may contain `null` |
| `AnomalyExclusion` | `starts_at`/`ends_at` (not `from`/`to`), `source: 'maintenance'\|'injected_fault'\|'confirmed_finding'\|'manual'`, `source_ref`, `datacenter_id`, `detector_id` and `metric_key` |
| `AnomalyOverride` | Add `expires_at` |
| `AnomalyDetector` | Add `alarm_type` and `precision`; `baseline` per §6.1 |
| `AnomalyHealth` | Add `severity_cap`, `l5_major_enabled` and `detail`. `lock_holder` is a string |
| `anomalyFindings` | Response is `Page<AnomalyFinding> & {facets?}` (`with_facets=true`) |
| New methods | `incidentFeedback(id, {action, scope, reason?, note?})` → §5.4 report. `explanationFeedback(id, {useful, note?})` |

**Websocket.**

- Subscribe to `anomalies` on the analytics view. Device pages rely on the
  existing `device:{id}` subscription.
- Use `useInvalidateOn` (`frontend/src/ws/useSocket.ts:58`) with the five
  event names against the doc 31 query keys.
- An `alarm_updated` frame with `via` also invalidates `['alarms']` and
  `['alarm', id]`, as doc 31 §16 already lists for feedback.
- Do not merge frame payloads into the cache.

## 14. Acceptance

Setup:

```
API=http://127.0.0.1:8000/api/v1
TOKEN=$(curl -s -X POST $API/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin1234"}' | jq -r .token)
H="Authorization: Bearer $TOKEN"; J='Content-Type: application/json'
VIEWER=$(cd backend && python -c "from app.core.security import issue_token; print(issue_token('v','viewer'))")
```

Use `127.0.0.1`, not `localhost`. `localhost` can resolve to `::1` first,
and that answers unreliably (`scripts/dev.sh:62-64`).

**Shape and auth**

- [ ] `curl -s "$API/anomalies/findings?with_total=true&with_facets=true" -H "$H" | jq 'keys'` returns `["facets","items","next_cursor","total"]`.
- [ ] Paging with `limit=10` and offsets 0, 10, 20… returns every id exactly once.
- [ ] `?lane=bogus` returns `400`, with `.detail.error == "unknown_enum"`.
- [ ] With the viewer token, `PATCH /anomalies/detectors/CR-1`, `POST …/feedback` and `POST …/incidents/{id}/feedback` return `403`. Every GET, including `/health`, returns `200`.
- [ ] **No existing shape changed.** The `jq 'keys'` of `GET /alarms?limit=1` and of `GET /alarms/{id}` match their pre-deploy captures.
- [ ] With `DCIM_ANOMALY_ENABLED=false`, every GET returns `200` with empty items. `/health` returns `enabled:false` and the configured `explainer`.

**Feedback**

- [ ] `curl -s -X POST "$API/anomalies/findings/$F/feedback" -H "$H" -H "$J" -d '{"action":"acknowledge"}'` returns `200` with `state=acknowledged`. Repeating it returns `409 illegal_transition` with `allowed == ["dismiss","confirm","reopen"]`.
- [ ] `-d '{"action":"dismiss"}'` returns `422 reason_required`. `-d '{"action":"confirm","reason":"duplicate"}'` returns `422 reason_not_allowed`.
- [ ] A parametrised test covers all 20 cells of the §4.3 table.
- [ ] Confirm on a `confirmed` finding returns `409`. Reopen followed by dismiss succeeds, and leaves two feedback elements and two audit rows.
- [ ] After `dismiss` and the condition then clearing, the row shows `state=resolved`, `verdict=dismissed`, `resolution=cleared`.
- [ ] Confirming a `process` finding writes one `confirmed_finding` exclusion. `DELETE` on it returns `409 exclusion_system_owned`.
- [ ] Two concurrent `acknowledge` calls give exactly one `200` and one `409`.

**One acknowledgement (§4.4)**

- [ ] Acknowledging an alert-lane finding sets its alarm to `ACKNOWLEDGED` in the same transaction. One `alarm_history` row and one `alarm.acknowledge` audit row are written, and `alarm_updated` and `anomaly.finding.updated` (`via:"finding"`) are published.
- [ ] `POST /alarms/{id}/acknowledge` on a derived alarm leaves its finding `acknowledged` within one sweep, with a feedback element `via:"alarm"`.
- [ ] Dismissing an alert-lane finding leaves its alarm `ACTIVE`.

**Incident bulk feedback (§5.4)**

- [ ] With 10 symptom findings, 1 of them already acknowledged, `{"action":"acknowledge","scope":"symptoms"}` returns `succeeded:9` and one `skipped`. It writes 9 audit rows and one `anomaly.incident.updated` frame (`bulk_feedback`).
- [ ] No L1 alarm in the incident changes state.
- [ ] `{"action":"reopen"}` returns `422 action_not_allowed`.

**Derived alerts, cap, websocket, Jira**

- [ ] CR-1 campaign: stick a CRAH CHW valve at 100% and raise supply temperature (doc 32). `GET /alarms?detection=derived` lists `alarm_type=crah_supply_over_setpoint`, `source=analytics`, `response_class=alert` and `severity=MINOR`. `GET /anomalies/findings?alarm_id=<id>` returns one `lane=alert` finding.
- [ ] Websocket subscribers on `alarms` and `anomalies` each receive `alarm_created` and `anomaly.finding.opened` exactly once, after the REST row exists.
- [ ] `test_anomaly_severity_cap.py`: no analytics raise exceeds MINOR while `l5_major_enabled=false`. `/health` shows `severity_cap:"MINOR"`.
- [ ] After `PATCH /detectors/L5 {"params":{"major_enabled":true}}` (admin), an all-chillers rerun escalates the root's existing alarm to MAJOR in place (same alarm id). `anomaly.incident.updated` (`escalated`) is published.
- [ ] With an active integration on the default policy, no `integration_outbox` row is written for a MINOR derived alarm. `policy/preview` explains why. Adding `"crah_supply_over_setpoint"` to `policy.alarm_types` makes it match after 300 s.
- [ ] A 60-sweep soak with no state changes publishes zero anomaly frames. 120 openings in one sweep produce ≤ 50 finding frames plus 1 `bulk` frame, and no session closes with code 1013.
- [ ] `SELECT count(*) FROM alarm s JOIN alarm r ON r.id=s.root_cause_alarm_id WHERE r.source='analytics' AND s.source<>'analytics'` returns 0.

**Expected band**

- [ ] `curl -s "$API/anomalies/expected?device_id=$CRAH&metric=supply_air_temp&metric=fan_speed_pct&from=2026-10-20T00:00:00Z&interval=1h" -H "$H" | jq '.items|length'` returns `2`. The CR-1 `rule_limit` band's timestamps equal the bucket timestamps of `/devices/$CRAH/history?…&interval=1h`.
- [ ] Omitting `metric` returns every modelled series. A series with no baseline returns an item with `points: []` and `reason: "no_baseline"`. An unknown device returns `404`.
- [ ] With `interval=auto`: a 4 h window gives `5m`, 2 h gives `1m` with `sigma_source:"5m"`, and 30 days gives `1d`. A 1 h window that starts 10 days ago steps up to `5m`.

**Explain**

- [ ] With `none`, `curl -s -X POST "$API/anomalies/incidents/$I/explain" -H "$H" -H "$J" -d '{}'` returns `409 explainer_disabled`. `GET /anomalies/incidents/$I` still returns `evidence.summary`.
- [ ] A stub explainer that cites `Z9` returns `200` with `fallback_used:true` and `validator.rejected_reason:"validation_failed"`, and `validator.errors` names `Z9`. A stub that names a device outside the bundle falls back the same way.
- [ ] A stub that sleeps past `DCIM_ANOMALY_EXPLAINER_TIMEOUT_S` returns `200` with `rejected_reason:"timeout"`, within the timeout plus 2 s.
- [ ] Two identical calls make one model call, and the second returns `cached:true`. `force:true` within 300 s returns `429` with `Retry-After`.
- [ ] Each run writes one `audit_log` row, `anomaly.explain`, with `after.prompt` and `after.raw_output`. `explanation` holds `prompt_sha256` but no prompt text.
- [ ] `POST /anomalies/incidents/$I/explain/feedback -d '{"useful":true}'` appends `{vote:"up", explanation_sha256}` to `explanation_feedback`. A repeat by the same user replaces that vote. On an incident with no explanation, the call returns `409 no_explanation`.
- [ ] An import test confirms `analytics.explainer` is unreachable from `ingest.worker`, `analytics.sweep` and `analytics.correlator`.

**Health**

- [ ] On the two-replica stack, after 10 min, `detail.sweep.runs_1h ≥ 10`, `gate_skips_1h ≈ runs_1h`, and `lock_holder` names a single consumer per interval. Record `p95_ms_1h` in doc 33's Phase 0 log.
- [ ] Check `XINFO GROUPS telemetry.v1` lag before and after the deploy. It must show no sustained growth.

## 15. Cross-doc reconciliation

| Item | Decision here | Where |
|---|---|---|
| `31` D3: topic and event names | Topic **`anomalies`** confirmed. Dotted names kept | §8 |
| `31` D4: `alarm_id` filter | Adopted | §4.1 |
| `31` D5: dismiss reason | `reason` enum, **required** for `dismiss` | §4.3 |
| `31` D6: `/health` | `enabled`, `explainer`, `sweep_s` and `baseline_last_fit_at` added, viewer-readable. `33` adds `severity_cap` and `l5_major_enabled` | §11 |
| `31` D7: `/expected` | `interval` added. `metric` repeatable or omitted. `{items}` response. A `reason` when empty. `from`/`to` kept; the `start`/`end` rename is withdrawn | §10 |
| `31` D8: thumbs | `POST /incidents/{id}/explain/feedback {useful, note?}`, stored in `explanation_feedback` | §7.2 |
| `31` D9: parameter bounds | `param_schema: ParamSpec[]` with `default` | §6.1 |
| `31` D10: incident feedback | `POST /incidents/{id}/feedback`: findings only, per-row report, scheduled with Phase 2 | §5.4 |
| `31` D11: acknowledgement | The alarm is authoritative. Finding → alarm in the same transaction. Alarm → finding at the next sweep | §4.4 |
| `31` §18 items 3–5 | A version bump resets accumulators. Facets via `with_facets`. Paging is `limit`/`offset`/`total` | §6.2, §4.1, §3 |
| `33` D4: alarm types | Snake_case names from doc 29 §9.3, not detector ids | §9 |
| `33` D5: severity | Capped at MINOR. MAJOR only by L5 escalation of the root's alarm, behind `L5.major_enabled` | §9 |
| `33` D13: L5 row | Listed and editable through `/detectors` | §6.1, §6.2 |
| `29` D3: `verdict` | Exposed. Survives resolution. State machine per `29` §8.1 | §3, §4.3 |
| `29` §4.6: `explanation_feedback` | Thumbs storage | §7.2 |
| `29` §7.5: audit | `anomaly.explain`, with the prompt and raw output in `audit_log` | §7.4 |
| `29` §4.3: exclusions | Every source is listed. System-owned rows cannot be deleted | §6.4 |
| Resolved in `29` | §8.5: symptom marking restricted to analytics-raised alarms. §4.6: column comment path is `/explain/feedback` | §1, §7.2 |
| Open for `31` | The §13 type corrections, and dropping the D11 interim rule | §13, §4.4 |
