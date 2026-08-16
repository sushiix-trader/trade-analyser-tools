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


class PortfolioError(ValueError):
    """Base class for invalid portfolio combinations."""


class CurrencyMismatchError(PortfolioError):
    """Portfolio members do not use one common account currency."""


class TimezoneMismatchError(PortfolioError):
    """Portfolio members do not use one common report timezone."""


class DuplicatePortfolioMemberError(PortfolioError):
    """A portfolio contains a duplicate source or strategy name."""


class PortfolioValidationError(PortfolioError):
    """A portfolio warning was promoted to an error by strict validation."""


class FilterError(ValueError):
    """Base class for invalid trade-filter configurations."""


class FilterConfigurationError(FilterError):
    """A filter cannot be evaluated deterministically with the supplied context."""


class SamplePeriodError(ValueError):
    """Base class for invalid sample-period configurations."""


class SamplePeriodConfigurationError(SamplePeriodError):
    """A named sample-period configuration is incomplete or ambiguous."""
