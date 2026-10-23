"""Turns extracted lines into structured blocks.

A PDF has no headings, no lists and no paragraphs. It has glyphs at
coordinates. Everything below is inference, and the honest framing is that it
is a set of heuristics that work well on text documents produced by word
processors and less well on magazine layouts.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from itertools import pairwise

from app.converters.models import Block, BlockKind, Table, TextLine

#: A line has to be this much larger than body text before size alone is taken
#: as evidence of a heading. Below it, ordinary variation in a paragraph would
#: promote random lines.
HEADING_SIZE_RATIO = 1.12

#: Headings are short. A long line in a large font is usually a pull quote or
#: a title page blurb, not a section heading.
MAX_HEADING_WORDS = 14

#: A gap this many times the normal distance between lines of a paragraph
#: means a new paragraph starts. Paragraph spacing is usually a third of a
#: line or more, while lines inside a paragraph vary by a few percent.
PARAGRAPH_GAP_RATIO = 1.2

#: Lines in one paragraph share a size. A larger jump than this, in points,
#: is a footnote, a caption or a different block altogether.
FONT_SIZE_TOLERANCE = 1.0

#: Bullets that word processors emit. The glyph varies by font.
BULLET_CHARACTERS = "•◦‣⁃∙·●○–—*-"

_BULLET_PATTERN = re.compile(rf"^\s*[{re.escape(BULLET_CHARACTERS)}]\s+(?P<text>.+)$")
_ORDERED_PATTERN = re.compile(r"^\s*(?P<marker>\d{1,3})[.)]\s+(?P<text>.+)$")
_LETTER_PATTERN = re.compile(r"^\s*(?P<marker>[a-zA-Z][.)])\s+(?P<text>.+)$")

#: A numbered heading: "2.1 Scope", "2.1.3. Detail", or a single number with
#: an explicit separator, "8. HABILITAÇÃO" or "8 - HABILITAÇÃO". A bare
#: "8 Something" is left to the minor heading rule, since plenty of sentences
#: start with a number.
_NUMBERED_HEADING = re.compile(
    r"""^\s*
    (?P<numbering>
        \d+(?:\.\d+){1,4}\.?        # 2.1  2.1.3  2.1.3.
      | \d{1,3}\s?[.)\-–—]          # 8.  8)  8 -  8 –
    )
    \s+(?P<text>\S.*)$""",
    re.VERBOSE,
)

#: A clause number at the start of a line, such as "1.1." or "13.11", which in
#: contracts and public notices starts a new paragraph.
_CLAUSE_START = re.compile(r"^\s*\d{1,3}(?:\.\d{1,3})+\.?\s")

#: A table of contents entry: a title, a dot leader and a page number.
_TOC_ENTRY = re.compile(r"…\s*\d{1,4}\s*$")


def detect_body_size(lines: list[TextLine]) -> float:
    """The font size carrying the most text, which is the body of the document.

    Using the mode rather than the mean matters: a document with a large title
    page would drag the mean up and suppress every real heading. Counting
    characters rather than lines matters on short documents, where two
    headings can outnumber two lines of body text, and on scans, where OCR
    spreads one body size over neighbouring estimates.
    """

    if not lines:
        return 0.0

    sizes: Counter[float] = Counter()

    for line in lines:
        sizes[round(line.font_size, 1)] += len(line.text)

    return sizes.most_common(1)[0][0]


def build_heading_scale(lines: list[TextLine], body_size: float) -> dict[float, int]:
    """Map each heading font size to a Markdown level.

    The largest size becomes ``#``, the next ``##``, and so on. Mapping sizes
    to levels rather than guessing per line keeps the hierarchy consistent
    across the document.
    """

    candidates = sorted(
        {
            round(line.font_size, 1)
            for line in lines
            if line.font_size >= body_size * HEADING_SIZE_RATIO
        },
        reverse=True,
    )

    return {size: min(level, 6) for level, size in enumerate(candidates, start=1)}


def typical_line_pitch(lines: list[TextLine], body_size: float) -> float | None:
    """The usual distance between consecutive lines of body text.

    A low percentile rather than the median: in a document of short
    paragraphs, most gaps are paragraph gaps, and the median would mistake
    them for line spacing. ``None`` when there is too little to measure.
    """

    gaps = sorted(
        following.top - line.top
        for line, following in pairwise(lines)
        if line.page == following.page
        and round(line.font_size, 1) == round(following.font_size, 1) == body_size
        and following.top > line.top
    )

    if len(gaps) < 3:
        return None

    return gaps[len(gaps) // 5]


def to_blocks(lines: list[TextLine], tables: list[Table] | None = None) -> list[Block]:
    """Classify every line and merge the ones that belong together.

    Tables are placed by their position on the page, so each one sits between
    the text above and below it rather than at the end of its page.
    """

    tables = tables or []

    if not lines and not tables:
        return []

    body_size = detect_body_size(lines)
    heading_scale = build_heading_scale(lines, body_size)
    indent_base = _detect_indent_base(lines)
    pitch = typical_line_pitch(lines, body_size)
    minor_level = min(len(heading_scale) + 1, 6)

    blocks: list[Block] = []
    paragraph: list[TextLine] = []

    # The table most recently emitted and the page it last reached, so one
    # that runs over a page break can be joined back together.
    open_table: Block | None = None
    open_table_page = 0

    def flush_paragraph() -> None:
        if not paragraph:
            return

        # A line standing on its own, in capitals at body size, is how many
        # documents set a subheading without making it bold:
        # "CARGO: MERENDEIRA ESCOLAR."
        if len(paragraph) == 1 and _is_capitalised_title(paragraph[0], body_size):
            blocks.append(
                Block(
                    kind=BlockKind.HEADING,
                    text=paragraph[0].text.strip(),
                    level=minor_level,
                    page=paragraph[0].page,
                    lines=list(paragraph),
                )
            )
        else:
            blocks.append(
                Block(
                    kind=BlockKind.PARAGRAPH,
                    text=_join_wrapped(paragraph),
                    page=paragraph[0].page,
                    lines=list(paragraph),
                )
            )

        paragraph.clear()

    for item in _in_reading_order(lines, tables):
        if isinstance(item, Table):
            flush_paragraph()

            # Nothing between the end of one page's table and the start of the
            # next page's, and the same columns: it is one table broken by the
            # page. A repeated header row is dropped rather than kept as data.
            if (
                open_table is not None
                and blocks
                and blocks[-1] is open_table
                and item.page == open_table_page + 1
                and len(item.rows[0]) == len(open_table.rows[0])
            ):
                repeats_header = item.rows[0] == open_table.rows[0]
                skip = 1 if repeats_header else 0
                open_table.rows.extend(item.rows[skip:])
                open_table.row_refs.extend(_row_refs(item)[skip:])
                open_table.tables.append(item)
            else:
                open_table = Block(
                    kind=BlockKind.TABLE,
                    text="",
                    rows=list(item.rows),
                    page=item.page,
                    tables=[item],
                    row_refs=_row_refs(item),
                )
                blocks.append(open_table)

            open_table_page = item.page
            continue

        line = item

        if line.is_blank:
            flush_paragraph()
            continue

        heading = _as_heading(line, heading_scale, body_size)

        if heading is not None:
            flush_paragraph()
            heading.lines = [line]
            blocks.append(heading)
            continue

        list_item = _as_list_item(line, indent_base)

        if list_item is not None:
            flush_paragraph()

            # "1. DAS INSCRIÇÕES" in capitals at body size is how many notices
            # set their section titles, and it is not the first item of a list.
            if _NUMBERED_HEADING.match(line.text) and _is_capitalised_title(line, body_size):
                blocks.append(
                    Block(
                        kind=BlockKind.HEADING,
                        text=line.text.strip(),
                        level=minor_level,
                        page=line.page,
                        lines=[line],
                    )
                )
                continue

            # The block's line carries the text as the block shows it, so a
            # wrapped item can be rejoined, and its offsets kept, like a
            # paragraph's.
            list_item.lines = [replace(line, text=list_item.text)]
            blocks.append(list_item)
            continue

        if not paragraph and blocks and continues_list_item(blocks[-1], line, pitch):
            item = blocks[-1]
            item.lines.append(line)
            item.text = _join_wrapped(item.lines)
            continue

        if paragraph and starts_new_paragraph(paragraph[-1], line, pitch):
            flush_paragraph()

        paragraph.append(line)

    flush_paragraph()

    return blocks


def starts_new_paragraph(previous: TextLine, line: TextLine, pitch: float | None) -> bool:
    """Whether ``line`` begins a paragraph rather than continuing ``previous``.

    Without this, every run of text between two headings became one
    paragraph, however many paragraphs the page showed.
    """

    if abs(line.font_size - previous.font_size) > FONT_SIZE_TOLERANCE:
        return True

    # A clause number after a finished sentence opens the next clause. The
    # punctuation check matters: "nos termos do subitem" followed by a line
    # starting "2.6.4 deste edital" is one sentence wrapped.
    if _CLAUSE_START.match(line.text) and previous.text.rstrip().endswith((".", ":", ";")):
        return True

    # A paragraph can continue onto the next page, so the gap only counts
    # between lines on the same one.
    if pitch and line.page == previous.page:
        return line.top - previous.top > pitch * PARAGRAPH_GAP_RATIO

    return False


def continues_list_item(previous: Block, line: TextLine, pitch: float | None) -> bool:
    """Whether ``line`` is the wrapped end of the list item before it.

    A notice wraps long items, "a) ser brasileiro nato ou naturalizado ou
    cidadão português em condição de igualdade de direitos com" over "os
    brasileiros; ...", and splitting the two halves cuts a requirement in two.
    An item that already ended its sentence is finished, though, and what
    follows it is a paragraph of its own.
    """

    if previous.kind is not BlockKind.LIST_ITEM or not previous.lines:
        return False

    if previous.text.rstrip().endswith((".", ";", ":", "!", "?")):
        return False

    last = previous.lines[-1]

    if line.page not in (last.page, last.page + 1):
        return False

    return not starts_new_paragraph(last, line, pitch)


def _row_refs(table: Table) -> list[tuple[int, tuple[float, float, float, float] | None]]:
    if len(table.row_boxes) == len(table.rows):
        return [(table.page, box) for box in table.row_boxes]

    return [(table.page, None) for _ in table.rows]


def _in_reading_order(lines: list[TextLine], tables: list[Table]) -> Iterator[TextLine | Table]:
    """Interleave tables with lines by page and vertical position.

    Lines are already in reading order, so they are never reordered; each
    table is emitted just before the first line below it.
    """

    pending = sorted(tables, key=lambda table: (table.page, table.top))
    index = 0

    for line in lines:
        while index < len(pending) and _above(pending[index], line):
            yield pending[index]
            index += 1

        yield line

    yield from pending[index:]


def _above(table: Table, line: TextLine) -> bool:
    return (table.page, table.top) < (line.page, line.top)


def _as_heading(line: TextLine, scale: dict[float, int], body_size: float) -> Block | None:
    """Decide whether a line is a heading, and at what level."""

    text = line.text.strip()

    if not text or len(text.split()) > MAX_HEADING_WORDS:
        return None

    if text.endswith((";", ",")) or _TOC_ENTRY.search(text):
        return None

    numbered = _NUMBERED_HEADING.match(text)

    # A trailing full stop is strong evidence of a sentence, except on a line
    # in capitals or a numbered one, which is how many documents punctuate
    # their titles: "1. DISPOSIÇÕES INICIAIS."
    if text.endswith(".") and not (numbered or _is_capitalised(text)):
        return None

    size = round(line.font_size, 1)
    level = scale.get(size)

    if level is not None:
        return Block(kind=BlockKind.HEADING, text=text, level=level, page=line.page)

    if numbered and line.is_bold:
        # "2.1.3 Scope" nests three deep, so the depth comes from the
        # numbering. Body size numbered headings sit below every size based
        # level, so a "2.1" can never outrank the "2" it belongs to.
        depth = len(re.findall(r"\d+", numbered.group("numbering")))

        return Block(
            kind=BlockKind.HEADING,
            text=text,
            level=min(len(scale) + depth, 6),
            page=line.page,
        )

    # Bold body text on its own short line, with no terminal punctuation, is
    # the most common way word processors mark a minor heading.
    if line.is_bold and size >= body_size and len(text.split()) <= 8:
        return Block(
            kind=BlockKind.HEADING,
            text=text,
            level=min(len(scale) + 1, 6),
            page=line.page,
        )

    return None


def _is_capitalised(text: str) -> bool:
    """Whether every letter that has a case is a capital.

    Letter categories are used rather than ``str.isupper``, because the
    ordinal indicators in "5º ANO" and "1ª ETAPA" count as lowercase there.
    """

    categories = [unicodedata.category(character) for character in text]

    return categories.count("Lu") >= 3 and "Ll" not in categories


def _is_capitalised_title(line: TextLine, body_size: float) -> bool:
    text = line.text.strip()
    numbered = _NUMBERED_HEADING.match(text)
    title = numbered.group("text") if numbered else text
    words = title.split()
    wordy = [word for word in words if any(character.isalpha() for character in word)]

    return (
        _is_capitalised(text)
        and len(words) <= MAX_HEADING_WORDS
        and len(text.split()) >= 2
        # Words rather than figures: "PROFESSOR DE HISTÓRIA 20 R$ 2.565,32 2"
        # is a table row that table detection missed, not a title.
        and len(wordy) >= len(words) * 0.75
        and round(line.font_size, 1) >= body_size
        and not text.endswith((";", ","))
        and not _TOC_ENTRY.search(text)
    )


def _as_list_item(line: TextLine, indent_base: float) -> Block | None:
    text = line.text.strip()

    bullet = _BULLET_PATTERN.match(text)

    if bullet:
        return Block(
            kind=BlockKind.LIST_ITEM,
            text=bullet.group("text").strip(),
            indent=_indent_level(line, indent_base),
            page=line.page,
        )

    ordered = _ORDERED_PATTERN.match(text)

    if ordered:
        return Block(
            kind=BlockKind.LIST_ITEM,
            text=f"{ordered.group('marker')}. {ordered.group('text').strip()}",
            indent=_indent_level(line, indent_base),
            page=line.page,
        )

    lettered = _LETTER_PATTERN.match(text)

    # A single letter followed by a full stop is ambiguous: "a. Something" is
    # a list item, but so is the start of many sentences. Requiring an indent
    # keeps false positives down.
    if lettered and line.x0 > indent_base + 5:
        # The letter stays: a notice refers to "alínea b" of an item, so it
        # is content, not decoration like a bullet glyph.
        return Block(
            kind=BlockKind.LIST_ITEM,
            text=f"{lettered.group('marker')} {lettered.group('text').strip()}",
            indent=_indent_level(line, indent_base),
            page=line.page,
        )

    return None


def _detect_indent_base(lines: list[TextLine]) -> float:
    """The left margin of the document, used as the zero point for indents."""

    positions = [line.x0 for line in lines if not line.is_blank]

    return min(positions) if positions else 0.0


def _indent_level(line: TextLine, base: float) -> int:
    """How deeply a list item is nested, in steps of roughly one tab."""

    offset = max(0.0, line.x0 - base)

    return min(int(offset // 18), 4)


def _join_wrapped(lines: list[TextLine]) -> str:
    return join_wrapped(lines)[0]


def join_wrapped(lines: list[TextLine]) -> tuple[str, list[tuple[int, int]]]:
    """Rejoin lines that a PDF wrapped mid sentence.

    A PDF stores every visual line separately, so a paragraph arrives as five
    lines. Joining them with spaces restores the sentence, and a hyphen at a
    line end means the word itself was split.

    Returns the text and, for each line, the ``(start, end)`` range of the
    text that came from it, which is how a quote from the paragraph finds its
    way back to a line on the page. A word rejoined across a hyphen is split
    between the two ranges where the hyphen was.
    """

    text = ""
    spans: list[tuple[int, int]] = []

    for line in lines:
        piece = line.text.strip()

        if text.endswith("-") and not text.endswith("--"):
            text = text[:-1]
            start = len(text)
            spans[-1] = (spans[-1][0], len(text))
        else:
            if text and piece:
                text += " "
            start = len(text)

        text += piece
        spans.append((start, len(text)))

    return text, spans
