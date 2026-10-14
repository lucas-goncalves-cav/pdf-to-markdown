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

    @property
    def is_blank(self) -> bool:
        return not self.text.strip()


@dataclass
class Table:
    """A table found on a page, with its position so it lands in reading order."""

    rows: list[list[str]]
    page: int
    #: ``(x0, top, x1, bottom)`` in points, in the same coordinates as
    #: :class:`TextLine`.
    bbox: tuple[float, float, float, float]

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
