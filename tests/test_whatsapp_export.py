"""The supplier's "share to WhatsApp" message is built from app data."""

from pathlib import Path

from app.pricelist import parse, render
from app.pricelist.render import offer_line

FIXTURE = Path(__file__).parent / "fixtures" / "nishi_palace_2026-11-12.txt"


def _data(price_list):
    """Everything that matters, without `raw` (spacing is normalised on export)."""
    return (
        price_list.header,
        price_list.footer,
        [
            (
                s.title,
                s.notes,
                [
                    (
                        p.heading,
                        p.notes_before,
                        p.notes_after,
                        [(o.price, o.mrp, o.units_per_box, o.label, o.flags, o.extra)
                         for o in p.offers],
                    )
                    for p in s.products
                ],
            )
            for s in price_list.sections
        ],
    )


def _load():
    return parse(FIXTURE.read_text(encoding="utf-8"))


def test_round_trip_import_export_import_is_identical():
    first = _load()
    message = render(first)
    again = parse(message)
    assert _data(again) == _data(first)
    # and the message is stable from then on
    assert render(again) == message


def test_inactive_products_and_offers_are_left_out():
    pl = _load()
    soap = next(p for p in pl.products() if p.heading == "බේබි ශෙරමි සබන් 75g")
    soap.offers[1].active = False  # 4+1 free pack no longer sold
    coke = next(p for p in pl.products() if p.heading == "කොකාකෝලා,ස්ප්‍රයිට්")
    coke.active = False

    message = render(pl)
    assert "4+1 free pack" not in message
    assert "(125/165)(150)" in message
    assert "කොකාකෝලා" not in message

    again = parse(message)
    assert len(again.products()) == len(pl.products()) - 1
    assert len(again.offers()) == len(pl.offers()) - 5 - 1


def test_section_with_nothing_active_is_dropped():
    pl = _load()
    stationery = next(s for s in pl.sections if s.title == "ලිපිද්‍රව්‍ය")
    for p in stationery.products:
        p.active = False
    assert "ලිපිද්‍රව්‍ය" not in render(pl)


def test_offer_line_format():
    pl = _load()
    lines = {offer_line(o) for o in pl.offers()}
    assert "(24.50/30)(240)" in lines
    assert "(98/120)(32) 250ml" in lines
    assert "(660)(100)" in lines  # flagged line rebuilt as written
    assert "(29/100)(12×26)" in lines
