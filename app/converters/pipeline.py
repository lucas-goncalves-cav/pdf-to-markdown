"""The conversion pipeline, from PDF bytes to Markdown."""

from __future__ import annotations

import logging

from app.converters import extractor, renderer, structure
from app.converters.models import ConversionResult

logger = logging.getLogger(__name__)


def convert(
    data: bytes,
    *,
    include_tables: bool = True,
    max_pages: int | None = None,
) -> ConversionResult:
    """Convert a PDF to Markdown.

    Raises:
        extractor.PdfExtractionError: the file could not be read as a PDF.
    """

    page_count = extractor.count_pages(data)
    lines = extractor.extract_lines(data, max_pages=max_pages)

    warnings: list[str] = []

    if not lines:
        # An image only PDF, which is what a scan produces. Returning an empty
        # file with no explanation is the least helpful possible outcome, so
        # this is surfaced to the caller instead.
        warnings.append(
            "No selectable text was found. This PDF is most likely a scan, "
            "which needs OCR rather than text extraction."
        )

        return ConversionResult(
            markdown="",
            page_count=page_count,
            block_count=0,
            warnings=warnings,
        )

    tables = extractor.extract_tables(data, max_pages=max_pages) if include_tables else {}
    blocks = structure.to_blocks(lines, tables)
    markdown = renderer.render(blocks)

    if max_pages is not None and page_count > max_pages:
        warnings.append(
            f"Only the first {max_pages} of {page_count} pages were converted."
        )

    logger.info(
        "Converted %d pages into %d blocks (%d characters).",
        page_count,
        len(blocks),
        len(markdown),
    )

    return ConversionResult(
        markdown=markdown,
        page_count=page_count,
        block_count=len(blocks),
        warnings=warnings,
    )


def convert_to_text(data: bytes, *, max_pages: int | None = None) -> str:
    """Plain text, with the structure detection applied but no Markdown syntax."""

    lines = extractor.extract_lines(data, max_pages=max_pages)
    blocks = structure.to_blocks(lines, {})

    parts: list[str] = []

    for block in blocks:
        if block.rows:
            parts.extend("\t".join(row) for row in block.rows)
        elif block.text:
            parts.append(block.text)

    return "\n\n".join(parts).strip() + "\n"


def convert_to_json(data: bytes, *, max_pages: int | None = None) -> dict:
    """The block structure itself, for callers that want to do their own layout."""

    lines = extractor.extract_lines(data, max_pages=max_pages)
    tables = extractor.extract_tables(data, max_pages=max_pages)
    blocks = structure.to_blocks(lines, tables)

    return {
        "pageCount": extractor.count_pages(data),
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
