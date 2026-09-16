"""Test PDFs, generated rather than committed as binaries.

Building them here means a test can state exactly what went into the document,
so a failure says which heuristic broke instead of "the fixture changed".
"""

from __future__ import annotations

import io

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def build_pdf(pages: list[list[dict]]) -> bytes:
    """Render a PDF from a description of each page.

    Each element is a dict with ``text`` and optional ``size``, ``bold``,
    ``x`` and ``leading``.
    """

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    _width, height = A4

    for page in pages:
        cursor = height - 72

        for element in page:
            size = element.get("size", 11)
            font = "Helvetica-Bold" if element.get("bold") else "Helvetica"
            x = 72 + element.get("x", 0)
            leading = element.get("leading", size * 1.6)

            pdf.setFont(font, size)
            pdf.drawString(x, cursor, element["text"])
            cursor -= leading

        pdf.showPage()

    pdf.save()

    return buffer.getvalue()


@pytest.fixture
def simple_pdf() -> bytes:
    """A title, a paragraph and a subheading, in three distinct sizes."""

    return build_pdf(
        [
            [
                {"text": "Quarterly Report", "size": 24, "bold": True},
                {"text": "Overview", "size": 16, "bold": True},
                {"text": "Revenue grew by twelve percent over the quarter,", "size": 11},
                {"text": "driven mainly by the enterprise segment.", "size": 11},
                {"text": "Outlook", "size": 16, "bold": True},
                {"text": "The next quarter is expected to follow the same trend.", "size": 11},
            ]
        ]
    )


@pytest.fixture
def list_pdf() -> bytes:
    """Bulleted, numbered and nested list items."""

    return build_pdf(
        [
            [
                {"text": "Requirements", "size": 18, "bold": True},
                {"text": "• The service must accept PDF uploads", "size": 11},
                {"text": "• Conversion must preserve headings", "size": 11},
                {"text": "• Nested detail about headings", "size": 11, "x": 20},
                {"text": "1. Validate the extension", "size": 11},
                {"text": "2. Validate the MIME type", "size": 11},
                {"text": "3. Validate the size", "size": 11},
            ]
        ]
    )


@pytest.fixture
def multipage_pdf() -> bytes:
    """Three pages, each with its own heading."""

    return build_pdf(
        [
            [
                {"text": f"Chapter {number}", "size": 20, "bold": True},
                {"text": f"Body text belonging to chapter {number}.", "size": 11},
            ]
            for number in (1, 2, 3)
        ]
    )


@pytest.fixture
def wrapped_paragraph_pdf() -> bytes:
    """A paragraph split across visual lines, including a hyphenated word."""

    return build_pdf(
        [
            [
                {"text": "Notes", "size": 18, "bold": True},
                {"text": "This sentence continues onto the following line and", "size": 11},
                {"text": "should be rejoined into a single paragraph by the", "size": 11},
                {"text": "converter rather than left as three fragments.", "size": 11},
            ]
        ]
    )


@pytest.fixture
def markdown_characters_pdf() -> bytes:
    """Prose containing characters Markdown would otherwise interpret."""

    return build_pdf(
        [
            [
                {"text": "Pricing", "size": 18, "bold": True},
                {"text": "See clause [1] and the * marked items for details.", "size": 11},
                {"text": "The rate is 5_000 per unit, see table_1 below.", "size": 11},
            ]
        ]
    )


@pytest.fixture
def empty_pdf() -> bytes:
    """A valid PDF with no text at all, which is what a scan looks like."""

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.showPage()
    pdf.save()

    return buffer.getvalue()


@pytest.fixture
def not_a_pdf() -> bytes:
    return b"This is plain text, not a PDF at all."
