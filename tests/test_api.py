"""Tests for the HTTP API, with the upload validation given the most attention."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.api import validation
from app.converters import pipeline
from app.main import app
from tests import workloads

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

    def test_limits_say_whether_ocr_is_available(self) -> None:
        payload = client.get("/api/limits").json()

        assert isinstance(payload["ocrEnabled"], bool)
        assert payload["ocrMaxPages"] >= 0

    def test_the_openapi_schema_is_generated(self) -> None:
        response = client.get("/openapi.json")

        assert response.status_code == 200
        assert "/api/convert" in response.json()["paths"]


class TestNormalizedDocumentEndpoint:
    @staticmethod
    def post(data: bytes, *, headers: dict | None = None, **form):
        return client.post(
            "/api/normalized-document",
            files={"file": ("../../edital final.pdf", data, "application/pdf")},
            data=form,
            headers=headers or {},
        )

    def test_returns_the_contract(self, simple_pdf: bytes) -> None:
        response = self.post(simple_pdf, sourceUri="https://example.gov.br/edital.pdf")

        assert response.status_code == 200
        payload = response.json()
        assert payload["schema"] == "normalized-document"
        assert payload["source"]["uri"] == "https://example.gov.br/edital.pdf"
        assert payload["source"]["filename"] == "edital final.pdf"
        assert payload["blocks"][0]["text"] == "Quarterly Report"

    def test_the_api_limit_does_not_change_the_document_id(self, simple_pdf: bytes) -> None:
        from app.normalized import normalize

        response = self.post(simple_pdf)

        assert response.json()["document_id"] == normalize(simple_pdf)["document_id"]

    def test_echoes_the_callers_correlation_id(self, simple_pdf: bytes) -> None:
        response = self.post(simple_pdf, headers={"X-Correlation-Id": "corr-123"})

        assert response.headers["X-Correlation-Id"] == "corr-123"

    def test_replaces_a_correlation_id_it_should_not_echo(self, simple_pdf: bytes) -> None:
        response = self.post(simple_pdf, headers={"X-Correlation-Id": "bad id\twith spaces"})

        assert response.headers["X-Correlation-Id"] != "bad id\twith spaces"
        assert len(response.headers["X-Correlation-Id"]) == 32

    def test_rejects_a_source_that_is_not_a_url(self, simple_pdf: bytes) -> None:
        response = self.post(simple_pdf, sourceUri="file:///etc/passwd")

        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_source_uri"

    def test_validates_the_upload_like_every_other_route(self, not_a_pdf: bytes) -> None:
        response = self.post(not_a_pdf)

        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "not_a_pdf"


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

    def test_running_headers_are_stripped_by_default(self, running_text_pdf: bytes) -> None:
        response = upload(running_text_pdf)

        assert "Página 3 de 4" not in response.json()["content"]

    def test_header_stripping_can_be_switched_off(self, running_text_pdf: bytes) -> None:
        response = client.post(
            "/api/convert",
            files={"file": ("document.pdf", running_text_pdf, "application/pdf")},
            data={"stripHeaders": "false"},
        )

        assert response.status_code == 200
        assert "Página 3 de 4" in response.json()["content"]

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
        assert response.json()["warnings"] == []

    def test_json_output_carries_tables_as_rows(self, table_pdf: bytes) -> None:
        response = client.post(
            "/api/convert/json",
            files={"file": ("document.pdf", table_pdf, "application/pdf")},
        )

        tables = [block for block in response.json()["blocks"] if block["kind"] == "table"]

        assert tables[0]["rows"][1] == ["Teacher", "20 hours", "R$ 2.565,32"]

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


class TestTimeLimit:
    """A conversion past the limit is stopped and answered, and the service carries on."""

    @staticmethod
    def post(route: str, data: bytes, **headers):
        return client.post(
            route, files={"file": ("document.pdf", data, "application/pdf")}, headers=headers
        )

    @pytest.fixture
    def stuck(self, monkeypatch):
        """Every conversion spins forever, under a one second limit."""

        monkeypatch.setattr(main, "CONVERSION_TIMEOUT_SECONDS", 1)
        monkeypatch.setattr(main, "normalize", workloads.spin)

        for name in ("convert", "convert_to_text", "convert_to_json"):
            monkeypatch.setattr(pipeline, name, workloads.spin)

    def test_the_limit_is_discoverable(self) -> None:
        limits = client.get("/api/limits").json()

        assert limits["conversionTimeoutSeconds"] == main.CONVERSION_TIMEOUT_SECONDS

    def test_the_default_answers_before_the_automation_core_gives_up(self, monkeypatch) -> None:
        monkeypatch.delenv("CONVERSION_TIMEOUT_SECONDS", raising=False)

        assert main._positive_seconds("CONVERSION_TIMEOUT_SECONDS", 240) == 240
        assert main._positive_seconds("CONVERSION_TIMEOUT_SECONDS", 240) < 300

    def test_the_limit_comes_from_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("CONVERSION_TIMEOUT_SECONDS", "90")

        assert main._positive_seconds("CONVERSION_TIMEOUT_SECONDS", 240) == 90

    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_a_limit_below_one_second_is_refused(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv("CONVERSION_TIMEOUT_SECONDS", value)

        with pytest.raises(ValueError, match="CONVERSION_TIMEOUT_SECONDS"):
            main._positive_seconds("CONVERSION_TIMEOUT_SECONDS", 240)

    def test_the_normalized_document_route_answers_504(self, stuck, simple_pdf: bytes) -> None:
        response = self.post(
            "/api/normalized-document", simple_pdf, **{"X-Correlation-Id": "corr-504"}
        )

        assert response.status_code == 504
        assert response.json()["detail"] == {
            "code": "conversion_timeout",
            "message": "The conversion took longer than the 1 second limit and was stopped.",
            "timeoutSeconds": 1,
        }
        assert response.headers["X-Correlation-Id"] == "corr-504"

    @pytest.mark.parametrize(
        "route", ["/api/convert", "/api/convert/download", "/api/convert/txt", "/api/convert/json"]
    )
    def test_every_conversion_route_has_the_limit(
        self, stuck, route: str, simple_pdf: bytes
    ) -> None:
        response = self.post(route, simple_pdf)

        assert response.status_code == 504
        assert response.json()["detail"]["code"] == "conversion_timeout"

    def test_the_service_is_healthy_after_a_timeout(self, monkeypatch, simple_pdf: bytes) -> None:
        with monkeypatch.context() as patch:
            patch.setattr(main, "CONVERSION_TIMEOUT_SECONDS", 1)
            patch.setattr(main, "normalize", workloads.spin)

            assert self.post("/api/normalized-document", simple_pdf).status_code == 504

        assert client.get("/health").status_code == 200

        response = self.post("/api/normalized-document", simple_pdf)

        assert response.status_code == 200
        assert response.json()["blocks"][0]["text"] == "Quarterly Report"

    def test_a_conversion_that_dies_answers_500(self, monkeypatch, simple_pdf: bytes) -> None:
        monkeypatch.setattr(pipeline, "convert", workloads.exit_quietly)

        response = self.post("/api/convert", simple_pdf)

        assert response.status_code == 500
        assert response.json()["detail"]["code"] == "conversion_failed"


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
