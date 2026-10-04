"""Print a one-table summary of the headline eval runs.

Reads eval/runs/<run>.summary.json for the rules planner, gpt-5-mini and the
three tuned qwen runs, and prints constraint adherence, dish fidelity and task
success side by side. Usage: python3 show_results.py (no arguments).
"""
import json
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

runs = [
    ('rules, no LLM', 'rules_text_paraphrased'),
    ('gpt-5-mini', 'agent_gpt5mini'),
    ('qwen, original prompt', 'agent_qwen'),
    ('qwen, tuned, run 1', 'agent_qwen_v2'),
    ('qwen, tuned, run 2', 'agent_qwen_v2b'),
    ('qwen, tuned, run 3', 'agent_qwen_v2c'),
]

print('%-24s %9s %9s %12s' % ('run', 'adherence', 'fidelity', 'task_success'))
for name, f in runs:
    with open('eval/runs/' + f + '.summary.json') as fh:
        s = json.load(fh)
    print('%-24s %9s %9s %12s' % (name, s['constraint_adherence'], s['dish_fidelity'], s['task_success']))
