"""Deterministic availability grading for Singapore.

Every grade is computed from stored evidence rows - never asserted by a model.

Grades (checked in this order):
  Confirmed  - at least one exact-match listing read directly from a retailer
               catalogue with in_stock = true on the verification date.
  Likely     - an exact-match listing exists, but stock was not verified
               (out of stock at check time, or only seen via a search index).
  Difficult  - no exact-match listing, but a variant form exists (paste instead
               of block, pickle instead of leaves, powder instead of whole, seeds
               to grow) OR nothing at all was found after searching
               >= MIN_SOURCES_FOR_ABSENCE independent sources.
  Unknown    - nothing found and fewer than MIN_SOURCES_FOR_ABSENCE sources
               searched: we do not know.

At read time `effective_grade` downgrades Confirmed -> Likely once the evidence
is older than MAX_CONFIRMED_AGE_DAYS, because stock claims go stale.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

MIN_SOURCES_FOR_ABSENCE = 3
MAX_CONFIRMED_AGE_DAYS = 30
CATALOGUE_METHODS = {"catalogue_api", "catalogue_page"}

GRADES = ("Confirmed", "Likely", "Difficult", "Unknown")


@dataclass
class Evidence:
    listing_id: str
    retailer_id: str
    match: str            # exact | variant | none
    stock_status: str     # in_stock | out_of_stock | unverified | n/a
    evidence_method: str  # catalogue_api | catalogue_page | search_index | web_search
    verified_on: str      # ISO date


@dataclass
class GradeResult:
    grade: str
    rule: str
    confidence: float
    n_retailers_in_stock_exact: int
    n_retailers_listed_exact: int
    n_retailers_variant: int
    n_sources_searched: int
    supporting_listing_ids: list[str] = field(default_factory=list)
    verified_on: str = ""


def grade_item(evidence: Iterable[Evidence]) -> GradeResult:
    ev = list(evidence)
    searched = {e.retailer_id for e in ev}
    exact_in_stock = [e for e in ev if e.match == "exact" and e.stock_status == "in_stock"
                      and e.evidence_method in CATALOGUE_METHODS]
    exact_listed = [e for e in ev if e.match == "exact" and e not in exact_in_stock]
    variants = [e for e in ev if e.match == "variant"]
    verified_on = max((e.verified_on for e in ev), default="")

    n_in = len({e.retailer_id for e in exact_in_stock})
    n_listed = len({e.retailer_id for e in exact_listed})
    n_var = len({e.retailer_id for e in variants})

    if exact_in_stock:
        return GradeResult("Confirmed", "R1_exact_in_stock_catalogue",
                           round(min(0.95, 0.7 + 0.1 * (n_in - 1)), 2),
                           n_in, n_listed, n_var, len(searched),
                           [e.listing_id for e in exact_in_stock], verified_on)
    if exact_listed:
        return GradeResult("Likely", "R2_exact_listed_stock_unverified", 0.5,
                           n_in, n_listed, n_var, len(searched),
                           [e.listing_id for e in exact_listed], verified_on)
    if variants:
        return GradeResult("Difficult", "R3a_variant_form_only", 0.35,
                           n_in, n_listed, n_var, len(searched),
                           [e.listing_id for e in variants], verified_on)
    if len(searched) >= MIN_SOURCES_FOR_ABSENCE:
        return GradeResult("Difficult", "R3b_absent_after_sufficient_search", 0.3,
                           n_in, n_listed, n_var, len(searched), [], verified_on)
    return GradeResult("Unknown", "R4_insufficient_evidence", 0.0,
                       n_in, n_listed, n_var, len(searched), [], verified_on)


def effective_grade(grade: str, verified_on: str, today: date | None = None,
                    max_age_days: int = MAX_CONFIRMED_AGE_DAYS) -> str:
    """Grade to show a user today: stale Confirmed evidence becomes Likely."""
    if grade != "Confirmed" or not verified_on:
        return grade
    today = today or date.today()
    age = (today - date.fromisoformat(verified_on)).days
    return "Likely" if age > max_age_days else grade
