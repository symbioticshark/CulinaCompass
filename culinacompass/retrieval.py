"""Lexical retrieval over the dish and ingredient knowledge base.

The corpus is small and closed (15 dishes, 77 ingredients, ~200 aliases), and
most queries are names in Telugu/Tamil/Malay transliteration, so alias matching
with fuzzy fallback beats embeddings here: it is exact, explainable and needs no
model. Returns scored candidates; the agent decides.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from . import data

_WORD = re.compile(r"[a-z0-9']+")


def norm(text: str) -> str:
    return " ".join(_WORD.findall(text.lower().replace("_", " ")))


def _dish_names() -> dict[str, list[str]]:
    out = {}
    for d in data.dishes().values():
        names = {d["name"], d["dish_id"]} | {a for a in d["aliases"].split("|") if a}
        cleaned = set()
        for n in names:
            inner = [x for x in re.findall(r"\((.*?)\)", n) if x not in ("related",)]
            for cand in [re.sub(r"\(.*?\)", "", n)] + inner:  # "X (stuffed brinjal curry)"
                cand = norm(cand)
                if cand:
                    cleaned.add(cand)
        out[d["dish_id"]] = sorted(cleaned)
    return out


def _score(query: str, name: str) -> float:
    if not query or not name:
        return 0.0
    if f" {name} " in f" {query} ":
        return 1.0 + len(name) / 100  # longer exact match wins ties ("tomato rasam" > "rasam")
    q_tokens, n_tokens = set(query.split()), set(name.split())
    overlap = len(q_tokens & n_tokens) / len(n_tokens)
    fuzzy = max((SequenceMatcher(None, w, name).ratio() for w in _ngrams(query, len(n_tokens))),
                default=0.0)
    return max(overlap * 0.8, fuzzy * 0.9 if fuzzy >= 0.8 else 0.0)


def _ngrams(query: str, n: int) -> list[str]:
    toks = query.split()
    return [" ".join(toks[i:i + n]) for i in range(max(1, len(toks) - n + 1))]


def search_dishes(query: str, k: int = 5) -> list[dict]:
    q = norm(query)
    results = []
    for dish_id, names in _dish_names().items():
        best, via = 0.0, ""
        for n in names:
            s = _score(q, n)
            if s > best:
                best, via = s, n
        if best >= 0.5 or not q:
            d = data.dishes()[dish_id]
            results.append({"dish_id": dish_id, "name": d["name"], "region": d["region"],
                            "course": d["course"], "matched": via, "score": round(best, 3)})
    results.sort(key=lambda r: -r["score"])
    return results if q else results  # empty query lists everything (score 0)


def resolve_ingredient(text: str, k: int = 5) -> list[dict]:
    q = norm(text)
    cands: dict[str, dict] = {}
    rows = [(i, norm(r["canonical_name"]), "", "canonical", "no")
            for i, r in data.ingredients().items()]
    rows += [(i, norm(i), "", "id", "no") for i in data.ingredients()]
    rows += [(r["ingredient_id"], norm(r["alias"]), r["note"], r["language"], r["is_ambiguous"])
             for r in data.read_csv("ingredient_aliases.csv")]
    ambiguous_names = {(iid, name) for iid, name, _n, _l, amb in rows if amb == "yes"}
    for iid, name, note, lang, amb in rows:
        amb = "yes" if (iid, name) in ambiguous_names else amb
        s = _score(q, name)
        if s >= 0.5 and s > cands.get(iid, {}).get("score", 0):
            cands[iid] = {"ingredient_id": iid, "canonical_name": data.ingredients()[iid]["canonical_name"],
                          "matched": name, "language": lang, "ambiguous": amb == "yes",
                          "note": note, "score": round(s, 3)}
    return sorted(cands.values(), key=lambda c: -c["score"])[:k]
