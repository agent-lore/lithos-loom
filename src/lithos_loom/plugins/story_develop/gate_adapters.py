"""Per-tool output adapters: tool JSON -> :class:`~.gate_findings.GateFinding` (#132).

Each adapter parses one tool's structured (JSON) output and maps the tool's
native severity onto loom's ``minor|major|critical`` via an explicit, reviewable
table (ADR 0003 §5). The adapter owns the *machine* invocation (e.g.
``ruff check --output-format=json``); the human-readable command stays for
display and the ledger renders its own summary.

Parsing is defensive: malformed / truncated / empty output yields no findings
(an off-format tool must never crash the run). Adapters take the **full** tool
output as a string — sourcing that (vs the display tail) is the integration
slice's job.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .gate_findings import GateFinding

# --- severity-mapping tables (per tool; the reviewable part of ADR §5) --------

# bandit reports HIGH/MEDIUM/LOW issue severities.
_BANDIT_SEVERITY: dict[str, str] = {
    "HIGH": "critical",
    "MEDIUM": "major",
    "LOW": "minor",
}

# A known dependency vulnerability is at least major; CVSS-graded mapping
# (critical for high CVSS) is a follow-up once pip-audit surfaces scores.
_PIP_AUDIT_SEVERITY = "major"


def _ruff_severity(code: str) -> str:
    """ruff has no severity axis; ``W`` (warning) rules are minor, the rest major."""
    return "minor" if code.startswith("W") else "major"


# --- helpers ------------------------------------------------------------------


def _loads(output: str) -> Any:
    try:
        return json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# --- per-tool parsers ---------------------------------------------------------


def _parse_ruff(check: str, output: str) -> list[GateFinding]:
    data = _loads(output)
    if not isinstance(data, list):
        return []
    findings: list[GateFinding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "ruff-error")
        location = item.get("location") or {}
        findings.append(
            GateFinding(
                check=check,
                tool="ruff",
                rule=code,
                severity=_ruff_severity(code),
                message=str(item.get("message", "")),
                file=str(item.get("filename", "")),
                line=_int_or_none(
                    location.get("row") if isinstance(location, dict) else None
                ),
            )
        )
    return findings


def _parse_bandit(check: str, output: str) -> list[GateFinding]:
    data = _loads(output)
    if not isinstance(data, dict):
        return []
    findings: list[GateFinding] = []
    for result in data.get("results", []) or []:
        if not isinstance(result, dict):
            continue
        native = str(result.get("issue_severity", "")).upper()
        findings.append(
            GateFinding(
                check=check,
                tool="bandit",
                rule=str(result.get("test_id", "")),
                severity=_BANDIT_SEVERITY.get(native, "major"),
                message=str(result.get("issue_text", "")),
                file=str(result.get("filename", "")),
                line=_int_or_none(result.get("line_number")),
            )
        )
    return findings


def _parse_pip_audit(check: str, output: str) -> list[GateFinding]:
    data = _loads(output)
    # pip-audit --format=json is ``{"dependencies": [...]}`` on current versions
    # and a bare list on older ones; accept both.
    deps = data.get("dependencies") if isinstance(data, dict) else data
    if not isinstance(deps, list):
        return []
    findings: list[GateFinding] = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("name", ""))
        version = str(dep.get("version", ""))
        for vuln in dep.get("vulns", []) or []:
            if not isinstance(vuln, dict):
                continue
            vuln_id = str(vuln.get("id", ""))
            fixes = vuln.get("fix_versions") or []
            fix_note = (
                f" (fix: {', '.join(str(v) for v in fixes)})"
                if fixes
                else " (no fix available)"
            )
            findings.append(
                GateFinding(
                    check=check,
                    tool="pip-audit",
                    rule=vuln_id,
                    severity=_PIP_AUDIT_SEVERITY,
                    message=f"{name} {version}: {vuln_id}{fix_note}",
                    package=name,
                )
            )
    return findings


_PARSERS: dict[str, Callable[[str, str], list[GateFinding]]] = {
    "ruff": _parse_ruff,
    "bandit": _parse_bandit,
    "pip-audit": _parse_pip_audit,
}

# The tools whose output this module can structure. A check whose tool is not
# here keeps its raw output tail (no structured findings) — never an error.
SUPPORTED_TOOLS = frozenset(_PARSERS)


def parse_findings(check: str, tool: str, output: str) -> list[GateFinding]:
    """Parse *tool*'s *output* into findings for *check*; ``[]`` if unsupported,
    empty, or off-format (defensive — never raises on bad tool output)."""
    parser = _PARSERS.get(tool)
    if parser is None or not output.strip():
        return []
    return parser(check, output)
