# 11 — WebSocket Specification

Endpoint: `GET /api/v1/ws?token=<jwt>`

The token goes in the query string because browsers cannot set headers on a
WebSocket handshake. Consequences, and the mitigations that are mandatory:

- Use a **short-lived WS ticket**, not the session JWT: `POST /api/v1/ws/ticket`
  returns a single-use token with a 60-second TTL, redeemable once.
- Never log the full URL of a WS upgrade.

---

## 1. Why subscriptions exist (finding A10)

664 devices × ~40 metrics ≈ 26,000 values per collection cycle. Broadcasting all
of it to every browser is roughly 3–5 MB/s per client and no rendering budget
left. The client declares what it is looking at; the server sends only that,
coalesced.

---

## 2. Protocol

All frames are JSON objects with an `op` field. Client→server ops are lowercase
verbs; server→client frames carry `event`.

### 2.1 Client → server

```json
{ "op": "subscribe",   "topics": ["dashboard", "alarms", "device:fa03fbfd"] }
{ "op": "unsubscribe", "topics": ["device:fa03fbfd"] }
{ "op": "ping",        "ts": 1755512400000 }
{ "op": "set_rate",    "max_hz": 1 }
```

### 2.2 Topics

| Topic | Delivers |
|---|---|
| `dashboard` | aggregate KPI deltas, ~1/s |
| `alarms` | all alarm lifecycle events the user may see |
| `alarms:critical` | only CRITICAL/MAJOR |
| `events` | event stream (high volume — opt-in only) |
| `device:{id}` | that device's telemetry, state and alarms |
| `rack:{id}` | roll-up + member device state (not member telemetry) |
| `room:{id}` | roll-up only |
| `topology:{layer}` | edge/node state changes on that layer |
| `collectors` | collector health |
| `job:{id}` | progress/completion of a task-worker job |

Rules:

- A connection may hold at most **50 topics**. Exceeding it returns an error
  frame; it does not silently drop.
- `device:*` wildcards are not supported. A page that wants 42 devices subscribes
  to `rack:{id}`, which is exactly why the rack topic sends state but not
  telemetry.
- Topics are authorised at subscribe time against the user's scope, and
  re-authorised if the token is refreshed.

### 2.3 Server → client

```json
{ "event": "hello", "session_id": "...", "server_time": "2026-08-18T10:00:00Z",
  "protocol_version": 1, "heartbeat_interval_s": 30 }

{ "event": "telemetry_update", "topic": "device:fa03fbfd",
  "device_id": "fa03fbfd", "ts": "2026-08-18T10:00:30Z",
  "metrics": { "cpu_temperature": {"v": 67.5, "u": "C", "q": "good"},
               "power_draw":      {"v": 812.0, "u": "W", "q": "good"} } }

{ "event": "device_status_change", "topic": "device:fa03fbfd",
  "device_id": "fa03fbfd", "status": "OFFLINE", "previous": "ONLINE",
  "health": "UNKNOWN", "ts": "2026-08-18T10:00:31Z" }

{ "event": "alarm_created", "topic": "alarms",
  "alarm": { "id": "...", "device_id": "...", "device_name": "CDU03-DC1-HA",
             "alarm_type": "cdu_leak", "severity": "CRITICAL", "state": "ACTIVE",
             "message": "Coolant leak detected", "instance": "",
             "first_seen": "2026-08-18T10:30:00Z", "is_symptom": false,
             "location": {"room": "Server Hall A", "rack": "R2-01"} } }

{ "event": "alarm_updated",   "topic": "alarms", "alarm": { ... } }
{ "event": "alarm_cleared",   "topic": "alarms", "alarm": { ... } }
{ "event": "alarm_acknowledged", "topic": "alarms", "alarm": { ... }, "by": "hari" }

{ "event": "event_created", "topic": "events",
  "item": { "id": 918273, "device_id": "...", "event_type": "link_down",
            "severity": "MAJOR", "source": "snmp_trap",
            "message": "Interface Ethernet1/3 down", "ts": "..." } }

{ "event": "dashboard_update", "topic": "dashboard",
  "patch": { "power.it_load_kw": 514.9, "alarms.active": 13, "pue.value": 1.43 } }

{ "event": "collector_status", "topic": "collectors",
  "collector_id": "col-1", "status": "DEGRADED",
  "detail": "publish queue 82% full" }

{ "event": "topology_change", "topic": "topology:power",
  "change": "edge_state", "edge_id": "...", "oper_state": "down" }

{ "event": "job_progress", "topic": "job:abc", "progress": 0.62, "message": "..." }
{ "event": "job_complete", "topic": "job:abc", "result_url": "/api/v1/..." }

{ "event": "error", "code": "topic_limit", "message": "max 50 topics" }
{ "event": "pong", "ts": 1755512400000 }
```

`dashboard_update` sends a **flat patch of changed paths**, not the whole summary
object. At 1 Hz the difference between a 40-byte patch and a 2 kB document across
20 clients is the difference between negligible and noticeable.

---

## 3. Coalescing

The server never emits telemetry more often than once per second per topic,
regardless of collection rate.

```python
class Coalescer:
    def __init__(self, interval_ms: int = 1000):
        self._pending: dict[tuple[str, str], dict] = {}   # (topic, device) → merged metrics

    def add(self, topic: str, device_id: str, metrics: dict) -> None:
        self._pending.setdefault((topic, device_id), {}).update(metrics)  # last wins

    async def flush_loop(self):
        while True:
            await asyncio.sleep(self._interval)
            batch, self._pending = self._pending, {}
            for (topic, device_id), metrics in batch.items():
                await self.hub.publish(topic, telemetry_frame(device_id, metrics))
```

Alarm and status-change events are **not** coalesced — they are rare and latency
matters. A critical alarm must reach the browser in well under a second.

`set_rate` lets a background tab drop to `max_hz: 0.2`; the client should send it
on `visibilitychange`, which cuts idle-tab traffic by ~80 % in practice.

---

## 4. Multi-worker fan-out

With more than one uvicorn worker, a WebSocket lives in one process while the
ingest worker runs in another. Redis pub/sub is the bridge and is not optional.

```
ingest worker → PUBLISH dcim:ws:{topic} <json>
                       ↓
each api worker: one subscriber task per process, pattern-subscribed to dcim:ws:*
                       ↓
                 local ConnectionHub routes to sessions holding that topic
```

Each API process subscribes **once** with a pattern, not once per client. A
naïve implementation that subscribes per connection will open thousands of Redis
subscriptions and fall over.

```python
class ConnectionHub:
    _by_topic: dict[str, set[Session]]
    _by_session: dict[str, set[str]]

    async def publish_local(self, topic: str, frame: dict) -> None:
        payload = orjson.dumps(frame)
        dead = []
        for s in self._by_topic.get(topic, ()):
            try:
                s.queue.put_nowait(payload)      # bounded per-session queue
            except asyncio.QueueFull:
                dead.append(s)                   # slow consumer
        for s in dead:
            await self.disconnect(s, reason="slow_consumer")
```

**Slow-consumer policy:** each session has a bounded outbound queue (256 frames).
On overflow the session is disconnected with `code=1013` and a `slow_consumer`
reason, and the client reconnects and re-syncs via REST. Buffering without bound
to protect one wedged browser tab is how the API process runs out of memory.

---

## 5. Connection lifecycle

```
client                                   server
  │── HTTP upgrade (?token=ticket) ──────▶│  validate ticket, single use
  │◀───────────── hello ──────────────────│
  │── subscribe {topics} ─────────────────▶│  authorise each topic
  │◀───────────── subscribed {topics} ────│
  │◀─── frames … ─────────────────────────│
  │── ping (every 30 s) ──────────────────▶│
  │◀───────────── pong ───────────────────│
```

- Server sends a WS-level ping every 30 s; a session with no pong in 90 s is
  closed.
- **Reconnect with backoff and jitter** — 1 s, 2 s, 4 s … capped at 30 s, ±30 %
  jitter. Without jitter, an API restart brings every browser back at the same
  instant.
- **On reconnect the client must re-fetch state over REST**, then resubscribe.
  The WebSocket carries deltas only and has no replay. Treating WS as the source
  of truth after a gap is how UIs end up showing an alarm that was cleared while
  disconnected.
- Close codes: `1008` auth failure, `1013` slow consumer, `1012` server
  restarting (client should reconnect immediately with jitter).

---

## 6. Client shape

```ts
const ws = new DcimSocket({
  url: "/api/v1/ws",
  getTicket: () => api.post("/ws/ticket").then(r => r.token),
  onFrame: (f) => store.apply(f),
  maxBackoffMs: 30_000,
});

// route-scoped subscription: mount subscribes, unmount unsubscribes
useTopics(["device:" + deviceId]);
```

One socket per browser tab, shared through React context. Multiple sockets
multiply server sessions for no benefit and make the topic limit meaningless.

The store applies frames by event type; the *only* place that mutates live state
is that reducer. Components read from the store, so a component never has to
decide whether the REST value or the WS value is newer — the store keeps
timestamps and drops out-of-order updates.
