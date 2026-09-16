"""Turns extracted lines into structured blocks.

A PDF has no headings, no lists and no paragraphs. It has glyphs at
coordinates. Everything below is inference, and the honest framing is that it
is a set of heuristics that work well on text documents produced by word
processors and less well on magazine layouts.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter

from app.converters.models import Block, BlockKind, TextLine

#: A line has to be this much larger than body text before size alone is taken
#: as evidence of a heading. Below it, ordinary variation in a paragraph would
#: promote random lines.
HEADING_SIZE_RATIO = 1.12

#: Headings are short. A long line in a large font is usually a pull quote or
#: a title page blurb, not a section heading.
MAX_HEADING_WORDS = 14

#: Bullets that word processors emit. The glyph varies by font.
BULLET_CHARACTERS = "•◦‣⁃∙·●○–—*-"

_BULLET_PATTERN = re.compile(rf"^\s*[{re.escape(BULLET_CHARACTERS)}]\s+(?P<text>.+)$")
_ORDERED_PATTERN = re.compile(r"^\s*(?P<marker>\d{1,3})[.)]\s+(?P<text>.+)$")
_LETTER_PATTERN = re.compile(r"^\s*(?P<marker>[a-zA-Z])[.)]\s+(?P<text>.+)$")

#: A numbered heading such as "2.1 Scope". Distinct from an ordered list item,
#: which has a single number.
_NUMBERED_HEADING = re.compile(r"^\s*(?P<numbering>\d+(?:\.\d+){1,4})\.?\s+(?P<text>\S.*)$")


def detect_body_size(lines: list[TextLine]) -> float:
    """The most common font size, which is the body text of the document.

    Using the mode rather than the mean matters: a document with a large title
    page would drag the mean up and suppress every real heading.
    """

    if not lines:
        return 0.0

    sizes = Counter(round(line.font_size, 1) for line in lines)

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


def to_blocks(lines: list[TextLine], tables_by_page: dict | None = None) -> list[Block]:
    """Classify every line and merge the ones that belong together."""

    if not lines:
        return []

    body_size = detect_body_size(lines)
    heading_scale = build_heading_scale(lines, body_size)
    indent_base = _detect_indent_base(lines)

    blocks: list[Block] = []
    paragraph: list[TextLine] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return

        blocks.append(
            Block(
                kind=BlockKind.PARAGRAPH,
                text=_join_wrapped(paragraph),
                page=paragraph[0].page,
            )
        )
        paragraph.clear()

    for line in lines:
        if line.is_blank:
            flush_paragraph()
            continue

        heading = _as_heading(line, heading_scale, body_size)

        if heading is not None:
            flush_paragraph()
            blocks.append(heading)
            continue

        item = _as_list_item(line, indent_base)

        if item is not None:
            flush_paragraph()
            blocks.append(item)
            continue

        paragraph.append(line)

    flush_paragraph()

    if tables_by_page:
        blocks = _insert_tables(blocks, tables_by_page)

    return blocks


def _as_heading(line: TextLine, scale: dict[float, int], body_size: float) -> Block | None:
    """Decide whether a line is a heading, and at what level."""

    text = line.text.strip()

    if not text or len(text.split()) > MAX_HEADING_WORDS:
        return None

    # A trailing full stop is strong evidence of a sentence, not a heading.
    if text.endswith((".", ";", ",")):
        return None

    size = round(line.font_size, 1)
    level = scale.get(size)

    if level is not None:
        return Block(kind=BlockKind.HEADING, text=text, level=level, page=line.page)

    numbered = _NUMBERED_HEADING.match(text)

    if numbered and line.is_bold:
        # "2.1.3 Scope" nests three deep, so the depth comes from the numbering
        # rather than the font size.
        depth = numbered.group("numbering").count(".") + 1

        return Block(
            kind=BlockKind.HEADING,
            text=text,
            level=min(depth, 6),
            page=line.page,
        )

    # Bold body text on its own short line, with no terminal punctuation, is
    # the most common way word processors mark a minor heading.
    if line.is_bold and size >= body_size and len(text.split()) <= 8:
        return Block(kind=BlockKind.HEADING, text=text, level=min(len(scale) + 1, 6), page=line.page)

    return None


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
        return Block(
            kind=BlockKind.LIST_ITEM,
            text=lettered.group("text").strip(),
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
    """Rejoin lines that a PDF wrapped mid sentence.

    A PDF stores every visual line separately, so a paragraph arrives as five
    lines. Joining them with spaces restores the sentence, and a hyphen at a
    line end means the word itself was split.
    """

    parts: list[str] = []

    for line in lines:
        text = line.text.strip()

        if parts and parts[-1].endswith("-") and not parts[-1].endswith("--"):
            parts[-1] = parts[-1][:-1] + text
        else:
            parts.append(text)

    return " ".join(part for part in parts if part).strip()


def _insert_tables(blocks: list[Block], tables_by_page: dict) -> list[Block]:
    """Append each page's tables after that page's text.

    Placing a table exactly where it appeared would require matching its
    bounding box against the surrounding lines. Grouping by page keeps the
    tables near their context without pretending to a precision the extraction
    does not have.
    """

    if not tables_by_page:
        return blocks

    result: list[Block] = []
    pages_seen: set[int] = set()

    def emit_tables_for(page: int) -> None:
        for rows in tables_by_page.get(page, []):
            if rows:
                result.append(Block(kind=BlockKind.TABLE, text="", rows=rows, page=page))

    for index, block in enumerate(blocks):
        previous_page = blocks[index - 1].page if index else block.page

        if block.page != previous_page and previous_page not in pages_seen:
            emit_tables_for(previous_page)
            pages_seen.add(previous_page)

        result.append(block)

    last_page = blocks[-1].page if blocks else None

    for page in sorted(tables_by_page):
        if page not in pages_seen and (last_page is None or page >= last_page):
            emit_tables_for(page)
            pages_seen.add(page)

    return result
