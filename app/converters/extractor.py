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
import statistics
from itertools import pairwise

import pdfplumber

from app.converters import ocr as ocr_engine
from app.converters.models import ExtractedDocument, Table, TextLine

logger = logging.getLogger(__name__)

#: Words on the same line drift by a fraction of a point. Anything within this
#: many points of the same baseline is treated as one line.
LINE_TOLERANCE = 2.5

#: A glyph the PDF could not map back to a character. Fonts without a
#: ToUnicode table produce these, and bullet glyphs are the most common case
#: because they usually come from a symbol font.
CID_PATTERN = re.compile(r"\(cid:(\d+)\)")

#: The run of dots between a table of contents entry and its page number.
#: Shorter runs are left alone, since three dots is an ellipsis.
DOT_LEADER = re.compile(r"\s*(?:\.\s?){4,}\s*")

#: Runs of spaces, tabs and no-break spaces, which justified text produces.
MULTIPLE_SPACES = re.compile("[ \\t\\u00a0]{2,}")

#: A page with an image over at least this share of its area, and no more
#: than ``SCANNED_MAX_CHARS`` characters of text, counts as scanned.
SCANNED_IMAGE_COVERAGE = 0.5
SCANNED_MAX_CHARS = 200

#: Sideways text in at most this many lines is a stamp in the margin rather
#: than content, and is left out.
MAX_STAMP_COLUMNS = 4


class PdfExtractionError(Exception):
    """The file could not be opened or read as a PDF."""


def extract_document(
    data: bytes,
    *,
    max_pages: int | None = None,
    include_tables: bool = True,
    ocr: ocr_engine.OcrSettings | None = None,
) -> ExtractedDocument:
    """Read every page once, returning its lines, its tables and what was scanned.

    When tables are included, text inside a table is left out of the lines,
    so it reaches the output once, as the table, rather than twice.
    """

    lines: list[TextLine] = []
    tables: list[Table] = []
    heights: dict[int, float] = {}
    widths: dict[int, float] = {}
    page_chars: dict[int, int] = {}
    rotated: dict[int, int] = {}
    turned_pages: list[int] = []
    scanned: list[int] = []
    ocr_pages: list[int] = []

    can_ocr = ocr is not None and ocr_engine.unavailable_reason(ocr) is None

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            page_count = len(pdf.pages)
            pages = pdf.pages if max_pages is None else pdf.pages[:max_pages]
            metadata = _metadata(pdf.metadata)

            for page_number, page in enumerate(pages, start=1):
                heights[page_number] = float(page.height)
                widths[page_number] = float(page.width)
                page_chars[page_number] = sum(1 for char in page.chars if char["text"].isalnum())

                if looks_scanned(page):
                    scanned.append(page_number)

                    if can_ocr and len(ocr_pages) < ocr.max_pages:
                        ocr_pages.append(page_number)
                        lines.extend(_ocr_page(page, page_number, ocr))
                        continue

                page_tables = _tables_for_page(page, page_number) if include_tables else []

                page_lines, sideways = _lines_for_page(page, page_number, exclude=page_tables)

                tables.extend(page_tables)
                lines.extend(page_lines)

                if sideways > 0:
                    rotated[page_number] = sideways
                elif sideways < 0:
                    turned_pages.append(page_number)
    except PdfExtractionError:
        raise
    except Exception as error:  # pragma: no cover - depends on the file
        raise PdfExtractionError(f"Could not read the PDF: {error}") from error

    return ExtractedDocument(
        lines=lines,
        tables=tables,
        page_count=page_count,
        page_heights=heights,
        scanned_pages=scanned,
        ocr_pages=ocr_pages,
        page_widths=widths,
        page_chars=page_chars,
        rotated_chars=rotated,
        turned_pages=turned_pages,
        metadata=metadata,
    )


def _metadata(info: dict | None) -> dict[str, str]:
    """The text entries of the information dictionary, the rest being binary noise."""

    keep = ("Title", "Author", "Subject", "Creator", "Producer", "CreationDate", "ModDate")

    return {
        key: str(info[key]).strip()
        for key in keep
        if info and isinstance(info.get(key), str) and str(info[key]).strip()
    }


def extract_lines(data: bytes, max_pages: int | None = None) -> list[TextLine]:
    """Return every text line in reading order, with font metadata attached."""

    return extract_document(data, max_pages=max_pages, include_tables=False).lines


def count_pages(data: bytes) -> int:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return len(pdf.pages)
    except Exception as error:
        raise PdfExtractionError(f"Could not read the PDF: {error}") from error


def looks_scanned(page) -> bool:
    """Whether a page's content is an image, which is what a scanner produces.

    No text at all is the obvious case. The common one in practice is a page
    image with a thin text layer added afterwards, such as a digital signature
    stamp, so a page mostly covered by an image with only a line or two of
    text counts as well.
    """

    if not page.chars:
        return True

    if len(page.chars) > SCANNED_MAX_CHARS:
        return False

    page_area = float(page.width * page.height)

    if page_area <= 0:
        return False

    covered = 0.0

    for image in page.images:
        width = min(image["x1"], page.width) - max(image["x0"], 0)
        height = min(image["bottom"], page.height) - max(image["top"], 0)
        covered += max(width, 0) * max(height, 0)

    return covered >= page_area * SCANNED_IMAGE_COVERAGE


def _ocr_page(page, page_number: int, settings: ocr_engine.OcrSettings) -> list[TextLine]:
    try:
        return ocr_engine.read_page(page, page_number, settings)
    except Exception as error:  # pragma: no cover - depends on tesseract
        logger.warning("OCR failed on page %d, skipping it: %s", page_number, error)
        return []


def _tables_for_page(page, page_number: int) -> list[Table]:
    """The tables on a page, with their bounding boxes.

    Table detection is genuinely hard: a PDF has no table element, only lines
    and text that happen to line up. pdfplumber infers them from ruling lines
    and alignment, which works well for bordered tables and less well for ones
    held together by whitespace alone.
    """

    try:
        found = page.find_tables()
    except Exception as error:  # pragma: no cover - depends on the file
        logger.warning("Table detection failed on page %d, skipping it: %s", page_number, error)
        return []

    tables: list[Table] = []

    for table in found:
        extracted = table.extract()
        rows, kept, columns = clean_table_rows(extracted)

        if not rows:
            continue

        # extract() returns one row per detected row, in order, so the boxes
        # line up with it before any empty row or column is dropped.
        detected = table.rows
        aligned = len(detected) == len(extracted)
        row_boxes = [tuple(detected[index].bbox) for index in kept] if aligned else []
        row_cell_boxes = (
            [[_cell_box(detected[index].cells, column) for column in columns] for index in kept]
            if aligned
            else []
        )

        tables.append(
            Table(
                rows=rows,
                page=page_number,
                bbox=tuple(table.bbox),
                row_boxes=row_boxes,
                cell_boxes=[tuple(cell) for cell in table.cells],
                row_cell_boxes=row_cell_boxes,
            )
        )

    return tables


def _lines_for_page(
    page, page_number: int, exclude: list[Table] | None = None
) -> tuple[list[TextLine], int]:
    """Group a page's characters into lines, carrying font metadata along.

    Characters inside a cell of an ``exclude`` table belong to the table,
    which is rendered separately, so they are left out here.

    Returns the lines and how many letters and digits were left out because
    they were set sideways, or a negative count of sideways characters that
    were kept as content.
    """

    characters = page.chars

    if exclude:
        characters = [char for char in characters if not _in_a_table(char, exclude)]

    # Text set on its side is usually a stamp in the margin, the digital
    # signature notice a signing tool adds to every page. Grouped by height
    # like the rest, each of its letters became a line of its own, scattered
    # through the paragraphs beside it. A stamp is a strip, one to a few
    # sideways lines; a landscape annex turned on the page has dozens, and is
    # content: keeping it badly ordered beats losing it.
    turned = [char for char in characters if not char.get("upright", True)]
    sideways = 0

    if turned and len({round(char["x0"] / 3) for char in turned}) <= MAX_STAMP_COLUMNS:
        sideways = sum(1 for char in turned if char["text"].isalnum())
        characters = [char for char in characters if char.get("upright", True)]
    elif turned:
        # Kept, but grouped by height it reads a letter at a time. The caller
        # is told, so the page can be read from the PDF instead.
        sideways = -len(turned)

    if not characters:
        return [], sideways

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
                # The snapped value only groups characters. The position kept
                # is the real one, because paragraph detection compares the
                # gaps between lines and snapping adds up to a point of noise.
                top=statistics.median(
                    char["top"] for char in chars if round(char["size"], 1) == font_size
                ),
                page=page_number,
                x1=max(char["x1"] for char in chars),
                bottom=max(char["bottom"] for char in chars),
            )
        )

    return lines, sideways


def _in_a_table(char: dict, tables: list[Table]) -> bool:
    """Whether a character sits in a cell of one of the tables.

    The table's box is checked first, which settles most characters cheaply.
    Inside it, only a cell counts: a table drawn past the page edge encloses
    the page footer, which is not in any cell and would otherwise be lost.
    """

    return any(
        _inside_any(char, [table.bbox])
        and (not table.cell_boxes or _inside_any(char, table.cell_boxes))
        for table in tables
    )


def _inside_any(char: dict, boxes: list[tuple]) -> bool:
    x = (char["x0"] + char["x1"]) / 2
    y = (char["top"] + char["bottom"]) / 2

    return any(x0 <= x <= x1 and top <= y <= bottom for x0, top, x1, bottom in boxes)


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

        # Justified text widens the gap after a real space character too, so
        # a space is only implied when neither side already is one.
        if gap > threshold and not current["text"].isspace() and not previous["text"].isspace():
            parts.append(" ")

        parts.append(current["text"])

    text = normalize_cids("".join(parts))

    # A dot leader carries no information, and a table of contents can spend
    # a hundred characters on each entry drawing one.
    text = DOT_LEADER.sub(" … ", text)

    return MULTIPLE_SPACES.sub(" ", text).strip()


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
    return clean_table_rows(table)[0]


def _cell_box(cells: list, column: int) -> tuple[float, float, float, float] | None:
    """The box of one cell of a detected row; ``None`` where a merged cell left a gap."""

    cell = cells[column] if column < len(cells) else None

    return tuple(cell) if cell else None


def clean_table_rows(
    table: list[list[str | None]],
) -> tuple[list[list[str]], list[int], list[int]]:
    """Normalise a table so every row has the same width and no ``None``.

    Empty rows and columns are dropped: merged cells leave whole columns of
    ``None`` behind. What is left has to have at least two columns, because a
    single column is a framed paragraph or list rather than tabular data, and
    reads better as ordinary text.

    Returns the rows, the index in ``table`` of each row kept and the index of
    each column kept, so the caller can keep the position of every cell that
    survived.
    """

    if not table:
        return [], [], []

    width = max(len(row) for row in table)
    cleaned: list[list[str]] = []
    kept: list[int] = []

    for index, row in enumerate(table):
        normalised = [(cell or "").replace("\n", " ").strip() for cell in row]
        normalised.extend([""] * (width - len(normalised)))

        if any(normalised):
            cleaned.append(normalised)
            kept.append(index)

    # A table where every cell is empty is a false positive from ruling lines.
    if not cleaned:
        return [], [], []

    filled = [index for index in range(width) if any(row[index] for row in cleaned)]

    if len(filled) < 2:
        return [], [], []

    return [[row[index] for index in filled] for row in cleaned], kept, filled
