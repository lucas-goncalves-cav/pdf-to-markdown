"""Tests for running each conversion in a process of its own, under a time limit.

What matters is what is left afterwards, so the tests that time out read the
process table in /proc and run on Linux only, which is where the service runs.
The conversions that misbehave are in ``tests/workloads.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from app import isolation
from app.converters import ocr, pipeline
from app.converters.extractor import PdfExtractionError
from app.converters.ocr import OcrSettings
from app.normalized import normalize
from tests import workloads
from tests.conftest import build_pdf, rasterise

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads the process table in /proc"
)

needs_tesseract = pytest.mark.skipif(
    ocr.unavailable_reason(OcrSettings(languages="eng")) is not None,
    reason="tesseract with English data is not installed",
)

#: The limit for the tests that time out: long enough for the child to start
#: and report what it started, short enough to keep the suite quick.
LIMIT = 5


def processes() -> dict[int, dict]:
    """Every process in the table, zombies included, with its state, parent and group."""

    table = {}

    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue

        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue

        # The command name is in parentheses and can contain anything,
        # parentheses included, so the fields are read after the last one.
        name = stat[stat.index("(") + 1 : stat.rindex(")")]
        state, ppid, pgid = stat[stat.rindex(")") + 2 :].split()[:3]
        table[int(entry.name)] = {
            "name": name,
            "state": state,
            "ppid": int(ppid),
            "pgid": int(pgid),
        }

    return table


def in_group(pgid: int) -> list[int]:
    return [pid for pid, process in processes().items() if process["pgid"] == pgid]


class TestConversionInAChild:
    def test_converts_a_pdf(self, simple_pdf: bytes) -> None:
        result = isolation.run(pipeline.convert, simple_pdf, timeout=60)

        assert "# Quarterly Report" in result["markdown"]
        assert result["page_count"] == 1

    def test_gives_the_same_document_as_converting_in_process(self, simple_pdf: bytes) -> None:
        document = isolation.run(
            normalize, simple_pdf, timeout=60, source_uri="https://x.gov.br/e.pdf"
        )

        assert document["document_id"] == normalize(simple_pdf)["document_id"]
        assert document["source"]["uri"] == "https://x.gov.br/e.pdf"

    def test_options_and_ocr_settings_reach_the_child(self) -> None:
        settings = OcrSettings(languages="por+eng", max_pages=7)

        answer = isolation.run(
            workloads.echo,
            b"%PDF-1.7",
            timeout=60,
            ocr=settings,
            max_pages=12,
            include_tables=False,
        )

        assert answer["size"] == len(b"%PDF-1.7")
        assert answer["ocr_type"] == "OcrSettings"
        assert answer["ocr"]["languages"] == "por+eng"
        assert answer["ocr"]["max_pages"] == 7
        assert answer["options"] == {"max_pages": 12, "include_tables": False}

    def test_the_child_writes_to_a_directory_of_its_own_that_is_removed(self) -> None:
        answer = isolation.run(workloads.echo, b"%PDF-1.7", timeout=60)

        assert Path(answer["tmpdir"]).name.startswith("conversion-")
        assert Path(answer["tmpdir"]).parent == Path(tempfile.gettempdir())
        assert not Path(answer["tmpdir"]).exists()

    def test_an_unreadable_pdf_is_reported_as_such(self) -> None:
        with pytest.raises(PdfExtractionError, match="no xref table"):
            isolation.run(workloads.unreadable, b"%PDF-1.7", timeout=60)

    def test_a_child_that_ends_without_an_answer_is_a_failure(self) -> None:
        with pytest.raises(isolation.ConversionFailed, match="exited with 3"):
            isolation.run(workloads.exit_quietly, b"%PDF-1.7", timeout=60)


@pytest.fixture(scope="class")
def timed_out(tmp_path_factory) -> dict:
    """A conversion that spins past the limit, with what it started and how long it took."""

    report = tmp_path_factory.mktemp("spin") / "report.json"
    started = time.monotonic()

    with pytest.raises(isolation.ConversionTimeout) as caught:
        isolation.run(workloads.spin, b"%PDF-1.7 scratch", timeout=LIMIT, report=str(report))

    return {
        "error": caught.value,
        "elapsed": time.monotonic() - started,
        "started": json.loads(report.read_text(encoding="utf-8")),
    }


@linux_only
class TestTimeLimit:
    def test_the_conversion_is_stopped_at_the_limit(self, timed_out: dict) -> None:
        assert LIMIT <= timed_out["elapsed"] < LIMIT + 2
        assert timed_out["error"].seconds == LIMIT
        assert "5 second limit" in str(timed_out["error"])

    def test_the_child_is_killed(self, timed_out: dict) -> None:
        worker = timed_out["started"]["worker"]

        assert worker == timed_out["error"].pid
        assert worker not in processes()

    def test_what_the_child_started_is_killed_and_reaped(self, timed_out: dict) -> None:
        grandchild = timed_out["started"]["grandchild"]

        assert grandchild not in processes()
        assert in_group(timed_out["error"].pid) == []

    def test_no_child_or_zombie_is_left_behind(self, timed_out: dict) -> None:
        mine = {pid: p for pid, p in processes().items() if p["ppid"] == os.getpid()}

        assert mine == {}

    def test_temporary_files_are_removed(self, timed_out: dict) -> None:
        assert not Path(timed_out["started"]["scratch"]).exists()
        assert not Path(timed_out["started"]["tmpdir"]).exists()

    def test_the_next_conversion_works(self, timed_out: dict, simple_pdf: bytes) -> None:
        result = isolation.run(pipeline.convert, simple_pdf, timeout=60)

        assert "# Quarterly Report" in result["markdown"]


@linux_only
class TestCleanupAfterACrash:
    def test_a_killed_child_does_not_leave_its_subprocesses_running(self, tmp_path: Path) -> None:
        report = tmp_path / "report.json"

        with pytest.raises(isolation.ConversionFailed, match="killed by signal 9"):
            isolation.run(workloads.crash, b"%PDF-1.7", timeout=60, report=str(report))

        started = json.loads(report.read_text(encoding="utf-8"))

        assert started["grandchild"] not in processes()
        assert in_group(started["worker"]) == []
        assert not Path(started["tmpdir"]).exists()


@linux_only
class TestAnAbandonedChildStopsItself:
    """The server enforces the limit. The child's watchdog covers the server being gone."""

    @staticmethod
    def start(tmp_path: Path, *, parent: int, deadline: float) -> tuple[subprocess.Popen, Path]:
        report = tmp_path / "report.json"
        header = {
            "function": "tests.workloads:spin",
            "options": {"report": str(report)},
            "correlation_id": None,
            "parent": parent,
            "deadline": deadline,
        }
        child = subprocess.Popen(
            [sys.executable, "-m", "app.isolation"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            cwd=isolation.ROOT,
            start_new_session=True,
        )
        child.stdin.write(json.dumps(header).encode() + b"\n%PDF-1.7")
        child.stdin.close()

        return child, report

    @staticmethod
    def emptied(group: int) -> bool:
        """Whether the group empties, reaping what this process inherits from it."""

        deadline = time.monotonic() + 5

        while in_group(group):
            if time.monotonic() > deadline:
                return False

            isolation._reap_group(group)
            time.sleep(0.05)

        return True

    def test_past_its_own_deadline(self, tmp_path: Path) -> None:
        child, report = self.start(tmp_path, parent=os.getpid(), deadline=2)

        assert child.wait(timeout=30) == -9
        assert json.loads(report.read_text(encoding="utf-8"))["worker"] == child.pid
        assert self.emptied(child.pid)

    def test_when_the_server_that_started_it_is_gone(self, tmp_path: Path) -> None:
        # A parent other than the one in the header means the server died and
        # the child was handed to someone else.
        child, _report = self.start(tmp_path, parent=os.getpid() + 1_000_000, deadline=600)

        assert child.wait(timeout=30) == -9
        assert self.emptied(child.pid)


@linux_only
@needs_tesseract
class TestTimeLimitDuringOcr:
    def test_tesseract_is_killed_with_the_conversion(self) -> None:
        line = (
            "The applicant shall submit every document listed in this notice before the deadline."
        )
        page = [{"text": f"{number}. {line}", "size": 11} for number in range(1, 40)]
        scan = rasterise(build_pdf([page] * 8), resolution=200)

        seen: set[tuple[int, int]] = set()
        done = threading.Event()

        def sample() -> None:
            while not done.is_set():
                seen.update(
                    (pid, p["pgid"]) for pid, p in processes().items() if p["name"] == "tesseract"
                )
                time.sleep(0.05)

        sampler = threading.Thread(target=sample)
        sampler.start()

        try:
            with pytest.raises(isolation.ConversionTimeout) as caught:
                isolation.run(
                    pipeline.convert,
                    scan,
                    timeout=6,
                    ocr=OcrSettings(languages="eng", max_pages=8),
                )
        finally:
            done.set()
            sampler.join()

        group = caught.value.pid
        ours = {pid for pid, pgid in seen if pgid == group}

        assert ours, "tesseract never ran, so the test proved nothing"
        assert not ours & processes().keys()
        assert in_group(group) == []
