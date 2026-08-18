# 13 — Observability and Security

---

## Part A — Observability

Build this from commit one. Retrofitting observability into a collector is
painful, and the collector is exactly where you cannot debug by reading code —
the failures are on the wire.

### A1. Collector metrics (Prometheus, `:9100/metrics`)

```
# Collection
dcim_collector_polls_total{protocol,device_type,result}          counter   result=success|failure|partial
dcim_collector_poll_duration_seconds{protocol,device_type}       histogram buckets .01 .05 .1 .25 .5 1 2.5 5 10
dcim_collector_poll_skipped_total{protocol}                      counter   previous poll still running
dcim_collector_poll_shed_total{protocol}                         counter   queue full
dcim_collector_samples_emitted_total{protocol,metric}            counter
dcim_collector_misses_total{protocol,reason}                     counter   no_such_object|timeout|decode|unsupported

# Endpoint health
dcim_collector_endpoints{protocol,status}                        gauge     ONLINE|DEGRADED|OFFLINE|UNKNOWN
dcim_collector_endpoint_failures_total{protocol,error_class}     counter

# Push sources
dcim_collector_traps_received_total{result}                      counter   ok|unknown_oid|unresolved_source|rate_limited
dcim_collector_redfish_events_total{result}                      counter
dcim_collector_bacnet_cov_total{result}                          counter
dcim_collector_gnmi_streams{state}                               gauge     active|reconnecting|failed
dcim_collector_gnmi_stream_restarts_total{reason}                counter

# Publishing — the four that matter most
dcim_collector_publish_queue_depth                               gauge
dcim_collector_publish_batch_size                                histogram
dcim_collector_publish_duration_seconds                          histogram
dcim_collector_publish_dropped_total{stream,reason}              counter

# Assignment
dcim_collector_assignment_version                                gauge
dcim_collector_assignment_age_seconds                            gauge
dcim_collector_assignment_errors_total                           counter

# Runtime
dcim_collector_open_fds / go_goroutines / go_memstats_*
```

**Cardinality discipline.** Never label a metric with `endpoint_id` or
`device_id` — 664 devices × 5 protocols × several metrics is an explosion, and it
is the single most common way a Prometheus install is killed. Per-device detail
belongs in the `poll_result` hypertable, which is designed for it.

### A2. Backend metrics

```
dcim_api_requests_total{method,path,status}
dcim_api_request_duration_seconds{method,path}                   histogram
dcim_db_query_duration_seconds{repository,operation}             histogram
dcim_db_pool_connections{state}                                  gauge  in_use|idle
dcim_ws_connections                                              gauge
dcim_ws_frames_sent_total{event}
dcim_ws_slow_consumer_disconnects_total

dcim_ingest_messages_total{stream,result}
dcim_ingest_lag_seconds{stream}                    gauge  ← the single most important number
dcim_ingest_batch_size                             histogram
dcim_ingest_write_duration_seconds{table}          histogram
dcim_ingest_stream_pending{stream}                 gauge  XPENDING depth

dcim_alarms_active{severity}                       gauge
dcim_alarm_transitions_total{action}               counter  raised|cleared|acked|suppressed
dcim_alarm_eval_duration_seconds                   histogram
```

`dcim_ingest_lag_seconds` is the health of the whole pipeline in one number. Alert
at > 60 s warning, > 300 s critical.

### A3. Structured logging

Go: `log/slog` JSON. Python: `structlog` JSON. One shared field set:

```json
{ "ts":"2026-08-18T10:00:00.123Z", "level":"warn", "service":"collector",
  "collector_id":"col-1", "component":"adapter.bacnet",
  "endpoint_id":"6d2f...", "device_id":"fa03...", "device_name":"CH01-DC1-PLANT",
  "protocol":"bacnet", "error_class":"timeout",
  "msg":"ReadPropertyMultiple timed out", "duration_ms":5001,
  "trace_id":"..." }
```

Levels, applied strictly:

| Level | Use |
|---|---|
| `error` | the process cannot do its job — DB unreachable, adapter init failed |
| `warn` | a device or endpoint problem — timeouts, decode failures, auth failures |
| `info` | lifecycle only — start, stop, assignment change, subscription created |
| `debug` | per-poll detail, off in production |

A per-poll `info` line at 664 endpoints × 2/min is 1,300 lines a minute of
nothing. Poll outcomes go to metrics and to `poll_result`, not to the log.

**Log sampling** on repetitive warnings: first occurrence, then 1-in-100, with a
counter. One unreachable device must not produce 2,880 identical lines a day.

### A4. Tracing

OpenTelemetry, sampled at 1 % plus 100 % of errors. The trace that pays for
itself is the end-to-end one:

```
poll(endpoint) ──▶ publish(batch) ──▶ [redis] ──▶ ingest(batch) ──▶ write ──▶ evaluate ──▶ ws.publish
```

Propagate `trace_id` through the Redis stream as a field on the batch. That is
what lets you answer "why did this alarm take 40 seconds to appear" with data
instead of a hypothesis.

### A5. Health endpoints

| Service | Endpoint | Meaning |
|---|---|---|
| collector | `/health` | process alive |
| collector | `/ready` | Redis reachable, assignment fetched, adapters up, listeners bound |
| api | `/api/v1/health` | process alive |
| api | `/api/v1/ready` | DB + Redis reachable **and ingest lag < 300 s** |
| ingest | `/health`, `/ready` | consumer group joined, lag acceptable |

### A6. Platform alarms

The DCIM must monitor itself, and these must appear in the same alarm list as
device alarms:

| Alarm | Condition |
|---|---|
| `collector_stale` | no heartbeat for 60 s |
| `collector_degraded` | publish drops > 0, or queue > 80 % |
| `ingest_lag_high` | lag > 60 s (warning) / 300 s (critical) |
| `assignment_stale` | collector's assignment older than 5 min |
| `unknown_trap_source` | traps from an IP matching no endpoint |
| `db_connection_pool_exhausted` | pool saturated for > 30 s |
| `discovery_drift` | responding device not in inventory |

An operator who cannot tell the difference between "the datacenter is quiet" and
"the collector died" has no monitoring at all.

---

## Part B — Security

### B1. Credentials

**Never in source, never in a config file, never in a log, never in an API
response.**

```
storage:   credential.secret_enc = AES-256-GCM(nonce || ciphertext || tag)
key:       DCIM_CREDENTIAL_KEY (32 bytes, base64) from env / KMS / Vault
rotation:  re-encrypt with a new key id; keep the previous key for one release
display:   credential.secret_hint only ("community: <device ip>", "user: admin")
```

The single unavoidable exception is `GET /api/v1/collector/assignments`, which
returns decrypted credentials to an authenticated collector over TLS. It cannot
be otherwise — the collector must authenticate to devices. Mitigate it properly:

- collector tokens are a distinct credential type with a `collector` scope and no
  user privileges;
- assignments are **scoped to the requesting `collector_id`**, so a compromised
  collector sees only its own shard;
- tokens are short-lived and rotatable without redeploying;
- every assignment fetch is audit-logged with collector id, IP and version;
- the endpoint is not reachable from the user-facing network in production.

Redaction is enforced, not hoped for: a logging processor scrubs any key matching
`password|secret|token|community|private_key` in both stacks, and a unit test
asserts a credential object never serialises its secret.

### B2. Simulator defaults must not become DCIM defaults

The simulator ships `admin`/`password` for Redfish, community == device IP, and
insecure gNMI. That is correct for a simulator. The DCIM must treat every one of
them as a per-endpoint configured value with no default, so that pointing at real
hardware is a data change, not a code change. In particular `verify_tls` lives in
`device_endpoint.addressing` and defaults to **true**; the simulator's endpoints
explicitly set it false.

### B3. API authentication and authorisation

- JWT access token, 60 min, plus a refresh token in an httpOnly cookie.
- Roles: `viewer` (read), `operator` (ack/clear alarms, maintenance mode,
  inventory edits, discovery promote), `admin` (rules, credentials, users,
  delete), `collector` (assignments and heartbeat only).
- Authorisation is a dependency on every route, never an `if` inside a handler.
- Optional scoping by datacenter for multi-tenant use — put the hook in the user
  model now (`user.scopes: list[dc_id]`) even if it is unused, because retrofitting
  row-level scoping later touches every query.

### B4. Transport

- TLS on the API, on the collector's Redfish event receiver, and on Redis in
  production (`rediss://`).
- Redis requires a password and is bound to the internal network. An unprotected
  Redis holding the telemetry stream and the counter baselines is a full
  compromise of the monitoring plane.
- The collector's device-facing traffic is whatever the device supports. SNMPv2c
  and insecure gNMI are cleartext; that is a property of the management network,
  not something the DCIM can fix — but the endpoint model is built for SNMPv3 and
  TLS gNMI so the upgrade is configuration.

### B5. Input handling

- Pydantic v2 validates every request body; no `dict[str, Any]` reaches a
  service.
- All SQL through SQLAlchemy with bound parameters. Any raw SQL (the recursive
  CTEs, `COPY`) uses parameters, never string interpolation.
- The device `search` parameter goes to a trigram index with a parameterised
  `ILIKE`, and is length-capped.
- The collector treats every device response as hostile input: bounded reads,
  APDU length checks, JSON size limits, and a `recover` at the job boundary. A
  malformed BACnet APDU must not take down collection for 600 devices.

### B6. Audit

```sql
CREATE TABLE audit_log (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    actor text NOT NULL,            -- username or 'collector:col-1' or 'system'
    action text NOT NULL,           -- device.update, alarm.ack, credential.create, ...
    target_type text, target_id text,
    before jsonb, after jsonb,
    ip inet, user_agent text
);
```

Audit every write to inventory, credentials, alarm rules and users, plus alarm
acknowledgements and manual clears. `before`/`after` with secrets stripped.

### B7. Rate limiting and abuse

- Per-user API rate limit (say 100 req/s) via Redis.
- Login attempts: 5 failures → 15 minute lockout, logged.
- WebSocket: 50 topics, 256-frame outbound queue, one ticket per connection.
- Trap receiver: per-source rate limit with an overflow counter, so a flapping
  device cannot fill the stream or the disk.

### B8. Dependency and build hygiene

- `govulncheck` and `pip-audit` in CI; fail on high severity.
- Pinned dependencies with a lockfile in all three stacks.
- Non-root containers; read-only root filesystem where possible. The collector
  needs `CAP_NET_BIND_SERVICE` for udp/162 — grant that capability rather than
  running the process as root.
- Images scanned; base images rebuilt on a schedule, not only on code change.
