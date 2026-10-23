"""FastAPI application exposing the converter."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.api.validation import UploadLimits, UploadRejected, safe_output_name, validate_upload
from app.converters import ocr as ocr_engine
from app.converters import pipeline
from app.converters.extractor import PdfExtractionError
from app.normalized import normalize
from app.web import INDEX_HTML

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

LIMITS = UploadLimits(
    max_bytes=int(os.getenv("MAX_UPLOAD_BYTES", 10 * 1024 * 1024)),
    max_pages=int(os.getenv("MAX_PAGES", 200)),
)


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


#: ``None`` when OCR is switched off. When it is on but tesseract is missing,
#: the settings are still passed along, so that a scanned page is reported as
#: needing OCR the server cannot provide rather than silently dropped.
OCR = (
    ocr_engine.OcrSettings(
        languages=os.getenv("OCR_LANG", "eng"),
        max_pages=int(os.getenv("OCR_MAX_PAGES", 30)),
    )
    if _env_flag("OCR_ENABLED", True)
    else None
)

if OCR is not None and (reason := ocr_engine.unavailable_reason(OCR)):
    logger.warning("OCR is enabled but cannot run: %s. Scanned pages will be skipped.", reason)


def _ocr_ready() -> bool:
    return OCR is not None and ocr_engine.unavailable_reason(OCR) is None


app = FastAPI(
    title="PDF to Markdown",
    version="1.0.0",
    description=(
        "Converts a PDF into Markdown, inferring headings from font sizes and "
        "recovering lists and tables. Scanned pages are read with OCR when "
        "tesseract is installed. Upload validation is strict: the file header "
        "is checked, not just the extension."
    ),
)

# Wide open by default so the demo works from anywhere. A real deployment sets
# ALLOWED_ORIGINS to the sites that should be able to call it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class ConvertResponse(BaseModel):
    filename: str = Field(description="Suggested name for the converted file.")
    content: str = Field(description="The converted Markdown.")
    page_count: int = Field(alias="pageCount")
    block_count: int = Field(alias="blockCount")
    warnings: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> str:
    return INDEX_HTML


@app.get("/health", tags=["Health"], summary="Liveness probe")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/limits", tags=["Conversion"], summary="The limits this instance enforces")
async def limits() -> dict[str, int | bool]:
    return {
        "maxBytes": LIMITS.max_bytes,
        "maxPages": LIMITS.max_pages,
        "ocrEnabled": _ocr_ready(),
        "ocrMaxPages": OCR.max_pages if _ocr_ready() else 0,
    }


@app.post(
    "/api/convert",
    response_model=ConvertResponse,
    tags=["Conversion"],
    summary="Converts a PDF to Markdown",
    responses={
        400: {"description": "The upload failed validation."},
        422: {"description": "The file is a PDF but could not be read."},
    },
)
async def convert(
    file: UploadFile = File(description="The PDF to convert."),
    include_tables: bool = Form(True, alias="includeTables"),
    strip_headers: bool = Form(
        True,
        alias="stripHeaders",
        description="Remove running headers and footers repeated on every page.",
    ),
) -> ConvertResponse:
    data = await _read_and_validate(file)

    try:
        # Conversion is CPU bound and OCR can take minutes, so it runs off the
        # event loop, which keeps /health and other requests answering.
        result = await run_in_threadpool(
            pipeline.convert,
            data,
            include_tables=include_tables,
            max_pages=LIMITS.max_pages,
            strip_headers=strip_headers,
            ocr=OCR,
        )
    except PdfExtractionError as error:
        # The file passed validation, so it is a PDF, but something inside it
        # could not be read. That is 422 rather than 400: the request was
        # well formed, the content was not processable.
        raise HTTPException(status_code=422, detail=str(error)) from error

    logger.info(
        "Converted %s: %d pages, %d blocks.",
        file.filename,
        result.page_count,
        result.block_count,
    )

    return ConvertResponse(
        filename=safe_output_name(file.filename or "document.pdf"),
        content=result.markdown,
        pageCount=result.page_count,
        blockCount=result.block_count,
        warnings=result.warnings,
    )


@app.post(
    "/api/normalized-document",
    tags=["Conversion"],
    summary="Converts a PDF into a NormalizedDocument v1",
    description=(
        "The machine contract: ordered blocks, tables and sections, each traceable to a "
        "page and a box, with a quality gate. Schema in "
        "`schemas/normalized-document.v1.schema.json`. The same PDF converted the same way "
        "always has the same `document_id`."
    ),
    responses={
        400: {"description": "The upload failed validation."},
        422: {"description": "The file is a PDF but could not be read."},
    },
)
async def normalized_document(
    request: Request,
    file: UploadFile = File(description="The PDF to convert."),
    source_uri: str | None = Form(
        None,
        alias="sourceUri",
        max_length=2048,
        description="Where the PDF came from, kept as provenance. Not fetched.",
    ),
    include_tables: bool = Form(True, alias="includeTables"),
    strip_headers: bool = Form(True, alias="stripHeaders"),
) -> JSONResponse:
    # Callers pass their own id so a conversion can be found in both logs.
    correlation_id = _correlation_id(request.headers.get("X-Correlation-Id"))
    data = await _read_and_validate(file)

    if source_uri is not None and not source_uri.startswith(("https://", "http://")):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_source_uri", "message": "sourceUri must be an http(s) URL."},
        )

    try:
        document = await run_in_threadpool(
            normalize,
            data,
            source_uri=source_uri,
            filename=safe_output_name(file.filename or "document.pdf").removesuffix(".md") + ".pdf",
            include_tables=include_tables,
            strip_headers=strip_headers,
            max_pages=LIMITS.max_pages,
            ocr=OCR,
        )
    except PdfExtractionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    quality = document["quality"]
    logger.info(
        "Normalized %s as %s: %s, %d pages, %d blocks, coverage %s. correlation_id=%s",
        document["source"]["content_hash"],
        document["document_id"],
        quality["status"],
        quality["pages_converted"],
        quality["block_count"],
        quality["text_coverage"],
        correlation_id,
    )

    return JSONResponse(document, headers={"X-Correlation-Id": correlation_id})


def _correlation_id(value: str | None) -> str:
    """The caller's id when it is a sane token, a new one otherwise.

    It ends up in logs and a response header, so anything else is replaced
    rather than echoed.
    """

    if value and len(value) <= 128 and all(c.isalnum() or c in "-_.:" for c in value):
        return value

    return uuid.uuid4().hex


@app.post(
    "/api/convert/download",
    response_class=PlainTextResponse,
    tags=["Conversion"],
    summary="Converts a PDF and returns it as a file download",
)
async def convert_download(file: UploadFile = File(...)) -> PlainTextResponse:
    data = await _read_and_validate(file)

    try:
        result = await run_in_threadpool(
            pipeline.convert, data, max_pages=LIMITS.max_pages, ocr=OCR
        )
    except PdfExtractionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    name = safe_output_name(file.filename or "document.pdf")

    return PlainTextResponse(
        content=result.markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.post(
    "/api/convert/{output_format}",
    tags=["Conversion"],
    summary="Converts a PDF to another format",
    description="Supports `txt` for plain text and `json` for the block structure.",
)
async def convert_to_format(
    output_format: Literal["txt", "json"],
    file: UploadFile = File(...),
):
    data = await _read_and_validate(file)

    try:
        if output_format == "txt":
            text = await run_in_threadpool(
                pipeline.convert_to_text, data, max_pages=LIMITS.max_pages, ocr=OCR
            )

            return PlainTextResponse(content=text, media_type="text/plain; charset=utf-8")

        return await run_in_threadpool(
            pipeline.convert_to_json, data, max_pages=LIMITS.max_pages, ocr=OCR
        )
    except PdfExtractionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


async def _read_and_validate(file: UploadFile) -> bytes:
    """Read the upload into memory and run every validation check.

    Reading the whole file is acceptable at a 10MB limit. A service accepting
    larger files would stream to disk and validate the header from the first
    chunk instead.
    """

    data = await file.read()

    try:
        validate_upload(
            filename=file.filename,
            content_type=file.content_type,
            data=data,
            limits=LIMITS,
        )
    except UploadRejected as error:
        logger.warning("Rejected an upload (%s): %s", error.code, error)

        raise HTTPException(
            status_code=400,
            detail={"code": error.code, "message": str(error)},
        ) from error

    return data
