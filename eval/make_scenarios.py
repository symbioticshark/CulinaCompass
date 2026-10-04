#!/usr/bin/env python3
"""Turn hand-written scenario seeds into scored scenarios with ground truth
derived deterministically from the dataset.

  eval/scenario_seeds.jsonl  (request, target dish, constraints - human written)
        + data tables + rules + substitution labels
  ->  eval/scenarios.jsonl   (expected outcome, forbidden ingredients, required
                              flags, signature ingredients, acceptable subs)

Outcome policy (documented so it can be argued with):
  * A signature ingredient blocked by the user's diet  -> abstain_and_explain,
    unless a substitution for it in this dish is labelled acceptable /
    acceptable_with_caveat AND the substitute itself passes the diet
    -> adapt_with_caveat.
  * Otherwise, any required flag (diet conflict on a non-signature line, label
    warning, Difficult/Unknown ingredient, missing equipment, budget overrun)
    -> adapt_with_caveat.
  * Otherwise -> adapt.
Labels: human_label wins over claude_proposed_acceptability when present.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from culinacompass import data, rules  # noqa: E402

OK_LABELS = {"acceptable", "acceptable_with_caveat"}


def effective_label(s: dict) -> str:
    return s["human_label"] or s["claude_proposed_acceptability"]


def label_source(s: dict) -> str:
    return "human" if s["human_label"] else "claude_proposed"


def blocked(ingredient_id: str, diets: list[str]) -> bool:
    return any(sev == "block" for _d, sev, _w in rules.check_ingredient(ingredient_id, diets))


def build(seed: dict) -> dict:
    dish = seed["target_dish_id"]
    diets = seed["constraints"].get("diets", [])
    have = set(seed["constraints"].get("equipment_have", []))
    rows = data.dish_ingredients()[dish]
    avail = data.availability()
    subs = data.read_csv("substitutions.csv")

    forbidden_global = sorted(i for i in data.ingredients() if blocked(i, diets))
    dish_conflicts = sorted({r["ingredient_id"] for r in rows if blocked(r["ingredient_id"], diets)})
    warnings = sorted({v.ingredient_id for v in rules.check_dish_diet(dish, diets)
                       if v.severity == "warn" and not v.is_optional})
    must_flag_avail = sorted({r["ingredient_id"] for r in rows
                              if r["is_optional"] != "yes" and not r["alt_group"]
                              and avail[("ingredient", r["ingredient_id"])]["grade"] in ("Difficult", "Unknown")})
    eq = rules.check_equipment(dish, have)
    signature = sorted({r["ingredient_id"] for r in rows if r["is_signature"] == "yes"})

    relevant_subs = [s for s in subs if s["item_type"] == "ingredient"
                     and s["context_dish_id"] in ("", dish)
                     and s["original_id"] in {r["ingredient_id"] for r in rows}]
    acceptable = [s for s in relevant_subs if effective_label(s) in OK_LABELS
                  and (not s["substitute_id"] or not blocked(s["substitute_id"], diets))]
    unacceptable = [s for s in relevant_subs if effective_label(s) == "unacceptable"
                    or (s["substitute_id"] and blocked(s["substitute_id"], diets))]

    blocked_sigs = [i for i in signature if blocked(i, diets)]
    rescued = {i for i in blocked_sigs
               if any(s["original_id"] == i and s["substitute_id"] for s in acceptable)}

    budget = seed["constraints"].get("budget_sgd")
    cost = rules.cost_dish(dish)
    basis = seed["constraints"].get("budget_basis", "cash_outlay")
    cost_value = cost["cash_outlay_sgd"] if basis == "cash_outlay" else cost["prorated_sgd"]
    budget_ok = None if budget is None else cost_value <= budget

    if blocked_sigs and set(blocked_sigs) - rescued:
        outcome = "abstain_and_explain"
    elif (blocked_sigs or dish_conflicts or warnings or must_flag_avail
          or not eq.feasible or budget_ok is False):
        outcome = "adapt_with_caveat"
    else:
        outcome = "adapt"

    return {
        **seed,
        "expected": {
            "expected_outcome": outcome,
            "signature_ingredient_ids": signature,
            "blocked_signature_ingredient_ids": blocked_sigs,
            "forbidden_ingredient_ids": forbidden_global,
            "dish_conflict_ingredient_ids": dish_conflicts,
            "must_flag_ingredient_ids": sorted(set(must_flag_avail) | set(warnings)),
            "must_flag_equipment": eq.missing,
            "equipment_alternatives_in_hand": eq.using_alternative,
            "budget_sgd": budget,
            "budget_basis": basis if budget is not None else None,
            "dataset_cost_sgd": cost_value if budget is not None else None,
            "budget_feasible": budget_ok,
            "acceptable_substitution_ids": sorted(s["sub_id"] for s in acceptable),
            "unacceptable_substitution_ids": sorted(s["sub_id"] for s in unacceptable),
            "label_sources": sorted({label_source(s) for s in relevant_subs}),
        },
    }


SETS = {"scenario_seeds.jsonl": "scenarios.jsonl",
        "scenario_seeds_paraphrased.jsonl": "scenarios_paraphrased.jsonl"}


def main() -> None:
    for src, dst in SETS.items():
        seeds = [json.loads(l) for l in (ROOT / "eval" / src).read_text().splitlines() if l.strip()]
        out = [build(s) for s in seeds]
        with open(ROOT / "eval" / dst, "w", encoding="utf-8") as f:
            for s in out:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        _report(dst, out)


def _report(dst, out) -> None:
    from collections import Counter
    print(f"{dst}: {len(out)} scenarios:", dict(Counter(s["expected"]["expected_outcome"] for s in out)))


if __name__ == "__main__":
    main()
