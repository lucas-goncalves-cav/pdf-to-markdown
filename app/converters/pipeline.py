"""The conversion pipeline, from PDF bytes to Markdown."""

from __future__ import annotations

import logging

from app.converters import extractor, furniture, renderer, structure
from app.converters.models import Block, ConversionResult, ExtractedDocument
from app.converters.ocr import OcrSettings

logger = logging.getLogger(__name__)

#: The answer for a PDF with no text and no OCR to read it, kept word for word
#: because callers match on it.
NO_TEXT_WARNING = (
    "No selectable text was found. This PDF is most likely a scan, "
    "which needs OCR rather than text extraction."
)


def convert(
    data: bytes,
    *,
    include_tables: bool = True,
    max_pages: int | None = None,
    strip_headers: bool = True,
    ocr: OcrSettings | None = None,
) -> ConversionResult:
    """Convert a PDF to Markdown.

    Raises:
        extractor.PdfExtractionError: the file could not be read as a PDF.
    """

    document, blocks = _extract_blocks(
        data,
        include_tables=include_tables,
        max_pages=max_pages,
        strip_headers=strip_headers,
        ocr=ocr,
    )

    warnings = scan_warnings(document, ocr_requested=ocr is not None)

    if not blocks:
        return ConversionResult(
            markdown="",
            page_count=document.page_count,
            block_count=0,
            warnings=warnings,
        )

    markdown = renderer.render(blocks)

    if max_pages is not None and document.page_count > max_pages:
        warnings.append(
            f"Only the first {max_pages} of {document.page_count} pages were converted."
        )

    logger.info(
        "Converted %d pages into %d blocks (%d characters).",
        document.page_count,
        len(blocks),
        len(markdown),
    )

    return ConversionResult(
        markdown=markdown,
        page_count=document.page_count,
        block_count=len(blocks),
        warnings=warnings,
    )


def convert_to_text(
    data: bytes,
    *,
    max_pages: int | None = None,
    strip_headers: bool = True,
    ocr: OcrSettings | None = None,
) -> str:
    """Plain text, with the structure detection applied but no Markdown syntax."""

    _document, blocks = _extract_blocks(
        data,
        include_tables=True,
        max_pages=max_pages,
        strip_headers=strip_headers,
        ocr=ocr,
    )

    parts: list[str] = []

    for block in blocks:
        if block.rows:
            parts.append("\n".join("\t".join(row) for row in block.rows))
        elif block.text:
            parts.append(block.text)

    return "\n\n".join(parts).strip() + "\n"


def convert_to_json(
    data: bytes,
    *,
    max_pages: int | None = None,
    strip_headers: bool = True,
    ocr: OcrSettings | None = None,
) -> dict:
    """The block structure itself, for callers that want to do their own layout."""

    document, blocks = _extract_blocks(
        data,
        include_tables=True,
        max_pages=max_pages,
        strip_headers=strip_headers,
        ocr=ocr,
    )

    return {
        "pageCount": document.page_count,
        "warnings": scan_warnings(document, ocr_requested=ocr is not None),
        "blocks": [
            {
                "kind": block.kind.value,
                "text": block.text,
                "level": block.level,
                "indent": block.indent,
                "rows": block.rows,
                "page": block.page,
            }
            for block in blocks
        ],
    }


def scan_warnings(document: ExtractedDocument, *, ocr_requested: bool) -> list[str]:
    """What the caller should know about pages that had no text layer.

    Returning an empty file with no explanation is the least helpful possible
    outcome, and so is quietly dropping the scanned annex of a document that
    is otherwise text.
    """

    if not (document.lines or document.tables):
        if document.ocr_pages:
            return [
                "No text was found, even with OCR. This PDF is most likely a scan "
                "of blank or unreadable pages."
            ]

        return [NO_TEXT_WARNING]

    # A scanned page can still carry a line or two of real text, such as a
    # signature stamp, so having lines does not mean the page was read.
    recovered = sorted({line.page for line in document.lines} & set(document.ocr_pages))
    unread = [page for page in document.scanned_pages if page not in recovered]
    skipped = [page for page in unread if page not in document.ocr_pages]

    warnings: list[str] = []

    if recovered:
        warnings.append(
            f"Read {_pages(recovered)} with OCR because there was no text layer. "
            "OCR text can contain recognition errors, and its headings are approximate."
        )

    if unread:
        if not skipped:
            reason = "OCR found no text"
        elif document.ocr_pages:
            reason = f"OCR stops after {len(document.ocr_pages)} scanned pages"
        elif ocr_requested:
            reason = "they need OCR, which is not available on this server"
        else:
            reason = "they need OCR, which was not used"

        warnings.append(f"Skipped scanned {_pages(unread)}: {reason}.")

    return warnings


def _extract_blocks(
    data: bytes,
    *,
    include_tables: bool,
    max_pages: int | None,
    strip_headers: bool,
    ocr: OcrSettings | None,
) -> tuple[ExtractedDocument, list[Block]]:
    document = extractor.extract_document(
        data,
        max_pages=max_pages,
        include_tables=include_tables,
        ocr=ocr,
    )

    lines = document.lines

    if strip_headers:
        lines, removed = furniture.strip_running_text(lines, document.page_heights)

        if removed:
            logger.info("Removed %d lines of running headers and footers.", removed)

    return document, structure.to_blocks(lines, document.tables)


def _pages(numbers: list[int]) -> str:
    """``[3, 4, 5, 9]`` as ``"pages 3-5, 9"``."""

    ranges: list[str] = []
    start = previous = numbers[0]

    for number in [*numbers[1:], None]:
        if number is not None and number == previous + 1:
            previous = number
            continue

        ranges.append(str(start) if start == previous else f"{start}-{previous}")

        if number is not None:
            start = previous = number

    noun = "page" if len(numbers) == 1 else "pages"

    return f"{noun} {', '.join(ranges)}"
