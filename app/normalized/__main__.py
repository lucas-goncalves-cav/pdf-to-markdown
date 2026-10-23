"""Convert a PDF into a NormalizedDocument from the command line.

    python -m app.normalized edital.pdf --source-uri https://... --out edital.json

Useful for batch jobs and benchmarks, where going through HTTP adds nothing.
OCR runs when tesseract has the languages asked for, as in the API.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.converters.ocr import OcrSettings
from app.normalized.builder import normalize


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.normalized", description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--source-uri", help="Where the PDF came from, kept as provenance.")
    parser.add_argument("--out", type=Path, help="Write the JSON here instead of stdout.")
    parser.add_argument("--ocr-lang", default="por+eng", help="Tesseract languages.")
    parser.add_argument(
        "--no-ocr", action="store_true", help="Report scanned pages, do not read them."
    )
    parser.add_argument("--max-pages", type=int)
    args = parser.parse_args(argv)

    document = normalize(
        args.pdf.read_bytes(),
        source_uri=args.source_uri,
        filename=args.pdf.name,
        max_pages=args.max_pages,
        ocr=None if args.no_ocr else OcrSettings(languages=args.ocr_lang),
    )

    text = json.dumps(document, ensure_ascii=False, indent=1)

    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")

    quality = document["quality"]
    print(
        f"{document['document_id']} {quality['status']}: {quality['pages_converted']} pages, "
        f"{quality['block_count']} blocks, {quality['table_count']} tables, "
        f"{quality['section_count']} sections, coverage {quality['text_coverage']}",
        file=sys.stderr,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
