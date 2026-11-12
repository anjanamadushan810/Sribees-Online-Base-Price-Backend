"""Text helpers for matching a supplier's (mostly Sinhala) line to an Online product
(mostly English, sometimes with name_si).

- romanize(): Sinhala script -> rough Latin ("බේබි ශෙරමි" -> "beebi sherami").
- sound_key(): a consonant skeleton that survives spelling differences between scripts
  ("Baby Cheramy" and "beebi sherami" both -> "PP SRM").
- parse_size(): "75g", "1.5L", "10s", "4pcs" -> (amount in g / ml / count, kind).

These only RANK suggestions. A person confirms every match (PLAN.md decision 6).
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

_CONSONANTS = {
    "ක": "k", "ඛ": "kh", "ග": "g", "ඝ": "gh", "ඞ": "ng", "ඟ": "ng", "ච": "ch", "ඡ": "ch",
    "ජ": "j", "ඣ": "jh", "ඤ": "ny", "ඥ": "gn", "ඦ": "nj", "ට": "t", "ඨ": "th", "ඩ": "d",
    "ඪ": "dh", "ණ": "n", "ඬ": "nd", "ත": "th", "ථ": "th", "ද": "d", "ධ": "dh", "න": "n",
    "ඳ": "nd", "ප": "p", "ඵ": "ph", "බ": "b", "භ": "bh", "ම": "m", "ඹ": "mb", "ය": "y",
    "ර": "r", "ල": "l", "ව": "v", "ශ": "sh", "ෂ": "sh", "ස": "s", "හ": "h", "ළ": "l",
    "ෆ": "f",
}
_VOWELS = {
    "අ": "a", "ආ": "aa", "ඇ": "ae", "ඈ": "aae", "ඉ": "i", "ඊ": "ii", "උ": "u", "ඌ": "uu",
    "ඍ": "ri", "එ": "e", "ඒ": "ee", "ඓ": "ai", "ඔ": "o", "ඕ": "oo", "ඖ": "au",
}
_SIGNS = {
    "ා": "aa", "ැ": "ae", "ෑ": "aae", "ි": "i", "ී": "ii", "ු": "u", "ූ": "uu", "ෘ": "ru",
    "ෙ": "e", "ේ": "ee", "ෛ": "ai", "ො": "o", "ෝ": "oo", "ෞ": "au", "ෲ": "ruu",
}
_VIRAMA = "්"
_ANUSVARA = {"ං": "ng", "ඃ": "h"}
_JOINERS = {"‌", "‍"}


def romanize(text: str) -> str:
    out: list[str] = []
    pending_a = False  # a consonant waiting for its inherent vowel
    for ch in unicodedata.normalize("NFC", text):
        if ch in _JOINERS:
            continue
        if ch in _CONSONANTS:
            if pending_a:
                out.append("a")
            out.append(_CONSONANTS[ch])
            pending_a = True
            continue
        if ch in _SIGNS:
            out.append(_SIGNS[ch])
            pending_a = False
            continue
        if ch == _VIRAMA:
            pending_a = False
            continue
        if pending_a:
            out.append("a")
            pending_a = False
        if ch in _VOWELS:
            out.append(_VOWELS[ch])
        elif ch in _ANUSVARA:
            out.append(_ANUSVARA[ch])
        else:
            out.append(ch)
    if pending_a:
        out.append("a")
    return "".join(out).lower()


def has_sinhala(text: str) -> bool:
    return any("඀" <= c <= "෿" for c in text)


# Consonant classes: sounds that different spellings / scripts swap for each other.
_DIGRAPHS = [
    ("ght", "t"), ("igh", "i"), ("x", "ks"), ("ph", "P"), ("bh", "P"), ("kh", "K"),
    ("gh", "K"), ("th", "T"), ("dh", "T"), ("sh", "S"), ("ch", "S"), ("ck", "K"),
    ("ng", "N"), ("ny", "N"), ("qu", "K"),
]
_SINGLE = {
    "s": "S", "z": "S", "j": "S",
    "k": "K", "c": "K", "q": "K", "g": "K",
    "t": "T", "d": "T",
    "p": "P", "b": "P", "f": "P", "v": "P", "w": "P",
    "m": "M", "n": "N", "l": "L", "r": "R",
}


def sound_key(word: str) -> str:
    """Consonant skeleton, vowels/h/y/spaces dropped, repeats collapsed
    ("Coca Cola" and "කොකාකෝලා" both -> "KL")."""
    w = romanize(word) if has_sinhala(word) else word.lower()
    w = re.sub(r"[^a-z]", "", w)
    for a, b in _DIGRAPHS:
        w = w.replace(a, b)
    key = []
    for c in w:
        k = c if c.isupper() else _SINGLE.get(c, "")
        if k and (not key or key[-1] != k):
            key.append(k)
    return "".join(key)


_SIZE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*"
    r"(kg|kgs|g|gm|gms|gram|grams|ml|l|ltr|litre|liter|pcs|pc|pieces|s|sch)(?![a-z])",
    re.IGNORECASE)
_UNIT = {
    "kg": ("mass", 1000), "kgs": ("mass", 1000), "g": ("mass", 1), "gm": ("mass", 1),
    "gms": ("mass", 1), "gram": ("mass", 1), "grams": ("mass", 1),
    "ml": ("volume", 1), "l": ("volume", 1000), "ltr": ("volume", 1000),
    "litre": ("volume", 1000), "liter": ("volume", 1000),
    "pcs": ("count", 1), "pc": ("count", 1), "pieces": ("count", 1), "s": ("count", 1),
}


def parse_size(text: str) -> tuple[Decimal, str] | None:
    """First size in the text: (amount in g, ml or pieces, kind). None when there is none."""
    for m in _SIZE.finditer(text or ""):
        unit = m.group(2).lower()
        if unit == "sch":  # "Sch" = sachet, not a size
            continue
        kind, factor = _UNIT[unit]
        return Decimal(m.group(1)) * factor, kind
    return None


def strip_size(text: str) -> str:
    return _SIZE.sub(" ", text or "")


_GENERIC = {
    # words that say what a thing is, not whose it is (Sinhala and English)
    "සබන්", "soap", "වොශින්", "පවුඩර්", "powder", "washing", "ටූත්", "toothpaste", "tooth",
    "paste", "බ්‍රෂ්", "brush", "පිටි", "milk", "කිරි", "තේ", "tea", "ශැම්පු", "ෂැම්පු",
    "shampoo", "බේබි", "baby", "new", "free", "pack", "promo", "ml", "g", "kg", "l",
}


def words(text: str) -> list[str]:
    return [w for w in re.split(r"[\s,/()+\-]+", strip_size(text).strip()) if w]


def brand_word(text: str) -> str:
    """First word that is not a generic one ("බේබි ශෙරමි සබන්" -> "ශෙරමි")."""
    for w in words(text):
        if w.lower() not in _GENERIC and not w.isdigit():
            return w
    ws = words(text)
    return ws[0] if ws else ""


# What a thing IS, in the supplier's Sinhala -> the English word Online uses. Used to
# require the same kind of product (a toothpaste line must not meet a toothbrush).
TYPE_WORDS = {
    "සබන්": "soap", "ෂැම්පු": "shampoo", "ශැම්පු": "shampoo", "ශැම්පූ": "shampoo",
    "තේ": "tea", "කිරි": "milk", "පිටි": "powder", "නූඩ්ල්ස්": "noodles",
    "බිස්කට්": "biscuit", "ටොෆි": "toffee", "හදුන්කූරු": "incense", "මාස්ක්": "mask",
    "පවුඩර්": "powder", "මැකරල්": "mackerel", "සෝස්": "sauce", "චීස්": "cheese",
    "පැස්ටා": "pasta", "ලයිටර්": "lighter", "ප්ලාස්ටර්": "plaster", "බාම්": "balm",
    "පෑන්": "pen", "ටේප්": "tape", "ග්ලු": "glue", "රේසර්": "razor", "නැප්කින්": "napkin",
    "පැම්පස්": "diaper", "බ්‍රෂ්": "toothbrush", "ක්‍රැකර්": "cracker", "මාරි": "marie",
    "මිල්ක්": "milk", "පීනට්": "peanut", "සෝයාමීට්": "soya", "සෝයා": "soya",
    "ඩ්‍රින්ක්": "drink", "බීම": "drink", "දියර": "liquid", "කහට": "tea",
    "ටූත්": "toothpaste", "ගිනි": "matches", "කැරලි": "clip", "කොලෝන්": "cologne",
}

# Kinds that tell products apart. A candidate showing one of these that is not ours is
# another kind of product. (milk / powder / liquid describe too many things to count.)
CONFLICT_KINDS = {
    "soap", "shampoo", "tea", "noodles", "biscuit", "toffee", "incense", "mask", "sauce",
    "cheese", "pasta", "lighter", "plaster", "balm", "pen", "tape", "glue", "razor",
    "napkin", "diaper", "toothbrush", "toothpaste", "cracker", "matches", "clip", "cologne",
    "drink", "jelly", "cordial", "perfume", "coffee", "pencil", "book", "notebook",
}


def kinds_in_english(text: str) -> set[str]:
    """Whole words only ("pen" is not in "pencils"); a trailing s is allowed."""
    found = set(re.findall(r"[a-z]+", (text or "").lower()))
    return {k for k in CONFLICT_KINDS if k in found or k + "s" in found or
            (k.endswith("s") and k[:-1] in found)}


def spelling(text: str) -> str:
    """Rough spelling for comparing short brands across scripts (kokaakoolaa ~ cocacola)."""
    s = romanize(text) if has_sinhala(text) else (text or "").lower()
    s = re.sub(r"[^a-z]", "", s).replace("c", "k").replace("w", "v").replace("ph", "f")
    return re.sub(r"([aeiou])\1+", r"\1", s)


def type_words(text: str) -> set[str]:
    kinds = {TYPE_WORDS[w] for w in words(text) if w in TYPE_WORDS}
    if "toothbrush" in kinds:
        kinds.discard("toothpaste")  # "ටූත් බ්‍රෂ්" is a brush
    return kinds
