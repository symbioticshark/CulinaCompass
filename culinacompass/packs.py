"""Parse pack sizes out of Singapore retailer product titles.

Titles are messy ("Fresh Pumpkin Yellow - 1 Pc (800 g to 1 Kg)", "Ooty Moong Dhall
Split 1kg (Pack of 5)", "Fresh Green Chilli 150g-200g"). This module turns a title
into grams (or millilitres, or a piece count) deterministically so unit prices and
dish costs never depend on a language model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_UNIT = r"(kg|kgs|g|gm|gms|gram|grams|ml|l|ltr|litre|liter)"
_NUM = r"(?<![A-Za-z\d.])(\d+(?:\.\d+)?)"

_RANGE = re.compile(
    _NUM + r"\s*" + _UNIT + r"?\s*(?:-|–|to)\s*" + _NUM + r"\s*" + _UNIT + r"\b",
    re.IGNORECASE,
)
_SINGLE = re.compile(_NUM + r"\s*" + _UNIT + r"\b", re.IGNORECASE)
_PACK_OF = re.compile(r"pack\s+of\s+(\d+)", re.IGNORECASE)
_TIMES = re.compile(r"(\d+)\s*[x×]\s*" + _NUM + r"\s*" + _UNIT + r"\b", re.IGNORECASE)
_PIECES = re.compile(r"(\d+)\s*(?:pcs|pc|pieces|piece|nos)\b", re.IGNORECASE)

_TO_BASE = {
    "kg": ("g", 1000.0), "kgs": ("g", 1000.0), "g": ("g", 1.0), "gm": ("g", 1.0),
    "gms": ("g", 1.0), "gram": ("g", 1.0), "grams": ("g", 1.0),
    "ml": ("ml", 1.0), "l": ("ml", 1000.0), "ltr": ("ml", 1000.0),
    "litre": ("ml", 1000.0), "liter": ("ml", 1000.0),
}


@dataclass
class Pack:
    qty: Optional[float]      # amount in base unit (g or ml); None if unknown
    unit: Optional[str]       # "g", "ml", or "piece"
    pieces: Optional[int]     # piece count when the title only gives pieces
    method: str               # how it was parsed, kept for audit

    def grams(self, density_g_per_ml: Optional[float] = None,
              typical_piece_g: Optional[float] = None) -> Optional[float]:
        if self.unit == "g":
            return self.qty
        if self.unit == "ml":
            return self.qty * (density_g_per_ml or 1.0) if self.qty is not None else None
        if self.unit == "piece" and self.pieces and typical_piece_g:
            return self.pieces * typical_piece_g
        return None


def _base(num: str, unit: str) -> tuple[str, float]:
    b, mult = _TO_BASE[unit.lower()]
    return b, float(num) * mult


def parse_pack(title: str) -> Pack:
    """Return the pack size implied by a product title."""
    t = title.replace(" ", " ")
    multiplier = 1
    m = _PACK_OF.search(t)
    if m:
        multiplier = int(m.group(1))

    m = _TIMES.search(t)
    if m:
        count = int(m.group(1))
        b, q = _base(m.group(2), m.group(3))
        return Pack(q * count * multiplier, b, None, "n_x_size")

    m = _RANGE.search(t)
    if m:
        lo_num, lo_unit, hi_num, hi_unit = m.groups()
        hb, hq = _base(hi_num, hi_unit)
        lb, lq = _base(lo_num, lo_unit or hi_unit)
        if lb == hb:
            return Pack((lq + hq) / 2 * multiplier, hb, None, "range_midpoint")

    m = _SINGLE.search(t)
    if m:
        b, q = _base(m.group(1), m.group(2))
        return Pack(q * multiplier, b, None, "single")

    m = _PIECES.search(t)
    if m:
        return Pack(None, "piece", int(m.group(1)) * multiplier, "pieces_only")

    return Pack(None, None, None, "unparsed")
