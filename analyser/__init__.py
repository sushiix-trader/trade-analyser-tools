"""Fast, deterministic MetaTrader 5 report analysis.

The public platform API deliberately separates parsing from analysis::

    from analyser import AnalysisConfig, analyze_file

    result = analyze_file("tester.htm", AnalysisConfig())
    result.metrics.custom_trade_event_sharpe
    result.monthly

Only completed single-run MT5 Strategy Tester HTML/XML reports are accepted;
optimization workbooks are rejected explicitly.
"""

from .analysis import (
    AnalysisResult,
    MonthlyDrawdown,
    MonthlyPerformance,
    MonthlyPerformanceTable,
    MonthlyPerformanceTableRow,
    analyze,
    analyze_file,
)
from .cache import AnalysisArtifact, AnalysisStore, PortfolioAnalysisArtifact
from .charts import render_equity_drawdown_chart, save_equity_drawdown_chart
from .comparison import ReportComparison, TradeMismatch, compare_reports
from .config import AnalysisConfig, SharpeConfig
from .equity import CurveSeries, EquityCurve, build_equity
from .errors import (
    CurrencyMismatchError,
    DuplicatePortfolioMemberError,
    PortfolioError,
    PortfolioValidationError,
    ReportError,
    ReportParseError,
    TimezoneMismatchError,
    UnhydratedInputError,
    UnsupportedReportError,
)
from .load import InputSource, load_report
from .matrices import AnalysisMatrix
from .metrics import Metrics, compute_metrics
from .models import AccountPoint, Report, Trade, TradeSide
from .portfolio import (
    AnalyzedPortfolioMember,
    PortfolioAnalysisResult,
    PortfolioConfig,
    PortfolioMember,
    PortfolioMemberResult,
    analyze_portfolio,
    combine_analyses,
)
from .simulations import MonteCarloConfig, MonteCarloResult, run_monte_carlo, run_monte_carlo_file

__all__ = [
    "AccountPoint",
    "AnalysisArtifact",
    "AnalysisStore",
    "render_equity_drawdown_chart",
    "save_equity_drawdown_chart",
    "AnalysisConfig",
    "AnalysisMatrix",
    "AnalysisResult",
    "CurveSeries",
    "EquityCurve",
    "InputSource",
    "Metrics",
    "MonteCarloConfig",
    "MonteCarloResult",
    "MonthlyDrawdown",
    "MonthlyPerformance",
    "MonthlyPerformanceTable",
    "MonthlyPerformanceTableRow",
    "AnalyzedPortfolioMember",
    "PortfolioAnalysisResult",
    "PortfolioAnalysisArtifact",
    "PortfolioConfig",
    "PortfolioMember",
    "PortfolioMemberResult",
    "CurrencyMismatchError",
    "DuplicatePortfolioMemberError",
    "PortfolioError",
    "PortfolioValidationError",
    "Report",
    "ReportComparison",
    "ReportError",
    "ReportParseError",
    "SharpeConfig",
    "Trade",
    "TradeMismatch",
    "TradeSide",
    "TimezoneMismatchError",
    "UnhydratedInputError",
    "UnsupportedReportError",
    "analyze",
    "analyze_file",
    "build_equity",
    "compare_reports",
    "compute_metrics",
    "analyze_portfolio",
    "combine_analyses",
    "load_report",
    "run_monte_carlo",
    "run_monte_carlo_file",
]
