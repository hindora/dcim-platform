"""Scoring anomaly detectors against injected ground truth (docs/33 section 4).

Pure functions, tested in CI against fixtures; the CLI that feeds them is
backend/scripts/anomaly_eval.py. Contract deviation D12: the scorer had no
home in the contract, so it lives here.

Inputs, all plain data so a fixture can stand in for each:

* **Scenarios** from the simulator's `tools/anomaly_campaigns.json` (docs/32
  section 13.1a) - the one file both repos read, so the expectations cannot
  drift between them.
* **Ledger entries** from the simulator's `GET /api/faults/ledger` (docs/32
  section 7.4), joined to a scenario run by `campaign = "<scenario>/<run_id>"`.
* **Detections** - findings and derived alerts from the platform.

Matching (section 4): a run detects its injection when a detection opens
inside the expected window from onset, +/- one poll interval of the slowest
required metric; names the root device (or belongs to an incident rooted
there); comes from an expected detector; and, from Phase 4, carries the
expected `kind`. Repeated raises of one (detector, device, instance) episode
count once. Ledger windows tagged as context are excluded from both the
numerator and the denominator.

Runs are statistically repeatable, not bit-exact, so delays are reported as
a median and a range across runs, never as one run's value.
"""

from __future__ import annotations

import csv
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Ledger kinds that are context, never an injection to detect (docs/32 7.1):
#: they are excluded from scoring rather than counted either way.
CONTEXT_KINDS = frozenset({"context"})


def _ts(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --- inputs ----------------------------------------------------------------------

@dataclass(frozen=True)
class Expectation:
    detector: str
    kind: str | None = None
    #: 'injected_device' (the only root the Phase 0-3 scenarios use) or a
    #: device id / name resolved by the runner.
    root: str = "injected_device"
    window_min_s: float = 0.0
    window_max_s: float = math.inf
    calibrated: bool = False


@dataclass(frozen=True)
class Scenario:
    name: str
    tier: str = "abrupt"
    runs: int = 1
    pending: str | None = None
    expected: tuple[Expectation, ...] = ()
    expected_misses: frozenset[str] = frozenset()
    must_not: frozenset[str] = frozenset()
    enable_for_run: tuple[str, ...] = ()

    @property
    def expected_detectors(self) -> frozenset[str]:
        return frozenset(e.detector for e in self.expected)

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Scenario:
        exps = []
        for e in d.get("expected") or ():
            w = e.get("window_s") or {}
            exps.append(Expectation(
                detector=e["detector"], kind=e.get("kind"),
                root=e.get("root") or "injected_device",
                window_min_s=float(w.get("min", 0.0)),
                window_max_s=float(w["max"]) if w.get("max") is not None else math.inf,
                calibrated=bool(w.get("calibrated", False))))
        return cls(name=d["name"], tier=d.get("tier", "abrupt"),
                   runs=int(d.get("runs", 1)), pending=d.get("pending"),
                   expected=tuple(exps),
                   expected_misses=frozenset(d.get("expected_misses") or ()),
                   must_not=frozenset(d.get("must_not") or ()),
                   enable_for_run=tuple(d.get("enable_for_run") or ()))


class CampaignsUnavailableError(FileNotFoundError):
    """The simulator's campaigns file is not where it was looked for."""


def load_campaigns(path: Path) -> dict[str, Scenario]:
    """Read anomaly_campaigns.json. Raises CampaignsUnavailableError if absent:
    the file ships with simulator task S3-1 and may not exist yet."""
    if not path.is_file():
        raise CampaignsUnavailableError(str(path))
    doc = json.loads(path.read_text(encoding="utf-8"))
    if int(doc.get("version", 0)) != 1:
        raise ValueError(f"unsupported anomaly_campaigns.json version {doc.get('version')!r}")
    return {s["name"]: Scenario.from_json(s) for s in doc.get("scenarios") or ()}


@dataclass(frozen=True)
class Injection:
    id: str
    kind: str
    device_id: str | None
    start: datetime
    end: datetime | None = None
    expected_kind: str | None = None
    scenario: str | None = None
    run_id: str | None = None
    context: str | None = None

    @property
    def is_context(self) -> bool:
        return self.kind in CONTEXT_KINDS or bool(self.context)

    def covers(self, t: datetime) -> bool:
        return self.start <= t and (self.end is None or t < self.end)

    @classmethod
    def from_ledger(cls, e: Mapping[str, Any]) -> Injection:
        scenario = run_id = None
        if e.get("campaign"):
            scenario, _, run_id = str(e["campaign"]).partition("/")
        start = _ts(e["start"])
        if start is None:
            raise ValueError(f"ledger entry {e.get('id')!r} has no start")
        return cls(id=e["id"], kind=e.get("kind") or "", device_id=e.get("device_id"),
                   start=start, end=_ts(e.get("end")),
                   expected_kind=e.get("expected_kind"),
                   scenario=scenario, run_id=run_id or None,
                   context=e.get("context"))


def parse_ledger(doc: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> list[Injection]:
    """A GET /faults/ledger response ({"entries": [...]}) or a bare list."""
    entries = doc.get("entries", []) if isinstance(doc, Mapping) else doc
    return sorted((Injection.from_ledger(e) for e in entries), key=lambda i: i.start)


@dataclass(frozen=True)
class Detection:
    """A finding or a derived alert, as the scorer sees it."""

    id: str
    detector_id: str
    device_id: str
    opened_at: datetime
    instance: str = ""
    detector_version: int = 1
    kind: str | None = None
    incident_root_device_id: str | None = None
    source: str = "finding"

    @property
    def episode_key(self) -> tuple[str, str, str]:
        return (self.detector_id, self.device_id, self.instance)

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> Detection:
        opened = _ts(d.get("opened_at") or d.get("raised_at") or d.get("first_seen"))
        if opened is None:
            raise ValueError(f"detection {d.get('id')!r} has no open time")
        return cls(id=str(d["id"]), detector_id=d.get("detector_id") or d["detector"],
                   device_id=str(d["device_id"]), opened_at=opened,
                   instance=d.get("instance") or "",
                   detector_version=int(d.get("detector_version") or 1),
                   kind=d.get("kind"),
                   incident_root_device_id=d.get("incident_root_device_id"),
                   source=d.get("source") or "finding")


# --- matching ----------------------------------------------------------------------

def dedupe_episodes(detections: Iterable[Detection],
                    gap_s: float | None = None) -> list[Detection]:
    """Repeated raises of one (detector, device, instance) episode count once.

    With `gap_s` None every raise of a key is one episode (a single run).
    With a gap, a raise more than `gap_s` after the previous raise of the same
    key starts a new episode - for a soak, where one key may genuinely fire on
    two separate days.
    """
    out: list[Detection] = []
    last: dict[tuple[str, str, str], datetime] = {}
    for d in sorted(detections, key=lambda x: x.opened_at):
        prev = last.get(d.episode_key)
        last[d.episode_key] = d.opened_at
        if prev is not None and (gap_s is None or
                                 (d.opened_at - prev).total_seconds() <= gap_s):
            continue
        out.append(d)
    return out


def names_root(d: Detection, inj: Injection) -> bool:
    return inj.device_id is not None and (
        d.device_id == inj.device_id or d.incident_root_device_id == inj.device_id)


@dataclass(frozen=True)
class Match:
    injection_id: str
    detected: bool
    detection_id: str | None = None
    detector_id: str | None = None
    detector_version: int | None = None
    delay_s: float | None = None
    kind_ok: bool | None = None


def match_injection(inj: Injection, detections: Iterable[Detection],
                    scenario: Scenario, *, tolerance_s: float = 0.0,
                    check_kind: bool = False) -> Match:
    """The earliest detection that satisfies every rule, or a miss."""
    by_det = {e.detector: e for e in scenario.expected}
    best: tuple[float, Detection, bool] | None = None
    for d in detections:
        exp = by_det.get(d.detector_id)
        if exp is None or not names_root(d, inj):
            continue
        delay = (d.opened_at - inj.start).total_seconds()
        if delay < exp.window_min_s - tolerance_s or delay > exp.window_max_s + tolerance_s:
            continue
        want_kind = exp.kind or inj.expected_kind
        kind_ok = want_kind is None or d.kind == want_kind
        if check_kind and not kind_ok:
            continue
        if best is None or delay < best[0]:
            best = (delay, d, kind_ok)
    if best is None:
        return Match(inj.id, False)
    delay, d, kind_ok = best
    return Match(inj.id, True, d.id, d.detector_id, d.detector_version, delay, kind_ok)


def in_context(t: datetime, context_windows: Iterable[Injection]) -> bool:
    return any(w.covers(t) for w in context_windows)


def false_detections(detections: Iterable[Detection], injections: Sequence[Injection],
                     scenario: Scenario | None, matched_ids: Iterable[str],
                     absorbed_ids: Iterable[str] = ()) -> list[Detection]:
    """Detections that count against precision (docs/33 section 4).

    False when it matches no injection and is not absorbed as a symptom of a
    correctly rooted incident, or when its detector is in the scenario's
    `must_not` set - even if it happens to sit on the injected device.
    Detections inside a context window are excluded, not counted. On a clean
    soak (`scenario` None, no injections) every detection outside context is
    false.
    """
    matched, absorbed = set(matched_ids), set(absorbed_ids)
    ctx = [i for i in injections if i.is_context]
    out = []
    for d in detections:
        if in_context(d.opened_at, ctx):
            continue
        if scenario is not None and d.detector_id in scenario.must_not:
            out.append(d)
            continue
        if d.id in matched or d.id in absorbed:
            continue
        out.append(d)
    return out


# --- figures -----------------------------------------------------------------------

def percentile(xs: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, q in [0, 100]."""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return float(s[0])
    pos = (len(s) - 1) * q / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    return float(s[lo] + (s[hi] - s[lo]) * (pos - lo))


def lead_over_l1(detected_at: datetime, l1_times: Iterable[datetime]) -> float | None:
    """Seconds from detection to the first L1 alarm on the chain.

    Positive: the detector was early. Negative is expected for abrupt
    diagnoses (docs/32 13.2). None: no L1 alarm ever fired ("No L1").
    """
    first = min(l1_times, default=None)
    if first is None:
        return None
    return (first - detected_at).total_seconds()


def false_per_device_day(false_count: int, devices: int, days: float) -> float | None:
    if devices <= 0 or days <= 0:
        return None
    return false_count / (devices * days)


def compression_ratio(raw_findings: int, incidents: int) -> float | None:
    """Raw findings / incidents. None with no incidents (undefined, not 0)."""
    if incidents <= 0:
        return None
    return raw_findings / incidents


@dataclass
class ScenarioScore:
    scenario: str
    runs: int
    detected_runs: int
    #: Recall over the runs; a 100% gate on an abrupt scenario means 3 of 3.
    recall: float | None
    precision: float | None
    delay_median_s: float | None
    delay_p90_s: float | None
    delay_range_s: tuple[float, float] | None
    false_ids: list[str] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)
    #: 'pending' scenarios are runnable but not gated (docs/32 13.1a).
    gated: bool = True
    by_detector: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["delay_range_s"] = list(self.delay_range_s) if self.delay_range_s else None
        return d


def score_scenario(scenario: Scenario, injections: Sequence[Injection],
                   detections: Sequence[Detection], *, tolerance_s: float = 0.0,
                   check_kind: bool = False,
                   absorbed_ids: Iterable[str] = ()) -> ScenarioScore:
    """Score every run of one scenario.

    `injections` may hold the whole ledger; only entries whose campaign names
    this scenario are scored, and context windows are applied from all of
    them. One run is one (run_id); a run with several injections is detected
    when every non-context injection of it is.
    """
    ctx = [i for i in injections if i.is_context]
    mine = [i for i in injections if i.scenario == scenario.name and not i.is_context]
    dets = sorted((d for d in detections if not in_context(d.opened_at, ctx)),
                  key=lambda d: d.opened_at)

    runs: dict[str, list[Injection]] = {}
    for inj in mine:
        runs.setdefault(inj.run_id or inj.id, []).append(inj)

    matches = [match_injection(i, dets, scenario, tolerance_s=tolerance_s,
                               check_kind=check_kind) for i in mine]
    by_inj = {m.injection_id: m for m in matches}
    detected_runs = sum(1 for injs in runs.values()
                        if injs and all(by_inj[i.id].detected for i in injs))
    delays = [m.delay_s for m in matches if m.detected and m.delay_s is not None]
    matched_ids = {m.detection_id for m in matches if m.detection_id}

    # Repeated raises of a matched episode are that episode, not false
    # findings: same (detector, device, instance) while its injection is open.
    by_id = {d.id: d for d in dets}
    episodes = []
    for inj, m in zip(mine, matches, strict=True):
        if m.detection_id:
            episodes.append((by_id[m.detection_id].episode_key, inj))
    repeat_ids = {d.id for d in dets if d.id not in matched_ids and any(
        d.episode_key == key and d.opened_at >= inj.start
        and (inj.end is None or d.opened_at < inj.end) for key, inj in episodes)}
    remaining = dedupe_episodes(d for d in dets
                                if d.id not in matched_ids and d.id not in repeat_ids)
    false = false_detections(remaining, injections, scenario, (), absorbed_ids)
    true_n = len(matched_ids)
    total = true_n + len(false)

    by_detector: dict[str, dict[str, Any]] = {}
    for m in matches:
        if m.detected and m.detector_id:
            row = by_detector.setdefault(m.detector_id, {
                "detector": m.detector_id, "version": m.detector_version,
                "detected": 0, "delays_s": []})
            row["detected"] += 1
            row["delays_s"].append(m.delay_s)
    for d in false:
        row = by_detector.setdefault(d.detector_id, {
            "detector": d.detector_id, "version": d.detector_version,
            "detected": 0, "delays_s": []})
        row["false"] = row.get("false", 0) + 1

    return ScenarioScore(
        scenario=scenario.name, runs=len(runs), detected_runs=detected_runs,
        recall=(detected_runs / len(runs)) if runs else None,
        precision=(true_n / total) if total else None,
        delay_median_s=percentile(delays, 50), delay_p90_s=percentile(delays, 90),
        delay_range_s=(min(delays), max(delays)) if delays else None,
        false_ids=[d.id for d in false], matches=matches,
        gated=scenario.pending is None, by_detector=by_detector)


# --- outputs -----------------------------------------------------------------------

HISTORY_FIELDS = ("scored_at", "scenario", "detector", "version", "runs", "detected",
                  "false", "delay_median_s", "recall", "precision")


def write_report(score: ScenarioScore, out_dir: Path, now: datetime | None = None) -> Path:
    """var/anomaly-eval/<UTC-date>-<scenario>.json, plus one history.csv row
    per (detector, version, scenario)."""
    now = now or datetime.now(UTC)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{now.date().isoformat()}-{score.scenario}.json"
    path.write_text(json.dumps({"scored_at": now.isoformat(), **score.to_json()},
                               indent=2, default=str), encoding="utf-8")
    hist = out_dir / "history.csv"
    new = not hist.exists()
    with hist.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        if new:
            w.writeheader()
        for row in score.by_detector.values():
            w.writerow({
                "scored_at": now.isoformat(), "scenario": score.scenario,
                "detector": row["detector"], "version": row["version"],
                "runs": score.runs, "detected": row["detected"],
                "false": row.get("false", 0),
                "delay_median_s": percentile([x for x in row["delays_s"] if x is not None], 50),
                "recall": score.recall, "precision": score.precision})
    return path
