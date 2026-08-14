"""Content-led parser selection."""

from __future__ import annotations

from .base import ReportParser
from .mt5_html import MT5HtmlParser
from .mt5_xml import MT5XmlParser


def detect_parser(source: str, filename: str = "") -> ReportParser:
    stripped = source.lstrip().lower()
    if stripped.startswith("<?xml") or stripped.startswith("<report") or "<report>" in stripped[:4096]:
        return MT5XmlParser()
    if "<html" in stripped[:4096] or "<!doctype html" in stripped[:4096]:
        return MT5HtmlParser()
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix == "xml":
        return MT5XmlParser()
    if suffix in {"htm", "html"}:
        return MT5HtmlParser()
    raise ValueError("Could not identify the input as MT5 XML or HTML")
