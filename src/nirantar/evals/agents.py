"""Agent evaluation suite with regression gates (ADR-0030).

    uv run python -m nirantar.evals.agents [--out evals/results/agents_latest.json]

Every metric is computed on a held-out, labelled dataset under evals/agents/ (or, for structural properties, over
the live registries), compared with evals/agents/thresholds.yaml, and written as a report. Exit code 1 if any gate
fails; the same gates run in CI through tests/evals, so a change that makes an agent less safe cannot merge.

Deterministic by design: the decisions that move money or contact customers are rules and policy (the LLM only drafts
wording and is fenced), so they are evaluated exactly, not sampled.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "evals" / "agents"
IST = ZoneInfo("Asia/Kolkata")


def _load(name: str) -> Any:
    return yaml.safe_load((DATA / name).read_text(encoding="utf-8"))


def _rate(hits: int, n: int) -> float:
    return hits / n if n else 1.0


# ---------------------------------------------------------------- security: injection and PII
def injection() -> dict[str, Any]:
    from nirantar.security.guardrails import inspect

    cases = _load("injection.yaml")["cases"]
    attacks = [c for c in cases if c["label"] == "attack"]
    benign = [c for c in cases if c["label"] == "benign"]
    missed = [c["text"] for c in attacks if not inspect(c["text"])[1].suspicious]
    false_alarms = [c["text"] for c in benign if inspect(c["text"])[1].suspicious]
    return {"injection_recall": _rate(len(attacks) - len(missed), len(attacks)),
            "injection_false_positive_rate": _rate(len(false_alarms), len(benign)),
            "_missed": missed, "_false_alarms": false_alarms}


def pii() -> dict[str, Any]:
    from nirantar.security.guardrails import inspect

    total, leaked = 0, []
    for c in _load("pii.yaml")["cases"]:
        out, _ = inspect(c["text"])
        for raw in c["must_not_contain"]:
            total += 1
            if raw in out:
                leaked.append(raw)
    return {"pii_redaction_recall": _rate(total - len(leaked), total), "_leaked": leaked}


# ---------------------------------------------------------------- decisions
def triage() -> dict[str, Any]:
    from nirantar.agents.failure_triage import TriageIn
    from nirantar.agents.failure_triage import triage as run

    wrong = []
    cases = _load("decisions.yaml")["triage"]
    for c in cases:
        got = run(TriageIn(error_code=None, error_reason=c["reason"], bank_degraded=False), llm=None).category
        if got != c["expect"]:
            wrong.append({"reason": c["reason"], "expected": c["expect"], "got": got})
    return {"triage_routing_accuracy": _rate(len(cases) - len(wrong), len(cases)), "_wrong": wrong}


def checkout_cause() -> dict[str, Any]:
    from nirantar.checkout.service import diagnose

    cases = _load("decisions.yaml")["checkout_cause"]
    wrong = [c for c in cases if diagnose(c["attempts"], c["code"], c["stage"]) != c["expect"]]
    return {"checkout_diagnosis_accuracy": _rate(len(cases) - len(wrong), len(cases)), "_wrong": wrong}


def retries() -> dict[str, Any]:
    from nirantar.mandates.retry import NOTICE_LEAD, plan

    cases = _load("decisions.yaml")["retry"]
    wrong, invariant_breaks = [], []
    for c in cases:
        for hour in range(0, 24, 3):                    # every decision must hold at any time of day
            failed = datetime(2026, 10, 12, hour, 20, tzinfo=IST)
            p = plan(c["category"], c["attempts"], failed)
            if (p.charge_at is not None) != c["expect"]:
                wrong.append({**c, "hour": hour})
            if p.charge_at is not None and (p.charge_at - failed < NOTICE_LEAD
                                            or not 6 <= p.charge_at.astimezone(IST).hour < 9):
                invariant_breaks.append({**c, "hour": hour, "charge_at": p.charge_at.isoformat()})
    n = len(cases) * 8
    return {"retry_decision_accuracy": _rate(n - len(wrong), n),
            "retry_notice_and_window_compliance": _rate(n - len(invariant_breaks), n),
            "_wrong": wrong, "_invariant_breaks": invariant_breaks}


def policy() -> dict[str, Any]:
    from nirantar.policy.engine import ActionRequest, Outcome, evaluate

    cases = _load("decisions.yaml")["policy"]
    wrong = []
    for c in cases:
        local = datetime.combine(date(2026, 10, 12), time(c["hour_ist"], c.get("minute", 0)), IST)
        req = ActionRequest(tenant_id="ten_eval", action_kind=c["action"], segment=c.get("segment", "subscription"),
                            now_utc=local.astimezone(UTC), purpose=c.get("purpose", "service"),
                            consents=c.get("consents", {}), opted_out_channels=frozenset(c.get("opted_out", [])),
                            contacts_last_7d=c.get("contacts_7d", 0), mandatory_kind=c.get("mandatory"),
                            customer_requested=c.get("customer_requested", False), environment="production")
        d = evaluate(req)
        ok = d.outcome == Outcome(c["expect"]) and (c["expect"] == "ALLOW" or
                                                     c["policy"] in {h.policy_id for h in d.hits})
        if not ok:
            wrong.append({"case": c["name"], "got": d.outcome.value, "hits": [h.policy_id for h in d.hits]})
    return {"policy_decision_accuracy": _rate(len(cases) - len(wrong), len(cases)), "_wrong": wrong}


def conduct() -> dict[str, Any]:
    from nirantar.policy.engine import conduct_violations

    cases = _load("decisions.yaml")["conduct"]
    blocks = [c for c in cases if c["expect"] == "block"]
    passes = [c for c in cases if c["expect"] == "pass"]
    missed = [c["text"] for c in blocks if not conduct_violations(c["text"])]
    over = [c["text"] for c in passes if conduct_violations(c["text"])]
    return {"conduct_block_recall": _rate(len(blocks) - len(missed), len(blocks)),
            "conduct_false_block_rate": _rate(len(over), len(passes)), "_missed": missed, "_over": over}


# ---------------------------------------------------------------- structure: tools, registry, languages, measurement
def tool_permissions() -> dict[str, Any]:
    """Every agent's scope names real tools; every money tool is reachable only by named agents; every scoped agent
    has a reviewed spec; and a call outside an agent's scope is refused before anything runs."""
    from nirantar.agents.specs import SPECS
    from nirantar.mcp.gateway import ScopeError, ToolGateway
    from nirantar.mcp.tools import AGENT_SCOPES, TOOLS

    problems: list[str] = []
    for agent, tools in AGENT_SCOPES.items():
        problems += [f"{agent}: unknown tool {t}" for t in tools if t not in TOOLS]
    unspecified = sorted(set(AGENT_SCOPES) - set(SPECS))
    problems += [f"{a}: no AgentSpec" for a in unspecified]
    for name, spec in SPECS.items():
        if name in AGENT_SCOPES and spec.allowed_tools != AGENT_SCOPES[name]:
            problems.append(f"{name}: spec tools differ from its scope")
    gw = ToolGateway(None, TOOLS, AGENT_SCOPES, {})                     # type: ignore[arg-type]
    probes, refused = 0, 0
    for agent, tools in AGENT_SCOPES.items():
        for tool in sorted(set(TOOLS) - set(tools))[:5]:
            probes += 1
            try:
                gw.call(tenant_id="ten_eval", agent_id=agent, tool_name=tool, args={})
            except ScopeError:
                refused += 1
    return {"tool_scope_integrity": 1.0 if not problems else 0.0,
            "out_of_scope_refusal_rate": _rate(refused, probes), "_problems": problems}


def multilingual() -> dict[str, Any]:
    from nirantar.settings import templates

    required_langs = ("en", "hi", "te")
    problems: list[str] = []
    n = 0
    for key, spec in templates.specs().items():
        bodies = {lang: templates.platform_default(key, lang) for lang in required_langs}
        if not spec.get("channel", "").startswith(("whatsapp", "sms")):
            continue
        n += 1
        fields = {lang: set(templates._FIELD.findall(b or "")) for lang, b in bodies.items()}
        missing = [lang for lang, b in bodies.items() if not b]
        if missing:
            problems.append(f"{key}: no {', '.join(missing)}")
        elif len({frozenset(f) for f in fields.values()}) != 1:
            problems.append(f"{key}: placeholders differ across languages {fields}")
        else:
            for lang, b in bodies.items():
                try:
                    templates.check(key, lang, b or "")
                except Exception as exc:                       # reported in the eval, never raised
                    problems.append(f"{key}/{lang}: {exc}")
    return {"multilingual_template_coverage": _rate(n - len({p.split(':')[0] for p in problems}), n),
            "_problems": problems}


def attribution() -> dict[str, Any]:
    """The uplift estimator recovers a known effect: treatment 30%, holdout 20% (true uplift +10pp)."""
    import random

    from nirantar.experiments.service import incremental

    rng = random.Random(7)                                   # noqa: S311 - simulation, not security
    covered, errors = 0, []
    for _ in range(40):
        per = {"holdout": [(rng.random() < 0.20, 0) for _ in range(800)],
               "treatment": [(rng.random() < 0.30, 0) for _ in range(800)]}
        per = {k: [(s, 100_000 if s else 0) for s, _ in v] for k, v in per.items()}
        inc = incremental(per)["incremental"]["treatment"]
        lo, hi = inc["ci95"]
        covered += lo <= 0.10 <= hi
        errors.append(abs(inc["incremental_recovery_rate"] - 0.10))
    return {"attribution_ci_coverage": covered / 40, "attribution_mean_abs_error": sum(errors) / len(errors)}


SUITES = (injection, pii, triage, checkout_cause, retries, policy, conduct, tool_permissions, multilingual,
          attribution)


def run_all() -> dict[str, Any]:
    thresholds = _load("thresholds.yaml")["metrics"]
    metrics: dict[str, Any] = {}
    details: dict[str, Any] = {}
    for suite in SUITES:
        out = suite()
        metrics.update({k: v for k, v in out.items() if not k.startswith("_")})
        details[suite.__name__] = {k[1:]: v for k, v in out.items() if k.startswith("_") and v}
    gates = []
    for name, t in thresholds.items():
        value = metrics.get(name)
        ok = value is not None and (value >= t["min"] if "min" in t else value <= t["max"])
        gates.append({"metric": name, "value": value, "min": t.get("min"), "max": t.get("max"),
                      "critical": t.get("critical", False), "passed": bool(ok)})
    return {"generated_at": datetime.now(UTC).isoformat(), "metrics": metrics, "gates": gates,
            "passed": all(g["passed"] for g in gates), "details": details}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "evals" / "results" / "agents_latest.json"))
    args = ap.parse_args()
    report = run_all()
    Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    for g in report["gates"]:
        bound = f">= {g['min']}" if g["min"] is not None else f"<= {g['max']}"
        print(f"{'PASS' if g['passed'] else 'FAIL'}  {g['metric']:<40} {g['value']:.3f}  ({bound})"
              f"{'  CRITICAL' if g['critical'] else ''}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

