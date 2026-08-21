# Chaos scenarios

The faults in `docs/14-testing-strategy.md` §7, as a runnable harness. Each
scenario checks the system is healthy enough to learn anything, breaks one
thing, observes what the platform *says* about it, restores, and verifies the
restore.

The observation is the deliverable. "It survived" is not a result; "it raised
`collector_stale` after 68.5 s, cleared it 24.4 s after the restart, and never
marked a single endpoint OFFLINE" is.

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
| `collector_stale` alarm | **raised**, 68.5 s after the kill (112.9 s on an earlier run that started with staler telemetry) |
| endpoints go UNKNOWN | **not met** — no endpoint became UNKNOWN |
| endpoints do **not** go OFFLINE | **met** — OFFLINE count unchanged |

Both figures are consistent with the design rather than a surprise: a collector
is stale after 60 s without a heartbeat and the platform evaluator runs every
30 s, so detection lands between 60 and 90 s plus however stale the telemetry
already was when the kill landed.

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

## The restart bug, and what it actually was

The first runs reported `restarted: False` and left the collector down twice and
the worker once. The cause written here originally — that a `setsid` child does
not survive the `wsl.exe` session exiting — was **wrong**, and measuring it took
two minutes: a detached child launched with two seconds of parent life is still
running afterwards. Worth recording, because the wrong theory sent the fix in
the direction of sleeps and process supervision instead of at the real problem.

There were three faults, each hiding the next.

**The restart helper was never called.** `_relaunch()` existed and both
scenarios still used an older inline command that never sourced `deploy/.env`.
An edit that would have wired them up had aborted on a failed assertion before
writing the file, so the helper sat there looking correct and doing nothing.

**Scripts were passed as arguments to `bash -lc`.** The Windows shell parsed
them first and ate every `$VAR`, so `. deploy/.env` loaded variables that then
expanded to nothing. This is the failure that mattered most, because it did not
look like a failure: the collector came back up, polled happily, and failed
every publish with `NOAUTH Authentication required` and `ERR Protocol error`
because its Redis URL had an empty password. Five thousand nine hundred and
seventeen publish failures, telemetry shed five hundred at a time, and a harness
cheerfully reporting a successful restart. Scripts now go in on stdin, which
nothing on the Windows side parses.

**`2>/dev/null` hid it.** The sourcing error was suppressed from the first
draft, so the one line that would have named the problem was thrown away. That
redirect is gone: an environment file that cannot be read must be loud, because
everything downstream depends on it.

A fourth was fixed pre-emptively while looking: `text=True` makes Python
translate newlines into the pipe on Windows, so every line would have reached
bash with a trailing carriage return — a second, independent way to produce the
identical symptom. The script is written as bytes now.

`restore()` replaces the fire-and-forget restart. It retries, verifies with
`pgrep`, and on final failure returns the tail of the child's own log instead of
a bare `False` — that log had been reporting the missing password all along.

Verified end to end after the fix, one clean cycle:

```
collector_stale_raised   True      detection 68.5 s
restarted                True      first attempt, pid 18158
alarm_cleared            True      24.4 s after the restart
telemetry_age            11.4 s    pipeline fully recovered
publish failures         0         (was 5917)
```

## Not run, and why

| Row | Status |
|---|---|
| Kill Postgres 5 min | **implemented, not run.** A clean `docker stop/start` on a healthchecked container; the restore path is proven now, so what remains is that this box has 0.5 GB free |
| Kill Redis 2 min | **implemented, not run.** Same. Redis here has been OOM-killed twice before |
| Partition collector from devices | not implemented — needs iptables rules with a timed auto-revert, or the fleet stays partitioned if the session drops |
| Clock skew +5 min | not implemented — there is no per-process clock in WSL, so skewing it moves Postgres and the collector together and tests nothing. Needs `libfaketime` around the collector alone |
| Malformed BACnet APDU | not implemented — needs a hostile responder that answers with a deliberately broken APDU |
| Simulator restart | not run — it is the user's application, and the assertion (no interface-throughput spike after a `sysUpTime` reset) needs a quiet fleet to read cleanly |

The two most valuable remaining rows are the simulator restart, because it
checks counter-reset detection that silently corrupts throughput charts when
wrong, and the partition, because it is the direct counterpart to the
collector-kill result above: the same silence must produce OFFLINE there and
UNKNOWN here, and only one of the two has been measured.
