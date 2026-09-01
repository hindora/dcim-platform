"""Seed the inventory from the simulator's topology export.

Physical placement is NOT discoverable over any protocol - nothing tells you a
device sits in Row 2, Rack 1, U18 - so inventory is seeded from the export and
protocol discovery is used afterwards only for reconciliation and drift.

The import is idempotent on ``device.external_id`` (the simulator's 8-character
device id), so it can be re-run on every fleet change. Devices that vanish from
the export are marked decommissioned, never deleted: their history still
references them.

Deliberately NOT imported: the live telemetry fields on each device record
(cpu_usage, inlet_temp, ...). They are a snapshot. Writing them into
device_state would create values that never age and never get corrected, which
is worse than an empty dashboard.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import encrypt_secret
from app.importer.endpoints import EndpointSpec, derive_endpoints
from app.importer.redundancy import recompute_power_sides

log = get_logger("importer")

# Simulator layer -> how a link of that layer terminates. Power cords land on an
# OUTLET and a PSU, not on interfaces; cooling and fieldbus relations have no
# ports at all. This is why one polymorphic connection table exists.
LAYER_TERMINATIONS = {
    "production": ("interface", "interface"),
    "management": ("interface", "interface"),
    "power": ("outlet", "psu"),
    "cooling": ("none", "none"),
    "fieldbus": ("none", "none"),
}


@dataclass
class ImportReport:
    datacenters: int = 0
    rooms: int = 0
    rows: int = 0
    racks: int = 0
    vendors: int = 0
    models: int = 0
    devices: int = 0
    interfaces: int = 0
    outlets: int = 0
    psus: int = 0
    connections: int = 0
    endpoints: int = 0
    retired_endpoints: int = 0
    connections_replaced: int = 0
    sites_sized: int = 0
    credentials: int = 0
    decommissioned: int = 0
    # Outcome of the post-import A/B derivation: how many device pairs got a
    # side, and how many devices are shared between both paths.
    redundancy: dict[str, Any] = field(default_factory=dict)
    skipped_device_types: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


async def fetch_topology(base_url: str, username: str, password: str,
                         timeout: float = 60.0) -> dict:
    """Log in to the simulator API and pull the full topology export."""
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        # The simulator mounts its auth router under /api + /auth.
        r = await client.post("/api/auth/login",
                              json={"username": username, "password": password})
        r.raise_for_status()
        token = r.json()["token"]
        r = await client.get("/api/topology/export",
                             headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()


class TopologyImporter:
    def __init__(self, session: AsyncSession, *,
                 include_protocols: frozenset[str] = frozenset({"snmp"}),
                 gnmi_gateway: str | None = None,
                 gnmi_port: int = 50051,
                 collector_id: str | None = None) -> None:
        self.s = session
        self.include_protocols = include_protocols
        self.gnmi_gateway = gnmi_gateway
        self.gnmi_port = gnmi_port
        self.collector_id = collector_id
        self.report = ImportReport()

        self._dc: dict[str, str] = {}          # code -> id
        self._room: dict[tuple[str, str], str] = {}
        self._row: dict[tuple[str, str], str] = {}
        self._rack: dict[tuple[str, str], str] = {}
        self._vendor: dict[str, str] = {}
        self._model: dict[tuple[str, str], str] = {}
        self._device: dict[str, str] = {}      # external_id -> id
        self._device_name: dict[str, str] = {}         # device_id -> name
        self._iface: dict[tuple[str, int], str] = {}   # (device_id, if_index) -> id
        # Edges address a port by its POSITION in the device's interface
        # list, which is not its ifIndex: the simulator numbers ifIndex
        # from 1, and src_iface/dst_iface count from 0. Keying the
        # lookup by ifIndex made every edge resolve one port to the left,
        # and since a miss drops BOTH ends to 'none' the ports vanished
        # entirely - 2792 of 2879 production links landed with no port,
        # leaving the table able to say LF1 connects to SRV05 but not on
        # which cable. Both maps are kept: interface rows are still
        # written with the real ifIndex, which is what SNMP reports.
        self._iface_at: dict[tuple[str, int], str] = {}  # (device_id, pos) -> id
        self._outlet: dict[tuple[str, int], str] = {}
        self._psu: dict[tuple[str, int], str] = {}
        self._profiles: dict[str, str] = {}
        self._credentials: dict[str, str] = {}
        self._endpoint_by_addr: dict[tuple[str, str], str] = {}  # (protocol, addr) -> id
        self._known_types: set[str] = set()
        # {(dc_code, room_name): floor-plan record}. The simulator classifies
        # its own rooms; see _apply_floorplan.
        self._floorplan: dict[tuple[str, str], dict] = {}

    # ------------------------------------------------------------------ run

    async def run(self, topology: dict) -> ImportReport:
        await self._load_lookups()
        self._read_floorplan(topology)

        nodes = topology.get("nodes") or []
        edges = topology.get("edges") or []
        log.info("import starting", nodes=len(nodes), edges=len(edges))

        devices = [n["device"] for n in nodes if n.get("device")]

        for dev in devices:
            await self._ensure_placement(dev)
        for node in nodes:
            dev = node.get("device")
            if dev:
                await self._upsert_device(dev, node.get("position") or {})

        await self._decommission_missing({d.get("id") for d in devices if d.get("id")})

        for dev in devices:
            await self._upsert_terminations(dev)

        await self._clear_simulator_connections()
        for edge in edges:
            await self._upsert_connection(edge)

        for dev in devices:
            await self._upsert_endpoints(dev)
        await self._resolve_via_links(devices)

        # After the edges, never during: a conductor's side depends on where
        # the whole tree diverges, which is not knowable one edge at a time.
        self.report.redundancy = await recompute_power_sides(self.s)

        # After redundancy, because the design figure depends on how many buses
        # the site actually has.
        self.report.sites_sized = await self._seed_design_capacity()

        log.info("import complete", **self.report.as_dict())
        return self.report

    def _read_floorplan(self, topology: dict) -> None:
        """Index the floor plan the simulator ships with its topology.

        Keys arrive as "DC1/Server Hall A". Anything that does not split into a
        datacentre and a room is skipped rather than guessed at - a
        mis-attributed room would silently move racks between sites.
        """
        rooms = ((topology.get("floorplan") or {}).get("rooms") or {})
        for key, rec in rooms.items():
            dc = rec.get("datacenter")
            room = rec.get("room")
            if not (dc and room) and "/" in str(key):
                dc, room = str(key).split("/", 1)
            if dc and room:
                self._floorplan[(dc, room)] = rec
        if rooms:
            log.info("floor plan read", rooms=len(self._floorplan))

    # UPS sizing target from the simulator's own selector (core/power_sizing.py):
    # a UPS SKU is chosen so the load it carries sits at ~80 % of nameplate.
    # Inverting it recovers the design IT load the site was built around.
    _UPS_TARGET_UTILISATION = 0.80

    async def _seed_design_capacity(self) -> int:
        """Derive each site's design IT load from its installed UPS.

        Nobody types a design figure into this system, and inventing one would
        make every capacity percentage on the home page meaningless. What the
        inventory does know is the UPS that was installed, and the estate was
        built to a rule: each 2N bus carries the whole IT load, and a UPS is
        selected so that load is ~80 % of its nameplate.

        So: total UPS nameplate, divided by the number of distinct redundancy
        sides the site's UPS feed (2 for a 2N pair, 1 if unsided), times the
        selector's utilisation target. On a 2N site that is one bus's worth,
        which is the load the facility is designed to carry.

        The derivation is written into `attributes.design_it_kw_basis` alongside
        the number, because a capacity percentage whose denominator cannot be
        explained is a percentage nobody should act on. A site with no UPS in
        inventory is left NULL - "unknown" is a usable answer here, a guess is
        not.
        """
        rows = (await self.s.execute(text("""
            WITH ups AS (
                SELECT dc.id AS dc_id,
                       d.id  AS device_id,
                       m.rated_power_w AS rated_w
                FROM device d
                JOIN model m       ON m.id = d.model_id
                LEFT JOIN rack r   ON r.id = d.rack_id
                LEFT JOIN rack_row rr ON rr.id = r.row_id
                LEFT JOIN room rm  ON rm.id = COALESCE(rr.room_id, d.room_id)
                JOIN datacenter dc ON dc.id = rm.datacenter_id
                WHERE d.device_type = 'ups'
                  AND d.lifecycle <> 'decommissioned'
                  AND m.rated_power_w IS NOT NULL
                  AND m.rated_power_w > 0
            ),
            sides AS (
                SELECT u.dc_id,
                       count(DISTINCT c.redundancy_side) AS n
                FROM ups u
                JOIN connection c ON c.a_device_id = u.device_id
                WHERE c.layer = 'power' AND c.redundancy_side IN ('A', 'B')
                GROUP BY u.dc_id
            )
            SELECT u.dc_id::text AS dc_id,
                   sum(u.rated_w) / 1000.0 AS ups_kw,
                   count(*)                AS ups_count,
                   COALESCE(max(s.n), 1)   AS buses
            FROM ups u
            LEFT JOIN sides s ON s.dc_id = u.dc_id
            GROUP BY u.dc_id
        """))).mappings().all()

        seeded = 0
        for r in rows:
            buses = max(1, int(r["buses"] or 1))
            ups_kw = float(r["ups_kw"] or 0.0)
            if ups_kw <= 0:
                continue
            design_kw = round(ups_kw / buses * self._UPS_TARGET_UTILISATION, 2)
            basis = (f"{int(r['ups_count'])} UPS totalling {ups_kw:.0f} kW "
                     f"across {buses} bus(es), at the "
                     f"{self._UPS_TARGET_UTILISATION:.0%} selector target")
            await self.s.execute(text("""
                UPDATE datacenter
                   SET design_it_kw = :kw,
                       attributes = attributes || CAST(:attrs AS jsonb)
                 WHERE id = CAST(:dc AS uuid)
            """), {"kw": design_kw, "dc": r["dc_id"],
                   "attrs": json.dumps({"design_it_kw_basis": basis,
                                        "design_it_kw_source": "derived"})})
            seeded += 1
        return seeded

    async def _load_lookups(self) -> None:
        rows = (await self.s.execute(text("SELECT name, id::text FROM poll_profile"))).all()
        self._profiles = dict(rows)
        rows = (await self.s.execute(text("SELECT code FROM device_type"))).all()
        self._known_types = {r[0] for r in rows}
        rows = (await self.s.execute(text("SELECT name, id::text FROM credential"))).all()
        self._credentials = dict(rows)

    # ---------------------------------------------------------- placement

    async def _ensure_placement(self, dev: dict) -> None:
        dc_code = dev.get("datacenter")
        if not dc_code:
            return
        if dc_code not in self._dc:
            self._dc[dc_code] = await self._scalar("""
                INSERT INTO datacenter (code, name, city, country)
                VALUES (:code, :name, :city, :country)
                ON CONFLICT (code) DO UPDATE
                    SET city = COALESCE(EXCLUDED.city, datacenter.city),
                        country = COALESCE(EXCLUDED.country, datacenter.country)
                RETURNING id::text
            """, code=dc_code, name=dc_code,
                city=dev.get("datacenter_city"), country=dev.get("country"))
            self.report.datacenters += 1

        room_name = dev.get("room")
        if not room_name:
            return
        rkey = (dc_code, room_name)
        if rkey not in self._room:
            fp = self._floorplan.get(rkey) or {}
            # The simulator says which rooms are white space and which are
            # facility, and it knows because it drew them. Falling back to the
            # name only when the floor plan has nothing to say about the room.
            room_class = fp.get("class") or _room_class_from_name(room_name)
            room_type = _room_type(room_name)
            rows = fp.get("rows") or []
            per_row = fp.get("racks_per_row") or 0
            designed = (len(rows) * int(per_row)) or None
            self._room[rkey] = await self._scalar("""
                INSERT INTO room (datacenter_id, name, floor, room_type,
                                  room_class, width_m, depth_m, designed_racks,
                                  attributes)
                VALUES (CAST(:dc AS uuid), :name, :floor, :rt, :rc, :w, :d, :dr,
                        CAST(:attrs AS jsonb))
                ON CONFLICT (datacenter_id, name) DO UPDATE
                    SET floor = EXCLUDED.floor,
                        room_type = EXCLUDED.room_type,
                        -- COALESCE keeps a previously imported classification
                        -- when this run has no floor plan to offer, rather than
                        -- blanking a good value with a missing one.
                        room_class = COALESCE(EXCLUDED.room_class, room.room_class),
                        width_m = COALESCE(EXCLUDED.width_m, room.width_m),
                        depth_m = COALESCE(EXCLUDED.depth_m, room.depth_m),
                        designed_racks = COALESCE(EXCLUDED.designed_racks,
                                                  room.designed_racks),
                        attributes = room.attributes || EXCLUDED.attributes
                RETURNING id::text
            """, dc=self._dc[dc_code], name=room_name,
                floor=str(dev.get("floor") or ""), rt=room_type, rc=room_class,
                w=fp.get("width_m"), d=fp.get("depth_m"), dr=designed,
                attrs=_json({"containment": fp.get("containment"),
                             "designed_rows": len(rows) or None,
                             "racks_per_row": per_row or None,
                             "class_source": "simulator floor plan" if fp
                                             else "inferred from room name"}))
            self.report.rooms += 1

        # Floor-standing plant has a synthetic room-grid coordinate rather than
        # a real rack position, so it gets no row/rack.
        if not _is_rack_mounted(dev):
            return

        row_num = dev.get("rack_row")
        if row_num is None:
            return
        row_name = f"R{row_num}"
        rowkey = (self._room[rkey], row_name)
        if rowkey not in self._row:
            self._row[rowkey] = await self._scalar("""
                INSERT INTO rack_row (room_id, name, ordinal, cold_aisle, hot_aisle)
                VALUES (CAST(:room AS uuid), :name, :ordinal, :ca, :ha)
                ON CONFLICT (room_id, name) DO UPDATE
                    SET cold_aisle = COALESCE(EXCLUDED.cold_aisle, rack_row.cold_aisle),
                        hot_aisle  = COALESCE(EXCLUDED.hot_aisle,  rack_row.hot_aisle)
                RETURNING id::text
            """, room=self._room[rkey], name=row_name, ordinal=int(row_num),
                ca=dev.get("cold_aisle"), ha=dev.get("hot_aisle"))
            self.report.rows += 1

        rack_num = dev.get("rack_num")
        if rack_num is None:
            return
        rack_name = f"{row_name}-{int(rack_num):02d}"
        rackkey = (self._row[rowkey], rack_name)
        if rackkey not in self._rack:
            self._rack[rackkey] = await self._scalar("""
                INSERT INTO rack (row_id, name, ordinal, u_height, facing, floor_x, floor_y)
                VALUES (CAST(:row AS uuid), :name, :ordinal, 42, :facing, :fx, :fy)
                ON CONFLICT (row_id, name) DO UPDATE
                    SET floor_x = COALESCE(EXCLUDED.floor_x, rack.floor_x),
                        floor_y = COALESCE(EXCLUDED.floor_y, rack.floor_y)
                RETURNING id::text
            """, row=self._row[rowkey], name=rack_name, ordinal=int(rack_num),
                facing=dev.get("rack_facing"),
                fx=dev.get("floor_x"), fy=dev.get("floor_y"))
            self.report.racks += 1

    # ------------------------------------------------------------- device

    async def _upsert_device(self, dev: dict, position: dict) -> None:
        ext = dev.get("id")
        if not ext:
            return
        dtype = dev.get("device_type") or "server"
        if dtype not in self._known_types:
            self.report.skipped_device_types[dtype] = \
                self.report.skipped_device_types.get(dtype, 0) + 1
            self.report.warnings.append(f"unknown device_type '{dtype}' for {dev.get('name')}")
            return

        vendor_id = await self._vendor_id(dev.get("vendor"))
        # Nameplate first, live draw only as a fallback.
        #
        # `power_draw_w` is what this ONE device happened to be drawing when the
        # export was taken, and writing it onto the MODEL made every unit of that
        # SKU inherit one machine's instantaneous load as its rating. The export
        # now carries `rated_power_w` resolved from the simulator's SKU catalog,
        # which is the actual nameplate. The fallback is kept because the catalog
        # only rates distribution and backup gear - it returns 0 for a server -
        # and dropping to NULL there would lose ratings the platform already has.
        model_id = await self._model_id(vendor_id, dev.get("model_name"), dtype,
                                        dev.get("rated_power_w")
                                        or dev.get("power_draw_w"))

        dc_code = dev.get("datacenter")
        room_id = self._room.get((dc_code, dev.get("room"))) if dc_code else None
        rack_id = None
        u_start = None
        if _is_rack_mounted(dev) and dev.get("rack_row") is not None \
                and dev.get("rack_num") is not None:
            row_id = self._row.get((room_id, f"R{dev['rack_row']}"))
            if row_id:
                rack_id = self._rack.get((row_id, f"R{dev['rack_row']}-{int(dev['rack_num']):02d}"))
            # rack_unit 0 means the device is IN the rack but occupies no rack
            # unit. That is the normal case for a vertically mounted rack PDU
            # and for a probe strapped to a rail - both are genuinely zero-U.
            # Storing it as U0 would make every such device collide with its
            # neighbours under the rack-unit exclusion constraint.
            raw_u = dev.get("rack_unit")
            u_start = raw_u if isinstance(raw_u, int) and raw_u > 0 else None

        device_id = await self._scalar("""
            INSERT INTO device (external_id, name, device_type, model_id, vendor_id,
                                room_id, rack_id, u_start, u_height, facing,
                                floor_x, floor_y, primary_ip, mgmt_ip, attributes)
            VALUES (:ext, :name, :dtype, CAST(:model AS uuid), CAST(:vendor AS uuid),
                    CAST(:room AS uuid), CAST(:rack AS uuid), :u_start, :u_height, :facing,
                    :fx, :fy, CAST(:pip AS inet), CAST(:mip AS inet), CAST(:attrs AS jsonb))
            ON CONFLICT (external_id) DO UPDATE SET
                name = EXCLUDED.name,
                device_type = EXCLUDED.device_type,
                model_id = EXCLUDED.model_id,
                vendor_id = EXCLUDED.vendor_id,
                room_id = EXCLUDED.room_id,
                rack_id = EXCLUDED.rack_id,
                u_start = EXCLUDED.u_start,
                u_height = EXCLUDED.u_height,
                facing = EXCLUDED.facing,
                floor_x = EXCLUDED.floor_x,
                floor_y = EXCLUDED.floor_y,
                primary_ip = EXCLUDED.primary_ip,
                mgmt_ip = EXCLUDED.mgmt_ip,
                attributes = device.attributes || EXCLUDED.attributes,
                lifecycle = 'in_service',
                decommissioned_at = NULL,
                updated_at = now()
            RETURNING id::text
        """, ext=ext, name=dev.get("name") or ext, dtype=dtype,
            model=model_id, vendor=vendor_id, room=room_id, rack=rack_id,
            # NOT rack_facing. That is the rack's orientation in the hall
            # ('N' faces lower y, 'S' faces higher y) and says nothing about
            # which side of the rack a device is mounted on. Copying it here
            # made every elevation slot claim a mount side of "N", which a
            # front/rear rack view would read as a real answer. The source
            # models no per-device mount side, so the honest value is nothing.
            u_start=u_start, u_height=_u_height(dev), facing=None,
            # floor_x/floor_y are metres within the room - the source documents
            # floor_x as "rack centre x within the room (m)". `position` is
            # something else entirely: pixel coordinates in the simulator's
            # fleet-wide canvas diagram, spanning 0-8920 across every room and
            # both datacenters. Preferring it filled a metre-typed column with
            # pixels, so floor-standing plant landed kilometres outside its own
            # room and a floor plan drawn from it was nonsense. Rack-mounted
            # gear is placed by its rack anyway; plant simply has no room
            # coordinate in the source, and null says that honestly.
            fx=dev.get("floor_x"),
            fy=dev.get("floor_y"),
            pip=dev.get("ip_address") or None, mip=dev.get("mgmt_ip") or None,
            attrs=_json({"mgmt_vlan": dev.get("mgmt_vlan"),
                         "power_draw_w": dev.get("power_draw_w"),
                         # The nameplate. Without it a PDU's draw is a number
                         # with nothing to be a fraction OF, and load_pct - the
                         # metric its overload rule is written against - cannot
                         # be derived at all.
                         "rated_power_w": dev.get("rated_power_w") or None,
                         "modbus_role": dev.get("modbus_role") or None,
                         "source": "simulator"}))
        self._device[ext] = device_id
        self._device_name[device_id] = dev.get("name") or ext
        self.report.devices += 1

    async def _decommission_missing(self, present: set[str]) -> None:
        """Anything previously imported but absent now is decommissioned."""
        result = await self.s.execute(text("""
            UPDATE device SET lifecycle = 'decommissioned',
                              decommissioned_at = now(), updated_at = now()
            WHERE external_id IS NOT NULL
              AND lifecycle <> 'decommissioned'
              AND attributes->>'source' = 'simulator'
              AND external_id <> ALL(:present)
            RETURNING id
        """), {"present": list(present)})
        self.report.decommissioned = len(result.all())

    # -------------------------------------------------------- terminations

    async def _upsert_terminations(self, dev: dict) -> None:
        device_id = self._device.get(dev.get("id") or "")
        if not device_id:
            return

        for idx, iface in enumerate(dev.get("interfaces") or []):
            if_index = iface.get("index", idx)
            name = iface.get("name") or f"if{if_index}"
            iface_id = await self._scalar("""
                INSERT INTO interface (device_id, if_index, name, role, speed_bps, mac, ip)
                VALUES (CAST(:dev AS uuid), :idx, :name, :role, :speed,
                        CAST(:mac AS macaddr), CAST(:ip AS inet))
                ON CONFLICT (device_id, name) DO UPDATE SET
                    if_index = EXCLUDED.if_index, role = EXCLUDED.role,
                    speed_bps = EXCLUDED.speed_bps
                RETURNING id::text
            """, dev=device_id, idx=if_index, name=name,
                role=iface.get("role") or "data",
                speed=_speed_bps(iface), mac=iface.get("mac") or None,
                ip=iface.get("ip") or None)
            self._iface[(device_id, if_index)] = iface_id
            self._iface_at[(device_id, idx)] = iface_id
            self.report.interfaces += 1

        # The export names these the way the device plane does - `index`, `type`,
        # `rated_a`, `bank` - not the way this schema does. Reading only the
        # schema's own spelling meant `number` was always None, every outlet hit
        # the skip below, and with the outlet map empty EVERY power cord then fell
        # through _upsert_connection's "no termination" path. 1080 power links
        # imported as bare device-to-device edges and the outlet and power_supply
        # tables stayed at zero rows - a whole layer of the model switched off by
        # one field name, and silently, because dropping the termination is also
        # what a legitimately portless link does.
        for outlet in dev.get("outlets") or []:
            number = outlet.get("number", outlet.get("index"))
            if number is None:
                continue
            oid = await self._scalar("""
                INSERT INTO outlet (device_id, number, connector, rated_amps, phase, branch)
                VALUES (CAST(:dev AS uuid), :num, :conn, :amps, :phase, :branch)
                ON CONFLICT (device_id, number) DO UPDATE SET
                    connector = EXCLUDED.connector, phase = EXCLUDED.phase,
                    branch = EXCLUDED.branch
                RETURNING id::text
            """, dev=device_id, num=int(number),
                conn=outlet.get("connector") or outlet.get("type") or "C13",
                amps=outlet.get("rated_amps", outlet.get("rated_a")),
                phase=outlet.get("phase"),
                # The bank IS the branch breaker: outlets on one bank share an
                # overcurrent device, so a trip takes all of them together.
                branch=(str(outlet["bank"]) if outlet.get("bank") is not None
                        else outlet.get("branch")))
            self._outlet[(device_id, int(number))] = oid
            self.report.outlets += 1

        for psu in dev.get("psus") or []:
            number = psu.get("number", psu.get("index"))
            if number is None:
                continue
            pid = await self._scalar("""
                INSERT INTO power_supply (device_id, number, connector, rated_watts)
                VALUES (CAST(:dev AS uuid), :num, :conn, :watts)
                ON CONFLICT (device_id, number) DO UPDATE SET connector = EXCLUDED.connector
                RETURNING id::text
            """, dev=device_id, num=int(number),
                conn=psu.get("connector") or psu.get("inlet") or "C14",
                watts=psu.get("rated_watts", psu.get("capacity_w")))
            self._psu[(device_id, int(number))] = pid
            self.report.psus += 1

    # --------------------------------------------------------- connections

    async def _clear_simulator_connections(self) -> None:
        """The export is the authority on cabling, so replace the set.

        Upserting edges one at a time cannot converge. A link imported once
        without ports and again with them is, to any key that includes the
        terminations, two different rows - so the corrected import laid a
        ported row beside every portless one instead of replacing it, and
        production went from 436 rows to 833 for 436 cables. Widening the key
        to ignore terminations is not the answer either: it would make two
        genuinely different cables between the same pair of devices - a LAG,
        an A/B pair - collapse into one.

        A topology export describes the whole plant, and this importer already
        treats it that way for devices, decommissioning anything absent. The
        cabling gets the same treatment: what the simulator no longer describes
        is no longer there.

        Scoped to links whose BOTH ends are simulator-sourced, so a connection
        recorded by hand between real equipment survives an import that knows
        nothing about it.
        """
        result = await self.s.execute(text("""
            DELETE FROM connection c
             USING device a, device b
             WHERE a.id = c.a_device_id AND b.id = c.b_device_id
               AND a.attributes->>'source' = 'simulator'
               AND b.attributes->>'source' = 'simulator'
         RETURNING c.id
        """))
        self.report.connections_replaced = len(result.all())

    async def _upsert_connection(self, edge: dict) -> None:
        # The export already resolves direction from src_node/dst_node; an
        # undirected edges() walk would otherwise hand each end the FAR side's
        # port. Use src/dst exactly as given and do not re-derive.
        a_ext, b_ext = edge.get("src"), edge.get("dst")
        a_id, b_id = self._device.get(a_ext or ""), self._device.get(b_ext or "")
        if not a_id or not b_id:
            return

        layer = edge.get("layer") or "production"
        a_type, b_type = LAYER_TERMINATIONS.get(layer, ("none", "none"))
        a_term = b_term = None

        if layer in ("production", "management"):
            # src_iface/dst_iface are deliberately NULL on power and cooling
            # edges; only port-bearing layers index into the interface map.
            a_term = self._iface_at.get((a_id, edge.get("src_iface")))
            b_term = self._iface_at.get((b_id, edge.get("dst_iface")))
            if a_term is None or b_term is None:
                a_type = b_type = "none"
                a_term = b_term = None
        elif layer == "power":
            outlet_no, psu_no = edge.get("outlet"), edge.get("psu")
            a_term = self._outlet.get((a_id, outlet_no)) if outlet_no is not None else None
            b_term = self._psu.get((b_id, psu_no)) if psu_no is not None else None
            if a_term is None or b_term is None:
                a_type = b_type = "none"
                a_term = b_term = None

        try:
            async with self.s.begin_nested():
                await self._insert_connection(layer, a_id, a_type, a_term,
                                              b_id, b_type, b_term, edge)
        except IntegrityError:
            # A port carries one cable. The uniqueness that says so is older
            # than this importer and correct; what is new is that ports now
            # resolve at all, so a topology that double-books one finally
            # collides instead of passing unnoticed as two portless rows.
            #
            # Keep the link and drop the ports rather than dropping the link:
            # that a cable exists is the more certain fact, and a report the
            # operator reads beats a row silently missing from the model.
            a_name = self._device_name.get(a_id, a_id)
            b_name = self._device_name.get(b_id, b_id)
            self.report.warnings.append(
                f"{layer} link {a_name}:{edge.get('src_iface')} -> "
                f"{b_name}:{edge.get('dst_iface')} claims a port that already "
                f"carries another cable; imported without terminations")
            async with self.s.begin_nested():
                await self._insert_connection(layer, a_id, "none", None,
                                              b_id, "none", None, edge)
        self.report.connections += 1

    async def _insert_connection(self, layer: str, a_id: str, a_type: str,
                                 a_term: str | None, b_id: str, b_type: str,
                                 b_term: str | None, edge: dict) -> None:
        await self.s.execute(text("""
            INSERT INTO connection (layer, link_type,
                                    a_device_id, a_termination_type, a_termination_id,
                                    b_device_id, b_termination_type, b_termination_id,
                                    redundancy_side, oper_state)
            VALUES (CAST(:layer AS layer_t), :link_type,
                    CAST(:a AS uuid), CAST(:at AS termination_t), CAST(:aid AS uuid),
                    CAST(:b AS uuid), CAST(:bt AS termination_t), CAST(:bid AS uuid),
                    :side, :oper)
            ON CONFLICT (layer, a_device_id, a_termination_type, a_termination_id,
                                b_device_id, b_termination_type, b_termination_id)
            DO UPDATE SET oper_state = EXCLUDED.oper_state
        """), {"layer": layer, "link_type": _link_type(layer),
               "a": a_id, "at": a_type, "aid": a_term,
               "b": b_id, "bt": b_type, "bid": b_term,
               # Filled in by recompute_power_sides once every edge exists.
               "side": None,
               "oper": "down" if edge.get("broken") else "unknown"})

    # ----------------------------------------------------------- endpoints

    async def _upsert_endpoints(self, dev: dict) -> None:
        device_id = self._device.get(dev.get("id") or "")
        if not device_id:
            return
        specs = derive_endpoints(dev, include_protocols=self.include_protocols,
                                 gnmi_gateway=self.gnmi_gateway,
                                 gnmi_port=self.gnmi_port)
        keep: list[str] = []
        for spec in specs:
            profile_id = self._profiles.get(spec.poll_profile)
            if profile_id is None:
                self.report.warnings.append(f"missing poll profile {spec.poll_profile}")
                continue
            cred_id = await self._credential_id(spec)
            endpoint_id = await self._scalar("""
                INSERT INTO device_endpoint (device_id, protocol, role, address, port,
                                             addressing, credential_id, poll_profile_id,
                                             collector_id, enabled)
                VALUES (CAST(:dev AS uuid), CAST(:proto AS protocol_t),
                        CAST(:role AS endpoint_role_t), CAST(:addr AS inet), :port,
                        CAST(:addressing AS jsonb), CAST(:cred AS uuid),
                        CAST(:profile AS uuid), :collector, :enabled)
                ON CONFLICT (device_id, protocol, role, coalesce(host(address), ''))
                DO UPDATE SET
                    port = EXCLUDED.port,
                    addressing = EXCLUDED.addressing,
                    credential_id = EXCLUDED.credential_id,
                    poll_profile_id = EXCLUDED.poll_profile_id,
                    collector_id = EXCLUDED.collector_id,
                    enabled = EXCLUDED.enabled,
                    updated_at = now()
                RETURNING id::text
            """, dev=device_id, proto=spec.protocol, role=spec.role,
                addr=spec.address, port=spec.port,
                addressing=_json(spec.addressing), cred=cred_id, profile=profile_id,
                collector=self.collector_id, enabled=spec.enabled)
            if spec.address:
                self._endpoint_by_addr.setdefault((spec.protocol, spec.address), endpoint_id)
            self.report.endpoints += 1
            keep.append(endpoint_id)

        await self._retire_undesired_endpoints(device_id, keep)

    async def _retire_undesired_endpoints(self, device_id: str,
                                          keep: list[str]) -> None:
        """Disable endpoints this device no longer implies.

        Without this the importer only ever adds. Narrowing which device types
        speak a protocol, or a device changing type, leaves the old endpoints
        enabled and polled forever - which is how 52 firewalls and console
        switches ended up holding gNMI sessions against servers that were never
        listening.

        Disabled, never deleted: poll results and alarms reference the row, and
        an endpoint that comes back should come back with its history.
        """
        if not self.include_protocols:
            return
        result = await self.s.execute(text("""
            UPDATE device_endpoint
               SET enabled = false, updated_at = now()
             WHERE device_id = CAST(:dev AS uuid)
               AND protocol = ANY(CAST(:protos AS protocol_t[]))
               AND enabled
               AND NOT (id = ANY(CAST(:keep AS uuid[])))
        """), {"dev": device_id, "protos": list(self.include_protocols),
               "keep": keep})
        if result.rowcount:
            self.report.retired_endpoints += result.rowcount

        # And stop the state row claiming a fault.
        #
        # A retired endpoint is never polled again, so whatever it last said
        # freezes: 52 firewalls and console switches sat at OFFLINE for eight
        # days after their gNMI endpoints were retired, which reads on the
        # device page as a fault nobody is fixing and buries the OFFLINE rows
        # that are real.
        #
        # DISABLED, not deleted, for the same reason the endpoint itself is
        # kept: last_success and the poll totals are the record of what this
        # endpoint did while it was in service, and an endpoint that comes back
        # should come back with its history.
        await self.s.execute(text("""
            UPDATE endpoint_state es
               SET status = 'DISABLED',
                   last_error = NULL,
                   last_error_class = NULL,
                   consecutive_failures = 0,
                   updated_at = now()
              FROM device_endpoint e
             WHERE e.id = es.endpoint_id
               AND e.device_id = CAST(:dev AS uuid)
               AND NOT e.enabled
               AND es.status <> 'DISABLED'
        """), {"dev": device_id})

    async def _resolve_via_links(self, devices: list[dict]) -> None:
        """Link field devices to the gateway/router endpoint they are reached through."""
        for dev in devices:
            device_id = self._device.get(dev.get("id") or "")
            if not device_id:
                continue
            for spec in derive_endpoints(dev, include_protocols=self.include_protocols,
                                         gnmi_gateway=self.gnmi_gateway,
                                 gnmi_port=self.gnmi_port):
                if not spec.via_address:
                    continue
                parent = self._endpoint_by_addr.get((spec.protocol, spec.via_address))
                if parent is None:
                    continue
                await self.s.execute(text("""
                    UPDATE device_endpoint SET via_endpoint_id = CAST(:parent AS uuid)
                    WHERE device_id = CAST(:dev AS uuid)
                      AND protocol = CAST(:proto AS protocol_t)
                      AND role = CAST(:role AS endpoint_role_t)
                      AND id <> CAST(:parent AS uuid)
                """), {"parent": parent, "dev": device_id,
                       "proto": spec.protocol, "role": spec.role})

    # ------------------------------------------------------------- helpers

    async def _vendor_id(self, name: str | None) -> str | None:
        if not name:
            return None
        if name in self._vendor:
            return self._vendor[name]
        vid = await self._scalar("""
            INSERT INTO vendor (name) VALUES (:name)
            ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
            RETURNING id::text
        """, name=name)
        self._vendor[name] = vid
        self.report.vendors += 1
        return vid

    async def _model_id(self, vendor_id: str | None, name: str | None,
                        dtype: str, power_w: Any) -> str | None:
        if not vendor_id or not name:
            return None
        key = (vendor_id, name)
        if key in self._model:
            return self._model[key]
        mid = await self._scalar("""
            INSERT INTO model (vendor_id, device_type, name, rated_power_w)
            VALUES (CAST(:vendor AS uuid), :dtype, :name, :power)
            ON CONFLICT (vendor_id, name) DO UPDATE SET
                device_type = EXCLUDED.device_type,
                rated_power_w = COALESCE(EXCLUDED.rated_power_w, model.rated_power_w)
            RETURNING id::text
        """, vendor=vendor_id, dtype=dtype, name=name,
            power=int(power_w) if isinstance(power_w, (int, float)) and power_w else None)
        self._model[key] = mid
        self.report.models += 1
        return mid

    async def _credential_id(self, spec: EndpointSpec) -> str | None:
        if not spec.credential_name or spec.credential_payload is None:
            return None
        if spec.credential_name in self._credentials:
            return self._credentials[spec.credential_name]
        blob = encrypt_secret(spec.credential_payload)
        cid = await self._scalar("""
            INSERT INTO credential (name, protocol, kind, secret_enc, secret_hint)
            VALUES (:name, CAST(:proto AS protocol_t), :kind, :blob, :hint)
            ON CONFLICT (name) DO UPDATE SET
                secret_enc = EXCLUDED.secret_enc, secret_hint = EXCLUDED.secret_hint
            RETURNING id::text
        """, name=spec.credential_name, proto=spec.protocol,
            kind=spec.credential_kind or "none", blob=blob, hint=spec.credential_hint)
        self._credentials[spec.credential_name] = cid
        self.report.credentials += 1
        return cid

    async def _scalar(self, sql: str, **params: Any) -> str:
        return (await self.s.execute(text(sql), params)).scalar_one()


# ------------------------------------------------------------------ helpers

def _json(obj: Any) -> str:
    import json

    return json.dumps({k: v for k, v in obj.items() if v is not None}
                      if isinstance(obj, dict) else obj)


def _room_class_from_name(name: str) -> str:
    """Last-resort classification when the floor plan carries no such room.

    White space is where IT equipment lives and where a rack position is a unit
    of capacity. Everything else - plant, switchrooms, the tower deck - is
    facility: real, metered, and counted in the site totals, but not somewhere
    anyone racks a server.
    """
    low = name.lower()
    if any(k in low for k in ("hall", "data centre", "data center", "suite")):
        return "white_space"
    if any(k in low for k in ("network", "mmr", "meet-me", "telco")):
        return "white_space"
    return "facility"


def _room_type(name: str) -> str:
    low = name.lower()
    if "plant" in low or "chiller" in low or "mechanical" in low:
        return "plant"
    if "electrical" in low or "ups" in low or "switchgear" in low:
        return "electrical"
    if "network" in low or "mmr" in low or "meet-me" in low:
        return "network"
    # Generator halls and the tower deck used to fall through to data_hall,
    # which is how a roof ended up listed as raised floor.
    if "generator" in low or "genset" in low:
        return "electrical"
    if "roof" in low or "yard" in low or "compound" in low:
        return "plant"
    return "data_hall"


# Floor-standing plant is located by room, not by rack unit. Mirrors
# core/device_manager.py::FACILITY_TYPES, with CDU excluded because in-rack
# CDUs really are rack-mounted.
_FACILITY_TYPES = frozenset({
    "generator", "utility_feed", "switchgear", "ats", "mcc", "mpp", "ups", "rpp",
    "crah", "crac", "chiller", "pump", "cooling_tower", "valve", "energy_monitor",
    "modbus_gateway", "bacnet_router",
})


def _is_rack_mounted(dev: dict) -> bool:
    return (dev.get("device_type") or "") not in _FACILITY_TYPES


def _u_height(dev: dict) -> int:
    model = (dev.get("model_name") or "").lower()
    if "2u" in model:
        return 2
    if "4u" in model or (dev.get("device_type") == "cdu"):
        return 4
    return 1


def _link_type(layer: str) -> str:
    return {"production": "ethernet", "management": "ethernet", "power": "cord",
            "cooling": "hydronic", "fieldbus": "serial"}.get(layer, "unknown")


def _speed_bps(iface: dict) -> int | None:
    raw = iface.get("speed") or iface.get("iface_type") or ""
    if isinstance(raw, (int, float)):
        return int(raw)
    text_ = str(raw).lower()
    for token, value in (("400 gbps", 400e9), ("100 gbps", 100e9), ("40 gbps", 40e9),
                         ("25 gbps", 25e9), ("10 gbps", 10e9), ("1 gbps", 1e9),
                         ("gigabit", 1e9), ("fast ethernet", 100e6)):
        if token in text_:
            return int(value)
    return None
