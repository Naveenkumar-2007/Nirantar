"""CI gate for the agent eval suite (ADR-0030): every metric must meet evals/agents/thresholds.yaml."""

from __future__ import annotations

import json

from nirantar.evals.agents import ROOT, run_all


def test_agent_evals_meet_their_thresholds() -> None:
    report = run_all()
    (ROOT / "evals" / "results").mkdir(parents=True, exist_ok=True)
    (ROOT / "evals" / "results" / "agents_latest.json").write_text(json.dumps(report, indent=2, default=str),
                                                                   encoding="utf-8")
    failed = [g for g in report["gates"] if not g["passed"]]
    assert not failed, (failed, report["details"])
