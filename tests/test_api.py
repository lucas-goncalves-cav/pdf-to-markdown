"""Tests for the HTTP API, with the upload validation given the most attention."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import validation
from app.main import app

client = TestClient(app)


def upload(data: bytes, *, filename: str = "document.pdf", content_type: str = "application/pdf"):
    return client.post("/api/convert", files={"file": (filename, data, content_type)})


class TestHealthAndDocs:
    def test_health_responds(self) -> None:
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_the_ui_is_served_at_the_root(self) -> None:
        response = client.get("/")

        assert response.status_code == 200
        assert "PDF to Markdown" in response.text

    def test_limits_are_discoverable(self) -> None:
        response = client.get("/api/limits")

        assert response.status_code == 200
        assert response.json()["maxBytes"] > 0
        assert response.json()["maxPages"] > 0

    def test_the_openapi_schema_is_generated(self) -> None:
        response = client.get("/openapi.json")

        assert response.status_code == 200
        assert "/api/convert" in response.json()["paths"]


class TestConversionEndpoint:
    def test_converts_a_pdf(self, simple_pdf: bytes) -> None:
        response = upload(simple_pdf)

        assert response.status_code == 200

        payload = response.json()
        assert "# Quarterly Report" in payload["content"]
        assert payload["pageCount"] == 1
        assert payload["blockCount"] > 0

    def test_suggests_a_markdown_filename(self, simple_pdf: bytes) -> None:
        response = upload(simple_pdf, filename="Quarterly Report.pdf")

        assert response.json()["filename"] == "Quarterly Report.md"

    def test_a_scanned_pdf_returns_a_warning_rather_than_an_error(self, empty_pdf: bytes) -> None:
        response = upload(empty_pdf)

        assert response.status_code == 200

        payload = response.json()
        assert payload["content"].strip() == ""
        assert payload["warnings"]
        assert "OCR" in payload["warnings"][0]

    def test_tables_can_be_switched_off(self, simple_pdf: bytes) -> None:
        response = client.post(
            "/api/convert",
            files={"file": ("document.pdf", simple_pdf, "application/pdf")},
            data={"includeTables": "false"},
        )

        assert response.status_code == 200

    def test_downloading_returns_a_markdown_attachment(self, simple_pdf: bytes) -> None:
        response = client.post(
            "/api/convert/download",
            files={"file": ("report.pdf", simple_pdf, "application/pdf")},
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert 'filename="report.md"' in response.headers["content-disposition"]
        assert "# Quarterly Report" in response.text

    def test_plain_text_output(self, simple_pdf: bytes) -> None:
        response = client.post(
            "/api/convert/txt",
            files={"file": ("document.pdf", simple_pdf, "application/pdf")},
        )

        assert response.status_code == 200
        assert "Quarterly Report" in response.text
        assert "#" not in response.text

    def test_json_output_exposes_blocks(self, simple_pdf: bytes) -> None:
        response = client.post(
            "/api/convert/json",
            files={"file": ("document.pdf", simple_pdf, "application/pdf")},
        )

        assert response.status_code == 200
        assert response.json()["blocks"]

    def test_an_unknown_output_format_is_rejected(self, simple_pdf: bytes) -> None:
        response = client.post(
            "/api/convert/docx",
            files={"file": ("document.pdf", simple_pdf, "application/pdf")},
        )

        assert response.status_code == 422


class TestUploadValidation:
    """The checks that matter, because this endpoint takes files from anyone."""

    def test_rejects_a_missing_file(self) -> None:
        assert client.post("/api/convert").status_code == 422

    def test_rejects_an_empty_file(self) -> None:
        response = upload(b"")

        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "empty_file"

    def test_rejects_a_non_pdf_extension(self, simple_pdf: bytes) -> None:
        response = upload(simple_pdf, filename="document.exe")

        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_extension"

    def test_rejects_an_unexpected_content_type(self, simple_pdf: bytes) -> None:
        response = upload(simple_pdf, content_type="text/html")

        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_content_type"

    def test_rejects_a_file_that_is_not_a_pdf_despite_its_name(self, not_a_pdf: bytes) -> None:
        """The check that actually decides.

        A caller controls the extension and the content type, so only the file
        header proves anything.
        """

        response = upload(not_a_pdf, filename="totally-a.pdf")

        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "not_a_pdf"

    def test_rejects_a_file_over_the_size_limit(self) -> None:
        oversized = b"%PDF-" + b"0" * (11 * 1024 * 1024)

        response = upload(oversized)

        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "file_too_large"

    def test_a_pdf_that_cannot_be_parsed_is_unprocessable_not_a_bad_request(self) -> None:
        # Correct header, corrupt body: the request was well formed, the
        # content was not processable.
        response = upload(b"%PDF-1.4\nbut the rest is garbage")

        assert response.status_code == 422


class TestFilenameSanitisation:
    """The output name reaches a Content-Disposition header, so it must not
    let a caller suggest a path."""

    @pytest.mark.parametrize(
        ("uploaded", "expected"),
        [
            ("report.pdf", "report.md"),
            ("Quarterly Report.pdf", "Quarterly Report.md"),
            ("../../etc/passwd.pdf", "passwd.md"),
            ("C:\\Windows\\System32\\config.pdf", "config.md"),
            ("with;semicolon.pdf", "with_semicolon.md"),
            ('quote".pdf', "quote.md"),
            ("report..final.pdf", "report..final.md"),
            ("....pdf", "document.md"),
            ("", "document.md"),
        ],
    )
    def test_produces_a_safe_name(self, uploaded: str, expected: str) -> None:
        assert validation.safe_output_name(uploaded) == expected

    def test_a_traversal_attempt_does_not_reach_the_header(self, simple_pdf: bytes) -> None:
        response = client.post(
            "/api/convert/download",
            files={"file": ("../../../etc/passwd.pdf", simple_pdf, "application/pdf")},
        )

        disposition = response.headers["content-disposition"]

        assert "/" not in disposition.split("filename=")[1]
        assert ".." not in disposition
