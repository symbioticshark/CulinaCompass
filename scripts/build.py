#!/usr/bin/env python3
"""Build the derived tables and the SQLite database from the evidence log.

  raw/listings_raw.tsv  --(parse packs, attach URLs)-->  data/sg_listings.csv
  sg_listings           --(grading.grade_item)------->  data/availability.csv
  dishes + listings     --(rules.cost_dish)---------->  data/dish_costs.csv
  everything            ----------------------------->  culinacompass_sg.db

Run:  python3 scripts/build.py
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from culinacompass import data, rules  # noqa: E402
from culinacompass.grading import Evidence, grade_item  # noqa: E402
from culinacompass.packs import parse_pack  # noqa: E402

RAW = ROOT / "raw" / "listings_raw.tsv"
DB = ROOT / "culinacompass_sg.db"

URL_PREFIX = {"waangoo": "https://www.waangoo.com", "selvistores": "https://selvistores.sg",
              "sgwetmarket": "https://sgwetmarket.com.sg"}
STOCK = {"true": "in_stock", "false": "out_of_stock", "unverified": "unverified", "": "n/a"}


def write_csv(name: str, rows: list[dict], fields: list[str]) -> None:
    with open(ROOT / "data" / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def build_listings() -> list[dict]:
    retailers = {r["retailer_id"]: r for r in data.read_csv("retailers.csv")}
    ings = data.ingredients()
    out = []
    with open(RAW, newline="", encoding="utf-8") as f:
        for i, r in enumerate(csv.DictReader(f, delimiter="\t"), start=1):
            item = r["ingredient_id"]
            item_type = "utensil" if item.startswith("U:") else "ingredient"
            item_id = item[2:] if item_type == "utensil" else item
            path = r["product_path"].strip()
            url = "" if path in ("", "-") else (path if path.startswith("http")
                                                else URL_PREFIX.get(r["retailer_id"], "") + path)
            is_note = r["match"] == "none"
            pack = parse_pack(r["product_title"]) if not is_note else None
            pack_g = None
            if pack and item_type == "ingredient":
                ing = ings[item_id]
                dens = float(ing["density_g_per_ml"]) if ing["density_g_per_ml"] else None
                piece = float(ing["typical_piece_g"]) if ing["typical_piece_g"] else None
                pack_g = pack.grams(dens, piece)
            price = r["price_sgd"].strip()
            unit_price = (round(float(price) / pack_g * 1000, 2)
                          if price and pack_g else "")
            out.append({
                "listing_id": f"L{i:04d}",
                "item_type": item_type,
                "item_id": item_id,
                "retailer_id": r["retailer_id"],
                "query": r["query"],
                "product_title": r["product_title"],
                "price_sgd": price,
                "stock_status": STOCK[r["in_stock"].strip()],
                "match": r["match"],
                "pack_qty": "" if not pack or pack.qty is None else round(pack.qty, 1),
                "pack_unit": "" if not pack or not pack.unit else pack.unit,
                "pack_pieces": "" if not pack or not pack.pieces else pack.pieces,
                "pack_parse": "" if not pack else pack.method,
                "pack_g": "" if pack_g is None else round(pack_g, 1),
                "unit_price_sgd_per_kg": unit_price,
                "product_url": url,
                "evidence_method": retailers[r["retailer_id"]]["evidence_method"],
                "verified_on": r["verified_on"],
            })
    # Only the newest observation of each retailer product counts for grading;
    # older rows stay as history (refresh_listings.py appends, never overwrites).
    newest: dict = {}
    for row in out:
        key = (row["item_type"], row["item_id"], row["retailer_id"],
               row["product_url"] or (row["query"] + "|" + row["product_title"]))
        if key not in newest or row["verified_on"] >= newest[key]["verified_on"]:
            newest[key] = row
    latest_ids = {r["listing_id"] for r in newest.values()}
    for row in out:
        row["is_latest"] = "yes" if row["listing_id"] in latest_ids else "no"
    return out


def build_availability(listings: list[dict]) -> list[dict]:
    by_item = defaultdict(list)
    for l in listings:
        if l["is_latest"] == "yes":
            by_item[(l["item_type"], l["item_id"])].append(l)
    items = [("ingredient", i) for i in data.ingredients()]
    items += [("utensil", u) for u, r in data.utensils().items() if r["is_basic_kit"] != "yes"]
    rows = []
    for key in items:
        ev = [Evidence(l["listing_id"], l["retailer_id"], l["match"], l["stock_status"],
                       l["evidence_method"], l["verified_on"]) for l in by_item.get(key, [])]
        g = grade_item(ev)
        priced = [l for l in by_item.get(key, []) if l["match"] == "exact"
                  and l["stock_status"] == "in_stock" and l["unit_price_sgd_per_kg"]]
        cheapest = min(priced, key=lambda l: float(l["unit_price_sgd_per_kg"]), default=None)
        rows.append({
            "item_type": key[0], "item_id": key[1], "grade": g.grade, "rule": g.rule,
            "confidence": g.confidence,
            "n_retailers_in_stock_exact": g.n_retailers_in_stock_exact,
            "n_retailers_listed_exact": g.n_retailers_listed_exact,
            "n_retailers_variant": g.n_retailers_variant,
            "n_sources_searched": g.n_sources_searched,
            "supporting_listing_ids": "|".join(g.supporting_listing_ids),
            "cheapest_unit_price_sgd_per_kg": "" if not cheapest else cheapest["unit_price_sgd_per_kg"],
            "cheapest_listing_id": "" if not cheapest else cheapest["listing_id"],
            "verified_on": g.verified_on,
        })
    return rows


def build_dish_summaries(avail: list[dict]) -> tuple[list[dict], list[dict]]:
    grade_of = {(a["item_type"], a["item_id"]): a["grade"] for a in avail}
    order = {"Confirmed": 0, "Likely": 1, "Difficult": 2, "Unknown": 3}
    summary, costs = [], []
    for dish_id in data.dishes():
        req = [r for r in data.dish_ingredients()[dish_id] if r["is_optional"] != "yes"]
        counts = defaultdict(int)
        for r in req:
            counts[grade_of[("ingredient", r["ingredient_id"])]] += 1
        sig = [r["ingredient_id"] for r in req if r["is_signature"] == "yes"]
        sig_grades = {s: grade_of[("ingredient", s)] for s in sig}
        worst_sig = max(sig_grades.values(), key=lambda g: order[g]) if sig_grades else ""
        blockers = sorted(r["ingredient_id"] for r in req
                          if grade_of[("ingredient", r["ingredient_id"])] in ("Difficult", "Unknown")
                          and not r["alt_group"])
        summary.append({
            "dish_id": dish_id,
            "n_required_ingredients": len(req),
            "n_confirmed": counts["Confirmed"], "n_likely": counts["Likely"],
            "n_difficult": counts["Difficult"], "n_unknown": counts["Unknown"],
            "signature_ingredients": "|".join(f"{k}:{v}" for k, v in sig_grades.items()),
            "worst_signature_grade": worst_sig,
            "required_ingredients_not_readily_available": "|".join(blockers),
        })
        c = rules.cost_dish(dish_id)
        costs.append({
            "dish_id": dish_id, "servings_est": data.dishes()[dish_id]["servings_est"],
            "prorated_cost_sgd": c["prorated_sgd"],
            "cash_outlay_empty_pantry_sgd": c["cash_outlay_sgd"],
            "unpriced_required_ingredients": "|".join(c["unpriced_ingredients"]),
            "priced_lines": len(c["lines"]) - len(c["unpriced_ingredients"]),
        })
    return summary, costs


def build_sqlite() -> None:
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    for csv_path in sorted((ROOT / "data").glob("*.csv")):
        rows = data.read_csv(csv_path.name)
        if not rows:
            continue
        table = csv_path.stem
        cols = list(rows[0].keys())

        def affinity(c):
            vals = [r[c] for r in rows if r[c] != ""]
            try:
                [float(v) for v in vals]
                return "REAL" if vals else "TEXT"
            except ValueError:
                return "TEXT"
        types = {c: affinity(c) for c in cols}
        con.execute(f'CREATE TABLE "{table}" ({", ".join(f"{chr(34)}{c}{chr(34)} {types[c]}" for c in cols)})')
        con.executemany(
            f'INSERT INTO "{table}" VALUES ({",".join("?" * len(cols))})',
            [[(None if r[c] == "" else (float(r[c]) if types[c] == "REAL" else r[c])) for c in cols]
             for r in rows])
    con.executescript("""
    CREATE VIEW v_dish_ingredient_availability AS
      SELECT di.dish_id, di.ingredient_id, i.canonical_name, di.component, di.qty_text, di.qty_g,
             di.is_optional, di.is_signature, di.alt_group,
             a.grade, a.rule, a.confidence, a.n_retailers_in_stock_exact,
             a.cheapest_unit_price_sgd_per_kg, a.verified_on
      FROM dish_ingredients di
      JOIN ingredients i ON i.ingredient_id = di.ingredient_id
      JOIN availability a ON a.item_type = 'ingredient' AND a.item_id = di.ingredient_id;
    CREATE VIEW v_evidence AS
      SELECT l.*, r.name AS retailer_name, r.channel
      FROM sg_listings l JOIN retailers r USING (retailer_id);
    """)
    con.commit()
    con.close()


def main() -> None:
    listings = build_listings()
    write_csv("sg_listings.csv", listings, list(listings[0].keys()))
    avail = build_availability(listings)
    write_csv("availability.csv", avail, list(avail[0].keys()))
    summary, costs = build_dish_summaries(avail)
    write_csv("dish_availability_summary.csv", summary, list(summary[0].keys()))
    write_csv("dish_costs.csv", costs, list(costs[0].keys()))
    build_sqlite()
    grades = defaultdict(int)
    for a in avail:
        grades[(a["item_type"], a["grade"])] += 1
    print(f"listings: {len(listings)}  graded items: {len(avail)}")
    for k in sorted(grades):
        print(f"  {k[0]:<10} {k[1]:<10} {grades[k]}")
    print(f"sqlite: {DB.name}")


if __name__ == "__main__":
    main()
