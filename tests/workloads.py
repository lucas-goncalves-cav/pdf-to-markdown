"""Conversions that misbehave, for the isolation tests.

They run in the child process, which imports them by name, so they live in a
module of their own rather than in a test file.
"""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from app.converters.extractor import PdfExtractionError


def spin(data: bytes, *, report: str | None = None, **_options) -> None:
    """Burn CPU forever, leaving what a killed OCR leaves: a subprocess and a temporary file.

    What it started is written to ``report``, so a test can look for it in the
    process table afterwards.
    """

    grandchild = _sleeper()

    with tempfile.NamedTemporaryFile(prefix="tess_", suffix=".png", delete=False) as scratch:
        scratch.write(data)

    if report:
        _report(report, grandchild=grandchild.pid, scratch=scratch.name)

    while True:
        pass


def crash(data: bytes, *, report: str, **_options) -> None:
    """Die the way the kernel kills a process out of memory, with a subprocess still running."""

    grandchild = _sleeper()
    _report(report, grandchild=grandchild.pid)

    os.kill(os.getpid(), signal.SIGKILL)


def exit_quietly(data: bytes, **_options) -> None:
    """End without an answer."""

    os._exit(3)


def unreadable(data: bytes, **_options) -> None:
    raise PdfExtractionError("Could not read the PDF: no xref table.")


def echo(data: bytes, *, ocr=None, **options) -> dict:
    """What arrived in the child: the bytes, the options and the OCR settings."""

    return {
        "size": len(data),
        "ocr": dataclasses.asdict(ocr) if ocr is not None else None,
        "ocr_type": type(ocr).__name__,
        "options": options,
        "tmpdir": tempfile.gettempdir(),
    }


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])


def _report(path: str, **started) -> None:
    started |= {"worker": os.getpid(), "tmpdir": tempfile.gettempdir()}
    staging = Path(path).with_suffix(".partial")
    staging.write_text(json.dumps(started), encoding="utf-8")
    staging.replace(path)
