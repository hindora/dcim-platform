#!/usr/bin/env python
"""Chaos scenarios from docs/14-testing-strategy.md §7.

Each scenario is the same shape: check the system is healthy enough to learn
anything, break one thing, observe what the platform says about it, put it back,
and verify it came back. The observation is the point - "it survived" is not a
result, "it raised collector_stale within 47 s and the endpoints went UNKNOWN
rather than OFFLINE" is.

Two rules the harness enforces on itself.

**Always revert, including on failure.** Every scenario restores in a finally
block. The services here have no supervisor - they are background processes
someone started by hand - so a harness that dies mid-scenario leaves the
platform broken. That is worse than not testing.

**Never assert on the absence of evidence alone.** A scenario that expects an
alarm waits for it and reports the latency; a scenario that expects nothing to
happen says what it checked and for how long. "No alarm appeared" is only
meaningful next to "the alarm path was working five minutes ago".

The distinction §7 cares about most, and the one worth reading the code for:
killing the COLLECTOR must make endpoints UNKNOWN, while partitioning the
collector from the DEVICES must make them OFFLINE. Both look like silence from
the database's point of view. Reporting a device as failed when the only thing
that failed was the monitoring is how an operator learns to distrust the
console.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import subprocess
import sys
import time
from typing import Any


def _find_docker() -> str:
    """Locate docker.exe.

    Forward slashes throughout: Windows accepts them, and a backslashed path
    written through two layers of quoting is one collapsed escape away from a
    syntax error - or worse, a path with a carriage return inside it. A
    POSIX-style /c/... path is equally unusable, because CreateProcess needs a
    real Windows path even where a git-bash shell resolves it happily.
    """
    import shutil
    found = shutil.which("docker")
    if found:
        return found
    for candidate in (
        "C:/Program Files/Docker/Docker/resources/bin/docker.exe",
        "C:/ProgramData/DockerDesktop/version-bin/docker.exe",
    ):
        if pathlib.Path(candidate).exists():
            return candidate
    raise SystemExit("docker.exe not found - Postgres and Redis run as "
                     "containers, so these scenarios cannot run without it")


DOCKER = _find_docker()
WSL = ["wsl.exe", "-d", "Ubuntu", "--", "bash", "-lc"]

COLLECTOR_CWD = "/home/hari/dcim-platform/collector"
COLLECTOR_CMD = "./bin/collector --config /home/hari/collector-live.yaml"
WORKER_CWD = "/home/hari/dcim-platform"
WORKER_CMD = "./backend/.venv/bin/python -m app.ingest.worker"


def sh(script: str, timeout: int = 120) -> str:
    """Run a shell script inside WSL, passed on STDIN as bytes.

    Two details, both learned from failures that looked like something else.

    **Stdin, not an argument.** A script handed to `wsl.exe -- bash -lc` is
    parsed by the Windows shell first, which eats every $VAR on the way. The
    restart script therefore sourced deploy/.env into variables that expanded
    to nothing: the collector came back up with an empty Redis password, failed
    every publish with NOAUTH and ERR Protocol error, and looked perfectly
    alive while moving no data. The harness called that a successful restart.

    **Bytes, not text.** With text=True Python translates newlines into the
    pipe on Windows, so each line reaches bash with a trailing carriage return
    and `. deploy/.env` becomes a source of a file whose name ends in CR.
    Encoding explicitly removes the second way to get the same symptom.
    """
    out = subprocess.run(["wsl.exe", "-d", "Ubuntu", "--", "bash", "-s"],
                         input=script.encode("utf-8"),
                         capture_output=True, timeout=timeout)
    return ((out.stdout or b"").decode("utf-8", "replace")
            + (out.stderr or b"").decode("utf-8", "replace"))



def docker(*args: str, timeout: int = 180) -> str:
    out = subprocess.run([DOCKER, *args], capture_output=True, text=True,
                         timeout=timeout)
    return (out.stdout or "") + (out.stderr or "")


def _relaunch(cwd: str, cmd: str, log: str) -> str:
    """A restart script that reconstructs the environment the process needs.

    These services have no supervisor and no unit file: they were launched by
    hand from a shell that had deploy/.env sourced. Relaunching without it
    starts a process that exits immediately on a missing setting, which looks
    exactly like the fault under test.
    """
    return f"""
cd {cwd} || exit 1
set -a
. /home/hari/dcim-platform/deploy/.env
set +a
export DCIM_DATABASE_URL="postgresql+asyncpg://${{POSTGRES_USER:-dcim}}:${{POSTGRES_PASSWORD}}@127.0.0.1:5432/${{POSTGRES_DB:-dcim}}"
export DCIM_REDIS_URL="redis://:${{REDIS_PASSWORD}}@127.0.0.1:6379/0"
setsid nohup {cmd} > /home/hari/{log} 2>&1 < /dev/null &
# Long enough for the child to fail loudly if it is going to. A process that
# exits on a missing setting does so within a second or two, so waiting here
# means the caller's check sees the steady state rather than a pid that is
# about to disappear. A setsid child DOES survive wsl.exe returning - that was
# measured, and is not the reason this used to fail.
sleep 15
echo "relaunched: $(pgrep -f '{cmd}' | tr '
' ' ')"
"""


def pids(pattern: str) -> list[int]:
    """PIDs whose cmdline matches the pattern.

    pgrep executed directly, with no shell anywhere in the chain, so the
    pattern reaches Linux exactly as written. Built as a shell loop first, it
    matched nothing - the Windows shell had eaten every $VAR on the way - and
    the harness cheerfully reported a running collector as already dead.
    """
    out = subprocess.run(["wsl.exe", "-d", "Ubuntu", "--", "pgrep", "-f", pattern],
                         capture_output=True, text=True, timeout=60)
    return [int(x) for x in (out.stdout or "").split() if x.isdigit()]



def restore(pattern: str, cwd: str, cmd: str, log: str,
            attempts: int = 3) -> dict[str, Any]:
    """Restart a service and PROVE it is running, or say why it is not.

    The harness kills services that have no supervisor, so this is the most
    important function in the file: a restore that quietly fails leaves the
    platform down. It therefore retries, and on final failure it returns the
    tail of the child's own log rather than a bare False - "restarted: false"
    sent me looking at WSL session semantics when the answer was a missing
    environment variable printed in the log all along.
    """
    for attempt in range(1, attempts + 1):
        sh(_relaunch(cwd, cmd, log), timeout=120)
        alive = pids(pattern)
        if alive:
            return {"restarted": True, "restart_attempts": attempt,
                    "restarted_pids": alive}
        print(f"  restart attempt {attempt} did not take")
    tail = sh(f"tail -5 /home/hari/{log} 2>/dev/null | cut -c1-200")
    return {"restarted": False, "restart_attempts": attempts,
            "restart_log_tail": tail.strip() or "(the child wrote no log at all)"}


# --- observation --------------------------------------------------------------

async def observe() -> dict[str, Any]:
    """One reading of everything the scenarios assert on.

    Builds and disposes its OWN engine each call. The application's engine is a
    module-level singleton that caches connections bound to the loop that
    created them, and every snapshot here runs in a fresh asyncio.run - so the
    second reading tried to write to a closed transport and died with
    "'NoneType' object has no attribute 'send'". It died mid-scenario, after
    the collector had been killed and before the restart, which is exactly the
    failure mode the harness is supposed to make impossible.
    """
    import os

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(os.environ["DCIM_DATABASE_URL"],
                                 pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT
                  (SELECT count(*) FROM alarm
                    WHERE device_id IS NULL AND state <> 'CLEARED') AS platform_alarms,
                  (SELECT count(*) FROM alarm
                    WHERE state <> 'CLEARED') AS all_alarms,
                  (SELECT extract(epoch FROM (now() - max(ts)))
                     FROM telemetry_sample) AS telemetry_age_s,
                  (SELECT count(*) FROM telemetry_sample) AS samples,
                  (SELECT count(*) FROM endpoint_state WHERE status = 'ONLINE') AS online,
                  (SELECT count(*) FROM endpoint_state WHERE status = 'OFFLINE') AS offline,
                  (SELECT count(*) FROM endpoint_state WHERE status = 'UNKNOWN') AS unknown,
                  (SELECT count(*) FROM endpoint_state WHERE status = 'DEGRADED') AS degraded
            """))).mappings().first()
            types = (await conn.execute(text("""
                SELECT alarm_type, instance, severity::text AS severity
                  FROM alarm WHERE device_id IS NULL AND state <> 'CLEARED'
                 ORDER BY last_seen DESC
            """))).mappings().all()
    finally:
        await engine.dispose()

    out = dict(row) if row else {}
    out["platform_alarm_types"] = [
        {"type": t["alarm_type"], "instance": t["instance"],
         "severity": t["severity"]} for t in types]
    out["at"] = time.time()
    return out



def snapshot() -> dict[str, Any]:
    """A reading, or a recorded failure to read.

    Raising here would abort the scenario mid-outage - after the fault was
    injected and before it was reverted - which is the one thing this harness
    must never do. During a Postgres outage the correct observation IS that the
    database cannot be read, so it is returned as data.
    """
    try:
        return asyncio.run(observe())
    except Exception as exc:
        return {"unreadable": type(exc).__name__, "detail": str(exc)[:120],
                "at": time.time(), "platform_alarm_types": []}


def wait_for(predicate, timeout_s: int, label: str, poll_s: int = 5
             ) -> tuple[bool, float, dict]:
    """Poll until the predicate holds. Returns (met, seconds, last reading).

    Latency to detection is part of the result: an alarm that appears eventually
    and an alarm that appears within a poll cycle are different products.
    """
    started = time.time()
    last: dict = {}
    while time.time() - started < timeout_s:
        last = snapshot()
        if predicate(last):
            return True, time.time() - started, last
        time.sleep(poll_s)
    return False, time.time() - started, last


def has_alarm(state: dict, alarm_type: str) -> bool:
    return any(a["type"] == alarm_type
               for a in state.get("platform_alarm_types", []))


def fresh(state: dict, within_s: float) -> bool:
    """Is the newest sample recent? False for an unreadable snapshot."""
    age = state.get("telemetry_age_s")
    return age is not None and age < within_s


def progressing(state: dict, baseline: dict, minimum: int = 200) -> bool:
    """Are rows still arriving? The correct test for recovery.

    Freshness is the WRONG signal while a backlog drains. Buffered samples are
    written with the timestamps they were collected at, so `now() - max(ts)`
    stays stale for as long as the replay takes while the pipeline is at full
    tilt - measured here at 1,285 rows a second with a reported "age" of three
    minutes. Judged on freshness the Redis recovery looked like a failure; on
    row growth it was plainly working. It is the same distinction the platform
    itself draws between pipeline lag and data age.
    """
    now = state.get("samples")
    was = baseline.get("samples")
    return now is not None and was is not None and (now - was) >= minimum


# --- scenarios ----------------------------------------------------------------

def scenario_collector_kill(args) -> dict:
    """Kill the collector: collector_stale, endpoints UNKNOWN (not OFFLINE)."""
    before = snapshot()
    victims = pids("bin/collector")
    if not victims:
        return {"skipped": "no collector process found"}
    print(f"  collector pids {victims}; killing")
    result: dict[str, Any] = {"before": before, "killed": victims}
    try:
        sh(f"kill -9 {' '.join(str(p) for p in victims)}")
        met, secs, state = wait_for(
            lambda s: has_alarm(s, "collector_stale"),
            args.timeout, "collector_stale")
        result["collector_stale_raised"] = met
        result["detection_seconds"] = round(secs, 1)
        result["during"] = state
        # The assertion §7 actually cares about: the DEVICES did not fail, the
        # monitoring did, so they must not be reported as OFFLINE.
        result["offline_delta"] = state["offline"] - before["offline"]
        result["unknown_delta"] = state["unknown"] - before["unknown"]
    finally:
        print("  restarting collector")
        result.update(restore("bin/collector", COLLECTOR_CWD, COLLECTOR_CMD,
                              "collector-chaos.log"))
    if result.get("restarted"):
        met, secs, state = wait_for(
            lambda s: not has_alarm(s, "collector_stale"),
            args.timeout, "collector_stale cleared")
        result["alarm_cleared"] = met
        result["clear_seconds"] = round(secs, 1)
        result["after"] = state
    return result


def scenario_redis_kill(args) -> dict:
    """Stop Redis: collector_degraded, telemetry shed, events preserved."""
    before = snapshot()
    result: dict[str, Any] = {"before": before, "outage_s": args.outage}
    try:
        print(f"  stopping redis for {args.outage}s")
        docker("stop", "dcim-redis-1")
        time.sleep(args.outage)
        result["during"] = snapshot()
        # The database is up throughout, so the alarm path is readable: this is
        # the one dependency whose loss the platform can still report on.
        result["collector_degraded_during"] = has_alarm(
            result["during"], "collector_degraded")
        log = sh("grep -c 'shedding telemetry' /home/hari/collector-chaos.log "
                 "2>/dev/null || echo 0")
        events = sh(r"grep -c 'shedding events\|dropping events' "
                    "/home/hari/collector-chaos.log 2>/dev/null || echo 0")
        result["telemetry_shed_lines"] = log.strip().splitlines()[-1:] or ["0"]
        # §7 is explicit that events must never be shed - they are state
        # changes, and a dropped one is a fault nobody ever hears about.
        result["events_shed_lines"] = events.strip().splitlines()[-1:] or ["0"]
    finally:
        print("  starting redis")
        docker("start", "dcim-redis-1")
        time.sleep(20)
    # Recovery is the assertion, not survival: telemetry must resume flowing.
    met, secs, state = wait_for(
        lambda s: progressing(s, before), args.timeout, "ingest progressing")
    result["telemetry_resumed"] = met
    result["recovery_seconds"] = round(secs, 1)
    result["after"] = state
    result["samples_written_during_recovery"] = state["samples"] - before["samples"]
    return result


def scenario_postgres_kill(args) -> dict:
    """Stop Postgres: the collector keeps polling, the stream grows, no loss."""
    before = snapshot()
    result: dict[str, Any] = {"before": before, "outage_s": args.outage,
                              "stream_before": _redis_stream_len()}
    try:
        print(f"  stopping postgres for {args.outage}s")
        docker("stop", "dcim-postgres-1")
        # With the database down the observer cannot read; the stream depth is
        # the only visible signal, and it is the one that matters - it shows
        # the pipeline buffering rather than discarding.
        time.sleep(args.outage)
        result["stream_during"] = _redis_stream_len()
        result["collector_alive_during"] = bool(pids("bin/collector"))
        result["worker_alive_during"] = bool(pids("app.ingest.worker"))
    finally:
        print("  starting postgres")
        docker("start", "dcim-postgres-1")
        time.sleep(25)
    met, secs, state = wait_for(
        lambda s: progressing(s, before), args.timeout, "ingest progressing")
    result["telemetry_resumed"] = met
    result["recovery_seconds"] = round(secs, 1)
    result["after"] = state
    result["samples_delta"] = (state.get("samples") or 0) - (before.get("samples") or 0)
    result["stream_after"] = _redis_stream_len()
    return result


def _redis_stream_len() -> int | None:
    """Depth of the telemetry stream.

    Through the client library rather than redis-cli: the CLI is not installed
    on the collector host, and a helper that silently returns None because a
    binary is missing would make "the stream did not grow" indistinguishable
    from "nobody looked".
    """
    async def _len() -> int:
        from redis.asyncio import Redis

        from app.core.config import get_settings
        r = Redis.from_url(get_settings().redis_url)
        try:
            return int(await r.xlen("telemetry.v1"))
        finally:
            await r.aclose()

    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "backend"))
        return asyncio.run(_len())
    except Exception as exc:
        print(f"  (stream length unavailable: {exc})")
        return None


def scenario_worker_kill(args) -> dict:
    """Kill one ingest worker: the survivor reclaims pending entries."""
    before = snapshot()
    victims = pids("app.ingest.worker")
    result: dict[str, Any] = {"before": before, "workers_found": victims}
    if len(victims) < 1:
        return {"skipped": "no ingest worker running"}
    try:
        print(f"  killing worker {victims[0]}")
        sh(f"kill -9 {victims[0]}")
        time.sleep(10)
        result["stream_after_kill"] = _redis_stream_len()
    finally:
        print("  restarting worker")
        result.update(restore("app.ingest.worker", WORKER_CWD, WORKER_CMD,
                              "worker-chaos.log"))
    met, secs, state = wait_for(
        lambda s: fresh(s, 180),
        args.timeout, "ingest resumed")
    result["ingest_resumed"] = met
    result["recovery_seconds"] = round(secs, 1)
    result["after"] = state
    return result


SCENARIOS = {
    "collector-kill": scenario_collector_kill,
    "redis-kill": scenario_redis_kill,
    "postgres-kill": scenario_postgres_kill,
    "worker-kill": scenario_worker_kill,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scenario", choices=[*SCENARIOS, "list"])
    ap.add_argument("--outage", type=int, default=120,
                    help="seconds to keep the dependency down")
    ap.add_argument("--timeout", type=int, default=300,
                    help="seconds to wait for an expected observation")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.scenario == "list":
        for name, fn in SCENARIOS.items():
            print(f"  {name:16} {fn.__doc__.splitlines()[0]}")
        return 0

    print(f"== {args.scenario}")
    result = SCENARIOS[args.scenario](args)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        for k, v in result.items():
            if k in ("before", "during", "after"):
                print(f"  {k:32} online={v.get('online')} offline={v.get('offline')} "
                      f"unknown={v.get('unknown')} "
                      f"telemetry_age={v.get('telemetry_age_s')} "
                      f"platform_alarms={v.get('platform_alarms')}")
            else:
                print(f"  {k:32} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
