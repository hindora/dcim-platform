# 08 — Protocol Adapter Design

Every fact in this document about the simulator was read from the source in this
repository, with the file cited. Verify against the code before relying on any of
it in a later version.

---

## 1. SNMP poller

### 1.1 How the simulator serves SNMP — and the two traps for the unwary

The simulator runs **snmpsim** with a **single wildcard listener**
`0.0.0.0:161` (`simulator/snmpsim_controller.py` — "Use a single wildcard
endpoint instead of one flag per device IP"). Requests are routed to a device by
the **community string**, which resolves to `datasets/snmp/<community>.snmprec`.

Two consequences that will cost you a day each if you do not know them:

1. **The community string is the device's SNMP IP address**, not `public`.
   `Device.__post_init__` sets `snmp_community = ip_address` when it is still the
   default (`core/device_manager.py`). A wrong community is a **silent drop** —
   snmpsim finds no dataset and does not answer — which looks exactly like a dead
   device.

2. **Which IP is the SNMP IP depends on the device type**
   (`SNMPRecGenerator.snmp_address`):

   | Device type | SNMP address | Reason |
   |---|---|---|
   | server | `ip_address` (production NIC) | the OS net-snmp agent binds the production NIC |
   | server BMC | `mgmt_ip` — a **second, separate** endpoint | the BMC is a different controller with its own agent (`bmc_address`, subtree `1.3.6.1.4.1.99999.26`) |
   | everything else | `mgmt_ip` if set, else `ip_address` | the NOS agent answers on the OOB management network |

   So the sample server `SRV01-DC1-HA-R2-01` has `ip_address = 10.50.11.19` and
   `mgmt_ip = 10.51.11.25`, and **two SNMP endpoints**, with communities
   `10.50.11.19` and `10.51.11.25` respectively. This is finding A2 in concrete
   form.

3. **Some device types have no SNMP agent at all** (`_NO_SNMP_TYPES`):
   `rpp`, `chiller`, `pump`, `cooling_tower`, `valve`. That is realistic — real
   chillers and pumps carry no SNMP card; the BMS gateways their points. Do not
   create SNMP endpoints for them, and do not alarm on their absence.

### 1.2 What to poll

| Group | OIDs | Applies to |
|---|---|---|
| system | `1.3.6.1.2.1.1` (sysDescr, sysObjectID, sysUpTime, sysName, sysLocation, sysContact) | all |
| interfaces | `1.3.6.1.2.1.2.2.1` + `1.3.6.1.2.1.31.1.1.1` (ifX: HC counters, ifHighSpeed, ifName, ifAlias) | network gear, servers |
| host resources | `1.3.6.1.2.1.25.*` (`hrProcessorLoad`, `hrStorage`) | servers |
| UCD | `1.3.6.1.4.1.2021` (load, memory) | servers |
| entity sensors | `1.3.6.1.2.1.99.1.1.1` (ENTITY-SENSOR-MIB) | sensors, some switches |
| UPS | `1.3.6.1.2.1.33.1` (UPS-MIB) + sim enterprise `1.3.6.1.4.1.99999.4` | UPS |
| PDU | sim enterprise `1.3.6.1.4.1.99999.5` (+ `.5.20.1` per-outlet) | PDU, floor PDU |
| generator | `1.3.6.1.4.1.99999.7` | generator |
| utility / switchgear / ATS / MCC / MPP | `1.3.6.1.4.1.99999.8 / .9 / .10 / .11 / .12` | electrical |
| plant SNMP (CRAH, CDU) | `1.3.6.1.4.1.99999.20–.25` | CRAH, CDU |
| BMC | `1.3.6.1.4.1.99999.26` | server BMC endpoint |
| vendor sensors | Raritan `1.3.6.1.4.1.13742.6.5.5.3.1`, Geist `1.3.6.1.4.1.21239.5.1`, APC NetBotz `1.3.6.1.4.1.318.1.1.10.4.2.2.1` | environmental probes |
| Cisco | `1.3.6.1.4.1.9.9.109.1.1.1.1` (CPU), `.9.9.48.1.1.1` (memory), `.9.9.13.1.3.1` (temp) | Cisco gear |

The `1.3.6.1.4.1.99999.*` tree is the **simulator's** enterprise space. When this
DCIM points at real hardware those become the vendor's real MIBs — which is
exactly why the mapping lives in `contracts/mappings/snmp/*.yaml` as data, keyed
by `(vendor, device_type)`, and not in Go code.

### 1.3 Mapping file shape

```yaml
# contracts/mappings/snmp/standard.yaml
version: 1
tables:
  - name: if_table
    walk: 1.3.6.1.2.1.2.2.1
    index_from: 1.3.6.1.2.1.2.2.1.1     # ifIndex is the instance
    columns:
      - oid: 1.3.6.1.2.1.31.1.1.1.6      # ifHCInOctets
        metric: if_in_octets
        value_type: counter
        counter_bits: 64
      - oid: 1.3.6.1.2.1.2.2.1.8         # ifOperStatus
        metric: if_oper_state
        value_type: bool
        transform: { map: { "1": true, "2": false, "3": false } }
      - oid: 1.3.6.1.2.1.31.1.1.1.15     # ifHighSpeed, Mbps
        metric: if_speed
        value_type: gauge
        transform: { scale: 1000000 }
scalars:
  - oid: 1.3.6.1.2.1.1.3.0
    metric: sys_uptime
    value_type: counter
    transform: { scale: 0.01 }           # TimeTicks are centiseconds
```

Supported transforms: `scale`, `offset`, `map`, `enum_to_bool`, `divide_by_oid`
(for scaled integer pairs, e.g. tenths of a degree with a separate exponent
column — ENTITY-SENSOR-MIB does exactly this and it must not be hardcoded).

### 1.4 Implementation rules

- **GETBULK for tables** (`max-repetitions: 25`), GET for scalars. One
  `WalkAll` per table, not per column.
- Never emit a metric for `noSuchObject` / `noSuchInstance` / `endOfMibView` —
  record it as a `Miss` with reason `no_such_object`. A device that legitimately
  lacks an OID must not generate a data gap alarm.
- Read `sysUpTime` **in the same PDU** as the counters where possible. A
  `sysUpTime` that went backwards means the agent restarted and every counter
  reset; set `counter_reset = true` on that cycle's samples.
- Cache the ifIndex → interface mapping per endpoint, refreshed every 10th poll
  or when `ifNumber` changes. Walking `ifDescr` every 30 s is wasted work.
- Emit the raw counter, not a rate. Rate derivation happens at ingest, once,
  where the previous value is durable.

---

## 2. SNMP trap receiver

### 2.1 How the simulator sends traps

`core/trap_engine.py` sends to a configurable receiver, defaulting to
`127.0.0.1:162`. Point it at the collector with
`POST /api/snmp/trap-receiver` on the simulator's API
(`api/routers/snmp.py`). The trap's community is set from the source device's
SNMP address (`_trap_source_ip(device, ...) or device.snmp_community`), so the
community is a second, independent identity hint alongside the source IP.

### 2.2 The critical detail: OIDs are rewritten to the vendor's MIB

`core/trap_definitions.py` defines 103 traps on the simulator's own tree
(`1.3.6.1.4.1.99999.*`), and its module docstring states plainly that
`core.vendor_oids.trap_oid()` **rewrites the OID to the vendor's own MIB OID at
send time**. The same logical over-current condition arrives as:

- APC rPDU → `1.3.6.1.4.1.318.0.276` (`rPDUOverload`)
- Raritan PX → `1.3.6.1.4.1.13742.6.0.65` (`overCurrentProtectorSensorStateChange`)

A receiver that only knows the placeholder tree will drop most traps. The
mapping table must therefore be keyed by the **wire OID**, with the placeholder
tree included as a fallback for traps that have no verified vendor counterpart.

### 2.3 Mapping file shape

```yaml
# contracts/mappings/snmp/traps.yaml
version: 1
traps:
  - oid: 1.3.6.1.6.3.1.1.5.3            # standard linkDown
    event_type: link_down
    severity: MAJOR
    is_clear: false
    instance_from_varbind: 1.3.6.1.2.1.2.2.1.1     # ifIndex
    clears: [link_up]
  - oid: 1.3.6.1.6.3.1.1.5.4
    event_type: link_up
    severity: CLEAR
    is_clear: true
    instance_from_varbind: 1.3.6.1.2.1.2.2.1.1
  - oid: 1.3.6.1.4.1.318.0.276
    vendor: APC
    event_type: pdu_overload
    severity: MAJOR
    is_clear: false
  - oid: 1.3.6.1.4.1.13742.6.0.65
    vendor: Raritan
    event_type: pdu_overload
    severity: MAJOR
  - oid: 1.3.6.1.4.1.99999.1.13         # simulator cpuNormal
    event_type: cpu_high
    severity: CLEAR
    is_clear: true
```

Note the shape of the clear pairs. `cpuNormal` maps to **`event_type: cpu_high`
with `is_clear: true`**, not to an event type of its own. That is what makes the
alarm key match on the clear side without the backend knowing anything about
OIDs. The simulator emits these clears on deliberately distinct OIDs precisely so
a receiver can do this.

### 2.4 Receiver implementation

```
udp/162 → gosnmp TrapListener
  → 8 worker goroutines (never process inline; a slow DB lookup drops packets)
  → identify source: srcIP → endpoint cache, fallback community → endpoint
  → look up trap OID → event_type, severity, is_clear
  → extract instance from the configured varbind
  → build Event{} with dedup_key = sha1(endpoint|event_type|instance|unix_second)
  → publish to events.v1
```

- **Buffer generously.** UDP traps are lossy by design and a burst during an
  outage is exactly when you need them. Read socket buffer ≥ 4 MB
  (`SO_RCVBUF`), and an internal channel of ≥ 10,000.
- **Unknown OIDs are still events.** Emit `event_type: unknown_trap` with the OID
  in `raw_identifier` and severity `INFO`. Never drop.
- **Unresolvable sources are still events.** `device_id = ""`, `source_ip` set.
  The backend records them and raises a platform alarm.
- **Rate-limit per source** (say 100 traps/minute/device) with an overflow
  counter, so one flapping interface cannot fill the stream.
- Traps set state, never values. Do not write telemetry from a trap.

---

## 3. gNMI

### 3.1 How the simulator serves gNMI

One gRPC server, **insecure** (`add_insecure_port`, `simulator/gnmi_server.py`),
default port **57400**, bound to `[::]`. Device selection is by the
**`prefix.target` field**, which is the device's gNMI IP:

> "Routing: The gNMI 'target' field in the path prefix maps to a device IP.
> e.g. target="10.1.0.2" → loads datasets/10.1.0.2.gnmi.json"

Supported encodings are `JSON_IETF` and `JSON`. `Get`, `Set`, `Capabilities` and
`Subscribe` (ONCE / POLL / STREAM) are implemented. With no target, the server
serves **all** loaded devices — useful for a smoke test, useless for production,
and something the adapter must never rely on.

Paths served are OpenConfig models: `openconfig-interfaces`,
`openconfig-system`, `openconfig-platform`, `openconfig-lldp`,
`openconfig-network-instance`.

### 3.2 Adapter design

```go
type subscription struct {
    endpoint *Endpoint
    conn     *grpc.ClientConn      // one per gNMI server address, shared by targets
    stream   gnmi.GNMI_SubscribeClient
    paths    []*gnmi.Path
}
```

- **One gRPC connection per server address, many targets over it.** HTTP/2
  multiplexes; opening 38 connections to the same host wastes fds for nothing.
- Always set `prefix.target`. Never rely on the "no target ⇒ all devices"
  behaviour.
- Subscription request:
  - `SAMPLE` with `sample_interval` for counters and numeric state;
  - `ON_CHANGE` for `oper-status` and other enums where the server supports it;
  - `updates_only = false` on the first subscribe so you get an initial snapshot,
    then rely on the sync marker.
- Wait for `sync_response = true` before marking the endpoint `ONLINE`. Data
  before the sync marker is the initial dump, not a change.
- **On reconnect, invalidate all counter baselines for that target.** The stream
  gap means you cannot know whether the counter reset. Emitting the delta across
  a reconnect is the single most common gNMI bug.
- Timestamps: gNMI `Notification.timestamp` is **nanoseconds since epoch** — use
  it as `observed_at`, do not restamp.
- `TypedValue` handling must cover `json_ietf_val` (the simulator's primary
  encoding), `uint_val`, `int_val`, `double_val`, `bool_val`, `string_val`. For
  `json_ietf_val` the payload is a JSON subtree, so the adapter walks it and
  emits one sample per leaf against the path mapping.
- Path → metric mapping keys on the **path with keys stripped**, and the key
  becomes the instance:
  `/interfaces/interface[name=Ethernet1]/state/counters/in-octets`
  → metric `if_in_octets`, instance `Ethernet1`.
  Then resolve the name to ifIndex at ingest, so gNMI and SNMP samples for the
  same interface land on the same instance.

That last point is important and easy to miss: SNMP keys interfaces by ifIndex
and gNMI keys them by name. If you do not normalise, one interface produces two
disjoint series.

- TLS: the simulator is insecure, so use `grpc.WithTransportCredentials(insecure.NewCredentials())`
  **driven by endpoint config**, never hardcoded. Real gNMI is TLS with client
  certificates.

---

## 4. BACnet — the hard one

### 4.1 Honest assessment

**There is no mature, complete BACnet/IP client library in Go.** This is the one
place where "use Go for the collector" has a real cost, and it should be stated
plainly rather than discovered in week 6.

Options, with a recommendation:

| Option | Verdict |
|---|---|
| Use an existing Go BACnet library (`github.com/absmach/bacnet` and similar) | Partial coverage, thin ReadPropertyMultiple support, little MS/TP routing. Usable as a reference for encoding, not as a dependency you can lean on. |
| **Write a narrow BACnet/IP client in Go** | **Recommended.** You need exactly: BVLC framing, NPDU (including routed DNET/DADR for MS/TP), and four APDU services — Who-Is/I-Am, ReadProperty, ReadPropertyMultiple, SubscribeCOV/COVNotification. That is roughly 1,200–1,800 lines including encoding, and it is well-specified. `core/bacnet_object_model.py` in this repository is a working reference implementation of the same encoding on the server side. |
| Run a side-process in Python (BACpypes3) that speaks the canonical contract | Acceptable fallback, and pragmatic if BACnet coverage becomes a schedule risk. It violates the "one collector" tidiness but not the architecture: it publishes the same protobuf to the same stream. Keep it as the documented plan B. |

Do **not** attempt full BACnet stack conformance. You are a client reading points
from known devices, not a BMS.

### 4.2 Simulator specifics

- BACnet/IP on **udp/47808** (`0xBAC0`) — `simulator/bacnet_controller.py`,
  `simulator/bacnet_device.py`.
- Each simulated plant device is a BACnet device object with its own
  **device instance number**; objects are Analog Input (AI) and Binary Input (BI)
  with instances assigned in list order
  (`core/bacnet_plant_generator.build_plant_object_tree`).
- **MS/TP devices have no IP.** `Device.mstp_net`, `mstp_mac`, `mstp_router_ip`
  — a Belimo "-BAC" actuator and a Grundfos CIM 300 pump card sit on an RS-485
  trunk behind a BACnet/IP router (Loytec class) and are addressed as
  `(network, MAC)` via that router's IP. Reads must carry a routed NPDU with
  DNET/DADR set. This is not an optional refinement; it is how half the cooling
  plant is reachable.

**Port collision warning.** If the collector runs on the same host as the
simulator, it **cannot** bind udp/47808 — the simulator already has it. Bind the
collector to **47809** and send to the simulator's 47808. This works for directed
requests. It does **not** work for broadcast Who-Is discovery, because the
simulator's devices reply to the broadcast port. For discovery against a
same-host simulator, use directed (unicast) Who-Is to each known IP, or run the
collector on a different host / in a container with its own network namespace.

### 4.3 Mapping

```yaml
# contracts/mappings/bacnet/plant.yaml
version: 1
device_types:
  chiller:
    analog_inputs:
      CHW_Supply_Temp:  { metric: chws_temperature,  unit: C }
      CHW_Return_Temp:  { metric: chwr_temperature,  unit: C }
      Active_Power:     { metric: active_power, unit: W, transform: { scale: 1000 } }  # kW → W
      COP:              { metric: cop, unit: ratio }
      Run_Hours:        { metric: run_hours, unit: h, value_type: counter }
    binary_inputs:
      Chiller_Running:      { metric: chiller_running_state }
      Alarm_HighPressure:   { metric: high_pressure_alarm,     event_type: chiller_high_pressure, severity: CRITICAL }
      Alarm_CondPressLimit: { metric: cond_press_limit_alarm,  event_type: chiller_cond_press_limit, severity: WARNING }
```

**Bind by object name, not by instance number.** The simulator's own source
warns that BI instances are assigned in list order and that new points are
appended at the end *specifically so existing instance numbers do not move* —
which tells you instance numbers are a fragile key even in a system that is
being careful. Read `Object_Name` once at discovery, cache
`name → (object_type, instance)` per device, and re-read the map when the device
restarts or a read misses.

### 4.4 Read strategy

1. **ReadPropertyMultiple**, not one ReadProperty per point. A chiller has 13 AI
   + 6 BI; that is one request instead of 19. Respect `Max_APDU_Length_Accepted`
   and split the request when it would overflow — a too-large RPM gets an
   abort-segmentation-not-supported, not a partial answer.
2. Read `Present_Value` always; read `Status_Flags` where you want quality
   (`in_alarm`, `fault`, `out_of_service`). Map `fault` or `out_of_service` to
   `Quality.SUSPECT` — a point in `out_of_service` is being overridden by a
   technician and its value is not the plant's real state.
3. `Units` is read **once** at discovery and cached; do not re-read it every
   cycle. Use it to validate against the registry unit and log a mismatch.
4. **SubscribeCOV** on the BI alarm points with a lifetime (300 s) and renewal at
   half-life. Handle `COVNotification` on the collector's bound port. If COV
   subscription fails, fall back to polling those points at 5 s and log the
   downgrade — never fail the whole endpoint.
5. Timestamps: BACnet carries none. `observed_at = collected_at = read time`.
6. One outstanding request per device unless you implement invoke-ID tracking.
   `per_host: 1` in the collector config exists for this reason.

---

## 5. Redfish

### 5.1 Simulator specifics

- HTTPS, port **8443**, one Redfish service per server BMC on the BMC's mgmt IP
  (`simulator/redfish_controller.py`, `simulator/redfish_device.py`).
- Default credentials `admin` / `password` — these are the controller's defaults
  and are configurable at start. Put them in the `credential` table, never in
  code.
- Self-signed certificate. Set `verify_tls: false` **per endpoint** in
  `addressing`, so the real-hardware path can turn it on without a code change.
- Implemented resources (verified in `simulator/redfish_device.py`):

```
/redfish/v1                                       service root
/redfish/v1/Systems  /Systems/{id}
/redfish/v1/Systems/{id}/EthernetInterfaces
/redfish/v1/Systems/{id}/Actions/ComputerSystem.Reset
/redfish/v1/Chassis  /Chassis/{id}
/redfish/v1/Chassis/{id}/Thermal                  Temperatures[], Fans[]
/redfish/v1/Chassis/{id}/Power                    PowerControl[], PowerSupplies[]
/redfish/v1/Managers /Managers/{id}  /Actions/Manager.Reset
/redfish/v1/SessionService/Sessions
/redfish/v1/EventService
/redfish/v1/EventService/Subscriptions            GET, POST, DELETE
/redfish/v1/EventService/Actions/EventService.SubmitTestEvent
```

### 5.2 Poll design

- **Do not crawl.** Discover once (service root → Systems → Chassis → the
  Thermal/Power URLs), cache the resolved URLs per endpoint, and afterwards fetch
  only `/Chassis/{id}/Thermal` and `/Chassis/{id}/Power` plus `/Systems/{id}`.
  Three GETs per cycle, not thirty.
- **Session auth, not basic auth per request.** `POST /SessionService/Sessions`
  once, keep the `X-Auth-Token`, re-authenticate on 401. Basic auth on every
  request makes real BMCs slow and, on some iLO firmware, rate-limits you.
- **Connection reuse is mandatory.** `http.Transport` with
  `MaxIdleConnsPerHost: 2`, `IdleConnTimeout: 90s`. Without keepalive you pay a
  TLS handshake per poll per BMC, which at 310 servers is the dominant cost in
  the whole collector.
- ETag / `If-None-Match` where the BMC supports it. The simulator may not; treat
  it as an optimisation, not a requirement.
- Map by **JSON pointer**, as data:

```yaml
# contracts/mappings/redfish/resources.yaml
thermal:
  temperatures:
    match_by: Name
    entries:
      "CPU*":    { metric: cpu_temperature,    instance_from: Name }
      "Inlet*":  { metric: inlet_temperature }
      "Exhaust*":{ metric: exhaust_temperature }
    reading_field: ReadingCelsius
  fans:
    metric: fan_speed
    reading_field: Reading
    instance_from: Name
power:
  power_control:
    - { field: PowerConsumedWatts, metric: power_draw }
  power_supplies:
    - { field: LineInputVoltage,  metric: psu_input_voltage, instance_from: MemberId }
    - { field: PowerOutputWatts,  metric: psu_output_power,  instance_from: MemberId }
    - { field: Status.State,      metric: psu_state, transform: { map: { Enabled: true } } }
```

- Sensors with `Status.State != "Enabled"` or a `null` reading must be emitted as
  a `Miss`, not as `0`. A zeroed absent sensor is how you get a false "CPU at 0 °C"
  alarm.

### 5.3 Event subscription (finding A4)

```
startup:
  GET  /redfish/v1/EventService/Subscriptions
  → delete subscriptions whose Destination points at this collector but with a
    stale URL (previous pod IP, old port)
  → if no subscription matches our current public_url:
      POST /redfish/v1/EventService/Subscriptions
        { "Destination": "https://collector-1:9443/redfish-events",
          "EventTypes": ["Alert","StatusChange","ResourceUpdated"],
          "Protocol": "Redfish",
          "Context": "<endpoint_id>" }
  → store the returned subscription URI on the endpoint

runtime:
  POST /redfish-events  (collector's HTTPS listener)
  → validate Context matches a known endpoint
  → for each Events[] member: MessageId → event_type + severity via mapping
  → Event{} → events.v1
```

Reconcile on every startup and every 10 minutes. Orphaned subscriptions
accumulate on real BMCs and eventually hit the subscription limit (often 8–20),
after which new subscriptions silently fail.

`MessageId` mapping example:

```yaml
messages:
  "Alert.1.0.TemperatureAbove":       { event_type: thermal_high,  severity: MAJOR }
  "Alert.1.0.TemperatureNormal":      { event_type: thermal_high,  severity: CLEAR, is_clear: true }
  "ResourceEvent.1.0.ResourceErrorsDetected": { event_type: hardware_error, severity: MAJOR }
```

Use `SubmitTestEvent` in integration tests — it exists precisely so you can prove
the event path without inducing a real fault.

---

## 6. Modbus/TCP (finding A8)

### 6.1 Simulator specifics

- Modbus/TCP on **tcp/502** (`simulator/modbus_controller.py`).
- Three device roles (`Device.modbus_role`):
  - `server` — native Modbus/TCP on the device's own IP;
  - `gateway` — owns an IP and fronts an RS-485 trunk, addressed by **unit id**
    (Moxa MGate class);
  - `rtu_slave` — a field transmitter on that trunk with **no IP of its own**,
    reached as `(gateway_ip, unit_id)`.
- The register map is in `core/modbus_register_map.py`, exportable via
  `GET /api/modbus/map/export` — use that export to generate
  `contracts/mappings/modbus/registers.yaml` rather than transcribing by hand.

### 6.2 Adapter rules

- Coalesce reads: one `ReadHoldingRegisters` over a contiguous span beats twelve
  single-register reads. Cap the span at 125 registers (the protocol limit).
- **Serialise per gateway.** All unit ids behind one Moxa share one RS-485 trunk;
  concurrent requests to different unit ids on the same gateway will time out and
  look like six dead sensors. `per_host: 1`.
- Encoding matters and is per-device: word order (big/little endian), 32-bit
  float vs scaled int16, and sign. Put `encoding: float32_be | int16_scaled | uint32_le`
  in the mapping, never in code.
- Modbus has no timestamps and no quality flags. `observed_at = read time`,
  quality from range validation only.
- A gateway timeout is **one** endpoint failure (the gateway), and its slaves
  become `UNKNOWN` by dependency — not six independent failures. This is what
  `via_endpoint_id` is for.

---

## 7. sFlow (optional)

The simulator emits sFlow to **udp/6343** (`simulator/sflow_controller.py`).
Flow analytics is a different product; include the receiver only if top-talker
analysis is actually wanted. If included, it should write to its own hypertable
(`flow_sample`) and never into `telemetry_sample` — flow records have a different
cardinality profile entirely and will wreck the compression on the metrics table.

---

## 8. Adapter conformance checklist

Every adapter must, before it is considered done:

- [ ] respect `ctx` deadlines on every network call;
- [ ] return `PollOutcome` with `Misses` populated rather than failing wholesale;
- [ ] emit only registry-known metric keys (enforced by the emit-time validator);
- [ ] convert units to the registry unit at the adapter boundary;
- [ ] set `observed_at` from the protocol where the protocol provides it;
- [ ] classify errors into `timeout | auth | refused | unreachable | decode | protocol`;
- [ ] never log a credential, and never log a full payload above `debug`;
- [ ] have an integration test against the running simulator that asserts at
      least one metric per device type it serves;
- [ ] survive a malformed response without panicking (fuzz the decoder).
