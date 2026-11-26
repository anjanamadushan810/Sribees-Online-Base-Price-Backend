"""One transport charge split over the delivered lines by weight (owner, 2026-11-26).

A supplier gives a price per item and one transport charge for the whole delivery. Each
line carries the share of the charge that its weight is of the whole delivery, and its
delivered unit price (= the base price) is the bill price plus that share per paid unit.
Free units weigh something, so they take part in the split, but they do not lower the
base price: the share is divided over the units paid for.

Pure arithmetic: no database, so the money rule can be read and tested on its own.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from app.services.errors import SupplyError

CENT = Decimal("0.01")
ZERO = Decimal("0")


@dataclass(frozen=True)
class WeighedLine:
    name: str
    weight_grams: int | None  # of the whole line, free units included; None = not known


def split_by_weight(charge: Decimal, lines: Sequence[WeighedLine]) -> list[Decimal]:
    """Each line's share of `charge`, in the order given; the shares add up exactly.

    Every share is first cut down to whole cents; the cents left over go one each to the
    lines that lost the most by it (earlier line first on a tie)."""
    if charge < 0 or charge != charge.quantize(CENT):
        raise SupplyError("the transport charge is rupees and cents, not below zero")
    if not lines:
        raise SupplyError("nothing received to carry the transport charge")
    if charge == 0:
        return [ZERO.quantize(CENT) for _ in lines]
    unknown = [ln.name for ln in lines if not ln.weight_grams or ln.weight_grams <= 0]
    if unknown:
        raise SupplyError("no weight for " + ", ".join(unknown) + ": add it on the product in "
                          "SRIBEESonline, sync, and save again")
    total = sum(ln.weight_grams for ln in lines)
    exact = [charge * ln.weight_grams / total for ln in lines]
    shares = [x.quantize(CENT, rounding=ROUND_DOWN) for x in exact]
    left = int((charge - sum(shares, ZERO)) / CENT)
    by_loss = sorted(range(len(lines)), key=lambda i: (shares[i] - exact[i], i))
    for i in by_loss[:left]:
        shares[i] += CENT
    return shares


def delivered_unit_price(unit_price: Decimal, share: Decimal, paid_units: int) -> Decimal:
    """Bill price per unit plus the line's transport share per paid unit, to the cent."""
    if paid_units <= 0:
        raise SupplyError("a delivered price needs at least one paid unit")
    return (unit_price + share / paid_units).quantize(CENT, rounding=ROUND_HALF_UP)
