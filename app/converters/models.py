"""Shared types for the extraction and conversion pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BlockKind(StrEnum):
    """What a run of text on the page turned out to be."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    CODE = "code"


@dataclass
class TextLine:
    """One line of text with the typographic detail needed to classify it.

    Font size and weight are what make heading detection possible. A PDF has
    no notion of a heading, only of text that happens to be larger and bolder
    than its neighbours.
    """

    text: str
    font_size: float
    is_bold: bool
    #: Distance from the left edge, used to detect nesting in lists.
    x0: float
    #: Distance from the top of the page, in points. It keeps reading order,
    #: and the gap between two lines is what separates paragraphs.
    top: float
    page: int
    #: Right edge and bottom of the line, in points. With ``x0`` and ``top``
    #: they are the box a piece of evidence points back to. Zero when unknown.
    x1: float = 0.0
    bottom: float = 0.0
    #: ``text_layer`` for text the PDF carries, ``ocr`` for text read from a
    #: picture of the page.
    source: str = "text_layer"
    #: Mean word confidence, 0 to 100, for a line read with OCR.
    confidence: float | None = None

    @property
    def is_blank(self) -> bool:
        return not self.text.strip()

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.top, self.x1, self.bottom)


@dataclass
class Table:
    """A table found on a page, with its position so it lands in reading order."""

    rows: list[list[str]]
    page: int
    #: ``(x0, top, x1, bottom)`` in points, in the same coordinates as
    #: :class:`TextLine`.
    bbox: tuple[float, float, float, float]
    #: The box of each row in ``rows``, so a fact read from one row can point
    #: at that row on the page rather than at the whole table.
    row_boxes: list[tuple[float, float, float, float]] = field(default_factory=list)

    @property
    def top(self) -> float:
        return self.bbox[1]


@dataclass
class ExtractedDocument:
    """Everything extraction found, before any of it is classified."""

    lines: list[TextLine]
    tables: list[Table]
    page_count: int
    #: Height of each extracted page in points. Running headers and footers
    #: are recognised partly by sitting near the top or bottom edge.
    page_heights: dict[int, float] = field(default_factory=dict)
    #: Pages whose content is an image rather than text, which is what a
    #: scanner produces.
    scanned_pages: list[int] = field(default_factory=list)
    #: The scanned pages that OCR was run on.
    ocr_pages: list[int] = field(default_factory=list)
    #: Width of each extracted page in points.
    page_widths: dict[int, float] = field(default_factory=dict)
    #: Letters and digits in each page's text layer, everything extraction
    #: could have read, to measure how much of it reached the output. Only
    #: letters and digits, because dot leaders and unmappable glyphs are
    #: dropped on purpose.
    page_chars: dict[int, int] = field(default_factory=dict)
    #: Letters and digits set sideways outside any table, per page. They are
    #: left out of the lines; see ``extractor._lines_for_page``.
    rotated_chars: dict[int, int] = field(default_factory=dict)
    #: Pages with sideways content kept in the lines, a landscape annex turned
    #: on the page, which comes out in the wrong order.
    turned_pages: list[int] = field(default_factory=list)
    #: The document information dictionary: title, producer, dates.
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class Block:
    """A converted chunk of the document, ready to be rendered as Markdown."""

    kind: BlockKind
    text: str
    #: Heading level, 1 to 6. Only set when ``kind`` is ``HEADING``.
    level: int | None = None
    #: Nesting depth for list items, starting at 0.
    indent: int = 0
    #: Rows for a table block, including the header row.
    rows: list[list[str]] = field(default_factory=list)
    page: int = 1
    #: The lines the block was built from, in order. They carry the page and
    #: the box of every piece of the text, which Markdown throws away.
    lines: list[TextLine] = field(default_factory=list)
    #: For a table block, the table on each page it spans: one, or several
    #: when a page break split it.
    tables: list[Table] = field(default_factory=list)
    #: For a table block, the page and box of each row in ``rows``. The box is
    #: ``None`` when the table detector did not report one.
    row_refs: list[tuple[int, tuple[float, float, float, float] | None]] = field(
        default_factory=list
    )


@dataclass
class ConversionResult:
    """The converted document plus what the converter noticed while doing it."""

    markdown: str
    page_count: int
    block_count: int
    #: Present when the PDF yielded no text at all, which usually means it is
    #: a scan. The caller should surface this rather than return an empty file.
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.markdown.strip()
