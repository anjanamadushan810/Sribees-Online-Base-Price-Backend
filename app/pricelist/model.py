"""Plain data for a supplier's WhatsApp price list (no DB, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# Flags on an offer line the importer could not read with certainty.
# A flagged line is never guessed into the catalogue: a person confirms it.
FLAG_NO_MRP = "no_mrp"  # "(660)(100)": is 100 the MRP or the box size?
FLAG_NO_BOX = "no_box"  # "(775/1360)": units per box missing
FLAG_BOX_UNCLEAR = "box_unclear"  # "(12×26)": not a plain whole number
FLAG_BAD_FORMAT = "bad_format"  # "(8490)(14400)(720)": unexpected bracket count
FLAG_PRICE_ABOVE_MRP = "price_above_mrp"  # buying price higher than the selling price


@dataclass
class Offer:
    """One price line, e.g. "(424/530)(36) 4+1 free pack"."""

    price: Decimal
    mrp: Decimal | None
    units_per_box: int | None
    label: str = ""  # text after the brackets: "250ml", "Promo", "4+1 free pack"
    raw: str = ""  # the line exactly as the supplier wrote it
    line_no: int = 0  # 1-based line in the pasted text (0 = not from a paste)
    flags: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)  # bracket contents we could not place
    active: bool = True

    @property
    def needs_review(self) -> bool:
        return bool(self.flags)


@dataclass
class Product:
    """One heading ("🟡බේබි ශෙරමි සබන් 75g") with its offers and notes."""

    heading: str
    offers: list[Offer] = field(default_factory=list)
    notes_before: list[str] = field(default_factory=list)  # e.g. "(Import brand)"
    notes_after: list[str] = field(default_factory=list)  # e.g. "( Rathmal/Delum/Almond )"
    active: bool = True


@dataclass
class Section:
    """A category block ("🛑🛑සබන් වර්ග 🛑🛑")."""

    title: str
    products: list[Product] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # free text, e.g. bulk-deal remarks


@dataclass
class PriceList:
    header: list[str] = field(default_factory=list)  # kept verbatim, blank lines included
    sections: list[Section] = field(default_factory=list)
    footer: list[str] = field(default_factory=list)  # kept verbatim

    def products(self) -> list[Product]:
        return [p for s in self.sections for p in s.products]

    def offers(self) -> list[Offer]:
        return [o for p in self.products() for o in p.offers]
