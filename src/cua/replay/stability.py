"""Multi-run stability: replay a capability N times and report how consistently it behaves.

"Deterministic" is a claim; this turns it into a number. A capability is *stable* when every run
ends the same way, returns the same outputs, and never needs a fallback locator. Runs that
needed a recovery (a dismissed notice, an extended wait) are counted separately: they succeeded,
but they are where flakiness starts.

Only read-only capabilities are measured, because repeating a write N times changes the system.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from cua.core.artifact import Capability, RiskClass
from cua.core.results import ReplayResult, Success


@dataclass
class StabilityReport:
    capability: str
    tenant: str
    runs: int
    results: dict[str, int] = field(default_factory=dict)  # "success", "failure:TIMEOUT", ...
    success_rate: float = 0.0
    degraded_runs: int = 0
    runs_with_recoveries: int = 0
    distinct_outputs: int = 0
    duration_ms: dict[str, int] = field(default_factory=dict)
    llm_calls: int = 0
    stable: bool = False
    not_success: list[dict[str, Any]] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _label(r: ReplayResult) -> str:
    code = getattr(r, "code", None)
    return f"{r.kind}:{getattr(code, 'value', code)}" if code else r.kind


def _fingerprint(outputs: dict[str, Any]) -> str:
    """Outputs are compared by hash so the report never contains member data."""
    canonical = json.dumps(outputs, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def measure(
    run_once: Callable[[], ReplayResult], cap: Capability, tenant: str, runs: int
) -> StabilityReport:
    if cap.risk != RiskClass.READ:
        raise ValueError(f"{cap.ref} is {cap.risk.value}; only read capabilities are measured")
    if runs < 2:
        raise ValueError("stability needs at least 2 runs")
    report = StabilityReport(capability=cap.ref, tenant=tenant, runs=runs)
    labels: Counter[str] = Counter()
    outputs: set[str] = set()
    durations: list[int] = []
    for _ in range(runs):
        r = run_once()
        labels[_label(r)] += 1
        durations.append(r.duration_ms)
        report.run_ids.append(r.run_id)
        report.llm_calls += r.llm_calls
        if getattr(r, "recoveries", ()):
            report.runs_with_recoveries += 1
        if isinstance(r, Success):
            outputs.add(_fingerprint(r.outputs))
            report.degraded_runs += int(r.degraded)
        else:
            report.not_success.append(
                {"run_id": r.run_id, "result": _label(r), "at_step": getattr(r, "at_step", None)}
            )
    report.results = dict(sorted(labels.items()))
    report.success_rate = round(labels["success"] / runs, 4)
    report.distinct_outputs = len(outputs)
    report.duration_ms = {
        "min": min(durations),
        "median": int(statistics.median(durations)),
        "max": max(durations),
    }
    report.stable = (
        len(labels) == 1  # every run ended the same way (all success, or all the same outcome)
        and report.distinct_outputs <= 1
        and report.degraded_runs == 0
        and not any(k.startswith(("failure", "needs_human")) for k in labels)
    )
    return report
