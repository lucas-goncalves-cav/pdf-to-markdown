"""Tests for the extraction and conversion pipeline, against real PDFs."""

from __future__ import annotations

import pytest

from app.converters import extractor, furniture, pipeline, structure
from app.converters.models import BlockKind, Table, TextLine


def body(text: str, top: float, *, page: int = 1, size: float = 11) -> TextLine:
    return TextLine(text=text, font_size=size, is_bold=False, x0=72, top=top, page=page)


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

    def test_body_size_is_the_size_carrying_the_most_text(self) -> None:
        # Two headings outnumber two lines of body text, but carry far less.
        lines = [
            self.line("Overview", 14),
            self.line("Revenue grew by twelve percent over the quarter, driven by", 10),
            self.line("Outlook", 14),
            self.line("The next quarter is expected to follow the same trend.", 10),
        ]

        assert structure.detect_body_size(lines) == 10

    def test_a_title_in_capitals_may_end_in_a_full_stop(self) -> None:
        # Public notices punctuate their section titles: "1. DISPOSIÇÕES INICIAIS."
        lines = [
            self.line("DISPOSIÇÕES INICIAIS.", 15),
            *[self.line("Body", 11) for _ in range(10)],
        ]

        blocks = structure.to_blocks(lines)

        assert blocks[0].kind is BlockKind.HEADING

    def test_a_numbered_title_may_end_in_a_full_stop(self) -> None:
        lines = [
            self.line("2. Inscrição no concurso.", 15),
            *[self.line("Body", 11) for _ in range(10)],
        ]

        assert structure.to_blocks(lines)[0].kind is BlockKind.HEADING

    def test_numbered_headings_nest_below_size_based_levels(self) -> None:
        """A "8.1" must never outrank the "8." it belongs to.

        Numbered headings at body size used to take their level from the
        numbering alone, so "8." landed below the size based levels while
        "8.1" landed above them.
        """

        lines = [
            self.line("Edital de abertura", 24),
            self.line("8. HABILITAÇÃO", 11, bold=True),
            self.line("8.1. Habilitação jurídica", 11, bold=True),
            *[self.line("Body", 11) for _ in range(10)],
        ]

        headings = [block for block in structure.to_blocks(lines) if block.kind is BlockKind.HEADING]

        assert [heading.level for heading in headings] == [1, 2, 3]

    def test_a_long_numbered_bold_title_is_a_heading_not_a_list_item(self) -> None:
        title = "5. DO CREDENCIAMENTO E DA PARTICIPAÇÃO NO PREGÃO ELETRÔNICO MUNICIPAL"

        lines = [
            self.line(title, 11, bold=True),
            *[self.line("Body", 11) for _ in range(10)],
        ]

        assert structure.to_blocks(lines)[0].kind is BlockKind.HEADING

    def test_a_table_of_contents_entry_is_not_a_heading(self) -> None:
        lines = [
            self.line("ANEXO I – REQUISITOS E ATRIBUIÇÕES … 31", 15),
            *[self.line("Body", 11) for _ in range(10)],
        ]

        assert structure.to_blocks(lines)[0].kind is not BlockKind.HEADING


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

    def test_a_lettered_item_keeps_its_letter(self) -> None:
        # A notice refers to "alínea b", so the letter is content. Dropping it
        # was most of the text a real notice lost in the benchmark.
        blocks = structure.to_blocks(
            [self.line("3.2 São requisitos:"), self.line("b) ter 18 anos completos;", x0=90)]
        )

        assert blocks[1].kind is BlockKind.LIST_ITEM
        assert blocks[1].text == "b) ter 18 anos completos;"

    def test_a_wrapped_item_is_rejoined(self) -> None:
        lines = [
            TextLine("• Ser brasileiro nato ou naturalizado, em condição de igualdade com", 11, False, 90, 100, 1),
            TextLine("os brasileiros, na forma da Constituição;", 11, False, 72, 114, 1),
            TextLine("• Gozar dos direitos políticos;", 11, False, 90, 128, 1),
        ]

        blocks = structure.to_blocks(lines)

        assert [block.kind for block in blocks] == [BlockKind.LIST_ITEM, BlockKind.LIST_ITEM]
        assert blocks[0].text.endswith("em condição de igualdade com os brasileiros, na forma da Constituição;")
        assert len(blocks[0].lines) == 2

    def test_a_finished_item_is_not_continued_by_the_next_paragraph(self) -> None:
        lines = [
            TextLine("• Escolaridade mínima exigida para o cargo.", 11, False, 90, 100, 1),
            TextLine("O candidato que não reunir os requisitos perderá o direito.", 11, False, 72, 114, 1),
        ]

        blocks = structure.to_blocks(lines)

        assert [block.kind for block in blocks] == [BlockKind.LIST_ITEM, BlockKind.PARAGRAPH]

    def test_a_numbered_title_in_capitals_is_a_heading_not_a_list(self) -> None:
        # How notices built in SEI set their sections: body size, not bold.
        blocks = structure.to_blocks(
            [self.line("1. DAS DISPOSIÇÕES PRELIMINARES"), self.line("1.1 O concurso será regido.")]
        )

        assert blocks[0].kind is BlockKind.HEADING
        assert blocks[0].text == "1. DAS DISPOSIÇÕES PRELIMINARES"


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


class TestParagraphBreaks:
    """A page shows paragraphs as space between lines, which has to be read back."""

    def test_extra_space_between_lines_starts_a_paragraph(self) -> None:
        lines = [
            body("The first paragraph runs", 100),
            body("over two lines.", 113.4),
            body("The second paragraph", 130.8),
            body("follows a wider gap.", 144.2),
        ]

        blocks = structure.to_blocks(lines)

        assert [block.text for block in blocks] == [
            "The first paragraph runs over two lines.",
            "The second paragraph follows a wider gap.",
        ]

    def test_ordinary_line_spacing_keeps_a_paragraph_together(self) -> None:
        lines = [body(f"Line {index} of one paragraph", 100 + index * 13.4) for index in range(5)]

        assert len(structure.to_blocks(lines)) == 1

    def test_a_numbered_clause_after_a_full_stop_starts_a_paragraph(self) -> None:
        # Evenly spaced, so only the clause number separates them.
        lines = [
            body("1.1. The notice is governed by these rules.", 100),
            body("1.2. Exams take place in the city of Porto Calvo.", 113.4),
            body("1.3. Results are published online.", 126.8),
        ]

        assert len(structure.to_blocks(lines)) == 3

    def test_a_clause_reference_wrapped_mid_sentence_does_not(self) -> None:
        lines = [
            body("Requests follow the procedure in subitem", 100),
            body("2.6.4 of this notice, within the deadline.", 113.4),
            body("Late requests are not considered at all.", 126.8),
        ]

        blocks = structure.to_blocks(lines)

        assert "subitem 2.6.4 of this notice" in blocks[0].text

    def test_a_change_of_font_size_starts_a_paragraph(self) -> None:
        lines = [
            body("Body text of the page.", 100),
            body("A footnote in a smaller size.", 113.4, size=8),
        ]

        assert len(structure.to_blocks(lines)) == 2

    def test_a_paragraph_continues_onto_the_next_page(self) -> None:
        lines = [
            body("A sentence that starts at the foot of one page and", 780, page=1),
            body("ends at the top of the next.", 72, page=2),
        ]

        blocks = structure.to_blocks(lines)

        assert len(blocks) == 1
        assert "page and ends at the top" in blocks[0].text


class TestNoticeLayout:
    """The layout of Brazilian public notices, which the original heuristics missed."""

    def test_section_titles_ending_in_a_full_stop_are_headings(self, notice_pdf: bytes) -> None:
        result = pipeline.convert(notice_pdf)

        assert "# 1. DISPOSIÇÕES INICIAIS." in result.markdown

    def test_each_clause_is_its_own_paragraph(self, notice_pdf: bytes) -> None:
        result = pipeline.convert(notice_pdf)

        assert "\n\n1.2. As provas serão aplicadas" in result.markdown
        assert "nos termos do subitem 2.6.4 deste edital." in result.markdown

    def test_a_line_in_capitals_on_its_own_is_a_subheading(self, notice_pdf: bytes) -> None:
        # The "º" in "6º" counts as a lowercase letter to str.isupper.
        result = pipeline.convert(notice_pdf)

        assert "## CARGO: PROFESSOR DO 6º AO 9º ANO." in result.markdown

    def test_a_stray_row_of_figures_is_not_a_subheading(self, notice_pdf: bytes) -> None:
        result = pipeline.convert(notice_pdf)

        assert "TOTAL 36 2 13 51" in result.markdown
        assert "# TOTAL" not in result.markdown

    def test_capitals_inside_a_longer_paragraph_are_not_a_subheading(self) -> None:
        lines = [
            body("THE FIRST LINE OF A PARAGRAPH SET", 100),
            body("entirely in capitals continues here.", 113.4),
        ]

        assert structure.to_blocks(lines)[0].kind is BlockKind.PARAGRAPH

    def test_dot_leaders_in_a_table_of_contents_are_collapsed(self, notice_pdf: bytes) -> None:
        result = pipeline.convert(notice_pdf)

        assert "1. DISPOSIÇÕES INICIAIS … 2" in result.markdown
        assert "...." not in result.markdown


class TestTables:
    def test_table_text_appears_once(self, table_pdf: bytes) -> None:
        """Text inside a table used to come out twice, once as the table and
        once flattened into the paragraph around it."""

        result = pipeline.convert(table_pdf)

        assert result.markdown.count("Teacher") == 1
        assert result.markdown.count("R$ 2.565,32") == 1

    def test_a_table_sits_where_it_was_on_the_page(self, table_pdf: bytes) -> None:
        markdown = pipeline.convert(table_pdf).markdown

        before = markdown.index("The positions on offer")
        table = markdown.index("| Position | Hours | Salary |")
        after = markdown.index("Applications open")

        assert before < table < after

    def test_the_text_either_side_of_a_table_is_not_merged(self, table_pdf: bytes) -> None:
        markdown = pipeline.convert(table_pdf).markdown

        assert "listed below. Applications" not in markdown

    def test_without_tables_the_cells_are_kept_as_text(self, table_pdf: bytes) -> None:
        markdown = pipeline.convert(table_pdf, include_tables=False).markdown

        assert "| --- |" not in markdown
        assert markdown.count("Teacher") == 1

    def test_a_table_broken_by_a_page_is_joined(self, split_table_pdf: bytes) -> None:
        markdown = pipeline.convert(split_table_pdf).markdown

        assert markdown.count("| --- | --- |") == 1
        # The header row repeated on the second page is not kept as data.
        assert markdown.count("| Event | Date |") == 1
        assert "| Written exam | 31/10/2026 |" in markdown

    def test_a_page_that_is_only_a_table_is_not_reported_as_a_scan(self) -> None:
        tables = [Table(rows=[["Event", "Date"], ["Exam", "31/10"]], page=1, bbox=(72, 72, 500, 120))]

        blocks = structure.to_blocks([], tables)

        assert [block.kind for block in blocks] == [BlockKind.TABLE]

    def test_empty_columns_are_dropped(self) -> None:
        rows = extractor._clean_table([["Event", None, "Date"], ["Exam", "", "31/10"]])

        assert rows == [["Event", "Date"], ["Exam", "31/10"]]

    def test_a_single_column_is_not_a_table(self) -> None:
        # A framed paragraph comes back from table detection as one column.
        assert extractor._clean_table([["A notice in a box"], ["that spans two lines"]]) == []


class TestRunningText:
    """Running headers and footers, repeated by the page template."""

    def test_footers_are_removed_after_the_first_page(self, running_text_pdf: bytes) -> None:
        markdown = pipeline.convert(running_text_pdf).markdown

        assert "Página 2 de 4" not in markdown
        assert "Página 4 de 4" not in markdown

    def test_the_first_page_keeps_its_copy(self, running_text_pdf: bytes) -> None:
        # Often the only place that names the organisation behind the document.
        markdown = pipeline.convert(running_text_pdf).markdown

        assert markdown.count("ACME Corporation") == 1
        assert "Página 1 de 4" in markdown

    def test_a_sentence_across_a_page_break_is_rejoined(self, running_text_pdf: bytes) -> None:
        markdown = pipeline.convert(running_text_pdf).markdown

        assert "continues onto the next page without interruption." in markdown

    def test_stripping_can_be_switched_off(self, running_text_pdf: bytes) -> None:
        markdown = pipeline.convert(running_text_pdf, strip_headers=False).markdown

        assert "Página 3 de 4" in markdown

    def test_page_numbers_do_not_stop_a_footer_matching(self) -> None:
        assert furniture.signature("Página 2 de 68") == furniture.signature("Página 3 de 68")

    def test_numbers_elsewhere_still_distinguish_lines(self) -> None:
        # "Chapter 2" opening page 2 is a heading, not a running header.
        assert furniture.signature("Chapter 2") != furniture.signature("Chapter 3")

    def test_text_repeated_mid_page_is_kept(self) -> None:
        lines = [body("The same clause, repeated in each annex.", 400, page=page) for page in (1, 2, 3)]

        kept, removed = furniture.strip_running_text(lines, {1: 842, 2: 842, 3: 842})

        assert removed == 0
        assert len(kept) == 3

    def test_a_footer_ending_in_a_bare_page_number_is_removed(self) -> None:
        # From a real notice: nothing says "página", the number just follows
        # the page, one behind it because the cover is not numbered.
        heights = {page: 842 for page in range(1, 6)}
        lines = []

        for page in range(1, 6):
            lines.append(body(f"Texto do corpo da página {page}.", 400, page=page))
            lines.append(body(f"MINISTÉRIO PÚBLICO | CONCURSO PÚBLICO {page - 1}", 800, page=page))

        kept, removed = furniture.strip_running_text(lines, heights)

        assert removed == 4
        assert [line.text for line in kept if "MINISTÉRIO" in line.text] == [
            "MINISTÉRIO PÚBLICO | CONCURSO PÚBLICO 0"
        ]

    def test_numbers_that_do_not_follow_the_page_are_not_page_numbers(self) -> None:
        heights = {page: 842 for page in range(1, 5)}
        lines = [body(f"Tabela {n}", 800, page=page) for page, n in ((1, 7), (2, 3), (3, 9), (4, 1))]

        _kept, removed = furniture.strip_running_text(lines, heights)

        assert removed == 0

    def test_chapter_numbers_at_the_top_still_count(self) -> None:
        heights = {page: 842 for page in range(1, 5)}
        lines = [body(f"Chapter {page}", 60, page=page) for page in range(1, 5)]

        _kept, removed = furniture.strip_running_text(lines, heights)

        assert removed == 0


class TestTextCleanup:
    def test_justified_text_has_single_spaces(self, justified_pdf: bytes) -> None:
        """Justified text widens the gap after a real space, which used to be
        read as a second, implied space."""

        lines = extractor.extract_lines(justified_pdf)

        assert lines[0].text == "Justified text spreads its words across the line."


class TestScannedPages:
    def test_a_page_with_no_text_is_scanned(self, empty_pdf: bytes) -> None:
        document = extractor.extract_document(empty_pdf)

        assert document.scanned_pages == [1]

    def test_a_scan_with_a_stamp_is_still_scanned(self, stamped_scan_pdf: bytes) -> None:
        # A one line text layer added by a signature does not make it a text page.
        document = extractor.extract_document(stamped_scan_pdf)

        assert document.scanned_pages == [1]

    def test_a_text_page_is_not_scanned(self, simple_pdf: bytes) -> None:
        assert extractor.extract_document(simple_pdf).scanned_pages == []

    def test_a_partly_scanned_document_says_which_pages_were_skipped(
        self, stamped_scan_pdf: bytes
    ) -> None:
        result = pipeline.convert(stamped_scan_pdf)

        assert any("Skipped scanned page 1" in warning for warning in result.warnings)
