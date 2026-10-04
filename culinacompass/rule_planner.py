"""Rule-based baseline: no language model.

  parse_request_rules(text) -> (dish_id, Constraints) via alias match + keyword regexes
  plan_rules(dish_id, constraints) -> Plan using the same data and policies as the agent

This is the comparison point the project proposal asked for. It is strong on
structured input and brittle on free text (unseen phrasings, negation, implied
constraints) - which is exactly where the LLM agent has to earn its keep.
"""
from __future__ import annotations

import re

from . import data, plan as planmod, retrieval, rules

_DIET_PATTERNS = [
    (r"\bvegan\b", ["vegan"]),
    (r"\bjain\b", ["jain"]),
    (r"coeliac|celiac|gluten", ["gluten_free"]),
    (r"peanut|groundnut", ["no_peanut"]),
    (r"cashew|tree ?nut|almond", ["no_tree_nut"]),
    (r"no nuts|nut[- ]free|nuts of any kind|allergic to nuts", ["no_peanut", "no_tree_nut"]),
    (r"sesame", ["no_sesame"]),
    (r"dairy|lactose", ["no_dairy"]),
    (r"coconut allerg|no coconut", ["no_coconut"]),
    (r"no onion|no garlic|without onion|without garlic|ekadashi|satvik|sattvic", ["no_onion_garlic"]),
]
_EQUIP_PATTERNS = [
    (r"mixer grinder|mixie|mixer", "mixer_grinder"),
    (r"wet grinder", "wet_grinder"),
    (r"\bblender\b", "blender"),
    (r"idli stand|idli cooker|idli plates", "idli_steamer"),
    (r"steamer", "steamer_pot"),
    (r"pressure cooker|cooker", "pressure_cooker"),
    (r"instant pot", "electric_pressure_cooker"),
    (r"\btawa\b", "dosa_tawa"),
    (r"kadai|wok", "kadai"),
]
_NEG = r"(no|without|don't have|do not have|lack|not)\b[^.,;]{0,25}"
_BUDGET = re.compile(r"(?:s\$|sgd\s*|\$)\s*(\d+(?:\.\d+)?)", re.I)


def _n(iid: str) -> str:
    return data.ingredients()[iid]["canonical_name"]


def parse_request_rules(text: str) -> tuple[str | None, planmod.Constraints]:
    t = text.lower()
    hits = retrieval.search_dishes(text)
    dish_id = hits[0]["dish_id"] if hits and hits[0]["score"] >= 1.0 else None
    diets: list[str] = []
    for pat, keys in _DIET_PATTERNS:
        if re.search(pat, t):
            diets += [k for k in keys if k not in diets]
    have = []
    for pat, uid in _EQUIP_PATTERNS:
        m = re.search(pat, t)
        if m and not re.search(_NEG + pat, t) and uid not in have:
            have.append(uid)
    m = _BUDGET.search(text)
    budget = float(m.group(1)) if m else None
    basis = "prorated" if re.search(r"already have|have basic spices|in my pantry", t) else "cash_outlay"
    return dish_id, planmod.Constraints(diets, have, budget, basis)


def plan_rules(dish_id: str | None, c: planmod.Constraints) -> planmod.Plan:
    if dish_id is None or dish_id not in data.dishes():
        return planmod.Plan(None, "abstain", explanation=(
            "That dish is not in the knowledge base yet. Supported dishes: "
            + ", ".join(d["name"] for d in data.dishes().values()) + "."))
    rows = data.dish_ingredients()[dish_id]
    group_ok = {}
    for r in rows:
        if r["alt_group"]:
            group_ok.setdefault(r["alt_group"], False)
            if r["is_optional"] != "yes" and not planmod.blocked(r["ingredient_id"], c.diets):
                group_ok[r["alt_group"]] = True

    subs, reasons = [], []
    for r in rows:
        iid = r["ingredient_id"]
        if r["is_optional"] == "yes" or (r["alt_group"] and group_ok[r["alt_group"]]):
            continue
        grade = planmod.evidence(iid)["grade"]
        is_blocked = planmod.blocked(iid, c.diets)
        hard_to_find = grade in ("Difficult", "Unknown")
        if not (is_blocked or hard_to_find):
            continue
        cands = [s for s in planmod.sub_rows(iid, dish_id)
                 if s["substitute_id"] and planmod.label_of(s) in planmod.OK_LABELS
                 and not planmod.blocked(s["substitute_id"], c.diets)
                 and planmod.evidence(s["substitute_id"])["grade"] in ("Confirmed", "Likely")]
        cands.sort(key=lambda s: (planmod.label_of(s) != "acceptable",
                                  {"low": 0, "medium": 1, "high": 2}.get(s["fidelity_impact"], 3)))
        if cands:
            s = cands[0]
            subs.append({"original_id": iid, "substitute_id": s["substitute_id"],
                         "reason": f"{s['sub_id']} ({planmod.label_of(s)})"})
            reasons.append(f"{_n(iid)} -> {_n(s['substitute_id'])} ({planmod.label_of(s).replace('_', ' ')})")
        elif is_blocked and r["is_signature"] == "yes":
            return planmod.Plan(dish_id, "abstain", constraints_inferred=c, explanation=(
                f"{data.ingredients()[iid]['canonical_name']} defines {data.dishes()[dish_id]['name']} but is ruled "
                f"out by {', '.join(c.diets)}, and no reviewed substitute keeps the dish recognisable."))
        elif is_blocked:
            subs.append({"original_id": iid, "substitute_id": None, "reason": "omitted for diet"})
            reasons.append(f"leave out {_n(iid)}")
    p = planmod.Plan(dish_id, "adapt", subs, constraints_inferred=c)
    lines = planmod.materialize(p, c)
    p.flagged_ingredient_ids = sorted(planmod.required_flags(p, c, lines))
    eq = rules.check_equipment(dish_id, set(c.equipment_have))
    p.flagged_equipment = list(eq.missing)
    if c.budget_sgd is not None:
        cost = planmod.price(lines)["cash_outlay_sgd" if c.budget_basis == "cash_outlay" else "prorated_sgd"]
        p.budget_flagged = cost > c.budget_sgd
    p.explanation = ("Rule-based plan. " + ("Changes: " + "; ".join(reasons) + ". " if reasons else "No changes needed. ")
                     + ("Check before you shop: " + ", ".join(_n(i) for i in p.flagged_ingredient_ids) + ". "
                        if p.flagged_ingredient_ids else ""))
    return p
