"""A plan = the agent's *decisions*; everything factual is derived here by code.

The model (or the rule planner) decides: which dish, adapt or abstain, which
substitutions/omissions, which optional lines to include, what to flag, and the
explanation. This module then deterministically:

  materialize()  -> the concrete ingredient list (alt-groups resolved, subs applied)
  validate()     -> rule violations the planner must fix before the plan is accepted
  price()        -> Singapore cost from in-stock listings (never estimated)
  evidence()     -> availability grade + best listing for every ingredient
  render_text()  -> the command-line answer
  to_eval_output() -> the record eval/score.py consumes

So the agent cannot invent an ingredient list, a price or a stock claim.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from . import data, rules
from .grading import effective_grade

OK_LABELS = {"acceptable", "acceptable_with_caveat"}
ALWAYS_FLAG_GRADES = {"Difficult", "Unknown"}


# ------------------------------------------------------------------ helpers
def blocked(ingredient_id: str, diets: list[str]) -> bool:
    return any(sev == "block" for _d, sev, _w in rules.check_ingredient(ingredient_id, diets))


def warned(ingredient_id: str, diets: list[str]) -> list[str]:
    return [f"{d}: {why}" for d, sev, why in rules.check_ingredient(ingredient_id, diets) if sev == "warn"]


def sub_rows(original: str, dish_id: str | None = None) -> list[dict]:
    rows = [s for s in data.read_csv("substitutions.csv")
            if s["item_type"] == "ingredient" and s["original_id"] == original
            and s["context_dish_id"] in ("", dish_id or "")]
    rows.sort(key=lambda s: 0 if s["context_dish_id"] == dish_id else 1)  # dish-specific first
    return rows


def label_of(s: dict) -> str:
    return s["human_label"] or s["claude_proposed_acceptability"]


def sub_verdict(original: str, substitute: str | None, dish_id: str) -> tuple[str, dict | None]:
    """(label, row). Unlisted omission of a non-signature line is policy-acceptable-with-caveat."""
    for s in sub_rows(original, dish_id):
        if (s["substitute_id"] or None) == (substitute or None):
            return label_of(s), s
    if substitute is None:
        sig = any(r["ingredient_id"] == original and r["is_signature"] == "yes"
                  for r in data.dish_ingredients()[dish_id])
        return ("unacceptable" if sig else "acceptable_with_caveat"), None
    return "unverified", None


# ------------------------------------------------------------------ plan type
@dataclass
class Constraints:
    diets: list[str] = field(default_factory=list)
    equipment_have: list[str] = field(default_factory=list)
    budget_sgd: float | None = None
    budget_basis: str = "cash_outlay"          # or "prorated"

    @classmethod
    def from_dict(cls, d: dict | None) -> "Constraints":
        d = d or {}
        return cls(list(d.get("diets", []) or []), list(d.get("equipment_have", []) or []),
                   d.get("budget_sgd"), d.get("budget_basis") or "cash_outlay")

    def merged(self, other: "Constraints") -> "Constraints":
        return Constraints(sorted(set(self.diets) | set(other.diets)),
                           sorted(set(self.equipment_have) | set(other.equipment_have)),
                           self.budget_sgd if self.budget_sgd is not None else other.budget_sgd,
                           self.budget_basis if self.budget_sgd is not None else other.budget_basis)

    def as_dict(self) -> dict:
        return {"diets": self.diets, "equipment_have": self.equipment_have,
                "budget_sgd": self.budget_sgd, "budget_basis": self.budget_basis}


@dataclass
class Plan:
    dish_id: str | None
    outcome: str                                  # "adapt" | "abstain"
    substitutions: list[dict] = field(default_factory=list)   # {original_id, substitute_id|None, reason}
    include_optional: list[str] = field(default_factory=list)
    flagged_ingredient_ids: list[str] = field(default_factory=list)
    flagged_equipment: list[str] = field(default_factory=list)
    budget_flagged: bool = False
    explanation: str = ""
    cooking_notes: str = ""
    constraints_inferred: Constraints = field(default_factory=Constraints)

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        subs = []
        for s in d.get("substitutions", []) or []:
            subs.append({"original_id": s.get("original_id"),
                         "substitute_id": s.get("substitute_id") or None,
                         "reason": s.get("reason", "")})
        return cls(d.get("dish_id") or None, d.get("outcome", "adapt"), subs,
                   list(d.get("include_optional", []) or []),
                   list(d.get("flagged_ingredient_ids", []) or []),
                   list(d.get("flagged_equipment", []) or []),
                   bool(d.get("budget_flagged", False)), d.get("explanation", "") or "",
                   d.get("cooking_notes", "") or "",
                   Constraints.from_dict(d.get("constraints_inferred")))


# ------------------------------------------------------------------ derivation
@dataclass
class Line:
    ingredient_id: str          # what is actually used
    original_id: str            # recipe line it came from
    qty_g: float
    qty_text: str
    is_signature: bool
    action: str                 # keep | substitute | alt_choice
    sub_label: str = ""


def materialize(plan: Plan, c: Constraints) -> list[Line]:
    """Concrete ingredient lines after alt-groups, optional choices and substitutions."""
    if plan.outcome != "adapt" or plan.dish_id not in data.dishes():
        return []
    rows = data.dish_ingredients()[plan.dish_id]
    subs = {s["original_id"]: s["substitute_id"] for s in plan.substitutions}
    lines: list[Line] = []
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r["alt_group"]:
            groups.setdefault(r["alt_group"], []).append(r)
    done_groups: set[str] = set()
    for r in rows:
        iid = r["ingredient_id"]
        if r["is_optional"] == "yes" and iid not in plan.include_optional:
            continue
        if r["alt_group"]:
            g = r["alt_group"]
            if g in done_groups:
                continue
            done_groups.add(g)
            members = [m for m in groups[g] if m["is_optional"] != "yes" or m["ingredient_id"] in plan.include_optional]
            explicit = [m for m in members if m["ingredient_id"] in subs]
            compliant = [m for m in members if m["ingredient_id"] not in subs and not blocked(m["ingredient_id"], c.diets)]
            r = compliant[0] if compliant else (explicit[0] if explicit else members[0])
            iid = r["ingredient_id"]
            if compliant:
                lines.append(Line(iid, iid, float(r["qty_g"]), r["qty_text"], r["is_signature"] == "yes",
                                  "alt_choice" if len(members) > 1 else "keep"))
                continue
        if iid in subs:
            new = subs[iid]
            label, _row = sub_verdict(iid, new, plan.dish_id)
            if new:
                lines.append(Line(new, iid, float(r["qty_g"]), r["qty_text"], r["is_signature"] == "yes",
                                  "substitute", label))
            continue  # omitted
        lines.append(Line(iid, iid, float(r["qty_g"]), r["qty_text"], r["is_signature"] == "yes", "keep"))
    return lines


def evidence(ingredient_id: str, as_of: date | None = None, item_type: str = "ingredient") -> dict:
    a = data.availability().get((item_type, ingredient_id))
    if not a:
        return {"grade": "Unknown", "effective_grade": "Unknown", "rule": "not_graded"}
    best = None
    ls = [l for l in data.listings() if l["item_type"] == item_type and l["item_id"] == ingredient_id
          and l["is_latest"] == "yes" and l["match"] in ("exact", "variant")]
    in_stock = [l for l in ls if l["match"] == "exact" and l["stock_status"] == "in_stock" and l["price_sgd"]]
    pool = in_stock or [l for l in ls if l["match"] == "exact"] or ls
    if pool:
        best = min(pool, key=lambda l: float(l["price_sgd"] or 1e9))
    retailers = {r["retailer_id"]: r["name"] for r in data.read_csv("retailers.csv")}
    return {
        "grade": a["grade"], "effective_grade": effective_grade(a["grade"], a["verified_on"], as_of),
        "rule": a["rule"], "confidence": float(a["confidence"]), "verified_on": a["verified_on"],
        "n_retailers_in_stock": int(a["n_retailers_in_stock_exact"]),
        "best_listing": None if not best else {
            "listing_id": best["listing_id"], "retailer": retailers[best["retailer_id"]],
            "product": best["product_title"], "price_sgd": best["price_sgd"] or None,
            "stock": best["stock_status"], "match": best["match"], "url": best["product_url"]},
    }


def price(lines: list[Line]) -> dict:
    pr, out, unpriced = 0.0, 0.0, []
    qty: dict[str, float] = {}
    for ln in lines:  # the same ingredient can come from two lines (e.g. a substitute already in the recipe)
        qty[ln.ingredient_id] = qty.get(ln.ingredient_id, 0.0) + ln.qty_g
    for iid, q in qty.items():
        ln = Line(iid, iid, q, "", False, "keep")
        opts = [l for l in data.listings() if l["item_type"] == "ingredient" and l["item_id"] == ln.ingredient_id
                and l["is_latest"] == "yes" and l["match"] == "exact" and l["stock_status"] == "in_stock"
                and l["price_sgd"] and l["pack_g"]]
        if not opts:
            unpriced.append(ln.ingredient_id)
            continue
        pr += ln.qty_g * min(float(o["price_sgd"]) / float(o["pack_g"]) for o in opts)
        out += min(float(o["price_sgd"]) * max(1, math.ceil(ln.qty_g / float(o["pack_g"]))) for o in opts)
    return {"prorated_sgd": round(pr, 2), "cash_outlay_sgd": round(out, 2),
            "unpriced_ingredients": sorted(set(unpriced))}


# ------------------------------------------------------------------ validation
def required_flags(plan: Plan, c: Constraints, lines: list[Line], as_of: date | None = None) -> set[str]:
    need = set()
    # a hard-to-find original that was replaced or dropped must still be explained
    for sub in plan.substitutions:
        if sub["original_id"] in data.ingredients() and \
                evidence(sub["original_id"], as_of)["grade"] in ALWAYS_FLAG_GRADES:
            need.add(sub["original_id"])
    for ln in lines:
        if evidence(ln.ingredient_id, as_of)["grade"] in ALWAYS_FLAG_GRADES:
            need.add(ln.ingredient_id)
        if warned(ln.ingredient_id, c.diets):
            need.add(ln.ingredient_id)
    return need


def validate(plan: Plan, given: Constraints, as_of: date | None = None) -> list[dict]:
    """Return issues; severity 'error' blocks acceptance, 'warning' is reported."""
    c = given.merged(plan.constraints_inferred)
    issues: list[dict] = []

    def err(code, msg, **kw):
        issues.append({"severity": "error", "code": code, "message": msg, **kw})

    def warn(code, msg, **kw):
        issues.append({"severity": "warning", "code": code, "message": msg, **kw})

    for d in c.diets:
        if d not in rules.DIETS:
            err("UNKNOWN_DIET", f"unknown diet key '{d}'; valid: {sorted(rules.DIETS)}")
    if plan.outcome not in ("adapt", "abstain"):
        err("BAD_OUTCOME", "outcome must be 'adapt' or 'abstain'")
        return issues
    if plan.dish_id is not None and plan.dish_id not in data.dishes():
        err("UNKNOWN_DISH", f"dish_id '{plan.dish_id}' is not in the knowledge base; use search_dishes")
        return issues
    if plan.outcome == "abstain":
        if len(plan.explanation.strip()) < 20:
            err("ABSTAIN_NO_REASON", "abstaining requires an explanation the user can act on")
        if plan.dish_id:
            sig_blocked = [r["ingredient_id"] for r in data.dish_ingredients()[plan.dish_id]
                           if r["is_signature"] == "yes" and blocked(r["ingredient_id"], c.diets)]
            rescuable = [i for i in sig_blocked if any(
                s["substitute_id"] and label_of(s) in OK_LABELS and not blocked(s["substitute_id"], c.diets)
                for s in sub_rows(i, plan.dish_id))]
            if not sig_blocked or len(rescuable) == len(sig_blocked):
                err("ABSTAIN_AVOIDABLE",
                    "no signature ingredient is irreconcilably blocked - adapt the dish the user named "
                    "instead of refusing (lazy abstention)")
        return issues
    if plan.dish_id is None:
        err("NO_DISH", "adapting requires a dish_id")
        return issues

    rows = {r["ingredient_id"]: r for r in data.dish_ingredients()[plan.dish_id]}
    for s in plan.substitutions:
        o = s["original_id"]
        if o not in rows:
            err("SUB_NOT_IN_DISH", f"'{o}' is not a line in {plan.dish_id}")
            continue
        if s["substitute_id"] and s["substitute_id"] not in data.ingredients():
            err("UNKNOWN_INGREDIENT", f"substitute '{s['substitute_id']}' is not a known ingredient")
            continue
        label, _row = sub_verdict(o, s["substitute_id"], plan.dish_id)
        target = s["substitute_id"] or "(omit)"
        if label == "unacceptable":
            err("SUB_UNACCEPTABLE", f"{o} -> {target} is labelled unacceptable for {plan.dish_id}")
        elif label == "unverified":
            warn("SUB_UNVERIFIED", f"{o} -> {target} has no reviewed label; tell the user it is untested")
    for o in plan.include_optional:
        if o not in rows:
            err("OPTIONAL_NOT_IN_DISH", f"'{o}' is not a line in {plan.dish_id}")
    if any(i["severity"] == "error" for i in issues):
        return issues

    lines = materialize(plan, c)
    for ln in lines:
        if blocked(ln.ingredient_id, c.diets):
            err("FORBIDDEN_USED", f"{ln.ingredient_id} violates {c.diets}; substitute or omit it",
                ingredient_id=ln.ingredient_id)
    used_originals = {ln.original_id for ln in lines}
    for iid, r in rows.items():
        if r["is_signature"] != "yes":
            continue
        if iid not in used_originals and not r["alt_group"]:
            err("SIGNATURE_DROPPED", f"signature ingredient {iid} was omitted - the result is no longer "
                f"{plan.dish_id}; keep it, use an acceptable substitute, or abstain", ingredient_id=iid)
    for ln in lines:
        if ln.is_signature and ln.action == "substitute" and ln.sub_label not in OK_LABELS:
            err("SIGNATURE_BAD_SUB", f"signature {ln.original_id} replaced by {ln.ingredient_id} "
                f"(label: {ln.sub_label})", ingredient_id=ln.original_id)
    missing_flags = sorted(required_flags(plan, c, lines, as_of) - set(plan.flagged_ingredient_ids))
    for m in missing_flags:
        err("FLAG_MISSING", f"{m} must be flagged to the user (hard to find in Singapore or label-dependent)",
            ingredient_id=m)
    eq = rules.check_equipment(plan.dish_id, set(c.equipment_have))
    fl = set(plan.flagged_equipment)
    for group in eq.missing:
        if not (group in fl or any(u in fl for u in group.split("|"))):
            err("EQUIPMENT_UNFLAGGED", f"user lacks {group.replace('|', ' / ')}; flag it and give options")
    if c.budget_sgd is not None:
        p = price(lines)
        cost = p["cash_outlay_sgd"] if c.budget_basis == "cash_outlay" else p["prorated_sgd"]
        if cost > c.budget_sgd and not plan.budget_flagged:
            err("BUDGET_UNFLAGGED", f"{c.budget_basis} cost S${cost:.2f} exceeds budget S${c.budget_sgd:.2f}")
    return issues


# ------------------------------------------------------------------ outputs
def to_eval_output(scenario_id: str, plan: Plan, given: Constraints, as_of: date | None = None) -> dict:
    c = given.merged(plan.constraints_inferred)
    lines = materialize(plan, c)
    p = price(lines)
    return {
        "scenario_id": scenario_id, "dish_id_addressed": plan.dish_id,
        "outcome": plan.outcome, "ingredients_used": [ln.ingredient_id for ln in lines],
        "substitutions": [{"original_id": s["original_id"], "substitute_id": s["substitute_id"]}
                          for s in plan.substitutions],
        "flagged_ingredient_ids": plan.flagged_ingredient_ids,
        "flagged_equipment": plan.flagged_equipment,
        "availability_claims": {ln.ingredient_id: evidence(ln.ingredient_id, as_of)["effective_grade"]
                                for ln in lines},
        "claimed_cost_sgd": (p["cash_outlay_sgd"] if c.budget_basis == "cash_outlay" else p["prorated_sgd"])
                            if plan.outcome == "adapt" else None,
        "budget_flagged": plan.budget_flagged,
    }


MARK = {"Confirmed": "[ok]  ", "Likely": "[?]   ", "Difficult": "[!]   ", "Unknown": "[??]  "}


def render_text(plan: Plan, given: Constraints, issues: list[dict], as_of: date | None = None) -> str:
    c = given.merged(plan.constraints_inferred)
    out = []
    ingr = data.ingredients()
    if plan.dish_id:
        d = data.dishes()[plan.dish_id]
        head = f"{d['name']}  ({d['region']})"
    else:
        head = "No matching dish in the knowledge base"
    status = {"adapt": "ADAPTED FOR SINGAPORE", "abstain": "CANNOT ADAPT FAITHFULLY"}[plan.outcome]
    out += [f"== {head} == {status}", ""]
    if c.diets or c.equipment_have or c.budget_sgd is not None:
        b = f"S${c.budget_sgd:.2f} ({c.budget_basis.replace('_', ' ')})" if c.budget_sgd is not None else "-"
        out += [f"Constraints: diet={', '.join(c.diets) or '-'} | equipment={', '.join(c.equipment_have) or 'basic pots/pans'} | budget={b}", ""]
    if plan.explanation:
        out += [plan.explanation.strip(), ""]
    if plan.outcome == "adapt" and plan.dish_id:
        lines = materialize(plan, c)
        out.append(f"Ingredients - Singapore availability (evidence date {lines and evidence(lines[0].ingredient_id, as_of).get('verified_on', '')}):")
        for ln in lines:
            ev = evidence(ln.ingredient_id, as_of)
            name = ingr[ln.ingredient_id]["canonical_name"]
            qty = f"{ln.qty_g:g} g"
            tag = MARK.get(ev["effective_grade"], "")
            sub = f"  <- replaces {ingr[ln.original_id]['canonical_name']} ({ln.sub_label.replace('_', ' ')})" \
                if ln.action == "substitute" else ""
            out.append(f"  {tag}{ev['effective_grade']:<9} {name} - {qty}{sub}")
            b = ev.get("best_listing")
            if b:
                pricetxt = f"S${b['price_sgd']}" if b["price_sgd"] else "price n/a"
                stock = "" if b["stock"] == "in_stock" else f" [{b['stock'].replace('_', ' ')}]"
                out.append(f"             {b['product']} - {pricetxt} @ {b['retailer']}{stock}  {b['url']}")
        omitted = [s["original_id"] for s in plan.substitutions if not s["substitute_id"]]
        if omitted:
            out.append("  Left out: " + ", ".join(ingr[o]["canonical_name"] for o in omitted))
        p = price(lines)
        out += ["", f"Cost: S${p['prorated_sgd']:.2f} for the quantities used; "
                    f"S${p['cash_outlay_sgd']:.2f} to buy whole packs from an empty pantry"
                + (f" (unpriced: {', '.join(p['unpriced_ingredients'])})" if p["unpriced_ingredients"] else "")]
        eq = rules.check_equipment(plan.dish_id, set(c.equipment_have))
        if eq.missing or eq.using_alternative:
            out.append("Equipment:")
            for k, v in eq.using_alternative.items():
                out.append(f"  use {v.replace('_', ' ')} for {k.replace('_', ' ')}")
            for m in eq.missing:
                opts = []
                for u in m.split("|"):
                    ev = evidence(u, as_of, "utensil")
                    b = ev.get("best_listing")
                    if b and b["price_sgd"]:
                        opts.append(f"{u.replace('_', ' ')} from S${b['price_sgd']} @ {b['retailer']}")
                out.append(f"  MISSING {m.replace('|', ' or ').replace('_', ' ')}"
                           + (f" - e.g. {'; '.join(opts)}" if opts else ""))
        if plan.cooking_notes:
            out += ["", "Method notes:", plan.cooking_notes.strip()]
        out += ["", f"Source recipe: {data.dishes()[plan.dish_id]['source_url']}"]
    errs = [i for i in issues if i["severity"] == "error"]
    warns = [i for i in issues if i["severity"] == "warning"]
    if errs:
        out += ["", "UNRESOLVED CHECKS (plan did not fully pass validation):"] + [f"  - {i['message']}" for i in errs]
    if warns:
        out += ["", "Notes:"] + [f"  - {i['message']}" for i in warns]
    out += ["", "Availability grades come from retailer catalogues; they are evidence, not a stock guarantee."]
    return "\n".join(out)
