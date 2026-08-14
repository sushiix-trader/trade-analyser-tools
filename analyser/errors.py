"""Public exceptions for invalid or unsupported report inputs."""

from __future__ import annotations


class ReportError(ValueError):
    """Base class for report input errors."""


class UnsupportedReportError(ReportError):
    """The input is recognized but is outside the single-run v1 contract."""


class UnhydratedInputError(ReportError):
    """The input is a Git LFS pointer rather than the report payload."""


class ReportParseError(ReportError):
    """The input could not be parsed as a supported report."""
