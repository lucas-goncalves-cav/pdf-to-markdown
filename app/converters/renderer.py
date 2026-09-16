"""Renders classified blocks as Markdown."""

from __future__ import annotations

import re

from app.converters.models import Block, BlockKind

#: Characters that would otherwise be read as Markdown syntax when they appear
#: in ordinary prose extracted from a PDF.
_ESCAPE_PATTERN = re.compile(r"([\\`*_\[\]<>|])")


def render(blocks: list[Block]) -> str:
    """Join blocks into a Markdown document with the right spacing."""

    parts: list[str] = []
    previous: Block | None = None

    for block in blocks:
        rendered = _render_block(block)

        if not rendered:
            continue

        if previous is not None:
            parts.append(_separator(previous, block))

        parts.append(rendered)
        previous = block

    return "".join(parts).strip() + "\n"


def _render_block(block: Block) -> str:
    match block.kind:
        case BlockKind.HEADING:
            level = block.level or 1

            return f"{'#' * level} {escape(block.text)}"

        case BlockKind.PARAGRAPH:
            return escape(block.text)

        case BlockKind.LIST_ITEM:
            indent = "  " * block.indent
            # An item already carrying "1." keeps its number; the rest get a
            # bullet. Renumbering ordered lists would silently change a
            # document that starts at 5.
            marker = "" if re.match(r"^\d+\.\s", block.text) else "- "

            return f"{indent}{marker}{escape(block.text)}"

        case BlockKind.TABLE:
            return _render_table(block.rows)

        case BlockKind.CODE:
            return f"```\n{block.text}\n```"

    return ""


def _render_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""

    header, *body = rows
    width = len(header)

    def render_row(cells: list[str]) -> str:
        padded = list(cells[:width]) + [""] * max(0, width - len(cells))

        # A pipe inside a cell would break the table, and escaping it is the
        # only thing that can be done about it.
        return "| " + " | ".join(cell.replace("|", "\\|") for cell in padded) + " |"

    lines = [render_row(header), "| " + " | ".join(["---"] * width) + " |"]
    lines.extend(render_row(row) for row in body)

    return "\n".join(lines)


def escape(text: str) -> str:
    """Escape Markdown syntax that appears in ordinary extracted prose.

    Without this, a legal document containing ``[1]`` renders as a broken link
    and a price list containing ``*`` turns half a paragraph into emphasis.
    """

    return _ESCAPE_PATTERN.sub(r"\\\1", text)


def _separator(previous: Block, current: Block) -> str:
    """How much blank space belongs between two blocks.

    Consecutive list items are a single list, so they get one newline.
    Everything else is a new block and gets a blank line.
    """

    if previous.kind is BlockKind.LIST_ITEM and current.kind is BlockKind.LIST_ITEM:
        return "\n"

    return "\n\n"
