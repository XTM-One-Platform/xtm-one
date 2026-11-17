"""Hand-written PDFs for the figure-description tests.

Each page is a list of drawing operations: ``("text", str)`` writes a line of
Helvetica, ``("image", fraction)`` draws a grey image covering *fraction* of
the page, ``("paths", count)`` strokes *count* line segments across the page,
``("outline_text",)`` draws letter shapes as paths only (an export whose text
was turned into outlines). No PDF library is needed to write them, and both
readers the platform uses (``pypdf`` for the text layer, ``pypdfium2`` for the
objects and the renders) read them.
"""

from __future__ import annotations

import math

PAGE_W, PAGE_H = 612, 792


def _page_stream(ops: list[tuple]) -> tuple[bytes, bool]:
    lines: list[str] = []
    has_image = False
    y = PAGE_H - 72
    for op in ops:
        kind = op[0]
        if kind == "text":
            text = op[1].replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            lines.append(f"BT /F1 11 Tf 72 {y} Td ({text}) Tj ET")
            y -= 16
        elif kind == "image":
            side = math.sqrt(op[1] * PAGE_W * PAGE_H)
            lines.append(f"q {side:.2f} 0 0 {side:.2f} 72 72 cm /Im1 Do Q")
            has_image = True
        elif kind == "paths":
            count = op[1]
            for i in range(count):
                x = 72 + (i * 400 / max(count, 1))
                lines.append(f"{x:.1f} 150 m {x + 40:.1f} 600 l S")
        elif kind == "outline_text":
            # Glyph-like closed shapes: a vectorised heading, no text object.
            for i in range(40):
                x = 72 + (i % 10) * 45
                yy = 650 - (i // 10) * 60
                lines.append(f"{x} {yy} m {x + 20} {yy + 30} l {x + 40} {yy} l h f")
    return "\n".join(lines).encode("latin-1"), has_image


def build_pdf(pages: list[list[tuple]]) -> bytes:
    """A PDF of *pages* (see the module docstring)."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled below
    pages_id = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pixels = bytes([128, 128, 128]) * 16
    image = add(
        b"<< /Type /XObject /Subtype /Image /Width 4 /Height 4 /ColorSpace /DeviceRGB "
        b"/BitsPerComponent 8 /Length " + str(len(pixels)).encode() + b" >>\nstream\n" + pixels + b"\nendstream"
    )
    kids: list[int] = []
    for ops in pages:
        stream, has_image = _page_stream(ops)
        content = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
        xobject = f" /XObject << /Im1 {image} 0 R >>" if has_image else ""
        kids.append(
            add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                f"/Resources << /Font << /F1 {font} 0 R >>{xobject} >> /Contents {content} 0 R >>".encode()
            )
        )
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    objects[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    )
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


#: A body line long enough that a page of a few of them is not "almost no text".
BODY = "The analyst records each narrative, its channels and the accounts that amplify it."

TEXT_PAGE = [("text", BODY), ("text", BODY)]
FIGURE_PAGE = [("text", BODY), ("text", BODY), ("image", 0.25)]
DRAWING_PAGE = [("text", BODY), ("text", BODY), ("paths", 30)]
SCANNED_PAGE = [("image", 0.9)]
OUTLINED_PAGE = [("outline_text",)]
BLANK_PAGE: list[tuple] = []
LOGO_PAGE = [("text", BODY), ("text", BODY), ("image", 0.01)]
RULED_PAGE = [("text", BODY), ("text", BODY), ("paths", 2)]


def pdf_and_text(*pages: list[tuple]) -> tuple[bytes, str]:
    """A PDF of *pages* and the text its upload stores (``extract_text_from_file``)."""
    from app.services.chunking import extract_text_from_file

    data = build_pdf(list(pages))
    return data, extract_text_from_file(data, "doc.pdf")


def reading_for(content: str, pages: list[dict], **extra: object) -> dict:
    """A ``complete`` figure reading of *content* with *pages*, *extra* overriding."""
    from app.services import kb_figures

    return {
        "version": kb_figures.READING_VERSION,
        "content_hash": kb_figures.content_digest(content),
        "status": kb_figures.STATUS_COMPLETE,
        "pages": pages,
        **extra,
    }
