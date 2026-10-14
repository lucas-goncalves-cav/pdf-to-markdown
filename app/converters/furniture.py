"""Running headers and footers: text a page template repeats on every page.

A PDF has no header element any more than it has a heading element, so this
is inference too. Two things identify running text, and both are required:

- It recurs: the same words at the same height on several pages. Page numbers
  change, so on a line that carries one the digits are ignored, which makes
  "Página 2 de 68" and "Página 3 de 68" the same line. Elsewhere digits count:
  "Chapter 2" opening page 2 is not a running header for "Chapter 3".
- It sits near an edge. Body text can repeat too, the same clause in two
  annexes for instance, but not usually at the top or bottom of three pages
  at exactly the same height.

The first page keeps its copy. A letterhead is often the only place that
names the organisation behind the document, and nothing comes before the
first page for it to interrupt.
"""

from __future__ import annotations

import re
from collections import defaultdict

from app.converters.models import TextLine

#: How far down from the top, as a share of the page height, a header may sit.
HEADER_ZONE = 0.2

#: How far up from the bottom a footer may sit.
FOOTER_ZONE = 0.15

#: How far, in points, the same running text may drift between pages.
POSITION_TOLERANCE = 4.0

#: Pages a line has to repeat on. A two page document only needs both.
MIN_REPEATS = 3

_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")

#: What a page number looks like: "Página 2", "pág. 2", "fls. 2", "2 de 68",
#: "2/68", "Page 2 of 4", or a number on its own, "2" or "- 2 -".
_PAGE_NUMBER = re.compile(
    r"\b(?:p[aá]g(?:ina)?|page|pg|folhas?|fls?)\b\.?\s*\d+"
    r"|\d+\s*(?:/|de|of)\s*\d+"
    r"|^\W*\d+\W*$",
    re.IGNORECASE,
)


def strip_running_text(
    lines: list[TextLine],
    page_heights: dict[int, float],
) -> tuple[list[TextLine], int]:
    """Remove running headers and footers. Returns the lines kept and how many went."""

    pages = sorted({line.page for line in lines})

    if len(pages) < 2:
        return lines, 0

    needed = min(MIN_REPEATS, len(pages))
    first_page = pages[0]

    candidates: dict[str, list[int]] = defaultdict(list)

    for index, line in enumerate(lines):
        height = page_heights.get(line.page)

        if height and _near_an_edge(line, height):
            candidates[signature(line.text)].append(index)

    running: set[int] = set()

    for indexes in candidates.values():
        for cluster in _clusters_by_height(indexes, lines):
            if len({lines[index].page for index in cluster}) >= needed:
                running.update(index for index in cluster if lines[index].page != first_page)

    kept = [line for index, line in enumerate(lines) if index not in running]

    return kept, len(running)


def signature(text: str) -> str:
    """The text as compared between pages, with spacing and page numbers normalised."""

    if _PAGE_NUMBER.search(text):
        text = _DIGITS.sub("#", text)

    return _SPACE.sub(" ", text).strip().lower()


def _near_an_edge(line: TextLine, page_height: float) -> bool:
    return line.top <= page_height * HEADER_ZONE or line.top >= page_height * (1 - FOOTER_ZONE)


def _clusters_by_height(indexes: list[int], lines: list[TextLine]) -> list[list[int]]:
    """Group occurrences of one text that sit at the same height."""

    ordered = sorted(indexes, key=lambda index: lines[index].top)
    clusters: list[list[int]] = []

    for index in ordered:
        if clusters and lines[index].top - lines[clusters[-1][0]].top <= POSITION_TOLERANCE:
            clusters[-1].append(index)
        else:
            clusters.append([index])

    return clusters
