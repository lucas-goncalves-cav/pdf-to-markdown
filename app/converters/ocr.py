"""OCR for pages that carry a picture of text instead of text.

This is optional. It needs the ``tesseract`` binary and the ``pytesseract``
package, and without them a scanned page is reported rather than read.

OCR produces lines in the same shape as the text layer does, so the heading
and paragraph heuristics run on them unchanged. Two things are weaker:

- Font size is estimated. Tesseract's ``x_size`` for a line is used rather
  than the height of its bounding box, because a box measures the letters
  that happen to be in it: a line in capitals has no descenders and comes out
  about a quarter smaller than body text set at the same size.
- Weight is unknown, so nothing on an OCR page is ever bold.
"""

from __future__ import annotations

import functools
import logging
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass

from app.converters.models import TextLine

logger = logging.getLogger(__name__)

#: hOCR classes tesseract uses for a single line of text.
_LINE_CLASSES = {"ocr_line", "ocr_header", "ocr_caption", "ocr_textfloat"}

#: A line whose words average below this confidence is usually a logo, a
#: stamp, or the ruling of a table that tesseract tried to read as text.
#: Prose comes back in the 80s and 90s; ruled table cells in the 30s and 40s.
MIN_LINE_CONFIDENCE = 50

_NUMBER = r"-?\d+(?:\.\d+)?"


@dataclass(frozen=True)
class OcrSettings:
    #: Tesseract language codes joined with ``+``, such as ``por+eng``.
    languages: str = "eng"
    #: OCR costs a second or more per page, so scanned pages beyond this are
    #: skipped and reported.
    max_pages: int = 30
    #: Render resolution. Tesseract reads best when body text is around 20
    #: to 30 pixels high, which is roughly 300 dpi for 10 to 12 point text.
    resolution: int = 300
    #: Seconds before tesseract is abandoned on a single page.
    timeout: int = 120


def unavailable_reason(settings: OcrSettings) -> str | None:
    """Why OCR cannot run with these settings, or ``None`` when it can."""

    return _check(settings.languages)


@functools.lru_cache(maxsize=8)
def _check(languages: str) -> str | None:
    try:
        import pytesseract
    except ImportError:
        return "the pytesseract package is not installed"

    try:
        installed = set(pytesseract.get_languages(config=""))
    except Exception as error:
        return f"tesseract could not be run ({error})"

    missing = [code for code in languages.split("+") if code and code not in installed]

    if missing:
        return f"tesseract has no language data for {', '.join(missing)}"

    return None


def read_page(page, page_number: int, settings: OcrSettings) -> list[TextLine]:
    """Render a pdfplumber page and return its text as lines, in points."""

    import pytesseract

    image = page.to_image(resolution=settings.resolution).original
    hocr = pytesseract.image_to_pdf_or_hocr(
        image,
        lang=settings.languages,
        extension="hocr",
        timeout=settings.timeout,
    )

    return parse_hocr(hocr, page_number=page_number, scale=72 / settings.resolution)


def parse_hocr(hocr: bytes | str, *, page_number: int, scale: float) -> list[TextLine]:
    """Turn tesseract's hOCR into lines, converting pixels to points.

    Kept separate from :func:`read_page` so it can be tested without running
    tesseract.
    """

    root = ElementTree.fromstring(hocr)
    lines: list[TextLine] = []

    for element in root.iter():
        if element.get("class") not in _LINE_CLASSES:
            continue

        line = _line_from_element(element, page_number=page_number, scale=scale)

        if line is not None:
            lines.append(line)

    return lines


def _line_from_element(element, *, page_number: int, scale: float) -> TextLine | None:
    words: list[str] = []
    confidences: list[float] = []

    for child in element.iter():
        if child.get("class") != "ocrx_word":
            continue

        word = "".join(child.itertext()).strip()

        if word:
            words.append(word)
            confidences.append(_number(child.get("title", ""), "x_wconf", default=100))

    text = " ".join(words)

    # Pure punctuation is what tesseract makes of rules, borders and specks.
    if not any(character.isalnum() for character in text):
        return None

    if sum(confidences) / len(confidences) < MIN_LINE_CONFIDENCE:
        return None

    title = element.get("title", "")
    box = re.search(rf"bbox ({_NUMBER}) ({_NUMBER}) ({_NUMBER}) ({_NUMBER})", title)

    if box is None:
        return None

    x0, y0, _x1, y1 = (float(value) for value in box.groups())
    height = y1 - y0

    size = _number(title, "x_size", default=height)
    descenders = _number(title, "x_descenders", default=0)
    baseline = re.search(rf"baseline {_NUMBER} ({_NUMBER})", title)

    # When the line model fails, typically on text boxed in by table rules,
    # x_size comes back several times taller than the line itself.
    if size > height * 2:
        size, descenders, baseline = height, 0.0, None

    # The top of the line is placed where the text layer would put it, the
    # ascent above the baseline, rather than at the top of the box, which
    # moves with the letters in the line and would make line gaps unreliable.
    top = y0

    if baseline is not None:
        top = (y1 + float(baseline.group(1))) - (size - descenders)

    return TextLine(
        text=text,
        # Whole points: the estimate wobbles by a few tenths between lines of
        # the same size, and finer steps would read that wobble as structure.
        font_size=float(round(size * scale)),
        is_bold=False,
        x0=x0 * scale,
        top=max(top, 0.0) * scale,
        page=page_number,
    )


def _number(title: str, key: str, *, default: float) -> float:
    match = re.search(rf"{key} ({_NUMBER})", title)

    return float(match.group(1)) if match else float(default)
