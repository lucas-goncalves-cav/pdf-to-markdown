"""Tests for OCR on scanned pages.

The parsing tests run anywhere. The end to end ones need the tesseract binary
and skip themselves without it, so CI installs it.
"""

from __future__ import annotations

import io

import pytest
from pypdf import PdfReader, PdfWriter

from app.converters import ocr, pipeline
from app.converters.ocr import OcrSettings

ENGLISH = OcrSettings(languages="eng")

needs_tesseract = pytest.mark.skipif(
    ocr.unavailable_reason(ENGLISH) is not None,
    reason="tesseract with English data is not installed",
)

#: What tesseract emits for a title, a body line, a smudge and a ruled table
#: cell, at 300 dpi.
HOCR = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<div class='ocr_page' title='bbox 0 0 2480 3508'>
 <span class='ocr_line' title="bbox 300 228 1092 321; baseline 0 -20; x_size 92; x_descenders 20; x_ascenders 20">
  <span class='ocrx_word' title='bbox 300 228 735 321; x_wconf 96'>Quarterly</span>
  <span class='ocrx_word' title='bbox 760 228 1092 321; x_wconf 95'>Report</span>
 </span>
 <span class='ocr_line' title="bbox 300 534 1658 577; baseline 0 -10; x_size 43; x_descenders 10; x_ascenders 9">
  <span class='ocrx_word' title='bbox 300 534 500 577; x_wconf 91'>Revenue</span>
  <span class='ocrx_word' title='bbox 520 534 640 577; x_wconf 93'>grew</span>
 </span>
 <span class='ocr_line' title="bbox 300 700 400 720; baseline 0 0; x_size 20; x_descenders 4; x_ascenders 4">
  <span class='ocrx_word' title='bbox 300 700 400 720; x_wconf 88'>|~;</span>
 </span>
 <span class='ocr_line' title="bbox 300 800 1500 840; baseline 0 -8; x_size 160; x_descenders 30; x_ascenders 30">
  <span class='ocrx_word' title='bbox 300 800 900 840; x_wconf 34'>[couror__[eranaseninas</span>
  <span class='ocrx_word' title='bbox 920 800 1500 840; x_wconf 41'>wszomoo</span>
 </span>
</div></body></html>"""


class TestHocrParsing:
    def test_lines_are_read_in_points(self) -> None:
        lines = ocr.parse_hocr(HOCR, page_number=3, scale=72 / 300)

        title = lines[0]

        assert title.text == "Quarterly Report"
        assert title.page == 3
        assert title.x0 == pytest.approx(72)
        assert not title.is_bold

    def test_font_size_comes_from_x_size_not_the_box(self) -> None:
        lines = ocr.parse_hocr(HOCR, page_number=1, scale=72 / 300)

        # 92 px at 300 dpi is 22 points, in whole points.
        assert lines[0].font_size == 22
        assert lines[1].font_size == 10

    def test_punctuation_only_lines_are_dropped(self) -> None:
        lines = ocr.parse_hocr(HOCR, page_number=1, scale=72 / 300)

        assert all("|~;" not in line.text for line in lines)

    def test_low_confidence_lines_are_dropped(self) -> None:
        # Table rules read as text come back with confidence in the 30s.
        lines = ocr.parse_hocr(HOCR, page_number=1, scale=72 / 300)

        assert [line.text for line in lines] == ["Quarterly Report", "Revenue grew"]


@needs_tesseract
class TestOcr:
    def test_a_scan_is_read(self, scanned_pdf: bytes) -> None:
        result = pipeline.convert(scanned_pdf, ocr=ENGLISH)

        assert "Quarterly Report" in result.markdown
        assert "enterprise segment" in result.markdown

    def test_headings_survive_ocr(self, scanned_pdf: bytes) -> None:
        result = pipeline.convert(scanned_pdf, ocr=ENGLISH)

        assert "# Quarterly Report" in result.markdown

    def test_the_caller_is_told_the_text_came_from_ocr(self, scanned_pdf: bytes) -> None:
        result = pipeline.convert(scanned_pdf, ocr=ENGLISH)

        assert any("OCR" in warning for warning in result.warnings)

    def test_without_ocr_a_scan_is_reported(self, scanned_pdf: bytes) -> None:
        result = pipeline.convert(scanned_pdf)

        assert result.is_empty
        assert result.warnings == [pipeline.NO_TEXT_WARNING]

    def test_a_signature_stamp_does_not_hide_the_scan(self, stamped_scan_pdf: bytes) -> None:
        result = pipeline.convert(stamped_scan_pdf, ocr=ENGLISH)

        assert "enterprise segment" in result.markdown

    def test_pages_beyond_the_limit_are_skipped_and_reported(self, scanned_pdf: bytes) -> None:
        two_pages = _concatenate(scanned_pdf, scanned_pdf)

        result = pipeline.convert(two_pages, ocr=OcrSettings(languages="eng", max_pages=1))

        assert result.markdown.count("Quarterly Report") == 1
        assert any("Skipped scanned page 2" in warning for warning in result.warnings)

    def test_a_blank_scan_says_ocr_found_nothing(self, empty_pdf: bytes) -> None:
        result = pipeline.convert(empty_pdf, ocr=ENGLISH)

        assert result.is_empty
        assert "even with OCR" in result.warnings[0]

    def test_missing_language_data_is_explained(self) -> None:
        reason = ocr.unavailable_reason(OcrSettings(languages="eng+xyz"))

        assert reason is not None
        assert "xyz" in reason


def _concatenate(*documents: bytes) -> bytes:
    writer = PdfWriter()

    for document in documents:
        for page in PdfReader(io.BytesIO(document)).pages:
            writer.add_page(page)

    buffer = io.BytesIO()
    writer.write(buffer)

    return buffer.getvalue()
