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

from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import text
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
        self._iface: dict[tuple[str, int], str] = {}   # (device_id, index) -> id
        self._outlet: dict[tuple[str, int], str] = {}
        self._psu: dict[tuple[str, int], str] = {}
        self._profiles: dict[str, str] = {}
        self._credentials: dict[str, str] = {}
        self._endpoint_by_addr: dict[tuple[str, str], str] = {}  # (protocol, addr) -> id
        self._known_types: set[str] = set()

    # ------------------------------------------------------------------ run

    async def run(self, topology: dict) -> ImportReport:
        await self._load_lookups()

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

        for edge in edges:
            await self._upsert_connection(edge)

        for dev in devices:
            await self._upsert_endpoints(dev)
        await self._resolve_via_links(devices)

        # After the edges, never during: a conductor's side depends on where
        # the whole tree diverges, which is not knowable one edge at a time.
        self.report.redundancy = await recompute_power_sides(self.s)

        log.info("import complete", **self.report.as_dict())
        return self.report

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
            # Plant, electrical and network rooms are not data halls; the
            # distinction drives capacity maths later.
            room_type = _room_type(room_name)
            self._room[rkey] = await self._scalar("""
                INSERT INTO room (datacenter_id, name, floor, room_type)
                VALUES (CAST(:dc AS uuid), :name, :floor, :rt)
                ON CONFLICT (datacenter_id, name) DO UPDATE SET floor = EXCLUDED.floor
                RETURNING id::text
            """, dc=self._dc[dc_code], name=room_name,
                floor=str(dev.get("floor") or ""), rt=room_type)
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
        model_id = await self._model_id(vendor_id, dev.get("model_name"), dtype,
                                        dev.get("power_draw_w"))

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
            fx=position.get("x") if position else dev.get("floor_x"),
            fy=position.get("y") if position else dev.get("floor_y"),
            pip=dev.get("ip_address") or None, mip=dev.get("mgmt_ip") or None,
            attrs=_json({"mgmt_vlan": dev.get("mgmt_vlan"),
                         "power_draw_w": dev.get("power_draw_w"),
                         "modbus_role": dev.get("modbus_role") or None,
                         "source": "simulator"}))
        self._device[ext] = device_id
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
            self.report.interfaces += 1

        for outlet in dev.get("outlets") or []:
            number = outlet.get("number")
            if number is None:
                continue
            oid = await self._scalar("""
                INSERT INTO outlet (device_id, number, connector, rated_amps, phase)
                VALUES (CAST(:dev AS uuid), :num, :conn, :amps, :phase)
                ON CONFLICT (device_id, number) DO UPDATE SET connector = EXCLUDED.connector
                RETURNING id::text
            """, dev=device_id, num=int(number),
                conn=outlet.get("connector") or "C13",
                amps=outlet.get("rated_amps"), phase=outlet.get("phase"))
            self._outlet[(device_id, int(number))] = oid
            self.report.outlets += 1

        for psu in dev.get("psus") or []:
            number = psu.get("number")
            if number is None:
                continue
            pid = await self._scalar("""
                INSERT INTO power_supply (device_id, number, connector, rated_watts)
                VALUES (CAST(:dev AS uuid), :num, :conn, :watts)
                ON CONFLICT (device_id, number) DO UPDATE SET connector = EXCLUDED.connector
                RETURNING id::text
            """, dev=device_id, num=int(number),
                conn=psu.get("connector") or "C14", watts=psu.get("rated_watts"))
            self._psu[(device_id, int(number))] = pid
            self.report.psus += 1

    # --------------------------------------------------------- connections

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
            a_term = self._iface.get((a_id, edge.get("src_iface")))
            b_term = self._iface.get((b_id, edge.get("dst_iface")))
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

        await self.s.execute(text("""
            INSERT INTO connection (layer, link_type,
                                    a_device_id, a_termination_type, a_termination_id,
                                    b_device_id, b_termination_type, b_termination_id,
                                    redundancy_side, oper_state)
            VALUES (CAST(:layer AS layer_t), :link_type,
                    CAST(:a AS uuid), CAST(:at AS termination_t), CAST(:aid AS uuid),
                    CAST(:b AS uuid), CAST(:bt AS termination_t), CAST(:bid AS uuid),
                    :side, :oper)
            ON CONFLICT DO NOTHING
        """), {"layer": layer, "link_type": _link_type(layer),
               "a": a_id, "at": a_type, "aid": a_term,
               "b": b_id, "bt": b_type, "bid": b_term,
               # Filled in by recompute_power_sides once every edge exists.
               "side": None,
               "oper": "down" if edge.get("broken") else "unknown"})
        self.report.connections += 1

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


def _room_type(name: str) -> str:
    low = name.lower()
    if "plant" in low or "chiller" in low or "mechanical" in low:
        return "plant"
    if "electrical" in low or "ups" in low or "switchgear" in low:
        return "electrical"
    if "network" in low or "mmr" in low or "meet-me" in low:
        return "network"
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
