"""Tools exposed to the LLM (OpenAI/OpenRouter function-calling format).

Every tool is a thin wrapper over deterministic code. The model gathers facts
with these and must finish by calling submit_plan, which runs plan.validate().
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from typing import Callable

from . import data, plan as planmod, retrieval, rules

DIET_KEYS = sorted(rules.DIETS)
UTENSIL_IDS = sorted(data.utensils())


def _schema(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required,
                       "additionalProperties": False}}}


STR_LIST = {"type": "array", "items": {"type": "string"}}

SCHEMAS = [
    _schema("search_dishes",
            "Find dishes in the knowledge base by name, alias (Telugu/Tamil/Kannada/Malay) or description. "
            "Empty query lists all 15 dishes. Only these dishes are supported.",
            {"query": {"type": "string"}}, ["query"]),
    _schema("get_dish",
            "Recipe lines for a dish: ingredient_id, quantity text, optional, signature (defines the dish), "
            "alt_group (recipe-endorsed either/or), plus required utensils and the source recipe URL.",
            {"dish_id": {"type": "string"}}, ["dish_id"]),
    _schema("resolve_ingredient",
            "Map an ingredient name in any language/spelling to ingredient_ids. Reports ambiguous names "
            "(e.g. 'drumstick' also means chicken in Singapore shops; 'yam' means taro).",
            {"text": {"type": "string"}}, ["text"]),
    _schema("check_diet",
            f"Deterministic diet check of a dish as written. Diet keys: {DIET_KEYS}. Returns blocked and "
            "warning lines, marking signature/optional lines and recipe alternatives that already comply.",
            {"dish_id": {"type": "string"}, "diets": {"type": "array", "items": {"type": "string", "enum": DIET_KEYS}}},
            ["dish_id", "diets"]),
    _schema("find_substitutes",
            "Reviewed substitutions for an ingredient (dish-specific first). Each has a human acceptability label, "
            "fidelity impact, evidence, whether it passes the diets, and its Singapore availability.",
            {"ingredient_id": {"type": "string"}, "dish_id": {"type": "string"},
             "diets": {"type": "array", "items": {"type": "string", "enum": DIET_KEYS}}},
            ["ingredient_id", "dish_id", "diets"]),
    _schema("check_availability",
            "Singapore availability for ingredient_ids: grade (Confirmed/Likely/Difficult/Unknown) derived from "
            "retailer evidence, the rule that fired, and the cheapest in-stock listing with URL and date. "
            "Never state availability that this tool did not return.",
            {"ingredient_ids": STR_LIST}, ["ingredient_ids"]),
    _schema("check_equipment",
            f"Can the dish be made with the user's equipment? Utensil ids: {UTENSIL_IDS}. Basic pots, pans and a "
            "hob are assumed. Returns missing items, alternatives in hand, and where to buy missing items.",
            {"dish_id": {"type": "string"}, "equipment_have": {"type": "array", "items": {"type": "string", "enum": UTENSIL_IDS}}},
            ["dish_id", "equipment_have"]),
    _schema("estimate_cost",
            "Singapore cost of a draft plan from in-stock listings: pro-rated cost and whole-pack cash outlay.",
            {"dish_id": {"type": "string"},
             "substitutions": {"type": "array", "items": {"type": "object", "properties": {
                 "original_id": {"type": "string"}, "substitute_id": {"type": ["string", "null"]}},
                 "required": ["original_id", "substitute_id"], "additionalProperties": False}},
             "include_optional": STR_LIST, "diets": STR_LIST},
            ["dish_id", "substitutions", "include_optional", "diets"]),
    _schema("submit_plan",
            "Submit the final plan. It is validated by code; if rejected you receive the issues and must fix and "
            "resubmit. Adapt the dish the user NAMED; abstain only when a signature ingredient is blocked by their "
            "constraints and no acceptable substitute exists (keep dish_id set to the named dish when abstaining; use null only "
            "if no supported dish matches). Ingredient lists, prices and stock are computed by code "
            "from your decisions, so do not restate them.",
            {"dish_id": {"type": ["string", "null"]},
             "outcome": {"type": "string", "enum": ["adapt", "abstain"]},
             "substitutions": {"type": "array", "items": {"type": "object", "properties": {
                 "original_id": {"type": "string"}, "substitute_id": {"type": ["string", "null"],
                                                                      "description": "null = omit the line"},
                 "reason": {"type": "string"}}, "required": ["original_id", "substitute_id", "reason"],
                 "additionalProperties": False}},
             "include_optional": {**STR_LIST, "description": "optional recipe lines to include"},
             "flagged_ingredient_ids": {**STR_LIST, "description": "ingredients the user must be warned about"},
             "flagged_equipment": {**STR_LIST, "description": "utensil ids the user lacks"},
             "budget_flagged": {"type": "boolean"},
             "constraints_inferred": {"type": "object", "properties": {
                 "diets": {"type": "array", "items": {"type": "string", "enum": DIET_KEYS}},
                 "equipment_have": {"type": "array", "items": {"type": "string", "enum": UTENSIL_IDS}},
                 "budget_sgd": {"type": ["number", "null"]},
                 "budget_basis": {"type": "string", "enum": ["cash_outlay", "prorated"]}},
                 "required": ["diets", "equipment_have", "budget_sgd", "budget_basis"], "additionalProperties": False},
             "explanation": {"type": "string", "description": "2-6 sentences to the user: what changed and why, caveats, "
                             "or why the dish cannot be made faithfully and what they could do"},
             "cooking_notes": {"type": "string", "description": "brief Singapore-specific method tips; point to the source recipe for full steps"}},
            ["dish_id", "outcome", "substitutions", "include_optional", "flagged_ingredient_ids",
             "flagged_equipment", "budget_flagged", "constraints_inferred", "explanation", "cooking_notes"]),
]


class Toolbox:
    """Executes tool calls. Holds the user's given constraints and the as-of date."""

    def __init__(self, given: planmod.Constraints, as_of: date | None = None, validate: bool = True,
                 max_rejections: int = 3):
        self.given, self.as_of, self.validate_on = given, as_of, validate
        self.max_rejections, self.rejections = max_rejections, 0
        self.final: planmod.Plan | None = None
        self.final_issues: list[dict] = []
        self.calls: list[dict] = []
        self._impl: dict[str, Callable[..., object]] = {
            "search_dishes": self.search_dishes, "get_dish": self.get_dish,
            "resolve_ingredient": self.resolve_ingredient, "check_diet": self.check_diet,
            "find_substitutes": self.find_substitutes, "check_availability": self.check_availability,
            "check_equipment": self.check_equipment, "estimate_cost": self.estimate_cost,
            "submit_plan": self.submit_plan,
        }

    def call(self, name: str, arguments: str | dict) -> str:
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
            if name not in self._impl:
                raise KeyError(f"unknown tool {name}")
            result = self._impl[name](**args)
        except Exception as e:  # tool errors go back to the model, not up the stack
            result = {"error": f"{type(e).__name__}: {e}"}
        self.calls.append({"tool": name, "args": arguments, "result": result})
        return json.dumps(result, ensure_ascii=False, default=str)

    # ---- tools
    def search_dishes(self, query: str):
        res = retrieval.search_dishes(query)
        return {"results": res[:15] if not query.strip() else res[:5],
                "note": "" if res else "No supported dish matches. Supported dishes: "
                + ", ".join(d["name"] for d in data.dishes().values())}

    def get_dish(self, dish_id: str):
        d = data.dishes()[dish_id]
        ingr = data.ingredients()
        return {"dish": {k: d[k] for k in ("dish_id", "name", "aliases", "region", "course", "yield_text",
                                           "needs_fermentation", "cooking_method", "source_url")},
                "lines": [{"ingredient_id": r["ingredient_id"], "name": ingr[r["ingredient_id"]]["canonical_name"],
                           "qty": r["qty_text"], "component": r["component"], "optional": r["is_optional"] == "yes",
                           "signature": r["is_signature"] == "yes", "alt_group": r["alt_group"] or None,
                           "recipe_alternative": r["recipe_alternative_text"] or None}
                          for r in data.dish_ingredients()[dish_id]],
                "utensils": [{"utensil_id": u["utensil_id"], "necessity": u["necessity"],
                              "alternatives": [a for a in u["alternatives"].split("|") if a]}
                             for u in data.dish_utensils().get(dish_id, [])]}

    def resolve_ingredient(self, text: str):
        return {"matches": retrieval.resolve_ingredient(text)}

    def check_diet(self, dish_id: str, diets: list[str]):
        rows = data.dish_ingredients()[dish_id]
        out = []
        for r in rows:
            hits = rules.check_ingredient(r["ingredient_id"], diets)
            if not hits:
                continue
            alts = [m["ingredient_id"] for m in rows if r["alt_group"] and m["alt_group"] == r["alt_group"]
                    and m["ingredient_id"] != r["ingredient_id"] and not planmod.blocked(m["ingredient_id"], diets)]
            out.append({"ingredient_id": r["ingredient_id"], "signature": r["is_signature"] == "yes",
                        "optional": r["is_optional"] == "yes",
                        "problems": [{"diet": d, "severity": s, "reason": w} for d, s, w in hits],
                        "recipe_alternative_that_complies": alts or None})
        return {"dish_id": dish_id, "diets": diets, "issues": out,
                "signature_blocked": [o["ingredient_id"] for o in out if o["signature"]
                                      and any(p["severity"] == "block" for p in o["problems"])]}

    def find_substitutes(self, ingredient_id: str, dish_id: str, diets: list[str]):
        res = []
        for s in planmod.sub_rows(ingredient_id, dish_id):
            sub = s["substitute_id"] or None
            res.append({"sub_id": s["sub_id"], "substitute_id": sub,
                        "substitute_name": data.ingredients()[sub]["canonical_name"] if sub else "(omit)",
                        "applies_to": s["context_dish_id"] or "any dish", "ratio": s["ratio"],
                        "prep_note": s["prep_note"], "fidelity_impact": s["fidelity_impact"],
                        "label": planmod.label_of(s), "label_source": "human" if s["human_label"] else "proposed",
                        "label_note": s["human_label_notes"], "evidence": s["evidence_type"],
                        "source_url": s["source_url"] or None,
                        "passes_diets": (not sub) or not planmod.blocked(sub, diets),
                        "availability": planmod.evidence(sub, self.as_of)["effective_grade"] if sub else None})
        return {"ingredient_id": ingredient_id, "dish_id": dish_id, "substitutes": res,
                "note": "" if res else "No reviewed substitutes. Omitting a non-signature line is allowed with a caveat; "
                                       "any other substitute you invent will be marked unverified."}

    def check_availability(self, ingredient_ids: list[str]):
        return {i: planmod.evidence(i, self.as_of) for i in ingredient_ids}

    def check_equipment(self, dish_id: str, equipment_have: list[str]):
        v = rules.check_equipment(dish_id, set(equipment_have))
        buy = {}
        for group in v.missing:
            for u in group.split("|"):
                buy[u] = planmod.evidence(u, self.as_of, "utensil")
        return {**asdict(v), "buy_options": buy}

    def estimate_cost(self, dish_id: str, substitutions: list[dict], include_optional: list[str], diets: list[str]):
        p = planmod.Plan.from_dict({"dish_id": dish_id, "outcome": "adapt", "substitutions": substitutions,
                                    "include_optional": include_optional})
        c = self.given.merged(planmod.Constraints(diets=diets))
        return planmod.price(planmod.materialize(p, c))

    def submit_plan(self, **kwargs):
        p = planmod.Plan.from_dict(kwargs)
        issues = planmod.validate(p, self.given, self.as_of)
        errors = [i for i in issues if i["severity"] == "error"]
        if errors and self.validate_on and self.rejections < self.max_rejections:
            self.rejections += 1
            return {"accepted": False, "issues": issues,
                    "instruction": "Fix every error and call submit_plan again."}
        self.final, self.final_issues = p, issues
        return {"accepted": True, "issues": issues}
