#!/usr/bin/env python3
"""Score agent outputs against eval/scenarios.jsonl on separate metrics.

Metrics (reported separately on purpose - no single number to game):
  constraint_adherence   no forbidden ingredient used; missing equipment and
                         budget overruns flagged. (Abstaining passes trivially -
                         that is the 'lazy winner' hole, so read it together
                         with dish_fidelity.)
  dish_fidelity          the plan is for the dish the user NAMED, the
                         adapt/abstain decision matches ground truth, and when
                         adapting every signature ingredient is kept or replaced
                         by a substitution labelled acceptable.
  task_success           adherence AND fidelity in the same scenario.
  substitution_acceptability
                         share of proposed substitutions labelled acceptable
                         (human label preferred; unlabelled pairs reported as
                         'unjudged', never counted as passes).
  flag_recall            share of must-flag ingredients (Difficult/Unknown or
                         label-dependent) that the agent flagged.
  availability_overclaims  count of 'Confirmed' claims the data does not support.
  false_alarms           flags on ingredients that needed no flag.

Agent output format (one JSON object per line):
  {"scenario_id": "S01", "dish_id_addressed": "idli", "outcome": "adapt"|"abstain",
   "ingredients_used": [...], "substitutions": [{"original_id": .., "substitute_id": ..|null}],
   "flagged_ingredient_ids": [...], "flagged_equipment": [...],
   "availability_claims": {"ingredient_id": "Confirmed"|...},
   "claimed_cost_sgd": 0.0, "budget_flagged": false}

Usage:
  python3 eval/score.py --outputs my_agent_outputs.jsonl
  python3 eval/score.py --baseline lazy_safe_dish|as_written|rules_oracle
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from culinacompass import data, rules  # noqa: E402

OK = {"acceptable", "acceptable_with_caveat"}


def load_scenarios(name: str = "scenarios.jsonl") -> list[dict]:
    return [json.loads(l) for l in (ROOT / "eval" / name).read_text().splitlines() if l.strip()]


def sub_label(original: str, substitute: str | None, dish: str) -> tuple[str, str]:
    """(label, source) for a proposed substitution; context row beats general row."""
    rows = [s for s in data.read_csv("substitutions.csv")
            if s["original_id"] == original and (s["substitute_id"] or None) == (substitute or None)]
    rows.sort(key=lambda s: 0 if s["context_dish_id"] == dish else (1 if not s["context_dish_id"] else 2))
    rows = [s for s in rows if s["context_dish_id"] in ("", dish)]
    if not rows and not substitute:
        # Policy: dropping a non-signature line is acceptable-with-caveat;
        # dropping a signature ingredient is unacceptable (it is no longer the dish).
        sig = any(r["ingredient_id"] == original and r["is_signature"] == "yes"
                  for r in data.dish_ingredients()[dish])
        return ("unacceptable" if sig else "acceptable_with_caveat"), "policy_omit"
    if not rows:
        return "unjudged", ""
    s = rows[0]
    return (s["human_label"] or s["claude_proposed_acceptability"],
            "human" if s["human_label"] else "claude_proposed")


def equipment_flagged(missing: list[str], flagged: list[str]) -> bool:
    fl = set(flagged)
    return all(any(m in fl for m in group.split("|")) or group in fl for group in missing)


def score_one(sc: dict, out: dict) -> dict:
    exp = sc["expected"]
    dish = sc["target_dish_id"]
    used = set(out.get("ingredients_used", []))
    subs = out.get("substitutions", [])
    introduced = {s["substitute_id"] for s in subs if s.get("substitute_id")}
    abstained = out.get("outcome") == "abstain"

    # constraint adherence
    forbidden_hit = sorted((used | introduced) & set(exp["forbidden_ingredient_ids"]))
    eq_ok = equipment_flagged(exp["must_flag_equipment"], out.get("flagged_equipment", []))
    budget_ok = True
    if exp["budget_sgd"] is not None and not abstained:
        claimed = out.get("claimed_cost_sgd")
        over = (exp["budget_feasible"] is False) or (claimed is not None and claimed > exp["budget_sgd"])
        budget_ok = (not over) or bool(out.get("budget_flagged"))
    adherence = abstained or (not forbidden_hit and eq_ok and budget_ok)

    # dish fidelity
    addressed = out.get("dish_id_addressed") == dish
    expect_abstain = exp["expected_outcome"] == "abstain_and_explain"
    outcome_ok = abstained == expect_abstain
    sig_ok = True
    if not abstained:
        replaced = {s["original_id"]: s.get("substitute_id") for s in subs}
        for sig in exp["signature_ingredient_ids"]:
            if sig in used and sig not in replaced:
                continue
            if sig in replaced and replaced[sig] and sub_label(sig, replaced[sig], dish)[0] in OK:
                continue
            sig_ok = False
    fidelity = addressed and outcome_ok and sig_ok

    # substitutions
    judged = [sub_label(s["original_id"], s.get("substitute_id"), dish) for s in subs]

    # flags and claims
    # flags matter only when the agent cooks something; a justified refusal owes no shopping caveats
    must = set(exp["must_flag_ingredient_ids"]) if not (abstained and expect_abstain) else set()
    flagged = set(out.get("flagged_ingredient_ids", []))
    avail = data.availability()
    overclaims = [i for i, g in out.get("availability_claims", {}).items()
                  if g == "Confirmed" and avail.get(("ingredient", i), {}).get("grade") != "Confirmed"]
    false_alarms = sorted(flagged - must - set(exp["dish_conflict_ingredient_ids"]))

    return {
        "scenario_id": sc["scenario_id"], "adherence": adherence, "fidelity": fidelity,
        "task_success": adherence and fidelity, "forbidden_hit": forbidden_hit,
        "equipment_ok": eq_ok, "budget_ok": budget_ok, "addressed_target": addressed,
        "outcome_ok": outcome_ok, "signatures_ok": sig_ok, "subs": judged,
        "must_flag": len(must), "flag_hits": len(must & flagged),
        "overclaims": overclaims, "false_alarms": false_alarms,
    }


def summarise(results: list[dict]) -> dict:
    n = len(results)
    subs = [lbl for r in results for lbl, _src in r["subs"]]
    judged = [l for l in subs if l != "unjudged"]
    must = sum(r["must_flag"] for r in results)
    return {
        "scenarios": n,
        "constraint_adherence": round(sum(r["adherence"] for r in results) / n, 3),
        "dish_fidelity": round(sum(r["fidelity"] for r in results) / n, 3),
        "task_success": round(sum(r["task_success"] for r in results) / n, 3),
        "substitution_acceptability": (round(sum(l in OK for l in judged) / len(judged), 3) if judged else None),
        "substitutions_proposed": len(subs),
        "substitutions_unjudged": len(subs) - len(judged),
        "flag_recall": round(sum(r["flag_hits"] for r in results) / must, 3) if must else None,
        "availability_overclaims": sum(len(r["overclaims"]) for r in results),
        "false_alarms": sum(len(r["false_alarms"]) for r in results),
    }


# ---------------------------------------------------------------- baselines
def _as_written(dish: str) -> list[str]:
    return [r["ingredient_id"] for r in data.dish_ingredients()[dish] if r["is_optional"] != "yes"]


def baseline(name: str, sc: dict) -> dict:
    exp, dish = sc["expected"], sc["target_dish_id"]
    if name == "lazy_safe_dish":
        # Always recommends plain dosa as written: satisfies every diet in this set.
        eq = rules.check_equipment("dosa", set(sc["constraints"].get("equipment_have", [])))
        cost = rules.cost_dish("dosa")
        budget = sc["constraints"].get("budget_sgd")
        return {"scenario_id": sc["scenario_id"], "dish_id_addressed": "dosa", "outcome": "adapt",
                "ingredients_used": _as_written("dosa"), "substitutions": [],
                "flagged_ingredient_ids": [], "flagged_equipment": eq.missing + exp["must_flag_equipment"],
                "availability_claims": {}, "claimed_cost_sgd": cost["cash_outlay_sgd"],
                "budget_flagged": budget is not None and cost["cash_outlay_sgd"] > budget}
    if name == "as_written":
        return {"scenario_id": sc["scenario_id"], "dish_id_addressed": dish, "outcome": "adapt",
                "ingredients_used": _as_written(dish), "substitutions": [],
                "flagged_ingredient_ids": [], "flagged_equipment": [],
                "availability_claims": {i: "Confirmed" for i in _as_written(dish)},
                "claimed_cost_sgd": None, "budget_flagged": False}
    if name == "rules_oracle":
        if exp["expected_outcome"] == "abstain_and_explain":
            return {"scenario_id": sc["scenario_id"], "dish_id_addressed": dish, "outcome": "abstain",
                    "flagged_ingredient_ids": exp["must_flag_ingredient_ids"]}
        forbidden = set(exp["forbidden_ingredient_ids"])
        subs_ok = {s["sub_id"]: s for s in data.read_csv("substitutions.csv")
                   if s["sub_id"] in exp["acceptable_substitution_ids"]}
        used, subs = [], []
        for i in _as_written(dish):
            if i not in forbidden:
                used.append(i)
                continue
            s = next((s for s in subs_ok.values() if s["original_id"] == i and s["substitute_id"]
                      and s["context_dish_id"] == dish), None) or \
                next((s for s in subs_ok.values() if s["original_id"] == i and s["substitute_id"]), None)
            subs.append({"original_id": i, "substitute_id": s["substitute_id"] if s else None})
            if s:
                used.append(s["substitute_id"])
        return {"scenario_id": sc["scenario_id"], "dish_id_addressed": dish, "outcome": "adapt",
                "ingredients_used": used, "substitutions": subs,
                "flagged_ingredient_ids": exp["must_flag_ingredient_ids"],
                "flagged_equipment": exp["must_flag_equipment"], "availability_claims": {},
                "claimed_cost_sgd": exp["dataset_cost_sgd"],
                "budget_flagged": exp["budget_feasible"] is False}
    raise ValueError(name)


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--outputs")
    g.add_argument("--baseline", choices=["lazy_safe_dish", "as_written", "rules_oracle"])
    ap.add_argument("--details", action="store_true")
    ap.add_argument("--scenarios", default="scenarios.jsonl")
    a = ap.parse_args()
    scen = load_scenarios(a.scenarios)
    if a.outputs:
        outs = {o["scenario_id"]: o for o in map(json.loads, Path(a.outputs).read_text().splitlines()) if o}
    else:
        outs = {s["scenario_id"]: baseline(a.baseline, s) for s in scen}
    results = [score_one(s, outs.get(s["scenario_id"], {"outcome": "missing"})) for s in scen]
    if a.details:
        for r in results:
            print(json.dumps(r))
    print(json.dumps(summarise(results), indent=2))


if __name__ == "__main__":
    main()
