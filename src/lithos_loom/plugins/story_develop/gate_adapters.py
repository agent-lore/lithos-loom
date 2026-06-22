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

# --- machine-invocation flags (JSON output + decouple exit code from findings) -

# A finding-producing check runs in JSON mode so its output is parseable. Where
# the tool offers it we also pass its "exit 0 even with findings" flag (ruff /
# bandit: --exit-zero) so the check's *aggregate/display* verdict stays GREEN for
# the informational case. That flag is a display convenience, **not** the blocking
# mechanism: per ADR §5 a finding-producing check's exit code never decides
# approval — the ledger's severity policy does.
#
# pip-audit has no such flag (verified against the image: only -S/--strict, which
# is the *opposite*), so its process still exits non-zero on findings. That is
# harmless *only* as long as a required finding-producing check derives blocking
# from the ledger (``GateLedger.blocking``), never from ``CheckResult.passed`` /
# ``gate.passed`` — otherwise pip-audit's "non-zero on any hit" would silently
# decide approval, which ADR §5 forbids. Wiring that required-floor blocking off
# the ledger is #139; until then every finding-producing check here is
# informational, so the gate exit code is ignored for approval regardless.
_MACHINE_FLAGS: dict[str, str] = {
    "ruff": "--output-format=json --exit-zero",
    "bandit": "-f json --exit-zero",
    "pip-audit": "--format=json",  # no exit-zero flag; #139 must block via the ledger
}


def command_tool(command: str) -> str:
    """The real tool a *command* runs, past a ``uv run`` prefix and a pipeline producer.

    An env-dependent check resolves to ``uv run <tool>`` on a uv-managed repo (#165),
    so ``command.split()[0]`` is the ``uv`` entrypoint — but adapter selection
    (:data:`SUPPORTED_TOOLS`, :func:`machine_command`) and the floor's severity read
    need the underlying tool (``pip-audit``, ``pyright``). A compound command pipes a
    producer into the finding-owning tool (``uv export … | pip-audit …`` — #167
    dep-audit); the **consumer** (last pipe segment) is that tool. ``""`` for an empty
    command (an expected-but-absent placeholder)."""
    parts = command.rsplit("|", 1)[-1].split()
    if not parts:
        return ""
    if len(parts) >= 3 and parts[0] == "uv" and parts[1] == "run":
        return parts[2]
    return parts[0]


def machine_command(tool: str, base: str) -> str:
    """The machine (JSON) invocation of *base* for *tool*, or *base* unchanged
    for a tool with no adapter. E.g. ``ruff check`` ->
    ``ruff check --output-format=json --exit-zero``.

    *tool* is the real tool (see :func:`command_tool`), so a uv-wrapped adapter like
    ``uv run pip-audit`` still gets its JSON flags appended to the full command."""
    flags = _MACHINE_FLAGS.get(tool)
    return f"{base} {flags}" if flags else base


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
