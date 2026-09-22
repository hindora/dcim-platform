# 25 — Jira / Jira Service Management integration

Plan for ticketing, change management and CMDB export against Atlassian.
Written 2026-09-21 against the working tree at `644f7b9`.

Scope: outbound alarm → ticket, inbound ticket → alarm, JSM change request ↔
maintenance window, and DCIM inventory → JSM Assets. Jira Cloud is the primary
target; Jira Data Center is supported by a different auth mode and the same
code path.

---

## 0. The verdict before the design

**Do not mirror alarms into Jira.** The obvious build — "every alarm becomes an
issue" — is the failure mode this feature is most likely to ship as, and it is
wrong for three reasons that are specific to this platform:

1. **The platform already separates a ticket-worthy condition from a
   notification.** `response_class` is `alarm` (act now, expects an
   acknowledgement) or `alert` (informational, belongs to whoever schedules the
   work) — `app/core/alert_taxonomy.py`. Ticketing every row throws that
   distinction away the moment it leaves the building.
2. **Correlation already collapses cascades and we would un-collapse them.**
   `is_symptom` / `root_cause_alarm_id` exist so one OOB switch failure reads as
   one incident, not twenty-one (`app/repositories/alarms.py:382`). A naive
   exporter posts twenty-one issues.
3. **A closed ticket is not a cleared fault.** Only the poll clears. This is the
   same rule the trap path already lives by, and the inbound webhook must obey
   it: Jira "Done" → `ACKNOWLEDGED`, never `CLEARED`.

So the design is **policy-driven ticketing**: a small, explicit, operator-owned
rule decides which conditions earn a Jira issue, and everything else stays in
the DCIM where it already works.

**Second pushback: Jira is not the alerting system.** In real datacenter
operations, an alarm reaches a human through an on-call/paging tier (PagerDuty,
Opsgenie — now JSM Operations, xMatters) and a *ticket* is the record of work.
Atlassian's own answer to "alarm" is the Operations alert API
(`api.atlassian.com/jsm/ops/api/{cloudId}/v1/alerts`), inherited from Opsgenie,
which has native alias de-duplication and ack/close semantics. Jira issues have
none of that and we must build it.

We still build issues first, because that is what was asked and because a
ticket is the artefact a facilities team actually works from. **JSM Operations
alerts are phase 5**, designed for but not built in phase 1. The abstraction
below (`JiraTarget`) exists so that alert-mode is a second backend, not a
rewrite.

---

## 1. What exists today (verified)

| Piece | Where | State |
|---|---|---|
| Alarm as a **stateful object** keyed `(device_id, alarm_type, instance)`, unique partial index while open | `alembic/versions/0005_alarms_and_events.py:97` | Built. **This is the fingerprint; do not invent another.** |
| One lifecycle for five sources → `AlarmAction(kind, alarm)` | `backend/app/alarms/service.py:48` | Built |
| Every action funnels through three call sites | `backend/app/ingest/worker.py:380`, `:696`, `:823` | Built. One tap point. |
| Alarm row already carries location + classification | `backend/app/repositories/alarms.py:268` | Built: `device_name`, `device_type`, `datacenter_code`, `room_name`, `rack_name`, `category`, `detection`, `response_class`, `severity`, `metric_key`, `trigger_value`, `threshold`, `occurrence_count`, `first_seen` |
| Root-cause suppression (`is_symptom`, `root_cause_alarm_id`) | `backend/app/alarms/correlation.py` | Built |
| Maintenance shelving — planned work does not page | `alembic/versions/0046_planned_work_should_not_page_anyone.py` | Built. `NOT_SHELVED` predicate in the alarm list. |
| `maintenance_window.change_ref text(100)` | `backend/app/api/v1/maintenance.py:29` | Built, **unused**. Jira issue key slots straight in. |
| `change_ref` on lifecycle changes and bulk ops | `backend/app/repositories/lifecycle.py:86`, `backend/app/api/v1/bulk.py:53` | Built, unused |
| Inbound close-the-loop targets | `backend/app/api/v1/alarms.py:92` (acknowledge), `:120` (clear) | Built, with audit + history + symptom release |
| `alarm_history` hypertable (`ts, alarm_id, action, actor, detail`) | `alembic/versions/0005_alarms_and_events.py:146` | Built |
| Audit log with key-based secret redaction, never raises, joins the caller's transaction | `backend/app/core/audit.py:67` | Built |
| AES-256-GCM secret-at-rest + `credential_hint` | `backend/app/core/security.py:33`, `:60` | Built. Reusable. |
| Config-in-DB pattern: sparse JSONB + integer `version` for ETag | `alembic/versions/0034_collector_config.py` | Built. Copy this shape. |
| Timer-sweep pattern in the ingest worker | `backend/app/ingest/worker.py:358` | Built |
| Platform self-monitoring alarms with a closed type list | `backend/app/alarms/platform.py:71` | Built. Extend it. |
| Role gate as a dependency | `backend/app/core/security.py:155` — `viewer < operator < admin` | Built |
| Settings shell, one section per page | `frontend/src/features/settings/SettingsLayout.tsx` | Built |

### 1.1 Gaps

- **No outbound HTTP anywhere except the seed importer.** `httpx` appears once,
  in `app/importer/simulator.py:24`. No retry, no backoff, no circuit breaker,
  no delivery record.
- **`Fanout` is the wrong transport for this.** It is Redis pub/sub and it
  *deliberately* swallows its own errors (`app/ingest/fanout.py:32`) — correct
  for a browser frame, fatal for a ticket. Ticketing needs a durable outbox.
- **Ingest runs two worker processes** (`feedback_ingest_needs_two_workers`). A
  naive timer-loop dispatcher would double-post. Needs `FOR UPDATE SKIP LOCKED`.
- **`credential.protocol` is the `protocol_t` enum** — Jira credentials do not
  belong in that table.
- No scheduler for periodic export jobs other than the ingest worker's timers.

---

## 2. What the market actually does (research, 2026-09-21)

The honest summary: **there is no DCIM→Jira standard, and the mature DCIM ITSM
integration is ServiceNow, not Jira.**

| Product | Jira? | What it really does |
|---|---|---|
| **Nlyte** | **No** | Connectors are ServiceNow / Cherwell / BMC Remedy. Deep bi-directional MAC-workflow ↔ Change Management with ServiceNow. Generic `NgageAPI` for DIY. |
| **Sunbird dcTrack** | Partial | 8.2+ "Universal Ticketing Connector" ships config *templates* for ServiceNow, **Jira** and Remedy over generic REST. Bi-directional asset/CI/ticket sync documented **only** for ServiceNow. |
| **Device42** | **Yes — the real one** | Three surfaces: Jira Cloud/JSM app (create issue from D42, attach CIs); **"Device42 for JSM Assets"** (Marketplace, free, needs **JSM Premium/Enterprise**) syncing devices/power units/parts/**racks** into Assets objects usable as an issue custom field; and a self-managed Jira CMDB connector. **No alarm-driven creation, no reverse status sync, no CAB.** |
| **NetBox Labs** | One-way | `quay.io/netboxlabs/netbox-jira-sync` container: provisions ~28 object types into Jira Assets, then incremental idempotent runs, cron-safe. **NetBox → Assets only, nothing written back.** Core NetBox has generic Event Rules + webhooks; no packaged Jira plugin exists. |
| **Hyperview** | **Marketing only** | Site claims "triggers Jira tickets on threshold breach". The product docs' Integrations page lists Google Maps, AccuWeather, vSphere, ServiceNow CMDB, Equinix — **Jira absent**. Treat the claim as marketing. |
| **Schneider EcoStruxure IT / DCE** | Via alerts | DCE → JSM/Opsgenie by **Alert Action → Send HTTP Post**, with *separate filter conditions for Create Alert and Close Alert* — the canonical closed loop for this vendor. IT Expert → ServiceNow is a **polling** reference app. No Jira issue connector. |
| **Vertiv, FNT, EkkoSense, openDCIM, RackTables** | **No** | Verified absent. FNT names ServiceNow / BMC Helix / Cisco DNA / Nagios / PRTG / SCOM and never Jira. openDCIM and RackTables have no ticket hooks in current source or wiki. |

**The useful reference implementations are the monitoring tools, not the DCIMs:**

| Tool | Dedup key | Close loop |
|---|---|---|
| **Zabbix** (in-tree `media_jira.yaml`, `media_jira_servicedesk.yaml`) | Jira issue key written back **onto the Zabbix event as a tag**, read later as `{EVENT.TAGS.*}` | Update/Recovery operations |
| **LibreNMS** `Alert/Transport/Jira.php` | **None** — create-only, every firing makes a new issue | Webhook mode only, separate Open/Close URLs |
| **Alertmanager** `jira_configs` (native since v0.28; `jiralert` deprecated) | One issue per **`group_by` groupKey**, not per-alert fingerprint | `reopen_transition` within `reopen_duration`; `wont_fix_resolution` blocks reopen |
| **Grafana** native Jira contact point | dedup hash in a **label `ALERT{hash}`** | Resolve / Reopen transitions + reopen window |
| **PagerDuty Events v2** | `dedup_key`; resolve/ack **dropped** if the key does not match | resolved key re-triggers as a *new* alert |
| **JSM Operations / Opsgenie** | **`alias`**, first-class: create against an *open* alert dedups and bumps `count`; closed aliases create new | native ack/close endpoints |

Three things worth stealing, and one worth avoiding:

- **Steal:** store the issue key back on the source record (Zabbix). Every
  integration that skips this ends up JQL-searching on the hot path.
- **Steal:** reopen-window semantics (Alertmanager, Grafana) — reopen inside N
  hours, create fresh after, never reopen a `Won't Fix`.
- **Steal:** separate create-path and close-path filters (Schneider DCE) — the
  conditions that justify opening a ticket are not the conditions that justify
  closing one.
- **Avoid:** LibreNMS's create-only transport. It is the shape we would
  accidentally build.

---

## 3. Design decisions

### D1 — Transactional outbox, not pub/sub
A row in `integration_outbox` is written **in the same session** that raises or
clears the alarm, so the alarm and the intent to ticket it commit together or
not at all. `app/ingest/worker.py:692` already computes alarm actions inside
`unit_of_work()` and fans out after commit — the outbox INSERT goes inside that
block, the HTTP call never does.

A dispatcher claims work with `SELECT … FOR UPDATE SKIP LOCKED LIMIT n`, which
is what makes two ingest workers safe. Rejected alternatives: a Redis stream
(not transactional with the alarm write, and Redis on this deployment has been
OOM-killed twice — `project_dcim_platform`), and calling Jira inline (a 429 or a
5 s Atlassian timeout would stall telemetry ingestion).

### D2 — The dedup key is the alarm key we already have
`fingerprint = sha256("dcim:v1:" + site + ":" + device_id + ":" + alarm_type + ":" + instance)[:16]`

It deliberately excludes severity, the measured value and every timestamp.
Severity is excluded because a WARNING that escalates to CRITICAL is the *same*
condition and must land on the *same* ticket — it is an update, not a second
issue. This mirrors the `alarm_active_key` index exactly, which means the DCIM
and Jira cannot disagree about what "the same problem" is.

The instance component matters here for the reason it already matters on this
estate: 23 conditions share one Raritan trap OID and are told apart only by the
slot carried as the alarm instance (`feedback_shared_trap_oid`). Drop
`instance` from the fingerprint and every probe on a PDU collapses into one
ticket.

### D3 — Store the issue key locally; JQL search is a *reconciler*, not a lookup
`jira_link` holds `(fingerprint → issue_key)`. The dispatch path never searches.
A nightly reconciler uses `POST /rest/api/3/search/jql` to find drift (issues
closed while we were down, links pointing at deleted issues).

`GET`/`POST /rest/api/3/search` are **410 Gone** since Oct 2025 — the
replacement is `POST /rest/api/3/search/jql`, which **no longer defaults
`fields`** (pass them explicitly or get almost nothing back) and has **no
`startAt`/`total`** — paging is a cursor on `nextPageToken`. Counts come from
`POST /rest/api/3/search/approximate-count`.

### D4 — Policy decides what gets a ticket
Default policy, all conditions ANDed:

```
response_class == "alarm"          # not the informational tier
AND severity in (MAJOR, CRITICAL)  # operator-settable
AND NOT is_symptom                 # the root, not the cascade
AND NOT shelved                    # planned work does not raise tickets
AND category in (...)              # default: all seven
AND dwell >= N seconds             # flap damping before the first create
```

Every clause maps to a column that already exists and is already indexed. The
policy is a row in `integration_config`, editable by an admin, previewed
against the last 7 days of `alarm_history` before it is saved — the same
"show me what this would have caught" affordance `POST /maintenance/windows/preview`
gives for windows.

### D5 — A Jira transition acknowledges; only the poll clears
Inbound `jira:issue_updated` with `status → Done` calls the **acknowledge**
path, writing `acknowledged_by = "jira:DCOPS-142"`. It never calls clear.

This is not pedantry. `feedback_traps_need_a_polled_backstop` is a standing rule
here because an event-driven clear once hid a live fault; a ticket closed by an
engineer who fixed the *symptom* would do exactly the same thing. If the
condition really is over, the next poll clears it within one interval and the
DCIM comments on the ticket saying so.

One exception, explicit and off by default: `allow_clear_on_transition` for
alarm types that have no polled backstop (manual-source alarms). Flagged in the
UI as "this trusts Jira over the plane".

### D6 — Jira issues in phase 1; JSM Operations alerts as a second target later
`JiraTarget` is an interface with two implementations sharing the outbox,
policy, fingerprint and rate limiter:

- `IssueTarget` — `/rest/api/3/issue` (Jira Software) or
  `/rest/servicedeskapi/request` (JSM). Phase 1–3.
- `OpsAlertTarget` — `/jsm/ops/api/{cloudId}/v1/alerts` with `alias =
  fingerprint`, native dedup and `/acknowledge` + `/close`. Phase 5.

### D7 — Assets export is one-way, scheduled, and optional
DCIM → JSM Assets, never back. The DCIM is the system of record for physical
infrastructure; a CMDB that can write back into rack elevations is a data
corruption vector. NetBox Labs made the same call and says so plainly.

---

## 4. Data model

Five migrations, `0067`–`0071`.

### 0067 — `integration_config`
One row per configured Jira instance. Copies the `collector_config` shape.

```sql
CREATE TABLE integration (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind          text NOT NULL,              -- 'jira_cloud' | 'jira_dc' | 'jsm_ops'
  name          text NOT NULL UNIQUE,
  enabled       boolean NOT NULL DEFAULT false,
  base_url      text NOT NULL,              -- https://acme.atlassian.net
  cloud_id      text,                       -- for the Ops API; NULL on DC
  config        jsonb NOT NULL DEFAULT '{}'::jsonb,  -- sparse: policy, mappings, project
  version       integer NOT NULL DEFAULT 1,
  -- AES-256-GCM, nonce || ct || tag, same key as device credentials
  secret_enc    bytea NOT NULL,
  secret_hint   text,
  secret_kind   text NOT NULL,              -- 'api_token' | 'pat' | 'oauth2'
  secret_expires_at timestamptz,            -- Cloud API tokens now expire <= 1 year
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  updated_by    text,
  CONSTRAINT ck_integration_kind CHECK (kind IN ('jira_cloud','jira_dc','jsm_ops'))
);
CREATE UNIQUE INDEX ix_integration_enabled_one ON integration (kind) WHERE enabled;
```

`config` holds the sparse, operator-owned half — project key, issue type,
request type id, the severity→priority map, the policy clauses, label prefix,
reopen window, custom-field ids. Absent keys fall through to code defaults, so a
default that changes in a release reaches every install that never overrode it.

`secret_expires_at` is not decoration: Cloud API tokens created after Dec 2024
expire within a year by default, and Atlassian force-expired the pre-Dec-2024
generation in spring 2026. §10.3.

### 0068 — `integration_outbox`
```sql
CREATE TABLE integration_outbox (
  id             bigserial PRIMARY KEY,
  integration_id uuid NOT NULL REFERENCES integration(id) ON DELETE CASCADE,
  kind           text NOT NULL,     -- alarm_raised|alarm_escalated|alarm_cleared|
                                    -- alarm_acked|window_opened|window_closed
  fingerprint    text NOT NULL,
  alarm_id       uuid,              -- no FK: the alarm may be purged before we give up
  payload        jsonb NOT NULL,    -- the frozen alarm row, as it was
  created_at     timestamptz NOT NULL DEFAULT now(),
  not_before     timestamptz NOT NULL DEFAULT now(),   -- backoff / Retry-After
  attempts       smallint NOT NULL DEFAULT 0,
  claimed_by     text,
  claimed_at     timestamptz,
  state          text NOT NULL DEFAULT 'pending',      -- pending|done|dead
  last_error     text,
  CONSTRAINT ck_outbox_state CHECK (state IN ('pending','done','dead'))
);
CREATE INDEX ix_outbox_due ON integration_outbox (not_before, id)
  WHERE state = 'pending';
CREATE INDEX ix_outbox_fp ON integration_outbox (fingerprint, created_at DESC);
```

`payload` freezes the alarm as it was at the moment of the action. The alarm row
mutates (severity escalates, `occurrence_count` climbs); a ticket comment that
says "this was MAJOR at 09:14" must not be rewritten by a later read.

`state = 'dead'` after `max_attempts`, and a dead row raises a platform alarm
rather than disappearing (§11.4).

### 0069 — `jira_link`
```sql
CREATE TABLE jira_link (
  fingerprint    text PRIMARY KEY,
  integration_id uuid NOT NULL REFERENCES integration(id) ON DELETE CASCADE,
  issue_key      text NOT NULL,
  issue_id       text,
  alarm_id       uuid,
  device_id      uuid REFERENCES device(id) ON DELETE SET NULL,
  status         text,            -- last status we saw, from webhook or reconcile
  status_category text,           -- 'To Do' | 'In Progress' | 'Done'
  resolution     text,
  opened_at      timestamptz NOT NULL DEFAULT now(),
  closed_at      timestamptz,
  last_pushed_at timestamptz,
  push_count     integer NOT NULL DEFAULT 0,
  CONSTRAINT ck_jira_link_key CHECK (issue_key ~ '^[A-Z][A-Z0-9_]+-[0-9]+$')
);
CREATE INDEX ix_jira_link_issue ON jira_link (integration_id, issue_key);
CREATE INDEX ix_jira_link_open ON jira_link (fingerprint) WHERE closed_at IS NULL;
```

`issue_key` reverse lookup is what the inbound webhook uses; it must be indexed
because webhooks arrive at Atlassian's cadence, not ours.

### 0070 — alarm ↔ change link on maintenance windows
`change_ref` already exists as free text. Add the structured half:

```sql
ALTER TABLE maintenance_window
  ADD COLUMN jira_issue_key text,
  ADD COLUMN jira_integration_id uuid REFERENCES integration(id) ON DELETE SET NULL,
  ADD COLUMN jira_approval_state text;   -- pending|approved|declined|NULL
CREATE INDEX ix_mw_jira ON maintenance_window (jira_issue_key)
  WHERE jira_issue_key IS NOT NULL;
```

`change_ref` stays as the human-typed field. `jira_issue_key` is the one we
own, and the UI shows one link when they agree and a warning when they do not.

### 0071 — `assets_sync_state`
```sql
CREATE TABLE assets_sync_state (
  integration_id uuid PRIMARY KEY REFERENCES integration(id) ON DELETE CASCADE,
  workspace_id   text,
  schema_id      text,
  -- object type name -> {"id": "23", "attributes": {"Name": "135", ...}}
  type_map       jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_run_at    timestamptz,
  last_run_status text,
  last_cursor    timestamptz,   -- device.updated_at high-water mark
  objects_pushed integer NOT NULL DEFAULT 0,
  last_error     text
);
```

`type_map` is the cache that makes Assets tolerable: everything in the Assets
API is addressed by **numeric attribute id**, never by name, so a sync must
first walk `GET .../v1/objecttype/{id}/attributes` and build the name→id map.
Caching it per integration is the difference between one warm-up call and one
per object.

---

## 5. Outbound path

```
ingest worker (unit_of_work)
  ├─ alarm raised / escalated / cleared        app/alarms/service.py
  ├─ policy.matches(alarm) ────────────── no ─→ nothing, and a debug counter
  └─ outbox.enqueue(session, kind, alarm)      ← SAME transaction
                    │  commit
                    ▼
       integration_outbox (pending)
                    │
  dispatcher (own asyncio task, both workers, SKIP LOCKED)
                    ▼
       JiraTarget.handle(action, payload, link)
                    │
     ┌──────────────┼───────────────────────────┐
  no link        link + open                link + closed
     │              │                            │
 create issue   comment / transition /      inside reopen window?
 + remotelink   escalate priority          yes → transition back
 + attachments  + bump custom fields       no  → create new issue,
     │              │                             link old ("Relates")
     └──────────────┴──────────────┬─────────────┘
                                   ▼
                      upsert jira_link, audit row
```

### 5.1 Dispatcher

New file `backend/app/integrations/dispatcher.py`. Runs as a task alongside the
ingest worker's existing timers (`app/ingest/worker.py:358` is the pattern),
because that process already has the DB session factory, the Redis handle and
a supervised lifecycle. It does **not** run in the API process — the API must
stay free of outbound blocking work.

Claim query:

```sql
UPDATE integration_outbox SET claimed_by = :me, claimed_at = now(), attempts = attempts + 1
WHERE id IN (
  SELECT id FROM integration_outbox
  WHERE state = 'pending' AND not_before <= now()
  ORDER BY id
  FOR UPDATE SKIP LOCKED
  LIMIT :batch
)
RETURNING *;
```

Ordering by `id` keeps per-fingerprint ordering (a clear can never overtake its
own raise) as long as the batch is processed in the returned order and one
fingerprint is never split across concurrent workers — enforced by an in-batch
group-by-fingerprint, processing each group serially.

### 5.2 Retry and backoff
`Retry-After` is honoured verbatim when present, because retrying early against
Jira returns the same or a longer `Retry-After`. Otherwise exponential with
jitter: base 2 s, ×2, cap 300 s, jitter 0.7–1.3, `max_attempts = 8`
(≈25 min of trying). Then `state = 'dead'` and a platform alarm.

Non-retryable and dead immediately: `400` (bad field mapping — retrying a
malformed create forever is how an integration silently burns quota), `401`,
`403`, `404` on the project. Retryable: `429`, `5xx`, connect/read timeouts.

### 5.3 Idempotency
Three independent guards, because a dispatcher can crash after the HTTP call
and before the commit:

1. **`remotelink.globalId`** is an upsert key. Posting
   `globalId = "system=<dcim base url>&alarm=<fingerprint>"` a second time
   updates rather than duplicates, and gives a reverse lookup via the
   `issuesWithRemoteLinksByGlobalId` JQL function.
2. **`fp-<fingerprint>` label** on the issue, so a crashed create is found by
   one JQL search on recovery instead of creating a twin.
3. **Recovery check**: before a create, if `attempts > 1`, search by that label
   first. Cheap because it only happens on retry.

---

## 6. Inbound path

`POST /api/v1/integrations/jira/webhook` — unauthenticated by JWT, authenticated
by HMAC.

### 6.1 Signature verification
Jira Cloud webhooks **do** support HMAC-SHA256 signing — `X-Hub-Signature:
sha256=<hex>` over the raw body, WebSub convention, secret supplied at
registration. Jira Data Center has had the same since 8.x. (The common belief
that classic Jira webhooks are unsigned is out of date; it was true before the
Cloud admin UI gained the secret field.)

Implementation rules:

- Verify against the **exact received bytes**. Read `await request.body()` and
  HMAC that; any middleware that re-serialises JSON before verification breaks
  the signature. FastAPI's `Request.body()` is cached, so the handler can parse
  afterwards.
- `hmac.compare_digest`, never `==`.
- The secret is stored in `integration.config` — **no**, it is stored in a
  second encrypted column, `webhook_secret_enc`, added by 0067. A webhook
  secret is a credential; `config` is returned to the UI.
- Defence in depth, all cheap: a high-entropy path segment
  (`/webhook/{token}`), an optional source-IP allowlist, and a 5-minute
  timestamp window against replay.

### 6.2 Handler contract
Respond `2xx` in under 30 s or Jira retries (5 attempts, randomised 5–15 min
backoff, 20 concurrent per tenant). So: verify, write one row to
`integration_inbox`, return `204`, process on the dispatcher. The handler does
no correlation, no alarm mutation, no outbound call.

### 6.3 What we act on
Register dynamically with `POST /rest/api/3/webhook`, JQL-filtered so we only
receive our own project, and `fieldIdsFilter` narrowed to `status` and
`resolution`:

```json
{ "url": "https://dcim.example.com/api/v1/integrations/jira/webhook/<token>",
  "webhooks": [{
    "events": ["jira:issue_updated", "jira:issue_deleted", "comment_created"],
    "jqlFilter": "project = DCOPS AND labels = dcim",
    "fieldIdsFilter": ["status", "resolution"]
  }] }
```

**Dynamic webhooks expire after 30 days.** A refresh sweep on the dispatcher's
timer extends them; a registration within 7 days of expiry with no successful
refresh raises a platform alarm. This is the single most likely way this
feature dies quietly in production.

Action mapping, read from the payload's `changelog.items[]` so we never re-read
the issue:

| Change | DCIM action |
|---|---|
| `status` → category `Done`, resolution not `Won't Fix`/`Duplicate` | `acknowledge(alarm, actor="jira:<key>", note=<resolution>)` |
| `status` → category `Done`, resolution `Won't Fix`/`Duplicate` | close `jira_link`, do **not** touch the alarm, mark the link `wont_reopen` |
| `status` → `In Progress` | `alarm_history` row `action='jira_in_progress'`, no state change |
| `jira:issue_deleted` | orphan the link, raise a platform alarm — an alarm now has no ticket and nobody knows |
| `comment_created`, public, from a human | append to `alarm_history.detail` |

Every one of these writes an `audit` row naming the Jira key and the Atlassian
account id, through `audit.record` (`app/core/audit.py:67`), which already
redacts and already joins the caller's transaction.

---

## 7. Field mapping

### 7.1 Severity → priority
Priority **ids** are resolved at runtime from `GET /rest/api/3/priority`; names
are site-editable and hardcoding them is how this breaks on a customer site.

| DCIM `severity` | Jira priority | JSM Ops | Datacenter example |
|---|---|---|---|
| `CRITICAL` | Highest | P1 | UPS on battery, chiller plant down, hall over ASHRAE allowable |
| `MAJOR` | High | P2 | CRAH failed with N+1 lost, PDU branch trip |
| `MINOR` | Medium | P3 | Rack inlet above ASHRAE *recommended*, redundancy degraded |
| `WARNING` | Low | P4 | Threshold approaching, filter differential rising |
| `INFO` | Lowest | P5 | Commissioning, config change |

The map is a `config` key so a site can shift the whole scale. If the target is
the Ops API, note that an unrecognised priority string **silently becomes P3** —
so the mapper emits `P1`–`P5` there, never `"CRITICAL"`.

### 7.2 Issue fields

| Alarm field | Jira target | Note |
|---|---|---|
| — | `project.key`, `issuetype.name` | from `config`; required |
| `device_name`, `message` | `summary` | `[{device_name}] {message}` — device code leading makes the queue scannable and `summary ~ "CRAH-DC1"` work |
| full row + raw payload | `description` (ADF) | paragraph, then an ADF `table` of attributes, then one `codeBlock` with the frozen JSON |
| `severity` | `priority` | §7.1 |
| `category`, `datacenter_code`, `room_name` | `labels`: `dcim`, `dcim-{category}`, `site-{code}`, `room-{slug}`, `fp-{fingerprint}` | Labels, not custom fields, for the taxonomy: free, no Jira admin required, fast JQL. Grafana made the same call. |
| `category` | `components` | **Optional and validated first.** A component that does not exist in the project fails the whole create. Off by default. |
| `device_id`, `device_name` | custom field (text) | id resolved via `GET /rest/api/3/field` at config-test time, cached in `config` |
| `rack_name`, `room_name`, `datacenter_code` | custom fields, or an **Assets object field** when Assets sync is on | Assets is strictly better — gives the containment tree and AQL |
| `fingerprint` | label `fp-<hash>` **and** issue property `dcim.alarm` | label for JQL speed, property for structure (32 KB cap) |
| `first_seen`, `last_seen`, `occurrence_count` | custom fields (datetime, number) | **Updated in place on a dedup hit rather than adding a comment.** A field update is one write; a comment is one write plus noise, and the per-issue limit is 20 writes / 2 s. |
| alarm URL | `POST /issue/{key}/remotelink` | `globalId="system=<base>&alarm=<fp>"`; `object.status.resolved` flips true on clear |
| trend PNG, varbind dump | attachment | `multipart/form-data`, part name **must** be `file`, header **must** include `X-Atlassian-Token: no-check` |

### 7.3 JSM vs Jira Software
If the project is a service project, create through
`POST /rest/servicedeskapi/request` with `serviceDeskId` + `requestTypeId`, not
`/rest/api/3/issue`. An issue created without a request type lands in the
project but is malformed on the customer portal and breaks portal automation.
Required fields per request type come from
`GET /rest/servicedesk/{id}/requesttype/{rtId}/field`, fetched at config-test
time.

Known ambiguity: `servicedeskapi` has historically taken `description` as a
**plain string** while `/rest/api/3/issue` demands ADF for the same field. The
mapper carries both renderers and the config test posts a scratch issue to
settle it per tenant. Do not assume.

---

## 8. Change management: JSM Change ↔ maintenance window

This is the half that makes the feature worth building, and the half no
surveyed DCIM product ships.

**Direction 1 — window creates a change.** `POST /maintenance/windows` with
`create_change: true` opens a JSM Change request carrying the window's title,
planned start/end, the device list (with rack/room), and the impact preview
that `POST /maintenance/windows/preview` already computes — "this window would
shelve 43 alarms across 11 racks" is exactly what a CAB needs and exactly what
nobody gives them. The issue key lands in `maintenance_window.jira_issue_key`.

**Direction 2 — approval gates the window.** If `require_approval` is set, the
window stays `scheduled` and the ticker refuses to advance it to `active` until
`jira_approval_state = 'approved'`, set by the inbound webhook reading the JSM
approval. Declined → the window auto-cancels and the requester is told.

The status ticker (`0046`'s column-not-predicate design) is the right place for
this: the gate must be one column both the API and the ingest worker read, for
the same reason `status` is a column and not `now() BETWEEN starts_at AND ends_at`.

**Direction 3 — the window reports back.** On completion, a comment on the
change listing what was shelved, what cleared, and — the useful part — **what
was still alarming when the window closed**. The alarm list already answers
"did anything else break" via the window's shelved set; this posts it.

**Lifecycle changes.** `change_ref` on `lifecycle.py:86` and `bulk.py:53`
was to become a validated Jira key when an integration is enabled, with a
remote link posted back. **Dropped while building** — see §17.

---

## 9. JSM Assets (CMDB) export

Phase 4. One-way, scheduled, resumable.

**Object schema** mirrors the containment tree the platform already owns:
`Datacenter → Room → Row → Rack → Device`, plus `Model`, `Vendor`,
`DeviceType`, `Supplier`. Reference attributes carry the tree, which is what
makes AQL dot-notation (`Rack.Room.Datacenter = "DC1"`) work on the Jira side.

**Transport: the Imports REST API**, not `object/create` per device. It is the
only route that is incremental, mapped, idempotent and progress-reporting:

```
GET  https://api.atlassian.com/jsm/assets/v1/imports/info   → HATEOAS links
GET  {getStatus}       → IDLE | RUNNING | DISABLED | MISSING_MAPPING
PUT  {mapping}         → our schema + object-type/attribute mapping
POST {start}           → { submitProgress, submitResults, getExecutionStatus, cancel }
PUT  {submitProgress}  → { steps: {...}, objects: {total, processed} }
POST {submitResults}   → { data: {...}, clientGeneratedId: "<idempotency key>" }
POST {submitResults}   → { completed: true }
```

The returned URLs are bound to the token that produced them and must not be
stored — `assets_sync_state` caches the *type map*, never the links.

Incremental by `device.updated_at > last_cursor`. Full resync is an explicit
button, because a full push of 1560 servers/DC will meet the Assets external
import rate limiter.

**Three constraints to state up front, not discover:**

1. **JSM Premium or Enterprise.** Assets is not in Standard. Device42's
   Marketplace app carries the same requirement. If the customer is on
   Standard, phase 4 does not apply to them and the UI must say so rather than
   failing at runtime.
2. **Everything is addressed by numeric attribute id**, not name. Hence
   `type_map`.
3. **Atlassian KB reports that Forge and OAuth 2.0 apps cannot reach the Assets
   AQL resource** — it wants Basic auth with a service-account email + API
   token. *This is second-hand and load-bearing; verify against a live tenant
   before phase 4 starts.* If true it confirms D-auth below and forecloses a
   Forge-hosted variant.

**Not built:** Assets Discovery. It is an agentless scanner for IP-enabled
hosts — CPU, RAM, OS, filesystems — and is useless for BACnet CRAHs, Modbus
meters, UPS and PDUs, which is most of what this platform knows that Jira does
not.

---

## 10. Auth and secrets

### 10.1 Which mode
| Deployment | Mode | Header |
|---|---|---|
| Jira Cloud | **API token + Basic**, from a dedicated *service account* | `Authorization: Basic base64(email:token)` |
| Jira Data Center | **Personal Access Token** | `Authorization: Bearer <pat>` |
| Later, optional | OAuth 2.0 3LO | via `api.atlassian.com/ex/jira/{cloudId}` |

Service-account Basic is tier 1 because this is a self-hosted product acting on
behalf of an organisation on its own schedule — there is no end user present at
02:00 to consent, and 3LO's per-user attribution is the wrong model for a
machine that opens tickets.

**Connect and Forge are explicitly out.** Connect is legacy. Forge is
Atlassian-hosted, which is the opposite of what a collector-adjacent process
needs: it cannot initiate calls from the customer's network on our schedule,
and (see §9) reportedly cannot reach the Assets AQL endpoint at all. If a
Marketplace listing is ever wanted, the right Forge artefact is a small *glance
panel* that calls **back** into the DCIM — not the transport.

If 3LO is added later: each refresh returns a **new** refresh token (90 days)
and **invalidates the one just used**. Persist the new token transactionally
*before* using the access token, and serialise refreshes with a lock — a
concurrent double-refresh permanently breaks the integration with
`invalid_grant`.

### 10.2 At rest
Reuse `encrypt_secret` / `decrypt_secret` (`app/core/security.py:33`) and the
existing `DCIM_CREDENTIAL_KEY`. `credential_hint` produces `secret_hint`, and
`hint_is_safe` is asserted in tests exactly as it is for device credentials. No
API ever returns anything but the hint. `SENSITIVE_KEY` in `app/core/audit.py:34`
already matches `token`, `secret`, `api_key` and `authorization`, so config
audit rows redact themselves.

### 10.3 Expiry is a first-class alarm
Cloud API tokens now expire in ≤ 1 year (1–365 days for scoped tokens), and
Atlassian force-expired the whole pre-Dec-2024 generation in spring 2026. A
silently expired token means the DCIM stops ticketing and nothing says so.

Add two types to `PLATFORM_ALARM_TYPES` (`app/alarms/platform.py:71`):

- `integration_credential_expiring` — WARNING at 30 days, MAJOR at 7
- `integration_degraded` — MAJOR when the dead-letter count crosses a
  threshold or auth has failed for 15 minutes

They flow through the existing platform-monitor sweep and land on the alarm
console with everything else, which is the point: a broken integration is a
visibility failure, and visibility failures already have a home here.

---

## 11. Storm control and rate limits

### 11.1 Jira's three limiters
| Limiter | Shape | On 429 |
|---|---|---|
| Hourly **points quota**, per tenant | varies by edition and user count | `RateLimit-Reason: jira-quota-tenant-based` |
| **Burst**, per second per endpoint | ~100 RPS GET/POST, ~50 PUT/DELETE | `jira-burst-based` |
| **Per-issue write** | **20 writes / 2 s**, **100 / 30 s** | `jira-per-issue-on-write` |

The per-issue limiter is the one a DCIM hits first, and it is the reason §7.2
updates custom fields on a dedup hit instead of adding a comment. A client-side
token bucket sits in front of the dispatcher (default 5 RPS, configurable),
with a per-issue-key sub-bucket at 5 writes / 2 s — well under the limit,
because we would rather be slow than throttled.

Bulk create (`POST /rest/api/3/issue/bulk`) caps at **50 issues per request**
and is used only by the backfill tool, never by the dispatcher.

### 11.2 Suppression
After any write on a fingerprint, refuse further writes on it for
`suppress_window` (default 10 min), except a clear and except an escalation to
CRITICAL. Coalesced updates accumulate in the outbox and collapse to one write
when the window opens.

### 11.3 Circuit breaker
If the policy would create more than `storm_threshold` issues in
`storm_window` (default 20 in 5 min), stop creating and open **one**
`DC storm` issue carrying the full list as an attachment, then link subsequent
fingerprints to it with remote links rather than new issues. Every operator
wants this; no surveyed product has it.

The DCIM has an advantage here that generic monitoring does not: it owns the
power and cooling dependency graph, so `correlation.py` has usually already
collapsed the cascade before the exporter sees it. The breaker is the backstop
for the cases correlation misses.

### 11.4 Dead letters
A `dead` row is never silently dropped. It raises `integration_degraded`, shows
in the Integrations settings page with the payload and last error, and has a
**Retry** button (admin only, audited).

---

## 12. API surface

All under `/api/v1/integrations`, new router `backend/app/api/v1/integrations.py`.

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/integrations` | viewer | List, with hints only |
| POST | `/integrations` | admin | Create |
| PATCH | `/integrations/{id}` | admin | Update config / policy, bumps `version` |
| DELETE | `/integrations/{id}` | admin | Remove |
| POST | `/integrations/{id}/test` | admin | Auth check, resolve project / issue type / request type / priority ids / custom field ids, cache into `config`, report what is missing |
| POST | `/integrations/{id}/policy/preview` | operator | "What would this policy have ticketed in the last 7 days" — reads `alarm_history` |
| GET | `/integrations/{id}/outbox` | operator | Pending / dead, filterable |
| POST | `/integrations/{id}/outbox/{row}/retry` | admin | Revive a dead letter |
| GET | `/integrations/{id}/links` | viewer | fingerprint → issue key, with alarm and status |
| POST | `/integrations/{id}/webhooks/register` | admin | Register / refresh the dynamic webhook |
| POST | `/integrations/jira/webhook/{token}` | **HMAC** | Inbound. No JWT. |
| POST | `/alarms/{id}/ticket` | operator | **Create a ticket by hand** for an alarm the policy did not match |
| GET | `/alarms/{id}/ticket` | viewer | Its link, if any |
| POST | `/integrations/{id}/assets/sync` | admin | Kick an Assets run |
| GET | `/integrations/{id}/assets/status` | viewer | Last run, cursor, errors |

`POST /alarms/{id}/ticket` matters more than it looks. It is the escape hatch
that lets the default policy stay conservative: an operator who wants a ticket
for a MINOR gets one in a click, and the policy does not have to be widened to
cover every judgement call.

---

## 13. Frontend

`/settings/integrations`, a fourth entry in `SETTINGS_NAV`
(`frontend/src/features/settings/SettingsLayout.tsx`).

- **List** of integrations: name, kind, project, enabled, last success, pending
  and dead counts. Dead count in `--critical` only when non-zero.
- **Editor**: connection (base URL, auth mode, secret — write-only, shows
  `secret_hint` and `secret_expires_at` with days remaining), target (project,
  issue type or request type, resolved live by `/test`), policy (the clauses of
  D4 as the **segmented facet controls** the chart rulebook mandates — one
  strip per facet, ALL cell first, counts in faint ink), field mapping, storm
  controls.
- **Policy preview** before save: a `VColumns` chart of tickets-per-day over
  the last 30 days under the proposed policy, with the total printed. The
  chart rulebook's form #2, `--accent`, zero-based, values printed.
- **Outbox / dead letters** table, paged with `components/Pagination.tsx` —
  the one pager, as the rulebook requires.
- **On the alarm row and the alarm drawer**: a Jira key chip linking out when a
  link exists, a "Create ticket" action when it does not. Status category
  colours the chip — `--text-faint` for Done, `--accent` otherwise. Never a new
  colour.
- **On the maintenance window page**: the change key, its approval state, and
  the gate's reason when a window is held.

No new chart style, no new pager, no raw hex — all three are CI-enforced
(`scripts/validate_palette.js`).

---

## 14. Configuration

`config` JSONB keys, all sparse, defaults in code:

```jsonc
{
  "project_key": "DCOPS",
  "issue_type": "Incident",            // Jira Software
  "service_desk_id": "10",             // JSM
  "request_type_id": "25",             // JSM
  "policy": {
    "response_classes": ["alarm"],
    "min_severity": "MAJOR",
    "categories": ["power","cooling","environmental","it_equipment","network","capacity","visibility"],
    "exclude_symptoms": true,
    "exclude_shelved": true,
    "dwell_s": 300
  },
  "priority_map": { "CRITICAL": "Highest", "MAJOR": "High", "MINOR": "Medium",
                    "WARNING": "Low", "INFO": "Lowest" },
  "labels": { "prefix": "dcim", "site": true, "room": true, "category": true },
  "fields": { "device": "customfield_10101", "location": "customfield_10102",
              "first_seen": "customfield_10103", "occurrences": "customfield_10104" },
  "components_enabled": false,
  "reopen_window_h": 24,
  "wont_reopen_resolutions": ["Won't Fix", "Duplicate"],
  "close_on_clear": "comment",          // comment | transition | none
  "clear_transition": "Done",
  "allow_clear_on_transition": false,   // D5's exception, off
  "suppress_window_s": 600,
  "storm_threshold": 20,
  "storm_window_s": 300,
  "rate_limit_rps": 5,
  "max_attempts": 8
}
```

Env additions (`app/core/config.py`): `DCIM_PUBLIC_BASE_URL` — the externally
reachable URL of this DCIM, needed for remote links and the webhook
registration. It has no sensible default and the integration refuses to enable
without it; a remote link pointing at `localhost` is worse than no link.

---

## 15. Testing

| Gate | What |
|---|---|
| `pytest` unit | Fingerprint stability across severity change, value change, restart; policy matcher against a table of alarm rows; severity→priority incl. the Ops P1–P5 fallback; ADF builder against the published `adf-json-schema`; HMAC verify incl. a re-serialised-body negative; backoff/jitter bounds |
| `pytest` integration, `respx`-mocked Jira | create → link stored; second raise → update not create; clear → comment/transition; closed + inside window → reopen; closed + outside → new issue linked to old; 429 with `Retry-After` honoured; 400 → dead immediately, no retry; crash-after-POST recovery finds the issue by `fp-` label and does not twin |
| `pytest` concurrency | Two dispatchers against one outbox: `SKIP LOCKED` yields no double-post; per-fingerprint ordering holds (clear never overtakes raise) |
| `pytest` webhook | Done→acknowledge, `Won't Fix`→no alarm change, delete→platform alarm; signature mismatch → 401 and nothing written; handler returns inside 30 s |
| `ruff check app tests` | CI gate |
| `alembic upgrade head` + `downgrade base` | CI gate — 0067–0071 must reverse |
| `npm run build`, `node scripts/validate_palette.js` | CI gates |
| **Live**, against a real Atlassian sandbox | The only way to settle the three open questions in §17. Not in CI. |

A recorded-cassette suite against a real sandbox response set is worth more
than any amount of hand-written mock, because the three things most likely to
break — ADF acceptance on `servicedeskapi`, custom field ids, transition ids —
are all tenant-shaped.

---

## 16. Phases

| Phase | Deliverable | Status |
|---|---|---|
| **1 — outbound spine** | 0067–0069, `integrations/` package, `JiraTarget`/`IssueTarget`, fingerprint, policy, outbox + dispatcher, rate limiter, create/update/comment, remote link, `/integrations` CRUD + `/test`, credential-expiry platform alarms | **Built 2026-09-22.** Reopen window and dead-letter retry landed here rather than in phase 2. |
| **2 — closed loop** | Webhook endpoint + HMAC, registration + refresh sweep, inbox, Done→acknowledge, audit throughout | **Built 2026-09-22.** One correction to §5: `POST /rest/api/3/webhook` is restricted to Connect and OAuth apps, so a Basic-auth service account gets a 403 and Cloud installs register the webhook BY HAND. See §17. |
| **3 — UI + manual ticketing** | Settings page, policy editor + preview chart, outbox table, alarm-row chip, `POST /alarms/{id}/ticket` | **Built 2026-09-22.** Verified on a scratch-DB probe. |
| **4 — change management** | 0071 (not 0070, which phase 2 took), window↔Change, approval gate, completion report comment | **Built 2026-09-22.** `change_ref` validation on lifecycle/bulk was DROPPED — see §17. |
| **5 — Assets export** | 0072, object tree, type map, Imports REST API, incremental cursor, full resync, Premium/Enterprise detection | **Built 2026-09-22.** The §9.3 gate RESOLVED ITSELF — see below. Never run against a real tenant; the export queries were exercised read-only against the live estate (840 objects, no dangling references). |
| **6 — JSM Operations alerts** | `OpsAlertTarget` on the same spine: `alias = fingerprint`, native dedup, `/acknowledge`, `/close`, P1–P5 | **Built 2026-09-22.** Outbound only — see below. No migration needed; `integration.cloud_id` was already there from 0067. |

Phases 1–3 are the minimum coherent product: without 2, tickets are write-only;
without 3, only an admin with `curl` can configure it.

### What phase 6 deliberately leaves out

**Operations alerts are OUTBOUND only.** A human acknowledging or closing an
alert inside JSM does not reach the DCIM. Closing that loop needs the
Opsgenie-heritage integration machinery — a separate webhook surface with its
own registration and payload shape — and it is a phase of its own rather than
a footnote to this one. The consequence, stated plainly so nobody discovers it
at 03:00: in Operations mode a fault acknowledged on a phone still shows as
ACTIVE on the console until the poll clears it. `POST /alarms/{id}/ticket` and
the DCIM's own acknowledge remain the way to stop a page from this side.

### The one thing that is not a preference

`ops_priority_map` is validated to P1–P5 and the target drops anything else.
The Operations API does not reject an unrecognised priority — it **silently
substitutes P3** — so a map containing "Highest" would page the estate's worst
faults at the same urgency as its mildest and nothing anywhere would say so.
That is the only validation in the configuration module that exists to prevent
a silent failure rather than a loud one.

---

## 17. Open questions and what we will not build

**Settled while building:**

* **Dynamic webhook registration is not available to this product on Jira
  Cloud.** `POST /rest/api/3/webhook` is restricted to Connect and OAuth 2.0
  apps; a service account using Basic auth with an API token gets a 403. So
  §5.3's "register dynamically, refresh every 30 days" holds only for Jira
  Data Center (`/rest/webhooks/1.0/webhook`, which accepts a PAT) and for a
  future OAuth mode. On Cloud, `POST /integrations/{id}/webhooks/register`
  mints the URL and the secret and returns them with instructions for an
  administrator to paste into Settings → System → WebHooks. The silver
  lining: a hand-registered webhook **does not expire**, so the 30-day
  refresh problem does not exist on the path most installs will use. The
  sweep and the `integration_webhook_expiring` alarm remain, for the
  registrations this platform makes itself.

**Verify before building:**

1. **Does `POST /rest/servicedeskapi/request` take `description` as a plain
   string or ADF?** Sources disagree. Settle it with a scratch issue on a real
   tenant in phase 1; the mapper carries both renderers until then.
2. **Can a non-Basic credential reach the Assets AQL resource?** Reported
   (Atlassian KB, second-hand) that Forge and OAuth 2.0 apps cannot. Load-bearing
   for phase 5. Verify on a live Premium tenant before phase 5 starts.
3. **Exact hourly point cost of our call mix** against a customer's tenant
   quota. The burst and per-issue limits are documented as numbers; the hourly
   quota is "varies by edition and user count". Measure, do not assume.

**Dropped while building, with reasons:**

* **`change_ref` validation on lifecycle and bulk operations.** The plan had it
  becoming a validated Jira key once an integration is enabled. Building it
  meant a query per request to learn whether one is, and a new way for a bulk
  edit of forty devices to be refused — over a free-text field nothing has ever
  read. The half with real value is the remote link back to the issue, so a
  decommission is visible from the ticket that authorised it; that needs a
  third outbox subject (neither an alarm nor a window) and is worth doing
  deliberately rather than as a footnote to this phase. Left undone, and the
  field stays exactly as free as it was.

**Deliberately not built:**

- A Marketplace app (Connect or Forge). §10.1.
- Bi-directional Assets sync. §D7.
- Assets Discovery integration. §9.
- Ticket-per-alarm. §0.
- Clearing an alarm from a Jira transition, by default. §D5.
- Any inbound path that mutates inventory. A Jira automation rule must not be
  able to decommission a device.

---

## Appendix A — files

```
backend/app/integrations/__init__.py
backend/app/integrations/fingerprint.py      # D2, pure, heavily tested
backend/app/integrations/policy.py           # D4, pure predicate over an alarm row
backend/app/integrations/outbox.py           # enqueue (in-session) + claim (SKIP LOCKED)
backend/app/integrations/dispatcher.py       # the loop, backoff, circuit breaker
backend/app/integrations/ratelimit.py        # token bucket + per-issue sub-bucket
backend/app/integrations/adf.py              # ADF document builder
backend/app/integrations/jira/client.py      # httpx, auth modes, 429 handling
backend/app/integrations/jira/target.py      # IssueTarget: create/update/transition/link
backend/app/integrations/jira/mapping.py     # §7, field + priority resolution
backend/app/integrations/jira/webhook.py     # HMAC verify, changelog reading
backend/app/integrations/jira/assets.py      # phase 5
backend/app/integrations/ops/target.py       # phase 6
backend/app/repositories/integrations.py
backend/app/services/integrations.py
backend/app/api/v1/integrations.py
backend/alembic/versions/0067..0071_*.py
backend/tests/integrations/*
frontend/src/features/settings/Integrations.tsx
frontend/src/features/settings/components/{PolicyEditor,OutboxTable,FieldMap}.tsx
docs/25-jira-integration-plan.md             # this file
```

## Appendix B — primary sources

Jira Cloud REST v3: [issues](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/) ·
[search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/) ·
[remote links](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-remote-links/) ·
[attachments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/) ·
[properties](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-properties/) ·
[bulk](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-bulk-operations/) ·
[ADF](https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/) ·
[rate limiting](https://developer.atlassian.com/cloud/jira/platform/rate-limiting/) ·
[webhooks](https://developer.atlassian.com/cloud/jira/platform/webhooks/) ·
[search/jql migration](https://confluence.atlassian.com/jirakb/run-jql-search-query-using-jira-cloud-rest-api-1289424308.html)

JSM: [requests](https://developer.atlassian.com/cloud/jira/service-desk/rest/api-group-request/) ·
[Operations alerts](https://developer.atlassian.com/cloud/jira/service-desk-ops/rest/v2/api-group-alerts/) ·
[alert de-duplication](https://support.atlassian.com/opsgenie/docs/what-is-alert-de-duplication/) ·
[priority mapping](https://support.atlassian.com/jira/kb/mapping-jira-service-management-ticket-priorities-with-opsgenie-operations-alert-priorities/)

Assets: [objects](https://developer.atlassian.com/cloud/assets/rest/api-group-object/) ·
[AQL](https://developer.atlassian.com/cloud/assets/rest/api-group-aql/) ·
[Imports workflow](https://developer.atlassian.com/cloud/assets/imports-rest-api-guide/workflow/)

Auth: [3LO](https://developer.atlassian.com/cloud/jira/service-desk/oauth-2-authorization-code-grants-3lo-for-apps/) ·
[DC PATs](https://developer.atlassian.com/server/jira/platform/personal-access-token/) ·
[API token expiry](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/) ·
[DC webhook HMAC](https://confluence.atlassian.com/spaces/ADMINJIRASERVER/pages/938846912/Managing+webhooks)

Prior art: [Device42 Jira](https://docs.device42.com/integration/external-integrations/jira-integrations/) ·
[Device42 JSM Assets](https://docs.device42.com/solution-guides/jira-service-management-integration/) ·
[NetBox Labs Jira Assets](https://netboxlabs.com/docs/integrations/tool-integrations/jira-assets/) ·
[Sunbird dcTrack 8.2](https://www.sunbirddcim.com/blog/introducing-dctrack-82) ·
[Nlyte connectors](https://www.nlyte.com/products/connectors/) ·
[Schneider DCE → JSM](https://support.atlassian.com/jira-service-management-cloud/docs/integrate-with-struxureware-data-center-expert/) ·
[Zabbix Jira](https://www.zabbix.com/integrations/jira) ·
[Grafana Jira contact point](https://grafana.com/docs/grafana/latest/alerting/configure-notifications/manage-contact-points/integrations/configure-jira/) ·
[Alertmanager jiralert (deprecated)](https://github.com/prometheus-community/jiralert) ·
[LibreNMS Jira transport](https://docs.librenms.org/Alerting/Transports/Jira/)
