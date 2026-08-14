"""Tolerant MT5 parsing helpers."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

_NUM_RE = re.compile(r"[-+]?\d[\d\s,]*(?:\.\d+)?(?:[eE][-+]?\d+)?")


def decode_report_bytes(data: bytes) -> str:
    """Decode MT5 exports, including the UTF-16 HTML emitted by MT5."""

    if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise ValueError("Input is a Git LFS pointer, not a hydrated report")
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")
    # MT5 HTML is often UTF-16 with a BOM, but tolerate BOM-less UTF-16 too.
    if len(data) >= 4 and data[1:2] == b"\x00" and data[3:4] == b"\x00":
        return data.decode("utf-16-le")
    if len(data) >= 4 and data[0:1] == b"\x00" and data[2:3] == b"\x00":
        return data.decode("utf-16-be")
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_number(raw: object) -> float | None:
    """Parse MT5 numbers with spaces or commas as thousands separators."""

    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace("\u00a0", " ")
    if not text or text in {"-", ".", "—", "N/A", "NA"}:
        return None
    # Preserve a leading sign and decimal point while removing thousands
    # separators and surrounding labels such as ``12.4 (3.2%)``.
    match = _NUM_RE.search(text)
    if not match:
        return None
    token = match.group(0).replace(" ", "").replace(",", "")
    try:
        return float(token)
    except ValueError:
        return None


def parse_int(raw: object) -> int | None:
    value = parse_number(raw)
    return int(value) if value is not None else None


def parse_datetime(raw: object) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Drop a trailing UTC offset only when datetime.fromisoformat cannot parse
    # the original value; MT5 exports are otherwise deliberately timezone-naive.
    normalized = text.replace("T", " ").replace("/", ".")
    for fmt in (
        "%Y.%m.%d %H:%M:%S.%f",
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y.%m.%d",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
    ):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def first(values: Iterable[object]) -> object | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return value
    return None
