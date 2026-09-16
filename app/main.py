"""FastAPI application exposing the converter."""

from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app.api.validation import UploadLimits, UploadRejected, safe_output_name, validate_upload
from app.converters import pipeline
from app.converters.extractor import PdfExtractionError
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

app = FastAPI(
    title="PDF to Markdown",
    version="1.0.0",
    description=(
        "Converts a PDF into Markdown, inferring headings from font sizes and "
        "recovering lists and tables. Upload validation is strict: the file "
        "header is checked, not just the extension."
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
async def limits() -> dict[str, int]:
    return {
        "maxBytes": LIMITS.max_bytes,
        "maxPages": LIMITS.max_pages,
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
) -> ConvertResponse:
    data = await _read_and_validate(file)

    try:
        result = pipeline.convert(data, include_tables=include_tables, max_pages=LIMITS.max_pages)
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
    "/api/convert/download",
    response_class=PlainTextResponse,
    tags=["Conversion"],
    summary="Converts a PDF and returns it as a file download",
)
async def convert_download(file: UploadFile = File(...)) -> PlainTextResponse:
    data = await _read_and_validate(file)

    try:
        result = pipeline.convert(data, max_pages=LIMITS.max_pages)
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
            return PlainTextResponse(
                content=pipeline.convert_to_text(data, max_pages=LIMITS.max_pages),
                media_type="text/plain; charset=utf-8",
            )

        return pipeline.convert_to_json(data, max_pages=LIMITS.max_pages)
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
