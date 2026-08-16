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
    PeriodAnalysisResult,
    analyze,
    analyze_file,
)
from .cache import AnalysisArtifact, AnalysisStore, PortfolioAnalysisArtifact
from .charts import (
    ChartConfig,
    render_correlation_heatmap,
    render_equity_drawdown_chart,
    save_correlation_heatmap,
    save_equity_drawdown_chart,
)
from .comparison import ReportComparison, TradeMismatch, compare_reports
from .correlation import CorrelationResults, DailyProfitCorrelationResult, DailyProfitPoint
from .config import AnalysisConfig, SharpeConfig
from .periods import (
    PeriodSuggestion,
    PeriodSuggestionResult,
    PeriodWindow,
    SamplePeriodConfig,
    suggest_sample_periods,
)
from .equity import CurveSeries, EquityCurve, build_equity
from .filters import (
    AllOf,
    AnyOf,
    FilterConfig,
    ForexSession,
    LongOnly,
    Not,
    OpenDateRangeFilter,
    SessionFilter,
    ShortOnly,
    TimeOfDayFilter,
    TradeFilter,
    TradeSelection,
    TradeSelectionRecord,
)
from .errors import (
    CurrencyMismatchError,
    FilterConfigurationError,
    FilterError,
    DuplicatePortfolioMemberError,
    PortfolioError,
    PortfolioValidationError,
    ReportError,
    ReportParseError,
    TimezoneMismatchError,
    SamplePeriodConfigurationError,
    SamplePeriodError,
    UnhydratedInputError,
    UnsupportedReportError,
    WhatIfConfigurationError,
    WhatIfError,
)
from .load import InputSource, load_report
from .matrices import AnalysisMatrix
from .metrics import Metrics, compute_metrics
from .models import AccountPoint, Report, Trade, TradeSide
from .what_if import InstrumentSpec, SizingAudit, WhatIfConfig, WhatIfResult, transform_report
from .portfolio import (
    AnalyzedPortfolioMember,
    PortfolioAnalysisResult,
    PortfolioConfig,
    PortfolioMember,
    PortfolioMemberResult,
    PortfolioPeriodResult,
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
    "render_correlation_heatmap",
    "save_correlation_heatmap",
    "AnalysisConfig",
    "InstrumentSpec",
    "SizingAudit",
    "WhatIfConfig",
    "WhatIfResult",
    "ChartConfig",
    "AnalysisMatrix",
    "AnalysisResult",
    "PeriodAnalysisResult",
    "AllOf",
    "AnyOf",
    "FilterConfig",
    "FilterConfigurationError",
    "FilterError",
    "ForexSession",
    "LongOnly",
    "Not",
    "OpenDateRangeFilter",
    "SessionFilter",
    "ShortOnly",
    "TimeOfDayFilter",
    "TradeFilter",
    "TradeSelection",
    "TradeSelectionRecord",
    "CurveSeries",
    "CorrelationResults",
    "DailyProfitCorrelationResult",
    "DailyProfitPoint",
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
    "PortfolioPeriodResult",
    "CurrencyMismatchError",
    "DuplicatePortfolioMemberError",
    "PortfolioError",
    "PortfolioValidationError",
    "Report",
    "ReportComparison",
    "ReportError",
    "ReportParseError",
    "SharpeConfig",
    "PeriodSuggestion",
    "PeriodSuggestionResult",
    "PeriodWindow",
    "SamplePeriodConfig",
    "SamplePeriodConfigurationError",
    "SamplePeriodError",
    "suggest_sample_periods",
    "Trade",
    "TradeMismatch",
    "TradeSide",
    "TimezoneMismatchError",
    "UnhydratedInputError",
    "UnsupportedReportError",
    "WhatIfConfigurationError",
    "WhatIfError",
    "analyze",
    "analyze_file",
    "build_equity",
    "compare_reports",
    "compute_metrics",
    "transform_report",
    "analyze_portfolio",
    "combine_analyses",
    "load_report",
    "run_monte_carlo",
    "run_monte_carlo_file",
]
