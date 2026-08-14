"""MT5 single-run report parsers."""

from .base import ReportParser, normalise_trade
from .mt5_html import MT5HtmlParser
from .mt5_xml import MT5XmlParser
from .registry import detect_parser

__all__ = [
    "ReportParser",
    "normalise_trade",
    "MT5HtmlParser",
    "MT5XmlParser",
    "detect_parser",
]
