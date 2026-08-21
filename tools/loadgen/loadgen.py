#!/usr/bin/env python
"""Load generation for the DCIM platform: synthetic endpoints that answer real SNMP.

The point is to exercise the collector, not to mock it. Every synthetic endpoint
is a real snmpsim agent answering real GETs and GETBULKs over UDP, seeded into
inventory so the collector picks it up through the ordinary assignment path. No
code in the collector or the backend knows this is a test.

Three design decisions are worth stating because they change what the numbers
mean.

**Communities, not addresses, select the agent.** snmpsim routes a request to
``<community>.snmprec``, so one process serves thousands of distinct agents. The
alternative - one listener per endpoint - runs out of file descriptors and
memory long before it runs out of interest.

**Each endpoint still gets its own destination address.** On Linux the whole of
127/8 is locally routable without configuring a single alias, so synthetic
endpoints are addressed 127.16.x.y and snmpsim binds 0.0.0.0. That keeps the
socket fan-out on the collector side realistic; pointing 5,000 endpoints at one
address would measure a scheduler and call it a network.

**The datasets answer the OIDs the collector actually asks for.** They were
written from contracts/mappings/snmp/standard.yaml rather than from a MIB dump.
An agent that does not carry the mapped OIDs still responds - so the poll
"succeeds" - while returning nothing, which inflates completion and deflates
every latency, and produces a load test that passes by measuring an empty
conversation.

Usage:

    python loadgen.py generate --count 5000 --out /tmp/loadgen-data
    <start snmpsim against that directory - see README>
    python loadgen.py seed --count 5000
    python loadgen.py measure --window 10
    python loadgen.py teardown
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import pathlib
import statistics
import sys
import time
import urllib.request

# --- what a synthetic agent serves --------------------------------------------
#
# Enough of SNMPv2-MIB, IF-MIB, HOST-RESOURCES-MIB and UCD-SNMP-MIB that the
# `system`, `interfaces` and `host_resources` profiles all return data.
# snmprec format: OID|TAG|VALUE, tags per snmpsim (2=Integer, 4=OctetString,
# 6=ObjectIdentifier, 65=Counter32, 66=Gauge32, 67=TimeTicks, 70=Counter64).

IFACES = 8


def snmprec_lines(index: int, uptime_ticks: int) -> list[str]:
    rows: list[str] = [
        "1.3.6.1.2.1.1.1.0|4|DCIM loadgen synthetic agent",
        "1.3.6.1.2.1.1.2.0|6|1.3.6.1.4.1.99999.1",
        f"1.3.6.1.2.1.1.3.0|67|{uptime_ticks}",
        f"1.3.6.1.2.1.1.5.0|4|load-{index:05d}",
        f"1.3.6.1.2.1.2.1.0|2|{IFACES}",
    ]
    for i in range(1, IFACES + 1):
        base = index * 1000 + i
        rows += [
            f"1.3.6.1.2.1.2.2.1.1.{i}|2|{i}",
            f"1.3.6.1.2.1.2.2.1.7.{i}|2|1",             # ifAdminStatus up
            f"1.3.6.1.2.1.2.2.1.8.{i}|2|1",             # ifOperStatus up
            f"1.3.6.1.2.1.2.2.1.13.{i}|65|{base % 7}",  # ifInDiscards
            f"1.3.6.1.2.1.2.2.1.14.{i}|65|{base % 3}",  # ifInErrors
            f"1.3.6.1.2.1.2.2.1.19.{i}|65|{base % 5}",
            f"1.3.6.1.2.1.2.2.1.20.{i}|65|{base % 2}",
            f"1.3.6.1.2.1.31.1.1.1.1.{i}|4|Ethernet{i}",
            # Counter64s large enough that the rate calculation has something
            # to do and small enough not to wrap during a run.
            f"1.3.6.1.2.1.31.1.1.1.6.{i}|70|{base * 1_000_003}",
            f"1.3.6.1.2.1.31.1.1.1.10.{i}|70|{base * 1_000_007}",
            f"1.3.6.1.2.1.31.1.1.1.15.{i}|66|10000",
        ]
    for cpu in range(1, 5):
        rows.append(f"1.3.6.1.2.1.25.3.3.1.2.{cpu}|2|{(index + cpu) % 90}")
    rows += [
        "1.3.6.1.4.1.2021.4.5.0|2|16777216",
        f"1.3.6.1.4.1.2021.4.6.0|2|{8388608 - (index % 1000) * 100}",
    ]
    # snmpsim requires the file to be sorted by OID, lexicographically over the
    # numeric arcs. Sorting as text puts .10 before .2 and the responder then
    # answers GETNEXT out of order, which looks like a broken agent.
    rows.sort(key=lambda line: [int(p) for p in line.split("|")[0].split(".")])
    return rows


def synthetic_address(index: int) -> str:
    """127.16.x.y - inside loopback, so no interface aliases are needed."""
    return str(ipaddress.ip_address(0x7F100000 + index + 1))


def community(index: int) -> str:
    return f"load-{index:05d}"


# --- generate -----------------------------------------------------------------

def cmd_generate(args: argparse.Namespace) -> int:
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    uptime = int(time.time() % 100_000) * 100
    for i in range(args.count):
        (out / f"{community(i)}.snmprec").write_text(
            "\n".join(snmprec_lines(i, uptime + i)) + "\n", encoding="utf-8")
    print(f"wrote {args.count} datasets to {out}")
    print(f"  each answers {len(snmprec_lines(0, 0))} OIDs across "
          f"{IFACES} interfaces")
    print("\nstart the responder against them (a SECOND instance - never the "
          "one serving the real fleet):")
    print(f"  snmpsim-command-responder --data-dir={out} "
          f"--agent-udpv4-endpoint=0.0.0.0:{args.port} --process-user=root "
          f"--process-group=root")
    return 0


# --- seed / teardown ----------------------------------------------------------

LOADTEST_DC = "LOADTEST"


async def _session():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "backend"))
    from app.db.session import get_sessionmaker
    return get_sessionmaker()


async def _seed(count: int, port: int, interval: int) -> None:
    from sqlalchemy import text

    from app.core.security import credential_hint, encrypt_secret
    maker = await _session()
    async with maker() as s:
        dc = (await s.execute(text("""
            INSERT INTO datacenter (code, name)
            VALUES (:code, 'Load test (synthetic)')
            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
        """), {"code": LOADTEST_DC})).scalar()
        room = (await s.execute(text("""
            INSERT INTO room (datacenter_id, name)
            VALUES (:dc, 'Synthetic Hall')
            ON CONFLICT (datacenter_id, name) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
        """), {"dc": dc})).scalar()

        # metric_groups selects which mapping profiles the collector asks for.
        # These three are exactly what the generated datasets answer; asking for
        # entity_sensors as well would produce a poll that half-fails and a
        # completion figure that measures the fixture rather than the collector.
        profile = (await s.execute(text("""
            INSERT INTO poll_profile (name, interval_s, timeout_ms, retries,
                                      metric_groups, push_enabled)
            VALUES ('loadgen-snmp', :i, 3000, 1,
                    ARRAY['system','interfaces','host_resources'], false)
            ON CONFLICT (name) DO UPDATE SET interval_s = EXCLUDED.interval_s
            RETURNING id
        """), {"i": interval})).scalar()

        made = 0
        for i in range(count):
            addr = synthetic_address(i)
            comm = community(i)
            # Conflict on external_id: device.name carries no unique index, so
            # re-running seed would otherwise insert a second copy of every
            # device rather than being idempotent.
            dev = (await s.execute(text("""
                INSERT INTO device (name, external_id, device_type, lifecycle,
                                    room_id, mgmt_ip)
                VALUES (:name, :ext, 'switch', 'in_service', :room,
                        CAST(:ip AS inet))
                ON CONFLICT (external_id) DO UPDATE SET room_id = EXCLUDED.room_id
                RETURNING id
            """), {"name": f"LOAD-{i:05d}", "ext": f"loadgen-{i:05d}",
                   "room": room, "ip": addr})).scalar()

            cred = (await s.execute(text("""
                INSERT INTO credential (name, protocol, kind, secret_enc, secret_hint)
                VALUES (:name, 'snmp', 'snmp_v2c', :blob, :hint)
                ON CONFLICT (name) DO UPDATE SET secret_enc = EXCLUDED.secret_enc
                RETURNING id
            """), {"name": f"loadgen-{comm}",
                   "blob": encrypt_secret({"community": comm}),
                   "hint": credential_hint("snmp_v2c", {"community": comm})})).scalar()

            # The conflict target must match the partial expression index
            # exactly - (device_id, protocol, role, COALESCE(host(address),''))
            # - or Postgres refuses with "no unique or exclusion constraint
            # matching the ON CONFLICT specification".
            await s.execute(text("""
                INSERT INTO device_endpoint (device_id, protocol, role, address,
                                             port, enabled, credential_id,
                                             poll_profile_id)
                VALUES (:dev, 'snmp', 'native_card', CAST(:ip AS inet), :port, true,
                        :cred, :profile)
                ON CONFLICT (device_id, protocol, role,
                             COALESCE(host(address), ''))
                DO UPDATE SET enabled = true, poll_profile_id = EXCLUDED.poll_profile_id
            """), {"dev": dev, "ip": addr, "port": port, "cred": cred,
                   "profile": profile})
            made += 1
            if made % 500 == 0:
                await s.commit()
                print(f"  seeded {made}/{count}")
        await s.commit()
    print(f"seeded {made} synthetic endpoints in datacenter {LOADTEST_DC}")
    print("the collector picks them up on its next assignment fetch "
          "(30 s by default)")


async def _teardown() -> None:
    from sqlalchemy import text
    maker = await _session()
    async with maker() as s:
        # Order matters: alarms and state reference endpoints, endpoints
        # reference devices and credentials.
        counts = {}
        # Telemetry first, and by device id rather than by cascade: the
        # hypertables carry no foreign key to device, so deleting the devices
        # leaves their samples behind as orphans that still answer every
        # analytics query - a synthetic room contributing load to a capacity
        # report for a room that no longer exists.
        ids = (await s.execute(text("""
            SELECT d.id FROM device d
              JOIN room r ON r.id = d.room_id
              JOIN datacenter dc ON dc.id = r.datacenter_id
             WHERE dc.code = :code
        """), {"code": LOADTEST_DC})).scalars().all()
        if ids:
            for table in ("telemetry_sample", "telemetry_bool", "telemetry_text"):
                res = await s.execute(
                    text(f"DELETE FROM {table} WHERE device_id = ANY(:ids)"),
                    {"ids": ids})
                counts[table] = res.rowcount
            res = await s.execute(text("""
                DELETE FROM poll_result WHERE endpoint_id IN
                    (SELECT id FROM device_endpoint WHERE device_id = ANY(:ids))
            """), {"ids": ids})
            counts["poll_result"] = res.rowcount
            await s.commit()

        for label, sql in (
            ("alarms", """DELETE FROM alarm WHERE device_id IN (
                            SELECT d.id FROM device d
                              JOIN room r ON r.id = d.room_id
                              JOIN datacenter dc ON dc.id = r.datacenter_id
                             WHERE dc.code = :code)"""),
            ("endpoints", """DELETE FROM device_endpoint WHERE device_id IN (
                            SELECT d.id FROM device d
                              JOIN room r ON r.id = d.room_id
                              JOIN datacenter dc ON dc.id = r.datacenter_id
                             WHERE dc.code = :code)"""),
            ("devices", """DELETE FROM device WHERE room_id IN (
                            SELECT r.id FROM room r
                              JOIN datacenter dc ON dc.id = r.datacenter_id
                             WHERE dc.code = :code)"""),
            ("credentials", "DELETE FROM credential WHERE name LIKE 'loadgen-%'"),
            ("rooms", """DELETE FROM room WHERE datacenter_id IN (
                            SELECT id FROM datacenter WHERE code = :code)"""),
            ("datacenter", "DELETE FROM datacenter WHERE code = :code"),
        ):
            res = await s.execute(text(sql), {"code": LOADTEST_DC})
            counts[label] = res.rowcount
        await s.execute(text("DELETE FROM poll_profile WHERE name = 'loadgen-snmp'"))
        await s.commit()
    print("removed:", ", ".join(f"{v} {k}" for k, v in counts.items()))


def cmd_seed(args: argparse.Namespace) -> int:
    asyncio.run(_seed(args.count, args.port, args.interval))
    return 0


def cmd_teardown(args: argparse.Namespace) -> int:
    asyncio.run(_teardown())
    return 0


# --- measure ------------------------------------------------------------------

def _scrape(url: str) -> dict[str, float]:
    """Parse a Prometheus exposition into {series: value}."""
    out: dict[str, float] = {}
    with urllib.request.urlopen(url, timeout=30) as r:
        for line in r.read().decode().splitlines():
            if not line or line.startswith("#"):
                continue
            name, _, value = line.rpartition(" ")
            try:
                out[name.strip()] = float(value)
            except ValueError:
                continue
    return out


def _histogram_quantile(buckets: list[tuple[float, float]], q: float) -> float | None:
    """Linear interpolation inside the bucket that contains the quantile.

    The same approximation Prometheus makes, with the same caveat: resolution is
    bounded by the bucket edges, so a p95 reported as 1.0 means "somewhere
    between 0.5 and 1.0", not exactly one second.
    """
    buckets = sorted(buckets)
    if not buckets:
        return None
    total = buckets[-1][1]
    if total <= 0:
        return None
    target = q * total
    prev_edge, prev_count = 0.0, 0.0
    for edge, count in buckets:
        if count >= target:
            if edge == float("inf"):
                return prev_edge
            span = count - prev_count
            if span <= 0:
                return edge
            return prev_edge + (edge - prev_edge) * (target - prev_count) / span
        prev_edge, prev_count = edge, count
    return buckets[-1][0]


def cmd_measure(args: argparse.Namespace) -> int:
    collector = _scrape(args.collector_metrics)
    backend = {}
    try:
        backend = _scrape(args.backend_metrics)
    except Exception as exc:
        print(f"  (backend metrics unavailable: {exc})")

    polls_ok = sum(v for k, v in collector.items()
                   if k.startswith("dcim_collector_polls_total{")
                   and 'result="success"' in k)
    polls_all = sum(v for k, v in collector.items()
                    if k.startswith("dcim_collector_polls_total{"))
    completion = 100.0 * polls_ok / polls_all if polls_all else None

    buckets: dict[float, float] = {}
    for k, v in collector.items():
        if k.startswith("dcim_collector_poll_duration_seconds_bucket{"):
            le = k.split('le="')[1].split('"')[0]
            edge = float("inf") if le == "+Inf" else float(le)
            buckets[edge] = buckets.get(edge, 0.0) + v
    p95 = _histogram_quantile(list(buckets.items()), 0.95)
    p50 = _histogram_quantile(list(buckets.items()), 0.50)

    endpoints = sum(v for k, v in collector.items()
                    if k.startswith("dcim_collector_endpoints{"))
    lag = next((v for k, v in backend.items()
                if k.startswith("dcim_ingest_lag_seconds")), None)
    pending = sum(v for k, v in backend.items()
                  if k.startswith("dcim_ingest_stream_pending{"))

    snapshot = {
        "at": time.time(),
        "endpoints": endpoints,
        "polls_total": polls_all,
        "completion_pct": completion,
        "poll_p50_s": p50,
        "poll_p95_s": p95,
        "ingest_lag_s": lag,
        "stream_pending": pending,
        "goroutines": collector.get("go_goroutines"),
        "open_fds": collector.get("process_open_fds"),
        "rss_mb": (collector.get("process_resident_memory_bytes") or 0) / 1e6,
        "publish_queue": collector.get("dcim_collector_publish_queue_depth"),
        "publish_dropped": sum(v for k, v in collector.items()
                               if k.startswith("dcim_collector_publish_dropped_total{")),
    }

    if args.json:
        print(json.dumps(snapshot, indent=2))
    else:
        _print_snapshot(snapshot)
    if args.save:
        pathlib.Path(args.save).write_text(json.dumps(snapshot), encoding="utf-8")
    return 0


def _fmt(v, unit="", nd=2):
    return "—" if v is None else f"{v:.{nd}f}{unit}"


def _print_snapshot(s: dict) -> None:
    # Counters are cumulative since collector start, so completion here is a
    # lifetime figure. For a windowed number, take two snapshots and diff them
    # with `compare`.
    print(f"  endpoints          {_fmt(s['endpoints'], nd=0)}")
    print(f"  polls (lifetime)   {_fmt(s['polls_total'], nd=0)}")
    print(f"  completion         {_fmt(s['completion_pct'], '%')}   target > 99.5%")
    print(f"  poll p50           {_fmt(s['poll_p50_s'], ' s', 3)}")
    print(f"  poll p95           {_fmt(s['poll_p95_s'], ' s', 3)}   target < 1 s")
    print(f"  ingest lag         {_fmt(s['ingest_lag_s'], ' s', 3)}   target < 5 s")
    print(f"  stream pending     {_fmt(s['stream_pending'], nd=0)}")
    print(f"  goroutines         {_fmt(s['goroutines'], nd=0)}   must be FLAT, not bounded")
    print(f"  open fds           {_fmt(s['open_fds'], nd=0)}   must be FLAT")
    print(f"  collector RSS      {_fmt(s['rss_mb'], ' MB', 1)}")
    print(f"  publish queue      {_fmt(s['publish_queue'], nd=0)}")
    print(f"  publish dropped    {_fmt(s['publish_dropped'], nd=0)}   target 0")


def cmd_compare(args: argparse.Namespace) -> int:
    """Diff two snapshots: the windowed view the pass criteria actually want."""
    a = json.loads(pathlib.Path(args.before).read_text(encoding="utf-8"))
    b = json.loads(pathlib.Path(args.after).read_text(encoding="utf-8"))
    elapsed = b["at"] - a["at"]
    polls = (b["polls_total"] or 0) - (a["polls_total"] or 0)
    ok_a = (a["completion_pct"] or 0) / 100 * (a["polls_total"] or 0)
    ok_b = (b["completion_pct"] or 0) / 100 * (b["polls_total"] or 0)
    completion = 100.0 * (ok_b - ok_a) / polls if polls else None

    print(f"  window             {elapsed / 60:.1f} min")
    print(f"  polls in window    {polls:.0f}  ({polls / max(1, elapsed):.1f}/s)")
    print(f"  completion         {_fmt(completion, '%')}   target > 99.5%")
    print(f"  poll p95 (now)     {_fmt(b['poll_p95_s'], ' s', 3)}   target < 1 s")
    print(f"  ingest lag (now)   {_fmt(b['ingest_lag_s'], ' s', 3)}   target < 5 s")
    for key, label, unit in (("goroutines", "goroutines", ""),
                             ("open_fds", "open fds", ""),
                             ("rss_mb", "collector RSS", " MB")):
        before, after = a.get(key), b.get(key)
        if before is None or after is None:
            continue
        delta = after - before
        rate = delta / (elapsed / 3600) if elapsed else 0
        print(f"  {label:18} {before:.0f} -> {after:.0f}{unit} "
              f"({delta:+.0f}, {rate:+.1f}/h)")
    print(f"  publish dropped    {(b['publish_dropped'] or 0) - (a['publish_dropped'] or 0):+.0f}"
          f"   target 0")
    return 0


# --- query load ---------------------------------------------------------------

async def _query_load(base: str, token: str, clients: int, seconds: int,
                      paths: list[str]) -> dict:
    import httpx

    latencies: list[float] = []
    errors = 0
    stop = time.monotonic() + seconds

    async def worker(n: int) -> None:
        nonlocal errors
        async with httpx.AsyncClient(
                base_url=base, timeout=30.0,
                headers={"Authorization": f"Bearer {token}"}) as client:
            i = n
            while time.monotonic() < stop:
                path = paths[i % len(paths)]
                i += 1
                started = time.perf_counter()
                try:
                    r = await client.get(path)
                    if r.status_code >= 400:
                        errors += 1
                except Exception:
                    errors += 1
                    continue
                latencies.append(time.perf_counter() - started)

    await asyncio.gather(*(worker(n) for n in range(clients)))
    latencies.sort()
    return {
        "requests": len(latencies), "errors": errors,
        "rps": len(latencies) / seconds,
        "p50": statistics.median(latencies) if latencies else None,
        "p95": latencies[int(len(latencies) * 0.95)] if latencies else None,
        "p99": latencies[int(len(latencies) * 0.99)] if latencies else None,
        "max": latencies[-1] if latencies else None,
    }


def cmd_query_load(args: argparse.Namespace) -> int:
    token = _login(args.api, args.user, args.password)
    # What a dashboard client actually pulls, not a single cheap endpoint: a
    # load test against /health measures the HTTP stack and nothing else.
    paths = ["/dashboard/summary", "/alarms/summary", "/alarms?limit=50",
             "/devices?limit=50", "/collector/instances", "/events?limit=50"]
    result = asyncio.run(_query_load(f"{args.api}/api/v1", token, args.clients,
                                     args.seconds, paths))
    print(f"  {args.clients} concurrent clients for {args.seconds}s over "
          f"{len(paths)} dashboard endpoints")
    print(f"  requests           {result['requests']}  ({result['rps']:.1f}/s)")
    print(f"  errors             {result['errors']}")
    print(f"  p50                {_fmt(result['p50'], ' s', 3)}")
    print(f"  p95                {_fmt(result['p95'], ' s', 3)}   target < 0.5 s")
    print(f"  p99                {_fmt(result['p99'], ' s', 3)}")
    print(f"  max                {_fmt(result['max'], ' s', 3)}")
    return 0


# --- websocket fan-out --------------------------------------------------------

async def _ws_fanout(base: str, token: str, clients: int, topics: int,
                     seconds: int) -> dict:
    import websockets

    frames = 0
    connected = 0
    dropped = 0

    async def client(n: int) -> None:
        nonlocal frames, connected, dropped
        # One ticket per connection, which is the documented rule.
        ticket = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"{base}/api/v1/ws/ticket", data=b"{}",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})).read())["ticket"]
        url = base.replace("http", "ws") + f"/api/v1/ws?ticket={ticket}"
        try:
            async with websockets.connect(url, open_timeout=30) as ws:
                connected += 1
                await ws.send(json.dumps({
                    "action": "subscribe",
                    "topics": [f"device.{(n * topics + t) % 600}"
                               for t in range(topics)]}))
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=2.0)
                        frames += 1
                    except TimeoutError:
                        continue
        except Exception:
            dropped += 1

    await asyncio.gather(*(client(n) for n in range(clients)))
    return {"connected": connected, "frames": frames, "failed": dropped}


def cmd_ws_fanout(args: argparse.Namespace) -> int:
    token = _login(args.api, args.user, args.password)
    before = _scrape(args.backend_metrics)
    result = asyncio.run(_ws_fanout(args.api, token, args.clients, args.topics,
                                    args.seconds))
    after = _scrape(args.backend_metrics)
    slow_before = before.get("dcim_ws_slow_consumer_disconnects_total", 0)
    slow_after = after.get("dcim_ws_slow_consumer_disconnects_total", 0)
    print(f"  {args.clients} clients x {args.topics} topics for {args.seconds}s")
    print(f"  connected          {result['connected']}")
    print(f"  failed to connect  {result['failed']}")
    print(f"  frames received    {result['frames']}")
    print(f"  slow-consumer drops {slow_after - slow_before:.0f}   target 0")
    return 0


# --- shared -------------------------------------------------------------------

def _login(api: str, user: str, password: str) -> str:
    req = urllib.request.Request(
        f"{api}/api/v1/login",
        data=json.dumps({"username": user, "password": password}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["token"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write snmprec datasets")
    g.add_argument("--count", type=int, default=5000)
    g.add_argument("--out", default="/tmp/loadgen-data")
    g.add_argument("--port", type=int, default=1161)
    g.set_defaults(func=cmd_generate)

    s = sub.add_parser("seed", help="insert synthetic endpoints into inventory")
    s.add_argument("--count", type=int, default=5000)
    s.add_argument("--port", type=int, default=1161)
    s.add_argument("--interval", type=int, default=30)
    s.set_defaults(func=cmd_seed)

    t = sub.add_parser("teardown", help="remove everything seed created")
    t.set_defaults(func=cmd_teardown)

    m = sub.add_parser("measure", help="snapshot the pass criteria")
    m.add_argument("--collector-metrics", default="http://127.0.0.1:9100/metrics")
    m.add_argument("--backend-metrics", default="http://127.0.0.1:8000/metrics")
    m.add_argument("--json", action="store_true")
    m.add_argument("--save")
    m.add_argument("--window", type=int, default=10)
    m.set_defaults(func=cmd_measure)

    c = sub.add_parser("compare", help="diff two saved snapshots")
    c.add_argument("before")
    c.add_argument("after")
    c.set_defaults(func=cmd_compare)

    q = sub.add_parser("query-load", help="concurrent dashboard clients")
    q.add_argument("--api", default="http://127.0.0.1:8000")
    q.add_argument("--clients", type=int, default=50)
    q.add_argument("--seconds", type=int, default=60)
    q.add_argument("--user", default=os.environ.get("DCIM_ADMIN_USER", "admin"))
    q.add_argument("--password", default=os.environ.get("DCIM_ADMIN_PASSWORD", ""))
    q.set_defaults(func=cmd_query_load)

    w = sub.add_parser("ws-fanout", help="websocket fan-out")
    w.add_argument("--api", default="http://127.0.0.1:8000")
    w.add_argument("--backend-metrics", default="http://127.0.0.1:8000/metrics")
    w.add_argument("--clients", type=int, default=200)
    w.add_argument("--topics", type=int, default=20)
    w.add_argument("--seconds", type=int, default=60)
    w.add_argument("--user", default=os.environ.get("DCIM_ADMIN_USER", "admin"))
    w.add_argument("--password", default=os.environ.get("DCIM_ADMIN_PASSWORD", ""))
    w.set_defaults(func=cmd_ws_fanout)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
