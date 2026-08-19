# 16 — Simulator Integration Reference

Every fact below was read from the source in this repository. File references are
given so each can be re-verified — the simulator is under active development and
these will drift.

Verified against the working tree on **2026-08-18**, topology fixture
`topologies/dual_dc_enterprise.json`.

---

## 1. Endpoints at a glance

| Plane | Transport | Port | Bind | Identity of a device |
|---|---|---|---|---|
| SNMP poll | UDP | 161 (UI-settable; API default 161) | `0.0.0.0` **wildcard, one listener** | the **community string** = the device's SNMP IP |
| SNMP trap | UDP | 162 (configurable receiver) | outbound to the configured receiver | source IP + community |
| gNMI | TCP/gRPC | 57400 (insecure) | `[::]` | `prefix.target` = the device's gNMI IP |
| BACnet/IP | UDP | 47808 (`0xBAC0`) | per device IP | BACnet device instance; MS/TP via `(network, MAC)` behind a router IP |
| Redfish | TCP/HTTP (not TLS) | 8443 | per BMC IP | the BMC IP; basic/session auth |
| Modbus/TCP | TCP | 502 | per device IP | unit id (1 for native, N behind a gateway) |
| sFlow | UDP | 6343 | outbound to a collector | agent IP |
| Simulator REST API | TCP/HTTP | 8001 | dual-stack `::` with `V6ONLY=0` | — |

Sources: `simulator/snmpsim_controller.py`, `core/trap_engine.py`,
`simulator/gnmi_controller.py`, `simulator/gnmi_server.py`,
`simulator/bacnet_controller.py`, `simulator/redfish_controller.py`,
`simulator/modbus_controller.py`, `simulator/sflow_controller.py`.

> Use `127.0.0.1` rather than `localhost` for CLI probes against the simulator's
> API — the dual-stack bind makes `localhost` resolve to IPv6 first and adds a
> noticeable connect delay on some hosts.

---

## 2. Addressing model

The simulator uses three management planes (renumbered off 192.168 because it
collided with host LANs):

| Plane | Range | Carries |
|---|---|---|
| Production | `10.50.x.y` | IT data network; server OS agents |
| IT out-of-band | `10.51.x.y` | switch/router mgmt, server BMCs |
| BMS out-of-band | `10.52.x.y` | facility/BMS gear |

The third octet encodes site + room.

### 2.1 Which address a protocol answers on

From `core/snmprec_generator.py`:

```python
@staticmethod
def snmp_address(device) -> str:
    if device.device_type == DeviceType.SERVER:
        return device.ip_address or device.mgmt_ip     # OS agent on the production NIC
    return device.mgmt_ip if device.mgmt_ip else device.ip_address

@staticmethod
def bmc_address(device) -> str:
    if device.device_type == DeviceType.SERVER and device.mgmt_ip:
        return device.mgmt_ip                          # BMC agent, separate MIB subtree
    return ""
```

So a server has **two** SNMP agents:

```
SRV01-DC1-HA-R2-01
  ip_address 10.50.11.19   → OS SNMP agent   community "10.50.11.19"
  mgmt_ip    10.51.11.25   → BMC SNMP agent  community "10.51.11.25"
                           → Redfish         https://10.51.11.25:8443
```

Everything else answers on `mgmt_ip`.

### 2.2 The community string is the IP

`core/device_manager.py`:

```python
if self.snmp_community == "public":
    self.snmp_community = self.ip_address
```

and `core/snmprec_generator.py` states the operational consequence directly:

> Output layout: `datasets/snmp/<snmp_addr>.snmprec`
> SNMPSim routes community `"<snmp_addr>"` → this file.
> **Configure your NMS/DCIM to poll the mgmt_ip with community=mgmt_ip.**

Because snmpsim runs **one wildcard listener** on `0.0.0.0:161` and routes purely
by community, the destination IP barely matters and the community is everything.
A wrong community produces **no response at all**, which is indistinguishable
from a dead device. If a whole class of devices appears offline, check the
community before anything else.

### 2.3 Devices with no SNMP

`_NO_SNMP_TYPES` in `core/snmprec_generator.py`:

```
rpp, chiller, pump, cooling_tower, valve
```

This is realistic — real chillers and pumps carry no SNMP card, and an RPP is a
passive breaker panel. Do not create SNMP endpoints for them and do not alarm on
their absence. CRAH and CDU **do** stay SNMP-capable because real units ship
native comm cards.

### 2.4 Devices with no IP at all

| Class | Field evidence (`core/device_manager.py`) | Addressed as |
|---|---|---|
| Modbus RTU slave (chilled-water transmitters, DP cells) | `modbus_role="rtu_slave"`, `modbus_unit_id`, `modbus_gateway_ip` | `(gateway IP, unit id)` |
| BACnet MS/TP device (Belimo valves, Grundfos pump cards) | `mstp_net`, `mstp_mac`, `mstp_router_ip` | `(router IP, network, MAC)` |
| Sensor-port probe (Raritan DPX2) | plugs into a PX2 RJ-12 SENSOR port | read through the PDU's SNMP agent |

The simulator's own comment on `modbus_role` is worth quoting because it is the
design rationale for finding A2:

> `rtu_slave` — a field transmitter ON that trunk. It has NO IP of its own — that
> is the entire point. A chilled-water thermowell is two wires into a
> transmitter, not a network node, and the gateway is what the BMS and the NMS
> both actually talk to.

---

## 3. The topology export — the inventory seed

```
POST /api/auth/login  {"username":"admin","password":"admin1234"}
     → {"token": "...", "expires_in": ..., "username": ..., "role": ...}
GET  /api/topology/export      Authorization: Bearer <token>
```

Shape (`core/topology_engine.to_dict`):

```json
{
  "metadata": { "has_management_layer": true, "has_power_layer": true,
                "has_floorplan": true },
  "nodes": [ { "id": "fa03fbfd",
               "position": {"x": 120, "y": 340},
               "device": { ...full device dict... } } ],
  "edges": [ { "src": "ec012c83", "dst": "b4f942f1",
               "src_iface": 0, "dst_iface": 0,
               "broken": false, "layer": "production" } ],
  "floorplan": { ... }
}
```

### 3.1 Scale and composition of the reference topology

664 nodes, 2566 edges, two datacenters.

| device_type | count | | device_type | count |
|---|---:|---|---|---:|
| server | 310 | | firewall | 12 |
| pdu | 80 | | cdu | 12 |
| switch | 38 | | router | 8 |
| oob_switch | 36 | | mpp | 8 |
| sensor | 32 | | chiller | 6 |
| crah | 28 | | cooling_tower | 6 |
| energy_monitor | 24 | | load_balancer | 4 |
| rpp | 16 | | generator | 4 |
| pump | 14 | | ups | 4 |
| | | | valve | 4 |
| | | | switchgear | 4 |
| | | | ats | 4 |
| | | | mcc | 4 |
| | | | utility_feed | 2 |
| | | | modbus_gateway | 2 |
| | | | bacnet_router | 2 |

Edges by layer:

| layer | count |
|---|---:|
| power | 1080 |
| management | 644 |
| production | 436 |
| cooling | 356 |
| fieldbus | 50 |

This is the fixture that proves finding A3: only the 436 `production` edges are
interface-to-interface.

### 3.2 Device fields available for import

```
name device_type vendor model_name id
ip_address mgmt_ip mgmt_vlan snmp_port snmp_community gnmi_port
interface_count interface_groups interfaces outlets psus
datacenter datacenter_city country room floor
rack_row rack_num rack_unit rack_facing cold_aisle hot_aisle floor_x floor_y
power_draw_w ups_backup
cpu_usage cpu_temp memory_total memory_used disk_total disk_used sys_uptime
inlet_temp mid_temp outlet_temp humidity dewpoint airflow
sys_contact sys_location_override metrics_enabled
```

Mapping into the DCIM schema:

| Simulator | DCIM |
|---|---|
| `id` | `device.external_id` (the idempotency key for re-import) |
| `datacenter` + `datacenter_city` + `country` | `datacenter` |
| `room` + `floor` | `room` |
| `rack_row` | `row` |
| `rack_num` | `rack` |
| `rack_unit` | `device.u_start` |
| `rack_facing` | `device.facing` |
| `floor_x` / `floor_y` | `device.floor_x` / `floor_y` and `rack.floor_x` / `floor_y` |
| `vendor` | `vendor.name` |
| `model_name` | `model.name` |
| `power_draw_w` | seeds `model.rated_power_w` where the catalog lacks it |
| `interfaces[]` | `interface` (with `role` data/mgmt) |
| `outlets[]` / `psus[]` | `outlet` / `power_supply` |
| the telemetry fields (`cpu_usage`, `inlet_temp`, …) | **ignore on import** — they are live state and must come through the collector |

That last row matters. Importing the telemetry snapshot would populate
`device_state` with values that never age and never get corrected, which is worse
than an empty dashboard.

### 3.3 Edge direction and terminations — a real trap

`core/topology_engine.to_dict` carries an explicit warning:

> `src_iface`/`dst_iface` are recorded against `src_node`/`dst_node`, and an
> undirected `edges()` walk can report `(u, v)` the other way round — emitting
> `u`/`v` here would pair each end with the far side's port.

The export already resolves this correctly (`data.get("src_node", u)`). The
importer must use the exported `src`/`dst` as given and must **not** re-derive
direction from anything else.

Power edges additionally carry termination detail:

```json
{ "src": "...", "dst": "...", "layer": "power",
  "src_iface": null, "dst_iface": null,
  "outlet": 12, "psu": 1, "supply_node": "..." }
```

`src_iface`/`dst_iface` are **null** on power and cooling edges deliberately — the
comment notes that defaulting them to `0` "would re-mint the fiction on every
save". So:

| layer | `a_termination_type` | `b_termination_type` |
|---|---|---|
| production, management | `interface` (by index) | `interface` |
| power | `outlet` (from `outlet`) | `psu` (from `psu`) |
| cooling | `none` | `none` |
| fieldbus | `none` | `none` |

---

## 4. Endpoint derivation rules for the importer

```python
def derive_endpoints(dev: dict) -> list[EndpointSpec]:
    eps = []
    dtype = dev["device_type"]

    # --- SNMP ---
    if dtype not in NO_SNMP_TYPES:
        snmp_ip = dev["ip_address"] if dtype == "server" else (dev["mgmt_ip"] or dev["ip_address"])
        if snmp_ip:
            eps.append(EndpointSpec(
                protocol="snmp",
                role="os_agent" if dtype == "server" else "native_card",
                address=snmp_ip, port=dev.get("snmp_port", 161),
                credential=SnmpV2c(community=snmp_ip)))     # community == the IP
        if dtype == "server" and dev["mgmt_ip"]:
            eps.append(EndpointSpec(
                protocol="snmp", role="bmc",
                address=dev["mgmt_ip"], port=161,
                credential=SnmpV2c(community=dev["mgmt_ip"])))

    # --- Redfish (servers only) ---
    if dtype == "server" and dev["mgmt_ip"]:
        eps.append(EndpointSpec(protocol="redfish", role="bmc",
                                address=dev["mgmt_ip"], port=8443,
                                addressing={"base": "/redfish/v1", "verify_tls": False},
                                credential=HttpBasic("admin", "password")))

    # --- gNMI (network gear) ---
    if dtype in ("switch", "router", "firewall", "load_balancer", "oob_switch"):
        tgt = dev["mgmt_ip"] or dev["ip_address"]
        eps.append(EndpointSpec(protocol="gnmi", role="native_card",
                                address=GNMI_SERVER_HOST, port=dev.get("gnmi_port", 57400),
                                addressing={"target": tgt}))

    # --- BACnet ---
    if dtype in BACNET_TYPES:                    # chiller pump cooling_tower valve crah cdu
        if dev.get("mstp_router_ip"):            # MS/TP device: no IP of its own
            eps.append(EndpointSpec(protocol="bacnet", role="field_device",
                                    address=dev["mstp_router_ip"], port=47808,
                                    addressing={"network": dev["mstp_net"],
                                                "mac": dev["mstp_mac"]},
                                    via=dev["mstp_router_ip"]))
        else:
            eps.append(EndpointSpec(protocol="bacnet", role="native_card",
                                    address=dev["mgmt_ip"] or dev["ip_address"], port=47808,
                                    addressing={"instance": bacnet_instance_of(dev)}))

    # --- Modbus ---
    role = dev.get("modbus_role")
    if role == "server":
        eps.append(EndpointSpec(protocol="modbus", role="native_card",
                                address=dev["mgmt_ip"] or dev["ip_address"], port=502,
                                addressing={"unit_id": dev.get("modbus_unit_id", 1)}))
    elif role == "gateway":
        eps.append(EndpointSpec(protocol="modbus", role="gateway",
                                address=dev["mgmt_ip"] or dev["ip_address"], port=502,
                                addressing={"unit_id": 0}))
    elif role == "rtu_slave":
        eps.append(EndpointSpec(protocol="modbus", role="field_device",
                                address=dev["modbus_gateway_ip"], port=502,
                                addressing={"unit_id": dev["modbus_unit_id"]},
                                via=dev["modbus_gateway_ip"]))
    return eps
```

**`GNMI_SERVER_HOST`** is the simulator host, not the device — the gNMI server is
a single process for all targets. Against real gear this becomes the device's own
mgmt IP, which is why it is a configuration value.

The BACnet device instance is assigned by the simulator's controller; read it
back from `GET /api/bacnet/status` or discover it with a directed Who-Is rather
than assuming a formula.

---

## 5. Simulator control endpoints useful to the DCIM

All under `/api`, bearer-authenticated. Read-only unless noted.

| Endpoint | Use |
|---|---|
| `GET /topology/export` | inventory seed and re-sync |
| `GET /topology/links?layer=` | per-layer link list with `broken` state |
| `GET /devices/{id}` | device detail |
| `GET /devices/faulted` | which devices currently have injected faults |
| `POST /snmp/trap-receiver` | **point the trap engine at the collector** |
| `POST /traps/send` | inject a trap for end-to-end tests |
| `POST /devices/{id}/fault` | inject a device fault |
| `POST /devices/{id}/override` | force a metric value |
| `POST /topology/links/break` · `/restore` | link fault injection |
| `GET /bacnet/status` · `/plant/metrics` | BACnet controller and plant state |
| `GET /modbus/map/export` | **generate the Modbus register mapping from this**, do not transcribe |
| `GET /redfish/status` · `/subscriptions` | verify subscription reconciliation |
| `GET /gnmi/status` | gNMI targets loaded |
| `POST /fleet/start` · `/advance` · `/provision-rack` · `/provision-hall` | lifecycle churn, for testing A7 |

`POST /api/snmp/trap-receiver` is the one required configuration step. Without it
the simulator sends traps to `127.0.0.1:162` (the default in
`core/trap_engine.py`) and the collector — if it is on another host or in a
container — sees nothing.

---

## 6. Traps: what arrives on the wire

`core/trap_definitions.py` defines 103 traps. Its module docstring is the
critical piece of integration knowledge:

> The OIDs here are the SIMULATOR-INTERNAL identity of each trap, not necessarily
> what goes on the wire. Real gear keys its notifications off the vendor, so
> `core.vendor_oids.trap_oid()` rewrites this OID to the vendor's own MIB OID at
> send time (an over-current leaves an APC rPDU as rPDUOverload 318.0.276 and a
> Raritan PX as overCurrentProtectorSensorStateChange 13742.6.0.65).

So the trap mapping table must be keyed on **vendor OIDs**, with the simulator's
`1.3.6.1.4.1.99999.*` tree retained as a fallback for traps that have no verified
vendor counterpart. Build the table by exporting from `core/vendor_oids.py` at
integration time rather than by hand.

Trap families present:

| Family | Examples |
|---|---|
| Standard | `coldStart`, `warmStart`, `linkDown`, `linkUp`, `authenticationFailure` |
| Routing | `bgpSessionDown`, `bgpEstablished` |
| UPS | `upsOnBattery`, `upsLowBattery`, `upsUtilityRestored`, `upsBatteryNormal`, `upsOutputOverload`, `upsOutputNormal`, `upsFanFailure`, `upsBatteryFailure`, `upsBatteryDisconnected` |
| Resource | `cpuHighUsage`, `cpuSustained`, `cpuTempCritical`, `memoryHighUsage`, `temperatureAlert` |
| Environmental | `sensorAmbientTempHigh` / `Critical`, `sensorHighHumidity` / `Critical` / `Low`, `sensorHighAirflow` / `Low`, `dewpointAlert` |
| PDU / generator | enterprise `.6.*` and `.3.*` trees |
| **Clears** | `cpuNormal`, `memoryNormal`, `temperatureNormal`, `sensorAmbientTempNormal`, `sensorHumidityNormal`, `sensorDewPointNormal`, `sensorAirflowNormal` |

The clear traps are on **deliberately distinct OIDs** — the source comment says
so explicitly, "so a receiver shows them as their own clear events instead of
decoding the generic linkUp OID". Map each clear to `is_clear: true` on the same
`event_type` as its raise (see `08-protocol-adapters.md` §2.3).

---

## 7. BACnet points

`core/bacnet_plant_generator.PLANT_SPEC` is the authoritative point list. Object
instances are assigned **in list order**, AI 1..N and BI 1..M, with the object
type disambiguating equal instance numbers.

The source repeatedly notes that new points are appended at the **end** so that
existing instance numbers do not move — which is a strong hint that instance
numbers are a fragile key. **Bind by `Object_Name`.**

Two semantic distinctions the simulator makes on purpose, both of which the DCIM
must preserve:

1. **Chiller** — `Alarm_HighPressure` is the *cutout* (latched, machine off,
   manual reset); `Alarm_CondPressLimit` is the *capacity limit* (machine running
   and unloading against warm condenser water). The source records that
   collapsing them made the BMS demote a healthy lead machine every tick.
2. **CRAH** — `Alarm_HighTemp` is high *discharge* (the unit has lost its ability
   to cool: its own fault); `Alarm_HighReturnAir` is high *return* (the unit is
   fine, the hot aisle feeding it is too hot: a load symptom). Different
   runbooks, different owners.

Point counts per type: chiller 13 AI / 6 BI, CDU 13 AI / 5 BI, CRAH 10 AI / 5 BI,
pump 9 AI / 3 BI, cooling tower 9 AI / 3 BI, valve 3 AI / 2 BI.

---

## 8. Known gotchas, collected

| # | Gotcha | Consequence if missed |
|---|---|---|
| 1 | Community == device IP, never `public` | every device looks dead |
| 2 | Servers have two SNMP endpoints on different IPs | half the server metrics missing |
| 3 | snmpsim is one wildcard listener | destination IP is not the identity; do not "fix" a failure by changing the target IP |
| 4 | `_NO_SNMP_TYPES` have no SNMP at all | 5 device types alarm as unreachable forever |
| 5 | Collector cannot bind udp/47808 on the simulator host | bind 47809; broadcast Who-Is will not work same-host |
| 6 | gNMI target goes in `prefix.target`, and the server is one process for all devices | without the target you get every device's data merged |
| 7 | Trap OIDs are rewritten to vendor MIBs | most traps unrecognised |
| 8 | Clear traps are distinct OIDs, not the raise OID | alarms never clear |
| 9 | Power/cooling edges have `src_iface = null` | an importer defaulting to 0 invents port 0 links |
| 10 | Edge direction comes from `src_node`/`dst_node` | power flow reversed on some edges |
| 11 | Redfish on 8443 is PLAIN HTTP, not TLS (verified against the running plane) | an adapter that assumes https by port number gets `wrong version number` and reports every BMC unreachable; the scheme must be per-endpoint data, and must never be guessed by falling back - a downgrade would put the BMC password on the wire in clear |
| 12 | Utility feed meter is Modbus-only | no site energy, therefore no PUE |
| 13 | The export's telemetry fields are a snapshot | importing them creates permanently stale state |
| 14 | Fleet lifecycle changes inventory at runtime | a static device list goes stale within minutes |
| 15 | The simulator's own SNMP port is a UI setting; the API default is 161 | probes against 1611 or 161 mismatch and look like a dead agent |
| 16 | BMC event delivery is a plain `urllib` POST, 3 s timeout, no retry | a slow receiver, or an `https` destination whose certificate the poster cannot verify, loses every event with no error at either end |
| 17 | Every simulated BMC event carries the same MessageId (`Simulator.1.0.Alert` / `.StatusChange`) | classifying on MessageId alone collapses all BMC events onto one alarm key; the condition is in the message TEXT |
| 18 | A clear arrives as Severity `OK` with the text `"<label> cleared"` | matching only the raised text leaves every alarm open forever |
| 19 | The warning and critical bands are different labels (`temperature high` vs `temperature critical`) | folding them onto one event type leaves the warning alarm open after the critical one asserts |
| 20 | A BMC reset drops every subscription silently, and there is a per-BMC subscription cap | subscribe-once means events stop with no symptom; reconcile on an interval |
| 21 | The topology export sets `modbus_role` only for the RS-485 trunk; the thirty native-TCP electrical devices carry none | keying endpoint creation on the role alone creates endpoints for twelve transmitters and no meters at all, and nothing fails - a device with no endpoint is simply never polled |
| 22 | An RTD and a magnetic flow meter are both `device_type: sensor` | the register template cannot be chosen from the device type; the probe role (from the name prefix, CHWS/CHWR/CWS/CWR/CTB/FLOW) is what distinguishes them |
| 23 | Modbus maps are sparse and a read crossing an unimplemented address is refused in its entirety | a blind span across a gap loses every point either side of it, so reads must be planned into contiguous blocks |
| 24 | The Eaton maps are word-SWAPPED; the Schneider ones are not | decoding one with the other's word order returns energy off by a factor of 65536, and the number charts perfectly well |
| 25 | Every map carries a validity discrete, and FC43 serves the map id in the revision slot | the validity bit is the only thing distinguishing "0 W" from "not sampled yet"; the map id is the only way to check a template belongs to the device answering |
| 26 | `proto/gnmi.proto` declares `go_package = openconfig/gnmi` but is NOT wire-compatible with it. `TypedValue.json_ietf_val` is field **13**, where the standard says 11 and assigns 13 to `proto_bytes` | a conformant client reads the JSON as opaque protobuf and decodes nothing at all, with no error anywhere - the Get succeeds and returns zero leaves |
| 27 | `GetRequest.path` is field **3**, where the standard says 2 (and `type`/`encoding` are 5/6 against 3/5) | the server never sees the requested path and answers with the ENTIRE document for every request, so a client that assumes the response was scoped to what it asked for finds nothing |
| 28 | gNMI listens on each device's OWN address on **50051** - 46 listeners on 46 device IPs | the per-device `gnmi_port` in the topology export says 57400 on every device and nothing listens there; the controller's own port is authoritative |
| 29 | `system/state/uptime` carries CENTISECONDS, the same units SNMP's sysUpTime returns, and is not a standard openconfig leaf | without the 0.01 scale the gNMI and SNMP planes disagree by a factor of a hundred for one device |
| 30 | A STREAM subscription re-sends the full snapshot each interval; there is no ON_CHANGE | every notification carries every mapped subtree, so a client that offers each notification to all its mappings publishes each counter once per update unless it deduplicates |
| 31 | This plane's SNMP ifName and openconfig `name` happen to be identical strings, so the two planes already agree on interface identity | it makes the double-series problem invisible here. Real gear does not oblige - an agent reporting Gi0/0 against openconfig's GigabitEthernet0/0, or indexing by ifIndex, produces two series per port - so normalisation is applied regardless and tested against gear that abbreviates |
| 32 | The SNMP dataset writes some type tags in HEX-as-decimal: `ifHCInOctets`/`ifHCOutOctets` use `44` and the ifTable error/discard columns use `41`, where .snmprec wants the decimal ASN.1 tag (**70** Counter64, **65** Counter32; 0x46 and 0x41 are the same values in hex) | snmpsim serves only the lines it can parse, so those OIDs are silently absent and SNMP reports no interface traffic at all. The proof is inside one walk: ifTable columns tagged `2` come back and columns tagged `41` from the same rows do not. A one-character fix in `core/snmprec_generator.py` restores six metrics across 894 endpoints |
| 33 | One SNMP poll averages **5.5 s** against this plane (measured live: 25815 polls). Redfish averages 0.22 s, Modbus 0.16 s, BACnet 0.08 s | snmpsim serves the whole fleet from ONE process behind a wildcard socket, so SNMP latency is a property of the responder rather than of any device. At 894 endpoints and 48 concurrent that projects to ~100 s per sweep against a 30 s interval - the collector is permanently behind on SNMP alone |
| 34 | An ifXTable walk against a 65-port switch returns ZERO varbinds without an error, while the same agent serves ifTable for all 65 ports and serves ifXTable normally on a 2-port server | so `if_speed` is present for servers and absent for large switches. The walk does not fail, so nothing is recorded as a miss - the table is simply empty, which is why it went unnoticed |

---

## 9. Bring-up checklist

1. Start the simulator; load `topologies/dual_dc_enterprise.json`; bind device
   IPs; start the SNMP, gNMI, BACnet, Redfish and Modbus controllers.
2. `POST /api/snmp/trap-receiver` → the collector's `host:162`.
3. Run the DCIM seed importer against `GET /api/topology/export`.
4. Verify endpoint counts by protocol against §3.1 (e.g. 310 servers should yield
   310 OS-agent SNMP endpoints, 310 BMC SNMP endpoints and 310 Redfish
   endpoints).
5. Start the collector; confirm it fetches an assignment and that
   `dcim_collector_endpoints{status="ONLINE"}` climbs to the expected total.
6. Confirm `dcim_ingest_lag_seconds` settles below 5 s.
7. Inject a trap via `POST /api/traps/send` and watch it reach the browser.
8. Inject a chiller fault and watch the BACnet alarm path.
9. `POST /api/fleet/provision-rack` and confirm the new devices are polled within
   one interval without a restart.
