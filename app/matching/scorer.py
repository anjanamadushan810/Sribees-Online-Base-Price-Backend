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

from app.matching.text import (
    brand_word,
    kinds_in_english,
    parse_size,
    sound_key,
    spelling,
    type_words,
    words,
)

BRAND_MIN = 0.85  # for brand keys longer than SHORT_KEY
SHORT_KEY = 4  # keys this short must be identical (PLPT != PLPS)
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


def _brand_close(a: str, b: str) -> float:
    """1.0 for the same brand sound, else a similarity that counts only for long keys.
    Short keys differ by one letter between unrelated brands (Velvet PLPT, Philips PLPS)."""
    ka, kb = sound_key(a), sound_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        if len(ka) > 3:
            return 1.0
        # 2-3 letter keys collide between brands (Pears/Wijaya "PS", Yahaposha/Pepsi):
        # the spelling must also be close.
        return 1.0 if _ratio(spelling(a), spelling(b)) >= 0.65 else 0.0
    if min(len(ka), len(kb)) <= SHORT_KEY:
        # Sri Lankan spelling drops sounds the English keeps (පෙයාස් "peyas" = Pears):
        # a short brand with a different key still counts when spelt almost alike.
        return 1.0 if _ratio(spelling(a), spelling(b)) >= 0.75 else 0.0
    return _ratio(ka, kb)


def _brand_sim(supplier_heading: str, c: Candidate) -> float:
    sb = brand_word(supplier_heading)
    best = _brand_close(sb, brand_word(c.name))
    if c.name_si:
        best = max(best, _brand_close(sb, brand_word(c.name_si)))
    # A two-word brand on Online written as one word by the supplier ("Coca Cola").
    wc = words(c.name)
    if len(wc) >= 2:
        best = max(best, _brand_close(sb, wc[0] + wc[1]))
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
    if mine_size and (not c.size or not _same_size(mine_size, c.size)):
        return None  # a stated size must be the product's size
    ours = type_words(heading)
    theirs = kinds_in_english(c.name)
    if ours and theirs and not (ours & theirs):
        return None  # clearly another kind of product (toothbrush for a toothpaste line)
    brand = _brand_sim(heading, c)
    if brand < BRAND_MIN:
        return None
    name = _name_sim(f"{heading} {label}", c)
    size_part = 1.0 if (mine_size and c.size) else 0.5
    return round(0.5 * brand + 0.3 * name + 0.2 * size_part, 3)


def best(heading: str, label: str, candidates: list[Candidate],
         exclude: set = frozenset()) -> list[tuple[Candidate, float]]:
    mine_size = parse_size(label) or parse_size(heading)
    ranked = []
    for c in candidates:
        if c.online_product_id in exclude:
            continue
        s = score(heading, label, c)
        if s is not None and s >= SUGGEST_MIN:
            ranked.append((c, s))
    # When the line states a size and some candidate has exactly that size, products
    # whose size is unknown are not offered beside it (Clogard 70g != a toothbrush).
    if mine_size and any(c.size and _same_size(mine_size, c.size) for c, _ in ranked):
        ranked = [(c, s) for c, s in ranked if c.size and _same_size(mine_size, c.size)]
    ranked.sort(key=lambda x: -x[1])
    return ranked[:TOP]
