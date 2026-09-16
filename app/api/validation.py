"""Upload validation.

An endpoint that accepts files from the internet is the part of this service
most worth being strict about. Every check below exists because the ones
before it can be lied to.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The first bytes of every PDF. The only check a caller cannot fake by
#: renaming a file or setting a header.
PDF_MAGIC = b"%PDF-"

ALLOWED_EXTENSIONS = {".pdf"}

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/x-pdf",
    # Browsers occasionally send this for a drag and drop upload.
    "application/octet-stream",
}


@dataclass(frozen=True)
class UploadLimits:
    max_bytes: int = 10 * 1024 * 1024
    max_pages: int = 200
    max_filename_length: int = 255


class UploadRejected(Exception):
    """The upload failed validation. The message is safe to show the caller."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def validate_filename(filename: str | None, limits: UploadLimits) -> str:
    if not filename:
        raise UploadRejected("A filename is required.", code="missing_filename")

    if len(filename) > limits.max_filename_length:
        raise UploadRejected(
            f"The filename is longer than {limits.max_filename_length} characters.",
            code="filename_too_long",
        )

    if not any(filename.lower().endswith(extension) for extension in ALLOWED_EXTENSIONS):
        raise UploadRejected("Only .pdf files are accepted.", code="invalid_extension")

    return filename


def validate_content_type(content_type: str | None) -> None:
    """Check the declared type, while treating it as a hint rather than proof.

    The client chooses this header, so it proves nothing on its own. It is
    checked anyway because rejecting an obviously wrong one early avoids
    reading the body at all.
    """

    if content_type is None:
        return

    # Strip any "; charset=" suffix a client may append.
    declared = content_type.split(";")[0].strip().lower()

    if declared not in ALLOWED_CONTENT_TYPES:
        raise UploadRejected(
            f"Content type '{declared}' is not accepted, expected application/pdf.",
            code="invalid_content_type",
        )


def validate_size(data: bytes, limits: UploadLimits) -> None:
    if not data:
        raise UploadRejected("The uploaded file is empty.", code="empty_file")

    if len(data) > limits.max_bytes:
        megabytes = limits.max_bytes / (1024 * 1024)

        raise UploadRejected(
            f"The file is larger than the {megabytes:.0f}MB limit.",
            code="file_too_large",
        )


def validate_magic_bytes(data: bytes) -> None:
    """The check that actually decides whether this is a PDF.

    The extension and the content type are both caller controlled. The file
    header is not, so it is the only one of the three that means anything.
    """

    if not data.startswith(PDF_MAGIC):
        raise UploadRejected(
            "The file does not look like a PDF. Its contents do not start with %PDF-.",
            code="not_a_pdf",
        )


def safe_output_name(filename: str) -> str:
    """Turn an uploaded filename into a safe download name.

    Path separators and traversal sequences are stripped, because this value
    ends up in a Content-Disposition header and must not let a caller suggest
    a path.
    """

    # Taking the last path segment is what defeats traversal. Removing ".."
    # as a raw substring would also mangle a legitimate "report..final.pdf",
    # and is unnecessary once the separators are gone.
    name = filename.replace("\\", "/").split("/")[-1].strip()

    if name.lower().endswith(".pdf"):
        name = name[: -len(".pdf")]

    safe = "".join(
        character if character.isalnum() or character in "-_ ." else "_"
        for character in name
    ).strip("._ ")

    return f"{safe or 'document'}.md"


def validate_upload(
    *,
    filename: str | None,
    content_type: str | None,
    data: bytes,
    limits: UploadLimits | None = None,
) -> str:
    """Run every check, cheapest first. Returns the validated filename."""

    limits = limits or UploadLimits()

    validated = validate_filename(filename, limits)
    validate_content_type(content_type)
    validate_size(data, limits)
    validate_magic_bytes(data)

    return validated
