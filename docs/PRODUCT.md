# CulinaCompass SG: Product Documentation

## Persona

**Lakshmi, 29, from Hyderabad, software engineer in Singapore for three months.** She misses her mother's gongura pachadi, cooks in a small flat with a blender and no idli steamer, and shops at Little India and online grocers. She writes to a cooking assistant the way she talks, mixing English with Telugu and Tamil words ("palli", "kaju", "mixie"). She may be vegan, Jain, gluten-free or allergic to groundnuts, and she is budget-conscious. What she needs is a plan she can shop from today, and an honest "this dish cannot be made faithfully" when that is the truth, instead of a confident guess.

## Input

- **Free text:** "I miss my amma's gongura pachadi. I'm vegan now."
- **Optional profile flags:** `--diet`, `--have` (equipment), `--budget`, `--as-of` (date used for evidence staleness).
- The dish is **never** given separately. The system must identify it from the text (aliases, spellings, Telugu, Tamil and Kannada names).

## Output

A command-line plan (also `--json`) containing:

- the dish and the verdict: **ADAPTED FOR SINGAPORE** or **CANNOT ADAPT FAITHFULLY**
- the full ingredient list with quantities, each graded Confirmed, Likely, Difficult or Unknown, with the retailer listing, price, URL and evidence date
- substitutions, each with a reason, and a note on what was left out
- equipment: what to use instead, and what is missing with a price
- cost in S$, both pro-rated for the quantities used and the cash outlay for whole packs
- a short explanation and cooking notes

## Architecture

```
  user text + optional profile (--diet / --have / --budget)
                       |
                       v
        +------------------------------+
        |  LLM agent  (OpenRouter)     |   external intelligence: understands intent,
        |  culinacompass/agent.py      |   chooses dish, adapt or abstain, substitutions,
        +--------------+---------------+   flags, explanation
                       |  tool calls (deterministic code over the data)
                       v
        +------------------------------+       +---------------------------+
        |  Tools  culinacompass/tools  | ----> |  Data  (data/*.csv, .db)  |
        |  search_dishes  get_dish     |       |  15 dishes, 77 ingredients|
        |  resolve_ingredient          |       |  286 evidence rows        |
        |  check_diet  check_equipment |       |  46 human-labelled subs   |
        |  check_availability          |       +---------------------------+
        |  find_substitutes            |        retrieval.py (alias/fuzzy RAG),
        |  estimate_cost  submit_plan  |        rules.py, grading.py, packs.py
        +--------------+---------------+
                       |  submit_plan
                       v
        +------------------------------+
        |  Validator  plan.validate()  |--- rejected: errors returned to the LLM,
        +--------------+---------------+    it re-plans (up to 3 times)
                       | accepted (or leftover errors shown, never hidden)
                       v
        +------------------------------+
        |  plan.py (code, not the LLM) |   ingredient list, prices, SG listings + URLs
        |  materialize / price /       |   + dates, rendered answer
        |  evidence / render_text      |
        +--------------+---------------+
                       v
                 rendered answer

  Baseline (no LLM, `--offline`): rule_planner.py parses the text with keyword rules and feeds
  the same plan.py, so both planners are scored by the same code.
```

**Division of labour:** the model owns *judgement* (what the user means, which substitute keeps the dish recognisable, how to explain). Code owns every *fact* (ingredient list, stock grade, price, diet verdict). That is why availability cannot be hallucinated and a constraint cannot be silently dropped.

## Metrics: targeted and reached

Targets are the author's original ones: task success 0.85 and constraint adherence 0.90, with the secondary metrics (flag recall, substitution acceptability) targeted at about 0.90. Two rows marked *added* (overclaims, cost) were added later as natural guardrails and were not part of the original targets. Reached figures are on the 32-scenario paraphrased set, text input, from `eval/runs/` (see `EVAL_CARD.md`).

| Metric | Target | Reached (qwen3.7-flash, tuned prompt, mean of 3 runs) | Met? |
|---|---|---|---|
| Task success | at least 0.85 (also above the rule baseline, 0.69) | **0.96** (runs: 0.88, 1.00, 1.00) | Yes, but see caveat |
| Constraint adherence | at least 0.90 | 0.98 | Yes |
| Availability overclaims *(added)* | 0 | 0 in every run | Yes |
| Flag recall | about 0.90 | 0.94 | Yes |
| Substitution acceptability | about 0.90 | 1.00 | Yes |
| Cost per query *(added)* | well under US$0.01 | about US$0.001 | Yes |

**Caveats:** the tuned prompt was derived from failures on this same scenario set, so 0.96 is optimistic and the set is not held-out for the LLM. Qwen's single runs ranged from 0.88 to 1.00. False alarms (over-flagging) rose from 3 to about 5 per run after tuning and are not covered by a target. The no-validator ablation has not been run.

## Where things live

| Question | File |
|---|---|
| How do I run it? | `README.md` |
| What data, and how good is it? | `docs/DATA_CARD.md` |
| How is it evaluated? | `docs/EVAL_CARD.md` |
| What did I conclude? | `docs/REPORT.docx` |
