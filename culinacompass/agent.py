"""The CulinaCompass agent: an LLM that plans through deterministic tools.

Loop: the model calls tools to identify the dish and gather facts, then calls
submit_plan. Code validates the plan; on errors the model gets the issues and
re-plans (up to max_rejections). The model never states prices or stock itself.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from . import plan as planmod
from .tools import SCHEMAS, Toolbox

SYSTEM_PROMPT = """You are CulinaCompass, an assistant that helps people who moved to Singapore cook the South Indian \
home dishes they grew up with. You rebuild the dish the user NAMED for Singapore: what is available, what to \
substitute, which utensils to use.

How to work:
1. Identify the dish with search_dishes (users write Telugu/Tamil/Kannada names, spellings vary). If nothing \
matches, submit_plan with dish_id null and outcome "abstain", listing what is supported.
2. Work out the user's constraints from their words and the profile: diets (vegetarian, vegan, jain, gluten_free, \
no_peanut, no_tree_nut, no_dairy, no_sesame, no_coconut, no_onion_garlic), equipment they have, budget. \
"Coeliac" means gluten_free; a cashew allergy means no_tree_nut; "no nuts" means both nut keys; Ekadashi/pooja \
cooking usually means no_onion_garlic. Put them in constraints_inferred.
3. get_dish, then check_diet, check_availability for the required lines, check_equipment, and estimate_cost if \
there is a budget.
4. For each blocked or hard-to-find line call find_substitutes. Prefer substitutes labelled "acceptable", then \
"acceptable_with_caveat". Never use one labelled "unacceptable" or one that fails the diets. A non-signature line \
may be omitted (substitute_id null). Recipe alt_groups already offer compliant choices - code picks them for you.
5. Decide: ADAPT whenever the dish can stay recognisably itself. ABSTAIN only when a signature ingredient is blocked \
by the user's constraints and no acceptable substitute exists - then explain honestly and suggest what they could \
do. Never quietly switch to a different dish: recommending some other "safe" dish is a failure. When you abstain \
because of a blocked signature ingredient, still submit the dish_id of the dish the user named (dish_id null is only \
for requests that match no supported dish).
6. Flag (flagged_ingredient_ids) every ingredient graded Difficult or Unknown and every label-dependent warning \
(e.g. hing may contain wheat). Flag missing equipment in flagged_equipment with utensil ids. Set budget_flagged if \
the cost exceeds the budget.
7. Availability and prices come ONLY from tools and are rendered by code - do not invent shops, prices or stock. \
Keep explanation to 2-6 plain sentences for the user; cooking_notes are short Singapore-specific tips.

Finish by calling submit_plan. If it is rejected, fix every listed error and submit again."""


@dataclass
class AgentResult:
    plan: planmod.Plan | None
    issues: list[dict]
    steps: int
    transcript: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    rejections: int = 0
    stopped_reason: str = ""


def profile_block(c: planmod.Constraints) -> str:
    if not (c.diets or c.equipment_have or c.budget_sgd is not None):
        return ""
    return ("\n\n[User profile]\n" + json.dumps(c.as_dict()))


def run_agent(request: str, llm, given: planmod.Constraints | None = None, as_of: date | None = None,
              validate: bool = True, max_steps: int = 20, max_rejections: int = 3,
              history: list[dict] | None = None) -> AgentResult:
    given = given or planmod.Constraints()
    box = Toolbox(given, as_of, validate=validate, max_rejections=max_rejections)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + list(history or [])
    messages.append({"role": "user", "content": request + profile_block(given)})
    nudges = 0
    for step in range(1, max_steps + 1):
        msg = llm.chat(messages, SCHEMAS)
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content"),
                         **({"tool_calls": calls} if calls else {})})
        if not calls:
            nudges += 1
            if nudges > 2:
                return AgentResult(None, [], step, messages, box.calls, box.rejections, "model stopped without a plan")
            messages.append({"role": "user", "content": "Please finish by calling submit_plan."})
            continue
        for tc in calls:
            fn = tc["function"]
            result = box.call(fn["name"], fn.get("arguments") or "{}")
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        if box.final is not None:
            return AgentResult(box.final, box.final_issues, step, messages, box.calls, box.rejections, "submitted")
    return AgentResult(None, [], max_steps, messages, box.calls, box.rejections, "step limit reached")
