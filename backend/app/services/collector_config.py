"""What a collector may be told from here, and what it may not.

The collector reads two configurations. Its FILE carries the things that let it
reach this platform at all - its own id, the API address, the token, Redis -
and those are deliberately not settable from this page. Break the path to the
control plane remotely and you cannot repair it remotely; somebody drives to
the site. Zabbix draws the same line: a proxy's ServerActive lives in its
config file and nothing in the frontend can move it.

Everything below is operational: how hard to poll, how long to wait, which
planes are on, and where the inbound listeners sit. Those belong to whoever
runs the estate rather than to whoever has a shell on the collector host.

WHAT APPLIES WHEN
-----------------
Most of these are read once, when the adapters are built. Saving one changes
what the next process does and nothing about the running one, so each field is
marked with when it takes effect and the UI says so rather than implying every
save reached the wire.

The SNMP trap receiver is the exception, and it is the one worth having live:
it owns its socket and its workers, so it can be closed and reopened in place.
"""

from __future__ import annotations

from typing import Any

#: Applied without a restart.
LIVE = "live"
#: Stored now, in force when the collector next starts.
ON_RESTART = "restart"


def _f(label: str, kind: str, when: str, help: str, **extra: Any
       ) -> dict[str, Any]:
    return {"label": label, "kind": kind, "when": when, "help": help, **extra}


#: Per-protocol tunables. Same shape for every plane, because the questions are
#: the same: is it on, how many at once, how many per host, how long to wait.
def _protocol(name: str, note: str) -> dict[str, dict[str, Any]]:
    return {
        "enabled": _f(f"{name} enabled", "bool", ON_RESTART, note),
        "max_concurrent": _f(
            "Max concurrent", "int", ON_RESTART,
            "Endpoints of this protocol polled at once across the whole "
            "collector.", min=1, max=512),
        "per_host": _f(
            "Per host", "int", ON_RESTART,
            "Requests in flight to ONE host. A BMC serialises them regardless, "
            "and a serial gateway forwards one transaction at a time - "
            "parallel requests only queue inside it, where nothing can see "
            "the delay.", min=1, max=32),
        "timeout_s": _f(
            "Timeout", "seconds", ON_RESTART,
            "How long to wait for one answer.", min=1, max=120),
        "retries": _f(
            "Retries", "int", ON_RESTART,
            "Attempts after the first. A device that answered with an "
            "exception answered; repeating gets the same exception.",
            min=0, max=5),
    }


SCHEMA: dict[str, dict[str, Any]] = {
    "snmp": {
        "title": "SNMP",
        "fields": _protocol(
            "SNMP",
            "Polling of servers, BMCs, PDUs and network gear. Off means none "
            "of them is read at all."),
    },
    "redfish": {
        "title": "Redfish",
        "fields": _protocol(
            "Redfish",
            "HTTPS to each BMC. The handshake is the cost, not the payload."),
    },
    "bacnet": {
        "title": "BACnet/IP",
        "fields": {
            **_protocol(
                "BACnet",
                "Every CRAH, CDU, chiller, tower, pump and valve carries its "
                "readings on BACnet only. Off means the cooling plant is not "
                "measured at all - the counter reads zero because nothing was "
                "asked, not because the plant is well."),
            "local_port": _f(
                "Local port", "int", ON_RESTART,
                "0 asks the kernel for an ephemeral port, which is enough for "
                "replies. 47808 (0xBAC0) is needed only to RECEIVE broadcasts "
                "- Who-Is, I-Am, COV - and fails to bind if anything else on "
                "the host already speaks BACnet.", min=0, max=65535),
        },
    },
    "modbus": {
        "title": "Modbus/TCP",
        "fields": _protocol(
            "Modbus",
            "UPS, switchgear, generators, ATS, branch-circuit monitors and the "
            "plant instruments behind the gateways."),
    },
    "gnmi": {
        "title": "gNMI",
        "fields": {
            # No retries. A Get is one RPC over a pooled connection and a
            # Subscribe reconnects rather than retrying, so the collector has
            # no such setting - offering one would store a value nothing reads,
            # which is worse than not offering it.
            **{k: v for k, v in _protocol(
                "gNMI",
                "Interface state and counters for the fabric.").items()
               if k != "retries"},
            "stream": _f(
                "Subscribe", "bool", ON_RESTART,
                "Use Subscribe for endpoints whose profile says the device "
                "pushes. Off falls back to polling them, which collects the "
                "same data far more expensively."),
        },
    },
    "snmp_trap": {
        "title": "SNMP traps",
        "danger": "listener",
        "fields": {
            "enabled": _f(
                "Trap receiver enabled", "bool", LIVE,
                "Off closes the socket. Devices keep sending; the datagrams "
                "go nowhere and nothing records that they did."),
            "listen": _f(
                "Listen address", "listen", LIVE,
                "Where traps are received. 162 is the standard and needs "
                "either root or CAP_NET_BIND_SERVICE "
                "(setcap 'cap_net_bind_service=+ep' ./bin/collector); 1162 is "
                "the unprivileged alternative. Every device must be told the "
                "same address - a trap sent to the old port is not an error "
                "anywhere, it is silence."),
            "workers": _f(
                "Workers", "int", LIVE,
                "Decoders behind the socket. Packets are queued, not dropped, "
                "while these are busy.", min=1, max=64),
            "rate_limit_per_minute": _f(
                "Rate limit per source", "int", LIVE,
                "One flapping interface must not fill the stream or the disk.",
                min=1, max=10_000),
        },
    },
    "redfish_event": {
        "title": "Redfish events",
        "danger": "listener",
        "fields": {
            "enabled": _f(
                "Event receiver enabled", "bool", ON_RESTART,
                "Pushed BMC events. Off by default because the advertise "
                "address has no safe default."),
            "listen": _f(
                "Listen address", "listen", ON_RESTART,
                "Where this collector accepts event POSTs."),
            "advertise": _f(
                "Advertise address", "text", ON_RESTART,
                "host:port the BMCs are TOLD to post to, which is not "
                "necessarily where this process listens. The collector cannot "
                "work it out: on a multi-homed host a wrong guess creates "
                "every subscription successfully and then delivers nothing, "
                "with no error at either end."),
            "tls": _f(
                "TLS on the destination", "bool", ON_RESTART,
                "Firmware that cannot verify the receiver's certificate drops "
                "events silently, so leave this off until the BMCs trust it."),
        },
    },
}

#: Every settable path, as "section.field".
PATHS = {f"{section}.{field}"
         for section, spec in SCHEMA.items()
         for field in spec["fields"]}

#: Sections whose changes reach a running collector.
LIVE_PATHS = {f"{section}.{field}"
              for section, spec in SCHEMA.items()
              for field, f in spec["fields"].items() if f["when"] == LIVE}


class CollectorConfigError(ValueError):
    """A rejected setting, with a message written for the operator."""


def validate(config: dict[str, Any]) -> dict[str, Any]:
    """Check a whole config document and return the cleaned version."""
    out: dict[str, Any] = {}
    for section, values in config.items():
        spec = SCHEMA.get(section)
        if spec is None:
            raise CollectorConfigError(f"'{section}' is not a configurable part "
                                       f"of the collector")
        if not isinstance(values, dict):
            raise CollectorConfigError(f"{section} takes a set of settings")
        clean: dict[str, Any] = {}
        for key, raw in values.items():
            field = spec["fields"].get(key)
            if field is None:
                raise CollectorConfigError(
                    f"{spec['title']} has no '{key}' setting")
            if raw is None:
                continue                       # falls back to the file's value
            clean[key] = _value(f"{spec['title']} {field['label']}", field, raw)
        if clean:
            out[section] = clean
    return out


def _value(label: str, field: dict[str, Any], raw: Any) -> Any:
    kind = field["kind"]
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in ("true", "false"):
            return str(raw).lower() == "true"
        raise CollectorConfigError(f"{label} is on or off")

    if kind in ("int", "seconds"):
        try:
            n = int(raw)
        except (TypeError, ValueError):
            raise CollectorConfigError(f"{label} must be a whole number") from None
        lo, hi = field.get("min", 0), field.get("max", 2**31 - 1)
        if not (lo <= n <= hi):
            raise CollectorConfigError(f"{label} must be between {lo} and {hi}")
        return n

    if kind == "listen":
        return _listen(label, str(raw))

    text = str(raw).strip()
    if len(text) > 255:
        raise CollectorConfigError(f"{label} is too long")
    return text


def _listen(label: str, raw: str) -> str:
    """host:port, with the port checked and the host left alone.

    The host half is genuinely free: 0.0.0.0 to accept on every interface, or
    one address to accept on exactly the management network and nowhere else,
    which is a real hardening choice on a multi-homed collector.
    """
    text = raw.strip()
    host, sep, port = text.rpartition(":")
    if not sep:
        raise CollectorConfigError(
            f"{label} is host:port - 0.0.0.0:1162 to accept on every "
            f"interface, or one address to accept only there")
    try:
        n = int(port)
    except ValueError:
        raise CollectorConfigError(f"{label} has no port number") from None
    if not (1 <= n <= 65535):
        raise CollectorConfigError(f"{label} port must be between 1 and 65535")
    return f"{host}:{n}"


def restart_needed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Which changed settings only a restart will pick up.

    The answer belongs on screen at the moment of saving. A page that reports
    "saved" for a value the running process will never read is telling the
    operator the estate changed when it did not.
    """
    pending: list[str] = []
    for section, spec in SCHEMA.items():
        for key, field in spec["fields"].items():
            if field["when"] == LIVE:
                continue
            old = (before.get(section) or {}).get(key)
            new = (after.get(section) or {}).get(key)
            if old != new and new is not None:
                pending.append(f"{spec['title']} · {field['label']}")
    return pending


def describe() -> dict[str, Any]:
    """The schema, for a UI that would otherwise hard-code all of this."""
    return {
        "sections": [
            {"key": key, "title": spec["title"],
             "danger": spec.get("danger"),
             "fields": [{"key": f, **spec["fields"][f]}
                        for f in spec["fields"]]}
            for key, spec in SCHEMA.items()
        ],
    }
