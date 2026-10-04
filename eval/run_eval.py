#!/usr/bin/env python3
"""Run a planner over the 32 scenarios, save outputs + traces, and score them.

  python3 eval/run_eval.py --planner rules --input profile
  python3 eval/run_eval.py --planner rules --input text
  python3 eval/run_eval.py --planner agent --input text --model qwen/qwen3.7-flash
  python3 eval/run_eval.py --planner agent --input text --no-validate      # ablation

Inputs:
  profile  the user's request text + their constraints as a structured profile
           (what the CLI flags give you)
  text     the request text only - the planner must infer diets/equipment/budget

The dish is never handed over: both planners must identify it from the text.
Outputs go to eval/runs/<name>.outputs.jsonl (+ .trace.jsonl for the agent) and
the metric summary to eval/runs/<name>.summary.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from culinacompass import plan as planmod  # noqa: E402
from culinacompass.agent import run_agent  # noqa: E402
from culinacompass.rule_planner import parse_request_rules, plan_rules  # noqa: E402
import score  # noqa: E402

AS_OF = date(2026, 9, 28)  # grade evidence as of collection, so runs are reproducible


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--planner", choices=["rules", "agent"], required=True)
    ap.add_argument("--input", choices=["profile", "text"], default="profile")
    ap.add_argument("--model", default=None)
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--only", nargs="*", help="scenario ids")
    ap.add_argument("--name")
    ap.add_argument("--set", choices=["original", "paraphrased"], default="original",
                    help="paraphrased = held-out rewording, written after the rule parser was frozen")
    a = ap.parse_args()

    scen = score.load_scenarios("scenarios.jsonl" if a.set == "original" else "scenarios_paraphrased.jsonl")
    if a.only:
        scen = [s for s in scen if s["scenario_id"] in a.only]
    llm = None
    if a.planner == "agent":
        from culinacompass.llm import OpenRouterClient
        llm = OpenRouterClient(a.model)
    name = a.name or "_".join(filter(None, [a.planner, a.input, a.set, "novalidate" if a.no_validate else "",
                                            (llm.model.replace("/", "-") if llm else "")]))
    out_dir = ROOT / "eval" / "runs"
    out_dir.mkdir(exist_ok=True)
    outputs, traces = [], []
    t0 = time.time()
    for s in scen:
        profile = planmod.Constraints.from_dict(s["constraints"]) if a.input == "profile" else planmod.Constraints()
        if a.planner == "rules":
            dish, parsed = parse_request_rules(s["request_text"])
            p = plan_rules(dish, profile.merged(parsed))
            meta = {}
        else:
            res = run_agent(s["request_text"], llm, profile, AS_OF, validate=not a.no_validate)
            p = res.plan or planmod.Plan(None, "abstain", explanation=f"no plan: {res.stopped_reason}")
            meta = {"steps": res.steps, "rejections": res.rejections, "stopped": res.stopped_reason}
            traces.append({"scenario_id": s["scenario_id"], **meta, "tool_calls": res.tool_calls,
                           "final_issues": res.issues})
        rec = planmod.to_eval_output(s["scenario_id"], p, profile, AS_OF)
        rec["_plan"] = {**p.__dict__, "constraints_inferred": p.constraints_inferred.as_dict()}
        rec["_meta"] = meta
        outputs.append(rec)
        print(f"{s['scenario_id']} {s['target_dish_id']:<20} -> {p.dish_id or '-':<20} {p.outcome:<8} {meta}",
              file=sys.stderr)

    (out_dir / f"{name}.outputs.jsonl").write_text("\n".join(json.dumps(o, default=str) for o in outputs) + "\n")
    if traces:
        (out_dir / f"{name}.trace.jsonl").write_text("\n".join(json.dumps(t, default=str) for t in traces) + "\n")
    results = [score.score_one(s, next(o for o in outputs if o["scenario_id"] == s["scenario_id"])) for s in scen]
    summary = score.summarise(results)
    summary.update({"run": name, "seconds": round(time.time() - t0, 1)})
    if llm:
        summary["usage"] = llm.usage
    (out_dir / f"{name}.summary.json").write_text(json.dumps(summary, indent=2))
    (out_dir / f"{name}.details.jsonl").write_text("\n".join(json.dumps(r) for r in results) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
