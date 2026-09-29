# Read-only device accounts

docs/26 Phase 4's own decision, stated plainly: **control and write
credentials stay out of the collector** until a separate, change-controlled
control feature exists. In production the DCIM reads while the BMS/EPMS
writes. This document is what to hand the team provisioning device accounts
so that decision is enforced by the device, not just by this collector's
code never happening to send a write.

Verified against this codebase, not assumed: `internal/adapters/bacnet` and
`internal/adapters/modbus` issue no write operation at all - no
`WriteProperty`, no `WriteSingleRegister`/`WriteMultipleRegisters`, no
`WriteCoil`. For those two protocols the collector's own code is already
the enforcement; the account-level guidance below is defence in depth, not
the only thing standing between a poll and a setpoint change.

## Redfish (BMC out-of-band management)

The DMTF Redfish specification defines a standard `Role` resource with a
`ReadOnly` privilege type: `Login` only, none of `ConfigureManager`,
`ConfigureComponents`, `ConfigureUsers` or `ConfigureSelf`. Every major BMC
vendor exposes this, under vendor-specific UI naming:

| Vendor | BMC | UI term | Redfish `RoleId` |
|---|---|---|---|
| Dell | iDRAC 9 | "Readonly" role group | `ReadOnly` |
| HPE | iLO 5 / iLO 6 | "Read-Only" privilege (no boxes checked under Configure...) | `ReadOnly` |
| Lenovo | XClarity Controller (XCC) | "Read Only" user authority | `ReadOnly` |

Create a dedicated account under this role for the collector - never reuse
an Administrator account "because it already works." A `ReadOnly` account
can still authenticate a `POST /redfish/v1/SessionService/Sessions` and
read every path this collector's mappings ask for; it cannot issue the
`PATCH`/`POST`/`DELETE` a firmware update, power-cycle, or fan-curve change
needs, so an operator typing the wrong password into the wrong tool fails
loudly instead of succeeding on hardware nobody meant to touch.

## SNMP

**v2c**: the community string itself is the entire access decision - most
implementations let a community be configured explicitly RO or RW (Cisco:
`snmp-server community <string> RO`). Provision a distinct RO community for
this collector; never reuse an RW one a write-capable tool also holds.

**v3** (docs/26 Phase 4's new USM support): read access is a VACM
(View-based Access Control Model) group-and-view assignment, independent of
the authPriv credential itself - a correct password does not imply write
access the way it can with v2c's single shared secret. Bind the collector's
security name to a group with a `read` view covering what it actually polls
and **no `write` view configured at all** (Cisco IOS: `snmp-server group
<name> v3 priv read <readview>`, with no `write` clause). A SET request
authenticated with valid v3 credentials but no write view is refused by the
agent regardless of how strong the auth/priv keys are.

## gNMI

Unlike Redfish, gNMI has no DMTF-style standard privilege model baked into
the protocol itself - authorization is whatever the underlying platform's
own AAA does with the username gNMI's metadata carries (this collector
sends `username`/`password` as gRPC metadata - see
`internal/adapters/gnmi/conn.go`'s `SetCredential`). Concretely:

- Arista EOS: `username <user> privilege <N> role <role>`, with `role`
  restricted to read commands (no `configure` access).
- Juniper Junos: a `login class` with `permissions view` (or `view-configuration`
  for visibility into config without edit rights), not `permissions all`.
- Cisco IOS-XR: a task group granting only read tasks.

Whatever the platform, provision the collector's gNMI account through the
device's normal AAA/RBAC path with read-only privilege, the same as any
other read-only CLI/NETCONF user on that device - gNMI rides on the same
authorization the vendor already has, it does not add a separate one.

## BACnet/IP and Modbus/TCP

Neither protocol has meaningful built-in authentication or authorization in
the deployments this collector talks to (BACnet Secure Connect and Modbus
Security/TLS exist as newer extensions but are rarely deployed on real
facility gear today). Enforcement here is procedural:

- **Modbus** distinguishes read and write at the function-code level (0x03/
  0x04 Read Holding/Input Registers vs 0x06/0x10 Write Single/Multiple
  Registers) - there is no account to restrict, only which function codes a
  client is permitted to issue. This collector issues only read function
  codes; if a serial gateway or PLC in front of it supports its own
  function-code allowlist, apply one there too as defence in depth.
- **BACnet/IP** similarly has no universal per-account ACL on real
  controllers; a device either implements `WriteProperty` or it does not.
  This collector never sends one. Where a BACnet router or BBMD supports
  restricting which service requests it forwards, apply that restriction
  for the collector's source address the same way.

## What this does not cover

Provisioning IPMI accounts (v2c-style shared "operator"/"user" privilege
levels, no v3-equivalent VACM) and any device-specific web-UI-only account
system this collector's protocol adapters do not reach - out of scope for
what the collector itself authenticates through.
