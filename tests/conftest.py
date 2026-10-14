"""Test PDFs, generated rather than committed as binaries.

Building them here means a test can state exactly what went into the document,
so a failure says which heuristic broke instead of "the fixture changed".
"""

from __future__ import annotations

import io

import pdfplumber
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

TABLE_ROW_HEIGHT = 18


def build_pdf(
    pages: list[list[dict]],
    *,
    header: str | None = None,
    footer: str | None = None,
) -> bytes:
    """Render a PDF from a description of each page.

    Each element is a dict with ``text`` and optional ``size``, ``bold``,
    ``x`` and ``leading``, or with ``table``, a list of rows drawn with
    ruling lines the way a word processor draws a bordered table.

    ``header`` and ``footer`` are drawn in the margins of every page, with
    ``{page}`` and ``{pages}`` filled in.
    """

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    for number, page in enumerate(pages, start=1):
        for template, y in ((header, height - 40), (footer, 30)):
            if template:
                pdf.setFont("Helvetica", 9)
                pdf.drawString(72, y, template.format(page=number, pages=len(pages)))

        cursor = height - 72

        for element in page:
            if "table" in element:
                cursor = _draw_table(pdf, element["table"], top=cursor, width=width - 144)
                continue

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


def _draw_table(pdf: canvas.Canvas, rows: list[list[str]], *, top: float, width: float) -> float:
    """Draw a bordered table whose first baseline sits at ``top``. Returns the next cursor."""

    columns = len(rows[0])
    xs = [72 + width * index / columns for index in range(columns + 1)]
    upper = top + 12
    ys = [upper - TABLE_ROW_HEIGHT * index for index in range(len(rows) + 1)]

    pdf.setLineWidth(0.5)
    pdf.grid(xs, ys)
    pdf.setFont("Helvetica", 10)

    for row_index, row in enumerate(rows):
        for column, cell in enumerate(row):
            pdf.drawString(xs[column] + 4, ys[row_index] - 12, cell)

    return ys[-1] - 24


def rasterise(data: bytes, *, resolution: int = 200) -> bytes:
    """Turn a PDF into pictures of its pages, which is what a scanner produces."""

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)

    with pdfplumber.open(io.BytesIO(data)) as source:
        for page in source.pages:
            image = page.to_image(resolution=resolution).original.convert("L")
            pdf.drawImage(ImageReader(image), 0, 0, width=A4[0], height=A4[1])
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


@pytest.fixture
def table_pdf() -> bytes:
    """A bordered table between two paragraphs."""

    return build_pdf(
        [
            [
                {"text": "Vacancies", "size": 18, "bold": True},
                {"text": "The positions on offer are listed below.", "size": 11, "leading": 30},
                {
                    "table": [
                        ["Position", "Hours", "Salary"],
                        ["Teacher", "20 hours", "R$ 2.565,32"],
                        ["Nurse", "40 hours", "R$ 4.750,00"],
                    ]
                },
                {"text": "Applications open on the date in the timetable.", "size": 11},
            ]
        ]
    )


@pytest.fixture
def split_table_pdf() -> bytes:
    """One table broken by a page, with its header row repeated on the second page."""

    return build_pdf(
        [
            [
                {"text": "Timetable", "size": 18, "bold": True},
                {"table": [["Event", "Date"], ["Applications open", "01/10/2026"]]},
            ],
            [
                {"table": [["Event", "Date"], ["Written exam", "31/10/2026"]]},
            ],
        ]
    )


@pytest.fixture
def running_text_pdf() -> bytes:
    """Four pages with a running header and footer, and a sentence across a page break."""

    def page(number: int, *, last_line: str | None = None, first_line: str | None = None):
        elements = []

        if first_line:
            elements.append({"text": first_line, "size": 11})

        elements.append({"text": f"Text that belongs to section {number} only.", "size": 11})

        if last_line:
            elements.append({"text": last_line, "size": 11})

        return elements

    return build_pdf(
        [
            [{"text": "Annual Report", "size": 22, "bold": True}, *page(1)],
            page(2, last_line="This sentence starts on one page and continues onto"),
            page(3, first_line="the next page without interruption."),
            page(4),
        ],
        header="ACME Corporation - Edital 12/2026",
        footer="www.acme.example | Página {page} de {pages}",
    )


@pytest.fixture
def notice_pdf() -> bytes:
    """The layout of a typical Brazilian public notice.

    Section titles in capitals ending in a full stop, numbered clauses with
    extra space between them, a subheading in capitals at body size, and a
    table of contents with dot leaders.
    """

    return build_pdf(
        [
            [
                {"text": "EDITAL DE ABERTURA Nº 001/2026", "size": 15},
                {"text": "1. DISPOSIÇÕES INICIAIS ........................ 2", "size": 11},
                {"text": "2. INSCRIÇÃO NO CONCURSO ...................... 3", "size": 11, "leading": 30},
                {"text": "1. DISPOSIÇÕES INICIAIS.", "size": 15, "leading": 26},
                {"text": "1.1. O concurso será regido por este edital e executado pela", "size": 11, "leading": 13.4},
                {"text": "banca organizadora, nos termos do subitem", "size": 11, "leading": 13.4},
                {"text": "2.6.4 deste edital.", "size": 11, "leading": 17.4},
                {"text": "1.2. As provas serão aplicadas na cidade de Porto Calvo.", "size": 11, "leading": 17.4},
                {"text": "CARGO: PROFESSOR DO 6º AO 9º ANO.", "size": 11, "leading": 17.4},
                {"text": "REQUISITOS: licenciatura completa na disciplina, no momento", "size": 11, "leading": 13.4},
                {"text": "da posse, com registro no conselho de classe.", "size": 11, "leading": 17.4},
                {"text": "TOTAL 36 2 13 51", "size": 11, "leading": 17.4},
                {"text": "Os candidatos aprovados serão convocados conforme o cronograma.", "size": 11},
            ]
        ]
    )


@pytest.fixture
def scanned_pdf(simple_pdf: bytes) -> bytes:
    """The simple document as pictures of its pages, with no text layer."""

    return rasterise(simple_pdf)


@pytest.fixture
def stamped_scan_pdf(simple_pdf: bytes) -> bytes:
    """A scan carrying a one line text layer, the way a digital signature stamp adds one."""

    with pdfplumber.open(io.BytesIO(simple_pdf)) as source:
        image = source.pages[0].to_image(resolution=150).original.convert("L")

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.drawImage(ImageReader(image), 0, 0, width=A4[0], height=A4[1])
    pdf.setFont("Helvetica", 7)
    pdf.drawString(72, 20, "Documento assinado digitalmente")
    pdf.showPage()
    pdf.save()

    return buffer.getvalue()


@pytest.fixture
def justified_pdf() -> bytes:
    """A line set with stretched word spacing, as justified text is."""

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    text = pdf.beginText(72, 700)
    text.setFont("Helvetica", 11)
    text.setWordSpace(6)
    text.textLine("Justified text spreads its words across the line.")
    pdf.drawText(text)
    pdf.showPage()
    pdf.save()

    return buffer.getvalue()
