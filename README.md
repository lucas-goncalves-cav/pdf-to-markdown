# pdf-to-markdown

Converts a PDF into Markdown by inferring structure from typography, because a PDF does not have any.

![Python](https://img.shields.io/badge/Python-3.13-3776AB)
![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688)
![Tests](https://img.shields.io/badge/tests-75%20passing-success)
![Docker](https://img.shields.io/badge/Docker-Enabled-2496ED)
![License](https://img.shields.io/badge/license-MIT-green)

## Description

A PDF has no headings, no lists and no paragraphs. It has glyphs at coordinates. Everything a converter produces is
inference, and the interesting part of the problem is which inferences are worth making.

This service extracts text with its font metadata intact, classifies it into blocks, and renders Markdown. It also
handles the parts that break naive converters: words with no space characters between them, paragraphs split across
visual lines, hyphenated words, and bullet glyphs that come back as `(cid:127)`.

## Objective

Show the reasoning behind a document processing pipeline, and take the upload path seriously, since an endpoint that
accepts files from strangers is the part most worth being strict about.

## How it works

```
  PDF bytes
      |
      v
[ extractor ]   characters -> lines, keeping font size, weight and position
      |         inserts missing spaces, normalises unmappable glyphs
      v
[ structure ]   lines -> blocks: headings, paragraphs, list items, tables
      |         font size drives the heading hierarchy
      v
[ renderer ]    blocks -> Markdown, escaping syntax found in prose
      |
      v
   Markdown
```

Extraction deliberately does not decide what anything *means*. It reports what is on the page and leaves
classification to the next stage, which is what makes the heuristics testable without a PDF at all.

## The decisions worth explaining

### Headings come from the font size distribution

The most common font size in a document is its body text. Everything meaningfully larger is a heading, and the sizes
sort into levels:

```python
sizes = Counter(round(line.font_size, 1) for line in lines)
body_size = sizes.most_common(1)[0][0]
```

**The mode, not the mean.** A document with a large title page would drag the mean up and suppress every real heading.

Size alone is not enough, so three guards stop false positives:

| Guard | Why |
| --- | --- |
| At least 12% larger than body | Ordinary variation inside a paragraph would otherwise promote random lines |
| At most 14 words | A long line in a large font is a pull quote, not a heading |
| No trailing `.` `;` `,` | Terminal punctuation is strong evidence of a sentence |

Two further cases are handled because word processors produce them constantly: a numbered heading like `2.1.3 Scope`
takes its depth from the numbering rather than the font size, and a short bold line at body size becomes a minor
heading.

### PDFs do not always emit spaces

Concatenating the glyphs of a line frequently gives `thequickbrownfox`. PDFs position words rather than separating
them, so a space has to be inferred from the gap:

```python
gap = current["x0"] - previous["x1"]
threshold = max(previous["width"], current["width"]) * 0.3

if gap > threshold:
    parts.append(" ")
```

A test asserts that `Quarterly Report` survives the round trip, because without this it does not.

### Paragraphs arrive as separate lines

A PDF stores every visual line separately, so a paragraph arrives as five. Joining them with spaces restores the
sentence, and a hyphen at a line end means the word itself was split:

```python
if parts and parts[-1].endswith("-"):
    parts[-1] = parts[-1][:-1] + text   # "hyphen-" + "ated" -> "hyphenated"
```

### Unmappable glyphs are usually bullets

Fonts without a ToUnicode table produce `(cid:127)` instead of a character, and bullets are the usual victim because
they come from a symbol font. This is not an edge case: it happens with the default fonts of common PDF generators.

A leading one is almost certainly a list marker, so it becomes a bullet. Anywhere else it carries no information and
is dropped, rather than left in the output as literal `(cid:127)`.

### Markdown syntax in prose is escaped

A legal document containing `[1]` would render as a broken link. A price list containing `*` turns half a paragraph
into emphasis. Both are escaped:

```
See clause [1] and the * marked items.
->
See clause \[1\] and the \* marked items.
```

### A scan is reported, not returned empty

A PDF of scanned pages contains images, not text, so extraction yields nothing. Returning an empty file with no
explanation is the least helpful possible outcome:

```json
{
  "content": "",
  "warnings": [
    "No selectable text was found. This PDF is most likely a scan, which needs OCR rather than text extraction."
  ]
}
```

### Upload validation trusts only the file header

Three checks run, cheapest first, and only one of them means anything:

| Check | Can the caller lie about it |
| --- | --- |
| File extension | Yes, rename the file |
| `Content-Type` header | Yes, it is client supplied |
| **First bytes are `%PDF-`** | **No** |

The first two are still checked, because rejecting an obviously wrong request before reading the body is worth doing.
The magic byte check is the one that decides.

The download filename gets the same treatment, since it reaches a `Content-Disposition` header:

```
../../../etc/passwd.pdf  ->  passwd.md
C:\Windows\System32\config.pdf  ->  config.md
```

Taking the last path segment is what defeats traversal. An earlier version also stripped `..` as a raw substring,
which quietly mangled a legitimate `report..final.pdf` into `report.md`.

### Status codes distinguish the caller's fault from the file's

| Situation | Status |
| --- | --- |
| Wrong extension, wrong type, too large, not a PDF | 400 |
| Valid PDF header, corrupt contents | 422 |
| A scan with no text | 200 with a warning |

A 422 is the honest answer for a file that passed validation but could not be read: the request was well formed, the
content was not processable.

## Technologies

| Area | Stack |
| --- | --- |
| API | FastAPI, Uvicorn |
| Extraction | pdfplumber, pypdf |
| Testing | pytest, generated PDFs via reportlab |
| Linting | ruff |
| Infrastructure | Docker, GitHub Actions |

## Project structure

```
app/
  converters/
    models.py      TextLine and Block, the types the pipeline passes around
    extractor.py   PDF -> lines with font metadata, plus table detection
    structure.py   lines -> classified blocks, where the heuristics live
    renderer.py    blocks -> Markdown, with escaping
    pipeline.py    the three stages wired together
  api/
    validation.py  upload checks and filename sanitisation
  main.py          FastAPI routes
  web.py           the browser UI, one self contained HTML page
tests/
  conftest.py         PDFs generated per test rather than committed as binaries
  test_conversion.py  extraction, heuristics and rendering
  test_api.py         HTTP behaviour, with upload validation covered hardest
```

## How to run

### With Docker

```bash
git clone https://github.com/lucas-goncalves-cav/pdf-to-markdown.git
cd pdf-to-markdown

cp .env.example .env
docker compose up -d
```

| URL | What |
| --- | --- |
| http://localhost:8000 | Drag and drop interface |
| http://localhost:8000/docs | Swagger UI |
| http://localhost:8000/health | Liveness |

### Locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt

uvicorn app.main:app --reload
```

### Tests

```bash
pytest
pytest --cov=app --cov-report=term-missing
```

75 tests. The PDFs they run against are built during the test rather than committed, so a test can state exactly what
went into the document and a failure names the heuristic that broke.

## API

### Convert

```bash
curl -X POST http://localhost:8000/api/convert \
  -F "file=@document.pdf;type=application/pdf"
```

```json
{
  "filename": "document.md",
  "content": "# Technical Specification\n\n## 1. Overview\n\n...",
  "pageCount": 12,
  "blockCount": 87,
  "warnings": []
}
```

| Method | Route | Returns |
| --- | --- | --- |
| `POST` | `/api/convert` | JSON with the Markdown and metadata |
| `POST` | `/api/convert/download` | The `.md` as a file attachment |
| `POST` | `/api/convert/txt` | Plain text, structure applied but no syntax |
| `POST` | `/api/convert/json` | The block structure, for callers doing their own layout |
| `GET` | `/api/limits` | The limits this instance enforces |
| `GET` | `/health` | Liveness |

`includeTables=false` skips table detection, which is the slowest part of extraction.

### Error shape

```json
{
  "detail": {
    "code": "not_a_pdf",
    "message": "The file does not look like a PDF. Its contents do not start with %PDF-."
  }
}
```

| Code | Meaning |
| --- | --- |
| `missing_filename` | No filename was sent |
| `invalid_extension` | Not a `.pdf` |
| `invalid_content_type` | Declared type is not a PDF type |
| `empty_file` | Zero bytes |
| `file_too_large` | Over the configured limit |
| `not_a_pdf` | The header is not `%PDF-` |

## Configuration

| Variable | Description | Default |
| --- | --- | --- |
| `API_PORT` | Host port | `8000` |
| `MAX_UPLOAD_BYTES` | Largest accepted upload | `10485760` (10MB) |
| `MAX_PAGES` | Pages converted before truncating, with a warning | `200` |
| `ALLOWED_ORIGINS` | Comma separated CORS origins | `*` |

## Where this does badly

Being explicit, because a converter that claims to handle everything is lying:

- **Scanned PDFs.** No OCR, so an image only document yields nothing. This is detected and reported rather than
  returned as an empty file, but it is not solved.
- **Multi column layouts.** Lines are read top to bottom across the full page width, so a two column academic paper
  interleaves its columns. Fixing it means clustering by x position first.
- **Tables held together by whitespace.** pdfplumber infers tables from ruling lines and alignment. Bordered tables
  come out well; ones separated only by spacing often do not.
- **Table placement.** Tables are grouped by page rather than positioned exactly where they appeared, because doing
  better means matching bounding boxes against surrounding text.
- **Headers, footers and page numbers.** They are extracted like any other text and appear in the output.
- **Images.** Dropped entirely. There is nowhere sensible for them to go in a Markdown string.

## Roadmap

- [ ] OCR fallback via Tesseract when no text is found
- [ ] Column detection for multi column layouts
- [ ] Strip repeated headers and footers by detecting text that recurs at the same position
- [ ] Extract images and reference them from the Markdown
- [ ] Rate limiting, before this is exposed publicly

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for details.
