"""Pulls lines and tables out of a PDF, keeping the typographic detail.

The extraction step deliberately does not decide what anything *means*. It
reports what is on the page, with sizes and positions intact, and leaves
classification to :mod:`app.converters.structure`. Keeping the two apart is
what makes the heading heuristics testable without a PDF.
"""

from __future__ import annotations

import io
import logging
import re
from itertools import pairwise

import pdfplumber

from app.converters.models import TextLine

logger = logging.getLogger(__name__)

#: Words on the same line drift by a fraction of a point. Anything within this
#: many points of the same baseline is treated as one line.
LINE_TOLERANCE = 2.5

#: A glyph the PDF could not map back to a character. Fonts without a
#: ToUnicode table produce these, and bullet glyphs are the most common case
#: because they usually come from a symbol font.
CID_PATTERN = re.compile(r"\(cid:(\d+)\)")


class PdfExtractionError(Exception):
    """The file could not be opened or read as a PDF."""


def extract_lines(data: bytes, max_pages: int | None = None) -> list[TextLine]:
    """Return every text line in reading order, with font metadata attached."""

    lines: list[TextLine] = []

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = pdf.pages if max_pages is None else pdf.pages[:max_pages]

            for page_number, page in enumerate(pages, start=1):
                lines.extend(_lines_for_page(page, page_number))
    except PdfExtractionError:
        raise
    except Exception as error:  # pragma: no cover - depends on the file
        raise PdfExtractionError(f"Could not read the PDF: {error}") from error

    return lines


def extract_tables(data: bytes, max_pages: int | None = None) -> dict[int, list[list[list[str]]]]:
    """Return the tables found on each page, keyed by page number.

    Table detection is genuinely hard: a PDF has no table element, only lines
    and text that happen to line up. pdfplumber infers them from ruling lines
    and alignment, which works well for bordered tables and less well for ones
    held together by whitespace alone.
    """

    tables: dict[int, list[list[list[str]]]] = {}

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = pdf.pages if max_pages is None else pdf.pages[:max_pages]

            for page_number, page in enumerate(pages, start=1):
                found = page.extract_tables()

                if found:
                    tables[page_number] = [_clean_table(table) for table in found]
    except Exception as error:  # pragma: no cover - depends on the file
        logger.warning("Table extraction failed, continuing without tables: %s", error)

    return tables


def count_pages(data: bytes) -> int:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return len(pdf.pages)
    except Exception as error:
        raise PdfExtractionError(f"Could not read the PDF: {error}") from error


def _lines_for_page(page, page_number: int) -> list[TextLine]:
    """Group a page's characters into lines, carrying font metadata along."""

    characters = page.chars

    if not characters:
        return []

    # Characters arrive in drawing order, which is not always reading order.
    # Sorting by vertical then horizontal position restores it.
    groups: dict[float, list[dict]] = {}

    for char in characters:
        baseline = _snap(char["top"])
        groups.setdefault(baseline, []).append(char)

    lines: list[TextLine] = []

    for baseline in sorted(groups):
        chars = sorted(groups[baseline], key=lambda item: item["x0"])
        text = _join_characters(chars)

        if not text.strip():
            continue

        # The dominant size on the line, not the mean. A paragraph containing
        # one superscript should not be classified by the superscript.
        sizes = [round(char["size"], 1) for char in chars]
        font_size = max(set(sizes), key=sizes.count)

        bold_count = sum(1 for char in chars if _is_bold(char))

        lines.append(
            TextLine(
                text=text,
                font_size=font_size,
                # Most of the line has to be bold, so a single bold word inside
                # a sentence does not turn the sentence into a heading.
                is_bold=bold_count > len(chars) * 0.6,
                x0=min(char["x0"] for char in chars),
                top=baseline,
                page=page_number,
            )
        )

    return lines


def _snap(value: float) -> float:
    """Round a vertical position so characters on one line share a key."""

    return round(value / LINE_TOLERANCE) * LINE_TOLERANCE


def _join_characters(chars: list[dict]) -> str:
    """Rebuild a line, inserting spaces where the PDF only implied them.

    PDFs frequently position words without emitting space characters, so
    concatenating the glyphs gives ``thequickbrownfox``. A gap wider than a
    fraction of the character width means a space belongs there.
    """

    if not chars:
        return ""

    parts = [chars[0]["text"]]

    for previous, current in pairwise(chars):
        gap = current["x0"] - previous["x1"]
        threshold = max(previous.get("width", 0), current.get("width", 0)) * 0.3

        if gap > threshold and not current["text"].isspace():
            parts.append(" ")

        parts.append(current["text"])

    return normalize_cids("".join(parts)).strip()


def normalize_cids(text: str) -> str:
    """Replace unmappable glyph references with something usable.

    A leading one is almost always a list bullet, since bullets come from a
    symbol font that rarely carries a ToUnicode table. Anything else carries no
    information at all, so it is dropped rather than left as ``(cid:127)`` in
    the output.
    """

    if "(cid:" not in text:
        return text

    leading = CID_PATTERN.match(text.lstrip())

    if leading:
        remainder = CID_PATTERN.sub("", text.lstrip(), count=1).strip()

        if remainder:
            return f"• {CID_PATTERN.sub('', remainder).strip()}"

    return CID_PATTERN.sub("", text)


def _is_bold(char: dict) -> bool:
    font = str(char.get("fontname", "")).lower()

    return "bold" in font or "black" in font or "heavy" in font


def _clean_table(table: list[list[str | None]]) -> list[list[str]]:
    """Normalise a table so every row has the same width and no ``None``."""

    if not table:
        return []

    width = max(len(row) for row in table)
    cleaned: list[list[str]] = []

    for row in table:
        normalised = [(cell or "").replace("\n", " ").strip() for cell in row]
        normalised.extend([""] * (width - len(normalised)))
        cleaned.append(normalised)

    # A table where every cell is empty is a false positive from ruling lines.
    if all(not cell for row in cleaned for cell in row):
        return []

    return cleaned
