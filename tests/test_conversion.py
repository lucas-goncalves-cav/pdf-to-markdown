"""Tests for the extraction and conversion pipeline, against real PDFs."""

from __future__ import annotations

import pytest

from app.converters import extractor, pipeline, structure
from app.converters.models import BlockKind, TextLine


class TestExtraction:
    def test_extracts_lines_with_font_metadata(self, simple_pdf: bytes) -> None:
        lines = extractor.extract_lines(simple_pdf)

        assert lines
        assert all(line.font_size > 0 for line in lines)
        assert any(line.is_bold for line in lines)

    def test_reads_words_with_spaces_between_them(self, simple_pdf: bytes) -> None:
        lines = extractor.extract_lines(simple_pdf)
        joined = " ".join(line.text for line in lines)

        # PDFs often position words without emitting spaces, which would give
        # "Quarterlyreport" if the gap detection were missing.
        assert "Quarterly Report" in joined
        assert "enterprise segment" in joined

    def test_keeps_reading_order(self, simple_pdf: bytes) -> None:
        lines = extractor.extract_lines(simple_pdf)
        texts = [line.text for line in lines]

        assert texts.index("Quarterly Report") < texts.index("Overview")
        assert texts.index("Overview") < texts.index("Outlook")

    def test_counts_pages(self, multipage_pdf: bytes) -> None:
        assert extractor.count_pages(multipage_pdf) == 3

    def test_tags_each_line_with_its_page(self, multipage_pdf: bytes) -> None:
        lines = extractor.extract_lines(multipage_pdf)

        assert {line.page for line in lines} == {1, 2, 3}

    def test_max_pages_stops_early(self, multipage_pdf: bytes) -> None:
        lines = extractor.extract_lines(multipage_pdf, max_pages=2)

        assert {line.page for line in lines} == {1, 2}

    def test_rejects_something_that_is_not_a_pdf(self, not_a_pdf: bytes) -> None:
        with pytest.raises(extractor.PdfExtractionError):
            extractor.extract_lines(not_a_pdf)

    def test_a_pdf_with_no_text_yields_no_lines(self, empty_pdf: bytes) -> None:
        assert extractor.extract_lines(empty_pdf) == []


class TestUnmappableGlyphs:
    """Fonts without a ToUnicode table produce ``(cid:N)`` instead of a
    character. Bullets are the usual victim, because they come from a symbol
    font, so a leading one is treated as a bullet rather than left in the text.
    """

    def test_a_leading_unmappable_glyph_becomes_a_bullet(self) -> None:
        assert extractor.normalize_cids("(cid:127) An item") == "• An item"

    def test_unmappable_glyphs_inside_text_are_dropped(self) -> None:
        assert extractor.normalize_cids("Price(cid:12) is fixed") == "Price is fixed"

    def test_text_without_them_is_untouched(self) -> None:
        assert extractor.normalize_cids("Ordinary text") == "Ordinary text"

    def test_a_line_that_is_only_an_unmappable_glyph_becomes_empty(self) -> None:
        assert extractor.normalize_cids("(cid:127)").strip() == ""

    def test_bulleted_lines_survive_the_round_trip(self, list_pdf: bytes) -> None:
        # The fixture uses a real bullet character, which Helvetica cannot map,
        # so this exercises the same path a word processor PDF would.
        lines = extractor.extract_lines(list_pdf)
        bulleted = [line for line in lines if line.text.startswith("•")]

        assert len(bulleted) == 3


class TestHeadingDetection:
    """The heuristics, tested directly so a failure names the rule that broke."""

    @staticmethod
    def line(text: str, size: float, *, bold: bool = False, x0: float = 72) -> TextLine:
        return TextLine(text=text, font_size=size, is_bold=bold, x0=x0, top=0, page=1)

    def test_body_size_is_the_most_common_size_not_the_average(self) -> None:
        lines = [
            self.line("Enormous title", 48),
            *[self.line(f"Body line {index}", 11) for index in range(20)],
        ]

        # The mean would be dragged up by the title and suppress real headings.
        assert structure.detect_body_size(lines) == 11

    def test_larger_sizes_become_lower_heading_levels(self) -> None:
        lines = [
            self.line("Title", 24),
            self.line("Section", 18),
            self.line("Subsection", 14),
            *[self.line("Body", 11) for _ in range(10)],
        ]

        scale = structure.build_heading_scale(lines, body_size=11)

        assert scale[24.0] == 1
        assert scale[18.0] == 2
        assert scale[14.0] == 3
        assert 11.0 not in scale

    def test_a_size_barely_above_body_is_not_a_heading(self) -> None:
        lines = [
            self.line("Slightly bigger", 11.5),
            *[self.line("Body", 11) for _ in range(10)],
        ]

        assert structure.build_heading_scale(lines, body_size=11) == {}

    def test_a_long_line_is_not_a_heading_however_large(self) -> None:
        long_text = " ".join(["word"] * 30)

        lines = [
            self.line(long_text, 20),
            *[self.line("Body", 11) for _ in range(10)],
        ]

        blocks = structure.to_blocks(lines)

        assert blocks[0].kind is BlockKind.PARAGRAPH

    def test_a_line_ending_in_a_full_stop_is_not_a_heading(self) -> None:
        lines = [
            self.line("This is a sentence in a larger font.", 20),
            *[self.line("Body", 11) for _ in range(10)],
        ]

        blocks = structure.to_blocks(lines)

        assert all(block.kind is not BlockKind.HEADING for block in blocks)

    def test_numbered_headings_nest_by_their_numbering(self) -> None:
        lines = [
            self.line("1 Introduction", 11, bold=True),
            self.line("1.2 Scope", 11, bold=True),
            self.line("1.2.3 Detail", 11, bold=True),
            *[self.line("Body", 11) for _ in range(10)],
        ]

        blocks = structure.to_blocks(lines)
        headings = [block for block in blocks if block.kind is BlockKind.HEADING]

        assert [heading.level for heading in headings[:3]] == [1, 2, 3]

    def test_short_bold_lines_are_treated_as_minor_headings(self) -> None:
        lines = [
            self.line("Notes", 11, bold=True),
            *[self.line("Body text", 11) for _ in range(10)],
        ]

        blocks = structure.to_blocks(lines)

        assert blocks[0].kind is BlockKind.HEADING


class TestListDetection:
    @staticmethod
    def line(text: str, *, x0: float = 72) -> TextLine:
        return TextLine(text=text, font_size=11, is_bold=False, x0=x0, top=0, page=1)

    @pytest.mark.parametrize("bullet", ["•", "◦", "-", "*", "–"])
    def test_recognises_common_bullet_glyphs(self, bullet: str) -> None:
        blocks = structure.to_blocks([self.line(f"{bullet} An item")])

        assert blocks[0].kind is BlockKind.LIST_ITEM
        assert blocks[0].text == "An item"

    @pytest.mark.parametrize("marker", ["1.", "2)", "17."])
    def test_recognises_ordered_markers(self, marker: str) -> None:
        blocks = structure.to_blocks([self.line(f"{marker} Do the thing")])

        assert blocks[0].kind is BlockKind.LIST_ITEM

    def test_indentation_becomes_nesting(self) -> None:
        lines = [
            self.line("• Top level", x0=72),
            self.line("• Nested once", x0=72 + 20),
            self.line("• Nested twice", x0=72 + 40),
        ]

        blocks = structure.to_blocks(lines)

        assert [block.indent for block in blocks] == [0, 1, 2]

    def test_a_sentence_starting_with_a_letter_and_a_stop_is_not_a_list(self) -> None:
        # "a. " at the left margin is far more likely to be prose than a list.
        blocks = structure.to_blocks([self.line("a. This looks like a list but is not indented")])

        assert blocks[0].kind is BlockKind.PARAGRAPH


class TestParagraphJoining:
    def test_wrapped_lines_become_one_paragraph(self, wrapped_paragraph_pdf: bytes) -> None:
        result = pipeline.convert(wrapped_paragraph_pdf, include_tables=False)

        assert "rejoined into a single paragraph" in result.markdown
        # The three source lines are one paragraph, so the sentence is not
        # broken by a newline.
        assert "following line and should be rejoined" in result.markdown

    def test_a_hyphen_at_a_line_end_rejoins_the_word(self) -> None:
        lines = [
            TextLine(text="This word is hyphen-", font_size=11, is_bold=False, x0=72, top=0, page=1),
            TextLine(text="ated across two lines.", font_size=11, is_bold=False, x0=72, top=20, page=1),
        ]

        blocks = structure.to_blocks(lines)

        assert "hyphenated across two lines." in blocks[0].text


class TestConversion:
    def test_produces_markdown_headings(self, simple_pdf: bytes) -> None:
        result = pipeline.convert(simple_pdf, include_tables=False)

        assert "# Quarterly Report" in result.markdown
        assert "## Overview" in result.markdown
        assert "## Outlook" in result.markdown

    def test_produces_markdown_lists(self, list_pdf: bytes) -> None:
        result = pipeline.convert(list_pdf, include_tables=False)

        assert "- The service must accept PDF uploads" in result.markdown
        assert "1. Validate the extension" in result.markdown

    def test_nested_items_are_indented(self, list_pdf: bytes) -> None:
        result = pipeline.convert(list_pdf, include_tables=False)

        assert "  - Nested detail about headings" in result.markdown

    def test_consecutive_list_items_stay_in_one_list(self, list_pdf: bytes) -> None:
        result = pipeline.convert(list_pdf, include_tables=False)

        # A blank line between items would split them into separate lists.
        assert "- The service must accept PDF uploads\n- Conversion must preserve headings" in result.markdown

    def test_escapes_markdown_syntax_found_in_prose(self, markdown_characters_pdf: bytes) -> None:
        result = pipeline.convert(markdown_characters_pdf, include_tables=False)

        # Left alone, "[1]" renders as a broken link and "*" starts emphasis.
        assert r"\[1\]" in result.markdown
        assert r"\*" in result.markdown
        assert r"5\_000" in result.markdown

    def test_reports_the_page_count(self, multipage_pdf: bytes) -> None:
        result = pipeline.convert(multipage_pdf, include_tables=False)

        assert result.page_count == 3

    def test_every_page_is_converted(self, multipage_pdf: bytes) -> None:
        result = pipeline.convert(multipage_pdf, include_tables=False)

        for number in (1, 2, 3):
            assert f"Chapter {number}" in result.markdown

    def test_max_pages_is_reported_as_a_warning(self, multipage_pdf: bytes) -> None:
        result = pipeline.convert(multipage_pdf, include_tables=False, max_pages=1)

        assert "Chapter 1" in result.markdown
        assert "Chapter 3" not in result.markdown
        assert any("first 1 of 3" in warning for warning in result.warnings)

    def test_a_scanned_pdf_is_reported_rather_than_returned_empty(self, empty_pdf: bytes) -> None:
        result = pipeline.convert(empty_pdf)

        assert result.is_empty
        assert result.warnings
        assert "scan" in result.warnings[0].lower()
        assert "OCR" in result.warnings[0]

    def test_an_invalid_file_raises(self, not_a_pdf: bytes) -> None:
        with pytest.raises(extractor.PdfExtractionError):
            pipeline.convert(not_a_pdf)

    def test_the_output_ends_with_a_single_newline(self, simple_pdf: bytes) -> None:
        result = pipeline.convert(simple_pdf, include_tables=False)

        assert result.markdown.endswith("\n")
        assert not result.markdown.endswith("\n\n")


class TestAlternativeFormats:
    def test_plain_text_carries_no_markdown_syntax(self, simple_pdf: bytes) -> None:
        text = pipeline.convert_to_text(simple_pdf)

        assert "Quarterly Report" in text
        assert "#" not in text

    def test_json_exposes_the_block_structure(self, simple_pdf: bytes) -> None:
        document = pipeline.convert_to_json(simple_pdf)

        assert document["pageCount"] == 1
        assert document["blocks"]

        kinds = {block["kind"] for block in document["blocks"]}
        assert "heading" in kinds
        assert "paragraph" in kinds

    def test_json_headings_carry_their_level(self, simple_pdf: bytes) -> None:
        document = pipeline.convert_to_json(simple_pdf)
        headings = [block for block in document["blocks"] if block["kind"] == "heading"]

        assert headings
        assert all(block["level"] is not None for block in headings)
