#!/usr/bin/env python3
"""Guard Git commits and pushes against sensitive data and raw report payloads.

The guard is intentionally dependency-free.  It is a safety net, not a claim
that heuristic scanning can prove that a file contains no confidential data.
The repository's committed HTML reports are limited to the allowlisted,
sanitized/synthetic example artifacts below.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import re
from pathlib import Path
import subprocess
import sys
from typing import Iterable, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ZERO_OID = "0" * 40
REPORT_SUFFIXES = frozenset({".html", ".htm", ".xml"})
WORKTREE_SKIP_DIRS = frozenset({
    ".git",
    ".venv",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
})

# These are deliberately narrow.  New report fixtures must be explicitly
# added here after they have been reviewed as sanitized or synthetic.
SAFE_REPORT_PATHS = frozenset({
    "results/current-interactive-report.html",
    "results/interactive-report.html",
    "results/interactive-portfolio-report.html",
    "results/synthetic-drawdown-report.html",
    "results/synthetic-portfolio-report.html",
    "results/sample_reports/example_strategy_a.html",
    "results/sample_reports/example_strategy_b.html",
    "results/sample_reports/example_strategy_c.html",
    "results/sample_reports/synthetic_drawdown_36_episodes.html",
    "results/sample_reports/synthetic_portfolio_strategy_a.html",
    "results/sample_reports/synthetic_portfolio_strategy_b.html",
})
SAFE_REPORT_MARKERS = (b"Example", b"Synthetic")

# High-confidence patterns only.  Findings report the category and path, never
# the matching value, so a failed hook does not echo a secret into a terminal
# transcript or CI log.
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("private key material", re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    (
        "GitHub token",
        re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    ("secret API key", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(rb"\bAIza[0-9A-Za-z_-]{20,}\b")),
    ("bearer token", re.compile(rb"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    (
        "credential assignment",
        re.compile(
            rb"(?i)(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)"
            rb"\s*[:=]\s*[\"']?[^\s\"']{8,}"
        ),
    ),
    (
        "email address",
        re.compile(rb"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
    (
        "user-specific absolute path",
        re.compile(rb"(?:(?:/home|/Users)/[A-Za-z0-9._-]+|[A-Za-z]:\\Users\\[^\\\s]+)"),
    ),
)


@dataclass(frozen=True)
class Finding:
    path: str
    reason: str


def _git(*args: str, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=REPOSITORY_ROOT,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        command = "git " + " ".join(args)
        raise RuntimeError(f"{command} failed{': ' + detail if detail else ''}")
    return completed.stdout


def _normalise_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _is_probably_binary(payload: bytes) -> bool:
    return b"\x00" in payload[:8192]


def _sensitive_findings(path: str, payload: bytes) -> list[Finding]:
    if _is_probably_binary(payload):
        return []
    return [Finding(path, reason) for reason, pattern in SENSITIVE_PATTERNS if pattern.search(payload)]


def _path_findings(path: str, payload: bytes) -> list[Finding]:
    path = _normalise_path(path)
    findings: list[Finding] = []
    if path.startswith("data/mt5_reports/") and path != "data/mt5_reports/README.md":
        findings.append(Finding(path, "raw local MT5 report path is not allowed; keep report payloads outside Git"))

    suffix = Path(path).suffix.lower()
    if suffix in REPORT_SUFFIXES and path not in SAFE_REPORT_PATHS:
        findings.append(Finding(path, "report-like HTML/XML file is not on the sanitized/synthetic allowlist"))
    elif path in SAFE_REPORT_PATHS and not any(marker in payload for marker in SAFE_REPORT_MARKERS):
        findings.append(Finding(path, "allowlisted report artifact is missing its expected example/synthetic marker"))

    # Catch a report renamed to a non-report extension, while excluding inline
    # test fixtures and application code that mention the MT5 table labels.
    inline_fixture_prefixes = ("tests/", "analyser/", "gui/", "examples/")
    if (
        path.startswith("results/")
        and not path.startswith(inline_fixture_prefixes)
        and b"Strategy Tester Report" in payload
        and b"Initial Deposit" in payload
        and path not in SAFE_REPORT_PATHS
    ):
        findings.append(Finding(path, "MT5 report signature is present in a non-allowlisted result"))
    return findings


def _scan_entries(entries: Iterable[tuple[str, bytes]]) -> list[Finding]:
    findings: list[Finding] = []
    for path, payload in entries:
        findings.extend(_path_findings(path, payload))
        findings.extend(_sensitive_findings(path, payload))
    # Keep output stable and avoid repeating the same category for a path.
    return sorted(set(findings), key=lambda finding: (finding.path, finding.reason))


def _report_findings(findings: Sequence[Finding], scope: str) -> int:
    if not findings:
        print(f"Repository guard passed ({scope}).")
        return 0
    print(f"Repository guard blocked {scope}:")
    for finding in findings:
        print(f"  - {finding.path}: {finding.reason}")
    print("Review or remove the flagged content before committing or pushing.")
    return 1


def _staged_entries() -> list[tuple[str, bytes]]:
    raw_paths = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    entries: list[tuple[str, bytes]] = []
    for raw_path in raw_paths.split(b"\0"):
        if not raw_path:
            continue
        path = raw_path.decode("utf-8", "surrogateescape")
        payload = _git("show", f":{path}")
        entries.append((path, payload))
    return entries


def check_pre_commit() -> int:
    # Git's own whitespace checker is cheap and catches accidental report or
    # generated-file damage before the privacy scan runs.
    _git("diff", "--cached", "--check")
    return _report_findings(_scan_entries(_staged_entries()), "staged content")


def _object_entries(object_lines: bytes) -> list[tuple[str, bytes]]:
    entries: list[tuple[str, bytes]] = []
    seen: set[tuple[str, str]] = set()
    for line in object_lines.decode("utf-8", "surrogateescape").splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        object_id, path = parts
        object_type = _git("cat-file", "-t", object_id).decode().strip()
        if object_type != "blob":
            continue
        key = (object_id, _normalise_path(path))
        if key in seen:
            continue
        seen.add(key)
        entries.append((path, _git("cat-file", "-p", object_id)))
    return entries


def _tree_entries(commitish: str) -> list[tuple[str, bytes]]:
    raw_paths = _git("ls-tree", "-r", "--name-only", "-z", commitish)
    entries: list[tuple[str, bytes]] = []
    for raw_path in raw_paths.split(b"\0"):
        if not raw_path:
            continue
        path = raw_path.decode("utf-8", "surrogateescape")
        entries.append((path, _git("show", f"{commitish}:{path}")))
    return entries


def _pre_push_entries(lines: Sequence[str]) -> list[tuple[str, bytes]]:
    entries: list[tuple[str, bytes]] = []
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            continue
        _local_ref, local_oid, _remote_ref, remote_oid = fields
        if local_oid == ZERO_OID:
            continue  # ref deletion has no new content to inspect
        entries.extend(_tree_entries(local_oid))
        if remote_oid == ZERO_OID:
            object_lines = _git("rev-list", "--objects", local_oid)
        else:
            object_lines = _git("rev-list", "--objects", local_oid, f"^{remote_oid}")
        entries.extend(_object_entries(object_lines))
    return entries


def check_pre_push() -> int:
    lines = sys.stdin.read().splitlines()
    if not lines:
        print("Repository guard skipped: Git supplied no ref updates to pre-push.")
        return 0
    return _report_findings(_scan_entries(_pre_push_entries(lines)), "outgoing push content")


def _working_tree_entries() -> list[tuple[str, bytes]]:
    entries: list[tuple[str, bytes]] = []
    for path in REPOSITORY_ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(REPOSITORY_ROOT).parts
        if any(part in WORKTREE_SKIP_DIRS for part in relative_parts):
            continue
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        entries.append((path.relative_to(REPOSITORY_ROOT).as_posix(), payload))
    return entries


def check_audit() -> int:
    history_findings = _scan_entries(_object_entries(_git("rev-list", "--objects", "--all")))
    working_tree_findings = _scan_entries(_working_tree_entries())
    findings = sorted(set(history_findings + working_tree_findings), key=lambda finding: (finding.path, finding.reason))
    return _report_findings(findings, "Git history and working tree")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("pre-commit", "pre-push", "audit"),
        help="run the relevant Git-hook check or audit history plus the working tree",
    )
    args = parser.parse_args(argv)
    try:
        if args.mode == "pre-commit":
            return check_pre_commit()
        if args.mode == "pre-push":
            return check_pre_push()
        return check_audit()
    except (OSError, RuntimeError, UnicodeError) as error:
        print(f"Repository guard could not complete: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
