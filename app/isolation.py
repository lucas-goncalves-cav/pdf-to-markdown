"""Each conversion in a process of its own, under a time limit.

A conversion can run for minutes: table detection on a long notice, OCR on a
scan. In a thread it cannot be stopped, so a caller that gives up leaves it
running, holding CPU and memory, until it finishes on its own. A process can
be stopped.

Each conversion runs in a child started in a new session, which makes the
child and the tesseract processes it starts one process group. When the time
is up the whole group is killed, every process in it is reaped, and the
child's temporary directory is removed, so nothing of the conversion outlives
the request.

The child is ``python -m app.isolation``. It reads a JSON header line and the
PDF from stdin and writes one JSON answer to stdout. Its logs go to stderr,
which it shares with the server. JSON rather than pickle, because the child
is the process that parses untrusted files, and what it says back should not
be able to run code in the server.
"""

from __future__ import annotations

import contextlib
import ctypes
import dataclasses
import importlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.converters.extractor import PdfExtractionError
from app.converters.ocr import OcrSettings

logger = logging.getLogger(__name__)

#: The directory holding the ``app`` package. The child starts there, so it
#: imports the same code as the server.
ROOT = Path(__file__).resolve().parent.parent

#: How long past the limit the child waits for the server to stop it before
#: it stops itself. It only acts when the server could not.
GRACE_SECONDS = 5

#: How long to wait for killed processes to exit so they can be reaped.
REAP_SECONDS = 5

#: ``prctl`` option that hands orphaned descendants to this process rather
#: than to init. From ``linux/prctl.h``.
_PR_SET_CHILD_SUBREAPER = 36

_POSIX = hasattr(os, "killpg")


class ConversionTimeout(Exception):
    """The conversion ran past its limit, and it and everything it started were killed."""

    def __init__(self, seconds: float, pid: int) -> None:
        super().__init__(
            f"The conversion took longer than the {seconds:g} second limit and was stopped."
        )
        self.seconds = seconds
        #: The child's pid, which is also its process group.
        self.pid = pid


class ConversionFailed(Exception):
    """The child ended without an answer: killed by the kernel, or a bug."""


def run(
    function: Callable[..., Any],
    data: bytes,
    *,
    timeout: float,
    correlation_id: str | None = None,
    **options: Any,
) -> Any:
    """Call ``function(data, **options)`` in a child process, for ``timeout`` seconds at most.

    ``function`` has to be importable by its module and name, and return
    something JSON serialisable or a dataclass, which comes back as a dict.
    An :class:`OcrSettings` in ``options`` is passed through.

    Raises:
        PdfExtractionError: the child could not read the PDF.
        ConversionTimeout: the limit passed.
        ConversionFailed: the child ended without answering.
    """

    _become_subreaper()

    header = {
        "function": f"{function.__module__}:{function.__qualname__}",
        "options": {
            name: dataclasses.asdict(value) if dataclasses.is_dataclass(value) else value
            for name, value in options.items()
        },
        "correlation_id": correlation_id,
        "parent": os.getpid(),
        "deadline": timeout + GRACE_SECONDS,
    }

    # Everything the child, pytesseract and tesseract write goes here, so one
    # rmtree frees it however the child ended. In production /tmp is a 512 MB
    # tmpfs, and a killed pytesseract leaves its page images behind.
    workdir = tempfile.mkdtemp(prefix="conversion-")

    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "app.isolation"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            cwd=ROOT,
            env={**os.environ, "TMPDIR": workdir},
            start_new_session=True,
        )
    except OSError as error:
        # Out of pids or memory, most likely.
        shutil.rmtree(workdir, ignore_errors=True)

        raise ConversionFailed(f"The conversion process could not be started: {error}") from error

    try:
        output, _ = process.communicate(json.dumps(header).encode() + b"\n" + data, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ConversionTimeout(timeout, process.pid) from None
    finally:
        _stop(process)
        shutil.rmtree(workdir, ignore_errors=True)

    return _answer(output, process.returncode)


def _answer(output: bytes, returncode: int) -> Any:
    answer = None

    if returncode == 0:
        with contextlib.suppress(ValueError):
            answer = json.loads(output)

    if not isinstance(answer, dict) or not answer.keys() & {"result", "error"}:
        ending = (
            f"was killed by signal {-returncode}" if returncode < 0 else f"exited with {returncode}"
        )

        raise ConversionFailed(f"The conversion process {ending} without an answer.")

    if "error" in answer:
        raise PdfExtractionError(answer["message"])

    return answer["result"]


def _stop(process: subprocess.Popen) -> None:
    """Kill the child's process group, close the pipes and reap every process in it.

    Run however the child ended. After a clean exit the group is already
    empty and this costs a failed ``killpg``. After a crash it may still hold
    a tesseract the child started, which would otherwise run on unwatched.
    """

    if _POSIX:
        # ProcessLookupError: already gone. PermissionError is what macOS says
        # for a group whose only members are zombies.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        # Windows has no process groups. The child is all there is to kill.
        process.kill()

    for stream in (process.stdin, process.stdout):
        with contextlib.suppress(OSError):
            stream.close()

    process.wait()
    _reap_group(process.pid)


def _reap_group(pgid: int) -> None:
    """Reap what is left of a killed group, once the child itself is reaped.

    A killed tesseract outlives its parent by a moment and is handed to the
    nearest subreaper, which is this process: see :func:`_become_subreaper`.
    Unreaped, it would stay a zombie and keep one of the container's pids.
    """

    if not _POSIX:
        return

    deadline = time.monotonic() + REAP_SECONDS

    while True:
        try:
            pid, _status = os.waitpid(-pgid, os.WNOHANG)
        except ChildProcessError:
            return

        if pid:
            continue

        if time.monotonic() > deadline:
            logger.warning("Processes of conversion group %d did not exit to be reaped.", pgid)
            return

        time.sleep(0.01)


_subreaper_lock = threading.Lock()
_subreaper_set = False


def _become_subreaper() -> None:
    """Have orphaned descendants handed to this process, which reaps them.

    In the container the server is PID 1 and receives orphans anyway. Being a
    subreaper makes that true wherever it runs, under a process manager or a
    test runner, so the reaping in :func:`_reap_group` does not depend on how
    the server was started. Linux only, and it needs no capability.
    """

    global _subreaper_set

    with _subreaper_lock:
        if _subreaper_set or not sys.platform.startswith("linux"):
            return

        _subreaper_set = True

        try:
            libc = ctypes.CDLL(None, use_errno=True)

            if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
                logger.warning(
                    "Could not become a child subreaper: %s", os.strerror(ctypes.get_errno())
                )
        except (OSError, AttributeError) as error:
            logger.warning("Could not become a child subreaper: %s", error)


def _child() -> int:
    # The answer goes down a descriptor of its own, which subprocesses do not
    # inherit, and stdout becomes stderr. Nothing a library prints can corrupt
    # the answer, and the pipe closes the moment this process ends, however it
    # ends, instead of staying open in a tesseract that outlived it.
    channel = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    stdin = sys.stdin.buffer
    header = json.loads(stdin.readline())
    data = stdin.read()

    correlation_id = header.get("correlation_id")
    suffix = f" correlation_id={correlation_id}".replace("%", "%%") if correlation_id else ""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s" + suffix,
    )

    _watch(parent=header["parent"], seconds=header["deadline"])

    module, _, name = header["function"].partition(":")
    function = getattr(importlib.import_module(module), name)
    options = header["options"]

    if options.get("ocr") is not None:
        options["ocr"] = OcrSettings(**options["ocr"])

    try:
        result = function(data, **options)
    except PdfExtractionError as error:
        answer = {"error": "unreadable_pdf", "message": str(error)}
    else:
        if dataclasses.is_dataclass(result):
            result = dataclasses.asdict(result)

        answer = {"result": result}

    channel.write(json.dumps(answer, ensure_ascii=False).encode("utf-8"))
    channel.close()

    return 0


def _watch(*, parent: int, seconds: float) -> None:
    """Kill this process group if the server goes away, or long after the limit.

    The server enforces the limit. This is for when it cannot, because it was
    killed or restarted mid-conversion, which would otherwise leave the
    conversion running with nobody waiting for it.
    """

    deadline = time.monotonic() + seconds

    def watch() -> None:
        while os.getppid() == parent and time.monotonic() < deadline:
            time.sleep(0.5)

        logger.error("Stopping an abandoned conversion.")

        if _POSIX:
            os.killpg(0, signal.SIGKILL)

        os._exit(1)

    threading.Thread(target=watch, name="watchdog", daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(_child())
