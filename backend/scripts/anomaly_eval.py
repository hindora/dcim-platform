#!/usr/bin/env python3
"""Score anomaly detectors against the simulator's injected ground truth.

docs/33 section 4. Three commands:

    score  --campaign NAME   one scenario's runs: recall, precision, delays
    soak   --days N          false findings per device-day on a clean soak
    replay --from --to       re-score a stored window (Phase 3; VUS-PR)

Phase 0 skeleton: inputs are files. `--ledger` is a saved
`GET /api/faults/ledger` response from the simulator, `--detections` a JSON
list of findings/derived alerts (id, detector_id, device_id, opened_at, ...).
Fetching both live (the simulator at 127.0.0.1:8001 as tools/_live_api.py
authenticates, and /api/v1/anomalies/*) arrives when there is something to
fetch: the ledger with simulator S0, findings with Phase 1.

The scenario expectations come from the simulator repo's
tools/anomaly_campaigns.json (docs/32 section 13.1a). Its location is
`--campaigns`, else $DCIM_ANOMALY_CAMPAIGNS, else the sibling checkouts this
estate uses (../DCIM/Datacenter_Network_Simulator and
../Datacenter_Network_Simulator next to this repo). The file ships with
simulator task S3-1; until then this exits 2 and says where it looked.

    cd backend && .venv/Scripts/python scripts/anomaly_eval.py score \\
        --campaign crah-supply-bias --ledger ledger.json --detections found.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analytics import evaluation as ev

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "var" / "anomaly-eval"


def campaign_candidates(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    if os.environ.get("DCIM_ANOMALY_CAMPAIGNS"):
        return [Path(os.environ["DCIM_ANOMALY_CAMPAIGNS"])]
    rel = Path("tools") / "anomaly_campaigns.json"
    return [REPO.parent / "DCIM" / "Datacenter_Network_Simulator" / rel,
            REPO.parent / "Datacenter_Network_Simulator" / rel]


def load_campaigns(explicit: str | None) -> dict[str, ev.Scenario] | None:
    tried = campaign_candidates(explicit)
    for path in tried:
        try:
            return ev.load_campaigns(path)
        except ev.CampaignsUnavailableError:
            continue
    print("anomaly_campaigns.json not found (simulator task S3-1 ships it). Looked in:",
          file=sys.stderr)
    for p in tried:
        print(f"  {p}", file=sys.stderr)
    return None


def _read_json(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_score(args: argparse.Namespace) -> int:
    scenarios = load_campaigns(args.campaigns)
    if scenarios is None:
        return 2
    scenario = scenarios.get(args.campaign)
    if scenario is None:
        print(f"unknown scenario {args.campaign!r}; known: {', '.join(sorted(scenarios))}",
              file=sys.stderr)
        return 2
    if not args.ledger or not args.detections:
        print("Phase 0: pass --ledger and --detections files (live fetch is not built yet)",
              file=sys.stderr)
        return 2
    injections = ev.parse_ledger(_read_json(args.ledger))
    detections = [ev.Detection.from_json(d) for d in _read_json(args.detections)]
    score = ev.score_scenario(scenario, injections, detections,
                              tolerance_s=args.tolerance_s, check_kind=args.check_kind)
    path = ev.write_report(score, Path(args.out))
    print(json.dumps({"report": str(path), "recall": score.recall,
                      "precision": score.precision, "runs": score.runs,
                      "delay_median_s": score.delay_median_s,
                      "gated": score.gated}, indent=2))
    return 0


def cmd_soak(args: argparse.Namespace) -> int:
    if not args.detections:
        print("Phase 0: pass --detections (and --ledger for context windows)", file=sys.stderr)
        return 2
    injections = ev.parse_ledger(_read_json(args.ledger)) if args.ledger else []
    detections = ev.dedupe_episodes(
        [ev.Detection.from_json(d) for d in _read_json(args.detections)], gap_s=3600)
    false = ev.false_detections(detections, injections, None, ())
    rate = ev.false_per_device_day(len(false), args.devices, args.days)
    print(json.dumps({"days": args.days, "devices": args.devices, "false": len(false),
                      "false_per_device_day": rate,
                      "at": datetime.now(UTC).isoformat()}, indent=2))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    print("replay scores stored L3/L4 windows and arrives with Phase 3 baselines",
          file=sys.stderr)
    return 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--campaigns", help="path to anomaly_campaigns.json")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="report directory")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("score", help="score one scenario")
    s.add_argument("--campaign", required=True)
    s.add_argument("--ledger", help="saved GET /api/faults/ledger response")
    s.add_argument("--detections", help="JSON list of findings / derived alerts")
    s.add_argument("--tolerance-s", type=float, default=0.0,
                   help="one poll interval of the slowest required metric")
    s.add_argument("--check-kind", action="store_true",
                   help="require the finding kind to match (Phase 4 on)")
    s.set_defaults(fn=cmd_score)

    k = sub.add_parser("soak", help="false findings per device-day on a clean soak")
    k.add_argument("--days", type=float, default=7.0)
    k.add_argument("--devices", type=int, default=716)
    k.add_argument("--ledger")
    k.add_argument("--detections")
    k.set_defaults(fn=cmd_soak)

    r = sub.add_parser("replay", help="re-score a stored window (Phase 3)")
    r.add_argument("--from", dest="since")
    r.add_argument("--to", dest="until")
    r.set_defaults(fn=cmd_replay)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
