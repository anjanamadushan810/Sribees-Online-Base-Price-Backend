"""Rank Online products for one supplier offer.

Rules (same spirit as Price Intel's owner rule: brand + name + size must agree):
- Sizes: when both sides state a size, they must be the same kind and amount, else the
  candidate is dropped. An unknown size never drops, but scores lower.
- Brand: the first non-generic word must sound alike (sound_key ratio >= BRAND_MIN),
  compared in Sinhala against name_si when Online has one, else by sound against the
  English name. Another brand is never suggested.
- Only suggestions: a person confirms every match.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher

from app.matching.text import brand_word, has_sinhala, parse_size, romanize, sound_key, words

BRAND_MIN = 0.75
SUGGEST_MIN = 0.6
TOP = 3


@dataclass(frozen=True)
class Candidate:
    online_product_id: object
    name: str
    name_si: str | None
    size: tuple[Decimal, str] | None


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _same_size(a: tuple[Decimal, str], b: tuple[Decimal, str]) -> bool:
    if a[1] != b[1]:
        return False
    big = max(a[0], b[0])
    return big == 0 or abs(a[0] - b[0]) / big <= Decimal("0.01")


def _brand_sim(supplier_heading: str, c: Candidate) -> float:
    sb = brand_word(supplier_heading)
    best = 0.0
    if c.name_si and has_sinhala(sb):
        best = _ratio(romanize(sb), romanize(brand_word(c.name_si)))
    best = max(best, _ratio(sound_key(sb), sound_key(brand_word(c.name))))
    # two-word brands ("Coca Cola", "Sun Light") written as one word on the other side
    ws, wc = words(supplier_heading), words(c.name)
    if len(wc) >= 2:
        best = max(best, _ratio(sound_key(sb), sound_key(wc[0] + wc[1])))
    if len(ws) >= 2:
        best = max(best, _ratio(sound_key(ws[0] + ws[1]), sound_key(brand_word(c.name))))
    return best


def _name_sim(text: str, c: Candidate) -> float:
    mine = [sound_key(w) for w in words(text)]
    mine = [k for k in mine if k]
    if not mine:
        return 0.0
    theirs = [k for k in (sound_key(w) for w in words(c.name)) if k]
    if c.name_si:
        theirs += [k for k in (sound_key(w) for w in words(c.name_si)) if k]
    hit = sum(1 for k in mine if any(_ratio(k, t) >= 0.8 for t in theirs))
    return hit / len(mine)


def score(heading: str, label: str, c: Candidate) -> float | None:
    """0..1, or None when the candidate must never be suggested."""
    mine_size = parse_size(label) or parse_size(heading)
    if mine_size and c.size and not _same_size(mine_size, c.size):
        return None
    brand = _brand_sim(heading, c)
    if brand < BRAND_MIN:
        return None
    name = _name_sim(f"{heading} {label}", c)
    size_part = 1.0 if (mine_size and c.size) else 0.5
    return round(0.5 * brand + 0.3 * name + 0.2 * size_part, 3)


def best(heading: str, label: str, candidates: list[Candidate],
         exclude: set = frozenset()) -> list[tuple[Candidate, float]]:
    ranked = []
    for c in candidates:
        if c.online_product_id in exclude:
            continue
        s = score(heading, label, c)
        if s is not None and s >= SUGGEST_MIN:
            ranked.append((c, s))
    ranked.sort(key=lambda x: -x[1])
    return ranked[:TOP]
