"""The transport split (owner, 2026-11-26): by weight, exact to the cent."""

import random
from decimal import Decimal

import pytest

from app.services.errors import SupplyError
from app.services.transport import WeighedLine, delivered_unit_price, split_by_weight


def D(x: str) -> Decimal:
    return Decimal(x)


def test_the_owners_example():
    # A: 100 x 100 g at 200; B: 60 x 500 g at 400; transport 4,000
    a, b = split_by_weight(D("4000.00"), [WeighedLine("A", 100 * 100), WeighedLine("B", 60 * 500)])
    assert (a, b) == (D("1000.00"), D("3000.00"))
    assert delivered_unit_price(D("200.00"), a, 100) == D("210.00")
    assert delivered_unit_price(D("400.00"), b, 60) == D("450.00")


def test_leftover_cents_are_not_lost():
    shares = split_by_weight(D("100.00"), [WeighedLine(n, 1) for n in "ABC"])
    assert shares == [D("33.34"), D("33.33"), D("33.33")]


def test_every_split_adds_up_exactly():
    rng = random.Random(1126)  # noqa: S311 -- repeatable test data
    for _ in range(500):
        charge = Decimal(rng.randint(0, 5_000_000)) / 100
        lines = [WeighedLine(str(i), rng.randint(1, 80_000)) for i in range(rng.randint(1, 120))]
        shares = split_by_weight(charge, lines)
        assert sum(shares) == charge
        total = sum(ln.weight_grams for ln in lines)
        for share, ln in zip(shares, lines, strict=True):
            assert share >= 0
            assert abs(share - charge * ln.weight_grams / total) < D("0.01")


def test_a_missing_weight_names_the_products():
    with pytest.raises(SupplyError) as why:
        split_by_weight(D("10.00"), [WeighedLine("Soap", 70), WeighedLine("Napkin", None),
                                     WeighedLine("Cheese", 0)])
    assert "Napkin" in str(why.value) and "Cheese" in str(why.value)
    assert "Soap" not in str(why.value)


def test_no_charge_needs_no_weight():
    assert split_by_weight(D("0.00"), [WeighedLine("Napkin", None)]) == [D("0.00")]


@pytest.mark.parametrize("charge", ["-1.00", "1.001"])
def test_a_bad_charge_is_refused(charge):
    with pytest.raises(SupplyError):
        split_by_weight(D(charge), [WeighedLine("A", 1)])


def test_nothing_to_carry_it():
    with pytest.raises(SupplyError):
        split_by_weight(D("1.00"), [])
    with pytest.raises(SupplyError):
        delivered_unit_price(D("1.00"), D("1.00"), 0)
