# Collector firewall matrix

What a collector needs open, in each direction. Give this to the network and
facilities teams verbatim when standing up a new site - see the operator
journey in [docs/26](../docs/26-collector-deployment-plan.md#operator-journey-bringing-up-dc3s-collectors-in-the-future-system)
for where this fits in the sequence.

## Outbound: collector -> platform

| Port | Protocol | Purpose |
|---|---|---|
| 443 | TCP (HTTPS) | Assignment fetch, config, discovery claims/results, heartbeat fallback. The only port this collector needs toward the platform's control plane |
| 6379 | TCP | Redis (telemetry, events, heartbeat streams) - see the note in `deploy/collector.env.example` about `DCIM_REDIS_URL`. Only needed until docs/26 Phase 3's ingest gateway ships; today this is a real route to the platform's datastore, not just its API, and should run over a VPN or private link, never the open internet |

## Outbound: collector -> devices

| Port | Protocol | Purpose |
|---|---|---|
| 161 | UDP | SNMP polling (v1/v2c/v3) |
| 443 | TCP | Redfish (BMC out-of-band management) |
| 502 | TCP | Modbus/TCP (facility gateways: Moxa MGate and similar, fronting serial RTU instruments) |
| 623 | UDP | IPMI, where a BMC predates Redfish |
| 47808 | UDP | BACnet/IP, including Who-Is/I-Am discovery and BBMD traffic |
| 9339 / 57400 / 6030 | TCP | gNMI (port varies by vendor; the assignment carries the endpoint's actual port) |

## Inbound: devices -> collector

| Port | Protocol | Purpose |
|---|---|---|
| 162 | UDP | SNMP trap receiver |
| 9143 | TCP | Redfish event receiver (`EventDestination` subscriptions), off by default - only needed if `protocols.redfish_event.enabled` is set |

A collector needs a **static IP** on whichever management network it polls:
both inbound ports above exist because devices send to it unprompted, and an
address that moves breaks every trap subscription and every Redfish
`EventDestination` silently until the next full reconciliation sweep.

## Binding port 162 without running as root

The trap listener's default port, 162/udp, is a privileged port. Three ways
to grant it, in the order this deployment prefers them:

1. **Linux capability** (native install): `deploy/collector.service` sets
   `AmbientCapabilities=CAP_NET_BIND_SERVICE`, which lets the
   `dcim-collector` system user bind it without running as root.
2. **Container capability**: `docker run --cap-add=NET_BIND_SERVICE …`, or
   the Kubernetes pod-spec equivalent
   (`securityContext.capabilities.add: ["NET_BIND_SERVICE"]`).
3. **A non-privileged listen port plus a redirect**, when neither of the
   above is available (some hardened container platforms strip all
   capabilities unconditionally): set `protocols.snmp_trap.listen` to
   `0.0.0.0:1162` and redirect 162 to it at the host or container-runtime
   level (`iptables -t nat -A PREROUTING -p udp --dport 162 -j REDIRECT
   --to-port 1162`). Point devices at port 162 as usual; only the collector's
   own listener moved.

## BACnet's own requirements

BACnet/IP discovery (Who-Is/I-Am) is UDP broadcast and does not cross a
router. A collector that needs to discover devices on a subnet other than its
own must either run with host networking (bridged Docker networking rewrites
the source address BACnet needs) or be registered as a BACnet Foreign Device
with that subnet's BBMD - a change the facilities team who owns the BBMD has
to make, not something this platform can do unattended. See docs/26 Phase 9
for the full treatment.
