# Chaos scenarios

The faults in `docs/14-testing-strategy.md` §7, as a runnable harness. Each
scenario checks the system is healthy enough to learn anything, breaks one
thing, observes what the platform *says* about it, restores, and verifies the
restore.

The observation is the deliverable. "It survived" is not a result; "it raised
`collector_stale` after 112.9 s, and the endpoints did not go OFFLINE" is.

```bash
python chaos.py list
python chaos.py collector-kill --timeout 300
python chaos.py worker-kill
python chaos.py redis-kill --outage 120
python chaos.py postgres-kill --outage 300
```

Needs `DCIM_DATABASE_URL`, `DCIM_REDIS_URL`, `DCIM_CREDENTIAL_KEY`,
`DCIM_COLLECTOR_TOKEN`, `DCIM_JWT_SECRET`, `DCIM_ADMIN_PASSWORD`.

## Results

Two of the eight rows were executed against the live stack. The rest are
implemented or deliberately deferred — see below.

### Kill the collector

| Expected (§7) | Observed |
|---|---|
| `collector_stale` alarm | **raised**, 112.9 s after the kill |
| endpoints go UNKNOWN | **not met** — no endpoint became UNKNOWN |
| endpoints do **not** go OFFLINE | **met** — OFFLINE count unchanged |

112.9 s is consistent with the design rather than a surprise: a collector is
stale after 60 s without a heartbeat, and the platform evaluator runs every
30 s, so detection lands between 60 and 90 s plus the sampling interval.

The half that fails is the interesting half. Over the ~2 minute observation the
94 endpoints that stopped reporting moved **ONLINE → DEGRADED**, and none
reached UNKNOWN. The distinction §7 is asking for is the one that matters most
when the monitoring itself breaks:

* **UNKNOWN** — we cannot see the device. Nothing is known about it.
* **OFFLINE** — we can see fine, and the device is not answering.

The platform gets the dangerous half right (it does not accuse 1,386 healthy
devices of being down) but it never reaches the honest half either. DEGRADED
reads as "the device is having trouble", which is a claim about the device made
at the moment the only thing that had failed was the collector. Whether that is
worth a state-machine change or is adequately covered by the `collector_stale`
alarm sitting beside it is a judgement call — but it is not what the test
strategy specifies, and it is recorded here rather than rounded up to a pass.

### Kill an ingest worker

| Expected (§7) | Observed |
|---|---|
| survivor reclaims pending entries, no duplicates | **partially exercised** |
| stream grows while ingestion is stopped | **met** — telemetry.v1 grew to 8001 |
| recovery | **met** — telemetry resumed 1.3 s after the worker returned |

Only one worker runs in this deployment, so this exercised buffer-and-recover,
not the `XAUTOCLAIM` handover between two live workers. The reclaim path needs a
second worker running before the kill; the harness does not start one yet.

## A defect in the harness itself

`_relaunch()` is not reliable in this environment. It reports
`restarted: False` and leaves the service down, and it did so after killing the
collector — twice — and after killing the worker once. Each time the service had
to be restored by hand.

The cause is not fully pinned. A `setsid nohup` child started from a
`wsl.exe … bash -s` session does not survive the session exiting, and extending
the parent's lifetime to 20 s did not fix it, though the identical command typed
as a heredoc does survive. Until that is understood:

**Do not run these scenarios unattended, and check the services afterwards.**

```bash
wsl -d Ubuntu -- pgrep -af 'bin/collector|app.ingest.worker'
```

Restoring by hand — the pattern that works — is in `docs/` and reproduced here:

```bash
cd /home/hari/dcim-platform/collector
set -a; . ../deploy/.env; set +a
export DCIM_REDIS_URL="redis://:${REDIS_PASSWORD}@127.0.0.1:6379/0"
setsid nohup ./bin/collector --config /home/hari/collector-live.yaml \
    > /home/hari/collector.log 2>&1 < /dev/null &
```

The collector reads its Redis URL from `DCIM_REDIS_URL` (`url_env` in
`collector-live.yaml`); the fallback `url:` in the file carries no password, so
a relaunch that forgets the variable comes up polling happily and fails every
publish with `NOAUTH Authentication required`. It looks alive and moves no data.

## Not run, and why

| Row | Status |
|---|---|
| Kill Postgres 5 min | **implemented, not run.** Clean `docker stop/start` on a healthchecked container, so the risk is modest — but the harness's restore step is unproven, and this box has 0.5 GB free |
| Kill Redis 2 min | **implemented, not run.** Same reason. Redis here has been OOM-killed before |
| Partition collector from devices | not implemented — needs iptables rules with a timed auto-revert, or the fleet stays partitioned if the session drops |
| Clock skew +5 min | not implemented — there is no per-process clock in WSL, so skewing it moves Postgres and the collector together and tests nothing. Needs `libfaketime` around the collector alone |
| Malformed BACnet APDU | not implemented — needs a hostile responder that answers with a deliberately broken APDU |
| Simulator restart | not run — it is the user's application, and the assertion (no interface-throughput spike after a `sysUpTime` reset) needs a quiet fleet to read cleanly |

The two most valuable remaining rows are the simulator restart, because it
checks counter-reset detection that silently corrupts throughput charts when
wrong, and the partition, because it is the direct counterpart to the
collector-kill result above: the same silence must produce OFFLINE there and
UNKNOWN here, and only one of the two has been measured.
