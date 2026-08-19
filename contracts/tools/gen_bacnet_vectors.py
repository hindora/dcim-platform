#!/usr/bin/env python3
"""Generate BACnet wire vectors from the simulator's own encoder.

Why this exists: a codec tested only against itself proves nothing. Every
encoder and decoder pair agrees with itself. These vectors are produced by the
DEVICE side - the simulator's `core.bacnet_object_model`, which is an
independent implementation of ASHRAE 135 - so the Go decoder is checked
against bytes it did not produce.

It runs in both directions:

  ACKS      the simulator builds responses; the Go decoder must read them.
  REQUESTS  the Go encoder writes requests to testdata/requests.json; this
            tool decodes each one with the simulator's request decoders and
            records what the DEVICE understood. The Go test then asserts the
            device read back what it meant to ask.

    python contracts/tools/gen_bacnet_vectors.py [--sim-root ../DCIM/...]

Regenerate whenever the codec or the simulator's encoder changes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "collector" / "internal" / "adapters" / "bacnet" / "testdata"
DEFAULT_SIM = ROOT.parent / "DCIM" / "Datacenter_Network_Simulator"


def load_sim(sim_root: Path):
    if not (sim_root / "core" / "bacnet_object_model.py").exists():
        sys.exit(f"simulator not found at {sim_root}\n"
                 f"pass --sim-root <path to Datacenter_Network_Simulator>")
    sys.path.insert(0, str(sim_root))
    import core.bacnet_object_model as m  # noqa: E402
    return m


def build_acks(m) -> list[dict]:
    """Response frames the device would send, with what they mean."""
    hexed = lambda b: b.hex()  # noqa: E731

    def complex_ack(invoke_id: int, service: int, body: bytes) -> bytes:
        apdu = bytes([(3 << 4), invoke_id, service]) + body
        return m.build_bvll(m.build_npdu(apdu))

    vectors: list[dict] = []

    # --- ReadProperty ack: present-value of an analog input (REAL) ---------
    body = (m.enc_ctx_oid(0, m.OBJ_ANALOG_INPUT, 4)
            + m.enc_ctx_uint(1, m.PROP_PRESENT_VALUE)
            + m.enc_ctx_open(3) + m.enc_app_real(22.5) + m.enc_ctx_close(3))
    vectors.append({
        "name": "read_property_present_value_real",
        "frame": hexed(complex_ack(7, 12, body)),
        "want": {"kind": "read_property", "object_type": 0, "instance": 4,
                 "property": 85, "values": [{"kind": "real", "num": 22.5}]},
    })

    # --- ReadProperty ack: object-name (character string) ------------------
    body = (m.enc_ctx_oid(0, m.OBJ_ANALOG_INPUT, 4)
            + m.enc_ctx_uint(1, m.PROP_OBJECT_NAME)
            + m.enc_ctx_open(3) + m.enc_app_charstr("Supply_Air_Temp")
            + m.enc_ctx_close(3))
    vectors.append({
        "name": "read_property_object_name",
        "frame": hexed(complex_ack(8, 12, body)),
        "want": {"kind": "read_property", "object_type": 0, "instance": 4,
                 "property": 77,
                 "values": [{"kind": "charstring", "text": "Supply_Air_Temp"}]},
    })

    # --- ReadProperty ack: binary input present-value is ENUMERATED --------
    # Not a real. A binary point's present-value is a state, and decoding it
    # as a measurement is how a running/stopped flag ends up on a chart.
    body = (m.enc_ctx_oid(0, m.OBJ_BINARY_INPUT, 1)
            + m.enc_ctx_uint(1, m.PROP_PRESENT_VALUE)
            + m.enc_ctx_open(3) + m.enc_app_enum(1) + m.enc_ctx_close(3))
    vectors.append({
        "name": "read_property_binary_present_value",
        "frame": hexed(complex_ack(9, 12, body)),
        "want": {"kind": "read_property", "object_type": 3, "instance": 1,
                 "property": 85, "values": [{"kind": "enumerated", "num": 1.0}]},
    })

    # --- ReadProperty ack: status-flags bit string -------------------------
    # in-alarm | fault | overridden | out-of-service, MSB first.
    body = (m.enc_ctx_oid(0, m.OBJ_ANALOG_INPUT, 4)
            + m.enc_ctx_uint(1, m.PROP_STATUS_FLAGS)
            + m.enc_ctx_open(3) + m.enc_app_bitstr(0b1000, 4) + m.enc_ctx_close(3))
    vectors.append({
        "name": "read_property_status_flags_in_alarm",
        "frame": hexed(complex_ack(10, 12, body)),
        "want": {"kind": "read_property", "object_type": 0, "instance": 4,
                 "property": 111, "values": [{"kind": "bitstring", "bits": 8}]},
    })

    # --- ReadProperty ack: object-list element 0 is the COUNT --------------
    body = (m.enc_ctx_oid(0, m.OBJ_DEVICE, 40001)
            + m.enc_ctx_uint(1, m.PROP_OBJECT_LIST) + m.enc_ctx_uint(2, 0)
            + m.enc_ctx_open(3) + m.enc_app_uint(14) + m.enc_ctx_close(3))
    vectors.append({
        "name": "read_property_object_list_count",
        "frame": hexed(complex_ack(11, 12, body)),
        "want": {"kind": "read_property", "object_type": 8, "instance": 40001,
                 "property": 76, "values": [{"kind": "unsigned", "num": 14.0}]},
    })

    # --- ReadProperty ack: whole object-list (an array of identifiers) -----
    oids = (m.enc_app_oid(m.OBJ_DEVICE, 40001)
            + m.enc_app_oid(m.OBJ_ANALOG_INPUT, 1)
            + m.enc_app_oid(m.OBJ_ANALOG_INPUT, 2)
            + m.enc_app_oid(m.OBJ_BINARY_INPUT, 1))
    body = (m.enc_ctx_oid(0, m.OBJ_DEVICE, 40001)
            + m.enc_ctx_uint(1, m.PROP_OBJECT_LIST)
            + m.enc_ctx_open(3) + oids + m.enc_ctx_close(3))
    vectors.append({
        "name": "read_property_object_list_whole",
        "frame": hexed(complex_ack(12, 12, body)),
        "want": {"kind": "read_property", "object_type": 8, "instance": 40001,
                 "property": 76,
                 "values": [{"kind": "objectid", "object_type": 8, "instance": 40001},
                            {"kind": "objectid", "object_type": 0, "instance": 1},
                            {"kind": "objectid", "object_type": 0, "instance": 2},
                            {"kind": "objectid", "object_type": 3, "instance": 1}]},
    })

    # --- RPM ack: several objects, one of them a per-property error --------
    # The point of the vector: one missing point must not cost the caller the
    # other results in the same response.
    def rpm_result(prop_id: int, value_bytes: bytes) -> bytes:
        return (m.enc_ctx_open(2) + m.enc_ctx_uint(0, prop_id)
                + m.enc_ctx_open(4) + value_bytes + m.enc_ctx_close(4)
                + m.enc_ctx_close(2))

    def rpm_error(prop_id: int, cls: int, code: int) -> bytes:
        return (m.enc_ctx_open(2) + m.enc_ctx_uint(0, prop_id)
                + m.enc_ctx_open(5) + m.enc_app_enum(cls) + m.enc_app_enum(code)
                + m.enc_ctx_close(5) + m.enc_ctx_close(2))

    body = b""
    body += m.enc_ctx_oid(0, m.OBJ_ANALOG_INPUT, 1) + m.enc_ctx_open(1)
    body += rpm_result(m.PROP_PRESENT_VALUE, m.enc_app_real(7.2))
    body += rpm_result(m.PROP_OBJECT_NAME, m.enc_app_charstr("CHW_Supply_Temp"))
    body += m.enc_ctx_close(1)
    body += m.enc_ctx_oid(0, m.OBJ_ANALOG_INPUT, 99) + m.enc_ctx_open(1)
    body += rpm_error(m.PROP_PRESENT_VALUE, 1, 31)   # object / unknown-object
    body += m.enc_ctx_close(1)
    body += m.enc_ctx_oid(0, m.OBJ_BINARY_INPUT, 2) + m.enc_ctx_open(1)
    body += rpm_result(m.PROP_PRESENT_VALUE, m.enc_app_enum(0))
    body += m.enc_ctx_close(1)
    vectors.append({
        "name": "rpm_mixed_values_and_error",
        "frame": hexed(complex_ack(13, 14, body)),
        "want": {"kind": "rpm", "results": [
            {"object_type": 0, "instance": 1, "property": 85,
             "values": [{"kind": "real", "num": 7.2}]},
            {"object_type": 0, "instance": 1, "property": 77,
             "values": [{"kind": "charstring", "text": "CHW_Supply_Temp"}]},
            {"object_type": 0, "instance": 99, "property": 85,
             "error": {"class": 1, "code": 31}},
            {"object_type": 3, "instance": 2, "property": 85,
             "values": [{"kind": "enumerated", "num": 0.0}]},
        ]},
    })

    # --- Error PDU ---------------------------------------------------------
    apdu = (bytes([(5 << 4), 14, 12]) + m.enc_app_enum(1) + m.enc_app_enum(31))
    vectors.append({
        "name": "error_unknown_object",
        "frame": hexed(m.build_bvll(m.build_npdu(apdu))),
        "want": {"kind": "error", "class": 1, "code": 31},
    })

    # --- Reject and Abort --------------------------------------------------
    vectors.append({
        "name": "reject_invalid_tag",
        "frame": hexed(m.build_bvll(m.build_npdu(bytes([(6 << 4), 15, 5])))),
        "want": {"kind": "reject", "reason": 5},
    })
    vectors.append({
        "name": "abort_buffer_overflow",
        "frame": hexed(m.build_bvll(m.build_npdu(bytes([(7 << 4), 16, 1])))),
        "want": {"kind": "abort", "reason": 1},
    })

    # --- I-Am --------------------------------------------------------------
    apdu = (bytes([(1 << 4), 0])
            + m.enc_app_oid(m.OBJ_DEVICE, 40007)
            + m.enc_app_uint(1476)
            + m.enc_app_enum(3)          # segmentation: no-segmentation
            + m.enc_app_uint(999))
    vectors.append({
        "name": "i_am",
        "frame": hexed(m.build_bvll(m.build_npdu(apdu), broadcast=True)),
        "want": {"kind": "i_am", "instance": 40007, "max_apdu": 1476,
                 "vendor": 999},
    })

    # --- A routed reply: the same ack re-emitted from behind an MS/TP router.
    # Without reading SNET/SADR every device on a trunk looks like the router.
    body = (m.enc_ctx_oid(0, m.OBJ_ANALOG_INPUT, 2)
            + m.enc_ctx_uint(1, m.PROP_PRESENT_VALUE)
            + m.enc_ctx_open(3) + m.enc_app_real(61.5) + m.enc_ctx_close(3))
    plain = complex_ack(17, 12, body)
    routed = m.with_source_route(plain, 2001, bytes([12]))
    vectors.append({
        "name": "routed_read_property_from_mstp",
        "frame": hexed(routed),
        "want": {"kind": "read_property", "object_type": 0, "instance": 2,
                 "property": 85, "values": [{"kind": "real", "num": 61.5}],
                 "src_net": 2001, "src_mac": "0c"},
    })

    return vectors


def decode_requests(m, requests: list[dict]) -> list[dict]:
    """Decode Go-generated requests with the DEVICE's own parsers."""
    out = []
    for req in requests:
        raw = bytes.fromhex(req["frame"])
        func, npdu = m.parse_bvll(raw)
        if func is None:
            sys.exit(f"{req['name']}: not a BVLL frame")
        parsed = m.parse_npdu_routed(npdu)
        if parsed is None:
            sys.exit(f"{req['name']}: the device rejected the NPDU")
        apdu = m.parse_apdu(parsed["apdu"])
        if apdu is None:
            sys.exit(f"{req['name']}: the device could not parse the APDU")

        rec = {"name": req["name"], "type": apdu["type"],
               "service": apdu["service"],
               "invoke_id": apdu.get("invoke_id", -1),
               # What the ROUTER saw. DADR is the only field that says which
               # device on an MS/TP trunk a request is addressed to.
               "dnet": -1 if parsed["dnet"] is None else parsed["dnet"],
               "dadr": "" if not parsed["dadr"] else parsed["dadr"].hex()}

        if apdu["service"] == m.SVC_READ_PROPERTY and apdu["type"] == "confirmed":
            got = m.decode_read_property(apdu["data"])
            if got is None:
                sys.exit(f"{req['name']}: decode_read_property failed")
            rec["object_type"], rec["instance"], rec["property"], idx = got
            rec["array_index"] = -1 if idx is None else idx
        elif apdu["service"] == m.SVC_READ_PROPERTY_MULTIPLE:
            items = m.decode_read_property_multiple(apdu["data"])
            if not items:
                sys.exit(f"{req['name']}: decode_read_property_multiple failed")
            rec["items"] = [
                {"object_type": it["obj_type"], "instance": it["obj_inst"],
                 "properties": [p for p, _ in it["props"]]}
                for it in items
            ]
        elif apdu["service"] == m.SVC_WHO_IS and apdu["type"] == "unconfirmed":
            low, high = m.decode_whois(apdu["data"])
            rec["low"] = -1 if low is None else low
            rec["high"] = -1 if high is None else high
        elif apdu["service"] == m.SVC_SUBSCRIBE_COV:
            sub = m.decode_subscribe_cov(apdu["data"])
            if sub is None:
                sys.exit(f"{req['name']}: decode_subscribe_cov failed")
            rec.update({"process_id": sub["process_id"],
                        "object_type": sub["obj_type"],
                        "instance": sub["obj_inst"],
                        "confirmed": bool(sub["confirmed"]),
                        "lifetime": sub["lifetime"]})
        out.append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim-root", default=str(DEFAULT_SIM),
                    help="path to the Datacenter_Network_Simulator checkout")
    args = ap.parse_args()

    m = load_sim(Path(args.sim_root).resolve())
    OUT.mkdir(parents=True, exist_ok=True)

    acks = build_acks(m)
    (OUT / "acks.json").write_text(
        json.dumps({"generated_from": "simulator core.bacnet_object_model",
                    "vectors": acks}, indent=2) + "\n", encoding="utf-8")
    print(f"acks.json: {len(acks)} vectors")

    req_path = OUT / "requests.json"
    if req_path.exists():
        requests = json.loads(req_path.read_text(encoding="utf-8"))["requests"]
        decoded = decode_requests(m, requests)
        (OUT / "requests_decoded.json").write_text(
            json.dumps({"decoded_by": "simulator core.bacnet_object_model",
                        "requests": decoded}, indent=2) + "\n", encoding="utf-8")
        print(f"requests_decoded.json: {len(decoded)} requests the device understood")
    else:
        print(f"no {req_path.name} yet - run the Go generator test first:")
        print("  DCIM_GEN_VECTORS=1 go test ./internal/adapters/bacnet/ -run Generate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
