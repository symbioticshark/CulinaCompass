#!/usr/bin/env python3
"""Integrity checks for the Singapore dataset. Exit code 1 on any failure.

Checks
  1. Referential integrity across every table.
  2. Every graded item's grade re-derives from its evidence rows (no hand edits).
  3. Every Confirmed item has >= 1 in-stock exact listing with a URL and date.
  4. Every dish ingredient and specialty dish utensil has an availability row.
  5. Alt-groups have >= 2 members; signature ingredients are never optional.
  6. Substitutions reference known items; sourced rows carry a URL.
  7. Scope caps (1 destination, 1 cuisine, 15 dishes, <= 80 ingredients).
  8. Eval scenarios reference known dishes/ingredients/utensils/diets.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from culinacompass import data, rules  # noqa: E402
from culinacompass.grading import Evidence, grade_item  # noqa: E402

errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        errors.append(msg)


def main() -> int:
    ings, utens, dishes = data.ingredients(), data.utensils(), data.dishes()
    retailers = {r["retailer_id"] for r in data.read_csv("retailers.csv")}
    listings = data.listings()
    avail = data.availability()

    # 1. referential integrity
    for r in data.read_csv("dish_ingredients.csv"):
        check(r["dish_id"] in dishes, f"dish_ingredients: unknown dish {r['dish_id']}")
        check(r["ingredient_id"] in ings, f"dish_ingredients: unknown ingredient {r['ingredient_id']}")
        try:
            check(float(r["qty_g"]) > 0, f"dish_ingredients: non-positive qty {r}")
        except ValueError:
            errors.append(f"dish_ingredients: bad qty_g {r['dish_id']}/{r['ingredient_id']}")
    for r in data.read_csv("dish_utensils.csv"):
        check(r["dish_id"] in dishes, f"dish_utensils: unknown dish {r['dish_id']}")
        for u in [r["utensil_id"]] + [a for a in r["alternatives"].split("|") if a]:
            check(u in utens, f"dish_utensils: unknown utensil {u}")
    for r in data.read_csv("ingredient_aliases.csv"):
        check(r["ingredient_id"] in ings, f"aliases: unknown ingredient {r['ingredient_id']}")
    for l in listings:
        check(l["retailer_id"] in retailers, f"listing {l['listing_id']}: unknown retailer")
        pool = ings if l["item_type"] == "ingredient" else utens
        check(l["item_id"] in pool, f"listing {l['listing_id']}: unknown {l['item_type']} {l['item_id']}")
        if l["match"] in ("exact", "variant"):
            check(bool(l["product_url"]), f"listing {l['listing_id']}: match without URL")
        check(bool(l["verified_on"]), f"listing {l['listing_id']}: missing verified_on")

    # 2 + 3. grades re-derive; Confirmed has in-stock URL evidence
    by_item = defaultdict(list)
    for l in listings:
        if l["is_latest"] == "yes":
            by_item[(l["item_type"], l["item_id"])].append(l)
    for key, a in avail.items():
        ev = [Evidence(l["listing_id"], l["retailer_id"], l["match"], l["stock_status"],
                       l["evidence_method"], l["verified_on"]) for l in by_item.get(key, [])]
        g = grade_item(ev)
        check(g.grade == a["grade"], f"availability {key}: stored {a['grade']} != recomputed {g.grade}")
        if a["grade"] == "Confirmed":
            ok = [l for l in by_item[key] if l["match"] == "exact" and l["stock_status"] == "in_stock"
                  and l["product_url"]]
            check(bool(ok), f"availability {key}: Confirmed without in-stock URL evidence")

    # 4. coverage
    for dish_id, rows in data.dish_ingredients().items():
        for r in rows:
            check(("ingredient", r["ingredient_id"]) in avail, f"no availability for {r['ingredient_id']}")
    for dish_id, rows in data.dish_utensils().items():
        for r in rows:
            if utens[r["utensil_id"]]["is_basic_kit"] != "yes":
                check(("utensil", r["utensil_id"]) in avail, f"no availability for utensil {r['utensil_id']}")

    # 5. alt groups and signatures
    for dish_id, rows in data.dish_ingredients().items():
        groups = defaultdict(list)
        for r in rows:
            if r["alt_group"]:
                groups[r["alt_group"]].append(r["ingredient_id"])
            if r["is_signature"] == "yes":
                check(r["is_optional"] != "yes", f"{dish_id}: signature {r['ingredient_id']} marked optional")
        for g, members in groups.items():
            check(len(members) >= 2, f"{dish_id}: alt_group {g} has one member {members}")
        check(any(r["is_signature"] == "yes" for r in rows), f"{dish_id}: no signature ingredient")

    # 6. substitutions
    for s in data.read_csv("substitutions.csv"):
        pool = ings if s["item_type"] == "ingredient" else utens
        check(s["original_id"] in pool, f"{s['sub_id']}: unknown original {s['original_id']}")
        if s["substitute_id"]:
            check(s["substitute_id"] in pool, f"{s['sub_id']}: unknown substitute {s['substitute_id']}")
        if s["context_dish_id"]:
            check(s["context_dish_id"] in dishes, f"{s['sub_id']}: unknown dish {s['context_dish_id']}")
        if s["evidence_type"] in ("recipe_author", "published_guide"):
            check(s["source_url"].startswith("http"), f"{s['sub_id']}: sourced row without URL")
        check(s["claude_proposed_acceptability"] in ("acceptable", "acceptable_with_caveat", "unacceptable"),
              f"{s['sub_id']}: bad proposed label")
        check(s["human_label"] in ("", "acceptable", "acceptable_with_caveat", "unacceptable"),
              f"{s['sub_id']}: bad human label")

    # 7. scope caps
    check(len(dishes) == 15, f"expected 15 dishes, found {len(dishes)}")
    check(len(ings) <= 80, f"ingredient cap exceeded: {len(ings)}")

    # 8. eval scenarios
    for scen_path in [ROOT / "eval" / "scenarios.jsonl", ROOT / "eval" / "scenarios_paraphrased.jsonl"]:
      if scen_path.exists():
        seen = set()
        for n, line in enumerate(scen_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            s = json.loads(line)
            sid = s["scenario_id"]
            check(sid not in seen, f"duplicate scenario {sid}")
            seen.add(sid)
            check(s["target_dish_id"] in dishes, f"{sid}: unknown dish")
            for d in s["constraints"].get("diets", []):
                check(d in rules.DIETS, f"{sid}: unknown diet {d}")
            for u in s["constraints"].get("equipment_have", []):
                check(u in utens, f"{sid}: unknown utensil {u}")
            exp = s["expected"]
            for k in ("forbidden_ingredient_ids", "must_flag_ingredient_ids", "signature_ingredient_ids"):
                for i in exp.get(k, []):
                    check(i in ings, f"{sid}: {k} unknown ingredient {i}")
            check(exp["expected_outcome"] in ("adapt", "adapt_with_caveat", "abstain_and_explain"),
                  f"{sid}: bad expected_outcome")

    if errors:
        print(f"FAILED: {len(errors)} problem(s)")
        for e in errors:
            print("  -", e)
        return 1
    print(f"OK: {len(dishes)} dishes, {len(ings)} ingredients, {len(listings)} evidence rows, "
          f"{len(avail)} graded items - all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
