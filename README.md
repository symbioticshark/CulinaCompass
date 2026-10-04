# CulinaCompass SG

Repository: https://github.com/symbioticshark/CulinaCompass

A command-line agent that rebuilds South Indian home dishes for Singapore: what you can buy (graded Confirmed / Likely / Difficult / Unknown, with the retailer listing and date behind every grade), what to substitute, which utensils to use, and what it costs.

It is stdlib-only Python 3.10+. The LLM runs through OpenRouter. The key is read from `culinacompass.env`, looked up in the current folder, then the project folder, then `~/.culinacompass.env`; environment variables override it, and the file is git-ignored. `--offline` uses the rule-based planner instead.

**No API key is needed** to run with `--offline`, to run the tests, or to read the saved eval results (`eval/runs/`, `docs/EVAL_CARD.md`). A key is only needed for live LLM queries and for re-running the agent evals: put it in a file named `culinacompass.env` (git-ignored, so create it if it is missing) containing the line `OPENROUTER_API_KEY=your-key`.

## Documentation map

| Read this | For |
|---|---|
| `docs/PRODUCT.md` | persona, input, output, architecture diagram, metrics targeted vs reached |
| `../CulinaCompass_Report.docx` | the written report: reasoning, results, critique, future path |
| `docs/DATA_CARD.md` | the data: sources, grading rules, limitations |
| `docs/EVAL_CARD.md` | the evals: scenarios, metrics, every run, how to reproduce |

Demo video: `../CulinaCompass_Demo_Video.mp4`, in the folder next to this one (about 6.5 minutes)

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate    # no pip install needed: the project is stdlib-only
python -m unittest discover -s tests                   # 44 offline tests, ~1 s
python -m culinacompass ask "idli for breakfast" --offline   # works with no API key
```

For LLM mode, create `culinacompass.env` in the project folder (it is git-ignored, so it is not in the repo) with the line `OPENROUTER_API_KEY=your-key`. The default model is set there (`OPENROUTER_MODEL`, currently `qwen/qwen3.7-flash`, about US$0.001 per query); `--model` overrides it per run.

## Quick start

```bash
# LLM mode: put OPENROUTER_API_KEY=... in culinacompass.env (https://openrouter.ai/keys); --offline needs none

python3 -m culinacompass ask "I miss my amma's gongura pachadi. I'm vegan now."
python3 -m culinacompass ask "idli for breakfast" --have blender --budget 10
python3 -m culinacompass ask "sambar" --diet vegan --diet gluten_free --trace
python3 -m culinacompass chat                       # interactive; follow-ups keep context
python3 -m culinacompass ask "pulihora, no peanuts" --offline    # no LLM
python3 -m culinacompass dishes                     # the 15 supported dishes
python3 -m culinacompass lookup "asam jawa"         # ingredient -> Singapore evidence
```

Other flags:
- `--json`: prints the plan, validation issues and the eval record.
- `--as-of 2026-09-28`: pins the date used for grade staleness. Confirmed becomes Likely once evidence is more than 30 days old.
- `--no-validate`: accepts the first plan the model submits (for the ablation).

## How it works

```
user text + optional profile (--diet/--have/--budget)
        |
   LLM (OpenRouter, tool calling)  <-- decides: which dish, adapt vs abstain, substitutions, flags
        |  tools, all deterministic code over the data:
        |    search_dishes · get_dish · resolve_ingredient · check_diet · find_substitutes
        |    check_availability · check_equipment · estimate_cost · submit_plan
        v
   submit_plan -> plan.validate()   rejects: forbidden ingredient used, signature dropped or badly
        |                            replaced, unacceptable substitution, hard-to-find item not flagged,
        |                            missing equipment/budget not flagged, lazy abstention
        |   (model re-plans, up to 3 rejections; leftover errors are shown, never hidden)
        v
   plan.materialize/price/evidence  -> ingredient list, SG listings + URLs + dates, cost  (code, not the model)
        v
   rendered answer
```

The model makes the judgement calls: understanding "plant-based", "palli" or "naivedyam", choosing between substitutes, and explaining. Code owns every fact: the ingredient list, stock grades, prices and diet verdicts. So availability can't be hallucinated, and a constraint can't be silently dropped.

| Module | Role |
|---|---|
| `culinacompass/agent.py` | system prompt + tool-calling loop with validation feedback |
| `culinacompass/tools.py` | tool schemas (OpenAI/OpenRouter format) + implementations |
| `culinacompass/plan.py` | plan → ingredient list, validation, pricing, evidence, CLI rendering, eval record |
| `culinacompass/rule_planner.py` | **baseline**: keyword parser + rule planner, no LLM (parser frozen, see below) |
| `culinacompass/retrieval.py` | alias/fuzzy retrieval over dishes and ingredients (the RAG layer) |
| `culinacompass/rules.py`, `grading.py`, `packs.py` | diet / equipment / cost checks, availability grades, pack-size parsing |
| `culinacompass/llm.py` | OpenRouter client (retries, usage counting) + `ScriptedLLM` for offline tests |

## Evaluation

```bash
python3 eval/run_eval.py --planner rules --input text --set paraphrased
python3 eval/run_eval.py --planner agent --input text --set paraphrased
python3 eval/run_eval.py --planner agent --input text --set paraphrased --no-validate   # ablation
```

- `--input profile` gives the planner the constraints in structured form. `--input text` gives the request text only, so constraints must be inferred.
- `--set original` uses the 32 scenario texts. `--set paraphrased` rewords the **same 32** scenarios in natural code-mixed English ("palli", "kaju", "mixie", "plant-based", "five dollars"). Those were written after the rule parser was frozen (`eval/rule_parser.frozen.sha256`), so it is a held-out test for the baseline.
- The dish is never handed to either planner.
- Outputs, traces and summaries are saved in `eval/runs/`.

Metrics are reported separately: constraint adherence, dish fidelity (did it adapt the dish the user named), task success (both), substitution acceptability (against your labels), flag recall, availability overclaims and false alarms.

Results so far (as of 2026-10-02):

| Run | Adherence | Fidelity | Task success |
|---|---|---|---|
| lazy_safe_dish (always plain dosa) | **1.00** | 0.06 | 0.06 |
| as_written (ignores constraints) | 0.44 | 0.84 | 0.44 |
| rules, profile, paraphrased | 1.00 | 0.94 | 0.94 |
| **rules, text only, paraphrased** | 0.75 | 0.88 | **0.69** |
| agent (qwen3.7-flash, tuned prompt), text only, paraphrased, mean of 3 runs | 0.98 | 0.98 | **0.96** (0.88 / 1.00 / 1.00) |
| agent (gpt-5-mini, original prompt), text only, paraphrased, 1 run | 1.00 | 0.97 | 0.97 |
| agent, no validator (ablation) | *not yet run* | | |

The rule baseline scores 1.00 on the original texts, because its keywords were written alongside them. That's why the held-out paraphrase set is the one to report. Caveat: it is held-out for the rule baseline only. The LLM prompt was tuned on failures from this same set, so the agent rows are optimistic (see `docs/EVAL_CARD.md`, section 8).

## Data (see docs/DATA_CARD.md)

```
raw/listings_raw.tsv       append-only evidence log (286 rows, 9 sources, 2026-09-28)
data/*.csv                 15 dishes, 77 ingredients, 204 aliases, 46 human-labelled substitutions,
                           graded availability, costs, SingStat reference prices
culinacompass_sg.db        SQLite of all tables
```

```bash
python3 scripts/build.py && python3 scripts/validate.py && python3 eval/make_scenarios.py
python3 -m unittest discover -s tests           # 44 tests, offline
python3 scripts/refresh_listings.py             # re-verify stock from your own network
```

All 46 substitutions are human-labelled, and the signature ingredients were reviewed (2026-09-29).
