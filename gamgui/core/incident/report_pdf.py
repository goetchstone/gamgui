"""The report as a PDF, drawn from the same blocks as its text and Markdown, so all three say the same. A small writer with no dependency: PDF's own Helvetica and Courier, so it needs no font file and runs the
same anywhere. Those fonts hold Western text only: a character they can't show reads "?", and the document says so at its end, the text and Markdown having it. Text an attacker wrote is only ever a string
in the page's content, escaped, so it can't reach the file's structure."""

from __future__ import annotations

import zlib
from typing import List, Sequence, Tuple

from .report import Block

PAGE_WIDTH, PAGE_HEIGHT = 612, 792      # US Letter, in points
MARGIN = 54.0
_LEFT, _RIGHT = MARGIN, PAGE_WIDTH - MARGIN
# Helvetica's widths for U+0020 to U+007E, per 1000 points, from its Adobe font metrics. Anything else counts as the widest, so it wraps early rather than running off the page: only the wrap uses them.
_WIDTHS = (278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
           1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
           333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584)
COULD_NOT_SHOW = "Some characters in this report can't be shown in a PDF's own fonts, and read as “?” here. The report's Markdown and text files have them."


def _width(text: str, size: float, font: str) -> float:
    if font == "F3":
        return len(text) * 600 * size / 1000
    per = 1.2 if font == "F2" else 1.0      # bold's letters run up to this much wider
    return sum(_WIDTHS[ord(c) - 32] if 32 <= ord(c) <= 126 else 1000 for c in text) * per * size / 1000


def _wrapped(text: str, size: float, font: str, width: float) -> List[str]:
    lines: List[str] = []
    current = ""
    for word in text.split():
        joined = f"{current} {word}" if current else word
        if _width(joined, size, font) <= width:
            current = joined
            continue
        if current:
            lines.append(current)
        if _width(word, size, font) <= width:
            current = word
            continue
        current = ""
        piece: List[str] = []
        used = 0.0
        for c in word:      # an address or an ID longer than the line: cut, once through it, never run off the page
            one = _width(c, size, font)
            if piece and used + one > width:
                lines.append("".join(piece))
                piece, used = [], 0.0
            piece.append(c)
            used += one
        current = "".join(piece)
    if current:
        lines.append(current)
    return lines or [""]


def _encoded(text: str) -> Tuple[bytes, bool]:
    """In PDF's WinAnsi encoding, and whether anything was replaced. Controls and line breaks are spaces."""
    clean = "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in text)
    encoded = clean.encode("cp1252", "replace")
    return encoded, encoded.count(b"?") != clean.count("?")


def _literal(data: bytes) -> bytes:
    return b"(" + data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


class _Pages:
    def __init__(self) -> None:
        self.pages: List[bytes] = []
        self.current: List[bytes] = []
        self.y = PAGE_HEIGHT - MARGIN
        self.replaced = False

    def _break(self) -> None:
        self.pages.append(b"\n".join(self.current))
        self.current = []
        self.y = PAGE_HEIGHT - MARGIN

    def line(self, text: str, size: float = 10.5, font: str = "F1", x: float = _LEFT, leading: float = 1.35) -> None:
        if self.y - size * leading < MARGIN:
            self._break()
        self.y -= size * leading
        data, replaced = _encoded(text)
        self.replaced = self.replaced or replaced
        self.current.append(b"BT /%s %.1f Tf %.1f %.1f Td %s Tj ET" % (font.encode(), size, x, self.y, _literal(data)))

    def gap(self, points: float) -> None:
        self.y -= points

    def paragraph(self, text: str, size: float = 10.5, font: str = "F1", x: float = _LEFT, bullet: bool = False) -> None:
        for i, piece in enumerate(_wrapped(text, size, font, _RIGHT - x)):
            if bullet and i == 0:
                self.line(f"•  {piece}", size, font, x - 12)
            else:
                self.line(piece, size, font, x)

    def finish(self) -> List[bytes]:
        if self.current or not self.pages:
            self._break()
        return self.pages


def pdf_of(blocks: Sequence[Block]) -> bytes:
    pages = _Pages()
    for block in blocks:
        if block.kind == "title":
            pages.paragraph("Incident report", 9, "F1")
            pages.paragraph(block.text, 18, "F2")
            pages.gap(6)
        elif block.kind == "heading":
            pages.gap(12)
            pages.paragraph(block.text, 13, "F2")
            pages.gap(3)
        elif block.kind == "line":
            pages.paragraph(block.text)
        elif block.kind == "item":
            pages.paragraph(block.text, x=_LEFT + 12, bullet=True)
        elif block.kind == "table":
            for row in block.rows:
                pages.paragraph("  ·  ".join(row), 9)
        elif block.kind == "fixed":
            fits = int((_RIGHT - _LEFT) / _width("0", 7.5, "F3"))
            for fixed in block.lines:      # as written, spaces and all: a fingerprint is to compare
                for at in range(0, max(len(fixed), 1), fits):
                    pages.line(fixed[at:at + fits], 7.5, "F3")
        else:
            pages.gap(8)
    if pages.replaced:
        pages.gap(12)
        pages.paragraph(COULD_NOT_SHOW, 9)
    return _file(pages.finish())


def _file(contents: Sequence[bytes]) -> bytes:
    fonts = b"<< /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >> /F2 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >> /F3 << /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >> >>"
    objects: List[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids: List[bytes] = []
    for content in contents:
        page, stream = len(objects) + 1, len(objects) + 2
        kids.append(b"%d 0 R" % page)
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources << /Font %s >> /Contents %d 0 R >>" % (PAGE_WIDTH, PAGE_HEIGHT, fonts, stream))
        packed = zlib.compress(content)
        objects.append(b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream" % (len(packed), packed))
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), len(kids))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: List[int] = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)
    return bytes(out)
