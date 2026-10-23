"""Benchmark do conversor contra editais reais (docs/benchmark-editais.md).

    python scripts/benchmark_editais.py edital1.pdf edital2.pdf ...

Para cada PDF mede o que importa para o Edital Analyzer, comparando o NormalizedDocument
com o texto que o próprio PDF tem em cada página:

- perda e duplicação: palavras da página (hifenização de fim de linha desfeita, carimbo
  girado fora) que não chegaram, ou chegaram a mais, aos blocos e tabelas; rodapé e
  cabeçalho removidos de propósito não contam;
- ordem: semelhança entre a sequência de palavras da página e a do documento, só em
  páginas sem tabela (em tabela com célula de várias linhas a ordem por palavra não
  significa nada);
- o quality gate do próprio documento: status, cobertura e avisos.

Os PDFs não ficam no repositório: são dado de terceiros, baixados de fontes oficiais.
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.converters import pipeline
from app.normalized import normalize

TOKEN = re.compile(r"\w+", re.UNICODE)


def tokens(text: str) -> list[str]:
    return [token.lower() for token in TOKEN.findall(text)]


def page_tokens(path: Path) -> dict[int, list[str]]:
    pages: dict[int, list[str]] = {}

    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            upright = page.filter(
                lambda o: o.get("object_type") != "char" or o.get("upright", True)
            )
            text = re.sub(r"(\w)-\n\s*(\w)", r"\1\2", upright.extract_text() or "")
            pages[number] = tokens(text)

    return pages


def document_tokens(document: dict) -> dict[int, list[str]]:
    """Palavras do documento por página. Cada palavra do texto do bloco vai para a página do
    span em que ela começa: um parágrafo que cruza a página conta em cada uma, e a palavra
    que o conversor juntou num hífen de fim de linha conta inteira, como na fonte."""

    pages: dict[int, list[str]] = {}
    tables = {table["id"]: table for table in document["tables"]}

    for block in document["blocks"]:
        if block["type"] == "table":
            for row in tables[block["table_id"]]["rows"]:
                pages.setdefault(row["page"], []).extend(tokens(" ".join(row["cells"])))
            continue

        spans = block["spans"]

        for match in TOKEN.finditer(block["text"]):
            span = next((s for s in spans if s["start"] <= match.start() < s["end"]), spans[-1])
            pages.setdefault(span["page"], []).append(match.group().lower())

    return pages


def measure(path: Path) -> dict:
    data = path.read_bytes()
    started = time.perf_counter()
    document = normalize(data, filename=path.name)
    seconds = time.perf_counter() - started

    furniture = Counter(
        token for line in pipeline.extract(data).furniture for token in tokens(line.text)
    )
    source_pages = page_tokens(path)
    output_pages = document_tokens(document)
    source = Counter(token for page in source_pages.values() for token in page)
    output = Counter(token for page in output_pages.values() for token in page)
    lost = (source - output) - furniture
    duplicated = output - source
    table_pages = {row["page"] for table in document["tables"] for row in table["rows"]}
    order = sorted(
        SequenceMatcher(None, words, output_pages.get(page, []), autojunk=False).ratio()
        for page, words in source_pages.items()
        if words and page not in table_pages
    )
    quality = document["quality"]
    total = max(1, sum(source.values()))

    return {
        "file": str(path),
        "pages": quality["pages_total"],
        "seconds": round(seconds, 1),
        "status": quality["status"],
        "text_coverage": quality["text_coverage"],
        "warnings": [warning["code"] for warning in document["warnings"]],
        "sections": quality["section_count"],
        "tables": quality["table_count"],
        "lost_pct": round(100 * sum(lost.values()) / total, 2),
        "duplicated_pct": round(100 * sum(duplicated.values()) / total, 2),
        "order_median_prose_pages": round(order[len(order) // 2], 3) if order else None,
        "order_worst_prose_page": round(order[0], 3) if order else None,
        "top_lost": lost.most_common(8),
        "top_duplicated": duplicated.most_common(8),
    }


if __name__ == "__main__":
    for argument in sys.argv[1:]:
        print(json.dumps(measure(Path(argument)), ensure_ascii=False), flush=True)
