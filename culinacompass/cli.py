"""Command line for CulinaCompass SG.

  python3 -m culinacompass ask "I miss my amma's gongura pachadi" --diet vegan
  python3 -m culinacompass ask "idli for breakfast" --have blender --budget 10 --offline
  python3 -m culinacompass chat                      # interactive, keeps context
  python3 -m culinacompass dishes                    # list supported dishes
  python3 -m culinacompass lookup "asam jawa"        # ingredient -> Singapore evidence

Needs OPENROUTER_API_KEY for the LLM agent; --offline uses the rule-based planner.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from . import data, plan as planmod, retrieval, rules
from .agent import run_agent
from .llm import DEFAULT_MODEL, LLMError, OpenRouterClient
from .rule_planner import parse_request_rules, plan_rules


def _constraints(a) -> planmod.Constraints:
    return planmod.Constraints(a.diet or [], a.have or [], a.budget, a.basis)


def _as_of(a) -> date | None:
    return date.fromisoformat(a.as_of) if a.as_of else None


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--diet", action="append", choices=sorted(rules.DIETS), help="repeatable")
    p.add_argument("--have", action="append", choices=sorted(data.utensils()), metavar="UTENSIL",
                   help=f"repeatable; one of {', '.join(sorted(data.utensils()))}")
    p.add_argument("--budget", type=float, help="budget in SGD")
    p.add_argument("--basis", choices=["cash_outlay", "prorated"], default="cash_outlay",
                   help="cash_outlay = buy whole packs (empty pantry); prorated = cost of quantities used")
    p.add_argument("--offline", action="store_true", help="rule-based planner, no LLM")
    p.add_argument("--model", default=DEFAULT_MODEL, help="OpenRouter model id")
    p.add_argument("--no-validate", action="store_true", help="ablation: accept the first submitted plan")
    p.add_argument("--as-of", help="YYYY-MM-DD for grade staleness (default: today)")
    p.add_argument("--json", action="store_true", help="print the plan as JSON")
    p.add_argument("--trace", action="store_true", help="print tool calls")


def answer(text: str, a, llm=None, history=None) -> tuple[planmod.Plan | None, list[dict], str]:
    given = _constraints(a)
    if a.offline:
        dish, parsed = parse_request_rules(text)
        c = given.merged(parsed)
        p = plan_rules(dish, c)
        issues = planmod.validate(p, given, _as_of(a))
        return p, issues, "rule-based planner"
    res = run_agent(text, llm, given, _as_of(a), validate=not a.no_validate, history=history)
    if a.trace:
        for c in res.tool_calls:
            args = c["args"] if isinstance(c["args"], str) else json.dumps(c["args"])
            print(f"  > {c['tool']}({args[:160]})", file=sys.stderr)
    note = f"{llm.model}; {res.steps} steps, {res.rejections} rejected submissions ({res.stopped_reason})"
    return res.plan, res.issues, note


def show(p, issues, note, a) -> None:
    if p is None:
        print(f"No plan produced ({note}).")
        return
    given = _constraints(a)
    if a.json:
        print(json.dumps({"plan": {**p.__dict__, "constraints_inferred": p.constraints_inferred.as_dict()},
                          "issues": issues,
                          "eval_record": planmod.to_eval_output("cli", p, given, _as_of(a))}, indent=2, default=str))
    else:
        print(planmod.render_text(p, given, issues, _as_of(a)))
        print(f"\n({note})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="culinacompass", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_ask = sub.add_parser("ask", help="one question")
    p_ask.add_argument("text")
    _add_common(p_ask)
    p_chat = sub.add_parser("chat", help="interactive session")
    _add_common(p_chat)
    sub.add_parser("dishes", help="list supported dishes")
    p_lk = sub.add_parser("lookup", help="ingredient name -> Singapore evidence")
    p_lk.add_argument("text")
    a = ap.parse_args(argv)

    if a.cmd == "dishes":
        for d in data.dishes().values():
            print(f"{d['dish_id']:<20} {d['name']:<45} {d['region']:<18} aka {d['aliases'].replace('|', ', ')}")
        return 0
    if a.cmd == "lookup":
        for m in retrieval.resolve_ingredient(a.text):
            ev = planmod.evidence(m["ingredient_id"])
            b = ev.get("best_listing") or {}
            amb = "  (AMBIGUOUS name)" if m["ambiguous"] else ""
            print(f"{m['canonical_name']} [{m['ingredient_id']}]{amb}: {ev['effective_grade']} ({ev['rule']})")
            if b:
                print(f"   {b['product']} - S${b['price_sgd']} @ {b['retailer']} [{b['stock']}] {b['url']}")
        return 0

    llm = None
    if not a.offline:
        try:
            llm = OpenRouterClient(a.model)
        except LLMError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    if a.cmd == "ask":
        try:
            p, issues, note = answer(a.text, a, llm)
        except LLMError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        show(p, issues, note, a)
        return 0

    print("CulinaCompass SG - ask about a South Indian dish (blank line or 'quit' to exit).")
    history: list[dict] = []
    while True:
        try:
            text = input("\nyou> ").strip()
        except EOFError:
            break
        if text.lower() in ("", "quit", "exit"):
            break
        try:
            p, issues, note = answer(text, a, llm, history)
        except LLMError as e:
            print(f"error: {e}")
            continue
        show(p, issues, note, a)
        if p is not None:  # keep compact context for follow-ups ("and if I'm vegan?")
            history += [{"role": "user", "content": text},
                        {"role": "assistant", "content": f"[previous plan] dish={p.dish_id} outcome={p.outcome} "
                         f"constraints={p.constraints_inferred.as_dict()} subs={p.substitutions} :: {p.explanation}"}]
            history = history[-6:]
    return 0


if __name__ == "__main__":
    sys.exit(main())
