"""Import of Nishi Palace's real WhatsApp price list (owner, 2026-11-12)."""

from decimal import Decimal
from pathlib import Path

import pytest

from app.pricelist import parse
from app.pricelist.parser import is_offer_line, parse_offer

FIXTURE = Path(__file__).parent / "fixtures" / "nishi_palace_2026-11-12.txt"


@pytest.fixture(scope="module")
def price_list():
    return parse(FIXTURE.read_text(encoding="utf-8"))


def _find(price_list, heading):
    found = [p for p in price_list.products() if p.heading == heading]
    assert len(found) == 1, f"{heading!r}: {len(found)} products"
    return found[0]


def _o(offer):
    return (offer.price, offer.mrp, offer.units_per_box, offer.label)


def test_counts(price_list):
    assert len(price_list.sections) == 11
    assert len(price_list.products()) == 144
    assert len(price_list.offers()) == 178
    assert all(p.heading for p in price_list.products()), "no offer may lose its heading"
    assert all(p.offers for p in price_list.products()), "every heading has offers"


def test_exactly_the_six_irregular_lines_are_flagged_never_guessed(price_list):
    flagged = {o.raw: o.flags for o in price_list.offers() if o.needs_review}
    assert flagged == {
        "(660)(100)": ["no_mrp"],
        "(775/1360)": ["no_box"],
        "(1460/2550)": ["no_box"],
        "(8490)(14400)(720)": ["no_mrp", "bad_format"],
        "(29/100)(12×26)": ["box_unclear"],
        "(1120)(1)": ["no_mrp"],
    }
    regular = [o for o in price_list.offers() if not o.needs_review]
    assert len(regular) == 172
    assert all(o.mrp is not None and o.units_per_box for o in regular)


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        (
            "බේබි ශෙරමි සබන් 75g",
            [
                (Decimal(125), Decimal(165), 150, ""),
                (Decimal(424), Decimal(530), 36, "4+1 free pack"),
                (Decimal(560), Decimal(690), 36, "5+1 free pack"),
            ],
        ),
        (
            # heading without 🟡, separated from its prices by a blank line
            "කොකාකෝලා,ස්ප්‍රයිට්",
            [
                (Decimal(98), Decimal(120), 32, "250ml"),
                (Decimal(174), Decimal(200), 24, "400ml"),
                (Decimal(249), Decimal(300), 12, "1L"),
                (Decimal(369), Decimal(420), 12, "1.5L"),
                (Decimal(419), Decimal(500), 12, "2L"),
            ],
        ),
        ("ඩීවා වොශින් පවුඩර් 55g", [(Decimal("24.50"), Decimal(30), 240, "")]),
        ("නෙස්ටමෝල්ට් Sch", [(Decimal("54.25"), Decimal(60), 450, "")]),
        (
            "ලයිෆ්බෝයි සබන්",
            [
                (Decimal(130), Decimal(170), 147, "95g"),
                (Decimal(233), Decimal(275), 75, "Double"),  # "( 75 )" with spaces
            ],
        ),
        (
            "වෙල්වට් සබන්",
            [
                (Decimal(92), Decimal(120), 180, "70g"),  # "( 92 /120)"
                (Decimal(128), Decimal(170), 144, "95g"),  # label glued: "(144)95g"
            ],
        ),
        (
            "ක්ලෝගාර්ඩ් ටූත්  200g",
            [
                (Decimal(268), Decimal(340), 24, ""),
                (Decimal(259), Decimal(299), 24, "Promo"),
            ],
        ),
        (
            "ෆෙම්ස් 10s",
            [
                (Decimal(184), Decimal(270), 24, ""),
                (Decimal(174), Decimal(240), 24, "Promo price"),
                (Decimal(209), Decimal(300), 24, "supierer"),
            ],
        ),
        ("හැක්ස් ටොෆි", [(Decimal(5650), Decimal(7500), 1, "")]),
    ],
)
def test_offers(price_list, heading, expected):
    assert [_o(o) for o in _find(price_list, heading).offers] == expected


def test_notes_are_kept_not_taken_for_prices(price_list):
    # The supplier typed two ZWJs in this word; kept as written (matching normalises later).
    assert _find(price_list, "රේ‍‍සර් තල").notes_before == ["(Import brand)"]
    assert _find(price_list, "පැන්ඩා බේබි සබන්").notes_after == ["( Rathmal/Delum/Almond )"]
    assert _find(price_list, "බේබි පැම්පස් (4 size)").notes_before == ["(NMRA Approve) 4pcs"]
    food = next(s for s in price_list.sections if s.title == "ආහාර/ටින් මාලු")
    assert food.notes == [
        "💥සැමන් කේස් 100ට වැඩි ඇණවුම් සඳහා විශේෂ මිලක් ලබාදිය හැක.",
        "Rs 420/-සිට",
    ]


def test_sinhala_joiners_survive(price_list):
    titles = [s.title for s in price_list.sections]
    assert "ටූත් පේස්ට් / බ්‍රෂ්" in titles  # contains U+200D
    assert "‍" in "".join(titles)


def test_header_and_footer_verbatim(price_list):
    assert price_list.header[0] == "#PRICE LIST (Wholesale ) 🛒👇"
    assert "(අපේ මිල/ විකුණුම් මිල)(බොක්ස් එකක ප්‍රමාණය)" in price_list.header
    assert price_list.footer[0] == "⚡ලංකාවේ අඩුම මිල සහ විශ්වාසවන්තම ආයතනය"
    assert "NISHI SUPER PALACE" in price_list.footer


@pytest.mark.parametrize(
    ("line", "is_offer"),
    [
        ("(125/165)(150)", True),
        ("(660)(100)", True),
        ("( 61 / 70 )(60) 50g", True),
        ("(NMRA Approve) 4pcs", False),
        ("( Rathmal/Delum/Almond )", False),
        ("(Import brand)", False),
        ("Rs 420/-සිට", False),
    ],
)
def test_is_offer_line(line, is_offer):
    assert is_offer_line(line) is is_offer


def test_price_above_mrp_is_flagged():
    assert parse_offer("(200/150)(10)").flags == ["price_above_mrp"]
