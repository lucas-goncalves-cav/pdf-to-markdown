"""NormalizedDocument v1: the contract, and what it promises about provenance."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.converters.models import TextLine
from app.converters.ocr import OcrSettings, unavailable_reason
from app.converters.structure import join_wrapped
from app.normalized import SCHEMA_VERSION, normalize
from tests.conftest import build_pdf, rasterise

SCHEMA = json.loads(
    (Path(__file__).parent.parent / "schemas" / "normalized-document.v1.schema.json").read_text(
        encoding="utf-8"
    )
)

FIXED_TIME = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def _validate(document: dict) -> None:
    jsonschema.Draft202012Validator(
        SCHEMA, format_checker=jsonschema.FormatChecker()
    ).validate(document)


@pytest.fixture
def edital_pdf() -> bytes:
    """Two pages shaped like a notice: numbered sections, a wrapped clause, a table."""

    return build_pdf(
        [
            [
                {"text": "EDITAL Nº 1/2026", "size": 16, "bold": True},
                {"text": "1. DAS DISPOSIÇÕES PRELIMINARES", "size": 13, "bold": True},
                {"text": "1.1 O concurso será regido por este edital e executado", "leading": 14},
                {"text": "pela banca organizadora, com provas em todo o país.", "leading": 24},
                {"text": "2. DOS CARGOS", "size": 13, "bold": True},
                {
                    "table": [
                        ["Cargo", "Vagas", "Remuneração"],
                        ["Analista", "10", "R$ 9.000,00"],
                        ["Técnico", "20", "R$ 5.000,00"],
                    ]
                },
            ],
            [
                {"text": "3. DAS INSCRIÇÕES", "size": 13, "bold": True},
                {"text": "3.1 As inscrições ficam abertas de 1º a 30 de novembro.", "leading": 24},
                {"text": "3.1.1 A taxa de inscrição é de R$ 120,00.", "leading": 24},
            ],
        ]
    )


@pytest.fixture
def document(edital_pdf: bytes) -> dict:
    return normalize(
        edital_pdf,
        source_uri="https://example.gov.br/edital.pdf",
        filename="edital.pdf",
        generated_at=FIXED_TIME,
    )


class TestContract:
    def test_validates_against_the_schema(self, document: dict) -> None:
        _validate(document)

    def test_declares_its_version_and_origin(self, document: dict) -> None:
        assert document["schema"] == "normalized-document"
        assert document["schema_version"] == SCHEMA_VERSION
        assert document["source"]["uri"] == "https://example.gov.br/edital.pdf"
        assert document["source"]["filename"] == "edital.pdf"
        assert document["source"]["page_count"] == 2
        assert document["source"]["content_hash"].startswith("sha256:")

    @pytest.mark.parametrize(
        "example", sorted((Path(__file__).parent.parent / "schemas" / "examples").glob("*.json"))
    )
    def test_published_examples_validate(self, example: Path) -> None:
        _validate(json.loads(example.read_text(encoding="utf-8")))

    def test_an_empty_scan_still_validates(self, edital_pdf: bytes) -> None:
        scan = normalize(rasterise(edital_pdf), generated_at=FIXED_TIME)

        _validate(scan)
        assert scan["quality"]["status"] == "unreadable"
        assert scan["warnings"][0]["code"] == "NO_TEXT"
        assert [page["text_source"] for page in scan["pages"]] == ["none", "none"]


class TestIdempotency:
    def test_same_pdf_same_ids_and_hash(self, edital_pdf: bytes, document: dict) -> None:
        again = normalize(edital_pdf, generated_at=datetime(2030, 1, 1, tzinfo=UTC))

        assert again["document_id"] == document["document_id"]
        assert again["normalized_content_hash"] == document["normalized_content_hash"]

    def test_different_options_are_a_different_document(
        self, edital_pdf: bytes, document: dict
    ) -> None:
        without_tables = normalize(edital_pdf, include_tables=False)

        assert without_tables["document_id"] != document["document_id"]

    def test_different_pdf_is_a_different_document(self, document: dict) -> None:
        other = normalize(build_pdf([[{"text": "Outro documento."}]]))

        assert other["document_id"] != document["document_id"]
        assert other["source"]["content_hash"] != document["source"]["content_hash"]


class TestProvenance:
    def test_every_block_points_at_its_page(self, document: dict) -> None:
        for block in document["blocks"]:
            assert block["spans"], block
            assert all(
                block["page_start"] <= span["page"] <= block["page_end"] for span in block["spans"]
            )

    def test_text_spans_cover_the_block_text(self, document: dict) -> None:
        for block in document["blocks"]:
            if block["type"] == "table":
                continue

            assert block["spans"][0]["start"] == 0
            assert block["spans"][-1]["end"] == len(block["text"])

    def test_a_quote_resolves_to_the_line_it_came_from(self, document: dict) -> None:
        clause = next(b for b in document["blocks"] if b["text"].startswith("1.1 O concurso"))
        start = clause["text"].index("banca organizadora")
        span = next(s for s in clause["spans"] if s["start"] <= start < s["end"])

        # The second line of the clause, below the first one.
        assert span is clause["spans"][1]
        assert span["bbox"][1] > clause["spans"][0]["bbox"][1]

    def test_boxes_sit_inside_the_page(self, document: dict) -> None:
        sizes = {page["number"]: (page["width"], page["height"]) for page in document["pages"]}

        for block in document["blocks"]:
            for span in block["spans"]:
                x0, top, x1, bottom = span["bbox"]
                width, height = sizes[span["page"]]

                assert 0 <= x0 < x1 <= width
                assert 0 <= top < bottom <= height

    def test_table_rows_keep_their_own_box(self, document: dict) -> None:
        (table,) = document["tables"]

        assert table["rows"][0]["cells"] == ["Cargo", "Vagas", "Remuneração"]
        assert table["rows"][1]["cells"] == ["Analista", "10", "R$ 9.000,00"]
        assert table["column_count"] == 3
        assert table["row_count"] == 3

        tops = [row["bbox"][1] for row in table["rows"]]
        assert tops == sorted(tops)
        assert all(row["page"] == 1 for row in table["rows"])

    def test_every_cell_keeps_its_box_inside_its_row(self, document: dict) -> None:
        (table,) = document["tables"]

        for row in table["rows"]:
            assert len(row["cell_boxes"]) == len(row["cells"])
            x0s = [box[0] for box in row["cell_boxes"]]
            assert x0s == sorted(x0s)

            for x0, top, x1, bottom in row["cell_boxes"]:
                assert row["bbox"][0] - 1 <= x0 < x1 <= row["bbox"][2] + 1
                assert row["bbox"][1] - 1 <= top < bottom <= row["bbox"][3] + 1

    def test_table_block_and_table_point_at_each_other(self, document: dict) -> None:
        (table,) = document["tables"]
        block = next(b for b in document["blocks"] if b["id"] == table["block_id"])

        assert block["type"] == "table"
        assert block["table_id"] == table["id"]
        assert block["text"] == ""


class TestSections:
    def test_numbered_headings_become_sections(self, document: dict) -> None:
        titles = [(s["numbering"], s["title"]) for s in document["sections"]]

        assert (None, "EDITAL Nº 1/2026") in titles
        assert ("1", "1. DAS DISPOSIÇÕES PRELIMINARES") in titles
        assert ("2", "2. DOS CARGOS") in titles
        assert ("3", "3. DAS INSCRIÇÕES") in titles

    def test_sections_nest_under_the_larger_heading(self, document: dict) -> None:
        by_title = {s["title"]: s for s in document["sections"]}
        root = by_title["EDITAL Nº 1/2026"]

        assert root["parent_id"] is None
        assert by_title["2. DOS CARGOS"]["parent_id"] == root["id"]
        assert root["last_block_id"] == document["blocks"][-1]["id"]

    def test_a_section_runs_until_the_next_heading_of_its_level(self, document: dict) -> None:
        by_title = {s["title"]: s for s in document["sections"]}
        cargos = by_title["2. DOS CARGOS"]
        (table,) = document["tables"]

        assert table["section_id"] == cargos["id"]
        assert cargos["last_block_id"] == table["block_id"]
        assert by_title["3. DAS INSCRIÇÕES"]["page_start"] == 2

    def test_every_block_names_an_existing_section(self, document: dict) -> None:
        ids = {s["id"] for s in document["sections"]}

        assert all(block["section_id"] in ids for block in document["blocks"])


class TestQuality:
    def test_a_clean_text_pdf_is_ok(self, document: dict) -> None:
        quality = document["quality"]

        assert quality["status"] == "ok"
        assert quality["pages_text_layer"] == 2
        assert quality["fallback_pages"] == []
        assert document["warnings"] == []

    def test_coverage_counts_table_text_once(self, document: dict) -> None:
        for page in document["pages"]:
            assert page["coverage"] == pytest.approx(1.0, abs=0.02)

    def test_truncation_is_reported_with_the_pages_left_out(self, edital_pdf: bytes) -> None:
        truncated = normalize(edital_pdf, max_pages=1)

        _validate(truncated)
        assert truncated["quality"]["status"] == "degraded"
        assert truncated["quality"]["pages_converted"] == 1
        warning = next(w for w in truncated["warnings"] if w["code"] == "TRUNCATED")
        assert warning["pages"] == [2]

    def test_a_sideways_signature_stamp_stays_out_of_the_text(self) -> None:
        # What a signing tool adds to every page of a real notice: a line set
        # on its side in the margin. It used to come out a letter per line,
        # scattered through the paragraphs.
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        pdf.setFont("Helvetica", 11)
        pdf.drawString(72, 760, "1.1 O concurso será regido por este edital.")
        pdf.drawString(72, 740, "1.2 As provas serão aplicadas em Vitória.")
        pdf.saveState()
        pdf.translate(560, 200)
        pdf.rotate(90)
        pdf.setFont("Helvetica", 7)
        pdf.drawString(0, 0, "Documento assinado digitalmente. Validação: ABC123")
        pdf.restoreState()
        pdf.save()

        document = normalize(buffer.getvalue())

        _validate(document)
        text = " ".join(block["text"] for block in document["blocks"])
        assert "1.2 As provas serão aplicadas em Vitória." in text
        assert "assinado" not in text and "ABC123" not in text
        assert document["pages"][0]["chars_sideways"] > 30
        assert document["pages"][0]["coverage"] == pytest.approx(1.0, abs=0.02)
        assert [w["code"] for w in document["warnings"]] == ["SIDEWAYS_TEXT_SKIPPED"]
        assert document["quality"]["status"] == "ok"

    def test_a_page_turned_on_its_side_is_kept_and_flagged(self) -> None:
        # A landscape annex drawn turned on a portrait page: many sideways
        # lines, not a strip in the margin. Its text is kept, but read by
        # height it comes out a letter at a time, so the page is flagged for
        # reading from the PDF rather than trusted.
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        pdf.saveState()
        pdf.translate(72, 72)
        pdf.rotate(90)
        pdf.setFont("Helvetica", 10)

        for row in range(12):
            pdf.drawString(0, -20 - row * 16, f"Cargo {row} Analista vagas {row + 1}")

        pdf.restoreState()
        pdf.save()

        document = normalize(buffer.getvalue())

        _validate(document)
        assert document["pages"][0]["chars_sideways"] == 0
        assert document["pages"][0]["coverage"] == pytest.approx(1.0, abs=0.02)
        assert [w["code"] for w in document["warnings"]] == ["SIDEWAYS_CONTENT"]
        assert document["quality"]["fallback_pages"] == [1]
        assert document["quality"]["status"] == "degraded"

    def test_a_scan_without_ocr_names_its_unread_pages(self, edital_pdf: bytes) -> None:
        # A text page followed by a scanned one: the common case of a notice
        # with a scanned annex.
        mixed = build_pdf([[{"text": "Texto do edital."}]])
        writer = PdfWriter()

        for data in (mixed, rasterise(edital_pdf)):
            for page in PdfReader(io.BytesIO(data)).pages:
                writer.add_page(page)

        buffer = io.BytesIO()
        writer.write(buffer)

        document = normalize(buffer.getvalue())

        _validate(document)
        assert document["quality"]["status"] == "degraded"
        assert document["quality"]["pages_unread"] == 2
        assert document["quality"]["fallback_pages"] == [2, 3]
        assert [page["text_source"] for page in document["pages"]] == ["text_layer", "none", "none"]


@pytest.mark.skipif(
    unavailable_reason(OcrSettings(languages="eng")) is not None,
    reason="tesseract is not installed",
)
class TestOcr:
    def test_ocr_pages_are_marked_with_their_confidence(self, edital_pdf: bytes) -> None:
        document = normalize(rasterise(edital_pdf, resolution=300), ocr=OcrSettings("eng"))

        _validate(document)
        assert {page["text_source"] for page in document["pages"]} == {"ocr"}
        assert all(page["ocr_confidence"] for page in document["pages"])
        assert document["quality"]["status"] == "degraded"
        assert any(w["code"] == "OCR_USED" for w in document["warnings"])
        assert all(block["source"] == "ocr" for block in document["blocks"])
        assert all(span["bbox"] for block in document["blocks"] for span in block["spans"])


class TestJoinWrapped:
    @staticmethod
    def _line(text: str) -> TextLine:
        return TextLine(text=text, font_size=11, is_bold=False, x0=72, top=0, page=1)

    def test_ranges_point_at_each_line(self) -> None:
        text, ranges = join_wrapped([self._line("Primeira linha"), self._line("segunda linha.")])

        assert text == "Primeira linha segunda linha."
        assert [text[start:end] for start, end in ranges] == ["Primeira linha", "segunda linha."]

    def test_a_hyphenated_word_is_split_where_the_hyphen_was(self) -> None:
        text, ranges = join_wrapped([self._line("conteúdo progra-"), self._line("mático do cargo")])

        assert text == "conteúdo programático do cargo"
        assert [text[start:end] for start, end in ranges] == ["conteúdo progra", "mático do cargo"]
