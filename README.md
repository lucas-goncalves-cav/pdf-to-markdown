# pdf-to-markdown

Converts a PDF into Markdown by inferring structure from typography, because a PDF does not have any.

![Python](https://img.shields.io/badge/Python-3.13-3776AB)
![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688)
![Tests](https://img.shields.io/badge/tests-129%20passing-success)
![Docker](https://img.shields.io/badge/Docker-Enabled-2496ED)
![License](https://img.shields.io/badge/license-MIT-green)

## Description

A PDF has no headings, no lists and no paragraphs. It has glyphs at coordinates. Everything a converter produces is
inference, and the interesting part of the problem is which inferences are worth making.

This service extracts text with its font metadata intact, classifies it into blocks, and renders Markdown. It also
handles the parts that break naive converters: words with no space characters between them, paragraphs split across
visual lines, hyphenated words, bullet glyphs that come back as `(cid:127)`, table text that would otherwise appear
twice, headers and footers repeated on every page, and scanned pages, which are read with OCR.

## Objective

Show the reasoning behind a document processing pipeline, and take the upload path seriously, since an endpoint that
accepts files from strangers is the part most worth being strict about.

## How it works

```
  PDF bytes
      |
      v
[ extractor ]   characters -> lines, keeping font size, weight and position
      |         tables found once per page, their text kept out of the lines
      |         scanned pages read with OCR when tesseract is available
      v
[ furniture ]   running headers and footers removed after the first page
      |
      v
[ structure ]   lines and tables -> blocks: headings, paragraphs, list items, tables
      |         font size drives the heading hierarchy, spacing the paragraphs
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
The mode is taken over characters rather than lines, so two headings cannot outvote two lines of body text on a short
page.

Size alone is not enough, so three guards stop false positives:

| Guard | Why |
| --- | --- |
| At least 12% larger than body | Ordinary variation inside a paragraph would otherwise promote random lines |
| At most 14 words | A long line in a large font is a pull quote, not a heading |
| No trailing `.` `;` `,` | Terminal punctuation is strong evidence of a sentence |

The full stop guard has one exception: a line in capitals, or a numbered one. Public notices punctuate their titles,
`1. DISPOSIÇÕES INICIAIS.`, and on a real 68 page notice the guard was rejecting every section title in it.

Three further cases are handled because word processors produce them constantly:

- A numbered heading like `2.1.3 Scope` takes its depth from the numbering, placed below every size based level, so
  an `8.1` can never outrank the `8.` it belongs to.
- A short bold line at body size becomes a minor heading.
- A line in capitals standing on its own as a paragraph, `CARGO: MERENDEIRA ESCOLAR.`, does too. It has to be mostly
  words, because `TOTAL 36 2 13 51` is a table row that table detection missed, not a title. Case comes from Unicode
  letter categories rather than `str.isupper`, which counts the ordinal in `5º ANO` as lowercase.

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

The opposite happens in justified text. Stretching a line widens the gap after a real space character too, which
used to read as a second space: a real notice came out with ten thousand double spaces. A space is now only implied
when neither neighbour already is one.

### Paragraphs arrive as separate lines

A PDF stores every visual line separately, so a paragraph arrives as five. Joining them with spaces restores the
sentence, and a hyphen at a line end means the word itself was split:

```python
if parts and parts[-1].endswith("-"):
    parts[-1] = parts[-1][:-1] + text   # "hyphen-" + "ated" -> "hyphenated"
```

### Paragraphs end where the spacing says

Joining lines is the easy half. A page shows where a paragraph ends with space, not with a character, so every run of
text between two headings used to come out as one paragraph. On the notice above, one of them was 158,000 characters
long.

A new paragraph starts when:

| Signal | Why |
| --- | --- |
| The gap to the previous line is 1.2 times the usual line spacing | Paragraph spacing is a third of a line or more |
| The font size changes by more than a point | A footnote or caption is not part of the paragraph above it |
| A clause number like `1.2.` follows a finished sentence | Contracts and notices number every paragraph |

The usual line spacing is a low percentile of the gaps rather than the median, because in a document of short
paragraphs most gaps are paragraph gaps. Line positions are kept unrounded for the same reason: lines are grouped on
a 2.5 point grid, and measuring gaps on that grid adds up to two and a half points of noise to a difference of four.

The clause rule checks the punctuation because `nos termos do subitem` followed by a line starting `2.6.4 deste
edital` is one sentence that happened to wrap before the number.

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

### Table text is emitted once, where the table is

pdfplumber finds a bordered table and returns its cells, but the characters inside it are still on the page, so they
also came out as lines. Every table appeared twice: once as a table, and once flattened into the text around it, with
the cells of a wrapped row interleaved.

Each page is now read once. Tables are found first, characters whose centre falls inside a table are left out of the
lines, and the table is placed in reading order by its position, between the text above and below it. Reading each
page once also halved the time on a 68 page document.

Two more details matter on real documents:

- A table broken by a page comes back as two tables. When nothing sits between them and the columns match, they are
  joined, and a header row repeated on the second page is dropped rather than kept as data.
- Empty columns are dropped, since merged cells leave whole columns of `None` behind, and a single column is not
  treated as a table at all: it is a framed paragraph, and reads better as one.

### Running headers and footers are recognised by recurrence

A page template repeats its header and footer on every page, and they end up in the middle of sentences that cross a
page break. A line is running text when both hold:

| Condition | Why |
| --- | --- |
| The same text at the same height, within 4 points, on at least three pages | Recurrence is what defines a running head |
| In the top 20% or bottom 15% of the page | Body text repeats too, a clause in each annex, but not at an edge |

Digits are ignored when comparing a line that carries a page number, so `Página 2 de 68` and `Página 3 de 68` match.
Elsewhere they count: `Chapter 2` opening page 2 is a heading, not a running header for `Chapter 3`. A test generating
exactly that layout caught the first version of this.

The first page keeps its copy. A letterhead is often the only place that names the organisation behind a document, and
nothing comes before the first page for it to interrupt. `stripHeaders=false` turns this off.

### A scan is read with OCR, page by page

A scanned page is a picture of text. When tesseract is installed, each page without a text layer is rendered at 300
dpi and read with OCR into the same line structure as the text layer, so the heading and paragraph heuristics run on
it unchanged. Scanning is decided per page, because the common case is a text document with a scanned annex, and a
page that is mostly image with a line or two of text, a digital signature stamp, still counts as scanned.

Two OCR details come from looking at real output:

- **Font size comes from tesseract's `x_size`, not the box.** A box measures the letters in it, so a title in capitals,
  with no descenders, came out a quarter smaller than body text set at the same size.
- **Lines below 50% confidence are dropped.** Ruled table cells come back as `[couror__[eranaseninas | wszomoo` at a
  confidence in the 30s, and an inflated size that turned them into headings. Prose comes back in the 80s and 90s.

OCR takes a second or more per page, so pages past `OCR_MAX_PAGES` are skipped, and conversion runs off the event loop
so a long job does not stop `/health` from answering.

### A scan is reported, not returned empty

Without OCR, a scanned page yields nothing. Returning an empty file with no explanation is the least helpful possible
outcome:

```json
{
  "content": "",
  "warnings": [
    "No selectable text was found. This PDF is most likely a scan, which needs OCR rather than text extraction."
  ]
}
```

The same goes for part of a document. A text document whose last pages are scanned says so, `Skipped scanned pages
41-44: they need OCR, which was not used.`, and pages that were read with OCR are flagged as such, since their text
can contain recognition errors.

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
| OCR | Tesseract, through pytesseract |
| Testing | pytest, generated PDFs via reportlab |
| Linting | ruff |
| Infrastructure | Docker, GitHub Actions |

## Project structure

```
app/
  converters/
    models.py      TextLine, Table and Block, the types the pipeline passes around
    extractor.py   PDF -> lines with font metadata and positioned tables, one pass per page
    ocr.py         scanned page -> lines, through tesseract's hOCR output
    furniture.py   running headers and footers, recognised by recurrence
    structure.py   lines and tables -> classified blocks, where the heuristics live
    renderer.py    blocks -> Markdown, with escaping
    pipeline.py    the stages wired together, and the warnings about scanned pages
  api/
    validation.py  upload checks and filename sanitisation
  main.py          FastAPI routes
  web.py           the browser UI, one self contained HTML page
tests/
  conftest.py         PDFs generated per test rather than committed as binaries
  test_conversion.py  extraction, heuristics and rendering
  test_ocr.py         hOCR parsing, and OCR end to end when tesseract is installed
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

OCR needs the tesseract binary as well, `apt install tesseract-ocr tesseract-ocr-por` on Debian and Ubuntu. Without
it everything else works, and scanned pages are reported instead of read.

### Tests

```bash
pytest
pytest --cov=app --cov-report=term-missing
```

129 tests. The PDFs they run against are built during the test rather than committed, so a test can state exactly
what went into the document and a failure names the heuristic that broke. Scanned PDFs are built the same way, by
rendering a generated PDF to images, and the OCR tests skip themselves when tesseract is not installed.

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

`/api/convert` takes two options as form fields:

| Field | Default | Effect |
| --- | --- | --- |
| `includeTables` | `true` | `false` skips table detection, the slowest part of extraction, and keeps table text as lines |
| `stripHeaders` | `true` | `false` keeps running headers and footers on every page |

`/api/limits` also reports `ocrEnabled` and `ocrMaxPages`, so a caller can tell whether a scan will be read before
sending one. `/api/convert/json` includes the same `warnings` as `/api/convert`.

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
| `OCR_ENABLED` | Read scanned pages with OCR when tesseract is installed | `true` |
| `OCR_LANG` | Tesseract languages, joined with `+` | `eng`, and `por+eng` in Docker Compose |
| `OCR_MAX_PAGES` | Scanned pages read per document; the rest are skipped and reported | `30` |

The Docker image installs tesseract with English and Portuguese. If `OCR_LANG` names a language whose data is not
installed, the startup log says so and scanned pages are reported rather than read.

## Where this does badly

Being explicit, because a converter that claims to handle everything is lying:

- **Scanned tables.** OCR reads prose well, but the ruling of a table confuses it. Low confidence lines are dropped,
  so a scanned table comes out as partial rows of text rather than as a table.
- **OCR structure.** Headings on scanned pages are approximate: font size is estimated and weight is unknown, so
  nothing is bold and two headings set alike can land a level apart.
- **Multi column layouts.** Lines are read top to bottom across the full page width, so a two column academic paper
  interleaves its columns. Fixing it means clustering by x position first.
- **Tables held together by whitespace.** pdfplumber infers tables from ruling lines and alignment. Bordered tables
  come out well; ones separated only by spacing often do not, and their rows read as lines of text.
- **Cells that wrap without a rule between them.** pdfplumber splits such a cell into one row per visual line, so a
  long cell can come out spread over several rows.
- **Running text that changes.** A header that differs by more than a page number, such as the current chapter's
  title, is matched text by text, so each version is only removed if it repeats on at least three pages.
- **Images.** Dropped entirely. There is nowhere sensible for them to go in a Markdown string.

## Roadmap

- [x] OCR fallback via Tesseract when no text is found
- [ ] Column detection for multi column layouts
- [x] Strip repeated headers and footers by detecting text that recurs at the same position
- [ ] Extract images and reference them from the Markdown
- [ ] Rate limiting, before this is exposed publicly

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for details.
