"""Builds a NormalizedDocument v1 from what the pipeline extracted.

Three properties matter more than anything else here:

- Every block, table row and section can be traced to a page and a box on it,
  so a fact taken from the document can be checked against the original PDF.
- The same PDF converted the same way gives the same ``document_id`` and the
  same ``normalized_content_hash``, which is what lets a caller convert a
  document once and reuse the result.
- What could not be read is said, page by page, rather than left out.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.converters import pipeline
from app.converters.models import Block, BlockKind, ExtractedDocument, TextLine
from app.converters.ocr import OcrSettings
from app.converters.ocr import unavailable_reason as ocr_unavailable_reason
from app.converters.structure import join_wrapped

SCHEMA = "normalized-document"
SCHEMA_VERSION = "1.0.0"
CONVERTER_NAME = "pdf-to-markdown"

#: Part of ``document_id``. Bump it whenever a change to extraction or
#: classification can change the output for the same PDF, so a caller that
#: cached the old result knows it is stale.
CONVERTER_VERSION = "1.1.0"

#: Below this share of a page's letters and digits reaching the output,
#: something on the page was not read, usually the inside of a table.
MIN_PAGE_COVERAGE = 0.98

#: Above this share, text was emitted twice.
MAX_PAGE_COVERAGE = 1.02

#: Section numbering at the start of a heading: "3", "3.2.1", "ANEXO II".
_NUMBERING = re.compile(
    r"^\s*(?P<numbering>(?:ANEXO|AP[ÊE]NDICE|CAP[ÍI]TULO|T[ÍI]TULO)\s+[IVXLCDM\d]+"
    r"|\d{1,3}(?:\.\d{1,3})*)(?=[\s.)\-–—:]|$)",
    re.IGNORECASE,
)

_BLOCK_TYPES = {
    BlockKind.HEADING: "heading",
    BlockKind.PARAGRAPH: "paragraph",
    BlockKind.LIST_ITEM: "list_item",
    BlockKind.TABLE: "table",
    BlockKind.CODE: "paragraph",
}


def normalize(
    data: bytes,
    *,
    source_uri: str | None = None,
    filename: str | None = None,
    include_tables: bool = True,
    strip_headers: bool = True,
    max_pages: int | None = None,
    ocr: OcrSettings | None = None,
    generated_at: datetime | None = None,
) -> dict:
    """Convert a PDF into a NormalizedDocument v1, as a JSON ready ``dict``.

    Raises:
        extractor.PdfExtractionError: the file could not be read as a PDF.
    """

    extraction = pipeline.extract(
        data,
        include_tables=include_tables,
        max_pages=max_pages,
        strip_headers=strip_headers,
        ocr=ocr,
    )

    options = {
        "include_tables": include_tables,
        "strip_headers": strip_headers,
        "max_pages": max_pages,
        "ocr": {
            "requested": ocr is not None,
            # Whether OCR could run changes what comes out of a scanned page.
            "available": ocr is not None and ocr_unavailable_reason(ocr) is None,
            "languages": ocr.languages if ocr is not None else None,
        },
    }

    return build(
        data,
        extraction,
        options=options,
        source_uri=source_uri,
        filename=filename,
        generated_at=generated_at,
    )


def build(
    data: bytes,
    extraction: pipeline.Extraction,
    *,
    options: dict,
    source_uri: str | None = None,
    filename: str | None = None,
    generated_at: datetime | None = None,
) -> dict:
    """Assemble the document from an extraction already made."""

    document = extraction.document
    source_hash = _sha256(data)
    converter = {"name": CONVERTER_NAME, "version": CONVERTER_VERSION, "options": options}
    effective = _canonical(_effective_options(options, document))
    document_id = "nd_" + _sha256(
        f"{source_hash}|{CONVERTER_NAME}/{CONVERTER_VERSION}|{effective}".encode()
    )[:32]

    structure = _Structure.of(extraction.blocks)
    pages = _pages(extraction)
    findings = _findings(extraction, pages, ocr_requested=options["ocr"]["requested"])

    content = {
        "pages": pages,
        "blocks": structure.blocks,
        "tables": structure.tables,
        "sections": structure.sections,
    }

    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "document_id": document_id,
        "normalized_content_hash": "sha256:" + _sha256(_canonical(content).encode()),
        "generated_at": (generated_at or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "coordinates": {"unit": "pt", "origin": "top-left", "bbox": "x0,top,x1,bottom"},
        "source": {
            "content_hash": f"sha256:{source_hash}",
            "size_bytes": len(data),
            "media_type": "application/pdf",
            "filename": filename,
            "uri": source_uri,
            "page_count": document.page_count,
            "metadata": document.metadata,
        },
        "converter": converter,
        "quality": _quality(extraction, pages, structure, findings),
        "warnings": [
            {"code": finding.code, "message": finding.message, "pages": list(finding.pages)}
            for finding in findings
        ],
        **content,
    }


def _effective_options(options: dict, document: ExtractedDocument) -> dict:
    """The options that changed what came out, which are the ones that identify it.

    A page limit the document did not reach, or OCR on a document with no
    scanned page, changed nothing. Leaving them in would give one conversion
    a different id behind the API, with its default limit and OCR on, than
    from the command line.
    """

    effective = dict(options)

    if len(document.page_heights) == document.page_count:
        effective["max_pages"] = None

    if not document.scanned_pages:
        effective["ocr"] = None

    return effective


@dataclass
class _Section:
    payload: dict
    level: int


@dataclass
class _Structure:
    """Blocks, tables and sections, numbered in reading order."""

    blocks: list[dict] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)

    @classmethod
    def of(cls, blocks: list[Block]) -> _Structure:
        result = cls()
        open_sections: list[_Section] = []

        for index, block in enumerate(blocks, start=1):
            block_id = f"b{index:05d}"
            spans, text = _spans(block)
            pages = [span["page"] for span in spans] or [block.page]

            if block.kind is BlockKind.HEADING:
                level = block.level or 1

                while open_sections and open_sections[-1].level >= level:
                    open_sections.pop()

                numbering = _NUMBERING.match(text)
                section = {
                    "id": f"s{len(result.sections) + 1:04d}",
                    "title": text,
                    "numbering": numbering.group("numbering").upper() if numbering else None,
                    "level": level,
                    "depth": len(open_sections) + 1,
                    "parent_id": open_sections[-1].payload["id"] if open_sections else None,
                    "heading_block_id": block_id,
                    "first_block_id": block_id,
                    "last_block_id": block_id,
                    "page_start": min(pages),
                    "page_end": max(pages),
                }
                result.sections.append(section)
                open_sections.append(_Section(section, level))

            # A block belongs to every section still open, so each of them now
            # reaches at least this far.
            for open_section in open_sections:
                open_section.payload["last_block_id"] = block_id
                open_section.payload["page_end"] = max(open_section.payload["page_end"], *pages)

            payload = {
                "id": block_id,
                "type": _BLOCK_TYPES[block.kind],
                "text": text,
                "section_id": open_sections[-1].payload["id"] if open_sections else None,
                "page_start": min(pages),
                "page_end": max(pages),
                "source": _source(block),
                "spans": spans,
            }

            if block.kind is BlockKind.HEADING:
                payload["level"] = block.level or 1

            if block.kind is BlockKind.LIST_ITEM:
                payload["indent"] = block.indent

            if block.kind is BlockKind.TABLE:
                table_id = f"t{len(result.tables) + 1:04d}"
                payload["table_id"] = table_id
                result.tables.append(_table(block, table_id, block_id, payload["section_id"]))

            result.blocks.append(payload)

        return result


def _spans(block: Block) -> tuple[list[dict], str]:
    """The block's text and where each part of it sits on the page."""

    if block.kind is BlockKind.TABLE:
        return [{"page": table.page, "bbox": _box(table.bbox)} for table in block.tables], ""

    if not block.lines:
        return [], block.text

    text, ranges = join_wrapped(block.lines)

    if text == block.text:
        return [
            {"page": line.page, "bbox": _line_box(line), "start": start, "end": end}
            for line, (start, end) in zip(block.lines, ranges, strict=True)
        ], text

    # Not expected, but should the block text ever be built some other way,
    # the whole of it still points at every line it came from.
    return [
        {"page": line.page, "bbox": _line_box(line), "start": 0, "end": len(block.text)}
        for line in block.lines
    ], block.text


def _table(block: Block, table_id: str, block_id: str, section_id: str | None) -> dict:
    pages = [ref.page for ref in block.row_refs] or [block.page]

    return {
        "id": table_id,
        "block_id": block_id,
        "section_id": section_id,
        "page_start": min(pages),
        "page_end": max(pages),
        "column_count": len(block.rows[0]) if block.rows else 0,
        "row_count": len(block.rows),
        "rows": [
            {
                "cells": row,
                "page": ref.page,
                "bbox": _box(ref.box) if ref.box else None,
                "cell_boxes": (
                    [_box(cell) if cell else None for cell in ref.cells]
                    if len(ref.cells) == len(row)
                    else [None] * len(row)
                ),
            }
            for row, ref in zip(block.rows, block.row_refs, strict=True)
        ],
    }


def _pages(extraction: pipeline.Extraction) -> list[dict]:
    document = extraction.document
    emitted: dict[int, int] = {}
    ocr_confidences: dict[int, list[float]] = {}

    # Furniture is in document.lines too: it was read, then dropped on purpose.
    for line in document.lines:
        emitted[line.page] = emitted.get(line.page, 0) + _alnum(line.text)

        if line.confidence is not None:
            ocr_confidences.setdefault(line.page, []).append(line.confidence)

    for table in document.tables:
        emitted[table.page] = emitted.get(table.page, 0) + sum(
            _alnum(cell) for row in table.rows for cell in row
        )

    read_by_ocr = {line.page for line in document.lines if line.source == "ocr"}
    pages: list[dict] = []

    for number in sorted(document.page_heights):
        if number in read_by_ocr:
            text_source = "ocr"
        elif number in document.scanned_pages:
            text_source = "none"
        else:
            text_source = "text_layer"

        available = document.page_chars.get(number, 0)
        sideways = document.rotated_chars.get(number, 0)
        confidences = ocr_confidences.get(number)

        pages.append(
            {
                "number": number,
                "width": round(document.page_widths.get(number, 0.0), 2),
                "height": round(document.page_heights[number], 2),
                "text_source": text_source,
                "chars_available": available,
                "chars_emitted": emitted.get(number, 0),
                "chars_sideways": sideways,
                # Measured against the text layer, so it means nothing on a
                # page read with OCR or not read at all. Sideways text was set
                # aside on purpose and is reported on its own.
                "coverage": (
                    round((emitted.get(number, 0) + sideways) / available, 4)
                    if text_source == "text_layer" and available
                    else None
                ),
                "ocr_confidence": (
                    round(sum(confidences) / len(confidences), 1) if confidences else None
                ),
            }
        )

    return pages


def _findings(
    extraction: pipeline.Extraction, pages: list[dict], *, ocr_requested: bool
) -> list[pipeline.Finding]:
    document = extraction.document
    findings = pipeline.scan_findings(document, ocr_requested=ocr_requested)

    converted = len(document.page_heights)

    if converted < document.page_count:
        findings.append(
            pipeline.Finding(
                "TRUNCATED",
                f"Only the first {converted} of {document.page_count} pages were converted.",
                tuple(range(converted + 1, document.page_count + 1)),
            )
        )

    sideways = [page["number"] for page in pages if page["chars_sideways"]]

    if sideways:
        findings.append(
            pipeline.Finding(
                "SIDEWAYS_TEXT_SKIPPED",
                f"Text set sideways on {_page_list(sideways)} was left out, usually a "
                "signature stamp in the margin.",
                tuple(sideways),
            )
        )

    if document.turned_pages:
        findings.append(
            pipeline.Finding(
                "SIDEWAYS_CONTENT",
                f"{_page_list(document.turned_pages).capitalize()} carry content turned on its "
                "side, kept but out of reading order; read them from the PDF.",
                tuple(document.turned_pages),
            )
        )

    measured = [page for page in pages if page["coverage"] is not None]
    low = [page["number"] for page in measured if page["coverage"] < MIN_PAGE_COVERAGE]
    high = [page["number"] for page in measured if page["coverage"] > MAX_PAGE_COVERAGE]

    if low:
        findings.append(
            pipeline.Finding(
                "LOW_TEXT_COVERAGE",
                f"Some text on {_page_list(low)} did not reach the output; check those pages "
                "against the PDF before relying on them.",
                tuple(low),
            )
        )

    if high:
        findings.append(
            pipeline.Finding(
                "TEXT_DUPLICATED",
                f"Some text on {_page_list(high)} reached the output more than once.",
                tuple(high),
            )
        )

    return findings


def _quality(
    extraction: pipeline.Extraction,
    pages: list[dict],
    structure: _Structure,
    findings: list[pipeline.Finding],
) -> dict:
    text_pages = [page for page in pages if page["coverage"] is not None]
    available = sum(page["chars_available"] for page in text_pages)
    emitted = sum(page["chars_emitted"] + page["chars_sideways"] for page in text_pages)
    confidences = [page["ocr_confidence"] for page in pages if page["ocr_confidence"]]

    # Pages whose text should not be trusted as extracted. A consumer that
    # needs them falls back to the PDF itself for those pages only.
    suspect = {
        page
        for finding in findings
        if finding.code
        in {"SCANNED_PAGES_SKIPPED", "NO_TEXT", "LOW_TEXT_COVERAGE", "SIDEWAYS_CONTENT"}
        for page in finding.pages
    }

    if not structure.blocks:
        status = "unreadable"
    elif suspect or any(finding.code in {"OCR_USED", "TRUNCATED"} for finding in findings):
        status = "degraded"
    else:
        status = "ok"

    return {
        "status": status,
        "pages_total": extraction.document.page_count,
        "pages_converted": len(pages),
        "pages_text_layer": sum(1 for page in pages if page["text_source"] == "text_layer"),
        "pages_ocr": sum(1 for page in pages if page["text_source"] == "ocr"),
        "pages_unread": sum(1 for page in pages if page["text_source"] == "none"),
        "text_coverage": round(emitted / available, 4) if available else None,
        "min_page_coverage": min((page["coverage"] for page in text_pages), default=None),
        "ocr_mean_confidence": (
            round(sum(confidences) / len(confidences), 1) if confidences else None
        ),
        "fallback_pages": sorted(suspect),
        "furniture_lines_removed": len(extraction.furniture),
        "block_count": len(structure.blocks),
        "table_count": len(structure.tables),
        "section_count": len(structure.sections),
    }


def _source(block: Block) -> str:
    return "ocr" if any(line.source == "ocr" for line in block.lines) else "text_layer"


def _line_box(line: TextLine) -> list[float] | None:
    if not (line.x1 or line.bottom):
        return None

    return _box(line.bbox)


def _box(box: tuple[float, float, float, float]) -> list[float]:
    return [round(value, 2) for value in box]


def _alnum(text: str) -> int:
    return sum(1 for character in text if character.isalnum())


def _page_list(numbers: list[int]) -> str:
    return ("page " if len(numbers) == 1 else "pages ") + ", ".join(map(str, numbers))


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
