# 10 — REST API Specification

Base path `/api/v1`. OpenAPI at `/docs` and `/openapi.json`; the committed
snapshot at `contracts/openapi/dcim-v1.yaml` is what the frontend client is
generated from.

---

## 1. Conventions

- **Auth:** `Authorization: Bearer <jwt>`. Roles `viewer` < `operator` < `admin`.
  Every write endpoint below names the minimum role.
- **Pagination:** `?limit=` (default 50, max 500) `&cursor=`. Cursor-based, not
  offset — offsets break under concurrent inserts on alarm and event lists.
  Response: `{ "items": [...], "next_cursor": "...", "total": 1234 }`
  (`total` omitted when it would require an expensive count).
- **Filtering:** repeated query params are OR within a field, AND across fields:
  `?device_type=server&device_type=switch&room_id=X` → (server OR switch) AND room X.
- **Sorting:** `?sort=-last_seen,name`.
- **Sparse fields:** `?fields=id,name,status` on list endpoints.
- **Times:** RFC 3339, always UTC, always with `Z`.
- **Errors:** RFC 7807 problem+json.

```json
{ "type": "https://dcim/errors/validation",
  "title": "Validation failed",
  "status": 422,
  "detail": "u_start 43 exceeds rack u_height 42",
  "instance": "/api/v1/devices",
  "errors": [{"field": "u_start", "message": "out of range"}] }
```

- **Idempotency:** `POST` accepts `Idempotency-Key`; a repeat within 24 h returns
  the original response.
- **Concurrency:** `GET` returns `ETag`; `PUT`/`PATCH` require `If-Match` and
  return `409` on mismatch. This matters for inventory edits from two operators.

---

## 2. Inventory

```
GET    /api/v1/datacenters                             viewer
GET    /api/v1/datacenters/{id}
GET    /api/v1/datacenters/{id}/summary                counts, power, PUE, alarms
POST   /api/v1/datacenters                             admin
PUT    /api/v1/datacenters/{id}                        admin
DELETE /api/v1/datacenters/{id}                        admin   (409 if it has rooms)

GET    /api/v1/rooms?datacenter_id=&room_type=
GET    /api/v1/rooms/{id}
GET    /api/v1/rooms/{id}/floorplan                    racks with x/y, aisles, temps
POST|PUT|DELETE /api/v1/rooms[/{id}]                   admin

GET    /api/v1/rows?room_id=
GET    /api/v1/racks?room_id=&row_id=&min_load_pct=&has_free_u=
GET    /api/v1/racks/{id}
GET    /api/v1/racks/{id}/elevation                    ← the rack view; see §2.1
GET    /api/v1/racks/{id}/power                        feeds, per-PDU load, redundancy state
GET    /api/v1/racks/{id}/capacity                     power/cooling/space/ports
POST|PUT|DELETE /api/v1/racks[/{id}]                   admin
```

### 2.1 Rack elevation

One call renders the whole rack. Anything that needs 42 follow-up requests is
wrong.

```json
GET /api/v1/racks/{id}/elevation
{
  "rack": { "id": "...", "name": "R2-01", "u_height": 42,
            "load_kw": 8.4, "rated_power_kw": 12.0, "max_inlet_c": 24.1 },
  "positions": [
    { "u_start": 41, "u_height": 1, "facing": "front",
      "device": { "id": "...", "name": "SRV01-DC1-HA-R2-01", "device_type": "server",
                  "status": "ONLINE", "health": "OK", "max_severity": "CLEAR",
                  "power_w": 812, "inlet_temp_c": 23.4, "cpu_util_pct": 79 } },
    { "u_start": 40, "u_height": 2, "facing": "front", "device": { ... } },
    { "u_start": 18, "u_height": 1, "free": true }
  ],
  "free_blocks": [ { "u_start": 18, "u_height": 6 } ]
}
```

`free_blocks` is computed server-side because "the largest contiguous free block"
is the question capacity planning actually asks, and computing it in the browser
from a sparse list invites off-by-one bugs.

---

## 3. Devices

```
GET    /api/v1/devices
       ?device_type=&vendor=&status=&health=&room_id=&rack_id=&datacenter_id=
       &search=          (name, serial, IP, asset tag — trigram index)
       &has_alarms=true&severity_gte=MAJOR
GET    /api/v1/devices/{id}                     identity + location + model + endpoints
GET    /api/v1/devices/{id}/state               current status + hot metrics
GET    /api/v1/devices/{id}/metrics             latest value of every metric it reports
GET    /api/v1/devices/{id}/history             ← §4
GET    /api/v1/devices/{id}/alarms?state=
GET    /api/v1/devices/{id}/events?limit=
GET    /api/v1/devices/{id}/interfaces
GET    /api/v1/devices/{id}/relationships?layer=  upstream/downstream on one layer
GET    /api/v1/devices/{id}/impact                blast radius across all layers
POST   /api/v1/devices                          operator
PUT    /api/v1/devices/{id}                     operator
PATCH  /api/v1/devices/{id}                     operator
DELETE /api/v1/devices/{id}                     admin  (soft: lifecycle=decommissioned)
POST   /api/v1/devices/{id}/maintenance         operator  {"until": "...", "note": "..."}
```

`POST /devices/{id}/maintenance` sets `admin_state = maintenance`, which
suppresses alarm *notification* while still recording alarms. Suppressing the
record instead of the notification is a common and regrettable design: you lose
the evidence of what happened during the window.

### 3.1 Endpoints

```
GET    /api/v1/devices/{id}/endpoints
POST   /api/v1/devices/{id}/endpoints           operator
PUT    /api/v1/endpoints/{id}                   operator
DELETE /api/v1/endpoints/{id}                   operator
GET    /api/v1/endpoints/{id}/state             comm health, counters, last error
POST   /api/v1/endpoints/{id}/test              operator — one synchronous probe
```

`POST /endpoints/{id}/test` is the "is this credential right" button. It runs
through the collector (`POST` to the collector's admin port, or a Redis
request/response with a 10 s timeout) and returns the raw result. It must have a
hard timeout and must never be called from a loop in the UI.

---

## 4. Telemetry

```
GET /api/v1/devices/{id}/history
    ?metric=cpu_temperature&metric=power_draw       (repeatable)
    &instance=                                       optional
    &start=2026-08-17T00:00:00Z&end=2026-08-18T00:00:00Z
    &interval=auto|raw|1m|5m|1h
    &agg=avg|min|max|last
```

```json
{
  "device_id": "...",
  "interval": "1m",
  "source": "telemetry_1m",
  "series": [
    { "metric": "cpu_temperature", "instance": "cpu0", "unit": "C",
      "points": [[1755388800000, 67.5], [1755388860000, 68.1]] }
  ],
  "truncated": false
}
```

- `interval=auto` applies the routing table in `04-data-model.md` §6.4. Always
  return the `source` actually used so a chart can label itself honestly.
- Points are `[epoch_ms, value]` pairs, not objects — at 5,000 points the size
  difference is roughly 4×, and every charting library takes this shape.
- Hard cap 10,000 points per series. Over the cap, coarsen the interval and set
  `"truncated": true` rather than silently sampling.
- `GET /api/v1/telemetry/query` accepts multiple devices for comparison charts;
  cap it at 20 devices × 5 metrics.

```
GET /api/v1/telemetry/top?metric=power_draw&scope=room:{id}&limit=10&window=1h
GET /api/v1/telemetry/latest?device_id=&device_id=&metric=      bulk current values
```

---

## 5. Alarms and events

```
GET   /api/v1/alarms
      ?state=ACTIVE&severity=CRITICAL&severity=MAJOR
      &device_type=&room_id=&rack_id=&alarm_type=
      &include_symptoms=false        ← default: roots only
      &since=&until=&sort=-last_seen
GET   /api/v1/alarms/{id}
GET   /api/v1/alarms/{id}/symptoms
GET   /api/v1/alarms/{id}/history
POST  /api/v1/alarms/{id}/acknowledge     operator   {"note": "..."}
POST  /api/v1/alarms/{id}/unacknowledge   operator
POST  /api/v1/alarms/{id}/clear           operator   {"note": "..."}   manual clear
POST  /api/v1/alarms/bulk/acknowledge     operator   {"ids": [...], "note": "..."}
GET   /api/v1/alarms/summary              counts by severity/state/type/room
```

`include_symptoms=false` by default is the single most important design decision
in this section. An alarm list that shows 21 rows for one OOB switch failure is
the reason operators stop looking at alarm lists.

```
GET   /api/v1/events
      ?device_id=&event_type=&source=&severity=&since=&until=
GET   /api/v1/events/{id}
GET   /api/v1/events/unresolved-sources    traps whose source IP matched no endpoint

GET   /api/v1/alarm-rules
POST|PUT|DELETE /api/v1/alarm-rules[/{id}]   admin
POST  /api/v1/alarm-rules/{id}/test          admin — evaluate against the last 24 h,
                                             return how many times it would have fired
```

`/alarm-rules/{id}/test` is what stops a badly-tuned threshold reaching
production. Make it a required step in the UI before saving a rule.

---

## 6. Topology

```
GET /api/v1/topology?layer=network|power|cooling|physical|management|fieldbus
    &scope=datacenter:{id}|room:{id}|rack:{id}|device:{id}
    &depth=2                       hops from the scope anchor
```

```json
{
  "layer": "power",
  "nodes": [ { "id":"...", "name":"UPS1-DC1", "device_type":"ups",
               "status":"ONLINE", "max_severity":"CLEAR",
               "metrics": {"ups_load_pct": 62.0},
               "location": {"room":"Electrical Room 1"} } ],
  "edges": [ { "id":"...", "source":"ups-id", "target":"pdu-id",
               "layer":"power", "link_type":"feeder",
               "redundancy_side":"A", "oper_state":"up",
               "a_termination":{"type":"outlet","id":"...","label":"Out-12"},
               "b_termination":{"type":"psu","id":"...","label":"PSU1"} } ],
  "truncated": false
}
```

- Node positions are **not** returned. Layout is the client's job, except for
  `layer=physical`, where floor coordinates are real data.
- Cap at 2,000 nodes; over the cap return `truncated: true` with the highest
  degree nodes retained, and require a narrower scope.

```
GET /api/v1/topology/path?src={id}&dst={id}&layer=
GET /api/v1/topology/impact/{device_id}
```

---

## 7. Domain views

```
GET /api/v1/power?scope=                totals, chain state, per-UPS/PDU load, redundancy
GET /api/v1/power/chain/{device_id}     grid → switchgear → ATS → UPS → PDU → device
GET /api/v1/cooling?scope=              plant state, chillers, loops, CRAH, CDU
GET /api/v1/cooling/plant/{room_id}     CHW loop detail: supply/return, flow, ΔT, capacity
GET /api/v1/environment?scope=          temp/humidity/dewpoint/airflow, hot spots
GET /api/v1/environment/heatmap?room_id=&metric=inlet_temperature
GET /api/v1/capacity?scope=&horizon=90d power/cooling/space/ports + forecast
GET /api/v1/analytics/pue?datacenter_id=&start=&end=&granularity=1h
GET /api/v1/analytics/thermal?room_id=&window=24h
GET /api/v1/analytics/power?scope=&window=7d
```

`GET /api/v1/power/chain/{device_id}` answers "what feeds this server, and is it
still redundant" in one call. It returns both paths (A and B) with the state of
each hop, and a `redundancy: "N+1" | "single_feed" | "no_feed"` verdict. That
verdict is the thing an operator actually needs during an event.

---

## 8. Dashboard

```
GET /api/v1/dashboard/summary?datacenter_id=
```

```json
{
  "devices": { "total": 664, "online": 651, "offline": 9, "degraded": 4, "unknown": 0 },
  "alarms":  { "active": 12, "critical": 1, "major": 3, "warning": 8, "acknowledged": 2,
               "suppressed_symptoms": 7 },
  "power":   { "it_load_kw": 512.4, "facility_load_kw": 214.8, "total_kw": 727.2,
               "ups_load_pct": 62.1, "generator_available": true },
  "cooling": { "load_kw": 198.2, "chillers_running": 2, "chillers_standby": 1,
               "chws_temp_c": 7.2, "plant_health": "OK" },
  "pue":     { "value": 1.42, "method": "energy", "level": 2, "window": "1h" },
  "environment": { "avg_inlet_c": 23.1, "max_inlet_c": 27.8, "avg_humidity_pct": 45.2,
                   "hot_spots": 1 },
  "collectors": [ { "id": "col-1", "status": "HEALTHY", "endpoints_owned": 1024,
                    "last_heartbeat": "2026-08-18T10:00:02Z" } ],
  "as_of": "2026-08-18T10:00:05Z"
}
```

One call, served from `device_state` and the summary views, target < 100 ms.
The dashboard must never fan out into 12 requests.

```
GET /api/v1/dashboard/trends?window=24h&metrics=it_load,cooling_load,pue,avg_inlet
```

---

## 9. Collector-facing endpoints

Authenticated with a collector token, separate from user JWTs, `collector` scope
only.

```
GET  /api/v1/collector/assignments?collector_id=col-1     ETag; 304 supported
POST /api/v1/collector/heartbeat                          (also available via the stream)
GET  /api/v1/collector/instances                          viewer — for the UI
GET  /api/v1/collector/instances/{id}/stats
```

```json
GET /api/v1/collector/assignments?collector_id=col-1
{
  "version": 47,
  "generated_at": "2026-08-18T10:00:00Z",
  "endpoints": [
    { "id": "6d2f...", "device_id": "fa03...", "device_type": "server",
      "vendor": "Supermicro", "model": "SYS-121H-TNR LCC",
      "protocol": "snmp", "role": "os_agent",
      "address": "10.50.11.19", "port": 161,
      "addressing": { "community": "10.50.11.19" },
      "poll": { "interval_s": 30, "timeout_ms": 3000, "retries": 2,
                "metric_groups": ["system","interfaces","host_resources"] } },
    { "id": "7a1c...", "device_id": "fa03...", "protocol": "redfish", "role": "bmc",
      "address": "10.51.11.25", "port": 8443,
      "addressing": { "base": "/redfish/v1", "verify_tls": false },
      "credential": { "username": "admin", "password": "..." },
      "poll": { "interval_s": 60, "push_enabled": true } }
  ]
}
```

Credentials are returned **decrypted** on this endpoint only, over TLS, to an
authenticated collector. That is unavoidable — the collector must authenticate to
devices. Mitigations: collector-scoped tokens, short TTL, per-collector
assignment scoping so a compromised collector sees only its own shard, and audit
logging of every assignment fetch.

---

## 10. Discovery and admin

```
POST /api/v1/discovery/runs             operator  {"method":"snmp_sweep","scope":{...}}
GET  /api/v1/discovery/runs
GET  /api/v1/discovery/runs/{id}/candidates
POST /api/v1/discovery/candidates/{id}/promote   operator  {device payload}
POST /api/v1/discovery/candidates/{id}/ignore    operator
POST /api/v1/import/simulator                    admin — run the seed importer

GET  /api/v1/metrics/registry           the metric dictionary, for the UI's formatters
GET  /api/v1/health                     liveness
GET  /api/v1/ready                      DB + Redis + ingest lag
GET  /api/v1/version
```

`/api/v1/ready` includes **ingest lag** (the age of the oldest unacked stream
entry). A backend that is up but 40 minutes behind is not ready, and nothing else
will tell you.

---

## 11. Performance targets

| Endpoint | p95 target |
|---|---|
| `/dashboard/summary` | 100 ms |
| `/devices` (50 rows) | 150 ms |
| `/racks/{id}/elevation` | 100 ms |
| `/devices/{id}/history` (1 day, 1m) | 300 ms |
| `/topology?layer=power&scope=room` | 400 ms |
| `/alarms` (50 rows) | 100 ms |

Anything projected to exceed 1 s becomes a task-worker job with a job-id
response and a WebSocket completion event — never a slow request.
