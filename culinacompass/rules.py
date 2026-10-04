"""Deterministic checks the agent must call instead of reasoning about them itself:
dietary compliance, equipment feasibility, and dish cost in Singapore dollars.

All functions are pure over the CSV tables, so the same input always gives the
same verdict - which is what makes constraint adherence measurable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import data

# diet key -> predicate over an ingredient row returning (severity, reason) or None
#   severity "block": ingredient definitely violates the diet
#   severity "warn":  depends on brand/label or strictness - must be surfaced
DIETS = {
    "vegetarian": lambda i: None if i["is_vegetarian"] == "yes" else ("block", "not vegetarian"),
    "vegan": lambda i: None if i["is_vegan"] == "yes" else ("block", "contains dairy/animal product"),
    "jain": lambda i: (None if i["jain_ok"] == "yes" else
                       ("warn", "Jain acceptability depends on practice") if i["jain_ok"] == "conditional"
                       else ("block", "excluded in Jain diet (root/bulb/rhizome or multi-seeded) - "
                             + (i["notes"] or i["canonical_name"]))),
    "gluten_free": lambda i: (None if i["gluten_risk"] == "none" else
                              ("block", "contains gluten") if i["gluten_risk"] == "contains"
                              else ("warn", "may contain gluten (e.g. compounded hing) - check label")),
    "no_peanut": lambda i: ("block", "peanut") if "peanut" in i["sfa_allergens"] else None,
    "no_tree_nut": lambda i: ("block", "tree nut") if "tree_nut" in i["sfa_allergens"] else None,
    "no_dairy": lambda i: ("block", "milk/dairy") if "milk" in i["sfa_allergens"] else None,
    "no_sesame": lambda i: ("block", "sesame") if "sesame" in i["other_sensitivities"] else None,
    "no_coconut": lambda i: ("block", "coconut") if "coconut" in i["other_sensitivities"] else None,
    "no_onion_garlic": lambda i: (("block", "onion/garlic family")
                                  if i["ingredient_id"] in {"onion", "shallots", "garlic",
                                                            "ginger_garlic_paste", "gongura_pickle"}
                                  else None),
}


@dataclass
class DietViolation:
    ingredient_id: str
    severity: str
    reason: str
    is_optional: bool
    is_signature: bool
    alt_group: str


def check_ingredient(ingredient_id: str, diets: list[str]) -> list[tuple[str, str, str]]:
    ing = data.ingredients()[ingredient_id]
    out = []
    for d in diets:
        if d not in DIETS:
            raise ValueError(f"unknown diet key: {d}")
        res = DIETS[d](ing)
        if res:
            out.append((d, res[0], res[1]))
    return out


def check_dish_diet(dish_id: str, diets: list[str]) -> list[DietViolation]:
    """Violations in the dish as written. Optional lines are reported but flagged
    optional; an alt-group violation is only real if every member violates."""
    rows = data.dish_ingredients()[dish_id]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r["alt_group"]:
            groups.setdefault(r["alt_group"], []).append(r)
    out: list[DietViolation] = []
    for r in rows:
        hits = check_ingredient(r["ingredient_id"], diets)
        if not hits:
            continue
        if r["alt_group"]:
            siblings = groups[r["alt_group"]]
            if any(not check_ingredient(s["ingredient_id"], diets) for s in siblings):
                continue  # a compliant alternative exists in the recipe itself
        for _d, sev, why in hits:
            out.append(DietViolation(r["ingredient_id"], sev, why,
                                     r["is_optional"] == "yes", r["is_signature"] == "yes",
                                     r["alt_group"]))
    return out


@dataclass
class EquipmentVerdict:
    feasible: bool
    missing: list[str] = field(default_factory=list)
    using_alternative: dict[str, str] = field(default_factory=dict)


def check_equipment(dish_id: str, have: set[str]) -> EquipmentVerdict:
    basic = {u for u, r in data.utensils().items() if r["is_basic_kit"] == "yes"}
    owned = set(have) | basic
    verdict = EquipmentVerdict(True)
    one_of = [r for r in data.dish_utensils().get(dish_id, []) if r["necessity"] == "one_of_required"]
    if one_of:
        pool = set()
        for r in one_of:
            pool.add(r["utensil_id"])
            pool.update(a for a in r["alternatives"].split("|") if a)
        hit = pool & owned
        if not hit:
            verdict.feasible = False
            verdict.missing.append("|".join(sorted(pool)))
        else:
            verdict.using_alternative["grinder"] = sorted(hit)[0]
    for r in data.dish_utensils().get(dish_id, []):
        if r["necessity"] != "required":
            continue
        u = r["utensil_id"]
        if u in owned:
            continue
        alts = [a for a in r["alternatives"].split("|") if a]
        use = next((a for a in alts if a in owned), None)
        if use:
            verdict.using_alternative[u] = use
        else:
            verdict.feasible = False
            verdict.missing.append(u)
    return verdict


def cost_dish(dish_id: str, include_optional: bool = False) -> dict:
    """Cost of the dish as written (alt-groups resolved the same way the planner does).
    Thin wrapper over plan.price so there is one costing code path."""
    from . import plan as planmod  # local import: plan imports rules
    opts = [r["ingredient_id"] for r in data.dish_ingredients()[dish_id] if r["is_optional"] == "yes"]
    p = planmod.Plan(dish_id, "adapt", include_optional=opts if include_optional else [])
    lines = planmod.materialize(p, planmod.Constraints())
    out = planmod.price(lines)
    return {"dish_id": dish_id, "prorated_sgd": out["prorated_sgd"],
            "cash_outlay_sgd": out["cash_outlay_sgd"],
            "unpriced_ingredients": out["unpriced_ingredients"], "lines": lines}
