"""Agent loop, validator, rule planner and CLI tests (offline; scripted LLM)."""
import json
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from culinacompass import plan as planmod  # noqa: E402
from culinacompass.agent import run_agent  # noqa: E402
from culinacompass.llm import ScriptedLLM  # noqa: E402
from culinacompass.rule_planner import parse_request_rules, plan_rules  # noqa: E402
from culinacompass.tools import SCHEMAS, Toolbox  # noqa: E402
import score  # noqa: E402

AS_OF = date(2026, 9, 28)
T = ScriptedLLM.tool_call

GONGURA_PLAN = {
    "dish_id": "gongura_pachadi", "outcome": "adapt",
    "substitutions": [{"original_id": "gongura_leaves", "substitute_id": "gongura_pickle",
                       "reason": "fresh leaves not found in Singapore"}],
    "include_optional": [], "flagged_ingredient_ids": [], "flagged_equipment": [], "budget_flagged": False,
    "constraints_inferred": {"diets": ["vegetarian"], "equipment_have": ["mixer_grinder"],
                             "budget_sgd": None, "budget_basis": "cash_outlay"},
    "explanation": "Fresh gongura is not sold in the Singapore shops we checked, so this uses store-bought gongura pickle.",
    "cooking_notes": "Temper the pickle briefly with onion.",
}


class AgentLoop(unittest.TestCase):
    def test_reject_then_fix(self):
        fixed = {**GONGURA_PLAN, "flagged_ingredient_ids": ["gongura_leaves"]}
        llm = ScriptedLLM([
            T("search_dishes", {"query": "gongura pachadi"}, "a"),
            T("get_dish", {"dish_id": "gongura_pachadi"}, "b"),
            T("check_availability", {"ingredient_ids": ["gongura_leaves"]}, "c"),
            T("submit_plan", GONGURA_PLAN, "d"),
            T("submit_plan", fixed, "e"),
        ])
        res = run_agent("amma's gongura pachadi", llm, planmod.Constraints(), AS_OF)
        self.assertEqual(res.stopped_reason, "submitted")
        self.assertEqual(res.rejections, 1)
        rejected = json.loads(res.transcript[-3]["content"])
        self.assertFalse(rejected["accepted"])
        self.assertIn("FLAG_MISSING", [i["code"] for i in rejected["issues"]])
        self.assertIn("gongura_leaves", res.plan.flagged_ingredient_ids)
        sc = next(s for s in score.load_scenarios() if s["scenario_id"] == "S22")
        out = planmod.to_eval_output("S22", res.plan, planmod.Constraints.from_dict(sc["constraints"]), AS_OF)
        self.assertTrue(score.score_one(sc, out)["task_success"])

    def test_no_validate_ablation_accepts_flawed_plan(self):
        llm = ScriptedLLM([T("submit_plan", GONGURA_PLAN)])
        res = run_agent("gongura pachadi", llm, planmod.Constraints(), AS_OF, validate=False)
        self.assertEqual(res.rejections, 0)
        self.assertTrue(any(i["code"] == "FLAG_MISSING" for i in res.issues))

    def test_gives_up_after_max_rejections(self):
        llm = ScriptedLLM([T("submit_plan", GONGURA_PLAN, f"x{i}") for i in range(5)])
        res = run_agent("gongura pachadi", llm, planmod.Constraints(), AS_OF, max_rejections=2)
        self.assertEqual(res.rejections, 2)
        self.assertTrue(any(i["severity"] == "error" for i in res.issues))  # recorded, not hidden

    def test_stops_if_model_never_submits(self):
        llm = ScriptedLLM([{"role": "assistant", "content": "Here is a recipe..."}] * 5)
        res = run_agent("idli", llm, planmod.Constraints(), AS_OF)
        self.assertIsNone(res.plan)
        self.assertEqual(res.stopped_reason, "model stopped without a plan")

    def test_profile_reaches_model(self):
        llm = ScriptedLLM([])
        run_agent("sambar", llm, planmod.Constraints(["vegan"]), AS_OF, max_steps=1)
        self.assertIn('"vegan"', llm.seen[0][-1]["content"])


class Tools(unittest.TestCase):
    def setUp(self):
        self.box = Toolbox(planmod.Constraints(), AS_OF)

    def test_schemas_are_well_formed(self):
        names = [s["function"]["name"] for s in SCHEMAS]
        self.assertIn("submit_plan", names)
        for s in SCHEMAS:
            self.assertEqual(s["function"]["parameters"]["type"], "object")

    def test_tool_error_is_returned_not_raised(self):
        self.assertIn("error", json.loads(self.box.call("get_dish", {"dish_id": "biryani"})))
        self.assertIn("error", json.loads(self.box.call("no_such_tool", {})))

    def test_find_substitutes_uses_human_labels(self):
        r = json.loads(self.box.call("find_substitutes", {"ingredient_id": "curd_yogurt", "dish_id": "curd_rice",
                                                          "diets": ["vegan"]}))
        s = r["substitutes"][0]
        self.assertEqual((s["label"], s["label_source"]), ("unacceptable", "human"))

    def test_drumstick_is_ambiguous(self):
        r = json.loads(self.box.call("resolve_ingredient", {"text": "drumstick"}))
        self.assertTrue(r["matches"][0]["ambiguous"])


class Validator(unittest.TestCase):
    def test_lazy_abstention_rejected(self):
        p = planmod.Plan("pulihora", "abstain", explanation="Peanuts are in pulihora so I cannot help with this.")
        codes = [i["code"] for i in planmod.validate(p, planmod.Constraints(["no_peanut"]))]
        self.assertIn("ABSTAIN_AVOIDABLE", codes)

    def test_justified_abstention_accepted(self):
        p = planmod.Plan("curd_rice", "abstain", explanation="Curd is what makes curd rice; a vegan version is a different dish.")
        self.assertEqual(planmod.validate(p, planmod.Constraints(["vegan"])), [])

    def test_forbidden_and_signature(self):
        p = planmod.Plan("gutti_vankaya_kura", "adapt",
                         substitutions=[{"original_id": "peanuts", "substitute_id": None, "reason": ""}])
        codes = {i["code"] for i in planmod.validate(p, planmod.Constraints(["no_peanut"]))}
        self.assertIn("SUB_UNACCEPTABLE", codes)  # dropping a signature = unacceptable omission

    def test_inferred_constraints_are_enforced(self):
        p = planmod.Plan("sambar", "adapt", constraints_inferred=planmod.Constraints(["gluten_free"]))
        codes = {i["code"] for i in planmod.validate(p, planmod.Constraints())}
        self.assertIn("FLAG_MISSING", codes)  # hing

    def test_unknown_substitute_is_warning(self):
        p = planmod.Plan("idli", "adapt", substitutions=[{"original_id": "poha", "substitute_id": "basmati_rice",
                                                          "reason": ""}])
        issues = planmod.validate(p, planmod.Constraints(["vegetarian"], ["mixer_grinder", "idli_steamer"]))
        self.assertEqual([(i["severity"], i["code"]) for i in issues], [("warning", "SUB_UNVERIFIED")])


class RulePlanner(unittest.TestCase):
    def test_parse(self):
        dish, c = parse_request_rules("Chitranna with no nuts of any kind, S$12, I have a blender")
        self.assertEqual(dish, "lemon_rice")
        self.assertEqual(set(c.diets), {"no_peanut", "no_tree_nut"})
        self.assertEqual((c.equipment_have, c.budget_sgd), (["blender"], 12.0))

    def test_negated_equipment(self):
        _d, c = parse_request_rules("garelu but no mixie and no kadai")
        self.assertEqual(c.equipment_have, [])

    def test_out_of_scope(self):
        p = plan_rules(None, planmod.Constraints())
        self.assertEqual((p.dish_id, p.outcome), (None, "abstain"))

    def test_rule_plans_always_validate(self):
        for s in score.load_scenarios():
            c = planmod.Constraints.from_dict(s["constraints"])
            p = plan_rules(s["target_dish_id"], c)
            errs = [i for i in planmod.validate(p, c, AS_OF) if i["severity"] == "error"]
            self.assertEqual(errs, [], s["scenario_id"])


class CLI(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "culinacompass", *args], cwd=ROOT,
                              capture_output=True, text=True)

    def test_offline_ask(self):
        r = self.run_cli("ask", "Onam avial, cannot find mangalore cucumber", "--offline", "--as-of", "2026-09-28")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Ash gourd", r.stdout)

    def test_offline_json(self):
        r = self.run_cli("ask", "vegan curd rice", "--offline", "--json")
        self.assertEqual(json.loads(r.stdout)["plan"]["outcome"], "abstain")

    def test_missing_key_is_clean_error(self):
        # Run from a copy of the project with no culinacompass.env and an empty HOME, so a real key in
        # the developer's config file (project folder or ~/.culinacompass.env) cannot leak into the test.
        import os
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            home = Path(tmp) / "home"
            home.mkdir()
            shutil.copytree(ROOT, proj, ignore=shutil.ignore_patterns(
                ".venv", "culinacompass.env", ".culinacompass.env", "__pycache__", "runs"))
            env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
            env["HOME"] = str(home)
            r = subprocess.run([sys.executable, "-m", "culinacompass", "ask", "idli"], cwd=proj,
                               capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("culinacompass.env", r.stderr)


if __name__ == "__main__":
    unittest.main()
