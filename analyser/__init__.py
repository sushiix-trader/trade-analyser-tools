"""Fast, deterministic MetaTrader 5 report analysis.

The public platform API deliberately separates parsing from analysis::

    from analyser import AnalysisConfig, analyze_file

    result = analyze_file("tester.htm", AnalysisConfig())
    result.metrics.custom_trade_event_sharpe
    result.monthly

Only completed single-run MT5 Strategy Tester HTML/XML reports are accepted;
optimization workbooks are rejected explicitly.
"""

from .analysis import AnalysisResult, MonthlyDrawdown, MonthlyPerformance, analyze, analyze_file
from .comparison import ReportComparison, TradeMismatch, compare_reports
from .config import AnalysisConfig, SharpeConfig
from .equity import CurveSeries, EquityCurve, build_equity
from .errors import ReportError, ReportParseError, UnhydratedInputError, UnsupportedReportError
from .load import InputSource, load_report
from .metrics import Metrics, compute_metrics
from .models import AccountPoint, Report, Trade, TradeSide

__all__ = [
    "AccountPoint",
    "AnalysisConfig",
    "AnalysisResult",
    "CurveSeries",
    "EquityCurve",
    "InputSource",
    "Metrics",
    "MonthlyDrawdown",
    "MonthlyPerformance",
    "Report",
    "ReportComparison",
    "ReportError",
    "ReportParseError",
    "SharpeConfig",
    "Trade",
    "TradeMismatch",
    "TradeSide",
    "UnhydratedInputError",
    "UnsupportedReportError",
    "analyze",
    "analyze_file",
    "build_equity",
    "compare_reports",
    "compute_metrics",
    "load_report",
]
