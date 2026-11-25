"""A4 sticker sheets of QR labels as a small vector PDF (no extra PDF library).

Each label is the QR of its 10-digit number only (decision 27: no price, and the owner chose
QR only: no printed digits, the labels are small). The QR is drawn as filled squares, so it
stays sharp on any printer. The last page lists every number in the run, so a label that
will not scan can still be typed.
"""

import zlib
from collections.abc import Sequence

import segno

MM = 72 / 25.4
A4_W, A4_H = 210.0, 297.0  # mm
MARGIN_X, MARGIN_Y = 5.0, 8.5  # mm, typical for pre-cut A4 sticker sheets
QR_GAP = 3.0  # mm of white kept around the QR inside each label (quiet zone)
LIST_COLS, LIST_ROWS = 6, 64


def qr_matrix(number: str) -> list[list[bool]]:
    """The modules of a number's QR, without the quiet zone (numeric mode, level M)."""
    qr = segno.make_qr(number, mode="numeric", error="m", boost_error=False)
    return [[bool(v) for v in row] for row in qr.matrix]


def _qr_ops(number: str, x0: float, y_top: float, size: float) -> list[str]:
    """PDF path operators that fill the dark modules of one QR. (x0, y_top) is the top-left
    corner in points; dark modules in a row are merged into one rectangle."""
    matrix = qr_matrix(number)
    n = len(matrix)
    m = size / n
    ops = []
    for r, row in enumerate(matrix):
        c = 0
        while c < n:
            if not row[c]:
                c += 1
                continue
            start = c
            while c < n and row[c]:
                c += 1
            y = y_top - (r + 1) * m
            ops.append(f"{x0 + start * m:.3f} {y:.3f} {(c - start) * m:.3f} {m:.3f} re")
    ops.append("f")
    return ops


def _text(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def layout(cols: int, rows: int) -> tuple[float, float, float]:
    """Label width, height and QR size in mm for a cols x rows sheet."""
    w = (A4_W - 2 * MARGIN_X) / cols
    h = (A4_H - 2 * MARGIN_Y) / rows
    return w, h, min(w, h) - 2 * QR_GAP


def sheet_pages(numbers: Sequence[str], cols: int, rows: int) -> list[str]:
    w, h, qr = layout(cols, rows)
    if qr < 10:
        raise ValueError("labels this small would not scan; use fewer per sheet")
    per_page = cols * rows
    pages = []
    for p in range(0, len(numbers), per_page):
        ops = ["0 g"]
        for i, number in enumerate(numbers[p:p + per_page]):
            r, c = divmod(i, cols)
            cx = MARGIN_X + c * w + (w - qr) / 2
            cy_top = A4_H - MARGIN_Y - r * h - (h - qr) / 2
            ops += _qr_ops(number, cx * MM, cy_top * MM, qr * MM)
        pages.append("\n".join(ops))
    return pages


def list_pages(numbers: Sequence[str], title: str) -> list[str]:
    pages = []
    per_page = LIST_COLS * LIST_ROWS
    total = (len(numbers) + per_page - 1) // per_page
    for k, p in enumerate(range(0, len(numbers), per_page), start=1):
        ops = ["BT", "/F1 11 Tf", f"{15 * MM:.2f} {(A4_H - 15) * MM:.2f} Td",
               f"({_text(f'{title} - page {k} of {total}')}) Tj", "ET"]
        for i, number in enumerate(numbers[p:p + per_page]):
            c, r = divmod(i, LIST_ROWS)
            x = (15 + c * 31) * MM
            y = (A4_H - 25 - r * 4.1) * MM
            ops += ["BT", "/F1 8.5 Tf", f"{x:.2f} {y:.2f} Td",
                    f"({number[:4]} {number[4:8]} {number[8:]}) Tj", "ET"]
        pages.append("\n".join(ops))
    return pages


def build_pdf(numbers: Sequence[str], *, cols: int, rows: int, title: str) -> bytes:
    contents = sheet_pages(numbers, cols, rows) + list_pages(numbers, title)
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled below
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for content in contents:
        data = zlib.compress(content.encode("latin-1"))
        stream = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data)
                     + data + b"\nendstream")
        kids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_obj, A4_W * MM, A4_H * MM, font, stream)))
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
    objects[pages_obj - 1] = (b"<< /Type /Pages /Kids [%s] /Count %d >>"
                              % (b" ".join(b"%d 0 R" % k for k in kids), len(kids)))
    info = add(b"<< /Title (%s) /Producer (Sribees Supply) >>" % _text(title).encode("latin-1"))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, catalog, info, xref))
    return bytes(out)
