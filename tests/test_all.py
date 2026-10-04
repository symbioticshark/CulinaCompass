"""Run: python3 -m unittest discover -s tests -v   (stdlib only)"""
import json
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from culinacompass import rules  # noqa: E402
from culinacompass.grading import Evidence, effective_grade, grade_item  # noqa: E402
from culinacompass.packs import parse_pack  # noqa: E402
import refresh_listings as rl  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def ev(r, m, s, meth="catalogue_api"):
    return Evidence("L", r, m, s, meth, "2026-09-28")


class Packs(unittest.TestCase):
    def test_single_and_units(self):
        self.assertEqual(parse_pack("Udhayam Idly Rice - 5 Kg").qty, 5000)
        self.assertEqual(parse_pack("Fortune Refined Sunflower Oil - 1 L").unit, "ml")

    def test_range_midpoint(self):
        self.assertEqual(parse_pack("Fresh Pumpkin Yellow - 1 Pc (800 g to 1 Kg)").qty, 900)
        self.assertEqual(parse_pack("Fresh Green Chilli 150g-200g").qty, 175)

    def test_grade_code_not_a_range(self):
        self.assertEqual(parse_pack("Nut But Premium Cashewnuts w320 - 250 g").qty, 250)

    def test_multipacks(self):
        self.assertEqual(parse_pack("Ooty Moong Dhall Split 1kg (Pack of 5)").qty, 5000)
        self.assertEqual(parse_pack("Poha Thick 3 x 500g").qty, 1500)

    def test_pieces(self):
        p = parse_pack("Fresh Raw Banana 2pcs")
        self.assertEqual((p.unit, p.pieces), ("piece", 2))
        self.assertEqual(p.grams(typical_piece_g=150), 300)


class Grading(unittest.TestCase):
    def test_confirmed(self):
        self.assertEqual(grade_item([ev("a", "exact", "in_stock")]).grade, "Confirmed")

    def test_search_index_alone_is_not_confirmed(self):
        g = grade_item([ev("fairprice", "exact", "in_stock", "search_index")])
        self.assertEqual(g.grade, "Likely")

    def test_out_of_stock_is_likely(self):
        self.assertEqual(grade_item([ev("a", "exact", "out_of_stock")]).grade, "Likely")

    def test_variant_only_is_difficult(self):
        self.assertEqual(grade_item([ev("a", "variant", "in_stock")]).grade, "Difficult")

    def test_absence_needs_three_sources(self):
        two = [ev("a", "none", "n/a"), ev("b", "none", "n/a")]
        self.assertEqual(grade_item(two).grade, "Unknown")
        self.assertEqual(grade_item(two + [ev("c", "none", "n/a")]).grade, "Difficult")

    def test_staleness(self):
        self.assertEqual(effective_grade("Confirmed", "2026-09-28", date(2026, 10, 10)), "Confirmed")
        self.assertEqual(effective_grade("Confirmed", "2026-09-28", date(2026, 11, 30)), "Likely")
        self.assertEqual(effective_grade("Difficult", "2026-01-01", date(2026, 11, 30)), "Difficult")


class Rules(unittest.TestCase):
    def test_alt_group_rescues(self):
        # sambar: 'ghee or oil' - vegan is satisfiable inside the recipe
        ids = {v.ingredient_id for v in rules.check_dish_diet("sambar", ["vegan"])}
        self.assertNotIn("ghee", ids)

    def test_signature_conflict(self):
        v = [x for x in rules.check_dish_diet("curd_rice", ["vegan"]) if x.is_signature]
        self.assertEqual([x.ingredient_id for x in v], ["curd_yogurt"])

    def test_hing_gluten_is_warning(self):
        v = {x.ingredient_id: x.severity for x in rules.check_dish_diet("sambar", ["gluten_free"])}
        self.assertEqual(v.get("asafoetida"), "warn")

    def test_jain_blocks_brinjal(self):
        ids = {v.ingredient_id for v in rules.check_dish_diet("gutti_vankaya_kura", ["jain"])}
        self.assertIn("brinjal_small", ids)

    def test_equipment(self):
        self.assertTrue(rules.check_equipment("dosa", {"blender"}).feasible)
        self.assertFalse(rules.check_equipment("idli", set()).feasible)
        self.assertTrue(rules.check_equipment("idli", {"blender", "steamer_pot"}).feasible)

    def test_cost_never_guesses_unpriced(self):
        c = rules.cost_dish("gongura_pachadi")
        self.assertIn("gongura_leaves", c["unpriced_ingredients"])
        self.assertGreater(c["cash_outlay_sgd"], c["prorated_sgd"])


class RefreshParsers(unittest.TestCase):
    def test_shopify_product(self):
        p = rl.parse_shopify_product_js(json.loads((FIX / "shopify_product.js.json").read_text()))
        self.assertEqual((p["price_sgd"], p["in_stock"]), ("3.25", "true"))

    def test_shopify_suggest(self):
        ps = rl.parse_shopify_suggest(json.loads((FIX / "shopify_suggest.json").read_text()))
        self.assertEqual(ps[1]["in_stock"], "false")
        self.assertEqual(ps[0]["path"], "/products/udhaiyam-toor-dal-india-shree-lakshmi")

    def test_woo(self):
        ps = rl.parse_woo_products(json.loads((FIX / "woo_products.json").read_text()))
        self.assertEqual((ps[0]["price_sgd"], ps[0]["in_stock"]), ("1.40", "true"))


class Scorer(unittest.TestCase):
    def run_baseline(self, name):
        out = subprocess.run([sys.executable, str(ROOT / "eval" / "score.py"), "--baseline", name],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def test_lazy_winner_is_caught(self):
        s = self.run_baseline("lazy_safe_dish")
        self.assertEqual(s["constraint_adherence"], 1.0)
        self.assertLess(s["dish_fidelity"], 0.2)

    def test_oracle_is_perfect(self):
        s = self.run_baseline("rules_oracle")
        self.assertEqual((s["task_success"], s["substitutions_unjudged"]), (1.0, 0))


class Dataset(unittest.TestCase):
    def test_validator_passes(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate.py")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
