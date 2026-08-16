"""Input loading and report parsing."""

from __future__ import annotations

from pathlib import Path
import hashlib
from typing import BinaryIO

from .errors import ReportParseError, UnhydratedInputError, UnsupportedReportError
from .models import Report
from .parsers.registry import detect_parser
from .parsing_utils import decode_report_bytes

InputSource = str | Path | bytes | bytearray | memoryview | BinaryIO


def read_input(source: InputSource) -> tuple[bytes, str]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        return path.read_bytes(), str(path)
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source), "<bytes>"
    if hasattr(source, "read"):
        data = source.read()
        if isinstance(data, str):
            data = data.encode("utf-8")
        name = str(getattr(source, "name", "<file-like>"))
        return bytes(data), name
    raise TypeError(f"Unsupported report input type: {type(source)!r}")


def load_report(source: InputSource) -> Report:
    """Parse one supported MT5 single-run report without running analytics."""

    data, filename = read_input(source)
    if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise UnhydratedInputError(
            "The input is a Git LFS pointer, not the actual report payload"
        )
    try:
        text = decode_report_bytes(data)
        parser = detect_parser(text, filename)
        report = parser.parse(text)
    except (UnhydratedInputError, UnsupportedReportError):
        raise
    except Exception as exc:
        if isinstance(exc, ValueError) and "Git LFS pointer" in str(exc):
            raise UnhydratedInputError(str(exc)) from exc
        if isinstance(exc, (TypeError,)):  # preserve programmer errors
            raise
        raise ReportParseError(f"Could not parse MT5 report {filename}: {exc}") from exc
    report.source_file = filename
    report.metadata["input_sha256"] = hashlib.sha256(data).hexdigest()
    report.metadata["input_size"] = len(data)
    return report


__all__ = ["InputSource", "load_report", "read_input", "Report"]
